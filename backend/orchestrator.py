# -*- coding: utf-8 -*-
r"""★本地协作编排（不依赖群聊 / 云端账号）—— 第一刀：Runner。

设计目标（见 docs/本地协作-方案.md）：
  * **本机**把一条任务同时派给多个 agent（各自独立 AcpService + 独立会话），收集结果；
  * **不经过 cocraft / WS / 登录**，数据只落 `runtime/state/_orchestrator/`；
  * 权限与面板**同一份等级**；命中自动放行，否则进**任务级待批队列**等用户逐条批准
    （fail-closed；破坏性永远要确认）；
  * 支持取消 / 超时 / 失败隔离 / 结果持久化；
  * 结果里的「对比 / 逐行差异 / 仲裁」在本地做（前端复用现有渲染函数），
    仲裁 = 再跑一个 reviewer 成员。

⚠ 第一刀只做 Runner + HTTP API，不做 UI；工作台在下一刀接。
⚠ 表单（ACP elicitation）暂自动拒绝并记一条活动（第一刀范围）；权限是完整排队的。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
import uuid

import snapshots
from cocraft_agents import (ROLES, allowed_by_level, allow_network, _is_destructive,
                            _is_net, norm_role, perm_level, role_line)
from engines import STATE_DIR
from engines.acp import agents as acp_agents
from engines.acp.engine import acp_log
from engines.acp.map_events import now_ms
from engines.acp.service import AcpService, BusyError, _load_ok_models

ROOT = os.path.join(STATE_DIR, "_orchestrator")
RUNS_DIR = os.path.join(ROOT, "runs")
WORKFLOWS_FILE = os.path.join(STATE_DIR, "_collab_workflows.json")   # ★可复用的工作流（成员/默认任务/画布布局）
KEEP_WORKFLOWS = 200
KEEP_RUNS = 60
ASK_TIMEOUT = float(os.environ.get("COCRAFT_ORCH_ASK_TIMEOUT") or 180)   # 等批准（秒）
RUN_TIMEOUT = float(os.environ.get("COCRAFT_ORCH_TIMEOUT") or 600)       # 单个成员上限（秒）

_LOCK = threading.RLock()
_WF_LOCK = threading.RLock()
_RUNS: dict = {}          # run_id -> run dict（内存权威；落盘用于重启后查看）
_ACTIVE: dict = {}        # (run_id, member_id) -> {"svc":..., "sid":...}
_CANCEL: dict = {}        # (run_id, member_id) -> Event
_PENDING: dict = {}       # perm_id -> {"event": Event, "decision": None, "run":..., "member":...}


# ---------------- 小工具 ----------------

def _now() -> int:
    return now_ms()


def _atomic_write(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _run_path(run_id: str) -> str:
    return os.path.join(RUNS_DIR, str(run_id) + ".json")


def _save(run: dict) -> None:
    run["updated"] = _now()
    try:
        _atomic_write(_run_path(run["id"]), run)
    except Exception as exc:  # noqa: BLE001
        acp_log("[orchestrator] 保存运行失败：%s" % exc)


def _load_file(run_id: str) -> dict:
    try:
        with open(_run_path(run_id), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _member_public(m: dict) -> dict:
    return {k: m.get(k) for k in
            ("id", "name", "role", "roleLabel", "engine", "status", "output", "error",
             "created", "started", "ended", "tokens", "context", "activities",
             "kind", "sessionID", "cancelled", "files", "pick", "permLevel")}


def public_run(run: dict) -> dict:
    if not run:
        return {}
    return {"id": run.get("id"), "task": run.get("task"), "cwd": run.get("cwd"),
            "title": run.get("title"), "status": run.get("status"),
            "workflowId": run.get("workflowId") or "",
            "workflowName": run.get("workflowName") or "",
            "created": run.get("created"), "updated": run.get("updated"),
            "members": [_member_public(m) for m in (run.get("members") or [])],
            # 全部申请（pending + 已结算）都回；「谁批准了什么」要能追溯
            "permissions": [p for p in (run.get("permissions") or [])],
            "arbitration": run.get("arbitration"),
            "error": run.get("error") or ""}


def list_runs(limit: int = 30) -> list:
    """最近任务：**内存 + 磁盘一起**（打开某条历史后它也进了内存，不能因此把别的历史挤掉）。

    列表多回 `done / failed / running` 三个计数：工作台左栏直接写「3/3 完成」，
    不用为每一条再拉一次详情。
    """
    with _LOCK:
        runs = list(_RUNS.values())
    have = {str(r.get("id") or "") for r in runs}
    if os.path.isdir(RUNS_DIR):
        for name in sorted(os.listdir(RUNS_DIR), reverse=True)[:max(limit * 2, 40)]:
            if not name.endswith(".json"):
                continue
            rid = name[:-5]
            if rid in have:
                continue
            d = _load_file(rid)
            if d:
                runs.append(d)
                have.add(rid)
    runs.sort(key=lambda r: r.get("created") or 0, reverse=True)
    out = []
    for r in runs[:limit]:
        ms = r.get("members") or []
        states = [m.get("status") for m in ms]
        out.append({"id": r.get("id"), "task": r.get("task"), "title": r.get("title"),
                    "cwd": r.get("cwd") or "",          # ★左栏要按项目分组/筛选：必须带上工作目录
                    "workflowId": r.get("workflowId") or "",
                    "workflowName": r.get("workflowName") or "",
                    "status": r.get("status"), "created": r.get("created"),
                    "updated": r.get("updated"), "members": len(ms),
                    "done": sum(1 for x in states if x == "done"),
                    "failed": sum(1 for x in states if x in ("failed", "timeout", "cancelled")),
                    "running": sum(1 for x in states if x in ("wait", "running"))})
    return out


def get_run(run_id: str) -> dict:
    rid = str(run_id or "")
    with _LOCK:
        run = _RUNS.get(rid)
    if run is None:
        run = _load_file(rid)
        if run:
            with _LOCK:
                _RUNS[rid] = run
    return public_run(run)


# ---------------- ★工作流（可复用：成员 / 默认任务 / 画布布局） ----------------

def _wf_pos(v) -> dict:
    """画布坐标：只存 x/y（设计像素），夹到一个合理范围，坏值当 0。"""
    try:
        x = float((v or {}).get("x") or 0)
        y = float((v or {}).get("y") or 0)
    except (TypeError, ValueError):
        x = y = 0.0
    return {"x": max(-4000.0, min(12000.0, x)),
            "y": max(-4000.0, min(12000.0, y))}


def _wf_clean_members(raw) -> list:
    """校验/清洗成员（与 start_run 同一口径）。保留 `m1..mN` 形式的 id（画布布局要用）。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("至少要有一个成员")
    if len(raw) > 8:
        raise ValueError("一次最多 8 个成员")
    out, used = [], set()
    for i, r in enumerate(raw):
        r = r if isinstance(r, dict) else {}
        engine = str(r.get("engine") or "").strip()
        if not engine:
            raise ValueError("成员 #%d 没选引擎" % (i + 1))
        role = norm_role(r.get("role"))
        name = re.sub(r"\s+", " ", str(r.get("name") or "").strip())[:24] or ("m%d" % (i + 1))
        mid = str(r.get("id") or "").strip()
        if not re.match(r"^m\d+$", mid) or mid in used:
            mid = "m%d" % (i + 1)
            while mid in used:
                mid = "m%d" % (int(mid[1:]) + 1)
        used.add(mid)
        out.append({"id": mid, "role": role, "name": name, "engine": engine,
                    "permLevel": norm_perm_level(r.get("permLevel"))})
    return out


def _wf_load() -> dict:
    try:
        with _WF_LOCK:
            with open(WORKFLOWS_FILE, "r", encoding="utf-8") as fh:
                d = json.load(fh)
    except Exception:  # noqa: BLE001
        return {"version": 1, "workflows": []}
    if not isinstance(d, dict):
        return {"version": 1, "workflows": []}
    if not isinstance(d.get("workflows"), list):
        d["workflows"] = []
    return d


def list_workflows(cwd: str = "") -> list:
    """工作流列表（按 updated 倒序；cwd 可选过滤，大小写不敏感）。"""
    d = _wf_load()
    out = [w for w in (d.get("workflows") or []) if isinstance(w, dict)]
    if cwd:
        key = str(cwd).replace("\\", "/").rstrip("/").lower()
        out = [w for w in out
               if str(w.get("cwd") or "").replace("\\", "/").rstrip("/").lower() == key]
    out.sort(key=lambda w: w.get("updated") or 0, reverse=True)
    return out


def save_workflow(data) -> dict:
    """新建 / 更新一个工作流。`data.id` 为空 = 新建。"""
    data = data if isinstance(data, dict) else {}
    name = re.sub(r"\s+", " ", str(data.get("name") or "").strip())[:40] or "未命名工作流"
    members = _wf_clean_members(data.get("members"))
    cwd = str(data.get("cwd") or "").strip()
    task = str(data.get("taskTemplate") or "").strip()[:2000]
    layout = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    nodes = {}
    for k, v in (layout.get("nodes") or {}).items():
        if isinstance(v, dict):
            nodes[str(k)[:24]] = _wf_pos(v)
    try:
        zoom = float(layout.get("zoom") or 1)
    except (TypeError, ValueError):
        zoom = 1.0
    zoom = max(0.6, min(1.6, zoom))
    now = _now()
    with _WF_LOCK:
        d = _wf_load()
        wfs = d.get("workflows") or []
        wid = str(data.get("id") or "").strip()
        cur = next((w for w in wfs if isinstance(w, dict)
                    and str(w.get("id")) == wid), None) if wid else None
        if cur is None:
            wid = "wf_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
            cur = {"id": wid, "created": now, "runCount": 0, "lastRunId": ""}
            wfs.append(cur)
        cur.update({"name": name, "cwd": cwd, "taskTemplate": task,
                    "members": members, "layout": {"zoom": zoom, "nodes": nodes},
                    "updated": now})
        wfs.sort(key=lambda w: w.get("updated") or 0, reverse=True)
        d["workflows"] = wfs[:KEEP_WORKFLOWS]
        d["version"] = 1
        _atomic_write(WORKFLOWS_FILE, d)
    return dict(cur)


def delete_workflow(wid: str) -> bool:
    """删除工作流。⚠ **不删任何 run**（历史运行保留，只是不再归属）。"""
    wid = str(wid or "").strip()
    if not wid or wid.startswith(".") or "/" in wid or "\\" in wid:
        raise ValueError("非法的工作流 id")
    with _WF_LOCK:
        d = _wf_load()
        wfs = [w for w in (d.get("workflows") or []) if isinstance(w, dict)]
        left = [w for w in wfs if str(w.get("id")) != wid]
        if len(left) == len(wfs):
            return False
        d["workflows"] = left
        _atomic_write(WORKFLOWS_FILE, d)
    return True


def duplicate_workflow(wid: str, name: str = "") -> dict:
    """复制一份（成员 + 默认任务 + 画布布局），新 id、运行计数归零。"""
    wid = str(wid or "").strip()
    with _WF_LOCK:
        d = _wf_load()
        src = next((w for w in (d.get("workflows") or [])
                    if isinstance(w, dict) and str(w.get("id")) == wid), None)
        if not src:
            raise ValueError("没有这个工作流")
        cp = json.loads(json.dumps(src))
        now = _now()
        cp["id"] = "wf_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
        cp["name"] = re.sub(r"\s+", " ", str(name or "").strip())[:40] or (str(src.get("name") or "") + " 副本")[:40]
        cp["created"] = now
        cp["updated"] = now
        cp["runCount"] = 0
        cp["lastRunId"] = ""
        d.setdefault("workflows", []).append(cp)
        d["workflows"].sort(key=lambda w: w.get("updated") or 0, reverse=True)
        d["workflows"] = d["workflows"][:KEEP_WORKFLOWS]
        _atomic_write(WORKFLOWS_FILE, d)
    return cp


def _wf_touch_run(wid: str, rid: str) -> None:
    """run 启动后，把 lastRunId / runCount 写回工作流（不改成员快照）。"""
    if not wid:
        return
    with _WF_LOCK:
        d = _wf_load()
        hit = False
        for w in (d.get("workflows") or []):
            if isinstance(w, dict) and str(w.get("id")) == str(wid):
                w["lastRunId"] = str(rid)
                w["runCount"] = int(w.get("runCount") or 0) + 1
                w["updated"] = _now()
                hit = True
                break
        if hit:
            _atomic_write(WORKFLOWS_FILE, d)


def delete_run(run_id: str) -> bool:
    """删除一条**已结束**的运行记录（JSON + 该任务的成员会话文件）。

    * 还在跑 → 明确拒绝（先「取消全部」，等进程退出再删）；
    * 不删 agent 侧（opencode-acp 共享库）的会话 —— 那是 agent 自己的数据。
    """
    rid = str(run_id or "").strip()
    if not rid or os.path.basename(rid) != rid or ".." in rid:
        raise ValueError("非法的任务 id")
    with _LOCK:
        run = _RUNS.get(rid) or _load_file(rid)
        if not run:
            return False
        if str(run.get("status") or "") == "running" or any(
                m.get("status") in ("wait", "running") for m in (run.get("members") or [])):
            raise ValueError("任务还在跑：先点「取消全部」，等它停下来再删除")
        if any(str(k[0]) == rid for k in _ACTIVE):
            raise ValueError("任务还在收尾（agent 进程没退出）：等几秒再删")
        # 清掉这个任务残留的待批 / 取消标记（正常应该已经空了）
        for pid in [p for p, v in _PENDING.items() if str(v.get("run")) == rid]:
            _PENDING.pop(pid, None)
        for key in [k for k in _CANCEL if str(k[0]) == rid]:
            _CANCEL.pop(key, None)
        _RUNS.pop(rid, None)
    try:
        os.remove(_run_path(rid))
    except FileNotFoundError:
        pass
    except OSError as exc:
        acp_log("[orchestrator] 删除运行记录文件失败 %s：%s" % (rid, exc))
    run_dir = os.path.join(ROOT, "runs", rid)
    if os.path.isdir(run_dir):
        try:
            shutil.rmtree(run_dir)
        except OSError as exc:
            acp_log("[orchestrator] 删除运行目录失败 %s：%s" % (rid, exc))
    acp_log("[orchestrator] 已删除运行记录 %s" % rid)
    return True


# ---------------- 权限队列 ----------------

# ★每个成员可以有自己的权限等级（节点级）：""=跟随面板；trust=信任（非破坏性自动放行）
PERM_LEVELS = [
    {"id": "", "label": "跟随面板", "note": "用面板当前等级"},
    {"id": "ask", "label": "全部询问", "note": "每条命令都问"},
    {"id": "read", "label": "只读自动", "note": "读自动；写/执行要问"},
    {"id": "edit", "label": "读写自动", "note": "工作区内读写自动"},
    {"id": "exec", "label": "执行自动", "note": "工作区内读写执行自动"},
    {"id": "trust", "label": "信任（非破坏性自动）", "note": "不再逐条问；破坏性操作仍然必须你确认"},
]
_PERM_IDS = {x["id"] for x in PERM_LEVELS}


def norm_perm_level(v) -> str:
    """成员权限等级归一；不认识的值回空（= 跟随面板），不猜。"""
    s = str(v or "").strip().lower()
    return s if s in _PERM_IDS else ""


def _pending_public(p: dict) -> dict:
    return {k: p.get(k) for k in
            ("id", "run", "member", "memberName", "kind", "title", "locations",
             "command", "destructive", "created", "status", "decision", "decidedAt")}


def pending_permissions(run_id: str = "") -> list:
    with _LOCK:
        items = [p for p in _PENDING.values()
                 if p.get("run") and (not run_id or str(p["run"]) == str(run_id))]
    return [_pending_public(p) for p in items]


def reply_permission(perm_id: str, decision: str) -> bool:
    d = str(decision or "").strip().lower()
    if d not in ("once", "always", "reject"):
        raise ValueError("decision 只能是 once / always / reject")
    with _LOCK:
        p = _PENDING.get(str(perm_id))
        if not p:
            return False
        p["decision"] = d
        p["status"] = "allowed" if d in ("once", "always") else "denied"
        p["decidedAt"] = _now()
        run = _RUNS.get(p["run"])
        if run:
            for item in (run.get("permissions") or []):
                if item.get("id") == p["id"]:
                    item.update({"status": p["status"], "decision": d,
                                 "decidedAt": p["decidedAt"]})
                    break
            _save(run)
        p["event"].set()
    return True


def _ask_permission(run: dict, member: dict, kind, title, locations, options, info) -> str:
    """auto_permission 钩子：等级命中→放行；否则排队等 UI（超时/取消→拒绝）。"""
    cmd = str((info or {}).get("command") or "")
    destructive = _is_destructive(cmd, title)
    net = _is_net(kind, title, cmd, locations)
    # ★节点级权限：成员自己的 permLevel 优先（"trust" = 非破坏性一律放行，破坏性仍然要问）
    mlevel = norm_perm_level(member.get("permLevel"))
    level = mlevel or perm_level()
    ok = (not destructive) and (mlevel == "trust"
                                or (net and allow_network())
                                or allowed_by_level(level, kind, title, cmd, locations,
                                                    run.get("cwd") or ""))
    if ok:
        if mlevel == "trust":
            acp_log("[orchestrator] %s/%s 自动放行（成员信任，非破坏性）：%s"
                    % (run["id"], member["id"], title or kind or ""))
        return "once"
    pid = "operm_%s" % uuid.uuid4().hex[:10]
    p = {"id": pid, "run": run["id"], "member": member["id"],
         "memberName": member.get("name") or member["id"], "kind": kind or "",
         "title": title or "", "locations": list(locations or []),
         "command": cmd, "destructive": bool(destructive), "created": _now(),
         "status": "pending", "decision": None, "event": threading.Event()}
    with _LOCK:
        _PENDING[pid] = p
        run.setdefault("permissions", []).append(
            {k: p.get(k) for k in ("id", "run", "member", "memberName", "kind", "title",
                                   "locations", "command", "destructive", "created",
                                   "status", "decision", "decidedAt")})
        _save(run)
    # 记一条活动，UI 即使不看待批列表也能看到"卡在哪"
    member.setdefault("activities", []).append({
        "ts": _now(), "kind": "permission", "name": title or kind or "权限",
        "status": "waiting", "command": cmd,
        "destructive": bool(destructive)})
    member["activities"] = member["activities"][-100:]
    acp_log("[orchestrator] %s/%s 权限等待：%s%s"
            % (run["id"], member["id"], title, "（破坏性）" if destructive else ""))
    cancel_evt = _CANCEL.get((run["id"], member["id"]))
    deadline = time.time() + ASK_TIMEOUT
    while time.time() < deadline:
        if p["event"].wait(0.5):
            break
        if cancel_evt is not None and cancel_evt.is_set():
            break
    decision = p.get("decision") or "reject"
    if cancel_evt is not None and cancel_evt.is_set():
        decision = "reject"
    if p.get("status") == "pending":
        p["status"] = "timeout" if decision == "reject" and not cancel_evt else "denied"
        p["decision"] = decision
        p["decidedAt"] = _now()
        with _LOCK:
            for item in (run.get("permissions") or []):
                if item.get("id") == pid:
                    item.update({"status": p["status"], "decision": decision,
                                 "decidedAt": p["decidedAt"]})
            _save(run)
    with _LOCK:
        _PENDING.pop(pid, None)
    # 用户选了「始终允许」→ 写进 ACP 的 always 记忆（和面板同一条）
    if decision == "always":
        try:
            active = _ACTIVE.get((run["id"], member["id"]))
            if active:
                active["svc"]._remember_always(active["sid"], kind, locations, title)
        except Exception:  # noqa: BLE001
            pass
    return decision if decision in ("once", "always", "reject") else "reject"


def _auto_form(form, params) -> dict:
    """第一刀：表单自动拒绝（避免 1800s 静默挂住）。权限才排队。"""
    return {"action": "decline"}


# ---------------- agent 解析 / 执行 ----------------

def _resolve_engine(engine: str):
    reg = acp_agents.load_registry()
    entry = acp_agents.find_agent(reg, engine)
    if not entry:
        raise ValueError("没有这个 ACP agent：%s" % engine)
    base = acp_agents.effective_baseline(entry)   # ★agent 自己的 mode 覆盖基线（与 engine 一致）
    env = {}
    env.update(base.get("env") or {})
    env.update(entry.get("env") or {})
    # ★安全兜底（第 ② 层）：本地协作也不允许静默用「永不问」模式
    env, _sec_fixes = acp_agents.sanitize_agent_env(entry.get("id"), env)
    cwd = str(entry.get("cwd") or base.get("cwd") or "").strip()
    return entry, base, env, cwd


def _assistant_text(svc, sid, mid) -> str:
    """取某条助手消息的正文（不依赖 messages()/共享库）。"""
    try:
        with svc._lock:
            s = svc._sessions.get(sid)
            msgs = list((s or {}).get("messages") or [])
    except Exception:  # noqa: BLE001
        return ""
    for m in reversed(msgs):
        if not isinstance(m, dict) or m.get("type") != "assistant":
            continue
        if mid and str(m.get("id")) != str(mid):
            continue
        parts = [str(p.get("text") or "") for p in (m.get("content") or [])
                 if isinstance(p, dict) and p.get("type") == "text"]
        txt = _strip_model_warning("\n".join(x for x in parts if x).strip())
        if txt:
            return txt
        if mid:
            return ""
    return ""


_MODEL_WARN_RE = re.compile(
    r"^\s*Warning: Model metadata for `[^`]*` not found\.[^\n]*\n+", re.I)


def _strip_model_warning(text: str) -> str:
    """Codex 在自定义 provider 下会把「Model metadata … not found」当正文开头（纯噪音）。

    面板本身在渲染时也剥一次；这里再剥一次 → 工作台 / 仲裁 / 对比拿到的都是干净正文。
    """
    return _MODEL_WARN_RE.sub("", str(text or "")).strip()


def _member_snapshot(svc, sid) -> tuple:
    try:
        with svc._lock:
            s = svc._sessions.get(sid) or {}
            return dict(s.get("tokens") or {}), s.get("context")
    except Exception:  # noqa: BLE001
        return {}, None


def _update_run(run: dict) -> None:
    with _LOCK:
        if run.get("status") in ("running",):
            states = [m.get("status") for m in (run.get("members") or [])]
            if states and all(x in ("done", "failed", "timeout", "cancelled") for x in states):
                done = [x for x in states if x == "done"]
                if not done and all(x == "cancelled" for x in states):
                    run["status"] = "cancelled"
                elif not done:
                    run["status"] = "failed"
                elif len(done) == len(states):
                    run["status"] = "done"
                else:
                    run["status"] = "partial"
        _save(run)


def _run_member(run: dict, member: dict, prompt_override: str = "") -> None:
    rid, mid = run["id"], member["id"]
    member["status"] = "running"
    member["started"] = _now()
    with _LOCK:
        _save(run)
    svc = None
    sid = ""
    q = None
    try:
        entry, base, env, cfg_cwd = _resolve_engine(member.get("engine") or "")
        cwd = str(run.get("cwd") or cfg_cwd or "").strip() or None
        run_dir = os.path.join(ROOT, "runs", rid)
        sessions_file = os.path.join(run_dir, "%s.sessions.json" % mid)
        # ★ 每个成员独立会话文件：不碰面板/群聊的 _acp_sessions.json
        svc = AcpService(list(entry.get("command") or []), cwd=cwd, env=env, log=acp_log,
                         agent_id=str(member.get("engine") or ""),
                         provider_id=entry.get("provider") or "", baseline=base,
                         sessions_file=sessions_file)
        svc._sessions = {}
        svc._order = []
        svc._bound = set()
        svc.auto_permission = (lambda kind, title, locations, options, info=None:
                               _ask_permission(run, member, kind, title, locations,
                                               options, info))
        svc.auto_form = _auto_form
        model = ""
        try:
            model = str((_load_ok_models() or {}).get(member.get("engine") or "") or "")
        except Exception:  # noqa: BLE001
            model = ""
        pub = svc.create_session(title="协作·%s" % (member.get("name") or mid),
                                 cwd=cwd, model=({"id": model} if model else None))
        sid = str(pub.get("id") or "")
        member["sessionID"] = sid
        with _LOCK:
            _ACTIVE[(rid, mid)] = {"svc": svc, "sid": sid}
            _save(run)
        body = prompt_override or str(run.get("task") or "")
        rl = role_line(member.get("role") or "")
        prompt = (rl + "\n\n" + body).strip() if rl else body
        q = svc.subscribe()
        t0 = time.time()
        cancel_evt = _CANCEL.get((rid, mid))
        try:
            svc.prompt(sid, prompt)
        except BusyError:
            member["status"] = "failed"
            member["error"] = "这个 agent 还在忙（上一轮未结束）"
            return
        turn_id = None
        while True:
            if cancel_evt is not None and cancel_evt.is_set():
                try:
                    svc.interrupt(sid)
                except Exception:  # noqa: BLE001
                    pass
                member["status"] = "cancelled"
                member["cancelled"] = True
                break
            if time.time() - t0 > RUN_TIMEOUT:
                try:
                    svc.interrupt(sid)
                except Exception:  # noqa: BLE001
                    pass
                member["status"] = "timeout"
                member["error"] = "超时（%ds）" % int(RUN_TIMEOUT)
                break
            try:
                ev = q.get(timeout=0.4)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(ev, dict):
                continue
            d = ev.get("data") or {}
            if str(d.get("sessionID")) != sid:
                continue
            t = str(ev.get("type") or "")
            if t == "session.step.started":
                turn_id = d.get("assistantMessageID")
            elif t == "session.tool.called":
                kind = str(d.get("kind") or "")
                if kind.lower() != "think":
                    member.setdefault("activities", []).append({
                        "ts": _now(), "kind": "tool", "name": str(d.get("name") or "工具"),
                        "status": "running", "detail": str(d.get("title") or "")[:160],
                        "toolCallId": d.get("toolCallId")})
                    member["activities"] = member["activities"][-100:]
            elif t in ("session.tool.success", "session.tool.error"):
                for a in reversed(member.get("activities") or []):
                    if a.get("toolCallId") == d.get("toolCallId") and a.get("status") == "running":
                        a["status"] = "done" if t.endswith("success") else "error"
                        break
            elif t == "session.error":
                member["error"] = str((d or {}).get("error") or "agent 报错")[:500]
            elif t in ("session.step.ended", "session.step.failed"):
                mid_id = d.get("assistantMessageID") or turn_id
                member["output"] = _assistant_text(svc, sid, mid_id)
                member["status"] = "done" if t.endswith("ended") else "failed"
                if t.endswith("failed") and not member.get("error"):
                    member["error"] = "这一轮失败（agent 没给原因）"
                break
        member["ended"] = _now()
        try:
            member["tokens"], member["context"] = _member_snapshot(svc, sid)
        except Exception:  # noqa: BLE001
            pass
        acp_log("[orchestrator] %s/%s %s（%.1fs）"
                % (rid, mid, member.get("status"), time.time() - t0))
    except Exception as exc:  # noqa: BLE001
        member["status"] = "failed"
        member["error"] = "%s: %s" % (type(exc).__name__, exc)
        acp_log("[orchestrator] %s/%s 失败：%s" % (rid, mid, member["error"]))
    finally:
        if q is not None and svc is not None:
            try:
                svc.unsubscribe(q)
            except Exception:  # noqa: BLE001
                pass
        with _LOCK:
            _ACTIVE.pop((rid, mid), None)
            _CANCEL.pop((rid, mid), None)
            _update_run(run)


def _start_threads(run: dict) -> None:
    for m in run.get("members") or []:
        with _LOCK:
            _CANCEL[(run["id"], m["id"])] = threading.Event()
        threading.Thread(target=_run_member, args=(run, m), daemon=True,
                         name="orch-%s-%s" % (run["id"], m["id"])).start()


# ---------------- 对外 API ----------------

def catalog() -> dict:
    reg = acp_agents.load_registry()
    out = []
    for a in (reg.get("agents") or []):
        if not isinstance(a, dict) or not a.get("id"):
            continue
        try:
            st = acp_agents.status_of(a) or {}
            avail = bool(st.get("available", True))
            missing = st.get("missing") or []
        except Exception:  # noqa: BLE001
            avail, missing = True, []
        out.append({"id": a.get("id"), "label": a.get("label") or a.get("id"),
                    "available": avail, "missing": missing,
                    "provider": a.get("provider") or ""})
    try:
        from cocraft_agents import roles_public, team_presets
        roles, presets = roles_public(), team_presets()
    except Exception:  # noqa: BLE001
        roles, presets = {k: {"label": v.get("label", k)} for k, v in ROLES.items()}, []
    return {"agents": out, "roles": roles, "presets": presets,
            # ★节点级权限等级（画布检查器 / 运行详情里给每个成员单独设）
            "permLevels": PERM_LEVELS,
            # 前端要用它算「待批还剩多少秒」；不暴露的话只能猜一个 180
            "askTimeout": int(ASK_TIMEOUT), "runTimeout": int(RUN_TIMEOUT)}


def start_run(task: str, cwd: str = "", members=None, title: str = "",
              workflow_id: str = "") -> dict:
    # ★ 从工作流启动：成员/目录/标题直接用工作流的（成员快照进 run，之后改工作流不影响历史）
    wf = None
    if workflow_id:
        wf = next((w for w in list_workflows() if str(w.get("id")) == str(workflow_id)), None)
        if wf is None:
            raise ValueError("工作流不存在（可能已被删除）")
        if not members:
            members = wf.get("members")
        if not str(cwd or "").strip():
            cwd = wf.get("cwd") or ""
        if not str(title or "").strip():
            title = wf.get("name") or ""
    task = str(task or "").strip()
    if not task and wf:
        task = str(wf.get("taskTemplate") or "").strip()
    if not task:
        raise ValueError("任务不能为空")
    if not isinstance(members, list) or not members:
        raise ValueError("至少要有一个成员")
    if len(members) > 8:
        raise ValueError("一次最多 8 个成员")
    rid = "run_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
    ms = []
    for i, raw in enumerate(members):
        raw = raw if isinstance(raw, dict) else {}
        engine = str(raw.get("engine") or "").strip()
        if not engine:
            raise ValueError("成员 #%d 没选引擎" % (i + 1))
        role = norm_role(raw.get("role"))
        name = re.sub(r"\s+", " ", str(raw.get("name") or "").strip())[:24] or ("m%d" % (i + 1))
        ms.append({"id": "m%d" % (i + 1), "name": name, "role": role,
                   "roleLabel": (ROLES.get(role) or {}).get("label") or "",
                   "engine": engine, "permLevel": norm_perm_level(raw.get("permLevel")),
                   "status": "wait", "output": "", "error": "",
                   "created": _now(), "started": 0, "ended": 0,
                   "tokens": {}, "context": None, "activities": [],
                   "kind": "member", "sessionID": "", "cancelled": False})
    run = {"id": rid, "task": task, "title": str(title or task)[:80],
           "cwd": str(cwd or "").strip(), "status": "running",
           "workflowId": str((wf or {}).get("id") or ""),
           "workflowName": str((wf or {}).get("name") or ""),
           "created": _now(), "updated": _now(), "members": ms,
           "permissions": [], "arbitration": None, "error": ""}
    with _LOCK:
        _RUNS[rid] = run
        _save(run)
    os.makedirs(os.path.join(ROOT, "runs", rid), exist_ok=True)
    _start_threads(run)
    if wf:
        try:
            _wf_touch_run(str(wf.get("id")), rid)
        except Exception:  # noqa: BLE001
            pass
    acp_log("[orchestrator] 开始任务 %s：%d 个成员%s"
            % (rid, len(ms), ("（工作流 %s）" % wf.get("name")) if wf else ""))
    return public_run(run)


def cancel_run(run_id: str, member_id: str = "") -> dict:
    rid = str(run_id or "")
    with _LOCK:
        run = _RUNS.get(rid)
    if run is None:
        raise ValueError("没有这个任务：%s" % rid)
    targets = [str(member_id)] if member_id else [m["id"] for m in (run.get("members") or [])]
    for mid in targets:
        ev = _CANCEL.get((rid, mid))
        if ev:
            ev.set()
        act = _ACTIVE.get((rid, mid))
        if act:
            try:
                act["svc"].interrupt(act["sid"])
            except Exception:  # noqa: BLE001
                pass
    with _LOCK:
        if not member_id:
            run["status"] = "cancelled"
        _save(run)
    return public_run(run)


def set_member(run_id: str, member_id: str, patch) -> dict:
    """改某个成员的运行时设置（目前只允许 permLevel）。

    ★ 运行中也能改：改完**下一条**权限申请立刻按新等级判（不用重启任务）。
      （已经挂起的申请不会被自动放行 —— 那一条仍然要你拍板。）
    """
    rid = str(run_id or "")
    mid = str(member_id or "")
    with _LOCK:
        run = _RUNS.get(rid)
    if run is None:
        run = _load_file(rid)
        if run:
            with _LOCK:
                _RUNS[rid] = run
    if not run:
        raise ValueError("没有这个任务：%s" % rid)
    m = next((x for x in (run.get("members") or []) if str(x.get("id")) == mid), None)
    if not m:
        raise ValueError("这个任务没有成员：%s" % mid)
    patch = patch if isinstance(patch, dict) else {}
    if "permLevel" in patch:
        m["permLevel"] = norm_perm_level(patch.get("permLevel"))
    m["updated"] = _now()
    run["updated"] = _now()
    with _LOCK:
        _save(run)
    acp_log("[orchestrator] %s/%s 成员权限 -> %s"
            % (rid, mid, m.get("permLevel") or "跟随面板"))
    return public_run(run)


def arbitrate(run_id: str, engine: str, name: str = "审查者") -> dict:
    rid = str(run_id or "")
    with _LOCK:
        run = _RUNS.get(rid)
    if run is None:
        run = _load_file(rid)
        if run:
            with _LOCK:
                _RUNS[rid] = run
    if not run:
        raise ValueError("没有这个任务：%s" % rid)
    if run.get("status") == "running":
        raise ValueError("任务还在跑，等完成后再仲裁")
    done = [m for m in (run.get("members") or [])
            if m.get("status") == "done" and str(m.get("output") or "").strip()]
    if len(done) < 2:
        raise ValueError("至少要有 2 个成员完成，才能对比仲裁")
    if run.get("arbitration") and run["arbitration"].get("status") in ("running",):
        raise ValueError("已经有一次仲裁在进行")
    engine = str(engine or "").strip()
    if not engine:
        raise ValueError("要给仲裁者选一个引擎")
    mid = "arb"
    blocks = []
    for m in done:
        label = m.get("name") or m.get("id")
        blocks.append("【方案｜%s（%s）】\n%s" % (label, m.get("roleLabel") or m.get("role") or "成员",
                                                _strip_model_warning(m.get("output") or "")))
    prompt = ("你是本次任务的**审查者**。下面是同一次任务里几位成员的方案，请评审并给出结论。\n"
              "要求：① 每个方案给 1-2 句具体评价（指出问题，不写场面话）；"
              "② 结尾单独一行用「结论：推荐 <方案名>」收口，并说一句为什么；"
              "③ 只评审、不要改代码。\n\n任务：%s\n\n%s"
              % (run.get("task"), "\n\n".join(blocks)))
    member = {"id": mid, "name": str(name or "审查者")[:24], "role": "reviewer",
              "roleLabel": "审查者", "engine": engine, "status": "wait", "output": "",
              "error": "", "created": _now(), "started": 0, "ended": 0,
              "tokens": {}, "context": None, "activities": [], "kind": "arbitration",
              "sessionID": "", "cancelled": False}
    run.setdefault("members", []).append(member)
    run["arbitration"] = {"member": mid, "status": "running", "pick": "",
                          "created": _now()}
    with _LOCK:
        _CANCEL[(rid, mid)] = threading.Event()
        _save(run)
    threading.Thread(target=_run_arbitration, args=(run, member, prompt), daemon=True,
                     name="orch-arb-%s" % rid).start()
    return public_run(run)


def _run_arbitration(run: dict, member: dict, prompt: str) -> None:
    _run_member(run, member, prompt_override=prompt)
    pick = ""
    m = re.search(r"推荐\s*[:：]?\s*[*_`]*([^\n，,。;；]{1,24})", str(member.get("output") or ""))
    if m:
        pick = m.group(1).replace("*", "").replace("`", "").strip()
    with _LOCK:
        run["arbitration"] = {"member": member.get("id"), "status": member.get("status"),
                              "pick": pick, "created": run.get("arbitration", {}).get("created")}
        _save(run)


def stats() -> dict:
    with _LOCK:
        return {"runs": len(_RUNS), "pending": len(_PENDING), "active": len(_ACTIVE)}
