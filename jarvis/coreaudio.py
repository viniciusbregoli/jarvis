"""The few CoreAudio calls Jarvis needs (list devices, read a name, switch the system input), through ctypes: PyObjC has no CoreAudio
bindings."""

import ctypes
import ctypes.util
import struct

import objc

_lib = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))
SYSTEM_OBJECT = 1


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32), ("element", ctypes.c_uint32)]


def _code(four: str) -> int:
    return struct.unpack(">I", four.encode())[0]


def _address(selector: str) -> _Address:
    return _Address(_code(selector), _code("glob"), 0)


def devices() -> list[int]:
    address, size = _address("dev#"), ctypes.c_uint32()
    _lib.AudioObjectGetPropertyDataSize(SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size))
    ids = (ctypes.c_uint32 * (size.value // 4))()
    _lib.AudioObjectGetPropertyData(SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size), ids)
    return list(ids)


def default_input() -> int:
    address, device = _address("dIn "), ctypes.c_uint32()
    size = ctypes.c_uint32(ctypes.sizeof(device))
    _lib.AudioObjectGetPropertyData(
        SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device)
    )
    return device.value


def has_input(device: int) -> bool:
    address, size = _Address(_code("stm#"), _code("inpt"), 0), ctypes.c_uint32()
    _lib.AudioObjectGetPropertyDataSize(device, ctypes.byref(address), 0, None, ctypes.byref(size))
    return size.value > 0


def labeled(pairs: list[tuple[int, str]]) -> dict[str, int]:
    """Menu label -> device id; repeated names get " (2)", " (3)" so each device stays pickable."""
    seen: dict[str, int] = {}
    labels = {}
    for device, device_name in pairs:
        seen[device_name] = seen.get(device_name, 0) + 1
        labels[device_name if seen[device_name] == 1 else f"{device_name} ({seen[device_name]})"] = device
    return labels


def inputs() -> dict[str, int]:
    """Input-capable devices by menu label. Output-only devices are left out, since macOS refuses them as input."""
    return labeled([(device, name(device)) for device in devices() if has_input(device)])


def set_default_input(device: int):
    """Same as picking the input in System Settings > Sound."""
    address, value = _address("dIn "), ctypes.c_uint32(device)
    _lib.AudioObjectSetPropertyData(
        SYSTEM_OBJECT, ctypes.byref(address), 0, None, ctypes.sizeof(value), ctypes.byref(value)
    )


def name(device: int) -> str:
    address, ref = _address("lnam"), ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if _lib.AudioObjectGetPropertyData(device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(ref)):
        return ""
    return str(objc.objc_object(c_void_p=ref.value))
