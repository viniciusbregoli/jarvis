from types import SimpleNamespace

import pytest

from jarvis import deck

ZEN, KEYNOTE, POWERPOINT, EDITOR = (
    "app.zen-browser.zen",
    "com.apple.iWork.Keynote",
    "com.microsoft.Powerpoint",
    "dev.zed.Zed",
)


@pytest.fixture(autouse=True)
def no_remembered_app(monkeypatch):
    monkeypatch.setattr(deck, "deck_app", None)


def test_a_deck_app_in_front_is_used_without_activating():
    assert deck.pick_app(ZEN, None) == (ZEN, False)
    assert deck.pick_app(KEYNOTE, ZEN) == (KEYNOTE, False)


def test_another_app_in_front_falls_back_to_the_remembered_deck_app_and_activates_it():
    assert deck.pick_app(EDITOR, ZEN) == (ZEN, True)


def test_no_deck_app_anywhere_means_nothing_to_send_keys_to():
    assert deck.pick_app(EDITOR, None) == (None, False)


def test_remember_deck_app_only_accepts_deck_apps():
    assert deck.remember_deck_app(KEYNOTE) and deck.deck_app == KEYNOTE
    assert not deck.remember_deck_app(EDITOR) and deck.deck_app == KEYNOTE


def test_goto_goes_home_then_presses_right_one_time_fewer_than_the_number():
    steps = deck.steps_for(ZEN, "goto", 4)
    assert steps == [("code", deck.HOME)] + [("code", deck.RIGHT)] * 3
    assert deck.steps_for(ZEN, "goto", 1) == [("code", deck.HOME)]


def test_key_script_sends_every_key_in_one_system_events_block_with_delays():
    script = deck.key_script(deck.steps_for(ZEN, "goto", 3))
    assert script == (
        'tell application "System Events"\nkey code 115\ndelay 0.05\nkey code 124\ndelay 0.05\nkey code 124\nend tell'
    )


def test_key_script_handles_keystrokes_and_modifiers():
    assert 'keystroke "f"' in deck.key_script(deck.steps_for(ZEN, "fullscreen"))
    assert 'keystroke "p" using {command down, option down}' in deck.key_script(deck.steps_for(KEYNOTE, "fullscreen"))
    assert "key code 36 using {command down, shift down}" in deck.key_script(deck.steps_for(POWERPOINT, "fullscreen"))


def test_every_app_covers_every_action():
    for bundle_id in deck.DECK_APPS:
        for action in ("next", "previous", "first", "last", "fullscreen", "exit"):
            assert deck.steps_for(bundle_id, action)


def fake_front(monkeypatch, bundle_id):
    monkeypatch.setattr(deck, "frontmost_bundle_id", lambda: bundle_id)


def fake_osascript(monkeypatch, returncode=0):
    calls = []

    def run(cmd, **_):
        calls.append(cmd[2])
        return SimpleNamespace(returncode=returncode, stderr="not allowed")

    monkeypatch.setattr(deck.subprocess, "run", run)
    return calls


def test_slide_sends_keys_to_the_front_deck_app_and_remembers_it(monkeypatch):
    fake_front(monkeypatch, ZEN)
    calls = fake_osascript(monkeypatch)
    assert deck.slide({"action": "next"}) == "Moved to the next slide in Zen"
    assert calls == ['tell application "System Events"\nkey code 124\nend tell']
    assert deck.deck_app == ZEN


def test_slide_goto_reports_the_number(monkeypatch):
    fake_front(monkeypatch, KEYNOTE)
    fake_osascript(monkeypatch)
    assert deck.slide({"action": "goto", "number": 7}) == "Went to slide 7 in Keynote"


def test_slide_from_another_app_activates_the_remembered_deck_app_first(monkeypatch):
    deck.remember_deck_app(ZEN)
    fake_front(monkeypatch, EDITOR)
    calls, activated = fake_osascript(monkeypatch), []
    monkeypatch.setattr(deck, "activate", lambda bundle_id: activated.append(bundle_id) or True)
    assert deck.slide({"action": "previous"}) == "Moved to the previous slide in Zen"
    assert activated == [ZEN] and len(calls) == 1


def test_slide_with_no_deck_app_sends_nothing(monkeypatch):
    fake_front(monkeypatch, EDITOR)
    calls = fake_osascript(monkeypatch)
    assert deck.slide({"action": "next"}).startswith("No presentation is open")
    assert calls == []


def test_slide_with_a_closed_remembered_app_sends_nothing(monkeypatch):
    deck.remember_deck_app(KEYNOTE)
    fake_front(monkeypatch, EDITOR)
    calls = fake_osascript(monkeypatch)
    monkeypatch.setattr(deck, "activate", lambda bundle_id: False)
    assert deck.slide({"action": "next"}) == "No presentation is open: Keynote isn't running."
    assert calls == []


def test_slide_validates_its_arguments(monkeypatch):
    calls = fake_osascript(monkeypatch)
    assert deck.slide({"action": "goto"}).startswith("Need a slide number")
    assert deck.slide({"action": "goto", "number": 5000}).startswith("Need a slide number")
    assert deck.slide({"action": "explode"}) == "Unknown slide action: explode"
    assert calls == []


def test_slide_reports_osascript_failures(monkeypatch):
    fake_front(monkeypatch, ZEN)
    fake_osascript(monkeypatch, returncode=1)
    assert deck.slide({"action": "exit"}) == "Couldn't send keys to Zen: not allowed"
