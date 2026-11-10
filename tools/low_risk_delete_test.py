# -*- coding: utf-8 -*-
r"""「低风险删除：先隔离再自动放行」接线回归（`ui 0.2.95`）。

覆盖两条真实链路（不打网络、不写真实审计 / 隔离区）：
  * **ACP**：`AcpService._ask_permission` 收到删除类申请 → 隔离 → 直接回 `once`；
  * **OpenCode**：`OpenCodeEngine.auto_ticket` → 隔离 → 替用户回 `once`（上游用桩）；
  * 反向：`taskkill` / 源码文件 → `auto=False`，**不会**替用户放行。

⚠ 测试纪律（见 AGENTS 踩坑 #36）：`_ALWAYS_FILE` / 隔离区 / 审计全部指到临时目录，
   最后断言真实隔离区没被写脏。
"""
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import snapshots as sn                      # noqa: E402
from engines import opencode as oc          # noqa: E402
from engines.acp import service as svc      # noqa: E402

N_PASS = 0
N_FAIL = 0


def check(name, cond, extra=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("PASS  %s" % name)
    else:
        N_FAIL += 1
        print("FAIL  %s%s" % (name, ("  | " + str(extra)) if extra else ""))


def touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def main():
    base = tempfile.mkdtemp(prefix="cocraft-ticket-")
    real_quar = sn.QUARANTINE_DIR
    real_cfg = sn.QUAR_CFG_FILE
    real_always = svc._ALWAYS_FILE
    real_audit = svc.audit.append
    real_upstream = oc._upstream_json
    try:
        root = os.path.join(base, "ws")
        os.makedirs(root)
        touch(os.path.join(root, "node_modules", "pkg", "a.js"))
        touch(os.path.join(root, "src", "app.py"), "print(1)\n")

        sn.QUARANTINE_DIR = os.path.join(base, "quarantine")
        sn.QUAR_CFG_FILE = os.path.join(base, "_quarantine.json")
        sn.set_quarantine_config(days=7, maxStar=1, remindHours=24, enabled=True, cleanup=True)
        os.makedirs(sn.QUARANTINE_DIR, exist_ok=True)
        svc._ALWAYS_FILE = os.path.join(base, "_acp_always.json")
        svc.audit.append = lambda *a, **k: {"hash": "stub"}     # 不写真实审计链（踩坑 #36）

        # ================= ACP =================
        s = svc.AcpService(command=["echo"], agent_id="test-agent",
                           sessions_file=os.path.join(base, "_acp_sessions.json"))
        sid = "ses_ticket"
        with s._lock:
            s._sessions[sid] = {"id": sid, "location": {"directory": root},
                                "messages": [], "permissions": {}}
        q = s.subscribe()
        params = {
            "sessionId": sid,
            "toolCall": {"kind": "execute", "title": "Run command",
                         "rawInput": {"command": "rm -rf node_modules"},
                         "locations": [{"path": os.path.join(root, "node_modules")}]},
            "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                        {"optionId": "reject", "kind": "reject_once", "name": "Reject"}],
        }
        res = s._ask_permission(params)
        check("ACP: 自动回 once", res == {"outcome": {"outcome": "selected", "optionId": "once"}},
              res)
        events = []
        while not q.empty():
            events.append(q.get())
        auto = [e for e in events if e.get("type") == "permission.auto"]
        check("ACP: 发出 permission.auto", bool(auto), events)
        check("ACP: verdict=delete-ticket",
              bool(auto and (auto[0].get("data") or {}).get("verdict") == "delete-ticket"),
              auto[0] if auto else None)
        recs = sn.list_quarantine(limit=10)
        check("ACP: 隔离记录已落盘", len(recs) == 1, recs)
        check("ACP: 记录标了 auto", bool(recs and recs[0].get("auto")), recs)
        check("ACP: 记录星级 ★1", int(recs[0].get("max_star") or 0) == 1, recs[0] if recs else None)
        check("ACP: 没有等待弹窗（waiter 为空）", not s._waiters, s._waiters)

        # ================= OpenCode =================
        calls = []

        def fake_upstream(method, path, body=None, timeout=60.0):
            calls.append((method, path, body))
            if method == "GET" and path.endswith("/permission"):
                return 200, {"data": [{"id": "perm_1", "action": "Delete file",
                                       "resources": [os.path.join(root, "node_modules")],
                                       "metadata": {"command": "rm -rf node_modules"}}]}
            if method == "GET" and "/api/session/" in path:
                return 200, {"location": {"directory": root}}
            if method == "POST" and path.endswith("/reply"):
                return 204, None
            return 404, None

        oc._upstream_json = fake_upstream
        eng = oc.OpenCodeEngine()
        r1 = eng.auto_ticket("ses_oc", "perm_1")
        check("OpenCode: auto=True", bool(r1.get("auto")), r1)
        check("OpenCode: 有隔离 id", bool(r1.get("id")), r1)
        check("OpenCode: 替用户回了 once",
              any(c[0] == "POST" and isinstance(c[2], dict)
                  and c[2].get("decision") == "once" for c in calls),
              calls)
        check("OpenCode: 隔离记录 +1", len(sn.list_quarantine(limit=20)) == 2,
              sn.list_quarantine(limit=20))

        # 反向 1：不是删除类
        calls.clear()

        def fake_kill(method, path, body=None, timeout=60.0):
            calls.append((method, path, body))
            if method == "GET" and path.endswith("/permission"):
                return 200, {"data": [{"id": "perm_2", "action": "Kill process",
                                       "metadata": {"command": "taskkill /F /IM node.exe"}}]}
            if method == "GET" and "/api/session/" in path:
                return 200, {"location": {"directory": root}}
            return 404, None

        oc._upstream_json = fake_kill
        r2 = eng.auto_ticket("ses_oc", "perm_2")
        check("OpenCode: taskkill 不自动", r2.get("auto") is False, r2)
        check("OpenCode: taskkill 没有回 once",
              not any(c[0] == "POST" for c in calls), calls)

        # 反向 2：源码文件
        calls.clear()

        def fake_src(method, path, body=None, timeout=60.0):
            calls.append((method, path, body))
            if method == "GET" and path.endswith("/permission"):
                return 200, {"data": [{"id": "perm_3", "action": "Delete file",
                                       "metadata": {"command": "rm -rf src/app.py"}}]}
            if method == "GET" and "/api/session/" in path:
                return 200, {"location": {"directory": root}}
            return 404, None

        oc._upstream_json = fake_src
        r3 = eng.auto_ticket("ses_oc", "perm_3")
        check("OpenCode: 源码不自动", r3.get("auto") is False, r3)
        check("OpenCode: 源码没有回 once",
              not any(c[0] == "POST" for c in calls), calls)

        # ---------- 真实隔离区没被写脏 ----------
        check("测试没污染真实隔离区",
              not os.path.isdir(os.path.join(real_quar, "panel-ses_ticket")),
              real_quar)
    finally:
        sn.QUARANTINE_DIR = real_quar
        sn.QUAR_CFG_FILE = real_cfg
        svc._ALWAYS_FILE = real_always
        svc.audit.append = real_audit
        oc._upstream_json = real_upstream
        shutil.rmtree(base, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
