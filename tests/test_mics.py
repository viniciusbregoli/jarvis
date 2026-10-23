from jarvis import audio

DEVICES = [
    {"name": "MacBook Air Speakers", "max_input_channels": 0},
    {"name": "External Microphone", "max_input_channels": 1},
    {"name": "MacBook Air Microphone", "max_input_channels": 1},
]


def test_lists_only_inputs(monkeypatch):
    monkeypatch.setattr(audio.sd, "query_devices", lambda: DEVICES)
    assert audio.input_mics() == ["External Microphone", "MacBook Air Microphone"]


def test_finds_mic_by_name_and_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(audio.sd, "query_devices", lambda: DEVICES)
    assert audio.input_device("MacBook Air Microphone") == 2
    assert audio.input_device(None) is None
    assert audio.input_device("Unplugged Headset") is None
    assert audio.input_device("MacBook Air Speakers") is None
