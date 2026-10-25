# -*- coding: utf-8 -*-
r"""无头回归：★主动编排「一键建组」（`cocraft_agents.team_plan / create_team`）。

**不联网**：`create_agent / join / set_enabled` 全部打桩，只验**编排逻辑与失败隔离**。

跑法：`python tools\team_test.py`
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cocraft_agents as ca      # noqa: E402

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


def main() -> int:
    import tempfile

    existing = [{"id": "1", "name": "yiyi", "chats": []}]
    ca._entries = lambda: list(existing)
    ca.acp_log = lambda *a, **k: None

    # ⚠ 自定义预设会写 `_cocraft_agents.json` —— 必须指到临时目录，
    #   否则会污染真实状态、也会让「内置 3 个」这类断言随本机数据漂移。
    state_tmp = tempfile.mkdtemp(prefix="team-state-")
    ca.STATE_FILE = os.path.join(state_tmp, "_cocraft_agents.json")

    print("== 1. 预设清单 ==")
    ps = ca.team_presets()
    ok("有 3 个预设", len(ps) == 3, "%r" % ([p["id"] for p in ps],))
    ok("每个预设的每行都带角色中文名",
       all(r.get("roleLabel") for p in ps for r in p["rows"]), "%r" % (ps,))
    ok("内置预设标了 builtin（前端据此禁删）", all(p.get("builtin") is True for p in ps))

    print("\n== 2. team_plan：默认 / 引擎 / 名字 / 重名 ==")
    plan = ca.team_plan("dev", {"default": "opencode-acp"})
    ok("dev → 实现 + 审查 + 测试（3 个）", [r["role"] for r in plan] == ["implementer", "reviewer", "tester"],
       "%r" % ([r["role"] for r in plan],))
    ok("default 引擎套给所有人", all(r["engine"] == "opencode-acp" for r in plan))
    ok("名字默认 impl / rev / test", [r["name"] for r in plan] == ["impl", "rev", "test"])

    plan2 = ca.team_plan("review", {"implementer": "codex-node"})
    ok("按角色给引擎：只有实现者变", plan2[0]["engine"] == "codex-node" and plan2[1]["engine"] == "")

    plan3 = ca.team_plan("review", {}, {"implementer": "coder"})
    ok("自定义名字生效", [r["name"] for r in plan3] == ["coder", "rev"], "%r" % ([r["name"] for r in plan3],))

    ca._entries = lambda: [{"id": "1", "name": "impl"}]
    plan4 = ca.team_plan("review", {}, {"implementer": "impl"})
    ok("重名自动加后缀（impl → impl-2）", plan4[0]["name"] == "impl-2", "%r" % (plan4[0],))
    ca._entries = lambda: list(existing)

    plan5 = ca.team_plan("dev", {}, {"implementer": "my name/x"})
    ok("名字里非法字符被清掉（只留 `[A-Za-z0-9_-]`，`@` 才解析得到）",
       plan5[0]["name"] == "mynamex", "%r" % (plan5[0]["name"],))
    plan6 = ca.team_plan("dev", {}, {"implementer": "纯中文名"})
    ok("全中文名会被清空成安全占位（因为 `@` 只认 ASCII）",
       plan6[0]["name"] == "ai", "%r" % (plan6[0]["name"],))

    try:
        ca.team_plan("nope")
        ok("不认识的预设 → ValueError", False)
    except ValueError:
        ok("不认识的预设 → ValueError", True)

    print("\n== 2b. 自定义组预设：存 / 列 / 删（内置不许删） ==")
    saved = ca.save_team_preset("我的对拍组", [{"role": "implementer", "name": "dev1"},
                                                {"role": "reviewer", "name": "rev1"}])
    ok("存自定义预设：id 由名字生成（保留中文）", saved["id"] == "我的对拍组", "%r" % (saved,))
    ok("自定义预设带 builtin=False（前端据此允许删）", saved.get("builtin") is False)
    ok("行里带角色中文名", [r.get("roleLabel") for r in saved["rows"]] == ["实现者", "审查者"],
       "%r" % (saved["rows"],))
    ps2 = ca.team_presets()
    ok("列表里多出这个自定义预设（3 → 4）", len(ps2) == 4 and ps2[-1]["id"] == "我的对拍组",
       "%r" % ([p["id"] for p in ps2],))
    ok("同名再存 = 更新那一份（不会冒出两个）", len(ca.team_presets()) == 4
       and ca.save_team_preset("我的对拍组", [{"role": "tester", "name": "t1"}])["id"] == "我的对拍组"
       and ca.team_presets()[-1]["rows"][0]["role"] == "tester")

    plan_rows = ca.team_plan("我的对拍组")
    ok("能直接用自定义预设建（角色来自预设）", [r["role"] for r in plan_rows] == ["tester"],
       "%r" % ([r["role"] for r in plan_rows],))

    # 行模式：前端把「面板里当前那几行」整行发过来（支持临时改角色 / 同角色多人 / 每行选不同大脑）
    plan_dup = ca.team_plan("dev", {}, {}, rows=[
        {"role": "implementer", "name": "a", "engine": "codex-node"},
        {"role": "implementer", "name": "a"},
        {"role": "tester", "name": "t"},
    ])
    ok("行模式：同角色可以出现两次（名字自动 a / a-2）",
       [r["name"] for r in plan_dup] == ["a", "a-2", "t"], "%r" % ([r["name"] for r in plan_dup],))
    ok("行模式：每行自己选的大脑生效（a=codex-node，其余留空）",
       plan_dup[0]["engine"] == "codex-node" and plan_dup[1]["engine"] == "" and plan_dup[2]["engine"] == "",
       "%r" % ([r["engine"] for r in plan_dup],))
    ok("行模式不再被按角色的 dict 串味（names 传了也不覆盖行里的名字）",
       [r["name"] for r in ca.team_plan("dev", {}, {"implementer": "zzz"},
                                        rows=[{"role": "implementer", "name": "a"},
                                              {"role": "implementer", "name": "b"}])] == ["a", "b"])

    try:
        ca.save_team_preset("坏组", [{"role": "nope", "name": "x"}])
        ok("不认识的角色 → ValueError", False)
    except ValueError:
        ok("不认识的角色 → ValueError", True)
    try:
        ca.save_team_preset("", [{"role": "tester", "name": "x"}])
        ok("没名字 → ValueError", False)
    except ValueError:
        ok("没名字 → ValueError", True)
    try:
        ca.save_team_preset("空组", [])
        ok("一行都没有 → ValueError", False)
    except ValueError:
        ok("一行都没有 → ValueError", True)
    try:
        ca.delete_team_preset("dev")
        ok("内置预设不许删 → ValueError", False)
    except ValueError:
        ok("内置预设不许删 → ValueError", True)
    try:
        ca.delete_team_preset("我的对拍组")
        ok("删自定义预设", "我的对拍组" not in [p["id"] for p in ca.team_presets()])
    except Exception as exc:  # noqa: BLE001
        ok("删自定义预设", False, repr(exc))
    try:
        ca.delete_team_preset("我的对拍组")
        ok("删不存在的预设 → KeyError", False)
    except KeyError:
        ok("删不存在的预设 → KeyError", True)

    print("\n== 3. create_team：建 → 进群 → 开应答 ==")
    calls = []

    def fake_create(token, name, display="", engine="", role=""):
        calls.append(("create", name, role, engine))
        return {"id": "id-" + name, "name": name, "role": role}

    def fake_join(token, aid, chat):
        calls.append(("join", aid, str(chat)))
        return {}

    def fake_enable(aid, on):
        calls.append(("enable", aid, bool(on)))
        return {}

    ca.create_agent = fake_create
    ca.join = fake_join
    ca.set_enabled = fake_enable

    res = ca.create_team("tkn", 7, "dev", {"default": "opencode-acp"})
    ok("返回 ok + created 3", res["ok"] is True and len(res["created"]) == 3, "%r" % (res,))
    ok("没有失败项", not res["failed"])
    ok("每个人都走 「建 → 进群 → 开应答」三步",
       len([c for c in calls if c[0] == "create"]) == 3
       and len([c for c in calls if c[0] == "join"]) == 3
       and len([c for c in calls if c[0] == "enable" and c[2] is True]) == 3, "%r" % (calls,))
    ok("进的是同一个群（7）", all(c[2] == "7" for c in calls if c[0] == "join"),
       "%r" % ([c for c in calls if c[0] == "join"],))
    ok("建的时候把角色也带上了",
       ("create", "impl", "implementer", "opencode-acp") in calls, "%r" % (calls[:3],))

    # ★行模式：前端把面板里当前那几行原样发来（临时改过角色 / 名字 / 大脑）
    calls.clear()
    res_rows = ca.create_team("tkn", 7, "dev", rows=[
        {"role": "implementer", "name": "mine", "engine": "opencode-acp"},
        {"role": "reviewer", "name": "boss", "engine": "codex-node"},
    ])
    ok("行模式也能真建（2 个 / 三步都走）",
       res_rows["ok"] is True and len(res_rows["created"]) == 2
       and len([c for c in calls if c[0] == "create"]) == 2
       and len([c for c in calls if c[0] == "join"]) == 2, "%r" % (res_rows,))
    ok("行里的角色 / 名字 / 大脑都被用上",
       ("create", "mine", "implementer", "opencode-acp") in calls
       and ("create", "boss", "reviewer", "codex-node") in calls, "%r" % (calls,))

    print("\n== 4. 失败隔离：一个人失败不影响别人 ==")
    calls.clear()

    def fake_join_bad(token, aid, chat):
        calls.append(("join", aid))
        if aid == "id-rev":
            raise RuntimeError("群里没权限")
        return {}

    ca.join = fake_join_bad
    res2 = ca.create_team("tkn", 7, "dev", {"default": "x"})
    ok("另外两个照样建好", len(res2["created"]) == 2 and res2["ok"] is True, "%r" % (res2,))
    ok("失败那条报「进群」阶段 + 原因",
       len(res2["failed"]) == 1 and res2["failed"][0]["name"] == "rev"
       and res2["failed"][0]["stage"] == "进群" and "群里没权限" in res2["failed"][0]["error"],
       "%r" % (res2["failed"],))

    def fake_create_bad(token, name, display="", engine="", role=""):
        if name == "test":
            raise RuntimeError("上游 500")
        return {"id": "id-" + name}

    ca.create_agent = fake_create_bad
    ca.join = lambda *a, **k: {}
    res3 = ca.create_team("tkn", 7, "dev")
    ok("建 agent 就失败 → 记「建 agent」阶段",
       len(res3["failed"]) == 1 and res3["failed"][0]["stage"] == "建 agent"
       and "上游 500" in res3["failed"][0]["error"], "%r" % (res3["failed"],))

    try:
        ca.create_team("tkn", "", "dev")
        ok("没选群 → ValueError", False)
    except ValueError:
        ok("没选群 → ValueError", True)

    print("\n== 5. changes：派活前 → 现在改了哪些文件（文件级，接方向 1 的轮快照） ==")
    import shutil
    import tempfile

    import snapshots

    tmp = tempfile.mkdtemp(prefix="team-chg-")
    snapshots.SNAPSHOT_DIR = os.path.join(tmp, "s")
    snapshots.STORE_DIR = os.path.join(snapshots.SNAPSHOT_DIR, "_store")
    snapshots._TURN_CACHE.clear()
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws)
    with open(os.path.join(ws, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("1\n")

    ca._ws_root = lambda aid: ws
    snap = ca._snap_before_turn("11", "7")
    # 实现返回 {scope, ts}（ts 用于 C3 时间线归属）；老版本返回裸字符串，两种都认
    scope = snap.get("scope") if isinstance(snap, dict) else snap
    ok("派活前落了轮快照（scope = cc-<chat>-<aid>）", scope == "cc-7-11", "%r" % (snap,))

    with open(os.path.join(ws, "a.txt"), "w", encoding="utf-8") as fh:
        fh.write("2222\n")
    with open(os.path.join(ws, "b.txt"), "w", encoding="utf-8") as fh:
        fh.write("new\n")

    chg = ca.changes("7", "11")
    ok("changes 报出 2 个文件（改 1 + 增 1）",
       chg.get("ok") is True and chg.get("files") == 2
       and chg["stats"]["changed"] == 1 and chg["stats"]["added"] == 1, "%r" % (chg,))
    ok("changes **文件级**：不会去存内容（内容仓 0 字节）", snapshots.store_bytes() == 0,
       "store=%d" % snapshots.store_bytes())
    ok("第二次轮快照走热扫（rehash 只有真正改动的那 2 个）",
       snapshots.take_turn_snapshot(ws, scope, "第二轮")["rehash"] <= 2)
    ok("刚落完快照 → changes 归零（不会虚报改动）", ca.changes("7", "11").get("files") == 0)

    ca._ws_root = lambda aid: ""
    bad = ca.changes("7", "11")
    ok("没有工作目录 → 明说原因（不假装成功）",
       bad.get("ok") is False and "工作目录" in str(bad.get("error")), "%r" % (bad,))

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
