import datetime as dt

from jarvis import tools


def test_applescript_string_escapes_quotes_and_backslashes():
    assert tools.applescript_string('say "hi" \\ bye') == '"say \\"hi\\" \\\\ bye"'


def test_reminder_script_sets_day_to_1_before_month():
    script = tools.reminder_script("Call Ana", dt.datetime(2026, 11, 30, 17, 5))
    lines = [line.strip() for line in script.splitlines()]
    assert lines.index("set day of d to 1") < lines.index("set month of d to 11") < lines.index("set day of d to 30")
    assert "set time of d to 61500" in lines
    assert 'name:"Call Ana"' in script


class Result:
    def __init__(self, returncode=0):
        self.returncode, self.stderr = returncode, ""


def test_media_clamps_volume(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "run", lambda *cmd, **_: calls.append(cmd) or Result())
    assert tools.media({"action": "volume", "level": 150}) == "Volume set to 100"
    assert calls == [("osascript", "-e", "set volume output volume 100")]


def test_media_reports_failures(monkeypatch):
    monkeypatch.setattr(tools, "run", lambda *cmd, **_: Result(1))
    assert tools.media({"action": "volume", "level": 30}) == "Couldn't change the volume"
    assert tools.media({"action": "mute"}) == "Couldn't mute"


def test_media_status_reads_real_settings(monkeypatch):
    result = Result()
    result.stdout = "output volume:30, input volume:50, alert volume:100, output muted:false"
    monkeypatch.setattr(tools, "run", lambda *cmd, **_: result)
    assert tools.media({"action": "status"}).startswith("Current settings: output volume:30")


def test_open_target_uses_open_for_urls_and_open_a_for_apps(monkeypatch):
    calls = []
    monkeypatch.setattr(tools, "run", lambda *cmd, **_: calls.append(cmd) or Result())
    tools.open_target({"target": "https://github.com"})
    tools.open_target({"target": "Slack"})
    assert calls == [("open", "https://github.com"), ("open", "-a", "Slack")]


def test_every_spec_has_a_handler_and_object_parameters():
    for spec in tools.SPECS:
        assert callable(spec["handler"]) and spec["parameters"]["type"] == "object"


def test_greeting_follows_the_time_of_day():
    from jarvis.agent import greeting

    hours = {2: "Ainda acordado, senhor?", 9: "Bom dia, senhor.", 15: "Boa tarde, senhor.", 21: "Boa noite, senhor."}
    for hour, expected in hours.items():
        assert greeting(dt.datetime(2026, 10, 24, hour)) == expected
