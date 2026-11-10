# -*- coding: utf-8 -*-
r"""会话归属登记（只读扫描）：把「工作流成员 / 群聊 AI 成员」的内部会话
从单聊会话列表里排除。

数据不新造一份：直接扫
  * `runtime/state/_cocraft_bridge_sessions.json`（群聊桥）
  * `runtime/state/_orchestrator/runs/*/*.sessions.json`（工作流成员）
拿到会话 id 集合。15 秒缓存，避免每次轮询都扫盘。

⚠ 这里**不做写操作**；新会话标题仍由创建方写进自己的 sessions 文件。
"""
from __future__ import annotations

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "runtime", "state")
BRIDGE_SESSIONS = os.path.join(STATE, "_cocraft_bridge_sessions.json")
RUNS_DIR = os.path.join(STATE, "_orchestrator", "runs")

_CACHE = {"ts": 0.0, "ids": set()}


def _take(path: str, ids: set) -> None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:  # noqa: BLE001
        return
    for s in (d.get("sessions") or []):
        if not isinstance(s, dict):
            continue
        for k in ("id", "agentSessionId"):
            v = s.get(k)
            if v:
                ids.add(str(v))


def managed_ids() -> set:
    now = time.time()
    if now - float(_CACHE.get("ts") or 0) < 15:
        return set(_CACHE.get("ids") or set())
    ids = set()
    _take(BRIDGE_SESSIONS, ids)
    try:
        for run in os.listdir(RUNS_DIR):
            d = os.path.join(RUNS_DIR, run)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if fn.endswith(".sessions.json"):
                    _take(os.path.join(d, fn), ids)
    except OSError:
        pass
    _CACHE["ts"] = now
    _CACHE["ids"] = ids
    return set(ids)


def is_managed(sid: str) -> bool:
    return str(sid or "") in managed_ids()


def invalidate() -> None:
    _CACHE["ts"] = 0.0
    _CACHE["ids"] = set()
