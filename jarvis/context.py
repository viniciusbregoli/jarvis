"""What's in front of Vinicius: the frontmost app, its window title, and the document or page it shows. While he talks
to Jarvis the frontmost app stays whatever he was using, since the HAL bar never takes focus."""

import glob
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import unquote, urlparse

import lz4.block
from AppKit import NSWorkspace

# Firefox-based browsers don't expose tabs to AppleScript, but save their session (with the selected tab) every few
# seconds to this file.
FIREFOX_SESSIONS = {
    "app.zen-browser.zen": "~/Library/Application Support/zen/Profiles/*/sessionstore-backups/recovery.jsonlz4",
    "org.mozilla.firefox": "~/Library/Application Support/Firefox/Profiles/*/sessionstore-backups/recovery.jsonlz4",
}
CHROMIUM_APPS = {"com.google.Chrome": "Google Chrome", "net.imput.helium": "Helium"}


def osascript(script: str) -> str:
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def window_title(process: str) -> str:
    return osascript(f'tell application "System Events" to tell process "{process}" to get name of front window')


def ax_document(process: str) -> str:
    """The open document's file URL, which document-based apps (Keynote, Preview, TextEdit) report to Accessibility."""
    script = f'tell application "System Events" to tell process "{process}" to get value of attribute "AXDocument" of front window'
    value = osascript(script)
    return "" if value in ("", "missing value") else value


def firefox_selected_url(pattern: str) -> str:
    files = glob.glob(os.path.expanduser(pattern))
    if not files:
        return ""
    data = Path(max(files, key=os.path.getmtime)).read_bytes()
    if data[:8] != b"mozLz40\0":
        return ""
    session = json.loads(lz4.block.decompress(data[8:]))
    window = session["windows"][session.get("selectedWindow", 1) - 1]
    tab = window["tabs"][window["selected"] - 1]
    return tab["entries"][tab["index"] - 1].get("url", "")


def location_of(bundle_id: str, process: str) -> str:
    if bundle_id in FIREFOX_SESSIONS:
        return firefox_selected_url(FIREFOX_SESSIONS[bundle_id])
    if bundle_id in CHROMIUM_APPS:
        return osascript(f'tell application "{CHROMIUM_APPS[bundle_id]}" to get URL of active tab of front window')
    if bundle_id == "com.apple.Safari":
        return osascript('tell application "Safari" to get URL of front document')
    return ax_document(process)


def as_path(location: str) -> str:
    """A file:// URL as a local path; anything else unchanged."""
    if location.startswith("file://"):
        return unquote(urlparse(location).path)
    return location


def front_document(_: dict) -> str:
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    name, bundle_id = app.localizedName(), app.bundleIdentifier() or ""
    location = as_path(location_of(bundle_id, name))
    title = window_title(name)
    parts = [f"App: {name}", f"Window: {title or '(unknown)'}"]
    if location:
        parts.append(f"{'File' if location.startswith('/') else 'URL'}: {location}")
    return " | ".join(parts)


SPECS = [
    {
        "name": "front_document",
        "handler": front_document,
        "description": "What Vinicius has in front of him right now: the frontmost app, its window title, and the "
        "file path or URL it shows (a slide deck, PDF, web page...). Use it for 'this presentation', 'this file', "
        "'what I'm looking at'.",
        "parameters": {"type": "object", "properties": {}},
    }
]
