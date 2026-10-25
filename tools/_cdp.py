# -*- coding: utf-8 -*-
r"""极简 CDP 客户端（纯标准库）：给无头 Edge 跑「导航 → 等页面里的探针结果 → 取值」。

为什么不用 `--dump-dom` + `--virtual-time-budget`：
    虚拟时间会**在有网络请求时暂停**，CI 的慢 runner 上经常出现"预算耗尽 / 进程卡住"，
    DOM 里拿不到探针结果（`acp_engine_ui_test` 在 CI 上反复因此挂）。
    走 CDP 就是**真实时间**：导航完轮询 `Runtime.evaluate`，拿到了就返回，拿不到就超时，可复现。

用法：
    with Browser() as b:
        b.navigate("http://127.0.0.1:8801/_probe.html")
        js = b.eval_js("document.getElementById('__probe__') && document.getElementById('__probe__').textContent")
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise RuntimeError("没找到 msedge.exe")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _WS:
    """够用的 WebSocket 客户端（文本帧 + ping/pong + 关闭）。"""

    def __init__(self, url: str, timeout: float = 30.0):
        u = urllib.parse.urlsplit(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + ("?" + u.query if u.query else "")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n") % (path, u.hostname, u.port or 80, key)
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WS 握手失败（连接被关）")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise RuntimeError("WS 握手失败：" + head.split(b"\r\n")[0].decode("latin-1"))
        self.buf = rest
        self.nid = 0

    def _recv_exact(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RuntimeError("WS 连接已断开")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def recv(self):
        while True:
            b0, b1 = self._recv_exact(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            payload = self._recv_exact(n)
            if op == 0x8:
                raise RuntimeError("WS 被对端关闭")
            if op == 0x9:                                  # ping → pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if op in (0x1, 0x2):
                return json.loads(payload.decode("utf-8"))

    def call(self, method: str, params=None, timeout: float = 60.0):
        self.nid += 1
        mid = self.nid
        self.send({"id": mid, "method": method, "params": params or {}})
        t0 = time.time()
        while time.time() - t0 < timeout:
            msg = self.recv()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s 失败：%s" % (method, msg["error"]))
                return msg.get("result") or {}
        raise TimeoutError(method)


class Browser:
    """无头 Edge + CDP：导航 / 求值。用作上下文管理器。"""

    def __init__(self, width: int = 1600, height: int = 1000):
        self.width, self.height = width, height
        self.proc = None
        self.ws = None
        self.port = _free_port()
        self.profile = os.path.join(tempfile.gettempdir(), "edge-cdp-%d" % self.port)

    def __enter__(self):
        self.proc = subprocess.Popen(
            [find_edge(), "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check", "--mute-audio",
             "--disable-extensions", "--disable-background-networking",
             "--window-size=%d,%d" % (self.width, self.height),
             "--remote-debugging-port=%d" % self.port,
             "--user-data-dir=" + self.profile,
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = ""
        t0 = time.time()
        while time.time() - t0 < 45:
            time.sleep(0.3)
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/json/list" % self.port, timeout=2) as r:
                    tabs = json.loads(r.read().decode("utf-8"))
                for t in tabs:
                    if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                        ws_url = t["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:                              # noqa: BLE001
                continue
        if not ws_url:
            raise RuntimeError("连不上 Edge 调试端口 %d" % self.port)
        self.ws = _WS(ws_url)
        self.ws.call("Page.enable")
        self.ws.call("Runtime.enable")
        return self

    def navigate(self, url: str) -> None:
        self.ws.call("Page.navigate", {"url": url})

    def eval_js(self, expr: str, await_promise: bool = False, timeout: float = 60.0):
        r = self.ws.call("Runtime.evaluate",
                         {"expression": expr, "returnByValue": True,
                          "awaitPromise": await_promise},
                         timeout=timeout)
        return (r.get("result") or {}).get("value")

    def wait_for_js(self, expr: str, timeout: float = 120.0, interval: float = 0.4):
        """轮询直到表达式返回真值；返回最后一次的值（超时返回 None）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                if self.eval_js(expr):
                    return True
            except Exception:                              # noqa: BLE001
                pass
            time.sleep(interval)
        return None

    def __exit__(self, *exc):
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:                              # noqa: BLE001
                self.proc.kill()
        finally:
            return False
