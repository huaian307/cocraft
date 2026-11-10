# -*- coding: utf-8 -*-
r"""Node 运行时更新（`agents/node`）—— 检查 / 下载 / 校验 / 替换 / 回滚。

为什么单独一条：随包 node 固定版本，但面板内 npm agent 更新可能要求更高 Node；
以前只能等 cocraft 发版。这里允许用户手动更新 Node，并保留上一版可回滚。

纪律：
  * 只从 **nodejs.org** 下载；校验 `SHASUMS256.txt`（不匹配就停）；
  * 替换前先收起 ACP 子进程（Windows 上 node.exe 被占用会失败）；
  * 旧目录 rename 到 `runtime/node-backup/`，失败自动放回；
  * 不自动更新（默认手动；`/node/apply` 必须由面板确认）。

⚠ 更新 Node 后，`agents/node` 与 `packaging/deps.lock.json` 不再一致；
  下一次安装包更新会把它覆盖回随包版本（这是预期行为）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import zipfile

from engines.acp import agents

ROOT = agents.ROOT_DIR
NODE_DIR = os.path.join(ROOT, "agents", "node")
CACHE_DIR = os.path.join(ROOT, "runtime", "cache", "node")
BACKUP_DIR = os.path.join(ROOT, "runtime", "node-backup")
STATE_FILE = os.path.join(ROOT, "runtime", "state", "_node.json")
API = "https://nodejs.org/dist/index.json"
UA = "cocraft-node-update"

_CACHE = {"ts": 0.0, "latest": "", "lts": ""}
_CACHE_TTL = 3600.0

_LOCK = threading.RLock()
_JOB = {"running": False, "phase": "", "percent": None, "detail": "",
        "bytesDone": 0, "bytesTotal": 0, "log": [], "error": "", "ok": False,
        "version": "", "started": 0.0, "finished": 0.0}


def _set(**kw) -> None:
    with _LOCK:
        _JOB.update(kw)


def _emit(progress, **kw) -> None:
    if callable(progress):
        try:
            progress(**kw)
        except TypeError:
            pass


def _progress(phase=None, percent=None, done=None, total=None, detail=None, log=None) -> None:
    with _LOCK:
        if phase:
            _JOB["phase"] = str(phase)[:120]
        if percent is not None:
            _JOB["percent"] = max(0, min(100, int(percent)))
        if done is not None:
            _JOB["bytesDone"] = int(done)
        if total is not None:
            _JOB["bytesTotal"] = int(total)
        if detail is not None:
            _JOB["detail"] = str(detail)[:240]
        if log:
            _JOB["log"].append(str(log)[:300])
            _JOB["log"] = _JOB["log"][-40:]


def status() -> dict:
    with _LOCK:
        out = dict(_JOB)
        out["log"] = list(_JOB["log"])
    busy = bool(out.pop("running", False))
    out["busy"] = busy
    t0 = float(out.get("started") or 0)
    out["elapsed"] = round(max(0.0, time.time() - t0), 1) if busy and t0 else 0.0
    return out


def _ver(v: str) -> tuple:
    nums = [int(x) for x in re.findall(r"\d+", str(v or ""))[:3]]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def _read_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:                           # noqa: BLE001
        return {}


def _write_state(**kw) -> dict:
    d = _read_state()
    d.update(kw)
    d["updated"] = time.time()
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, STATE_FILE)
    except Exception:                           # noqa: BLE001
        pass
    return d


def node_exe() -> str:
    p = os.path.join(NODE_DIR, "node.exe")
    if os.path.isfile(p):
        return p
    try:
        return agents._glob_node_exe() or ""
    except Exception:                           # noqa: BLE001
        return ""


def current() -> dict:
    exe = node_exe()
    if not exe:
        return {"version": "", "path": ""}
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        v = (r.stdout or "").strip()
    except Exception:                           # noqa: BLE001
        v = ""
    return {"version": v, "path": exe}


def _http(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def check(force: bool = False) -> dict:
    """查 nodejs.org 最新的 LTS（带 win-x64 构建）。缓存 1 小时。"""
    now = time.time()
    if not force and _CACHE["latest"] and (now - _CACHE["ts"]) < _CACHE_TTL:
        latest, lts = _CACHE["latest"], _CACHE["lts"]
    else:
        data = json.loads(_http(API, 20).decode("utf-8", "replace"))
        best, best_lts = "", ""
        for it in (data or []):
            if not it.get("lts"):
                continue
            files = it.get("files") or []
            # nodejs.org 的 index.json 里 Windows zip 叫 `win-x64-zip`（不是 `win-x64`）
            if not any(f in files for f in ("win-x64-zip", "win-x64")):
                continue
            v = str(it.get("version") or "")
            if v and (not best or _ver(v) > _ver(best)):
                best, best_lts = v, str(it.get("lts") or "")
        latest, lts = best, best_lts
        _CACHE.update({"ts": now, "latest": latest, "lts": lts})
    cur = current().get("version") or ""
    return {"current": cur, "latest": latest, "lts": lts,
            "hasUpdate": bool(latest and cur and _ver(latest) > _ver(cur)),
            "cached": bool(now - _CACHE["ts"] < _CACHE_TTL and _CACHE["ts"])}


def _kill_node_processes() -> None:
    """收掉 exe 在 agents/node 下的残留（不碰用户其它 node 进程）。"""
    try:
        from engines import existing_engine
        eng = existing_engine("acp")
        if eng is not None:
            eng.shutdown()
    except Exception:                           # noqa: BLE001
        pass
    d = NODE_DIR.replace("'", "''")
    ps = ("$d='%s'; Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like ($d + '\\*') } "
          "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }" % d)
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                           # noqa: BLE001
        pass


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for c in iter(lambda: fh.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def _download(url: str, dest: str, progress=None) -> str:
    """流式下载（更新 bytesDone/bytesTotal，前端能看到 MB 和百分比）。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as resp:  # noqa: S310
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(dest, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                done += len(chunk)
                _emit(progress, done=done, total=total,
                      detail="已下载 %.1f/%.1f MB" % (done / 1048576.0,
                                                      (total or 0) / 1048576.0))
    return dest


def _download_verify(version: str, progress=None) -> str:
    v = str(version or "").strip()
    if not re.match(r"^v?\d+\.\d+\.\d+$", v):
        raise ValueError("非法 Node 版本：%s" % version)
    v = v if v.startswith("v") else "v" + v
    name = "node-%s-win-x64.zip" % v
    os.makedirs(CACHE_DIR, exist_ok=True)
    zip_path = os.path.join(CACHE_DIR, name)
    if not os.path.isfile(zip_path):
        _emit(progress, phase="下载 Node", percent=5, detail=name)
        url = "https://nodejs.org/dist/%s/%s" % (v, name)
        _download(url, zip_path, progress)
    else:
        _emit(progress, phase="复用已下载的 Node", percent=70,
              detail=os.path.basename(zip_path))
    _emit(progress, phase="校验 SHA256", percent=75, detail="下载 nodejs.org SHASUMS256.txt")
    sums = _http("https://nodejs.org/dist/%s/SHASUMS256.txt" % v, 30).decode("utf-8", "replace")
    want = ""
    for line in sums.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("*") == name:
            want = parts[0].strip().lower()
            break
    if not want:
        raise RuntimeError("SHASUMS256.txt 里找不到 %s" % name)
    got = _sha256(zip_path)
    if got.lower() != want:
        try:
            os.remove(zip_path)
        except OSError:
            pass
        raise RuntimeError("Node 压缩包 SHA256 不匹配（got %s / want %s）" % (got, want))
    _emit(progress, phase="校验通过", percent=80, detail="SHA256 OK")
    return zip_path


def _replace_tree(src: str) -> str:
    """把 src 换到 NODE_DIR；旧目录备份到 runtime/node-backup/<version>-<ts>。返回备份路径。"""
    old = current().get("version") or "unknown"
    os.makedirs(BACKUP_DIR, exist_ok=True)
    bak = os.path.join(BACKUP_DIR, "%s-%s" % (old.lstrip("vV") or "unknown",
                                             time.strftime("%Y%m%d-%H%M%S")))
    _kill_node_processes()
    if os.path.isdir(NODE_DIR):
        os.rename(NODE_DIR, bak)
    try:
        os.makedirs(os.path.dirname(NODE_DIR), exist_ok=True)
        os.rename(src, NODE_DIR)
    except Exception:
        if os.path.isdir(bak) and not os.path.isdir(NODE_DIR):
            os.rename(bak, NODE_DIR)
        raise
    # 只留最近 2 份
    try:
        olds = [os.path.join(BACKUP_DIR, d) for d in os.listdir(BACKUP_DIR)
                if os.path.isdir(os.path.join(BACKUP_DIR, d))]
        for d in sorted(olds, key=os.path.getmtime, reverse=True)[2:]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass
    return bak


def apply(version: str = "", progress=None) -> dict:
    """下载 / 校验 / 替换 Node。`version` 为空 = 最新 LTS。"""
    _emit(progress, phase="查询版本", percent=1, detail="nodejs.org")
    if not version:
        info = check(force=True)
        version = info.get("latest") or ""
    if not version:
        raise RuntimeError("查不到可用的 Node LTS 版本")
    zip_path = _download_verify(version, progress)
    # 解压到临时目录，找到顶层目录
    _emit(progress, phase="解压", percent=82, detail="准备解压")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tmp = os.path.join(CACHE_DIR, "extract-" + stamp)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        members = z.infolist()
        for i, m in enumerate(members):
            z.extract(m, tmp)
            if i % 50 == 0:
                _emit(progress, percent=82 + int(i / max(1, len(members)) * 8),
                      detail="解压 %d/%d" % (i + 1, len(members)))
    entries = [d for d in os.listdir(tmp) if os.path.isdir(os.path.join(tmp, d))]
    src = os.path.join(tmp, entries[0]) if entries else ""
    if not src or not os.path.isfile(os.path.join(src, "node.exe")):
        raise RuntimeError("解压后找不到 node.exe（压缩包结构不对）")
    if not os.path.isdir(os.path.join(src, "node_modules", "npm")):
        raise RuntimeError("解压后的 Node 没有 npm（面板内更新需要它）")
    _emit(progress, phase="备份旧版", percent=92, detail="rename 到 runtime/node-backup")
    old = current().get("version") or ""
    bak = _replace_tree(src)
    shutil.rmtree(tmp, ignore_errors=True)
    cur = current()
    _write_state(current=cur.get("version") or "",
                 path=cur.get("path") or "",
                 previous=bak,
                 previous_version=old.lstrip("vV"))
    _emit(progress, phase="完成", percent=100,
          detail="Node %s" % (cur.get("version") or "?"))
    return {"ok": True, "version": cur.get("version") or "",
            "path": cur.get("path") or "", "backup": bak,
            "previous": _read_state().get("previous")}


def apply_async(version: str = "") -> dict:
    """异步更新 Node（HTTP 立刻返回；进度看 /node/status）。"""
    with _LOCK:
        if _JOB["running"]:
            raise RuntimeError("已经有一个 Node 更新在跑：%s" % (_JOB["phase"] or "?"))
        _JOB.update({"running": True, "phase": "准备", "percent": 0, "detail": "",
                     "bytesDone": 0, "bytesTotal": 0, "log": [], "error": "",
                     "ok": False, "version": "", "started": time.time(),
                     "finished": 0.0})
    threading.Thread(target=_apply_worker, args=(str(version or ""),), daemon=True).start()
    return {"ok": True, "started": True, "version": version}


def _apply_worker(version: str) -> None:
    try:
        res = apply(version, progress=_progress)
        _set(running=False, ok=True, phase="完成", percent=100,
             finished=time.time(), version=res.get("version") or "",
             detail="Node %s" % (res.get("version") or "?"))
    except Exception as exc:                    # noqa: BLE001
        _set(running=False, ok=False, phase="失败", finished=time.time(),
             error="%s: %s" % (type(exc).__name__, exc))


def backups() -> list:
    try:
        out = []
        for d in os.listdir(BACKUP_DIR):
            p = os.path.join(BACKUP_DIR, d)
            if os.path.isdir(p):
                out.append({"name": d, "path": p, "mtime": os.path.getmtime(p)})
        out.sort(key=lambda x: x["mtime"], reverse=True)
        return out
    except OSError:
        return []


def rollback() -> dict:
    """回到上一版 Node（从最近的备份）。"""
    bs = backups()
    if not bs:
        raise RuntimeError("没有可回滚的 Node 备份")
    bak = bs[0]["path"]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cur_bak = os.path.join(BACKUP_DIR, "rollback-" + stamp)
    _kill_node_processes()
    if os.path.isdir(NODE_DIR):
        os.rename(NODE_DIR, cur_bak)
    try:
        os.rename(bak, NODE_DIR)
    except Exception:
        if not os.path.isdir(NODE_DIR) and os.path.isdir(cur_bak):
            os.rename(cur_bak, NODE_DIR)
        raise
    cur = current()
    _write_state(current=cur.get("version") or "", path=cur.get("path") or "",
                 previous="", previous_version="")
    return {"ok": True, "version": cur.get("version") or "",
            "path": cur.get("path") or "", "restored": os.path.basename(bak)}


def info() -> dict:
    st = _read_state()
    return {"current": current(), "latest": _CACHE.get("latest") or "",
            "lts": _CACHE.get("lts") or "", "node_dir": NODE_DIR,
            "backups": backups()[:5], "state": st}
