# -*- coding: utf-8 -*-
"""ACP 假 agent —— 开发/联调用的最小 ACP v1 实现（不是产品运行时）。

用途：在没有任何外部 agent（Codex / Claude / dsh）的情况下，验证我们的 ACP
桥路（分帧、双向请求、事件映射、弹窗、「始终允许」记忆、session/list 导入）。
配合 tools/acp_demo.py 一键起面板，或 tools/acp_events_test.py 做无头回归。

它按 ACP v1 说 JSONL/JSON-RPC，一轮完整的 prompt 会覆盖：
   1) plan / plan_update（agent 的计划）
   2) session_info_update（给会话起真名）
   3) usage_update（上下文窗口占用）
   4) agent_message_chunk / agent_thought_chunk
   5) tool_call(in_progress) + tool_call_update(_meta.terminal_output_delta 增量输出)
   6) **反向请求** session/request_permission（带 allow_always 选项）→ 等客户端回复
   7) 回复后：tool_call_update(completed) + elicitation/create(form)
   8) 表单回复后：agent_message_chunk("完成。") → 结束本轮，
      结果带 usage（账单口径 token）与 stopReason
另外支持：session/list（两条已有会话，其中一条"已导入"）、session/resume|load、
session/close|delete。对未知的带 id 请求**故意不回**（用于验证客户端超时）。
"""
import json
import os
import sys
import time

# ACP 的 JSONL 必须是 UTF-8；stdout 是管道时 Python 默认会用系统 ANSI 代码页（GBK）→ 乱码
for _s in (sys.stdin, sys.stdout):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

STATE = {"sid": "", "prompt_id": None, "client_caps": {}, "turn": 0}
# session/list 用的三条"agent 侧已有会话"（第三条是**内部会话**：群聊 / 本地协作那种）
REMOTE = [
    {"sessionId": "remote_alpha", "cwd": "D:/agentlist/shared", "title": "整理共享工作区",
     "updatedAt": "2026-09-26T02:10:00Z"},
    {"sessionId": "remote_beta", "cwd": "D:/agentlist/shared", "title": "",
     "updatedAt": "2026-09-25T23:00:00Z"},
    {"sessionId": "remote_internal", "cwd": "D:/agentlist/shared",
     "title": "【你的角色：实现者】只回复 OK 两个字母",
     "updatedAt": "2026-10-20T08:00:00Z"},
]

MODEL_OPTS = {"id": "model", "name": "Model", "category": "model", "type": "select",
              "currentValue": "mock-fast",
              "options": [{"value": "mock-fast", "name": "mock-fast"},
                          {"value": "mock-deep", "name": "mock-deep"}]}
EFFORT_OPTS = {"id": "effort", "name": "Effort", "category": "model", "type": "select",
               "currentValue": "low",
               "options": [{"value": "low", "name": "low"}, {"value": "high", "name": "high"}]}
MODES = {"currentModeId": "read-only",
         "availableModes": [{"id": "read-only", "name": "只读（问面板）"},
                            {"id": "auto", "name": "自动"}]}


def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def respond(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def notify(update, sid=None):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid or STATE["sid"], "update": update}})


def chunk(text, kind="agent_message_chunk"):
    notify({"sessionUpdate": kind, "content": {"type": "text", "text": text}})


def session_config():
    return {"configOptions": [MODEL_OPTS, EFFORT_OPTS], "modes": MODES}


while True:
    line = sys.stdin.readline()
    if not line:
        break
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue

    method = msg.get("method")
    rid = msg.get("id")

    if method == "initialize":
        STATE["client_caps"] = (msg.get("params") or {}).get("clientCapabilities") or {}
        respond(rid, {"protocolVersion": 1,
                      "agentCapabilities": {"loadSession": True,
                                            "promptCapabilities": {"image": False},
                                            "sessionCapabilities": {"resume": {}, "load": {},
                                                                    "list": {}, "close": {}, "delete": {}}},
                      "agentInfo": {"name": "acp-mock", "title": "ACP Mock", "version": "0.0.3"},
                      "authMethods": []})
    elif method == "session/new":
        STATE["sid"] = "sess_mock_1"
        # ★ 真实 agent（opencode-acp / codex）会在 `session/new` **返回之前**就发
        #   available_commands_update —— 那会儿客户端还没登记会话。这里照样模拟，
        #   用来锁住"命令不能被丢"（见 service._on_notification 的提前分支）。
        notify({"sessionUpdate": "available_commands_update",
                "availableCommands": [{"name": "mock-cmd", "description": "mock slash command"},
                                      {"name": "init", "description": "guided setup"}]},
               sid="sess_mock_1")
        res = {"sessionId": "sess_mock_1"}
        res.update(session_config())
        respond(rid, res)
    elif method in ("session/resume", "session/load"):
        res = session_config()
        if method == "session/resume":
            res["resumed"] = True
        respond(rid, res)
    elif method == "session/list":
        respond(rid, {"sessions": REMOTE, "nextCursor": None})
    elif method in ("session/close", "session/delete"):
        respond(rid, {})
    elif method == "session/set_mode":
        MODES["currentModeId"] = (msg.get("params") or {}).get("modeId") or MODES["currentModeId"]
        respond(rid, {})
    elif method == "session/set_config_option":
        respond(rid, {"configOptions": [MODEL_OPTS, EFFORT_OPTS]})
    elif method == "session/prompt":
        STATE["prompt_id"] = rid
        STATE["turn"] += 1
        turn = STATE["turn"]
        # ★ 本地协作测试用的「简单模式」：不弹权限/表单，直接回一句（可加延迟测取消）
        if os.environ.get("ACP_MOCK_SIMPLE") == "1":
            try:
                delay = float(os.environ.get("ACP_MOCK_DELAY") or 0)
            except Exception:  # noqa: BLE001
                delay = 0.0
            if delay > 0:
                time.sleep(delay)
            chunk(os.environ.get("ACP_MOCK_TEXT") or ("MOCK-REPLY %d" % turn))
            notify({"sessionUpdate": "usage_update", "used": 1200, "size": 128000})
            respond(STATE["prompt_id"], {"stopReason": "end_turn",
                                         "usage": {"totalTokens": 1250, "inputTokens": 1200,
                                                   "outputTokens": 50, "thoughtTokens": 0,
                                                   "cachedReadTokens": 0}})
            STATE["prompt_id"] = None
            continue
        # ① 计划：先给一份 items，再追加一条 markdown
        notify({"sessionUpdate": "plan", "entries": [
            {"content": "读文件", "status": "in_progress", "priority": "medium"}]})
        notify({"sessionUpdate": "plan_update",
                "plan": {"type": "markdown", "planId": "p1", "content": "1) 读文件 2) 跑测试"}})
        # ② 会话真名（前端自动命名过也能被顶掉，用户改过的不会）
        if turn == 1:
            notify({"sessionUpdate": "session_info_update", "title": "假 agent 的正名"})
        # ③ 上下文窗口占用（不是账单 token）
        notify({"sessionUpdate": "usage_update", "used": 12000, "size": 128000})
        chunk("你好，")
        chunk("思考中…", kind="agent_thought_chunk")
        notify({"sessionUpdate": "tool_call", "toolCallId": "tc1", "name": "run_tests",
                "title": "运行测试", "kind": "execute", "status": "in_progress",
                "rawInput": {"cmd": "pytest"}})
        # 运行中输出：Codex 风格（_meta.terminal_output_delta 增量）
        notify({"sessionUpdate": "tool_call_update", "toolCallId": "tc1",
                "_meta": {"terminal_output_delta": {"data": "collecting ... 12 passed\n",
                                                    "terminal_id": "tc1"}}})
        send({"jsonrpc": "2.0", "id": 1000, "method": "session/request_permission",
              "params": {"sessionId": STATE["sid"],
                         "toolCall": {"toolCallId": "tc1", "title": "运行测试", "kind": "execute",
                                      "locations": [{"path": "D:/agentlist/shared"}]},
                         "options": [{"optionId": "allow-once", "name": "允许一次", "kind": "allow_once"},
                                     {"optionId": "allow-always", "name": "始终允许", "kind": "allow_always"},
                                     {"optionId": "reject-once", "name": "拒绝", "kind": "reject_once"}]}})
    elif method == "session/cancel":
        pass
    elif method == "boom":
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": "boom"}})
    elif rid is not None and ("result" in msg or "error" in msg):
        if rid == 1000:                      # 权限答复
            chosen = ((msg.get("result") or {}).get("outcome") or {}).get("optionId")
            notify({"sessionUpdate": "tool_call_update", "toolCallId": "tc1",
                    "status": "completed",
                    "rawOutput": {"formatted_output": "collecting ... 12 passed\n",
                                  "exit_code": 0}})
            if chosen != "reject-once":
                send({"jsonrpc": "2.0", "id": 2000, "method": "elicitation/create",
                      "params": {"sessionId": STATE["sid"], "mode": "form",
                                 "message": "选择重构策略",
                                 "requestedSchema": {"type": "object",
                                                     "properties": {"strategy": {
                                                         "type": "string",
                                                         "enum": ["conservative", "balanced", "aggressive"],
                                                         "title": "策略"}},
                                                     "required": ["strategy"]}}})
            else:
                chunk("好的，已拒绝。")
                respond(STATE["prompt_id"], {"stopReason": "end_turn",
                                             "usage": {"totalTokens": 12050, "inputTokens": 12000,
                                                       "outputTokens": 50, "thoughtTokens": 0,
                                                       "cachedReadTokens": 9000}})
                STATE["prompt_id"] = None
        elif rid == 2000:                    # 表单答复
            chunk("完成。")
            notify({"sessionUpdate": "usage_update", "used": 12100, "size": 128000})
            respond(STATE["prompt_id"], {"stopReason": "end_turn",
                                         "usage": {"totalTokens": 12100, "inputTokens": 12000,
                                                   "outputTokens": 100, "thoughtTokens": 0,
                                                   "cachedReadTokens": 9000}})
            STATE["prompt_id"] = None
    # 其它带 id 的请求：故意不回
