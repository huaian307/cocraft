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

import base64
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
# ★ 失败缓存 10 分钟（不要用 60s）：GitHub **匿名**查询限流是 60 次/小时/IP（实测响应头），
#   网络不通时前端启动就查一次，60s 的短缓存会把配额**烧光**，之后即使网络恢复也一直 403。
FAIL_CACHE_SECONDS = 600
# ★ 下载读超时：200MB 的包经第三方镜像，60s 偏紧（慢镜像会中途被判超时 → 整包重下）。
DOWNLOAD_TIMEOUT = 120
# ★ 启动更新脚本后，等它自报一次 `phase="started"` 的上限（本机 PowerShell 不可用时会静默什么都不做）。
PS_START_WAIT = 12.0
CREATE_NO_WINDOW = 0x08000000
# ⚠ 不要用它启动 powershell：实测（Windows 10.0.26200）用 DETACHED_PROCESS 起 PowerShell，
#   进程 `rc=0` 但**脚本一行都不执行**（-File / -EncodedCommand 都一样，静默无声）。
#   这正是「更新脚本 12 秒没有回报」的真凶，见 AGENTS.md 踩坑 #52。
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
# ★ 直接拉起安装器后，看它"还活着"要等多久（活着一律算"起来了"）。
DIRECT_START_WAIT = 2.5

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


# ---------------- GitHub Token（可选，解决匿名限流） ----------------
# ★ 匿名查 GitHub API 只有 **60 次/小时/IP**（实测响应头，见踩坑 #44）；共享出口（梯子 / 代理 / 公司 NAT）
#   很容易被别人一起刷没 → 用户看到的就是"检查失败 / 更新失败"。
#   带 Token 是 **5000 次/小时/账号**（按账号算，不受出口 IP 影响）→ 要治本就得让面板带上 Authorization。
#   ⚠ **只在直连 api.github.com 时带** —— 绝不把 Token 交给第三方镜像代理（那等于把凭据送给别人）。
#   本机保存位置 `runtime/state/_github_token.json`（**只在本地；不进安装包、不进仓库**）；
#   也认环境变量 `COCRAFT_GITHUB_TOKEN` / `GITHUB_TOKEN`（两者优先级高于文件）。
def _token_path(app: str = "") -> str:
    return os.path.join(app or ROOT, "runtime", "state", "_github_token.json")


def github_token() -> str:
    v = (os.environ.get("COCRAFT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    if v:
        return v
    try:
        with open(_token_path(), "r", encoding="utf-8-sig") as fh:
            return str((json.load(fh) or {}).get("token") or "").strip()
    except (OSError, ValueError):
        return ""


def mask_secret(t: str) -> str:
    """只回掩码（跟「模型 API Key」那一行同一个规矩：**明文不进界面**）。"""
    t = str(t or "")
    if not t:
        return ""
    if len(t) <= 8:
        return "已设置"
    return "%s…%s（%d 位）" % (t[:4], t[-4:], len(t))


def read_token_info() -> dict:
    env = (os.environ.get("COCRAFT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()
    t = github_token()
    return {"set": bool(t), "mask": mask_secret(t), "source": ("env" if env else ("state" if t else ""))}


def write_token(token) -> dict:
    """`token` 留空 = 清除。写盘后**清掉 check 缓存**，让改动立刻生效。"""
    t = str(token or "").strip()
    p = _token_path()
    try:
        if not t:
            os.remove(p)
        else:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"token": t}, fh)
            os.replace(tmp, p)
    except OSError:
        pass
    _cache.update(t=0.0, data=None)
    return read_token_info()


def _gh_headers(url: str, accept: str = "application/vnd.github+json") -> dict:
    """请求头。★ **只有在直连 GitHub API 时**才加 Authorization（不给第三方镜像代理看）。"""
    h = {"User-Agent": UA, "Accept": accept}
    if url == API_URL:
        tok = github_token()
        if tok:
            h["Authorization"] = "Bearer " + tok
    return h


def _api_candidates() -> list:
    """「查最新版」的源：**直连 → 各镜像代理**（去重）。

    ★ 2026-10-14：国内很常见的问题是**连不上 github.com / api.github.com**（实测 WinError 10060）。
      下载早就有一层镜像兜底，但这里以前只走直连 → 网络一挡就**完全查不到更新**（用户看到的
      就是"更新失败"）。实测：`gh-proxy.com` 能代理 `api.github.com`（返回完整 JSON，带 size/digest）。
    """
    env = (os.environ.get("COCRAFT_UPDATE_DOWNLOAD") or "").strip()
    cands = []
    if env:
        cands.append(env.replace("{url}", API_URL) if "{url}" in env else env.rstrip("/") + "/" + API_URL)
    cands.append(API_URL)
    for m in DOWNLOAD_MIRRORS:
        cands.append(m + API_URL)
    seen, out = set(), []
    for u in cands:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


_RE_TAG = re.compile(r"/releases/tag/([^/\"?#]+)")
_RE_ASSET = re.compile(r"/releases/download/([^/\"?]+)/([^\"?]+\.exe)")


def _http_text(url: str, timeout: float = 25.0):
    """GET 一个文本页（自动跟随重定向）；返回 `(最终URL, 正文)`。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl(), resp.read(600000).decode("utf-8", "replace")


def _check_via_html():
    """★ 兜底：连不上 GitHub / API 全挂时，用**镜像**从 releases 页面抠版本号与安装包名。

    `mirror + github.com/<repo>/releases/latest` → 302 到 `.../releases/tag/<tag>`（实测 ghproxy.net / ghfast.top 可用）；
    资产名在 `.../releases/expanded_assets/<tag>` 片段里（同两个镜像可用）。
    拿不到 `size` / `digest`（那是 API 才有的）→ 后面只做"够大 + MZ"两项校验。
    """
    for m in DOWNLOAD_MIRRORS:
        try:
            final, html = _http_text(m + "https://github.com/%s/releases/latest" % REPO)
        except Exception:  # noqa: BLE001
            continue
        mt = _RE_TAG.search(final) or _RE_TAG.search(html)
        if not mt:
            continue
        tag = mt.group(1)
        name = ""
        try:
            _f2, ea = _http_text(m + "https://github.com/%s/releases/expanded_assets/%s" % (REPO, tag))
            ma = _RE_ASSET.search(ea)
            if ma:
                name = ma.group(2)
        except Exception:  # noqa: BLE001
            pass
        if not name:                       # 片段拿不到就按命名约定兜一个（新名/旧名各试一次）
            ver = tag[1:] if tag.startswith("v") else tag
            for pfx in ASSET_PREFIXES:
                name = "%s%s-windows-x64.exe" % (pfx, ver)
                break
        return {"tag": tag, "latest": (tag[1:] if tag.startswith("v") else tag),
                "asset": {"name": name,
                          "url": "https://github.com/%s/releases/download/%s/%s" % (REPO, tag, name),
                          "size": None, "digest": ""},
                "source": m + "(html)"}
    return None


def check(force: bool = False) -> dict:
    """查最新版本。结果缓存 1 小时（失败缓存 `FAIL_CACHE_SECONDS`，别烧光 GitHub 匿名限流）。"""
    now = time.time()
    cached = _cache.get("data")
    if cached is not None and not force:
        ttl = CACHE_SECONDS if cached.get("ok") else FAIL_CACHE_SECONDS
        if now - _cache["t"] < ttl:
            return cached

    cur = current_version()
    out = {"ok": True, "supported": bool(cur), "current": cur, "latest": "",
           "hasUpdate": False, "asset": None, "notes": "", "htmlUrl": "",
           "checkedAt": int(now), "reason": "", "source": ""}
    if not cur:
        out["reason"] = "没有版本信息（开发环境 / 非安装版）"
        _cache.update(t=now, data=out)
        return out

    # ① API：直连 → 镜像代理 api.github.com（拿到完整数据：size / sha256 digest）
    rel, src, errors, limit_hit = None, "", [], False
    for url in _api_candidates():
        host = _host_of(url)
        try:
            req = urllib.request.Request(url, headers=_gh_headers(url, "application/vnd.github+json"))
            with urllib.request.urlopen(req, timeout=20) as resp:
                rel = json.loads(resp.read().decode("utf-8"))
            src = host
            break
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429):
                limit_hit = True
                errors.append("%s：HTTP %s（可能是限流）" % (host, exc.code))
            else:
                errors.append("%s：HTTP %s" % (host, exc.code))
        except Exception as exc:  # noqa: BLE001
            errors.append("%s：%s" % (host, exc))

    if rel is not None:
        tag = str(rel.get("tag_name") or "").strip()
        latest = tag[1:] if tag.startswith("v") else tag
        asset = _pick_asset(rel.get("assets"))
        out["latest"] = latest
        out["notes"] = str(rel.get("body") or "")[:2000]
        out["htmlUrl"] = str(rel.get("html_url") or "")
        out["asset"] = asset or None
        out["source"] = src
        out["hasUpdate"] = bool(latest and _verkey(latest) > _verkey(cur))
        if out["hasUpdate"] and not asset:
            out["hasUpdate"] = False
            out["reason"] = "最新版本里没有可用的安装包"
        _cache.update(t=now, data=out)
        return out

    # ② HTML 兜底：API 全挂（典型是连不上 GitHub）→ 借镜像从 releases 页面拿版本号 + 安装包名
    fb = _check_via_html()
    if fb:
        out["latest"] = fb["latest"]
        out["asset"] = fb["asset"]
        out["source"] = fb["source"]
        out["htmlUrl"] = "https://github.com/%s/releases/tag/%s" % (REPO, fb["tag"])
        out["hasUpdate"] = bool(fb["latest"] and _verkey(fb["latest"]) > _verkey(cur))
        _cache.update(t=now, data=out)
        return out

    # ③ 都不行 → 明确说是网络问题（并给一句可操作的话）
    out["ok"] = False
    if limit_hit:
        out["reason"] = ("GitHub 接口限流（HTTP 403/429）：匿名查询只有 60 次/小时/IP（共享出口容易被别人刷没）。"
                         "在「设置 → 软件更新 → GitHub Token」填一个只读 token 可提到 5000 次/小时/账号。")
    else:
        out["reason"] = ("检查失败：连不上 GitHub（已试 %d 个源）—— 多半是本机网络/DNS 到 GitHub 不通；"
                         "%s" % (len(errors), "；".join(errors[:3])))
    _cache.update(t=now, data=out)
    return out


def _relaunch_command(app: str) -> list:
    """装完怎么把面板重新拉起来（安装版有 python\\pythonw.exe + launchers\\launch_opencode.py）。"""
    pyw = os.path.join(app, "python", "pythonw.exe")
    launcher = os.path.join(app, "launchers", "launch_opencode.py")
    if os.path.isfile(pyw) and os.path.isfile(launcher):
        return [pyw, launcher]
    return []


def powershell_exe() -> str:
    """PowerShell 的**绝对路径**优先（不靠 PATH；找不到才退回裸名字）。

    ★ 2026-10-25（用户的另一台机器 `v0.1.66` 实测）：安装包**已经下载完**了，却卡在
      「更新脚本没能启动（12 秒内没有回报）」—— 因为起脚本用的是裸 `"powershell"`，
      那台机器的 PATH 不可靠 / `powershell.exe` 起不来（`0xc0000142` 这类系统层问题）。
      先按 `%SystemRoot%\\System32\\WindowsPowerShell\\v1.0\\powershell.exe` 找绝对路径，
      能救掉「PATH 被改坏」这一类；真起不来还有 `_launch_setup_direct()` 兜底。
    """
    root = os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
    for rel in (r"System32\WindowsPowerShell\v1.0\powershell.exe",
                r"SysWOW64\WindowsPowerShell\v1.0\powershell.exe"):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            return p
    return "powershell"


def _ps_encoded_args(script: str) -> list:
    """把脚本塞进 `-EncodedCommand`（base64 / UTF-16LE），**不走 .ps1 文件**。

    ★ 2026-10-25 现场证据（用户那台 `lenovo`）：PowerShell **本体是好的**
      （`powershell.exe -NoProfile -Command "exit 0"` → `errorlevel=0`），
      但更新脚本以 `-File apply.ps1` 方式起时**一声不响什么都不做**。
      最常见的两种原因是「组策略把 ExecutionPolicy 钉成 AllSigned/RemoteSigned（`-ExecutionPolicy Bypass`
      顶不过 GPO）」和「Defender ASR / 智能应用控制拦 `%TEMP%` 里的 .ps1」——
      两者的共同点是：**只拦"脚本文件"，不拦 `-Command` / `-EncodedCommand`**。
      所以主路径改成 EncodedCommand（脚本仅 ~2 KB，远低于 32K 命令行上限），
      `apply.ps1` 仍然写一份到临时目录**只作排查用**。
    """
    b64 = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return ["-NoProfile", "-NonInteractive", "-EncodedCommand", b64]


def _launch_setup_direct(setup: str, log: str) -> tuple:
    """**不经过 PowerShell**，直接以脱离进程的隐藏方式拉起安装器（同为静默装）。

    判据：`DIRECT_START_WAIT` 秒后它还**活着**（`poll() is None`）= 起来了；
    一启动就退出 = 被拦了（杀软 / 权限 / 文件坏了），返回退出码给人看。
    """
    args = [setup, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
            "/CLOSEAPPLICATIONS", "/LOG=%s" % log]
    # ⚠ 这里也**不用 DETACHED_PROCESS**（原因同上，见踩坑 #52）；安装器是 GUI 程序，
    #   面板退出后它照样跑完（Windows 不会因为父进程退出就杀子进程）。
    flags = ((CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP)
             if sys.platform == "win32" else 0)
    try:
        proc = subprocess.Popen(args, close_fds=True, creationflags=flags)
    except Exception as exc:  # noqa: BLE001
        return False, "直接启动安装器失败：%s" % exc
    time.sleep(DIRECT_START_WAIT)
    rc = proc.poll()
    if rc is None:
        return True, ""
    return False, "安装器一启动就退出（exit code %s）" % rc


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
        # ★ 第一件事：告诉面板「脚本起来了」。否则面板分不清"脚本根本没跑"和"正在装" ——
        #   本机 PowerShell 不可用时（见 AGENTS.md 踩坑 #69 的 0xc0000142）脚本会静默什么都不做。
        "Write-UpdateState 'started' $null ''",
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
        "if ($code -eq 0) { Write-UpdateState 'done' $true '' } else {",
        # ★ 把 install.log 尾部带进错误里 —— 否则用户（和我）只看到 "exit code N"，还得去找那个文件
        "  $tail = ''",
        "  if (Test-Path '%s') { $tail = ((Get-Content '%s' -Tail 12 -ErrorAction SilentlyContinue) -join ' | ') }"
        % (sq(log), sq(log)),
        "  if (-not $tail) { $tail = '(no log)' }",
        "  Write-UpdateState 'failed' $false ('installer exit code ' + $code + '; log tail: ' + $tail)",
        "}",
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
    used_src = ""
    for cand in _download_candidates(asset["url"]):
        try:
            req = urllib.request.Request(cand, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as resp, open(setup, "wb") as fh:
                shutil.copyfileobj(resp, fh, 256 * 1024)
            got = True
            used_src = _host_of(cand)
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
    write_status(app, phase="downloading", ok=None, version=latest, fromVersion=cur,
                 error="", source=used_src)          # ★ 记下"是哪个源下到的"，现场排查用

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
    ps_script = _build_apply_ps(app, setup, log, latest, cur, relaunch)
    ps = os.path.join(tmp, "apply.ps1")
    with open(ps, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ps_script)             # 只作排查用（真正执行走 -EncodedCommand）

    ps_exe = powershell_exe()
    ps_err = ""
    try:
        subprocess.Popen(
            [ps_exe] + _ps_encoded_args(ps_script),
            close_fds=True,
            # ★ 只给 CREATE_NO_WINDOW（隐藏窗口）+ 新进程组；**绝不能加 DETACHED_PROCESS**
            #   —— 实测那样 PowerShell 会静默什么都不做（见 AGENTS.md 踩坑 #52）。
            #   实测（Popen + 2s 延迟脚本）父进程退出后脚本照样跑完，所以不需要 DETACHED 保活。
            creationflags=(CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP) if sys.platform == "win32" else 0)
    except Exception as exc:  # noqa: BLE001
        ps_err = "启动 PowerShell 失败：%s" % exc

    # ★ 等脚本**自报 started** —— 本机 PowerShell 起不来（0xc0000142 之类）时它会静默什么都不做，
    #   面板就会永远停在"更新中…（装完自动重启）"，用户只能得出"更新失败"。这里把它变成明确报错。
    t0 = time.time()
    started = False
    while time.time() - t0 < PS_START_WAIT:
        ph = str(read_status(app).get("phase") or "")
        if ph and ph != "installing":        # started / done / failed 都算"脚本确实跑了"
            started = True
            break
        time.sleep(0.5)
    if started:
        return {"ok": True, "message": "已开始静默更新，安装完成后面板会自动重启",
                "latest": latest, "setup": setup, "size": size, "backup": backup,
                "source": used_src, "via": "powershell", "willRestart": bool(relaunch)}

    # ★ 兜底：PowerShell 起不来（PATH 坏 / 0xc0000142 / 被策略拦）也**不能让更新卡死** ——
    #   安装包已经下好并校验过了，直接把它以脱离进程的方式拉起来（同为静默装）。
    #   代价：没人帮我们记结果、也自动重启不了面板 → **装完要自己打开 cocraft**（文案里说清楚）。
    ok_direct, direct_err = _launch_setup_direct(setup, log)
    if ok_direct:
        write_status(app, phase="installing", ok=None, version=latest, fromVersion=cur,
                     error="", backup=backup, via="direct")
        return {"ok": True,
                "message": "这台机器的 PowerShell 起不来，已改为**直接启动安装程序**；"
                           "面板会被关闭，装完请重新打开 cocraft（设置已备份，不会丢）",
                "latest": latest, "setup": setup, "size": size, "backup": backup,
                "source": used_src, "via": "direct", "willRestart": False}

    return fail("更新没能启动：PowerShell %s；直接启动 %s。可手动运行 %s"
                % (ps_err or "12 秒内没有回报（%s）" % ps_exe, direct_err, setup))
