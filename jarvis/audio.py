import threading
import time

import AVFoundation as AV
import numpy as np
import sounddevice as sd
from elevenlabs.conversational_ai.conversation import AudioInterface
from openwakeword.model import Model

RATE = 16000  # mic audio for ElevenAgents and openWakeWord: 16 kHz mono int16
OUTPUT_RATE = 44100  # what the agent sends back (agent_output_audio_format pcm_44100)
WAKE_FRAME = 1280  # 80 ms, openWakeWord's native chunk
WAKE_THRESHOLD = 0.5
DEVICE_RECHECK_SECONDS = 10


class VoiceProcessingAudio(AudioInterface):
    """Conversation audio through Apple's voice processing (the echo cancellation FaceTime uses), so Jarvis
    doesn't hear itself on the laptop speakers and you can still talk over it."""

    def __init__(self):
        self.pending = 0  # buffers scheduled but not yet played
        self.stopping = False
        self._stop_lock = threading.Lock()
        self._tap_thread = None
        # Set here, not in start(): the SDK calls start() from its own thread after connecting, and the idle timer
        # may read this first.
        self.last_played = time.monotonic()

    def start(self, input_callback):
        self.engine = AV.AVAudioEngine.new()
        mic = self.engine.inputNode()
        self.engine.outputNode(), self.engine.mainMixerNode()  # must exist before voice processing, or start fails
        # Always the system input: selecting a device here makes voice processing fail to start or go silent
        # about half the time, so the mic menu switches macOS's input instead.
        mic.setVoiceProcessingEnabled_error_(True, None)
        # Voice processing reconfigures the input from 5 to 7 channels a moment after it's enabled; a graph built
        # before that never receives audio.
        deadline = time.monotonic() + 3
        while mic.inputFormatForBus_(0).channelCount() == 5 and time.monotonic() < deadline:
            time.sleep(0.05)

        self.player = AV.AVAudioPlayerNode.new()
        self.engine.attachNode_(self.player)
        self.out_format = AV.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(OUTPUT_RATE, 1)
        self.engine.connect_to_format_(self.player, self.engine.mainMixerNode(), self.out_format)

        def tap(buffer, _when):
            if self.stopping:
                return
            self._tap_thread = threading.get_ident()
            try:
                frames = buffer.frameLength()
                # Channel 0 is the echo-cancelled voice; the others are raw mic channels.
                voice = np.frombuffer(buffer.floatChannelData()[0].as_buffer(frames), dtype=np.float32)
                input_callback(to_pcm16(voice, buffer.format().sampleRate()))
            except Exception as error:  # noqa: BLE001
                # An exception escaping here turns into an NSException inside AVFoundation and kills the app.
                print(f"mic callback error: {error!r}")

        mic.installTapOnBus_bufferSize_format_block_(0, 4096, None, tap)
        self.engine.prepare()
        ok, error = self.engine.startAndReturnError_(None)
        if not ok:
            self.engine = None  # stop() must never touch an engine that failed to start: that crashes the process
            raise RuntimeError(f"Couldn't start voice processing: {error}")
        self.player.play()

    def stop(self):
        if threading.get_ident() == self._tap_thread:
            # When the agent hangs up, the SDK stops the session from inside the mic callback; removing a tap from its
            # own callback deadlocks or crashes, so finish on another thread.
            threading.Thread(target=self.stop, daemon=True).start()
            return
        with self._stop_lock:
            if not getattr(self, "engine", None):
                return
            self.stopping = True
            # Let the sign-off finish before cutting the speaker.
            deadline = time.monotonic() + 5
            while self.pending and time.monotonic() < deadline:
                time.sleep(0.1)
            self.engine.inputNode().removeTapOnBus_(0)
            self.engine.stop()
            self.engine.inputNode().setVoiceProcessingEnabled_error_(False, None)
            self.engine = None

    def output(self, audio: bytes):
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768
        buffer = AV.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(self.out_format, len(samples))
        buffer.setFrameLength_(len(samples))
        np.frombuffer(buffer.floatChannelData()[0].as_buffer(len(samples)), dtype=np.float32)[:] = samples
        self.pending += 1
        self.player.scheduleBuffer_completionHandler_(buffer, self._played)

    def _played(self):
        self.pending -= 1
        self.last_played = time.monotonic()

    def interrupt(self):
        # stop() drops everything scheduled (firing each completion handler); play() readies it for new audio.
        self.player.stop()
        self.player.play()


def to_pcm16(samples: np.ndarray, rate: float) -> bytes:
    """Float samples at the mic's rate to 16 kHz int16, what ElevenAgents expects."""
    count = int(len(samples) * RATE / rate)
    resampled = np.interp(np.linspace(0, len(samples) - 1, count), np.arange(len(samples)), samples)
    return (np.clip(resampled, -1, 1) * 32767).astype(np.int16).tobytes()


def heard_wake_word(model: Model, frame: np.ndarray) -> bool:
    return max(model.predict(frame).values()) > WAKE_THRESHOLD


def input_mics() -> list[str]:
    return [d["name"] for d in sd.query_devices() if d["max_input_channels"] > 0]


def refresh_devices():
    """PortAudio reads the device list once; re-read it so a plugged or unplugged headset is picked up."""
    sd._terminate()
    sd._initialize()


def wait_for_wake_word(model: Model, interrupt: threading.Event) -> bool:
    """True when "Hey Jarvis" is heard. False when `interrupt` is set (Talk now, or a mic change) or after
    DEVICE_RECHECK_SECONDS, so the caller can refresh the device list while no stream is open."""
    stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=WAKE_FRAME)
    deadline = time.monotonic() + DEVICE_RECHECK_SECONDS
    with stream:
        while not interrupt.is_set() and time.monotonic() < deadline:
            frame, _ = stream.read(WAKE_FRAME)
            if heard_wake_word(model, frame[:, 0]):
                model.reset()
                return True
    return False
