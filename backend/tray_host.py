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
import urllib.request


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


def pick_pending(items, include_blocked: bool = False):
    """挑最旧的一条待批（热键用）。

    `include_blocked=False` 时跳过破坏性 / 敏感读取 —— 那两类**不允许热键批准**。
    """
    xs = [x for x in (items or []) if isinstance(x, dict) and x.get("id")]
    if not include_blocked:
        xs = [x for x in xs if not x.get("destructive") and not x.get("secret")]
    return min(xs, key=lambda x: x.get("created") or 0, default=None)


def post_decision(port: int, perm_id, decision, via: str = "tray", timeout: float = 6.0):
    """回一条权限决定到本机面板（**显式绕过系统代理**）。返回 (ok, detail)。"""
    body = json.dumps({"id": str(perm_id or ""), "decision": str(decision or ""),
                       "via": str(via or "tray")}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/orchestrate/permission" % int(port),
        data=body, method="POST", headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        return True, raw
    except Exception as exc:  # noqa: BLE001
        return False, "%s: %s" % (type(exc).__name__, exc)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="cocraft")
    ap.add_argument("--state", default="")
    ap.add_argument("--parent-pid", type=int, default=0)
    ap.add_argument("--port", type=int, default=8787)
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

    def _log(msg: str) -> None:
        try:
            p = os.path.join(root, "runtime", "logs", "tray.log")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:  # noqa: BLE001
            pass

    def on_toggle(icon, item):
        _write_cmd(state_dir, "toggle")

    def on_quit(icon, item):
        _write_cmd(state_dir, "quit")
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass

    approvals_file = os.path.join(state_dir, "_approvals.json")

    def _items() -> list:
        d = _read_json(approvals_file) or {}
        return [x for x in (d.get("items") or []) if isinstance(x, dict)]

    def _short(x: dict) -> str:
        name = str(x.get("memberName") or x.get("member") or "")
        title = " ".join(str(x.get("title") or x.get("kind") or "").split())[:40]
        out = (name + " · " + title).strip(" ·")
        return out or str(x.get("id") or "")[:12]

    def _toast(title: str, body: str = "") -> None:
        try:
            icon.notify(body or title, title)
        except Exception:  # noqa: BLE001
            pass

    def _post(perm_id, decision, via):
        """从托盘/热键回一条权限决定（本机接口；**显式绕过系统代理**）。"""
        ok, detail = post_decision(args.port, perm_id, decision, via)
        if ok:
            _log("decision %s %s via=%s -> %s" % (perm_id, decision, via, detail[:120]))
            return True
        _log("decision %s %s via=%s FAILED: %s" % (perm_id, decision, via, detail))
        _toast("批准请求失败", detail)
        return False

    def _mk_cb(perm_id, decision, label):
        def _cb(icon_, item_):
            if _post(perm_id, decision, "tray"):
                _toast(("已允许：" if decision != "reject" else "已拒绝：") + label)
            _refresh_menu()
        return _cb

    def _open_workbench(icon_, item_):
        _write_cmd(state_dir, "show")

    def _nop(icon_, item_):
        return None

    def _refresh_menu(items=None):
        items = _items() if items is None else items
        sub = []
        for x in items[:6]:
            label = _short(x)
            blocked = bool(x.get("destructive") or x.get("secret"))
            if blocked:
                sub.append(pystray.MenuItem("⚠ " + label + "（需打开工作台）", _open_workbench))
            else:
                sub.append(pystray.MenuItem("批准 · " + label,
                                            _mk_cb(x.get("id"), "once", label)))
            sub.append(pystray.MenuItem("拒绝 · " + label,
                                        _mk_cb(x.get("id"), "reject", label)))
        if len(items) > 6:
            sub.append(pystray.MenuItem("… 还有 %d 条，打开工作台" % (len(items) - 6),
                                        _open_workbench))
        sub.append(pystray.MenuItem("打开工作台", _open_workbench))
        menu = pystray.Menu(
            pystray.MenuItem("显示 / 隐藏面板", on_toggle, default=True),
            pystray.MenuItem(("待批 %d 条" % len(items)) if items else "待批 0 条",
                             pystray.Menu(*sub), enabled=bool(items)),
            pystray.MenuItem("热键：Ctrl+Alt+Y 批准（非破坏性）/ Ctrl+Alt+N 拒绝",
                             _nop, enabled=False),
            pystray.MenuItem("退出", on_quit),
        )
        try:
            icon.menu = menu
            icon.update_menu()
        except Exception:  # noqa: BLE001
            pass

    icon = pystray.Icon(
        "cocraft", image, args.title,
        pystray.Menu(pystray.MenuItem("显示 / 隐藏面板", on_toggle, default=True),
                     pystray.MenuItem("退出", on_quit)))

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
    appr_seen = {"sig": ""}

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
            # ★待批快照（orchestrator 写）→ 托盘菜单/热键
            items = _items()
            sig = "|".join(str(x.get("id")) for x in items)
            if sig != appr_seen["sig"]:
                appr_seen["sig"] = sig
                _log("approvals pending=%d" % len(items))
                _refresh_menu(items)
            time.sleep(0.8)

    def _hotkey_loop():
        """Ctrl+Alt+Y 批准最旧的**非破坏性/非敏感**申请；Ctrl+Alt+N 拒绝最旧的一条。"""
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x0001, 0x0002, 0x4000
            WM_HOTKEY = 0x0312
            ok_y = user32.RegisterHotKey(None, 0xA1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("Y"))
            ok_n = user32.RegisterHotKey(None, 0xA2, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("N"))
            _log("hotkey register Y=%s N=%s" % (bool(ok_y), bool(ok_n)))
            if not (ok_y or ok_n):
                _toast("热键注册失败", "Ctrl+Alt+Y/N 被别的程序占用；请用托盘菜单批准")
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message != WM_HOTKEY:
                    continue
                items = _items()
                if msg.wParam == 0xA1:
                    target = pick_pending(items, include_blocked=False)
                    if target:
                        _post(target.get("id"), "once", "hotkey")
                        _refresh_menu()
                    else:
                        _toast("没有可用热键批准的申请",
                               "破坏性 / 敏感读取请打开工作台确认")
                elif msg.wParam == 0xA2:
                    target = pick_pending(items, include_blocked=True)
                    if target:
                        _post(target.get("id"), "reject", "hotkey")
                        _refresh_menu()
        except Exception as exc:  # noqa: BLE001
            _log("hotkey loop failed: %s" % exc)

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
        _refresh_menu()
        threading.Thread(target=_watch_notify, daemon=True).start()
        threading.Thread(target=_hotkey_loop, daemon=True).start()
        if args.parent_pid:
            threading.Thread(target=_watch_parent, daemon=True).start()

    icon.run(_setup)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
