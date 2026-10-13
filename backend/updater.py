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
* **下载走镜像**：release 资产会 302 到 GitHub 下载 CDN（国内常 `WinError 10060` 连不上）——
  依次尝试 环境变量 `COCRAFT_UPDATE_DOWNLOAD`（模板 `{url}` 或前缀）> 镜像（ghfast.top / gh-proxy.com / ghproxy.net）> 直连。

安全：只从**配置的仓库**的 latest release 下载（客户端传的 URL 一律不信）；可用环境变量
`COCRAFT_UPDATE_API`（或旧的 `OPENCODE_UI_UPDATE_API`）换成别的 API，`COCRAFT_VERSION` /
`OPENCODE_UI_VERSION` 覆盖当前版本（测试用）。
"""

from __future__ import annotations

import hashlib
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

# 下载走镜像：release 资产的 `browser_download_url` 会 302 到 GitHub 的下载 CDN
# （objects.githubusercontent.com / release-assets.githubusercontent.com），国内经常**连不上**
# （实测直连 `WinError 10060` 超时）。项目里技能安装早就用这些代理（ghfast.top ~1s）。
# 顺序：环境变量覆盖 > 镜像 > 直连（直连放最后当兜底，海外用户仍可用）。
DOWNLOAD_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://ghproxy.net/")


def _download_candidates(url: str) -> list:
    """给一个 release 资产地址，返回「依次尝试」的候选 URL 列表（去重）。
    可用 `COCRAFT_UPDATE_DOWNLOAD` 覆盖：既是模板时写 `{url}`，否则当作前缀。"""
    cands = []
    env = (os.environ.get("COCRAFT_UPDATE_DOWNLOAD") or "").strip()
    if env:
        cands.append(env.replace("{url}", url) if "{url}" in env else env.rstrip("/") + "/" + url)
    for m in DOWNLOAD_MIRRORS:
        cands.append(m + url)
    cands.append(url)
    seen, out = set(), []
    for u in cands:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


# ---------------- 更新结果上报（安装器是独立进程，装完/装失败我们看不到 → 落一个状态文件） ----------------
# 面板每次启动读一次并提示（成功/失败/疑似中断），读完就清（`/update/ack`）。
# PowerShell 那边也会写这个文件（它没法调 Python），所以格式就是简单 JSON。

def _status_path(app: str) -> str:
    return os.path.join(app or ROOT, "runtime", "state", "_update.json")


def write_status(app: str, **fields) -> None:
    try:
        p = _status_path(app)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        data = {}
        try:
            with open(p, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh) or {}
        except (OSError, ValueError):
            data = {}
        data.update(fields)
        data["at"] = int(time.time())
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError:
        pass


def read_status(app: str = "") -> dict:
    try:
        with open(_status_path(app), "r", encoding="utf-8-sig") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def clear_status(app: str = "") -> None:
    try:
        os.remove(_status_path(app))
    except OSError:
        pass


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_state(app: str) -> str:
    """更新前把**用户数据**备份到 `%TEMP%\\cocraft-state-backup\\`。

    ★ 2026-10-13 扩展：不只 `runtime/state`，还包含 **`runtime/themes`（用户自己装的主题）**。
      备份完由安装脚本在**装完后自动恢复**（见 `_build_apply_ps` 的 robocopy 步骤）——
      以前只备份、**从不恢复**，等于白备；而 `init_state.py` 又会硬写 baseline
      （清掉 API Key），用户看到的就是"更新把我的设置覆盖了"。
    """
    pairs = [("state", os.path.join(app, "runtime", "state")),
             ("themes", os.path.join(app, "runtime", "themes"))]
    dst_root = os.path.join(tempfile.gettempdir(), "cocraft-state-backup")
    done = []
    try:
        if os.path.isdir(dst_root):
            shutil.rmtree(dst_root, ignore_errors=True)
        for name, src in pairs:
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(dst_root, name))
                done.append(name)
        return dst_root if done else ""
    except OSError:
        return dst_root if done else ""


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


def _build_apply_ps(app: str, setup: str, log: str, latest: str, cur: str, relaunch: list) -> str:
    """生成「静默安装 → **恢复用户数据** → 记录结果 → 重启面板」的 PowerShell 脚本。

    ⚠ 保持**纯 ASCII**：PS5.1 按 ANSI 读无 BOM 的 .ps1，中文会让换行错位、语句串行
    （见 AGENTS.md 踩坑 #9；所以安装失败的文案在这里只用英文，中文由面板侧补）。

    ★ 2026-10-13：装完后用 `robocopy` 把 `%TEMP%\\cocraft-state-backup\\{state,themes}`
      **盖回** `<app>\\runtime\\{state,themes}` —— 这样即使安装器/init_state 动了用户设置，
      更新完也自动回到用户原来的主题/壁纸/外观/权限等级/API Key。
      `/E /IS /IT`：包含子目录、**连"看起来一样"的也覆盖**（保证用户的旧值赢）。
    """
    def sq(s):
        return str(s).replace("'", "''")

    backup = os.path.join(tempfile.gettempdir(), "cocraft-state-backup")
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        "$statePath = '%s'" % sq(_status_path(app)),
        "function Write-UpdateState($phase, $okTag, $msg) {",
        "  try {",
        "    $dir = Split-Path -Parent $statePath",
        "    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }",
        "    $obj = @{ phase=$phase; ok=$okTag; version='%s'; fromVersion='%s'; error=$msg; at=[int](Get-Date -UFormat %%s) }"
        % (sq(latest), sq(cur)),
        "    [IO.File]::WriteAllText($statePath, ($obj | ConvertTo-Json -Compress))",
        "  } catch {}",
        "}",
        "Start-Sleep -Seconds 1",
        "$p = Start-Process -FilePath '%s' -ArgumentList '/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS','/LOG=%s' -PassThru -Wait"
        % (sq(setup), sq(log)),
        "$code = if ($p) { $p.ExitCode } else { -1 }",
        # ★ 恢复用户数据（装成功装失败都恢复）
        "$bk = '%s'" % sq(backup),
        "foreach ($n in @('state','themes')) {",
        "  $src = Join-Path $bk $n",
        "  $dst = Join-Path '%s' ('runtime\\' + $n)" % sq(app),
        "  if (Test-Path $src) {",
        "    if (-not (Test-Path $dst)) { New-Item -ItemType Directory -Path $dst -Force | Out-Null }",
        "    robocopy $src $dst /E /IS /IT /NFL /NDL /NJH /NJS /NP | Out-Null",
        "  }",
        "}",
        "if ($code -eq 0) { Write-UpdateState 'done' $true '' } else { Write-UpdateState 'failed' $false ('installer exit code ' + $code + ' (log: %s)') }"
        % sq(log),
        "Start-Sleep -Seconds 2",
    ]
    if relaunch:
        lines.append("Start-Process -FilePath '%s' -ArgumentList '%s'"
                     % (sq(relaunch[0]), sq(relaunch[1])))
    return "\n".join(lines) + "\n"


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
    backup = _backup_state(app)          # ★ 更新前先备份 runtime/state（装挂了好捞）
    name = os.path.basename(asset["name"]) or "cocraft-setup.exe"
    setup = os.path.join(tmp, name)
    latest = info.get("latest") or ""
    cur = info.get("current") or ""
    log = os.path.join(tmp, "install.log")

    def fail(msg):
        write_status(app, phase="failed", ok=False, version=latest, fromVersion=cur, error=msg)
        return {"ok": False, "error": msg}

    write_status(app, phase="downloading", ok=None, version=latest, fromVersion=cur, error="")

    # 下载（流式；依次尝试 环境变量 > 镜像 > 直连；下完先校验再动安装器）
    errors = []
    got = False
    for cand in _download_candidates(asset["url"]):
        try:
            req = urllib.request.Request(cand, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as resp, open(setup, "wb") as fh:
                shutil.copyfileobj(resp, fh, 256 * 1024)
            got = True
            break
        except Exception as exc:  # noqa: BLE001
            errors.append("%s：%s" % (_host_of(cand), exc))
            if os.path.exists(setup):
                try:
                    os.remove(setup)          # 清掉半截文件，别让下一次从断点续（也不做续传）
                except OSError:
                    pass
    if not got:
        return fail("下载失败（已试 %d 个源）：%s" % (len(errors), "；".join(errors)))

    # 完整性校验：拒绝半截 / 错文件 / 代理返回的 HTML（否则安装器会静默失败，看着像"没更新"）
    size = os.path.getsize(setup)
    if size < 1024 * 1024:
        return fail("下载的文件太小（%d 字节），可能不是安装包" % size)
    want_size = asset.get("size")
    if want_size and size != int(want_size):
        return fail("下载不完整：%d / %s 字节" % (size, want_size))
    try:
        with open(setup, "rb") as fh:
            if fh.read(2) != b"MZ":
                return fail("下载的不是可执行文件（可能是错误页面）")
    except OSError as exc:
        return fail("读取下载文件失败：%s" % exc)
    digest = str(asset.get("digest") or "").strip()
    if digest.lower().startswith("sha256:"):
        want_hash = digest.split(":", 1)[1].strip().lower()
        got_hash = _sha256(setup)
        if got_hash != want_hash:
            return fail("校验失败（sha256 不符）：%s…（期望 %s…）" % (got_hash[:12], want_hash[:12]))

    # 写一个隐藏 PowerShell：静默装 → 记录结果（成功/退出码）→ 装完重启面板。
    # ⚠ 它不在 {app} 下，安装器的 KillOurProcesses 按路径杀不到它，所以能"活过"安装。
    write_status(app, phase="installing", ok=None, version=latest, fromVersion=cur, error="", backup=backup)
    relaunch = _relaunch_command(app)
    ps = os.path.join(tmp, "apply.ps1")
    with open(ps, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_build_apply_ps(app, setup, log, latest, cur, relaunch))

    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", ps],
            close_fds=True,
            creationflags=(CREATE_NO_WINDOW | DETACHED_PROCESS) if sys.platform == "win32" else 0)
    except Exception as exc:  # noqa: BLE001
        return fail("启动更新程序失败：%s" % exc)

    return {"ok": True, "message": "已开始静默更新，安装完成后面板会自动重启",
            "latest": latest, "setup": setup, "size": size, "backup": backup,
            "willRestart": bool(relaunch)}
