# -*- coding: utf-8 -*-
r"""工作流并行隔离（P0-1）：每个成员一个**独立工作区**，跑完再由你决定应用谁的改动。

两种模式：
  * **worktree**（git 仓库）：`git worktree add -B cocraft/<run>/<member> <runtime/isolation/...>`
    → 每个成员在自己的目录 + 分支里干活；主工作区不会被动。
  * **shadow**（非 git 目录）：整目录**复制**（真副本；不支持硬链接的情况同样正常）到隔离目录；
    有文件数 / 体积上限，超了就不隔离并**明确告知**（`mode="off"` + reason）。

安全边界：
  * 只隔离**文件**；数据库 / 远程 API / 部署这些本来就不在文件级保护范围（与整体定位一致）；
  * 应用回主目录前会**先隔离旧版**（复用 `snapshots.quarantine_paths`）；
  * 主目录在 prepare 之后又被人改过（哈希变了）→ 视为**冲突**，默认拒绝，`force=True` 才覆盖；
  * 隔离目录放 `runtime/isolation/`（已被 `.gitignore` 忽略，不会污染仓库状态）。

不做：
  * 自动合并（冲突静默合并比失败更危险）；
  * 跨仓库 / submodule 特判（v1 遇到就退回 shadow 或 off）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

import fsview
import snapshots

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ISO_ROOT = os.path.join(ROOT, "runtime", "isolation")

MAX_SHADOW_FILES = 20000                      # shadow 最多复制多少个文件
MAX_SHADOW_BYTES = 2 * 1024 * 1024 * 1024     # 2 GB
MAX_UNCOMMITTED_SYNC = 64 * 1024 * 1024       # 把未提交改动同步进 worktree 的上限（64 MB）

_GIT_LOCK = threading.Lock()
_BAD_CHARS = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")

CONFIG_FILE = os.path.join(ROOT, "runtime", "state", "_isolation.json")


def enabled() -> bool:
    """并行隔离总开关（`runtime/state/_isolation.json` → `{"enabled": true|false}`；默认开）。

    ⚠ 只影响**新启动**的运行；已经在跑的 run 不受影响。
    环境变量 `COCRAFT_ISOLATION=0` 可临时关掉（测试 / 调试用）。
    """
    try:
        if str(os.environ.get("COCRAFT_ISOLATION", "")).strip() == "0":
            return False
    except Exception:  # noqa: BLE001
        pass
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return bool(d.get("enabled", True))
    except Exception:  # noqa: BLE001
        return True



# ---------------------------------------------------------------- 基础

def _run_dir(rid: str) -> str:
    return os.path.join(ISO_ROOT, str(rid or "run"))


def _plan_path(rid: str) -> str:
    return os.path.join(_run_dir(rid), "_plan.json")


def _now() -> float:
    return time.time()


def _save(plan: dict) -> None:
    d = _run_dir(plan.get("run"))
    os.makedirs(d, exist_ok=True)
    tmp = _plan_path(plan.get("run")) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)
    os.replace(tmp, _plan_path(plan.get("run")))


def load(rid: str) -> dict:
    try:
        with open(_plan_path(rid), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _git(cwd: str, *args, timeout: int = 60, input: bytes | None = None):
    """跑一条 git 命令 → `(rc, stdout, stderr)`；没有 git / 失败都回 rc!=0。"""
    try:
        p = subprocess.run(["git", "-C", str(cwd)] + [str(a) for a in args],
                           input=input,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, creationflags=0x08000000)
    except Exception as exc:  # noqa: BLE001
        return 127, b"", str(exc).encode("utf-8", "replace")
    return p.returncode, p.stdout, p.stderr


def _git_ok(cwd: str, *args, timeout: int = 60) -> bool:
    rc, _o, _e = _git(cwd, *args, timeout=timeout)
    return rc == 0


def toplevel(cwd: str) -> str:
    """`cwd` 所在 git 仓库根；不是仓库 / 没装 git → ""。"""
    cwd = str(cwd or "")
    if not cwd or not os.path.isdir(cwd):
        return ""
    rc, out, _err = _git(cwd, "rev-parse", "--show-toplevel")
    if rc != 0:
        return ""
    top = out.decode("utf-8", "replace").strip()
    return os.path.abspath(top) if top else ""


def _rel_inside(root: str, path: str) -> str:
    try:
        return os.path.relpath(path, root)
    except Exception:  # noqa: BLE001
        return ""


def _safe(rel: str) -> bool:
    rel = str(rel or "")
    if not rel or rel.startswith("..") or os.path.isabs(rel):
        return False
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    return bool(parts) and not any(p == ".." for p in parts)


# ---------------------------------------------------------------- shadow 复制

def _count_tree(root: str, limit_files: int, limit_bytes: int):
    """快速数文件 / 体积；超限就提前返回 `(n, size, True)`。"""
    n = 0
    size = 0
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for ent in it:
                try:
                    if ent.is_dir(follow_symlinks=False):
                        if ent.name in fsview.SKIP_DIRS:
                            continue
                        stack.append(ent.path)
                    elif ent.is_file(follow_symlinks=False):
                        n += 1
                        size += ent.stat(follow_symlinks=False).st_size
                        if n > limit_files or size > limit_bytes:
                            return n, size, True
                except OSError:
                    continue
    return n, size, False


def _link_or_copy(src: str, dst: str):
    """同一分区优先硬链接（快、省盘）；失败回退复制。"""
    try:
        os.link(src, dst)
        return dst
    except Exception:  # noqa: BLE001
        return shutil.copy2(src, dst)


def _ignore_dirs(_dir, names):
    return [n for n in names if n in fsview.SKIP_DIRS]


# ---------------------------------------------------------------- 准备

def prepare(run_cwd: str, rid: str, member_ids, enabled: bool = True) -> dict:
    """给一次运行准备隔离工作区。返回 plan（也落盘到 `runtime/isolation/<run>/_plan.json`）。

    `mode` 只可能是 `worktree` / `shadow` / `off`；`off` 一定带 `reason`（前端要如实显示）。
    """
    rid = str(rid or "")
    base = {"run": rid, "root": os.path.abspath(str(run_cwd or "")),
            "mode": "off", "reason": "", "gitTop": "", "gitRel": "",
            "members": {}, "created": _now(), "limits": {
                "files": MAX_SHADOW_FILES, "bytes": MAX_SHADOW_BYTES}}
    if not enabled:
        base["reason"] = "已关闭并行隔离"
        return base
    root = base["root"]
    if not root or not os.path.isdir(root):
        base["reason"] = "工作目录不存在，未隔离"
        return base
    ids = [str(x) for x in (member_ids or []) if str(x).strip()]
    if len(ids) < 2:
        base["reason"] = "只有一个成员，不需要隔离"
        return base

    os.makedirs(ISO_ROOT, exist_ok=True)

    # ① git 仓库 → worktree
    top = toplevel(root)
    if top:
        plan = _prepare_worktree(base, top, ids)
        if plan:
            return plan
        # worktree 失败 → 继续尝试 shadow（把原因记在 base）
        base["reason"] = "git worktree 创建失败，已退回影子副本"

    # ② 非 git / worktree 失败 → shadow
    return _prepare_shadow(base, ids)


def _prepare_worktree(base: dict, top: str, ids: list):
    rel = _rel_inside(top, base["root"])
    if rel == ".":
        rel = ""
    # ⚠ v1 只支持「工作目录 == 仓库根」：`git status --porcelain` 的路径是相对**仓库根**的，
    #   子目录会让"路径 + 成员 cwd"错位 → 遇到子目录就退回影子副本（更安全）。
    if rel or re.search(r"\.\.", rel or ""):
        base["reason"] = "工作目录是仓库子目录，v1 先用影子副本"
        return None
    # 主工作区基线（用于冲突判定）：扫一次（跳过 node_modules / runtime 等）
    try:
        base_manifest = snapshots.scan(base["root"])
    except Exception:  # noqa: BLE001
        base_manifest = {"files": {}}
    members = {}
    created = []
    try:
        for mid in ids:
            wpath = os.path.join(_run_dir(base["run"]), mid)
            branch = "cocraft/%s/%s" % (base["run"][-24:], mid)
            if os.path.isdir(wpath):
                _remove_tree(wpath)
            with _GIT_LOCK:
                rc, out, err = _git(top, "worktree", "add", "-B", branch, wpath, "HEAD")
            if rc != 0:
                raise RuntimeError("worktree add 失败：%s" % (
                    (err or out).decode("utf-8", "replace")[:200]))
            created.append((wpath, branch))
            # ★ 把主工作区的**未提交改动**同步进去（否则成员看不到用户正在改的东西）
            _sync_uncommitted(top, wpath)
            cwd = os.path.join(wpath, rel) if rel else wpath
            if not os.path.isdir(cwd):
                os.makedirs(cwd, exist_ok=True)
            members[mid] = {"cwd": cwd, "path": wpath, "branch": branch}

        plan = dict(base, mode="worktree", reason="", gitTop=top, gitRel=rel,
                    members=members, baseManifest=base_manifest)
        _save(plan)
        return plan
    except Exception as exc:  # noqa: BLE001
        for wpath, branch in created:
            _drop_worktree(top, wpath, branch)
        base["reason"] = "git worktree 失败：%s" % str(exc)[:160]
        return None


def _sync_uncommitted(top: str, wpath: str) -> None:
    """把主工作区的未提交改动（已暂存 + 未暂存 + 未跟踪）搬进 worktree（best-effort）。"""
    rc, out, _err = _git(top, "diff", "--binary", "HEAD")
    if rc != 0:
        return
    if out:
        if len(out) > MAX_UNCOMMITTED_SYNC:
            raise RuntimeError("未提交改动太大（%.1f MB），不适合 worktree 隔离"
                               % (len(out) / 1048576.0))
        rc2, _o, err = _git(wpath, "apply", "--binary", "--whitespace=nowarn", "-", input=out)
        if rc2 != 0:
            # apply 不接受 stdin 的这种写法时，退回临时补丁文件
            import tempfile
            fd, tmp = tempfile.mkstemp(prefix="cocraft-patch-", suffix=".diff")
            os.close(fd)
            try:
                with open(tmp, "wb") as fh:
                    fh.write(out)
                rc3, _o3, err3 = _git(wpath, "apply", "--binary", "--whitespace=nowarn", tmp)
                if rc3 != 0:
                    raise RuntimeError("同步未提交改动失败：%s"
                                       % (err3 or err).decode("utf-8", "replace")[:160])
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    # 未跟踪文件（??）：逐个复制过去（不复制被忽略的）
    rc, out, _e = _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if rc != 0:
        return
    for ent in out.split(b"\x00"):
        if not ent:
            continue
        try:
            line = ent.decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        if not line.startswith("?? "):
            continue
        rel = line[3:].strip().replace("\\", "/")
        if not _safe(rel):
            continue
        src = os.path.join(top, rel.replace("/", os.sep))
        dst = os.path.join(wpath, rel.replace("/", os.sep))
        try:
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
        except OSError:
            continue


def _prepare_shadow(base: dict, ids: list) -> dict:
    n, size, over = _count_tree(base["root"], MAX_SHADOW_FILES, MAX_SHADOW_BYTES)
    if over:
        base["mode"] = "off"
        base["reason"] = ("目录太大（>%d 文件或 >%d MB），未做影子隔离；"
                          "建议用 git 仓库或缩小工作目录"
                          % (MAX_SHADOW_FILES, int(MAX_SHADOW_BYTES / 1048576)))
        _save(base)
        return base
    try:
        base_manifest = snapshots.scan(base["root"])
    except Exception:  # noqa: BLE001
        base_manifest = {"files": {}}
    members = {}
    try:
        for mid in ids:
            wpath = os.path.join(_run_dir(base["run"]), mid)
            if os.path.isdir(wpath):
                _remove_tree(wpath)
            os.makedirs(os.path.dirname(wpath), exist_ok=True)
            shutil.copytree(base["root"], wpath, symlinks=True,
                            ignore=_ignore_dirs, copy_function=shutil.copy2)
            members[mid] = {"cwd": wpath, "path": wpath, "branch": ""}
        plan = dict(base, mode="shadow", reason="", members=members,
                    baseManifest=base_manifest)
        _save(plan)
        return plan
    except Exception as exc:  # noqa: BLE001
        for mid in list(members):
            _remove_tree(members[mid]["path"])
        base["mode"] = "off"
        base["reason"] = "影子副本创建失败：%s" % str(exc)[:160]
        _save(base)
        return base


def _remove_tree(path: str) -> None:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass


def _drop_worktree(top: str, wpath: str, branch: str) -> None:
    try:
        _git(top, "worktree", "remove", "--force", wpath, timeout=120)
    except Exception:  # noqa: BLE001
        pass
    _remove_tree(wpath)
    try:
        _git(top, "worktree", "prune")
        if branch:
            _git(top, "branch", "-D", branch)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 查询 / 差异

def member_cwd(plan: dict, mid: str, fallback: str = "") -> str:
    m = ((plan or {}).get("members") or {}).get(str(mid)) or {}
    p = str(m.get("cwd") or "")
    return p if p and os.path.isdir(p) else (fallback or "")


def _parse_porcelain(out: bytes) -> dict:
    """`git status --porcelain=v1 -z` → 面板要的形状。"""
    added, changed, deleted, renamed = [], [], [], []
    ents = out.split(b"\x00")
    i = 0
    while i < len(ents):
        ent = ents[i]
        i += 1
        if not ent:
            continue
        line = ent.decode("utf-8", "replace")
        if len(line) < 4:
            continue
        x, y, rel = line[0], line[1], line[3:]
        rel = rel.replace("\\", "/")
        if x == "R" or y == "R":
            # 重命名：下一个 -z 条目是旧路径
            old = ""
            if i < len(ents) and ents[i]:
                old = ents[i].decode("utf-8", "replace").replace("\\", "/")
                i += 1
            renamed.append({"from": old, "to": rel})
            continue
        if x == "?" or y == "?":
            added.append({"path": rel})
        elif x == "D" or y == "D":
            deleted.append({"path": rel})
        elif x == "A" or y == "A":
            added.append({"path": rel})
        else:
            changed.append({"path": rel})
    return {"added": added, "changed": changed, "deleted": deleted, "renamed": renamed}


def changes(plan: dict, mid: str) -> dict:
    """该成员工作区相对**基线**改了什么（文件级）。"""
    m = ((plan or {}).get("members") or {}).get(str(mid)) or {}
    cwd = str(m.get("cwd") or "")
    if not cwd or not os.path.isdir(cwd):
        return {"files": 0, "added": [], "changed": [], "deleted": [], "renamed": []}
    mode = (plan or {}).get("mode")
    if mode == "worktree":
        rc, out, _err = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        if rc != 0:
            return {"files": 0, "added": [], "changed": [], "deleted": [], "renamed": []}
        d = _parse_porcelain(out)
    else:
        try:
            base = ((plan or {}).get("baseManifest") or {}).get("files") or {}
            cur = snapshots.scan(cwd)
            diff = snapshots.diff_manifests({"files": base}, cur)
        except Exception:  # noqa: BLE001
            return {"files": 0, "added": [], "changed": [], "deleted": [], "renamed": []}
        d = {"added": diff.get("added") or [], "changed": diff.get("changed") or [],
             "deleted": diff.get("deleted") or [], "renamed": diff.get("renamed") or []}
    n = len(d["added"]) + len(d["changed"]) + len(d["deleted"]) + len(d["renamed"])
    paths = [x.get("path") or (x.get("to") if x.get("to") else "") for x in
             (d["added"] + d["changed"] + d["deleted"])][:20]
    return {"files": n, "added": d["added"], "changed": d["changed"],
            "deleted": d["deleted"], "renamed": d["renamed"], "paths": paths}


# ---------------------------------------------------------------- 应用回主目录

def _norm(p: str) -> str:
    return p.replace("\\", "/").lstrip("./")


def apply_member(plan: dict, mid: str, force: bool = False) -> dict:
    """把一个成员的改动**应用回主工作目录**（先隔离旧版；冲突默认拒绝）。

    ⚠ 不做自动合并：主目录里同名文件在 prepare 之后又变了 → 记入 `conflicts`，默认不写。
    """
    if not plan or plan.get("mode") not in ("worktree", "shadow"):
        raise RuntimeError("这次运行没有独立工作区，无法应用（%s）"
                           % ((plan or {}).get("reason") or "未隔离"))
    m = (plan.get("members") or {}).get(str(mid)) or {}
    cwd = str(m.get("cwd") or "")
    root = str(plan.get("root") or "")
    if not cwd or not os.path.isdir(cwd) or not root or not os.path.isdir(root):
        raise RuntimeError("隔离目录不在了（可能已被清理）")

    diff = changes(plan, mid)
    base = ((plan.get("baseManifest") or {}).get("files") or {})
    try:
        cur_main = snapshots.scan(root)
    except Exception:  # noqa: BLE001
        cur_main = {"files": {}}
    cur_files = cur_main.get("files") or {}

    conflicts, applied, deleted, skipped = [], [], [], []
    ops = []
    for it in diff.get("added") or []:
        ops.append(("add", _norm(it.get("path")), None))
    for it in diff.get("changed") or []:
        ops.append(("modify", _norm(it.get("path")), None))
    for it in diff.get("deleted") or []:
        ops.append(("delete", _norm(it.get("path")), None))
    for it in diff.get("renamed") or []:
        ops.append(("rename", _norm(it.get("to")), _norm(it.get("from"))))

    # 冲突：主目录该文件在 prepare 之后被改过（哈希与基线不同）
    for kind, rel, old in ops:
        if not _safe(rel):
            skipped.append({"path": rel, "why": "路径不合法"})
            continue
        b = base.get(rel)
        c = cur_files.get(rel)
        if b and c and b.get("hash") != c.get("hash"):
            conflicts.append({"path": rel, "why": "主目录里这个文件在运行期间被改过"})
        if b is None and c is not None and kind == "add":
            conflicts.append({"path": rel, "why": "主目录里已经有同名文件"})

    if conflicts and not force:
        return {"ok": False, "conflicts": conflicts, "applied": [], "deleted": [],
                "skipped": skipped,
                "hint": "确认覆盖请带 force=true（会先把主目录现有文件放进隔离区）"}

    for kind, rel, old in ops:
        if not _safe(rel):
            continue
        src = os.path.join(cwd, rel.replace("/", os.sep))
        dst = os.path.join(root, rel.replace("/", os.sep))
        try:
            if kind == "delete":
                if os.path.exists(dst):
                    _quarantine(root, [dst], "删除前（应用成员改动）")
                    os.remove(dst)
                    deleted.append(rel)
                continue
            if not os.path.isfile(src):
                skipped.append({"path": rel, "why": "隔离区里没有这个文件"})
                continue
            if os.path.exists(dst):
                _quarantine(root, [dst], "覆盖前（应用成员改动）")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            applied.append(rel)
            if old and old != rel:
                oldp = os.path.join(root, old.replace("/", os.sep))
                if os.path.exists(oldp):
                    _quarantine(root, [oldp], "改名前的旧文件")
                    try:
                        os.remove(oldp)
                    except OSError:
                        pass
        except Exception as exc:  # noqa: BLE001
            skipped.append({"path": rel, "why": str(exc)[:120]})

    return {"ok": True, "conflicts": conflicts if force else [],
            "applied": applied, "deleted": deleted, "skipped": skipped}


def _quarantine(root: str, paths, note: str) -> None:
    """应用前把主目录现有文件存一份（失败不影响应用，但会记在返回里由上层提示）。"""
    try:
        snapshots.quarantine_paths(root, paths, scope="panel", note=note, kind="modify")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- 清理

def cleanup(rid: str) -> dict:
    plan = load(rid)
    if not plan:
        _remove_tree(_run_dir(rid))
        return {"ok": True, "removed": []}
    removed = []
    mode = plan.get("mode")
    if mode == "worktree":
        top = str(plan.get("gitTop") or "")
        for mid, m in (plan.get("members") or {}).items():
            wpath, branch = str(m.get("path") or ""), str(m.get("branch") or "")
            if wpath:
                _drop_worktree(top, wpath, branch)
                removed.append(mid)
    else:
        for mid, m in (plan.get("members") or {}).items():
            _remove_tree(str(m.get("path") or ""))
            removed.append(mid)
    _remove_tree(_run_dir(rid))
    return {"ok": True, "removed": removed}
