import os
import sys
from pathlib import Path

from elevenlabs.client import ElevenLabs

NAME = "Jarvis"
VOICE_ID = "xru6qZB94sJdkyqP12qN"  # James - English Butler

PROMPT = """You are Jarvis, a voice assistant running on Vinicius's Mac, in the spirit of Tony Stark's Jarvis: calm, dry, quick.

- Everything you say is spoken aloud. Keep replies to one or two short sentences, no lists, no markdown, no URLs.
- Reply in the language Vinicius just spoke (Brazilian Portuguese or English). Switch when they switch.
- You can't act on the computer yet. If asked to run, open, check or change something, say that ability isn't connected yet.
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


def ensure_agent(client: ElevenLabs) -> str:
    found = [a for a in client.conversational_ai.agents.list(search=NAME).agents if a.name == NAME]
    if found:
        client.conversational_ai.agents.update(agent_id=found[0].agent_id, conversation_config=CONFIG)
        return found[0].agent_id
    return client.conversational_ai.agents.create(name=NAME, conversation_config=CONFIG).agent_id
