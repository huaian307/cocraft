# -*- coding: utf-8 -*-
r"""离线回归：★方向 3 收尾 —— 群聊成员自己的「权限上限」。

规则：成员上限**只能收紧**，不能给自己放权。
    生效 = min(面板等级, 成员上限)；没设上限 → 跟随面板。

覆盖：
  1. norm_member_perm 只认 5 档（trust 之类 → 空 = 跟随面板）
  2. effective_perm_level：上限更严 / 面板更严 / 没设 / ask 最严
  3. allowed_by_level 在 read 上限下：纯读自动、写/执行不自动
  4. public() 带 permLevel / effectivePermLevel / panelPermLevel / 来源
  5. update_agent 保存与归一 permLevel，且**不重建桥**（改上限不该打断正在跑的桥）

跑法：python tools\agent_perm_cap_test.py
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cocraft_agents as ca  # noqa: E402

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
    print("\n== 1. 归一（只认 5 档） ==")
    ok("空 / None → 跟随面板", ca.norm_member_perm("") == "" and ca.norm_member_perm(None) == "")
    ok("合法值保留", ca.norm_member_perm("read") == "read"
       and ca.norm_member_perm("all") == "all")
    ok("不认识的值（trust / 数字）→ 跟随面板",
       ca.norm_member_perm("trust") == "" and ca.norm_member_perm(3) == "")

    print("\n== 2. 生效等级 = min(面板, 成员上限) ==")
    ca.perm_level = lambda: "exec"
    eff, src = ca.effective_perm_level({"permLevel": ""})
    ok("没设上限 → 用面板（exec）", eff == "exec" and "跟随" in src, "%s / %s" % (eff, src))
    eff, src = ca.effective_perm_level({"permLevel": "read"})
    ok("成员上限更严（read）→ 用成员", eff == "read" and "成员" in src, "%s / %s" % (eff, src))
    eff, src = ca.effective_perm_level({"permLevel": "all"})
    ok("成员上限更宽（all）→ 仍按面板（exec）", eff == "exec" and "面板更严" in src,
       "%s / %s" % (eff, src))
    ca.perm_level = lambda: "ask"
    eff, _ = ca.effective_perm_level({"permLevel": "all"})
    ok("面板 ask 最严：成员设 all 也只能 ask", eff == "ask", eff)
    eff, _ = ca.effective_perm_level({"permLevel": "read"})
    ok("面板 ask + 成员 read → ask", eff == "ask", eff)
    ca.perm_level = lambda: "edit"
    eff, _ = ca.effective_perm_level({"permLevel": "nonsense"})
    ok("脏上限 → 按面板（edit）", eff == "edit", eff)

    print("\n== 3. read 上限下 allowed_by_level ==")
    ok("纯读 → 自动放行",
       ca.allowed_by_level("read", "read", "Read file", "", ["/ws/a.txt"], "/ws") is True)
    ok("写 → 不自动放行",
       ca.allowed_by_level("read", "edit", "Edit file", "", ["/ws/a.txt"], "/ws") is False)
    ok("跑命令 → 不自动放行",
       ca.allowed_by_level("read", "execute", "Run command", "", ["/ws"], "/ws") is False)

    print("\n== 4. public() 暴露上限与生效值 ==")
    ca.perm_level = lambda: "edit"
    pub = ca.public({"id": "a1", "name": "yiyi", "engine": "",
                     "role": "reviewer", "permLevel": "read"})
    ok("permLevel / effectivePermLevel / panelPermLevel 都在",
       pub.get("permLevel") == "read" and pub.get("effectivePermLevel") == "read"
       and pub.get("panelPermLevel") == "edit", repr(pub))
    ok("来源写明「成员上限」", "成员上限" in (pub.get("effectivePermLevelFrom") or ""),
       repr(pub.get("effectivePermLevelFrom")))

    print("\n== 5. update_agent 保存 / 归一 / 不重建桥 ==")
    dropped = {"n": 0}
    ca.find = lambda aid: {"id": aid, "name": "x", "engine": "eng", "permLevel": ""}
    ca._patch = lambda aid, **kv: dict({"id": aid, "name": "x", "engine": "eng"}, **kv)
    ca.public = lambda a: dict(a)
    ca._drop_bridge = lambda aid: dropped.__setitem__("n", dropped["n"] + 1)

    out = ca.update_agent("a1", {"permLevel": "read"})
    ok("保存 permLevel=read", out.get("permLevel") == "read", repr(out))
    ok("改上限不会重建桥（不打断正在跑的成员）", dropped["n"] == 0, repr(dropped))
    out = ca.update_agent("a1", {"permLevel": "trust"})
    ok("未知上限归一成空（跟随面板）", out.get("permLevel") == "", repr(out))
    out = ca.update_agent("a1", {"permLevel": ""})
    ok("清空 = 跟随面板", out.get("permLevel") == "", repr(out))
    out = ca.update_agent("a1", {"engine": "eng2"})
    ok("换大脑仍会重建桥（原有行为不变）", dropped["n"] == 1, repr(dropped))

    print("\ntotal %d, failed %d" % (PASS + FAIL, FAIL))
    if FAIL:
        print("failed:", BAD)
        return 1
    print("result: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
