"""Slide control: arrow keys and friends sent to the app that holds the presentation. When Vinicius says "next slide"
the frontmost app is usually the deck, but not always, and a Right arrow in a code editor would move his cursor, so
keys only go to a known deck app: the frontmost one if it is one (remembered from then on), else the last one used."""

import subprocess
import time

from AppKit import NSRunningApplication, NSWorkspace

KEY_DELAY = 0.05  # between the arrow presses of a "go to slide N", so the page keeps up
ACTIVATE_DELAY = 0.4  # for the app to come to the front before it gets keys
MAX_SLIDE = 300

RIGHT, LEFT, HOME, END, ESC = 124, 123, 115, 119, 53
# Steps are ("code", n) for a key code or ("key", character, modifiers) for a keystroke.
COMMON = {"next": [("code", RIGHT)], "previous": [("code", LEFT)], "first": [("code", HOME)], "last": [("code", END)]}
BROWSER_KEYS = {**COMMON, "fullscreen": [("key", "f", ())], "exit": [("code", ESC)]}  # "f" is the decks' own binding

# bundle id -> (name for messages, keys by action). Actions missing from a table fall back to COMMON.
DECK_APPS = {
    "app.zen-browser.zen": ("Zen", BROWSER_KEYS),
    "org.mozilla.firefox": ("Firefox", BROWSER_KEYS),
    "com.google.Chrome": ("Chrome", BROWSER_KEYS),
    "net.imput.helium": ("Helium", BROWSER_KEYS),
    "com.apple.Safari": ("Safari", BROWSER_KEYS),
    "com.apple.iWork.Keynote": (
        "Keynote",
        {**COMMON, "fullscreen": [("key", "p", ("command", "option"))], "exit": [("code", ESC)]},
    ),
    "com.microsoft.Powerpoint": (
        "PowerPoint",
        {**COMMON, "fullscreen": [("code", 36, ("command", "shift"))], "exit": [("code", ESC)]},
    ),
    "com.apple.Preview": (
        "Preview",
        {**COMMON, "fullscreen": [("key", "f", ("command", "shift"))], "exit": [("code", ESC)]},
    ),
}

DONE = {
    "next": "Moved to the next slide",
    "previous": "Moved to the previous slide",
    "first": "Went to the first slide",
    "last": "Went to the last slide",
    "fullscreen": "Toggled fullscreen",
    "exit": "Left the presentation (Esc)",
}

deck_app: str | None = None  # bundle id of the last app seen holding a deck


def remember_deck_app(bundle_id: str) -> bool:
    """Set the app `slide` falls back to when no deck app is in front, e.g. after opening a deck file. Bundle ids
    that aren't deck apps are ignored."""
    global deck_app
    if bundle_id not in DECK_APPS:
        return False
    deck_app = bundle_id
    return True


def pick_app(front: str, remembered: str | None) -> tuple[str | None, bool]:
    """(bundle id to send keys to, whether it has to be brought to the front first)."""
    if front in DECK_APPS:
        return front, False
    return (remembered, True) if remembered else (None, False)


def steps_for(bundle_id: str, action: str, number: int = 1) -> list[tuple]:
    keys = DECK_APPS[bundle_id][1]
    if action == "goto":
        return keys["first"] + keys["next"] * (number - 1)
    return keys[action]


def key_script(steps: list[tuple]) -> str:
    lines = []
    for step in steps:
        modifiers = step[-1] if len(step) == 3 else ()
        using = f" using {{{', '.join(m + ' down' for m in modifiers)}}}" if modifiers else ""
        lines.append(f"key code {step[1]}{using}" if step[0] == "code" else f'keystroke "{step[1]}"{using}')
        lines.append(f"delay {KEY_DELAY}")
    return 'tell application "System Events"\n' + "\n".join(lines[:-1]) + "\nend tell"


def frontmost_bundle_id() -> str:
    return NSWorkspace.sharedWorkspace().frontmostApplication().bundleIdentifier() or ""


def activate(bundle_id: str) -> bool:
    apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)
    if not apps:
        return False
    apps[0].activateWithOptions_(2)  # NSApplicationActivateIgnoringOtherApps
    time.sleep(ACTIVATE_DELAY)
    return True


def describe(action: str, number: int, name: str) -> str:
    if action == "goto":
        return f"Went to slide {number} in {name}"
    return f"{DONE[action]} in {name}"


def slide(params: dict) -> str:
    action = params["action"]
    number = int(params.get("number") or 0)
    if action != "goto" and action not in DONE:
        return f"Unknown slide action: {action}"
    if action == "goto" and not 1 <= number <= MAX_SLIDE:
        return f"Need a slide number between 1 and {MAX_SLIDE}."
    bundle_id, needs_front = pick_app(frontmost_bundle_id(), deck_app)
    if not bundle_id:
        return "No presentation is open (no browser, Keynote, PowerPoint or Preview was used for one)."
    remember_deck_app(bundle_id)
    name = DECK_APPS[bundle_id][0]
    if needs_front and not activate(bundle_id):
        return f"No presentation is open: {name} isn't running."
    steps = steps_for(bundle_id, action, number)
    result = subprocess.run(
        ["osascript", "-e", key_script(steps)], capture_output=True, text=True, timeout=10 + len(steps) * KEY_DELAY,
        check=False,
    )  # fmt: skip
    if result.returncode:
        return f"Couldn't send keys to {name}: {result.stderr.strip()}"
    return describe(action, number, name)


SPECS = [
    {
        "name": "slide",
        "handler": slide,
        "description": "Control the presentation Vinicius is showing (HTML deck in a browser, Keynote, PowerPoint, "
        "Preview) with the keyboard. Use it for 'passa o slide', 'próximo', 'volta', 'vai pro slide 7', 'primeiro "
        "slide', 'último', 'tela cheia', 'sai da apresentação'. It is instant and goes to the app showing the deck, "
        "even if another app is in front: never start a desktop task for slides. If it says no presentation is "
        "open, tell him.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["next", "previous", "first", "last", "goto", "fullscreen", "exit"],
                    "description": "next/previous slide, first/last slide, goto a slide number, toggle fullscreen "
                    "(starts the slideshow in Keynote, PowerPoint and Preview), exit (leave fullscreen or the "
                    "slideshow)",
                },
                "number": {"type": "integer", "description": "Slide number counting from 1, only for action=goto"},
            },
            "required": ["action"],
        },
    }
]
