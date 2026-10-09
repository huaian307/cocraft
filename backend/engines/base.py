# -*- coding: utf-8 -*-
"""引擎接口与通用常量。"""
from __future__ import annotations

# 逐跳首部，不能端到端转发（原来定义在 server.py；现在 server 与引擎共用这一份）
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


class EngineError(Exception):
    """引擎把「已知错误」抛给 HTTP 层：`code` = 建议的 HTTP 状态码。

    用途：技能 / 命令这类面板统一接口由 server.py 调用引擎方法，引擎用它与
    「意料之外的异常（500）」区分开（比如 404 找不到技能、409 会话正忙）。
    """
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = int(code)
        self.message = str(message)


class Engine:
    """一个可替换的 agent 后端。

    实现者直接往 HTTP handler 上写响应，语义必须和改造前的 OpenCode 反代一致
    —— 这样前端完全不需要知道背后换了谁。
    """

    id = "base"
    label = "Base"

    # ---- 生命周期 ----

    def available(self) -> bool:
        """引擎依赖是否就绪（例如 OpenCode 的状态文件是否存在）。"""
        return False

    def read_service(self):
        """返回 (url, auth_header_or_None, version)。未实现则抛异常。"""
        raise NotImplementedError

    # ---- HTTP ----

    def status(self) -> dict:
        """给 GET /engine/status 用的自述。"""
        return {"id": self.id, "label": self.label, "available": self.available()}

    def healthz(self):
        """返回 (http_code, body_dict)。server.py 会再补上 ui 版本号。"""
        raise NotImplementedError

    def handle_api(self, handler, method: str) -> None:
        """实现 /api/* 的语义（handler 是 BaseHTTPRequestHandler）。"""
        raise NotImplementedError

    # ---- 技能 / 斜杠命令（面板统一语义）----
    #
    # 背景：OpenCode 原生有 skill（`/api/skill` + `/api/experimental/session/{id}/skill`），
    # 而 ACP 协议**没有** skill —— 由我们自己扫文件系统发现 + 把 SKILL.md 正文注入会话。
    # 这里把两者抽象成同一组方法，server.py 只认这一组，前端不用管背后是谁。

    def list_skills(self, directory: str = None) -> dict:
        """列出技能：`{location:{directory}, data:[{id,name,description,autoinvoke,source,path}]}`。

        ⚠ 列表**不带正文**（`content`）：省流量，正文走 `skill_detail()`。
        """
        raise NotImplementedError

    def skill_detail(self, skill_id: str, directory: str = None):
        """单个技能详情（含 `content` 正文）；找不到回 None。"""
        raise NotImplementedError

    def list_commands(self, directory: str = None) -> dict:
        """列出斜杠命令：`{location:{directory}, data:[{name,description}]}`。"""
        raise NotImplementedError

    def activate_skill(self, sid: str, skill_id: str, resume: bool = False) -> dict:
        """把技能注入会话。默认**只注入**（`resume=False`），什么时候跑交给用户下一条消息。

        失败请抛 `EngineError`（找不到技能 → 404）。
        """
        raise NotImplementedError

    def run_command(self, sid: str, name: str, text: str = "", files=None) -> dict:
        """执行一个斜杠命令。失败请抛 `EngineError`（会话不存在 → 404、忙 → 409）。"""
        raise NotImplementedError
