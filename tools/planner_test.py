#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""planner P0 离线回归（不联网、不起 server）。

覆盖：plan 校验、会话/计划存取、报告生成、CLI 参数解析（基础）。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "tools"))


def _load_backend_planner():
    import importlib.util
    p = os.path.join(ROOT, "backend", "planner.py")
    spec = importlib.util.spec_from_file_location("planner_backend", p)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)  # type: ignore
    return mod


def _use_temp_state():
    planner = _load_backend_planner()
    td = tempfile.mkdtemp(prefix="planner_test_")
    planner.STATE_DIR = os.path.join(td, "state")
    planner.PLANNER_DIR = os.path.join(planner.STATE_DIR, "_planner")
    planner.SESSIONS_FILE = os.path.join(planner.STATE_DIR, "_planner_sessions.json")
    os.makedirs(planner.STATE_DIR, exist_ok=True)
    return td, planner


def test_validate_and_session():
    td, planner = _use_temp_state()
    cwd = os.path.join(td, "proj")
    os.makedirs(cwd, exist_ok=True)
    sess = planner.create_session(title="T", cwd=cwd, goal="G")
    assert sess["id"].startswith("plan_sess_")
    assert sess["projectKey"]
    got = planner.get_session(sess["id"])
    assert got and got["title"] == "T" and "token" not in got

    plan = planner.load_plan(sess["id"])
    assert plan and plan["schema"] == "cocraft.plan/v1"
    vr = planner.validate_plan(plan, sess)
    assert vr["ok"] is True
    # missing engine => can save but cannot run
    assert vr["canRun"] is False

    # fill engine
    plan["stages"][0]["members"][0]["engine"] = "opencode-acp"
    vr = planner.validate_plan(plan, sess)
    assert vr["ok"] is True and vr["canRun"] is True

    # session cwd is immutable: even if plan says empty cwd, validation uses session cwd
    bad = json.loads(json.dumps(plan))
    bad["cwd"] = ""
    vr = planner.validate_plan(bad, sess)
    assert vr["ok"] is True
    assert vr["plan"]["cwd"] == sess["cwd"]

    # but without session context, empty cwd is an error
    vr2 = planner.validate_plan({"schema": "cocraft.plan/v1", "cwd": "", "stages": [{"id": "s1", "members": [{"id": "m1", "engine": "x", "role": "implementer"}]}]})
    assert vr2["ok"] is False
    return True


def test_set_plan_version_and_confirm():
    td, planner = _use_temp_state()
    cwd = os.path.join(td, "proj")
    os.makedirs(cwd, exist_ok=True)
    sess = planner.create_session(title="T", cwd=cwd)
    plan = planner.load_plan(sess["id"])
    plan["goal"] = "v2"
    res = planner.set_plan(sess["id"], plan)
    assert res["plan"]["version"] >= 2
    # missing engine: not runnable => cannot confirm (P0: 校验/就绪失败不能进入执行)
    try:
        planner.set_plan(sess["id"], res["plan"], status="confirmed")
        assert False, "should have failed"
    except ValueError as e:
        assert "不能确认" in str(e) or "engine" in str(e).lower() or "agentId" in str(e)
    res["plan"]["stages"][0]["members"][0]["engine"] = "opencode-acp"
    res["plan"]["version"] = planner.load_plan(sess["id"])["version"]
    res2 = planner.set_plan(sess["id"], res["plan"], status="confirmed")
    assert res2["plan"]["status"] == "confirmed"
    return True


def test_report_shape():
    td, planner = _use_temp_state()
    cwd = os.path.join(td, "proj")
    os.makedirs(cwd, exist_ok=True)
    sess = planner.create_session(title="T", cwd=cwd, goal="G")
    rep = planner.report(sess["id"])
    assert rep["sessionId"] == sess["id"]
    assert "markdown" in rep and "完成报告" in rep["markdown"]
    assert "summary" in rep
    return True


def test_handoff_and_switch():
    td, planner = _use_temp_state()
    cwd = os.path.join(td, "proj")
    os.makedirs(cwd, exist_ok=True)
    sess = planner.create_session(title="T", cwd=cwd, goal="实现 P0.5", planner_agent="agent-a")
    plan = planner.load_plan(sess["id"])
    plan["stages"][0]["members"][0]["engine"] = "opencode-acp"
    planner.set_plan(sess["id"], plan, status="confirmed")

    h = planner.write_handoff(sess["id"], reason="manual", note="note1")
    assert h["ok"] and os.path.isfile(h["path"])
    got = planner.load_handoff(sess["id"])
    assert got["exists"] and "HANDOFF" in got["markdown"] and "实现 P0.5" in got["markdown"]
    ctx = planner.handoff_context(sess["id"])
    assert ctx["ok"] and "plan.json" in ctx["text"] and "HANDOFF" in ctx["text"]

    sw = planner.switch_planner_agent(sess["id"], "agent-b", note="switch")
    assert sw["ok"] and sw["fromAgent"] == "agent-a" and sw["toAgent"] == "agent-b"
    assert sw["handoff"]["written"] and "review" in sw and "context" in sw
    sess2 = planner.get_session(sess["id"])
    assert sess2["plannerAgent"] == "agent-b"
    return True


def test_auto_handoff_and_recover():
    td, planner = _use_temp_state()
    cwd = os.path.join(td, "proj")
    os.makedirs(cwd, exist_ok=True)
    sess = planner.create_session(title="T", cwd=cwd, goal="G")
    sid = sess["id"]
    r = planner.maybe_auto_handoff(sid, used=79, size=100)
    assert r["ok"] and not r["needed"] and not r["written"]
    r = planner.maybe_auto_handoff(sid, used=81, size=100)
    assert r["ok"] and r["needed"] and r["written"] and os.path.isfile(r["handoff"]["path"])
    r2 = planner.maybe_auto_handoff(sid, used=90, size=100)
    assert r2["ok"] and r2["needed"] and not r2["written"] and r2.get("deduped")

    # recover: simulate orphan active run (orchestrator unavailable => treated as missing/interrupted)
    planner._touch_session(sid, status="running", activeRunId="run_missing_123")
    plan = planner.load_plan(sid)
    plan["status"] = "running"
    planner.save_plan(sid, plan)
    rec = planner.recover(sid)
    assert rec["ok"] and rec["count"] >= 1
    s2 = planner.get_session(sid)
    assert s2["activeRunId"] == ""
    assert s2["status"] in ("failed", "done", "canceled")
    h = planner.load_handoff(sid)
    assert h["exists"] and ("crash-recovery" in h["markdown"] or "中断" in h["markdown"] or "HANDOFF" in h["markdown"])

    # bind + find
    b = planner.bind_chat_session(sid, "chat_abc", engine="acp")
    assert b["ok"] and b["session"]["chatSessionId"] == "chat_abc"
    f = planner.find_by_chat_session("chat_abc")
    assert f and f["id"] == sid
    return True


def test_cli_parser():
    import runpy
    cli = runpy.run_path(os.path.join(ROOT, "tools", "planner.py"))
    ap = cli["build_parser"]()
    args = ap.parse_args(["session", "list", "--limit", "5"])
    assert args.cmd == "session"
    args = ap.parse_args(["plan", "get", "--id", "x"])
    assert args.cmd == "plan"
    args = ap.parse_args(["run", "start", "--id", "x"])
    assert args.cmd == "run"
    args = ap.parse_args(["report", "--id", "x"])
    assert args.cmd == "report"
    args = ap.parse_args(["handoff", "write", "--id", "x", "--note", "n"])
    assert args.cmd == "handoff"
    args = ap.parse_args(["handoff", "show", "--id", "x", "--context"])
    assert args.cmd == "handoff" and args.context is True
    args = ap.parse_args(["agent", "switch", "--id", "x", "--to", "agent-b"])
    assert args.cmd == "agent" and args.to == "agent-b"
    args = ap.parse_args(["handoff", "auto", "--id", "x", "--used", "81", "--size", "100"])
    assert args.cmd == "handoff" and args.used == 81.0
    args = ap.parse_args(["recover", "--id", "x"])
    assert args.cmd == "recover"
    args = ap.parse_args(["bind", "--id", "x", "--chat", "chat1"])
    assert args.cmd == "bind" and args.chat == "chat1"
    args = ap.parse_args(["resume", "--id", "x"])
    assert args.cmd == "resume"
    return True


def main():
    tests = [test_validate_and_session, test_set_plan_version_and_confirm, test_report_shape, test_handoff_and_switch, test_auto_handoff_and_recover, test_cli_parser]
    ok = 0
    for t in tests:
        try:
            t(); ok += 1; print("[OK]", t.__name__)
        except Exception as e:
            import traceback
            print("[FAIL]", t.__name__, e)
            traceback.print_exc()
    print(f"{ok}/{len(tests)} passed")
    raise SystemExit(0 if ok == len(tests) else 1)


if __name__ == "__main__":
    main()
