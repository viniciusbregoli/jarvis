"""Mac actions the voice agent calls as ElevenLabs client tools. Each SPECS entry is synced to ElevenLabs by
`agent.ensure_agent` and its handler runs here, on the Mac."""

import datetime as dt
import subprocess
import tempfile
import threading
from pathlib import Path

from elevenlabs.client import ElevenLabs

from .agent import VOICE_ID, api_key

MEDIA_KEYS = {"play_pause": 16, "next": 17, "previous": 18}  # NX_KEYTYPE_* codes
CLIPBOARD_LIMIT = 4000


def run(*cmd: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=timeout)


def applescript_string(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(text: str):
    run("osascript", "-e", f"display notification {applescript_string(text)} with title {applescript_string('Jarvis')}")


def announce(text: str):
    """Notification plus speech, for things that happen outside a conversation (a timer going off)."""
    notify(text)
    audio = ElevenLabs(api_key=api_key()).text_to_speech.convert(VOICE_ID, text=text, output_format="mp3_44100_128")
    with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
        f.write(b"".join(audio))
        f.flush()
        # afplay, not sounddevice: the voice thread may be re-initializing PortAudio at the same moment.
        run("afplay", f.name, timeout=120)


def open_target(params: dict) -> str:
    target = params["target"].strip()
    result = run("open", target) if "://" in target else run("open", "-a", target)
    return f"Couldn't open {target}: {result.stderr.strip()}" if result.returncode else f"Opened {target}"


def press_media_key(code: int):
    import Quartz
    from AppKit import NSEvent

    for flags in (0xA00, 0xB00):  # key down, key up
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), flags, 0, 0, None, 8, (code << 16) | flags, -1
        )
        Quartz.CGEventPost(0, event.CGEvent())


def media(params: dict) -> str:
    action = params["action"]
    if action == "volume":
        level = max(0, min(100, int(params.get("level", 50))))
        run("osascript", "-e", f"set volume output volume {level}")
        return f"Volume set to {level}"
    if action in ("mute", "unmute"):
        run("osascript", "-e", f"set volume output muted {str(action == 'mute').lower()}")
        return f"{action.capitalize()}d"
    if action in MEDIA_KEYS:
        press_media_key(MEDIA_KEYS[action])
        return f"Sent {action.replace('_', '/')}"
    return f"Unknown media action: {action}"


def set_timer(params: dict) -> str:
    seconds, label = int(params["seconds"]), params.get("label") or "Timer"
    # shortcut: timers live in memory and are lost if Jarvis quits; use reminders for anything that must survive.
    threading.Timer(seconds, announce, [f"{label} is done."]).start()
    return f"Timer set for {seconds} seconds"


def reminder_script(text: str, due: dt.datetime) -> str:
    # Day goes to 1 first so moving from the 31st into a 30-day month can't overflow.
    return f"""tell application "Reminders"
    set d to current date
    set day of d to 1
    set year of d to {due.year}
    set month of d to {due.month}
    set day of d to {due.day}
    set time of d to {due.hour * 3600 + due.minute * 60}
    make new reminder with properties {{name:{applescript_string(text)}, remind me date:d}}
end tell"""


def add_reminder(params: dict) -> str:
    due = dt.datetime.fromisoformat(params["due"])
    result = run("osascript", "-e", reminder_script(params["text"], due))
    if result.returncode:
        return f"Couldn't create the reminder: {result.stderr.strip()}"
    return f"Reminder set for {due:%A %H:%M}"


def read_clipboard(_: dict) -> str:
    text = run("pbpaste").stdout
    return text[:CLIPBOARD_LIMIT] or "The clipboard is empty or holds no text."


def describe_screen(params: dict) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        shot, answer = Path(tmp, "screen.png"), Path(tmp, "answer.txt")
        if run("screencapture", "-x", str(shot)).returncode:
            return "Couldn't take a screenshot (Screen Recording permission?)"
        question = params.get("question") or "What is on this screen?"
        prompt = f"{question} Answer in two or three short spoken sentences, no markdown."
        run(
            "codex", "exec", "--skip-git-repo-check", "--ephemeral", "-s", "read-only",
            "-i", str(shot), "-o", str(answer), prompt,
            timeout=120,
        )  # fmt: skip
        return answer.read_text().strip() if answer.exists() else "Couldn't describe the screen."


def string(description: str) -> dict:
    return {"type": "string", "description": description}


SPECS = [
    {
        "name": "open_app",
        "handler": open_target,
        "description": "Open or focus a Mac app by name (e.g. Slack, Spotify) or open a URL in the browser.",
        "parameters": {
            "type": "object",
            "properties": {"target": string("App name, or a full URL starting with https://")},
            "required": ["target"],
        },
    },
    {
        "name": "media",
        "handler": media,
        "description": "Control audio: set the volume, mute or unmute, play/pause, next or previous track.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["volume", "mute", "unmute", "play_pause", "next", "previous"],
                    "description": "What to do",
                },
                "level": {"type": "integer", "description": "Volume 0 to 100, only for action=volume"},
            },
            "required": ["action"],
        },
    },
    {
        "name": "set_timer",
        "handler": set_timer,
        "description": "Start a countdown timer. Jarvis speaks and notifies when it ends.",
        "parameters": {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "Duration in seconds"},
                "label": string("Short name for the timer, e.g. 'Pasta'"),
            },
            "required": ["seconds"],
        },
    },
    {
        "name": "add_reminder",
        "handler": add_reminder,
        "description": "Create a reminder in Apple Reminders that alerts at a given local date and time.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": string("What to remind about"),
                "due": string("Local date and time in ISO format, e.g. 2026-10-23T17:00"),
            },
            "required": ["text", "due"],
        },
    },
    {
        "name": "read_clipboard",
        "handler": read_clipboard,
        "description": "Return the text currently on the clipboard.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_screen",
        "handler": describe_screen,
        "description": "Look at the user's screen and answer a question about it. Takes about 10 to 20 seconds.",
        "parameters": {
            "type": "object",
            "properties": {"question": string("What to find out about the screen")},
        },
        "timeout": 120,
    },
]
