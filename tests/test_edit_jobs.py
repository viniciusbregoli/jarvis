from pathlib import Path

from jarvis import jobs as jobs_module
from jarvis.context import as_path
from jarvis.jobs import Job, Jobs, claude_command, edit_folder, undo_task


def test_file_urls_become_paths():
    assert as_path("file:///Users/v/My%20Deck/index.html") == "/Users/v/My Deck/index.html"
    assert as_path("https://youtube.com/watch?v=1") == "https://youtube.com/watch?v=1"


def test_edit_commands_may_write_only_in_the_job_folder():
    command = claude_command("fix slide 4", edit=True)
    allowed = command[command.index("--allowedTools") + 1].split(",")
    assert "Edit(./**)" in allowed and "Write(./**)" in allowed
    assert "Edit(./**)" not in claude_command("look")[claude_command("look").index("--allowedTools") + 1]


def test_edit_folder_is_the_documents_folder_inside_home(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    deck = tmp_path / "decks" / "roadmap"
    deck.mkdir(parents=True)
    (deck / "index.html").write_text("<h2>Slide</h2>")
    assert edit_folder(str(deck / "index.html")) == (deck, "")
    assert edit_folder(str(tmp_path))[0] is None  # never the whole home folder
    assert edit_folder("/etc/hosts")[0] is None
    assert edit_folder(str(deck / "missing.html"))[0] is None


def test_undo_puts_the_folder_back(monkeypatch, tmp_path):
    folder, snapshot = tmp_path / "deck", tmp_path / "snap"
    folder.mkdir()
    snapshot.mkdir()
    (snapshot / "index.html").write_text("before")
    (folder / "index.html").write_text("after")
    (folder / "new.css").write_text("added by the job")
    fake = Jobs()
    fake.all = {1: Job(1, "edit", "fix", status="done", folder=folder, snapshot=snapshot)}
    monkeypatch.setattr(jobs_module, "jobs", fake)
    assert undo_task({"task_id": 1}).startswith("Restored")
    assert (folder / "index.html").read_text() == "before" and not (folder / "new.css").exists()


def test_undo_refuses_read_only_tasks(monkeypatch):
    fake = Jobs()
    fake.all = {1: Job(1, "computer", "look", status="done")}
    monkeypatch.setattr(jobs_module, "jobs", fake)
    assert "can't undo" in undo_task({"task_id": 1}) or "didn't edit" in undo_task({"task_id": 1})
