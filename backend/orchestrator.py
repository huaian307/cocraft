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
             "stage", "retryCount")}


def public_run(run: dict) -> dict:
    if not run:
        return {}
    return {"id": run.get("id"), "task": run.get("task"), "cwd": run.get("cwd"),
            "title": run.get("title"), "status": run.get("status"),
            "workflowId": run.get("workflowId") or "",
            "workflowName": run.get("workflowName") or "",
            "created": run.get("created"), "updated": run.get("updated"),
            # ★阶段快照（P4）：状态 / 条件 / 失败策略 / 重试都在里面
            "stages": [dict(s) for s in (run.get("stages") or []) if isinstance(s, dict)],
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
            cur = {"id": wid, "created": now, "runCount": 0, "lastRunId": "",
                   "archived": False}
            wfs.append(cur)
        # ★归档状态不在 payload 里（前端自动保存不会误改）；只由 archive_workflow 动
        cur.setdefault("archived", False)
        cur.update({"name": name, "cwd": cwd, "taskTemplate": task,
                    "stages": stages,
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
        cp["archived"] = False        # ★副本不继承归档状态（复制通常是想用）
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


TERMINAL_STATES = ("done", "failed", "timeout", "cancelled", "skipped")
TERMINAL_STAGES = ("done", "partial", "failed", "skipped", "cancelled")


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
           "created": _now(), "updated": _now(), "members": ms,
           "stages": [dict(s, status="wait", started=0, ended=0, note="") for s in stages],
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
        if not member_id:
            run["status"] = "cancelled"
            # ★取消全部：还没开始的阶段/成员立刻标 skipped（不用等调度线程收尾）
            for st in (run.get("stages") or []):
                if st.get("status") == "wait":
                    st["status"] = "skipped"
                    st["note"] = "任务已取消"
                    st["ended"] = _now()
            for m in (run.get("members") or []):
                if m.get("status") == "wait":
                    m["status"] = "skipped"
                    m["error"] = "任务已取消"
                    m["ended"] = _now()
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
