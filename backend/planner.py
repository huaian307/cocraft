# -*- coding: utf-8 -*-
r"""★策划会话（Planner Session）—— P0 核心。

一个策划会话 = 一个项目的控制器：
  * 产出/校验 Plan JSON（可执行真相）
  * 按计划驱动工作流/Runner（复用 orchestrator）
  * 汇总运行结果，生成完成报告

数据落盘（本机）：
  runtime/state/_planner_sessions.json          会话索引
  runtime/state/_planner/<sid>/plan.json        计划（唯一可执行真相）
  runtime/state/_planner/<sid>/PLAN.md          人读版（由 plan.json 渲染）
  runtime/state/_planner/<sid>/events.jsonl     事件流（可追加，P0 先简单写）

⚠ 约束（对齐 docs/策划会话-方案.md）：
  - 一个会话 = 一个项目（projectKey/cwd 不可变）
  - plan 校验失败不能运行
  - 策划 agent 不能给自己/成员批权限（这里只做计划/派活，不暴露批准接口）
  - 破坏性操作仍走现有审批/隔离链路（由 orchestrator/runner 负责）
"""
from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import uuid
from typing import Any

try:
    import orchestrator
except Exception:  # pragma: no cover - test isolation
    orchestrator = None  # type: ignore
try:
    import audit
except Exception:  # pragma: no cover
    audit = None  # type: ignore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(ROOT, "runtime", "state")
PLANNER_DIR = os.path.join(STATE_DIR, "_planner")
SESSIONS_FILE = os.path.join(STATE_DIR, "_planner_sessions.json")

_LOCK = threading.Lock()

# 角色/权限白名单（与 orchestrator/cocraft_agents 对齐；校验时尽量复用运行时事实）
DEFAULT_ROLES = {"researcher", "implementer", "reviewer", "tester"}
DEFAULT_PERM_IDS = {"ask", "read", "edit", "exec", "trust", "all"}
MAX_MEMBERS = 8
MAX_PARALLEL = 8
MAX_BUDGET = 500_000


def _now_ts() -> int:
    return int(time.time())


def _atomic_write(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _project_key(cwd: str) -> str:
    cwd = str(cwd or "").strip()
    if not cwd:
        return ""
    # Windows: normcase + normpath; keep as a stable key (not necessarily an existing path at validation-only time)
    try:
        cwd = os.path.abspath(cwd)
    except Exception:
        pass
    return os.path.normcase(os.path.normpath(cwd))


def _sid_dir(sid: str) -> str:
    # sid is controlled; still be defensive
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", str(sid or ""))
    return os.path.join(PLANNER_DIR, safe)


def _load_sessions() -> dict:
    d = _read_json(SESSIONS_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("sessions", [])
    if not isinstance(d["sessions"], list):
        d["sessions"] = []
    return d


def _save_sessions(d: dict) -> None:
    _atomic_write(SESSIONS_FILE, d)


def _find_session(d: dict, sid: str):
    for s in d.get("sessions") or []:
        if isinstance(s, dict) and str(s.get("id")) == str(sid):
            return s
    return None


def _perm_ids() -> set:
    try:
        if orchestrator and hasattr(orchestrator, "PERM_LEVELS"):
            return {str(x.get("id")) for x in (orchestrator.PERM_LEVELS or []) if isinstance(x, dict) and x.get("id")}
    except Exception:
        pass
    return set(DEFAULT_PERM_IDS)


def _role_ids() -> set:
    try:
        if orchestrator and hasattr(orchestrator, "catalog"):
            cat = orchestrator.catalog() or {}
            roles = cat.get("roles") or {}
            if isinstance(roles, dict) and roles:
                return {str(k) for k in roles.keys()}
    except Exception:
        pass
    return set(DEFAULT_ROLES)


def _agent_ids() -> set:
    try:
        if orchestrator and hasattr(orchestrator, "catalog"):
            cat = orchestrator.catalog() or {}
            agents = cat.get("agents") or []
            out = set()
            for a in agents:
                if isinstance(a, dict) and a.get("id"):
                    out.add(str(a["id"]))
            return out
    except Exception:
        pass
    return set()


def _new_token() -> str:
    return "plt_" + secrets.token_urlsafe(24)


# ---------------- Plan model / validation ----------------

def default_plan(session: dict, goal: str = "") -> dict:
    sid = str(session.get("id") or "")
    cwd = str(session.get("cwd") or "")
    now = _now_ts()
    return {
        "schema": "cocraft.plan/v1",
        "id": "plan_" + uuid.uuid4().hex[:12],
        "sessionId": sid,
        "projectKey": _project_key(cwd),
        "cwd": cwd,
        "title": str(session.get("title") or "新计划"),
        "goal": str(goal or ""),
        "constraints": {
            "budgetYuan": 0,
            "deadline": "",
            "noDestructiveWithoutConfirm": True,
            "network": "inherit",
        },
        "stages": [
            {
                "id": "s1",
                "name": "实现",
                "objective": str(goal or "完成目标"),
                "inputs": [],
                "members": [
                    {
                        "id": "m1",
                        "engine": "",
                        "agentId": "",
                        "role": "implementer",
                        "permLevel": "edit",
                        "prompt": "",
                        "parallelGroup": "g1",
                    }
                ],
                "doneWhen": "产出可审查的结果",
                "onFail": "ask",
                "retries": 0,
            }
        ],
        "successCriteria": [],
        "risks": [],
        "status": "draft",
        "version": 1,
        "createdAt": now,
        "updatedAt": now,
    }


def validate_plan(plan: Any, session: dict | None = None) -> dict:
    """返回 {ok, errors[], warnings[], plan(normalized-ish)}。"""
    errors = []
    warnings = []
    if not isinstance(plan, dict):
        return {"ok": False, "errors": ["plan 必须是 JSON 对象"], "warnings": [], "plan": None}

    p = json.loads(json.dumps(plan))  # cheap deep copy
    schema = str(p.get("schema") or "")
    if schema and schema != "cocraft.plan/v1":
        warnings.append("未知 schema=%s（按 v1 尝试校验）" % schema)
    p["schema"] = "cocraft.plan/v1"

    if session:
        sid = str(session.get("id") or "")
        if str(p.get("sessionId") or "") not in ("", sid):
            errors.append("sessionId 与当前策划会话不一致")
        p["sessionId"] = sid
        skey = _project_key(session.get("cwd") or "")
        if skey:
            if str(p.get("projectKey") or "") and _project_key(p.get("projectKey")) != skey and str(p.get("cwd") or "") not in ("", str(session.get("cwd") or "")):
                # be tolerant if cwd matches; projectKey is derived
                warnings.append("projectKey/cwd 与会话项目不一致，已按会话修正")
            p["cwd"] = str(session.get("cwd") or "")
            p["projectKey"] = skey

    cwd = str(p.get("cwd") or "")
    if not cwd.strip():
        errors.append("cwd 不能为空（策划会话必须绑定项目目录）")
    else:
        # 校验时不强制目录已存在？用户可能先规划；但运行会失败。给 warning。
        if not os.path.isdir(cwd):
            warnings.append("cwd 目录当前不存在：%s" % cwd)

    stages = p.get("stages")
    if not isinstance(stages, list) or not stages:
        errors.append("stages 至少需要 1 个阶段")
        stages = []
        p["stages"] = stages
    if len(stages) > 20:
        errors.append("stages 过多（最多 20）")

    perm_ids = _perm_ids()
    role_ids = _role_ids()
    agent_ids = _agent_ids()
    all_members = 0
    stage_ids = set()
    for si, st in enumerate(stages):
        if not isinstance(st, dict):
            errors.append("stages[%d] 必须是对象" % si)
            continue
        stid = str(st.get("id") or ("s%d" % (si + 1)))
        if stid in stage_ids:
            errors.append("阶段 id 重复：%s" % stid)
        stage_ids.add(stid)
        st["id"] = stid
        if not str(st.get("name") or "").strip():
            st["name"] = "阶段 %d" % (si + 1)
        members = st.get("members")
        if not isinstance(members, list) or not members:
            errors.append("阶段 %s 至少需要 1 个成员" % stid)
            members = []
            st["members"] = members
        mids = set()
        for mi, m in enumerate(members):
            if not isinstance(m, dict):
                errors.append("%s.members[%d] 必须是对象" % (stid, mi))
                continue
            all_members += 1
            mid = str(m.get("id") or ("m%d" % (mi + 1)))
            if mid in mids:
                errors.append("阶段 %s 成员 id 重复：%s" % (stid, mid))
            mids.add(mid)
            m["id"] = mid
            role = str(m.get("role") or "implementer").strip()
            if role_ids and role not in role_ids:
                warnings.append("%s/%s: 未知角色 %s（允许运行时按自定义处理）" % (stid, mid, role))
            m["role"] = role or "implementer"
            perm = str(m.get("permLevel") or "edit").strip()
            if perm_ids and perm not in perm_ids:
                errors.append("%s/%s: 未知权限等级 %s" % (stid, mid, perm))
            m["permLevel"] = perm or "edit"
            eng = str(m.get("engine") or m.get("agentId") or m.get("agent") or "").strip()
            # engine/agentId: at plan time may be empty? better require resolvable for execution; warn if empty.
            if not eng:
                warnings.append("%s/%s: 未指定 engine/agentId（运行前必须指定）" % (stid, mid))
            elif agent_ids and eng not in agent_ids:
                warnings.append("%s/%s: agent %s 当前不可用或未知（运行前请确认）" % (stid, mid, eng))
            m["engine"] = eng
            if "agentId" not in m or not m.get("agentId"):
                m["agentId"] = eng
            pg = str(m.get("parallelGroup") or "").strip() or ("g_%s" % stid)
            m["parallelGroup"] = pg
        if len(members) > MAX_MEMBERS:
            errors.append("阶段 %s 成员过多（最多 %d）" % (stid, MAX_MEMBERS))

    if all_members > MAX_MEMBERS * max(1, len(stages)):
        warnings.append("成员总数较多，请注意并行与预算")
    if all_members == 0:
        errors.append("计划中没有可执行成员")

    constraints = p.get("constraints")
    if not isinstance(constraints, dict):
        constraints = {}
        p["constraints"] = constraints
    try:
        by = int(constraints.get("budgetYuan") or 0)
    except Exception:
        by = 0
        errors.append("constraints.budgetYuan 必须是数字")
    if by < 0:
        errors.append("constraints.budgetYuan 不能为负数")
    constraints["budgetYuan"] = by
    constraints.setdefault("noDestructiveWithoutConfirm", True)

    status = str(p.get("status") or "draft")
    if status not in ("draft", "confirmed", "running", "paused", "done", "failed", "canceled"):
        warnings.append("未知 status=%s，按 draft 处理" % status)
        status = "draft"
        p["status"] = status

    try:
        ver = int(p.get("version") or 1)
    except Exception:
        ver = 1
    p["version"] = max(1, ver)
    p["updatedAt"] = _now_ts()
    if not p.get("createdAt"):
        p["createdAt"] = p["updatedAt"]

    # execution gate: warnings about missing engine don't block draft save, but block run? handled by can_run.
    can_run = (len(errors) == 0) and all(
        str(m.get("engine") or m.get("agentId") or "").strip()
        for st in (p.get("stages") or []) if isinstance(st, dict)
        for m in (st.get("members") or []) if isinstance(m, dict)
    ) and bool(str(p.get("cwd") or "").strip())
    if len(errors) == 0 and not can_run:
        warnings.append("计划可保存，但缺少 engine/agentId，运行前需补全")

    return {"ok": len(errors) == 0, "canRun": can_run, "errors": errors, "warnings": warnings, "plan": p}


def plan_markdown(plan: dict) -> str:
    if not isinstance(plan, dict):
        return "# 计划\n\n（无效计划）\n"
    lines = []
    lines.append("# %s" % (plan.get("title") or "计划"))
    lines.append("")
    meta = [
        ("session", plan.get("sessionId")),
        ("project", plan.get("cwd") or plan.get("projectKey")),
        ("status", plan.get("status")),
        ("version", plan.get("version")),
    ]
    for k, v in meta:
        if v not in (None, ""):
            lines.append("- **%s**: %s" % (k, v))
    lines.append("")
    if plan.get("goal"):
        lines.append("## 目标")
        lines.append("")
        lines.append(str(plan["goal"]))
        lines.append("")
    lines.append("## 阶段")
    lines.append("")
    for st in (plan.get("stages") or []):
        if not isinstance(st, dict):
            continue
        lines.append("### %s. %s" % (st.get("id"), st.get("name") or ""))
        if st.get("objective"):
            lines.append("")
            lines.append(str(st["objective"]))
        lines.append("")
        lines.append("| 成员 | 引擎/Agent | 角色 | 权限 | 并行组 |")
        lines.append("|---|---|---|---|---|")
        for m in (st.get("members") or []):
            if not isinstance(m, dict):
                continue
            lines.append("| %s | %s | %s | %s | %s |" % (
                m.get("id"), m.get("engine") or m.get("agentId") or "",
                m.get("role") or "", m.get("permLevel") or "", m.get("parallelGroup") or ""))
        lines.append("")
        if st.get("doneWhen"):
            lines.append("- **完成标准**: %s" % st["doneWhen"])
        if st.get("onFail"):
            lines.append("- **失败策略**: %s" % st["onFail"])
        lines.append("")
    sc = plan.get("successCriteria") or []
    if sc:
        lines.append("## 成功标准")
        lines.append("")
        for x in sc:
            lines.append("- %s" % x)
        lines.append("")
    risks = plan.get("risks") or []
    if risks:
        lines.append("## 风险")
        lines.append("")
        for x in risks:
            if isinstance(x, dict):
                lines.append("- %s：%s（缓解：%s）" % (x.get("id", "?"), x.get("desc", x), x.get("mitigation", "")))
            else:
                lines.append("- %s" % x)
        lines.append("")
    c = plan.get("constraints") or {}
    lines.append("## 约束")
    lines.append("")
    lines.append("- 预算上限: %s" % (c.get("budgetYuan") or 0))
    lines.append("- 破坏性操作需确认: %s" % ("是" if c.get("noDestructiveWithoutConfirm", True) else "否"))
    lines.append("")
    return "\n".join(lines)


# ---------------- Storage ----------------

def _plan_path(sid: str) -> str:
    return os.path.join(_sid_dir(sid), "plan.json")


def _plan_md_path(sid: str) -> str:
    return os.path.join(_sid_dir(sid), "PLAN.md")


def _events_path(sid: str) -> str:
    return os.path.join(_sid_dir(sid), "events.jsonl")


def append_event(sid: str, event: dict) -> None:
    p = _events_path(sid)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    ev = dict(event or {})
    ev.setdefault("ts", _now_ts())
    ev.setdefault("sessionId", sid)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def save_plan(sid: str, plan: dict) -> dict:
    _atomic_write(_plan_path(sid), plan)
    try:
        md = plan_markdown(plan)
        os.makedirs(os.path.dirname(_plan_md_path(sid)), exist_ok=True)
        with open(_plan_md_path(sid), "w", encoding="utf-8") as fh:
            fh.write(md)
    except Exception:
        pass
    return plan


def load_plan(sid: str):
    return _read_json(_plan_path(sid), None)


# ---------------- Sessions ----------------

def create_session(title: str = "", cwd: str = "", goal: str = "", planner_agent: str = "") -> dict:
    cwd = str(cwd or "").strip()
    if not cwd:
        raise ValueError("cwd 不能为空（策划会话必须绑定项目目录）")
    # cwd may not exist yet? allow, but normalize.
    try:
        cwd = os.path.abspath(cwd)
    except Exception:
        pass
    pkey = _project_key(cwd)
    now = _now_ts()
    sid = "plan_sess_" + uuid.uuid4().hex[:12]
    shadow_of = ""
    with _LOCK:
        d0 = _load_sessions()
        for s0 in (d0.get("sessions") or []):
            if not isinstance(s0, dict): continue
            if str(s0.get("projectKey") or "") != pkey: continue
            st0 = str(s0.get("status") or "")
            if str(s0.get("activeRunId") or "") or st0 in ("running", "planning"): shadow_of = str(s0.get("id") or ""); break
    status = "shadow" if shadow_of else "idle"
    sess = {
        "id": sid, "type": "planner", "title": str(title or goal or "策划会话").strip()[:80] or "策划会话",
        "cwd": cwd, "projectKey": pkey, "plannerAgent": str(planner_agent or "").strip(),
        "status": status, "shadowOf": shadow_of, "activeRunId": "", "lastRunId": "",
        "activePlanKey": "main", "planVariants": [{"key":"main","name":"主计划","path":"plan.json","active":True,"updatedAt":now}],
        "token": _new_token(), "createdAt": now, "updatedAt": now,
    }
    with _LOCK:
        d = _load_sessions(); d.setdefault("sessions", []); d["sessions"].insert(0, sess)
        d["sessions"] = d["sessions"][:200]; _save_sessions(d)
    # initialize plan
    plan = default_plan(sess, goal=goal)
    vr = validate_plan(plan, sess)
    if vr.get("plan"):
        save_plan(sid, vr["plan"])
    append_event(sid, {"type": "session.created", "title": sess["title"], "cwd": cwd})
    return sess


def list_sessions(limit: int = 50, cwd: str = "") -> list:
    with _LOCK:
        d = _load_sessions()
        items = [s for s in (d.get("sessions") or []) if isinstance(s, dict)]
    if cwd:
        key = _project_key(cwd)
        items = [s for s in items if str(s.get("projectKey") or "") == key or _project_key(s.get("cwd")) == key]
    items.sort(key=lambda s: int(s.get("updatedAt") or s.get("createdAt") or 0), reverse=True)
    return items[: max(1, min(200, int(limit or 50)))]


def get_session(sid: str, include_token: bool = False):
    with _LOCK:
        d = _load_sessions()
        s = _find_session(d, sid)
        if not s:
            return None
        out = dict(s)
    if not include_token:
        out.pop("token", None)
    plan = load_plan(sid)
    out["planStatus"] = (plan or {}).get("status")
    out["planVersion"] = (plan or {}).get("version")
    out["hasPlan"] = bool(plan)
    return out


def bind_chat_session(sid: str, chat_session_id: str, engine: str = "") -> dict:
    """把策划会话绑定到一个引擎聊天会话（用于上下文自动交接钩子）。"""
    chat_session_id = str(chat_session_id or "").strip()
    if not chat_session_id:
        raise ValueError("chat_session_id 不能为空")
    out = _touch_session(sid, chatSessionId=chat_session_id, chatEngine=str(engine or ""))
    if not out:
        raise ValueError("策划会话不存在")
    append_event(sid, {"type": "session.chat_bound", "chatSessionId": chat_session_id, "engine": engine})
    return {"ok": True, "session": get_session(sid)}


def find_by_chat_session(chat_session_id: str):
    chat_session_id = str(chat_session_id or "").strip()
    if not chat_session_id:
        return None
    with _LOCK:
        d = _load_sessions()
        for s in (d.get("sessions") or []):
            if isinstance(s, dict) and str(s.get("chatSessionId") or "") == chat_session_id:
                return public_session(s)
    return None


def _touch_session(sid: str, **patch) -> dict | None:
    with _LOCK:
        d = _load_sessions()
        s = _find_session(d, sid)
        if not s:
            return None
        s.update(patch)
        s["updatedAt"] = _now_ts()
        _save_sessions(d)
        out = dict(s)
    out.pop("token", None)
    if out.get("status") == "shadow" and out.get("shadowOf"):
        out["note"] = "影子会话：同项目已有活跃策划会话，可查看/编辑草稿，但执行控制应以主会话为准。"
    return out


def delete_session(sid: str) -> bool:
    with _LOCK:
        d = _load_sessions()
        before = len(d.get("sessions") or [])
        d["sessions"] = [s for s in (d.get("sessions") or []) if not (isinstance(s, dict) and str(s.get("id")) == str(sid))]
        changed = len(d["sessions"]) != before
        if changed:
            _save_sessions(d)
    append_event(sid, {"type": "session.deleted"})
    return changed


def auth_token(sid: str, token: str) -> bool:
    token = str(token or "").strip()
    if not token:
        return False
    with _LOCK:
        d = _load_sessions()
        s = _find_session(d, sid)
        if not s:
            return False
        return secrets.compare_digest(str(s.get("token") or ""), token)


# ---------------- Plan operations ----------------

def get_plan(sid: str) -> dict:
    with _LOCK:
        d = _load_sessions()
        sess = _find_session(d, sid)
        if not sess:
            raise ValueError("策划会话不存在")
        sess = dict(sess)
    plan = load_plan(sid)
    if not plan:
        plan = default_plan(sess)
        vr = validate_plan(plan, sess)
        plan = vr.get("plan") or plan
        save_plan(sid, plan)
    return {"session": get_session(sid), "plan": plan, "validation": validate_plan(plan, sess)}


def set_plan(sid: str, plan: dict, expect_version: int | None = None, status: str | None = None) -> dict:
    with _LOCK:
        d = _load_sessions()
        sess = _find_session(d, sid)
        if not sess:
            raise ValueError("策划会话不存在")
        sess = dict(sess)
    cur = load_plan(sid)
    if expect_version is not None and cur and int(cur.get("version") or 1) != int(expect_version):
        raise ValueError("plan 版本冲突（当前 v%s），请先刷新" % (cur.get("version") or 1))
    vr = validate_plan(plan, sess)
    np = vr.get("plan")
    if not np:
        raise ValueError("plan 无效")
    if cur:
        np["version"] = int(cur.get("version") or 1) + 1
        np["createdAt"] = cur.get("createdAt") or np.get("createdAt")
    if status:
        if status not in ("draft", "confirmed"):
            raise ValueError("set_plan 只能设置 draft/confirmed")
        if status == "confirmed" and not (vr.get("ok") and vr.get("canRun")):
            why = vr.get("errors") or vr.get("warnings") or ["请补全成员的 engine/agentId 并修正错误"]
            raise ValueError("计划未就绪，不能确认：" + "；".join([str(x) for x in why]))
        np["status"] = status
    elif vr.get("ok") and str(np.get("status")) not in ("running", "done", "failed", "canceled"):
        # keep current intent; don't auto-confirm
        pass
    save_plan(sid, np)
    append_event(sid, {"type": "plan.updated", "version": np.get("version"), "status": np.get("status"),
                       "ok": vr.get("ok"), "errors": len(vr.get("errors") or [])})
    _touch_session(sid, status="planning" if np.get("status") == "draft" else "idle")
    return {"session": get_session(sid), "plan": np, "validation": vr}


# ---------------- Run / status / report ----------------

def _plan_fail_to_stage(v) -> str:
    """plan failPolicy -> orchestrator stage onFail (P4: stop/continue)."""
    v = str(v or "").strip()
    if v in ("continue", "continue_on_fail", "onFailContinue"):
        return "continue"
    return "stop"  # stop_all / default


def _plan_members_to_workflow(plan: dict) -> tuple[list, list]:
    """plan.stages -> (flat members, stages_meta). Legacy/flat fallback."""
    members = []
    stages_meta = []
    for st in (plan.get("stages") or []):
        if not isinstance(st, dict):
            continue
        sm = {"id": st.get("id"), "name": st.get("name"), "objective": st.get("objective"),
              "doneWhen": st.get("doneWhen"), "memberIds": []}
        for m in (st.get("members") or []):
            if not isinstance(m, dict):
                continue
            eng = str(m.get("engine") or m.get("agentId") or "").strip()
            if not eng:
                continue
            mid = "%s_%s" % (st.get("id") or "s", m.get("id") or "m")
            prompt = str(m.get("prompt") or st.get("objective") or plan.get("goal") or "").strip()
            members.append({
                "id": mid,
                "name": str(m.get("role") or m.get("agentId") or mid),
                "role": str(m.get("role") or "implementer"),
                "engine": eng,
                "task": prompt,
                "stageId": str(st.get("id") or ""),
                "stageName": str(st.get("name") or ""),
            })
            sm["memberIds"].append(mid)
        stages_meta.append(sm)
    return members, stages_meta


def _plan_task_text(plan: dict) -> str:
    """给执行引擎的任务说明：含目标、约束与各阶段/成员分工（补足成员级 prompt 在 workflow 中不直传的缺口）。"""
    parts = ["【目标】\n" + str(plan.get("goal") or "").strip()]
    cons = plan.get("constraints") or []
    if cons:
        parts.append("【约束】\n" + "\n".join("- " + str(c) for c in cons if c))
    for st in (plan.get("stages") or []):
        if not isinstance(st, dict):
            continue
        head = "【阶段 %s：%s】" % (st.get("id") or "", st.get("name") or st.get("objective") or "")
        lines = [head]
        if st.get("objective"):
            lines.append("目标：" + str(st.get("objective")))
        if st.get("doneWhen"):
            lines.append("完成标准：" + str(st.get("doneWhen")))
        for m in (st.get("members") or []):
            if not isinstance(m, dict):
                continue
            who = str(m.get("role") or m.get("agentId") or m.get("id") or "成员")
            eng = str(m.get("engine") or m.get("agentId") or "")
            pr = str(m.get("prompt") or "").strip()
            if pr:
                lines.append("- %s（%s）：%s" % (who, eng, pr))
        parts.append("\n".join(lines))
    return "\n\n".join([p for p in parts if str(p).strip()])[:12000]


def _plan_to_workflow_data(plan: dict, sess: dict) -> dict:
    """把 plan 映射成 orchestrator workflow（保留阶段串行/when/onFail/retries 语义，P4 引擎原生执行）。"""
    stages = []
    members = []
    si = 0
    for st in (plan.get("stages") or []):
        if not isinstance(st, dict):
            continue
        si += 1
        sid = "s%d" % si
        st_name = re.sub(r"\s+", " ", str(st.get("name") or st.get("objective") or ("阶段%d" % si)).strip())[:40]
        try:
            retries = max(0, min(2, int(st.get("retries") or 0)))
        except Exception:
            retries = 0
        stages.append({
            "id": sid,
            "name": st_name,
            "when": str(st.get("when") or "always").strip() or "always",
            "onFail": _plan_fail_to_stage(st.get("failPolicy")),
            "retries": retries,
        })
        mi = 0
        for m in (st.get("members") or []):
            if not isinstance(m, dict):
                continue
            eng = str(m.get("engine") or m.get("agentId") or "").strip()
            if not eng:
                continue
            mi += 1
            role = str(m.get("role") or "implementer").strip()[:24] or "implementer"
            name = re.sub(r"\s+", " ", str(m.get("agentId") or role or ("m%d" % mi)).strip())[:24]
            members.append({
                "id": "m%d" % (len(members) + 1),
                "name": name,
                "role": role,
                "engine": eng,
                "permLevel": str(m.get("permLevel") or "edit").strip() or "edit",
                "stage": sid,
            })
    if not members:
        members = [{"id": "m1", "name": "执行", "role": "implementer", "engine": "planner",
                    "permLevel": "edit", "stage": "s1"}]
        if not stages:
            stages = [{"id": "s1", "name": "执行", "when": "always", "onFail": "stop", "retries": 0}]
    plan_id = str(plan.get("id") or "plan")
    psid = str((sess or {}).get("id") or "")[:20]
    wf_id = "planner_%s_%s" % (psid or "s", plan_id[:24])
    wf_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", wf_id)[:64]
    return {
        "id": wf_id,
        "name": ("策划·" + str(plan.get("title") or plan.get("goal") or "计划")[:30]).strip(),
        "cwd": str((sess or {}).get("cwd") or (plan.get("workspace") or {}).get("cwd") or "").strip(),
        "taskTemplate": _plan_task_text(plan) or str(plan.get("goal") or "").strip(),
        "stages": stages,
        "members": members,
    }

def start_run(sid: str, note: str = "") -> dict:
    if orchestrator is None:
        raise ValueError("orchestrator 不可用")
    with _LOCK:
        d = _load_sessions()
        sess = _find_session(d, sid)
        if not sess:
            raise ValueError("策划会话不存在")
        if str(sess.get("status") or "") == "shadow":
            raise ValueError("影子会话不可直接启动执行；请在主策划会话执行，或先切换主控")
        if str(sess.get("activeRunId") or ""):
            raise ValueError("当前已有运行中的任务，请先等待或取消")
        sess = dict(sess)
    plan = load_plan(sid)
    if not plan:
        raise ValueError("还没有计划")
    vr = validate_plan(plan, sess)
    if not vr.get("ok"):
        raise ValueError("计划校验未通过：" + "；".join(vr.get("errors") or []))
    if not vr.get("canRun"):
        raise ValueError("计划未满足运行条件：" + "；".join(vr.get("warnings") or ["请补全 engine/agentId"]))
    plan = vr.get("plan") or plan
    plan["status"] = "running"
    save_plan(sid, plan)

    members, stages_meta = _plan_members_to_workflow(plan)
    if not members:
        plan["status"] = "draft"
        save_plan(sid, plan)
        raise ValueError("计划中没有可运行成员（请为成员指定 engine/agentId）")
    task = _plan_task_text(plan) or str(plan.get("goal") or plan.get("title") or "执行计划").strip()
    if note:
        task = task + "\n\n补充说明：" + str(note).strip()
    workflow_id = ""
    try:
        wf_data = _plan_to_workflow_data(plan, sess)
        if hasattr(orchestrator, "save_workflow"):
            saved_wf = orchestrator.save_workflow(wf_data)
            workflow_id = str((saved_wf or {}).get("id") or wf_data.get("id") or "")
        # 原生阶段语义（串行/when/onFail/retries）走 workflow_id；失败则回退平铺 members
        if workflow_id and hasattr(orchestrator, "start_run"):
            run = orchestrator.start_run(task=task, cwd=str(plan.get("cwd") or sess.get("cwd") or ""),
                                          members=None, title=str(plan.get("title") or "计划执行"),
                                          workflow_id=workflow_id)
        else:
            run = orchestrator.start_run(task=task, cwd=str(plan.get("cwd") or sess.get("cwd") or ""),
                                          members=members, title=str(plan.get("title") or "计划执行"))
    except Exception as e:
        plan["status"] = "confirmed" if plan.get("status") != "draft" else "draft"
        save_plan(sid, plan)
        append_event(sid, {"type": "run.start_failed", "error": str(e)})
        raise
    rid = str(run.get("id") or "")
    # attach planner metadata to run (best-effort; orchestrator owns run storage)
    try:
        run["plannerSessionId"] = sid
        run["planId"] = plan.get("id")
        run["planVersion"] = plan.get("version")
        run["planStages"] = stages_meta
        run["plannerWorkflowId"] = workflow_id
        if hasattr(orchestrator, "_save_run"):
            orchestrator._save_run(run)  # type: ignore[attr-defined]
        elif hasattr(orchestrator, "_save"):
            orchestrator._save(run)  # type: ignore[attr-defined]
    except Exception:
        pass
    _touch_session(sid, status="running", activeRunId=rid, lastRunId=rid, plannerWorkflowId=workflow_id)
    append_event(sid, {"type": "run.started", "runId": rid, "planVersion": plan.get("version"), "workflowId": workflow_id})
    try:
        if audit:
            audit.append("planner.run.start", "planner", sid=sid, title=plan.get("title"),
                         detail={"runId": rid, "planId": plan.get("id"), "version": plan.get("version"),
                                 "cwd": plan.get("cwd"), "members": len(members), "workflowId": workflow_id})
    except Exception:
        pass
    return {"ok": True, "session": get_session(sid), "runId": rid, "run": run, "plan": plan, "workflowId": workflow_id}

def status(sid: str) -> dict:
    sess = get_session(sid)
    if not sess:
        raise ValueError("策划会话不存在")
    plan = load_plan(sid)
    vr = validate_plan(plan, sess) if plan else None
    run = None
    rid = str(sess.get("activeRunId") or sess.get("lastRunId") or "")
    if rid and orchestrator and hasattr(orchestrator, "get_run"):
        try:
            run = orchestrator.get_run(rid)
        except Exception:
            run = None
    # reconcile terminal run state
    if run and isinstance(run, dict):
        st = str(run.get("status") or "")
        if st in ("done", "failed", "canceled") and sess.get("activeRunId") == rid:
            plan_status = "done" if st == "done" else ("failed" if st == "failed" else "canceled")
            if plan:
                plan["status"] = plan_status
                save_plan(sid, plan)
            sess = _touch_session(sid, status=plan_status, activeRunId="") or sess
    return {"session": sess, "plan": plan, "validation": vr, "run": run}


def report(sid: str) -> dict:
    st = status(sid)
    plan = st.get("plan") or {}
    run = st.get("run") or {}
    members = run.get("members") or [] if isinstance(run, dict) else []
    done = [m for m in members if isinstance(m, dict) and str(m.get("status")) == "done"]
    failed = [m for m in members if isinstance(m, dict) and str(m.get("status")) in ("failed", "error", "canceled")]
    changed_files = []
    try:
        for m in members:
            if not isinstance(m, dict):
                continue
            for ch in (m.get("changes") or []):
                if isinstance(ch, dict) and ch.get("path"):
                    changed_files.append(ch.get("path"))
    except Exception:
        pass
    changed_files = list(dict.fromkeys(changed_files))  # stable unique
    failures = []
    for m in failed:
        failures.append({
            "member": m.get("id") or m.get("name"),
            "stageId": m.get("stageId"),
            "status": m.get("status"),
            "error": (m.get("error") or m.get("failReason") or "")[:500],
        })
    risks = plan.get("risks") or []
    body = {
        "sessionId": sid,
        "planId": plan.get("id"),
        "planVersion": plan.get("version"),
        "title": plan.get("title"),
        "goal": plan.get("goal"),
        "runId": run.get("id") if isinstance(run, dict) else "",
        "runStatus": run.get("status") if isinstance(run, dict) else "",
        "summary": {
            "membersTotal": len(members),
            "membersDone": len(done),
            "membersFailed": len(failed),
            "changedFiles": len(changed_files),
        },
        "completed": [{"member": m.get("id") or m.get("name"), "stageId": m.get("stageId"),
                       "output": str(m.get("output") or m.get("result") or "")[:2000]} for m in done],
        "failures": failures,
        "changedFiles": changed_files[:500],
        "risks": risks,
        "nextActions": _next_actions(st, failures, changed_files),
        "generatedAt": _now_ts(),
    }
    body["markdown"] = report_markdown(body)
    return body


def _next_actions(st: dict, failures: list, changed_files: list) -> list:
    out = []
    run = st.get("run") or {}
    rstatus = str((run or {}).get("status") or "")
    sess = st.get("session") or {}
    plan = st.get("plan") or {}
    retry_count = int((plan.get("meta") or {}).get("retryCount") or 0) if isinstance(plan.get("meta"), dict) else 0
    if failures:
        if retry_count < 2:
            out.append("失败可重试/重规划（最多 2 次）：修正约束或成员 prompt 后重新确认并执行；当前重试计数=%d" % retry_count)
        else:
            out.append("已达到重试上限（2 次）：请人工审查失败原因并重规划（不要自动循环）")
        out.append("查看失败成员的错误原因；若是权限/隔离问题，先在会话内处理审批或应用隔离")
    if changed_files:
        out.append("在画布/文件查看中检查改动文件，确认后再应用隔离区改动或提交")
    if rstatus == "done" and not failures:
        out.append("运行完成：检查结果并决定是否应用隔离区改动/归档本次计划")
    if not out:
        if str((sess or {}).get("status") or "") == "draft" or not plan:
            out.append("补全计划中的 engine/agentId，审查变更后校验并确认，然后开始执行")
        else:
            out.append("计划就绪：审查变更后开始执行")
    return out


def report_markdown(rep: dict) -> str:
    lines = []
    lines.append("# 完成报告：%s" % (rep.get("title") or rep.get("planId") or ""))
    lines.append("")
    lines.append("- 会话: `%s`" % rep.get("sessionId", ""))
    if rep.get("runId"):
        lines.append("- 运行: `%s`（%s）" % (rep.get("runId"), rep.get("runStatus") or ""))
    if rep.get("goal"):
        lines.append("- 目标: %s" % rep["goal"])
    s = rep.get("summary") or {}
    lines.append("")
    lines.append("## 摘要")
    lines.append("")
    lines.append("- 成员: %s 完成 / %s 失败 / %s 总数" % (s.get("membersDone", 0), s.get("membersFailed", 0), s.get("membersTotal", 0)))
    lines.append("- 改动文件: %s" % s.get("changedFiles", 0))
    lines.append("")
    if rep.get("failures"):
        lines.append("## 失败与阻塞")
        lines.append("")
        for f in rep["failures"]:
            lines.append("- **%s** (%s): %s" % (f.get("member"), f.get("status"), (f.get("error") or "无错误信息")[:300]))
        lines.append("")
    if rep.get("changedFiles"):
        lines.append("## 改动文件")
        lines.append("")
        for p in (rep.get("changedFiles") or [])[:120]:
            lines.append("- `%s`" % p)
        extra = len(rep.get("changedFiles") or []) - 120
        if extra > 0:
            lines.append("- … 另有 %d 个文件" % extra)
        lines.append("")
    if rep.get("nextActions"):
        lines.append("## 下一步")
        lines.append("")
        for a in rep["nextActions"]:
            lines.append("- %s" % a)
        lines.append("")
    return "\n".join(lines)


def cancel_run(sid: str) -> dict:
    if orchestrator is None:
        raise ValueError("orchestrator 不可用")
    sess = get_session(sid)
    if not sess:
        raise ValueError("策划会话不存在")
    rid = str(sess.get("activeRunId") or "")
    if not rid:
        raise ValueError("当前没有运行中的任务")
    try:
        res = orchestrator.cancel(rid)
    except Exception as e:
        raise ValueError("取消失败：%s" % e)
    plan = load_plan(sid)
    if plan:
        plan["status"] = "canceled"
        save_plan(sid, plan)
    _touch_session(sid, status="canceled", activeRunId="")
    append_event(sid, {"type": "run.canceled", "runId": rid})
    return {"ok": True, "runId": rid, "result": res, "session": get_session(sid)}


def public_session(sess: dict | None) -> dict | None:
    if not sess:
        return None
    out = dict(sess)
    out.pop("token", None)
    return out


# ---------------- P0.5 Handoff（接力包） ----------------

def _handoff_path(sid: str) -> str:
    return os.path.join(_sid_dir(sid), "HANDOFF.md")


def _read_events_tail(sid: str, limit: int = 40) -> list:
    p = _events_path(sid)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except Exception:
        return []
    out = []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"raw": line[:300]})
    return out


def _catalog_review(plan: dict, new_agent: str = "") -> dict:
    """能力复核（P0.5）：检查 plan 里引用的 engine/agent 当前是否可用。"""
    findings = []
    warnings = []
    agent_ids = _agent_ids()
    known = set()
    try:
        if orchestrator and hasattr(orchestrator, "catalog"):
            cat = orchestrator.catalog() or {}
            for a in (cat.get("agents") or []):
                if isinstance(a, dict) and a.get("id"):
                    known.add(str(a["id"]))
                    agent_ids.add(str(a["id"]))
            if new_agent and known and new_agent not in known:
                warnings.append("目标策划 agent `%s` 不在当前 catalog 中（可能仍可用，请确认）" % new_agent)
            if new_agent and known and new_agent in known:
                for a in (cat.get("agents") or []):
                    if isinstance(a, dict) and str(a.get("id")) == new_agent and not a.get("available", True):
                        warnings.append("目标策划 agent `%s` 当前不可用：%s" % (new_agent, ", ".join(a.get("missing") or []) or "unknown"))
    except Exception:
        pass
    for st in (plan.get("stages") or []):
        if not isinstance(st, dict):
            continue
        for m in (st.get("members") or []):
            if not isinstance(m, dict):
                continue
            eng = str(m.get("engine") or m.get("agentId") or "").strip()
            if eng and agent_ids and eng not in agent_ids:
                findings.append({"level": "warn", "stage": st.get("id"), "member": m.get("id"),
                                 "msg": "agent `%s` 当前不可用或未知；换策划 agent 后请复核计划/改引擎" % eng})
    return {"ok": not any(f.get("level") == "error" for f in findings), "findings": findings, "warnings": warnings,
            "checkedAgent": new_agent, "planVersion": plan.get("version"), "checkedAt": _now_ts()}


def render_handoff(sid: str, reason: str = "manual", note: str = "", to_agent: str = "") -> str:
    sess = get_session(sid) or {}
    plan = load_plan(sid) or {}
    vr = validate_plan(plan, sess) if plan else {"ok": False, "errors": ["无计划"], "warnings": []}
    st = None
    try:
        st = status(sid)
    except Exception:
        st = {"session": sess, "plan": plan, "run": None}
    run = (st or {}).get("run") or {}
    members = run.get("members") or [] if isinstance(run, dict) else []
    done = [m.get("name") or m.get("id") for m in members if isinstance(m, dict) and m.get("status") == "done"]
    doing = [m.get("name") or m.get("id") for m in members if isinstance(m, dict) and m.get("status") in ("running", "wait", "pending")]
    failed = []
    for m in members:
        if isinstance(m, dict) and m.get("status") in ("failed", "error", "canceled"):
            failed.append("%s: %s" % (m.get("name") or m.get("id"), str(m.get("error") or m.get("failReason") or "")[:180]))
    changed = []
    for m in members:
        if not isinstance(m, dict):
            continue
        for ch in (m.get("changes") or []):
            if isinstance(ch, dict) and ch.get("path") and ch.get("path") not in changed:
                changed.append(ch["path"])
    recent = _read_events_tail(sid, limit=15)
    constraints = plan.get("constraints") or {}
    risk_lines = []
    for r in (plan.get("risks") or [])[:8]:
        if isinstance(r, dict):
            risk_lines.append("- %s：%s" % (r.get("id", "risk"), r.get("desc") or r.get("mitigation") or ""))
        else:
            risk_lines.append("- %s" % r)
    next_steps = _next_actions(st or {}, [{"member": x} for x in failed], changed)
    now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    lines = []
    lines += [f"# 策划会话交接（HANDOFF）", "", f"- 生成时间：{now}", f"- 原因：{reason}",
              f"- 会话：`{sid}`", f"- 项目：`{sess.get('cwd') or plan.get('cwd') or ''}`",
              f"- 策划 agent：`{sess.get('plannerAgent') or ''}`" + (f" → `{to_agent}`" if to_agent else ""),
              f"- 计划：`{plan.get('id') or ''}` v{plan.get('version') or 1}（{plan.get('status') or 'draft'}）",
              f"- 会话状态：{sess.get('status') or ''}", ""]
    lines += ["## 1. 目标（一句话）", "", str(plan.get("goal") or sess.get("title") or "").strip() or "（未填写）", ""]
    lines += ["## 2. 当前状态", "", f"- 计划校验：{'通过' if vr.get('ok') else '未通过'}，可运行：{'是' if vr.get('canRun') else '否'}",
              f"- 运行：`{run.get('id') or ''}`（{run.get('status') or '无'}）" if run else "- 运行：无",
              f"- 已完成：{', '.join(map(str, done[:12])) or '无'}",
              f"- 进行中：{', '.join(map(str, doing[:12])) or '无'}",
              f"- 失败/阻塞：{'; '.join(failed[:8]) or '无'}", ""]
    lines += ["## 3. 关键决策（最近事件摘要）", ""]
    if recent:
        for ev in recent[-10:]:
            if not isinstance(ev, dict):
                continue
            lines.append("- `%s` %s" % (ev.get("type") or "event", json.dumps({k: v for k, v in ev.items() if k not in ("sessionId",)}, ensure_ascii=False)[:220]))
    else:
        lines.append("- （无事件记录）")
    lines += ["", "## 4. 未决问题", "", "- （由接棒者根据失败/校验警告补充）"]
    if vr.get("errors"):
        for e in vr["errors"][:8]:
            lines.append("- 校验错误：%s" % e)
    if vr.get("warnings"):
        for w in vr["warnings"][:8]:
            lines.append("- 校验提醒：%s" % w)
    lines += ["", "## 5. 约束与预算", "",
              f"- 预算上限：{constraints.get('budgetYuan') or 0}",
              f"- 破坏性操作需确认：{'是' if constraints.get('noDestructiveWithoutConfirm', True) else '否'}",
              f"- 网络策略：{constraints.get('network') or 'inherit'}",
              f"- 计划文件（执行真相）：`runtime/state/_planner/{sid}/plan.json`（v{plan.get('version') or 1}）",
              f"- 人读版：`runtime/state/_planner/{sid}/PLAN.md`", ""]
    lines += ["## 6. 下一步（给接棒者）", ""]
    for a in next_steps[:10]:
        lines.append("1. %s" % a if not str(a).startswith(("1.", "-", "*")) else str(a))
    if note:
        lines += ["", "## 7. 交接备注", "", str(note).strip()]
    lines += ["", "## 8. 不要做的事", "",
              "- 不要重新设计整份计划（除非计划校验失败或用户明确要求）；先读 `plan.json`。",
              "- 不要给自己或成员批权限；不要绕过破坏性操作确认。",
              "- 不要猜测已完成内容；以运行记录/改动文件/`plan.json` 为准。", ""]
    if changed:
        lines += ["## 9. 已知改动文件（来自最近运行）", ""]
        for p in changed[:80]:
            lines.append("- `%s`" % p)
        lines.append("")
    if risk_lines:
        lines += ["## 10. 风险", "", *risk_lines, ""]
    return "\n".join(lines).rstrip() + "\n"


def write_handoff(sid: str, reason: str = "manual", note: str = "", to_agent: str = "") -> dict:
    with _LOCK:
        d = _load_sessions()
        sess = _find_session(d, sid)
        if not sess:
            raise ValueError("策划会话不存在")
    text = render_handoff(sid, reason=reason, note=note, to_agent=to_agent)
    path = _handoff_path(sid)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    append_event(sid, {"type": "handoff.written", "reason": reason, "toAgent": to_agent, "path": path})
    _touch_session(sid, handoffAt=_now_ts(), handoffReason=reason)
    return {"ok": True, "sessionId": sid, "path": path, "markdown": text, "reason": reason}


def load_handoff(sid: str) -> dict:
    p = _handoff_path(sid)
    text = ""
    try:
        with open(p, "r", encoding="utf-8") as fh:
            text = fh.read()
    except Exception:
        text = ""
    return {"ok": bool(text), "sessionId": sid, "path": p, "markdown": text, "exists": bool(text)}


def handoff_context(sid: str, max_chars: int = 12000) -> dict:
    """给新会话/新 agent 注入的接力上下文（分层思想的 P0.5 实现：先 HANDOFF，再 plan 摘要）。"""
    h = load_handoff(sid)
    plan = load_plan(sid) or {}
    parts = []
    header = (
        "你正在接手一个既有策划会话。请先阅读交接包和 plan.json 摘要；"
        "不要重新设计整份计划，除非校验失败或用户明确要求。plan.json 是唯一可执行真相。"
    )
    parts.append(header)
    if h.get("markdown"):
        parts.append("\n---\n## HANDOFF.md\n\n" + h["markdown"].strip())
    if plan:
        summ = {
            "id": plan.get("id"), "version": plan.get("version"), "status": plan.get("status"),
            "cwd": plan.get("cwd"), "goal": plan.get("goal"),
            "stages": [{"id": s.get("id"), "name": s.get("name"),
                        "members": [{"id": m.get("id"), "engine": m.get("engine") or m.get("agentId"), "role": m.get("role"), "permLevel": m.get("permLevel")}
                                    for m in (s.get("members") or []) if isinstance(m, dict)]}
                       for s in (plan.get("stages") or []) if isinstance(s, dict)],
            "constraints": plan.get("constraints") or {},
        }
        parts.append("\n---\n## plan.json 摘要\n\n```json\n" + json.dumps(summ, ensure_ascii=False, indent=2) + "\n```")
    text = "\n".join(parts)
    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n[...接力上下文已截断，请按需读取 HANDOFF.md / plan.json ...]"
        truncated = True
    return {"ok": True, "sessionId": sid, "text": text, "truncated": truncated, "hasHandoff": bool(h.get("exists"))}


def switch_planner_agent(sid: str, new_agent: str, note: str = "") -> dict:
    new_agent = str(new_agent or "").strip()
    if not new_agent:
        raise ValueError("new_agent 不能为空")
    with _LOCK:
        d = _load_sessions()
        sess = _find_session(d, sid)
        if not sess:
            raise ValueError("策划会话不存在")
        old = str(sess.get("plannerAgent") or "")
        sess = dict(sess)
    # 1) write handoff BEFORE switching (per §6)
    h = write_handoff(sid, reason="switch-agent", note=note, to_agent=new_agent)
    # 2) capability review
    plan = load_plan(sid) or default_plan(sess)
    review = _catalog_review(plan, new_agent=new_agent)
    # 3) switch
    _touch_session(sid, plannerAgent=new_agent, plannerAgentSwitchedAt=_now_ts(), previousPlannerAgent=old)
    append_event(sid, {"type": "planner.agent_switched", "from": old, "to": new_agent})
    ctx = handoff_context(sid)
    return {"ok": True, "session": get_session(sid), "fromAgent": old, "toAgent": new_agent,
            "handoff": {"path": h.get("path"), "written": True}, "review": review, "context": ctx}


def maybe_auto_handoff(sid: str, used: float, size: float, threshold: float = 0.8, note: str = "") -> dict:
    """P0.5：上下文用量达到阈值时写交接包（幂等：同一 plan 版本+running 窗口内不重复刷屏）。
    返回 {needed, written, ratio, handoff?, action}。调用方可据此“新开会话并注入 handoff_context”。
    """
    try:
        used_n = float(used or 0)
        size_n = float(size or 0)
    except Exception:
        return {"ok": False, "needed": False, "written": False, "error": "invalid usage"}
    ratio = (used_n / size_n) if size_n > 0 else 0.0
    if ratio < float(threshold or 0.8):
        return {"ok": True, "needed": False, "written": False, "ratio": round(ratio, 4)}
    plan = load_plan(sid) or {}
    ver = plan.get("version")
    marker = os.path.join(_sid_dir(sid), "_handoff_auto_v%s" % ver)
    if os.path.exists(marker):
        h = load_handoff(sid)
        return {"ok": True, "needed": True, "written": False, "ratio": round(ratio, 4),
                "deduped": True, "handoff": {"path": h.get("path"), "exists": h.get("exists")},
                "action": "inject_context_or_new_session"}
    h = write_handoff(sid, reason="context-full", note=note or ("context %.0f%%" % (ratio * 100)))
    try:
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(str(_now_ts()))
    except Exception:
        pass
    append_event(sid, {"type": "handoff.auto", "ratio": round(ratio, 4), "planVersion": ver})
    return {"ok": True, "needed": True, "written": True, "ratio": round(ratio, 4),
            "handoff": {"path": h.get("path")}, "action": "inject_context_or_new_session",
            "context": handoff_context(sid)}


def recover(sid: str | None = None) -> dict:
    """崩溃/重启恢复：对 activeRunId 做 reconcile；孤儿 running 标记为 interrupted。"""
    fixed = []
    with _LOCK:
        d = _load_sessions()
        items = d.get("sessions") or []
        targets = [s for s in items if isinstance(s, dict) and (not sid or str(s.get("id")) == str(sid))]
    for s in targets:
        ssid = str(s.get("id") or "")
        rid = str(s.get("activeRunId") or "")
        if not rid:
            continue
        run = None
        if orchestrator and hasattr(orchestrator, "get_run"):
            try:
                run = orchestrator.get_run(rid)
            except Exception:
                run = None
        st = str((run or {}).get("status") or "")
        if st in ("done", "failed", "canceled"):
            plan = load_plan(ssid)
            if plan and st in ("done", "failed", "canceled"):
                plan["status"] = "done" if st == "done" else ("failed" if st == "failed" else "canceled")
                save_plan(ssid, plan)
            _touch_session(ssid, status=plan.get("status") if plan else st, activeRunId="")
            fixed.append({"sessionId": ssid, "runId": rid, "runStatus": st, "action": "reconciled"})
        elif not run or st == "":
            # run missing after restart -> mark interrupted, keep evidence pointers
            _touch_session(ssid, status="failed", activeRunId="", lastError="运行记录丢失或面板重启后中断，请查看最近 run/HANDOFF")
            plan = load_plan(ssid)
            if plan and plan.get("status") == "running":
                plan["status"] = "failed"
                save_plan(ssid, plan)
            write_handoff(ssid, reason="crash-recovery", note="面板重启/运行记录不可用，已标记中断；请根据 plan.json 与最近 run 决定重试")
            append_event(ssid, {"type": "recover.interrupted", "runId": rid})
            fixed.append({"sessionId": ssid, "runId": rid, "runStatus": "missing", "action": "interrupted"})
    return {"ok": True, "fixed": fixed, "count": len(fixed)}


def plan_diff(sid: str, new_plan) -> dict:
    """P1：计划变更对比（人审用）。不落盘；返回结构化差异与摘要。"""
    sess = get_session(sid)
    if not sess:
        raise ValueError("策划会话不存在")
    old = load_plan(sid) or {}
    if not isinstance(new_plan, dict):
        raise ValueError("plan 必须是对象")
    try:
        cand = _normalize_plan(sid, new_plan, cwd_fallback=sess.get("cwd") or "")
        cand_vr = validate_plan(cand, sess)
        cand = cand_vr.get("plan") or cand
    except Exception:
        cand = new_plan
        cand_vr = {"ok": False, "canRun": False, "errors": ["normalize failed"], "warnings": []}

    def _fp(p):
        if not isinstance(p, dict): return {}
        def ms(m):
            if not isinstance(m, dict): return {}
            return {k: m.get(k) for k in ("id","role","agentId","engine","prompt","permLevel")}
        def ss(s):
            if not isinstance(s, dict): return {}
            d = {k: s.get(k) for k in ("id","name","objective","doneWhen","when","failPolicy","retries")}
            d["members"] = [ms(m) for m in (s.get("members") or []) if isinstance(m, dict)]
            return d
        return {"title": p.get("title"), "goal": p.get("goal"),
                "cwd": p.get("cwd") or (p.get("workspace") or {}).get("cwd"),
                "stages": [ss(s) for s in (p.get("stages") or []) if isinstance(s, dict)],
                "constraints": p.get("constraints") or [], "risks": p.get("risks") or []}

    ofp, nfp = _fp(old), _fp(cand)
    changes = []
    for k, label in (("title","标题"),("goal","目标"),("cwd","工作目录")):
        if ofp.get(k) != nfp.get(k):
            changes.append({"field": k, "label": label, "from": ofp.get(k), "to": nfp.get(k), "kind": "field"})
    if ofp.get("constraints") != nfp.get("constraints"):
        changes.append({"field":"constraints","label":"约束","kind":"list","fromCount":len(ofp.get("constraints") or []),"toCount":len(nfp.get("constraints") or [])})
    os_, ns_ = ofp.get("stages") or [], nfp.get("stages") or []
    if len(os_) != len(ns_):
        changes.append({"field":"stages","label":"阶段数量","from":len(os_),"to":len(ns_),"kind":"structure"})
    oidx = {str(s.get("id")): s for s in os_}; nidx = {str(s.get("id")): s for s in ns_}
    for sid_ in nidx:
        if sid_ not in oidx:
            changes.append({"field":"stage","label":"新增阶段","stageId":sid_,"kind":"add","name":(nidx[sid_] or {}).get("name")})
    for sid_ in oidx:
        if sid_ not in nidx:
            changes.append({"field":"stage","label":"删除阶段","stageId":sid_,"kind":"remove","name":(oidx[sid_] or {}).get("name")})
    for sid_ in nidx:
        if sid_ in oidx and oidx[sid_] != nidx.get(sid_):
            changes.append({"field":"stage","label":"阶段变更","stageId":sid_,"kind":"change","name":(nidx.get(sid_) or {}).get("name")})
    return {"ok": True, "hasChanges": bool(changes) or (ofp != nfp),
            "changeCount": len(changes), "changes": changes[:80],
            "summary": ("有 %d 处变更" % len(changes)) if changes else ("内容有变化" if ofp != nfp else "无变化"),
            "validation": {"ok": cand_vr.get("ok"), "canRun": cand_vr.get("canRun"),
                           "errors": cand_vr.get("errors") or [], "warnings": cand_vr.get("warnings") or []},
            "oldVersion": old.get("version")}


# ---------------- Retry / replan (P1) / isolation linkage ----------------

def run_retry(sid: str, mode: str = "retry", note: str = "") -> dict:
    """失败重试/重规划（最多 2 次）。mode=retry: 计数+1，允许在计划仍可执行时再次启动；mode=replan: 计数+1，计划回 draft 需重审。"""
    if orchestrator is None:
        raise ValueError("orchestrator 不可用")
    mode = str(mode or "retry").strip().lower()
    if mode not in ("retry", "replan", "retry_stage"):
        mode = "retry"
    with _LOCK:
        d = _load_sessions()
        sess = _find_session(d, sid)
        if not sess:
            raise ValueError("策划会话不存在")
        sess = dict(sess)
    if str(sess.get("activeRunId") or ""):
        raise ValueError("当前仍有运行中的任务，请先取消或等待结束")
    plan = load_plan(sid)
    if not plan:
        raise ValueError("还没有计划")
    meta = plan.get("meta") if isinstance(plan.get("meta"), dict) else {}
    rc = int(meta.get("retryCount") or 0)
    if rc >= 2:
        raise ValueError("已达到重试/重规划上限（2 次），请人工审查后新建计划版本")
    meta = dict(meta)
    meta["retryCount"] = rc + 1
    meta["lastRetryMode"] = mode
    meta["lastRetryAt"] = _now_ts()
    if note:
        meta["lastRetryNote"] = str(note)[:500]
    plan["meta"] = meta
    plan["updatedAt"] = _now_ts()
    if mode == "replan":
        plan["status"] = "draft"
        # bump a planning revision marker (keeps id/version semantics clear for human review)
        meta["replanRevision"] = int(meta.get("replanRevision") or 0) + 1
        plan["meta"] = meta
        save_plan(sid, plan)
        _touch_session(sid, status="idle", activeRunId="")
        append_event(sid, {"type": "plan.replan", "retryCount": meta["retryCount"], "note": note})
        return {"ok": True, "mode": mode, "plan": plan, "session": get_session(sid),
                "message": "已进入重规划：请修改计划、审查变更、校验并确认后再执行（retryCount=%d/2）" % meta["retryCount"]}
    # retry: keep plan, require it to be runnable; start a new run
    save_plan(sid, plan)
    append_event(sid, {"type": "run.retry", "retryCount": meta["retryCount"], "note": note})
    res = start_run(sid, note=("重试#%d %s" % (meta["retryCount"], note)).strip())
    res["mode"] = mode
    res["message"] = "已按同一计划发起第 %d 次重试" % meta["retryCount"]
    return res


def _planner_run(sid: str):
    sess = get_session(sid)
    if not sess:
        raise ValueError("策划会话不存在")
    rid = str(sess.get("activeRunId") or sess.get("lastRunId") or "")
    if not rid or not orchestrator or not hasattr(orchestrator, "get_run"):
        return sess, None, ""
    try:
        return sess, orchestrator.get_run(rid), rid
    except Exception:
        return sess, None, rid


def isolation_list(sid: str) -> dict:
    sess, run, rid = _planner_run(sid)
    if not rid:
        return {"ok": True, "runId": "", "items": [], "session": sess}
    # prefer orchestrator API; fallback to isolation module
    try:
        if hasattr(orchestrator, "get_run"):
            r = orchestrator.get_run(rid) or {}
            iso = r.get("isolation") or {}
            members = []
            for m in (r.get("members") or []):
                if isinstance(m, dict):
                    mi = (m.get("isolation") or {}) if isinstance(m.get("isolation"), dict) else {}
                    members.append({"memberId": m.get("id"), "name": m.get("name"), "status": m.get("status"), "isolation": mi})
            return {"ok": True, "runId": rid, "isolation": iso, "members": members, "session": sess}
    except Exception:
        pass
    return {"ok": True, "runId": rid, "items": [], "session": sess}


def isolation_apply(sid: str, member_id: str = "", all_members: bool = False, force: bool = False) -> dict:
    sess, run, rid = _planner_run(sid)
    if not rid:
        raise ValueError("没有可应用隔离区的运行记录")
    if all_members:
        results = []
        for m in ((run or {}).get("members") or []):
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id") or "")
            if not mid:
                continue
            try:
                if hasattr(orchestrator, "apply_member_isolation"):
                    results.append({"memberId": mid, "result": orchestrator.apply_member_isolation(rid, mid, force=bool(force))})
                else:
                    results.append({"memberId": mid, "error": "orchestrator.apply_member_isolation unavailable"})
            except Exception as e:
                results.append({"memberId": mid, "error": str(e)})
        append_event(sid, {"type": "isolation.apply_all", "runId": rid, "results": len(results)})
        return {"ok": True, "runId": rid, "results": results}
    if not member_id:
        raise ValueError("缺少 memberId（或 all=true）")
    if hasattr(orchestrator, "apply_member_isolation"):
        res = orchestrator.apply_member_isolation(rid, member_id, force=bool(force))
    else:
        raise ValueError("orchestrator.apply_member_isolation 不可用")
    append_event(sid, {"type": "isolation.apply", "runId": rid, "memberId": member_id})
    return {"ok": True, "runId": rid, "memberId": member_id, "result": res}


def isolation_cleanup(sid: str) -> dict:
    sess, run, rid = _planner_run(sid)
    if not rid:
        raise ValueError("没有可清理的运行记录")
    if hasattr(orchestrator, "cleanup_isolation"):
        res = orchestrator.cleanup_isolation(rid)
    else:
        raise ValueError("orchestrator.cleanup_isolation 不可用")
    append_event(sid, {"type": "isolation.cleanup", "runId": rid})
    return {"ok": True, "runId": rid, "result": res}


def approvals(sid: str) -> dict:
    """会话内批权限联动：返回该策划会话当前 run 相关的待审批（来自 orchestrator/全局 approvals，按 runId/workflowId 过滤）。"""
    sess, run, rid = _planner_run(sid)
    items = []
    try:
        # try common locations
        for fn in (os.path.join(STATE_DIR, "_approvals.json"), os.path.join(BASE_DIR, "runtime", "state", "_approvals.json")):
            data = _read_json(fn, None)
            if isinstance(data, list):
                src = data
            elif isinstance(data, dict):
                src = data.get("items") or data.get("approvals") or data.get("pending") or []
            else:
                src = []
            for a in (src if isinstance(src, list) else []):
                if not isinstance(a, dict):
                    continue
                ar = str(a.get("runId") or a.get("run") or (a.get("meta") or {}).get("runId") or "")
                aw = str(a.get("workflowId") or (a.get("meta") or {}).get("workflowId") or "")
                if rid and ar == rid:
                    items.append(a)
                elif (run or {}).get("workflowId") and aw == str((run or {}).get("workflowId")):
                    items.append(a)
            if items:
                break
    except Exception:
        pass
    return {"ok": True, "session": sess, "runId": rid, "approvals": items,
            "hint": "在协作/画布或托盘中处理审批；本接口提供 run 关联的待审批快照。"}


# ---------------- P2: multi plan/workflow variants (same planner session) ----------------

def _plan_path_key(sid: str, key: str) -> str:
    key = re.sub(r'[^a-zA-Z0-9_-]+', '', str(key or 'main'))[:32] or 'main'
    return _plan_path(sid) if key == 'main' else os.path.join(_sid_dir(sid), 'plan.%s.json' % key)

def list_plan_variants(sid: str) -> dict:
    sess = get_session(sid)
    if not sess: raise ValueError('策划会话不存在')
    variants = sess.get('planVariants') if isinstance(sess.get('planVariants'), list) else []
    if not variants:
        variants = [{"key":"main","name":"主计划","path":"plan.json","active":True}]
    active = str(sess.get('activePlanKey') or 'main')
    for v in variants:
        if isinstance(v, dict):
            v['exists'] = os.path.exists(_plan_path_key(sid, v.get('key') or 'main'))
            v['active'] = (str(v.get('key') or '') == active)
    return {"ok": True, "sessionId": sid, "activePlanKey": active, "variants": variants}

def create_plan_variant(sid: str, key: str, name: str = '', from_key: str = '') -> dict:
    sess = get_session(sid)
    if not sess: raise ValueError('策划会话不存在')
    key = re.sub(r'[^a-zA-Z0-9_-]+', '', str(key or ''))[:32]
    if not key or key == 'main': raise ValueError('请提供有效的 variant key（不能是 main）')
    with _LOCK:
        d = _load_sessions(); s = _find_session(d, sid)
        if not s: raise ValueError('策划会话不存在')
        vs = s.get('planVariants') if isinstance(s.get('planVariants'), list) else []
        if any(isinstance(v,dict) and str(v.get('key'))==key for v in vs): raise ValueError('该 plan variant 已存在')
        src = _plan_path_key(sid, from_key or str(s.get('activePlanKey') or 'main'))
        dst = _plan_path_key(sid, key)
        plan = _read_json(src, None) or default_plan(dict(s))
        plan['id'] = str(plan.get('id') or 'plan') + '_' + key
        plan['variantKey'] = key
        _atomic_write(dst, plan)
        vs = list(vs) if vs else [{"key":"main","name":"主计划","path":"plan.json","active":(str(s.get('activePlanKey') or 'main')=='main')}]
        vs.append({"key":key,"name":str(name or key)[:40],"path":os.path.basename(dst),"active":False,"updatedAt":_now_ts()})
        s['planVariants'] = vs; s['updatedAt'] = _now_ts()
        _save_sessions(d)
    append_event(sid, {"type":"plan.variant_created","key":key,"from":from_key})
    return list_plan_variants(sid)

def switch_plan_variant(sid: str, key: str) -> dict:
    sess = get_session(sid)
    if not sess: raise ValueError('策划会话不存在')
    key = str(key or 'main')
    with _LOCK:
        d = _load_sessions(); s = _find_session(d, sid)
        if not s: raise ValueError('策划会话不存在')
        vs = s.get('planVariants') if isinstance(s.get('planVariants'), list) else []
        if key != 'main' and not any(isinstance(v,dict) and str(v.get('key'))==key for v in vs) and not os.path.exists(_plan_path_key(sid,key)):
            raise ValueError('plan variant 不存在')
        s['activePlanKey'] = key
        for v in (vs or []):
            if isinstance(v,dict): v['active'] = (str(v.get('key'))==key)
        s['planVariants'] = vs; s['updatedAt'] = _now_ts()
        _save_sessions(d)
    append_event(sid, {"type":"plan.variant_switched","key":key})
    return list_plan_variants(sid)

