import os
import signal
import subprocess
import time
from types import SimpleNamespace

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
    stopped = []
    fake = Jobs()
    job = Job(1, "computer", "x", status="cancelled")
    process = object()
    monkeypatch.setattr(jobs_module.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(jobs_module, "stop", stopped.append)
    fake._run(job, None)
    assert stopped == [process]


def test_jobs_start_in_their_own_session(monkeypatch):
    options = {}

    def popen(*args, **kwargs):
        options.update(kwargs)
        raise FileNotFoundError("claude")

    monkeypatch.setattr(jobs_module.subprocess, "Popen", popen)
    Jobs()._run(Job(1, "computer", "x"), None)
    assert options["start_new_session"] is True


def test_cancel_all_stops_running_jobs_of_the_given_kind():
    fake = Jobs()
    fake.all = {
        1: Job(1, "desktop", "click"),
        2: Job(2, "computer", "find"),
        3: Job(3, "desktop", "scroll"),
        4: Job(4, "desktop", "old", status="done"),
    }
    assert fake.cancel_all("desktop") == [1, 3]
    assert [job.status for job in fake.all.values()] == ["cancelled", "running", "cancelled", "done"]
    assert all(fake.all[i].ended for i in (1, 3))
    assert fake.cancel_all() == [2] and fake.cancel_all() == []


def dies(pid: int, process: subprocess.Popen) -> bool:
    """True once `pid` is gone (checked for up to 5 s); otherwise kills the group so the test leaves nothing behind."""
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    os.killpg(process.pid, signal.SIGKILL)
    return False


def test_cancelling_kills_the_whole_process_tree():
    # sh runs two sleeps as children: terminating only sh would leave them running.
    process = subprocess.Popen(
        ["sh", "-c", "sleep 30 & echo $! ; sleep 30"], stdout=subprocess.PIPE, text=True, start_new_session=True
    )
    child = int(process.stdout.readline())
    fake = Jobs()
    fake.all = {1: Job(1, "desktop", "x", process=process)}
    assert fake.cancel_all() == [1]
    assert process.wait(timeout=5) == -signal.SIGTERM
    assert dies(child, process)


def test_a_timed_out_job_is_killed_with_its_children(monkeypatch, tmp_path):
    spawned = []
    real_popen = subprocess.Popen
    script = f"sleep 30 & echo $! > {tmp_path / 'child'}; sleep 30"

    def popen(command, **kwargs):
        spawned.append(real_popen(["sh", "-c", script], **kwargs))
        return spawned[0]

    monkeypatch.setattr(jobs_module.subprocess, "Popen", popen)
    monkeypatch.setattr(jobs_module, "JOB_TIMEOUT", 0.5)
    job = Job(1, "computer", "x")
    Jobs()._run(job, None)
    assert job.status == "failed" and "20 minutos" in job.result
    assert dies(int((tmp_path / "child").read_text()), spawned[0])


def test_kill_group_ignores_a_process_that_is_already_gone():
    process = subprocess.Popen(["true"], start_new_session=True)
    process.wait()
    jobs_module.kill_group(process)  # no error, and nothing signalled


def test_cancel_task_with_and_without_an_id(monkeypatch):
    fake = Jobs()
    fake.all = {1: Job(1, "desktop", "a"), 2: Job(2, "computer", "b"), 3: Job(3, "computer", "c")}
    monkeypatch.setattr(jobs_module, "jobs", fake)
    assert jobs_module.cancel_task({"task_id": 2}) == "Cancelled task 2."
    assert jobs_module.cancel_task({"task_id": 2}) == "Task 2 isn't running."
    assert jobs_module.cancel_task({}) == "Cancelled tasks 1, 3."
    assert jobs_module.cancel_task({}) == "No tasks were running."


def test_cancel_task_spec_does_not_require_an_id():
    spec = next(spec for spec in jobs_module.SPECS if spec["name"] == "cancel_task")
    assert "required" not in spec["parameters"]


def test_lookup_answers_even_when_it_times_out(monkeypatch):
    def slow(*args, **kwargs):
        raise jobs_module.subprocess.TimeoutExpired("claude", 90)

    monkeypatch.setattr(jobs_module.subprocess, "run", slow)
    assert "demorou" in jobs_module.lookup({"question": "x"})


def test_start_task_refuses_deleting_and_sending(monkeypatch):
    started = []
    monkeypatch.setattr(jobs_module.jobs, "start", lambda *args, **kwargs: started.append(args))
    for task in [
        "Apagar todos os arquivos da pasta Downloads. Use rm -rf ~/Downloads/*",
        "Abrir o Slack, encontrar a conversa com João e enviar a mensagem 'vou atrasar'",
        "Send an email to Ana saying I'm late",
    ]:
        assert "Not started" in jobs_module.start_task({"kind": "computer", "task": task})
    assert not started


def test_start_task_allows_reading_tasks(monkeypatch):
    monkeypatch.setattr(jobs_module.jobs, "start", lambda *args, **kwargs: SimpleNamespace(id=7))
    for task in [
        "Procurar a apresentação do roadmap de 2027, excluindo cópias em node_modules",
        "Ver quais mensagens novas chegaram no canal do Slack",
        "Check the CI status of PR 19 on the jarvis repository",
    ]:
        assert "Started task 7" in jobs_module.start_task({"kind": "computer", "task": task})
