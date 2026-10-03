# -*- coding: utf-8 -*-
r"""独立托盘进程（pystray 版）—— 与窗口渲染技术无关，可配 pywebview。

主窗口（pywebview）进程不带任何 Qt；托盘在这里单独跑，菜单动作通过命令文件
`runtime/state/_panel_cmd.json` 通知主窗口执行 `toggle` / `quit`。

用法（由主窗口进程 spawn）：
    <panel python> backend\tray_host.py --title "cocraft · 絵梨衣" [--parent-pid N]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time


def _write_cmd(state_dir: str, cmd: str) -> None:
    path = os.path.join(state_dir, "_panel_cmd.json")
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"cmd": cmd, "t": time.time()}, fh)
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _pid_alive(pid: int) -> bool:
    if not pid:
        return True
    try:
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not h:
            return False
        ctypes.windll.kernel32.CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="cocraft · 絵梨衣")
    ap.add_argument("--state", default="")
    ap.add_argument("--parent-pid", type=int, default=0)
    args = ap.parse_args()

    import pystray                       # noqa: PLC0415
    from PIL import Image                # noqa: PLC0415

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_dir = args.state or os.path.join(root, "runtime", "state")
    os.makedirs(state_dir, exist_ok=True)
    ico = os.path.join(root, "frontend", "assets", "cocraft.ico")

    if os.path.isfile(ico):
        image = Image.open(ico)
    else:
        image = Image.new("RGBA", (64, 64), (200, 50, 60, 255))

    def on_toggle(icon, item):
        _write_cmd(state_dir, "toggle")

    def on_quit(icon, item):
        _write_cmd(state_dir, "quit")
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass

    menu = pystray.Menu(
        pystray.MenuItem("显示 / 隐藏面板", on_toggle, default=True),
        pystray.MenuItem("退出", on_quit),
    )
    icon = pystray.Icon("cocraft", image, args.title, menu)

    if args.parent_pid:
        def _watch():
            while True:
                if not _pid_alive(args.parent_pid):
                    try:
                        icon.stop()
                    except Exception:  # noqa: BLE001
                        pass
                    return
                time.sleep(2)
        threading.Thread(target=_watch, daemon=True).start()

    icon.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
