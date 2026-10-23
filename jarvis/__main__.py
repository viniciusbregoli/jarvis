import threading
import time
from pathlib import Path

import openwakeword
import rumps
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation
from openwakeword.model import Model

from .agent import api_key, ensure_agent
from .audio import SoundDeviceAudio, wait_for_wake_word

IDLE_SECONDS = 12  # silence after Jarvis stops talking before the session closes (sessions bill per minute)
RETRY_SECONDS = 10
ASSETS = Path(__file__).parent.parent / "assets"
# HAL's eye doubles as a status light: grey when not listening, dim red while waiting, bright red in a conversation.
ICONS = {state: str(ASSETS / f"menubar-{state}.png") for state in ("idle", "listening", "talking")}


def converse(client: ElevenLabs, agent_id: str):
    audio = SoundDeviceAudio()
    ended = threading.Event()
    last_user = time.monotonic()

    def on_user(text):
        nonlocal last_user
        last_user = time.monotonic()
        print(f"you: {text}")

    conversation = Conversation(
        client,
        agent_id,
        requires_auth=True,
        audio_interface=audio,
        callback_user_transcript=on_user,
        callback_agent_response=lambda text: print(f"jarvis: {text}"),
        callback_end_session=ended.set,
    )
    conversation.start_session()
    while not ended.wait(1):
        if time.monotonic() - max(last_user, audio.last_played) > IDLE_SECONDS:
            conversation.end_session()
    conversation.wait_for_session_end()


class JarvisApp(rumps.App):
    def __init__(self):
        super().__init__("Jarvis", icon=ICONS["idle"], quit_button="Quit Jarvis")
        self.status = ("idle", "Starting...")
        self.shown_icon = "idle"
        self.status_item = rumps.MenuItem(self.status[1])
        self.talk_now = threading.Event()
        self.client = ElevenLabs(api_key=api_key())  # exits here, on the main thread, when the key is missing
        self.menu = [self.status_item, rumps.MenuItem("Talk now", callback=lambda _: self.talk_now.set())]
        threading.Thread(target=self.voice_loop, daemon=True).start()

    # AppKit must only be touched from the main thread, so the voice thread just sets self.status.
    @rumps.timer(0.5)
    def refresh(self, _):
        icon, self.status_item.title = self.status
        if icon != self.shown_icon:
            self.icon, self.shown_icon = ICONS[icon], icon

    def voice_loop(self):
        model = Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
        agent_id = None
        while True:
            try:
                agent_id = agent_id or ensure_agent(self.client)
                self.status = ("listening", "Listening for “Hey Jarvis”")
                wait_for_wake_word(model, self.talk_now)
                self.status = ("talking", "In conversation")
                converse(self.client, agent_id)
            except Exception as error:  # noqa: BLE001
                # A dropped network or mic shouldn't kill the thread while the icon still says "running".
                self.status = ("idle", f"Error, retrying: {error}"[:80])
                time.sleep(RETRY_SECONDS)


JarvisApp().run()
