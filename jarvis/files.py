"""Instant file lookup through Spotlight, for "acha a apresentação X" and "abre o relatório Y". It replaces a background
Claude job (about 15 s) with one mdfind call (well under a second)."""

import datetime as dt
import subprocess
from pathlib import Path

from AppKit import NSURL, NSBundle, NSWorkspace

from . import deck

MAX_RESULTS = 5
SEARCH_TIMEOUT = 5
MAX_CANDIDATES = 100  # ranked by mtime first, so the mdls batch for "last used" stays small
# Names that mark noise (caches, dependencies, app data) at any depth; hidden directories (.Trash, .jarvis with the
# edit snapshots, .git, .venv, .cache) are excluded as a class.
EXCLUDED_DIRS = {"library", "node_modules", "site-packages", "__pycache__", "cache", "caches"}
PRESENTATION_EXTS = {".key", ".pptx", ".ppt", ".odp", ".pdf", ".html", ".htm"}
DECK_ONLY_EXTS = PRESENTATION_EXTS - {".pdf"}  # a PDF only counts as a presentation when it was asked for as one
DOCUMENT_EXTS = {
    ".pdf", ".doc", ".docx", ".pages", ".txt", ".md", ".rtf", ".odt", ".xls", ".xlsx", ".numbers", ".csv", ".key",
    ".ppt", ".pptx", ".epub",
}  # fmt: skip
UNITS = [("year", 365 * 86400), ("month", 30 * 86400), ("week", 7 * 86400), ("day", 86400), ("hour", 3600),
         ("minute", 60)]  # fmt: skip


def split_words(query: str) -> list[str]:
    """The words to match; wildcards typed by the user are dropped so they can't widen the pattern."""
    return [word for word in query.replace("*", " ").replace("?", " ").split() if word]


def quoted(word: str) -> str:
    return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(words: list[str]) -> str:
    """Spotlight query where every word must be part of the name, ignoring case and accents (the `cd` flags)."""
    return " && ".join(f"(kMDItemFSName == {quoted(f'*{word}*')}cd)" for word in words)


def is_excluded(path: Path, home: Path) -> bool:
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        parts = path.parts
    return any(part.startswith(".") or part.lower() in EXCLUDED_DIRS for part in parts)


def doc_name(path: Path) -> str:
    """What a copy of the same document is called: an HTML deck is its folder, a file is its name without extension."""
    if path.name.lower() in ("index.html", "index.htm"):
        return path.parent.name.casefold()
    return (path.name if path.is_dir() else path.stem).casefold()


def to_deck(path: Path) -> Path | None:
    """The presentation a search hit stands for: the file itself, or the index.html inside a matching folder."""
    if path.suffix.lower() in PRESENTATION_EXTS:
        return path
    for index in ("index.html", "index.htm"):
        if path.is_dir() and (path / index).is_file():
            return path / index
    return None


def filter_kind(paths: list[Path], kind: str) -> list[Path]:
    if kind == "folder":
        return [path for path in paths if path.is_dir()]
    if kind == "document":
        return [path for path in paths if path.suffix.lower() in DOCUMENT_EXTS and not path.is_dir()]
    if kind != "presentation":
        return paths
    decks = list(dict.fromkeys(deck_path for path in paths if (deck_path := to_deck(path))))
    # A deck exported to PDF sits next to its HTML; the HTML is the one to present.
    html = {(path.parent, doc_name(path)) for path in decks if path.suffix.lower() in (".html", ".htm")}
    return [path for path in decks if path.suffix.lower() != ".pdf" or (path.parent, doc_name(path)) not in html]


def parse_last_used(output: str, count: int) -> list[dt.datetime | None]:
    """Dates from `mdls -raw` over several files: NUL-separated, "(null)" where Spotlight has no date."""
    values = output.split("\0")
    if len(values) != count:
        return [None] * count
    dates = []
    for value in values:
        try:
            dates.append(dt.datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S %z"))
        except ValueError:
            dates.append(None)
    return dates


def rank(paths: list[Path]) -> list[tuple[Path, dt.datetime, bool]]:
    """(path, when, was it last used rather than modified), most recent first."""
    paths = sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)[:MAX_CANDIDATES]
    used = [None] * len(paths)
    if paths:
        out = subprocess.run(
            ["mdls", "-name", "kMDItemLastUsedDate", "-raw", *map(str, paths)],
            capture_output=True, text=True, timeout=10, check=False,
        )  # fmt: skip
        used = parse_last_used(out.stdout, len(paths))
    ranked = [
        (path, last_used or dt.datetime.fromtimestamp(path.stat().st_mtime, dt.UTC), last_used is not None)
        for path, last_used in zip(paths, used)
    ]
    return sorted(ranked, key=lambda item: item[1], reverse=True)


def age_text(then: dt.datetime, now: dt.datetime) -> str:
    seconds = int((now - then).total_seconds())
    for unit, size in UNITS:
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'s' if count > 1 else ''} ago"
    return "just now"


def default_app_bundle_id(path: Path) -> str:
    """The app `open` would use for this file."""
    app = NSWorkspace.sharedWorkspace().URLForApplicationToOpenURL_(NSURL.fileURLWithPath_(str(path)))
    bundle = NSBundle.bundleWithURL_(app) if app else None
    return (bundle.bundleIdentifier() if bundle else "") or ""


def describe(path: Path, when: dt.datetime, used: bool, now: dt.datetime) -> str:
    return f"{path} ({'used' if used else 'modified'} {age_text(when, now)})"


def find_files(params: dict) -> str:
    words = split_words(params.get("query", ""))
    if not words:
        return "Need at least one word to search for."
    kind = params.get("kind") or "any"
    home = Path.home()
    try:
        found = subprocess.run(
            ["mdfind", "-onlyin", str(home), build_query(words)],
            capture_output=True, text=True, timeout=SEARCH_TIMEOUT, check=False,
        ).stdout.splitlines()  # fmt: skip
    except subprocess.TimeoutExpired:  # a one-letter query matches most of the disk
        return "The search took too long: the words are too generic, ask for a more specific name."
    paths = [Path(line) for line in found if line and not is_excluded(Path(line), home)]
    matches = filter_kind([path for path in paths if path.exists()], kind)
    ranked = rank(matches)
    shown = ranked[:MAX_RESULTS]
    if not shown:
        return f"No {'files' if kind == 'any' else kind + 's'} found matching '{' '.join(words)}'."
    now = dt.datetime.now(dt.UTC)
    lines = [describe(*item, now) for item in shown]
    if not params.get("open"):
        count = f"{len(matches)} match{'es' if len(matches) > 1 else ''}"
        more = f", showing the {MAX_RESULTS} most recent" if len(matches) > MAX_RESULTS else ""
        return f"Found {count}{more}:\n" + "\n".join(lines)

    best = shown[0][0]
    result = subprocess.run(["open", str(best)], capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        return f"Couldn't open {best}: {result.stderr.strip()}"
    copies = sum(doc_name(path) == doc_name(best) for path, *_ in ranked) - 1
    message = f"Opened {lines[0]}."
    if copies:
        message += f" {copies} other cop{'ies' if copies > 1 else 'y'} exist{'' if copies > 1 else 's'}."
    is_deck = kind == "presentation" or best.suffix.lower() in DECK_ONLY_EXTS
    if is_deck and deck.remember_deck_app(default_app_bundle_id(best)):
        message += " The slide tool now controls it."
    if len(lines) > 1:
        message += "\nOther matches:\n" + "\n".join(lines[1:])
    return message


SPECS = [
    {
        "name": "find_files",
        "handler": find_files,
        "description": "Instant search for files and folders in the home folder by name (Spotlight, under a second), "
        "newest first. Use it for 'acha a apresentação do roadmap', 'abre o relatório X', 'onde está o arquivo Y'. "
        "With open=true it opens the best match right away, and for a presentation the slide tool then controls it. "
        "Prefer it over a background task for anything that is just finding or opening a file by name. It matches "
        "names only, not file contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Words from the file or folder name, e.g. 'roadmap 2027'; each must be part of "
                    "the name (any case, accents ignored)",
                },
                "kind": {
                    "type": "string",
                    "enum": ["presentation", "document", "folder", "any"],
                    "description": "presentation: Keynote, PowerPoint, PDF and HTML decks; document: PDFs, text and "
                    "office files; folder: folders only. Default any.",
                },
                "open": {"type": "boolean", "description": "Open the best match right away"},
            },
            "required": ["query"],
        },
    }
]
