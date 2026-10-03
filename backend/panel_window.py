# -*- coding: utf-8 -*-
r"""原生面板窗口（pywebview / WebView2）—— 稳定、不闪的窗口方案。

★ 为什么是 pywebview 而不是 PySide6/QtWebEngine：实测在这台机器（NVIDIA + 150% DPI）上
  QtWebEngine 一旦页面重绘就整块闪、甚至黑屏；而 pywebview（WebView2）完全不闪。
  托盘本来要靠 Qt，但 Qt 在**主窗口进程**里会触发闪烁 —— 所以托盘改为**独立进程**
  （`backend/tray_host.py`，pystray 实现），通过命令文件 `runtime/state/_panel_cmd.json`
  通知本窗口执行 toggle / quit。

能力：持久 profile、关窗收托盘（`closing` 拦截）、/bye 告知守护、独立托盘进程。

用法（由 watch.py 拉起）：
    <panel python> backend\panel_window.py --port 8787 [--title "..."]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

CREATE_NO_WINDOW = 0x08000000


def _post(path: str, base: str, timeout: float = 3.0) -> bool:
    try:
        req = urllib.request.Request(base + path, data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=timeout).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--title", default="cocraft · 絵梨衣")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--no-bye", action="store_true")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--auto-close", type=float, default=0, help="测试用：N 秒后真正退出")
    args = ap.parse_args()

    try:
        import webview  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("pywebview 不可用：%s\n" % exc)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ico = os.path.join(root, "frontend", "assets", "cocraft.ico")
    state_dir = os.path.join(root, "runtime", "state")
    os.makedirs(state_dir, exist_ok=True)
    cmd_file = os.path.join(state_dir, "_panel_cmd.json")
    try:
        os.remove(cmd_file)
    except OSError:
        pass
    storage = os.path.join(root, "runtime", "webview2-profile")
    try:
        os.makedirs(storage, exist_ok=True)
    except Exception:  # noqa: BLE001
        storage = None
    base = "http://127.0.0.1:%d" % args.port

    win = webview.create_window(args.title, base + "/", width=args.width, height=args.height,
                                min_size=(900, 600))
    state = {"really_quit": False, "tray": None, "visible": True}

    def _on_closing():
        # 有独立托盘 → 点 X 只收托盘（返回 False 取消关闭）；否则放行（真退出）。
        if state["really_quit"] or state["tray"] is None:
            return True
        try:
            win.hide()
            state["visible"] = False
        except Exception:  # noqa: BLE001
            pass
        return False

    def _on_closed():
        if not args.no_bye:
            _post("/bye", base)

    def _on_loaded():
        try:
            win.set_title(args.title)
        except Exception:  # noqa: BLE001
            pass

    try:
        win.events.closing += _on_closing
        win.events.closed += _on_closed
        win.events.loaded += _on_loaded
    except Exception:  # noqa: BLE001
        pass

    # ---- 独立托盘进程（pystray，跟窗口渲染无关）----
    if not args.no_tray:
        tray_py = os.path.join(root, "backend", "tray_host.py")
        if os.path.isfile(tray_py):
            try:
                state["tray"] = subprocess.Popen(
                    [sys.executable, tray_py, "--title", args.title,
                     "--state", state_dir, "--parent-pid", str(os.getpid())],
                    cwd=root, env=dict(os.environ), creationflags=CREATE_NO_WINDOW)
            except Exception:  # noqa: BLE001
                state["tray"] = None

    # ---- 命令轮询线程：托盘子进程写命令文件，本进程执行 ----
    def _poll_cmd():
        while True:
            try:
                with open(cmd_file, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:  # noqa: BLE001
                time.sleep(0.35)
                continue
            try:
                os.remove(cmd_file)
            except OSError:
                pass
            cmd = str(data.get("cmd") or "")
            try:
                if cmd == "toggle":
                    if state.get("visible", True):
                        win.hide()
                        state["visible"] = False
                    else:
                        win.show()
                        state["visible"] = True
                elif cmd == "quit":
                    state["really_quit"] = True
                    win.destroy()
                    return
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.35)

    threading.Thread(target=_poll_cmd, daemon=True).start()

    def _after_start():
        try:
            win.maximize()
        except Exception:  # noqa: BLE001
            pass
        state["visible"] = True
        if args.auto_close > 0:
            def _later():
                time.sleep(args.auto_close)
                state["really_quit"] = True
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=_later, daemon=True).start()

    webview.start(_after_start, private_mode=False, storage_path=storage)

    # 退出了 → 收拾托盘子进程
    tp = state.get("tray")
    if tp is not None and tp.poll() is None:
        try:
            tp.terminate()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
