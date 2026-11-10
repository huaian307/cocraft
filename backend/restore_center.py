# -*- coding: utf-8 -*-
r"""忏悔室：把「群聊 / 工作流 / 单聊」的删除前副本、修改前旧版与轮快照，聚合成
**分类 → 项目 → 文件 → 版本时间线**，并支持把**指定文件**回滚到**指定版本**。

数据来源（都已在 `snapshots.py` 里）：
  * `runtime/quarantine/<scope>/<ts>/`：破坏性操作放行前的副本（`files/` + `_manifest.json`）
  * `runtime/snapshots/<scope>/<ts>.json`：每轮快照（`files` 是 rel→{hash,size}）
  * `runtime/snapshots/_store/<sha>`：快照内容仓（只有 `with_content=True` 的快照才有）

映射规则：
  * scope `chat-*` / `agent-*` / `cc-*` → 群聊
  * scope `run_*` / `r_*` / `wf-*`       → 工作流
  * 其余（`panel-*` / `ses_*` / uuid）   → 单聊
  * 项目 = 记录里的 `root`（工作目录）；没有 root 归到「（无项目）」

安全：
  * 回滚前**先把当前文件复制进忏悔室**（回滚本身也可逆）；
  * 快照版本必须有内容仓 blob（`with_content=True`），否则只显示不可回滚；
  * 路径必须落在快照的 `root` 内（快照）；忏悔室用 manifest 里的原 `src`。
"""
from __future__ import annotations

import os
import shutil
import time

import snapshots

CATEGORIES = [("chat", "群聊"), ("workflow", "工作流"), ("single", "单聊")]
_CAT_LABEL = dict(CATEGORIES)


def _norm(p: str) -> str:
    return os.path.normcase(os.path.abspath(str(p or "")))


def _inside(path: str, root: str) -> bool:
    p, r = _norm(path), _norm(root)
    return p == r or p.startswith(r + os.sep)


def category_of(scope) -> str:
    s = str(scope or "")
    if s.startswith(("chat-", "agent-", "cc-")):
        return "chat"
    if s.startswith(("run_", "run-", "r_", "wf-")):
        return "workflow"
    return "single"


def _project(root) -> dict:
    r = str(root or "")
    if not r or not os.path.isdir(r):
        return {"key": "", "name": "（无项目）", "root": r}
    real = os.path.realpath(r)
    return {"key": _norm(real), "name": os.path.basename(real) or real, "root": real}


# ---------------------------------------------------------------- 名称解析

_SESS_CACHE = {"ts": 0.0, "map": {}}
_RUN_CACHE = {"ts": 0.0, "map": {}}


def _sessions() -> dict:
    now = time.time()
    if _SESS_CACHE["map"] and now - _SESS_CACHE["ts"] < 30:
        return _SESS_CACHE["map"]
    p = os.path.join(snapshots.RUNTIME, "state", "_acp_sessions.json")
    out = {}
    try:
        import json
        with open(p, "r", encoding="utf-8") as fh:
            d = json.load(fh) or {}
        for s in (d.get("sessions") or []):
            if isinstance(s, dict) and s.get("id"):
                out[str(s["id"])] = s
    except Exception:                       # noqa: BLE001
        out = {}
    _SESS_CACHE.update({"ts": now, "map": out})
    return out


def _runs() -> dict:
    now = time.time()
    if _RUN_CACHE["map"] and now - _RUN_CACHE["ts"] < 30:
        return _RUN_CACHE["map"]
    d = os.path.join(snapshots.RUNTIME, "state", "_orchestrator", "runs")
    out = {}
    try:
        import json
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, fn), "r", encoding="utf-8") as fh:
                    r = json.load(fh) or {}
                if isinstance(r, dict) and r.get("id"):
                    out[str(r["id"])] = r
            except Exception:               # noqa: BLE001
                continue
    except OSError:
        out = {}
    _RUN_CACHE.update({"ts": now, "map": out})
    return out


def _single_label(scope: str) -> str:
    s = str(scope or "")
    sid = s[6:] if s.startswith("panel-") else s
    for k, v in _sessions().items():
        if k == sid or k.startswith(sid) or sid.startswith(k[:20]):
            return "单聊 · " + (str(v.get("title") or k)[:40])
    return "单聊 · " + (sid[:14] or "?")


def _workflow_label(scope: str) -> str:
    s = str(scope or "")
    member = ""
    if s.startswith("wf-"):
        body = s[3:]
        run_prefix, member = body[:20], body[21:]
    else:
        run_prefix = s[:24]
    for rid, r in _runs().items():
        if rid.startswith(run_prefix) or run_prefix.startswith(rid[:20]):
            name = str(r.get("workflowName") or r.get("title") or rid)
            mname = ""
            if member:
                for m in (r.get("members") or []):
                    if str(m.get("id") or "").startswith(member):
                        mname = str(m.get("name") or "")
                        break
            return "工作流 · " + name[:36] + ((" · " + mname) if mname else "")
    return "工作流 · " + (run_prefix[:16] or "?")


def _chat_label(scope: str) -> str:
    s = str(scope or "")
    for p in ("chat-", "agent-", "cc-"):
        if s.startswith(p):
            s = s[len(p):]
            break
    return "群聊 · " + (s[:18] or "?")


def label_of(scope: str) -> str:
    c = category_of(scope)
    if c == "workflow":
        return _workflow_label(scope)
    if c == "chat":
        return _chat_label(scope)
    return _single_label(scope)


# ---------------------------------------------------------------- 索引

def _scan_snapshots() -> list:
    base = snapshots.SNAPSHOT_DIR
    out = []
    try:
        scopes = os.listdir(base)
    except OSError:
        return out
    for scope in scopes:
        if scope == "_store":
            continue
        d = os.path.join(base, scope)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            ts = fn[:-5]
            man = snapshots.load_manifest(scope, ts)
            if not man:
                continue
            out.append({"scope": scope, "ts": ts,
                        "taken_at": float(man.get("taken_at") or 0),
                        "root": str(man.get("root") or ""),
                        "files": man.get("files") or {},
                        "with_content": bool(man.get("with_content")),
                        "label": str(man.get("label") or "")})
    return out


def _scan_quarantine() -> list:
    out = []
    for it in snapshots.list_quarantine(limit=10000):
        rec = snapshots.load_quarantine(str(it.get("id") or ""))
        if rec:
            out.append(rec)
    return out


def _build_index() -> dict:
    files = {}

    def _ensure(cat, proj, path, rel):
        key = (cat, proj["key"], _norm(path))
        f = files.get(key)
        if f is None:
            f = {"path": path, "rel": rel.replace("\\", "/"),
                 "category": cat, "project": proj, "versions": []}
            files[key] = f
        return f

    # 轮快照
    for ev in _scan_snapshots():
        cat = category_of(ev["scope"])
        proj = _project(ev["root"])
        root = proj["root"]
        for rel, meta in (ev["files"] or {}).items():
            if not isinstance(meta, dict):
                continue
            path = os.path.join(root, rel.replace("/", os.sep)) if root else rel
            f = _ensure(cat, proj, path, rel)
            h = str(meta.get("hash") or "")
            rest = bool(ev["with_content"] and h and not h.startswith("big:")
                        and snapshots.has_blob(h))
            f["versions"].append({
                "source": "snapshot", "kind": "snapshot",
                "scope": ev["scope"], "ts": ev["taken_at"], "ts_tag": ev["ts"],
                "label": label_of(ev["scope"]),
                "detail": ev["label"] or "轮快照",
                "hash": h, "size": int(meta.get("size") or 0),
                "restorable": rest,
            })

    # 忏悔室（删除前副本）
    for rec in _scan_quarantine():
        scope = str(rec.get("scope") or "")
        cat = category_of(scope)
        proj = _project(rec.get("root"))
        for item in (rec.get("items") or []):
            if not isinstance(item, dict):
                continue
            src = str(item.get("src") or "")
            if not src:
                continue
            rel = str(item.get("rel") or os.path.basename(src))
            f = _ensure(cat, proj, src, rel)
            tomb = bool(item.get("tombstone"))
            qkind = str(rec.get("kind") or "delete")
            f["versions"].append({
                "source": "quarantine", "kind": qkind,
                "id": str(rec.get("id") or ""), "scope": scope,
                "ts": float(rec.get("taken_at") or 0),
                "label": label_of(scope),
                "detail": ("修改前副本" if qkind == "modify" else "删除前副本")
                          + ("（仅清单）" if tomb else ""),
                "stored": str(item.get("stored") or ""),
                "hash": str(item.get("hash") or ""),
                "size": int(item.get("size") or 0),
                "auto": bool(rec.get("auto")),
                "note": str(rec.get("note") or rec.get("cmd") or "")[:200],
                "restorable": bool(item.get("stored")) and not tomb,
            })

    # 去重 + 排序：同一文件连续相同内容的版本只留最新（快照 / 修改前副本都适用）；
    # 删除事件即便是同样内容也保留（它代表一次真实删除）。
    for f in files.values():
        vs = sorted(f["versions"], key=lambda x: x.get("ts") or 0, reverse=True)
        out, prev_hash = [], ""
        for v in vs:
            h = str(v.get("hash") or "")
            if v["source"] == "quarantine" and v.get("kind") == "delete":
                out.append(v)
                prev_hash = ""
                continue
            if h and h == prev_hash:
                continue
            out.append(v)
            prev_hash = h
        f["versions"] = out[:80]
        f["last"] = out[0]["ts"] if out else 0
        f["count"] = len(out)
    return files


# ---------------------------------------------------------------- 查询

def overview() -> dict:
    """三类 → 项目（文件数 / 版本数 / 最近时间）。"""
    files = _build_index()
    cats = []
    for cid, clabel in CATEGORIES:
        projects = {}
        for f in files.values():
            if f["category"] != cid:
                continue
            p = f["project"]
            row = projects.setdefault(p["key"], {
                "key": p["key"], "name": p["name"], "root": p["root"],
                "files": 0, "versions": 0, "last": 0,
            })
            row["files"] += 1
            row["versions"] += f["count"]
            row["last"] = max(row["last"], f["last"])
        plist = sorted(projects.values(), key=lambda x: x["last"], reverse=True)
        cats.append({"id": cid, "label": clabel, "projects": plist,
                     "files": sum(p["files"] for p in plist),
                     "versions": sum(p["versions"] for p in plist)})
    return {"categories": cats}


def timeline(category: str, project: str = "", path: str = "", limit: int = 120) -> dict:
    """某个分类/项目下的文件时间线。`path` 非空 = 只看一个文件。"""
    cid = str(category or "")
    if cid not in _CAT_LABEL:
        raise ValueError("未知分类：%s" % category)
    files = _build_index()
    rows = []
    for f in files.values():
        if f["category"] != cid:
            continue
        if project and f["project"]["key"] != project:
            continue
        if path and _norm(f["path"]) != _norm(path):
            continue
        rows.append(f)
    rows.sort(key=lambda x: x["last"], reverse=True)
    total = len(rows)
    rows = rows[:max(1, int(limit or 120))]
    return {"category": cid, "label": _CAT_LABEL[cid],
            "project": project, "files": rows, "total": total}


# ---------------------------------------------------------------- 回滚

def _backup_target(root: str, target: str) -> str:
    """回滚前把当前文件复制进忏悔室（回滚本身也可逆）。"""
    try:
        if not os.path.exists(target):
            return ""
        rec = snapshots.quarantine_paths(
            root or os.path.dirname(target), [target],
            scope="restore-%s" % time.strftime("%Y%m%d-%H%M%S"),
            note="回滚前自动备份", cmd="restore")
        return str((rec or {}).get("id") or "")
    except Exception:                       # noqa: BLE001
        return ""


def _write_bytes(target: str, data: bytes) -> None:
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = target + ".cocraft-restore-%d" % int(time.time() * 1000)
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, target)


def restore_file(payload: dict) -> dict:
    """把**一个文件**恢复到**一个版本**。

    `payload`：
      * `source="quarantine"` + `id`（隔离记录）+ `path`（原路径）
      * `source="snapshot"` + `scope` + `ts`（时间戳 tag）+ `rel`（相对路径）
    """
    b = payload if isinstance(payload, dict) else {}
    source = str(b.get("source") or "")
    if source == "quarantine":
        rec = snapshots.load_quarantine(str(b.get("id") or ""))
        if not rec:
            raise ValueError("找不到隔离记录")
        want = str(b.get("path") or b.get("src") or "")
        item = next((x for x in (rec.get("items") or [])
                     if str(x.get("src") or "") == want), None)
        if not item:
            raise ValueError("这条隔离记录里没有这个文件")
        if item.get("tombstone") or not item.get("stored"):
            raise ValueError("这条记录只有清单（可再生文件未复制内容），无法回滚")
        stored = os.path.join(snapshots._q_dir(str(rec.get("id") or "").replace("/", os.sep)),
                              "files", str(item.get("stored") or ""))
        if not os.path.exists(stored):
            raise ValueError("隔离副本不见了（可能已被清理）")
        target = str(item.get("src") or "")
        if not target:
            raise ValueError("记录里没有原路径")
        root = str(rec.get("root") or os.path.dirname(target))
        backup = _backup_target(root, target)
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(stored):
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                shutil.copytree(stored, target, symlinks=True)
            else:
                if os.path.isdir(target):
                    shutil.rmtree(target, ignore_errors=True)
                elif os.path.exists(target):
                    os.remove(target)
                shutil.copy2(stored, target, follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("回滚失败：%s" % exc)
        size = sum(os.path.getsize(os.path.join(dp, f))
                   for dp, _dn, fns in os.walk(target) for f in fns) \
            if os.path.isdir(target) else os.path.getsize(target)
        return {"ok": True, "source": source, "path": target, "bytes": size,
                "backup": backup}

    if source == "snapshot":
        scope = str(b.get("scope") or "")
        ts = str(b.get("ts") or b.get("ts_tag") or "")
        rel = str(b.get("rel") or "").replace("\\", "/")
        man = snapshots.load_manifest(scope, ts)
        if not man:
            raise ValueError("找不到这条快照")
        root = str(man.get("root") or "")
        meta = (man.get("files") or {}).get(rel)
        if not isinstance(meta, dict):
            raise ValueError("这条快照里没有这个文件")
        if not man.get("with_content"):
            raise ValueError("这条快照只记了文件名（没存内容），无法回滚；"
                             "请打开「轮快照带内容」后再回滚")
        h = str(meta.get("hash") or "")
        if not h or h.startswith("big:"):
            raise ValueError("这个文件太大/没有内容哈希，无法回滚")
        data = snapshots.get_blob(h)
        if data is None:
            raise ValueError("内容仓里找不到这个版本（可能已被回收）")
        if not root:
            raise ValueError("快照里没有工作目录")
        target = os.path.join(root, rel.replace("/", os.sep))
        if not _inside(target, root):
            raise ValueError("目标路径越界（不在工作目录内）")
        backup = _backup_target(root, target)
        _write_bytes(target, data)
        return {"ok": True, "source": source, "path": target,
                "bytes": len(data), "backup": backup}

    raise ValueError("source 只支持 quarantine / snapshot")
