"""只截目标窗口（PrintWindow），不抢焦点、不截全屏。

纯 ctypes（GDI）+ 标准库 zlib 手写 PNG —— 不依赖 PIL、不启 PowerShell 子进程。
一次典型耗时 ~30ms（2560x1600）。
"""

import ctypes
import os
import struct
import time
import zlib
from ctypes import wintypes as wt

from . import winapi

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

# 64 位句柄必须声明 restype，否则 HDC/HBITMAP 被截成 32 位
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.GetWindowDC.restype = ctypes.c_void_p
user32.GetWindowDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, wt.DWORD]

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(os.path.dirname(HERE), "..", "runtime", "cache", "cu")
CACHE_DIR = os.path.abspath(CACHE_DIR)

KEEP_SECONDS = 600      # 10 分钟
KEEP_FILES = 30         # 最多 30 张


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
        ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wt.DWORD]


def _png_bytes(rgba, width, height):
    """RGBA bytes → PNG bytes（每行 filter 0，zlib 流式压缩）。"""
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    comp = zlib.compressobj(6)
    stride = width * 4
    parts = []
    for y in range(height):
        parts.append(comp.compress(b"\x00" + rgba[y * stride:(y + 1) * stride]))
    parts.append(comp.flush())
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", b"".join(parts))
            + chunk(b"IEND", b""))


def _mean_brightness(rgba, width, height):
    """抽样估算亮度（0~255），用于自检"是不是一张白/黑图"。"""
    total = width * height
    step = max(1, total // 20000)
    acc = 0
    n = 0
    for i in range(0, total, step):
        p = i * 4
        acc += (rgba[p] * 299 + rgba[p + 1] * 587 + rgba[p + 2] * 114) // 1000
        n += 1
    return round(acc / n, 1) if n else 0.0


def _downscale(rgba, width, height, max_width):
    """最近邻缩到 max_width（切片实现，够快）。"""
    if not max_width or width <= max_width:
        return rgba, width, height
    step = int(width / float(max_width))
    if step < 1:
        step = 1
    new_w = (width + step - 1) // step
    out = bytearray(new_w * height * 4)
    pos = 0
    for y in range(height):
        row = rgba[y * width * 4:(y + 1) * width * 4]
        o = bytearray(new_w * 4)
        o[0::4] = row[0::4][::step]
        o[1::4] = row[1::4][::step]
        o[2::4] = row[2::4][::step]
        o[3::4] = row[3::4][::step]
        out[pos:pos + len(o)] = o
        pos += len(o)
    return bytes(out), new_w, height


def prune_cache():
    """清掉过期 / 超量的截图。"""
    try:
        if not os.path.isdir(CACHE_DIR):
            return 0
        now = time.time()
        items = []
        for name in os.listdir(CACHE_DIR):
            if not name.lower().endswith(".png"):
                continue
            p = os.path.join(CACHE_DIR, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append((st.st_mtime, p))
        items.sort(reverse=True)
        removed = 0
        for idx, (mtime, p) in enumerate(items):
            if idx >= KEEP_FILES or (now - mtime) > KEEP_SECONDS:
                try:
                    os.remove(p)
                    removed += 1
                except OSError:
                    pass
        return removed
    except Exception:
        return 0


def capture_window(hwnd, out_path=None, max_width=None, full_content=True):
    """截目标窗口 → 保存 PNG，返回元信息。不激活窗口。"""
    winapi.init_dpi()
    hwnd = int(hwnd)
    if not winapi.is_window(hwnd):
        return {"ok": False, "error": "窗口不存在或已关闭（hwnd=%d）" % hwnd}

    rect = winapi.raw_rect(hwnd)
    if not rect:
        return {"ok": False, "error": "拿不到窗口矩形"}
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        return {"ok": False, "error": "窗口尺寸为 0（可能已最小化）", "rect": rect}

    hdc_screen = user32.GetDC(None)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bits = ctypes.c_void_p()
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height   # 负值 = 自上而下
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                  ctypes.byref(bits), None, 0)
    old = None
    try:
        if not hbmp or not bits:
            return {"ok": False, "error": "CreateDIBSection 失败"}
        old = gdi32.SelectObject(hdc_mem, hbmp)
        flags = PW_RENDERFULLCONTENT if full_content else 0
        if not user32.PrintWindow(wt.HWND(hwnd), hdc_mem, flags):
            # 少数窗口不响应 PrintWindow → 退到 BitBlt（会拍到遮挡物）
            if not gdi32.BitBlt(hdc_mem, 0, 0, width, height,
                                user32.GetWindowDC(wt.HWND(hwnd)), 0, 0, 0x00CC0020):
                return {"ok": False, "error": "PrintWindow / BitBlt 均失败"}

        raw = ctypes.string_at(bits, width * height * 4)
        arr = bytearray(raw)
        arr[0::4], arr[2::4] = arr[2::4], arr[0::4]   # BGRA → RGBA
        arr[3::4] = b"\xff" * (width * height)        # PrintWindow 的 alpha 常为 0
        rgba = bytes(arr)

        rgba, out_w, out_h = _downscale(rgba, width, height, max_width)

        if not out_path:
            prune_cache()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            out_path = os.path.join(CACHE_DIR, "win-%d-%s.png" % (hwnd, stamp))
        else:
            out_path = os.path.abspath(out_path)
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

        data = _png_bytes(rgba, out_w, out_h)
        with open(out_path, "wb") as f:
            f.write(data)

        return {
            "ok": True,
            "path": out_path,
            "width": out_w,
            "height": out_h,
            "src_width": width,
            "src_height": height,
            "bytes": len(data),
            "mean": _mean_brightness(rgba, out_w, out_h),
            "hwnd": hwnd,
            "window_title": winapi.window_text(hwnd),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
    finally:
        if old:
            gdi32.SelectObject(hdc_mem, old)
        if hbmp:
            gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
