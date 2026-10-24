"""Git side of coding jobs: find the repo Vinicius named, give the job its own worktree and branch off the latest
default branch, and publish the branch as a PR once he says "sim"."""

import re
import shutil
import subprocess
from pathlib import Path

ROOTS = [Path.home() / ".t3/projects", Path.home() / "Code"]
WORKTREES = Path(".claude/worktrees")  # inside the repo, where Vinicius keeps his own worktrees
FETCH_TIMEOUT = 60
PR_TITLE = re.compile(r"^\s*PR title:\s*(.+)$", re.IGNORECASE | re.MULTILINE)
PR_BODY = re.compile(r"^\s*PR body:\s*$", re.IGNORECASE | re.MULTILINE)


def git(repo: Path, *args: str, timeout: int = 30) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout, check=True,
    ).stdout.strip()  # fmt: skip


def main_checkout(path: Path) -> Path | None:
    """The repo's main checkout for any path inside it, including inside one of its worktrees."""
    try:
        common = git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except (subprocess.CalledProcessError, OSError):
        return None
    return Path(common).parent


def last_used(repo: Path) -> float:
    index = repo / ".git/index"
    return index.stat().st_mtime if index.exists() else 0


def find_repo(name: str, roots: list[Path] | None = None) -> tuple[Path | None, str]:
    """A repo by the name Vinicius said ("jarvis", "cosmos") or by a path inside it."""
    roots = ROOTS if roots is None else roots
    name = name.strip()
    if not name:
        return None, "Say which repository."
    if name.startswith(("/", "~")):
        path = Path(name).expanduser()
        repo = main_checkout(path if path.is_dir() else path.parent) if path.exists() else None
        return (repo, "") if repo else (None, f"{name} isn't inside a git repository.")
    wanted = name.lower().replace(" ", "-")
    repos = [p for root in roots if root.is_dir() for p in root.iterdir() if (p / ".git").exists()]
    exact = [p for p in repos if p.name.lower() == wanted]
    # "cosmos" for kinebot-cosmos, "lambda" for kinebot-lambda.
    partial = [p for p in repos if wanted in p.name.lower()]
    # The same name can live in two roots (screamr): the one he worked in last is the likely one.
    for found in (exact, partial):
        if found:
            return max(found, key=last_used), ""
    return None, f"No repository called {name} in {' or '.join(str(r) for r in roots)}."


def default_branch(repo: Path) -> str:
    try:
        return git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD").removeprefix("origin/")
    except subprocess.CalledProcessError:
        return git(repo, "rev-parse", "--abbrev-ref", "HEAD")


def create_worktree(repo: Path, name: str) -> tuple[Path, str, str]:
    """A new worktree on branch jarvis/<name>, off the latest default branch. Returns (worktree, base branch, start
    commit). Offline, it starts from the local default branch instead."""
    base = default_branch(repo)
    try:
        git(repo, "fetch", "origin", base, timeout=FETCH_TIMEOUT)
        start = f"origin/{base}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        start = base
    worktree = repo / WORKTREES / name
    git(repo, "worktree", "add", "--no-track", "-b", f"jarvis/{name}", str(worktree), start, timeout=120)
    return worktree, base, git(worktree, "rev-parse", "HEAD")


def commits_since(worktree: Path, start: str) -> int:
    return int(git(worktree, "rev-list", "--count", f"{start}..HEAD"))


def remove_if_empty(repo: Path, worktree: Path, start: str) -> bool:
    """Drop a worktree whose job committed nothing and left nothing uncommitted, so answers and failed attempts don't
    pile up worktrees. Anything with work in it stays for Vinicius to look at."""
    try:
        if commits_since(worktree, start) or git(worktree, "status", "--porcelain"):
            return False
        branch = git(worktree, "branch", "--show-current")
        git(repo, "worktree", "remove", str(worktree))
        git(repo, "branch", "-D", branch)
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def split_pr(answer: str) -> tuple[str, str, str]:
    """(spoken part, PR title, PR body) from a coding job's answer, which ends with 'PR title:' and 'PR body:' lines
    that are never read aloud."""
    title = PR_TITLE.search(answer)
    if not title:
        return answer.strip(), "", ""
    body = PR_BODY.search(answer, title.end())
    return (
        answer[: title.start()].strip(),
        title.group(1).strip().strip("`"),
        answer[body.end() :].strip() if body else "",
    )


def publish(worktree: Path, base: str, title: str, body: str) -> str:
    """Push the job's branch and open its PR, or update the PR it already has. Returns the PR URL."""
    branch = git(worktree, "branch", "--show-current")
    git(worktree, "push", "-u", "origin", branch, timeout=120)
    existing = gh(worktree, "pr", "view", branch, "--json", "url", "--jq", ".url", check=False)
    if existing:
        return existing
    fields = ["--title", title, "--body-file", "-"] if title else ["--fill"]
    created = gh(worktree, "pr", "create", "--base", base, "--head", branch, "--assignee", "@me", *fields, stdin=body)
    return created.splitlines()[-1]  # the URL comes last, after any warnings


def gh(cwd: Path, *args: str, stdin: str = "", check: bool = True) -> str:
    """gh's output, or "" when it fails and check is off (e.g. `gh pr view` for a branch with no PR)."""
    if not shutil.which("gh"):
        raise OSError("gh isn't installed")
    result = subprocess.run(
        ["gh", *args], cwd=cwd, input=stdin, capture_output=True, text=True, timeout=60, check=check
    )
    return "" if result.returncode else result.stdout.strip()
