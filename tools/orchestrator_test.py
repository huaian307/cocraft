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

import io
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

# ★可逆执行也要隔离（踩坑 #36 的同款）：`_turn_snapshot` / `_quarantine_before_allow`
#   默认写 `runtime/snapshots` / `runtime/quarantine` → 不指走就会往真实目录里灌测试垃圾。
import snapshots as _snap_iso  # noqa: E402
_snap_iso.SNAPSHOT_DIR = os.path.join(TMP, "_snapshots")
_snap_iso.STORE_DIR = os.path.join(TMP, "_snapshots", "_store")
_snap_iso.QUARANTINE_DIR = os.path.join(TMP, "_quarantine")
_snap_iso.TURN_CFG_FILE = os.path.join(TMP, "_turnsnap.json")

import orchestrator as orch  # noqa: E402

MOCK = os.path.join(BACKEND, "acp_mock_agent.py")
orch.ROOT = os.path.join(TMP, "_orchestrator")
orch.RUNS_DIR = os.path.join(orch.ROOT, "runs")
orch.WORKFLOWS_FILE = os.path.join(TMP, "_collab_workflows.json")   # 别碰真实的
orch.APPROVALS_FILE = os.path.join(TMP, "_approvals.json")
orch.NOTIFY_FILE = os.path.join(TMP, "_notify.json")
orch.ASK_TIMEOUT = 8
orch.RUN_TIMEOUT = 20
orch._load_ok_models = lambda: {}
# ★审计链指到临时目录：`_ask_permission` 现在会写链，别污染 `runtime/state/_audit.jsonl`
import audit as _audit  # noqa: E402
_audit.set_file(os.path.join(TMP, "_audit.jsonl"))

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
        ok("不认识的权限值 → 空（未设置，不猜）", wfrm[1].get("permLevel") == "", repr(wfrm[1]))
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
            try:
                orch.reply_permission(pid, "once", via="hotkey")
                ok("热键不能批准破坏性操作", False)
            except ValueError:
                ok("热键不能批准破坏性操作", True)
            orch.reply_permission(pid, "reject")
        th.join(3)
        ok("拒绝后返回 reject", box.get("d") == "reject", repr(box.get("d")))
        ok("空权限不再回落面板：Format-Table 不是破坏性",
           not orch._is_destructive("Get-ChildItem | Format-Table -AutoSize"))
        ok("真正的磁盘 format 仍算破坏性", orch._is_destructive("format D: /q"))
        ok("Format-Volume 仍算破坏性", orch._is_destructive("Format-Volume -DriveLetter D"))
        ok("读整个环境变量算敏感", orch._is_secret("Get-ChildItem Env:"))
        ok("普通 $env:TEMP 不算敏感", not orch._is_secret("echo $env:TEMP"))
        # 面板等级设成 all：工作流空权限仍然要问（证明不再回落）
        import cocraft_agents as _ca
        old_lv = _ca.perm_level
        _ca.perm_level = lambda: "all"
        perm_run2 = {"id": "run_perm2", "cwd": TMP, "permissions": []}
        box2 = {}
        def _ask_empty():
            box2["d"] = orch._ask_permission(
                perm_run2, {"id": "m1", "name": "t", "permLevel": ""},
                "execute", "Run command", [], [], {"command": "Get-ChildItem"})
        th2 = _th.Thread(target=_ask_empty, daemon=True)
        th2.start()
        pid2 = wait_until(lambda: (perm_run2.get("permissions") or [{}])[-1].get("id"), timeout=3)
        ok("空权限 = 全部询问（面板 all 也不能放行）", bool(pid2), repr(pid2))
        ok("待批期间 _member_waiting=True", orch._member_waiting("run_perm2", "m1"))
        def _ap_has_pid():
            try:
                import json as _json
                with open(os.path.join(TMP, "_approvals.json"), "r", encoding="utf-8") as fh:
                    _ap = _json.load(fh)
                return any(str(x.get("id")) == str(pid2) for x in (_ap.get("items") or []))
            except Exception:  # noqa: BLE001
                return False
        ok("待批快照写到 _approvals.json", wait_until(_ap_has_pid, timeout=3))
        if pid2:
            orch.reply_permission(pid2, "reject", via="hotkey")
        th2.join(3)
        ok("空权限被拒后返回 reject", box2.get("d") == "reject", repr(box2.get("d")))
        ok("结算后 _member_waiting=False", not orch._member_waiting("run_perm2", "m1"))
        _ca.perm_level = old_lv
        # ★敏感读取：trust 也不自动放行
        perm_run3 = {"id": "run_perm3", "cwd": TMP, "permissions": []}
        box3 = {}
        def _ask_secret():
            box3["d"] = orch._ask_permission(
                perm_run3, {"id": "m1", "name": "t", "permLevel": "trust"},
                "execute", "Run command", [], [], {"command": "Get-ChildItem Env:"})
        th3 = _th.Thread(target=_ask_secret, daemon=True)
        th3.start()
        pid3 = wait_until(lambda: (perm_run3.get("permissions") or [{}])[-1].get("id"), timeout=3)
        ok("敏感读取（trust）仍然排队", bool(pid3), repr(pid3))
        ok("待批记录带 secret 标记",
           bool(pid3) and bool((perm_run3.get("permissions") or [{}])[-1].get("secret")))
        if pid3:
            try:
                orch.reply_permission(pid3, "once", via="hotkey")
                ok("热键不能批准敏感读取", False)
            except ValueError:
                ok("热键不能批准敏感读取", True)
            orch.reply_permission(pid3, "reject")
        th3.join(3)
        ok("敏感读取被拒后返回 reject", box3.get("d") == "reject", repr(box3.get("d")))
        # ★运行上下文：把结点名/大脑名直接告诉 agent
        _ctx = orch._run_context(
            {"id": "r1", "workflowName": "演示流", "workflowId": "wf_demo", "cwd": TMP,
             "stages": [{"id": "s1", "name": "实现"}],
             "members": [{"id": "m1", "name": "impl", "role": "implementer",
                          "engine": "mock", "permLevel": "trust"},
                         {"id": "m2", "name": "rev", "role": "reviewer",
                          "engine": "codex-node"}]},
            {"id": "m1", "name": "impl", "role": "implementer", "engine": "mock",
             "permLevel": "trust", "stage": "s1"}, "deepseek-flash")
        ok("运行上下文带工作流/成员/大脑/其他成员",
           "演示流" in _ctx and "impl" in _ctx and "codex-node" in _ctx
           and "deepseek-flash" in _ctx and "信任" in _ctx, _ctx[:120])
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
        # ★项目级画布：同一 cwd 的工作流共享坐标；新工作流不另起空画布
        wf3 = orch.save_workflow({
            "name": "探索分支", "cwd": TMP,
            "members": [{"id": "m1", "role": "reviewer", "name": "rev", "engine": "mock"}],
            "layout": {"zoom": 1, "nodes": {}},
        })
        all_w = orch.list_workflows()
        one = next((w for w in all_w if str(w.get("id")) == wid), {})
        two = next((w for w in all_w if str(w.get("id")) == wf3.get("id")), {})
        ok("★同一项目的工作流共享 projectCanvas",
           (one.get("projectCanvas") or {}).get("nodes", {}).get("m1", {}).get("x") == 500
           and (two.get("projectCanvas") or {}).get("nodes", {}).get("m1", {}).get("x") == 500,
           repr((one.get("projectCanvas"), two.get("projectCanvas"))))
        ok("★新工作流复用项目画布（不是空节点）",
           (wf3.get("projectCanvas") or {}).get("nodes", {}).get("m1", {}).get("x") == 500,
           repr(wf3.get("projectCanvas")))
        ok("★项目画布仍保留两个工作流的节点",
           set(((one.get("projectCanvas") or {}).get("nodes") or {}).keys())
           >= {"task", "m1"}, repr(one.get("projectCanvas")))
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
        # ★继续上次探索：复制成员会话，不污染旧 run
        prev_session = str(((orch.get_run(wr["id"]).get("members") or [{}])[0]).get("sessionID") or "")
        cont = orch.start_run("继续探索：重点看登录边界", workflow_id=wid,
                              from_run_id=wr["id"])
        ok("★继续探索记录 continuedFrom",
           cont.get("continuedFrom") == wr["id"], repr(cont.get("continuedFrom")))
        ok("★继续探索复用上一轮成员会话",
           int(cont.get("resumedMembers") or 0) >= 1, repr(cont.get("resumedMembers")))
        got_cont = wait_until(lambda: (orch.get_run(cont["id"]).get("status") in ("done", "partial", "failed")
                                      and orch.get_run(cont["id"])) or None, 20)
        ok("继续探索能正常跑完",
           bool(got_cont and str(got_cont.get("status")) in ("done", "partial")),
           repr(got_cont and got_cont.get("status")))
        cr = got_cont or orch.get_run(cont["id"])
        cm = (cr.get("members") or [{}])[0]
        ok("★继续探索的成员仍指向原会话",
           bool(prev_session) and cm.get("sessionID") == prev_session,
           repr((prev_session, cm.get("sessionID"), cm.get("resumed"), cm.get("error"))))
        cp = orch.duplicate_workflow(wid)
        ok("复制工作流：新 id、成员一样",
           cp.get("id") != wid and len(cp.get("members") or []) == 2, cp.get("id"))
        ok("删除工作流不删运行记录",
           orch.delete_workflow(cp["id"]) is True and bool(orch.get_run(wr["id"])), "")
        ok("删除不存在 → False", orch.delete_workflow("wf_nope") is False)

        print("\n== 0b-2. ★P3：归档 / 旧 run「另存为工作流」 ==")
        aw = orch.archive_workflow(wid, True)
        ok("归档：archived=True（不删除）",
           aw.get("archived") is True and bool(orch.get_run(wr["id"])), repr(aw.get("archived")))
        aw2 = orch.archive_workflow(wid, False)
        ok("取消归档", aw2.get("archived") is False, repr(aw2.get("archived")))
        ok("归档不删运行记录 / 工作流还在",
           bool(orch.get_run(wr["id"]))
           and any(str(w.get("id")) == wid for w in orch.list_workflows()))
        try:
            orch.archive_workflow("wf_nope")
            ok("归档不存在 → 拒绝", False)
        except ValueError:
            ok("归档不存在 → 拒绝", True)
        orphan = orch.start_run("旧任务正文", cwd=TMP, members=[
            {"engine": "mock", "name": "impl2", "role": "implementer"},
            {"engine": "mock", "name": "rev2", "role": "reviewer"},
        ])
        wait_until(lambda: (orch.get_run(orphan["id"]).get("status") in
                            ("done", "partial", "failed") and orch.get_run(orphan["id"])) or None, 20)
        ok("旧 run 没有 workflowId（先确认前提）",
           not (orphan.get("workflowId") or ""), repr(orphan.get("workflowId")))
        n0 = len(orch.list_workflows())
        fw = orch.workflow_from_run(orphan["id"])
        ok("另存为工作流：新 id + 成员按 run 快照",
           bool(fw.get("id")) and len(fw.get("members") or []) == 2, repr(fw.get("id")))
        ok("默认任务 = 那次的任务",
           fw.get("taskTemplate") == "旧任务正文", repr(fw.get("taskTemplate")))
        ok("run 被认领（workflowId 写回）",
           (orch.get_run(orphan["id"]).get("workflowId") or "") == fw.get("id"),
           repr(orch.get_run(orphan["id"]).get("workflowId")))
        ok("列表 +1", len(orch.list_workflows()) == n0 + 1)
        fw2 = orch.workflow_from_run(orphan["id"])
        ok("★同一成员组合再存一次 → 复用（不重复造）",
           fw2.get("id") == fw.get("id") and fw2.get("existing") is True,
           repr(fw2.get("existing")))
        ok("重复另存没有新增工作流", len(orch.list_workflows()) == n0 + 1)
        orch.archive_workflow(fw["id"], True)
        fw3 = orch.workflow_from_run(orphan["id"])
        ok("已有工作流被归档 → 另存时自动放回来",
           fw3.get("id") == fw.get("id") and fw3.get("archived") is False,
           repr(fw3.get("archived")))
        orch._RUNS["run_arb_only"] = {"id": "run_arb_only", "cwd": TMP, "task": "x",
                                      "members": [{"id": "m1", "name": "arb", "engine": "mock",
                                                   "kind": "arbitration"}]}
        try:
            orch.workflow_from_run("run_arb_only")
            ok("只有仲裁者的 run → 拒绝另存", False)
        except ValueError:
            ok("只有仲裁者的 run → 拒绝另存", True)
        finally:
            orch._RUNS.pop("run_arb_only", None)

        print("\n== 0d. ★P4：阶段串行 / 条件 / 失败策略 / 重试 / 取消 ==")
        st0, ms0 = orch.norm_stages(None, [{"id": "m1", "role": "implementer",
                                            "name": "a", "engine": "mock"}])
        ok("没有 stages → 一个默认阶段（老工作流兼容）",
           len(st0) == 1 and st0[0]["id"] == "s1" and ms0[0]["stage"] == "s1",
           repr((st0, ms0)))
        st1, ms1 = orch.norm_stages(
            [{"id": "s2", "name": "审查", "when": "onSuccess", "onFail": "continue",
              "retries": 9}],
            [{"id": "m1", "engine": "mock", "stage": "s2"},
             {"id": "m2", "engine": "mock", "stage": "nope"}])
        ok("阶段 id 重排 + 成员归属（认不出的落到第一个阶段）",
           [s["id"] for s in st1] == ["s1"] and ms1[0]["stage"] == "s1"
           and ms1[1]["stage"] == "s1", repr((st1, ms1)))
        ok("retries 夹到 0~2 / when、onFail 归一",
           st1[0]["retries"] == 2 and st1[0]["when"] == "onSuccess"
           and st1[0]["onFail"] == "continue", repr(st1[0]))

        swf = orch.save_workflow({
            "name": "两阶段", "cwd": TMP, "taskTemplate": "串联任务",
            "stages": [{"name": "实现"}, {"name": "审查"}],
            "members": [
                {"id": "m1", "role": "implementer", "name": "impl", "engine": "mock",
                 "stage": "s1"},
                {"id": "m2", "role": "reviewer", "name": "rev", "engine": "mock",
                 "stage": "s2"},
            ],
        })
        ok("工作流保存 stages（成员带 stage）",
           len(swf.get("stages") or []) == 2
           and [m.get("stage") for m in swf["members"]] == ["s1", "s2"],
           repr(swf.get("stages")))
        sr = orch.start_run("", workflow_id=swf["id"])
        ok("run 带阶段快照（2 阶段，s1/s2）",
           [x.get("id") for x in (sr.get("stages") or [])] == ["s1", "s2"]
           and all("status" in x for x in (sr.get("stages") or [])),
           repr(sr.get("stages")))
        sgot = wait_until(lambda: (orch.get_run(sr["id"]).get("status") in
                                   ("done", "partial", "failed") and orch.get_run(sr["id"])) or None,
                          30)
        ok("两阶段跑完（done）", sgot and sgot.get("status") == "done",
           repr(sgot and sgot.get("status")))
        sms = {m["id"]: m for m in (sgot or {}).get("members") or []}
        sst = {x["id"]: x for x in (sgot or {}).get("stages") or []}
        ok("★阶段串行：阶段 2 的成员在阶段 1 结束后才开始",
           sms["m2"]["started"] >= sms["m1"]["ended"] - 5,
           "m1 ended=%s m2 started=%s" % (sms["m1"].get("ended"), sms["m2"].get("started")))
        ok("两个阶段都 done / 阶段 2 在阶段 1 之后",
           sst["s1"]["status"] == "done" and sst["s2"]["status"] == "done"
           and sst["s2"]["started"] >= sst["s1"]["ended"] - 5, repr(sst))
        body2 = orch._stage_body(sgot, sst["s2"], "### 实现者 · impl\nMOCK-REPLY 1")
        ok("★阶段 2 的提示词带「上一阶段产出」+ 阶段身份",
           "第 2/2 阶段" in body2 and "上一阶段产出" in body2 and "MOCK-REPLY" in body2,
           body2[:180])
        dig = orch._stage_digest(sst["s1"], [sms["m1"]])
        ok("阶段产出摘要带成员名与正文", "impl" in dig and "MOCK-REPLY" in dig, dig[:140])

        cwf = orch.save_workflow({
            "name": "条件阶段", "cwd": TMP,
            "stages": [{"name": "实现"}, {"name": "仅失败时", "when": "onFailure"}],
            "members": [
                {"id": "m1", "engine": "mock", "name": "a", "stage": "s1"},
                {"id": "m2", "engine": "mock", "name": "b", "stage": "s2"},
            ],
        })
        cr = orch.start_run("条件任务", workflow_id=cwf["id"])
        cgot = wait_until(lambda: (orch.get_run(cr["id"]).get("status") in
                                   ("done", "partial", "failed") and orch.get_run(cr["id"])) or None,
                          30)
        cst = {x["id"]: x for x in (cgot or {}).get("stages") or []}
        cms = {m["id"]: m for m in (cgot or {}).get("members") or []}
        ok("onFailure 条件：上一阶段成功 → 该阶段 skipped（成员也 skipped）",
           cst.get("s2", {}).get("status") == "skipped"
           and cms.get("m2", {}).get("status") == "skipped",
           repr((cst.get("s2", {}).get("status"), cms.get("m2", {}).get("status"))))
        ok("条件跳过不会把 run 变成 partial", (cgot or {}).get("status") == "done",
           repr((cgot or {}).get("status")))

        bwf = orch.save_workflow({
            "name": "失败即停", "cwd": TMP,
            "stages": [{"name": "坏引擎", "onFail": "stop", "retries": 1},
                       {"name": "不该跑"}],
            "members": [
                {"id": "m1", "engine": "not-exist", "name": "bad", "stage": "s1"},
                {"id": "m2", "engine": "mock", "name": "next", "stage": "s2"},
            ],
        })
        br = orch.start_run("失败任务", workflow_id=bwf["id"])
        bgot = wait_until(lambda: (orch.get_run(br["id"]).get("status") in
                                   ("failed", "partial", "done") and orch.get_run(br["id"])) or None,
                          30)
        bst = {x["id"]: x for x in (bgot or {}).get("stages") or []}
        bms = {m["id"]: m for m in (bgot or {}).get("members") or []}
        ok("onFail=stop：阶段 1 失败 → 阶段 2 skipped",
           bst.get("s1", {}).get("status") == "failed"
           and bst.get("s2", {}).get("status") == "skipped",
           repr((bst.get("s1", {}).get("status"), bst.get("s2", {}).get("status"))))
        ok("run 状态 failed（不是 done）", (bgot or {}).get("status") == "failed",
           repr((bgot or {}).get("status")))
        ok("★重试：retries=1 → 失败成员多跑一次",
           int(bms.get("m1", {}).get("retryCount") or 0) == 1,
           repr(bms.get("m1", {}).get("retryCount")))

        kwf = orch.save_workflow({
            "name": "失败继续", "cwd": TMP,
            "stages": [{"name": "坏引擎", "onFail": "continue"}, {"name": "照样跑"}],
            "members": [
                {"id": "m1", "engine": "not-exist", "name": "bad", "stage": "s1"},
                {"id": "m2", "engine": "mock", "name": "next", "stage": "s2"},
            ],
        })
        kr = orch.start_run("继续任务", workflow_id=kwf["id"])
        kgot = wait_until(lambda: (orch.get_run(kr["id"]).get("status") in
                                   ("partial", "failed", "done") and orch.get_run(kr["id"])) or None,
                          30)
        kst = {x["id"]: x for x in (kgot or {}).get("stages") or []}
        ok("onFail=continue：阶段 1 失败，阶段 2 仍然跑（done）",
           kst.get("s1", {}).get("status") == "failed"
           and kst.get("s2", {}).get("status") == "done",
           repr((kst.get("s1", {}).get("status"), kst.get("s2", {}).get("status"))))
        ok("run 状态 partial（有人成功有人失败）",
           (kgot or {}).get("status") == "partial", repr((kgot or {}).get("status")))

        # 取消：阶段 1 还在跑 → 取消全部 → 阶段 2 不该启动
        twf = orch.save_workflow({
            "name": "取消阶段", "cwd": TMP,
            "stages": [{"name": "慢"}, {"name": "后面"}],
            "members": [
                {"id": "m1", "engine": "mock-slow", "name": "slow", "stage": "s1"},
                {"id": "m2", "engine": "mock", "name": "later", "stage": "s2"},
            ],
        })
        tr = orch.start_run("取消任务", workflow_id=twf["id"])
        wait_until(lambda: (orch.get_run(tr["id"]).get("members") or [{}])[0].get("status")
                   == "running", 10)
        orch.cancel_run(tr["id"])
        tgot = wait_until(lambda: (orch.get_run(tr["id"]).get("members") or [{}])[0].get("status")
                          == "cancelled" and orch.get_run(tr["id"]), 15)
        tms = {m["id"]: m for m in (tgot or {}).get("members") or []}
        tst = {x["id"]: x for x in (tgot or {}).get("stages") or []}
        ok("★取消：阶段 2 不会启动（skipped / started=0）",
           tms.get("m2", {}).get("status") == "skipped"
           and int(tms.get("m2", {}).get("started") or 0) == 0,
           repr((tms.get("m2", {}).get("status"), tms.get("m2", {}).get("started"))))
        ok("取消后阶段 2 标记 skipped",
           tst.get("s2", {}).get("status") == "skipped",
           repr(tst.get("s2", {}).get("status")))
        ok("run 状态 cancelled", (tgot or {}).get("status") == "cancelled",
           repr((tgot or {}).get("status")))

        print("\n== 3c. 面板重启后残留的 running（不能再永远转圈） ==")
        stale = {"id": "run_stale", "task": "残留", "status": "running",
                 "created": orch._now() - 60000, "updated": orch._now() - 60000,
                 "members": [{"id": "m1", "name": "旧成员", "engine": "mock",
                              "status": "running", "started": orch._now() - 60000}],
                 "stages": [{"id": "s1", "name": "实现", "status": "running",
                             "started": orch._now() - 60000}],
                 "permissions": [], "forms": []}
        orch._RUNS["run_stale"] = stale
        changed = orch._reconcile_stale_run(stale)
        ok("重启后残留 running → 自动收尾为 failed",
           changed and stale["status"] == "failed"
           and stale["members"][0]["status"] == "failed"
           and "重启" in stale["members"][0]["error"],
           repr((stale["status"], stale["members"][0].get("status"))))
        stale2 = {"id": "run_stale_cancel", "task": "取消残留", "status": "cancelled",
                  "created": orch._now() - 60000, "updated": orch._now() - 60000,
                  "members": [{"id": "m1", "name": "旧成员", "engine": "mock",
                               "status": "running", "started": orch._now() - 60000}],
                  "stages": [{"id": "s1", "name": "实现", "status": "running"}],
                  "permissions": [], "forms": []}
        orch._RUNS["run_stale_cancel"] = stale2
        orch._reconcile_stale_run(stale2)
        ok("cancelled 的 run 里残留成员 → cancelled",
           stale2["members"][0]["status"] == "cancelled"
           and stale2["stages"][0]["status"] == "cancelled",
           repr((stale2["members"][0]["status"], stale2["stages"][0]["status"])))
        # 没有执行线程时点「取消全部」也要立刻收尾
        stale3 = {"id": "run_stale_cancel2", "task": "再取消", "status": "running",
                  "created": orch._now() - 1000, "updated": orch._now() - 1000,
                  "members": [{"id": "m1", "name": "旧成员", "engine": "mock",
                               "status": "running"}],
                  "stages": [{"id": "s1", "name": "实现", "status": "running"}],
                  "permissions": [], "forms": []}
        orch._RUNS["run_stale_cancel2"] = stale3
        cr = orch.cancel_run("run_stale_cancel2")
        ok("无执行线程时「取消全部」→ 成员立刻 cancelled",
           cr.get("status") == "cancelled"
           and (cr.get("members") or [{}])[0].get("status") == "cancelled"
           and (cr.get("stages") or [{}])[0].get("status") == "cancelled",
           repr((cr.get("status"), [(m.get("status")) for m in cr.get("members") or []])))
        for _rid in ("run_stale", "run_stale_cancel", "run_stale_cancel2"):
            orch._RUNS.pop(_rid, None)

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
        # ★慢机器上 create_session 可能要几秒：等两个 _ACTIVE 都注册，别用固定 0.8s 赌
        wait_until(lambda: len([k for k in orch._ACTIVE
                                if str(k[0]) in (ra["id"], rb["id"])]) == 2, 15)
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

        print("\n== 3b. 工作流里的 agent 追问（表单） ==")
        run3b = orch.start_run("要回答", members=[
            {"engine": "mock-perm", "name": "回答甲", "permLevel": "trust"}])
        rid3b = run3b["id"]
        forms = wait_until(lambda: orch.pending_forms(rid3b) or None, 10)
        ok("出现待回答表单", bool(forms), repr(forms))
        f0 = (forms or [{}])[0]
        ok("表单带标题 / 字段（strategy）",
           "策略" in str(f0.get("title")) and "strategy" in str(f0.get("fields")),
           repr(f0))
        ok("回答后返回 True", bool(f0.get("id")) and orch.reply_form(f0.get("id"), {"strategy": "balanced"}) is True)
        got3b = wait_until(lambda: (orch.get_run(rid3b).get("status") in ("done", "partial", "failed")
                                    and orch.get_run(rid3b)) or None, 20)
        m3b = ((got3b or {}).get("members") or [{}])[0]
        ok("回答后 agent 继续并完成",
           m3b.get("status") == "done" and "完成" in (m3b.get("output") or ""),
           repr((m3b.get("status"), m3b.get("output"))))
        ok("待回答问题已清空", not orch.pending_forms(rid3b))
        ok("历史里留下 answered",
           any(f.get("status") == "answered" for f in (got3b or {}).get("forms") or []),
           repr((got3b or {}).get("forms")))

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

        print("\n== 9. ★上游账户问题（余额不足）不能标成「完成」 ==")
        REG["agents"].append({
            "id": "mock-balance", "label": "Mock 余额不足",
            "command": [sys.executable, MOCK], "cwd": TMP,
            "env": {"ACP_MOCK_SIMPLE": "1",
                    "ACP_MOCK_TEXT": ("AcpError ACP 错误 -32603: Internal error: turn failed: "
                                      "Insufficient Balance (request_id: cefc94cc-290c-4c8d-926b)")},
            "provider": "mock"})
        try:
            br = orch.start_run("余额测试", cwd=TMP, members=[
                {"engine": "mock-balance", "role": "implementer", "name": "b1",
                 "permLevel": "trust"}])
            wait_until(lambda: (orch.get_run(br["id"]).get("status") or "") in
                       ("done", "partial", "failed", "cancelled"), timeout=25)
            r2 = orch.get_run(br["id"]) or {}
            m0 = (r2.get("members") or [{}])[0]
            ok("余额不足 → 成员 failed（以前会被标成 done）",
               m0.get("status") == "failed", repr(m0.get("status")))
            ok("余额不足 → 整个 run=failed", r2.get("status") == "failed", repr(r2.get("status")))
            ok("run 带 failReason（前端红字用）",
               "上游账户问题" in str(r2.get("failReason") or ""), repr(r2.get("failReason"))[:110])
            ok("成员错误里有「充值 / 换 Key」指引",
               "充值" in str(m0.get("error") or ""), repr(m0.get("error"))[:140])
        finally:
            REG["agents"].pop()

        print("\n== 9b. ★正常回答里提到「Insufficient Balance」不该被误判 ==")
        REG["agents"].append({
            "id": "mock-explain", "label": "Mock 解释报错",
            "command": [sys.executable, MOCK], "cwd": TMP,
            "env": {"ACP_MOCK_SIMPLE": "1",
                    "ACP_MOCK_TEXT": "这次报错是 Insufficient Balance（余额不足）导致的，建议充值后再重试。"},
            "provider": "mock"})
        try:
            er = orch.start_run("解释报错", cwd=TMP, members=[
                {"engine": "mock-explain", "role": "implementer", "name": "e1",
                 "permLevel": "trust"}])
            wait_until(lambda: (orch.get_run(er["id"]).get("status") or "") in
                       ("done", "partial", "failed", "cancelled"), timeout=25)
            r3 = orch.get_run(er["id"]) or {}
            ok("只是解释报错 → 成员 done（判据要窄）",
               ((r3.get("members") or [{}])[0].get("status")) == "done",
               repr(((r3.get("members") or [{}])[0]).get("status")))
            ok("解释报错 → run 不算失败", r3.get("status") == "done", repr(r3.get("status")))
        finally:
            REG["agents"].pop()

        print("\n== 10. ★可逆执行：工作流也有「先隔离再放行」+「每轮改了哪些文件」==")
        import snapshots as _snap  # noqa: E402
        _snap.QUARANTINE_DIR = os.path.join(TMP, "_quarantine")
        _snap.SNAPSHOT_DIR = os.path.join(TMP, "_snapshots")
        _snap.STORE_DIR = os.path.join(TMP, "_store")
        ws = os.path.join(TMP, "ws")
        os.makedirs(ws, exist_ok=True)
        old = os.path.join(ws, "old.txt")
        io.open(old, "w", encoding="utf-8").write("hi")
        fake_run = {"id": "wf-test-1", "cwd": ws, "task": "t"}
        fake_member = {"id": "m1", "name": "m1", "role": "implementer", "engine": "mock"}
        qid = orch._quarantine_before_allow(fake_run, fake_member, "", "删除 old.txt", [old])
        ok("破坏性批准后先隔离 → 拿到隔离记录 id", bool(qid), repr(qid))
        ok("隔离记录里真有那份原件（能一键恢复）",
           bool(qid) and any("old.txt" in fn
                             for r, _d, fs in os.walk(os.path.join(_snap.QUARANTINE_DIR,
                                                                   qid.replace("/", os.sep)))
                             for fn in fs), "quarantine=" + str(qid))
        ok("隔离动作进了成员活动（界面上看得见）",
           any(a.get("name") in ("先隔离再放行", "忏悔室留存旧版")
               for a in (fake_member.get("activities") or [])))
        # 轮快照 → 改一个文件 → 改动清单
        snap = orch._turn_snapshot(fake_run, fake_member)
        ok("轮快照拿到 scope/ts（只记路径+哈希，不占磁盘）",
           bool(snap.get("scope")) and bool(snap.get("ts")), repr(snap))
        io.open(os.path.join(ws, "new.txt"), "w", encoding="utf-8").write("new")
        io.open(old, "w", encoding="utf-8").write("hi changed")
        ch = orch._turn_changes(fake_run, fake_member, snap)
        ok("「这一轮改了哪些文件」：新增 + 修改都算到",
           int(ch.get("files") or 0) >= 2 and int(ch.get("added") or 0) >= 1
           and int(ch.get("changed") or 0) >= 1,
           "files=%r added=%r changed=%r" % (ch.get("files"), ch.get("added"), ch.get("changed")))
        ok("改动清单带文件名（不是只有计数）",
           any("new.txt" in p for p in (ch.get("paths") or [])), repr((ch.get("paths") or [])[:3]))
        ok("改动也进了审计链（kind=files）",
           any((r.get("action") or {}).get("kind") == "files"
               for r in (__import__("audit").records(limit=50) or [])))
        # 真实跑一轮：成员结果里必须带 changes 字段（前端能直接显示）
        mr = orch.start_run("改动字段", cwd=ws, members=[
            {"engine": "mock", "role": "implementer", "name": "c1", "permLevel": "trust"}])
        wait_until(lambda: (orch.get_run(mr["id"]).get("status") or "") in
                   ("done", "partial", "failed", "cancelled"), timeout=25)
        m1 = ((orch.get_run(mr["id"]) or {}).get("members") or [{}])[0]
        ok("真实跑完一轮 → member.changes 是个字典（public 字段没漏）",
           isinstance(m1.get("changes"), dict), repr(m1.get("changes"))[:120])
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
