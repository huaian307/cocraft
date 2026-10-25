# -*- coding: utf-8 -*-
r"""独立托盘进程（pystray 版）—— 与窗口渲染技术无关，可配 pywebview。

主窗口（pywebview）进程不带任何 Qt；托盘在这里单独跑，菜单动作通过命令文件
`runtime/state/_panel_cmd.json` 通知主窗口执行 `toggle` / `quit`。

用法（由主窗口进程 spawn）：
    <panel python> backend\tray_host.py --title "cocraft · 守烛人" [--parent-pid N]
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


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="cocraft")
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

    # ★B3：面板写 `_notify.json`（POST /panel/notify）→ 这里弹系统气泡 + 让窗口闪任务栏。
    # 靠 `ts` 变大判定"是新的一条"；启动时先记下已有 ts，避免把上次遗留的又弹一遍。
    notify_file = os.path.join(state_dir, "_notify.json")
    seen = {"ts": 0.0}
    _cur = _read_json(notify_file)
    if isinstance(_cur, dict):
        try:
            seen["ts"] = float(_cur.get("ts") or 0)
        except Exception:  # noqa: BLE001
            seen["ts"] = 0.0

    def _watch_notify():
        # ★托盘提示跟着面板名字走：面板改名时前端 POST /panel/title 写 `_panel_title.json`，
        #   这里每 0.8s 顺手读一次（文件很小；变了才改 tooltip，不闪）。
        title_file = os.path.join(state_dir, "_panel_title.json")
        cur_title = {"t": args.title}
        while True:
            t = _read_json(title_file)
            if isinstance(t, dict):
                want = " ".join(str(t.get("title") or "").split())[:60]
                # `cocraft · cocraft` → `cocraft`（两边同名时别重复）
                _p = [x.strip() for x in want.split("·")]
                if len(_p) > 1 and len({x.lower() for x in _p if x}) == 1:
                    want = _p[0]
                if want and want != cur_title["t"]:
                    cur_title["t"] = want
                    try:
                        icon.title = want
                    except Exception:  # noqa: BLE001
                        pass
            data = _read_json(notify_file)
            if isinstance(data, dict):
                try:
                    ts = float(data.get("ts") or 0)
                except Exception:  # noqa: BLE001
                    ts = 0.0
                if ts > seen["ts"]:
                    seen["ts"] = ts
                    title = str(data.get("title") or args.title)
                    body = str(data.get("body") or "")
                    try:
                        icon.notify(body or title, title)
                    except Exception:  # noqa: BLE001
                        pass
                    if data.get("flash", True):
                        _write_cmd(state_dir, "flash")     # 让窗口进程闪任务栏
            time.sleep(0.8)

    def _watch_parent():
        while True:
            if not _pid_alive(args.parent_pid):
                try:
                    icon.stop()
                except Exception:  # noqa: BLE001
                    pass
                return
            time.sleep(2)

    def _setup(_icon):
        # ⚠ pystray 的规矩：**一旦传了自定义 setup，它就不会再帮你显示图标** ——
        # 必须在这里自己 `visible = True`。见 pystray/_base.py 的 _start_setup：
        #     if setup: setup(self)          # 自定义：什么都不做
        #     else:     self.visible = True  # 默认：才 NIM_ADD
        # （B3 把 _watch_notify 挪进 _setup 时漏了这一句 → 托盘图标从 ui 0.1.68 起一直没显示。）
        _icon.visible = True
        # 气泡要等图标就绪（icon.run 起来之后）才发得出去，所以轮询放这里启动。
        threading.Thread(target=_watch_notify, daemon=True).start()
        if args.parent_pid:
            threading.Thread(target=_watch_parent, daemon=True).start()

    icon.run(_setup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
