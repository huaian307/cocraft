# -*- coding: utf-8 -*-
"""ACP session/update → 前端（OpenCode 形状）事件与消息。

纯逻辑，不碰进程/网络，方便单测。前端的消费点（见 frontend/app.js）：
  SSE 事件   {"type": "...", "data": {...}}
    session.step.started   {sessionID, assistantMessageID, agent, model:{id}, started}
    session.text.delta     {sessionID, assistantMessageID, ordinal, delta}
    session.reasoning.delta 同上
    session.tool.called    {sessionID, assistantMessageID, name, input, toolCallId}
    session.tool.progress  {..., toolCallId, delta, output}   ← 工具运行中的输出（stdout/stderr）
    session.tool.success / .error  {sessionID, assistantMessageID, toolCallId}
    session.plan.update    {..., plan}          ← agent 的计划（todo）列表
    session.usage.update   {..., used, size}    ← 上下文窗口占用（不是账单 token）
  权威消息（GET /message）：
    {id, type:"assistant", agent, model:{id}, time:{created,completed?},
     content:[{type:"text",text}|{type:"reasoning",text}|{type:"tool",name,state:{status,input,output}}]}

⚠ 计划（plan）**不进 content**：前端对未知 part 只会兜底显示原始 JSON，很难看。
  它走 SSE 事件 + 会话字段（`session["plan"]`），等前端接了专门的计划面板再上屏。
"""
from __future__ import annotations

import json
import time


def now_ms() -> int:
    return int(time.time() * 1000)


def content_text(content):
    """ACP 的 content 可能是块、块数组或字符串；只取其中的文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if content.get("type") == "text":
            return content.get("text") or ""
        return ""
    if isinstance(content, list):
        return "".join(content_text(c) for c in content)
    return ""


def normalize_output(raw):
    """工具输出统一成**字符串**（前端 `String(out)` 直接显示）。

    ⚠ Codex 的 `rawOutput` 是对象（`{formatted_output, exit_code}`）；以前原样丢给前端，
      面板里就是一行 "[object Object]"（工具输出全白读）。这里挑给人看的字段。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for k in ("formatted_output", "formattedOutput", "output", "text",
                  "stdout", "content", "result", "diff"):
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                tail = []
                for k2 in ("exit_code", "exitCode", "status", "truncated"):
                    v2 = raw.get(k2)
                    # ⚠ 不能写 `v2 not in (None, "", False)`：Python 里 0 == False 为真，
                    #   exit_code=0 会被当成"空值"丢掉（这个坑真踩过）。
                    if v2 is None or v2 is False or v2 == "":
                        continue
                    tail.append("%s=%s" % (k2, v2))
                return v + (("\n[" + " · ".join(tail) + "]") if tail else "")
        try:
            return json.dumps(raw, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            return str(raw)
    return str(raw)


def meta_progress(update) -> str:
    """从 `_meta` 里取工具的运行中输出（Codex 走这里，标准 `content` 只在别的适配器用）。

    Codex（见 @agentclientprotocol/codex-acp 源码）：
      `_meta.terminal_output`       = **全量**输出（覆盖）
      `_meta.terminal_output_delta` = 增量片段（追加）
      `_meta.mcp_output_delta`      = MCP 工具日志片段（追加）
    返回 `("文本", 是否全量覆盖)`。
    """
    meta = update.get("_meta") if isinstance(update, dict) else None
    if not isinstance(meta, dict):
        return "", False
    full = meta.get("terminal_output")
    if isinstance(full, dict) and isinstance(full.get("data"), str) and full["data"]:
        return full["data"], True
    for key in ("terminal_output_delta", "mcp_output_delta"):
        d = meta.get(key)
        if isinstance(d, dict) and isinstance(d.get("data"), str) and d["data"]:
            return d["data"], False
    return "", False


class AssistantTurn:
    """一轮助手回答：累积增量，产出「实时事件」与「权威消息」。"""

    def __init__(self, session_id, message_id, agent="", model=""):
        self.session_id = session_id
        self.id = message_id
        self.agent = agent
        self.model = model
        self.started = now_ms()
        self.completed = None
        self.aborted = False
        self.text_parts = []
        self.text_segments = []        # ★按工具调用切段：Codex 的「碎碎念」每段单独一条
        self.reason_parts = []
        self.tools = []
        self.plan = None            # agent 的计划（todo）；不进 content，走事件
        self.plan_id = ""
        self.context = None         # 上下文窗口占用 {used, size}
        self._tool_index = {}
        self._text_seq = 0
        self._reason_seq = 0

    # ---- 增量 → 事件 ----
    def _delta(self, kind, text):
        if not text:
            return None
        if kind == "text":
            seq = self._text_seq
            self._text_seq += 1
            self.text_parts.append(text)
            if not self.text_segments:
                self.text_segments.append("")
            self.text_segments[-1] += text
            etype = "session.text.delta"
        else:
            seq = self._reason_seq
            self._reason_seq += 1
            self.reason_parts.append(text)
            etype = "session.reasoning.delta"
        return {"type": etype, "data": {"sessionID": self.session_id,
                "assistantMessageID": self.id, "ordinal": seq, "delta": text}}

    def _ev(self, etype, **data):
        data.setdefault("sessionID", self.session_id)
        data.setdefault("assistantMessageID", self.id)
        return {"type": etype, "data": data}

    def _finish_tool(self, tcid, status):
        i = self._tool_index.get(tcid)
        if i is None:
            return []
        if status == "completed":
            self.tools[i]["status"] = "completed"
            return [self._ev("session.tool.success", toolCallId=tcid,
                             name=self.tools[i]["name"])]
        self.tools[i]["status"] = "error"
        return [self._ev("session.tool.error", toolCallId=tcid,
                         name=self.tools[i]["name"])]

    def _plan_update(self, update, kind):
        """计划（todo）：`plan` 给整份 entries，`plan_update` 给增量（items/markdown/file）。"""
        plan_id = update.get("planId") or ""
        entries, text = None, None
        if kind == "plan":
            entries = update.get("entries") or []
        else:
            p = update.get("plan") or {}
            if not isinstance(p, dict):
                return []
            plan_id = p.get("planId") or plan_id
            t = p.get("type")
            if t == "items":
                entries = p.get("entries") or []
            elif t == "markdown":
                text = p.get("content") or ""
            elif t == "file":
                text = "计划文件：%s" % (p.get("uri") or "")
        if plan_id:
            self.plan_id = str(plan_id)
        if entries is not None:
            self.plan = {"entries": [{"content": str(e.get("content") or ""),
                                      "status": str(e.get("status") or ""),
                                      "priority": str(e.get("priority") or "")}
                                     for e in entries if isinstance(e, dict)]}
        elif text is not None:
            self.plan = {"markdown": str(text)}
        return [self._ev("session.plan.update", planId=self.plan_id, plan=self.plan)]

    def break_text_segment(self) -> None:
        """工具调用开始时切一段：之前的文字算「碎碎念」，之后的算下一段（最终回答通常最后一段）。"""
        if self.text_segments and self.text_segments[-1].strip():
            self.text_segments.append("")

    def apply(self, update) -> list:
        kind = (update or {}).get("sessionUpdate")
        if kind == "agent_message_chunk":
            ev = self._delta("text", content_text(update.get("content")))
            return [ev] if ev else []
        if kind == "agent_thought_chunk":
            ev = self._delta("reasoning", content_text(update.get("content")))
            return [ev] if ev else []
        if kind == "compaction_summary_chunk":
            # 上下文压缩的摘要：丢进思考里比整段丢掉强
            ev = self._delta("reasoning", content_text(update.get("content")))
            return [ev] if ev else []
        if kind == "tool_call":
            self.break_text_segment()          # ★文字 → 工具 = 一段结束
            tcid = update.get("toolCallId") or ("t%d" % len(self.tools))
            name = update.get("title") or update.get("name") or update.get("kind") or "tool"
            out = normalize_output(update.get("rawOutput"))
            if not out:
                out = content_text(update.get("content")) or None
            self._tool_index[tcid] = len(self.tools)
            self.tools.append({"id": tcid, "name": name, "input": update.get("rawInput"),
                               "kind": update.get("kind") or "",
                               "status": "running", "output": out, "chunks": []})
            evs = [self._ev("session.tool.called", name=name, input=update.get("rawInput"),
                            toolCallId=tcid, kind=update.get("kind") or "",
                            status=update.get("status") or "running")]
            # ⚠ Codex 很多工具是「一次性」发 tool_call 且 status 直接 completed
            #   （读文件、web 搜索、上下文压缩…）。以前一律当 running →
            #   面板上工具永远转圈、也没有 success 事件。
            st = update.get("status")
            if st == "completed":
                evs += self._finish_tool(tcid, "completed")
            elif st in ("failed", "error"):
                evs += self._finish_tool(tcid, "error")
            return evs
        if kind == "tool_call_update":
            tcid = update.get("toolCallId")
            i = self._tool_index.get(tcid)
            if i is None:                       # 没见过这个 id → 当成一次新的工具调用
                merged = dict(update)
                merged["sessionUpdate"] = "tool_call"
                return self.apply(merged)
            evs = []
            if update.get("title"):
                self.tools[i]["name"] = str(update["title"])
            if update.get("kind"):
                self.tools[i]["kind"] = str(update["kind"])
            if update.get("rawInput") is not None:
                self.tools[i]["input"] = update.get("rawInput")
            out = normalize_output(update.get("rawOutput"))
            if out:                              # 终态全量输出：覆盖之前的片段
                self.tools[i]["chunks"] = [out]
                self.tools[i]["output"] = out
            # 运行中的输出：标准 content（dsh 等）或 Codex 的 _meta.*_delta
            chunk, full = content_text(update.get("content")), False
            if not chunk:
                chunk, full = meta_progress(update)
            if chunk:
                if full:
                    self.tools[i]["chunks"] = [chunk]
                else:
                    self.tools[i]["chunks"].append(chunk)
                self.tools[i]["output"] = "".join(self.tools[i]["chunks"])
                evs.append(self._ev("session.tool.progress", toolCallId=tcid,
                                    name=self.tools[i]["name"], delta=chunk,
                                    output=self.tools[i]["output"]))
            st = update.get("status")
            if st == "completed":
                evs += self._finish_tool(tcid, "completed")
            elif st in ("failed", "error"):
                evs += self._finish_tool(tcid, "error")
            return evs
        if kind in ("plan", "plan_update"):
            return self._plan_update(update, kind)
        if kind == "plan_removed":
            self.plan = None
            return [self._ev("session.plan.update", planId=update.get("planId") or self.plan_id,
                             plan=None)]
        if kind == "usage_update":
            # ⚠ 这是**上下文窗口**占用（used/size），不是账单 token —— 别拿它当费用。
            used = int(update.get("used") or 0)
            size = int(update.get("size") or 0)
            self.context = {"used": used, "size": size}
            return [self._ev("session.usage.update", used=used, size=size,
                             ratio=(round(used / size, 4) if size else None))]
        return []

    # ---- 权威消息 ----
    def message(self) -> dict:
        content = []
        if self.reason_parts:
            content.append({"type": "reasoning", "text": "".join(self.reason_parts)})
        segs = [s for s in (self.text_segments or []) if str(s).strip()]
        if not segs and self.text_parts:
            segs = ["".join(self.text_parts)]
        for seg in segs:
            content.append({"type": "text", "text": seg})
        for t in self.tools:
            content.append({"type": "tool", "name": t["name"],
                            "state": {"status": t["status"], "input": t["input"], "output": t["output"]}})
        m = {"id": self.id, "type": "assistant",
             "model": {"id": self.model} if self.model else {},
             "time": {"created": self.started},
             "content": content}
        if self.agent:
            m["agent"] = self.agent
        if self.completed:
            m["time"]["completed"] = self.completed
        if self.context:                 # 上下文窗口占用（前端暂不显示，留给以后的用量面板）
            m["context"] = self.context
        if self.plan:                    # 同上：计划挂在消息上，不进 content
            m["plan"] = self.plan
        return m


# ---------------- elicitation / permission 的字段转换 ----------------

_SCHEMA_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean"}


def schema_to_fields(schema) -> list:
    """把 elicitation 的 requestedSchema 转成前端 form 弹窗的 fields[]。"""
    schema = schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    fields = []
    for key, spec in props.items():
        spec = spec if isinstance(spec, dict) else {}
        options = None
        if isinstance(spec.get("enum"), list):
            ftype = "string"
            options = [{"value": str(v), "label": str(v)} for v in spec["enum"]]
        else:
            ftype = _SCHEMA_TYPES.get(spec.get("type"), "string")
        f = {"key": key, "title": spec.get("title") or key,
             "required": key in required, "type": ftype}
        if spec.get("description"):
            f["description"] = spec["description"]
        if options is not None:
            f["options"] = options
        if "default" in spec:
            f["default"] = spec["default"]
        fields.append(f)
    return fields


_KIND_PREF = {"once": ["allow_once"], "always": ["allow_always"],
              "reject": ["reject_once", "reject_always"]}


def pick_option_id(options, decision):
    """把前端的 once/always/reject 映射回 ACP 的 optionId。

    ⚠ 绝不跨方向兜底：用户点「拒绝」时找不到 reject 选项，只能回 None（→ 取消），
      绝不能退而选一个 allow 选项（那会把拒绝做成允许）。
    """
    options = [o for o in (options or []) if isinstance(o, dict)]
    allow = decision in ("once", "always")
    for want in _KIND_PREF.get(decision, []):
        for o in options:
            if o.get("kind") == want:
                return o.get("optionId")
    for o in options:                            # 兜底：只接受同向的 kind
        kind = str(o.get("kind", ""))
        if allow and kind.startswith("allow"):
            return o.get("optionId")
        if (not allow) and kind.startswith("reject"):
            return o.get("optionId")
    return None
