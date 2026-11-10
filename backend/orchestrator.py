# -*- coding: utf-8 -*-
r"""★本地协作编排（不依赖群聊 / 云端账号）—— 第一刀：Runner。

设计目标（见 docs/本地协作-方案.md）：
  * **本机**把一条任务同时派给多个 agent（各自独立 AcpService + 独立会话），收集结果；
  * **不经过 cocraft / WS / 登录**，数据只落 `runtime/state/_orchestrator/`；
  * **权限由工作流自己持有**（`members[].permLevel`，不再跟随面板）；命中自动放行，否则进
    **任务级待批队列**等用户逐条批准（fail-closed；破坏性 / 敏感读取永远要确认）；
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
import audit
import errkind
import isolation
from cocraft_agents import (ROLES, allowed_by_level, allow_network, _is_destructive,
                            _is_secret, _is_net, norm_role, role_line)
from engines import STATE_DIR
from engines.base import capture_before_allow
from engines.acp import agents as acp_agents
from engines.acp.engine import acp_log
from engines.acp.map_events import now_ms
from engines.acp.service import AcpService, BusyError, _load_ok_models

ROOT = os.path.join(STATE_DIR, "_orchestrator")
RUNS_DIR = os.path.join(ROOT, "runs")
WORKFLOWS_FILE = os.path.join(STATE_DIR, "_collab_workflows.json")   # ★可复用的工作流（成员/默认任务/画布布局）
KEEP_WORKFLOWS = 200
KEEP_RUNS = 60
ASK_TIMEOUT = float(os.environ.get("COCRAFT_ORCH_ASK_TIMEOUT") or 1800)   # 等批准（秒；0=不限）
RUN_TIMEOUT = float(os.environ.get("COCRAFT_ORCH_TIMEOUT") or 1800)       # 单个成员上限（秒；**不含等批准**）
APPROVALS_FILE = os.path.join(STATE_DIR, "_approvals.json")   # 托盘/热键读取的待批快照
NOTIFY_FILE = os.path.join(STATE_DIR, "_notify.json")         # 托盘气泡（与前端 B3 共用）

_LOCK = threading.RLock()
_WF_LOCK = threading.RLock()
_RUNS: dict = {}          # run_id -> run dict（内存权威；落盘用于重启后查看）
_ACTIVE: dict = {}        # (run_id, member_id) -> {"svc":..., "sid":...}
_CANCEL: dict = {}        # (run_id, member_id) -> Event
_PENDING: dict = {}       # perm_id -> {"event": Event, "decision": None, "run":..., "member":...}
_PENDING_FORMS: dict = {} # form_id -> {"event": Event, "answer": ..., "declined": ...}
_NOTIFY_STATE = {"run": "", "ts": 0.0}   # 待批气泡节流（同一 run 30s 内不重复弹）
_FORM_NOTIFY_STATE = {"run": "", "ts": 0.0}


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
    # ★删除运行记录后，还在收尾的线程可能再 _save 一次（会把删掉的文件写回来）
    if run.get("_deleted"):
        return
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
             "kind", "sessionID", "cancelled", "files", "pick", "permLevel",
             "stage", "retryCount", "changes", "continuedFrom", "resumed",
             "isolation", "applied")}


def public_run(run: dict) -> dict:
    if not run:
        return {}
    return {"id": run.get("id"), "task": run.get("task"), "cwd": run.get("cwd"),
            "title": run.get("title"), "status": run.get("status"),
            "workflowId": run.get("workflowId") or "",
            "workflowName": run.get("workflowName") or "",
            "created": run.get("created"), "updated": run.get("updated"),
            "continuedFrom": run.get("continuedFrom") or "",
            "exploration": bool(run.get("exploration")),
            "resumedMembers": int(run.get("resumedMembers") or 0),
            # ★并行隔离（P0-1）：模式 / 原因 / 是否已清理（前端据此显示"独立工作区"徽章）
            "isolation": dict(run.get("isolation") or {}),
            # ★阶段快照（P4）：状态 / 条件 / 失败策略 / 重试都在里面
            "stages": [dict(s) for s in (run.get("stages") or []) if isinstance(s, dict)],
            "members": [_member_public(m) for m in (run.get("members") or [])],
            # 全部申请（pending + 已结算）都回；「谁批准了什么」要能追溯
            "permissions": [p for p in (run.get("permissions") or [])],
            # 工作流里的 agent 追问（pending + 已结算）
            "forms": [f for f in (run.get("forms") or [])],
            "arbitration": run.get("arbitration"),
            # ★上游账户问题（余额不足 / 凭据无效）：前端在任务头显示一行红字
            "failReason": run.get("failReason") or "",
            "error": run.get("error") or ""}


def _reconcile_stale_run(run: dict) -> bool:
    """把「已经没有执行线程、却还停在 running」的 run 收尾。

    典型场景：任务跑到一半，面板服务被重启（`_ACTIVE`/线程全没了），但 run 文件里
    成员还是 `running` —— 之后点「取消全部」也只会改 run.status，成员永远转圈。
    **只处理超过 30 秒没更新、且没有 `_ACTIVE` 的 run**，避免误伤刚启动的。
    """
    try:
        if not run:
            return False
        rid = str(run.get("id") or "")
        if any(str(k[0]) == rid for k in _ACTIVE):
            return False
        updated = float(run.get("updated") or 0)
        if updated and (_now() - updated) < 30000:
            return False
        members = run.get("members") or []
        stages = run.get("stages") or []
        live_m = [m for m in members if m.get("status") in ("running", "wait")]
        live_s = [s for s in stages if s.get("status") in ("running", "wait")]
        if not live_m and not live_s and str(run.get("status") or "") != "running":
            return False
        cancelled = str(run.get("status") or "") == "cancelled"
        now = _now()
        for m in live_m:
            m["status"] = "cancelled" if cancelled else "failed"
            m["error"] = "任务已取消" if cancelled else "面板服务重启，执行线程已中断"
            if cancelled:
                m["cancelled"] = True
            m["ended"] = now
        for s in live_s:
            s["status"] = "cancelled" if cancelled else "failed"
            s["note"] = "任务已取消" if cancelled else "面板服务重启，执行线程已中断"
            s["ended"] = now
        if str(run.get("status") or "") == "running":
            run["status"] = "cancelled" if any(m.get("cancelled") for m in members) else "failed"
        run["updated"] = now
        _save(run)
        acp_log("[orchestrator] 已收尾中断的运行 %s（%s）"
                % (rid, "cancelled" if cancelled else "failed"))
        return True
    except Exception:  # noqa: BLE001
        return False


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
        _reconcile_stale_run(r)
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
    if run:
        _reconcile_stale_run(run)
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
                    "permLevel": norm_perm_level(r.get("permLevel")),
                    "stage": str(r.get("stage") or "").strip()})
    return out


# ---------------- ★阶段（P4：串行阶段 / 条件 / 失败策略 / 重试） ----------------

STAGE_WHEN = ("always", "onSuccess", "onFailure")
STAGE_ONFAIL = ("stop", "continue")
MAX_STAGE_RETRIES = 2
MAX_STAGES = 8


def _stage_when(v) -> str:
    v = str(v or "").strip()
    return v if v in STAGE_WHEN else "always"


def _stage_onfail(v) -> str:
    v = str(v or "").strip()
    return v if v in STAGE_ONFAIL else "stop"


def _stage_retries(v) -> int:
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(MAX_STAGE_RETRIES, n))


def norm_stages(raw_stages, members) -> tuple:
    """把「阶段 + 成员归属」统一成 `s1..sN`（老数据 / 直传成员都兼容）。

    * 没有 stages（`0.2.14` 及更早的工作流、或直接传 members 的 start）→ **一个默认阶段**；
    * 阶段 id 重排成 `s1..sN`（顺序就是执行顺序）；
    * 成员的 `stage` 认 `s2` / `2` / 原始 id，认不出就落到第一个阶段；
    * 空阶段**保留**（画布上可能刚加还没放人），执行时直接算完成。
    """
    ms = []
    for i, m in enumerate(members or []):
        m = dict(m) if isinstance(m, dict) else {}
        m.setdefault("id", "m%d" % (i + 1))
        ms.append(m)
    raw_list = raw_stages if isinstance(raw_stages, list) else []
    stages, orig_ids = [], []
    for i, s in enumerate(raw_list[:MAX_STAGES]):
        s = s if isinstance(s, dict) else {}
        orig_ids.append(str(s.get("id") or "").strip())
        stages.append({
            "id": "s%d" % (i + 1),
            "name": (re.sub(r"\s+", " ", str(s.get("name") or "").strip())[:24]
                     or ("阶段 %d" % (i + 1))),
            "when": _stage_when(s.get("when")),
            "onFail": _stage_onfail(s.get("onFail")),
            "retries": _stage_retries(s.get("retries")),
        })
    if not stages:
        stages = [{"id": "s1", "name": "全部成员", "when": "always",
                   "onFail": "stop", "retries": 0}]
    ids = [s["id"] for s in stages]

    def pick(v):
        v = str(v or "").strip()
        if not v:
            return ids[0]
        if v in ids:
            return v
        m = re.match(r"^s?(\d+)$", v)
        if m:
            n = int(m.group(1))
            if 1 <= n <= len(ids):
                return ids[n - 1]
        if v in orig_ids:
            i = orig_ids.index(v)
            if i < len(ids):
                return ids[i]
        return ids[0]

    for m in ms:
        m["stage"] = pick(m.get("stage"))
    return stages, ms


def _wf_key(cwd) -> str:
    """项目画布的键：目录归一（大小写 / 斜杠）；没目录用 __default__。"""
    s = str(cwd or "").replace("\\", "/").rstrip("/").strip()
    return s.lower() if s else "__default__"


def _project_name(cwd) -> str:
    """项目名：目录 basename；没目录时给中性名。"""
    s = str(cwd or "").strip()
    if not s:
        return "未指定项目"
    try:
        return os.path.basename(os.path.realpath(s).rstrip("\\/")) or s
    except Exception:  # noqa: BLE001
        return s


def _member_session_title(run: dict, member: dict) -> str:
    """工作流成员会话名：`<项目名> · <结点名>`（用户 2026-11-05 指定）。"""
    return "%s · %s" % (_project_name((run or {}).get("cwd")),
                        str((member or {}).get("name") or (member or {}).get("id") or "成员"))


def _wf_canvas_from(raw) -> dict:
    """把任意 layout 归一成 {zoom, nodes}（坐标越界夹回）。"""
    raw = raw if isinstance(raw, dict) else {}
    nodes = {}
    for k, v in (raw.get("nodes") or {}).items():
        if isinstance(v, dict):
            nodes[str(k)[:24]] = _wf_pos(v)
    try:
        zoom = float(raw.get("zoom") or 1)
    except (TypeError, ValueError):
        zoom = 1.0
    return {"zoom": max(0.6, min(1.6, zoom)), "nodes": nodes}


def _project_canvas(d: dict, cwd) -> dict:
    """项目的**唯一画布**：优先 `projects[key]`，老数据回落到该项目第一个工作流的 layout。"""
    key = _wf_key(cwd)
    proj = (d.get("projects") or {}).get(key)
    if isinstance(proj, dict):
        one = _wf_canvas_from(proj)
        if one["nodes"] or proj.get("zoom"):
            return one
    # 兼容老版本：每个 workflow 各有一份 layout
    for w in (d.get("workflows") or []):
        if not isinstance(w, dict):
            continue
        if _wf_key(w.get("cwd")) != key:
            continue
        one = _wf_canvas_from(w.get("layout"))
        if one["nodes"]:
            return one
    return {"zoom": 1.0, "nodes": {}}


def _wf_load() -> dict:
    try:
        with _WF_LOCK:
            with open(WORKFLOWS_FILE, "r", encoding="utf-8") as fh:
                d = json.load(fh)
    except Exception:  # noqa: BLE001
        return {"version": 1, "workflows": [], "projects": {}}
    if not isinstance(d, dict):
        return {"version": 1, "workflows": [], "projects": {}}
    if not isinstance(d.get("workflows"), list):
        d["workflows"] = []
    if not isinstance(d.get("projects"), dict):
        d["projects"] = {}
    return d


def list_workflows(cwd: str = "") -> list:
    """工作流列表（按 updated 倒序；cwd 可选过滤，大小写不敏感）。

    ⚠ `archived` 是 P3 新增的字段（归档只是"收起来"，**不删除**）：
    老数据没有这个键 → 统一补成 False，前端不用再判 undefined。
    """
    d = _wf_load()
    out = [w for w in (d.get("workflows") or []) if isinstance(w, dict)]
    for w in out:
        if "archived" not in w:
            w["archived"] = False
        # ★老工作流没有 stages → 内存里补一个默认阶段（不写盘，真正的归一发生在 save）
        st, ms = norm_stages(w.get("stages"), w.get("members"))
        w["stages"], w["members"] = st, ms
        # ★项目级画布（0.2.103）：同一 cwd 的所有工作流共享同一张画布
        w["projectCanvas"] = _project_canvas(d, w.get("cwd"))
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
    stages, members = norm_stages(data.get("stages"), members)
    cwd = str(data.get("cwd") or "").strip()
    task = str(data.get("taskTemplate") or "").strip()[:2000]
    layout = data.get("layout") if isinstance(data.get("layout"), dict) else {}
    payload_canvas = _wf_canvas_from(layout)
    now = _now()
    with _WF_LOCK:
        d = _wf_load()
        projects = d.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = {}
            d["projects"] = projects
        key = _wf_key(cwd)
        proj = _project_canvas(d, cwd)
        # ★项目画布 + 本次提交的节点（同一项目的工作流共享坐标；新工作流不另起一张空画布）
        nodes = dict(proj.get("nodes") or {})
        nodes.update(payload_canvas.get("nodes") or {})
        zoom = payload_canvas.get("zoom") if payload_canvas.get("nodes") else proj.get("zoom")
        try:
            zoom = max(0.6, min(1.6, float(zoom or 1)))
        except (TypeError, ValueError):
            zoom = 1.0
        projects[key] = {"zoom": zoom, "nodes": nodes, "updated": now}
        wfs = d.get("workflows") or []
        wid = str(data.get("id") or "").strip()
        cur = next((w for w in wfs if isinstance(w, dict)
                    and str(w.get("id")) == wid), None) if wid else None
        if cur is None:
            wid = "wf_%s_%s" % (time.strftime("%Y%m%d-%H%M%S"), uuid.uuid4().hex[:6])
            cur = {"id": wid, "created": now, "runCount": 0, "lastRunId": "",
                   "archived": False}
            wfs.append(cur)
        # ★归档状态不在 payload 里（前端自动保存不会误改）；只由 archive_workflow 动
        cur.setdefault("archived", False)
        cur.update({"name": name, "cwd": cwd, "taskTemplate": task,
                    "stages": stages,
                    "members": members,
                    "layout": {"zoom": zoom, "nodes": dict(nodes)},
                    "projectCanvas": {"zoom": zoom, "nodes": dict(nodes)},
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
        cp["archived"] = False        # ★副本不继承归档状态（复制通常是想用）
        cp["layout"] = _project_canvas(d, src.get("cwd"))
        cp["projectCanvas"] = dict(cp["layout"])
        d.setdefault("workflows", []).append(cp)
        d["workflows"].sort(key=lambda w: w.get("updated") or 0, reverse=True)
        d["workflows"] = d["workflows"][:KEEP_WORKFLOWS]
        _atomic_write(WORKFLOWS_FILE, d)
    return cp


def archive_workflow(wid: str, archived: bool = True) -> dict:
    """★归档 / 取消归档（P3）：**不删除任何东西**，只让左栏把它收进折叠区。

    * 归档的不会被默认选中（`collabLoadWorkflows` 里跳过）；
    * 成员 / 默认任务 / 画布布局 / 运行历史全部原样保留；
    * 跑过的 run 仍然带着 `workflowId`，随时能从「已归档」里点回来。
    """
    wid = str(wid or "").strip()
    if not wid or wid.startswith(".") or "/" in wid or "\\" in wid:
        raise ValueError("非法的工作流 id")
    with _WF_LOCK:
        d = _wf_load()
        w = next((x for x in (d.get("workflows") or [])
                  if isinstance(x, dict) and str(x.get("id")) == wid), None)
        if not w:
            raise ValueError("没有这个工作流")
        w["archived"] = bool(archived)
        _atomic_write(WORKFLOWS_FILE, d)
        return dict(w)


def _wf_sig(cwd: str, members) -> tuple:
    """工作流「成员组合」指纹：目录 + 每个成员的 (阶段, 角色, 名字, 大脑)。
    ⚠ 不比 id / permLevel —— 同一套人马重新跑过几遍也不该存成好几个工作流。"""
    ckey = str(cwd or "").replace("\\", "/").rstrip("/").lower()
    parts = []
    for m in (members or []):
        if not isinstance(m, dict):
            continue
        parts.append((str(m.get("stage") or "s1"), str(m.get("role") or ""),
                      str(m.get("name") or ""), str(m.get("engine") or "")))
    return (ckey, tuple(parts))


def workflow_from_run(run_id: str, name: str = "") -> dict:
    """★把一条**已有的运行**另存为工作流（P3）。

    * 成员取 run 的成员快照（**排除仲裁者** `kind=arbitration`）；
    * 默认任务 = 那次的任务；目录 = 那次的工作目录；
    * **按「目录 + 成员组合」去重**：已经有一模一样的工作流就复用它（`existing=True`），
      不重复造 —— 这是"另存为"最容易踩的坑；
    * 存完把这条 run 认领到新工作流（`workflowId/Name`）—— 这是**用户显式操作**，
      不是"自动猜旧 run 属于谁"。
    """
    rid = str(run_id or "").strip()
    if not rid or os.path.basename(rid) != rid or ".." in rid:
        raise ValueError("非法的任务 id")
    with _LOCK:
        run = _RUNS.get(rid) or _load_file(rid)
    if not run:
        raise ValueError("没有这个任务")
    raw = [m for m in (run.get("members") or [])
           if isinstance(m, dict) and str(m.get("kind") or "") != "arbitration"
           and str(m.get("engine") or "").strip()]
    if not raw:
        raise ValueError("这条运行没有可用的成员（可能只有仲裁者）")
    members = []
    for i, m in enumerate(raw[:8]):
        members.append({"id": "m%d" % (i + 1), "role": norm_role(m.get("role")),
                        "name": str(m.get("name") or ""),
                        "engine": str(m.get("engine") or ""),
                        "permLevel": norm_perm_level(m.get("permLevel")),
                        "stage": str(m.get("stage") or "s1")})
    stages = [{"id": str(s.get("id") or ""), "name": str(s.get("name") or ""),
               "when": s.get("when"), "onFail": s.get("onFail"),
               "retries": s.get("retries")}
              for s in (run.get("stages") or []) if isinstance(s, dict)]
    cwd = str(run.get("cwd") or "")
    sig = _wf_sig(cwd, members)
    for w in list_workflows():
        if _wf_sig(w.get("cwd"), w.get("members")) == sig:
            if w.get("archived"):
                w = archive_workflow(w.get("id"), False)
            return dict(w, existing=True)
    nm = re.sub(r"\s+", " ", str(name or run.get("title") or run.get("task") or "").strip())[:40] \
        or "从运行另存"
    wf = save_workflow({"name": nm, "cwd": cwd,
                        "taskTemplate": str(run.get("task") or ""),
                        "stages": stages,
                        "members": members,
                        "layout": {"zoom": 1, "nodes": {}}})
    # 认领这条 run（历史记录本身不变，只补归属）
    with _LOCK:
        cur = _RUNS.get(rid) or _load_file(rid) or {}
        if cur:
            cur["workflowId"] = str(wf.get("id") or "")
            cur["workflowName"] = str(wf.get("name") or "")
            _RUNS[rid] = cur
            _save(cur)
    _wf_touch_run(wf.get("id"), rid)
    fresh = next((w for w in list_workflows()
                  if str(w.get("id")) == str(wf.get("id"))), wf)
    return dict(fresh, existing=False)


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


def apply_member_isolation(run_id: str, member_id: str, force: bool = False) -> dict:
    """把一个成员在**独立工作区**里的改动应用回主目录（先隔离旧版；冲突默认拒绝）。"""
    rid = str(run_id or "").strip()
    with _LOCK:
        run = _RUNS.get(rid) or _load_file(rid)
    if not run:
        raise ValueError("没有这个任务：%s" % rid)
    if str(run.get("status") or "") == "running":
        raise ValueError("任务还在跑：等它结束再应用")
    m = next((x for x in (run.get("members") or [])
              if str(x.get("id")) == str(member_id)), None)
    if m is None:
        raise ValueError("没有这个成员：%s" % member_id)
    plan = isolation.load(rid)
    res = isolation.apply_member(plan, member_id, force=bool(force))
    if res.get("ok"):
        with _LOCK:
            m["applied"] = {"at": _now(),
                            "applied": len(res.get("applied") or []),
                            "deleted": len(res.get("deleted") or []),
                            "skipped": len(res.get("skipped") or [])}
            run["time"] = run.get("time") or {}
            run["updated"] = _now()
            _save(run)
        try:
            audit.append(
                "collab",
                actor={"kind": "agent", "id": str(m.get("engine") or ""),
                       "name": m.get("name") or "", "member": m.get("id") or "",
                       "engine": m.get("engine") or ""},
                action={"kind": "files", "title": "应用成员改动回主目录",
                        "targets": (res.get("applied") or [])[:20] + (res.get("deleted") or [])[:20],
                        "destructive": bool(res.get("deleted"))},
                decision="manual", result="ok" if not res.get("skipped") else "partial",
                ref={"run": rid, "member": m.get("id")},
                note="+%d -%d skipped=%d" % (len(res.get("applied") or []),
                                             len(res.get("deleted") or []),
                                             len(res.get("skipped") or [])))
        except Exception:  # noqa: BLE001
            pass
    return res


def cleanup_isolation(run_id: str) -> dict:
    """删掉这次运行的隔离目录（worktree / 影子副本）—— 不影响主目录、不影响运行记录。"""
    rid = str(run_id or "").strip()
    res = isolation.cleanup(rid)
    with _LOCK:
        run = _RUNS.get(rid) or _load_file(rid)
        if run:
            for m in (run.get("members") or []):
                m.pop("isoCwd", None)
                if isinstance(m.get("isolation"), dict):
                    m["isolation"] = dict(m["isolation"], cleaned=True)
            run["isolation"] = dict(run.get("isolation") or {}, cleaned=True)
            _save(run)
    return res


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
        run["_deleted"] = True          # 让还在收尾的线程别再 _save（否则文件会被写回来）
        _RUNS.pop(rid, None)
    # 顺手清掉隔离目录（worktree / 影子副本）；失败不影响删除
    try:
        isolation.cleanup(rid)
    except Exception:  # noqa: BLE001
        pass
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

# ★每个成员**必须**有自己的权限等级（节点级，不再跟随面板）；trust=信任（非破坏性自动放行）
PERM_LEVELS = [
    {"id": "ask", "label": "全部询问", "note": "每条命令都问"},
    {"id": "read", "label": "只读自动", "note": "读自动；写/执行要问"},
    {"id": "edit", "label": "读写自动", "note": "工作区内读写自动"},
    {"id": "exec", "label": "执行自动", "note": "工作区内读写执行自动"},
    {"id": "trust", "label": "信任（非破坏性自动）", "note": "不再逐条问；破坏性 / 敏感读取仍然必须你确认"},
]
_PERM_IDS = {x["id"] for x in PERM_LEVELS}


def norm_perm_level(v) -> str:
    """成员权限等级归一；不认识的值回空（= 跟随面板），不猜。"""
    s = str(v or "").strip().lower()
    return s if s in _PERM_IDS else ""


def _pending_public(p: dict) -> dict:
    return {k: p.get(k) for k in
            ("id", "run", "member", "memberName", "kind", "title", "locations",
             "command", "destructive", "secret", "created", "status", "decision", "decidedAt")}


def _write_approvals() -> int:
    """把「待批」快照写给托盘/热键（`_approvals.json`）。返回当前条数。"""
    with _LOCK:
        items = [_pending_public(p) for p in _PENDING.values()
                 if p.get("status") == "pending"]
    try:
        _atomic_write(APPROVALS_FILE, {"ts": time.time(), "items": items})
    except Exception as exc:  # noqa: BLE001
        acp_log("[orchestrator] 写待批快照失败：%s" % exc)
    return len(items)


def _notify_approval(p: dict, total: int) -> None:
    """新待批 → 系统气泡 + 闪任务栏（托盘进程读 `_notify.json`；不抢焦点）。"""
    rid = str(p.get("run") or "")
    now = time.time()
    # 同一 run 30 秒内只弹一次；新 run / 单条立即弹
    if rid == _NOTIFY_STATE.get("run") and (now - float(_NOTIFY_STATE.get("ts") or 0)) < 30:
        return
    _NOTIFY_STATE["run"] = rid
    _NOTIFY_STATE["ts"] = now
    who = str(p.get("memberName") or p.get("member") or "工作流")
    title = str(p.get("title") or p.get("kind") or "权限申请")
    cmd = " ".join(str(p.get("command") or "").split())[:160]
    flag = "破坏性" if p.get("destructive") else ("敏感读取" if p.get("secret") else "")
    body = "%s：%s" % (who, title)
    if cmd:
        body += " · " + cmd
    if flag:
        body += " · " + flag
    if total > 1:
        body += "（共 %d 条待批）" % total
    try:
        _atomic_write(NOTIFY_FILE, {"ts": now, "title": "协作待批：" + who,
                                    "body": body, "flash": True})
    except Exception as exc:  # noqa: BLE001
        acp_log("[orchestrator] 写待批通知失败：%s" % exc)


def pending_permissions(run_id: str = "") -> list:
    with _LOCK:
        items = [p for p in _PENDING.values()
                 if p.get("run") and (not run_id or str(p["run"]) == str(run_id))]
    return [_pending_public(p) for p in items]


def _member_waiting(run_id: str, member_id: str) -> bool:
    """这个成员此刻是不是在等批准 / 等回答？（等人**不算** RUN_TIMEOUT 的算力时间）"""
    with _LOCK:
        return (any(str(p.get("run")) == str(run_id)
                    and str(p.get("member")) == str(member_id)
                    and p.get("status") == "pending"
                    for p in _PENDING.values())
                or any(str(f.get("run")) == str(run_id)
                       and str(f.get("member")) == str(member_id)
                       and f.get("status") == "pending"
                       for f in _PENDING_FORMS.values()))


def reply_permission(perm_id: str, decision: str, via: str = "") -> bool:
    d = str(decision or "").strip().lower()
    if d not in ("once", "always", "reject"):
        raise ValueError("decision 只能是 once / always / reject")
    via = str(via or "").strip().lower()
    with _LOCK:
        p = _PENDING.get(str(perm_id))
        if not p:
            return False
        # ★破坏性 / 敏感读取：热键只能拒绝，不能批准（必须回工作台看命令原文）
        if via == "hotkey" and d in ("once", "always"):
            if p.get("destructive"):
                raise ValueError("破坏性操作不能用热键批准，请打开工作台确认")
            if p.get("secret"):
                raise ValueError("敏感读取不能用热键批准，请打开工作台确认")
        p["decision"] = d
        p["status"] = "allowed" if d in ("once", "always") else "denied"
        p["decidedAt"] = _now()
        p["via"] = via or "unknown"
        run = _RUNS.get(p["run"])
        if run:
            for item in (run.get("permissions") or []):
                if item.get("id") == p["id"]:
                    item.update({"status": p["status"], "decision": d,
                                 "decidedAt": p["decidedAt"], "via": p["via"]})
                    break
            _save(run)
        p["event"].set()
    _write_approvals()
    return True


def _form_public(f: dict) -> dict:
    return {k: f.get(k) for k in
            ("id", "run", "member", "memberName", "title", "fields",
             "created", "status", "decidedAt")}


def pending_forms(run_id: str = "") -> list:
    with _LOCK:
        items = [f for f in _PENDING_FORMS.values()
                 if f.get("run") and (not run_id or str(f["run"]) == str(run_id))]
    return [_form_public(f) for f in items]


def reply_form(form_id: str, answer=None, decline: bool = False) -> bool:
    with _LOCK:
        f = _PENDING_FORMS.get(str(form_id))
        if not f:
            return False
        f["answer"] = None if decline else (answer if isinstance(answer, dict) else {})
        f["declined"] = bool(decline)
        f["event"].set()
    return True


def _notify_form(f: dict, total: int) -> None:
    rid = str(f.get("run") or "")
    now = time.time()
    if rid == _FORM_NOTIFY_STATE.get("run") and (now - float(_FORM_NOTIFY_STATE.get("ts") or 0)) < 30:
        return
    _FORM_NOTIFY_STATE["run"] = rid
    _FORM_NOTIFY_STATE["ts"] = now
    who = str(f.get("memberName") or f.get("member") or "工作流")
    title = str(f.get("title") or "需要你回答")
    body = "%s：%s" % (who, title)
    if total > 1:
        body += "（共 %d 条待回答）" % total
    try:
        _atomic_write(NOTIFY_FILE, {"ts": now, "title": "协作待回答",
                                    "body": body, "flash": True})
    except Exception as exc:  # noqa: BLE001
        acp_log("[orchestrator] 写待回答通知失败：%s" % exc)


def _ask_form(run: dict, member: dict, form: dict, params) -> dict:
    """工作流里的 agent 追问（elicitation）：排队到工作台，等人回答；超时/取消→拒绝。"""
    if str((params or {}).get("mode") or "form") != "form":
        return {"action": "decline"}
    fid = "oform_%s" % uuid.uuid4().hex[:10]
    created = _now()
    title = str(form.get("title") or "需要你回答")
    item = {"id": fid, "run": run["id"], "member": member["id"],
            "memberName": member.get("name") or member["id"],
            "title": title, "fields": form.get("fields") or [], "created": created,
            "status": "pending", "decidedAt": 0, "answer": None, "declined": False,
            "event": threading.Event()}
    with _LOCK:
        _PENDING_FORMS[fid] = item
        run.setdefault("forms", []).append(
            {k: item.get(k) for k in ("id", "run", "member", "memberName",
                                      "title", "fields", "created", "status", "decidedAt")})
        _save(run)
    member.setdefault("activities", []).append({
        "ts": _now(), "kind": "form", "name": title, "status": "waiting"})
    member["activities"] = member["activities"][-100:]
    acp_log("[orchestrator] %s/%s 等待回答：%s"
            % (run["id"], member["id"], title))
    _notify_form(item, len(pending_forms(run["id"])))
    cancel_evt = _CANCEL.get((run["id"], member["id"]))
    deadline = (time.time() + ASK_TIMEOUT) if ASK_TIMEOUT > 0 else None
    while True:
        if item["event"].wait(0.5):
            break
        if cancel_evt is not None and cancel_evt.is_set():
            break
        if deadline is not None and time.time() >= deadline:
            break
    answered = bool(item["event"].is_set())
    declined = bool(item.get("declined"))
    answer = item.get("answer")
    cancelled = bool(cancel_evt is not None and cancel_evt.is_set())
    if not answered:
        status = "timeout"
    elif declined or answer is None:
        status = "declined"
    else:
        status = "answered"
    decided = _now()
    with _LOCK:
        for row in (run.get("forms") or []):
            if row.get("id") == fid:
                row.update({"status": status, "decidedAt": decided})
                break
        _save(run)
        _PENDING_FORMS.pop(fid, None)
        member["_waitSec"] = float(member.get("_waitSec") or 0) + max(
            0.0, (time.time() * 1000.0 - created) / 1000.0)
    if cancelled:
        return {"action": "cancel"}
    if status == "answered":
        return {"action": "accept", "content": answer or {}}
    return {"action": "decline"}


def _wf_scope(run: dict, member: dict) -> str:
    """工作流每轮快照的 scope：一条 run 里每个成员各自一条，互不串味。"""
    return "wf-%s-%s" % (str(run.get("id") or "")[:20], str(member.get("id") or "")[:12])


def _turn_snapshot(run: dict, member: dict) -> dict:
    """★动手前给**成员自己的工作区**落一次快照（只记路径+哈希）。

    ⚠ 隔离（worktree / shadow）开着时，成员的 root 是它自己的目录 —— 快照跟它走，
      否则"这一轮改了哪些文件"会统计成主目录（错的）。
    """
    root = str(member.get("isoCwd") or run.get("cwd") or "")
    if not root or not os.path.isdir(root):
        return {}
    scope = _wf_scope(run, member)
    want = False
    try:
        want = snapshots.turn_with_content(group=True)
    except Exception:  # noqa: BLE001
        want = False
    try:
        res = snapshots.take_turn_snapshot(root, scope, label="工作流派活", with_content=want)
        return {"scope": scope, "ts": str((res or {}).get("ts") or ""), "with_content": bool(want)}
    except Exception as exc:  # noqa: BLE001 - 快照只是安全网
        acp_log("[orchestrator] %s/%s 轮快照失败：%r" % (run.get("id"), member.get("id"), exc))
        return {}


def _turn_changes(run: dict, member: dict, snap: dict) -> dict:
    """这一轮**改了哪些文件**（文件级；只回名字 + 计数，不存内容 → 不能一键回滚）。"""
    root = str(member.get("isoCwd") or run.get("cwd") or "")
    scope = str((snap or {}).get("scope") or "")
    if not root or not scope:
        return {"files": 0}
    try:
        res = snapshots.changes_since_latest(root, scope)
    except Exception as exc:  # noqa: BLE001 - 统计失败不影响结果
        acp_log("[orchestrator] %s/%s 轮改动统计失败：%r" % (run.get("id"), member.get("id"), exc))
        return {"files": 0}
    added = [str(x.get("path") or "") for x in (res.get("added") or [])]
    changed = [str(x.get("path") or "") for x in (res.get("changed") or [])]
    deleted = [str(x.get("path") or "") for x in (res.get("deleted") or [])]
    renamed = ["%s → %s" % (x.get("from") or "", x.get("to") or "")
               for x in (res.get("renamed") or [])]
    paths = [p for p in (added + changed + renamed + deleted) if p][:20]
    out = {"files": int(res.get("files") or 0),
           "added": len(added), "changed": len(changed),
           "deleted": len(deleted), "renamed": len(renamed),
           "paths": paths, "since": res.get("since")}
    if out["files"]:
        # 进审计链：和工作流审批同一本账（谁、哪一轮、改了哪几个文件）
        try:
            audit.append(
                "collab",
                actor={"kind": "agent", "id": str(member.get("engine") or ""),
                       "name": member.get("name") or "", "member": member.get("id") or "",
                       "role": member.get("role") or "",
                       "engine": member.get("engine") or ""},
                action={"kind": "files", "title": "改了 %d 个文件" % out["files"],
                        "targets": paths, "destructive": False},
                decision="auto", result="ok",
                ref={"run": run.get("id"), "member": member.get("id"),
                     "scope": scope, "ts": str((snap or {}).get("ts") or "")},
                note="+%d ~%d -%d%s" % (out["added"], out["changed"], out["deleted"],
                                       (" ⇄%d" % out["renamed"]) if out["renamed"] else ""),
            )
        except Exception as exc:  # noqa: BLE001 - 审计失败不影响运行
            acp_log("[orchestrator] %s/%s 改动入链失败：%r" % (run.get("id"), member.get("id"), exc))
    return out


def _quarantine_before_allow(run: dict, member: dict, cmd, title, locations,
                             kind="") -> str:
    """★忏悔室（`ui 0.2.99`）：工作流放行前把「修改前 / 删除前」的旧版复制进库。

    修改类只存单文件（目录改动交给删除类）；删除类目录也存。
    失败只记日志、**绝不挡放行**（安全网不该变成新的故障点）。
    """
    root = str(run.get("cwd") or "")
    try:
        qid = capture_before_allow(
            root, cmd, title or "", locations,
            scope=str(run.get("id") or "")[:24] or "wf",
            note=str(title or cmd or "")[:200], kind=kind or "")
    except Exception as exc:  # noqa: BLE001
        acp_log("[orchestrator] %s/%s 忏悔室留存失败（不影响放行）：%r"
                % (run.get("id"), member.get("id"), exc))
        return ""
    if qid:
        try:
            n = len((snapshots.load_quarantine(qid) or {}).get("items") or [])
        except Exception:  # noqa: BLE001
            n = 0
        member.setdefault("activities", []).append({
            "ts": _now(), "kind": "tool", "name": "忏悔室留存旧版",
            "status": "done", "detail": "留存 %d 项 → %s" % (n, qid)})
        member["activities"] = member["activities"][-100:]
        acp_log("[orchestrator] %s/%s 忏悔室留存 %d 项（%s）"
                % (run.get("id"), member.get("id"), n, qid))
    return qid


def _ask_permission(run: dict, member: dict, kind, title, locations, options, info) -> str:
    """auto_permission 钩子：等级命中→放行；否则排队等 UI（超时/取消→拒绝）。"""
    cmd = str((info or {}).get("command") or "")
    destructive = _is_destructive(cmd, title)
    secret = _is_secret(cmd, title)
    net = _is_net(kind, title, cmd, locations)
    # ★工作流权限：只认成员自己的 permLevel；**绝不回落到面板等级**（空 = 全部询问，fail-closed）
    mlevel = norm_perm_level(member.get("permLevel"))
    level = mlevel or "ask"
    ok = ((not destructive) and (not secret)
          and (mlevel == "trust"
               or (net and allow_network())
               or allowed_by_level(level, kind, title, cmd, locations,
                                   member.get("isoCwd") or run.get("cwd") or "")))
    if ok:
        if mlevel == "trust":
            acp_log("[orchestrator] %s/%s 自动放行（成员信任，非破坏性）：%s"
                    % (run["id"], member["id"], title or kind or ""))
        _quarantine_before_allow(run, member, cmd, title, locations, kind=kind)
        return "once"
    pid = "operm_%s" % uuid.uuid4().hex[:10]
    created = _now()
    p = {"id": pid, "run": run["id"], "member": member["id"],
         "memberName": member.get("name") or member["id"], "kind": kind or "",
         "title": title or "", "locations": list(locations or []),
         "command": cmd, "destructive": bool(destructive), "secret": bool(secret),
         "created": created, "status": "pending", "decision": None,
         "via": "", "event": threading.Event()}
    with _LOCK:
        _PENDING[pid] = p
        run.setdefault("permissions", []).append(
            {k: p.get(k) for k in ("id", "run", "member", "memberName", "kind", "title",
                                   "locations", "command", "destructive", "secret", "created",
                                   "status", "decision", "decidedAt")})
        _save(run)
    # 记一条活动，UI 即使不看待批列表也能看到"卡在哪"
    member.setdefault("activities", []).append({
        "ts": _now(), "kind": "permission", "name": title or kind or "权限",
        "status": "waiting", "command": cmd,
        "destructive": bool(destructive), "secret": bool(secret)})
    member["activities"] = member["activities"][-100:]
    acp_log("[orchestrator] %s/%s 权限等待：%s%s%s"
            % (run["id"], member["id"], title,
               "（破坏性）" if destructive else "",
               "（敏感读取）" if secret else ""))
    total = _write_approvals()
    _notify_approval(p, total)
    cancel_evt = _CANCEL.get((run["id"], member["id"]))
    deadline = (time.time() + ASK_TIMEOUT) if ASK_TIMEOUT > 0 else None
    while True:
        if p["event"].wait(0.5):
            break
        if cancel_evt is not None and cancel_evt.is_set():
            break
        if deadline is not None and time.time() >= deadline:
            break
    decision = p.get("decision") or "reject"
    answered = bool(p.get("event") is not None and p["event"].is_set())
    if cancel_evt is not None and cancel_evt.is_set():
        decision = "reject"
    # ★忏悔室（`ui 0.2.99`）：批准之后、真正执行之前，把**修改前 / 删除前**的旧版
    #   复制进隔离库（不再只限破坏性命令；编辑类也会留旧版）。
    qid = ""
    if decision in ("once", "always"):
        qid = _quarantine_before_allow(run, member, cmd, title, locations, kind=kind)
    if p.get("status") == "pending":
        p["status"] = "timeout" if decision == "reject" and not cancel_evt else "denied"
        p["decision"] = decision
        p["decidedAt"] = _now()
        with _LOCK:
            for item in (run.get("permissions") or []):
                if item.get("id") == pid:
                    item.update({"status": p["status"], "decision": decision,
                                 "decidedAt": p["decidedAt"], "quarantine": qid})
            _save(run)
    waited = round((time.time() * 1000.0 - created) / 1000.0, 1)
    _cancelled = bool(cancel_evt is not None and cancel_evt.is_set())
    via = str(p.get("via") or "")
    with _LOCK:
        _PENDING.pop(pid, None)
        member["_waitSec"] = float(member.get("_waitSec") or 0) + max(0.0, waited)
    _write_approvals()
    # ★审计链：**只记结论**（谁批准/拒绝了哪一条 + 命令原文 + 等了多久）。
    #   策略自动放行（等级/trust）刻意不入链 —— 见 backend/audit.py 顶部「口径」。
    audit.append(
        "collab",
        actor={"kind": "agent", "id": str(member.get("engine") or ""),
               "name": member.get("name") or "", "member": member.get("id") or "",
               "role": member.get("role") or "",
               "engine": member.get("engine") or ""},
        action={"kind": kind or "", "title": title or "", "command": cmd,
                "targets": locations, "destructive": bool(destructive),
                "secret": bool(secret)},
        decision=decision,
        result=("ok" if decision in ("once", "always") else
                "denied" if answered else "timeout"),
        approver=({"kind": "user", "id": "local", "name": "owner"} if answered else None),
        ref={"run": run["id"], "member": member["id"], "perm": pid},
        quarantine=qid,
        note=("等待 %.1fs" % waited) + ((" · via=" + via) if via else "")
             + (" · 已取消" if _cancelled else "")
             + (" · 已隔离 " + qid if qid else "")
             + ("" if answered else " · 无人批准"), ts=created / 1000.0)
    # 用户选了「始终允许」→ 写进 ACP 的 always 记忆（和面板同一条）
    if decision == "always":
        try:
            active = _ACTIVE.get((run["id"], member["id"]))
            if active:
                active["svc"]._remember_always(active["sid"], kind, locations, title)
        except Exception:  # noqa: BLE001
            pass
    return decision if decision in ("once", "always", "reject") else "reject"


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


TERMINAL_STATES = ("done", "failed", "timeout", "cancelled", "skipped")
TERMINAL_STAGES = ("done", "partial", "failed", "skipped", "cancelled")


# 上游「账户 / 凭据」类错误：换模型 / 重试都没用，必须人处理（充值、换 Key、换 agent）
_ERR_PREFIX = ("acperror", "acp 错误", "acp error", "error:", "apierror", "unexpected status",
               "request failed", "api error", "traceback (most recent call last)")


def _account_error_kind(text: str) -> str:
    """上游账户/凭据问题的类型（余额不足 / 额度 / 401 / 没配 Key）；不是这类返回空串。

    ⚠ 判据统一在 `backend/errkind.py` —— 各家写法差别很大（Insufficient Balance /
      exceeded your current quota / credit balance is too low / 402 …），
      以前只认 4 种，导致「连余额不足都没识别出来」（用户实测报过）。
      这里只挑**账户类**返回（其余错误不需要"别换模型重试"）。
    """
    k = errkind.classify(text)
    return k["label"] if k.get("account") else ""


def _is_errorish_output(text: str) -> bool:
    """正文其实是「错误」被 agent 当回答吐出来 → 不能算"有输出"。

    ⚠ 判据要**窄**：正常回答里解释 "Insufficient Balance" 不该被误判，所以只认
    "以错误前缀开头" 或 "短文本 + request_id / status 4xx"。
    """
    s = str(text or "").strip()
    if not s:
        return True
    low = s.lower()
    if low.startswith(_ERR_PREFIX):
        return True
    if len(s) <= 400 and re.search(r"request_id\s*[:=]|status\s*40[123]\b", low):
        return True
    return False


def _account_hint(kind: str) -> str:
    return ("上游账户问题：%s —— 换模型 / 重试都没用；%s"
            % (kind, errkind.KINDS["balance"][1]))


def _error_hint(text: str) -> str:
    """任意报错的人话建议（工作流 / 群聊 / 单聊共用同一套）。"""
    return errkind.classify(text)["hint"]


def _update_run(run: dict) -> None:
    with _LOCK:
        if run.get("status") in ("running",):
            # ★P4：还有阶段没跑完就**别提前收口**（成员全终态 ≠ 整个任务结束）
            stages = [s for s in (run.get("stages") or []) if isinstance(s, dict)]
            if stages and any(str(s.get("status") or "wait") not in TERMINAL_STAGES
                              for s in stages):
                _save(run)
                return
            states = [m.get("status") for m in (run.get("members") or [])]
            if states and all(x in TERMINAL_STATES for x in states):
                # ★skipped（条件不满足 / 前面失败 / 取消）不算失败，别把 run 变成 partial
                active = [x for x in states if x != "skipped"]
                done = [x for x in active if x == "done"]
                if not active:
                    run["status"] = "cancelled" if any(x == "cancelled" for x in states) else "done"
                elif not done and all(x == "cancelled" for x in active):
                    run["status"] = "cancelled"
                elif not done:
                    run["status"] = "failed"
                elif len(done) == len(active):
                    run["status"] = "done"
                else:
                    run["status"] = "partial"
            if run.get("status") in ("failed", "partial"):
                acct = [m for m in (run.get("members") or []) if m.get("accountError")]
                if acct:
                    kind2 = acct[0].get("accountErrorKind") or "余额不足（402）"
                    names = "、".join(str(m.get("name") or m.get("id")) for m in acct)
                    run["failReason"] = ("上游账户问题：%s —— 换模型 / 重试没用；"
                                         "请充值或换 Key（设置 → 模型凭据），涉及成员：%s"
                                         % (kind2, names))
                else:
                    run.pop("failReason", None)
        _save(run)


_PERM_LABEL = {x["id"]: x["label"] for x in PERM_LEVELS}


def _run_context(run: dict, member: dict, model: str = "") -> str:
    """把「工作流 / 阶段 / 成员 / 大脑 / 其他成员 / 权限」直接告诉 agent。

    这次失败的 run 前十几条命令都在翻 `_acp_sessions.json` / `_collab_workflows.json`
    找「结点名 / 大脑名」—— 这些本来就是我们自己知道的信息，不该让 agent 去猜/去翻状态文件。
    """
    stages = run.get("stages") or []
    stage_id = str(member.get("stage") or "")
    st = next((s for s in stages if str(s.get("id")) == stage_id), None)
    if st is not None:
        idx = stages.index(st) + 1
        stage_txt = "%s（%d/%d）" % (st.get("name") or st.get("id") or "阶段", idx, len(stages))
    else:
        stage_txt = stage_id or "（未分阶段）"
    others = []
    for m in (run.get("members") or []):
        if str(m.get("id")) == str(member.get("id")) or m.get("kind") == "arbitration":
            continue
        lbl = (ROLES.get(norm_role(m.get("role"))) or {}).get("label") or "无角色"
        others.append("%s（%s）· 大脑：%s" % (m.get("name") or m.get("id"), lbl,
                                             m.get("engine") or "?"))
    level = norm_perm_level(member.get("permLevel")) or "ask"
    level_label = _PERM_LABEL.get(level, level)
    my_lbl = (ROLES.get(norm_role(member.get("role"))) or {}).get("label") or "无角色"
    wf = str(run.get("workflowName") or "").strip() or "（临时任务）"
    wf_id = str(run.get("workflowId") or "").strip()
    lines = [
        "【本次运行上下文】",
        "工作流：%s%s" % (wf, ("（%s）" % wf_id) if wf_id else ""),
        "阶段：%s" % stage_txt,
        "成员：%s（%s）· 大脑：%s%s" % (
            member.get("name") or member.get("id"), my_lbl, member.get("engine") or "?",
            (" · 模型：" + model) if model else ""),
        "其他成员：%s" % ("；".join(others) if others else "（无）"),
        "项目目录：%s" % (run.get("cwd") or "（未设置）"),
        "权限：%s（%s）" % (level_label,
                          "非破坏性自动；破坏性 / 敏感读取仍然要你确认"
                          if level == "trust" else "按这个等级判定"),
        "",
        "★ 要操作本机窗口（原生应用）就用 `cu` 桌面工具（已在 PATH 里）：",
        "  cu windows / cu tree --hwnd N / cu find --hwnd N --name X / cu click --hwnd N --name X /",
        "  cu type --hwnd N --class-like EDIT --text ... / cu shot --hwnd N / cu status",
        "  先 tree/find 再 click，**不要截图猜坐标**；输出都是 JSON。",
        "★ 网页任务用浏览器 CDP，不要用桌面像素（Chromium 的 UIA 拿不到 DOM）：",
        "  cu browser open --url URL / cu browser text --selector \"#id\" /",
        "  cu browser click --selector \"#btn\" / cu browser type --selector \"#q\" --text ... /",
        "  cu browser wait --selector \"#done\" / cu browser shot [--full] / cu browser list",
        "★ 只想调一个接口（不想开页面）：cu http get --url URL（只读，可带浏览器 cookie）·",
        "  cu http post --url URL --json '{...}'（写请求，按动作管）",
        "★ 有 API/CLI 的优先用 API/CLI；「发送/支付/删除」这类动作会被拒绝。",
    ]
    return "\n".join(lines)


def _run_member(run: dict, member: dict, prompt_override: str = "") -> None:
    rid, mid = run["id"], member["id"]
    member["status"] = "running"
    member["started"] = _now()
    member["_waitSec"] = 0.0
    snap: dict = {}                    # ★这一轮的轮快照（失败就是空 {}）
    with _LOCK:
        _save(run)
    svc = None
    sid = ""
    q = None
    try:
        entry, base, env, cfg_cwd = _resolve_engine(member.get("engine") or "")
        # ★ 桌面工具（cu）的权限来源：只有 trust 成员才允许**动作**（点/输入/快捷键）；
        #   只读工具（窗口/树/截图）人人可用 —— 它们不改任何东西，而且每次调用都进审计。
        #   ⚠ env 必须拷贝：registry 里那份是共享的，直接改会污染别的成员。
        level_now = norm_perm_level(member.get("permLevel")) or "ask"
        env = dict(env or {})
        env.update({
            "COCRAFT_CU_ACTIONS": "allow" if level_now == "trust" else "deny",
            "COCRAFT_CU_ACTOR": "%s/%s" % (member.get("engine") or "acp", mid),
            "COCRAFT_CU_ACTOR_NAME": str(member.get("name") or mid),
            "COCRAFT_CU_MEMBER": mid,
            "COCRAFT_CU_ENGINE": str(member.get("engine") or ""),
            "COCRAFT_CU_RUN": rid,
        })
        cwd = str(member.get("isoCwd") or run.get("cwd") or cfg_cwd or "").strip() or None
        run_dir = os.path.join(ROOT, "runs", rid)
        sessions_file = os.path.join(run_dir, "%s.sessions.json" % mid)
        # ★ 每个成员独立会话文件：不碰面板/群聊的 _acp_sessions.json
        svc = AcpService(list(entry.get("command") or []), cwd=cwd, env=env, log=acp_log,
                         agent_id=str(member.get("engine") or ""),
                         provider_id=entry.get("provider") or "", baseline=base,
                         sessions_file=sessions_file)
        svc.auto_permission = (lambda kind, title, locations, options, info=None:
                               _ask_permission(run, member, kind, title, locations,
                                               options, info))
        svc.auto_form = (lambda form, params: _ask_form(run, member, form, params))
        resume_sid = str(member.get("resumeSessionID") or "")
        if not resume_sid or resume_sid not in (getattr(svc, "_sessions", {}) or {}):
            # 新探索 / 继续失败 → 干净会话
            svc._sessions = {}
            svc._order = []
            svc._bound = set()
            resume_sid = ""
            member["resumed"] = False
        model = ""
        try:
            model = str((_load_ok_models() or {}).get(member.get("engine") or "") or "")
        except Exception:  # noqa: BLE001
            model = ""
        if resume_sid:
            sid = resume_sid
            member["sessionID"] = sid
            member["resumed"] = True
        else:
            pub = svc.create_session(title=_member_session_title(run, member),
                                     cwd=cwd, model=({"id": model} if model else None))
            sid = str(pub.get("id") or "")
            member["sessionID"] = sid
            member["resumed"] = False
        with _LOCK:
            _ACTIVE[(rid, mid)] = {"svc": svc, "sid": sid}
            _save(run)
        body = prompt_override or str(run.get("task") or "")
        rl = role_line(member.get("role") or "")
        ctx = _run_context(run, member, model)
        prompt = "\n\n".join(x for x in (ctx, rl, body) if x).strip()
        q = svc.subscribe()
        t0 = time.time()
        cancel_evt = _CANCEL.get((rid, mid))
        # ★动手前先记一份（只记路径+哈希，不占磁盘）→ 结束后就能回答「这一轮改了哪些文件」
        snap = _turn_snapshot(run, member)
        try:
            svc.prompt(sid, prompt)
        except BusyError:
            member["status"] = "failed"
            member["error"] = "这个 agent 还在忙（上一轮未结束）"
            return
        turn_id = None
        retry_model_pending = False
        retry_model_done = False
        while True:
            if cancel_evt is not None and cancel_evt.is_set():
                try:
                    svc.interrupt(sid)
                except Exception:  # noqa: BLE001
                    pass
                member["status"] = "cancelled"
                member["cancelled"] = True
                break
            # ★等批准的时间不算成员算力时间（`_waitSec` 由 _ask_permission 累加）
            waited = float(member.get("_waitSec") or 0)
            if (not _member_waiting(rid, mid)) and (time.time() - t0 - waited) > RUN_TIMEOUT:
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
                err = str((d or {}).get("error") or "agent 报错")[:500]
                member["error"] = err
                low = err.lower()
                kind0 = _account_error_kind(err)
                if kind0:
                    # ★余额不足 / 凭据无效：换模型重试 100% 还是失败，直接判死并说清原因
                    member["accountError"] = True
                    member["accountErrorKind"] = kind0
                    member["error"] = _account_hint(kind0) + "\n原始报错：" + err
                    acp_log("[orchestrator] %s/%s 上游账户问题：%s" % (rid, mid, kind0))
                elif (not retry_model_done
                        and any(x in low for x in ("endpoint is unavailable",
                                                   "provider.internal",
                                                   "upstream request failed",
                                                   "status 503"))):
                    note = svc._recover_model(sid, err)
                    if note and "已自动切到" in note:
                        retry_model_pending = True
                        member["error"] = "模型服务端不可用，准备换模型重试：" + note
                    elif note:
                        member["error"] = note
            elif t in ("session.step.ended", "session.step.failed"):
                mid_id = d.get("assistantMessageID") or turn_id
                member["output"] = _assistant_text(svc, sid, mid_id)
                if retry_model_pending:
                    retry_model_pending = False
                    retry_model_done = True
                    try:
                        member["status"] = "running"
                        member["error"] = ""
                        svc.prompt(sid, prompt)
                        turn_id = None
                        t0 = time.time()
                        continue
                    except Exception as exc:  # noqa: BLE001
                        member["status"] = "failed"
                        member["error"] = "换模型后重试失败：%s: %s" % (type(exc).__name__, exc)
                        break
                out = str(member.get("output") or "").strip()
                err0 = str(member.get("error") or "")
                # ★「错误被当回答吐出来」也算失败：否则界面会显示「完成」+ 一段报错
                #   （用户实测：`AcpError … Insufficient Balance` 被标成完成、run 也显示完成）
                err_only = ((not out) or _is_errorish_output(out)
                            or bool(err0 and out and out in err0))
                if t.endswith("failed") or err_only:
                    member["status"] = "failed"
                    if not member.get("error"):
                        member["error"] = out[:500] or "这一轮失败（agent 没给原因）"
                    kind1 = _account_error_kind(err0 or out)
                    if kind1 and not member.get("accountError"):
                        member["accountError"] = True
                        member["accountErrorKind"] = kind1
                        member["error"] = _account_hint(kind1) + "\n原始报错：" + (err0 or out)[:400]
                else:
                    member["status"] = "done"
                break
        member["ended"] = _now()
        # ★这一轮改了哪些文件（文件级；只记名字+计数，不存内容）
        member["changes"] = _turn_changes(run, member, snap)
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


def _stage_body(run: dict, stage: dict, prev_text: str = "") -> str:
    """★P4：给某个阶段拼用户消息 —— **上一阶段产出** + 本次任务 + 阶段身份。

    抽成纯函数（好测）：第 2 阶段起，成员能看到上一阶段所有人写了什么；
    没有上一阶段产出时就是「阶段 1/3 + 任务」。
    """
    stages = [s for s in (run.get("stages") or []) if isinstance(s, dict)]
    idx = next((i for i, s in enumerate(stages)
                if str(s.get("id")) == str(stage.get("id"))), 0) + 1
    parts = []
    if len(stages) > 1:
        parts.append("【第 %d/%d 阶段：%s】" % (idx, len(stages), stage.get("name") or ""))
    if str(prev_text or "").strip():
        parts.append("【上一阶段产出】\n" + str(prev_text).strip())
    parts.append("【本次任务】\n" + str(run.get("task") or "").strip())
    return "\n\n".join(parts)


def _stage_digest(stage: dict, members, prev_text: str = "") -> str:
    """把这一阶段各成员的产出压成一段，供下一阶段参考（截断，别把上下文撑爆）。"""
    per = 4000
    lines = ["## 阶段：%s" % (stage.get("name") or "")]
    for m in members:
        who = "%s%s" % (m.get("roleLabel") and (m.get("roleLabel") + " · ") or "",
                        m.get("name") or m.get("id") or "")
        out = str(m.get("output") or "").strip()
        err = str(m.get("error") or "").strip()
        if out:
            lines.append("### %s\n%s" % (who, out[:per]))
        elif err:
            lines.append("### %s（失败：%s）" % (who, err[:300]))
        else:
            lines.append("### %s（没有输出）" % who)
    text = "\n\n".join(lines).strip()
    # 总长也封顶（多成员 × 多阶段一起滚雪球就没边了）
    return text[:12000] if text else str(prev_text or "")


def _skip_stage(run: dict, stage: dict, reason: str) -> None:
    """跳过某个阶段（条件不满足 / 前面的阶段失败 / 任务被取消）。"""
    with _LOCK:
        stage["status"] = "skipped"
        stage["note"] = str(reason or "")[:200]
        stage["ended"] = _now()
        for m in (run.get("members") or []):
            if str(m.get("stage")) == str(stage.get("id")) and \
                    m.get("status") in ("wait", "running"):
                m["status"] = "skipped"
                m["error"] = str(reason or "")[:300]
                m["ended"] = _now()
        _save(run)


def _start_stage_members(run: dict, members, body: str) -> None:
    rid = run["id"]
    for m in members:
        with _LOCK:
            ev = threading.Event()
            if run.get("status") == "cancelled":
                ev.set()          # 关掉窗口：刚决定取消、还没建 event 的那一下
            _CANCEL[(rid, m["id"])] = ev
        threading.Thread(target=_run_member, args=(run, m, body), daemon=True,
                         name="orch-%s-%s" % (rid, m["id"])).start()


def _stage_loop(run: dict) -> None:
    """★P4：**阶段串行、阶段内并行**。

    - 一个阶段的所有成员跑完（含重试）才进下一阶段；
    - `when=onSuccess/onFailure` 看**上一阶段**有没有失败，决定这一阶段跑不跑；
    - `onFail=stop`：这一阶段有失败 → 后面全部 skipped；`continue`：继续；
    - `retries`：阶段内失败的成员**重跑**（新会话），最多 2 次。
    ⚠ 仍然是「一个任务派给 N 个成员」，只是现在可以分成几段先后做。
    """
    rid = run["id"]
    stage_list = [s for s in (run.get("stages") or []) if isinstance(s, dict)]
    prev_text = ""
    prev_failed = False
    try:
        for idx, st in enumerate(stage_list):
            with _LOCK:
                cancelled = run.get("status") == "cancelled"
            if cancelled:
                _skip_stage(run, st, "任务已取消")
                prev_failed = True
                continue
            if idx > 0 and st.get("when") == "onSuccess" and prev_failed:
                _skip_stage(run, st, "上一阶段有失败 → 条件不满足（onSuccess）")
                continue
            if idx > 0 and st.get("when") == "onFailure" and not prev_failed:
                _skip_stage(run, st, "上一阶段全部成功 → 条件不满足（onFailure）")
                continue
            members = [m for m in (run.get("members") or [])
                       if str(m.get("stage")) == str(st.get("id"))]
            with _LOCK:
                st["status"] = "running"
                st["started"] = _now()
                for m in members:
                    if m.get("kind") == "arbitration":
                        continue
                    m["status"] = "wait"
                    m["error"] = ""
                _save(run)
            if not members:
                with _LOCK:
                    st["status"] = "done"
                    st["ended"] = _now()
                    _save(run)
                acp_log("[orchestrator] %s/%s 空阶段 → 直接完成" % (rid, st.get("id")))
                continue
            body = _stage_body(run, st, prev_text)
            attempt = 0
            retries = int(st.get("retries") or 0)
            while True:
                _start_stage_members(run, members, body)
                while True:
                    with _LOCK:
                        states = [m.get("status") for m in members]
                        cancelled = run.get("status") == "cancelled"
                    if cancelled or all(x in TERMINAL_STATES for x in states):
                        break
                    time.sleep(0.15)
                if cancelled:
                    break
                bad = [m for m in members
                       if m.get("status") in ("failed", "timeout", "cancelled")]
                if bad and attempt < retries:
                    attempt += 1
                    with _LOCK:
                        for m in bad:
                            m["retryCount"] = int(m.get("retryCount") or 0) + 1
                            m["status"] = "wait"
                            m["error"] = ""
                            m["output"] = ""
                        _save(run)
                    acp_log("[orchestrator] %s/%s 第 %d 次重试（%d 个成员失败）"
                            % (rid, st.get("id"), attempt, len(bad)))
                    continue
                break
            with _LOCK:
                states = [m.get("status") for m in members]
                if all(x == "done" for x in states):
                    st["status"] = "done"
                elif all(x in ("cancelled", "skipped") for x in states):
                    st["status"] = "cancelled" if any(x == "cancelled" for x in states) else "skipped"
                elif any(x == "done" for x in states):
                    st["status"] = "partial"
                else:
                    st["status"] = "failed"
                st["ended"] = _now()
                _save(run)
            failed_now = st["status"] in ("failed", "partial", "cancelled")
            if st["status"] != "skipped":
                prev_failed = failed_now
            prev_text = _stage_digest(st, members, prev_text)
            acp_log("[orchestrator] %s 阶段 %s（%s）：%s"
                    % (rid, st.get("id"), st.get("name"), st["status"]))
            if failed_now and st.get("onFail") == "stop":
                for s2 in stage_list[idx + 1:]:
                    _skip_stage(run, s2, "上一阶段失败（onFail=stop）")
                break
    except Exception as exc:  # noqa: BLE001
        run["error"] = "阶段调度失败：%s: %s" % (type(exc).__name__, exc)
        acp_log("[orchestrator] %s 阶段调度失败：%s" % (rid, exc))
    finally:
        with _LOCK:
            if run.get("status") == "cancelled":
                # 取消是用户意图：剩下的阶段/成员标 skipped（别再启动）
                for st in stage_list:
                    if st.get("status") in ("wait", "running"):
                        st["status"] = "skipped"
                        st["note"] = "任务已取消"
                        st["ended"] = _now()
                for m in (run.get("members") or []):
                    # 只处理「还没启动」的；正在跑的由 _run_member 自己收成 cancelled
                    if m.get("status") == "wait":
                        m["status"] = "skipped"
                        m["error"] = "任务已取消"
                        m["ended"] = _now()
                _save(run)
            else:
                _update_run(run)


def _start_threads(run: dict) -> None:
    """兼容旧调用：现在起的是**阶段调度线程**（阶段串行、阶段内并行）。"""
    threading.Thread(target=_stage_loop, args=(run,), daemon=True,
                     name="orch-stages-%s" % run["id"]).start()


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


def list_member_sessions(workflow_id: str, limit: int = 30) -> dict:
    """工作流成员会话列表（工作流管理界面专用，不混进单聊）。

    只读 run 目录里的 `<run>/<member>.sessions.json`，并把旧标题迁移成
    `<项目名> · <结点名>`（用户 2026-11-05 指定）。
    """
    wid = str(workflow_id or "").strip()
    wf = next((w for w in list_workflows() if str(w.get("id")) == wid), {}) if wid else {}
    runs = []
    try:
        names = os.listdir(RUNS_DIR)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        r = _load_file(name[:-5])
        if not r or (wid and str(r.get("workflowId") or "") != wid):
            continue
        runs.append(r)
    runs.sort(key=lambda r: r.get("created") or 0, reverse=True)
    items = []
    for r in runs[:max(1, int(limit or 30))]:
        rid = str(r.get("id") or "")
        run_dir = os.path.join(ROOT, "runs", rid)
        for m in (r.get("members") or []):
            if not isinstance(m, dict) or str(m.get("kind") or "") == "arbitration":
                continue
            mid = str(m.get("id") or "")
            title = _member_session_title(r, m)
            sid = str(m.get("sessionID") or "")
            p = os.path.join(run_dir, "%s.sessions.json" % mid)
            msg_count = 0
            chosen = None
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
                sessions = [s for s in (doc.get("sessions") or []) if isinstance(s, dict)]
                chosen = next((s for s in sessions if str(s.get("id")) == sid), None) \
                    or (sessions[-1] if sessions else None)
                if chosen:
                    sid = str(chosen.get("id") or sid)
                    msg_count = len(chosen.get("messages") or [])
                    if str(chosen.get("title") or "") != title:
                        chosen["title"] = title
                        _atomic_write(p, doc)
            except Exception:  # noqa: BLE001
                chosen = None
            items.append({"runId": rid, "runTitle": r.get("title") or r.get("task") or "",
                          "runStatus": r.get("status") or "",
                          "runCreated": r.get("created") or 0,
                          "memberId": mid, "memberName": m.get("name") or mid,
                          "engine": m.get("engine") or "", "status": m.get("status") or "",
                          "sessionID": sid, "title": title,
                          "messageCount": msg_count,
                          "resumed": bool(m.get("resumed"))})
    return {"workflowId": wid, "workflowName": wf.get("name") or "", "items": items}


def start_run(task: str, cwd: str = "", members=None, title: str = "",
              workflow_id: str = "", from_run_id: str = "") -> dict:
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
    prev_continue = None
    if str(from_run_id or "").strip():
        with _LOCK:
            prev_continue = _RUNS.get(str(from_run_id)) or _load_file(str(from_run_id))
        if not prev_continue:
            raise ValueError("找不到要继续的上一轮")
        if str(prev_continue.get("status") or "") in ("running", "wait"):
            raise ValueError("上一轮还在跑，等它结束再继续探索")
        if _wf_key(prev_continue.get("cwd")) != _wf_key(cwd):
            raise ValueError("只能继续同一个项目里的上一轮")
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
                   "stage": str(raw.get("stage") or ""), "retryCount": 0,
                   "status": "wait", "output": "", "error": "",
                   "created": _now(), "started": 0, "ended": 0,
                   "tokens": {}, "context": None, "activities": [],
                   "kind": "member", "sessionID": "", "cancelled": False})
    # ★P4：阶段串行（阶段内并行）。老工作流没有 stages → 一个默认阶段，行为与以前一致。
    stages, ms = norm_stages((wf or {}).get("stages"), ms)
    run = {"id": rid, "task": task, "title": str(title or task)[:80],
           "cwd": str(cwd or "").strip(), "status": "running",
           "workflowId": str((wf or {}).get("id") or ""),
           "workflowName": str((wf or {}).get("name") or ""),
           "continuedFrom": str(from_run_id or ""),
           "exploration": bool(str(from_run_id or "").strip()),
           "resumedMembers": 0,
           "created": _now(), "updated": _now(), "members": ms,
           "stages": [dict(s, status="wait", started=0, ended=0, note="") for s in stages],
           "forms": [], "permissions": [], "arbitration": None, "error": ""}
    # ★并行隔离（P0-1）：每个成员一个独立工作区（git → worktree；非 git → 影子副本）。
    #   失败 / 目录太大 / 只有一个成员 → mode=off + reason（前端如实显示，不假装隔离了）。
    try:
        iso = isolation.prepare(str(cwd or "").strip(), rid, [m["id"] for m in ms],
                                enabled=isolation.enabled())
    except Exception as exc:  # noqa: BLE001
        iso = {"mode": "off", "reason": "隔离准备失败：%s" % str(exc)[:160], "members": {}}
    run["isolation"] = {k: iso.get(k) for k in ("mode", "reason", "gitRel", "created")}
    for m in ms:
        mm = (iso.get("members") or {}).get(m["id"]) or {}
        if mm.get("cwd"):
            m["isoCwd"] = mm["cwd"]
            m["isolation"] = {"mode": iso.get("mode"), "cwd": mm["cwd"],
                              "branch": mm.get("branch") or ""}
    with _LOCK:
        _RUNS[rid] = run
        _save(run)
    os.makedirs(os.path.join(ROOT, "runs", rid), exist_ok=True)
    # ★继续上一轮探索（0.2.103）：复制上一轮每个成员的会话文件到新 run 目录，
    #   但不污染旧 run；成员匹配同 id，其次 name+engine。匹配不到就正常新建会话。
    if str(from_run_id or "").strip():
        prev = prev_continue
        if prev:
            prev_members = {str(m.get("id")): m for m in (prev.get("members") or [])
                            if isinstance(m, dict)}
            prev_by_sig = {}
            for pm in prev_members.values():
                if pm.get("engine"):
                    prev_by_sig[(str(pm.get("name") or "").lower(),
                                 str(pm.get("engine") or ""))] = pm
            same_cwd = (_wf_key(prev.get("cwd")) == _wf_key(cwd))
            if same_cwd:
                for m in ms:
                    pm = prev_members.get(str(m.get("id"))) \
                        or prev_by_sig.get((str(m.get("name") or "").lower(),
                                            str(m.get("engine") or "")))
                    if not pm or not str(pm.get("sessionID") or ""):
                        continue
                    src = os.path.join(ROOT, "runs", str(prev.get("id") or ""),
                                       "%s.sessions.json" % str(pm.get("id") or ""))
                    dst = os.path.join(ROOT, "runs", rid, "%s.sessions.json" % str(m.get("id") or ""))
                    try:
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)
                            m["resumeSessionID"] = str(pm.get("sessionID") or "")
                            m["resumed"] = True
                            m["continuedFrom"] = {"run": str(prev.get("id") or ""),
                                                  "member": str(pm.get("id") or "")}
                    except OSError:
                        continue
                run["resumedMembers"] = sum(1 for m in ms if m.get("resumed"))
                if run["resumedMembers"]:
                    _save(run)
    _start_threads(run)
    if wf:
        try:
            _wf_touch_run(str(wf.get("id")), rid)
        except Exception:  # noqa: BLE001
            pass
    acp_log("[orchestrator] 开始任务 %s：%d 个成员 / %d 个阶段%s"
            % (rid, len(ms), len(stages), ("（工作流 %s）" % wf.get("name")) if wf else ""))
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
        now = _now()
        for m in (run.get("members") or []):
            if member_id and str(m.get("id")) != str(member_id):
                continue
            if m.get("status") in ("wait", "running"):
                if not member_id and m.get("status") == "wait":
                    m["status"] = "skipped"
                    m["error"] = "任务已取消"
                else:
                    m["status"] = "cancelled"
                    m["error"] = "任务已取消"
                    m["cancelled"] = True
                m["ended"] = now
        if not member_id:
            run["status"] = "cancelled"
            # ★取消全部：还没开始的阶段标 skipped，正在跑的标 cancelled（线程没了也不会永远转圈）
            for st in (run.get("stages") or []):
                if st.get("status") == "wait":
                    st["status"] = "skipped"
                    st["note"] = "任务已取消"
                    st["ended"] = now
                elif st.get("status") == "running":
                    st["status"] = "cancelled"
                    st["note"] = "任务已取消"
                    st["ended"] = now
        else:
            # 单成员取消：它所在的阶段如果已经没人跑了，也一起收尾
            for st in (run.get("stages") or []):
                if st.get("status") in ("running", "wait"):
                    kids = [m for m in (run.get("members") or [])
                            if str(m.get("stage") or "s1") == str(st.get("id"))]
                    if kids and all(m.get("status") in TERMINAL_STATES for m in kids):
                        st["status"] = "cancelled"
                        st["note"] = "成员已取消"
                        st["ended"] = now
            states = [m.get("status") for m in (run.get("members") or [])]
            if states and all(x in TERMINAL_STATES for x in states):
                active = [x for x in states if x != "skipped"]
                if not active or all(x == "cancelled" for x in active):
                    run["status"] = "cancelled"
                elif all(x == "done" for x in active):
                    run["status"] = "done"
                elif any(x == "done" for x in active):
                    run["status"] = "partial"
                else:
                    run["status"] = "failed"
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


# 进程启动时清掉上一次遗留的「待批」快照（托盘别显示已经不存在的申请）
try:
    _write_approvals()
except Exception:  # noqa: BLE001
    pass
