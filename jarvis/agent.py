import os
import sys
from pathlib import Path

from elevenlabs import ToolRequestModel
from elevenlabs.client import ElevenLabs

NAME = "Jarvis"
VOICE_ID = "xru6qZB94sJdkyqP12qN"  # James - English Butler

PROMPT = """You are Jarvis, a voice assistant running on Vinicius's Mac, in the spirit of Tony Stark's Jarvis: calm, dry, quick.

- Everything you say is spoken aloud. Keep replies to one or two short sentences, no lists, no markdown, no URLs.
- Reply in the language Vinicius just spoke (Brazilian Portuguese or English). Switch when they switch.
- The current local time is {{now}}. Use it to turn "in 10 minutes" or "tomorrow at 5" into exact values for tools.
- Use your tools when asked: open apps or URLs, control volume and music, set timers, add reminders, read the clipboard, look at the screen. Do it right away, then confirm in a few words, in the language Vinicius used.
- describe_screen takes a while: say "one moment" before calling it.
- Call the tool before saying anything about the action. Never say something was done, or describe the current volume or any other state, unless a tool result in this conversation says so. To check the volume, call media with action "status".
- You can't yet run commands, check code, browse the web or click around apps. If asked, say that ability isn't connected yet.
- When Vinicius says goodbye, thanks you to close, or says "that's all" / "é isso", answer with a short sign-off and call end_call."""

# The agent's config lives here and is pushed on every start, so the dashboard never drifts from the code.
CONFIG = {
    "realtime_model": "eleven_realtime_v1_mini",
    "agent": {
        "first_message": "Yes, sir?",
        "language": "en",
        "prompt": {
            "prompt": PROMPT,
            "built_in_tools": {
                "end_call": {
                    "type": "system",
                    "name": "end_call",
                    "description": "",
                    "params": {"system_tool_type": "end_call"},
                }
            },
        },
    },
    "tts": {"voice_id": VOICE_ID},
    # Max allowed; our own idle timer closes the session well before the agent would nag.
    "turn": {"turn_timeout": 30},
}


def api_key() -> str:
    if key := os.environ.get("ELEVEN_LABS_API_KEY"):
        return key
    env = Path.home() / ".claude/.env"
    if env.exists():
        for line in env.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "ELEVEN_LABS_API_KEY":
                return value.strip().strip("\"'")
    sys.exit("ELEVEN_LABS_API_KEY is not set (env or ~/.claude/.env)")


def sync_tools(client: ElevenLabs, specs: list[dict]) -> list[str]:
    """Create or update each client tool in ElevenLabs and return their ids, matching on name."""
    tools = client.conversational_ai.tools
    existing = {
        t.tool_config.name: t.id
        for t in tools.list(types="client", show_only_owned_documents=True, page_size=100).tools
    }
    ids = []
    for spec in specs:
        request = ToolRequestModel(
            tool_config={
                "type": "client",
                "name": spec["name"],
                "description": spec["description"],
                "parameters": spec["parameters"],
                "expects_response": True,
                "response_timeout_secs": spec.get("timeout", 20),
            }
        )
        tool_id = existing.get(spec["name"])
        ids.append(tools.update(tool_id, request=request).id if tool_id else tools.create(request=request).id)
    return ids


def ensure_agent(client: ElevenLabs, specs: list[dict]) -> str:
    prompt = {**CONFIG["agent"]["prompt"], "tool_ids": sync_tools(client, specs)}
    config = {**CONFIG, "agent": {**CONFIG["agent"], "prompt": prompt}}
    found = [a for a in client.conversational_ai.agents.list(search=NAME).agents if a.name == NAME]
    if found:
        client.conversational_ai.agents.update(agent_id=found[0].agent_id, conversation_config=config)
        return found[0].agent_id
    return client.conversational_ai.agents.create(name=NAME, conversation_config=config).agent_id
