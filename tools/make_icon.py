# -*- coding: utf-8 -*-
r"""★把 `frontend/assets/icon.svg` 变成 Windows 图标（多尺寸 `.ico` + 预览 PNG）

为什么要有这个脚本：
  安装包 / 托盘 / 面板窗口 / 网页 favicon 都吃 **`.ico`**，而设计稿是 **SVG**。
  手搓一份 ico 之后没人知道它跟 SVG 还同不同步 —— 所以把"渲染 + 拼装"固化成脚本：
  **改图标只改 `icon.svg`**，然后跑一次这个脚本。

怎么做的（不引入新依赖）：
  1. 用系统自带 Edge（headless）把 SVG 渲染成 256×256 RGBA PNG（透明背景）；
  2. 自己解 PNG（zlib + 反滤波，只认 8 位 RGB/RGBA、非隔行 —— Edge 就是这个）；
  3. **面积平均**降采样到 16/24/32/48/64/128（256 直接用原图）；
  4. 拼成 ICO：≤128 用 BMP(DIB) 条目（兼容性最好），256 用 PNG 条目（Vista+ 标准做法）。

用法：
    python tools\make_icon.py                 :: 写 frontend\assets\cocraft.ico + icon-256.png
    python tools\make_icon.py --check         :: 只检查现有 ico 是否包含全部尺寸（CI / 体检用）

输出纯文本（`[OK]` / `[BAD]`）。
"""
from __future__ import annotations

import argparse
import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SVG = os.path.join(ROOT, "frontend", "assets", "icon.svg")
ICO = os.path.join(ROOT, "frontend", "assets", "cocraft.ico")
PNG256 = os.path.join(ROOT, "frontend", "assets", "icon-256.png")
SIZES = (16, 24, 32, 48, 64, 128, 256)
EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------- PNG 解码 / 编码

def read_png(path: str):
    """PNG → (w, h, rgba bytes)。只认 8 位 RGB/RGBA、非隔行。"""
    data = open(path, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos, idat, w, h, ctype = 8, b"", 0, 0, 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w, h, depth, ctype, _comp, _filt, inter = struct.unpack(">IIBBBBB", body)
            if depth != 8 or ctype not in (2, 6) or inter != 0:
                raise ValueError("只支持 8 位 RGB/RGBA 非隔行（拿到 depth=%d type=%d inter=%d）"
                                 % (depth, ctype, inter))
        elif typ == b"IDAT":
            idat += body
        elif typ == b"IEND":
            break
        pos += 12 + ln
    raw = zlib.decompress(idat)
    bpp = 4 if ctype == 6 else 3
    stride = w * bpp
    out = bytearray()
    prev = bytearray(stride)
    p = 0
    for _y in range(h):
        ft = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ft == 1:                      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:                    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ft == 3:                    # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif ft == 4:                    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        elif ft != 0:
            raise ValueError("未知的 PNG 行滤波 %d" % ft)
        if bpp == 4:
            out += line
        else:
            for i in range(0, stride, 3):
                out += bytes((line[i], line[i + 1], line[i + 2], 255))
        prev = line
    return w, h, bytes(out)


def write_png(path: str, w: int, h: int, rgba: bytes) -> None:
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                     # filter: none
        raw += rgba[y * stride:(y + 1) * stride]

    def chunk(typ: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + typ + body
                + struct.pack(">I", zlib.crc32(typ + body) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


def downscale(sw: int, sh: int, rgba: bytes, dw: int, dh: int) -> bytes:
    """面积平均降采样（先预乘 alpha，避免透明边缘发黑）。"""
    out = bytearray(dw * dh * 4)
    xs = sw / dw
    ys = sh / dh
    for dy in range(dh):
        y0, y1 = dy * ys, (dy + 1) * ys
        for dx in range(dw):
            x0, x1 = dx * xs, (dx + 1) * xs
            r = g = b = a = wsum = 0.0
            sy = int(y0)
            while sy < min(sh, int(y1) + 1):
                oy = max(0.0, min(y1, sy + 1) - max(y0, sy))
                sx = int(x0)
                while sx < min(sw, int(x1) + 1):
                    ox = max(0.0, min(x1, sx + 1) - max(x0, sx))
                    wgt = ox * oy
                    if wgt > 0:
                        i = (sy * sw + sx) * 4
                        pa = rgba[i + 3] / 255.0
                        r += rgba[i] * pa * wgt
                        g += rgba[i + 1] * pa * wgt
                        b += rgba[i + 2] * pa * wgt
                        a += pa * wgt
                        wsum += wgt
                    sx += 1
                sy += 1
            if wsum <= 0:
                continue
            aavg = a / wsum
            o = (dy * dw + dx) * 4
            if aavg > 0:
                out[o] = min(255, int(round(r / wsum / aavg)))
                out[o + 1] = min(255, int(round(g / wsum / aavg)))
                out[o + 2] = min(255, int(round(b / wsum / aavg)))
            out[o + 3] = min(255, int(round(aavg * 255)))
    return bytes(out)


# ---------------------------------------------------------------- ICO 拼装

def _bmp_entry(w: int, h: int, rgba: bytes) -> bytes:
    """32bpp BGRA 的 DIB + 1bpp AND 掩码（全 0 = 不透明）。"""
    rows = []
    stride = w * 4
    for y in range(h - 1, -1, -1):                # 自下而上
        line = bytearray()
        o = y * stride
        for x in range(w):
            i = o + x * 4
            line += bytes((rgba[i + 2], rgba[i + 1], rgba[i], rgba[i + 3]))   # BGRA
        rows.append(bytes(line))
    pixels = b"".join(rows)
    mask_row = ((w + 31) // 32) * 4               # 1bpp 行按 4 字节对齐
    mask = b"\x00" * (mask_row * h)
    header = struct.pack("<IiiHHIIiiII", 40, w, h * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    return header + pixels + mask


def build_ico(pngs: dict) -> bytes:
    """pngs: {size: (w, h, rgba, png_bytes or None)} → 完整 .ico 字节。"""
    blobs = []
    for size in sorted(pngs):
        w, h, rgba, png = pngs[size]
        blobs.append((size, png if size >= 256 else _bmp_entry(w, h, rgba)))
    count = len(blobs)
    out = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, body = b"", b""
    for size, blob in blobs:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(blob), offset)
        offset += len(blob)
        body += blob
    return out + entries + body


def ico_sizes(path: str) -> list:
    """读 .ico 目录 → [尺寸]（0 表示 256）。"""
    try:
        data = open(path, "rb").read()
    except OSError:
        return []
    if len(data) < 6 or data[:4] != b"\x00\x00\x01\x00":
        return []
    n = struct.unpack("<H", data[4:6])[0]
    out = []
    for i in range(n):
        e = data[6 + 16 * i:6 + 16 * (i + 1)]
        if len(e) < 16:
            break
        out.append(e[0] or 256)
    return out


# ---------------------------------------------------------------- Edge 渲染

def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("[BAD] 没找到 Edge（headless 渲染 SVG 要用它）")


def render_svg(px: int = 256) -> str:
    edge = find_edge()
    tmp = tempfile.mkdtemp(prefix="cocraft_icon_")
    svg = open(SVG, "r", encoding="utf-8").read()
    html = ("<!doctype html><html><head><meta charset='utf-8'><style>"
            "html,body{margin:0;padding:0;background:transparent;overflow:hidden}"
            "svg{display:block;width:%dpx;height:%dpx}</style></head><body>%s</body></html>"
            % (px, px, svg))
    page = os.path.join(tmp, "icon.html")
    with open(page, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    png = os.path.join(tmp, "icon.png")
    subprocess.run([edge, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    "--default-background-color=00000000",
                    "--user-data-dir=" + os.path.join(tmp, "profile"),
                    "--screenshot=" + png, "--window-size=%d,%d" % (px, px),
                    "file:///" + page.replace("\\", "/")],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    if not os.path.isfile(png):
        raise SystemExit("[BAD] Edge 没有产出 PNG")
    return png


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="从 icon.svg 生成 cocraft.ico")
    ap.add_argument("--check", action="store_true", help="只检查现有 ico 的尺寸集合")
    args = ap.parse_args(argv)

    if args.check:
        got = ico_sizes(ICO)
        ok = got == sorted(SIZES)
        print("%s %s 尺寸：%s" % ("[OK] " if ok else "[BAD]", os.path.relpath(ICO, ROOT), got))
        return 0 if ok else 1

    if not os.path.isfile(SVG):
        print("[BAD] 找不到设计稿：%s" % SVG)
        return 1
    src_png = render_svg(256)
    sw, sh, rgba = read_png(src_png)
    if (sw, sh) != (256, 256):
        print("[BAD] 渲染尺寸不对：%dx%d" % (sw, sh))
        return 1
    pngs = {}
    for size in SIZES:
        if size == 256:
            img = rgba
            buf = open(src_png, "rb").read()
        else:
            img = downscale(sw, sh, rgba, size, size)
            tmp = os.path.join(os.path.dirname(src_png), "s%d.png" % size)
            write_png(tmp, size, size, img)
            buf = None
        pngs[size] = (size, size, img, buf)
    with open(ICO, "wb") as fh:
        fh.write(build_ico(pngs))
    write_png(PNG256, sw, sh, rgba)
    print("[OK] 已生成 %s（%d 字节，尺寸 %s）"
          % (os.path.relpath(ICO, ROOT), os.path.getsize(ICO), ico_sizes(ICO)))
    print("[OK] 预览图 %s" % os.path.relpath(PNG256, ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
