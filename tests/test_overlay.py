from jarvis.jobs import Job
from jarvis.overlay import activity_label, is_user_key, job_label, visible_jobs


def test_activity_labels_name_the_tool_in_portuguese():
    assert activity_label("open_app", {"target": "Slack"}) == "Abrindo Slack"
    assert activity_label("lookup", {"question": "dólar"}) == "Pesquisando"
    assert activity_label("unknown_tool", {}) == "unknown_tool"


def test_job_label_shows_mark_task_and_elapsed_time():
    assert job_label(Job(1, "computer", "Achar o PDF", started=100.0), now=172.0) == "● Achar o PDF · 1:12"
    done = Job(2, "computer", "x" * 60, status="done", started=0.0, ended=9.0)
    assert job_label(done, now=999.0) == "✓ " + "x" * 47 + "… · 0:09"


def test_finished_jobs_stay_visible_for_a_few_seconds():
    running = Job(1, "computer", "a")
    recent = Job(2, "computer", "b", status="done", ended=100.0)
    old = Job(3, "computer", "c", status="failed", ended=50.0)
    assert visible_jobs([running, recent, old], now=103.0) == [running, recent]


def test_only_events_without_a_source_process_count_as_the_user():
    assert is_user_key(0) is True
    assert is_user_key(4242) is False


def test_cancelled_jobs_get_a_cross_and_stay_visible_briefly():
    cancelled = Job(1, "desktop", "passar slides", status="cancelled", started=0.0, ended=65.0)
    assert job_label(cancelled, now=999.0) == "✕ passar slides · 1:05"
    assert visible_jobs([cancelled], now=68.0) == [cancelled]
    assert visible_jobs([cancelled], now=80.0) == []
