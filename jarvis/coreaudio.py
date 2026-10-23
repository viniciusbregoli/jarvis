"""The two CoreAudio calls Jarvis needs (list devices, read a name), through ctypes: PyObjC has no CoreAudio
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


def name(device: int) -> str:
    address, ref = _address("lnam"), ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(ref))
    if _lib.AudioObjectGetPropertyData(device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(ref)):
        return ""
    return str(objc.objc_object(c_void_p=ref.value))
