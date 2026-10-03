# -*- coding: utf-8 -*-
r"""软件更新：查 GitHub Releases 最新版 + 一键静默更新（装完自动重启面板）。

分发渠道是 GitHub Releases（仓库 `huaian307/cocraft`；曾用名 `opencode-ui`，改名后 GitHub 会重定向），
安装包随包写了 `<app>/VERSION`（= 构建时的前端 `?v=`，现在是**语义化版本**如 `0.1.0`）。这里：

* `current_version()` 读 `VERSION`（开发环境没有 → 回退空串，等于"不支持检查"）；
* `check()` 问 GitHub `releases/latest`，挑 `cocraft-setup-*.exe`（兼容旧的 `opencode-ui-setup-*`），
  按 **SemVer**（主.次.修订，预发布 < 正式版）比版本；
* `apply()` 下载安装包到临时目录，再用一个**独立（脱离本进程）的隐藏 PowerShell** 去
  静默运行安装器（`/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS`），**装完自动重启面板**。
  之所以要"独立进程"：安装器会先按端口/路径**杀掉我们自己的进程**，压在面板进程里做重启就来不及。

安全：只从**配置的仓库**的 latest release 下载（客户端传的 URL 一律不信）；可用环境变量
`COCRAFT_UPDATE_API`（或旧的 `OPENCODE_UI_UPDATE_API`）换成别的 API，`COCRAFT_VERSION` /
`OPENCODE_UI_VERSION` 覆盖当前版本（测试用）。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 证书兜底：安装版嵌入式 Python 缺 CA 根证书（见 netsafe）。
# server.py 启动时已把 backend 放到 sys.path 最前，这里直接 import（拿不到就跳过，
# server.py 也会自己装一次，不影响）。
try:
    import netsafe
    netsafe.install()
except Exception:  # noqa: BLE001
    netsafe = None

REPO = "huaian307/cocraft"
API_URL = (os.environ.get("COCRAFT_UPDATE_API")
           or os.environ.get("OPENCODE_UI_UPDATE_API")
           or "https://api.github.com/repos/%s/releases/latest" % REPO)
UA = "cocraft-updater"
ASSET_PREFIXES = ("cocraft-setup-", "opencode-ui-setup-")   # 新名优先，兼容旧包名
CACHE_SECONDS = 3600
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

_cache = {"t": 0.0, "data": None}


def current_version() -> str:
    v = (os.environ.get("COCRAFT_VERSION") or os.environ.get("OPENCODE_UI_VERSION") or "").strip()
    if v:
        return v
    try:
        with open(os.path.join(ROOT, "VERSION"), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _verkey(v: str):
    """标准语义化版本：`1.2.3` / `v1.2.3` / `1.2.3-rc1` → 可比较的元组（正式版 > 预发布）。"""
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+]([0-9A-Za-z.\-]+))?$", (v or "").strip())
    if not m:
        return (0, 0, 0, 0, "")            # 认不出来的一律当最旧（不会误报"有新版"）
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    pre = m.group(4) or ""
    return (major, minor, patch, 1 if not pre else 0, pre)


def _pick_asset(assets) -> dict:
    for prefix in ASSET_PREFIXES:          # 新包名优先，旧包名兜底
        for a in assets or []:
            name = str(a.get("name") or "")
            low = name.lower()
            if low.startswith(prefix) and low.endswith(".exe"):
                return {"name": name, "url": a.get("browser_download_url"),
                        "size": a.get("size"), "digest": a.get("digest") or ""}
    return {}


def check(force: bool = False) -> dict:
    """查最新版本。结果缓存 1 小时（失败缓存短一点，避免频繁打接口）。"""
    now = time.time()
    cached = _cache.get("data")
    if cached is not None and not force:
        ttl = CACHE_SECONDS if cached.get("ok") else 60
        if now - _cache["t"] < ttl:
            return cached

    cur = current_version()
    out = {"ok": True, "supported": bool(cur), "current": cur, "latest": "",
           "hasUpdate": False, "asset": None, "notes": "", "htmlUrl": "",
           "checkedAt": int(now), "reason": ""}
    if not cur:
        out["reason"] = "没有版本信息（开发环境 / 非安装版）"
        _cache.update(t=now, data=out)
        return out

    try:
        req = urllib.request.Request(API_URL, headers={
            "User-Agent": UA, "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            rel = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["reason"] = "检查失败：%s" % exc
        _cache.update(t=now, data=out)
        return out

    tag = str(rel.get("tag_name") or "").strip()
    latest = tag[1:] if tag.startswith("v") else tag
    asset = _pick_asset(rel.get("assets"))
    out["latest"] = latest
    out["notes"] = str(rel.get("body") or "")[:2000]
    out["htmlUrl"] = str(rel.get("html_url") or "")
    out["asset"] = asset or None
    out["hasUpdate"] = bool(latest and _verkey(latest) > _verkey(cur))
    if out["hasUpdate"] and not asset:
        out["hasUpdate"] = False
        out["reason"] = "最新版本里没有可用的安装包"
    _cache.update(t=now, data=out)
    return out


def _relaunch_command(app: str) -> list:
    """装完怎么把面板重新拉起来（安装版有 python\\pythonw.exe + launchers\\launch_opencode.py）。"""
    pyw = os.path.join(app, "python", "pythonw.exe")
    launcher = os.path.join(app, "launchers", "launch_opencode.py")
    if os.path.isfile(pyw) and os.path.isfile(launcher):
        return [pyw, launcher]
    return []


def apply_update(app_dir: str = "") -> dict:
    """下载最新安装包并**脱离本进程**静默安装，装完自动重启面板。"""
    info = check(force=True)
    if not info.get("ok"):
        return {"ok": False, "error": info.get("reason") or "检查更新失败"}
    if not info.get("supported"):
        return {"ok": False, "error": info.get("reason") or "当前环境不支持在线更新（安装版才有 VERSION）"}
    if not info.get("hasUpdate"):
        return {"ok": False, "error": "已经是最新版本（%s）" % (info.get("current") or "?")}
    asset = info.get("asset") or {}
    if not asset.get("url"):
        return {"ok": False, "error": "没有可下载的安装包"}

    app = app_dir or ROOT
    tmp = os.path.join(tempfile.gettempdir(), "cocraft-update")
    os.makedirs(tmp, exist_ok=True)
    name = os.path.basename(asset["name"]) or "cocraft-setup.exe"
    setup = os.path.join(tmp, name)

    # 下载（流式；下完再动安装器，别装到一半）
    try:
        req = urllib.request.Request(asset["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as resp, open(setup, "wb") as fh:
            shutil.copyfileobj(resp, fh, 256 * 1024)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "下载失败：%s" % exc}
    size = os.path.getsize(setup)
    if size < 1024 * 1024:
        return {"ok": False, "error": "下载的文件太小（%d 字节），可能不是安装包" % size}

    # 写一个隐藏 PowerShell：静默装 → 装完重启面板（它不在 {app} 下，杀进程不会误伤它）
    relaunch = _relaunch_command(app)
    ps = os.path.join(tmp, "apply.ps1")
    lines = [
        "Start-Sleep -Seconds 1",
        "$p = Start-Process -FilePath '%s' -ArgumentList '/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS' -PassThru -Wait"
        % setup.replace("'", "''"),
        "Start-Sleep -Seconds 2",
    ]
    if relaunch:
        lines.append("Start-Process -FilePath '%s' -ArgumentList '%s'"
                     % (relaunch[0].replace("'", "''"), relaunch[1].replace("'", "''")))
    with open(ps, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", ps],
            close_fds=True,
            creationflags=(CREATE_NO_WINDOW | DETACHED_PROCESS) if sys.platform == "win32" else 0)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "启动更新程序失败：%s" % exc}

    return {"ok": True, "message": "已开始静默更新，安装完成后面板会自动重启",
            "latest": info.get("latest"), "setup": setup, "size": size,
            "willRestart": bool(relaunch)}
