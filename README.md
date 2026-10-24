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

Ask in Portuguese or English; Jarvis always answers in Brazilian Portuguese, in the manner of the movie J.A.R.V.I.S. (polite, terse, dry wit, "senhor"), with a custom voice and a greeting that follows the time of day:

- **Open apps and URLs**: "open Slack", "abre o GitHub"
- **Volume and music**: "volume 30", "mute", "pause the music", "next song" (media keys, so any player)
- **Timers**: "timer 10 minutes for the pasta"; Jarvis speaks and notifies when it ends (lost if Jarvis quits)
- **Reminders**: "remind me tomorrow at 5 to call Ana" goes to Apple Reminders, so it reaches your phone too
- **Clipboard**: "read what I copied"
- **Screen**: "what's on my screen?" takes a screenshot and has Claude describe it with every tool turned off (about 5 s, uses your Claude login)

### Background jobs

- **Quick answers**: "qual a cotação do dólar?" runs a short Claude Code lookup (Haiku, web search) and Jarvis answers in the same conversation, in about 15 to 30 s
- **Jobs on the computer**: "procura minha apresentação do roadmap", "o CI do último PR passou?" start a Claude Code job in the background (Haiku, read-only); Jarvis tells you the result when it finishes, in the conversation or out loud if it already ended (no notification)
- **Jobs on the screen**: "abre a Calculadora e calcula 12 vezes 12" asks "posso usar a tela?" and then hands it to Codex computer use (your ChatGPT login)
- "e aí, terminou?", "cancela a tarefa", "e quando ela foi modificada?" check, stop or continue a job

- **Edits**: "corrige o slide 4 da apresentação que eu tô apresentando" checks what's in front (Keynote, Preview, TextEdit, or the selected tab in Zen, Firefox, Chrome, Safari) and starts a Claude Code job that may write only inside that document's folder; the folder is snapshotted first, the document reloads when it's done, and "desfaz" restores the snapshot (`~/.jarvis/snapshots`)

Other jobs only look and search (files, git, GitHub, the web) and can't change, send or delete anything. Claude Code jobs run in `dontAsk` mode with an allowlist and without your Claude settings files, so only that list applies and anything outside it is refused.

### The HAL bar

While you talk to Jarvis, a black bar with a red glow appears at the bottom of the screen: the eye pulses with Jarvis's voice, your voice shows in warm white bars and Jarvis's in red, a line says what it's doing ("Abrindo Slack", "Pesquisando..."), and background jobs show as chips with their elapsed time and a check or an X when they finish. After the conversation, if jobs are still running, it shrinks to just the chips. It never takes focus and clicks pass through it. Press Esc to end the conversation.

Conversations go through Apple's voice processing (the echo cancellation FaceTime uses), so Jarvis doesn't hear itself on the laptop speakers and you can talk over it to interrupt. While it's on, macOS lowers other audio, like during a FaceTime call.

First use of each asks for a macOS permission for the app running Jarvis: Accessibility (media keys), Automation for Reminders, Screen Recording (screen).

Needs `ELEVEN_LABS_API_KEY` in the environment or in `~/.claude/.env`. Each start pushes the agent config in `jarvis/agent.py` to ElevenLabs, creating the "Jarvis" agent on first run. A session ends when you say goodbye or after 12 s of silence.

## Test

```sh
uv run pytest
```

## Scenario battery

Portuguese phrases, each with the tool call the voice agent should make, run against the agent in text:

```sh
uv run python scripts/scenarios.py [--only name-substring] [--list] [--repeat N] [--no-sync] [--verbose]
```

It builds a separate agent, "Jarvis scenarios", from the same prompt, model and tool specs as Jarvis (the tools are imported from the code, so a new tool is covered as soon as it exists) with its own copies of the tool objects, so it never touches the live agent or its tools. Conversations are text-only (no TTS or audio, Eleven Realtime Max on the same prompt) and every tool is mocked: the call is recorded and answered with a canned result, never run. A text scenario costs roughly 20 to 80 credits, against 200 or more for a voice conversation of the same length; the run prints the total. It needs the ElevenLabs key, so it is not part of `pytest`.

Scenarios live in `scenarios/battery.toml` (the header explains the format): user turns, canned tool results, the expected tool calls with key arguments, forbidden calls, and regexes for the spoken reply. Speaking before the first tool call ("Vou procurar...") fails a step unless it sets `announce = true`, and the summary counts how often it happens. Scenarios that expect a tool the code doesn't have are skipped. The agent is not deterministic (temperature 0.6), so use `--repeat 3` to see how stable a failure is.
