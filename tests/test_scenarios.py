import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "scenarios.py"
spec = importlib.util.spec_from_file_location("scenarios_script", SCRIPT)
scn = importlib.util.module_from_spec(spec)
sys.modules["scenarios_script"] = scn  # dataclasses look their module up here
spec.loader.exec_module(scn)


def call(tool, **args):
    return {"tool": tool, "args": args}


def step(**fields):
    return scn.parse_scenarios(f'[[scenario]]\nname = "x"\nsay = "oi"\n{fields.pop("toml", "")}')[0].steps[0]


def test_value_matching():
    assert scn.value_matches("edit", "edit")
    assert not scn.value_matches("edit", "computer")
    assert scn.value_matches(30, 30) and not scn.value_matches(30, 31)
    assert scn.value_matches(True, True) and not scn.value_matches(True, None)
    assert scn.value_matches("re:deck", "/Users/x/Deck/index.html")  # case-insensitive search
    assert not scn.value_matches("re:^deck", "/Users/x/deck")
    assert scn.value_matches("*", "anything") and not scn.value_matches("*", "") and not scn.value_matches("*", None)
    assert not scn.value_matches("re:x", None)
    assert scn.value_matches("!", None) and scn.value_matches("!", False) and not scn.value_matches("!", True)


def test_alternative_tools_accept_either_and_count_as_available_if_one_exists():
    expected = scn.as_call("start_task|lookup")
    assert scn.call_matches(expected, call("lookup", question="q"))
    assert scn.call_matches(expected, call("start_task", kind="computer"))
    assert not scn.call_matches(expected, call("open_app"))
    scenario = scn.parse_scenarios('[[scenario]]\nname = "a"\nsay = "x"\ncalls = ["start_task|lookup"]')[0]
    assert scn.missing_tools(scenario, {"lookup"}) == []
    assert scn.missing_tools(scenario, {"media"}) == ["start_task"]


def test_call_matching_requires_every_expected_arg():
    expected = scn.as_call({"tool": "start_task", "args": {"kind": "edit", "reload": True}})
    assert scn.call_matches(expected, call("start_task", kind="edit", reload=True, task="t"))
    assert not scn.call_matches(expected, call("start_task", kind="edit"))
    assert not scn.call_matches(expected, call("open_app", kind="edit", reload=True))
    assert scn.call_matches(scn.as_call("media"), call("media", action="next"))


def test_no_tool_step_fails_on_any_call():
    s = step(toml='calls = "none"')
    assert scn.check_step(s, [], "Tudo em ordem, senhor.") == []
    assert scn.check_step(s, [call("lookup", question="q")], "") == ["expected no tool call, got lookup(question='q')"]


def test_calls_are_matched_in_order_and_extra_calls_are_allowed():
    s = step(toml='calls = ["front_document", {tool = "start_task", args = {kind = "edit"}}]')
    assert scn.check_step(s, [call("front_document"), call("start_task", kind="edit"), call("task_status")], "") == []
    assert "got nothing" in scn.check_step(s, [call("front_document")], "")[0]
    problems = scn.check_step(s, [call("start_task", kind="edit")], "")
    assert problems == ["call 1: expected front_document(), got start_task(kind='edit')"]


def test_forbidden_calls_fail_even_after_the_expected_ones():
    s = step(toml='calls = ["slide"]\nforbid = [{tool = "start_task", args = {kind = "desktop"}}, "describe_screen"]')
    assert scn.check_step(s, [call("slide", action="next")], "") == []
    assert scn.check_step(s, [call("slide"), call("start_task", kind="computer")], "") == []
    bad = scn.check_step(s, [call("slide"), call("start_task", kind="desktop", task="t"), call("describe_screen")], "")
    assert len(bad) == 2 and all(p.startswith("forbidden call") for p in bad)


def test_reply_expectations():
    s = step(toml='calls = "none"\nreply = "posso usar a tela"\nno_reply = "feito"')
    assert scn.check_step(s, [], "Posso usar a tela, senhor?") == []
    assert scn.check_step(s, [], "Feito.") == [
        "reply should match /posso usar a tela/",
        "reply should not match /feito/",
    ]


def test_parse_single_turn_shorthand_and_steps():
    scenarios = scn.parse_scenarios(
        """
[[scenario]]
name = "one"
say = "passa o slide"
calls = [{ tool = "slide", args = { action = "next" } }]

[[scenario]]
name = "two"
results = { front_document = "App: Zen" }

[[scenario.step]]
say = "corrige o slide 4"
calls = ["front_document", { tool = "start_task", args = { kind = "edit" } }]

[[scenario.step]]
say = "desfaz"
calls = ["undo_task"]
"""
    )
    one, two = scenarios
    assert one.steps[0].calls == [{"tool": "slide", "args": {"action": "next"}}]
    assert [s.say for s in two.steps] == ["corrige o slide 4", "desfaz"]
    assert two.results == {"front_document": "App: Zen"}
    assert two.tools_expected() == [["front_document"], ["start_task"], ["undo_task"]]


def test_parse_rejects_bad_files():
    with pytest.raises(ValueError, match="duplicate"):
        scn.parse_scenarios('[[scenario]]\nname = "a"\nsay = "x"\n[[scenario]]\nname = "a"\nsay = "y"')
    with pytest.raises(ValueError, match="no steps"):
        scn.parse_scenarios('[[scenario]]\nname = "a"')
    with pytest.raises(ValueError, match="without a name"):
        scn.parse_scenarios('[[scenario]]\nsay = "x"')


def test_missing_tools_are_reported_not_failed():
    scenario = scn.parse_scenarios('[[scenario]]\nname = "a"\nsay = "x"\ncalls = ["slide"]')[0]
    assert scn.missing_tools(scenario, {"media"}) == ["slide"]
    assert scn.missing_tools(scenario, {"slide", "media"}) == []
    none = scn.parse_scenarios('[[scenario]]\nname = "a"\nsay = "x"\ncalls = "none"')[0]
    assert scn.missing_tools(none, set()) == []


def test_the_shipped_battery_parses_and_names_are_unique():
    scenarios = scn.parse_scenarios(scn.BATTERY.read_text())
    assert len(scenarios) == len({s.name for s in scenarios})
    assert all(s.steps and all(step.say for step in s.steps) for s in scenarios)


def test_speaking_before_the_first_tool_call_is_flagged_unless_allowed():
    quiet = step(toml='calls = ["media"]')
    assert scn.check_step(quiet, [call("media")], "Feito.") == []
    assert scn.check_step(quiet, [call("media")], "Feito.", "Vou ajustar o volume.") == [
        "spoke before acting: 'Vou ajustar o volume.'"
    ]
    allowed = step(toml='calls = ["lookup"]\nannounce = true')
    assert scn.check_step(allowed, [call("lookup")], "24 graus.", "Um momento.") == []
    chat = step(toml='calls = "none"')
    assert scn.check_step(chat, [], "Tudo bem.", "Tudo bem.") == []


def test_turn_result_knows_when_it_announced():
    assert scn.TurnResult(calls=[call("media")], said_first=["Vou ver."]).announced
    assert not scn.TurnResult(calls=[call("media")]).announced
    assert not scn.TurnResult(said_first=["Oi."]).announced
