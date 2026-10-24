import datetime as dt
import threading
import time
from pathlib import Path

import openwakeword
import rumps
from elevenlabs.client import ElevenLabs
from elevenlabs.conversational_ai.conversation import ClientTools, Conversation, ConversationInitiationData
from openwakeword.model import Model

from . import coreaudio
from .agent import api_key, ensure_agent, greeting
from .audio import VoiceProcessingAudio, refresh_devices, wait_for_wake_word
from .context import SPECS as CONTEXT_SPECS
from .jobs import SPECS as JOB_SPECS
from .jobs import jobs
from .overlay import HALBar, activity_label
from .tools import SPECS as MAC_SPECS
from .tools import speak

IDLE_SECONDS = 12  # silence after Jarvis stops talking before the session closes (sessions bill per minute)
RETRY_SECONDS = 10
ASSETS = Path(__file__).parent.parent / "assets"
SPECS = MAC_SPECS + CONTEXT_SPECS + JOB_SPECS
# HAL's eye doubles as a status light: grey when not listening, dim red while waiting, bright red in a conversation.
ICONS = {state: str(ASSETS / f"menubar-{state}.png") for state in ("idle", "listening", "talking")}


class RealtimeConversation(Conversation):
    """Eleven Realtime sends the reply after an interruption with the same event id as the interruption, and the SDK
    drops audio with event_id <= the interruption's, so every reply after you (or a job result) cut Jarvis off went
    silent. Keep dropping only the older audio of the interrupted answer."""

    def _handle_message(self, message, ws):
        super()._handle_message(message, ws)
        if message.get("type") == "interruption":
            self._last_interrupt_id -= 1


def converse(client: ElevenLabs, agent_id: str, on_session, on_activity=lambda text: None):
    audio = VoiceProcessingAudio()
    ended = threading.Event()
    last_user = time.monotonic()
    busy = 0  # tool calls in flight; a lookup can take 30 s and the session must not close under it

    def on_user(text):
        nonlocal last_user
        last_user = time.monotonic()
        print(f"you: {text}")

    def tracked(name, handler):
        def run(params):
            nonlocal busy, last_user
            busy += 1
            on_activity(activity_label(name, params))
            try:
                return handler(params)
            finally:
                busy -= 1
                last_user = time.monotonic()
                on_activity("")

        return run

    now = dt.datetime.now()
    tools = ClientTools()
    for spec in SPECS:
        tools.register(spec["name"], tracked(spec["name"], spec["handler"]))
    conversation = RealtimeConversation(
        client,
        agent_id,
        requires_auth=True,
        audio_interface=audio,
        client_tools=tools,
        config=ConversationInitiationData(
            dynamic_variables={"now": now.strftime("%A %Y-%m-%d %H:%M"), "greeting": greeting(now)}
        ),
        callback_user_transcript=on_user,
        callback_agent_response=lambda text: print(f"jarvis: {text}"),
        callback_end_session=ended.set,
    )

    def quiet() -> bool:
        """Jarvis isn't talking and Vinicius hasn't spoken for a second: safe to slip a job result in."""
        return not audio.pending and time.monotonic() - max(last_user, audio.last_played) > 1

    conversation.start_session()
    on_session((conversation, quiet, audio))
    try:
        while not ended.wait(1):
            if not busy and time.monotonic() - max(last_user, audio.last_played) > IDLE_SECONDS:
                conversation.end_session()
        conversation.wait_for_session_end()
    finally:
        on_session(None)


class JarvisApp(rumps.App):
    def __init__(self):
        super().__init__("Jarvis", icon=ICONS["idle"], quit_button="Quit Jarvis")
        self.status = ("idle", "Starting...")
        self.shown_icon = "idle"
        self.status_item = rumps.MenuItem(self.status[1])
        self.interrupt = threading.Event()
        self.talk_requested = False
        self.shown_mics: dict[str, int] = {}  # menu label -> CoreAudio device id
        self.mic_menu = rumps.MenuItem("Microphone")
        self.session = None  # (conversation, quiet, audio) while one is live, so finished jobs can report into it
        self.bar = HALBar(jobs, ASSETS / "menubar-talking.png")
        self.bar.on_escape = self.end_conversation
        jobs.on_done = self.job_done
        self.client = ElevenLabs(api_key=api_key())  # exits here, on the main thread, when the key is missing
        self.menu = [self.status_item, rumps.MenuItem("Talk now", callback=self.talk_now), self.mic_menu]
        threading.Thread(target=self.voice_loop, daemon=True).start()

    # AppKit must only be touched from the main thread, so the voice thread just sets self.status.
    @rumps.timer(0.5)
    def refresh(self, _):
        icon, self.status_item.title = self.status
        if icon != self.shown_icon:
            self.icon, self.shown_icon = ICONS[icon], icon
        # Read from macOS every tick, so a change in System Settings shows up even during a conversation.
        mics = coreaudio.inputs()
        if mics != self.shown_mics:
            for title in list(self.mic_menu.keys()):  # rumps clear() fails on a submenu that was never filled
                del self.mic_menu[title]
            for label in mics:
                self.mic_menu.add(rumps.MenuItem(label, callback=self.pick_mic))
            self.shown_mics = mics
        self.show_mic()

    @rumps.timer(1 / 30)
    def animate(self, _):
        self.bar.tick()

    def set_session(self, session):
        self.session = session
        if session:
            audio = session[2]
            self.bar.levels = lambda: (audio.mic_level, audio.voice_level)
        self.bar.talking = bool(session)
        self.bar.activity = ""

    def set_activity(self, text):
        self.bar.activity = text

    def end_conversation(self):
        if session := self.session:
            # Off the main thread: ending waits for the sign-off to finish playing.
            threading.Thread(target=session[0].end_session, daemon=True).start()

    def job_done(self, job):
        # Injected while Jarvis is mid-sentence, the update interrupts it and the answer's audio gets lost, so wait
        # for a pause (up to 30 s).
        deadline = time.monotonic() + 30
        while (session := self.session) and not session[1]() and time.monotonic() < deadline:
            time.sleep(0.2)
        if session := self.session:
            # A contextual update would only be read on the next turn; a message makes the agent say it now.
            session[0].send_user_message(
                f"[Automatic update, not Vinicius speaking] Background task {job.id} ({job.task}) {job.status}. "
                f"Result: {job.result}"
            )
        else:
            outcome = "terminou" if job.status == "done" else "falhou"
            speak(f"Senhor, a tarefa {job.id} {outcome}. {job.result}")

    def talk_now(self, _):
        self.talk_requested = True
        self.interrupt.set()

    def pick_mic(self, item):
        # Switches macOS's input (other apps follow too); Jarvis always listens on the system input.
        coreaudio.set_default_input(self.shown_mics[item.title])
        self.show_mic()  # the next ticks read back what macOS actually applied (it refuses some virtual devices)
        self.interrupt.set()  # reopen the wake word stream on the new mic; a conversation in progress keeps its mic

    def show_mic(self):
        current = coreaudio.default_input()
        label = next((label for label, device in self.shown_mics.items() if device == current), None)
        self.mic_menu.title = f"Microphone: {label or coreaudio.name(current)}"
        for item in self.mic_menu.values():
            item.state = self.shown_mics.get(item.title) == current

    def voice_loop(self):
        model = Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
        agent_id = None
        while True:
            try:
                agent_id = agent_id or ensure_agent(self.client, SPECS)
                refresh_devices()
                self.status = ("listening", "Listening for “Hey Jarvis”")
                heard = wait_for_wake_word(model, self.interrupt)
                self.interrupt.clear()
                if not heard and not self.talk_requested:
                    continue
                self.talk_requested = False
                self.status = ("talking", "In conversation")
                converse(self.client, agent_id, self.set_session, self.set_activity)
            except Exception as error:  # noqa: BLE001
                # A dropped network or mic shouldn't kill the thread while the icon still says "running".
                self.status = ("idle", f"Error, retrying: {error}"[:80])
                time.sleep(RETRY_SECONDS)


if __name__ == "__main__":
    JarvisApp().run()
