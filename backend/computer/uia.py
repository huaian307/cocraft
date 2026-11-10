"""UI Automation 客户端 —— 连 `tools/uia_host.ps1` 常驻宿主（JSON 行协议）。

为什么要常驻：每次冷启一个 powershell.exe 要 0.4~0.8s，一个 GUI 任务
（tree → find → click → type）就是 3~5 次冷启动，几秒钟全在等进程。
宿主只启动一次，之后每次调用约 20~60ms；空闲 90 秒自动退出。

⚠ 必须用 Windows PowerShell 5.1（`powershell.exe`）：`UIAutomationClient`
是 .NET Framework 程序集，pwsh 7（.NET Core）里加载不了。
"""

import atexit
import json
import os
import queue
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
HOST_PS1 = os.path.join(ROOT, "tools", "uia_host.ps1")
LOG_DIR = os.path.join(ROOT, "runtime", "logs")
LOG_PATH = os.path.join(LOG_DIR, "cu.log")
STATE_PATH = os.path.join(ROOT, "runtime", "state", "_uia_host.json")

CREATE_NO_WINDOW = 0x08000000
IDLE_SECONDS = 90


def _save_state(pid):
    """记下宿主 pid（面板的「通用工具」列表要显示它在不在，**不能**为了看状态去启动它）。"""
    try:
        os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump({"pid": int(pid), "since": time.strftime("%Y-%m-%dT%H:%M:%S")}, f)
    except Exception:
        pass


def _drop_state():
    try:
        os.remove(STATE_PATH)
    except Exception:
        pass


def host_state():
    """只读：UIA 宿主在不在跑（不会启动它）。"""
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"running": False, "mode": "lazy"}
    try:
        pid = int(data.get("pid") or 0)
    except Exception:
        pid = 0
    if not pid:
        return {"running": False, "mode": "lazy"}
    try:
        from . import winapi
        alive = bool(winapi.process_name(pid))
    except Exception:
        alive = False
    out = {"running": alive, "mode": "lazy", "pid": pid, "since": data.get("since") or ""}
    if not alive:
        _drop_state()
    return out


class UiaError(RuntimeError):
    pass


def powershell_exe():
    """Windows PowerShell 5.1 的绝对路径（PATH 坏掉也能用）。"""
    cands = []
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    cands.append(os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    cands.append(os.path.join(sysroot, "SysWOW64", "WindowsPowerShell", "v1.0", "powershell.exe"))
    for p in cands:
        if os.path.isfile(p):
            return p
    return "powershell.exe"


class UiaHost:
    def __init__(self, script=HOST_PS1, timeout=25.0):
        self.script = script
        self.timeout = timeout
        self.proc = None
        self._q = queue.Queue()
        self._reader = None
        self._lock = threading.Lock()
        self._seq = 0
        self._stderr = None
        self.last_used = 0.0
        self._watchdog = None

    # ---------- 进程管理 ----------
    def _log(self, text):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except Exception:
            pass

    def _ensure(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        if not os.path.isfile(self.script):
            raise UiaError("UIA 宿主脚本不存在：%s" % self.script)
        if not os.path.isfile(LOG_PATH):
            try:
                os.makedirs(LOG_DIR, exist_ok=True)
            except Exception:
                pass
        self._stderr = open(LOG_PATH, "ab")
        self._stderr.write(b"\n--- uia_host start %s ---\n" % time.strftime("%Y-%m-%d %H:%M:%S").encode())
        self._stderr.flush()
        args = [powershell_exe(), "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-File", self.script]
        self.proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._stderr,
            cwd=ROOT,
            creationflags=CREATE_NO_WINDOW,
        )
        self._q = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, args=(self.proc, self._q), daemon=True)
        self._reader.start()
        self._start_watchdog()
        self._log("host started pid=%s" % self.proc.pid)
        _save_state(self.proc.pid)

    @staticmethod
    def _read_loop(proc, q):
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)

    def _start_watchdog(self):
        if self._watchdog and self._watchdog.is_alive():
            return

        def _run():
            while True:
                time.sleep(5)
                if self.proc is None or self.proc.poll() is not None:
                    return
                if self.last_used and (time.time() - self.last_used) > IDLE_SECONDS:
                    self.close()
                    return

        self._watchdog = threading.Thread(target=_run, daemon=True)
        self._watchdog.start()

    def close(self):
        p, self.proc = self.proc, None
        if p is None:
            return
        try:
            p.stdin.write(b'{"id":0,"cmd":"shutdown"}\n')
            p.stdin.flush()
        except Exception:
            pass
        try:
            p.wait(timeout=2)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass
        for f in (getattr(p, "stdin", None), getattr(p, "stdout", None)):
            try:
                f.close()
            except Exception:
                pass
        if self._stderr:
            try:
                self._stderr.close()
            except Exception:
                pass
            self._stderr = None
        self._log("host closed")
        _drop_state()

    # ---------- 调用 ----------
    def call(self, cmd, timeout=None, **kw):
        with self._lock:
            self._ensure()
            self._seq += 1
            rid = self._seq
            payload = {"id": rid, "cmd": cmd}
            payload.update(kw)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except Exception as exc:
                self.close()
                raise UiaError("UIA 宿主写入失败（已重启）：%s" % exc)

            deadline = time.time() + (timeout or self.timeout)
            noise = None
            while True:
                left = deadline - time.time()
                if left <= 0:
                    self._log("timeout cmd=%s" % cmd)
                    self.close()
                    hint = ""
                    if isinstance(noise, dict) and noise.get("error"):
                        hint = "；宿主回报：%s" % noise.get("error")
                    raise UiaError("UIA 调用超时（%s，%.1fs）%s"
                                   % (cmd, timeout or self.timeout, hint))
                try:
                    line = self._q.get(timeout=min(left, 0.5))
                except queue.Empty:
                    if self.proc is None or self.proc.poll() is not None:
                        self.close()
                        raise UiaError("UIA 宿主已退出（详见 runtime/logs/cu.log）")
                    continue
                if line is None:
                    self.close()
                    raise UiaError("UIA 宿主输出结束（已退出）")
                try:
                    resp = json.loads(line)
                except Exception:
                    # 宿主偶尔会吐警告/噪声，跳过不像响应的行
                    self._log("skip non-json: %s" % line[:200])
                    continue
                if resp.get("id") != rid:
                    # 这不是给我们的响应：说明请求格式/协议对不上，别干等超时
                    noise = resp
                    self._log("skip mismatched id: %s" % line[:200])
                    continue
                self.last_used = time.time()
                if not resp.get("ok"):
                    raise UiaError(resp.get("error") or "UIA 调用失败")
                return resp

    # ---------- 便捷方法 ----------
    def ping(self):
        return self.call("ping")

    def describe(self, hwnd):
        return self.call("describe", hwnd=int(hwnd))["window"]

    def tree(self, hwnd, depth=4, max_nodes=300):
        return self.call("tree", hwnd=int(hwnd), depth=int(depth), max_nodes=int(max_nodes),
                         timeout=max(20.0, self.timeout))

    def find(self, hwnd, name=None, role=None, automation_id=None, name_like=None,
             class_like=None, limit=10):
        kw = {"hwnd": int(hwnd), "limit": int(limit)}
        if name:
            kw["name"] = name
        if role:
            kw["role"] = role
        if automation_id:
            kw["automation_id"] = automation_id
        if name_like:
            kw["name_like"] = name_like
        if class_like:
            kw["class_like"] = class_like
        return self.call("find", **kw)["items"]

    def wait(self, hwnd, timeout_ms=8000, poll_ms=120, **find_kw):
        kw = {"hwnd": int(hwnd), "timeout_ms": int(timeout_ms), "poll_ms": int(poll_ms)}
        kw.update({k: v for k, v in find_kw.items() if v})
        return self.call("wait", timeout=max(10.0, timeout_ms / 1000.0 + 8.0), **kw)

    def click(self, element_id=None, hwnd=None, **find_kw):
        kw = {}
        if element_id:
            kw["element"] = element_id
        if hwnd:
            kw["hwnd"] = int(hwnd)
        kw.update({k: v for k, v in find_kw.items() if v})
        return self.call("click", **kw)

    def set_value(self, text, element_id=None, hwnd=None, **find_kw):
        kw = {"text": text}
        if element_id:
            kw["element"] = element_id
        if hwnd:
            kw["hwnd"] = int(hwnd)
        kw.update({k: v for k, v in find_kw.items() if v})
        return self.call("setvalue", **kw)

    def focus(self, element_id=None, hwnd=None, **find_kw):
        kw = {}
        if element_id:
            kw["element"] = element_id
        if hwnd:
            kw["hwnd"] = int(hwnd)
        kw.update({k: v for k, v in find_kw.items() if v})
        return self.call("focus", **kw)


_HOST = None
_HOST_LOCK = threading.Lock()


def get_host():
    global _HOST
    with _HOST_LOCK:
        if _HOST is None:
            _HOST = UiaHost()
        return _HOST


@atexit.register
def _shutdown():
    global _HOST
    if _HOST is not None:
        try:
            _HOST.close()
        except Exception:
            pass
        _HOST = None
