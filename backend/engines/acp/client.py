# -*- coding: utf-8 -*-
"""ACP 客户端 —— 在 AcpProcess 之上做 JSON-RPC 2.0 的配对与双向请求。

方向：
  我们 → agent   request(method, params) 有 id、等响应；notify() 无 id
  agent → 我们   notification → on_notification(method, params)
                 request      → on_request(method, params)（在工作线程里执行，
                                允许阻塞等用户答复，不会堵住后续消息的读取）

约定：
  - 每个请求都有超时；超时抛 AcpTimeout，错误响应抛 AcpError，连接断了抛 AcpClosed。
  - 默认不声明任何客户端能力（clientCapabilities = {}）；ACP 规定「没声明 = 不支持」，
    所以 agent 不会擅自调 fs/terminal/elicitation。要开哪项由调用方显式传入。
"""
from __future__ import annotations

import itertools
import threading

from .process import AcpProcess

PROTOCOL_VERSION = 1


class AcpError(Exception):
    def __init__(self, code, message, data=None):
        super().__init__("ACP 错误 %s: %s" % (code, message))
        self.code = code
        self.message = message
        self.data = data


class AcpTimeout(Exception):
    pass


class AcpClosed(Exception):
    pass


class AcpClient:
    def __init__(self, command, cwd=None, env=None,
                 on_notification=None, on_request=None, on_log=None,
                 default_timeout=60.0, client_info=None):
        self.command = list(command)
        self.default_timeout = default_timeout
        self._on_notification = on_notification or (lambda method, params: None)
        self._on_request = on_request
        self._on_log = on_log or (lambda line: None)
        self._ids = itertools.count(1)
        self._pending = {}
        self._lock = threading.Lock()
        self._closed = False
        self.client_info = client_info or {"name": "cocraft", "title": "cocraft", "version": "0.1.0"}
        self.init_result = None
        self.agent_capabilities = {}
        self.protocol_version = None
        self._proc = AcpProcess(command, cwd=cwd, env=env,
                                on_message=self._dispatch, on_log=self._on_log,
                                on_exit=self._on_exit)

    # ---------- 生命周期 ----------

    def start(self):
        self._proc.start()

    def is_alive(self) -> bool:
        return self._proc.alive

    def close(self, timeout: float = 3.0):
        self._closed = True
        self._fail_pending(AcpClosed("连接已关闭"))
        self._proc.stop(timeout=timeout)

    def _on_exit(self, code):
        self._on_log("[acp] agent 退出 code=%r" % (code,))
        self._closed = True
        self._fail_pending(AcpClosed("agent 已退出"))

    def _fail_pending(self, exc):
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for p in pending:
            p["error"] = exc
            p["event"].set()

    # ---------- 发送 ----------

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not self._proc.send(msg):
            raise AcpClosed("无法写入 agent（进程可能已退出）")

    def request(self, method, params=None, timeout=None):
        if self._closed:
            raise AcpClosed("连接已关闭")
        rid = next(self._ids)
        pending = {"event": threading.Event(), "result": None, "error": None}
        with self._lock:
            self._pending[rid] = pending
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        if not self._proc.send(msg):
            with self._lock:
                self._pending.pop(rid, None)
            raise AcpClosed("无法写入 agent（进程可能已退出）")
        if not pending["event"].wait(timeout if timeout is not None else self.default_timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise AcpTimeout("请求超时：%s" % method)
        if pending["error"] is not None:
            raise pending["error"]
        return pending["result"]

    # ---------- 接收 ----------

    def _dispatch(self, msg):
        if "method" in msg:
            if "id" in msg:
                self._handle_request(msg)      # agent → 我们的请求
            else:
                try:
                    self._on_notification(msg.get("method"), msg.get("params") or {})
                except Exception as exc:  # noqa: BLE001
                    self._on_log("[acp] on_notification 抛错：%r" % (exc,))
            return
        if "id" in msg:
            self._handle_response(msg)         # 我们请求的响应
            return
        self._on_log("[acp] 无法识别的消息：%r" % (msg,))

    def _handle_response(self, msg):
        with self._lock:
            pending = self._pending.pop(msg.get("id"), None)
        if pending is None:
            return
        if msg.get("error") is not None:
            err = msg["error"] or {}
            pending["error"] = AcpError(err.get("code"), err.get("message", ""), err.get("data"))
        else:
            pending["result"] = msg.get("result")
        pending["event"].set()

    def _handle_request(self, msg):
        if self._on_request is None:
            self._reply(msg.get("id"), error={"code": -32601, "message": "客户端不支持 " + str(msg.get("method"))})
            return
        # 读线程绝不能被"等用户答复"堵住 → 丢到工作线程
        threading.Thread(target=self._run_request, args=(msg,), daemon=True).start()

    def _run_request(self, msg):
        try:
            result = self._on_request(msg.get("method"), msg.get("params") or {})
            self._reply(msg.get("id"), result=result if result is not None else {})
        except Exception as exc:  # noqa: BLE001
            self._reply(msg.get("id"), error={"code": -32603, "message": "%s: %s" % (type(exc).__name__, exc)})

    def _reply(self, rid, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": rid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result if result is not None else {}
        try:
            self._proc.send(msg)
        except Exception:  # noqa: BLE001
            pass

    # ---------- 协议便捷方法（S2 只做到握手 + 建会话）----------

    def initialize(self, client_capabilities=None, timeout=None):
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": client_capabilities if client_capabilities is not None else {},
            "clientInfo": self.client_info,
        }
        result = self.request("initialize", params, timeout=timeout) or {}
        self.init_result = result
        self.agent_capabilities = result.get("agentCapabilities") or {}
        self.protocol_version = result.get("protocolVersion")
        return result

    def session_new(self, cwd, mcp_servers=None, timeout=None):
        params = {"cwd": cwd, "mcpServers": mcp_servers or []}
        return self.request("session/new", params, timeout=timeout)
