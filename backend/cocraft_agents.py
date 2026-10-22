# -*- coding: utf-8 -*-
r"""群聊「AI 成员」—— 把面板里的 ACP agent 接进 cocraft 群聊（引擎无关的 cocraft 侧桥）。

背景（见桌面 `cocraft-多智能体群聊_服务端接口需求_v3.md`）：
  * cocraft 服务端只做**路由**：谁被 `@` 就给谁的 WebSocket 推 `mention`；
    房里有主持人时，人类发的、**没有 @ 任何人**的消息会推 `moderator.route` 给主持人。
  * "agent 本体"要有人来跑：拿 `agent_token` 连上 WS，被叫到时生成回复再发回群里。

这里就是那个"跑 agent 的人"：
  1. **管理**（用**用户令牌**调上游）：建 agent（`POST /agents`）、进/退房（`POST|DELETE /chats/{id}/agents`）、删 agent。
  2. **运行时**：每个启用的成员一个线程 → 用它的 `agent_token` 连 `wss://<host>/ws`；
     收到 `mention` / `moderator.route` 时，用它**绑定的 ACP agent**（claude / codex / opencode…）
     生成回复，回帖 `{"type":"message", chat_id, content, reply_to}`。
  3. **隔离**：驱动 ACP agent 用的是**独立的 AcpService**（独立会话文件），
     不动用户在面板里当前选中的那个 agent，也不把桥的会话混进面板会话列表。

状态：`runtime/state/_cocraft_agents.json`
    {"agents":[{id,name,display,agent_token,engine,chats:[..],enabled:bool,created}]}
⚠ 这个文件**含 agent_token**（等于 agent 的登录凭据）——只在本机，不进安装包。

纯标准库（TLS 走 netsafe 的证书兜底）。
"""
from __future__ import annotations

import base64
import collections
import datetime
import json
import os
import queue
import random
import re
import socket
import ssl
import string
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import netsafe
import snapshots
from engines import STATE_DIR
from engines.acp import agents as acp_agents
from engines.acp.engine import acp_log
from engines.acp.service import AcpService, BusyError, _load_ok_models

BASE = (os.environ.get("OPENCODE_UI_COCRAFT_BASE") or "https://www.animaker.cc.cd").rstrip("/")
STATE_FILE = os.path.join(STATE_DIR, "_cocraft_agents.json")
BRIDGE_SESSIONS_FILE = os.path.join(STATE_DIR, "_cocraft_bridge_sessions.json")

_LOCK = threading.RLock()
_workers: dict = {}          # aid -> {"stop": Event, "thread": Thread, "running": bool}
_BRIDGE_LOCK = threading.RLock()
_bridges: dict = {}          # aid -> {"svc": AcpService, "sid": str}


# ---------------- 状态 ----------------

PERM_LEVEL_FILE = os.path.join(STATE_DIR, "_perm_level.json")   # 和面板同一份（设置里改了桥也跟着变）

_RE_READ = re.compile(r"read|search|fetch|think|list|glob|grep|view|cat|\bls\b|\bdir\b")
_RE_WRITE = re.compile(r"edit|write|create|delete|move|rename|patch|mkdir|\brm\b|touch|save")
_RE_EXEC = re.compile(r"execute|exec|bash|shell|\brun\b|command|terminal|powershell|\bcmd\b")
# ★「联网」——**引擎无关**：不同 agent 的措辞不一样（codex 的联网命令报的是 "Run command"，
#   claude / opencode 可能报 "network access to example.com" / "webfetch"…），所以只认**文本里网络相关的字样**，
#   外加"资源里带 `://`"（那不是文件路径，是 URL）。
#   它属于**独立能力**：**不受工作区约束**（网络不是路径）；等级只要不是「全部询问」就放行。
_RE_NET = re.compile(r"network|internet|联网|网络|外网|上网|域名|domain|\bhost\b|\burl\b|http|socket|"
                     r"\bproxy\b|\bweb\b|fetch|dns|curl|wget")

# ★ 破坏性 / 不可逆操作 —— **永不自动放行**（无论等级多宽），必须拥有者**明确回「确认」**。
#   2026-10-13：桌面被清空那次，就是"自动放行 + 弹窗只写 Run command（看不到命令）"两个问题叠加。
_RE_DESTRUCTIVE = re.compile(
    r"remove-item|\brm\b|\brd\b|rmdir|\bdel\b|\berase\b|delete|recycle|"
    r"\bformat\b|diskpart|shutdown|taskkill|stop-process|kill\b|reg\s+delete|"
    r"清空|删除|格式化|抹掉|重置|卸载", re.I)
_CONFIRM = re.compile(r"确认|確定|确定|confirm|我确认|真的删|确定执行", re.I)

# ---------------- ★主动编排（方向 2）：角色预设 ----------------
# 一个"角色"= 一段**只注入给这个成员自己**的职责说明（别的成员看不到 → 不污染群聊上下文）。
# ⚠ 角色的作用域是「这个 AI 成员」，不是「这次派活」：换派活场景就改角色。
ROLES = {
    "implementer": {
        "label": "实现者",
        "brief": ("职责：**动手改**。直接改代码 / 文件，改完给一句结论（改了哪些文件、为什么这么改）。"
                  "不要长篇解释，不要替别的角色去写测试或做审查。"),
    },
    "reviewer": {
        "label": "审查者",
        "brief": ("职责：**只挑错，不改代码**。指出具体文件 / 位置、说明为什么有问题、给最小修复建议。"
                  "输出格式：先给「结论：通过 / 不通过」，再列问题（每条：位置 → 问题 → 建议）；"
                  "没发现问题就直接说「没发现问题」。"),
    },
    "tester": {
        "label": "测试者",
        "brief": ("职责：**验证行为**。写 / 跑测试或给复现步骤，**贴真实命令与输出**。"
                  "输出格式：先给「结论：通过 / 失败」，再列用例（用例 → 预期 → 实际）；**别编输出**。"),
    },
    "researcher": {
        "label": "调研者",
        "brief": ("职责：**先查清楚再下结论**。给依据（文件路径 / 行号 / 命令输出 / 文档）。"
                  "不确定的地方明说不确定；**不要改代码**；用要点式，别写散文。"),
    },
}


def norm_role(role) -> str:
    """角色 id 归一：认得的才留，其余当空（＝普通成员）。"""
    r = str(role or "").strip().lower()
    return r if r in ROLES else ""


def role_line(role) -> str:
    """给 `_handle_event` 用：把角色职责拼成一段提示词（没有角色 → 空串）。"""
    r = norm_role(role)
    if not r:
        return ""
    meta = ROLES[r]
    return ("\n\n【你的角色：%s（%s）】\n%s\n"
            "（同一任务里可能有别的角色同时在干 —— **只做你这个角色该做的**，别抢别人的活。）"
            % (meta["label"], r, meta["brief"]))


def roles_public() -> dict:
    """给前端渲染下拉 / 小标用。"""
    return {k: {"label": v["label"]} for k, v in ROLES.items()}


# ---------------- ★主动编排：一键建组 ----------------
# 一个"组"= 几个角色 + 各自的**大脑（ACP agent）** + 自动进群 + 打开应答。
# 名字必须是 `@` 能解析的字符（前端 `ccSend` 用 `@([A-Za-z0-9_\-]+)`）。
TEAM_PRESETS = {
    "dev": {"label": "开发 + 审查 + 测试",
            "rows": [{"role": "implementer", "name": "impl"},
                     {"role": "reviewer", "name": "rev"},
                     {"role": "tester", "name": "test"}]},
    "review": {"label": "实现 + 审查（对拍）",
               "rows": [{"role": "implementer", "name": "impl"},
                        {"role": "reviewer", "name": "rev"}]},
    "research": {"label": "调研 + 审查",
                 "rows": [{"role": "researcher", "name": "scan"},
                          {"role": "reviewer", "name": "rev"}]},
}


def _custom_presets() -> dict:
    """用户自定义的组预设（存在 `_cocraft_agents.json` 的 `teamPresets` 里）。

    ⚠ 这是**用户数据**，不是安装包内容：跟着 runtime/state 走，覆盖安装也会保留。
    """
    v = _load().get("teamPresets")
    return dict(v) if isinstance(v, dict) else {}


def _save_custom_presets(m: dict) -> None:
    with _LOCK:
        d = _load()
        d["teamPresets"] = m
        _save(d)


def _preset_rows_public(rows) -> list:
    return [{"role": r.get("role"), "name": r.get("name"),
             "roleLabel": (ROLES.get(norm_role(r.get("role"))) or {}).get("label") or ""}
            for r in (rows or [])]


def team_presets() -> list:
    """内置 3 个 + 用户自定义（`builtin` 标出来：内置不能删/改，只能「另存一份」）。"""
    out = [{"id": k, "label": v["label"], "builtin": True,
            "rows": _preset_rows_public(v["rows"])}
           for k, v in TEAM_PRESETS.items()]
    for k, v in _custom_presets().items():
        if k in TEAM_PRESETS:            # 防御：内置 id 不许被影子覆盖
            continue
        out.append({"id": k, "label": v.get("label") or k, "builtin": False,
                    "rows": _preset_rows_public(v.get("rows") or [])})
    return out


def _slug_id(text) -> str:
    """预设 id：小写、非文字字符变 `-`（**保留中文**，用户在界面上不直接看到它）。"""
    s = re.sub(r"[\W]+", "-", str(text or "").strip().lower()).strip("-_")
    return (s or "custom")[:32]


def save_team_preset(label, rows, preset_id: str = "") -> dict:
    """★自定义组预设：把「当前这几行角色/名字」存下来（以后一键复用）。

    * `rows` = `[{role, name}]`（1~8 行；角色必须认得）；
    * `preset_id` 有 = 覆盖那个自定义预设；没有 = 按 label 造 id（撞了自动 `-2`）；
    * **内置 id 一律不许占用**（`dev` / `review` / `research`）。
    """
    label = str(label or "").strip()[:40]
    if not label:
        raise ValueError("给预设起个名字")
    if not isinstance(rows, list) or not rows:
        raise ValueError("预设至少要有一个角色")
    if len(rows) > 8:
        raise ValueError("一个组最多 8 个人（再多并行也没人看得过来）")
    clean = []
    for r in rows:
        role = norm_role((r or {}).get("role"))
        if not role:
            raise ValueError("有不认识的角色：%s" % ((r or {}).get("role"),))
        nm = _uniq_name((r or {}).get("name") or role, set())     # 只做字符清洗（重名留给建组时处理）
        clean.append({"role": role, "name": nm})
    pid = _slug_id(preset_id or label)
    customs = _custom_presets()
    if pid in TEAM_PRESETS:
        raise ValueError("「%s」是内置预设的 id，换个名字" % pid)
    if pid not in customs:                                        # 新建时撞 id → 加 -2 / -3
        base, n = pid, 2
        while pid in customs or pid in TEAM_PRESETS:
            pid = "%s-%d" % (base, n)
            n += 1
    customs[pid] = {"label": label, "rows": clean, "updated": int(time.time())}
    _save_custom_presets(customs)
    return {"id": pid, "label": label, "builtin": False,
            "rows": _preset_rows_public(clean)}


def delete_team_preset(preset_id) -> dict:
    """删一个**自定义**预设（内置的不许删 → ValueError；不存在 → KeyError）。"""
    pid = str(preset_id or "").strip()
    if pid in TEAM_PRESETS:
        raise ValueError("内置预设不能删（可以「存为预设」另存一份再改）")
    customs = _custom_presets()
    if pid not in customs:
        raise KeyError("没有这个预设：%s" % pid)
    customs.pop(pid)
    _save_custom_presets(customs)
    return {"deleted": pid}


def _uniq_name(base: str, taken: set) -> str:
    """`@` 友好的名字：只留 `[A-Za-z0-9_-]`；重名就加 `-2` / `-3`…"""
    s = re.sub(r"[^A-Za-z0-9_-]+", "", str(base or "").strip())[:24] or "ai"
    if s.lower() not in taken:
        return s
    n = 2
    while ("%s-%d" % (s, n)).lower() in taken and n < 99:
        n += 1
    return "%s-%d" % (s, n)


def team_plan(preset: str = "dev", engines: dict = None, names: dict = None,
              rows: list = None) -> list:
    """把「预设 + 每个角色选的大脑/名字」算成一张**要建什么**的清单（纯函数，好测）。

    * `rows`：前端**当前面板里那几行**（`[{role, name, engine}]`）—— 传了就以它为准，
      这样用户临时改过角色/名字也能直接建（不必先存成预设）；也天然支持**同一个角色出现两次**；
    * `engines`：`{role: agent_id}`（也认 `{"default": id}` 兜底）——**空就留空**（用基线默认）；
    * `names`：`{role: 名字}`（留空用预设名 / 行里的名字）；
    * **重名自动加后缀**（同一批内部 + 已有的成员都算）；
    * 角色不认识 → 抛 ValueError（别静默建出一个没有角色的人）。
    """
    engines = engines or {}
    names = names or {}
    if isinstance(rows, list) and rows:
        src = rows
        names = {}          # ⚠ 行模式：每行自带名字/引擎，别让按角色的 dict 覆盖（同角色多人时它会串）
        engines = {}
    else:
        pid = str(preset or "dev")
        p = TEAM_PRESETS.get(pid) or _custom_presets().get(pid)
        if not p:
            raise ValueError("不认识的组预设：%s" % preset)
        src = p["rows"]
    taken = {str(a.get("name") or "").lower() for a in _entries()}
    out = []
    for row in src:
        role = norm_role((row or {}).get("role"))
        if not role:
            raise ValueError("组预设里有不认识的角色：%s" % ((row or {}).get("role"),))
        nm = _uniq_name(names.get(role) or (row or {}).get("name") or role, taken)
        taken.add(nm.lower())
        eng = str((row or {}).get("engine") or engines.get(role)
                  or engines.get("default") or engines.get("") or "").strip()
        out.append({"role": role, "roleLabel": (ROLES.get(role) or {}).get("label") or "",
                    "name": nm, "display": nm, "engine": eng})
    return out


def create_team(user_token, chat_id, preset: str = "dev", engines=None, names=None,
                rows: list = None) -> dict:
    """按 `team_plan()` 真去建：**建 agent → 进群 → 打开应答**。一个失败不影响其它（逐条报结果）。"""
    plan = team_plan(preset, engines, names, rows)
    chat = str(chat_id or "").strip()
    if not chat:
        raise ValueError("要先选一个群（会话）")
    created, failed = [], []
    for row in plan:
        try:
            a = create_agent(user_token, row["name"], row["display"], row["engine"], row["role"])
            aid = (a or {}).get("id")
            if not aid:
                raise RuntimeError("建 agent 没回 id")
            try:
                join(user_token, aid, chat)
            except Exception as exc:                 # noqa: BLE001
                failed.append({"name": row["name"], "id": aid, "stage": "进群",
                               "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            try:
                set_enabled(aid, True)
            except Exception as exc:                 # noqa: BLE001
                failed.append({"name": row["name"], "id": aid, "stage": "启用",
                               "error": "%s: %s" % (type(exc).__name__, exc)})
                continue
            created.append({"id": aid, "name": row["name"], "role": row["role"],
                            "roleLabel": row["roleLabel"], "engine": row["engine"]})
            acp_log("[cocraft-agents] 建组：%s（%s / %s）已进群并开应答"
                    % (row["name"], row["role"], row["engine"] or "(默认引擎)"))
        except Exception as exc:                     # noqa: BLE001
            failed.append({"name": row["name"], "stage": "建 agent",
                           "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"ok": bool(created), "created": created, "failed": failed,
            "agents": list_agents()}


def _is_destructive(*texts) -> bool:
    for t in texts:
        if t and _RE_DESTRUCTIVE.search(str(t)):
            return True
    return False


def _looks_like_net(resources) -> bool:
    """资源里有没有 URL（`scheme://…`）—— 有就说明这是网络动作，不是文件路径。"""
    for x in (resources or []):
        s = str(x or "").strip()
        if s and "://" in s:
            return True
    return False


def _is_net(kind, action="", message="", resources=None) -> bool:
    """这个权限请求是不是「联网」？（引擎无关：只看文本里的网络字样 + 资源里的 URL）"""
    hay = ("%s %s %s" % (kind or "", action or "", message or "")).lower()
    return bool(_RE_NET.search(hay)) or _looks_like_net(resources)


def allow_network() -> bool:
    """群聊 agent 是否**一律允许联网**（默认 **true**）。

    可在 `_cocraft_agents.json` 顶层写 `"allow_network": false` 关掉 —— 关掉后回到"按等级"。
    ⚠ 只对**能被识别成联网**的请求生效；codex 的联网命令报的是 "Run command"（认不出），
      那种情况要靠等级（exec/all）或小弹窗批准 —— 见 AGENTS.md 的"联网"一节。
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            js = json.load(fh) or {}
        if "allow_network" in js:
            return bool(js.get("allow_network"))
    except Exception:  # noqa: BLE001
        pass
    return True


def perm_level() -> str:
    """面板的权限等级：ask / read / edit / exec / all（读不到就当 ask）。"""
    try:
        with open(PERM_LEVEL_FILE, "r", encoding="utf-8") as fh:
            v = str((json.load(fh) or {}).get("level") or "")
    except Exception:  # noqa: BLE001
        v = ""
    return v if v in ("ask", "read", "edit", "exec", "all") else "ask"


# ★方向 3 收尾：群聊成员可以有自己的「权限上限」（只会比面板更严，不会更宽）
PERM_LEVELS = ("ask", "read", "edit", "exec", "all")
PERM_RANK = {name: i for i, name in enumerate(PERM_LEVELS)}


def norm_member_perm(v) -> str:
    """成员自己的上限（空 = 跟随面板）。不认识的值一律当成"跟随面板"。"""
    v = str(v or "").strip()
    return v if v in PERM_RANK else ""


def effective_perm_level(a: dict) -> tuple:
    """返回 (生效等级, 人话来源)。

    规则：**上限只能收紧**。生效 = min(面板, 成员上限)；
    成员没设 → 跟随面板；上限比面板宽 → 仍按面板（不能靠"成员上限"给自己放权）。
    """
    panel = perm_level()
    cap = norm_member_perm((a or {}).get("permLevel"))
    if not cap:
        return panel, "跟随面板"
    if PERM_RANK[cap] < PERM_RANK[panel]:
        return cap, "成员上限 %s" % cap
    return panel, "面板更严（%s）" % panel


def _in_dir(paths, root) -> bool:
    """`paths` **全部**都在 `root` 里 → True。

    ⚠ **没给路径 / 给的是空 → False（范围未知，不能当成"区内"）**。
      2026-10-13 改：以前这里是 fail-open（空 = 区内），结果 codex —— 它**从不带路径**
      （`locations` 永远是 `[]`）—— 连"删掉桌面所有文件"都被当成"区内"自动放行 → 桌面 55 项被清空。
      "不知道"不能当"安全"：**未知就去问**。
    """
    if not paths:
        return False
    if not root:
        return False
    r = str(root).replace("\\", "/").rstrip("/").lower()
    for x in (paths or []):
        n = str(x or "").replace("\\", "/").lower()
        if not n:
            return False                      # 有一个空项 → 范围不明确
        if not (n == r or n.startswith(r + "/")):
            return False
    return True


def allowed_by_level(level, kind, action, message, resources, workspace) -> bool:
    """**和面板 app.js 的 `permAllowedByLevel` 逐条对应**（保持一致）。

    ★ 「联网」是**独立能力**（引擎无关）：判据只有"请求文本里网络相关的字样 / 资源里带 `://`"，
      **不套工作区约束**（网络不是文件路径）；等级只要不是「全部询问」就放行。
    """
    if not level or level == "ask":
        return False
    if level == "all":
        return True
    hay = ("%s %s %s" % (kind or "", action or "", message or "")).lower()
    if _is_net(kind, action, message, resources):
        return True                     # ★ 联网：不判工作区（独立能力）；read/edit/exec/all 都放行
    is_read = bool(_RE_READ.search(hay))
    is_write = bool(_RE_WRITE.search(hay))
    is_exec = bool(_RE_EXEC.search(hay))
    pure_read = is_read and not is_write and not is_exec
    ws = _in_dir(resources, workspace)          # ★ 现在"没给路径"→ False（范围未知 = 不能算区内）
    if level == "read":
        return pure_read
    if level == "edit":
        # 纯读不看路径（读本就是只读沙箱已有的能力）；**写 / 执行必须已知在区内**，未知就问
        return pure_read or ((is_read or is_write) and not is_exec and ws)
    if level == "exec":
        return pure_read or ((is_read or is_write or is_exec) and ws)
    return False


ASK_TIMEOUT = 90.0          # 在群里等拥有者批准的秒数（默认值）
_ASK_MIN, _ASK_MAX = 30.0, 900.0
_ASK_LOCK = threading.Lock()   # 同一时刻只处理一个权限申请
# ★ 建桥 / agent 冷启动那一刻，perm_ctx（WS + 群）往往还没就绪（`start_worker` 起 WS 线程的
#   同时**并发**建桥）。以前这里直接静默拒绝 → agent 的第一步动作莫名失败。现在**等一会儿**再问。
PERM_CTX_WAIT = 20.0        # 等 perm_ctx 就绪的上限（秒）


def ask_timeout() -> float:
    """等拥有者批准的秒数：默认 90；可在 `_cocraft_agents.json` 顶层写 `ask_timeout`（30~900）覆盖。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            v = float((json.load(fh) or {}).get("ask_timeout") or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if _ASK_MIN <= v <= _ASK_MAX else ASK_TIMEOUT

_APPROVE = re.compile(r"允许|同意|批准|可以|ok|yes|approve", re.I)
_DENY = re.compile(r"拒绝|不同意|不允许|禁止|不行|no|deny|cancel", re.I)


def _owner_id(aid) -> int:
    """agent 的拥有者（owner_user_id）；本地没记就去上游查一次。"""
    a = find(aid)
    try:
        oid = int(a.get("owner_user_id") or 0)
    except Exception:  # noqa: BLE001
        oid = 0
    if oid:
        return oid
    tok = _MEM.get("user_token") or ""
    if not tok:
        # ★ 没有用户令牌时**不做那次调用**：agent 自己的令牌调 GET /agents 只会拿到空列表
        #   （那是"我的 agent"接口，owner 才有意义）→ 白调还容易误判。
        return oid
    try:
        st, js = _api(tok, "GET", "/agents")
        items = (js.get("items") if isinstance(js, dict) else js) or []
        for x in items:
            if str(x.get("id")) != str(aid):
                continue
            cand = x.get("owner_user_id")
            if not cand:
                try:
                    cand = int(str(x.get("email") or 0))
                except Exception:  # noqa: BLE001
                    cand = 0
            if cand:
                oid = int(cand)
                break
    except Exception:  # noqa: BLE001
        oid = 0
    if oid:
        try:
            _patch(aid, owner_user_id=oid)
        except Exception:  # noqa: BLE001
            pass
    return oid


def _mentions_agent(m: dict, a: dict) -> bool:
    """这条消息是否 **@ 到了这个 agent**（内容里的 `@名字`，或服务端给的显式 `mentions`）。

    ★ 拥有者批准权限时**必须 @ 本 agent**（用户要求）：避免闲聊里的「可以」被当成批准。
    """
    names = [str(a.get("name") or "").lower(), str(a.get("display") or "").lower()]
    names = [n for n in names if n]
    if not names:
        return False
    c = str(m.get("content") or "").lower()
    for n in names:
        if ("@" + n) in c:
            return True
    aid = str(a.get("id"))
    for x in (m.get("mentions") or []):
        if str(x).lower() in names or str(x) == aid:
            return True
    return False


def _perm_ctx_now(aid, net=True):
    """当前能用来『把权限申请发到群里』的 `(owner, ws, chat)`。

    ★ 建桥那一刻 `perm_ctx` 还没写、成员的 WS 也可能还没连上 —— 这里做三路兜底：
      WS 取成员的活连接、群取成员配置的 `chats[0]`、owner 先读本地记录（读不到再问上游）。

    `net=False` = 不发起网络请求（owner 只读本地记录）—— 给等待循环用，别每 0.5s 打一次上游。
    """
    ctx = (_bridges.get(str(aid)) or {}).get("perm_ctx") or {}
    ws = ctx.get("ws")
    if not ws or getattr(ws, "closed", False):
        ws = ((_workers.get(str(aid)) or {}).get("ws"))
    if ws and getattr(ws, "closed", False):
        ws = None
    chat = ctx.get("chat")
    a = find(aid) or {}
    if chat is None:
        cs = list(a.get("chats") or [])
        chat = cs[0] if cs else None
    try:
        owner = int(ctx.get("owner") or a.get("owner_user_id") or 0)
    except Exception:  # noqa: BLE001
        owner = 0
    if not owner and net:
        owner = _owner_id(aid)
    return owner, ws, chat


def _quarantine_before_allow(aid, chat, cmd, title, locations, ws_root) -> dict:
    """★ 可逆执行：破坏性操作**放行前**，把要动的目标复制进面板隔离区。

    为什么不用 OS 回收站：2026-10-13 那次事故里，回收站 `$I` 元数据说删了 62 项，
    但**没有任何 `$R` 内容实体** → 21 项真没了、救不回来。所以这里**自己复制一份**。

    目标来源两路：① agent 报的 `locations`（有的 agent 会带，有的从不带）；② 从**命令原文**里猜。
    范围未知 / 猜不出来 → **如实告诉拥有者"这次没保住"**，不假装成功、也不因此拦下已确认的操作。
    """
    out = {"items": 0, "missed": 0, "unresolved": []}
    try:
        root = os.path.realpath(ws_root) if ws_root and os.path.isdir(ws_root) else ""
        cands, unresolved = [], []
        if cmd:
            try:
                g = snapshots.guess_targets(cmd, root)
                cands.extend(g.get("targets") or [])
                unresolved.extend(g.get("unresolved") or [])
            except Exception as e:  # noqa: BLE001
                acp_log("[cocraft-agents] %s 隔离：解析命令失败 %r" % (aid, e))
        for x in (locations or []):                 # agent 自己报的路径（能带就更准）
            s = str(x or "").strip()
            if not s or "://" in s:
                continue
            try:
                p = os.path.realpath(s if os.path.isabs(s) else os.path.join(root, s))
            except Exception:  # noqa: BLE001
                continue
            if os.path.exists(p):
                cands.append(p)
            else:
                unresolved.append(s[:200])
        targets, seen = [], set()
        for p in cands:
            rp = os.path.realpath(p)
            if rp not in seen:
                seen.add(rp)
                targets.append(rp)
        rec = snapshots.quarantine_paths(
            root, targets, scope=("chat-%s" % chat) if chat not in (None, "") else ("agent-%s" % aid),
            note="破坏性操作放行前隔离：%s" % title, cmd=cmd)
        n = len(rec.get("items") or [])
        missed = len(rec.get("skipped") or [])
        out.update({"items": n, "missed": missed, "id": rec.get("id"), "unresolved": unresolved[:8]})
        if n:
            names = "、".join(os.path.basename(str(it.get("src") or "")) for it in rec["items"][:6])
            msg = ("🛡 **已隔离（可恢复）**：%d 项已备份进面板隔离区 —— %s%s\n"
                   "（面板「工具 → 隔离区」可**一键恢复**；隔离只是备份，命令仍照常执行）"
                   % (n, names, "…" if n > 6 else ""))
        else:
            msg = ("⚠️ **这次没能预先隔离**：命令里没能识别出要动的文件（通配符 / 变量 / 工作区外…），"
                   "**执行后可能找不回来**。")
        if missed:
            msg += "\n⚠️ 另 %d 项没保住（不存在 / 太大 / 范围过大）。" % missed
        _send_message(aid, chat, msg)
        _act_add(chat, str((find(aid) or {}).get("name") or ""), "隔离备份",
                 ("%d 项" % n), "completed" if n else "error",
                 detail=("、".join(str(it.get("src") or "") for it in (rec.get("items") or [])[:6])
                         or msg))
        acp_log("[cocraft-agents] %s 隔离：%d 项已备份（未保住 %d，识别不出 %d）dest=%s"
                % (aid, n, missed, len(unresolved), rec.get("id")))
    except Exception as e:  # noqa: BLE001 - 隔离失败**绝不能**打断已确认的操作
        acp_log("[cocraft-agents] %s 隔离失败：%r" % (aid, e))
        try:
            _send_message(aid, chat, "⚠️ 隔离失败（%s）—— 已确认的操作仍会继续，但**这次不可恢复**。" % e)
        except Exception:  # noqa: BLE001
            pass
    return out


# ---------------- ★对比带上「文件改动」：轮快照（只记路径+哈希，不存内容） ----------------

def _ws_root(aid) -> str:
    """这个成员干活的工作目录（与 `_build_bridge` 里判定的**同一个来源**）。"""
    a = find(aid)
    eng = str(a.get("engine") or "")
    if not eng:
        return ""
    try:
        entry = acp_agents.find_agent(acp_agents.load_registry(), eng) or {}
        b = acp_agents.baseline() or {}
        return str(entry.get("cwd") or b.get("cwd") or "")
    except Exception:  # noqa: BLE001
        return ""


def _turn_scope(aid, chat) -> str:
    return "cc-%s-%s" % (chat, aid)


def _snap_before_turn(aid, chat) -> str:
    """★群聊派活：在 agent **动手之前**记一份快照（只记路径 + 哈希 → 不占磁盘）。"""
    root = _ws_root(aid)
    if not root or not os.path.isdir(root):
        return ""
    scope = _turn_scope(aid, chat)
    try:
        snapshots.take_turn_snapshot(root, scope, label="群聊派活")
        return scope
    except Exception as exc:                     # noqa: BLE001 - 快照失败绝不能挡住回帖
        acp_log("[cocraft-agents] %s 轮快照失败：%r" % (aid, exc))
        return ""


def changes(chat, aid) -> dict:
    """「派活之前 → 现在」这个成员改了哪些文件（**文件级**，没有行级 diff）。"""
    root = _ws_root(aid)
    if not root:
        return {"ok": False, "error": "这个成员没有工作目录（没绑 agent 或没配 cwd）", "files": 0}
    if not os.path.isdir(root):
        return {"ok": False, "error": "工作目录不存在：%s" % root, "files": 0}
    res = snapshots.changes_since_latest(root, _turn_scope(aid, chat))
    res["root"] = res.get("root") or root
    return res


PERM_LOG_FILE = os.path.join(STATE_DIR, "_cc_perm_log.json")
_PERM_LOG_LOCK = threading.Lock()
PERM_LOG_MAX = 500


def _perm_log_add(aid, chat, entry) -> None:
    """记一条「权限申请 → 谁批准 / 拒绝 / 超时 / 按等级自动放行」的审计（落盘，重启不丢）。

    ⚠ 只记**结论**（一次申请一条），不记"正在等" —— 待批由前端从群消息里推。
    ★ 刻意保留**命令原文 / 路径 / 拥有者 id / 回复原话**：
      「谁批准了什么」必须能追溯到具体那一条，不能只给统计（信任面板的红线）。
    """
    try:
        a = find(aid) or {}
    except Exception:  # noqa: BLE001
        a = {}
    rec = {
        "id": "pm-%d" % time.time_ns(),
        "ts": time.time(),
        "chat": "" if chat is None else str(chat),
        "aid": str(aid or ""),
        "agent": str(a.get("display") or a.get("name") or aid or ""),
        "owner": str(entry.get("owner") or ""),
        "kind": str(entry.get("kind") or ""),
        "title": str(entry.get("title") or "")[:300],
        "cmd": str(entry.get("cmd") or "")[:300],
        "paths": [str(x)[:300] for x in (entry.get("locations") or [])[:8]],
        "destructive": bool(entry.get("destructive")),
        "verdict": str(entry.get("verdict") or ""),      # once / always / reject / auto
        "by": str(entry.get("by") or ""),                # owner / level / timeout / no-ctx / send
        "reply": str(entry.get("reply") or "")[:160],
    }
    try:
        with _PERM_LOG_LOCK:
            try:
                with open(PERM_LOG_FILE, "r", encoding="utf-8") as fh:
                    arr = json.load(fh)
            except Exception:  # noqa: BLE001
                arr = []
            if not isinstance(arr, list):
                arr = []
            arr.append(rec)
            arr = arr[-PERM_LOG_MAX:]
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = PERM_LOG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(arr, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, PERM_LOG_FILE)
    except Exception:  # noqa: BLE001
        pass                  # 审计落盘失败绝不能挡住权限流程本身


def perm_log(limit: int = 80) -> list:
    """最近的权限审计（新 → 旧）。读不到回空列表。"""
    try:
        with _PERM_LOG_LOCK:
            with open(PERM_LOG_FILE, "r", encoding="utf-8") as fh:
                arr = json.load(fh)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(arr, list):
        return []
    out = [r for r in arr if isinstance(r, dict)]

    def _ts(r):
        try:
            return float(r.get("ts") or 0)
        except (TypeError, ValueError):
            return 0.0

    out.sort(key=_ts, reverse=True)      # 坏 ts 不让整个接口 500
    return out[:max(1, int(limit))]


def _perm_ask_group(aid, kind, title, locations, level_ok: bool,
                    cmd="", destructive=False, ws_root="") -> str:
    """把权限申请发到群里，**只等 agent 拥有者**回复。

    返回 "once" / "always" / "reject"（拒绝或超时或认不出拥有者）。
    等待期间从同一条 WS 取帧：不是“拥有者的批准/拒绝”的帧**原样放回队列**，不吞掉别人的 @。

    ★ `destructive=True`（破坏性/不可逆）：**必须拥有者回「确认」**才放行（回「可以」不算），
      并且申请里带上**命令原文** —— 不做盲签。
    ★ 拥有者确认后、**放行前**先做一次「**先隔离再放行**」（`_quarantine_before_allow`）：
      把这条命令要动的目标复制进面板隔离区 `runtime/quarantine/`，之后可一键恢复（不走 OS 回收站）。
    """
    if level_ok and not destructive:
        # 等级已放行 → 不打扰拥有者（破坏性除外：永远要问）。
        # ⚠ **不写审计**：这类是"策略自动放行"，不是"谁批准了"；写进去会把真正的审批记录冲掉，
        #   而且每个读/命令都重写一遍审计文件（IO 放大）。活动流里已经能看到这些操作。
        return "always"
    # ★ 建桥 / 冷启动太早 → **等 perm_ctx 就绪**（WS 连上、群已知），别静默拒绝。
    owner, ws, chat = _perm_ctx_now(aid)
    t_w = time.time()
    last_owner_try = t_w
    while time.time() - t_w < PERM_CTX_WAIT and (not owner or not ws or chat is None):
        time.sleep(0.5)
        _net = (time.time() - last_owner_try) >= 2.0    # owner 每 ~2s 才查一次上游
        if _net:
            last_owner_try = time.time()
        owner, ws, chat = _perm_ctx_now(aid, net=_net)
    if not owner or not ws or chat is None:
        acp_log("[cocraft-agents] %s 权限：等了 %.0fs 仍拿不到 owner/ws → 保守拒绝"
                % (aid, time.time() - t_w))
        _perm_log_add(aid, chat or "", {"kind": kind, "title": title, "cmd": cmd,
                                       "locations": locations, "destructive": destructive,
                                       "verdict": "reject", "by": "no-ctx"})
        return "reject"
    if time.time() - t_w >= 0.5:
        acp_log("[cocraft-agents] %s 权限：等到 perm_ctx 就绪（%.1fs）后发到群里"
                % (aid, time.time() - t_w))
    paths = "、".join(str(x) for x in (locations or [])[:4])
    budget = ask_timeout()
    # ★ 等 perm_ctx 的这段时间里注册表可能刚落盘 —— 重读一次，别用空名字发申请
    a = find(aid) or {}
    aname = str(a.get("name") or "")
    cmdtxt = " ".join(str(cmd or "").split())[:300]
    head = "⚠️ **破坏性操作（可能不可逆）**\n" if destructive else ""
    ask_word = "「确认」" if destructive else "「可以」"
    tip = ("%s🔐 **权限申请**：%s\n类型：%s%s%s\n**仅 agent 拥有者（用户 %s）可批准**："
           "请 **@%s** 并回复 %s%s（%d 秒内）—— **必须 @ 到本 agent**，否则不算数"
           % (head, title, kind or "-", ("\n涉及：" + paths) if paths else "",
              ("\n**要执行的命令**：`" + cmdtxt + "`") if cmdtxt else "",
              owner, aname, ask_word,
              "（回「不行」= 拒绝）" if destructive else "或「不行」"
              + ("；回「始终可以」则以后同类不再问"
                 if not destructive else ""), int(budget)))
    if not _send_message(aid, chat, tip):
        _perm_log_add(aid, chat, {"kind": kind, "title": title, "cmd": cmd,
                                  "locations": locations, "destructive": destructive,
                                  "owner": owner, "verdict": "reject", "by": "send"})
        return "reject"
    # ★C1：权限申请也进活动流（前端画「正在申请权限」那张卡）
    _ptcid = "perm-%d" % int(time.time() * 1000)
    _act_add(chat, aname, "破坏性操作" if destructive else "权限申请",
             (cmdtxt or " ".join(str(title or "").split())[:120]), "asking",
             detail=" ".join(str(title or "").split())[:300], tcid=_ptcid)
    t0 = time.time()
    leftovers = []
    got_lock = _ASK_LOCK.acquire(timeout=5.0)   # 串行化：同时刻只处理一个申请
    try:
        while time.time() - t0 < budget:
            fr = ws.recv(timeout=1.0)
            if fr is None:
                continue
            if not isinstance(fr, dict):
                leftovers.append(fr); continue
            if (fr.get("type") == "message"
                    and str(fr.get("chat_id")) == str(chat)
                    and str(fr.get("sender_kind")) == "user"
                    and str(fr.get("sender_id")) == str(owner)
                    and _mentions_agent(fr, a)):       # ★ 必须 @ 到本 agent 才算
                c = str(fr.get("content") or "").strip()
                # 只把「短句」当批复（@yiyi 可以 = 9 字；放宽到 40 免得带说明就被忽略）
                if len(c) <= 40:
                    # ★ 破坏性操作：只认「确认」，**不认「可以」**（防手滑）
                    ok_pat = _CONFIRM if destructive else _APPROVE
                    if ok_pat.search(c):
                        v = "once" if destructive else (
                            "always" if ("始终" in c or "一直" in c) else "once")
                        _seen_push(fr.get("id"))       # 这条算“已处理”：别再被当成 @ 回一遍
                        _send_message(aid, chat, ("✅ 已确认，开始执行：" if destructive
                                                  else "✅ 已允许：") + title)
                        if destructive:
                            # ★ 可逆执行：**放行前**先隔离（不信 OS 回收站）
                            _quarantine_before_allow(aid, chat, cmd, title, locations, ws_root)
                        _act_finish(chat, _ptcid, "allowed", detail="拥有者回复：%s" % c[:120])
                        _perm_log_add(aid, chat, {"kind": kind, "title": title, "cmd": cmd,
                                                  "locations": locations,
                                                  "destructive": destructive,
                                                  "owner": owner, "verdict": v,
                                                  "by": "owner", "reply": c})
                        return v
                    if _DENY.search(c):
                        _seen_push(fr.get("id"))
                        _send_message(aid, chat, "⛔ 已拒绝：" + title)
                        _act_finish(chat, _ptcid, "denied", detail="拥有者回复：%s" % c[:120])
                        _perm_log_add(aid, chat, {"kind": kind, "title": title, "cmd": cmd,
                                                  "locations": locations,
                                                  "destructive": destructive,
                                                  "owner": owner, "verdict": "reject",
                                                  "by": "owner", "reply": c})
                        return "reject"
            leftovers.append(fr)           # 别人说的 / 别的会话 → 留给主循环
    finally:
        if got_lock:
            try: _ASK_LOCK.release()
            except Exception: pass
        for fr in leftovers:               # 放回队列，@ 不丢
            try: ws.q.put(fr)
            except Exception: pass
    _send_message(aid, chat, "⌛ 权限申请超时（%d 秒无人批准），已拒绝：%s" % (int(budget), title))
    _act_finish(chat, _ptcid, "timeout")
    _perm_log_add(aid, chat, {"kind": kind, "title": title, "cmd": cmd,
                              "locations": locations, "destructive": destructive,
                              "owner": owner, "verdict": "reject", "by": "timeout"})
    return "reject"


def _ws_url() -> str:
    u = urllib.parse.urlsplit(BASE)
    scheme = "wss" if u.scheme == "https" else "ws"
    return "%s://%s/ws" % (scheme, u.netloc)


def _load() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save(d: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _entries() -> list:
    d = _load()
    return list(d.get("agents") or [])


def find(aid) -> dict:
    for a in _entries():
        if str(a.get("id")) == str(aid):
            return a
    return {}


def _patch(aid, **kv) -> dict:
    with _LOCK:
        d = _load()
        arr = d.get("agents") or []
        out = {}
        for a in arr:
            if str(a.get("id")) == str(aid):
                a.update(kv)
                out = a
        d["agents"] = arr
        _save(d)
    return out


def guard_note(engine) -> str:
    """这个「大脑」会不会请求权限？

    不会的话（比如某个 agent 的 env 写成 `..._PERMISSION_MODE=danger-full-access` / `yolo` 之类），
    面板的权限等级与「群里等拥有者批准」对它**根本不生效** —— 返回一句警告供前端显示（空串 = 正常，受约束）。
    """
    if not engine:
        return "还没绑定大脑"
    try:
        reg = acp_agents.load_registry()
        entry = acp_agents.find_agent(reg, engine)
    except Exception:  # noqa: BLE001
        entry = None
    if not entry:
        return "绑定的引擎不在 ACP 注册表里"
    try:
        b = acp_agents.baseline()
        env = {}
        env.update(b.get("env") or {})
        env.update(entry.get("env") or {})
    except Exception:  # noqa: BLE001
        env = {}
    # 与注册表的安全兜底共用同一套判据（免得两边正则各写一份、慢慢走偏）
    hits = acp_agents.dangerous_env(env)
    if hits:
        k, v = hits[0]
        return "该大脑声明「%s=%s」→ 它不请求权限，权限等级与群审批对它不生效" % (k, v)
    return ""


def public(a: dict) -> dict:
    aid = str(a.get("id"))
    w = _workers.get(aid)
    cap = norm_member_perm(a.get("permLevel"))
    eff, eff_src = effective_perm_level(a)
    return {"id": a.get("id"), "name": a.get("name") or "", "display": a.get("display") or "",
            "engine": a.get("engine") or "", "chats": list(a.get("chats") or []),
            "enabled": bool(a.get("enabled")), "running": bool(w and w.get("running")),
            "warn": guard_note(a.get("engine")),
            "role": norm_role(a.get("role")),
            "roleLabel": (ROLES.get(norm_role(a.get("role"))) or {}).get("label") or "",
            # ★方向 3 收尾：成员自己的权限上限 + 当前生效等级（只紧不松）
            "permLevel": cap,
            "effectivePermLevel": eff,
            "effectivePermLevelFrom": eff_src,
            "panelPermLevel": perm_level(),
            "created": a.get("created") or 0}


def list_agents() -> list:
    return [public(a) for a in _entries()]


# ---------------- 上游 REST（用**用户令牌**；管理操作用）----------------

def _api(user_token, method, path, body=None, timeout=20):
    url = BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "cocraft")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    if user_token:
        req.add_header("Authorization", "Bearer " + user_token)
    try:
        with netsafe.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            raw = json.loads(raw)
        except Exception:  # noqa: BLE001
            pass
        return e.code, raw


# ---------------- 管理操作 ----------------

def create_agent(user_token, name, display="", engine="", role="") -> dict:
    """建上游 agent 并记进本地。返回 public 视图（**不含 token**）。"""
    name = "".join(ch for ch in str(name or "").strip() if ch not in " \t\r\n")
    if not name:
        raise ValueError("名字必填（群里 @ 用它）")
    st, js = _api(user_token, "POST", "/agents",
                  {"name": name, "display": display or name, "machine": "cocraft"})
    if st != 200 or not isinstance(js, dict) or not js.get("agent_token"):
        raise RuntimeError("建 agent 失败：%s %s" % (st, str(js)[:160]))
    ag = js.get("agent") or {}
    # ★ owner：新服务端 agent 就是 `users` 行，owner 存在它的 `email`；
    #   旧服务端叫 `owner_user_id`。两种都认，再不行就用当前登录用户的 id。
    owner = ag.get("owner_user_id") or 0
    if not owner:
        try:
            owner = int(str(ag.get("email") or 0))
        except Exception:  # noqa: BLE001
            owner = 0
    if not owner:
        try:
            _st, me = _api(user_token, "GET", "/auth/me")
            if isinstance(me, dict):
                owner = int(me.get("id") or 0)
        except Exception:  # noqa: BLE001
            owner = 0
    entry = {"id": ag.get("id"), "name": name, "display": display or name,
             "agent_token": js.get("agent_token"), "engine": str(engine or ""),
             "role": norm_role(role),
             "owner_user_id": owner,
             "chats": [], "enabled": False, "created": int(time.time())}
    with _LOCK:
        d = _load()
        arr = [a for a in (d.get("agents") or []) if str(a.get("id")) != str(entry["id"])]
        arr.append(entry)
        d["agents"] = arr
        _save(d)
    return public(entry)


def remove_agent(user_token, aid, hard=True) -> bool:
    """停掉桥 + 从本地删除；`hard=True` 再调上游删掉 agent。"""
    stop_worker(aid)
    if hard:
        try:
            _api(user_token, "DELETE", "/agents/%s" % urllib.parse.quote(str(aid)))
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        d = _load()
        d["agents"] = [a for a in (d.get("agents") or []) if str(a.get("id")) != str(aid)]
        _save(d)
    _bridges.pop(str(aid), None)
    return True


def join(user_token, aid, chat_id) -> dict:
    a = find(aid)
    if not a:
        raise KeyError("没有这个 AI 成员")
    st, js = _api(user_token, "POST", "/chats/%s/agents" % urllib.parse.quote(str(chat_id)),
                  {"agent_id": a.get("id")})
    if st not in (200, 201):
        raise RuntimeError("加入会话失败：%s %s" % (st, str(js)[:160]))
    chats = list(a.get("chats") or [])
    if str(chat_id) not in [str(x) for x in chats]:
        chats.append(chat_id)
    return public(_patch(aid, chats=chats))


def leave(user_token, aid, chat_id) -> dict:
    try:
        _api(user_token, "DELETE", "/chats/%s/agents/%s"
             % (urllib.parse.quote(str(chat_id)), urllib.parse.quote(str(aid))))
    except Exception:  # noqa: BLE001
        pass
    a = find(aid)
    chats = [c for c in (a.get("chats") or []) if str(c) != str(chat_id)]
    return public(_patch(aid, chats=chats))


def set_enabled(aid, on) -> dict:
    a = _patch(aid, enabled=bool(on))
    if on:
        start_worker(aid)
    else:
        stop_worker(aid)
    return public(a)


def update_agent(aid, patch: dict) -> dict:
    allow = {k: v for k, v in (patch or {}).items()
             if k in ("engine", "display", "role", "permLevel")}
    if not allow:
        return public(find(aid))
    if "role" in allow:
        allow["role"] = norm_role(allow["role"])
    if "permLevel" in allow:                 # ★方向 3：成员自己的权限上限（空 = 跟随面板）
        allow["permLevel"] = norm_member_perm(allow["permLevel"])
    if "engine" in allow:                      # 换大脑 → 丢掉旧桥（下次重建）
        _drop_bridge(aid)
    return public(_patch(aid, **allow))


# ---------------- ACP 桥（绑定的"大脑"）----------------

def _build_bridge(aid) -> dict:
    """给某个 AI 成员建一个**独立的** AcpService + 一条会话（不碰用户当前 agent）。"""
    a = find(aid)
    eng = str(a.get("engine") or "")
    if not eng:
        raise RuntimeError("没绑定引擎")
    reg = acp_agents.load_registry()
    entry = acp_agents.find_agent(reg, eng)
    if not entry:
        raise RuntimeError("绑定的引擎不存在：%s" % eng)
    b = acp_agents.effective_baseline(entry)   # ★agent 自己的 mode 覆盖基线（与 engine 一致）
    env = {}
    env.update(b.get("env") or {})
    env.update(entry.get("env") or {})
    # ★安全兜底（第 ② 层）：群聊大脑也不允许静默用「永不问」模式
    env, _sec_fixes = acp_agents.sanitize_agent_env(entry.get("id"), env)
    cfg_cwd = entry.get("cwd") or b.get("cwd") or ""
    t_build = time.time()
    svc = AcpService(list(entry.get("command") or []), cwd=cfg_cwd, env=env, log=acp_log,
                     agent_id=eng, provider_id=entry.get("provider") or "",
                     baseline=b)
    # ★ 隔离持久化：不清空的话 __init__ 已经把面板的会话读进来了，_save 会把它们写回/覆盖。
    svc._sessions = {}
    svc._order = []
    svc._bound = set()
    svc._sessions_file = BRIDGE_SESSIONS_FILE
    # ★ 权限：和面板用**同一份等级**。命中→放行；不允许（含 ask）→ **发到群里等拥有者批准**。
    _ws_root = cfg_cwd or ""

    def _auto_perm(kind, title, locations, options, info=None):
        # ★ 每次现读：面板等级 / 成员自己的上限改了都立刻生效（不用重建桥）
        a_now = find(aid) or a
        _level, _src = effective_perm_level(a_now)
        cmd = str((info or {}).get("command") or "")
        destructive = _is_destructive(cmd, title)
        net = _is_net(kind, title, cmd, locations)
        # ★ 破坏性：**永不自动放行**（等级再宽也要拥有者回「确认」）
        ok = (not destructive) and ((net and allow_network())
                                    or allowed_by_level(_level, kind, title, cmd, locations, _ws_root))
        verdict = _perm_ask_group(aid, kind, title, locations, ok, cmd=cmd,
                                  destructive=destructive, ws_root=_ws_root)
        acp_log("[cocraft-agents] %s 权限 -> %s（等级=%s，%s%s%s，%s）"
                % (aid, verdict, _level, _src,
                   "，联网放行" if (net and allow_network() and not destructive) else "",
                   "，破坏性→必须确认" if destructive else "", title))
        return verdict or None

    svc.auto_permission = _auto_perm
    # ★ 用「这个 agent 最近跑通的模型」建会话：默认模型可能是
    #   `opencode/*` 托管模型（需 OpenCode 账号，本机没有 → 第一轮必失败）。
    try:
        model = (_load_ok_models() or {}).get(eng) or None
    except Exception:  # noqa: BLE001
        model = None
    pub = svc.create_session(title="群聊AI·%s" % (a.get("name") or aid),
                             cwd=cfg_cwd or None, model=({"id": model} if model else None))
    if model:
        acp_log("[cocraft-agents] %s 会话模型=%s" % (aid, model))
    bridge = {"svc": svc, "sid": pub.get("id"), "cwd": cfg_cwd}
    _bridges[str(aid)] = bridge
    acp_log("[cocraft-agents] 已为 %s 建桥：engine=%s session=%s（用时 %.1fs）"
            % (aid, eng, pub.get("id"), time.time() - t_build))
    return bridge


def _get_bridge(aid) -> dict:
    with _BRIDGE_LOCK:
        b = _bridges.get(str(aid))
        if b and b.get("svc"):
            if b["svc"].running():
                return b
            # ★ 子进程挂了：**只重起进程、保留同一条会话**（省冷启动 + 不丢上下文）
            try:
                b["svc"].ensure_started()
                acp_log("[cocraft-agents] %s bridge restarted (session kept)" % aid)
                return b
            except Exception as e:  # noqa: BLE001
                acp_log("[cocraft-agents] %s bridge restart failed: %r" % (aid, e))
        return _build_bridge(aid)


def _warm_bridge(aid) -> None:
    """启用成员时**提前**把桥建好（起子进程 + session/new），首次被 @ 就不用等冷启动。"""
    try:
        _get_bridge(aid)
        acp_log("[cocraft-agents] %s bridge warmed" % aid)
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s bridge warm failed: %r" % (aid, e))


def _drop_bridge(aid) -> None:
    b = _bridges.pop(str(aid), None)
    if b and b.get("svc"):
        try:
            b["svc"].close()
        except Exception:  # noqa: BLE001
            pass


def _text_of(msg: dict) -> str:
    parts = []
    for p in (msg.get("content") or []):
        if isinstance(p, dict) and p.get("type") == "text":
            parts.append(str(p.get("text") or ""))
    return _clean("".join(parts))


# codex 在自定义 provider 下会把「模型元数据缺失」的告警当正文开头（前端 `stripModelWarning` 同款处理）
_RE_WARN = re.compile(r"(?im)^\s*(?:Warning:\s*)?Model metadata for[^\n]*\n?")


def _clean(text: str) -> str:
    t = _RE_WARN.sub("", str(text or ""))
    # 顺带把 codex 常见的 "fallback model" 整行也去掉
    t = re.sub(r"(?im)^.*Defaulting to fallback model.*\n?", "", t)
    return t.strip()


# ---------------- C1：进行中活动卡（内存环形缓冲，**不落盘 / 不往群里发消息**） ----------------
# 面板用 `GET /ccagent/activity?chat=<id>&since=<rev>` 增量拉；桥跑在独立线程 → 读写都加锁。
# 设计口径见 docs/群聊存在感-方案.md §12.1。
# ⚠ 每条活动有**两个**序号，别混：
#   · `id`  稳定标识（工具的 toolCallId / 权限卡片）—— 前端按它**替换**同一条
#   · `rev` 单调自增的**修改版本**—— 每次新增/收尾都 +1，`since` 比的就是它
#   · `seq` 创建顺序（不可变）—— 只用来排序，收尾后那一行不会乱跳
#   为什么必须这样：收尾是**原地改**原本那条（runnging → completed），
#   如果 `since` 比的是它自己的 seq，客户端就永远收不到"变化"（踩过）。
_ACT_LOCK = threading.Lock()
_ACT = collections.deque(maxlen=200)         # 上限自然淘汰，不清理
_ACT_REV = [0]                               # 全局修改版本（每次新增/收尾都 +1）

# 工具 → 中文动词（与轨迹页 `TOOL_META` 同口径；ACP 给的是 ToolKind，名字兜底）
_TOOL_ZH = {
    "execute": "运行命令", "edit": "修改文件", "write": "写入文件", "read": "读取文件",
    "search": "搜索内容", "fetch": "抓取网页", "think": "思考", "list": "列出目录",
    "move": "移动文件", "delete": "删除文件", "other": "工具",
}


def _act_label(name, kind) -> str:
    return _TOOL_ZH.get(str(kind or "").strip().lower()) or (" ".join(str(name or "工具").split())[:60])


def _act_agent_name(aid) -> str:
    """活动卡上显示谁在干活（用成员表里的显示名，跟群里一致）。"""
    try:
        a = find(aid) or {}
        return str(a.get("display") or a.get("name") or aid)
    except Exception:  # noqa: BLE001
        return str(aid)


def _act_summary(name, kind, inp) -> str:
    """一行中文摘要：命令首行 / 文件名 / 搜索词 / 网址 / 兜底工具名（沿用轨迹页口径）。"""
    d = inp if isinstance(inp, dict) else {}
    cmd = d.get("command") or d.get("cmd") or ""
    if cmd:
        first = " ".join(str(cmd).strip().splitlines()[0].split())
        return first[:160]
    for k in ("filePath", "file_path", "path", "notebook_path", "target_file"):
        if d.get(k):
            return str(d[k]).replace("\\", "/").rsplit("/", 1)[-1][:120]
    for k in ("pattern", "query", "q"):
        if d.get(k):
            return "「%s」" % str(d[k])[:80]
    if d.get("url"):
        return str(d["url"])[:120]
    return " ".join(str(name or "").split())[:80]


def _act_add(chat, agent, tool, summary, status="running", detail="", tcid="") -> dict:
    global _ACT_REV
    with _ACT_LOCK:
        _ACT_REV[0] += 1
        rev = _ACT_REV[0]
        rec = {"id": str(tcid) or ("a%d" % rev), "seq": rev, "rev": rev,
               "chat": "" if chat is None else str(chat),
               "agent": str(agent or ""), "tool": str(tool or ""),
               "summary": str(summary or ""), "status": str(status or ""),
               "detail": str(detail or "")[:2000],
               "ts": time.time()}
        _ACT.append(rec)
        return dict(rec)


def _act_finish(chat, tcid, status="completed", detail="") -> None:
    """给某次活动收尾（**原地改**那条：`status`/`detail` 变，`rev` 变大 → 客户端下次就收到）。"""
    if not tcid:
        return
    key = "" if chat is None else str(chat)
    with _ACT_LOCK:
        for rec in reversed(_ACT):
            if rec.get("id") == str(tcid) and rec.get("chat") == key:
                _ACT_REV[0] += 1
                rec["rev"] = _ACT_REV[0]
                rec["status"] = str(status or "completed")
                rec["ts"] = time.time()
                if detail:
                    rec["detail"] = str(detail)[:2000]
                return


def activity(since: float = 0, chat=None) -> dict:
    """面板用：`since` 之后**有过修改**的活动（含被收尾的那条）+ 当前最大 rev。
    `chat` 为空 = 全部会话。"""
    try:
        since_f = float(since or 0)
    except Exception:  # noqa: BLE001
        since_f = 0.0
    with _ACT_LOCK:
        items = [dict(r) for r in _ACT
                 if float(r.get("rev") or 0) > since_f
                 and (chat in (None, "") or r.get("chat") == str(chat))]
        mx = _ACT_REV[0]
    return {"items": items, "max": mx}


def _ask(aid, prompt: str, files=None, timeout: float = 240.0, chat=None) -> str:
    """用绑定的 ACP agent 生成回复。

    ★ 事件驱动（不是轮询 messages()）：prompt() 是异步的，_run_turn 收尾会 emit
      session.step.ended，那时助手消息已经写进服务的本地会话。
      以前用 messages() 轮询：对 opencode-acp 它会去读共享 DB（拿不到桥会话的这轮），
      于是一直等 -> 每次都等满超时（实测日志「生成超时（240s）」，用户看到"很久不回"）。
    """
    bridge = _get_bridge(aid)
    svc, sid = bridge["svc"], bridge["sid"]
    # ★ 带附件时**强制新开 agent 侧会话**再发这一轮。
    #   实测：同一会话的第 2 轮，模型**不会重新看图**，会沿用上一轮的答案
    #   （同一张图答对；把图换成纯蓝，它仍答「绿色」）。新会话第一轮每次都准。
    #   `_drop_agent_binding` 会立 `_forceNew` + `_needsReseed` → 下一次 prompt 走 `session/new`
    #   并把本地历史补记过去，所以上下文/连续性不丢。
    if files:
        try:
            svc._drop_agent_binding(sid)
        except Exception:  # noqa: BLE001
            pass
    q = svc.subscribe()
    t0 = time.time()
    turn_id = None
    try:
        try:
            svc.prompt(sid, prompt, files=files)
        except BusyError:
            time.sleep(1.0)
            return ""                              # 上一轮还没完 → 这条放弃，别堆
        while time.time() - t0 < timeout:
            try:
                ev = q.get(timeout=0.5)
            except queue.Empty:
                continue
            if not isinstance(ev, dict):
                continue
            d = ev.get("data") or {}
            if str(d.get("sessionID")) != str(sid):
                continue
            t = ev.get("type")
            if t == "session.step.started":
                turn_id = d.get("assistantMessageID")
            elif t == "session.tool.called":
                # ★C1：工具一开跑就记一条「进行中活动」（面板据此画活动卡）
                kind = str(d.get("kind") or "")
                if kind.lower() != "think":        # 「思考」太碎，不当活动卡
                    _act_add(chat, _act_agent_name(aid), _act_label(d.get("name"), kind),
                             _act_summary(d.get("name"), kind, d.get("input")),
                             "running", tcid=d.get("toolCallId"))
            elif t in ("session.tool.success", "session.tool.error"):
                _act_finish(chat, d.get("toolCallId"),
                            "completed" if t.endswith("success") else "error")
            elif t in ("session.step.ended", "session.step.failed"):
                mid = d.get("assistantMessageID") or turn_id
                txt = _assistant_text(svc, sid, mid)
                acp_log("[cocraft-agents] %s 生成用时 %.1fs（%s）" % (aid, time.time() - t0, t))
                return txt
    finally:
        try:
            svc.unsubscribe(q)
        except Exception:  # noqa: BLE001
            pass
    acp_log("[cocraft-agents] %s 生成超时（%.0fs）" % (aid, timeout))
    return _assistant_text(svc, sid, turn_id)


def _assistant_text(svc, sid, mid) -> str:
    """从服务的本地会话里取某条助手消息的正文（不依赖 messages() / 共享 DB）。"""
    try:
        with svc._lock:
            s = svc._sessions.get(sid)
            msgs = list((s or {}).get("messages") or [])
    except Exception:  # noqa: BLE001
        return ""
    if mid:
        for m in reversed(msgs):
            if str(m.get("id")) == str(mid):
                return _text_of(m)
    for m in reversed(msgs):
        if m.get("type") == "assistant":
            return _text_of(m)
    return ""


# ---------------- 极简 WebSocket 客户端 ----------------

class _WS:
    def __init__(self, url, token, timeout=20.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        self.sock = None
        self.q: "queue.Queue" = queue.Queue()
        self.closed = False
        self.error = ""
        self._buf = b""
        self._stop = threading.Event()

    def connect(self):
        u = urllib.parse.urlsplit(self.url)
        host, port = u.hostname, (u.port or (443 if u.scheme == "wss" else 80))
        q = [(k, v) for (k, v) in urllib.parse.parse_qsl(u.query) if k != "token"]
        q.append(("token", self.token))
        query = urllib.parse.urlencode(q)
        raw = socket.create_connection((host, port), timeout=self.timeout)
        if u.scheme == "wss":
            raw = netsafe.ssl_context().wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = ("GET %s?%s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"
               "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n"
               "Origin: http://127.0.0.1:8787\r\nUser-Agent: cocraft-bridge\r\n\r\n"
               % (u.path or "/ws", query, host, key))
        raw.sendall(req.encode("utf-8"))
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = raw.recv(4096)
            if not chunk:
                break
            head += chunk
        line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        self._buf = head.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in head else b""
        if " 101 " not in line:
            self.closed = True
            self.error = "handshake: " + line
            try:
                self.close_code = int(line.split()[1])
            except Exception:  # noqa: BLE001
                pass
            return self
        try:
            raw.settimeout(None)      # ★ 握手后转阻塞读：否则空闲 20s 会报 socket.timeout
        except Exception:  # noqa: BLE001
            pass                      #   → 被当成「断线」→ 每 20s 假重连一次
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self):
        try:
            while not self._stop.is_set():
                fr = self._read_frame()
                if fr is None:
                    break
                op, payload = fr
                if op == 0x1:
                    try:
                        self.q.put(json.loads(payload.decode("utf-8", "replace")))
                    except Exception:  # noqa: BLE001
                        pass
                elif op == 0x8:
                    self.closed = True
                    break
                elif op == 0x9:
                    self._send(0xA, payload)
        except Exception as e:  # noqa: BLE001
            self.error = "%s: %s" % (type(e).__name__, e)
        finally:
            self.closed = True

    def _read_exact(self, n):
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
        op = h[0] & 0x0F
        ln = h[1] & 0x7F
        if ln == 126:
            ln = int.from_bytes(self._read_exact(2), "big")
        elif ln == 127:
            ln = int.from_bytes(self._read_exact(8), "big")
        if h[1] & 0x80:
            self._read_exact(4)
        payload = self._read_exact(ln) if ln else b""
        return op, payload

    def _send(self, op, payload: bytes):
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        hdr = bytes([0x80 | op])
        n = len(payload)
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            hdr += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.sock.sendall(hdr + mask + masked)

    def send(self, obj) -> bool:
        try:
            self._send(0x1, json.dumps(obj, ensure_ascii=False).encode("utf-8"))
            return True
        except Exception as e:  # noqa: BLE001
            self.error = str(e)
            self.closed = True
            return False

    def recv(self, timeout=1.0):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self):
        self._stop.set()
        try:
            if self.sock:
                try:
                    self.sock.shutdown(socket.SHUT_RDWR)
                except Exception:  # noqa: BLE001
                    pass
                self.sock.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------- 运行时：每个成员一个线程 ----------------

def _seen_push(msg_id):
    """已回过的消息 id（防重复回帖）。"""
    if len(_replied) > 400:
        _replied.clear()
    if msg_id in _replied:
        return False
    _replied.add(msg_id)
    return True


_replied: set = set()


def _handle_event(aid, frame) -> None:
    a = find(aid)
    if not a or not a.get("enabled"):
        return
    t = frame.get("type")
    if t == "mention":
        if str(frame.get("agent_id")) != str(aid):
            return
        msg = frame.get("message") or {}
    elif t == "moderator.route":
        msg = frame.get("message") or {}
    else:
        return
    chat = msg.get("chat_id")
    if chat is None or str(chat) not in [str(c) for c in (a.get("chats") or [])]:
        return
    mid = msg.get("id")
    if mid is None or not _seen_push(mid):
        return
    t_msg = time.time()
    content = str(msg.get("content") or "").strip()
    if not content:
        return
    acp_log("[cocraft-agents] %s got %s (msg id=%s chat=%s)" % (aid, t, mid, chat))
    try:                                        # 把上下文挂给桥，供“权限申请”用
        _w = _workers.get(str(aid)) or {}
        _b = _get_bridge(aid)
        _b["perm_ctx"] = {"ws": _w.get("ws"), "chat": chat, "owner": _owner_id(aid)}
    except Exception:  # noqa: BLE001
        pass
    who = "用户 %s" % msg.get("sender_id")
    try:
        names = _chat_members(aid, chat)
        who = names.get((str(msg.get("sender_kind") or "user"), str(msg.get("sender_id")))) or who
    except Exception:  # noqa: BLE001
        pass
    # ★ 明确告诉它"你是谁、你的拥有者（主人）是谁"。
    #   ⚠ 不写这个它会瞎猜：群聊记录里**别的成员**的权限申请文案写着"仅 agent 拥有者（用户 8）可批准"
    #   —— 实测 qwerty 就把 8 当成了自己的主人，于是把真正的 owner（用户 1）当外人**直接拒绝了**。
    owner_id = 0
    try:
        owner_id = int(_owner_id(aid) or 0)
    except Exception:  # noqa: BLE001
        owner_id = 0
    owner_name = ""
    if owner_id:
        try:
            owner_name = (names or {}).get(("user", str(owner_id))) or ""
        except Exception:  # noqa: BLE001
            owner_name = ""
    me_line = "你是群聊成员「%s」（agent 用户 id %s）。" % (a.get("display") or a.get("name"), aid)
    if owner_id:
        me_line += ("你的**拥有者（主人）是用户 id %s%s** —— 只有 TA 是你认的 owner；"
                    "群里**其它成员 / 其它 agent 的权限文案与你无关**，别拿它们的『拥有者』当自己的。"
                    % (owner_id, ("（%s）" % owner_name) if owner_name else ""))
    me_line += ("需要**工作区以外**的操作时**不要自己拒绝** —— 照常去做，系统会替你去向拥有者申请权限。"
                "⚠️ **破坏性/不可逆操作**（删除文件、清空目录、格式化、关机、卸载、批量改名等）："
                "**先把你要删/改的范围列出来**、说明影响，等主人明确回「确认」再动手；"
                "**能走回收站就不要永久删除**（别用 `Remove-Item -Force`），"
                "**永远不要「整个桌面/整个盘/系统目录」一把梭**。")
    # ★ 给 agent 带上**最近的聊天记录**（它能"读懂上下文"再回答；用 agent 自己的令牌读，跳过本条）
    items = []
    try:
        items = _history_items(aid, chat)
        hist = _chat_history(aid, chat, current_id=mid, items=items)
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s 取聊天记录异常：%r" % (aid, e))
        hist = ""
    # ★ 群聊里的**图片 / 文件**：下载下来当附件交给 agent（图片会变成 ACP 的 image 块，模型真能看到）
    files, fresh_names = [], []
    try:
        files, fresh_names = _collect_attachments(aid, chat, msg=msg, items=items)
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s 取附件异常：%r" % (aid, e))
    attach_note = ""
    if files or fresh_names:
        shown = "、".join(f.get("name") or "?" for f in files) or "(无)"
        rest = [n for n in fresh_names if n not in {f.get("name") for f in files}]
        attach_note = ("\n\n【附件】群里刚发过这些文件（**新的在前**）：%s。"
                       "这次已把 **%s** 附上。**只看这一轮附上的图片/文件**来回答 —— "
                       "不要沿用你上一轮的答案（实测：同一会话里它会照抄上一轮，甚至看图不看新的）。"
                       % ("、".join(fresh_names), shown))
        if rest:
            attach_note += "（其余（%s）这次没附上。）" % "、".join(rest)
        acp_log("[cocraft-agents] %s 附带文件：%s%s"
                % (aid, shown, ("（另有未附：%s）" % "、".join(rest)) if rest else ""))
    # ★ 把群里的文件**落到本机**（工作区里），并在提示词里给出**本地路径** —— 这样 agent 能用
    #   自己的文件/命令工具去处理它们（读、改、转换、跑），而不是只能"看一眼"。
    inbox = {}
    try:
        inbox = _ensure_inbox(aid, chat, items)
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s 文件落地异常：%r" % (aid, e))
    if inbox:
        d = _inbox_dir(aid, chat)
        shown_paths = [os.path.join(d, fn) for _, fn in list(inbox.items())[:8]]
        attach_note += ("\n\n【已保存到本机】群里这些文件已经存到**本机磁盘**：\n"
                        + "\n".join("  · `%s`" % p for p in shown_paths)
                        + "\n你可以直接用文件/命令工具**读、改、转换、运行**它们来完成用户交办的事"
                          "（这些都在你的工作区内）。"
                        + ("\n完整清单（含群里更早的文件）：`%s`" % os.path.join(d, "_files.md")))
        attach_note += _inline_texts(inbox, d)
        acp_log("[cocraft-agents] %s 已落地 %d 个文件到 %s" % (aid, len(inbox), d))
    base_prompt = ("%s%s\n\n下面是**最近的群聊记录**（从早到晚，`HH:MM 谁: 内容`），"
                   "最后一条是刚 @ 你的消息。请结合上下文用中文简短回答，不要复述指令、不要寒暄。\n\n"
                   "【最近的群聊记录】\n%s\n\n【你要回应的】%s: %s"
                   % (me_line, role_line(a.get("role")), hist or "（暂无）", who, content))
    plain_prompt = base_prompt + (("\n\n（群里有 %d 个文件（%s），但这轮没读进来 —— 只能按文件名回答。）"
                                  % (len(files), "、".join(f.get("name") or "?" for f in files))) if files else "")
    prompt = base_prompt + attach_note
    try:
        _snap_before_turn(aid, chat)          # ★派活前记一份（之后能并排看"谁改了哪些文件"）
        reply = _ask(aid, prompt, files=files, chat=chat)
        if files and not str(reply or "").strip():
            # ★ 有些“大脑/模型”不吃图片：带附件那一轮会**空回复**（实测 text_parts=0）。
            #   这时去掉附件重试一次 —— 宁可它按文字/文件名答，也别在群里沉默。
            acp_log("[cocraft-agents] %s 带附件这一轮没出正文 → 去掉附件重试一次" % aid)
            reply = _ask(aid, plain_prompt, files=None, chat=chat)
        if not str(reply or "").strip():
            # ★ 还是空的（实测：`text_parts=1` 且那 1 条就是被剥掉的 "Model metadata" 警告 →
            #   模型只给了思考、没给正文）→ **再试一次**，把要求说死。否则群里看着就是"它不回复"。
            acp_log("[cocraft-agents] %s 空回复 → 重试一次" % aid)
            reply = _ask(aid, plain_prompt + "\n\n（刚才那一轮你没有输出正文。"
                                           "请**只用中文回一句话**，不要输出思考过程。）",
                         files=None, chat=chat)
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s 生成失败：%r" % (aid, e))
        reply = ""
    if not reply:
        acp_log("[cocraft-agents] %s no reply (%.1fs)" % (aid, time.time() - t_msg))
        return
    _send_message(aid, chat, reply, mid)
    acp_log("[cocraft-agents] %s replied in %.1fs (total)" % (aid, time.time() - t_msg))


def _send_message(aid, chat_id, text, reply_to=None) -> bool:
    """通过**成员自己的** WS 回帖（没有长连接时退回 REST，用... 没有用户令牌 → 只能靠 WS）。"""
    w = _workers.get(str(aid))
    ws = w.get("ws") if w else None
    if ws and not ws.closed:
        ok = ws.send({"type": "message", "chat_id": chat_id, "content": text,
                      "reply_to": reply_to, "client_msg_id": _uuid()})
        if ok:
            return True
    acp_log("[cocraft-agents] %s 回帖失败（WS 不可用）" % aid)
    return False


def _uuid():
    return "%08x-%04x-%04x" % (random.getrandbits(32), random.getrandbits(16), random.getrandbits(16))


# ---------------- 心跳（★必须用拥有者的用户令牌）----------------
# cocraft 的 `POST /agents/{id}/heartbeat` 是**"owner 管自己名下 agent"**那一族的接口：
# 用 agent 自己的令牌发会 `403 {"detail":"you do not own this agent"}`（实测 2026-10-13）。
# 所以这里在**内存**里记住最近一次面板带来的用户令牌（**不落盘** —— 那是全权限凭据），
# 心跳时用它发。重启面板服务后，等下一次群聊面板里任何一次操作（会带 Authorization）重新武装。
_MEM = {"user_token": ""}
_HB_ERR: dict = {}        # aid -> 上次心跳的失败描述（只在"状态变化"时记日志，别每 25s 刷屏）
_HB_NOUSER: set = set()   # 已经提示过"没有用户令牌"的 aid


def remember_token(tok) -> None:
    """记住最近的**用户令牌**（只在内存，供 `_heartbeat()` 用 owner 身份发心跳）。"""
    t = str(tok or "").strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if not t or t == _MEM["user_token"]:
        return
    first = not _MEM["user_token"]
    _MEM["user_token"] = t
    _HB_ERR.clear()
    _HB_NOUSER.clear()
    if first:
        acp_log("[cocraft-agents] 已记住用户令牌（心跳将用 owner 身份发）")


def _heartbeat(a) -> None:
    """给某个 agent 报一次到（best-effort，但**失败会记一行日志**，不再静默）。"""
    aid = str(a.get("id"))
    tok = _MEM.get("user_token") or ""
    if not tok:
        if aid not in _HB_NOUSER:
            _HB_NOUSER.add(aid)
            acp_log("[cocraft-agents] %s 心跳暂缓：还没有用户令牌（在群聊面板里做一次操作后自动开始）" % aid)
        return
    try:
        st, js = _api(tok, "POST", "/agents/%s/heartbeat" % urllib.parse.quote(aid))
    except Exception as e:  # noqa: BLE001
        st, js = None, repr(e)
    err = "" if st == 200 else "%s %s" % (st, str(js)[:120])
    prev = _HB_ERR.get(aid, "")
    if err != prev:
        _HB_ERR[aid] = err
        if err:
            acp_log("[cocraft-agents] %s 心跳失败：%s" % (aid, err))
        else:
            acp_log("[cocraft-agents] %s 心跳恢复（200）" % aid)


# ---------------- 聊天记录（给 agent 当上下文）----------------
# 读接口对 **agent 自己的令牌也放行**（实测 `GET /chats/{id}/messages` → 200）→ 桥可以自己读历史。
HISTORY_DEFAULT = 60
_MEMBERS_CACHE: dict = {}      # chat -> (ts, {("user","1"): "mew", ...})
_MEMBERS_TTL = 120.0


def history_limit() -> int:
    """给 agent 带多少条聊天记录（默认 60；可在 `_cocraft_agents.json` 顶层写 `history`，1~200）。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            v = int((json.load(fh) or {}).get("history") or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if 1 <= v <= 200 else HISTORY_DEFAULT


def _hhmm(iso) -> str:
    """UTC ISO → **本地** HH:MM（服务端给的时间是 UTC）。"""
    s = str(iso or "")
    try:
        d = datetime.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return d.astimezone().strftime("%H:%M")
    except Exception:  # noqa: BLE001
        return s[11:16] if len(s) >= 16 else ""


def _chat_members(aid, chat) -> dict:
    """`{("user","1"): "mew", ("agent","11"): "yiyi"}` —— 缓存 120s。"""
    key = str(chat)
    now = time.time()
    hit = _MEMBERS_CACHE.get(key)
    if hit and now - hit[0] < _MEMBERS_TTL:
        return hit[1]
    out: dict = {}
    a = find(aid) or {}
    try:
        _st, js = _api(a.get("agent_token") or "", "GET",
                       "/chats/%s/members" % urllib.parse.quote(key))
        for m in ((js or {}).get("items") or []):
            if isinstance(m, dict):
                out[(str(m.get("kind") or "user"), str(m.get("id")))] = \
                    str(m.get("name") or m.get("username") or "")
    except Exception:  # noqa: BLE001
        pass
    _MEMBERS_CACHE[key] = (now, out)
    return out


def _msg_body(m: dict) -> str:
    if str(m.get("message_type") or "") == "file":
        return "[文件] " + _file_name(m.get("content"))
    t = re.sub(r"\s+", " ", str(m.get("content") or "")).strip()
    return t[:300] + ("…" if len(t) > 300 else "")


# ---------------- 群聊文件（图片/文件）→ 交给 agent ----------------
# 实测：**agent 自己的令牌就能下载**（`GET /chats/{id}/files/{name}` → 200 image/jpeg）。
# 上传接口只收文件、**不能带说明/@**，所以做法是：被 @ 时把"刚发进群里的图片/文件"一起带上。
FILE_MAX_BYTES = 4 * 1024 * 1024     # 单个上限（base64 后约 5.3MB；超了只给文字说明）
FILES_MAX_DEFAULT = 1                # ⚠ 默认**只带最新 1 个**：实测一次给两张，模型会答"另一张"（答错）
_FILES_RANGE = (1, 5)


def files_max() -> int:
    """一次最多带几个刚发的文件（默认 1；可在 `_cocraft_agents.json` 顶层写 `files_max`，1~5）。"""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            v = int((json.load(fh) or {}).get("files_max") or 0)
    except Exception:  # noqa: BLE001
        v = 0
    return v if _FILES_RANGE[0] <= v <= _FILES_RANGE[1] else FILES_MAX_DEFAULT


FILE_FRESH_SEC = 1800.0              # 多久内的文件算"刚发的"（30 分钟，**只看时间、不看位置**）
_MIME_BY_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "avif": "image/avif", "svg": "image/svg+xml",
    "pdf": "application/pdf", "txt": "text/plain", "md": "text/markdown", "json": "application/json",
    "csv": "text/csv", "log": "text/plain", "py": "text/x-python", "js": "text/javascript",
    "ts": "text/typescript", "html": "text/html", "css": "text/css", "zip": "application/zip",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _file_name(content) -> str:
    return str(content or "").replace("\\", "/").rsplit("/", 1)[-1]


def _mime_of(name: str, header: str = "") -> str:
    h = (header or "").split(";")[0].strip()
    if h and h != "application/octet-stream":
        return h
    return _MIME_BY_EXT.get(str(name).rsplit(".", 1)[-1].lower(), "application/octet-stream")


def _api_bytes(tok, path, limit=FILE_MAX_BYTES + 1, timeout=60):
    """原始字节 GET（文件下载用）。返回 (status, headers, bytes)。"""
    req = urllib.request.Request(BASE + path)
    req.add_header("User-Agent", "cocraft")
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    try:
        with netsafe.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read(limit)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read(limit)
    except Exception as e:  # noqa: BLE001
        return None, {}, repr(e).encode()


def _fetch_file(aid, chat, content):
    """下载一个群聊文件 → `{uri, name, mime, bytes}`（uri 是 data: URL，ACP 会翻成 image/resource 块）。"""
    a = find(aid) or {}
    tok = a.get("agent_token") or ""
    name = _file_name(content)
    if not tok or not name:
        return None
    st, hdrs, body = _api_bytes(tok, "/chats/%s/files/%s?token=%s"
                               % (urllib.parse.quote(str(chat)), urllib.parse.quote(name),
                                  urllib.parse.quote(tok)))
    if st != 200 or not isinstance(body, (bytes, bytearray)) or not body:
        acp_log("[cocraft-agents] %s 下载群文件失败：%s %s" % (aid, st, name))
        return None
    if len(body) > FILE_MAX_BYTES:
        acp_log("[cocraft-agents] %s 群文件太大（%.1fMB）→ 只给说明：%s"
                % (aid, len(body) / 1048576.0, name))
        return None
    mime = _mime_of(name, hdrs.get("Content-Type") or hdrs.get("content-type") or "")
    return {"uri": "data:%s;base64,%s" % (mime, base64.b64encode(bytes(body)).decode("ascii")),
            "name": name, "mime": mime, "bytes": bytes(body)}


# ---------------- 群聊文件"落地"：让 agent 能拿本机文件干活 ----------------
# 用户要求：agent 要能**利用群里的文件（含图片）在本机完成权限允许范围内的工作**。
# 所以除了把图片当附件发给模型看，还要把文件**存到 agent 工作区里**（`cocraft-inbox/<chat>/`），
# 这样它就能用自己的文件/命令工具读、改、转换、跑 —— 而且落在工作区内，`workspace-write` 下免批准。
INBOX_DIRNAME = "cocraft-inbox"
INBOX_MAX_FILES = 12                    # 一次最多落地几个（防止刷屏式上传把工作区塞满）
INBOX_MAX_BYTES = 48 * 1024 * 1024      # 一次最多落地多少字节
TEXT_INLINE_MAX = 8192                  # 单个「文本类」文件最多内联多少字节（省得为读它去要权限）
TEXT_INLINE_TOTAL = 12288               # 一次最多内联多少
_TEXT_EXT = ("txt", "md", "json", "csv", "log", "py", "js", "ts", "html", "css", "yml", "yaml",
             "ini", "cfg", "toml", "ps1", "bat", "cmd", "sh", "sql", "xml", "srt", "vtt")


def _is_texty(name: str, mime: str = "") -> bool:
    if (mime or "").startswith("text/"):
        return True
    return str(name).rsplit(".", 1)[-1].lower() in _TEXT_EXT


def _inline_texts(inbox: dict, d: str) -> str:
    """把落地的小文本文件**直接内联**到提示词里（常见需求是"读一下这个 txt/csv/json"）。"""
    if not inbox or not d:
        return ""
    out, total = [], 0
    for _mid, fn in list(inbox.items()):
        p = os.path.join(d, fn)
        if not _is_texty(fn):
            continue
        try:
            with open(p, "rb") as fh:
                raw = fh.read(TEXT_INLINE_MAX + 1)
        except OSError:
            continue
        if len(raw) > TEXT_INLINE_MAX or total + len(raw) > TEXT_INLINE_TOTAL:
            continue
        try:
            txt = raw.decode("utf-8")
        except UnicodeDecodeError:
            txt = raw.decode("utf-8", "replace")
        total += len(raw)
        out.append("--- %s ---\n%s" % (fn, txt.rstrip()))
    if not out:
        return ""
    return "\n\n【文件内容（小的文本文件已直接附上，不必再要权限去读）】\n" + "\n\n".join(out)


def _inbox_dir(aid, chat) -> str:
    """落地目录 = `<agent 工作区>/cocraft-inbox/<chat>/`（工作区内 → agent 能免批直接处理）。"""
    b = _bridges.get(str(aid)) or {}
    root = str(b.get("cwd") or "").strip() or STATE_DIR
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(chat)) or "chat"
    d = os.path.join(root, INBOX_DIRNAME, safe)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        return ""
    return d


def _ensure_inbox(aid, chat, items):
    """把群聊里最近的文件**落到本机**，并写一份 `_files.md` 清单。

    返回 `{群消息 id: 本地文件名}`。同名同大小视为已落地（不重复下载）；同名但内容不同 → 加 `-<id>`。
    """
    d = _inbox_dir(aid, chat)
    if not d or not items:
        return {}
    a = find(aid) or {}
    tok = a.get("agent_token") or ""
    if not tok:
        return {}
    names = _chat_members(aid, chat)
    out, saved_bytes = {}, 0
    for m in reversed(items):                     # 新的在前：先保证最新的落地
        if not isinstance(m, dict) or str(m.get("message_type") or "") != "file":
            continue
        if len(out) >= INBOX_MAX_FILES or saved_bytes >= INBOX_MAX_BYTES:
            break
        content = str(m.get("content") or "")
        name = _file_name(content)
        if not name:
            continue
        mid = str(m.get("id"))
        got = _fetch_file(aid, chat, content)
        if not got:
            continue
        data = got.get("bytes") or b""
        fn = name
        target = os.path.join(d, fn)
        if os.path.isfile(target):
            try:
                same = os.path.getsize(target) == len(data)
            except OSError:
                same = False
            if not same:                          # 同名不同内容 → 用消息 id 区分
                stem, ext = os.path.splitext(name)
                fn = "%s-%s%s" % (stem, mid, ext)
                target = os.path.join(d, fn)
        if not os.path.isfile(target):
            try:
                with open(target, "wb") as fh:
                    fh.write(data)
            except OSError as e:
                acp_log("[cocraft-agents] %s 落地失败：%r" % (aid, e))
                continue
        saved_bytes += len(data)
        out[mid] = fn
    # 清单（含没落地的：让 agent 知道群里还有哪些文件）
    try:
        lines = ["# cocraft 群聊文件（chat %s）" % chat, "",
                 "落地目录：`%s`" % d,
                 "（`✓` = 已存到本机，可直接用文件/命令工具处理；否则只是群里有过这个文件）", "",
                 "| 群消息 | 时间 | 发送者 | 文件 | 本地文件 |",
                 "| --- | --- | --- | --- | --- |"]
        for m in reversed(items):
            if not isinstance(m, dict) or str(m.get("message_type") or "") != "file":
                continue
            mid = str(m.get("id"))
            kind = str(m.get("sender_kind") or "user")
            who = names.get((kind, str(m.get("sender_id")))) or (
                ("agent %s" % m.get("sender_id")) if kind == "agent" else ("用户 %s" % m.get("sender_id")))
            lines.append("| %s | %s | %s | %s | %s |"
                         % (mid, _hhmm(m.get("created_at")), who,
                            _file_name(m.get("content")), out.get(mid) or "（未落地）"))
        with open(os.path.join(d, "_files.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s 写清单失败：%r" % (aid, e))
    return out


def _collect_attachments(aid, chat, msg=None, items=None):
    """挑出「刚发进群里的图片/文件」当附件。

    返回 `(files, fresh_names)`：
      · `files` = 真正下载好的附件（最多 `files_max()` 个，**新的在前**）；
      · `fresh_names` = 所有"刚发的"文件名（新的在前，含没附上的 —— 用于提示词里说明）。

    ⚠ 规则与教训：
      · 触发那条本身就是文件消息 → 一定带上；
      · 其余只取 **`created_at` 在 `FILE_FRESH_SEC` 内** 的（**按时间判**，不看位置 ——
        曾按"最后 5 条"判，把**昨天**的旧图也带上去了）；
      · **默认只带 1 个**：实测一次给两张（新的绿图 + 16 分钟前的照片），模型答的是那张照片 → 答错。
    """
    if items is None:
        items = []
    cands = []
    if isinstance(msg, dict) and str(msg.get("message_type") or "") == "file":
        cands.append(msg)
    now = datetime.datetime.now(datetime.timezone.utc)
    for m in items:
        if not isinstance(m, dict) or str(m.get("message_type") or "") != "file":
            continue
        try:
            d = datetime.datetime.fromisoformat(str(m.get("created_at") or ""))
            if d.tzinfo is None:
                d = d.replace(tzinfo=datetime.timezone.utc)
            if (now - d).total_seconds() <= FILE_FRESH_SEC:
                cands.append(m)
        except Exception:  # noqa: BLE001
            continue
    ordered, seen_names = [], set()
    for m in reversed(cands):                     # 新的在前
        c = str(m.get("content") or "")
        if not c or c in seen_names:
            continue
        seen_names.add(c)
        ordered.append(c)
    names = [_file_name(c) for c in ordered]
    out = []
    for c in ordered[:files_max()]:
        got = _fetch_file(aid, chat, c)
        if got:
            out.append(got)
    return out, names


def _history_items(aid, chat) -> list:
    """最近 N 条消息的**原始 items**（给上下文和附件共用；失败返回 []）。

    ★ 用 agent 自己的令牌读：读接口对 agent 放行；分页与面板同一套（先问 total 再取**最后一页**，
      因为 `/messages` 只按 id 升序 + offset 给，没有 order 参数）。
    """
    a = find(aid) or {}
    tok = a.get("agent_token") or ""
    if not tok:
        return []
    n = history_limit()
    base = "/chats/%s/messages" % urllib.parse.quote(str(chat))
    try:
        _st, page = _api(tok, "GET", base + "?limit=1&offset=0")
        total = int((page or {}).get("total") or 0) if isinstance(page, dict) else 0
        _st, page = _api(tok, "GET", "%s?limit=%d&offset=%d" % (base, n, max(0, total - n)))
        return ((page or {}).get("items") or []) if isinstance(page, dict) else []
    except Exception as e:  # noqa: BLE001
        acp_log("[cocraft-agents] %s 读聊天记录失败：%r" % (aid, e))
        return []


def _chat_history(aid, chat, current_id=None, items=None) -> str:
    """最近 N 条聊天记录（**跳过** `current_id` 那条 —— 它单独放进「你要回应的」）。"""
    if items is None:
        items = _history_items(aid, chat)
    a = find(aid) or {}
    names = _chat_members(aid, chat)
    mine = {str(a.get("name") or ""), str(a.get("display") or "")}
    mine.discard("")
    lines = []
    for m in items:
        if not isinstance(m, dict):
            continue
        if current_id is not None and str(m.get("id")) == str(current_id):
            continue
        kind = str(m.get("sender_kind") or "user")
        who = names.get((kind, str(m.get("sender_id")))) or (
            ("agent %s" % m.get("sender_id")) if kind == "agent" else ("用户 %s" % m.get("sender_id")))
        if kind == "agent" and who in mine:
            who += "（我自己）"
        lines.append("%s %s: %s" % (_hhmm(m.get("created_at")), who, _msg_body(m)))
    return "\n".join(lines)


def _worker_loop(aid):
    backoff = 1
    while True:
        w = _workers.get(str(aid))
        if not w or w["stop"].is_set():
            break
        a = find(aid)
        if not a or not a.get("enabled"):
            break
        try:
            ws = _WS(_ws_url(), a.get("agent_token") or "").connect()
        except Exception as e:  # noqa: BLE001
            w["running"] = False
            acp_log("[cocraft-agents] %s 建连异常：%r，%ds 后重试" % (aid, e, backoff))
            if w["stop"].wait(backoff):
                break
            backoff = min(backoff * 2, 30)
            continue
        w["ws"] = ws
        if ws.closed:
            w["running"] = False
            acp_log("[cocraft-agents] %s 连不上 WS（%s），%ds 后重试" % (aid, ws.error, backoff))
            if w["stop"].wait(backoff):
                break
            backoff = min(backoff * 2, 30)
            continue
        w["running"] = True
        backoff = 1
        acp_log("[cocraft-agents] %s 已连上 WS（%s）" % (aid, _ws_url()))
        last_hb = 0.0
        last_ping = 0.0
        while not w["stop"].is_set() and not ws.closed:
            fr = ws.recv(timeout=1.0)
            if fr is not None:
                try:
                    _handle_event(aid, fr)
                except Exception as e:  # noqa: BLE001
                    acp_log("[cocraft-agents] 处理事件出错：%r" % e)
            now = time.time()
            if now - last_ping > 20:               # 应用层保活（面板也是每 25s 发一次 {"type":"ping"}）
                last_ping = now
                ws.send({"type": "ping"})
            if now - last_hb > 25:
                last_hb = now
                _heartbeat(a)                          # ★ 用拥有者的用户令牌（见 _heartbeat）
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        w["ws"] = None
        w["running"] = False
        if w["stop"].is_set():
            break
        if w["stop"].wait(backoff):
            break
        backoff = min(backoff * 2, 30)


def start_worker(aid) -> bool:
    aid = str(aid)
    with _LOCK:
        w = _workers.get(aid)
        if w and w["thread"].is_alive():
            return True
        stop = threading.Event()
        w = {"stop": stop, "running": False, "ws": None}
        th = threading.Thread(target=_worker_loop, args=(aid,), daemon=True)
        w["thread"] = th
        _workers[aid] = w
        th.start()
    threading.Thread(target=_warm_bridge, args=(aid,), daemon=True).start()
    return True


def stop_worker(aid) -> None:
    with _LOCK:
        w = _workers.pop(str(aid), None)
    if w:
        w["stop"].set()
        try:
            if w.get("ws"):
                w["ws"].close()
        except Exception:  # noqa: BLE001
            pass
    _drop_bridge(aid)


def ensure_workers() -> list:
    """服务启动时把**已启用**的成员拉起来。"""
    started = []
    for a in _entries():
        if a.get("enabled"):
            start_worker(a.get("id"))
            started.append(a.get("id"))
    return started


def shutdown() -> None:
    for aid in list(_workers.keys()):
        stop_worker(aid)
    for aid in list(_bridges.keys()):
        _drop_bridge(aid)


def status() -> dict:
    return {"base": BASE, "ws": _ws_url(), "agents": list_agents(),
            "workers": sorted(str(k) for k in _workers.keys())}
