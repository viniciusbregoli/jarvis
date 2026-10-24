"""Background jobs the voice agent hands off: Claude Code for files, code, PRs and research; Codex computer use for
clicking around apps. Read-only for now: jobs can look and search but not change, send or delete anything."""

import itertools
import json
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

JOB_TIMEOUT = 20 * 60
LOOKUP_TIMEOUT = 90

# Read and search only. Claude Code also allows commands it classifies as read-only (ls, cat, date) on its own;
# in dontAsk mode everything else is refused.
READ_ONLY_TOOLS = [
    "Read", "Glob", "Grep", "WebSearch", "WebFetch",
    "Bash(cd *)", "Bash(mdfind *)", "Bash(mdls *)", "Bash(stat *)",
    "Bash(git log *)", "Bash(git status *)", "Bash(git diff *)", "Bash(git show *)", "Bash(git branch *)",
    "Bash(gh pr list *)", "Bash(gh pr view *)", "Bash(gh pr checks *)", "Bash(gh pr diff *)",
    "Bash(gh run list *)", "Bash(gh run view *)", "Bash(gh issue list *)", "Bash(gh issue view *)",
]  # fmt: skip

SPOKEN_ANSWER = (
    "Your final answer is spoken aloud by a voice assistant: one to three short sentences in Brazilian Portuguese, "
    "no markdown, no lists, no long paths or URLs."
)
JOB_PROMPT = (
    "You are running as a background job for Jarvis, Vinicius's voice assistant on his Mac. Work on your own and "
    "never ask questions. You can only read and search: never try to change, send, delete or create anything; if "
    "the task needs that, say it isn't enabled yet. To find files, prefer mdfind (Spotlight), one simple query per call: never put || or && inside a query. "
    + SPOKEN_ANSWER
)
DESKTOP_PROMPT = (
    "Use computer use to do this on the Mac: {task}\n\nNever send or post messages, buy, delete, or submit forms; if "
    "the task needs that, stop and explain. " + SPOKEN_ANSWER
)


def claude_command(prompt: str, resume: str | None = None) -> list[str]:
    # Haiku: a file search took a median 19 s against 55 s on Sonnet, with the same answer. --strict-mcp-config skips
    # starting every MCP server, whose tools read-only jobs can't call anyway.
    command = [
        "claude", "-p", prompt, "--output-format", "json", "--permission-mode", "dontAsk",
        "--allowedTools", ",".join(READ_ONLY_TOOLS), "--append-system-prompt", JOB_PROMPT,
        "--model", "claude-haiku-5-5", "--strict-mcp-config",
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
    kind: str  # "computer" (Claude Code) or "desktop" (Codex computer use)
    task: str
    status: str = "running"  # running, done, failed, cancelled
    result: str = ""
    session_id: str | None = None
    started: float = field(default_factory=time.time)
    process: subprocess.Popen | None = field(default=None, repr=False)


class Jobs:
    def __init__(self):
        self.all: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self.on_done = lambda job: None

    def start(self, kind: str, task: str, resume: str | None = None) -> Job:
        job = Job(next(self._ids), kind, task)
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
                command = claude_command(job.task, resume)
            cwd = tmp if job.kind == "desktop" else Path.home()
            # stdin closed: claude -p reads any inherited input as part of the prompt.
            job.process = subprocess.Popen(
                command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
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
        self.on_done(job)

    def cancel(self, job_id: int) -> bool:
        job = self.all.get(job_id)
        if not job or job.status != "running":
            return False
        job.status = "cancelled"
        if job.process:
            job.process.terminate()
        return True

    def describe(self, job: Job) -> str:
        minutes = int((time.time() - job.started) // 60)
        line = f"Tarefa {job.id} ({job.kind}, {job.status}, há {minutes} min): {job.task}"
        return f"{line}. Resultado: {job.result}" if job.result else line


jobs = Jobs()


def lookup(params: dict) -> str:
    result = subprocess.run(
        claude_command(params["question"]),
        cwd=Path.home(), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=LOOKUP_TIMEOUT, check=False,
    )  # fmt: skip
    answer, _, ok = parse_claude(result.stdout)
    return answer if ok and answer else "Não consegui encontrar a resposta agora."


def start_task(params: dict) -> str:
    kind = params.get("kind", "computer")
    if kind not in ("computer", "desktop"):
        return f"Unknown kind: {kind}"
    job = jobs.start(kind, params["task"])
    return f"Started task {job.id}. Its result will arrive as an update when it finishes."


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
    if not job or job.kind != "computer" or not job.session_id:
        return "That task can't be continued; start a new one instead."
    follow_up = jobs.start("computer", params["message"], resume=job.session_id)
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
        "description": "Hand a longer job to a background agent and return right away. kind=computer for anything "
        "about files on the Mac, code, git, GitHub PRs and CI, or deeper research (Claude Code). kind=desktop for "
        "things that need clicking and typing inside apps (Codex computer use); ask permission to use the screen "
        "first. Jobs are read-only: they can look and search but not change, send or delete.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "What to do, self-contained, with every detail Vinicius gave",
                },
                "kind": {"type": "string", "enum": ["computer", "desktop"], "description": "Which agent runs it"},
            },
            "required": ["task", "kind"],
        },
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
