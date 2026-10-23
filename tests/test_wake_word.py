import subprocess
import wave

import numpy as np
import openwakeword
from openwakeword.model import Model

from jarvis.audio import WAKE_FRAME, heard_wake_word


def spoken(text: str, tmp_path) -> np.ndarray:
    """Synthesize speech with macOS `say` as 16 kHz int16, padded with a second of silence each side."""
    path = tmp_path / "speech.wav"
    subprocess.run(["say", "-o", str(path), "--data-format=LEI16@16000", text], check=True)
    with wave.open(str(path)) as f:
        speech = np.frombuffer(f.readframes(f.getnframes()), dtype=np.int16)
    silence = np.zeros(16000, dtype=np.int16)
    return np.concatenate([silence, speech, silence])


def detects(audio: np.ndarray) -> bool:
    model = Model(wakeword_model_paths=[openwakeword.models["hey_jarvis"]["model_path"]])
    frames = (audio[i : i + WAKE_FRAME] for i in range(0, len(audio) - WAKE_FRAME, WAKE_FRAME))
    return any(heard_wake_word(model, frame) for frame in frames)


def test_hears_hey_jarvis(tmp_path):
    assert detects(spoken("Hey Jarvis", tmp_path))


def test_ignores_other_speech(tmp_path):
    assert not detects(spoken("What's the weather like today", tmp_path))
