# -*- coding: utf-8 -*-
"""引擎注册表 —— 把「跟哪个 agent 对话」抽象成可替换的实现。

约定：server.py 里所有跟 agent 有关的语义（/api/* 反代、/healthz 探测、
启动前置检查）都改成先问「当前引擎」，再由引擎实现。默认引擎是 opencode，
所以在没有切换之前，行为与改造前完全一致。

状态：runtime/state/_engine.json  {"engine": "opencode"}
      文件不存在 / 内容非法 / 引擎未注册 → 一律回落到 opencode。

新增引擎时：写一个 backend/engines/<id>.py，实现 base.Engine，
然后在下面的 registry() 里登记即可（server.py 不需要再改）。
"""
from __future__ import annotations

import json
import os
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKEND_DIR)
STATE_DIR = os.path.join(ROOT, "runtime", "state")
ENGINE_STATE_FILE = os.path.join(STATE_DIR, "_engine.json")

DEFAULT_ENGINE = "opencode"
ENV_ENGINE = "OPENCODE_UI_ENGINE"      # 环境变量可临时覆盖当前引擎（联调用）


def registry() -> dict:
    """引擎 id → 类。延迟 import，避免与 server.py 形成循环依赖。

    单个引擎 import 失败不影响其它引擎 —— 免得一个坏模块把整个服务带崩。
    """
    reg = {}
    try:
        from .opencode import OpenCodeEngine
        reg[OpenCodeEngine.id] = OpenCodeEngine
    except Exception:  # noqa: BLE001
        pass
    try:
        from .acp.engine import AcpEngine
        reg[AcpEngine.id] = AcpEngine
    except Exception:  # noqa: BLE001
        pass
    return reg


def list_engines() -> list:
    """已注册的引擎 id（给 /engine/status 用）。"""
    return sorted(registry().keys())


def describe() -> list:
    """每个引擎的自述：`[{id,label,available,error}]`。

    为什么需要：`list_engines()` 只是"注册过的 id"，**不代表能用**（OpenCode 没装、
    ACP 没配 agent 时都只是注册着）。首次设置向导 / 设置弹窗 / 守护进程要能看出
    "哪个真的能用"，否则会选到（或死等）一个打不通的引擎。单个引擎探测失败不影响其它引擎。
    """
    out = []
    for eid, cls in sorted(registry().items()):
        item = {"id": eid, "label": eid, "available": True, "error": ""}
        try:
            eng = get_engine(eid)
            item["label"] = str(getattr(eng, "label", "") or eid)
            item["available"] = bool(eng.available())
        except Exception as exc:  # noqa: BLE001
            item["available"] = False
            item["error"] = "%s: %s" % (type(exc).__name__, exc)
        out.append(item)
    return out


def first_available_engine() -> str:
    """第一个"真的能用"的引擎 id（找不到就回空）。

    用途：`_engine.json` 缺失/非法时的兜底 —— **不再写死 opencode**（没装 OpenCode 的机器
    会落到一个打不通的引擎上）。优先 opencode（本机装了就用它，保持老行为），
    否则取第一个可用的；一个都没有时才回落到 `DEFAULT_ENGINE` 让页面起来报错。
    """
    desc = {e["id"]: e for e in describe()}
    if desc.get(DEFAULT_ENGINE, {}).get("available"):
        return DEFAULT_ENGINE
    for eid in sorted(desc):
        if desc[eid].get("available"):
            return eid
    return ""


def is_registered(engine_id: str) -> bool:
    return engine_id in registry()


def active_engine_id() -> str:
    """当前引擎 id；任何异常/未配置都回落到**第一个可用的**引擎。

    环境变量 OPENCODE_UI_ENGINE 优先于状态文件 —— 方便 `tools/acp_demo.py`
    起一个独立服务做联调，而不影响线上面板用的引擎。

    ⚠ 以前这里（以及 `DEFAULT_ENGINE`）写死 `opencode`：没装 OpenCode 的机器会落在
      一个打不通的引擎上。现在状态文件缺失/非法时改成 `first_available_engine()`
      —— 本机装了 OpenCode 仍是它（老行为不变），只有它不可用时才落到 acp。
    """
    env = os.environ.get(ENV_ENGINE, "").strip()
    if env and is_registered(env):
        return env
    try:
        with open(ENGINE_STATE_FILE, "r", encoding="utf-8") as fh:
            eid = str(json.load(fh).get("engine") or "").strip()
    except Exception:  # noqa: BLE001
        eid = ""
    if is_registered(eid):
        return eid
    return first_available_engine() or DEFAULT_ENGINE


def set_active_engine_id(engine_id: str) -> bool:
    """切换当前引擎；未注册的 id 一律拒绝。返回是否成功。"""
    engine_id = str(engine_id or "").strip()
    if not is_registered(engine_id):
        return False
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = ENGINE_STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"engine": engine_id}, fh, ensure_ascii=False)
    os.replace(tmp, ENGINE_STATE_FILE)
    return True


_instances = {}
_instances_lock = threading.Lock()


def get_engine(engine_id: str = None):
    """按 id 取引擎实例；id 为空/未注册则取当前（或默认）引擎。

    ⚠ 必须**缓存单例**：引擎（尤其 ACP）持有会话/进程等状态，每个请求新建实例
    会把这些状态丢掉。
    """
    reg = registry()
    eid = engine_id if engine_id in reg else active_engine_id()
    with _instances_lock:
        inst = _instances.get(eid)
        if inst is None:
            inst = reg[eid]()
            _instances[eid] = inst
    return inst


def active_engine():
    return get_engine()


def existing_engine(engine_id: str):
    """**已经创建过**的引擎实例（没有就回 None）。

    为什么要它：有些"顺手收一下子进程"的调用（例如面板内更新 agent 前释放文件占用）不该
    为了这件事凭空造一个引擎（造 ACP 引擎会拉起/持有会话与子进程状态）。只有本来就在用的
    实例才值得去动它。
    """
    with _instances_lock:
        return _instances.get(str(engine_id or ""))
