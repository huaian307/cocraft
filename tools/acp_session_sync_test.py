# -*- coding: utf-8 -*-
r"""无头回归：★ACP 会话同步 —— **在 OpenCode 客户端里删掉的会话，别在 ACP 里继续留着**。

背景：`opencode-acp` 与桌面端**共用同一个 SQLite**（`~/.local/share/opencode/opencode.db`），
删除动作不会通知我们；以前本地 `runtime/state/_acp_sessions.json` 会一直留着那条记录，
于是 ACP 会话列表里"删了还在"。

本测试**不碰真实库 / 真实状态**：把 `STATE_DIR` 指到临时目录、把 `_opencode_session_ids()`
换成内存集合，验证：

  1. 库里没有的 `ses_…` 会话 → 从本地删掉（并落盘）
  2. 库里还在的（`agentSessionId` 命中 / id 命中）→ 保留
  3. 本地草稿（没绑过 agent、id 不是 `ses_`）→ **不碰**（可能是本地唯一的历史）
  4. 库读不到（None）→ **一条都不删**
  5. 非 opencode-acp 的 agent（codex 等）→ 不参与同步
  6. `list_sessions()` 会顺带对账（节流），不用等重启
  7. `_opencode_session_ids()`：库不存在 → None；有 `session_v2` → 真集合

跑法：python tools\acp_session_sync_test.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from engines.acp import service as svc_mod  # noqa: E402

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


def make_session(sid, agent="opencode-acp", asid=None, msgs=1):
    return {"id": sid, "title": "t-" + sid,
            "agent": agent,
            "agentSessionId": sid if asid is None else asid,
            "location": {"directory": "D:/ws"},
            "time": {"created": 1, "updated": 1},
            "messages": [{"id": "m%d" % i, "type": "user", "text": "x"}
                         for i in range(msgs)]}


def write_state(tmp, sessions):
    p = os.path.join(tmp, "_acp_sessions.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"sessions": sessions}, fh, ensure_ascii=False)
    return p


def read_state(tmp):
    p = os.path.join(tmp, "_acp_sessions.json")
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh).get("sessions") or []
    except Exception:  # noqa: BLE001
        return []


def ids_of(svc):
    return {str(s.get("id")) for s in svc.list_sessions()}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="acp-sync-")
    old_state = svc_mod.STATE_DIR
    old_ids_fn = svc_mod._opencode_session_ids
    old_db = svc_mod._OPENCODE_DB
    svc_mod.STATE_DIR = tmp
    try:
        print("== 1. 库里有 / 没有 ==")
        write_state(tmp, [
            make_session("ses_keep_a"),                       # 库里有
            make_session("ses_gone_b"),                       # 库里没有 → 应删
            make_session("acp_r1", asid="ses_keep_c"),        # 本地 id + 有效 agent id
            make_session("acp_draft", asid=""),               # 本地草稿 → 不动
            make_session("ses_other", agent="codex-node"),    # 别的 agent → 不归它管
        ])
        svc_mod._opencode_session_ids = lambda: {"ses_keep_a", "ses_keep_c"}
        svc = svc_mod.AcpService(["opencode-cli.exe", "acp"], agent_id="opencode-acp")
        got = ids_of(svc)
        ok("库里没有的 ses_gone_b 被清掉", "ses_gone_b" not in got, repr(got))
        ok("库里还在的保留（ses_keep_a / acp_r1→ses_keep_c 归一后仍在）",
           "ses_keep_a" in got and "ses_keep_c" in got, repr(got))
        ok("本地草稿（没绑 agent）不碰", "acp_draft" in got, repr(got))
        ok("别的 agent 的会话不归它管（也没被删）",
           "ses_other" in {str(s.get("id")) for s in read_state(tmp)}, repr(read_state(tmp)))
        disk = {str(s.get("id")) for s in read_state(tmp)}
        ok("已经从盘里清掉（不是只从内存删）", "ses_gone_b" not in disk, repr(disk))

        print("\n== 2. 库读不到 → 一条都不删 ==")
        write_state(tmp, [make_session("ses_keep_a"), make_session("ses_gone_b")])
        svc_mod._opencode_session_ids = lambda: None
        svc2 = svc_mod.AcpService(["opencode-cli.exe", "acp"], agent_id="opencode-acp")
        got2 = ids_of(svc2)
        ok("读不到库就保持原样（宁可留着，不能误删）",
           got2 == {"ses_keep_a", "ses_gone_b"}, repr(got2))

        print("\n== 3. 非 opencode agent 不参与 ==")
        write_state(tmp, [make_session("ses_gone_b", agent="codex-node")])
        svc_mod._opencode_session_ids = lambda: set()
        svc3 = svc_mod.AcpService(["codex-acp.exe"], agent_id="codex-node")
        ok("codex 的会话不拿 OpenCode 的库去对账",
           "ses_gone_b" in ids_of(svc3), repr(ids_of(svc3)))

        print("\n== 3b. 假 agent（路径含 opencode-ui）不能被当成 OpenCode ==")
        fake = svc_mod.AcpService([sys.executable, os.path.join(BACKEND, "acp_mock_agent.py")],
                                  agent_id="mock")
        ok("参数里只有 acp_mock_agent.py → 不是 OpenCode（否则会误删会话）",
           fake._is_opencode() is False)
        ok("真 opencode-cli.exe acp → 是", svc_mod.AcpService(
            [r"D:\x\opencode-cli.exe", "acp"], agent_id="opencode-acp")._is_opencode() is True)
        ok("npm 的 opencode-ai 包 + acp → 是", svc_mod.AcpService(
            ["npx", "opencode-ai", "acp"], agent_id="x")._is_opencode() is True)
        ok("codex-acp.exe（没有 acp 子命令）→ 不是", svc_mod.AcpService(
            [r"D:\x\codex-acp.exe"], agent_id="codex")._is_opencode() is False)

        print("\n== 4. list_sessions() 会顺带对账（不用重启） ==")
        write_state(tmp, [make_session("ses_keep_a"), make_session("ses_gone_b")])
        svc_mod._opencode_session_ids = lambda: {"ses_keep_a"}
        svc4 = svc_mod.AcpService(["opencode-cli.exe", "acp"], agent_id="opencode-acp")
        ok("启动时已经删过一轮", "ses_gone_b" not in ids_of(svc4))
        # 模拟：启动时全在，随后在 OpenCode 客户端里删掉 b
        write_state(tmp, [make_session("ses_keep_a"), make_session("ses_gone_b")])
        state_ids = {"ses_keep_a", "ses_gone_b"}
        svc_mod._opencode_session_ids = lambda: set(state_ids)
        svc5 = svc_mod.AcpService(["opencode-cli.exe", "acp"], agent_id="opencode-acp")
        ok("对账前两条都在", ids_of(svc5) == {"ses_keep_a", "ses_gone_b"}, repr(ids_of(svc5)))
        state_ids.discard("ses_gone_b")                # ← 外部删除
        svc5._prune_at = 0                            # 跳过节流（真实场景 5 秒后自然触发）
        got5 = ids_of(svc5)
        ok("再列一次会话 → 被外部删掉的那条消失",
           got5 == {"ses_keep_a"}, repr(got5))

        print("\n== 5. _opencode_session_ids()：库不存在 / 有表 ==")
        svc_mod._opencode_session_ids = old_ids_fn     # 前面打了桩，这里要测真的
        svc_mod._OPENCODE_DB = os.path.join(tmp, "nope.db")
        ok("库不存在 → None（不是空集合）", svc_mod._opencode_session_ids() is None)
        db = os.path.join(tmp, "fake.db")
        con = sqlite3.connect(db)
        con.execute("create table session_v2 (id text)")
        con.executemany("insert into session_v2 values (?)", [("ses_x",), ("ses_y",)])
        con.commit()
        con.close()
        svc_mod._OPENCODE_DB = db
        got_db = svc_mod._opencode_session_ids()
        ok("有 session_v2 → 真集合", got_db == {"ses_x", "ses_y"}, repr(got_db))
        # 表不存在 → None（老库 / 结构变了）
        db2 = os.path.join(tmp, "old.db")
        con = sqlite3.connect(db2)
        con.execute("create table other (id text)")
        con.commit()
        con.close()
        svc_mod._OPENCODE_DB = db2
        ok("表结构不对 → None（不误删）", svc_mod._opencode_session_ids() is None)
    finally:
        svc_mod.STATE_DIR = old_state
        svc_mod._opencode_session_ids = old_ids_fn
        svc_mod._OPENCODE_DB = old_db
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
