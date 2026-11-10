# -*- coding: utf-8 -*-
r"""可逆执行 / 工作区快照底座（**引擎无关**，纯标准库）。

> 北极星：「敢让 AI 动你电脑 —— 因为每一步都可见、可管、**可撤回**。」

为什么不是 git（设计说明，见 `docs/通用diff-工作区快照底座.md` §2）：
  * git 是**事后**的（得先 commit 才有得回），且要求目录是仓库、要求用户会 git；
  * 未跟踪 / 被 `.gitignore` 的文件 git **看不见** —— 而那正是 agent 最会动、也最找不回的；
  * git 不知道"这是哪个 agent 的哪一步干的"。
所以这里做的是**第 1 层地基（永远在）**：自己扫盘 + 内容哈希，git / 工具 patch / `/api/vcs` 只是**可选增强**。

本模块提供三样东西（一套基建，服务 diff 面板 / 可逆执行 / 群聊审计）：

  1. **快照**（`take_snapshot` / `latest_manifest` / `diff_manifests`）：
     目录树 → `{rel: {size, mtime, hash}}`；哈希按 `mtime+size` 缓存（未变文件零重算）；
     文件内容进**内容寻址仓**（`_store/<sha[:2]>/<sha>`，天然去重）→ 所以增量快照只写"新内容"。
  2. **忏悔室**（`quarantine` / `list_quarantine` / `restore_quarantine`）：
     破坏性操作**放行前**把要动的目标**复制**进 `runtime/quarantine/<scope>/<ts>/`，
     并写 `_manifest.json`（原路径 + 命令原文）→ 之后可一键「恢复」。
  3. **撤销**（`undo_snapshot`）：拿某次快照，把"被删的恢复回来、被改的写回旧内容、被新增的删掉
     （删之前先自动进忏悔室，所以撤销本身也可逆）"。

安全（与 `fsview.py` 同一套纪律）：
  * 所有落盘都只在 `runtime/quarantine` / `runtime/snapshots` 下；
  * 目标路径做 `realpath` 归一，**绝不**把忏悔室/快照目录自己当成操作对象；
  * 文件数 / 单文件 / 总量都有上限（超了**如实记录"没保住"**，不假装成功）。

⚠ 诚实的边界：只看**文件内容**。命令对系统做的事（装服务、改注册表、动环境变量）看不到 —— git 也一样。
"""
from __future__ import annotations

import glob as _glob
import hashlib
import json
import os
import re
import shutil
import threading
import time

import fsview
from engines import ROOT

RUNTIME = os.path.join(ROOT, "runtime")
QUARANTINE_DIR = os.path.join(RUNTIME, "quarantine")
SNAPSHOT_DIR = os.path.join(RUNTIME, "snapshots")
STORE_DIR = os.path.join(SNAPSHOT_DIR, "_store")
# ★轮快照配置（`ui 0.2.85`）：`{"groupContent": bool}` —— 群聊 / 工作流的轮快照
#   默认**只记路径+哈希**（不占磁盘、不能回滚）；打开后连内容一起进仓 → 能一键退回。
TURN_CFG_FILE = os.path.join(RUNTIME, "state", "_turnsnap.json")


def turn_config() -> dict:
    """读轮快照配置（文件坏了 / 没有 → 全默认，绝不抛）。"""
    try:
        with open(TURN_CFG_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:                             # noqa: BLE001
        return {}


def set_turn_config(**kw) -> dict:
    """合并写轮快照配置（只认 bool 键；写失败不抛）。"""
    cur = turn_config()
    for k, v in (kw or {}).items():
        if isinstance(v, bool):
            cur[str(k)] = v
    try:
        os.makedirs(os.path.dirname(TURN_CFG_FILE), exist_ok=True)
        with open(TURN_CFG_FILE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=2)
    except Exception:                             # noqa: BLE001
        pass
    return cur


def turn_with_content(group: bool = True) -> bool:
    """这一轮的轮快照要不要带内容（群聊 / 工作流共用这个开关）。"""
    cfg = turn_config()
    if group:
        return bool(cfg.get("groupContent"))
    return bool(cfg.get("panelContent"))

# 与编码模式同一套忽略规则（另外把**我们自己的**两个目录也排除，免得自吞）
SKIP_DIRS = set(fsview.SKIP_DIRS) | {"quarantine", "snapshots"}

MAX_FILES = 20000                       # 单次扫描的文件数上限（保护）
MAX_HASH_BYTES = 8 * 1024 * 1024        # 超过就不读内容：只按 size 判（标 big）
MAX_BLOB_BYTES = 8 * 1024 * 1024        # 单文件进内容仓的上限
MAX_DIFF_TEXT = 1024 * 1024             # 行级 diff 的文本上限
KEEP_SNAPSHOTS = 40                     # 每个 scope 保留多少次快照
MAX_STORE_BYTES = 2 * 1024 * 1024 * 1024  # ★内容仓硬上限（超了就丢最旧的快照来回收）
MIN_SNAPSHOTS = 5                       # 但每个 scope 至少留这么多次（别把「撤销这一轮」弄没了）

MAX_QUARANTINE_FILE = 64 * 1024 * 1024  # 单个文件隔离上限
MAX_QUARANTINE_BYTES = 512 * 1024 * 1024  # 一次隔离总量上限
MAX_QUARANTINE_ITEMS = 400              # 一次隔离的目标数上限

# ★低风险删除（`ui 0.2.95`）：只有评分 ≤ maxStar 的「可再生成」目标才会「先隔离再自动放行」。
#   保留期到了以后，**只自动清"安全项"**（原件已恢复 / 本来就是可再生成的），
#   git 跟踪 / 无法判定的一律 hold（永久保留 + 每天提醒）。
QUAR_CFG_FILE = os.path.join(RUNTIME, "state", "_quarantine.json")
QUAR_CFG_DEFAULT = {"enabled": True, "maxStar": 1, "days": 7,
                    "remindHours": 24, "cleanup": True}
# 自动隔离时最多复制这么多内容；超过就只留「清单墓碑」（可再生目录如 node_modules 不该占磁盘）
AUTO_COPY_MAX_BYTES = 64 * 1024 * 1024


def quarantine_config() -> dict:
    """读忏悔室策略（文件坏了 / 没有 → 全默认，绝不抛）。"""
    cfg = dict(QUAR_CFG_DEFAULT)
    try:
        with open(QUAR_CFG_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict):
            if "enabled" in d:
                cfg["enabled"] = bool(d.get("enabled"))
            if "cleanup" in d:
                cfg["cleanup"] = bool(d.get("cleanup"))
            try:
                cfg["maxStar"] = max(1, min(2, int(d.get("maxStar") or 1)))
            except Exception:                          # noqa: BLE001
                pass
            try:
                cfg["days"] = max(1, min(90, int(d.get("days") or 7)))
            except Exception:                          # noqa: BLE001
                pass
            try:
                cfg["remindHours"] = max(0, min(168, int(d.get("remindHours") if d.get("remindHours") is not None else 24)))
            except Exception:                          # noqa: BLE001
                pass
    except Exception:                                  # noqa: BLE001
        pass
    return cfg


def set_quarantine_config(**kw) -> dict:
    """合并写忏悔室策略（越界值归一；写失败不抛，返回当前值）。"""
    cur = quarantine_config()
    for k, v in (kw or {}).items():
        if k in ("enabled", "cleanup") and isinstance(v, bool):
            cur[k] = v
        elif k == "maxStar":
            try:
                cur[k] = max(1, min(2, int(v)))
            except Exception:                          # noqa: BLE001
                pass
        elif k == "days":
            try:
                cur[k] = max(1, min(90, int(v)))
            except Exception:                          # noqa: BLE001
                pass
        elif k == "remindHours":
            try:
                cur[k] = max(0, min(168, int(v)))
            except Exception:                          # noqa: BLE001
                pass
    try:
        os.makedirs(os.path.dirname(QUAR_CFG_FILE), exist_ok=True)
        tmp = QUAR_CFG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, QUAR_CFG_FILE)
    except Exception:                                  # noqa: BLE001
        pass
    return cur


_LOCK = threading.RLock()               # 保护"写清单 + 裁剪 + 回收"（可能被并发调用）


# ---------------------------------------------------------------- 小工具

def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _sha1_file(path: str, limit: int = MAX_HASH_BYTES):
    """流式哈希；超过 limit 只读前 limit 字节并标 big（同大小的巨文件改动检测不到 —— 如实标注）。"""
    h = hashlib.sha1()
    total = 0
    big = False
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(262144)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    big = True
                    break
                h.update(chunk)
    except OSError:
        return "", False
    if big:
        return "big:%d" % total, True
    return h.hexdigest(), False


def slug(name, fallback: str = "default") -> str:
    """把 scope 名压成安全的**单段**目录名（中文也留，只挡路径分隔符与怪字符）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", str(name or "")).strip(" .")
    s = s[:80]
    return s or fallback


def _now_tag() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.localtime()) + "-%03d" % (int(time.time() * 1000) % 1000)


def _inside(path: str, parent: str) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    q = os.path.normcase(os.path.realpath(parent))
    return p == q or p.startswith(q + os.sep)


def _is_protected(path: str) -> bool:
    """不能拿我们自己（忏悔室 / 快照仓）当操作对象 —— 会自吞。"""
    return _inside(path, QUARANTINE_DIR) or _inside(path, SNAPSHOT_DIR)


def _drive_root(path: str) -> bool:
    p = os.path.abspath(path)
    return os.path.dirname(p) == p          # C:\ 这种


def _rt(*parts) -> str:
    return os.path.join(RUNTIME, *parts)


# ---------------------------------------------------------------- 1. 快照

def iter_files(root: str, ignore=None):
    """递归列文件（跳过 SKIP_DIRS）。yield (abs_path, rel_path)。

    ⚠ 用 `os.scandir` 而不是 `os.walk`，并把每个条目各自 try/except —— 一个坏目录不拖垮整棵树。
    """
    skip = set(ignore) if ignore is not None else SKIP_DIRS
    r = os.path.realpath(root)
    stack = [(r, "")]
    n = 0
    while stack:
        d, relbase = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for ent in it:
                try:
                    if ent.is_dir(follow_symlinks=False):
                        if ent.name in skip:
                            continue
                        rel = os.path.join(relbase, ent.name) if relbase else ent.name
                        stack.append((ent.path, rel))
                    elif ent.is_file(follow_symlinks=False):
                        rel = os.path.join(relbase, ent.name) if relbase else ent.name
                        n += 1
                        if n > MAX_FILES:
                            return
                        yield ent.path, rel
                except OSError:
                    continue


def scan(root: str, cache=None, ignore=None) -> dict:
    """扫出一份「树清单」：`{rel: {size, mtime, hash, big?}}`。

    `cache` = 上次的 `files` 字典；`mtime` + `size` 都没变 → **直接沿用上次的 hash**（零重算）。
    实测（真实工作区 358 文件 / 103 MB）：冷扫 ~0.32s、热扫 ~0.009s。
    """
    if not root or not os.path.isdir(root):
        raise ValueError("根目录不存在：%s" % (root or "(空)"))
    r = os.path.realpath(root)
    prev = cache if isinstance(cache, dict) else {}
    files = {}
    rehash = 0
    for abs_p, rel in iter_files(r, ignore):
        try:
            st = os.stat(abs_p)
        except OSError:
            continue
        size = st.st_size
        mtime = int(st.st_mtime)
        # ★ 缓存判定用 **纳秒**（`mtime` 只到秒）：否则"同一秒内的同大小改写"会被当成没变。
        mtime_ns = int(getattr(st, "st_mtime_ns", 0) or 0)
        key = rel.replace("\\", "/")
        old = prev.get(key) or prev.get(rel)
        same = (old and old.get("size") == size and old.get("hash")
                and old.get("mtime_ns") == mtime_ns)
        if same:
            files[key] = {"size": size, "mtime": mtime, "mtime_ns": mtime_ns,
                          "hash": old["hash"], "big": bool(old.get("big"))}
            continue
        h, big = _sha1_file(abs_p, MAX_HASH_BYTES)
        rehash += 1
        files[key] = {"size": size, "mtime": mtime, "mtime_ns": mtime_ns, "hash": h, "big": big}
    return {"root": r, "taken_at": time.time(), "count": len(files),
            "rehash": rehash, "files": files}


def diff_manifests(base: dict, cur: dict) -> dict:
    """两份树清单的差异：added / deleted / changed（+ 同 hash 的 renamed 启发式）。"""
    a = (base or {}).get("files") or {}
    b = (cur or {}).get("files") or {}
    added, deleted, changed = [], [], []
    for rel, info in b.items():
        o = a.get(rel)
        if o is None:
            added.append({"path": rel, "size": info.get("size")})
        elif o.get("hash") != info.get("hash"):
            changed.append({"path": rel, "size": info.get("size"),
                            "size_before": o.get("size"), "binary": bool(info.get("big"))})
    for rel, info in a.items():
        if rel not in b:
            deleted.append({"path": rel, "size": info.get("size")})
    # 重命名启发式：同名 hash 一增一删 → 认为是移动
    renamed = []
    if added and deleted:
        by_hash = {}
        for d in deleted:
            h = (a.get(d["path"]) or {}).get("hash")
            if h:
                by_hash.setdefault(h, []).append(d["path"])
        keep_added, keep_deleted = [], list(deleted)
        for ad in added:
            h = (b.get(ad["path"]) or {}).get("hash")
            if h and by_hash.get(h):
                src = by_hash[h].pop(0)
                renamed.append({"from": src, "to": ad["path"]})
                keep_deleted = [d for d in keep_deleted if d["path"] != src]
            else:
                keep_added.append(ad)
        added, deleted = keep_added, keep_deleted
    return {"added": added, "deleted": deleted, "changed": changed, "renamed": renamed,
            "stats": {"added": len(added), "deleted": len(deleted),
                      "changed": len(changed), "renamed": len(renamed),
                      "base_at": (base or {}).get("taken_at"),
                      "cur_at": (cur or {}).get("taken_at")}}


# ---------------------------------------------------------------- 内容仓（blob store）

def blob_path(sha: str) -> str:
    return os.path.join(STORE_DIR, sha[:2], sha)


def has_blob(sha: str) -> bool:
    return bool(sha) and os.path.isfile(blob_path(sha))


def put_blob(data: bytes) -> str:
    h = _sha1(data)
    p = blob_path(h)
    if not os.path.isfile(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp%d" % os.getpid()
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, p)
    return h


def get_blob(sha: str):
    try:
        with open(blob_path(sha), "rb") as fh:
            return fh.read()
    except OSError:
        return None


def store_file(path: str):
    """把文件内容放进内容仓（太大就跳过）。返回 sha 或 None。"""
    try:
        if os.path.getsize(path) > MAX_BLOB_BYTES:
            return None
        with open(path, "rb") as fh:
            return put_blob(fh.read())
    except OSError:
        return None


# ---------------------------------------------------------------- 快照落盘

def _scope_dir(scope: str) -> str:
    return os.path.join(SNAPSHOT_DIR, slug(scope))


def list_snapshots(scope: str) -> list:
    """这个 scope 的快照（新 → 旧）。"""
    d = _scope_dir(scope)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for nm in names:
        if not nm.endswith(".json") or nm.startswith("_"):
            continue
        try:
            with open(os.path.join(d, nm), "r", encoding="utf-8") as fh:
                js = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        out.append({"ts": nm[:-5], "taken_at": js.get("taken_at"), "root": js.get("root"),
                    "label": js.get("label") or "", "count": js.get("count") or 0,
                    "with_content": bool(js.get("with_content"))})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def latest_manifest(scope: str):
    lst = list_snapshots(scope)
    if not lst:
        return None
    return load_manifest(scope, lst[0]["ts"])


def load_manifest(scope: str, ts: str):
    p = os.path.join(_scope_dir(scope), "%s.json" % ts)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def take_snapshot(root: str, scope: str, label: str = "", cache=None,
                  with_content: bool = True, keep: int = KEEP_SNAPSHOTS) -> dict:
    """给某个工作区落一次快照。`with_content=True` 时把文件内容也进内容仓（增量、天然去重）。

    返回 `{ts, path, count, rehash, stored, root, scope}`。
    """
    man = scan(root, cache=cache)
    stored = 0
    if with_content:
        r = man["root"]
        for rel, info in man["files"].items():
            if info.get("big") or not info.get("hash"):
                continue
            if has_blob(info["hash"]):
                continue                                   # ★ 内容寻址：已有就跳过（增量只写新内容）
            if store_file(os.path.join(r, rel.replace("/", os.sep))) is not None:
                stored += 1
    man["label"] = label
    man["with_content"] = bool(with_content)
    man["scope"] = str(scope)
    with _LOCK:
        d = _scope_dir(scope)
        os.makedirs(d, exist_ok=True)
        ts = _now_tag()
        while os.path.exists(os.path.join(d, "%s.json" % ts)):   # 同一毫秒内并发：让开一个
            ts += "x"
        p = os.path.join(d, "%s.json" % ts)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(man, fh, ensure_ascii=False)
        os.replace(tmp, p)
        _prune(scope, keep)
        _enforce_store_budget(scope)
    return {"ts": ts, "path": p, "count": man["count"], "rehash": man["rehash"],
            "stored": stored, "root": man["root"], "scope": str(scope), "label": label}


def _prune(scope: str, keep: int) -> None:
    lst = list_snapshots(scope)
    dropped = False
    for old in lst[keep:]:
        try:
            os.remove(os.path.join(_scope_dir(scope), "%s.json" % old["ts"]))
            dropped = True
        except OSError:
            pass
    if dropped:
        # ★ 快照是"每轮都落"的 → 裁剪后必须回收**没人引用**的内容块，否则仓会无限长。
        try:
            gc_store()
        except Exception:  # noqa: BLE001 - 回收失败不该影响快照本身
            pass


def drop_snapshot(scope: str, ts: str) -> dict:
    """删掉某一次快照（**永久**）；顺手回收没人引用的内容块。"""
    s = str(ts or "").strip()
    if not s or "/" in s or "\\" in s or ".." in s:
        raise ValueError("非法快照 ts")
    p = os.path.join(_scope_dir(scope), "%s.json" % s)
    if not os.path.isfile(p):
        raise ValueError("找不到这次快照：%s / %s" % (scope, s))
    with _LOCK:
        try:
            os.remove(p)
        except OSError as exc:
            raise ValueError("删除失败：%s" % exc)
        gc_store()
    return {"ok": True, "scope": scope, "ts": s}


def gc_store() -> int:
    """回收**没有任何快照引用**的内容块。返回删掉的个数。

    只在裁剪 / 删快照时调用（走一遍所有清单收集 hash → 删多出来的块）。
    """
    refs = set()
    try:
        scopes = os.listdir(SNAPSHOT_DIR)
    except OSError:
        return 0
    for sc in scopes:
        if sc == "_store":
            continue
        d = os.path.join(SNAPSHOT_DIR, sc)
        if not os.path.isdir(d):
            continue
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for nm in names:
            if not nm.endswith(".json"):
                continue
            try:
                with open(os.path.join(d, nm), "r", encoding="utf-8") as fh:
                    js = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
            for info in (js.get("files") or {}).values():
                h = info.get("hash")
                if h and not str(h).startswith("big:"):
                    refs.add(str(h))
    removed = 0
    for dp, _dn, fns in os.walk(STORE_DIR):
        for f in fns:
            if f in refs:
                continue
            try:
                os.remove(os.path.join(dp, f))
                removed += 1
            except OSError:
                pass
    return removed


def store_bytes() -> int:
    """内容仓当前占用（字节）。"""
    t = 0
    for dp, _dn, fns in os.walk(STORE_DIR):
        for f in fns:
            try:
                t += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return t


def _enforce_store_budget(scope: str) -> int:
    """★内容仓硬上限：超了就**丢最旧的快照 + 回收**，直到降回上限之下。

    为什么必须有：快照是「每轮都落」的 —— 只靠 `KEEP_SNAPSHOTS` 限次数，遇上"每次改的都是大文件"
    仍可能长到几个 GB。这里给一个**总量天花板**兜底；每个 scope 至少留 `MIN_SNAPSHOTS` 次。
    返回被额外丢掉的快照数。
    """
    try:
        if store_bytes() <= MAX_STORE_BYTES:
            return 0
    except Exception:  # noqa: BLE001
        return 0
    dropped = 0
    lst = list_snapshots(scope)
    while len(lst) > MIN_SNAPSHOTS and store_bytes() > MAX_STORE_BYTES:
        oldest = lst.pop()
        try:
            os.remove(os.path.join(_scope_dir(scope), "%s.json" % oldest["ts"]))
            dropped += 1
        except OSError:
            break
        gc_store()
    return dropped


def line_diff(base_sha: str, cur_sha: str, rel: str, root: str = "") -> dict:
    """行级 diff：`base_sha` 的内容取自内容仓，`cur_sha` 缺失时**回落到磁盘上的当前文件**。

    （当前内容通常没进仓 —— 只有"快照那一刻"的版本才会被 `put_blob`。）
    二进制 / 太大 / 缺版本 → 只报状态，不硬凑。
    """
    import difflib
    a = get_blob(base_sha) if base_sha else None
    b = get_blob(cur_sha) if cur_sha else None
    if b is None and root and rel:
        try:
            with open(os.path.join(root, rel.replace("/", os.sep)), "rb") as fh:
                b = fh.read()
        except OSError:
            b = None
    if a is None:
        return {"ok": False, "reason": "内容仓里没有旧版本（这次快照没存内容，或文件太大）"}
    if b is None:
        return {"ok": False, "reason": "拿不到当前内容（文件已不存在）"}
    if len(a) > MAX_DIFF_TEXT or len(b) > MAX_DIFF_TEXT:
        return {"ok": False, "reason": "文件太大，不做行级 diff"}
    if b"\x00" in a[:8192] or b"\x00" in b[:8192]:
        return {"ok": False, "reason": "二进制文件，只能标「已改」"}
    at = a.decode("utf-8", "replace").splitlines()
    bt = b.decode("utf-8", "replace").splitlines()
    lines = list(difflib.unified_diff(at, bt, fromfile="a/" + rel, tofile="b/" + rel, lineterm=""))
    add = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    dele = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return {"ok": True, "lines": lines[:4000], "additions": add, "deletions": dele}


def undo_snapshot(scope: str, ts: str, paths=None, quarantine: bool = True) -> dict:
    """把工作区**回退到那次快照**：被删的恢复、被改的写回旧内容、被新增的删掉。

    ⚠ 删「新增」文件前会**先自动进忏悔室**（所以"撤销"本身也可逆）。
    `paths` = 只处理这些相对路径（默认全部）。
    """
    man = load_manifest(scope, ts)
    if not man:
        raise ValueError("找不到这次快照：%s / %s" % (scope, ts))
    if not man.get("with_content"):
        raise ValueError("这次快照没存内容，撤不了")
    root = man["root"]
    cur = scan(root)
    d = diff_manifests(man, cur)
    want = set(str(p).replace("\\", "/") for p in paths) if paths else None

    def _want(rel):
        return want is None or rel.replace("\\", "/") in want

    actions = []
    restored, removed, skipped = 0, 0, 0
    added_paths = [x["path"] for x in d["added"] if _want(x["path"])]
    if added_paths and quarantine:
        try:
            quarantine_paths(root, [os.path.join(root, r.replace("/", os.sep)) for r in added_paths],
                             scope=scope, note="撤销 %s：先隔离「新增」的文件" % ts, cmd="")
        except Exception:  # noqa: BLE001
            pass
    for item in d["deleted"] + d["changed"]:
        rel = item["path"]
        if not _want(rel):
            continue
        info = man["files"].get(rel) or {}
        data = get_blob(info.get("hash") or "")
        if data is None:
            actions.append({"path": rel, "action": "skip", "reason": "内容仓里没有这一版"})
            skipped += 1
            continue
        dest = os.path.join(root, rel.replace("/", os.sep))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as fh:
                fh.write(data)
            actions.append({"path": rel, "action": "restore"})
            restored += 1
        except OSError as exc:
            actions.append({"path": rel, "action": "skip", "reason": str(exc)})
            skipped += 1
    for rel in added_paths:
        dest = os.path.join(root, rel.replace("/", os.sep))
        try:
            if os.path.isdir(dest) and not os.path.islink(dest):
                shutil.rmtree(dest, ignore_errors=True)
            elif os.path.exists(dest):
                os.remove(dest)
            else:
                continue
            actions.append({"path": rel, "action": "remove"})
            removed += 1
        except OSError as exc:
            actions.append({"path": rel, "action": "skip", "reason": str(exc)})
            skipped += 1
    return {"ok": True, "scope": scope, "ts": ts, "root": root,
            "restored": restored, "removed": removed, "skipped": skipped,
            "actions": actions[:500], "diff": d["stats"]}


# ---------------------------------------------------------------- 2. 忏悔室

def _q_dir(qid: str) -> str:
    return os.path.join(QUARANTINE_DIR, qid)


def _copy_target(src: str, dest: str) -> str:
    """复制文件 / 目录（保留时间戳）。目录用 copytree（不跟随符号链接）。"""
    if os.path.isdir(src) and not os.path.islink(src):
        shutil.copytree(src, dest, symlinks=True,
                        ignore=shutil.ignore_patterns(*sorted(SKIP_DIRS)))
        return "dir"
    shutil.copy2(src, dest, follow_symlinks=False)
    return "file"


def quarantine_paths(root: str, targets, scope="default", note="", cmd="",
                     auto=False, scores=None, content=True, kind="delete") -> dict:
    """把 `targets`（**绝对路径**）复制进 `runtime/quarantine/<scope>/<ts>/`。

    返回记录（也落 `_manifest.json`）：`{id, ts, scope, note, cmd, root, items[], bytes, skipped[]}`。
    上限：单文件 64 MB / 单次总量 512 MB / 目标数 400 —— 超了**如实记进 `skipped`**（不假装成功）。

    ★`kind`（`ui 0.2.99` 起）：`delete` = 删除前副本；`modify` = 修改前旧版。
      忏悔室时间线按它显示「删除前」/「修改前」。

    ★`auto=True`（低风险删除自动隔离，`ui 0.2.95`）：
      * 只复制 `AUTO_COPY_MAX_BYTES`（64 MB）以内的内容；超过就只写「清单墓碑」
        （`tombstone=True`、`stored=""`、`bytes=0`）—— `node_modules` 这种再生成目录不该占磁盘；
      * 记录里带 `auto / expires_at / scores`，供保留期清理与面板显示。
    """
    r = os.path.realpath(root) if root and os.path.isdir(root) else ""
    kind_s = "modify" if str(kind or "").lower() == "modify" else "delete"
    scope_s = slug(scope)
    ts = _now_tag()
    qid = os.path.join(scope_s, ts)
    base = _q_dir(qid)
    files_dir = os.path.join(base, "files")
    items, skipped = [], []
    total = 0

    # 归一去重：父目录已收，就不必再收子项
    norm = []
    for t in (targets or []):
        try:
            p = os.path.realpath(str(t))
        except Exception:  # noqa: BLE001
            continue
        if not p or not os.path.exists(p):
            skipped.append({"src": str(t), "reason": "不存在"})
            continue
        if _is_protected(p):
            skipped.append({"src": p, "reason": "是忏悔室/快照目录本身"})
            continue
        if _drive_root(p):
            skipped.append({"src": p, "reason": "盘符根目录（范围过大）"})
            continue
        norm.append(p)
    norm = sorted(set(norm), key=len)
    keep = []
    for p in norm:
        if any(_inside(p, k) and p != k for k in keep):
            continue
        keep.append(p)

    if len(keep) > MAX_QUARANTINE_ITEMS:
        skipped.extend({"src": p, "reason": "超过单次目标数上限"} for p in keep[MAX_QUARANTINE_ITEMS:])
        keep = keep[:MAX_QUARANTINE_ITEMS]

    def _size(p, cap=None):
        t = 0
        try:
            if os.path.isfile(p):
                return os.path.getsize(p)
            for dp, _dn, fns in os.walk(p):
                for f in fns:
                    try:
                        t += os.path.getsize(os.path.join(dp, f))
                    except OSError:
                        continue
                    if cap and t > cap:
                        return t
            return t
        except OSError:
            return t

    sizes = {}
    tombstone = False
    if auto and content:
        try:
            cap = AUTO_COPY_MAX_BYTES * 4
            est = 0
            for p in keep:
                sizes[p] = _size(p, cap)
                est += sizes[p]
            if est > AUTO_COPY_MAX_BYTES:
                tombstone = True
                content = False
        except Exception:  # noqa: BLE001
            tombstone = True
            content = False

    os.makedirs(files_dir, exist_ok=True)
    for i, src in enumerate(keep):
        rel = (os.path.relpath(src, r).replace("\\", "/") if r and _inside(src, r) else "")
        if tombstone:
            sz = sizes.get(src) or _size(src, AUTO_COPY_MAX_BYTES)
            items.append({"src": src, "stored": "", "kind": ("dir" if os.path.isdir(src) else "file"),
                          "size": sz, "rel": rel, "tombstone": True, "hash": ""})
            continue
        name = "%04d__%s" % (i + 1, os.path.basename(src) or "item")
        dest = os.path.join(files_dir, name)
        item_hash = ""
        try:
            if os.path.isfile(src) and not os.path.islink(src):
                item_hash = _sha1_file(src)[0] or ""
        except Exception:  # noqa: BLE001
            item_hash = ""
        try:
            if os.path.isfile(src) and not os.path.islink(src):
                sz = os.path.getsize(src)
                if sz > MAX_QUARANTINE_FILE:
                    skipped.append({"src": src, "reason": "文件太大（>%d MB）" % (MAX_QUARANTINE_FILE // 1048576)})
                    continue
                if total + sz > MAX_QUARANTINE_BYTES:
                    skipped.append({"src": src, "reason": "超过单次总量上限"})
                    continue
            elif os.path.isdir(src) and not os.path.islink(src):
                try:
                    sz = sum(os.path.getsize(os.path.join(dp, f))
                             for dp, _dn, fns in os.walk(src) for f in fns)
                except OSError:
                    sz = 0
                if total + sz > MAX_QUARANTINE_BYTES:
                    skipped.append({"src": src, "reason": "目录太大（超过单次总量上限）"})
                    continue
            else:
                sz = 0
            kind = _copy_target(src, dest)
            items.append({"src": src, "stored": name, "kind": kind, "size": sz, "rel": rel,
                          "hash": item_hash if os.path.isfile(src) else ""})
            total += sz
        except OSError as exc:
            skipped.append({"src": src, "reason": "复制失败：%s" % exc})

    taken = time.time()
    cfg = quarantine_config()
    safe_scores = []
    for sc in (scores or []):
        if not isinstance(sc, dict):
            continue
        safe_scores.append({
            "path": str(sc.get("path") or "")[:500],
            "rel": str(sc.get("rel") or "")[:500],
            "stars": int(sc.get("stars") or 0),
            "score": int(sc.get("score") or 0),
            "reasons": [str(x)[:120] for x in (sc.get("reasons") or [])[:4]],
        })
    rec = {"id": qid.replace("\\", "/"), "ts": ts, "scope": scope_s, "note": str(note or "")[:600],
           "cmd": " ".join(str(cmd or "").split())[:600], "root": r, "kind": kind_s,
           "taken_at": taken, "bytes": total,
           "auto": bool(auto), "tombstone": bool(tombstone),
           "expires_at": taken + float(cfg.get("days") or 7) * 86400.0,
           "scores": safe_scores,
           "items": items, "skipped": skipped}
    with open(os.path.join(base, "_manifest.json"), "w", encoding="utf-8", newline="\n") as fh:
        json.dump(rec, fh, ensure_ascii=False, indent=1)
    return rec


def list_quarantine(limit: int = 60) -> list:
    """忏悔室记录（新 → 旧）。"""
    out = []
    try:
        scopes = os.listdir(QUARANTINE_DIR)
    except OSError:
        return out
    for sc in scopes:
        d = os.path.join(QUARANTINE_DIR, sc)
        if not os.path.isdir(d):
            continue
        try:
            tags = os.listdir(d)
        except OSError:
            continue
        for tag in tags:
            mp = os.path.join(d, tag, "_manifest.json")
            if not os.path.isfile(mp):
                continue
            try:
                with open(mp, "r", encoding="utf-8") as fh:
                    js = json.load(fh)
            except Exception:  # noqa: BLE001
                continue
            out.append({
                "id": (js.get("id") or ("%s/%s" % (sc, tag))).replace("\\", "/"),
                "ts": js.get("ts") or tag, "scope": js.get("scope") or sc,
                "taken_at": js.get("taken_at") or 0, "note": js.get("note") or "",
                "cmd": js.get("cmd") or "", "root": js.get("root") or "",
                "kind": js.get("kind") or "delete",
                "count": len(js.get("items") or []), "bytes": js.get("bytes") or 0,
                "missed": len(js.get("skipped") or []),
                "auto": bool(js.get("auto")), "tombstone": bool(js.get("tombstone")),
                "expires_at": js.get("expires_at") or 0,
                "max_star": max([int(x.get("stars") or 0) for x in (js.get("scores") or [])] or [0]),
            })
    out.sort(key=lambda x: x["taken_at"], reverse=True)
    return out[:max(1, int(limit))]


def load_quarantine(qid: str) -> dict:
    p = os.path.join(_q_dir(qid.replace("/", os.sep)), "_manifest.json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:  # noqa: BLE001
        return {}


def restore_quarantine(qid: str, srcs=None, override: bool = False) -> dict:
    """把忏悔室里的东西**放回原处**（`srcs` 为空 = 全部）。

    目标已存在 → 除非 `override=True`，否则记为 `conflict`（**不静默覆盖**）。
    """
    rec = load_quarantine(qid)
    if not rec:
        raise ValueError("找不到隔离记录：%s" % qid)
    base = _q_dir(qid.replace("/", os.sep))
    want = set(str(s) for s in srcs) if srcs else None
    restored, conflict, missing, failed, tombstone = 0, 0, 0, 0, 0
    actions = []
    for item in (rec.get("items") or []):
        src = item.get("src") or ""
        if want is not None and src not in want:
            continue
        if item.get("tombstone"):
            actions.append({"src": src, "action": "tombstone",
                            "reason": "自动隔离时只记了清单（可再生文件没复制内容，无法恢复）"})
            tombstone += 1
            continue
        stored = os.path.join(base, "files", item.get("stored") or "")
        if not os.path.exists(stored):
            actions.append({"src": src, "action": "missing", "reason": "隔离副本不见了"})
            missing += 1
            continue
        if os.path.exists(src) and not override:
            actions.append({"src": src, "action": "conflict", "reason": "目标已存在（未覆盖）"})
            conflict += 1
            continue
        try:
            parent = os.path.dirname(src)
            if parent:
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(stored) and not os.path.islink(stored):
                if os.path.exists(src):
                    shutil.rmtree(src, ignore_errors=True)
                shutil.copytree(stored, src, symlinks=True)
            else:
                if os.path.isdir(src):
                    shutil.rmtree(src, ignore_errors=True)
                elif os.path.exists(src):
                    os.remove(src)
                shutil.copy2(stored, src, follow_symlinks=False)
            actions.append({"src": src, "action": "restore"})
            restored += 1
        except OSError as exc:
            actions.append({"src": src, "action": "failed", "reason": str(exc)})
            failed += 1
    return {"ok": True, "id": qid, "restored": restored, "conflict": conflict,
            "missing": missing, "failed": failed, "tombstone": tombstone,
            "actions": actions[:500]}


def drop_quarantine(qid: str) -> dict:
    """删掉一条隔离记录（**永久**）—— 记录清空后才真删；带 `..` 之类一律拒。"""
    s = str(qid or "").replace("\\", "/").strip("/")
    if not s or ".." in s.split("/"):
        raise ValueError("非法记录 id")
    d = _q_dir(s.replace("/", os.sep))
    if not _inside(d, QUARANTINE_DIR) or d == os.path.realpath(QUARANTINE_DIR):
        raise ValueError("越界")
    if os.path.isfile(os.path.join(d, "_manifest.json")):
        shutil.rmtree(d, ignore_errors=True)
    return {"ok": True, "id": s, "removed": not os.path.exists(d)}


def _plan_entry(it: dict, state: str, hours_left=None) -> dict:
    return {"id": it.get("id"), "ts": it.get("ts"), "scope": it.get("scope"),
            "taken_at": it.get("taken_at") or 0, "count": it.get("count") or 0,
            "bytes": it.get("bytes") or 0, "note": it.get("note") or "",
            "auto": bool(it.get("auto")), "tombstone": bool(it.get("tombstone")),
            "expires_at": it.get("expires_at") or 0,
            "max_star": it.get("max_star") or 0, "state": state,
            "hours_left": round(float(hours_left), 1) if hours_left is not None else None}


def _record_safety(rec: dict, cfg: dict) -> dict:
    """判断一条隔离记录能不能"安全清掉"（**只有 auto 记录会走到这里**）。

    安全 = 清掉这份备份**不会造成额外损失**：
      * 原件还在（已恢复 / 没删成）→ 备份多余；
      * 本来就是 ★≤阈值 的可再生成目标（我们放行时就认了"可以不回滚"）；
      * 清单墓碑（内容当时就没复制）。
    风险 = 原件已不在、且它是 **git 跟踪文件** 或 **星级不明**（无法确认可再生成）→ **hold**。
    """
    reasons = []
    stars = {}
    for x in (rec.get("scores") or []):
        try:
            stars[str(x.get("path") or "")] = int(x.get("stars") or 0)
        except Exception:                              # noqa: BLE001
            continue
    max_star = int(cfg.get("maxStar") or 1)
    qbase = _q_dir(str(rec.get("id") or "").replace("/", os.sep))
    unknown = []
    for item in (rec.get("items") or []):
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "")
        if not src or item.get("tombstone"):
            continue
        if os.path.exists(src):
            continue                                   # 原件还在 → 备份是多余的
        stored = str(item.get("stored") or "")
        if not stored or not os.path.exists(os.path.join(qbase, "files", stored)):
            reasons.append("原件与副本都不在了：%s" % src)
            continue
        st = stars.get(src)
        if st is not None and st <= max_star:
            continue                                   # 放行时就认了"可再生成"
        unknown.append(src)

    tracked = {}
    if unknown and rec.get("root"):
        try:
            import file_rank                            # 延迟导入：snapshots ← file_rank 会成环
            gs = file_rank.git_state(str(rec.get("root")), unknown)
            tracked = {p: bool((gs[i] or {}).get("tracked")) for i, p in enumerate(unknown)}
        except Exception:                              # noqa: BLE001
            tracked = {}
    for p in unknown:
        if tracked.get(p):
            reasons.append("git 跟踪文件已不在：%s" % p)
        else:
            reasons.append("无法确认可再生成：%s" % p)
    return {"safe": not reasons, "reasons": reasons[:10]}


def quarantine_plan(now=None) -> dict:
    """保留期计划：`keep`（还早）/ `soon`（进入提醒窗口）/ `due`（到期可清）/ `hold`（到期但不能清）。

    ⚠ **手动隔离的记录永远不进 due**（`auto=False`）—— 只有"低风险删除自动隔离"的才定时清。
    """
    cfg = quarantine_config()
    now = float(now or time.time())
    days = float(cfg.get("days") or 7)
    remind_h = float(cfg.get("remindHours") if cfg.get("remindHours") is not None else 24)
    total_s = days * 86400.0
    soon_from = max(0.0, total_s - max(0.0, remind_h) * 3600.0)

    keep, soon, due, hold = [], [], [], []
    for it in list_quarantine(limit=10000):
        if not it.get("auto"):
            keep.append(_plan_entry(it, "manual"))
            continue
        taken = float(it.get("taken_at") or 0)
        age = max(0.0, now - taken)
        if age < soon_from:
            left = (total_s - age) / 3600.0
            keep.append(_plan_entry(it, "keep", left))
        elif age < total_s:
            left = (total_s - age) / 3600.0
            soon.append(_plan_entry(it, "soon", left))
        else:
            rec = load_quarantine(str(it.get("id") or ""))
            safe = _record_safety(rec, cfg)
            entry = _plan_entry(it, "due" if safe["safe"] else "hold", 0)
            if safe["safe"]:
                due.append(entry)
            else:
                entry["hold_reasons"] = safe["reasons"]
                hold.append(entry)
    nxt = None
    for e in keep + soon:
        if not e.get("auto"):
            continue
        t = float(e.get("expires_at") or 0)
        if t and (nxt is None or t < nxt):
            nxt = t
    return {"ok": True, "config": cfg, "now": now,
            "keep": keep, "soon": soon, "due": due, "hold": hold,
            "counts": {"keep": len(keep), "soon": len(soon),
                       "due": len(due), "hold": len(hold)},
            "next_expire_at": nxt,
            "bytes_due": sum(int(e.get("bytes") or 0) for e in due)}


def cleanup_quarantine(now=None, dry_run=False, ids=None) -> dict:
    """按保留期清理 `due` 记录（**只清安全项**；`hold` 永远留着）。

    * `dry_run=True` 只算不动；
    * `ids` 非空 = 只清这几条（后台守护用：到期后先提醒、宽限期过了才清）；
    * `ids=None` = 清全部 `due`（面板上用户手动点「立即清理」时用）。
    """
    plan = quarantine_plan(now)
    cfg = plan["config"]
    want = {str(x) for x in ids} if ids else None
    due = [e for e in plan["due"] if e.get("id") and (want is None or str(e.get("id")) in want)]
    ids_due = [str(e.get("id")) for e in due]
    dropped = []
    if not dry_run and cfg.get("cleanup"):
        for qid in ids_due:
            try:
                drop_quarantine(qid)
                dropped.append(qid)
            except Exception:                          # noqa: BLE001
                continue
    return {"ok": True, "dry_run": bool(dry_run), "enabled": bool(cfg.get("cleanup")),
            "ids": ids_due, "dropped": dropped,
            "freed": sum(int(e.get("bytes") or 0) for e in due if e.get("id") in dropped),
            "bytes_due": sum(int(e.get("bytes") or 0) for e in due),
            "held": plan["hold"], "soon": plan["soon"],
            "counts": plan["counts"]}


def stats() -> dict:
    """忏悔室 / 快照仓的总占用（给 UI 显示）。"""
    def _size(p):
        t = 0
        for dp, _dn, fns in os.walk(p):
            for f in fns:
                try:
                    t += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        return t
    return {"quarantine": _size(QUARANTINE_DIR), "store": _size(STORE_DIR),
            "snapshots": _size(SNAPSHOT_DIR), "quarantine_dir": QUARANTINE_DIR,
            "snapshot_dir": SNAPSHOT_DIR,
            "store_limit": MAX_STORE_BYTES, "keep": KEEP_SNAPSHOTS,
            "quarantine_cfg": quarantine_config(),
            "auto_copy_limit": AUTO_COPY_MAX_BYTES}


# ---------------------------------------------------------------- 3. 从命令里猜"要动的东西"

# ⚠ 这是**尽力而为**：解析不出来时如实记进 `unresolved`，不假装保住了。
_SPLIT = re.compile(r"&&|\|\||;|\r?\n|\|")
_TOK = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|(\S+)')
_WIN_ABS = re.compile(r"^[A-Za-z]:[\\/]")
_PATHISH = re.compile(r"[\\/*?]")
_FLAG = re.compile(r"^(--?[A-Za-z]|/[A-Za-z])")
_ENV = re.compile(r"%[^%]+%|\$env:[A-Za-z_][A-Za-z0-9_]*|\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")
_VERB_DESTRUCTIVE = re.compile(
    r"remove-item|\brm\b|\brd\b|rmdir|\bdel\b|\berase\b|move-item|\bmv\b|\bmove\b|"
    r"clear-content|set-content|out-file|copy-item|\bcp\b|robocopy|xcopy", re.I)

# ★代码内嵌删除（Python / Node / .NET）：命令原文里没有 `rm` / `del`，但确实在删文件。
#   只认**带引号的字面路径**；变量 / 拼接一律进 `unresolved`（fail-closed，交给上层弹窗）。
_CODE_DELETE = re.compile(
    r"""(?ix)
    (?:
        shutil\.rmtree | os\.removedirs | os\.remove | os\.unlink
      | (?:fs|fsp)\.(?:rmSync|rm|unlinkSync|unlink|rmdirSync|rmdir)
      | \.(?:rmSync|unlinkSync|rmdirSync)
      | (?:file|directory)\s*\]?\s*::\s*delete
    )
    \s*\(\s*(['"])([^'"]+)\1
    """
)
# `Path('a.log').unlink()`：引号在 `.unlink` 前面，单独抓。
_PATHLIB_UNLINK = re.compile(
    r"""(?ix) path \s* \( \s* (['"]) ([^'"]+) \1 \s* \) \s* \.unlink \s* \( """)


def _tokens(cmd: str) -> list:
    out = []
    for m in _TOK.finditer(cmd or ""):
        if m.group(1) is not None:
            out.append(m.group(1))
        elif m.group(2) is not None:
            out.append(m.group(2))
        else:
            out.append(m.group(3))
    return out


def _resolve(tok: str, root: str):
    """把一个 token 解析成**存在的**绝对路径；解析不出返回 None。"""
    t = _ENV.sub("", tok.strip().strip('"').strip("'"))
    if not t:
        return None
    try:
        t = os.path.expanduser(os.path.expandvars(t))
    except Exception:  # noqa: BLE001
        return None
    cands = []
    if os.path.isabs(t):
        cands.append(t)
    elif root:
        cands.append(os.path.join(root, t))
        cands.append(os.path.join(os.path.dirname(os.path.realpath(root)), t))
    for c in cands:
        if any(ch in c for ch in "*?"):
            try:
                hits = _glob.glob(c)
            except Exception:  # noqa: BLE001
                hits = []
            if hits:
                return hits
        elif os.path.exists(c):
            return [os.path.realpath(c)]
    return None


def guess_targets(cmd: str, root: str) -> dict:
    """从命令原文里猜「它要动什么」（**尽力而为**，返回 `{targets, unresolved}`）。

    * 支持 `Remove-Item -Recurse -Force 'C:\\x\\*'` / `del a.txt` / `rm -rf build` / `rmdir /s /q n` …
    * `powershell -Command "Remove-Item ..."` 这种**套一层**的会递归进去。
    * 通配符会展开；相对路径相对 `root`（以及 `root` 的父目录）解析。
    """
    targets, unresolved, seen = [], [], set()

    def _push(p):
        rp = os.path.realpath(p)
        if rp not in seen:
            seen.add(rp)
            targets.append(rp)

    for seg in _SPLIT.split(cmd or ""):
        toks = _tokens(seg)
        if not toks:
            continue
        for idx, tk in enumerate(toks):
            raw = tk.strip()
            if not raw:
                continue
            if _VERB_DESTRUCTIVE.search(raw) and idx > 0 and (" " in raw or "\\" in raw or "/" in raw):
                # 套在引号里的一整条命令 → 递归（但别把只是"提到动词"的普通路径当命令）
                if _VERB_DESTRUCTIVE.search(raw.split()[0] if raw.split() else ""):
                    sub = guess_targets(raw, root)
                    for p in sub["targets"]:
                        _push(p)
                    unresolved.extend(sub["unresolved"])
                    continue
            if idx == 0:
                continue                                   # 第一个 token 是程序名
            if _FLAG.match(raw) and not _WIN_ABS.match(raw):
                continue                                   # -Recurse / /s 之类
            looks = bool(_PATHISH.search(raw)) or bool(_WIN_ABS.match(raw))
            if not looks and root:
                # 没分隔符也试一次：可能是 root / root 的父目录下真实存在的名字（`del desktop` 这种）
                looks = bool(_resolve(raw, root))
            if not looks:
                continue
            hits = _resolve(raw, root)
            if hits:
                for p in hits:
                    _push(p)
            else:
                unresolved.append(raw[:200])

    # ★代码内嵌删除（Python / Node / .NET）：只认带引号的字面路径，变量 → unresolved
    for pat in (_CODE_DELETE, _PATHLIB_UNLINK):
        for m in pat.finditer(cmd or ""):
            raw = m.group(2)
            hits = _resolve(raw, root)
            if hits:
                for p in hits:
                    _push(p)
            else:
                unresolved.append(raw[:200])
    return {"targets": targets, "unresolved": unresolved}


# ---------------------------------------------------------------- 轮快照（给「群聊派活 / 对比」用）

# scope -> 上次的 files 字典（**只放内存**）：让"每一轮"的快照走热扫（~9ms 而不是 ~0.3s）。
# ⚠ 不存文件内容（`with_content=False`）：这一层只要"**改了哪些文件**"，不要行级 diff / 不要回滚，
#   所以不会像 `0.1.77` 那样把整个工作区镜像进内容仓（用户明确说过那样太重）。
_TURN_CACHE: dict = {}
_TURN_LOCK = threading.RLock()


def take_turn_snapshot(root: str, scope: str, label: str = "",
                       with_content: bool = False) -> dict:
    """给「某一轮」落一次快照。

    `with_content=False`（默认）= **只记路径+哈希**（不带内容，热扫走内存缓存）：
    用来回答「这个人改了哪些文件」，不占磁盘、**不能回滚**。
    `with_content=True` = 连内容一起进内容仓（增量、天然去重）：
    多花一点磁盘，但之后能 `undo_snapshot` **一键退回**（`ui 0.2.85` 起可选）。
    """
    with _TURN_LOCK:
        cache = _TURN_CACHE.get(str(scope))
    res = take_snapshot(root, scope, label=label or "turn", cache=cache,
                        with_content=bool(with_content))
    man = load_manifest(str(scope), res["ts"]) or {}
    with _TURN_LOCK:
        _TURN_CACHE[str(scope)] = man.get("files") or {}
        if len(_TURN_CACHE) > 200:                  # 别无限长（每个 scope 一份字典）
            for k in list(_TURN_CACHE.keys())[:50]:
                _TURN_CACHE.pop(k, None)
    return res


def list_scopes(limit: int = 40) -> list:
    """所有 scope 的「最新一次快照」摘要（审计页「轮快照」列表用）。

    返回 `[{scope, ts, taken_at, root, label, count, with_content}]`，**按时间新→旧**。
    `with_content=True` 的才能一键退回（否则只有清单）。
    """
    out = []
    try:
        names = os.listdir(SNAPSHOT_DIR)
    except OSError:
        return out
    for nm in names:
        if nm.startswith("_"):
            continue
        d = os.path.join(SNAPSHOT_DIR, nm)
        if not os.path.isdir(d):
            continue
        lst = list_snapshots(nm)
        if not lst:
            continue
        one = dict(lst[0])
        one["scope"] = nm
        one["runs"] = len(lst)
        out.append(one)
    out.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return out[:max(1, int(limit or 40))]


def changes_since_latest(root: str, scope: str) -> dict:
    """「上一次轮快照 → 现在」改了哪些文件（文件级；不算行级 diff）。"""
    man = latest_manifest(scope)
    if not man:
        return {"ok": True, "since": None, "files": 0,
                "stats": {"added": 0, "deleted": 0, "changed": 0, "renamed": 0},
                "added": [], "deleted": [], "changed": [], "renamed": []}
    try:
        cur = scan(root, cache=(_TURN_CACHE.get(str(scope)) if _TURN_CACHE else None))
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "files": 0}
    d = diff_manifests(man, cur)
    return {"ok": True, "since": man.get("taken_at"), "root": man.get("root"),
            "files": len(d["added"]) + len(d["deleted"]) + len(d["changed"]) + len(d["renamed"]),
            **d}


def _self_test() -> None:                       # pragma: no cover - 手动跑
    print("[snapshots] quarantine=%s" % QUARANTINE_DIR)
    print("[snapshots] stats=%r" % (stats(),))


if __name__ == "__main__":                      # pragma: no cover
    _self_test()
