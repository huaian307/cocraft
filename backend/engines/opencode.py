# -*- coding: utf-8 -*-
"""OpenCode 引擎 —— 反代到本机 OpenCode 后台服务。

这里是从 server.py 原样搬过来的逻辑（read_service / /api/* 流式反代 /
/healthz 上游探测），搬运过程中不改任何行为，保证默认引擎下表现一致。

⚠ 「上哪儿找 service.json」以前写死 `os.path.expanduser("~")` —— 而安装器 / 守护进程
   拉起的进程里 USERPROFILE / HOMEDRIVE / HOMEPATH / HOME **可能全都没有**，那时
   expanduser 会返回字面量 `'~'` → 路径成 `~\\.local\\...` → **OpenCode 明明装了、
   引擎却永远"未就绪"**（2026-09-26 用户实测：安装版面板里 opencode 一直灰着，就是这个）。
   现在按"多个候选 + 谁存在用谁"来找，见 `_home_dirs()` / `service_states()`。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import urllib.error
import urllib.request

from .base import Engine, HOP_BY_HOP

_NO_WINDOW = 0x08000000

SERVICE_REL = os.path.join(".local", "state", "opencode", "service.json")


def _home_dirs() -> list:
    """所有可能的用户目录（存在、去重、顺序稳定）。

    ⚠ 只用 `expanduser("~")` 不够：缺 USERPROFILE/HOME 时它会回字面量 `'~'`（实测）。
      所以把能想到的线索都用上：USERPROFILE / HOMEDRIVE+HOMEPATH / HOME /
      LOCALAPPDATA 去掉 `AppData\\Local` / expanduser（只在不是 `'~'` 时才认）。
    """
    cands = []
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v:
            cands.append(v)
    hd, hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if hd and hp:
        cands.append(hd.rstrip("\\/") + hp)
    la = os.environ.get("LOCALAPPDATA") or ""
    tail = os.path.join("AppData", "Local")
    if la.lower().endswith(tail.lower()):
        cands.append(la[: -len(tail)].rstrip("\\/"))
    try:
        exp = os.path.expanduser("~")
        if exp and exp != "~":
            cands.append(exp)
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for p in cands:
        try:
            p = os.path.normpath(str(p))
        except Exception:  # noqa: BLE001
            continue
        key = p.lower()
        if p and key not in seen and os.path.isdir(p):
            seen.add(key)
            out.append(p)
    return out


def service_states() -> list:
    """候选的 service.json 路径（`OPENCODE_SERVICE_JSON` 环境变量可覆盖，联调/测试用）。"""
    env = os.environ.get("OPENCODE_SERVICE_JSON", "").strip()
    out = [env] if env else []
    out += [os.path.join(h, SERVICE_REL) for h in _home_dirs()]
    seen, uniq = set(), []
    for p in out:
        k = str(p).lower()
        if p and k not in seen:
            seen.add(k)
            uniq.append(str(p))
    return uniq


def service_state_path() -> str:
    """当前**真的存在**的那个 service.json（都没有就回空）。"""
    for p in service_states():
        if os.path.isfile(p):
            return p
    return ""


def cli_path() -> str:
    """本机 OpenCode 的 opencode-cli.exe —— 装了它就该能选出 opencode 引擎。"""
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "@opencodedesktop",
                     "resources", "opencode-cli.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "@opencodedesktop", "resources",
                     "opencode-cli.exe"),
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return os.path.realpath(p)
    w = shutil.which("opencode-cli") or shutil.which("opencode")
    return os.path.realpath(w) if w else ""


def desktop_exe() -> str:
    """OpenCode **桌面端**（OpenCode.exe）的路径；找不到回 ""。"""
    for p in (os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                           "@opencodedesktop", "OpenCode.exe"),
              r"C:\Program Files\@opencodedesktop\OpenCode.exe"):
        if p and os.path.isfile(p):
            return os.path.realpath(p)
    return ""


def desktop_running() -> bool:
    """OpenCode 桌面端在不在跑（纯 ctypes 名字枚举，不起子进程）。"""
    try:
        import procutils
        return procutils.is_running("OpenCode.exe")
    except Exception:  # noqa: BLE001
        return False


def launch_desktop() -> bool:
    """把 OpenCode 桌面端拉起来（引擎 = opencode 但没在跑时用）。已在跑就不重复。

    ⚠ 为什么需要：以前**只有桌面启动器** `launch_opencode.py` 会在"启动那一刻引擎=opencode"
      时启动它；**运行中把引擎切到 opencode**、或**启动时引擎还是 acp**，就没人拉起 →
      面板代理不到上游，界面报 `502 无法连接 OpenCode 服务`（用户实测）。
    """
    try:
        exe = desktop_exe()
        if not exe or desktop_running():
            return False
        subprocess.Popen([exe], cwd=os.path.dirname(exe), creationflags=_NO_WINDOW)
        return True
    except Exception:  # noqa: BLE001
        return False


def read_service(path: str = None):
    """读取 OpenCode 后台服务的地址与密码（默认取当前存在的那个 service.json）。"""
    target = path or service_state_path()
    if not target:
        raise FileNotFoundError("找不到 OpenCode 的 service.json（OpenCode 还没跑过？）")
    with open(target, "r", encoding="utf-8") as fh:
        svc = json.load(fh)
    url = str(svc["url"]).rstrip("/")
    pw = svc.get("password")
    auth = "Basic " + base64.b64encode(f"opencode:{pw}".encode()).decode() if pw else None
    return url, auth, svc.get("version")


class OpenCodeEngine(Engine):
    id = "opencode"
    label = "OpenCode"

    @property
    def service_state(self) -> str:
        """（server.py 启动自检里会打印它）当前用的 service.json 路径。"""
        return service_state_path() or (service_states() or [""])[0]

    # ---- 生命周期 ----

    def available(self) -> bool:
        """能不能用：有 service.json 就直接能用；只有装好的 CLI 也算"可用"。

        为什么把"装了 CLI 但没跑过"也算可用：否则面板会把 opencode 引擎标成「未就绪」
        并禁选（用户实测的困惑点）。真选它时启动器会把 OpenCode 拉起来。
        """
        return bool(service_state_path() or cli_path())

    def read_service(self):
        return read_service()

    def status(self) -> dict:
        info = {"id": self.id, "label": self.label, "available": self.available()}
        try:
            url, _, version = read_service()
            info["upstream"] = url
            info["version"] = version
        except Exception as exc:  # noqa: BLE001
            info["error"] = f"{type(exc).__name__}: {exc}"
        return info

    # ---- HTTP ----

    def healthz(self):
        try:
            url, auth, version = read_service()
            req = urllib.request.Request(url + "/api/info", headers={"Authorization": auth or ""})
            with urllib.request.urlopen(req, timeout=5) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            return 200, {"ok": True, "upstream": url, "version": info.get("version", version)}
        except Exception as exc:  # noqa: BLE001
            return 503, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_api(self, handler, method: str) -> None:
        url, auth, _ = read_service()

        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None

        headers = {}
        for key, value in handler.headers.items():
            low = key.lower()
            if low in HOP_BY_HOP or low in ("host", "authorization", "content-length", "accept-encoding"):
                continue
            headers[key] = value
        if auth:
            headers["Authorization"] = auth

        req = urllib.request.Request(url + handler.path, data=body, headers=headers, method=method)
        try:
            upstream = urllib.request.urlopen(req, timeout=None)
        except urllib.error.HTTPError as exc:
            upstream = exc
        except Exception as exc:  # noqa: BLE001
            handler._send_json(502, {"error": f"无法连接 OpenCode 服务: {type(exc).__name__}: {exc}",
                                     "upstream": url})
            return

        with upstream:
            handler.send_response(upstream.status)
            for key, value in upstream.headers.items():
                low = key.lower()
                if low in HOP_BY_HOP or low == "content-length":
                    continue
                handler.send_header(key, value)
            declared = upstream.headers.get("Content-Length")
            if declared:
                handler.send_header("Content-Length", declared)
            else:
                # 流式（含 SSE）：不声明长度，改用连接关闭界定结尾
                handler.send_header("Connection", "close")
                handler.close_connection = True
            handler.end_headers()

            # read1 会在有数据时尽快返回，适合 SSE 实时透传
            while True:
                chunk = upstream.read1(16384)
                if not chunk:
                    break
                handler.wfile.write(chunk)
                handler.wfile.flush()
