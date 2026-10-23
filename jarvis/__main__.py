import threading
import time
from pathlib import Path

import openwakeword
import rumps
import sounddevice as sd
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import Conversation
from openwakeword.model import Model

from .agent import api_key, ensure_agent
from .audio import SoundDeviceAudio, input_mics, refresh_devices, wait_for_wake_word

IDLE_SECONDS = 12  # silence after Jarvis stops talking before the session closes (sessions bill per minute)
RETRY_SECONDS = 10
ASSETS = Path(__file__).parent.parent / "assets"
MIC_FILE = Path.home() / ".config/jarvis/mic"
# HAL's eye doubles as a status light: grey when not listening, dim red while waiting, bright red in a conversation.
ICONS = {state: str(ASSETS / f"menubar-{state}.png") for state in ("idle", "listening", "talking")}


def converse(client: ElevenLabs, agent_id: str, mic: str | None):
    audio = SoundDeviceAudio(mic)
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
        self.interrupt = threading.Event()
        self.talk_requested = False
        self.mic = MIC_FILE.read_text().strip() or None if MIC_FILE.exists() else None
        # Filled by the voice thread after each device refresh; the main thread only reads them.
        self.mics, self.default_mic, self.shown_mics = [], "", None
        self.mic_menu = rumps.MenuItem("Microphone")
        self.client = ElevenLabs(api_key=api_key())  # exits here, on the main thread, when the key is missing
        self.menu = [self.status_item, rumps.MenuItem("Talk now", callback=self.talk_now), self.mic_menu]
        threading.Thread(target=self.voice_loop, daemon=True).start()

    # AppKit must only be touched from the main thread, so the voice thread just sets self.status.
    @rumps.timer(0.5)
    def refresh(self, _):
        icon, self.status_item.title = self.status
        if icon != self.shown_icon:
            self.icon, self.shown_icon = ICONS[icon], icon
        if self.mics != self.shown_mics:
            for title in list(self.mic_menu.keys()):  # rumps clear() fails on a submenu that was never filled
                del self.mic_menu[title]
            for name in [None, *self.mics]:
                self.mic_menu.add(rumps.MenuItem(name or "System default", callback=self.pick_mic))
            self.shown_mics = self.mics
        self.show_mic()

    def talk_now(self, _):
        self.talk_requested = True
        self.interrupt.set()

    def pick_mic(self, item):
        self.mic = None if item.title == "System default" else item.title
        MIC_FILE.parent.mkdir(parents=True, exist_ok=True)
        MIC_FILE.write_text(self.mic or "")
        self.show_mic()
        self.interrupt.set()  # reopen the wake word stream on the new mic; a conversation in progress keeps its mic

    def show_mic(self):
        in_use = self.mic if self.mic in self.mics else self.default_mic  # a saved mic that's unplugged falls back
        self.mic_menu.title = f"Microphone: {in_use}"
        for item in self.mic_menu.values():
            item.state = item.title == (self.mic or "System default")

    def voice_loop(self):
        model = Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
        agent_id = None
        while True:
            try:
                agent_id = agent_id or ensure_agent(self.client)
                refresh_devices()
                self.mics, self.default_mic = input_mics(), sd.query_devices(kind="input")["name"]
                self.status = ("listening", "Listening for “Hey Jarvis”")
                heard = wait_for_wake_word(model, self.mic, self.interrupt)
                self.interrupt.clear()
                if not heard and not self.talk_requested:
                    continue
                self.talk_requested = False
                self.status = ("talking", "In conversation")
                converse(self.client, agent_id, self.mic)
            except Exception as error:  # noqa: BLE001
                # A dropped network or mic shouldn't kill the thread while the icon still says "running".
                self.status = ("idle", f"Error, retrying: {error}"[:80])
                time.sleep(RETRY_SECONDS)


JarvisApp().run()
