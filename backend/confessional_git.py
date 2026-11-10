# -*- coding: utf-8 -*-
r"""忏悔室 · 按 git 提交出清（`ui 0.2.100`）。

用户要的语义：
  * 项目 **git 提交** 后，提交前的「删除前副本 / 修改前旧版」可以清掉；
  * 因为旧内容已经进了 git 对象库，历史版本能从 git 找回来；
  * 本地工作区只保留最新版文件，旧版不再占忏悔室空间。

安全边界（fail-closed）：
  * 只清 `taken_at <= 当前 HEAD 提交时间` 的记录（提交之后的新改动留在忏悔室）；
  * 逐项验证**旧内容本身**是不是 git 对象（不是只看路径有没有历史）：
      - 把忏悔室里的副本算出 git blob SHA1，再 `git cat-file -e` 确认对象存在；
      - 目录则递归检查里面的每个文件；
  * 验证不过 → **整条记录保留**，并在结果里给出原因；
  * 快照同样只清「所有带内容的 blob 都在 git 对象库」的旧快照；
    带未提交文件 / 未跟踪文件的快照保留。
  * 首次运行只记录当前 HEAD 作为基线，**不清理**；之后检测到新 commit 才清。
    也可以 `force=True` 立即清（面板按钮）。

不调用模型、不联网，只读 git 对象库 + 删本地忏悔室记录。
"""
from __future__ import annotations

import hashlib
import json
import os
import time

import file_rank
import snapshots

STATE_FILE = os.path.join(snapshots.RUNTIME, "state", "_confessional_git.json")


# ---------------------------------------------------------------- 基础

def _norm_root(p: str) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(p or "")))
    except Exception:  # noqa: BLE001
        return ""


def _load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, STATE_FILE)
    except Exception:  # noqa: BLE001
        pass


def _git(root: str, args, data=None):
    return file_rank._git_run(root, args, data=data, timeout=8.0)


def head_info(root: str) -> dict:
    """当前 HEAD（不是 git 仓库 / 没装 git → ok=False）。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "reason": "目录不存在"}
    p = _git(root, ["rev-parse", "HEAD"])
    if not p or p.returncode != 0:
        return {"ok": False, "reason": "不是 git 仓库或还没有提交"}
    head = p.stdout.decode("utf-8", "replace").strip()
    q = _git(root, ["log", "-1", "--format=%ct", "HEAD"])
    if not q or q.returncode != 0:
        return {"ok": False, "reason": "读不到提交时间"}
    try:
        at = float(q.stdout.decode("utf-8", "replace").strip() or 0)
    except ValueError:
        at = 0.0
    return {"ok": bool(head and at), "head": head, "at": at}


def _git_blob_sha(data: bytes) -> str:
    """和 `git hash-object` 同一算法：sha1("blob <len>\\0" + data)。"""
    h = hashlib.sha1()
    h.update(("blob %d\x00" % len(data)).encode("utf-8"))
    h.update(data)
    return h.hexdigest()


def _git_missing(root: str, shas) -> set:
    """批量问 git：这些 blob 是否在本机对象库里。返回缺失的集合。"""
    vals = sorted({str(x) for x in (shas or []) if x})
    if not vals:
        return set()
    data = ("\n".join(vals) + "\n").encode("utf-8")
    p = _git(root, ["cat-file", "--batch-check"], data=data)
    if not p or p.returncode != 0:
        return set(vals)                       # git 不可用 → 一律当缺失（保守）
    missing = set()
    for line in p.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "missing":
            missing.add(parts[0])
    return missing


def _path_blobs(path: str) -> list:
    """文件 → 内容字节列表；目录 → 递归所有文件；不存在 → 空。"""
    out = []
    try:
        if os.path.isfile(path) and not os.path.islink(path):
            with open(path, "rb") as fh:
                out.append(fh.read())
        elif os.path.isdir(path) and not os.path.islink(path):
            for dp, _dn, fns in os.walk(path):
                for f in fns:
                    fp = os.path.join(dp, f)
                    try:
                        with open(fp, "rb") as fh:
                            out.append(fh.read())
                    except OSError:
                        continue
    except OSError:
        pass
    return out


def _stored_path(rec: dict, item: dict) -> str:
    qid = str(rec.get("id") or "").replace("/", os.sep)
    stored = str(item.get("stored") or "")
    if not qid or not stored:
        return ""
    return os.path.join(snapshots._q_dir(qid), "files", stored)


def _stars(rec: dict) -> dict:
    out = {}
    for x in (rec.get("scores") or []):
        if isinstance(x, dict):
            try:
                out[str(x.get("path") or "")] = int(x.get("stars") or 0)
            except Exception:  # noqa: BLE001
                continue
    return out


def _record_verified(rec: dict, root: str, max_star: int = 1):
    """这条忏悔室记录的所有旧内容，是否都能在 git 对象库里找到？"""
    stars = _stars(rec)
    for item in (rec.get("items") or []):
        if not isinstance(item, dict):
            continue
        if item.get("tombstone"):
            continue
        src = str(item.get("src") or "")
        stored = _stored_path(rec, item)
        if not stored or not os.path.exists(stored):
            continue                          # 没有副本 → 没有可丢的内容
        blobs = _path_blobs(stored)
        if not blobs:
            continue
        shas = [_git_blob_sha(b) for b in blobs]
        missing = _git_missing(root, shas)
        if not missing:
            continue
        # auto 低风险删除：本来就是 ★ 可再生成目标 → 即使不在 git 也可清
        try:
            star = int(stars.get(src) or 0)
        except Exception:  # noqa: BLE001
            star = 0
        if rec.get("auto") and 0 < star <= max_star:
            continue
        return False, "git 里找不到旧内容：%s" % (src or "?")
    return True, ""


def _snapshot_verified(man: dict, root: str):
    """快照里**仍然代表旧版本**的内容，是否都能在 git 找到？

    快照是全量清单；没改过的文件（当前磁盘内容仍等于快照哈希）不需要 git 证明，
    否则 AGENTS.md / 日志这类未跟踪文件会让所有快照永远清不掉。
    只有「当前已不存在 / 当前内容已不同」的文件才需要旧内容在 git 里。
    """
    if not man.get("with_content"):
        return True, ""
    need = []
    for rel, meta in (man.get("files") or {}).items():
        if not isinstance(meta, dict):
            continue
        h = str(meta.get("hash") or "")
        if not h or h.startswith("big:"):
            continue
        target = os.path.join(root, str(rel).replace("/", os.sep))
        try:
            cur = snapshots._sha1_file(target)[0] if os.path.isfile(target) else ""
        except Exception:  # noqa: BLE001
            cur = ""
        if cur and cur == h:
            continue                          # 没改过 → 不是旧版本
        data = snapshots.get_blob(h)
        if data is None:
            continue                          # 内容不在了 → 没有可丢的旧版
        need.append(_git_blob_sha(data))
    missing = _git_missing(root, need)
    if missing:
        return False, "快照里有 %d 个旧文件的内容不在 git（未提交 / 未跟踪）" % len(missing)
    return True, ""


# ---------------------------------------------------------------- 清理

def _safe_rel(root: str, p: str) -> bool:
    try:
        r = os.path.realpath(root)
        x = os.path.realpath(p)
        return x == r or x.startswith(r + os.sep)
    except Exception:  # noqa: BLE001
        return False


def clean_project(root: str, force: bool = False, dry_run: bool = False) -> dict:
    """按当前 HEAD 提交时间，清理这个项目在忏悔室里的旧版。"""
    root = os.path.realpath(str(root or ""))
    key = _norm_root(root)
    if not key or not os.path.isdir(root):
        return {"ok": False, "root": root, "reason": "目录不存在"}
    info = head_info(root)
    if not info.get("ok"):
        return {"ok": True, "root": root, "skipped": True,
                "reason": info.get("reason") or "不是 git 仓库"}
    state = _load_state()
    projects = state.get("projects") if isinstance(state.get("projects"), dict) else {}
    old = projects.get(key) if isinstance(projects.get(key), dict) else None
    if not force and old is None:
        # 第一次见到这个项目：先记基线，不清理（升级 / 新装不会突然清空）
        projects[key] = {"head": info["head"], "at": info["at"], "baseline": True,
                         "checked_at": time.time()}
        state["projects"] = projects
        if not dry_run:
            _save_state(state)
        return {"ok": True, "root": root, "baseline": True, "head": info["head"],
                "commit_at": info["at"], "dropped": [], "held": [],
                "snapshots_dropped": [], "snapshots_held": []}
    if not force and old and old.get("head") == info["head"]:
        return {"ok": True, "root": root, "skipped": True, "head": info["head"],
                "commit_at": info["at"], "dropped": [], "held": [],
                "snapshots_dropped": [], "snapshots_held": []}

    # 只碰「提交前」的记录；提交之后的新改动留到下一次 commit
    cutoff = float(info["at"]) + 1.0
    cfg = snapshots.quarantine_config()
    max_star = int(cfg.get("maxStar") or 1)
    dropped, held = [], []
    bytes_dropped = 0
    for it in snapshots.list_quarantine(limit=10000):
        if it.get("root") and _norm_root(it["root"]) != key:
            continue
        try:
            taken = float(it.get("taken_at") or 0)
        except (TypeError, ValueError):
            taken = 0.0
        if taken > cutoff:
            continue
        rec = snapshots.load_quarantine(str(it.get("id") or ""))
        if not rec:
            continue
        ok, why = _record_verified(rec, root, max_star=max_star)
        if ok:
            if not dry_run:
                try:
                    snapshots.drop_quarantine(str(rec.get("id") or ""))
                except Exception as exc:          # noqa: BLE001
                    held.append({"id": rec.get("id"), "reason": "删除失败：%s" % exc})
                    continue
            dropped.append(rec.get("id"))
            bytes_dropped += int(it.get("bytes") or 0)
        else:
            held.append({"id": rec.get("id"), "reason": why})

    # 快照：只清「内容也在 git」的旧快照；否则保留
    sdropped, sheld = [], []
    try:
        scopes = os.listdir(snapshots.SNAPSHOT_DIR)
    except OSError:
        scopes = []
    for scope in scopes:
        if str(scope).startswith("_"):
            continue
        for snap in snapshots.list_snapshots(str(scope)):
            if _norm_root(snap.get("root") or "") != key:
                continue
            try:
                taken = float(snap.get("taken_at") or 0)
            except (TypeError, ValueError):
                taken = 0.0
            if taken > cutoff:
                continue
            man = snapshots.load_manifest(str(scope), str(snap.get("ts") or ""))
            if not man:
                continue
            ok, why = _snapshot_verified(man, root)
            if ok:
                if not dry_run:
                    try:
                        snapshots.drop_snapshot(str(scope), str(snap.get("ts") or ""))
                    except Exception as exc:      # noqa: BLE001
                        sheld.append({"scope": scope, "ts": snap.get("ts"),
                                      "reason": "删除失败：%s" % exc})
                        continue
                sdropped.append({"scope": scope, "ts": snap.get("ts")})
            else:
                sheld.append({"scope": scope, "ts": snap.get("ts"), "reason": why})

    projects[key] = {"head": info["head"], "at": info["at"],
                     "checked_at": time.time(), "baseline": False,
                     "last_dropped": len(dropped), "last_held": len(held),
                     "last_snapshots_dropped": len(sdropped)}
    state["projects"] = projects
    if not dry_run:
        _save_state(state)
    return {"ok": True, "root": root, "head": info["head"], "commit_at": info["at"],
            "dropped": dropped, "held": held,
            "snapshots_dropped": sdropped, "snapshots_held": sheld,
            "freed": bytes_dropped}


def _roots() -> list:
    out, seen = [], set()

    def add(p):
        k = _norm_root(p)
        if k and k not in seen and os.path.isdir(str(p or "")):
            seen.add(k)
            out.append(os.path.realpath(str(p)))

    for it in snapshots.list_quarantine(limit=10000):
        add(it.get("root") or "")
    try:
        for scope in os.listdir(snapshots.SNAPSHOT_DIR):
            if str(scope).startswith("_"):
                continue
            for snap in snapshots.list_snapshots(str(scope)):
                add(snap.get("root") or "")
    except OSError:
        pass
    return out


def clean_all(force: bool = False) -> dict:
    """所有忏悔室里出现过的项目，各跑一次 git 出清。"""
    items = []
    for root in _roots():
        try:
            items.append(clean_project(root, force=force))
        except Exception as exc:                  # noqa: BLE001
            items.append({"ok": False, "root": root, "reason": "%s: %s" % (type(exc).__name__, exc)})
    return {"ok": True, "items": items,
            "dropped": sum(len(x.get("dropped") or []) for x in items),
            "held": sum(len(x.get("held") or []) for x in items),
            "snapshots_dropped": sum(len(x.get("snapshots_dropped") or []) for x in items),
            "baseline": sum(1 for x in items if x.get("baseline"))}


def status() -> dict:
    """上次按 git 出清的状态（给面板显示/排查）。"""
    return {"ok": True, "state_file": STATE_FILE, "state": _load_state()}

