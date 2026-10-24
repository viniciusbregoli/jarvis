"""Background jobs the voice agent hands off: Claude Code for files, code, PRs and research; Codex computer use for
clicking around apps. Jobs read and search by default; "edit" jobs may also change files, but only inside one folder,
which is snapshotted first so "desfaz" can put it back."""

import itertools
import json
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from AppKit import NSWorkspace

JOB_TIMEOUT = 20 * 60
SNAPSHOTS = Path.home() / ".jarvis/snapshots"
MAX_SNAPSHOT_BYTES = 300 * 1024 * 1024  # an edit job's folder is copied first; anything bigger is too broad a target
EDIT_TOOLS = ["Edit(./**)", "Write(./**)"]  # relative to the job's folder, its working directory
LOOKUP_TIMEOUT = 90

# Read and search only. Claude Code also allows commands it classifies as read-only (ls, cat, date) on its own;
# in dontAsk mode everything else is refused.
READ_ONLY_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Bash(cd *)", "Bash(mdfind *)", "Bash(mdls *)", "Bash(stat *)",
    "Bash(git log *)", "Bash(git status *)", "Bash(git diff *)", "Bash(git show *)",
    "Bash(gh pr list *)", "Bash(gh pr view *)", "Bash(gh pr checks *)", "Bash(gh pr diff *)",
    "Bash(gh run list *)", "Bash(gh run view *)", "Bash(gh issue list *)", "Bash(gh issue view *)",
]  # fmt: skip
# Flags that make otherwise read-only commands write files.
WRITE_FLAGS = ["Bash(git log --output*)", "Bash(git diff --output*)", "Bash(git show --output*)"]

SPOKEN_ANSWER = (
    "Your final answer is spoken aloud by a voice assistant: one to three short sentences in Brazilian Portuguese, "
    "no markdown, no lists, no long paths or URLs."
)
JOB_PROMPT = (
    "You are running as a background job for Jarvis, Vinicius's voice assistant on his Mac. Work on your own and "
    "never ask questions. You can only read and search: never try to change, send, delete or create anything; if "
    "the task needs that, say it isn't enabled yet. To find files, prefer mdfind (Spotlight), one simple query per call: never put || or && inside a query. "
    + SPOKEN_ANSWER
    + " If you found files or folders, end with one last line starting with 'Paths:' and their full absolute paths, "
    "separated by ' | '; Jarvis uses that line to open them and never reads it aloud."
)
EDIT_PROMPT = (
    "You are running as a background job for Jarvis, Vinicius's voice assistant on his Mac. Work on your own and "
    "never ask questions. Your working directory is the folder of the document he is working on: you may edit and "
    "create files only inside it. Make exactly the change he asked for and nothing else; if the request is unclear "
    "(e.g. which slide), pick the most likely reading and say which one you changed. " + SPOKEN_ANSWER
)
DESKTOP_PROMPT = (
    "Use computer use to do this on the Mac: {task}\n\nNever send or post messages, buy, delete, or submit forms; if "
    "the task needs that, stop and explain. " + SPOKEN_ANSWER
)


def claude_command(prompt: str, resume: str | None = None, edit: bool = False) -> list[str]:
    # Haiku: a file search took a median 19 s against 55 s on Sonnet, with the same answer. --strict-mcp-config skips
    # starting every MCP server, whose tools read-only jobs can't call anyway.
    command = [
        "claude", "-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(READ_ONLY_TOOLS + (EDIT_TOOLS if edit else [])),
        "--disallowedTools", ",".join(WRITE_FLAGS),
        "--append-system-prompt", EDIT_PROMPT if edit else JOB_PROMPT,
        "--model", "claude-haiku-5-5", "--strict-mcp-config",
        # No settings files: your own allow rules (ssh, sshpass, MCP tools) would otherwise apply to jobs too.
        "--setting-sources", "",
    ]  # fmt: skip
    return command + ["--resume", resume] if resume else command


def parse_claude(stdout: str) -> tuple[str, str | None, bool]:
    """(answer, session id, ok) from `claude -p --output-format json`."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "A tarefa falhou sem resposta.", None, False
    return (data.get("result") or "").strip(), data.get("session_id"), not data.get("is_error")


@dataclass
class Job:
    id: int
    kind: str  # "computer" (Claude Code), "edit" (Claude Code writing inside `folder`) or "desktop" (Codex)
    task: str
    status: str = "running"  # running, done, failed, cancelled
    result: str = ""
    session_id: str | None = None
    started: float = field(default_factory=time.time)
    ended: float | None = None
    folder: Path | None = None  # edit jobs: the only place they may write
    snapshot: Path | None = None  # edit jobs: the folder as it was before
    on_success: object = field(default=None, repr=False)  # edit jobs: e.g. reload the document
    process: subprocess.Popen | None = field(default=None, repr=False)


class Jobs:
    def __init__(self):
        self.all: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self.on_done = lambda job: None

    def start(self, kind: str, task: str, resume: str | None = None, **fields) -> Job:
        job = Job(next(self._ids), kind, task, **fields)
        self.all[job.id] = job
        threading.Thread(target=self._run, args=(job, resume), daemon=True).start()
        return job

    def _run(self, job: Job, resume: str | None):
        with tempfile.TemporaryDirectory() as tmp:
            answer = Path(tmp, "answer.txt")
            if job.kind == "desktop":
                command = ["codex", "exec", "--skip-git-repo-check", "-s", "read-only", "-o", str(answer)]
                command.append(DESKTOP_PROMPT.format(task=job.task))
            else:
                command = claude_command(job.task, resume, edit=job.kind == "edit")
            cwd = {"desktop": tmp, "edit": job.folder}.get(job.kind, Path.home())
            # stdin closed: claude -p reads any inherited input as part of the prompt.
            try:
                job.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as error:
                job.status, job.result, job.ended = "failed", f"Não consegui iniciar a tarefa: {error}", time.time()
                self.on_done(job)
                return
            if job.status == "cancelled":  # cancelled while it was starting
                job.process.terminate()
                return
            try:
                stdout, _ = job.process.communicate(timeout=JOB_TIMEOUT)
            except subprocess.TimeoutExpired:
                job.process.kill()
                job.status, job.result = "failed", "Demorou mais de 20 minutos e foi interrompida."
            else:
                if job.status == "cancelled":
                    return
                if job.kind == "desktop":
                    ok = job.process.returncode == 0 and answer.exists()
                    job.result = answer.read_text().strip() if ok else "A tarefa na tela falhou."
                else:
                    job.result, job.session_id, ok = parse_claude(stdout)
                job.status = "done" if ok else "failed"
                if ok and job.on_success:
                    job.on_success()
        job.ended = time.time()
        self.on_done(job)

    def cancel(self, job_id: int) -> bool:
        job = self.all.get(job_id)
        if not job or job.status != "running":
            return False
        job.status, job.ended = "cancelled", time.time()
        if job.process:
            job.process.terminate()
        return True

    def describe(self, job: Job) -> str:
        minutes = int((time.time() - job.started) // 60)
        line = f"Tarefa {job.id} ({job.kind}, {job.status}, há {minutes} min): {job.task}"
        return f"{line}. Resultado: {job.result}" if job.result else line


jobs = Jobs()


def lookup(params: dict) -> str:
    try:
        result = subprocess.run(
            claude_command(params["question"]),
            cwd=Path.home(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=LOOKUP_TIMEOUT, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return "A pesquisa demorou demais, não consegui a resposta."
    answer, _, ok = parse_claude(result.stdout)
    return answer if ok and answer else "Não consegui encontrar a resposta agora."


def edit_folder(path: str) -> tuple[Path | None, str]:
    """The folder an edit job may write in: the document's own folder, if it's a sensible place to edit."""
    target = Path(path).expanduser()
    folder = target if target.is_dir() else target.parent
    home = Path.home()
    if not target.exists():
        return None, f"Nothing at {path}"
    if folder == home or home not in folder.parents:
        return None, "I only edit inside a project or document folder under your home folder."
    size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
    if size > MAX_SNAPSHOT_BYTES:
        return None, f"That folder is too big to snapshot safely ({size // 2**20} MB)."
    return folder, ""


def start_task(params: dict) -> str:
    kind = params.get("kind", "computer")
    if kind not in ("computer", "desktop", "edit"):
        return f"Unknown kind: {kind}"
    if kind != "edit":
        job = jobs.start(kind, params["task"])
        return f"Started task {job.id}. Its result will arrive as an update when it finishes."
    folder, problem = edit_folder(params.get("path", ""))
    if not folder:
        return problem
    snapshot = SNAPSHOTS / f"{int(time.time())}-{folder.name}"
    shutil.copytree(folder, snapshot)
    reload = reload_front_app() if params.get("reload") else None
    job = jobs.start("edit", params["task"], folder=folder, snapshot=snapshot, on_success=reload)
    return f"Started task {job.id}, editing {folder.name}. Its result will arrive as an update when it finishes."


def reload_front_app():
    """Cmd+R in the app that's in front now, if it's still in front when the edit lands (the deck being presented)."""
    app = NSWorkspace.sharedWorkspace().frontmostApplication().localizedName()

    def reload():
        if NSWorkspace.sharedWorkspace().frontmostApplication().localizedName() == app:
            script = f'tell application "System Events" to tell process "{app}" to keystroke "r" using command down'
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10, check=False)

    return reload


def undo_task(params: dict) -> str:
    job = jobs.all.get(int(params["task_id"]))
    if not job or job.kind != "edit" or not job.snapshot or not job.snapshot.exists():
        return "That task didn't edit anything I can undo."
    if job.status == "running":
        return "That task is still running; cancel it first."
    shutil.rmtree(job.folder)
    shutil.copytree(job.snapshot, job.folder)
    return f"Restored {job.folder.name} to how it was before task {job.id}."


def task_status(params: dict) -> str:
    if task_id := params.get("task_id"):
        job = jobs.all.get(int(task_id))
        return jobs.describe(job) if job else f"No task {task_id}."
    recent = sorted(jobs.all.values(), key=lambda j: j.id)[-5:]
    return "\n".join(jobs.describe(job) for job in recent) or "No tasks yet."


def cancel_task(params: dict) -> str:
    task_id = int(params["task_id"])
    return f"Cancelled task {task_id}." if jobs.cancel(task_id) else f"Task {task_id} isn't running."


def reply_to_task(params: dict) -> str:
    job = jobs.all.get(int(params["task_id"]))
    if not job or job.kind not in ("computer", "edit") or not job.session_id:
        return "That task can't be continued; start a new one instead."
    fields = {}
    if job.kind == "edit":  # same folder, fresh snapshot so undo goes back one step at a time
        fields = {"folder": job.folder, "snapshot": SNAPSHOTS / f"{int(time.time())}-{job.folder.name}"}
        shutil.copytree(job.folder, fields["snapshot"])
    follow_up = jobs.start(job.kind, params["message"], resume=job.session_id, **fields)
    return f"Started task {follow_up.id}, continuing task {job.id}."


def task_id_param(description: str = "The task number") -> dict:
    return {"type": "object", "properties": {"task_id": {"type": "integer", "description": description}}}


SPECS = [
    {
        "name": "lookup",
        "handler": lookup,
        "description": "Answer a quick question that needs current or external information (news, prices, weather, "
        "facts, a quick web search). Waits for the answer, about 10 to 30 seconds.",
        "parameters": {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "The question, with all needed context"}},
            "required": ["question"],
        },
        "timeout": LOOKUP_TIMEOUT + 10,
    },
    {
        "name": "start_task",
        "handler": start_task,
        "description": "Hand a longer job to a background agent and return right away. kind=computer to look things "
        "up: files on the Mac, code, git, GitHub PRs and CI, deeper research (Claude Code, read-only). kind=edit to "
        "change a document or project: give path (the file or folder, e.g. from front_document); the agent may only "
        "write inside that folder, which is snapshotted so undo_task can restore it; set reload=true when it's the "
        "document on screen (a deck being presented). kind=desktop for clicking and typing inside apps (Codex "
        "computer use); ask permission to use the screen first.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What to do, self-contained, with every detail Vinicius gave",
                },
                "kind": {
                    "type": "string",
                    "enum": ["computer", "edit", "desktop"],
                    "description": "Which agent runs it",
                },
                "path": {"type": "string", "description": "kind=edit only: absolute path of the file or folder"},
                "reload": {"type": "boolean", "description": "kind=edit only: reload the front app when done"},
            },
            "required": ["task", "kind"],
        },
    },
    {
        "name": "undo_task",
        "handler": undo_task,
        "description": "Undo an edit task: put its folder back exactly as it was before the task ran.",
        "parameters": {**task_id_param(), "required": ["task_id"]},
    },
    {
        "name": "task_status",
        "handler": task_status,
        "description": "Status and results of background tasks: one task by number, or the five most recent.",
        "parameters": task_id_param("Optional task number; omit for the recent tasks"),
    },
    {
        "name": "cancel_task",
        "handler": cancel_task,
        "description": "Stop a running background task.",
        "parameters": {**task_id_param(), "required": ["task_id"]},
    },
    {
        "name": "reply_to_task",
        "handler": reply_to_task,
        "description": "Continue a finished computer task with a follow-up, keeping its context (e.g. 'and open it').",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "The task to continue"},
                "message": {"type": "string", "description": "The follow-up request"},
            },
            "required": ["task_id", "message"],
        },
    },
]
