# -*- coding: utf-8 -*-
r"""cocraft 多智能体群聊 —— 服务端接口完备性自检。

对照 `C:\Users\TWG\Desktop\cocraft-多智能体群聊_服务端新增接口需求_v1.md` 的契约：
  * **结构化核对**（不需要登录）：拉 `/openapi.json`，检查「端点是否存在」「schema 字段是否齐」；
  * **运行时核对**（需要 token）：`/auth/me`、建/删 agent、房间成员、消息 `after` 等。

用法（面板服务在 8787；也可直接给 base）：
    python tools\cocraft_agents_check.py                       # 只做结构化核对
    python tools\cocraft_agents_check.py --token <cocraft_token>   # 加上运行时核对
    python tools\cocraft_agents_check.py --base https://www.animaker.cc.cd
环境变量也可：`COCRAFT_TOKEN` / `OPENCODE_UI_COCRAFT_BASE`。

⚠ 只读为主；只有给了 token 才会**建一个名为 cocraft_check_<随机> 的测试 agent 随后删掉**。
输出纯 ASCII（避免控制台 GBK 乱码）。
"""
from __future__ import annotations

import json
import os
import random
import string
import sys
import urllib.error
import urllib.request

DEFAULT_BASE = os.environ.get("OPENCODE_UI_COCRAFT_BASE") or "http://127.0.0.1:8787/cocraft"


def http(base, path, token=None, method="GET", body=None, timeout=20):
    url = base.rstrip("/") + path
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
        except Exception:
            pass
        return e.code, raw
    except Exception as e:  # noqa: BLE001
        return None, str(e)


class Report:
    def __init__(self):
        self.ok = self.bad = 0
        self.missing = []

    def check(self, name, cond, detail=""):
        if cond:
            self.ok += 1
            print("  [OK ] %s" % name)
        else:
            self.bad += 1
            self.missing.append(name)
            print("  [BAD] %s %s" % (name, ("<%s>" % detail) if detail else ""))


def schema_props(oas, name):
    return set((oas.get("components", {}).get("schemas", {}).get(name, {}) or {}).get("properties", {}).keys())


def structural(oas, r: Report):
    paths = oas.get("paths", {})
    print("\n== 端点是否存在 ==")
    need = [
        ("POST", "/agents"), ("GET", "/agents"),
        ("PATCH", "/agents/{agent_id}"), ("DELETE", "/agents/{agent_id}"),
        ("POST", "/agents/{agent_id}/heartbeat"), ("POST", "/agents/{agent_id}/token/rotate"),
        ("POST", "/chats/{chat_id}/agents"), ("DELETE", "/chats/{chat_id}/agents/{agent_id}"),
        ("GET", "/chats/{chat_id}/members"),
        ("GET", "/chats/{chat_id}/messages"), ("POST", "/chats/{chat_id}/messages"),
    ]
    for method, p in need:
        has = p in paths and method.lower() in {k.lower() for k in paths[p].keys()}
        r.check("%-6s %s" % (method, p), has)

    # messages 的 after 游标
    msg_params = [q.get("name") for q in (paths.get("/chats/{chat_id}/messages", {}).get("get", {}).get("parameters") or [])]
    r.check("GET /messages 有 after 游标", "after" in msg_params, str(msg_params))

    print("\n== schema 字段 ==")
    r.check("AgentRead.name/display/machine/avatar/presence/last_seen",
            {"name", "display", "machine", "avatar", "presence", "last_seen"} <= schema_props(oas, "AgentRead"))
    r.check("AgentWithToken.agent_token", "agent_token" in schema_props(oas, "AgentWithToken"))
    r.check("AgentCreate.name/display/machine",
            {"name", "display", "machine"} <= schema_props(oas, "AgentCreate"))
    r.check("MessageRead.sender_kind/mentions/reply_to/hop/origin/client_msg_id",
            {"sender_kind", "mentions", "reply_to", "hop", "origin", "client_msg_id"} <= schema_props(oas, "MessageRead"))
    r.check("MessageSend.content/mentions/reply_to/client_msg_id",
            {"content", "mentions", "reply_to", "client_msg_id"} <= schema_props(oas, "MessageSend"))
    r.check("MemberRead.kind/name/username/avatar/presence",
            {"kind", "name", "username", "avatar", "presence"} <= schema_props(oas, "MemberRead"))
    r.check("ChatRead.agent_members/settings",
            {"agent_members", "settings"} <= schema_props(oas, "ChatRead"))

    print("\n== v2 端点（主持人 / 发言权）==")
    r.check("PUT/GET /chats/{chat_id}/moderator", "/chats/{chat_id}/moderator" in paths)
    r.check("POST /chats/{chat_id}/floor", "/chats/{chat_id}/floor" in paths)


def runtime(base, token, r: Report):
    print("\n== 运行时核对（需要 token）==")
    st, me = http(base, "/auth/me", token)
    r.check("GET /auth/me", st == 200, "%s %s" % (st, me))

    st, agents = http(base, "/agents", token)
    r.check("GET /agents", st == 200 and isinstance(agents, dict) and "items" in agents, "%s %s" % (st, str(agents)[:120]))

    name = "check_" + "".join(random.choice(string.ascii_lowercase) for _ in range(6))
    st, created = http(base, "/agents", token, "POST", {"name": name, "display": "自检", "machine": "tester"})
    ok = st == 200 and isinstance(created, dict) and created.get("agent_token") and (created.get("agent") or {}).get("id")
    r.check("POST /agents -> agent + agent_token", ok, "%s %s" % (st, str(created)[:160]))
    aid = (created.get("agent") or {}).get("id") if isinstance(created, dict) else None
    if aid:
        st, hb = http(base, "/agents/%s/heartbeat" % aid, token, "POST")
        r.check("POST /agents/{id}/heartbeat", st == 200, "%s %s" % (st, hb))
        st, rot = http(base, "/agents/%s/token/rotate" % aid, token, "POST")
        r.check("POST /agents/{id}/token/rotate", st == 200, "%s %s" % (st, rot))
        st, dele = http(base, "/agents/%s" % aid, token, "DELETE")
        r.check("DELETE /agents/{id}（清理）", st in (200, 204), "%s %s" % (st, dele))

    st, chats = http(base, "/chats", token)
    if isinstance(chats, list):
        items = chats
    elif isinstance(chats, dict):
        items = chats.get("data") or chats.get("items")
    else:
        items = None
    r.check("GET /chats", st == 200, "%s %s" % (st, str(chats)[:120]))
    if isinstance(items, list) and items:
        cid = (items[0] or {}).get("id")
        if cid:
            st, mem = http(base, "/chats/%s/members" % cid, token)
            r.check("GET /chats/{id}/members -> items", st == 200 and "items" in (mem or {}), "%s %s" % (st, str(mem)[:120]))
            st, page = http(base, "/chats/%s/messages?after=0&limit=5" % cid, token)
            r.check("GET /chats/{id}/messages?after=0", st == 200 and "items" in (page or {}), "%s %s" % (st, str(page)[:120]))


def main() -> int:
    args = sys.argv[1:]
    base = DEFAULT_BASE
    token = os.environ.get("COCRAFT_TOKEN") or ""
    if "--base" in args:
        base = args[args.index("--base") + 1]
    if "--token" in args:
        token = args[args.index("--token") + 1]

    print("base :", base)
    print("token:", ("(provided)" if token else "(none -> only structural checks)"))
    r = Report()
    st, oas = http(base, "/openapi.json")
    if st != 200 or not isinstance(oas, dict):
        print("拿不到 openapi.json：", st, str(oas)[:200])
        return 1
    structural(oas, r)
    if token:
        runtime(base, token, r)
    else:
        print("\n== 运行时核对 ==")
        print("  [SKIP] 没有 token：登录 cocraft 后把 token 传进来（--token / COCRAFT_TOKEN）再跑")

    print("\n==== 结果：OK %d / 缺 %d ====" % (r.ok, r.bad))
    if r.missing:
        print("缺失/未过：")
        for m in r.missing:
            print("  -", m)
    return 0 if r.bad == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
