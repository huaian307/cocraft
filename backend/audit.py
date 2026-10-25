# -*- coding: utf-8 -*-
r"""★审计链（append-only + 哈希链）—— 引擎无关、纯本机、无账号体系、不联网。

## 为什么要有它

工作台 / 群聊的「审批记录」以前各自落一份 JSON 数组，而**导出是前端从页面内存拼的**
（`frontend/js/20-collab.js` 的 `collabExport()` 只读 `COLLAB.run`）→
改一下 `runtime/state/_orchestrator/runs/*.json` 就能改掉"谁批准了什么"，
既不权威、也无法证明"这份记录没被改过"。

这里把「谁、批准/拒绝了什么、结果如何、隔离到哪」写成一条**不可静默篡改**的链：

    runtime/state/_audit.jsonl      一行一条记录（**只追加**，不重写、不压缩）
    hash = sha256(canonical_json(record 去掉 hash) + prev)

- **只记决定与结果**（人批了 / 拒了 / 超时 / 撤销 / 恢复），**不记成员正文**（正文可能很大或敏感）。
  命令原文、目标路径、隔离记录 id **必须留** —— 「谁批准了什么」要能追到具体那一条，不做盲签。
- **fail-open**：追加失败只写一行日志，**绝不阻断**权限 / 执行流程（护栏产品不能因为日志坏了就卡住用户）。
- **多进程安全**：`server.py`（工作台审批）与 `cocraft_agents.py`（群聊桥）会同时写 →
  用 `msvcrt` 锁一个 `.lock` 文件；拿不到锁就退化成进程内锁（测试 / 非 Windows）。
- **可校验**：`verify()` 逐条重算哈希，并要求每条的 `prev` 能在链内解析到；
  区间导出（按时间/成员筛选）会**跳过**中间记录，所以校验允许"空洞"但要如实报 `gaps`。

## 记录形状（v1）

```jsonc
{
  "v": 1,
  "seq": 1024,                                  // 自增，链内唯一
  "ts": "2026-10-24T20:11:02.123Z",
  "tsu": 1792843862.123,                         // 便于筛选/排序（不改 ts 的可读性）
  "machine": "WS-01",
  "actor":    {"kind": "agent", "id": "codex-node", "name": "yiyi",
               "member": "m1", "role": "implementer", "engine": "codex-node"},
  "approver": {"kind": "user", "id": "local", "name": "owner"},   // 没经人批准就是 null
  "action":   {"kind": "execute", "title": "运行命令", "command": "Remove-Item ...",
               "targets": ["D:\\ws\\dist"], "destructive": true},
  "decision": "once",                            // once|always|reject|confirm|auto|undo|restore|drop
  "result":   "ok",                              // ok|denied|timeout|failed|skipped
  "quarantine": "3/20261024-2011",               // 隔离记录 id（没有就空）
  "source":   "collab",                          // collab|ccagent|snapshot|manual
  "ref":      {"run": "", "member": "", "perm": "", "chat": "", "aid": "", "session": "", "scope": "", "ts": ""},
  "note":     "",
  "prev": "sha256:9f2c…",
  "hash": "sha256:41ab…"
}
```

## 口径（刻意不做的事，别当成 bug）

1. **策略自动放行不入链**：按权限等级 / `trust` 自动放过的读写执行不写（IO 放大；
   而且"谁批准了"这类记录本来就该只收**人的决定**）。破坏性操作**永远**要人确认，所以不进这个口子。
   活动流（`/ccagent/activity`）能看这些自动操作。
2. **不记"正在等"**：只记结论。进程在等待时被杀掉 → 没有记录，这是诚实的（确实没有决定）。
3. **不自动清理**：目前只追加。保留期 / 归档 / 报告是后续步骤（见 `docs/审计-方案.md`）。
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import socket
import threading
import time

try:  # Windows 上用真正的跨进程锁；其它平台退化成线程锁
    import msvcrt  # type: ignore
except Exception:  # noqa: BLE001
    msvcrt = None

try:
    from engines import ROOT, STATE_DIR
except ImportError:  # 以 `backend.audit` 形式加载时（`tools/acp_events_test.py` 那种命名空间包）
    _HERE = os.path.dirname(os.path.abspath(__file__))
    ROOT = os.path.dirname(_HERE)
    STATE_DIR = os.path.join(ROOT, "runtime", "state")

VERSION = 1
GENESIS = "sha256:" + "0" * 64        # 链首的 prev（全零 = 没有前一条）
FORMAT = "cocraft-audit"

AUDIT_FILE = (os.environ.get("COCRAFT_AUDIT_FILE")
              or os.path.join(STATE_DIR, "_audit.jsonl"))
MAX_LINE = 256 * 1024                 # 单条记录上限（防御性；正常一条几百字节）
TAIL_CHUNK = 128 * 1024               # 找链尾时从文件尾部读多少

_LOCK = threading.RLock()
_MACHINE = ""


# ---------------------------------------------------------------- 小工具

def set_file(path: str) -> str:
    """换一个审计文件（**只给测试用**：把落盘隔离到临时目录，别污染真实链）。"""
    global AUDIT_FILE
    AUDIT_FILE = str(path)
    return AUDIT_FILE


def machine() -> str:
    global _MACHINE
    if not _MACHINE:
        try:
            _MACHINE = socket.gethostname() or ""
        except Exception:  # noqa: BLE001
            _MACHINE = ""
    return _MACHINE


def _canonical(obj) -> bytes:
    """规范化 JSON（键排序、无空格、不转义中文）→ 字节。哈希只认这一种序列化。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(body: dict, prev: str) -> str:
    h = hashlib.sha256()
    h.update(_canonical(body))
    h.update(str(prev or GENESIS).encode("utf-8"))
    return "sha256:" + h.hexdigest()


def _iso(ts: float) -> str:
    try:
        dt = datetime.datetime.fromtimestamp(float(ts), datetime.timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + ("%03d" % (dt.microsecond // 1000)) + "Z"


def _path() -> str:
    return AUDIT_FILE


def _archive_dir() -> str:
    """归档目录 = 与主文件同级（测试里 `set_file()` 换目录后归档也跟着走）。"""
    return os.path.join(os.path.dirname(_path()) or ".", "_audit-archive")


def _lock_path() -> str:
    return _path() + ".lock"


def _app_version() -> str:
    """`cocraft <版本>`：安装版有 VERSION 文件；开发树没有 → `dev`。"""
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8") as fh:
            v = fh.read().strip()
        if v:
            return "cocraft " + v.splitlines()[0].strip()
    except Exception:  # noqa: BLE001
        pass
    return "cocraft dev"


@contextlib.contextmanager
def _locked():
    """进程内锁 + （Windows）跨进程文件锁。拿不到文件锁也继续（日志不该拖死业务）。"""
    with _LOCK:
        fh = None
        try:
            os.makedirs(os.path.dirname(_path()) or ".", exist_ok=True)
            fh = open(_lock_path(), "a+b")
            try:
                if fh.seek(0, os.SEEK_END) == 0:
                    fh.write(b"\0")
                    fh.flush()
                fh.seek(0)
                if msvcrt is not None:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            except Exception:  # noqa: BLE001
                pass
            yield
        finally:
            if fh is not None:
                try:
                    if msvcrt is not None:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------- 读

def _parse_line(raw: bytes):
    s = raw.decode("utf-8", "replace").strip()
    if not s:
        return None
    if len(s) > MAX_LINE:
        return {"__bad": "记录过长（>%d 字节）" % MAX_LINE}
    try:
        obj = json.loads(s)
    except Exception as exc:  # noqa: BLE001
        return {"__bad": "JSON 解析失败：%s" % exc}
    if not isinstance(obj, dict):
        return {"__bad": "不是 JSON 对象"}
    return obj


def _read_file(path: str):
    """单个文件 → (记录列表, 坏行列表)。坏行不抛、不静默跳过：交给 verify 报。"""
    recs, bad = [], []
    try:
        with open(path, "rb") as fh:
            for i, raw in enumerate(fh, 1):
                obj = _parse_line(raw)
                if obj is None:
                    continue
                if "__bad" in obj:
                    bad.append({"line": i, "file": os.path.basename(path),
                                "reason": obj["__bad"]})
                    continue
                recs.append(obj)
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [{"line": 0, "file": os.path.basename(path),
                     "reason": "读文件失败：%s" % exc}]
    return recs, bad


def archive_files() -> list:
    """归档文件（**按文件名排序 = 按 seq 先后**）。目录不存在 → 空。"""
    try:
        names = sorted(n for n in os.listdir(_archive_dir()) if n.endswith(".jsonl"))
    except OSError:
        return []
    return [os.path.join(_archive_dir(), n) for n in names]


def _read_lines():
    """读**全部**记录（归档 + 主文件）→ (记录列表, 坏行列表)。

    ★ 归档文件是只读的；`seq` 重复时保留先读到的那份（切换瞬间可能短暂重叠），
      所以「归档→主文件」拼起来仍然是一条连续的链。
    """
    recs, bad = [], []
    for p in archive_files() + [_path()]:
        r, b = _read_file(p)
        recs.extend(r)
        bad.extend(b)
    seen, out = set(), []
    for r in recs:
        try:
            s = int(r.get("seq") or 0)
        except (TypeError, ValueError):
            s = 0
        if s:
            if s in seen:
                continue
            seen.add(s)
        out.append(r)
    return out, bad


def _main_lines() -> list:
    """主文件里的原始行（bytes，去掉空行）—— 归档/重写时逐字节搬运，哈希不变。"""
    try:
        with open(_path(), "rb") as fh:
            return [x for x in fh.read().split(b"\n") if x.strip()]
    except OSError:
        return []


def _tail() -> tuple:
    """链尾 (seq, hash)。文件尾部被撕行（断电/写到一半）→ 退化成整文件扫描。"""
    try:
        size = os.path.getsize(_path())
    except OSError:
        return 0, GENESIS
    if size <= 0:
        return 0, GENESIS
    try:
        with open(_path(), "rb") as fh:
            fh.seek(max(0, size - TAIL_CHUNK))
            chunk = fh.read()
    except OSError:
        return 0, GENESIS
    for raw in reversed(chunk.split(b"\n")):
        obj = _parse_line(raw)
        if obj is None or "__bad" in obj:
            continue
        try:
            return int(obj.get("seq") or 0), str(obj.get("hash") or GENESIS)
        except (TypeError, ValueError):
            continue
    # 尾部那段里没有完整记录 → 老实扫一遍**主文件**（通常是文件还很小，或者撕裂发生在尾部）
    recs, _bad = _read_file(_path())
    if recs:
        try:
            return int(recs[-1].get("seq") or 0), str(recs[-1].get("hash") or GENESIS)
        except (TypeError, ValueError):
            pass
    return 0, GENESIS


# ---------------------------------------------------------------- 写

def append(source: str, actor=None, action=None, decision: str = "", result: str = "",
           approver=None, ref=None, quarantine: str = "", note: str = "",
           ts: float = 0.0, via: str = ""):
    """追加一条审计记录 → 返回带 `hash` 的记录；**失败返回 None，绝不抛异常**。

    `actor`      {"kind":"agent|user|system","id":…,"name":…,"member":…,"role":…,"engine":…}
    `action`     {"kind":"execute|edit|read|net|undo|restore|drop","title":…,"command":…,
                  "targets":[…],"destructive":bool}
    `approver`   {"kind":"user","id":…,"name":…} 或 None（策略/超时/撤销这类没人批准）
    `ref`        {"run":…,"member":…,"perm":…,"chat":…,"aid":…,"session":…,"scope":…,"ts":…}
    `via`        这次动作**怎么来的**：`ui`（面板里点的）/ `api`（本机 HTTP，没带面板标记）/
                 `system`（程序内部，默认）—— 用来区分"人点的"和"被脚本/agent 调的"
    """
    t = float(ts or time.time())
    act = dict(action or {})
    act["targets"] = [str(x)[:500] for x in (act.get("targets") or [])[:20]]
    act["command"] = str(act.get("command") or "")[:2000]
    act["title"] = str(act.get("title") or "")[:300]
    act["kind"] = str(act.get("kind") or "")
    act["destructive"] = bool(act.get("destructive"))
    ac = dict(actor or {})
    ac = {k: str(ac.get(k) or "") for k in ("kind", "id", "name", "member", "role", "engine")}
    ac["kind"] = ac["kind"] or "system"
    ap = approver if isinstance(approver, dict) else None
    if ap is not None:
        ap = {k: str(ap.get(k) or "") for k in ("kind", "id", "name")}
        ap["kind"] = ap["kind"] or "user"
    rf = {k: str((ref or {}).get(k) or "") for k in
          ("run", "member", "perm", "chat", "aid", "session", "scope", "ts")}
    try:
        with _locked():
            seq, prev = _tail()
            body = {
                "v": VERSION,
                "seq": seq + 1,
                "ts": _iso(t),
                "tsu": round(t, 3),
                "machine": machine(),
                "actor": ac,
                "approver": ap,
                "action": act,
                "decision": str(decision or ""),
                "result": str(result or ""),
                "quarantine": str(quarantine or ""),
                "source": str(source or ""),
                "via": str(via or "system"),
                "ref": rf,
                "note": str(note or "")[:500],
                "prev": prev or GENESIS,
            }
            body["hash"] = _digest(body, body["prev"])
            line = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            with open(_path(), "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line + "\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            rec = body
    except Exception:  # noqa: BLE001 - 审计落盘失败绝不能挡住权限/执行流程
        return None
    # ★保留期：超过阈值就把最旧的一段移进归档（绝不影响本次追加）
    try:
        maybe_rotate()
    except Exception:  # noqa: BLE001
        pass
    return rec


# ---------------------------------------------------------------- 保留期 / 归档

ROTATE_BYTES = int(os.environ.get("COCRAFT_AUDIT_ROTATE_BYTES") or 4 * 1024 * 1024)
KEEP_BYTES = int(os.environ.get("COCRAFT_AUDIT_KEEP_BYTES") or 1024 * 1024)


def rotate(max_bytes=None, keep_bytes=None, via: str = "") -> dict:
    """把主文件**最旧的一段**移进归档，并在链里留一条 `archive` 记录。

    - 归档文件：`<state>/_audit-archive/<首seq>-<末seq>.jsonl`（**逐字节搬运**，哈希不变、不再改动）；
    - 链**跨文件仍然连续**：主文件第一条的 `prev` 指向归档最后一条的 hash（`verify()` 会把归档一起读）；
    - 归档这件事本身写进链（`action.kind=archive`，带归档文件名），所以"删过什么"也可追溯（§4.4）。
    - 只在主文件超过 `max_bytes` 时动手；参数给 0 = 不归档。
    """
    max_b = int(max_bytes if max_bytes is not None else ROTATE_BYTES)
    keep_b = int(keep_bytes if keep_bytes is not None else KEEP_BYTES)
    if max_b <= 0:
        return {"ok": True, "rotated": False, "reason": "已关闭（阈值 0）"}
    try:
        size = os.path.getsize(_path())
    except OSError:
        return {"ok": True, "rotated": False, "reason": "还没有审计文件"}
    if size <= max_b:
        return {"ok": True, "rotated": False, "bytes": size, "maxBytes": max_b}
    with _locked():
        raw = _main_lines()
        if not raw:
            return {"ok": True, "rotated": False, "reason": "主文件是空的"}
        keep_from, acc = len(raw), 0
        while keep_from > 0 and acc < keep_b:
            keep_from -= 1
            acc += len(raw[keep_from]) + 1
        if keep_from <= 0:
            return {"ok": True, "rotated": False, "reason": "全都不够一格（太小，不动）"}
        moved = raw[:keep_from]
        first = _parse_line(moved[0]) or {}
        last = _parse_line(moved[-1]) or {}
        if "__bad" in first or "__bad" in last:
            return {"ok": False, "rotated": False, "reason": "归档段里有坏行（先修再归档）",
                    "bytes": size}
        a, b = int(first.get("seq") or 0), int(last.get("seq") or 0)
        if not a or b < a:
            return {"ok": False, "rotated": False, "reason": "归档段 seq 不合法"}
        seq, prev = _tail()
        body = {
            "v": VERSION, "seq": seq + 1, "ts": _iso(time.time()),
            "tsu": round(time.time(), 3), "machine": machine(),
            "actor": {"kind": "system", "id": "audit", "name": "cocraft",
                      "member": "", "role": "", "engine": ""},
            "approver": None,
            "action": {"kind": "archive", "title": "归档审计记录（%d 条）" % len(moved),
                       "command": "", "targets": [], "destructive": False},
            "decision": "archive", "result": "ok", "quarantine": "",
            "source": "manual", "via": str(via or "system"),
            "ref": {"run": "", "member": "", "perm": "", "chat": "", "aid": "",
                    "session": "", "scope": "", "ts": ""},
            "note": "seq %d..%d 移入归档（只留痕，不改内容）" % (a, b),
            "prev": prev or GENESIS,
        }
        body["hash"] = _digest(body, body["prev"])
        name = "%06d-%06d.jsonl" % (a, b)
        dest = os.path.join(_archive_dir(), name)
        n = 2
        while os.path.exists(dest):                    # 同一区间重复归档 → 让开一个
            name = "%06d-%06d-%d.jsonl" % (a, b, n)
            dest = os.path.join(_archive_dir(), name)
            n += 1
        try:
            os.makedirs(_archive_dir(), exist_ok=True)
            tmp = dest + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(b"\n".join(moved) + b"\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp, dest)
            # 主文件 = 剩余记录 + 刚写的那条 archive 记录（原子替换）
            rest = raw[keep_from:] + [json.dumps(body, ensure_ascii=False,
                                                 separators=(",", ":")).encode("utf-8")]
            tmp2 = _path() + ".tmp"
            with open(tmp2, "wb") as fh:
                fh.write(b"\n".join(rest) + b"\n")
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    pass
            os.replace(tmp2, _path())
        except OSError as exc:
            return {"ok": False, "rotated": False, "reason": "归档写盘失败：%s" % exc}
        return {"ok": True, "rotated": True, "archive": name, "from": a, "to": b,
                "moved": len(moved), "kept": len(rest),
                "bytes": os.path.getsize(_path())}


def maybe_rotate() -> dict:
    """每次追加之后顺手看一眼：主文件超阈值就归档（不超就是一次 `stat`，很便宜）。"""
    try:
        size = os.path.getsize(_path())
    except OSError:
        return {"ok": True, "rotated": False}
    if ROTATE_BYTES <= 0 or size <= ROTATE_BYTES:
        return {"ok": True, "rotated": False, "bytes": size}
    return rotate()


# ---------------------------------------------------------------- 查 / 导出 / 校验

_FILTER_KEYS = ("source", "run", "member", "chat", "aid", "session", "actor", "approver",
                "decision", "result")


def records(limit: int = 0, since: float = 0.0, until: float = 0.0, **filters) -> list:
    """按条件取记录（**新 → 旧**）。坏行不影响其它记录。"""
    recs, _bad = _read_lines()
    lo = float(since or 0)
    hi = float(until or 0)
    out = []
    for r in recs:
        try:
            t = float(r.get("tsu") or 0)
        except (TypeError, ValueError):
            t = 0.0
        if lo and t < lo:
            continue
        if hi and t > hi:
            continue
        ok = True
        for k in _FILTER_KEYS:
            want = filters.get(k)
            if not want:
                continue
            if k in ("source", "decision", "result"):
                got = str(r.get(k) or "")
            elif k in ("actor", "approver"):
                got = str((r.get(k) or {}).get("id") or "")
            else:
                got = str((r.get("ref") or {}).get(k) or "")
            if str(want) != got:
                ok = False
                break
        want_kind = filters.get("kind")
        if ok and want_kind and str((r.get("action") or {}).get("kind") or "") != str(want_kind):
            ok = False
        if ok:
            out.append(r)
    out.sort(key=lambda r: (float(r.get("tsu") or 0), int(r.get("seq") or 0)), reverse=True)
    if limit and int(limit) > 0:
        return out[:int(limit)]
    return out


def _ordered(items) -> list:
    return sorted(items, key=lambda r: int(r.get("seq") or 0))


def verify(items=None, subset: bool = False) -> dict:
    """逐条重算哈希 + 要求 `prev` 能在链内解析。返回可直接给前端/脚本的结构。

    - `ok`      ：没有断裂（哈希对得上、prev 都找得到）
    - `broken`  ：[{index, seq, reason, expected, actual}]
    - `gaps`    ：seq 不连续处（区间导出必然有，属正常；完整导出应为 0）
    - `tail`    ：最后一条的 hash（对照导出文件头）
    - `subset`  ：这一份是**区间导出**（首条的 `prev` 指向范围外，无法就地验证 → 只当锚点）

    ⚠ `subset=True` 时首条的 `prev` 会被当作已知锚点（否则任何按时间/成员筛选的导出都会"报断"）。
      这也意味着**区间导出的第一条没法证明自己衔接在哪** —— 需要在完整链上复核，属已知边界。
    """
    if items is None:
        rec_items, bad = _read_lines()
    else:
        rec_items, bad = [r for r in (items or []) if isinstance(r, dict)], []
    seq_items = _ordered(rec_items)
    broken = [{"index": 0, "seq": 0, "reason": "坏行：%s" % b.get("reason"),
               "expected": "", "actual": ""} for b in bad]
    known = {GENESIS}
    anchor = ""
    if subset and seq_items:
        anchor = str(seq_items[0].get("prev") or "")
        if anchor:
            known.add(anchor)
    prev_seq = 0
    gaps = 0
    for i, r in enumerate(seq_items, 1):
        seq = int(r.get("seq") or 0)
        prev = str(r.get("prev") or "")
        digest_body = {k: v for k, v in r.items() if k not in ("__bad",)}
        got_hash = str(digest_body.pop("hash", "") or "")
        want_hash = _digest(digest_body, prev)
        if got_hash != want_hash:
            broken.append({"index": i, "seq": seq, "reason": "哈希不匹配（内容被改过 / 序列化不是 canonical）",
                           "expected": want_hash, "actual": got_hash})
        if prev not in known:
            broken.append({"index": i, "seq": seq, "reason": "前一条哈希找不到（链断了 / 重排过）",
                           "expected": "已知哈希或 %s" % GENESIS[:16] + "…", "actual": prev})
        if prev_seq and seq != prev_seq + 1:
            gaps += 1
        prev_seq = seq
        known.add(got_hash or want_hash)
    tail = str(seq_items[-1].get("hash") or GENESIS) if seq_items else GENESIS
    return {"ok": not broken, "count": len(seq_items),
            "first": int(seq_items[0].get("seq") or 0) if seq_items else 0,
            "last": int(seq_items[-1].get("seq") or 0) if seq_items else 0,
            "tail": tail, "genesis": GENESIS, "gaps": gaps, "subset": bool(subset),
            "anchor": anchor,
            "broken": broken[:50], "brokenCount": len(broken)}


def export(since: float = 0.0, until: float = 0.0, limit: int = 0, **filters) -> dict:
    """生成可验证导出：**文件头 + 记录 + 链尾**（格式见 `docs/商业模式 §4.3`）。"""
    items = records(limit=limit, since=since, until=until, **filters)
    narrowed = bool(since or until or limit or any(v2 for v2 in filters.values()))
    items.reverse()                              # 导出按时间正序，方便人读 / 逐条核对
    tail = str(items[-1].get("hash") or GENESIS) if items else GENESIS
    v = verify(items, subset=narrowed)
    return {
        "format": FORMAT,
        "version": VERSION,
        "exported": _iso(time.time()),
        "records": len(items),
        "tail": tail,
        "engine": _app_version(),
        "machine": machine(),
        "filters": {"since": float(since or 0), "until": float(until or 0),
                    "limit": int(limit or 0),
                    **{k: str(v2) for k, v2 in filters.items() if v2}},
        "chain": {"ok": v["ok"], "gaps": v["gaps"], "subset": v["subset"],
                  "first": v["first"], "last": v["last"],
                  "anchor": v["anchor"], "genesis": GENESIS},
        "items": items,
    }


_PERM_REPLY_RE = re.compile(r"^/api/session/([^/]+)/permission/([^/]+)/reply$")


def note_permission_reply(path, method, body, agent_id: str = "") -> bool:
    """★面板里对「权限申请」的答复 —— OpenCode 引擎走反代，只能在这里截。

    ACP 引擎在 `engines/acp/service.py` 内部（等待点结束处）直接写，不经过这里。
    `path` = `handler.path`（可能带 query）；`body` = 已读出的原始 bytes（或 dict）。
    返回 True = 认出来这是一次权限答复（写没写进去都算 True：fail-open）。
    """
    if str(method or "").upper() not in ("POST", "PUT"):
        return False
    try:
        from urllib.parse import urlsplit
        p = urlsplit(str(path or "")).path
    except Exception:  # noqa: BLE001
        return False
    m = _PERM_REPLY_RE.match(p)
    if not m:
        return False
    data = {}
    if isinstance(body, (bytes, bytearray)):
        try:
            data = json.loads(bytes(body).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            data = {}
    elif isinstance(body, dict):
        data = body
    if not isinstance(data, dict):
        data = {}
    decision = str(data.get("decision") or data.get("reply") or "").strip().lower()
    if decision not in ("once", "always", "reject"):
        return False
    append(
        "panel",
        actor={"kind": "system", "id": str(agent_id or "opencode"), "name": "OpenCode"},
        action={"kind": "permission", "title": "面板权限答复"},
        decision=decision,
        result=("ok" if decision in ("once", "always") else "denied"),
        approver={"kind": "user", "id": "local", "name": "owner"},
        ref={"session": m.group(1), "perm": m.group(2)},
        note="面板内单会话审批")
    return True


# ---------------------------------------------------------------- CSV（留档用）

# ⚠ 列顺序**定死**（`docs/审计-方案.md` §6 的口径）：加列只能往后加，别重排 ——
#   否则别人手里的历史 CSV 对不上表头。
CSV_FIELDS = [
    "v", "seq", "ts", "tsu", "machine", "source",
    "actor_kind", "actor_id", "actor_name", "actor_member", "actor_role", "actor_engine",
    "approver_kind", "approver_id", "approver_name",
    "decision", "result",
    "action_kind", "title", "command", "targets", "destructive",
    "quarantine", "ref_run", "ref_member", "ref_perm", "ref_chat", "ref_aid",
    "ref_session", "ref_scope", "ref_ts", "note", "prev", "hash",
    "via",                       # ★加在最末尾（加列只能往后加，别重排）
]
_ACTOR_KEYS = ("kind", "id", "name", "member", "role", "engine")
_APPROVER_KEYS = ("kind", "id", "name")
_REF_KEYS = ("run", "member", "perm", "chat", "aid", "session", "scope", "ts")


def _row_of(r: dict) -> list:
    """一条记录 → CSV 一行（*targets* 用紧凑 JSON，避免分隔符打架）。"""
    ac = r.get("actor") or {}
    ap = r.get("approver") or {}
    act = r.get("action") or {}
    rf = r.get("ref") or {}
    return [
        r.get("v") or VERSION, r.get("seq") or 0, r.get("ts") or "", r.get("tsu") or 0,
        r.get("machine") or "", r.get("source") or "",
        ac.get("kind") or "", ac.get("id") or "", ac.get("name") or "",
        ac.get("member") or "", ac.get("role") or "", ac.get("engine") or "",
        ap.get("kind") or "", ap.get("id") or "", ap.get("name") or "",
        r.get("decision") or "", r.get("result") or "",
        act.get("kind") or "", act.get("title") or "", act.get("command") or "",
        json.dumps(act.get("targets") or [], ensure_ascii=False, separators=(",", ":")),
        "1" if act.get("destructive") else "0",
        r.get("quarantine") or "",
        rf.get("run") or "", rf.get("member") or "", rf.get("perm") or "",
        rf.get("chat") or "", rf.get("aid") or "", rf.get("session") or "",
        rf.get("scope") or "", rf.get("ts") or "", r.get("note") or "",
        r.get("prev") or "", r.get("hash") or "", r.get("via") or "",
    ]


def to_csv(items) -> str:
    """记录列表 → CSV 文本（**纯表格**：第一行是列名，没有前导说明行，Excel 能直接开）。

    ⚠ 每行都带 `prev` / `hash`，所以 CSV **也能被校验脚本逐条重算**
      （见 `csv_records()`；文件级链尾看 HTTP 头的 `X-Audit-Tail`）。
    """
    import csv as _csv
    import io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_FIELDS)
    for r in items:
        if isinstance(r, dict):
            w.writerow(_row_of(r))
    return buf.getvalue()


def csv_records(text: str) -> list:
    """CSV 文本 → 审计记录列表（给校验脚本用）。认不出的行跳过，不抛。"""
    import csv as _csv
    import io as _io
    out = []
    try:
        rdr = _csv.DictReader(_io.StringIO(str(text or "").lstrip("\ufeff")))
        for row in rdr:
            if not row:
                continue
            try:
                seq = int(str(row.get("seq") or 0) or 0)
            except (TypeError, ValueError):
                continue
            if seq <= 0:
                continue
            try:
                tsu = float(str(row.get("tsu") or 0) or 0)
            except (TypeError, ValueError):
                tsu = 0.0
            try:
                targets = json.loads(str(row.get("targets") or "[]") or "[]")
                if not isinstance(targets, list):
                    targets = []
            except Exception:  # noqa: BLE001
                targets = []
            actor = {k: str(row.get("actor_" + k) or "") for k in _ACTOR_KEYS}
            akind = str(row.get("approver_kind") or "")
            approver = ({k: str(row.get("approver_" + k) or "") for k in _APPROVER_KEYS}
                        if akind else None)
            ref = {k: str(row.get("ref_" + k) or "") for k in _REF_KEYS}
            try:
                v = int(str(row.get("v") or VERSION) or VERSION)
            except (TypeError, ValueError):
                v = VERSION
            out.append({
                "v": v, "seq": seq, "ts": str(row.get("ts") or ""), "tsu": tsu,
                "machine": str(row.get("machine") or ""), "source": str(row.get("source") or ""),
                "actor": actor, "approver": approver,
                "action": {"kind": str(row.get("action_kind") or ""),
                           "title": str(row.get("title") or ""),
                           "command": str(row.get("command") or ""),
                           "targets": [str(x) for x in targets],
                           "destructive": str(row.get("destructive") or "") in ("1", "true", "True")},
                "decision": str(row.get("decision") or ""), "result": str(row.get("result") or ""),
                "quarantine": str(row.get("quarantine") or ""),
                "ref": ref, "note": str(row.get("note") or ""),
                "prev": str(row.get("prev") or ""), "hash": str(row.get("hash") or ""),
            })
            # ⚠ `via` 是 0.2.42 之后才有的列：
            #   · 老 CSV 没这一列 → 不加这个键；
            #   · 老记录（0.2.42 前写的）导出时这一列是空串 → 也不加这个键。
            #   否则重建出来的记录会多一个字段、哈希对不上（假报断）。
            if row.get("via"):
                out[-1]["via"] = str(row.get("via"))
    except Exception:  # noqa: BLE001
        return []
    return out


def stats() -> dict:
    """给面板看的摘要（含归档：条数 / 文件数 / 主文件字节）。"""
    recs, bad = _read_lines()
    try:
        size = os.path.getsize(_path())
    except OSError:
        size = 0
    afiles = archive_files()
    archived = 0
    for p in afiles:
        try:
            r, _b = _read_file(p)
            archived += len(r)
        except Exception:  # noqa: BLE001
            pass
    first = recs[0] if recs else {}
    last = recs[-1] if recs else {}
    return {"ok": True, "file": _path(), "bytes": size, "records": len(recs),
            "badLines": len(bad), "machine": machine(),
            "archived": {"files": len(afiles), "records": archived,
                         "dir": _archive_dir(),
                         "latest": os.path.basename(afiles[-1]) if afiles else ""},
            "rotateBytes": ROTATE_BYTES, "keepBytes": KEEP_BYTES,
            "first": {"seq": int(first.get("seq") or 0), "ts": first.get("ts") or ""},
            "last": {"seq": int(last.get("seq") or 0), "ts": last.get("ts") or "",
                     "hash": str(last.get("hash") or GENESIS)}}


# ---------------------------------------------------------------- 一页报告（给人看的）

_DECISION_ZH = {"once": "允许一次", "always": "始终允许", "reject": "拒绝",
                "confirm": "破坏性确认", "auto": "自动记录", "restore": "恢复",
                "drop": "删除恢复点", "undo": "撤销这一轮", "archive": "归档"}
_RESULT_ZH = {"ok": "成功", "denied": "被拒", "timeout": "超时", "failed": "失败",
              "skipped": "跳过"}
_SOURCE_ZH = {"collab": "本地协作", "ccagent": "群聊 AI 成员", "snapshot": "隔离区/撤销",
              "panel": "面板会话", "manual": "手动/系统"}


def report(since: float = 0.0, until: float = 0.0, limit: int = 0, **filters) -> str:
    """生成一页 Markdown 报告（可交给同事/甲方；不含成员正文）。"""
    items = records(limit=limit, since=since, until=until, **filters)
    items.reverse()                                    # 正序，方便人从上往下读
    v = verify(items, subset=bool(since or until or limit
                                  or any(x for x in filters.values())))
    by = {"decision": {}, "result": {}, "source": {}}
    destructive = []
    acked = 0
    files_touched = set()
    file_turns = 0
    for r in items:
        d = str(r.get("decision") or "")
        res = str(r.get("result") or "")
        src = str(r.get("source") or "")
        by["decision"][d] = by["decision"].get(d, 0) + 1
        by["result"][res] = by["result"].get(res, 0) + 1
        by["source"][src] = by["source"].get(src, 0) + 1
        act = r.get("action") or {}
        if act.get("destructive"):
            destructive.append(r)
        if str((r.get("approver") or {}).get("kind") or "") == "user":
            acked += 1
        if str(act.get("kind") or "") == "files":
            file_turns += 1
            for t in (act.get("targets") or []):
                files_touched.add(str(t))
    def _range():
        if since or until:
            a = _iso(since) if since else "最早"
            b = _iso(until) if until else "现在"
            return "%s ~ %s" % (a, b)
        return "全部"
    def _counts(m, zh):
        if not m:
            return "（无）"
        return " · ".join("%s %d" % (zh.get(k) or k or "-", n)
                          for k, n in sorted(m.items(), key=lambda kv: (-kv[1], kv[0])))
    lines = [
        "# 审计报告（cocraft）",
        "",
        "| 项 | 值 |",
        "| --- | --- |",
        "| 生成时间 | %s |" % _iso(time.time()),
        "| 版本 | %s |" % _app_version(),
        "| 机器 | %s |" % (machine() or "-"),
        "| 范围 | %s |" % _range(),
        "| 记录条数 | %d |" % len(items),
        "| 序号 | %d .. %d |" % (v["first"], v["last"]),
        "| 链校验 | %s |" % ("完整（逐条哈希对得上）" if v["ok"] else
                            ("断裂 %d 处" % v["brokenCount"])),
        "| 链尾 | `%s` |" % v["tail"],
        "| 经人批准 | %d 次 |" % acked,
        "| 破坏性操作 | %d 次 |" % len(destructive),
        "| 文件改动轮次 | %d 轮 / 涉及 %d 个文件 |" % (file_turns, len(files_touched)),
        "",
        "## 结论分布",
        "",
        "- 决定：" + _counts(by["decision"], _DECISION_ZH),
        "- 结果：" + _counts(by["result"], _RESULT_ZH),
        "- 来源：" + _counts(by["source"], _SOURCE_ZH),
        "",
    ]
    if destructive:
        lines += ["## 破坏性操作（逐条）", "",
                  "| 时间 | 谁 | 命令 | 谁批的 | 隔离 | 结果 |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for r in destructive[-50:]:
            act = r.get("action") or {}
            who = (r.get("actor") or {}).get("name") or (r.get("actor") or {}).get("id") or "-"
            ap = (r.get("approver") or {}) or {}
            cmd = " ".join(str(act.get("command") or act.get("title") or "").split())
            lines.append("| %s | %s | `%s` | %s | %s | %s |" % (
                (r.get("ts") or "")[:19].replace("T", " "), who, cmd[:120] or "-",
                (ap.get("name") or ap.get("id") or "-"),
                (r.get("quarantine") or "未隔离"), _RESULT_ZH.get(str(r.get("result")), r.get("result") or "-")))
        lines.append("")
    lines += ["## 明细（最近 %d 条）" % len(items[-200:]), "",
              "| seq | 时间 | 来源 | 谁 | 决定 | 结果 | 动作 / 目标 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in items[-200:]:
        act = r.get("action") or {}
        who = (r.get("actor") or {}).get("name") or (r.get("actor") or {}).get("id") or "-"
        what = " ".join(str(act.get("command") or act.get("title") or act.get("kind") or "").split())
        tg = (act.get("targets") or [])
        if tg:
            what = (what + " → " + "、".join(tg[:3]) + ("…" if len(tg) > 3 else ""))
        lines.append("| %d | %s | %s | %s | %s | %s | %s |" % (
            int(r.get("seq") or 0), (r.get("ts") or "")[:19].replace("T", " "),
            _SOURCE_ZH.get(str(r.get("source")), r.get("source") or "-"), who,
            _DECISION_ZH.get(str(r.get("decision")), r.get("decision") or "-"),
            _RESULT_ZH.get(str(r.get("result")), r.get("result") or "-"),
            what[:160] or "-"))
    lines += ["", "> 只含「决定 / 命令 / 目标路径」，**不含成员正文**；",
              "> 链校验用 `tools\\cocraft_audit_verify.py <导出文件>` 可独立复核。", ""]
    return "\n".join(lines)
