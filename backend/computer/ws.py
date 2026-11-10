"""极简 WebSocket 客户端（纯标准库，给 CDP 用）。

只做文本帧 + ping/pong + close；**同步 recv**（CDP 的请求/响应是严格配对，
事件帧由调用方跳过即可，不需要读线程）。大帧（截图 base64 上 MB）走循环读。

⚠ 不设 `Origin` 头：Chrome/Edge 的 DevTools 端点只校验 Host，带 Origin 反而可能被拒。
"""

import base64
import os
import socket
import struct
import urllib.parse


class WebSocketError(RuntimeError):
    pass


class WebSocket:
    def __init__(self, url, timeout=20.0):
        self.url = url
        self.timeout = timeout
        self.sock = None
        self._buf = b""
        self.closed = False

    # ---------- 连接 ----------
    def connect(self):
        u = urllib.parse.urlsplit(self.url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        raw = socket.create_connection((host, port), timeout=self.timeout)
        raw.settimeout(self.timeout)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ) % (path, host, port, key)
        raw.sendall(req.encode("utf-8"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                break
            head += chunk
            if len(head) > 65536:
                break
        line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        if " 101 " not in line:
            self.closed = True
            raise WebSocketError("WebSocket 握手失败：%s" % line)
        self._buf = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        return self

    # ---------- 收发 ----------
    def _read_exact(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                return b""
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self):
        h = self._read_exact(2)
        if len(h) < 2:
            return None
        b1, b2 = h[0], h[1]
        opcode = b1 & 0x0F
        length = b2 & 0x7F
        if length == 126:
            ext = self._read_exact(2)
            if len(ext) < 2:
                return None
            length = struct.unpack(">H", ext)[0]
        elif length == 127:
            ext = self._read_exact(8)
            if len(ext) < 8:
                return None
            length = struct.unpack(">Q", ext)[0]
        mask = None
        if b2 & 0x80:
            mask = self._read_exact(4)
        payload = self._read_exact(length) if length else b""
        if len(payload) < length:
            return None
        if mask:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload

    def _send_frame(self, opcode, payload):
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        self.sock.sendall(hdr + mask + masked)

    def send_text(self, text):
        self._send_frame(0x1, text.encode("utf-8"))

    def recv_text(self):
        """读一条文本消息（跳过 ping/pong 与分片拼接的简化处理）。"""
        chunks = []
        while True:
            fr = self._read_frame()
            if fr is None:
                self.closed = True
                return None
            opcode, payload = fr
            if opcode == 0x1:            # text（可能 FIN=0，这里当完整消息）
                return payload.decode("utf-8", "replace")
            if opcode == 0x0:            # continuation
                chunks.append(payload)
                continue
            if opcode == 0x9:            # ping → pong
                self._send_frame(0xA, payload)
                continue
            if opcode == 0x8:            # close
                self.closed = True
                return None
            # binary / pong 忽略

    def close(self):
        self.closed = True
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None
