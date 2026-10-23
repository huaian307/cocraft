# -*- coding: utf-8 -*-
r"""离线回归：Codex 的「碎碎念」按工具调用切段（backend/engines/acp/map_events.py）。

背景（2026-10-23 用户报）：
  Codex 会边干活边说（"还没完成…我改用…"），以前所有 `agent_message_chunk`
  被 `"".join()` 拼成**一大段正文**，最后一口气糊在回答里。
  现在：**文字 → 工具 = 一段结束**；`message()` 输出多个 text part，
  前端把前面的收进「过程」折叠块，最后一段才是最终回答。

断言：
  * 文字/工具交替 → 多个 text part，顺序不变；
  * 只有文字 → 一段；
  * 同一个工具的 `tool_call_update` 不再切段；
  * `text_parts` 仍然保留完整流水（给权限背景 / 日志用）。

跑法：`python tools\chatter_split_test.py`（纯离线）
"""
from __future__ import annotations

import os
import sys

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from engines.acp.map_events import AssistantTurn   # noqa: E402

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


def text_of(update):
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": update}}


def main() -> int:
    print("== 1. 文字 → 工具 → 文字 → 工具 → 文字 ==")
    t = AssistantTurn("s1", "m1", agent="codex-node", model="deepseek-flash")
    t.apply(text_of("我先看看环境。"))
    t.apply({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Run command",
             "rawInput": {"command": "dir"}, "status": "completed"})
    t.apply(text_of("再下载模型……"))
    t.apply({"sessionUpdate": "tool_call", "toolCallId": "t2", "title": "Run command",
             "rawInput": {"command": "curl"}})
    t.apply({"sessionUpdate": "tool_call_update", "toolCallId": "t2", "status": "running",
             "rawInput": {"command": "curl"}})
    t.apply(text_of("最终：图已经生成好了。"))
    t.completed = 123
    m = t.message()
    texts = [p.get("text") for p in (m.get("content") or []) if p.get("type") == "text"]
    ok("切成 3 段（不是一大坨）", texts == ["我先看看环境。", "再下载模型……", "最终：图已经生成好了。"],
       repr(texts))
    ok("顺序不变（最终在最后）", texts[-1].startswith("最终"), repr(texts[-1] if texts else ""))
    ok("工具调用都在（2 个）",
       len([p for p in (m.get("content") or []) if p.get("type") == "tool"]) == 2)
    ok("agent 还是 codex-node", m.get("agent") == "codex-node", repr(m.get("agent")))

    print("\n== 2. 只有文字 → 一段 ==")
    t2 = AssistantTurn("s1", "m2", agent="codex-node")
    t2.apply(text_of("第一句"))
    t2.apply(text_of("第二句"))
    t2.completed = 200
    texts2 = [p.get("text") for p in (t2.message().get("content") or []) if p.get("type") == "text"]
    ok("文字连续不切段", texts2 == ["第一句第二句"], repr(texts2))

    print("\n== 3. 工具更新不切段 / 兼容字段 ==")
    t3 = AssistantTurn("s1", "m3", agent="codex-node")
    t3.apply(text_of("看一下"))
    t3.apply({"sessionUpdate": "tool_call", "toolCallId": "x", "title": "Run", "rawInput": {}})
    t3.apply({"sessionUpdate": "tool_call_update", "toolCallId": "x", "status": "completed"})
    t3.apply(text_of("好了"))
    texts3 = [p.get("text") for p in (t3.message().get("content") or []) if p.get("type") == "text"]
    ok("tool_call_update 不会多切一段", texts3 == ["看一下", "好了"], repr(texts3))
    ok("text_parts 仍是完整流水（权限背景/日志用）",
       "".join(t3.text_parts) == "看一下好了", repr(t3.text_parts))

    print("\n== 4. 空段不输出 ==")
    t4 = AssistantTurn("s1", "m4", agent="codex-node")
    t4.apply({"sessionUpdate": "tool_call", "toolCallId": "y", "title": "Run", "rawInput": {}})
    t4.apply(text_of("只有最终"))
    t4.completed = 300
    texts4 = [p.get("text") for p in (t4.message().get("content") or []) if p.get("type") == "text"]
    ok("工具前面没文字 → 不产生空 text part", texts4 == ["只有最终"], repr(texts4))

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
