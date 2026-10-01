# -*- coding: utf-8 -*-
"""ACP 引擎：把 AcpService 接到 server.py 的 /api/* 语义上。

配置在 runtime/state/_acp.json：
    {"command": ["<可执行文件>", "<参数>", ...], "cwd": "D:\\some\\dir", "env": {...}}
没有 command 就视为未配置（available=False）。

S3 覆盖：/api/event（SSE）、会话增删改查、消息、prompt、interrupt、
        form/permission 查询与回复、/api/model（占位空表）。
模型清单（ACP session config options）留到后续。
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.parse

from ..base import Engine
from . import agents
from .service import AcpService, BusyError


# ACP / agent 的 stderr 以前是直接丢掉的（log=lambda s: None）—— 出问题时（比如随包 codex 起不来、
# 网络不通、缺 API Key）完全看不到现场。现在落到 runtime/logs/acp.log，超过 2MB 截断重来。
ACP_LOG_LOCK = threading.Lock()


def acp_log_path() -> str:
    return os.path.join(agents.ROOT_DIR, "runtime", "logs", "acp.log")


def acp_log(s: str) -> None:
    try:
        p = acp_log_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with ACP_LOG_LOCK:
            try:
                if os.path.getsize(p) > 2 * 1024 * 1024:
                    open(p, "w", encoding="utf-8").close()
            except OSError:
                pass
            with open(p, "a", encoding="utf-8") as fh:
                fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), str(s)))
    except Exception:  # noqa: BLE001
        pass


def _empty(handler, code: int):
    """发一个没有正文的响应（204 用；不能带 body）。"""
    handler.send_response(code)
    handler.send_header("Content-Length", "0")
    handler.end_headers()


def _q_int(path: str, key: str, default: int) -> int:
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    try:
        return int((q.get(key) or [default])[0])
    except Exception:  # noqa: BLE001
        return default


class AcpEngine(Engine):
    id = "acp"
    label = "ACP"

    def __init__(self):
        # 每个 agent id 一个 AcpService（也就是一个常驻子进程）：切回旧 agent 不重启。
        self._services = {}
        self._services_lock = threading.RLock()
        self._config = None

    # ---------------- 自述 ----------------

    def config(self) -> dict:
        """当前生效的 agent（注册表）+ **共享基线**（cwd / env / mode）。

        基线用于「一套规则服务所有 agent」：agent 只写自己的 command（和必要的专属 env），
        公共的 cwd / API key / 审批模式放 baseline，避免每个 agent 各配一份。
        """
        if self._config is None:
            a = agents.active_agent() or {}
            b = agents.baseline()
            env = {}
            env.update(b.get("env") or {})
            env.update(a.get("env") or {})
            cfg = dict(a)
            cfg["cwd"] = a.get("cwd") or b.get("cwd") or ""
            cfg["env"] = env
            # 审批模式：**agent 自己的 `mode` 覆盖共享基线**。不同 agent 的模式名不一样
            # （Codex：read-only / agent / agent-full-access；OpenCode：build / plan），
            # 全局只写一个 baseline.mode 会对不上——对不上时后端会跳过并记一行日志。
            b2 = dict(b)
            if a.get("mode"):
                b2["mode"] = a["mode"]
            cfg["mode"] = b2.get("mode") or ""
            cfg["_baseline"] = b2
            self._config = cfg
        return self._config

    def available(self) -> bool:
        return bool(self.config().get("command"))

    def read_service(self):
        raise NotImplementedError("ACP 引擎不需要上游地址（自己拉起 agent 子进程）")

    def status(self) -> dict:
        cfg = self.config()
        st = agents.status_of(cfg)
        return {"id": self.id, "label": self.label, "available": self.available(),
                "agent": self.active_agent_id(), "agentLabel": cfg.get("label") or "",
                "ready": st["available"], "missing": st["missing"],
                "command": cfg.get("command"), "running": self.service_running()}

    # ---------------- agentlist（多 agent）----------------

    def active_agent_id(self) -> str:
        return str(agents.load_registry().get("active") or "")

    def list_agents(self) -> list:
        det = agents.detect_all_cached()
        return [agents.public(a, det) for a in (agents.load_registry().get("agents") or [])]

    def select_agent(self, aid: str) -> bool:
        """切换当前 agent。**不关旧的**：每个 agent 一个常驻 AcpService，切回不重启。"""
        if not agents.set_active(aid):
            return False
        self._config = None
        threading.Thread(target=self.prewarm, daemon=True).start()
        return True

    def patch_agent(self, aid: str, patch: dict) -> dict:
        a = agents.update_agent(aid, patch)
        if a:
            self._config = None
            # 只有启动相关字段变了才重启这个 agent 的进程；只改 label/note 不动
            if any(k in patch for k in ("command", "env", "cwd", "provider")):
                self.shutdown_agent(str(aid))
        return a

    def autofill_agents(self) -> dict:
        """自动检测并补全「命令不可用」的 agent；只关被改动的 agent。"""
        r = agents.autofill()
        self._config = None
        for aid in (r.get("changed") or []):
            self.shutdown_agent(str(aid))
        return r

    def add_agent(self, label, command, cwd="", env=None, note="", provider=""):
        a = agents.add_agent(label, command, cwd, env, note, provider)
        self._config = None
        return a

    def remove_agent(self, aid) -> bool:
        ok = agents.remove_agent(aid)
        if ok:
            self.shutdown_agent(str(aid))
        return ok

    def set_baseline_env(self, patch: dict) -> dict:
        """写共享基线的环境变量（模型 API Key）。

        ⚠ 环境变了，**所有** ACP 子进程都得重起（它们启动时把 env 传给子进程），
          所以这里清掉配置缓存 + 关掉所有 AcpService；下次用的时候按需重拉。
        """
        b = agents.update_baseline_env(patch)
        self._config = None
        with self._services_lock:
            ids = list(self._services.keys())
        for aid in ids:
            self.shutdown_agent(str(aid))
        return b

    def baseline_view(self) -> dict:
        """给界面看的基线视图：**只给掩码**，绝不出明文。"""
        b = agents.baseline()
        env = b.get("env") if isinstance(b.get("env"), dict) else {}
        return {"cwd": b.get("cwd") or "", "mode": b.get("mode") or "",
                "env": {str(k): agents.mask_secret(v) for k, v in env.items()},
                "envNames": sorted(str(k) for k in env.keys())}

    def log_tail(self, n: int = 200) -> str:
        """最近 n 行 ACP / agent 日志（排查随包 codex 起不来、网络不通、缺 Key 用）。"""
        try:
            n = max(1, min(int(n), 2000))
        except Exception:  # noqa: BLE001
            n = 200
        try:
            with open(acp_log_path(), "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return ""
        return "".join(lines[-n:])

    def prewarm(self):
        """启动当前 agent 的子进程并顺手拿一次模型清单（不弹命令、不建会话）。"""
        try:
            svc = self.service()
            svc.ensure_started()
            try:
                svc.ensure_model_config()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    def healthz(self):
        if not self.available():
            return 503, {"ok": False, "engine": "acp",
                         "error": "未配置 ACP agent：runtime/state/_acp.json 缺少 command"}
        if self.service_running():
            return 200, {"ok": True, "engine": "acp"}
        return 200, {"ok": True, "engine": "acp", "note": "尚未启动 agent（首次建会话时拉起）"}

    def service_running(self) -> bool:
        with self._services_lock:
            svc = self._services.get(self.active_agent_id())
        return bool(svc and svc.running())

    def service(self) -> AcpService:
        """当前 agent 的 AcpService（按 agent id 缓存，切回不重启）。"""
        aid = self.active_agent_id()
        with self._services_lock:
            svc = self._services.get(aid)
            if svc is not None:
                return svc
            cfg = self.config()
            command = cfg.get("command")
            if not command:
                raise RuntimeError("未配置 ACP agent 命令：runtime/state/_acp.json 缺少 command")
            svc = AcpService(command, cwd=cfg.get("cwd"),
                             env=cfg.get("env"), log=acp_log,
                             agent_id=aid,
                             provider_id=cfg.get("provider") or "",
                             baseline=cfg.get("_baseline") or {})
            self._services[aid] = svc
            return svc

    def shutdown_agent(self, aid: str) -> None:
        """只收起某个 agent 的子进程（配置变更 / 删除该 agent 时用）。"""
        with self._services_lock:
            svc = self._services.pop(str(aid), None)
        if svc is not None:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass

    def shutdown(self):
        """收起所有 agent 子进程（供联调脚本 / 服务退出时调用）。"""
        with self._services_lock:
            services = list(self._services.values())
            self._services.clear()
        for svc in services:
            try:
                svc.close()
            except Exception:  # noqa: BLE001
                pass

    # ---------------- HTTP 分派 ----------------

    def handle_api(self, handler, method: str) -> None:
        path = urllib.parse.urlsplit(handler.path).path
        try:
            if path == "/api/event" and method == "GET":
                return self._sse(handler)
            if path == "/api/model" and method == "GET":
                svc = self.service()
                try:
                    svc.ensure_model_config()      # 重启/切 agent 后旧会话也能拿到清单
                except Exception:  # noqa: BLE001
                    pass
                return handler._send_json(200, {"location": {"directory": svc.cwd},
                                                "data": svc.models()})
            if path == "/api/model/default" and method == "GET":
                svc = self.service()
                try:
                    svc.ensure_model_config()
                except Exception:  # noqa: BLE001
                    pass
                d = svc.model_default()
                if not d:
                    return handler._send_json(404, {"error": "尚不知道默认模型（先新建一个会话）"})
                return handler._send_json(200, {"location": {"directory": svc.cwd}, "data": d})
            if path == "/api/agent" and method == "GET":
                # 与 OpenCode /api/agent 形状对齐；ACP 里「agent」对应会话的审批模式
                svc = self.service()
                try:
                    svc.ensure_model_config()
                except Exception:  # noqa: BLE001
                    pass
                return handler._send_json(200, {"location": {"directory": svc.cwd},
                                                "data": svc.agents()})
            if path == "/api/permission/always":
                # 权限「始终允许」的记忆（面板还没入口，先把接口与数据铺好）
                svc = self.service()
                if method == "GET":
                    sid = str((urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
                               .get("session") or [""])[0]) or None
                    return handler._send_json(200, {"ok": True, "agent": svc.agent_id,
                                                    "data": svc.always_rules(sid)})
                if method == "DELETE":
                    b = handler._read_json_body() or {}
                    n = svc.forget_always(sid=b.get("session") or None,
                                          kind=b.get("kind") or None,
                                          rule_id=b.get("id") or None)
                    return handler._send_json(200, {"ok": True, "removed": n})
                return handler._send_json(405, {"error": "GET/DELETE only"})
            # ⚠ 这两条必须在下面的 /api/session/<id> 正则**之前**判，
            #    否则 "remote" / "import" 会被当成会话 id。
            if path == "/api/session/remote" and method == "GET":
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
                return handler._send_json(200, self.service().list_remote(
                    cwd=(q.get("cwd") or [""])[0] or None,
                    cursor=(q.get("cursor") or [""])[0] or ""))
            if path == "/api/session/import" and method == "POST":
                b = handler._read_json_body() or {}
                try:
                    s = self.service().import_remote(b.get("id") or b.get("sessionId"),
                                                      b.get("title"))
                except ValueError as exc:
                    return handler._send_json(400, {"error": str(exc)})
                except Exception as exc:  # noqa: BLE001
                    return handler._send_json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})
                return handler._send_json(200, {"data": s})
            if path == "/api/session":
                if method == "GET":
                    limit = _q_int(handler.path, "limit", 50)
                    data = self.service().list_sessions()[:limit]
                    return handler._send_json(200, {"data": data, "cursor": None})
                if method == "POST":
                    b = handler._read_json_body() or {}
                    loc = (b.get("location") or {}).get("directory")
                    s = self.service().create_session(title=b.get("title"), cwd=loc, model=b.get("model"))
                    return handler._send_json(200, {"data": s})
                return handler._send_json(405, {"error": "GET/POST only"})
            m = re.match(r"^/api/session/([^/]+)(?:/(.+))?$", path)
            if m:
                return self._session_api(handler, method,
                                         urllib.parse.unquote(m.group(1)), m.group(2) or "")
            return handler._send_json(404, {"error": "未知的 /api 路径", "path": path})
        except Exception as exc:  # noqa: BLE001
            handler._send_json(500, {"error": "%s: %s" % (type(exc).__name__, exc)})

    def _session_api(self, handler, method, sid, rest):
        svc = self.service()
        if rest == "":
            if method == "GET":
                s = svc.get_session(sid)
                if s is None:
                    return handler._send_json(404, {"error": "会话不存在"})
                return handler._send_json(200, {"data": s})
            if method == "PATCH":
                b = handler._read_json_body() or {}
                if not svc.rename_session(sid, b.get("title") or ""):
                    return handler._send_json(404, {"error": "会话不存在"})
                return _empty(handler, 204)
            if method == "DELETE":
                try:
                    svc.close_agent_session(sid)   # best-effort：告诉 agent 侧 close/delete
                except Exception:  # noqa: BLE001
                    pass
                svc.delete_session(sid)
                return _empty(handler, 204)
            return handler._send_json(405, {"error": "GET/PATCH/DELETE only"})

        if rest == "message" and method == "GET":
            limit = _q_int(handler.path, "limit", 80)
            cursor = str((urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
                          .get("cursor") or [""])[0])
            msgs, next_cursor = svc.messages(sid, limit, cursor)
            return handler._send_json(200, {"data": msgs, "cursor": next_cursor})
        if rest == "prompt" and method == "POST":
            b = handler._read_json_body() or {}
            try:
                svc.prompt(sid, b.get("text") or "", b.get("files"))
            except KeyError:
                return handler._send_json(404, {
                    "error": "会话不存在（可能刚切换过引擎：请刷新面板，或在左侧重选一个会话）"})
            except BusyError as exc:
                return handler._send_json(409, {"error": str(exc)})
            return handler._send_json(200, {"ok": True})
        if rest == "interrupt" and method == "POST":
            svc.interrupt(sid)
            return handler._send_json(200, {"ok": True})
        if rest == "model" and method == "POST":
            b = handler._read_json_body() or {}
            svc.set_model(sid, b.get("model"))
            return _empty(handler, 204)
        if rest == "agent" and method == "POST":
            b = handler._read_json_body() or {}
            if not svc.set_agent(sid, b.get("agent")):
                return handler._send_json(400, {
                    "error": "切换模式失败（该 agent 可能不支持，或会话没接回）"})
            return _empty(handler, 204)
        if rest == "form" and method == "GET":
            return handler._send_json(200, {"data": svc.list_forms(sid)})
        if rest == "permission" and method == "GET":
            # 顺带把这条会话的「始终允许」记忆带回去（前端可显示"已记住"）
            return handler._send_json(200, {"data": svc.list_permissions(sid),
                                            "always": svc.always_rules(sid)})

        mf = re.match(r"^form/([^/]+)/reply$", rest)
        if mf and method == "POST":
            b = handler._read_json_body() or {}
            svc.reply_form(sid, urllib.parse.unquote(mf.group(1)), b.get("answer") or {})
            return _empty(handler, 204)
        mp = re.match(r"^permission/([^/]+)/reply$", rest)
        if mp and method == "POST":
            b = handler._read_json_body() or {}
            svc.reply_permission(sid, urllib.parse.unquote(mp.group(1)), b.get("decision") or "reject")
            return _empty(handler, 204)
        return handler._send_json(404, {"error": "未知的会话子路径", "sub": rest})

    # ---------------- SSE ----------------

    def _sse(self, handler):
        svc = self.service()
        q = svc.subscribe()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()

        def frame(obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            handler.wfile.write(b"data: " + data + b"\n\n")
            handler.wfile.flush()

        try:
            frame({"type": "server.connected", "data": {}})
            while True:
                try:
                    frame(q.get(timeout=15.0))
                except queue.Empty:
                    handler.wfile.write(b": ping\n\n")
                    handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            svc.unsubscribe(q)
            handler.close_connection = True
