import ctypes
import time
from typing import Protocol

class InputBackend(Protocol):
    def move_mouse(self, dx: int, dy: int) -> None: ...
    def key_down(self, ch: str) -> None: ...
    def key_up(self, ch: str) -> None: ...
    def key_tap(self, ch: str) -> None: ...

# ---------------- WinAPI implementation ----------------
user32 = ctypes.windll.user32

PUL = ctypes.POINTER(ctypes.c_ulong)

class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    )

class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", ctypes.c_ushort),
        ("wScan", ctypes.c_ushort),
        ("dwFlags", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("dwExtraInfo", PUL),
    )

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", ctypes.c_ulong),
        ("wParamL", ctypes.c_short),
        ("wParamH", ctypes.c_ushort),
    )

class INPUT(ctypes.Structure):
    class _I(ctypes.Union):
        _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT))

    _anonymous_ = ("i",)
    _fields_ = (("type", ctypes.c_ulong), ("i", _I))

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
MOUSEEVENTF_MOVE = 0x0001
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_KEYUP = 0x0002

VK_MAP = {
    "f": 0x21,
}

VK_FKEY = {
    "f8": (0x42, 0x4200),
    "f9": (0x43, 0x4300),
}

def _ensure_char(key) -> str:
    if key is None:
        return ""
    if isinstance(key, int):
        try:
            return chr(key & 0xFFFF)
        except ValueError:
            return ""
    text = str(key)
    return text[:1]

def _scan_for_char(ch: str):
    ch = _ensure_char(ch).lower()
    if not ch or not ('a' <= ch <= 'z'):
        return None, None
    vk = user32.VkKeyScanW(ch) & 0xFF
    sc = user32.MapVirtualKeyW(vk, 0)
    return vk, sc

def _send_mouse_move(dx, dy):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = MOUSEINPUT(dx, dy, 0, MOUSEEVENTF_MOVE, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

def _send_key(key: str, is_up=False):
    ch = _ensure_char(key)
    if not ch:
        return
    vk = user32.VkKeyScanW(ch)
    if vk == -1:
        return
    sc = user32.MapVirtualKeyW(vk & 0xFF, 0)
    if sc == 0:
        return
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    flags = KEYEVENTF_SCANCODE
    if is_up:
        flags |= KEYEVENTF_KEYUP
    inp.ki = KEYBDINPUT(0, sc, flags, 0, None)
    user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))

class WinAPIBackend:
    def move_mouse(self, dx: int, dy: int) -> None:
        _send_mouse_move(dx, dy)

    def key_down(self, ch: str) -> None:
        _send_key(ch, is_up=False)

    def key_up(self, ch: str) -> None:
        _send_key(ch, is_up=True)

    def key_tap(self, ch: str) -> None:
        self.key_down(ch)
        time.sleep(0.01)
        self.key_up(ch)

# ---------------- fallback: pynput ----------------
from pynput.mouse import Controller as PMouse
from pynput.keyboard import Controller as PKey

class PynputBackend:
    def __init__(self):
        self.m = PMouse()
        self.k = PKey()

    def move_mouse(self, dx: int, dy: int) -> None:
        self.m.move(dx, dy)

    def key_down(self, ch: str) -> None:
        self.k.press(ch)

    def key_up(self, ch: str) -> None:
        self.k.release(ch)

    def key_tap(self, ch: str) -> None:
        self.key_down(ch)
        self.key_up(ch)
