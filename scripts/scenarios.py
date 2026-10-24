"""Scenario battery for the Jarvis voice agent: Portuguese phrases, each with the tool call it should trigger.

    uv run python scripts/scenarios.py [--only substring[,substring]] [--list] [--repeat N] [--no-sync] [--verbose]

Runs against a separate ElevenLabs agent ("Jarvis scenarios") built from the live agent's CONFIG and tool specs, in
text-only mode (no TTS, no audio) with every tool mocked: the handlers never run, each call is recorded and answered
with a canned result. The live "Jarvis" agent and its tool objects are never touched. The scenarios live in
scenarios/battery.toml; it needs the ElevenLabs key and spends credits, so it is not part of the test suite.
"""

import argparse
import concurrent.futures
import copy
import datetime as dt
import json
import re
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from websockets.exceptions import ConnectionClosed

ROOT = Path(__file__).resolve().parent.parent
BATTERY = ROOT / "scenarios" / "battery.toml"
SCENARIO_AGENT = "Jarvis scenarios"
# Fixed so "amanhã às 9" always means the same timestamp.
NOW = dt.datetime(2026, 10, 23, 14, 30)
SETTLE_SECONDS = 3.0  # quiet time after the agent's reply before the turn counts as finished
TOOL_WAIT_SECONDS = 30.0  # longest the agent may take to reply after a tool result
TURN_TIMEOUT = 60.0

# What the real handlers return in the common case; scenarios override per tool.
DEFAULT_RESULTS = {
    "open_app": "Opened it",
    "media": "Done",
    "set_timer": "Timer set",
    "add_reminder": "Reminder set",
    "read_clipboard": "Reunião com a Ana na quinta às 15h",
    "describe_screen": "Um editor de código com um arquivo Python aberto e um terminal embaixo.",
    "lookup": "Em São Paulo faz 24 graus, com chuva fraca à tarde.",
    "start_task": "Started task 1. Its result will arrive as an update when it finishes.",
    "undo_task": "Restored the folder to how it was before task 1.",
    "task_status": "Tarefa 1 (computer, running, há 1 min): procurar a apresentação do roadmap",
    "cancel_task": "Cancelled task 1.",
    "reply_to_task": "Started task 2, continuing task 1.",
    "slide": "Done",
    "find_files": "Found it. Paths: /Users/x/Documents/roadmap-2027.key",
}
GENERIC_RESULT = "Done"
SPOKE = "spoke before acting:"
TOTALS = [("pass", "passed"), ("fail", "failed"), ("error", "errors"), ("skip", "skipped")]


# --- scenario file ---------------------------------------------------------------------------------------------------


@dataclass
class Step:
    say: str
    calls: list[dict] = field(default_factory=list)  # expected leading tool calls, in order; empty means "no tool"
    forbid: list[dict] = field(default_factory=list)  # calls that must not happen at all
    reply: str | None = None  # regex the agent's spoken reply must match
    no_reply: str | None = None  # regex the reply must not match
    announce: bool = False  # allow speaking before the first tool call (otherwise that is a failure)
    extra_calls: bool = False  # allow tool calls beyond the expected ones (otherwise that is a failure)


@dataclass
class Scenario:
    name: str
    steps: list[Step]
    results: dict[str, str] = field(default_factory=dict)  # canned tool results, by tool name
    note: str = ""

    def tools_expected(self) -> list[list[str]]:
        """The tools each expected call could be, one list of alternatives per call."""
        return [c["tool"].split("|") for step in self.steps for c in step.calls]


def as_call(raw) -> dict:
    """A tool expectation: 'tool_name' or {tool = 'tool_name', args = {...}}."""
    call = {"tool": raw} if isinstance(raw, str) else dict(raw)
    if "tool" not in call:
        raise ValueError(f"expectation without a tool: {raw!r}")
    call.setdefault("args", {})
    return call


def parse_step(raw: dict, where: str) -> Step:
    if "say" not in raw:
        raise ValueError(f"{where}: step without 'say'")
    calls = [] if raw.get("calls") in (None, "none", "any") else [as_call(c) for c in raw["calls"]]
    return Step(
        say=raw["say"],
        calls=calls,
        forbid=[as_call(c) for c in raw.get("forbid", [])],
        reply=raw.get("reply"),
        no_reply=raw.get("no_reply"),
        announce=raw.get("announce", False),
        extra_calls=raw.get("extra_calls", raw.get("calls") == "any"),
    )


def parse_scenarios(text: str) -> list[Scenario]:
    data = tomllib.loads(text)
    scenarios, seen = [], set()
    for raw in data.get("scenario", []):
        name = raw.get("name")
        if not name:
            raise ValueError("scenario without a name")
        if name in seen:
            raise ValueError(f"duplicate scenario name: {name}")
        seen.add(name)
        if "say" in raw:  # single-turn shorthand: the step fields sit on the scenario itself
            raw = {
                **raw,
                "step": [
                    {
                        k: raw[k]
                        for k in ("say", "calls", "forbid", "reply", "no_reply", "announce", "extra_calls")
                        if k in raw
                    }
                ],
            }
        if not raw.get("step"):
            raise ValueError(f"{name}: no steps")
        steps = [parse_step(s, name) for s in raw["step"]]
        scenarios.append(Scenario(name, steps, dict(raw.get("results", {})), raw.get("note", "")))
    return scenarios


# --- expectations ----------------------------------------------------------------------------------------------------


def value_matches(expected, actual) -> bool:
    """Literals compare equal; 're:<pattern>' searches the text of the value; '*' only requires it to be present and
    '!' that it be absent or false."""
    if expected == "*":
        return actual not in (None, "")
    if expected == "!":
        return actual in (None, "", False)
    if isinstance(expected, str) and expected.startswith("re:"):
        return actual is not None and re.search(expected[3:], str(actual), re.IGNORECASE) is not None
    return actual == expected


def call_matches(expected: dict, call: dict) -> bool:
    if call["tool"] not in expected["tool"].split("|"):  # "start_task|lookup" accepts either
        return False
    return all(value_matches(want, call["args"].get(key)) for key, want in expected["args"].items())


def describe_call(call: dict) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in call["args"].items())
    return f"{call['tool']}({args})"


def acted(calls: list[dict]) -> bool:
    """Whether the agent did something besides saying goodbye, whose sign-off is meant to come first."""
    return any(call["tool"] != "end_call" for call in calls)


def check_step(step: Step, calls: list[dict], reply: str, said_first: str = "") -> list[str]:
    """Why the step failed, as a list of problems (empty when it passed)."""
    problems = []
    if not step.calls and calls and not step.extra_calls:
        problems.append(f"expected no tool call, got {describe_call(calls[0])}")
    for i, want in enumerate(step.calls):
        if i >= len(calls):
            problems.append(f"call {i + 1}: expected {describe_call(want)}, got nothing")
            break
        if not call_matches(want, calls[i]):
            problems.append(f"call {i + 1}: expected {describe_call(want)}, got {describe_call(calls[i])}")
            break
    if step.calls and len(calls) > len(step.calls) and not step.extra_calls:
        problems.append(f"unexpected extra call: {describe_call(calls[len(step.calls)])}")
    for banned in step.forbid:
        for call in calls:
            if call_matches(banned, call):
                problems.append(f"forbidden call: {describe_call(call)}")
    if acted(calls) and said_first and not step.announce:
        problems.append(f"{SPOKE} {said_first!r}")
    if step.reply and not re.search(step.reply, reply, re.IGNORECASE):
        problems.append(f"reply should match /{step.reply}/")
    if step.no_reply and re.search(step.no_reply, reply, re.IGNORECASE):
        problems.append(f"reply should not match /{step.no_reply}/")
    return problems


def missing_tools(scenario: Scenario, available: set[str]) -> list[str]:
    return sorted({options[0] for options in scenario.tools_expected() if not set(options) & available})


# --- the scenario agent ----------------------------------------------------------------------------------------------


def load_specs() -> list[dict]:
    """The same tool specs the app syncs, so a tool added to the code is covered without touching this script."""
    sys.path.insert(0, str(ROOT))
    from jarvis.__main__ import SPECS

    return SPECS


def scenario_config() -> dict:
    from jarvis.agent import CONFIG

    config = copy.deepcopy(CONFIG)
    config["agent"]["first_message"] = ""  # the greeting would only cost a turn; scenarios start with the user
    # The filler lines ("Verificando, senhor.") come from the platform, not the model, and would show up as replies,
    # and as "spoke before acting" when a tool is slow.
    config["turn"] = {**config["turn"], "soft_timeout_config": {"timeout_seconds": -1}}
    return config


def sync_scenario_agent(client, specs: list[dict]) -> str:
    """Create or update the scenario agent. Its tools are separate objects from the live agent's (same names, own
    ids), found through the scenario agent's own tool_ids, so the live Jarvis is never touched."""
    from elevenlabs import ToolRequestModel

    from jarvis.agent import tool_config

    tools = client.conversational_ai.tools
    agents = client.conversational_ai.agents
    found = [a for a in agents.list(search=SCENARIO_AGENT).agents if a.name == SCENARIO_AGENT]
    own: dict[str, str] = {}
    if found:
        current = agents.get(found[0].agent_id).conversation_config.agent.prompt.tool_ids or []
        own = {tools.get(tool_id).tool_config.name: tool_id for tool_id in current}
    ids = []
    for spec in specs:
        request = ToolRequestModel(tool_config=tool_config(spec))
        tool_id = own.get(spec["name"])
        ids.append(tools.update(tool_id, request=request).id if tool_id else tools.create(request=request).id)
    config = scenario_config()
    config["agent"]["prompt"] = {**config["agent"]["prompt"], "tool_ids": ids}
    settings = {"overrides": {"conversation_config_override": {"conversation": {"text_only": True}}}}
    if found:
        agents.update(agent_id=found[0].agent_id, conversation_config=config, platform_settings=settings)
        return found[0].agent_id
    return agents.create(name=SCENARIO_AGENT, conversation_config=config, platform_settings=settings).agent_id


def find_scenario_agent(client) -> str:
    found = [a for a in client.conversational_ai.agents.list(search=SCENARIO_AGENT).agents if a.name == SCENARIO_AGENT]
    if not found:
        sys.exit(f'No "{SCENARIO_AGENT}" agent yet: run without --no-sync once.')
    return found[0].agent_id


def available_tools(client, agent_id: str) -> set[str]:
    ids = client.conversational_ai.agents.get(agent_id).conversation_config.agent.prompt.tool_ids or []
    return {client.conversational_ai.tools.get(tool_id).tool_config.name for tool_id in ids}


# --- running a conversation ------------------------------------------------------------------------------------------


@dataclass
class TurnResult:
    calls: list[dict] = field(default_factory=list)
    replies: list[str] = field(default_factory=list)
    said_first: list[str] = field(default_factory=list)  # what the agent said before its first tool call
    ended: bool = False  # the agent hung up (end_call), so the conversation can't go on

    @property
    def announced(self) -> bool:
        return acted(self.calls) and bool(self.said_first)

    @property
    def reply(self) -> str:
        return " ".join(self.replies)


@dataclass
class Outcome:
    scenario: Scenario
    status: str  # pass, fail, skip, error
    problems: list[str] = field(default_factory=list)
    turns: list[TurnResult] = field(default_factory=list)
    credits: int = 0
    seconds: float = 0.0


def run_turn(ws, text: str, results: dict[str, str], verbose: bool = False) -> TurnResult:
    """Send one user message and collect the agent's tool calls and replies until it goes quiet."""
    turn = TurnResult()
    ws.send(json.dumps({"type": "user_message", "text": text}))
    started = last_event = time.monotonic()
    replied = False  # the agent has spoken since the last tool result (or since the message, if no tool ran)
    waiting_tool = False
    while time.monotonic() - started < TURN_TIMEOUT:
        try:
            message = json.loads(ws.recv(timeout=0.5))
        except ConnectionClosed:  # end_call is a built-in tool: it shows up as the server closing the session
            turn.calls.append({"tool": "end_call", "args": {}})
            turn.ended = True
            break
        except TimeoutError:
            quiet = time.monotonic() - last_event
            if replied and quiet > SETTLE_SECONDS or waiting_tool and quiet > TOOL_WAIT_SECONDS:
                break
            continue
        kind = message.get("type")
        if kind == "ping":  # keep-alives arrive every second and must not count as the agent doing something
            ws.send(json.dumps({"type": "pong", "event_id": message["ping_event"]["event_id"]}))
            continue
        last_event = time.monotonic()
        if verbose and kind != "agent_chat_response_part":
            print(f"    [{kind}] {json.dumps(message, ensure_ascii=False)[:300]}")
        if kind == "agent_response":
            turn.replies.append(message["agent_response_event"]["agent_response"].strip())
            if not turn.calls:
                turn.said_first.append(turn.replies[-1])
            replied, waiting_tool = True, False
        elif kind == "client_tool_call":
            call = message["client_tool_call"]
            name, params = call["tool_name"], call.get("parameters", {})
            turn.calls.append({"tool": name, "args": params})
            result = results.get(name, DEFAULT_RESULTS.get(name, GENERIC_RESULT))
            ws.send(
                json.dumps(
                    {
                        "type": "client_tool_result",
                        "tool_call_id": call["tool_call_id"],
                        "result": result,
                        "is_error": False,
                    }
                )
            )
            replied, waiting_tool = False, True
    return turn


def run_scenario(client, agent_id: str, scenario: Scenario, verbose: bool = False) -> Outcome:
    from elevenlabs.client import ElevenLabs
    from websockets.sync.client import connect

    assert isinstance(client, ElevenLabs)
    started = time.monotonic()
    outcome = Outcome(scenario, "pass")
    conversation_id = None
    url = client.conversational_ai.conversations.get_signed_url(agent_id=agent_id).signed_url
    with connect(url, max_size=16 * 1024 * 1024) as ws:
        ws.send(
            json.dumps(
                {
                    "type": "conversation_initiation_client_data",
                    "conversation_config_override": {"conversation": {"text_only": True}},
                    "dynamic_variables": {"now": NOW.strftime("%A %Y-%m-%d %H:%M"), "greeting": ""},
                }
            )
        )
        first = json.loads(ws.recv(timeout=20))
        if first.get("type") == "conversation_initiation_metadata":
            conversation_id = first["conversation_initiation_metadata_event"]["conversation_id"]
        for i, step in enumerate(scenario.steps, 1):
            turn = run_turn(ws, step.say, scenario.results, verbose)
            outcome.turns.append(turn)
            problems = check_step(step, turn.calls, turn.reply, " ".join(turn.said_first))
            prefix = f"step {i}: " if len(scenario.steps) > 1 else ""
            if turn.ended and i < len(scenario.steps):
                problems.append("the agent ended the conversation before the next step")
            outcome.problems += [prefix + p for p in problems]
            if problems:
                break  # later steps depend on this one having gone right
    if outcome.problems:
        outcome.status = "fail"
    outcome.seconds = time.monotonic() - started
    outcome.credits = conversation_credits(client, conversation_id)
    return outcome


def conversation_credits(client, conversation_id: str | None) -> int:
    """Credits ElevenLabs charged for the conversation; the figure shows up a moment after it ends."""
    if not conversation_id:
        return 0
    for _ in range(5):
        metadata = client.conversational_ai.conversations.get(conversation_id).metadata
        if metadata.cost is not None:
            return metadata.cost
        time.sleep(1.5)
    return 0


# --- reporting -------------------------------------------------------------------------------------------------------


def format_calls(turn: TurnResult) -> str:
    return "; ".join(describe_call(c) for c in turn.calls) or "(no tool)"


def render(outcomes: list[Outcome]) -> str:
    width = max(len(o.scenario.name) for o in outcomes)
    lines = [f"{'scenario'.ljust(width)}  result  credits  time  first tool call"]
    for o in outcomes:
        first = format_calls(o.turns[0]) if o.turns else ""
        if any(t.announced for t in o.turns):
            first += "  [spoke first]"
        if o.status == "skip":
            first = o.problems[0]
        lines.append(
            f"{o.scenario.name.ljust(width)}  {o.status.upper().ljust(6)}  {str(o.credits).rjust(7)}  "
            f"{o.seconds:4.0f}s  {first[:70]}"
        )
    for o in outcomes:
        if o.status not in ("fail", "error"):
            continue
        lines += ["", f"{o.status.upper()}: {o.scenario.name}"]
        lines += [f"  - {p}" for p in o.problems]
        for i, turn in enumerate(o.turns, 1):
            lines += [f"  user {i}:  {o.scenario.steps[i - 1].say}", f"  tools {i}: {format_calls(turn)}"]
            lines.append(f"  agent {i}: {turn.reply or '(silence)'}")
    count = {s: sum(o.status == s for o in outcomes) for s in ("pass", "fail", "error", "skip")}
    credits = sum(o.credits for o in outcomes)
    talk_only = [o for o in outcomes if o.status == "fail" and all(SPOKE in p for p in o.problems)]
    totals = ", ".join(f"{count[status]} {label}" for status, label in TOTALS)
    lines += ["", f"{totals} of {len(outcomes)}; {credits} credits"]
    lines.append(f"{len(talk_only)} of the failures are only about speaking before acting")
    acting = [t for o in outcomes for t in o.turns if acted(t.calls)]
    if acting:
        talked = sum(t.announced for t in acting)
        lines.append(f"Spoke before the first tool call in {talked} of {len(acting)} turns that used a tool")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--only", help="run only scenarios whose name contains this text (comma-separated for several)")
    parser.add_argument("--list", action="store_true", help="list scenarios and exit (no API calls)")
    parser.add_argument(
        "--repeat", type=int, default=1, help="run each scenario N times (the agent is not deterministic)"
    )
    parser.add_argument("--no-sync", action="store_true", help="reuse the scenario agent as it is")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--battery", type=Path, default=BATTERY)
    parser.add_argument("--verbose", action="store_true", help="print every websocket event")
    args = parser.parse_args()

    scenarios = parse_scenarios(args.battery.read_text())
    if args.only:
        wanted = args.only.split(",")
        scenarios = [s for s in scenarios if any(part in s.name for part in wanted)]
    if not scenarios:
        sys.exit("No scenarios match.")
    if args.list:
        for s in scenarios:
            print(f"{s.name}: {' / '.join(step.say for step in s.steps)}")
        return 0

    sys.path.insert(0, str(ROOT))
    from elevenlabs.client import ElevenLabs

    from jarvis.agent import api_key

    client = ElevenLabs(api_key=api_key())
    specs = load_specs()
    agent_id = find_scenario_agent(client) if args.no_sync else sync_scenario_agent(client, specs)
    available = {spec["name"] for spec in specs} if not args.no_sync else available_tools(client, agent_id)
    available.add("end_call")  # built into the agent, not one of the client tools
    print(f'Scenario agent "{SCENARIO_AGENT}" ({agent_id}), {len(available)} tools, {len(scenarios)} scenarios')

    lock = threading.Lock()

    def run(scenario: Scenario) -> Outcome:
        if missing := missing_tools(scenario, available):
            return Outcome(scenario, "skip", [f"skipped: tool missing ({', '.join(missing)})"])
        try:
            outcome = run_scenario(client, agent_id, scenario, args.verbose)
        except Exception as error:  # noqa: BLE001
            return Outcome(scenario, "error", [f"{type(error).__name__}: {error}"[:300]])
        with lock:
            print(f"  {outcome.status.upper().ljust(5)} {scenario.name}", flush=True)
        return outcome

    jobs = scenarios
    if args.repeat > 1:
        jobs = [replace(s, name=f"{s.name}#{n}") for s in scenarios for n in range(1, args.repeat + 1)]
    with concurrent.futures.ThreadPoolExecutor(args.concurrency) as pool:
        outcomes = list(pool.map(run, jobs))
    print()
    print(render(outcomes))
    return 1 if any(o.status in ("fail", "error") for o in outcomes) else 0


if __name__ == "__main__":
    sys.exit(main())
