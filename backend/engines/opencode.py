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
import urllib.parse
import urllib.request

import netsafe                                   # ★ 连本机服务要走 local_urlopen（绕过系统代理）

try:
    import audit                                 # ★审计链：面板权限答复在反代里截
except ImportError:                              # 以 `backend.engines.opencode` 形式加载时（测试）
    from backend import audit  # type: ignore  # noqa: F401,F811

from .base import Engine, EngineError, HOP_BY_HOP

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


def _upstream_json(method: str, path: str, body=None, timeout: float = 60.0):
    """向 OpenCode 上游发一个 JSON 请求，回 `(状态码, 解析后的对象或 None)`。

    与 `handle_api` 的流式反代不同：这里用于面板**主动调用**上游（技能 / 命令清单）。
    HTTP 错误也当正常结果返回（状态码 / 错误体交给调用方决定怎么映射）。
    """
    try:
        url, auth, _ = read_service()
    except Exception as exc:  # noqa: BLE001
        raise EngineError(503, "OpenCode 未就绪：%s" % exc)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url + path, data=data, headers=headers, method=method)
    try:
        with netsafe.local_urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw.decode("utf-8")) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else None
        except Exception:  # noqa: BLE001
            obj = {"error": raw.decode("utf-8", "replace")[:300]}
        return exc.code, obj
    except Exception as exc:  # noqa: BLE001
        raise EngineError(502, "无法连接 OpenCode 服务: %s: %s" % (type(exc).__name__, exc))


def _loc_query(directory) -> str:
    """OpenCode 的 `location` 查询参数（deepObject：`location[directory]=…`）。"""
    if not directory:
        return ""
    return "?" + urllib.parse.urlencode({"location[directory]": str(directory)})


def _err_text(obj, fallback: str) -> str:
    if isinstance(obj, dict):
        return str(obj.get("error") or obj.get("message") or fallback)
    return fallback


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
            with netsafe.local_urlopen(req, timeout=5) as resp:
                info = json.loads(resp.read().decode("utf-8"))
            return 200, {"ok": True, "upstream": url, "version": info.get("version", version)}
        except Exception as exc:  # noqa: BLE001
            return 503, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def handle_api(self, handler, method: str) -> None:
        url, auth, _ = read_service()

        length = int(handler.headers.get("Content-Length") or 0)
        body = handler.rfile.read(length) if length else None

        # ★审计链：面板里点「允许 / 始终允许 / 拒绝」→ 记一条（谁批准的、哪条申请）
        #   放在**转发之前**：即使上游回错，这次"人的决定"也已经有留痕。
        try:
            audit.note_permission_reply(handler.path, method, body, agent_id=self.id)
        except Exception:  # noqa: BLE001 - 审计绝不能挡住反代
            pass

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
            upstream = netsafe.local_urlopen(req, timeout=None)   # ★ 本机上游：别走系统代理
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

    # ---- 技能 / 斜杠命令（面板统一接口，见 base.Engine）----
    #
    # 列表/详情/命令都是面板**主动**打上游；激活技能走上游那条 experimental 路径
    # （`/api/experimental/session/{id}/skill`）—— 把 "experimental" 挡在面板之外。

    def _list_endpoint(self, base: str, directory=None) -> dict:
        """GET 一个清单端点（技能 / 命令）。

        ⚠ 实测：OpenCode 对**没注册过的 location** 会回**空列表**（连内置技能也一起没有）。
          所以先按 location 查，拿到空就退回全局清单 —— 保证选择器不会莫名其妙空着。
        """
        if directory:
            code, obj = _upstream_json("GET", base + _loc_query(directory))
            if code == 200 and isinstance(obj, dict) and (obj.get("data") or []):
                return obj
        code, obj = _upstream_json("GET", base)
        if code != 200 or not isinstance(obj, dict):
            raise EngineError(code if code != 200 else 502, _err_text(obj, "清单读取失败"))
        return obj

    def list_skills(self, directory=None) -> dict:
        obj = self._list_endpoint("/api/skill", directory)
        data = []
        for it in (obj.get("data") or []):
            if not isinstance(it, dict):
                continue
            row = {k: it.get(k) for k in ("id", "name", "description", "autoinvoke", "path")}
            row["source"] = "builtin" if str(it.get("path") or "").startswith("/builtin") else "opencode"
            data.append(row)     # 列表不带 content
        return {"location": obj.get("location") or {"directory": directory or ""}, "data": data}

    def skill_detail(self, skill_id, directory=None):
        locs = ([directory] if directory else []) + [None]
        for loc in locs:
            try:
                obj = self._list_endpoint("/api/skill", loc)
            except EngineError:
                continue
            for it in (obj.get("data") or []):
                if isinstance(it, dict) and str(it.get("id")) == str(skill_id):
                    return it
        return None

    def list_commands(self, directory=None) -> dict:
        obj = self._list_endpoint("/api/command", directory)
        data = [{"name": it.get("name"), "description": it.get("description") or ""}
                for it in (obj.get("data") or []) if isinstance(it, dict) and it.get("name")]
        return {"location": obj.get("location") or {"directory": directory or ""}, "data": data}

    def activate_skill(self, sid, skill_id, resume=False) -> dict:
        path = "/api/experimental/session/%s/skill" % urllib.parse.quote(str(sid), safe="")
        code, obj = _upstream_json("POST", path, {"id": skill_id, "resume": bool(resume)})
        if code not in (200, 201, 204):
            raise EngineError(code, _err_text(obj, "激活技能失败"))
        return {"ok": True, "injected": skill_id, "resumed": bool(resume)}

    def run_command(self, sid, name, text="", files=None) -> dict:
        path = "/api/session/%s/command" % urllib.parse.quote(str(sid), safe="")
        body = {"name": str(name or ""), "text": str(text or "")}
        if files:
            body["files"] = files
        code, obj = _upstream_json("POST", path, body)
        if code not in (200, 201, 204):
            raise EngineError(code, _err_text(obj, "执行命令失败"))
        return {"ok": True}
