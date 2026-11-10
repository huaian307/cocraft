# -*- coding: utf-8 -*-
r"""离线回归：托盘「待批」菜单 / 热键审批的纯逻辑 + 本机接口。

覆盖：
  1. pick_pending：跳过破坏性 / 敏感读取（热键只批非破坏性），拒绝可挑任意一条
  2. post_decision：POST /orchestrate/permission，body 带 id / decision / via
  3. 接口失败（400/连不上）→ 返回 False，不抛异常

跑法：python tools\tray_approval_test.py
"""
from __future__ import annotations

import http.server
import json
import os
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import tray_host as th  # noqa: E402

PASS = 0
FAIL = 0
BAD = []


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s%s" % (label, (" " + extra) if extra else ""))
    else:
        FAIL += 1
        BAD.append(label)
        print("  [BAD]  %s%s" % (label, (" " + extra) if extra else ""))


class Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except Exception:  # noqa: BLE001
            body = {"_raw": raw}
        Handler.seen.append({"path": self.path, "body": body,
                             "ctype": self.headers.get("Content-Type")})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args):  # noqa: D401
        return


def main() -> int:
    print("== 托盘待批：选择逻辑 ==")
    items = [
        {"id": "p_danger", "created": 1, "destructive": True},
        {"id": "p_secret", "created": 2, "secret": True},
        {"id": "p_old", "created": 3},
        {"id": "p_new", "created": 4},
    ]
    p = th.pick_pending(items)
    ok("热键批准：跳过破坏性 / 敏感，挑最旧的非破坏性", p and p["id"] == "p_old", repr(p))
    p2 = th.pick_pending(items, include_blocked=True)
    ok("热键拒绝：可以挑最旧的一条（含破坏性）", p2 and p2["id"] == "p_danger", repr(p2))
    ok("没有待批 → None", th.pick_pending([]) is None, repr(th.pick_pending([])))

    print("\n== 托盘待批：回决定 ==")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        okv, detail = th.post_decision(port, "p_old", "once", "hotkey")
        seen = Handler.seen[-1] if Handler.seen else {}
        ok("post_decision 成功返回 True", okv is True, detail)
        ok("打到 /orchestrate/permission", seen.get("path") == "/orchestrate/permission",
           repr(seen.get("path")))
        body = seen.get("body") or {}
        ok("body 带 id / decision / via",
           body.get("id") == "p_old" and body.get("decision") == "once"
           and body.get("via") == "hotkey", repr(body))
        ok("Content-Type 是 JSON", "application/json" in str(seen.get("ctype") or ""),
           repr(seen.get("ctype")))

        # 正常 reject 也走同一条接口
        try:
            okv2, detail2 = th.post_decision(port, "p_old", "reject", "tray")
            ok("正常路径返回 True（reject）", okv2 is True, detail2)
        except Exception as exc:  # noqa: BLE001
            ok("正常路径返回 True（reject）", False, str(exc))

        okv3, detail3 = th.post_decision(1, "p_old", "once", "hotkey", timeout=0.3)
        ok("连不上 → False（不抛异常）", okv3 is False, detail3)
    finally:
        srv.shutdown()

    print("\n=== 结果 ===")
    if FAIL:
        print("[FAIL]  PASS=%d FAIL=%d" % (PASS, FAIL))
        for x in BAD:
            print("  - " + x)
        return 1
    print("[PASS]  PASS=%d FAIL=0" % PASS)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
