"""浏览器 CDP（Chrome DevTools Protocol）—— 网页任务不走桌面像素。

为什么值得单独做一套：Chromium 的 UIA 树只到 `RootView`（拿不到 DOM），而
"截图 + 按像素找按钮"在网页上极不可靠（滚动、缩放、动画、暗色模式都会变）。
CDP 能直接问浏览器："这个选择器的元素在哪、它叫什么、点了之后 DOM 变没变"。

范围（V1）：
  · 只能连**本机** Chromium（Edge/Chrome），端口从 `runtime/state/_cdp.json` 读；
  · 需要时由我们自己拉起一个**专用 profile**（`runtime/cu-browser`），不碰用户日常浏览器；
  · 只做：列表 / 打开 / 跳转 / 读文本 / 执行 JS / 点击 / 输入 / 等待 / 截图 / 关闭。

安全：`cu browser ...` 的命令级权限在 `gate.parse_cu_command()` 里分级；
动作类（open/click/type/go/close）与桌面动作同一套闸门（默认 deny、trust 才 allow），
并且点击前会把元素的可见文本交给危险词检查（"发送/支付/删除"直接拒）。
"""

import base64
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

from .ws import WebSocket

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATE_DIR = os.path.join(ROOT, "runtime", "state")
STATE_FILE = os.path.join(STATE_DIR, "_cdp.json")
PROFILE_DIR = os.path.join(ROOT, "runtime", "cu-browser")
CACHE_DIR = os.path.join(ROOT, "runtime", "cache", "cu")
DEFAULT_PORT = 9333

_CREATE_NO_WINDOW = 0x08000000
_EDGE_PATHS = [
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
]
_CHROME_PATHS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
]


class CdpError(RuntimeError):
    pass


# ---------------------------------------------------------------- 本地 HTTP

def _local_json(url, timeout=5.0):
    """连本机端口**绕开系统代理**（踩坑 #45：代理一断，连 127.0.0.1 也会被送出去）。"""
    opener = None
    try:
        sys.path.insert(0, os.path.join(ROOT, "backend"))
        from netsafe import local_urlopen  # noqa: WPS433
        opener = local_urlopen
    except Exception:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
    req = urllib.request.Request(url, headers={"Host": url.split("/")[2]})
    with opener(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _port_open(port, timeout=0.4):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------- 状态文件

def _read_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _write_state(data):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
        return True
    except Exception:
        return False


def _clear_state():
    try:
        os.remove(STATE_FILE)
    except Exception:
        pass


def update_state(**kw):
    """合并写状态（port/pid/tab…）；失败静默。"""
    st = _read_state()
    st.update({k: v for k, v in kw.items() if v is not None})
    return _write_state(st)


def set_tab(target_id):
    """记住"当前标签"——不记的话每次都会落到 edge:// 的弹窗上（实测踩到）。"""
    return update_state(tab=target_id) if target_id else False


def current_tab():
    return _read_state().get("tab")


def is_internal(url):
    u = str(url or "").lower()
    return (u.startswith(("edge://", "chrome://", "devtools://", "about:"))
            or "sync-confirmation" in u)


def find_exe():
    """找 Edge / Chrome 的可执行文件（可用 COCRAFT_BROWSER_EXE 覆盖）。"""
    env = os.environ.get("COCRAFT_BROWSER_EXE")
    if env and os.path.isfile(env):
        return env
    for pat in _EDGE_PATHS + _CHROME_PATHS:
        p = os.path.expandvars(pat)
        if os.path.isfile(p):
            return p
    return ""


def _port_of_state():
    st = _read_state()
    try:
        return int(st.get("port") or 0)
    except Exception:
        return 0


def current_port():
    return _port_of_state() or int(os.environ.get("COCRAFT_CDP_PORT") or DEFAULT_PORT)


# ---------------------------------------------------------------- 进程管理

def is_up(port=None):
    port = int(port or current_port())
    if not _port_open(port):
        return False
    try:
        _local_json("http://127.0.0.1:%d/json/version" % port, timeout=3)
        return True
    except Exception:
        return False


def start(port=None, headless=False, url="about:blank"):
    """拉起一个**专用 profile** 的 Edge/Chrome（不动用户日常浏览器）。"""
    port = int(port or current_port())
    if is_up(port):
        return {"ok": True, "already": True, "port": port,
                "version": _local_json("http://127.0.0.1:%d/json/version" % port)}
    exe = find_exe()
    if not exe:
        raise CdpError("没找到 Edge/Chrome（可用 COCRAFT_BROWSER_EXE 指定路径）")
    os.makedirs(PROFILE_DIR, exist_ok=True)
    args = [
        exe,
        "--remote-debugging-port=%d" % port,
        "--user-data-dir=%s" % PROFILE_DIR,
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--disable-background-networking",
        "--disable-sync",
        "--disable-default-apps",
        "--no-service-autorun",
        "--window-size=1440,980",
    ]
    if headless:
        args += ["--headless=new", "--disable-gpu"]
    if url:
        args.append(url)
    try:
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=_CREATE_NO_WINDOW)
    except OSError as exc:
        raise CdpError("启动浏览器失败：%s" % exc)
    deadline = time.time() + 30
    while time.time() < deadline:
        if is_up(port):
            info = _read_state()
            info.update({"port": port, "pid": proc.pid, "exe": exe,
                         "headless": bool(headless), "profile": PROFILE_DIR,
                         "started": time.strftime("%Y-%m-%dT%H:%M:%S")})
            _write_state(info)
            return {"ok": True, "already": False, "port": port, "pid": proc.pid,
                    "headless": bool(headless),
                    "version": _local_json("http://127.0.0.1:%d/json/version" % port)}
        time.sleep(0.35)
    raise CdpError("浏览器起来了但 CDP 端口没响应（%d）；看 runtime/cu-browser 是否被占用" % port)


def stop(port=None):
    """先请浏览器自己退（Browser.close），再按 pid 兜底。"""
    st = _read_state()
    port = int(port or st.get("port") or current_port())
    closed = False
    if is_up(port):
        try:
            b = Browser(port=port)
            b.connect()
            b.request("Browser.close", timeout=5)
            closed = True
        except Exception:
            pass
            b = None
        finally:
            try:
                b.close()
            except Exception:
                pass
    pid = st.get("pid")
    if pid:
        deadline = time.time() + 6
        while time.time() < deadline:
            if not _port_open(port):
                break
            time.sleep(0.3)
        if _port_open(port):
            try:
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass
    _clear_state()
    return {"ok": True, "closed": closed, "port": port, "still_up": is_up(port)}


def status():
    st = _read_state()
    port = int(st.get("port") or current_port())
    up = is_up(port)
    out = {"ok": True, "running": up, "port": port,
           "exe": st.get("exe") or find_exe(),
           "profile": st.get("profile") or PROFILE_DIR,
           "headless": bool(st.get("headless")),
           "started": st.get("started"),
           "state_file": STATE_FILE}
    if up:
        try:
            out["version"] = _local_json("http://127.0.0.1:%d/json/version" % port)
        except Exception as exc:
            out["version_error"] = str(exc)
        try:
            out["targets"] = len(Browser(port=port).list_targets())
        except Exception:
            pass
    return out


# ---------------------------------------------------------------- CDP 客户端

class Browser:
    def __init__(self, port=None, timeout=25.0):
        self.port = int(port or current_port())
        self.timeout = timeout
        self.ws = None
        self._id = 0
        self.events = []

    # ---- 连接 ----
    def version(self):
        return _local_json("http://127.0.0.1:%d/json/version" % self.port)

    def connect(self, timeout=None):
        if self.ws is not None:
            return self
        info = self.version()
        ws_url = info.get("webSocketDebuggerUrl")
        if not ws_url:
            raise CdpError("浏览器没给 webSocketDebuggerUrl（CDP 未就绪）")
        self.ws = WebSocket(ws_url, timeout=timeout or self.timeout).connect()
        return self

    def close(self):
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:
                pass
            self.ws = None

    # ---- 请求/响应 ----
    def request(self, method, params=None, session=None, timeout=None):
        self.connect()
        self._id += 1
        rid = self._id
        payload = {"id": rid, "method": method}
        if params:
            payload["params"] = params
        if session:
            payload["sessionId"] = session
        try:
            self.ws.sock.settimeout(timeout or self.timeout)
            self.ws.send_text(json.dumps(payload, ensure_ascii=False))
        except Exception as exc:
            raise CdpError("CDP 写入失败：%s" % exc)
        while True:
            try:
                line = self.ws.recv_text()
            except socket.timeout:
                raise CdpError("CDP 超时（%s，%.0fs）" % (method, timeout or self.timeout))
            except OSError as exc:
                raise CdpError("CDP 读取失败：%s" % exc)
            if line is None:
                raise CdpError("CDP 连接已断开（浏览器退出了？）")
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == rid:
                if msg.get("error"):
                    err = msg["error"]
                    raise CdpError("%s → %s" % (method, err.get("message") or err))
                return msg.get("result") or {}
            if msg.get("method"):
                self.events.append(msg)
                if len(self.events) > 300:
                    del self.events[:100]
                # 页面级事件：记一份"最近一次网络/加载"给 wait 用
                continue

    # ---- 目标（标签页）----
    def list_targets(self):
        try:
            raw = _local_json("http://127.0.0.1:%d/json/list" % self.port, timeout=5)
        except Exception:
            raw = []
        out = []
        for t in raw or []:
            if t.get("type") != "page":
                continue
            out.append({"id": t.get("id"), "title": t.get("title"),
                        "url": t.get("url"), "type": t.get("type")})
        return out

    def new_tab(self, url="about:blank"):
        res = self.request("Target.createTarget", {"url": url or "about:blank"})
        tid = res.get("targetId")
        if not tid:
            raise CdpError("新建标签失败（没返回 targetId）")
        set_tab(tid)
        return tid

    def close_tab(self, target_id):
        res = self.request("Target.closeTarget", {"targetId": target_id})
        if current_tab() == target_id:
            try:
                update_state(tab="")
                _write_state({k: v for k, v in _read_state().items() if k != "tab"})
            except Exception:
                pass
        return res

    def attach(self, target_id):
        res = self.request("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = res.get("sessionId")
        if not sid:
            raise CdpError("附加到标签失败（没返回 sessionId）")
        return Page(self, sid, target_id)

    def pick_target(self, tab=None):
        """tab 可以是 id 前缀 / 索引（0 起）/ 空。

        空的顺序：**当前标签**（上次 open/go 记住的）→ 第一个"非内置页"
        （跳过 edge:// / chrome:// / about: / 同步提示弹窗）→ 第一个 page。
        """
        targets = self.list_targets()
        if not targets:
            return None
        if tab in (None, ""):
            wanted = current_tab()
            if wanted:
                for t in targets:
                    if t.get("id") == wanted:
                        return t
            for t in targets:
                if not is_internal(t.get("url")):
                    return t
            return targets[0]
        tab = str(tab)
        for t in targets:
            if str(t.get("id")) == tab or str(t.get("id", "")).startswith(tab):
                return t
        if tab.isdigit():
            idx = int(tab)
            if 0 <= idx < len(targets):
                return targets[idx]
        return None

    def page(self, tab=None, create_url=None):
        """拿一个可用页面；没有就新建（并把它记为当前标签）。"""
        t = self.pick_target(tab)
        if t is None:
            if create_url is None:
                create_url = "about:blank"
            tid = self.new_tab(create_url)
            return self.attach(tid)
        if tab in (None, ""):
            set_tab(t["id"])
        return self.attach(t["id"])


class Page:
    def __init__(self, browser, session_id, target_id):
        self.browser = browser
        self.sid = session_id
        self.target_id = target_id

    def _cmd(self, method, params=None, timeout=None):
        return self.browser.request(method, params, session=self.sid, timeout=timeout)

    # ---- 读 ----
    def eval(self, expr, await_promise=True, timeout=None):
        res = self._cmd("Runtime.evaluate", {
            "expression": expr, "returnByValue": True,
            "awaitPromise": bool(await_promise), "userGesture": True,
        }, timeout=timeout)
        if res.get("exceptionDetails"):
            d = res["exceptionDetails"]
            msg = (d.get("exception") or {}).get("description") or d.get("text") or "JS 异常"
            raise CdpError("JS 执行出错：%s" % str(msg)[:300])
        return (res.get("result") or {}).get("value")

    def url(self):
        return self.eval("location.href", await_promise=False)

    def title(self):
        return self.eval("document.title", await_promise=False)

    def text(self, selector="body", limit=8000):
        expr = ("(() => { const e = document.querySelector(%r); if (!e) return null;"
                " const t = (e.innerText || e.value || e.textContent || '');"
                " return t.*SLICE*; })()") % (selector,)
        expr = expr.replace("*SLICE*", "slice(0, %d)" % int(limit))
        val = self.eval(expr)
        if val is None:
            raise CdpError("找不到元素：%s" % selector)
        return val

    # ---- 动作 ----
    def goto(self, url, timeout_ms=25000):
        self._cmd("Page.navigate", {"url": url})
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                if self.eval("document.readyState", await_promise=False) == "complete":
                    break
            except CdpError:
                pass
            time.sleep(0.15)
        return {"url": self.url(), "title": self.title()}

    def locate(self, selector):
        expr = ("(() => { const e = document.querySelector(%r); if (!e) return null;"
                " e.scrollIntoView({block:'center', inline:'center'});"
                " const r = e.getBoundingClientRect();"
                " const cs = getComputedStyle(e);"
                " return {x: r.left + r.width/2, y: r.top + r.height/2,"
                "   w: r.width, h: r.height, tag: e.tagName,"
                "   type: e.type || '', name: (e.getAttribute('name')||''),"
                "   id: e.id || '', aria: e.getAttribute('aria-label') || '',"
                "   text: (e.innerText || e.value || e.getAttribute('title') || '').slice(0,120),"
                "   hidden: (cs.display === 'none' || cs.visibility === 'hidden' || r.width < 1 || r.height < 1),"
                "   disabled: !!e.disabled }; })()") % (selector,)
        info = self.eval(expr, await_promise=False)
        if info is None:
            raise CdpError("找不到元素：%s" % selector)
        return info

    def click(self, selector):
        info = self.locate(selector)
        if info.get("hidden"):
            raise CdpError("元素不可见（display/visibility/尺寸为 0）：%s" % selector)
        if info.get("disabled"):
            raise CdpError("元素是 disabled 状态：%s" % selector)
        x, y = info["x"], info["y"]
        method = "mouse"
        try:
            for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1}
                if t == "mouseMoved":
                    params.pop("button")
                    params.pop("clickCount")
                self._cmd("Input.dispatchMouseEvent", params, timeout=10)
        except CdpError:
            # 兜底：直接 DOM click（有些元素吃不到合成鼠标事件）
            ok = self.eval("(() => { const e = document.querySelector(%r); if (!e) return false;"
                           " e.click(); return true; })()" % (selector,), await_promise=False)
            if not ok:
                raise
            method = "dom"
        return {"method": method, "element": info}

    def type_text(self, selector, text, clear=False, timeout=15):
        info = self.locate(selector)
        if info.get("disabled"):
            raise CdpError("元素是 disabled 状态：%s" % selector)
        self.eval("(() => { const e = document.querySelector(%r); if (e) { e.focus();"
                  " if (%s && e.select) { try { e.select(); } catch (_) {} } } })()"
                  % (selector, "true" if clear else "false"), await_promise=False)
        try:
            self._cmd("Input.insertText", {"text": str(text)}, timeout=timeout)
            method = "insertText"
        except CdpError:
            ok = self.eval("(() => { const e = document.querySelector(%r); if (!e) return false;"
                           " const v = %r; const proto = e instanceof HTMLTextAreaElement"
                           "   ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;"
                           " const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
                           " setter.call(e, v);"
                           " e.dispatchEvent(new Event('input', {bubbles:true}));"
                           " e.dispatchEvent(new Event('change', {bubbles:true}));"
                           " return true; })()" % (selector, str(text)), await_promise=False)
            if not ok:
                raise
            method = "dom"
        value = self.eval("(() => { const e = document.querySelector(%r);"
                          " return e ? (e.value !== undefined ? e.value : (e.innerText || '')) : null; })()"
                          % (selector,), await_promise=False)
        return {"method": method, "value": value, "element": info}

    def wait(self, selector, timeout_ms=8000, poll_ms=150):
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            try:
                ok = self.eval("!!document.querySelector(%r)" % (selector,), await_promise=False)
            except CdpError:
                ok = False
            if ok:
                return {"found": True, "elapsed_ms": int((timeout_ms - (deadline - time.time()) * 1000))}
            time.sleep(max(0.05, poll_ms / 1000.0))
        return {"found": False, "elapsed_ms": int(timeout_ms)}

    def shot(self, path=None, full=False):
        res = self._cmd("Page.captureScreenshot",
                        {"format": "png", "captureBeyondViewport": bool(full)}, timeout=30)
        data = res.get("data") or ""
        if not data:
            raise CdpError("截图没数据（captureScreenshot 失败）")
        raw = base64.b64decode(data)
        if not path:
            os.makedirs(CACHE_DIR, exist_ok=True)
            try:
                host = (self.url() or "page").split("//")[-1].split("/")[0][:40]
            except Exception:
                host = "page"
            host = "".join(c for c in host if c.isalnum() or c in ".-_") or "page"
            path = os.path.join(CACHE_DIR, "web-%s-%s.png" % (host, time.strftime("%Y%m%d-%H%M%S")))
        else:
            path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
        return {"path": path, "bytes": len(raw), "url": self.url(), "title": self.title()}
