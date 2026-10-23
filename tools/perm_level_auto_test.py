# -*- coding: utf-8 -*-
r"""离线回归：ACP 权限的「面板等级」自动放行（后端做，不依赖前端轮询）。

为什么要有它（2026-10-23 用户报「有时候询问不会给回答」）：
  前端只轮询**当前会话**；切到别的会话 / 页面关掉 / 刷新时，权限申请没人应答 →
  等到 ANSWER_TIMEOUT（30 分钟）→ 用户看到"没反应"。
  现在后端在 `_ask_permission` 里按同一份 `_perm_level.json` 自动放行（破坏性永不自动），
  即使 UI 不在也能继续。

断言：
  * level=all + 普通命令 → 立刻 allow_once；
  * level=all + **破坏性命令** → **不自动**（按超时走 reject）；
  * level=ask → 不自动；
  * level=read + 纯读 → 自动；level=edit + 区内写 → 自动；区外 / 执行 / 无路径 → 不自动；
  * level=all + 联网命令 → 自动。

跑法：`python tools\perm_level_auto_test.py`（不联网、不碰真实 state）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from engines.acp import service as svc_mod      # noqa: E402
import cocraft_agents as ca                     # noqa: E402

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
    tmp = tempfile.mkdtemp(prefix="cocraft-permlevel-")
    svc_mod.STATE_DIR = tmp
    svc_mod._ALWAYS_FILE = os.path.join(tmp, "_acp_always.json")
    svc_mod.ANSWER_TIMEOUT = 0.3                   # 测试里别真等 30 分钟
    ca.PERM_LEVEL_FILE = os.path.join(tmp, "_perm_level.json")

    svc = svc_mod.AcpService(["cmd"], agent_id="test")
    svc._sessions["s1"] = {"id": "s1", "location": {"directory": "D:/ws"}, "messages": []}

    opts = [
        {"optionId": "allow_once", "kind": "allow_once", "name": "Yes"},
        {"optionId": "accept_execpolicy_amendment", "kind": "allow_always", "name": "Always"},
        {"optionId": "cancel", "kind": "reject_once", "name": "No"},
    ]

    def set_level(lv):
        with io.open(ca.PERM_LEVEL_FILE, "w", encoding="utf-8") as fh:
            json.dump({"level": lv}, fh)

    def ask(level, kind="execute", title="Run command", cmd="echo hi", paths=None):
        set_level(level)
        tc = {"kind": kind, "title": title, "rawInput": {"command": cmd} if cmd else {},
              "locations": [{"path": p} for p in (paths or [])]}
        t0 = time.time()
        r = svc._ask_permission({"sessionId": "s1", "toolCall": tc, "options": opts})
        return r, round(time.time() - t0, 2)

    def allowed(r):
        return ((r or {}).get("outcome") or {}).get("optionId") == "allow_once"

    try:
        print("== 1. level=all：普通命令自动放行 ==")
        r, dt = ask("all", cmd="curl https://example.com/zen")
        ok("联网命令（非破坏性）自动放行", allowed(r) and dt < 0.2, "%r / %.2fs" % (r, dt))
        r, dt = ask("all", cmd="echo hi")
        ok("普通命令自动放行（不用等 UI）", allowed(r) and dt < 0.2, "%r / %.2fs" % (r, dt))

        print("\n== 2. 破坏性永不自动（即使 level=all）==")
        r, dt = ask("all", cmd="Remove-Item -Recurse -Force D:\\ws\\dist")
        ok("破坏性命令不自动放行", not allowed(r) and dt >= 0.25, "%r / %.2fs" % (r, dt))

        print("\n== 3. level=ask：全部要问 ==")
        r, dt = ask("ask", cmd="echo hi")
        ok("ask 下不自动", not allowed(r) and dt >= 0.25, "%r / %.2fs" % (r, dt))

        print("\n== 4. level=read：只放纯读 ==")
        r, dt = ask("read", kind="read", title="Read file", cmd="", paths=["D:/ws/a.txt"])
        ok("纯读自动放行", allowed(r) and dt < 0.2, "%r / %.2fs" % (r, dt))
        r, dt = ask("read", kind="execute", title="Run command", cmd="echo hi")
        ok("read 下命令不自动", not allowed(r) and dt >= 0.25, "%r / %.2fs" % (r, dt))

        print("\n== 5. level=edit：区内写自动、区外/执行不自动 ==")
        r, dt = ask("edit", kind="edit", title="Edit file", cmd="", paths=["D:/ws/a.txt"])
        ok("区内写自动放行", allowed(r) and dt < 0.2, "%r / %.2fs" % (r, dt))
        r, dt = ask("edit", kind="edit", title="Edit file", cmd="", paths=["D:/other/a.txt"])
        ok("区外写不自动", not allowed(r) and dt >= 0.25, "%r / %.2fs" % (r, dt))
        r, dt = ask("edit", kind="execute", title="Run command", cmd="echo hi", paths=["D:/ws/a.txt"])
        ok("edit 下执行不自动", not allowed(r) and dt >= 0.25, "%r / %.2fs" % (r, dt))

        print("\n== 6. level=exec：无路径 → 范围未知，不自动 ==")
        r, dt = ask("exec", kind="execute", title="Run command", cmd="echo hi", paths=[])
        ok("没给路径不自动（fail-closed）", not allowed(r) and dt >= 0.25, "%r / %.2fs" % (r, dt))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
