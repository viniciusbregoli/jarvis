"""Background jobs the voice agent hands off: Claude Code for files, code, PRs and research; Codex computer use for
clicking around apps. Jobs read and search by default; "edit" jobs may also change files, but only inside one folder,
which is snapshotted first so "desfaz" can put it back; "code" jobs change a repo in their own worktree and commit
there, and only publish_task pushes."""

import itertools
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from AppKit import NSWorkspace

from jarvis import code

JOB_TIMEOUT = 20 * 60
# The prompt forbids these, but the voice agent still sometimes starts a job for "apaga tudo de Downloads" or "manda
# mensagem pro João" while saying it's on it; refuse here so it has to tell the truth. Jobs couldn't do it anyway.
NOT_ENABLED = re.compile(
    r"\b(apag(ar|a|ue)|delet(ar|a|e)|exclu(ir|i|a)|rm\s+-\w*r)\b"
    r"|\b(envi(ar|a|e)|mand(ar|a|e)|send|post(ar|a|e)?|respond(er|e|a)|reply)\b.*\b(mensagem|message|e-?mail|slack|teams|whatsapp|canal|channel)",
    re.IGNORECASE,
)
SNAPSHOTS = Path.home() / ".jarvis/snapshots"
MAX_SNAPSHOT_BYTES = 300 * 1024 * 1024  # an edit job's folder is copied first; anything bigger is too broad a target
EDIT_TOOLS = ["Edit(./**)", "Write(./**)"]  # relative to the job's folder, its working directory
LOOKUP_TIMEOUT = 90
KILL_GRACE = 5  # seconds a cancelled job gets to exit on SIGTERM before its whole process group is SIGKILLed

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
# Coding jobs also commit and run the repo's checks, all inside their worktree. No push: that's publish_task, after a
# spoken "sim".
CODE_TOOLS = EDIT_TOOLS + [
    "Bash(git add *)", "Bash(git commit *)", "Bash(git branch -m *)", "Bash(git restore *)", "Bash(git rm *)",
    "Bash(git mv *)",
    "Bash(uv run pytest*)", "Bash(uv run python -m pytest*)", "Bash(uv run ruff *)", "Bash(uv run mypy *)",
    "Bash(pytest*)", "Bash(python -m pytest*)", "Bash(python3 -m pytest*)",
    "Bash(npm ci*)", "Bash(npm test*)", "Bash(npm run *)",
    "Bash(pnpm install --frozen-lockfile*)", "Bash(pnpm test*)", "Bash(pnpm run *)", "Bash(pnpm lint*)",
    "Bash(cargo test*)", "Bash(cargo build*)", "Bash(go test *)", "Bash(swift build*)", "Bash(swift test*)",
]  # fmt: skip
# Claude Code adds a co-author trailer to its commits by default; Vinicius never wants one.
NO_ATTRIBUTION = json.dumps({"includeCoAuthoredBy": False, "attribution": {"commit": "", "pr": ""}})

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
CODE_PROMPT = (
    "You are running as a background coding job for Jarvis, Vinicius's voice assistant on his Mac. Work on your own "
    "and never ask questions. Your working directory is a fresh git worktree of one of his repositories, on its own "
    "branch, based on the latest default branch. Read the repo's CLAUDE.md or AGENTS.md first if it has one and "
    "follow it. Make exactly the change he asked for, kept simple and matching the surrounding code; no unrelated "
    "cleanups. Change files only with the Edit and Write tools, never sed, heredocs or shell redirects. Bash runs "
    "one plain command per call, and only these: git add, commit, branch -m, restore, rm, mv, status, diff, log, "
    "and the repo's test or lint command (uv run pytest, uv run ruff, npm test, npm run <script>, pnpm run "
    "<script>, cargo test, go test, swift test); anything else, or several commands chained in one call, is "
    "refused. When a call is refused, do it another allowed way instead of giving up. "
    "Write or update tests for any behavior change and run the repo's tests and linter. Before your first "
    "commit, rename the branch with `git branch -m <type>/<short-name>` (type feat, fix or chore; short name in "
    "kebab-case). Commit in small, focused commits with conventional commit messages in English, in the imperative "
    "(`fix: stop the deck from skipping slides`), passing the message with -m (no heredocs, no $(...)), with no "
    "co-author or AI attribution lines and no emojis. Never push, open a PR or change anything outside this folder: "
    "Vinicius decides later whether to publish. If you can't finish, don't commit broken work; say what's missing. "
    + SPOKEN_ANSWER
    + " Say what you changed and whether the tests pass, without branch names. After the spoken part, if you "
    "committed anything, add a line 'PR title: <Type>: <description>' (e.g. 'Fix: stop the deck from skipping "
    "slides') and a line 'PR body:' followed by the PR description in English markdown, filling the repo's "
    ".github/pull_request_template.md if it has one; "
    "Jarvis uses those lines to open the PR and never reads them aloud."
)
DESKTOP_PROMPT = (
    "Use computer use to do this on the Mac: {task}\n\nNever send or post messages, buy, delete, or submit forms; if "
    "the task needs that, stop and explain. " + SPOKEN_ANSWER
)


def claude_command(prompt: str, resume: str | None = None, kind: str = "computer") -> list[str]:
    # Haiku: a file search took a median 19 s against 55 s on Sonnet, with the same answer. Coding jobs run in the
    # background and have to get the change right, so they get Sonnet. --strict-mcp-config skips starting every MCP
    # server, whose tools jobs can't call anyway.
    tools = {"edit": EDIT_TOOLS, "code": CODE_TOOLS}.get(kind, [])
    command = [
        "claude", "-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(READ_ONLY_TOOLS + tools),
        "--disallowedTools", ",".join(WRITE_FLAGS),
        "--append-system-prompt", {"edit": EDIT_PROMPT, "code": CODE_PROMPT}.get(kind, JOB_PROMPT),
        "--model", "claude-sonnet-5-5" if kind == "code" else "claude-haiku-5-5", "--strict-mcp-config",
        # No settings files: your own allow rules (ssh, sshpass, MCP tools) would otherwise apply to jobs too.
        "--setting-sources", "", "--settings", NO_ATTRIBUTION,
    ]  # fmt: skip
    return command + ["--resume", resume] if resume else command


def parse_claude(stdout: str) -> tuple[str, str | None, bool]:
    """(answer, session id, ok) from `claude -p --output-format json`."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return "A tarefa falhou sem resposta.", None, False
    return (data.get("result") or "").strip(), data.get("session_id"), not data.get("is_error")


def kill_group(process: subprocess.Popen, sig: int = signal.SIGTERM):
    """Signal a job's whole process tree. Jobs start in their own session, so the process is its group leader and
    codex's helper processes (the ones pressing keys) are in the group too. The group is signalled even after the
    leader has exited, since a helper that ignored SIGTERM can outlive it; the kernel doesn't reuse a pid while a
    group with that id still has members, and an empty group raises ProcessLookupError."""
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


def stop(process: subprocess.Popen):
    """SIGTERM the process tree now and SIGKILL it if anything is still alive after KILL_GRACE."""
    kill_group(process)
    timer = threading.Timer(KILL_GRACE, kill_group, (process, signal.SIGKILL))
    timer.daemon = True
    timer.start()


@dataclass
class Job:
    id: int
    kind: str  # "computer" (Claude Code), "edit" (Claude Code writing inside `folder`), "code" (Claude Code in a
    # worktree of `repo`) or "desktop" (Codex)
    task: str
    status: str = "running"  # running, done, failed, cancelled
    result: str = ""
    session_id: str | None = None
    started: float = field(default_factory=time.time)
    ended: float | None = None
    folder: Path | None = None  # edit jobs: the only place they may write; code jobs: their worktree
    snapshot: Path | None = None  # edit jobs: the folder as it was before
    on_success: object = field(default=None, repr=False)  # edit jobs: e.g. reload the document
    repo: Path | None = None  # code jobs: the main checkout
    base: str = ""  # code jobs: the branch the PR goes into
    start: str = ""  # code jobs: the commit the worktree started from
    commits: int = 0
    pr_title: str = ""
    pr_body: str = ""
    pr_url: str = ""
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
                command = claude_command(job.task, resume, kind=job.kind)
            if job.kind == "code" and not job.folder:
                try:
                    job.folder, job.base, job.start = code.create_worktree(
                        job.repo, f"{time.strftime('%Y%m%d-%H%M%S')}-{job.id}"
                    )
                except (subprocess.SubprocessError, OSError) as error:
                    detail = (getattr(error, "stderr", "") or str(error)).strip().splitlines()[-1:]
                    job.status, job.result, job.ended = (
                        "failed",
                        f"Não consegui criar a worktree: {' '.join(detail)}",
                        time.time(),
                    )
                    self.on_done(job)
                    return
            cwd = {"desktop": tmp, "edit": job.folder, "code": job.folder}.get(job.kind, Path.home())
            # stdin closed: claude -p reads any inherited input as part of the prompt.
            try:
                job.process = subprocess.Popen(
                    command,
                    cwd=cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
            except OSError as error:
                job.status, job.result, job.ended = "failed", f"Não consegui iniciar a tarefa: {error}", time.time()
                self.on_done(job)
                return
            if job.status == "cancelled":  # cancelled while it was starting
                stop(job.process)
                return
            try:
                stdout, _ = job.process.communicate(timeout=JOB_TIMEOUT)
            except subprocess.TimeoutExpired:
                kill_group(job.process, signal.SIGKILL)
                job.process.wait()
                job.status, job.result = "failed", "Demorou mais de 20 minutos e foi interrompida."
            else:
                if job.status == "cancelled":
                    return
                if job.kind == "desktop":
                    ok = job.process.returncode == 0 and answer.exists()
                    job.result = answer.read_text().strip() if ok else "A tarefa na tela falhou."
                else:
                    job.result, job.session_id, ok = parse_claude(stdout)
                if job.kind == "code":
                    self._finish_code(job)
                job.status = "done" if ok else "failed"
                if ok and job.on_success:
                    job.on_success()
        job.ended = time.time()
        self.on_done(job)

    def _finish_code(self, job: Job):
        job.result, job.pr_title, job.pr_body = code.split_pr(job.result)
        if not job.pr_title:  # a follow-up that only answered keeps the PR text its first run wrote
            earlier = [other for other in list(self.all.values()) if other.folder == job.folder and other.pr_title]
            if earlier:
                job.pr_title, job.pr_body = earlier[-1].pr_title, earlier[-1].pr_body
        try:
            job.commits = code.commits_since(job.folder, job.start)
        except (subprocess.SubprocessError, OSError, ValueError):
            job.commits = 0
        if job.commits:
            published = any(other.pr_url for other in list(self.all.values()) if other.folder == job.folder)
            job.result += " Quer que eu atualize o PR?" if published else " Quer que eu abra o PR?"
        elif code.remove_if_empty(job.repo, job.folder, job.start):
            job.folder = None

    def cancel(self, job_id: int) -> bool:
        job = self.all.get(job_id)
        if not job or job.status != "running":
            return False
        job.status, job.ended = "cancelled", time.time()
        if job.process:
            stop(job.process)
        return True

    def cancel_all(self, kind: str | None = None) -> list[int]:
        """Cancel every running job, or only those of one kind. Returns the cancelled ids."""
        ids = [job.id for job in list(self.all.values()) if job.status == "running" and kind in (None, job.kind)]
        return [job_id for job_id in ids if self.cancel(job_id)]

    def shutdown(self):
        """Cancel everything and give the process trees a moment to die: Jarvis is about to exit, and the jobs run in
        their own sessions, so they would outlive it."""
        processes = [job.process for job in self.all.values() if job.process and job.process.poll() is None]
        self.cancel_all()
        for process in processes:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            kill_group(process, signal.SIGKILL)  # the leader may be gone while a helper that ignored SIGTERM isn't

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
    if kind not in ("computer", "desktop", "edit", "code"):
        return f"Unknown kind: {kind}"
    if kind == "code":
        repo, problem = code.find_repo(params.get("path", ""))
        if not repo:
            return problem
        job = jobs.start("code", params["task"], repo=repo)
        return f"Started task {job.id} in {repo.name}. Its result will arrive as an update when it finishes."
    if kind != "edit":
        if NOT_ENABLED.search(params["task"]):
            return "Not started: deleting files and sending messages aren't enabled yet. Tell Vinicius that."
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
    if not (task_id := params.get("task_id")):
        ids = jobs.cancel_all()
        return f"Cancelled tasks {', '.join(map(str, ids))}." if ids else "No tasks were running."
    task_id = int(task_id)
    return f"Cancelled task {task_id}." if jobs.cancel(task_id) else f"Task {task_id} isn't running."


def reply_to_task(params: dict) -> str:
    job = jobs.all.get(int(params["task_id"]))
    if not job or job.kind not in ("computer", "edit", "code") or not job.session_id:
        return "That task can't be continued; start a new one instead."
    fields = {}
    if job.kind == "code":
        if not job.folder:
            return "That task's worktree is gone (it changed nothing); start a new one instead."
        fields = {"folder": job.folder, "repo": job.repo, "base": job.base, "start": job.start}
    if job.kind == "edit":  # same folder, fresh snapshot so undo goes back one step at a time
        fields = {"folder": job.folder, "snapshot": SNAPSHOTS / f"{int(time.time())}-{job.folder.name}"}
        shutil.copytree(job.folder, fields["snapshot"])
    follow_up = jobs.start(job.kind, params["message"], resume=job.session_id, **fields)
    return f"Started task {follow_up.id}, continuing task {job.id}."


def publish_task(params: dict) -> str:
    if task_id := params.get("task_id"):
        job = jobs.all.get(int(task_id))
    else:  # "abre o PR": the latest coding job that committed something
        job = max(
            (j for j in list(jobs.all.values()) if j.kind == "code" and j.commits), key=lambda j: j.id, default=None
        )
    if not job or job.kind != "code":
        return "There's no coding task to publish."
    if job.status == "running":
        return f"Task {job.id} is still running."
    if not job.commits or not job.folder:
        return f"Task {job.id} didn't commit anything, so there's nothing to publish."
    try:
        job.pr_url = code.publish(job.folder, job.base, job.pr_title, job.pr_body)
    except (subprocess.SubprocessError, OSError) as error:
        detail = (getattr(error, "stderr", "") or str(error)).strip().splitlines()
        return f"Couldn't publish task {job.id}: {detail[-1] if detail else 'unknown error'}"
    return f"Published task {job.id}: {job.pr_url}"


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
        "change a document: give path (the file or folder, e.g. from front_document); the agent may only "
        "write inside that folder, which is snapshotted so undo_task can restore it; set reload=true when it's the "
        "document on screen (a deck being presented). kind=code to change code in a git repository: give path (the "
        "repo's name as he said it, e.g. jarvis or cosmos, or a path inside it); the agent works in its own worktree "
        "and branch, runs the tests and commits, but never pushes. kind=desktop for clicking and typing inside apps "
        "(Codex computer use); ask permission to use the screen first.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What to do, self-contained, with every detail Vinicius gave",
                },
                "kind": {
                    "type": "string",
                    "enum": ["computer", "edit", "code", "desktop"],
                    "description": "Which agent runs it",
                },
                "path": {
                    "type": "string",
                    "description": "kind=edit: absolute path of the file or folder; kind=code: the repository name or a path inside it",
                },
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
        "description": "Stop a running background task by number. Without a number, stops every running task: use "
        "it for 'para tudo' or 'cancela tudo', or when Vinicius wants whatever is happening on screen to stop.",
        "parameters": task_id_param("Optional task number; omit to stop all running tasks"),
    },
    {
        "name": "reply_to_task",
        "handler": reply_to_task,
        "description": "Continue a finished computer, edit or code task with a follow-up, keeping its context (e.g. "
        "'and open it', 'adiciona um teste também').",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "The task to continue"},
                "message": {"type": "string", "description": "The follow-up request"},
            },
            "required": ["task_id", "message"],
        },
    },
    {
        "name": "publish_task",
        "handler": publish_task,
        "description": "Push a finished code task's branch and open its pull request on GitHub (or update the one it "
        "already has). Publishing is outward: call it only after Vinicius said 'sim' to opening the PR in this "
        "conversation, never on your own.",
        "parameters": task_id_param("Optional task number; omit for the latest code task"),
        "timeout": 120,
    },
]
