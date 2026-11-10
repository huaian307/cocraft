"""SendInput / 鼠标 / 剪贴板 —— 纯 ctypes。

只用 SendInput（不是 keybd_event）：SendInput 是原子的、能一次发完整序列，
UNICODE 模式可以直接送中文/emoji，不用管当前键盘布局。
"""

import ctypes
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# ⚠ 64 位下句柄是 8 字节：不声明 restype 会被截成 int，GlobalAlloc/GetClipboardData
# 直接失败（"clipboard-set 返回 False" 就是这么来的）。
kernel32.GlobalAlloc.restype = ctypes.c_void_p
kernel32.GlobalAlloc.argtypes = [wt.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
user32.OpenClipboard.argtypes = [ctypes.c_void_p]
user32.GetClipboardData.restype = ctypes.c_void_p
user32.GetClipboardData.argtypes = [wt.UINT]
user32.SetClipboardData.restype = ctypes.c_void_p
user32.SetClipboardData.argtypes = [wt.UINT, ctypes.c_void_p]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
kernel32.CloseHandle.argtypes = [ctypes.c_void_p]

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800

KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_EXTENDEDKEY = 0x0001

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

VK = {
    "CTRL": 0x11, "CONTROL": 0x11, "ALT": 0x12, "SHIFT": 0x10, "WIN": 0x5B,
    "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09, "ESC": 0x1B, "ESCAPE": 0x1B,
    "SPACE": 0x20, "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
    "INSERT": 0x2D, "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "F1": 0x70, "F2": 0x71, "F3": 0x72, "F4": 0x73, "F5": 0x74, "F6": 0x75,
    "F7": 0x76, "F8": 0x77, "F9": 0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "PRINTSCREEN": 0x2C, "CAPSLOCK": 0x14, "NUMLOCK": 0x90,
}


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.POINTER(wt.ULONG))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


user32.SendInput.argtypes = [wt.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wt.UINT


def _send(inputs):
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    sent = user32.SendInput(n, arr, ctypes.sizeof(INPUT))
    return sent == n


def _key(vk, up=False, unicode_char=None):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    if unicode_char is not None:
        inp.ki.wVk = 0
        inp.ki.wScan = ord(unicode_char)
        inp.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    else:
        inp.ki.wVk = vk
        inp.ki.wScan = 0
        inp.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    return inp


def _mouse(flags, dx=0, dy=0, data=0):
    inp = INPUT()
    inp.type = INPUT_MOUSE
    inp.mi.dx = dx
    inp.mi.dy = dy
    inp.mi.mouseData = data
    inp.mi.dwFlags = flags
    return inp


def _virtual_key(name):
    name = str(name).strip().upper()
    if name in VK:
        return VK[name]
    if len(name) == 1:
        return ord(name)
    raise ValueError("不认识的按键：%s" % name)


def parse_keys(spec):
    """"CTRL+S" / "ctrl+alt+y" / ["CTRL","S"] → [vk, vk, ...]"""
    if isinstance(spec, (list, tuple)):
        parts = list(spec)
    else:
        parts = [p for p in str(spec).replace(" ", "").split("+") if p]
    if not parts:
        raise ValueError("按键为空")
    return [_virtual_key(p) for p in parts]


def send_keys(spec, hold_ms=20):
    """按下组合键再逆序抬起。"""
    vks = parse_keys(spec)
    downs = [_key(v) for v in vks]
    ups = [_key(v, up=True) for v in reversed(vks)]
    ok = _send(downs)
    time.sleep(max(0.01, hold_ms / 1000.0))
    ok = _send(ups) and ok
    return ok


def type_text(text, per_char_ms=6):
    """UNICODE 直送（中文/emoji 都行，不看键盘布局）。"""
    text = str(text)
    if not text:
        return True
    inputs = []
    for ch in text:
        if ch == "\n":
            inputs.append(_key(VK["ENTER"]))
            inputs.append(_key(VK["ENTER"], up=True))
            continue
        inputs.append(_key(0, unicode_char=ch))
        inputs.append(_key(0, up=True, unicode_char=ch))
    for i in range(0, len(inputs), 200):
        if not _send(inputs[i:i + 200]):
            return False
        if per_char_ms:
            time.sleep(per_char_ms / 1000.0)
    return True


def cursor_pos():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def click_at(x, y, button="left", clicks=1, gap_ms=40):
    """移动光标到物理像素坐标并点击。"""
    if not user32.SetCursorPos(int(x), int(y)):
        return False
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }.get(str(button).lower(), (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP))
    for i in range(max(1, int(clicks))):
        if not _send([_mouse(down)]):
            return False
        time.sleep(0.02)
        if not _send([_mouse(up)]):
            return False
        if i + 1 < clicks:
            time.sleep(max(0.01, gap_ms / 1000.0))
    return True


def scroll(amount, x=None, y=None):
    if x is not None and y is not None:
        user32.SetCursorPos(int(x), int(y))
    return _send([_mouse(MOUSEEVENTF_WHEEL, data=int(amount))])


# ---------------------------------------------------------------- 剪贴板

def clipboard_get():
    if not user32.OpenClipboard(None):
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def clipboard_set(text):
    text = str(text)
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not user32.OpenClipboard(None):
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return False
        try:
            ctypes.memmove(ptr, data, len(data))
        finally:
            kernel32.GlobalUnlock(h)
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            return False
        return True
    finally:
        user32.CloseClipboard()
