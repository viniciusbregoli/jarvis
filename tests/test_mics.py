from jarvis import coreaudio


def test_labeled_numbers_repeated_names():
    pairs = [(89, "MacBook Air Microphone"), (101, "USB Headset"), (102, "USB Headset")]
    assert coreaudio.labeled(pairs) == {"MacBook Air Microphone": 89, "USB Headset": 101, "USB Headset (2)": 102}


def test_inputs_leave_out_output_only_devices(monkeypatch):
    names = {89: "MacBook Air Microphone", 82: "MacBook Air Speakers", 101: "USB Headset", 102: "USB Headset"}
    monkeypatch.setattr(coreaudio, "devices", lambda: list(names))
    monkeypatch.setattr(coreaudio, "name", names.get)
    monkeypatch.setattr(coreaudio, "has_input", lambda device: device in (89, 102))
    assert coreaudio.inputs() == {"MacBook Air Microphone": 89, "USB Headset": 102}
