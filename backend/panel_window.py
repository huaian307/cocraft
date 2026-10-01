# -*- coding: utf-8 -*-
r"""原生面板窗口（WebView2 / pywebview）——把面板从"Edge --app 浏览器窗口"换成真正的软件窗口。

为什么要它：Edge `--app` 本质还是浏览器（任务栏归 Edge、标题栏/Alt+Tab 都带 Edge）。
WebView2 窗口是**原生 WinForms 窗口**，任务栏独立、没有浏览器边框，像正常软件。

⚠ 这是一个**可选组件**（pywebview + pythonnet + WebView2 运行时）：
  - 开发机：`runtime\venvs\panel\Scripts\pythonw.exe`
  - 安装包：`runtime\site-packages\panel`（用自带解释器 + PYTHONPATH）
  没装时 `backend/watch.py` 会**自动退回 Edge --app**（老行为不变）。

用法（由 watch.py 拉起）：
    <panel python> backend\panel_window.py --port 8787 [--title "..."]
"""
from __future__ import annotations

import argparse
import os
import sys
import urllib.request


def _post(path: str, base: str, timeout: float = 3.0) -> bool:
    """给面板服务发一个空 POST（关窗时用来发 /bye）。失败无所谓。"""
    try:
        req = urllib.request.Request(base + path, data=b"{}",
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=timeout).close()
        return True
    except Exception:  # noqa: BLE001
        return False


def _set_window_icon(win, ico: str) -> None:
    """把窗口/任务栏图标换成我们自己的（默认会是 python 的图标，看起来像"随便一个进程"）。"""
    try:
        if not os.path.isfile(ico):
            return
        import clr  # noqa: PLC0415, F401
        from System.Drawing import Icon  # noqa: PLC0415
        form = getattr(win, "native", None)
        if form is not None:
            form.Icon = Icon(ico)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--title", default="cocraft · 絵梨衣")
    ap.add_argument("--width", type=int, default=1440)
    ap.add_argument("--height", type=int, default=900)
    ap.add_argument("--no-bye", action="store_true", help="关窗时不发 /bye（调试用）")
    ap.add_argument("--auto-close", type=float, default=0, help="测试用：N 秒后自动关窗")
    args = ap.parse_args()

    try:
        import webview  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("pywebview 不可用：%s\n" % exc)
        return 2

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ico = os.path.join(root, "frontend", "assets", "cocraft.ico")
    # ⚠ pywebview 的 WebView2 默认是 `private_mode=True` → 用**临时**用户数据目录 →
    #   localStorage 不持久：每次开都当"首次"，首次设置向导反复弹、主题/默认模型等设置也一起丢。
    #   这里固定一个**持久**目录（与 Edge 的 browser-profile 分开）。
    storage = os.path.join(root, "runtime", "webview2-profile")
    try:
        os.makedirs(storage, exist_ok=True)
    except Exception:  # noqa: BLE001
        storage = None
    base = "http://127.0.0.1:%d" % args.port
    win = webview.create_window(args.title, base + "/", width=args.width, height=args.height,
                                min_size=(900, 600))

    def _on_closed():
        # 页面自己的 pagehide 不一定在 webview 关闭时触发 → 这里显式告诉守护"面板关了"
        if not args.no_bye:
            _post("/bye", base)

    def _on_loaded():
        # 页面 <title> 会盖掉 create_window 的标题 → 加载完再写一次；顺手换上我们的图标
        try:
            win.title = args.title
        except Exception:  # noqa: BLE001
            pass
        _set_window_icon(win, ico)

    try:
        win.events.closed += _on_closed
        win.events.loaded += _on_loaded
    except Exception:  # noqa: BLE001
        pass

    def _after_start():
        try:
            win.maximize()          # 起手最大化，像正常软件
        except Exception:  # noqa: BLE001
            pass
        _set_window_icon(win, ico)
        if args.auto_close > 0:     # 测试用：到点自己关，好验证 /bye 与关窗流程
            import threading
            import time as _t
            def _later():
                _t.sleep(args.auto_close)
                try:
                    win.destroy()
                except Exception:  # noqa: BLE001
                    pass
            threading.Thread(target=_later, daemon=True).start()

    webview.start(_after_start, private_mode=False, storage_path=storage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
