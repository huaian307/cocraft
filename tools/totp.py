# -*- coding: utf-8 -*-
r"""本地 TOTP（两步验证）验证码生成器 —— **纯标准库、完全离线**。

用途：GitHub（或任何支持 TOTP 的服务）要求开两步验证时，如果没有手机 App，
可以在这里算 6 位验证码。

⚠ 三条铁律：
  1. **种子（setup key）＝ 账号第二把钥匙** —— 别贴到聊天里、别提交进仓库。
     本工具只从「隐藏输入的终端」或环境变量 `COCRAFT_TOTP_SECRET` 读，**从不打印种子**。
  2. **不联网**：纯 `hmac` / `hashlib` 计算，不发任何请求。
  3. 种子存 `runtime/state/_totp.json`（`runtime/` 已在 .gitignore 里，不进仓库）。
     它是**明文**，只在本机；介意的话改用手机 App，或每次用 env 临时给。

用法：
    python tools\totp.py --set          # 录一次 setup key（隐藏输入，不回显）
    python tools\totp.py                # 打印当前 6 位码 + 剩余秒数
    python tools\totp.py --watch        # 每秒刷新（盯着填）
    python tools\totp.py --copy         # 算完顺便放进剪贴板（Windows clip）
    python tools\totp.py --check        # 用 RFC 6238 官方测试向量自检（不读种子）
    python tools\totp.py --forget       # 删掉本机存的种子

退出码：0 正常 / 2 没有种子 / 3 种子格式不对。
"""
from __future__ import annotations

import argparse
import base64
import binascii
import getpass
import hashlib
import hmac
import json
import os
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(HERE, "runtime", "state")
SECRET_FILE = os.path.join(STATE_DIR, "_totp.json")
ENV_KEY = "COCRAFT_TOTP_SECRET"

DEFAULT_STEP = 30
DEFAULT_DIGITS = 6


def normalize(secret: str) -> str:
    """把 GitHub 显示的 setup key 归一：去空格/短横线、转大写、补 `=` 填充。"""
    s = "".join(str(secret or "").split()).replace("-", "").upper()
    if not s:
        return ""
    pad = (-len(s)) % 8
    return s + ("=" * pad)


def decode_secret(secret: str) -> bytes:
    s = normalize(secret)
    if not s:
        raise ValueError("没有种子")
    try:
        return base64.b32decode(s, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("种子不是合法的 base32：%s" % exc)


def totp(secret: str, t: float | None = None, digits: int = DEFAULT_DIGITS,
         step: int = DEFAULT_STEP, algo: str = "sha1") -> str:
    """标准 TOTP（RFC 6238）：HMAC → 动态截断 → 取 `digits` 位。"""
    raw = decode_secret(secret)
    counter = int((time.time() if t is None else t) // step)
    mac = hmac.new(raw, struct.pack(">Q", counter), getattr(hashlib, algo)).digest()
    off = mac[-1] & 0x0F
    code = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


def remaining(t: float | None = None, step: int = DEFAULT_STEP) -> int:
    now = time.time() if t is None else t
    return int(step - (now % step))


# ---------------------------------------------------------------- 种子存取

def save_secret(secret: str) -> str:
    s = normalize(secret)
    decode_secret(s)                               # 先校验，别存坏种子
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = SECRET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump({"secret": s, "saved": int(time.time())}, fh)
    os.replace(tmp, SECRET_FILE)
    return SECRET_FILE


def load_secret() -> str:
    env = os.environ.get(ENV_KEY) or ""
    if env.strip():
        return env
    try:
        with open(SECRET_FILE, "r", encoding="utf-8") as fh:
            return str((json.load(fh) or {}).get("secret") or "")
    except Exception:  # noqa: BLE001
        return ""


def forget() -> bool:
    try:
        os.remove(SECRET_FILE)
        return True
    except OSError:
        return False


def to_clipboard(text: str) -> bool:
    try:
        p = subprocess.Popen(["clip"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-16le"))
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 自检

RFC_VECTORS = [
    # (unix 秒, 期望 8 位码) —— 种子是 ASCII "12345678901234567890"
    (59, "94287082"),
    (1111111109, "07081804"),
    (1111111111, "14050471"),
    (1234567890, "89005924"),
    (2000000000, "69279037"),
    (20000000000, "65353130"),
]


def self_check() -> int:
    seed = base64.b32encode(b"12345678901234567890").decode()     # RFC 6238 的测试种子
    bad = 0
    print("RFC 6238 测试向量（SHA1 / 8 位）：")
    for t, want in RFC_VECTORS:
        got = totp(seed, t=t, digits=8)
        ok = got == want
        bad += 0 if ok else 1
        print("  %s  T=%-12s want=%s got=%s" % ("[OK]" if ok else "[BAD]", t, want, got))
    # GitHub 用的是 6 位：同一时刻取后 6 位应等于 8 位码的后 6 位
    t = 1700000000
    a = totp(seed, t=t, digits=8)
    b = totp(seed, t=t, digits=6)
    ok = b == a[-6:]
    bad += 0 if ok else 1
    print("  %s  6 位 = 8 位的后 6 位（%s / %s）" % ("[OK]" if ok else "[BAD]", b, a[-6:]))
    # base32 归一：带空格 / 短横线 / 小写也要认
    s2 = " ".join([seed[i:i + 4] for i in range(0, len(seed), 4)]).lower()
    ok = totp(seed, t=t, digits=6) == totp(s2, t=t, digits=6)
    bad += 0 if ok else 1
    print("  %s  种子带空格/短横线/小写也能算（GitHub 就是 4 个一组显示）" % ("[OK]" if ok else "[BAD]"))
    print("\n%s  %d 项失败" % ("[PASS]" if bad == 0 else "[FAIL]", bad))
    return 0 if bad == 0 else 1


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="本地 TOTP 验证码（离线，纯标准库）")
    ap.add_argument("--set", action="store_true", help="录入 setup key（隐藏输入）")
    ap.add_argument("--forget", action="store_true", help="删掉本机存的种子")
    ap.add_argument("--watch", action="store_true", help="每秒刷新（盯着填）")
    ap.add_argument("--copy", action="store_true", help="算完放进剪贴板")
    ap.add_argument("--check", action="store_true", help="用 RFC 6238 向量自检")
    ap.add_argument("--digits", type=int, default=DEFAULT_DIGITS)
    args = ap.parse_args()

    if args.check:
        return self_check()

    if args.forget:
        print("已删除本机种子。" if forget() else "本机没有存过种子。")
        return 0

    if args.set:
        print("把 GitHub 那一页的「setup key」粘进来（隐藏输入，不会回显、不会打印）：")
        try:
            s = getpass.getpass("setup key: ")
        except (KeyboardInterrupt, EOFError):
            print("\n取消。")
            return 1
        try:
            save_secret(s)
        except ValueError as exc:
            print("[BAD] %s" % exc)
            return 3
        print("[OK] 已存到 %s（明文，只在本机；runtime/ 不进仓库）" % SECRET_FILE)
        print("     现在算一次，好和 GitHub 页面对一下：%s（剩 %ds）"
              % (totp(s), remaining()))
        return 0

    secret = load_secret()
    if not secret:
        print("没有种子。先 `python tools\\totp.py --set`（或设环境变量 %s）。" % ENV_KEY)
        return 2
    try:
        decode_secret(secret)
    except ValueError as exc:
        print("[BAD] 存着的种子有问题：%s —— 用 --set 重录一次。" % exc)
        return 3

    if args.watch:
        try:
            while True:
                c = totp(secret, digits=args.digits)
                left = remaining()
                sys.stdout.write("\r验证码 %s   剩 %2ds   " % (" ".join(c), left))
                sys.stdout.flush()
                time.sleep(0.5)
        except KeyboardInterrupt:
            print()
        return 0

    code = totp(secret, digits=args.digits)
    print("%s   （剩 %ds）" % (" ".join(code), remaining()))
    if args.copy:
        print("已复制到剪贴板。" if to_clipboard(code) else "复制失败（可以手动选中上面那串）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
