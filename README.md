<img src="assets/app-icon.png" width="96" height="96" alt="Jarvis icon">

# jarvis

Created in [T3 Code](https://t3.codes).

Voice assistant for the Mac: say "Hey Jarvis" and talk to it through ElevenLabs' Eleven Realtime.

## Run

```sh
uv sync
uv run python -m jarvis
```

Jarvis lives in the menu bar as a HAL 9000 eye that doubles as a status light: grey when it isn't listening (starting or retrying after an error), dim red while waiting for "Hey Jarvis", bright red during a conversation. Its menu shows what it's doing ("Listening for “Hey Jarvis”", "In conversation", or an error it's retrying), plus **Talk now** to start a conversation without the wake word, and **Quit Jarvis**.

Needs `ELEVEN_LABS_API_KEY` in the environment or in `~/.claude/.env`. Each start pushes the agent config in `jarvis/agent.py` to ElevenLabs, creating the "Jarvis" agent on first run. A session ends when you say goodbye or after 12 s of silence.

## Test

```sh
uv run pytest
```
