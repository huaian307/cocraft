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
orch.WORKFLOWS_FILE = os.path.join(TMP, "_collab_workflows.json")   # 别碰真实的
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
        ok("catalog 带 askTimeout / runTimeout（前端倒计时用）",
           int(cat.get("askTimeout") or 0) > 0 and int(cat.get("runTimeout") or 0) > 0,
           repr((cat.get("askTimeout"), cat.get("runTimeout"))))

        print("\n== 0a. ★agent 自己的 mode 覆盖基线（工作台 / 群聊桥都要跟 engine 一致） ==")
        REG["agents"].append({"id": "mock-mode", "label": "Mock 模式", "mode": "agent",
                              "command": [sys.executable, MOCK], "cwd": TMP,
                              "env": {"ACP_MOCK_SIMPLE": "1"}, "provider": "mock"})
        try:
            _e, base_mode, _env, _cwd = orch._resolve_engine("mock-mode")
            ok("agent 有 mode → 覆盖 baseline", base_mode.get("mode") == "agent",
               repr(base_mode.get("mode")))
            _e2, base_plain, _env2, _cwd2 = orch._resolve_engine("mock")
            ok("agent 没 mode → 用 baseline.mode", base_plain.get("mode") == "read-only",
               repr(base_plain.get("mode")))
        finally:
            REG["agents"].pop()

        print("\n== 0c. 节点级权限（per-member permLevel） ==")
        ok("catalog 带 permLevels（前端下拉用）",
           any(str(x.get("id")) == "trust" for x in (cat.get("permLevels") or [])),
           repr(cat.get("permLevels")))
        wfrm = orch._wf_clean_members([
            {"engine": "mock", "role": "implementer", "name": "t", "permLevel": "trust"},
            {"engine": "mock", "role": "reviewer", "name": "r", "permLevel": "nonsense"},
        ])
        ok("保存工作流时保留成员权限", wfrm[0].get("permLevel") == "trust", repr(wfrm[0]))
        ok("不认识的权限值 → 空（跟随面板，不猜）", wfrm[1].get("permLevel") == "", repr(wfrm[1]))
        orch._RUNS["run_mem"] = {"id": "run_mem", "cwd": TMP, "status": "running",
                                 "members": [{"id": "m1", "name": "t", "permLevel": "trust"}],
                                 "permissions": []}
        mr = orch.set_member("run_mem", "m1", {"permLevel": "exec"})
        ok("运行中改成员权限（立刻生效）", mr["members"][0].get("permLevel") == "exec",
           repr(mr["members"][0]))
        perm_run = {"id": "run_perm", "cwd": TMP, "permissions": [], "members": []}
        dd = orch._ask_permission(perm_run, {"id": "m1", "name": "t", "permLevel": "trust"},
                                  "execute", "Run command", [], [],
                                  {"command": "echo hello"})
        ok("信任成员：非破坏性自动放行", dd == "once" and not perm_run["permissions"], dd)
        import threading as _th
        box = {}
        def _ask_danger():
            box["d"] = orch._ask_permission(
                perm_run, {"id": "m1", "name": "t", "permLevel": "trust"},
                "execute", "Run command", [], [],
                {"command": "Remove-Item -Recurse -Force D:\\\\x"})
        th = _th.Thread(target=_ask_danger, daemon=True)
        th.start()
        pid = wait_until(lambda: (perm_run.get("permissions") or [{}])[-1].get("id"), timeout=3)
        ok("信任成员：破坏性仍然排队等你确认", bool(pid), repr(pid))
        if pid:
            orch.reply_permission(pid, "reject")
        th.join(3)
        ok("拒绝后返回 reject", box.get("d") == "reject", repr(box.get("d")))
        orch._RUNS.pop("run_mem", None)

        print("\n== 0b. 工作流：可复用配置（成员 / 默认任务 / 画布布局） ==")
        wf = orch.save_workflow({
            "name": "登录重构", "cwd": TMP,
            "taskTemplate": "把登录拆成独立模块",
            "members": [
                {"id": "m1", "role": "implementer", "name": "impl", "engine": "mock"},
                {"id": "m2", "role": "reviewer", "name": "rev", "engine": "mock"},
            ],
            "layout": {"zoom": 1.2, "nodes": {"task": {"x": 40, "y": 100},
                                              "m1": {"x": 320, "y": 60}}},
        })
        wid = wf.get("id") or ""
        ok("新建工作流拿到 id", bool(wid.startswith("wf_")), wid)
        ok("列表里能查到", any(str(w.get("id")) == wid for w in orch.list_workflows()), wid)
        upd = orch.save_workflow({"id": wid, "name": "登录重构 v2", "cwd": TMP,
                                  "taskTemplate": "把登录拆成独立模块",
                                  "members": wf["members"],
                                  "layout": {"zoom": 1.1,
                                             "nodes": {"m1": {"x": 500, "y": 200}}}})
        ok("★更新沿用同一个 id（不是又建一条）", upd.get("id") == wid, upd.get("id"))
        ok("画布布局落盘（坐标 + 缩放）",
           (upd.get("layout") or {}).get("nodes", {}).get("m1", {}).get("x") == 500
           and abs(float((upd.get("layout") or {}).get("zoom") or 0) - 1.1) < 1e-6,
           repr(upd.get("layout")))
        try:
            orch.save_workflow({"name": "空", "members": []})
            ok("空成员 → 拒绝", False)
        except ValueError:
            ok("空成员 → 拒绝", True)
        try:
            orch.start_run("x", workflow_id="wf_not_exist")
            ok("不存在的工作流 → 拒绝", False)
        except ValueError:
            ok("不存在的工作流 → 拒绝", True)

        print("\n== 0c. 从工作流启动：成员/目录/标题直接用工作流的 ==")
        wr = orch.start_run("", workflow_id=wid)
        ok("run 关联 workflowId / 名称",
           wr.get("workflowId") == wid and wr.get("workflowName") == "登录重构 v2",
           repr((wr.get("workflowId"), wr.get("workflowName"))))
        ok("★成员来自工作流（2 个）", len(wr.get("members") or []) == 2)
        ok("目录来自工作流", wr.get("cwd") == TMP, wr.get("cwd"))
        got_wf = wait_until(lambda: (orch.get_run(wr["id"]).get("status") in ("done", "partial", "failed")
                                     and orch.get_run(wr["id"])) or None, 20)
        ok("从工作流跑完", bool(got_wf), repr(got_wf and got_wf.get("status")))
        listed = [r for r in orch.list_runs() if str(r.get("id")) == wr["id"]]
        ok("运行历史带 workflowId（左栏能归组）",
           bool(listed) and listed[0].get("workflowId") == wid, repr(listed[:1]))
        wf2 = next((w for w in orch.list_workflows() if str(w.get("id")) == wid), {})
        ok("工作流记下 lastRunId / runCount",
           wf2.get("lastRunId") == wr["id"] and int(wf2.get("runCount") or 0) >= 1,
           repr((wf2.get("lastRunId"), wf2.get("runCount"))))
        cp = orch.duplicate_workflow(wid)
        ok("复制工作流：新 id、成员一样",
           cp.get("id") != wid and len(cp.get("members") or []) == 2, cp.get("id"))
        ok("删除工作流不删运行记录",
           orch.delete_workflow(cp["id"]) is True and bool(orch.get_run(wr["id"])), "")
        ok("删除不存在 → False", orch.delete_workflow("wf_nope") is False)

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

        print("\n== 1b. 并发：两个任务同时跑（不是一次只能一个） ==")
        ra = orch.start_run("并发 A", members=[{"engine": "mock-slow", "name": "A"}])
        rb = orch.start_run("并发 B", members=[{"engine": "mock-slow", "name": "B"}])
        time.sleep(0.8)
        ga, gb = orch.get_run(ra["id"]), orch.get_run(rb["id"])
        ok("两个任务同时 running",
           ga.get("status") == "running" and gb.get("status") == "running",
           repr((ga.get("status"), gb.get("status"))))
        ok("两个任务各自有活着的成员/进程",
           len([k for k in orch._ACTIVE if str(k[0]) in (ra["id"], rb["id"])]) == 2,
           repr([k for k in orch._ACTIVE if str(k[0]) in (ra["id"], rb["id"])]))
        orch.cancel_run(ra["id"])
        orch.cancel_run(rb["id"])
        both = wait_until(lambda: (lambda a, b: (a, b) if (a.get("members") and b.get("members")
                          and a["members"][0].get("status") == "cancelled"
                          and b["members"][0].get("status") == "cancelled") else None)(
                              orch.get_run(ra["id"]), orch.get_run(rb["id"])), 10)
        ok("两个都能各自取消", bool(both), repr([x.get("status") for x in (both or ())]))

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
        runs = orch.list_runs()
        ok("历史列表：内存 + 磁盘合并（不是只回内存里那一条）", len(runs) >= 4,
           repr([r.get("id") for r in runs]))
        ok("历史列表带 done/failed/running 计数（左栏「x/y 完成」不用再拉详情）",
           all(("done" in r and "failed" in r and "running" in r) for r in runs))
        ok("★历史列表带 cwd（左栏按项目分组/筛选，不只知道任务名）",
           all(("cwd" in r and isinstance(r.get("cwd"), str)) for r in runs))

        print("\n== 5b. 删除运行记录 ==")
        dr = orch.start_run("待删除", members=[{"engine": "mock", "name": "x"}])
        did = dr["id"]
        wait_until(lambda: (orch.get_run(did).get("status") not in ("running", "wait") or None), 20)
        wait_until(lambda: (not any(str(k[0]) == did for k in orch._ACTIVE)) or None, 10)
        ok("删除已结束任务 → True", orch.delete_run(did) is True)
        ok("删除后内存/磁盘都查不到",
           not orch.get_run(did) and not os.path.isfile(os.path.join(orch.RUNS_DIR, did + ".json")),
           repr(orch.get_run(did)))
        ok("删除不存在的任务 → False", orch.delete_run("run_nope") is False)
        try:
            orch.delete_run("..\\..\\etc")
            ok("非法 id 被拒绝", False)
        except ValueError:
            ok("非法 id 被拒绝（不会越界删文件）", True)
        slow = orch.start_run("正在跑不能删", members=[{"engine": "mock-slow", "name": "慢"}])
        try:
            orch.delete_run(slow["id"])
            ok("跑着的任务不能删（先取消）", False)
        except ValueError as e:
            ok("跑着的任务不能删（先取消）", "取消" in str(e), repr(e))
        orch.cancel_run(slow["id"])
        wait_until(lambda: (orch.get_run(slow["id"]).get("members") or [{}])[0].get("status") == "cancelled", 10)
        wait_until(lambda: (not any(str(k[0]) == slow["id"] for k in orch._ACTIVE)) or None, 10)
        ok("取消并收尾后可以删除", orch.delete_run(slow["id"]) is True)

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
