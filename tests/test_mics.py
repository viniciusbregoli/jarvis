from jarvis import audio

DEVICES = [
    {"name": "MacBook Air Speakers", "max_input_channels": 0},
    {"name": "External Microphone", "max_input_channels": 1},
    {"name": "MacBook Air Microphone", "max_input_channels": 1},
]


def test_lists_only_inputs(monkeypatch):
    monkeypatch.setattr(audio.sd, "query_devices", lambda: DEVICES)
    assert audio.input_mics() == ["External Microphone", "MacBook Air Microphone"]


def test_coreaudio_find_matches_exact_names(monkeypatch):
    from jarvis import coreaudio

    names = {89: "MacBook Air Microphone", 68: "Microsoft Teams Audio"}
    monkeypatch.setattr(coreaudio, "devices", lambda: list(names))
    monkeypatch.setattr(coreaudio, "name", names.get)
    assert coreaudio.find("Microsoft Teams Audio") == 68
    assert coreaudio.find("Unplugged Headset") is None
