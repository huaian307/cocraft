# -*- coding: utf-8 -*-
r"""cocraft 多智能体群聊 —— **WebSocket 推送**自检（纯标准库，无第三方依赖）。

对照桌面 `cocraft-多智能体群聊_服务端接口需求_v3.md` §2 的验收标准：
  * §2.5 事件：`message` / `mention` / `presence` / `floor` / `moderator.route`
  * §2.6 + §5 复现：**同一条用户令牌开两条 WebSocket（A/B）**，A 发消息 → B 应立刻收到 `message`；
    再走 REST 发一条 → A、B 都应收到。

用法：
    python tools\cocraft_ws_check.py --token <令牌>
    python tools\cocraft_ws_check.py --token <令牌> --chat 3
    python tools\cocraft_ws_check.py --token <令牌> --ws wss://www.animaker.cc.cd/ws
环境变量也可：`COCRAFT_TOKEN`；REST/WS 默认从面板代理 `GET /cocraft/_url` 取。

输出纯 ASCII（避免控制台 GBK 乱码）。只读为主；唯一的写操作是**发一条带时间戳的测试消息**。
"""
from __future__ import annotations

import base64
import json
import os
import queue
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

PANEL = os.environ.get("COCRAFT_PANEL") or "http://127.0.0.1:8787"
DEFAULT_WS = "wss://www.animaker.cc.cd/ws"


# ---------------- REST ----------------

def http(url, token=None, method="GET", body=None, timeout=20):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
        return e.code, raw
    except Exception as e:  # noqa: BLE001
        return None, str(e)


# ---------------- 极简 WebSocket 客户端（纯标准库）----------------

class WSClient:
    """够用的客户端：TLS + 握手 + 文本帧收发 + ping/pong + close。"""

    def __init__(self, url: str, token: str, timeout: float = 15.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.sock = None
        self.frames: queue.Queue = queue.Queue()
        self.closed = False
        self.close_code = None
        self.error = ""
        self._buf = b""
        self._stop = threading.Event()
        self.handshake_status = ""

    def connect(self):
        u = urllib.parse.urlsplit(self.url)
        host, port = u.hostname, (u.port or (443 if u.scheme == "wss" else 80))
        path = u.path or "/ws"
        q = urllib.parse.parse_qsl(u.query)
        q = [(k, v) for (k, v) in q if k != "token"] + [("token", self.token)]
        query = urllib.parse.urlencode(q)
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if u.scheme == "wss":
            ctx = ssl.create_default_context()
            raw = ctx.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            "GET %s?%s HTTP/1.1\r\n"
            "Host: %s\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://127.0.0.1:8787\r\n"
            "User-Agent: cocraft-ws-check\r\n"
            "\r\n"
        ) % (path, query, host, key)
        raw.sendall(req.encode("utf-8"))
        # 读握手响应
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                break
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        self.handshake_status = line
        self._buf = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        if " 101 " not in line and not line.endswith("101"):
            self.error = "handshake not upgraded: " + line
            # 兼容：也可能是 401/403/1008 之类
            try:
                self.close_code = int(line.split()[1])
            except Exception:  # noqa: BLE001
                pass
            self.closed = True
            return self
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self):
        try:
            while not self._stop.is_set():
                fr = self._read_frame()
                if fr is None:
                    break
                opcode, payload = fr
                if opcode == 0x1:            # text
                    self.frames.put(payload.decode("utf-8", "replace"))
                elif opcode == 0x8:          # close
                    self.closed = True
                    if len(payload) >= 2:
                        self.close_code = int.from_bytes(payload[:2], "big")
                    break
                elif opcode == 0x9:          # ping -> pong
                    self._send_frame(0xA, payload)
                # pong / binary 忽略
        except Exception as e:  # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.closed = True

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self._buf)))
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
            length = int.from_bytes(ext, "big")
        elif length == 127:
            ext = self._read_exact(8)
            if len(ext) < 8:
                return None
            length = int.from_bytes(ext, "big")
        if b2 & 0x80:  # 服务端不应掩码；万一有，读掉掩码
            self._read_exact(4)
        payload = self._read_exact(length) if length else b""
        if len(payload) < length:
            return None
        return opcode, payload

    def _send_frame(self, opcode: int, payload: bytes):
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        hdr = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.sock.sendall(hdr + mask + masked)

    def send_text(self, obj):
        self._send_frame(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def drain(self, seconds: float):
        """收集 seconds 秒内到达的帧（已解析成 dict，非 JSON 的原样返回）。"""
        out = []
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = self.frames.get(timeout=min(0.5, max(0.05, end - time.time())))
            except queue.Empty:
                continue
            try:
                out.append(json.loads(raw))
            except Exception:  # noqa: BLE001
                out.append({"_raw": raw})
        return out

    def close(self):
        self._stop.set()
        try:
            if self.sock:
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass


def types_of(frames):
    counts = {}
    for f in frames:
        t = f.get("type", "?") if isinstance(f, dict) else "?"
        counts[t] = counts.get(t, 0) + 1
    return counts


def mention_test(rest, ws_url, token, chat):
    """建一个临时 agent、拉进房间、用它自己的 agent_token 连 WS，再从用户侧 @ 它。

    预期：**只有该 agent 的连接**收到 `{"type":"mention", ...}`（房间其余人收到 `message`）。
    结束后清理（移出房间 + 删除 agent）。
    """
    import random
    import string
    name = "wscheck_" + "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    st, created = http(rest + "/agents", token, "POST",
                       {"name": name, "display": "WS自检", "machine": "cocraft-ws-check"})
    if st != 200 or not isinstance(created, dict):
        print("  [BAD] 建 agent 失败：", st, str(created)[:160])
        return None
    aid = (created.get("agent") or {}).get("id")
    atok = created.get("agent_token") or ""
    print("  临时 agent：id=%s name=%s token=%s" % (aid, name, "有" if atok else "无"))
    try:
        st, j = http(rest + "/chats/%s/agents" % chat, token, "POST", {"agent_id": aid})
        print("  加入房间：", st, str(j)[:120])
        aws = WSClient(ws_url, atok).connect()
        uws = WSClient(ws_url, token).connect()
        time.sleep(0.8)
        print("  agent 连接：", aws.handshake_status, "closed=", aws.closed)
        uws.send_text({"type": "message", "chat_id": chat,
                       "content": "@%s ping" % name, "mentions": [name]})
        ga = aws.drain(6.0)
        gu = uws.drain(2.0)
        print("  agent 连接收到：", types_of(ga))
        print("  用户连接收到：", types_of(gu))
        got = any(isinstance(f, dict) and f.get("type") == "mention" for f in ga)
        msg = any(isinstance(f, dict) and f.get("type") == "message" for f in ga)
        print("  mention（定向给该 agent）：", "OK" if got else "MISSING")
        print("  message（房间广播到 agent）：", "OK" if msg else "MISSING")
        aws.close(); uws.close()
        return got
    finally:
        http(rest + "/chats/%s/agents/%s" % (chat, aid), token, "DELETE")
        st2, _ = http(rest + "/agents/%s" % aid, token, "DELETE")
        print("  已清理临时 agent（leave=%s delete=%s）" % (st, st2))


def main() -> int:
    args = sys.argv[1:]
    token = os.environ.get("COCRAFT_TOKEN") or ""
    ws_url = DEFAULT_WS
    chat = None
    if "--token" in args:
        token = args[args.index("--token") + 1]
    if "--ws" in args:
        ws_url = args[args.index("--ws") + 1]
    if "--chat" in args:
        chat = args[args.index("--chat") + 1]
    want_mention = "--mention" in args

    print("panel :", PANEL)
    # 从面板代理拿 REST base + ws 地址（跑不通就用默认）
    rest = PANEL.rstrip("/") + "/cocraft"
    try:
        st, info = http(PANEL.rstrip("/") + "/cocraft/_url")
        if isinstance(info, dict) and info.get("ws"):
            ws_url = info["ws"]
            # ⚠ REST 一律走**面板代理**（/cocraft/*）：直连上游会被 Cloudflare 拦（403 1010 / 400）。
        print("_url  :", st, str(info)[:160])
    except Exception as e:  # noqa: BLE001
        print("_url  : (skipped)", e)
    print("rest  :", rest)
    print("ws    :", ws_url)
    print("token :", "(provided)" if token else "(NONE -> 只能验证握手/鉴权行为)")

    if not token:
        w = WSClient(ws_url, "", timeout=15).connect()
        time.sleep(1.0)
        print("\n== 无 token 握手 ==")
        print("  status :", w.handshake_status)
        print("  closed :", w.closed, "close_code:", w.close_code, "error:", w.error)
        w.close()
        return 0

    st, me = http(rest + "/auth/me", token)
    print("\n== 令牌校验 ==")
    print("  /auth/me ->", st, str(me)[:160])
    if st != 200:
        print("  [BAD] 令牌无效/过期，无法继续（请重新登录 cocraft 后把新 token 传进来）")
        return 2

    # 选一个聊天室
    if not chat:
        st, ch = http(rest + "/chats", token)
        items = ch.get("items") if isinstance(ch, dict) else (ch if isinstance(ch, list) else None)
        if not items:
            print("  [BAD] 拿不到会话列表：", st, str(ch)[:160])
            return 2
        chat = (items[0] or {}).get("id")
    print("  chat   ->", chat)

    print("\n== 开两条 WebSocket（A / B，同一用户令牌）==")
    A = WSClient(ws_url, token).connect()
    B = WSClient(ws_url, token).connect()
    print("  A:", A.handshake_status, "closed=", A.closed, A.error)
    print("  B:", B.handshake_status, "closed=", B.closed, B.error)
    if A.closed or B.closed:
        print("  [BAD] 至少一条没连上（1008=登录失效）")
        A.close(); B.close()
        return 2

    warm = A.drain(2.0) + B.drain(0.1)  # 先收一会儿（可能来 presence）
    print("  初始收到类型：", types_of(warm) or "（暂无）")

    stamp = "ws-check-%d" % int(time.time())
    print("\n== ① A 用 WebSocket 发消息 ==")
    A.send_text({"type": "message", "chat_id": chat, "content": stamp})
    ra, rb = A.drain(6.0), B.drain(6.0)
    print("  A 收到：", types_of(ra))
    print("  B 收到：", types_of(rb))
    b_msg = [f for f in rb if isinstance(f, dict) and f.get("type") == "message"
             and f.get("content") == stamp]
    print("  B 收到该条 message：", "YES" if b_msg else "NO")

    print("\n== ② REST 发消息（POST /chats/{id}/messages）==")
    stamp2 = stamp + "-rest"
    st, resp = http(rest + "/chats/%s/messages" % chat, token, "POST", {"content": stamp2})
    print("  POST ->", st, str(resp)[:140])
    ra2 = A.drain(6.0)
    rb2 = B.drain(6.0)
    print("  A 收到：", types_of(ra2))
    print("  B 收到：", types_of(rb2))
    got = [f for f in (ra2 + rb2) if isinstance(f, dict) and f.get("type") == "message"
           and f.get("content") == stamp2]
    print("  广播该条 message：", "YES" if got else "NO")

    print("\n== ③ 其余事件（本轮观测，非必现）==")
    seen = types_of(warm + ra + rb + ra2 + rb2)
    for t in ("presence", "message", "mention", "floor", "moderator.route"):
        print("  %-16s %s" % (t, "OK" if t in seen else "-"))

    okm = None
    if want_mention:
        print("\n== ④ mention（建临时 agent 验证定向推送）==")
        okm = mention_test(rest, ws_url, token, chat)

    A.close(); B.close()
    ok1 = bool(b_msg)
    ok2 = bool(got)
    line = "==== 结果：① WS发->B收到 %s · ②REST发->广播 %s" % (
        "OK" if ok1 else "FAIL", "OK" if ok2 else "FAIL")
    if okm is not None:
        line += " · ④mention->agent %s" % ("OK" if okm else "FAIL")
    print("\n" + line + " ====")
    return 0 if (ok1 and ok2 and (okm in (None, True))) else 2


if __name__ == "__main__":
    raise SystemExit(main())
