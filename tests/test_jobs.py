from jarvis import jobs as jobs_module
from jarvis.jobs import Job, Jobs, claude_command, parse_claude


def test_claude_command_is_read_only_and_headless():
    command = claude_command("find my slides")
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert not {"Edit", "Write", "NotebookEdit", "Bash", "Bash(*)"} & set(allowed)
    assert all(not rule.startswith("Bash(") or rule.endswith(" *)") for rule in allowed)
    assert "--strict-mcp-config" in command and "--resume" not in command


def test_claude_command_resumes_a_session():
    command = claude_command("and open it", resume="abc")
    assert command[-2:] == ["--resume", "abc"]


def test_parse_claude_reads_result_and_session():
    assert parse_claude('{"result": " Achei. ", "session_id": "s1", "is_error": false}') == ("Achei.", "s1", True)
    assert parse_claude('{"result": "x", "is_error": true}')[2] is False
    assert parse_claude("not json")[2] is False


def test_reply_only_continues_finished_computer_tasks(monkeypatch):
    fake = Jobs()
    fake.all = {1: Job(1, "desktop", "click"), 2: Job(2, "computer", "find", session_id="s2")}
    started = []
    monkeypatch.setattr(fake, "start", lambda kind, task, resume=None: started.append(resume) or Job(3, kind, task))
    monkeypatch.setattr(jobs_module, "jobs", fake)
    assert "can't be continued" in jobs_module.reply_to_task({"task_id": 1, "message": "more"})
    assert jobs_module.reply_to_task({"task_id": 2, "message": "more"}) == "Started task 3, continuing task 2."
    assert started == ["s2"]


def test_start_task_rejects_unknown_kinds():
    assert jobs_module.start_task({"task": "x", "kind": "root"}) == "Unknown kind: root"


def test_cancel_only_running_tasks():
    fake = Jobs()
    fake.all = {1: Job(1, "computer", "x", status="done")}
    assert fake.cancel(1) is False
    fake.all[2] = Job(2, "computer", "y")
    assert fake.cancel(2) is True and fake.all[2].status == "cancelled"


def test_claude_command_ignores_settings_files_and_blocks_write_flags():
    command = claude_command("x")
    assert command[command.index("--setting-sources") + 1] == ""
    assert "Bash(git log --output*)" in command[command.index("--disallowedTools") + 1]
    assert not any("git branch" in rule for rule in command[command.index("--allowedTools") + 1].split(","))


def test_a_job_that_cannot_start_is_reported_failed(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(jobs_module.subprocess, "Popen", missing)
    fake, finished = Jobs(), []
    fake.on_done = finished.append
    job = Job(1, "computer", "x")
    fake._run(job, None)
    assert job.status == "failed" and job.ended and finished == [job]


def test_a_job_cancelled_while_starting_is_killed(monkeypatch):
    class Process:
        terminated = False

        def terminate(self):
            Process.terminated = True

    fake = Jobs()
    job = Job(1, "computer", "x", status="cancelled")
    monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda *a, **k: Process())
    fake._run(job, None)
    assert Process.terminated


def test_lookup_answers_even_when_it_times_out(monkeypatch):
    def slow(*args, **kwargs):
        raise jobs_module.subprocess.TimeoutExpired("claude", 90)

    monkeypatch.setattr(jobs_module.subprocess, "run", slow)
    assert "demorou" in jobs_module.lookup({"question": "x"})
