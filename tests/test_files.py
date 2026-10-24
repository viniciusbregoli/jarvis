import datetime as dt
import os
from pathlib import Path
from types import SimpleNamespace

from jarvis import deck, files

HOME = Path("/Users/v")
NOW = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.UTC)


def test_build_query_requires_every_word_ignoring_case_and_accents():
    assert files.build_query(["roadmap", "2027"]) == (
        '(kMDItemFSName == "*roadmap*"cd) && (kMDItemFSName == "*2027*"cd)'
    )


def test_build_query_escapes_quotes_and_backslashes():
    assert files.build_query(['a"b\\c']) == '(kMDItemFSName == "*a\\"b\\\\c*"cd)'


def test_split_words_drops_wildcards_and_blanks():
    assert files.split_words("  road*map  2027? ") == ["road", "map", "2027"]
    assert files.split_words("*?") == []


def test_noise_is_excluded_and_ordinary_paths_are_not():
    excluded = [
        "Library/Caches/x.pdf",
        ".Trash/deck.key",
        ".jarvis/snapshots/1-deck/index.html",
        "Code/app/node_modules/pkg/index.html",
        "Code/app/.git/HEAD",
        "Code/app/.venv/lib/python3.12/site-packages/x.html",
        "Code/app/site-packages/x.html",
        "Code/app/Caches/x.html",
        "Code/app/.cache/x.html",
    ]
    assert all(files.is_excluded(HOME / path, HOME) for path in excluded)
    assert not files.is_excluded(HOME / "jarvis-demo/ideias-roadmap-2027/index.html", HOME)
    assert not files.is_excluded(HOME / "Documents/library-card.pdf", HOME)


def make_tree(tmp_path):
    html = tmp_path / "ideias-roadmap-2027"
    html.mkdir()
    (html / "index.html").write_text("<h2>Slide</h2>")
    (html / "ideias-roadmap-2027.pdf").write_text("pdf")
    (tmp_path / "roadmap.key").write_text("key")
    (tmp_path / "roadmap-notes.txt").write_text("notes")
    (tmp_path / "plain-folder").mkdir()
    return html


def names(paths, root):
    return sorted(str(path.relative_to(root)) for path in paths)


def test_presentations_are_decks_with_the_html_preferred_over_its_pdf(tmp_path):
    html = make_tree(tmp_path)
    hits = [html, html / "ideias-roadmap-2027.pdf", tmp_path / "roadmap.key", tmp_path / "roadmap-notes.txt"]
    assert names(files.filter_kind(hits, "presentation"), tmp_path) == [
        "ideias-roadmap-2027/index.html",
        "roadmap.key",
    ]


def test_a_pdf_without_an_html_deck_stays_a_presentation(tmp_path):
    make_tree(tmp_path)
    pdf = tmp_path / "ideias-roadmap-2027" / "ideias-roadmap-2027.pdf"
    assert files.filter_kind([pdf], "presentation") == [pdf]


def test_folders_and_documents_are_told_apart(tmp_path):
    html = make_tree(tmp_path)
    hits = [html, tmp_path / "roadmap.key", tmp_path / "roadmap-notes.txt", tmp_path / "plain-folder"]
    assert names(files.filter_kind(hits, "folder"), tmp_path) == ["ideias-roadmap-2027", "plain-folder"]
    assert names(files.filter_kind(hits, "document"), tmp_path) == ["roadmap-notes.txt", "roadmap.key"]
    assert files.filter_kind(hits, "any") == hits


def test_doc_name_treats_an_html_deck_as_its_folder(tmp_path):
    html = make_tree(tmp_path)
    assert files.doc_name(html / "index.html") == files.doc_name(html / "ideias-roadmap-2027.pdf") == html.name


def test_parse_last_used_reads_dates_and_nulls():
    output = "2026-10-21 14:03:11 +0000\0(null)"
    assert files.parse_last_used(output, 2) == [dt.datetime(2026, 10, 21, 14, 3, 11, tzinfo=dt.UTC), None]
    assert files.parse_last_used(output, 3) == [None, None, None]  # a missing file shifts the output: trust none


def test_age_text_uses_the_largest_unit():
    assert files.age_text(NOW - dt.timedelta(seconds=20), NOW) == "just now"
    assert files.age_text(NOW - dt.timedelta(minutes=1), NOW) == "1 minute ago"
    assert files.age_text(NOW - dt.timedelta(hours=5), NOW) == "5 hours ago"
    assert files.age_text(NOW - dt.timedelta(days=2, hours=3), NOW) == "2 days ago"
    assert files.age_text(NOW - dt.timedelta(days=45), NOW) == "1 month ago"


def test_rank_puts_the_last_used_first_and_falls_back_to_mtime(monkeypatch, tmp_path):
    old, recent, unused = (tmp_path / name for name in ("old.pdf", "recent.pdf", "unused.pdf"))
    for path, age in ((old, 30), (recent, 20), (unused, 3)):
        path.write_text("x")
        os.utime(path, (NOW.timestamp() - age * 86400,) * 2)
    # Spotlight says old.pdf was opened yesterday, which beats unused.pdf's modification three days ago.
    stdout = "(null)\x00(null)\x002026-10-22 12:00:00 +0000"
    seen = []
    monkeypatch.setattr(
        files.subprocess, "run", lambda cmd, **_: seen.append(cmd) or SimpleNamespace(stdout=stdout)
    )  # mtime order sent to mdls: unused, recent, old
    paths = [old, recent, unused]
    result = files.rank(paths)
    assert [path for path, *_ in result] == [old, unused, recent]
    assert [used for *_, used in result] == [True, False, False]
    assert [Path(arg) for arg in seen[0][4:]] == [unused, recent, old]


def fake_system(monkeypatch, home, found):
    """mdfind returns `found`, mdls reports nothing, `open` succeeds."""
    calls = []

    def run(cmd, **_):
        calls.append(cmd)
        if cmd[0] == "mdfind":
            return SimpleNamespace(stdout="\n".join(map(str, found)), returncode=0)
        if cmd[0] == "mdls":
            return SimpleNamespace(stdout="\0".join(["(null)"] * (len(cmd) - 4)), returncode=0)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    monkeypatch.setattr(files.subprocess, "run", run)
    monkeypatch.setattr(files.Path, "home", lambda: home)
    return calls


def test_find_files_lists_matches_newest_first_and_skips_noise(monkeypatch, tmp_path):
    new, old = tmp_path / "roadmap-new.pdf", tmp_path / "roadmap-old.pdf"
    snapshot = tmp_path / ".jarvis" / "snapshots" / "roadmap-new.pdf"
    snapshot.parent.mkdir(parents=True)
    for path, age in ((new, 1), (old, 5), (snapshot, 0)):
        path.write_text("x")
        os.utime(path, (NOW.timestamp() - age * 86400,) * 2)
    calls = fake_system(monkeypatch, tmp_path, [old, new, snapshot])
    result = files.find_files({"query": "roadmap"})
    lines = result.splitlines()
    assert lines[0] == "Found 2 matches:"
    assert lines[1].startswith(str(new)) and lines[2].startswith(str(old))
    assert ".jarvis" not in result
    assert calls[0][:3] == ["mdfind", "-onlyin", str(tmp_path)] and calls[0][3] == files.build_query(["roadmap"])


def test_find_files_without_matches_or_words(monkeypatch, tmp_path):
    fake_system(monkeypatch, tmp_path, [])
    assert files.find_files({"query": "nothing", "kind": "presentation"}) == (
        "No presentations found matching 'nothing'."
    )
    assert files.find_files({"query": "  "}) == "Need at least one word to search for."


def test_find_files_reports_a_search_that_takes_too_long(monkeypatch, tmp_path):
    def run(cmd, **_):
        raise files.subprocess.TimeoutExpired(cmd, 5)

    monkeypatch.setattr(files.subprocess, "run", run)
    monkeypatch.setattr(files.Path, "home", lambda: tmp_path)
    assert files.find_files({"query": "a"}).startswith("The search took too long")


def test_find_files_opens_the_best_match_mentions_copies_and_remembers_the_deck_app(monkeypatch, tmp_path):
    mine, copy = tmp_path / "talk", tmp_path / "backup" / "talk"
    for folder, age in ((mine, 1), (copy, 9)):
        folder.mkdir(parents=True)
        (folder / "index.html").write_text("x")
        os.utime(folder / "index.html", (NOW.timestamp() - age * 86400,) * 2)
    calls = fake_system(monkeypatch, tmp_path, [copy, mine])
    monkeypatch.setattr(deck, "deck_app", None)
    monkeypatch.setattr(files, "default_app_bundle_id", lambda path: "app.zen-browser.zen")
    result = files.find_files({"query": "talk", "kind": "presentation", "open": True})
    assert result.startswith(f"Opened {mine / 'index.html'} (modified")
    assert "1 other copy exists." in result and str(copy / "index.html") in result
    assert ["open", str(mine / "index.html")] in calls
    assert deck.deck_app == "app.zen-browser.zen"


def test_find_files_does_not_remember_an_app_for_plain_documents(monkeypatch, tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("x")
    fake_system(monkeypatch, tmp_path, [doc])
    monkeypatch.setattr(deck, "deck_app", None)
    monkeypatch.setattr(files, "default_app_bundle_id", lambda path: "app.zen-browser.zen")
    assert files.find_files({"query": "notes", "open": True}).startswith(f"Opened {doc}")
    assert deck.deck_app is None


def test_find_files_reports_open_failures(monkeypatch, tmp_path):
    doc = tmp_path / "notes.txt"
    doc.write_text("x")
    calls = fake_system(monkeypatch, tmp_path, [doc])
    run = files.subprocess.run
    monkeypatch.setattr(
        files.subprocess,
        "run",
        lambda cmd, **kw: (
            SimpleNamespace(stdout="", stderr="no app", returncode=1) if cmd[0] == "open" else run(cmd, **kw)
        ),
    )
    assert files.find_files({"query": "notes", "open": True}) == f"Couldn't open {doc}: no app"
    assert calls
