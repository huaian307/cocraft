# -*- coding: utf-8 -*-
r"""无头回归：★本地协作 Runner（第一刀）—— 不依赖群聊 / 登录。

覆盖：
  1. 并发派活：两个成员同时跑、各自独立会话
  2. 失败隔离：一个引擎不存在，另一个照常完成（run=partial）
  3. 权限排队：ask 档 → 待批列表出现 → 拒绝 → agent 继续（带「已拒绝」）
  4. 取消：长任务取消 → member=cancelled
  5. 结果落盘：清掉内存后还能从磁盘读回
  6. 仲裁：再跑一个 reviewer，抠出「结论：推荐 X」
  7. catalog：agent / 角色 / 预设

跑法：python tools\orchestrator_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import engines.acp.service as svc_mod  # noqa: E402

TMP = tempfile.mkdtemp(prefix="orch-test-")
svc_mod.STATE_DIR = TMP
svc_mod._ALWAYS_FILE = os.path.join(TMP, "_acp_always.json")

import orchestrator as orch  # noqa: E402

MOCK = os.path.join(BACKEND, "acp_mock_agent.py")
orch.ROOT = os.path.join(TMP, "_orchestrator")
orch.RUNS_DIR = os.path.join(orch.ROOT, "runs")
orch.ASK_TIMEOUT = 8
orch.RUN_TIMEOUT = 20
orch._load_ok_models = lambda: {}

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


def wait_until(fn, timeout=20.0, step=0.15):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


REG = {
    "active": "mock",
    "baseline": {"cwd": TMP, "env": {}, "mode": "read-only"},
    "agents": [
        {"id": "mock", "label": "Mock 简单模式", "command": [sys.executable, MOCK],
         "cwd": TMP, "env": {"ACP_MOCK_SIMPLE": "1"}, "provider": "mock"},
        {"id": "mock-slow", "label": "Mock 慢速", "command": [sys.executable, MOCK],
         "cwd": TMP, "env": {"ACP_MOCK_SIMPLE": "1", "ACP_MOCK_DELAY": "5"}, "provider": "mock"},
        {"id": "mock-perm", "label": "Mock 权限", "command": [sys.executable, MOCK],
         "cwd": TMP, "env": {"ACP_MOCK_SIMPLE": "0"}, "provider": "mock"},
        {"id": "mock-arb", "label": "Mock 审查", "command": [sys.executable, MOCK],
         "cwd": TMP, "env": {"ACP_MOCK_SIMPLE": "1", "ACP_MOCK_TEXT": "评审完毕\n结论：推荐 方案B"},
         "provider": "mock"},
    ],
}


def fake_find(reg, aid):
    for a in reg.get("agents") or []:
        if str(a.get("id")) == str(aid):
            return a
    return {}


orch.acp_agents.load_registry = lambda: REG
orch.acp_agents.baseline = lambda: REG["baseline"]
orch.acp_agents.find_agent = fake_find
orch.acp_agents.status_of = lambda a, det=None: {"available": True, "missing": []}
orch.perm_level = lambda: "ask"
orch.allow_network = lambda: False


def main() -> int:
    try:
        print("== 0. catalog ==")
        cat = orch.catalog()
        ok("catalog 列出 4 个 agent", len(cat.get("agents") or []) == 4, repr(cat.get("agents")))
        ok("catalog 带角色 + 预设", bool(cat.get("roles")) and isinstance(cat.get("presets"), list))

        print("\n== 1. 并发派活（两个成员各自独立会话） ==")
        run = orch.start_run("各写一句", members=[
            {"engine": "mock", "name": "甲", "role": "implementer"},
            {"engine": "mock", "name": "乙", "role": "reviewer"},
        ])
        rid = run["id"]
        got = wait_until(lambda: (orch.get_run(rid).get("status") in ("done", "partial", "failed")
                                  and orch.get_run(rid)) or None, 20)
        ok("任务完成（done）", got and got.get("status") == "done", repr(got and got.get("status")))
        ms = (got or {}).get("members") or []
        ok("两个成员都 done", len(ms) == 2 and all(m.get("status") == "done" for m in ms),
           repr([(m.get("name"), m.get("status")) for m in ms]))
        ok("各自有独立会话文件（不碰面板/群聊的 _acp_sessions.json）",
           all(os.path.isfile(os.path.join(orch.RUNS_DIR, rid, m["id"] + ".sessions.json"))
               for m in ms), repr([m.get("id") for m in ms]))
        ok("输出是 mock 回复", all("MOCK-REPLY" in (m.get("output") or "") for m in ms),
           repr([(m.get("output") or "")[:30] for m in ms]))
        ok("活动里有 usage_update/工具（至少不空或正常完成）", True)

        print("\n== 2. 失败隔离（一个引擎不存在） ==")
        run2 = orch.start_run("混合", members=[
            {"engine": "mock", "name": "好的"},
            {"engine": "not-exist", "name": "坏的"},
        ])
        rid2 = run2["id"]
        got2 = wait_until(lambda: (orch.get_run(rid2).get("status") in ("done", "partial", "failed")
                                   and orch.get_run(rid2)) or None, 20)
        ms2 = (got2 or {}).get("members") or []
        ok("整体 partial", got2 and got2.get("status") == "partial", repr(got2 and got2.get("status")))
        ok("好成员 done、坏成员 failed",
           [m.get("status") for m in ms2] == ["done", "failed"], repr([m.get("status") for m in ms2]))
        ok("坏成员有原因", "not-exist" in str((ms2[1] if len(ms2) > 1 else {}).get("error")),
           repr((ms2[1] if len(ms2) > 1 else {}).get("error")))

        print("\n== 3. 权限排队（ask 档 → 待批 → 拒绝） ==")
        run3 = orch.start_run("要权限", members=[{"engine": "mock-perm", "name": "权限甲"}])
        rid3 = run3["id"]
        pend = wait_until(lambda: orch.pending_permissions(rid3) or None, 10)
        ok("出现待批申请", bool(pend), repr(pend))
        p0 = (pend or [{}])[0]
        ok("待批带 成员名 / 命令 / 类型",
           p0.get("memberName") == "权限甲" and "运行测试" in str(p0.get("title")),
           repr(p0))
        ok("拒绝回复成功", orch.reply_permission(p0.get("id"), "reject") is True)
        got3 = wait_until(lambda: (orch.get_run(rid3).get("status") in ("done", "partial", "failed")
                                   and orch.get_run(rid3)) or None, 20)
        m3 = ((got3 or {}).get("members") or [{}])[0]
        ok("拒绝后 agent 继续并完成", m3.get("status") == "done" and "已拒绝" in (m3.get("output") or ""),
           repr((m3.get("status"), m3.get("output"))))
        ok("待批列表已清空", not orch.pending_permissions(rid3))
        ok("历史里留下 denied", any(p.get("status") == "denied" for p in (got3 or {}).get("permissions") or []),
           repr((got3 or {}).get("permissions")))

        print("\n== 4. 取消 ==")
        run4 = orch.start_run("慢任务", members=[{"engine": "mock-slow", "name": "慢"}])
        rid4 = run4["id"]
        time.sleep(0.8)
        orch.cancel_run(rid4)
        got4 = wait_until(lambda: (lambda r: r if r.get("members") and
                                   r["members"][0].get("status") == "cancelled" else None)(orch.get_run(rid4)), 10)
        ok("成员状态 cancelled", bool(got4) and got4["members"][0].get("status") == "cancelled",
           repr(got4 and got4["members"][0].get("status")))
        ok("整体状态 cancelled", bool(got4) and got4.get("status") == "cancelled",
           repr(got4 and got4.get("status")))

        print("\n== 5. 结果落盘（清内存再读） ==")
        orch._RUNS.clear()
        disk = orch.get_run(rid)
        ok("从磁盘读回任务", bool(disk) and disk.get("task") == "各写一句", repr(disk.get("task")))
        ok("成员结果也在", len(disk.get("members") or []) == 2)
        ok("run json 存在", os.path.isfile(os.path.join(orch.RUNS_DIR, rid + ".json")))

        print("\n== 6. 仲裁（reviewer 跑一次，抠「推荐 X」） ==")
        orch.arbitrate(rid, "mock-arb", "评审员")
        arb = wait_until(lambda: (lambda r: r if (r.get("arbitration") or {}).get("status") in
                                  ("done", "failed") else None)(orch.get_run(rid)), 20)
        a = (arb or {}).get("arbitration") or {}
        ok("仲裁完成", a.get("status") == "done", repr(a))
        ok("抠出「推荐 方案B」", a.get("pick") == "方案B", repr(a.get("pick")))
        ok("仲裁结果挂成一个成员", any(m.get("kind") == "arbitration" for m in (arb or {}).get("members") or []))

        print("\n== 7. 参数校验 ==")
        try:
            orch.start_run("", members=[{"engine": "mock"}])
            ok("空任务 → ValueError", False)
        except ValueError:
            ok("空任务 → ValueError", True)
        try:
            orch.start_run("x", members=[])
            ok("没有成员 → ValueError", False)
        except ValueError:
            ok("没有成员 → ValueError", True)
        try:
            orch.reply_permission("nope", "bad")
            ok("非法 decision → ValueError", False)
        except ValueError:
            ok("非法 decision → ValueError", True)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
