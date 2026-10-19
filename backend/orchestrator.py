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
KEEP_RUNS = 60
ASK_TIMEOUT = float(os.environ.get("COCRAFT_ORCH_ASK_TIMEOUT") or 180)   # 等批准（秒）
RUN_TIMEOUT = float(os.environ.get("COCRAFT_ORCH_TIMEOUT") or 600)       # 单个成员上限（秒）

_LOCK = threading.RLock()
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
             "kind", "sessionID", "cancelled", "files", "pick")}


def public_run(run: dict) -> dict:
    if not run:
        return {}
    return {"id": run.get("id"), "task": run.get("task"), "cwd": run.get("cwd"),
            "title": run.get("title"), "status": run.get("status"),
            "created": run.get("created"), "updated": run.get("updated"),
            "members": [_member_public(m) for m in (run.get("members") or [])],
            # 全部申请（pending + 已结算）都回；「谁批准了什么」要能追溯
            "permissions": [p for p in (run.get("permissions") or [])],
            "arbitration": run.get("arbitration"),
            "error": run.get("error") or ""}


def list_runs(limit: int = 30) -> list:
    with _LOCK:
        runs = list(_RUNS.values())
    if not runs and os.path.isdir(RUNS_DIR):
        for name in sorted(os.listdir(RUNS_DIR), reverse=True)[:limit]:
            if name.endswith(".json"):
                d = _load_file(name[:-5])
                if d:
                    runs.append(d)
    runs.sort(key=lambda r: r.get("created") or 0, reverse=True)
    return [{"id": r.get("id"), "task": r.get("task"), "title": r.get("title"),
             "status": r.get("status"), "created": r.get("created"),
             "updated": r.get("updated"), "members": len(r.get("members") or [])}
            for r in runs[:limit]]


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


# ---------------- 权限队列 ----------------

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
    level = perm_level()
    ok = (not destructive) and ((net and allow_network())
                                or allowed_by_level(level, kind, title, cmd, locations,
                                                    run.get("cwd") or ""))
    if ok:
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
    base = acp_agents.baseline()
    env = {}
    env.update(base.get("env") or {})
    env.update(entry.get("env") or {})
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
        txt = "\n".join(x for x in parts if x).strip()
        if txt:
            return txt
        if mid:
            return ""
    return ""


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
                if not done:
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
    return {"agents": out, "roles": roles, "presets": presets}


def start_run(task: str, cwd: str = "", members=None, title: str = "") -> dict:
    task = str(task or "").strip()
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
                   "engine": engine, "status": "wait", "output": "", "error": "",
                   "created": _now(), "started": 0, "ended": 0,
                   "tokens": {}, "context": None, "activities": [],
                   "kind": "member", "sessionID": "", "cancelled": False})
    run = {"id": rid, "task": task, "title": str(title or task)[:80],
           "cwd": str(cwd or "").strip(), "status": "running",
           "created": _now(), "updated": _now(), "members": ms,
           "permissions": [], "arbitration": None, "error": ""}
    with _LOCK:
        _RUNS[rid] = run
        _save(run)
    os.makedirs(os.path.join(ROOT, "runs", rid), exist_ok=True)
    _start_threads(run)
    acp_log("[orchestrator] 开始任务 %s：%d 个成员" % (rid, len(ms)))
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
                                                str(m.get("output") or "").strip()))
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
