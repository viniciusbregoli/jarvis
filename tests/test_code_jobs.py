import json
import os
import subprocess
import sys
import time

import pytest

from jarvis import code
from jarvis import jobs as jobs_module
from jarvis.jobs import CODE_TOOLS, Job, Jobs, claude_command, publish_task, reply_to_task, start_task


def run(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone of a local bare 'origin' with one commit on main, like a GitHub checkout."""
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "test@example.com")
    origin = tmp_path / "origin.git"
    run("git", "init", "--bare", "-q", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    run("git", "init", "-q", "-b", "main", str(seed))
    (seed / "README.md").write_text("hello\n")
    run("git", "add", ".", cwd=seed)
    run("git", "commit", "-q", "-m", "chore: start", cwd=seed)
    run("git", "push", "-q", str(origin), "main", cwd=seed)
    clone = tmp_path / "projects" / "jarvis"
    clone.parent.mkdir()
    run("git", "clone", "-q", str(origin), str(clone))
    return clone


def commit_file(worktree, name="fix.txt"):
    (worktree / name).write_text("fixed\n")
    run("git", "add", name, cwd=worktree)
    run("git", "commit", "-q", "-m", "fix: change something", cwd=worktree)


# --- finding the repo ------------------------------------------------------------------------------------------------


def test_find_repo_by_name_partial_name_and_path(tmp_path, repo):
    roots = [repo.parent]
    other = repo.parent / "kinebot-cosmos"
    run("git", "init", "-q", str(other))
    assert code.find_repo("jarvis", roots) == (repo, "")
    assert code.find_repo(" Jarvis ", roots) == (repo, "")
    assert code.find_repo("cosmos", roots) == (other, "")
    assert code.find_repo(str(repo / "README.md"), roots) == (repo, "")
    assert code.find_repo("nope", roots)[0] is None
    assert code.find_repo("", roots)[0] is None
    assert code.find_repo(str(tmp_path / "missing"), roots)[0] is None


def test_find_repo_picks_the_checkout_used_last(tmp_path):
    older, newer = tmp_path / "a" / "screamr", tmp_path / "b" / "screamr"
    for path in (older, newer):
        run("git", "init", "-q", str(path))
        (path / ".git/index").write_text("")
    os.utime(older / ".git/index", (time.time() - 1000, time.time() - 1000))
    assert code.find_repo("screamr", [tmp_path / "a", tmp_path / "b"]) == (newer, "")


def test_find_repo_from_inside_a_worktree_gives_the_main_checkout(repo):
    worktree, _, _ = code.create_worktree(repo, "t1")
    assert code.find_repo(str(worktree), [])[0] == repo


# --- worktrees ---------------------------------------------------------------------------------------------------------


def test_worktree_starts_from_the_latest_default_branch(tmp_path, repo):
    # Someone else pushed to main after the clone: the job must start from that, not the stale local main.
    other = tmp_path / "other"
    run("git", "clone", "-q", str(tmp_path / "origin.git"), str(other))
    commit_file(other, "new.txt")
    run("git", "push", "-q", "origin", "main", cwd=other)
    worktree, base, start = code.create_worktree(repo, "t1")
    assert worktree == repo / ".claude/worktrees/t1" and base == "main"
    assert (worktree / "new.txt").exists()
    assert run("git", "branch", "--show-current", cwd=worktree) == "jarvis/t1"
    assert start == run("git", "rev-parse", "origin/main", cwd=repo)
    assert run("git", "branch", "--show-current", cwd=repo) == "main"  # his checkout is untouched


def test_worktree_without_a_remote_starts_from_the_local_branch(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_AUTHOR_NAME", "T")
    local = tmp_path / "local"
    run("git", "init", "-q", "-b", "trunk", str(local))
    run("git", "-c", "user.email=t@t", "-c", "user.name=T", "commit", "-q", "--allow-empty", "-m", "x", cwd=local)
    worktree, base, _ = code.create_worktree(local, "t1")
    assert base == "trunk" and worktree.is_dir()


def test_empty_worktrees_are_removed_and_ones_with_work_kept(repo):
    empty, _, start = code.create_worktree(repo, "empty")
    assert code.remove_if_empty(repo, empty, start)
    assert not empty.exists() and "jarvis/empty" not in run("git", "branch", cwd=repo)

    committed, _, start = code.create_worktree(repo, "committed")
    commit_file(committed)
    assert not code.remove_if_empty(repo, committed, start) and committed.exists()

    dirty, _, start = code.create_worktree(repo, "dirty")
    (dirty / "half-done.txt").write_text("wip")
    assert not code.remove_if_empty(repo, dirty, start) and dirty.exists()


# --- the job's answer and publishing -----------------------------------------------------------------------------------


def test_split_pr_keeps_the_pr_text_out_of_the_spoken_answer():
    answer = (
        "Corrigi o typo e os testes passam.\nPR title: `Fix: correct the README typo`\nPR body:\n## Description\nFixed."
    )
    assert code.split_pr(answer) == (
        "Corrigi o typo e os testes passam.",
        "Fix: correct the README typo",
        "## Description\nFixed.",
    )
    assert code.split_pr("Não mudei nada.") == ("Não mudei nada.", "", "")
    assert code.split_pr("Feito.\nPR title: Fix: x") == ("Feito.", "Fix: x", "")


def test_publish_pushes_the_branch_and_opens_a_pr(tmp_path, repo, monkeypatch):
    worktree, base, _ = code.create_worktree(repo, "t1")
    run("git", "branch", "-m", "fix/readme-typo", cwd=worktree)
    commit_file(worktree)
    calls = []

    def fake_gh(cwd, *args, stdin="", check=True):
        calls.append((args, stdin))
        return (
            "" if args[:2] == ("pr", "view") else "Warning: 1 uncommitted change\nhttps://github.com/v/jarvis/pull/30"
        )

    monkeypatch.setattr(code, "gh", fake_gh)
    url = code.publish(worktree, base, "Fix: correct the README typo", "## Description")
    assert url == "https://github.com/v/jarvis/pull/30"
    assert run("git", "branch", "--list", "fix/readme-typo", cwd=tmp_path / "origin.git")  # pushed
    args, body = calls[-1]
    assert args[:2] == ("pr", "create") and "@me" in args and body == "## Description"
    assert args[args.index("--title") + 1] == "Fix: correct the README typo"
    assert args[args.index("--base") + 1] == "main"


def test_publish_updates_an_existing_pr_instead_of_opening_another(repo, monkeypatch):
    worktree, base, _ = code.create_worktree(repo, "t1")
    commit_file(worktree)
    calls = []
    monkeypatch.setattr(
        code, "gh", lambda cwd, *args, **kw: calls.append(args) or "https://github.com/v/jarvis/pull/30"
    )
    assert code.publish(worktree, base, "Fix: x", "") == "https://github.com/v/jarvis/pull/30"
    assert [args[:2] for args in calls] == [("pr", "view")]


# --- the job itself ----------------------------------------------------------------------------------------------------


def test_code_commands_commit_but_never_push():
    command = claude_command("fix it", kind="code")
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert "Bash(git commit *)" in allowed and "Edit(./**)" in allowed
    assert not any("push" in rule or "gh pr create" in rule for rule in allowed)
    assert not {"Bash", "Bash(*)", "Bash(uv run *)", "Bash(git *)"} & set(allowed)
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"
    settings = json.loads(command[command.index("--settings") + 1])
    assert settings["includeCoAuthoredBy"] is False and settings["attribution"]["commit"] == ""
    assert "Bash(git commit *)" not in claude_command("look")[claude_command("look").index("--allowedTools") + 1]
    assert set(CODE_TOOLS) >= {"Edit(./**)", "Write(./**)"}


FAKE_CLAUDE = """
import json, subprocess, sys
commit = sys.argv[1] == "commit"
if commit:
    subprocess.run(["git", "branch", "-m", "fix/readme-typo"], check=True)
    open("README.md", "a").write("fixed\\n")
    subprocess.run(["git", "commit", "-qam", "fix: correct the README typo"], check=True)
    result = "Corrigi o README.\\nPR title: Fix: correct the README typo\\nPR body:\\nFixed the typo."
else:
    result = "O prompt fica em jarvis/agent.py."
print(json.dumps({"result": result, "session_id": "s1", "is_error": False}))
"""


def run_job(monkeypatch, tmp_path, fake, job):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE)
    monkeypatch.setattr(
        jobs_module, "claude_command", lambda task, resume=None, kind="computer": [sys.executable, str(script), task]
    )
    done = []
    fake.on_done = done.append
    fake._run(job, None)
    assert done == [job]
    return job


def test_a_coding_job_works_in_its_own_worktree_and_offers_the_pr(tmp_path, repo, monkeypatch):
    fake = Jobs()
    job = run_job(monkeypatch, tmp_path, fake, Job(1, "code", "commit", repo=repo))
    assert job.status == "done" and job.commits == 1
    assert job.result == "Corrigi o README. Quer que eu abra o PR?"
    assert (job.pr_title, job.pr_body) == ("Fix: correct the README typo", "Fixed the typo.")
    assert job.folder.parent == repo / ".claude/worktrees"
    assert run("git", "branch", "--show-current", cwd=job.folder) == "fix/readme-typo"
    assert run("git", "status", "--porcelain", cwd=repo) == "?? .claude/"  # his checkout only gained the worktree


def test_a_coding_job_that_changes_nothing_leaves_no_worktree(tmp_path, repo, monkeypatch):
    job = run_job(monkeypatch, tmp_path, Jobs(), Job(1, "code", "answer", repo=repo))
    assert job.status == "done" and job.commits == 0 and job.folder is None
    assert job.result == "O prompt fica em jarvis/agent.py."
    assert run("git", "worktree", "list", cwd=repo).count("\n") == 0


def test_a_coding_job_fails_cleanly_when_the_worktree_cant_be_made(tmp_path, monkeypatch):
    job = run_job(monkeypatch, tmp_path, Jobs(), Job(1, "code", "commit", repo=tmp_path / "not-a-repo"))
    assert job.status == "failed" and "worktree" in job.result


def test_start_task_code_needs_a_known_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(code, "ROOTS", [tmp_path])
    assert start_task({"task": "fix", "kind": "code", "path": "ghost"}).startswith("No repository called ghost")


def test_start_task_code_starts_a_job_on_the_repo(monkeypatch, repo):
    fake = Jobs()
    started = []
    monkeypatch.setattr(fake, "start", lambda kind, task, **fields: started.append(fields) or Job(4, kind, task))
    monkeypatch.setattr(jobs_module, "jobs", fake)
    monkeypatch.setattr(code, "ROOTS", [repo.parent])
    # "apaga" would be refused for other kinds; deleting code in a worktree is a normal change.
    assert start_task({"task": "apaga a função morta", "kind": "code", "path": "jarvis"}).startswith(
        "Started task 4 in jarvis"
    )
    assert started == [{"repo": repo}]


def test_publish_task_picks_the_latest_coding_job_with_commits(monkeypatch, tmp_path):
    fake = Jobs()
    fake.all = {
        1: Job(1, "code", "a", status="done", commits=1, folder=tmp_path, base="main", pr_title="Fix: a"),
        2: Job(2, "code", "b", status="done", commits=0),
        3: Job(3, "computer", "c", status="done"),
    }
    monkeypatch.setattr(jobs_module, "jobs", fake)
    published = []
    monkeypatch.setattr(code, "publish", lambda *args: published.append(args) or "https://github.com/v/j/pull/9")
    assert publish_task({}) == "Published task 1: https://github.com/v/j/pull/9"
    assert published == [(tmp_path, "main", "Fix: a", "")] and fake.all[1].pr_url.endswith("/9")
    assert "didn't commit" in publish_task({"task_id": 2})
    assert "no coding task" in publish_task({"task_id": 3})


def test_publish_task_refuses_running_jobs_and_reports_failures(monkeypatch, tmp_path):
    fake = Jobs()
    fake.all = {
        1: Job(1, "code", "a", commits=1, folder=tmp_path),
        2: Job(2, "code", "b", status="done", commits=1, folder=tmp_path),
    }
    monkeypatch.setattr(jobs_module, "jobs", fake)

    def rejected(*args):
        raise subprocess.CalledProcessError(1, "push", "", "remote: Permission denied\nfatal: unable to access")

    monkeypatch.setattr(code, "publish", rejected)
    assert "still running" in publish_task({"task_id": 1})
    assert publish_task({"task_id": 2}) == "Couldn't publish task 2: fatal: unable to access"
    assert publish_task({}) == "Couldn't publish task 2: fatal: unable to access"  # the latest one with commits


def test_follow_ups_continue_in_the_same_worktree(monkeypatch, tmp_path):
    fake = Jobs()
    fake.all = {
        1: Job(
            1, "code", "fix", status="done", session_id="s1", folder=tmp_path, repo=tmp_path, base="main", start="abc"
        )
    }
    started = []
    monkeypatch.setattr(
        fake, "start", lambda kind, task, resume=None, **fields: started.append((resume, fields)) or Job(2, kind, task)
    )
    monkeypatch.setattr(jobs_module, "jobs", fake)
    assert reply_to_task({"task_id": 1, "message": "adiciona um teste"}) == "Started task 2, continuing task 1."
    assert started == [("s1", {"folder": tmp_path, "repo": tmp_path, "base": "main", "start": "abc"})]
    fake.all[1].folder = None
    assert "worktree is gone" in reply_to_task({"task_id": 1, "message": "mais"})
