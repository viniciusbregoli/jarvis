import queue
import threading
import time

import numpy as np
import sounddevice as sd
from elevenlabs.conversational_ai.conversation import AudioInterface
from openwakeword.model import Model

RATE = 16000  # ElevenAgents and openWakeWord both expect 16 kHz mono int16
WAKE_FRAME = 1280  # 80 ms, openWakeWord's native chunk
WAKE_THRESHOLD = 0.5


class SoundDeviceAudio(AudioInterface):
    """Same job as the SDK's DefaultAudioInterface, which needs PyAudio (it doesn't build on this Mac)."""

    def __init__(self):
        # Set here, not in start(): the SDK calls start() from its own thread after connecting, and the idle timer
        # may read this first.
        self.last_played = time.monotonic()

    def start(self, input_callback):
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.mic = sd.RawInputStream(
            samplerate=RATE,
            channels=1,
            dtype="int16",
            blocksize=4000,
            callback=lambda data, *_: input_callback(bytes(data)),
        )
        self.speaker = sd.RawOutputStream(samplerate=RATE, channels=1, dtype="int16")
        self.mic.start()
        self.speaker.start()
        self.player = threading.Thread(target=self._play, daemon=True)
        self.player.start()

    def _play(self):
        while not self.stopping.is_set():
            try:
                self.speaker.write(self.pending.get(timeout=0.25))
                self.last_played = time.monotonic()
            except queue.Empty:
                pass

    def stop(self):
        if self.stopping.is_set():
            return
        self.mic.stop()
        self.mic.close()
        # Let the sign-off finish before cutting the speaker.
        deadline = time.monotonic() + 5
        while not self.pending.empty() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.stopping.set()
        self.player.join()
        self.speaker.close()

    def output(self, audio: bytes):
        self.pending.put(audio)

    def interrupt(self):
        while True:
            try:
                self.pending.get_nowait()
            except queue.Empty:
                return


def heard_wake_word(model: Model, frame: np.ndarray) -> bool:
    return max(model.predict(frame).values()) > WAKE_THRESHOLD


def wait_for_wake_word(model: Model, talk_now: threading.Event):
    """Returns when "Hey Jarvis" is heard or `talk_now` is set (the menu bar's Talk now)."""
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=WAKE_FRAME) as mic:
        while not talk_now.is_set():
            frame, _ = mic.read(WAKE_FRAME)
            if heard_wake_word(model, frame[:, 0]):
                model.reset()
                return
    talk_now.clear()
