# -*- coding: utf-8 -*-
"""引擎接口与通用常量。"""
from __future__ import annotations

import os
import re
import urllib.parse

# 逐跳首部，不能端到端转发（原来定义在 server.py；现在 server 与引擎共用这一份）
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


def is_destructive(*texts) -> bool:
    """破坏性判定 —— 复用群聊那条（`cocraft_agents._RE_DESTRUCTIVE`）。

    ⚠ **延迟导入**：`cocraft_agents` 会拉起 ACP 服务（它自己又 import 引擎），
      在模块顶部 import 会成环。这里只在一个 `if` 里用到，代价可以忽略。
    """
    try:
        from cocraft_agents import _is_destructive        # noqa: PLC0415
        return bool(_is_destructive(*texts))
    except Exception:                                     # noqa: BLE001
        return False


# 修改类动作：ACP `kind` 与常见标题 / 命令。命中才值得在放行前存旧版。
_CHANGE_KINDS = {"edit", "write", "patch", "delete", "remove", "move", "rename", "create"}
_CHANGE_RE = re.compile(
    r"(edit|write|patch|modify|delete|remove|rename|move|create|save|修改|写入|删除|移除|重命名|移动|保存|新建)",
    re.I)


def is_change_action(kind="", title="", cmd="") -> bool:
    """这条权限请求是不是「会改动文件」的（编辑 / 写入 / 删除 / 移动…）。"""
    k = str(kind or "").strip().lower()
    if k in _CHANGE_KINDS:
        return True
    if _CHANGE_RE.search(str(title or "")):
        return True
    if re.search(r"(^|[\s;&|()])(rm|del|erase|rmdir|rd|move|mv|copy|cp|rename|ren|"
                 r"remove-item|move-item|copy-item|set-content|out-file|new-item)\b",
                 str(cmd or ""), re.I):
        return True
    try:
        return bool(is_destructive(cmd))
    except Exception:                                     # noqa: BLE001
        return False


def _loc_path(root: str, raw) -> str:
    """把权限请求里的 location 归一成绝对路径（支持 file:// 与相对路径）。"""
    s = str(raw or "").strip()
    if not s:
        return ""
    if s.startswith("file://"):
        try:
            p = urllib.parse.unquote(urllib.parse.urlparse(s).path or "")
            if re.match(r"^/[A-Za-z]:", p):
                p = p[1:]
            s = p
        except Exception:                                 # noqa: BLE001
            return ""
    if "://" in s:
        return ""
    if not os.path.isabs(s):
        base = str(root or "")
        if not base:
            return ""
        s = os.path.join(base, s)
    try:
        return os.path.realpath(s)
    except Exception:                                     # noqa: BLE001
        return ""


def capture_before_allow(root, cmd="", title="", locations=None, scope="panel",
                         note="", kind="") -> str:
    """★忏悔室（`ui 0.2.99`）：**放行前**把「将被修改/删除的旧版」复制进隔离库。

    - 只在动作看起来会改文件时做（`kind` / 标题 / 命令任一命中）；
    - 修改类只收**单个文件**（目录改动不在这里扫，避免把整个项目复制一遍）；
    - 删除类目录也收（`rm -r` 要能恢复）；
    - 任何异常都返回 `""`，**绝不挡放行**。
    """
    if not is_change_action(kind, title, cmd):
        return ""
    try:
        import snapshots                                   # noqa: PLC0415
        root_s = str(root or "")
        qkind = "delete" if (
            str(kind or "").lower() in ("delete", "remove", "move", "rename")
            or re.search(r"(delete|remove|rmdir|rm\b|unlink|删除|移除|重命名|移动)", str(title or ""), re.I)
            or is_destructive(cmd)
        ) else "modify"
        cands = []
        if cmd:
            try:
                cands.extend((snapshots.guess_targets(str(cmd or ""), root_s) or {}).get("targets") or [])
            except Exception:                              # noqa: BLE001
                pass
        for raw in (locations or []):
            p = _loc_path(root_s, raw)
            if p and os.path.exists(p):
                cands.append(p)
        targets, seen = [], set()
        for p in cands:
            try:
                rp = os.path.realpath(str(p))
            except Exception:                              # noqa: BLE001
                continue
            if rp in seen:
                continue
            if qkind == "modify" and os.path.isdir(rp):
                continue                                    # 修改只备份单文件；目录交给删除类
            if not os.path.exists(rp):
                continue
            seen.add(rp)
            targets.append(rp)
        if not targets:
            return ""
        rec = snapshots.quarantine_paths(
            root_s, targets, scope=str(scope or "panel"),
            note=str(note or title or cmd or "")[:200], cmd=str(cmd or ""), kind=qkind)
        return str((rec or {}).get("id") or "")
    except Exception:                                      # noqa: BLE001
        return ""


def quarantine_before_allow(root, cmd, title="", locations=None, scope="panel", note="") -> str:
    """★先隔离再放行（面板单聊 · `ui 0.2.85`）。

    只对**破坏性**命令生效（删除 / 覆盖…），其它修改走 `capture_before_allow`。
    返回隔离记录 id（没做 / 失败 → `""`）。**任何异常都不挡放行**。
    """
    try:
        if not is_destructive(cmd, title):
            return ""
    except Exception:                                     # noqa: BLE001
        return ""
    return capture_before_allow(root, cmd, title, locations, scope=scope, note=note, kind="delete")


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

    def auto_ticket(self, sid: str, pid: str) -> dict:
        """低风险删除：**先隔离，再自动放行**（引擎可选能力）。

        返回 `{ok, auto, reason, id?, count?, stars?, tombstone?}`；默认不支持。
        ACP 在自己进程里处理，不需要这个接口。
        """
        return {"ok": True, "auto": False, "reason": "该引擎不支持低风险删除自动隔离"}

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
