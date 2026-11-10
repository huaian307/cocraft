# -*- coding: utf-8 -*-
"""新建项目的「根目录」怎么定（引擎无关）。

用户 2026-11-05 的要求：
  ① 接入的 agent **有自己的客户端 / 桌面端工作目录** → 项目建在它下面（`<home>/projects`）；
  ② 没有（随包的 node 适配器 / 面板内 npm 装的 agent）→ 建在 **cocraft 里**，
     **按创建时用的 agent 分类**：`<cocraft>/runtime/projects/<agent-id>/`；
  ③ **工作流**的项目一律进 cocraft 的专用大文件夹：`<cocraft>/runtime/workflow-projects/`
     （每个项目一个子目录，所有成员共用）。

可覆盖（不想用默认时）`runtime/state/_project_roots.json`：
  {
    "cocraft":    "D:/projects/cocraft",        # ② 的父目录（默认 runtime/projects）
    "workflow":   "D:/projects/workflows",      # ③ 的父目录（默认 runtime/workflow-projects）
    "agents": {                                  # 按 agent 强制指定（最高优先级）
      "codex-node": "D:/projects/codex",
      "opencode-acp": "C:/Users/me/Documents/oc"
    }
  }

⚠ 这里只决定"默认建在哪"；用户点「选择位置…」随时可以改。
"""
from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "runtime", "state")
OVERRIDE_FILE = os.path.join(STATE_DIR, "_project_roots.json")

# 客户端 / 桌面端「工作目录」的判定（按 agent 类型）
_CLIENT_HOME = {
    "codex": ("~/.codex",),
    "claude": ("~/.claude",),
    "opencode": ("~/.local/share/opencode",),
}


def _home_dirs() -> list:
    """所有可能的用户目录（存在、去重）。

    ⚠ 与 engines/opencode.py 同款：安装版进程可能缺 USERPROFILE/HOME，
      `expanduser("~")` 会回字面量 `'~'`（踩坑 #38）。
    """
    cands = []
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v:
            cands.append(v)
    hd, hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if hd and hp:
        cands.append(hd.rstrip("\\/") + hp)
    la = os.environ.get("LOCALAPPDATA") or ""
    tail = os.path.join("AppData", "Local")
    if la.lower().endswith(tail.lower()):
        cands.append(la[: -len(tail)].rstrip("\\/"))
    try:
        exp = os.path.expanduser("~")
        if exp and exp != "~":
            cands.append(exp)
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for p in cands:
        try:
            p = os.path.normpath(str(p))
        except Exception:  # noqa: BLE001
            continue
        if p and p.lower() not in seen and os.path.isdir(p):
            seen.add(p.lower())
            out.append(p)
    return out


def _overrides() -> dict:
    try:
        with open(OVERRIDE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _expand(p) -> str:
    s = str(p or "").strip()
    if not s:
        return ""
    s = os.path.expandvars(os.path.expanduser(s))
    return os.path.abspath(s)


def agent_kind(agent_id: str, cfg: dict = None) -> str:
    """按 agent id / 命令判断它属于哪家（codex / claude / opencode / ""）。"""
    aid = str(agent_id or "").lower()
    cmd = " ".join(str(x) for x in ((cfg or {}).get("command") or [])).lower()
    hay = aid + " " + cmd
    for k in ("codex", "claude", "opencode"):
        if k in hay:
            return k
    return ""


def client_home(kind: str, cfg: dict = None) -> str:
    """该 agent「自己的客户端/桌面端工作目录」；没有 / 不存在 → ""。

    · codex  → `$CODEX_HOME`（注册表 env 里那个）或 `~/.codex`
    · claude → `$CLAUDE_CONFIG_DIR` 或 `~/.claude`
    · opencode → `$XDG_DATA_HOME/opencode` 或 `~/.local/share/opencode`

    ⚠ 后两个 env 很重要：**随包的 opencode-node** 会把 `XDG_DATA_HOME` 指到
      `<app>/runtime/opencode-data`（不碰你自己的 OpenCode）→ 它的项目就该落在 cocraft 里，
      而不是用户真实的 `~/.local/share/opencode`。
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    if kind == "codex":
        p = _expand(env.get("CODEX_HOME"))
        if p and os.path.isdir(p):
            return p
    if kind == "claude":
        p = _expand(env.get("CLAUDE_CONFIG_DIR"))
        if p and os.path.isdir(p):
            return p
    if kind == "opencode":
        xdg = _expand(env.get("XDG_DATA_HOME"))
        if xdg:
            p = os.path.join(xdg, "opencode")
            if os.path.isdir(p):
                return p
    for pat in _CLIENT_HOME.get(kind) or ():
        rel = pat[2:] if pat.startswith("~/") else ""
        for h in _home_dirs():
            p = os.path.join(h, rel.replace("/", os.sep)) if rel else _expand(pat)
            if p and os.path.isdir(p):
                return os.path.abspath(p)
    return ""


def resolve(scope: str = "session", agent_id: str = "", cfg: dict = None) -> dict:
    """算出「新建项目」的默认父目录（并确保它存在）。

    返回 `{root, source, label, scope, agent}`：
      · source=`client`   → 用的 agent 自己的客户端目录（`<home>/projects`）
      · source=`cocraft`  → cocraft 的 `runtime/projects/<agent-id>`（按 agent 分类）
      · source=`workflow` → cocraft 的 `runtime/workflow-projects`（工作流专用大文件夹）
      · source=`override` → `_project_roots.json` 里手动指定的
    """
    ov = _overrides()
    scope = "workflow" if str(scope or "").strip().lower() == "workflow" else "session"
    agent_id = str(agent_id or "").strip()

    if scope == "workflow":
        root = _expand(ov.get("workflow")) or os.path.join(ROOT, "runtime", "workflow-projects")
        out = {"root": root, "source": "workflow",
               "label": "cocraft 工作流项目目录（所有成员共用）",
               "scope": scope, "agent": agent_id}
    else:
        over_agents = ov.get("agents") if isinstance(ov.get("agents"), dict) else {}
        hit = _expand(over_agents.get(agent_id)) if agent_id else ""
        if hit:
            out = {"root": hit, "source": "override",
                   "label": "自定义目录（%s）" % agent_id, "scope": scope, "agent": agent_id}
        else:
            kind = agent_kind(agent_id, cfg)
            home = client_home(kind, cfg)
            if home:
                out = {"root": os.path.join(home, "projects"), "source": "client",
                       "label": "%s 自己的目录" % (kind or "agent"), "scope": scope,
                       "agent": agent_id}
            else:
                base = _expand(ov.get("cocraft")) or os.path.join(ROOT, "runtime", "projects")
                out = {"root": os.path.join(base, agent_id or "default"), "source": "cocraft",
                       "label": "cocraft（按 agent 分类）", "scope": scope, "agent": agent_id}
    try:
        os.makedirs(out["root"], exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return out
