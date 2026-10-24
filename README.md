<img src="assets/app-icon.png" width="96" height="96" alt="Jarvis icon">

# jarvis

Created in [T3 Code](https://t3.codes).

Voice assistant for the Mac: say "Hey Jarvis" and talk to it through ElevenLabs' Eleven Realtime.

## Run

```sh
uv sync
uv run python -m jarvis
```

Jarvis lives in the menu bar as a HAL 9000 eye that doubles as a status light: grey when it isn't listening (starting or retrying after an error), dim red while waiting for "Hey Jarvis", bright red during a conversation. Its menu shows what it's doing ("Listening for “Hey Jarvis”", "In conversation", or an error it's retrying), **Talk now** to start a conversation without the wake word, **Microphone** to switch the Mac's input device (other apps follow, as when you change it in System Settings; Jarvis always listens on the system input), and **Quit Jarvis**.

## What it can do

Ask in Portuguese or English; Jarvis always answers in Brazilian Portuguese, starting with "Pois não, senhor?":

- **Open apps and URLs**: "open Slack", "abre o GitHub"
- **Volume and music**: "volume 30", "mute", "pause the music", "next song" (media keys, so any player)
- **Timers**: "timer 10 minutes for the pasta"; Jarvis speaks and notifies when it ends (lost if Jarvis quits)
- **Reminders**: "remind me tomorrow at 5 to call Ana" goes to Apple Reminders, so it reaches your phone too
- **Clipboard**: "read what I copied"
- **Screen**: "what's on my screen?" takes a screenshot and has Claude describe it with every tool turned off (about 5 s, uses your Claude login)

### Background jobs

- **Quick answers**: "qual a cotação do dólar?" runs a short Claude Code lookup (Haiku, web search) and Jarvis answers in the same conversation, in about 15 to 30 s
- **Jobs on the computer**: "procura minha apresentação do roadmap", "o CI do último PR passou?" start a Claude Code job in the background (Haiku, read-only); Jarvis tells you the result when it finishes, out loud if no conversation is open, plus a notification
- **Jobs on the screen**: "abre a Calculadora e calcula 12 vezes 12" asks "posso usar a tela?" and then hands it to Codex computer use (your ChatGPT login)
- "e aí, terminou?", "cancela a tarefa", "e quando ela foi modificada?" check, stop or continue a job

Jobs are read-only for now: they can look and search (files, git, GitHub, the web) but not change, send or delete anything. Claude Code jobs run in `dontAsk` mode with an allowlist, so anything outside it is refused.

Conversations go through Apple's voice processing (the echo cancellation FaceTime uses), so Jarvis doesn't hear itself on the laptop speakers and you can talk over it to interrupt. While it's on, macOS lowers other audio, like during a FaceTime call.

First use of each asks for a macOS permission for the app running Jarvis: Accessibility (media keys), Automation for Reminders, Screen Recording (screen).

Needs `ELEVEN_LABS_API_KEY` in the environment or in `~/.claude/.env`. Each start pushes the agent config in `jarvis/agent.py` to ElevenLabs, creating the "Jarvis" agent on first run. A session ends when you say goodbye or after 12 s of silence.

## Test

```sh
uv run pytest
```
