import numpy as np

from jarvis.audio import to_pcm16


def test_to_pcm16_resamples_48k_to_16k_and_clips():
    samples = np.full(4800, 2.0, dtype=np.float32)  # 100 ms at 48 kHz, out of range on purpose
    pcm = np.frombuffer(to_pcm16(samples, 48000), dtype=np.int16)
    assert len(pcm) == 1600
    assert pcm.max() == 32767


def test_to_pcm16_keeps_a_tone_at_the_same_frequency():
    t = np.arange(48000) / 48000
    pcm = np.frombuffer(to_pcm16(np.sin(2 * np.pi * 440 * t).astype(np.float32) * 0.5, 48000), dtype=np.int16)
    peak_hz = np.argmax(np.abs(np.fft.rfft(pcm))) * 16000 / len(pcm)
    assert abs(peak_hz - 440) < 2
