# -*- coding: utf-8 -*-
r"""模型凭据（多 Key）—— 一个用户可以同时保存多个 API Key，并按 agent 选择用哪一个。

为什么要有它（2026-10-23）：
  * 以前面板只有「一个 Key」输入框（写进 baseline.env）：换 agent 输入框跟着换，
    用户以为只存了一个；**同一家两个账号（个人 / 公司）根本没法共存** —— 都叫
    `DEEPSEEK_API_KEY`，后写的覆盖前一个。
  * 现在把 Key 收进独立凭据表：
        runtime/state/_credentials.json
        {"v":1,"migrated":true,
         "items":[{"id","label","env","provider","secret","created"}...],
         "defaults":{"DEEPSEEK_API_KEY":"c-..."}}
    再按「agent 覆盖 → 全局默认 → 旧 baseline.env」解析成环境变量交给子进程。

安全约定（别退回去）：
  * 任何对外返回（`list_public()`）**只给掩码**，明文只在本机文件里；
  * 迁移时把 `baseline.env` 里看起来像密钥的键**搬进凭据表**（并从 baseline 删掉），
    否则「删掉凭据」会被 baseline 里的旧值顶回来；
  * 迁移前备份 `_acp_agents.json`（`.bak-before-credentials`）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid

from .. import STATE_DIR
from . import agents

CRED_FILE = os.path.join(STATE_DIR, "_credentials.json")
LOCK = threading.RLock()

# 环境变量名：大写字母开头，只允许 A-Z0-9_
ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# 像密钥的变量（迁移只搬这些；BASE_URL / 代理 / 权限模式一律留在 baseline）
SECRET_ENV_RE = re.compile(r"(^|_)(API_?KEY|TOKEN|SECRET|PASSWORD)$", re.I)

_ENV_BRAND = {
    "DEEPSEEK": ("DeepSeek", "deepseek"),
    "OPENAI": ("OpenAI", "openai"),
    "ANTHROPIC": ("Anthropic", "anthropic"),
    "GEMINI": ("Google", "google"),
    "GOOGLE": ("Google", "google"),
    "XAI": ("xAI", "xai"),
    "MOONSHOT": ("Moonshot", "moonshotai"),
    "DASHSCOPE": ("通义", "alibaba"),
    "ZHIPUAI": ("智谱", "zhipuai"),
    "MISTRAL": ("Mistral", "mistral"),
    "GROQ": ("Groq", "groq"),
}


def _now() -> int:
    return int(time.time() * 1000)


def _load() -> dict:
    try:
        with open(CRED_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            d.setdefault("v", 1)
            d.setdefault("migrated", False)
            d.setdefault("items", [])
            d.setdefault("defaults", {})
            if not isinstance(d["items"], list):
                d["items"] = []
            if not isinstance(d["defaults"], dict):
                d["defaults"] = {}
            return d
    except Exception:                       # noqa: BLE001  坏文件当空表（别把面板带崩）
        pass
    return {"v": 1, "migrated": False, "items": [], "defaults": {}}


def _save(d: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = CRED_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    try:
        if os.path.isfile(CRED_FILE):
            shutil.copy2(CRED_FILE, CRED_FILE + ".bak")
    except Exception:                       # noqa: BLE001
        pass
    os.replace(tmp, CRED_FILE)


def _new_id(d: dict) -> str:
    used = {str(x.get("id")) for x in (d.get("items") or [])}
    for _ in range(20):
        cid = "c-" + uuid.uuid4().hex[:10]
        if cid not in used:
            return cid
    return "c-" + uuid.uuid4().hex


def _brand_of(env: str) -> tuple:
    """`DEEPSEEK_API_KEY` → ("DeepSeek", "deepseek")；认不出就回 ("<env>", "")."""
    e = str(env or "").upper()
    e = re.sub(r"(_API_?KEY|_TOKEN|_SECRET|_PASSWORD|_KEY)$", "", e)
    head = e.split("_")[0] if e else ""
    if head in _ENV_BRAND:
        return _ENV_BRAND[head]
    return (str(env or ""), "")


def mask(value: str) -> str:
    return agents.mask_secret(value)


def _find(d: dict, cid: str) -> dict:
    for it in (d.get("items") or []):
        if str(it.get("id")) == str(cid):
            return it
    return {}


def ensure_migrated() -> dict:
    """把旧 `baseline.env` 里的密钥搬进凭据表（只做一次）。"""
    with LOCK:
        d = _load()
        if d.get("migrated"):
            return d
        reg = agents.load_registry()
        b = reg.get("baseline") if isinstance(reg.get("baseline"), dict) else {}
        env = dict(b.get("env") or {})
        moved = {k: str(v).strip() for k, v in list(env.items())
                 if SECRET_ENV_RE.search(str(k)) and str(v or "").strip()}
        if moved:
            try:
                if os.path.isfile(agents.AGENTS_FILE):
                    shutil.copy2(agents.AGENTS_FILE,
                                 agents.AGENTS_FILE + ".bak-before-credentials")
            except Exception:               # noqa: BLE001
                pass
            have = {(str(x.get("env")), str(x.get("secret"))) for x in (d.get("items") or [])}
            for k, v in moved.items():
                env.pop(k, None)            # ★搬走，别留在 baseline（否则删凭据会被旧值顶回来）
                if (k, v) in have:
                    continue
                label, provider = _brand_of(k)
                it = {"id": _new_id(d), "label": label, "env": k, "provider": provider,
                      "secret": v, "created": _now()}
                d["items"].append(it)
                d["defaults"].setdefault(k, it["id"])
            b["env"] = env
            reg["baseline"] = b
            agents.save_registry(reg)
        d["migrated"] = True
        _save(d)
        return d


def env_for(entry) -> dict:
    """某个 agent 实际要注入的环境变量（密钥部分）。

    优先级：agent 覆盖（`entry.credentials = {ENV: credId}`）> 全局默认。
    """
    d = ensure_migrated()
    byid = {str(x.get("id")): x for x in (d.get("items") or [])}
    out = {}
    for env, cid in (d.get("defaults") or {}).items():
        it = byid.get(str(cid))
        if it and str(it.get("env")) == str(env):
            out[str(env)] = str(it.get("secret") or "")
    for env, cid in ((entry or {}).get("credentials") or {}).items():
        it = byid.get(str(cid))
        if it and str(it.get("secret") or ""):
            out[str(env)] = str(it["secret"])
    return out


def list_public() -> dict:
    """给界面看的凭据清单：**只有掩码**，外加「默认」和「哪些 agent 在用」。"""
    d = ensure_migrated()
    reg = agents.load_registry()
    used = {}
    for a in (reg.get("agents") or []):
        for env, cid in (a.get("credentials") or {}).items():
            used.setdefault(str(cid), []).append(str(a.get("id")))
    items = []
    for it in sorted(d.get("items") or [],
                     key=lambda x: (str(x.get("env") or ""), str(x.get("label") or ""))):
        env = str(it.get("env") or "")
        items.append({
            "id": str(it.get("id") or ""),
            "label": str(it.get("label") or env),
            "env": env,
            "provider": str(it.get("provider") or ""),
            "masked": mask(str(it.get("secret") or "")),
            "isDefault": str((d.get("defaults") or {}).get(env) or "") == str(it.get("id") or ""),
            "usedBy": used.get(str(it.get("id") or ""), []),
            "created": int(it.get("created") or 0),
        })
    return {"items": items,
            "defaults": dict(d.get("defaults") or {}),
            "envNames": sorted({str(x.get("env") or "") for x in (d.get("items") or [])
                                if x.get("env")})}


def _check_env(env: str) -> str:
    env = str(env or "").strip().upper()
    if not ENV_RE.match(env):
        raise ValueError("环境变量名不合法（大写字母开头，只允许 A-Z0-9_）：%s" % (env or "(空)"))
    return env


def add(label: str, env: str, secret: str, provider: str = "") -> dict:
    env = _check_env(env)
    secret = str(secret or "").strip()
    if not secret:
        raise ValueError("Key 不能为空")
    label = str(label or "").strip() or _brand_of(env)[0]
    provider = str(provider or "").strip() or _brand_of(env)[1]
    with LOCK:
        d = ensure_migrated()
        it = {"id": _new_id(d), "label": label, "env": env, "provider": provider,
              "secret": secret, "created": _now()}
        d["items"].append(it)
        if env not in (d.get("defaults") or {}):
            d["defaults"][env] = it["id"]
        _save(d)
    return {"id": it["id"], "label": label, "env": env, "masked": mask(secret)}


def update(cid: str, patch: dict) -> dict:
    with LOCK:
        d = ensure_migrated()
        it = _find(d, cid)
        if not it:
            raise KeyError("没有这条凭据：%s" % cid)
        old_env = str(it.get("env") or "")
        if "label" in patch and patch.get("label") is not None:
            it["label"] = str(patch.get("label") or "").strip() or old_env
        if "env" in patch and patch.get("env") is not None:
            it["env"] = _check_env(patch.get("env"))
        if "provider" in patch and patch.get("provider") is not None:
            it["provider"] = str(patch.get("provider") or "").strip()
        if "secret" in patch and patch.get("secret") is not None:
            sec = str(patch.get("secret") or "").strip()
            if not sec:
                raise ValueError("Key 不能为空（要删除请用删除操作）")
            it["secret"] = sec
        new_env = str(it.get("env") or "")
        if new_env != old_env:
            # 默认指向跟着改；agent 的覆盖映射也把 env 键迁过去
            defs = d.get("defaults") or {}
            if defs.get(old_env) == str(it.get("id")):
                defs.pop(old_env, None)
                defs[new_env] = str(it.get("id"))
            d["defaults"] = defs
            reg = agents.load_registry()
            changed = False
            for a in (reg.get("agents") or []):
                cm = a.get("credentials") or {}
                if old_env in cm and str(cm.get(old_env)) == str(it.get("id")):
                    cm.pop(old_env, None)
                    cm[new_env] = str(it.get("id"))
                    a["credentials"] = cm
                    changed = True
            if changed:
                agents.save_registry(reg)
        _save(d)
    return list_public()


def delete(cid: str) -> dict:
    with LOCK:
        d = ensure_migrated()
        it = _find(d, cid)
        if not it:
            raise KeyError("没有这条凭据：%s" % cid)
        env = str(it.get("env") or "")
        d["items"] = [x for x in (d.get("items") or []) if str(x.get("id")) != str(cid)]
        defs = d.get("defaults") or {}
        if defs.get(env) == str(cid):
            defs.pop(env, None)
            # 同 env 还有别的 → 自动选一个当默认（别让用户以为"没默认可用了"）
            nxt = next((x for x in (d.get("items") or []) if str(x.get("env")) == env), None)
            if nxt:
                defs[env] = str(nxt.get("id"))
        d["defaults"] = defs
        _save(d)
        reg = agents.load_registry()
        changed = False
        for a in (reg.get("agents") or []):
            cm = a.get("credentials") or {}
            if str(cm.get(env)) == str(cid):
                cm.pop(env, None)
                a["credentials"] = cm
                changed = True
        if changed:
            agents.save_registry(reg)
    return list_public()


def set_default(cid: str) -> dict:
    with LOCK:
        d = ensure_migrated()
        it = _find(d, cid)
        if not it:
            raise KeyError("没有这条凭据：%s" % cid)
        env = str(it.get("env") or "")
        d.setdefault("defaults", {})[env] = str(cid)
        _save(d)
    return list_public()


def describe() -> dict:
    """给诊断/前端用的一句话摘要（不含明文）。"""
    d = ensure_migrated()
    return {"count": len(d.get("items") or []),
            "envNames": sorted({str(x.get("env") or "") for x in (d.get("items") or [])})}
