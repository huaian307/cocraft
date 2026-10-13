# -*- coding: utf-8 -*-
r"""面板内终端：真正的伪终端（ConPTY），引擎无关。

架构：
    前端  ⇄  server.py（系统 Python，纯标准库）  ⇄  pty_host.py（隔离 venv，唯一 import winpty）
                                                        ⇄  ConPTY（pwsh / powershell / cmd）

为什么用子进程宿主：`winpty` 是**可选组件**，装在 `runtime/venvs/terminal`（或安装包的
`runtime/site-packages/terminal`），而 server.py 跑在纯标准库解释器里。分进程既解决了依赖，
也隔离了崩溃。

公开接口：`create / get / remove / list_all / close_all`，以及 `terminal_available()`。
会话只在内存里，进程退出即没了；前端关闭终端时会 DELETE 掉。
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HOST = os.path.join(HERE, "pty_host.py")

CREATE_NO_WINDOW = 0x08000000

_MAX_SESSIONS = 6

_py_cache = {"checked": False, "exe": None, "env": None, "error": "", "note": ""}


def _venv_python() -> str:
    cand = os.path.join(ROOT, "runtime", "venvs", "terminal", "Scripts", "python.exe")
    return cand if os.path.isfile(cand) else ""


def _venv_site(v: str) -> str:
    return os.path.join(os.path.dirname(os.path.dirname(v)), "Lib", "site-packages")


def _venv_base_interpreter(v: str) -> str:
    """从 venv 的 `Scripts\\python.exe` 反推 base 解释器（读 `pyvenv.cfg` 的 `home`）。

    ⚠ Windows 上 venv 的 `python.exe` 是个"转发壳"，会多起一个进程、多花 ~0.07s
    （见 AGENTS 踩坑 #18）。直接用 base 解释器 + `PYTHONPATH=<venv site-packages>` 更快。
    """
    venv_dir = os.path.dirname(os.path.dirname(v))
    home = ""
    try:
        with open(os.path.join(venv_dir, "pyvenv.cfg"), encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.strip().lower().startswith("home"):
                    home = ln.split("=", 1)[1].strip()
                    break
    except OSError:
        home = ""
    base = os.path.join(home, "python.exe") if home else ""
    return base if base and os.path.isfile(base) else ""


def _site_packages() -> str:
    cand = os.path.join(ROOT, "runtime", "site-packages", "terminal")
    return cand if os.path.isdir(cand) else ""


def _result() -> dict:
    return {"ok": bool(_py_cache["exe"]), "exe": _py_cache["exe"],
            "env": _py_cache["env"] or {}, "error": _py_cache["error"]}


def terminal_python(deep: bool = False) -> dict:
    """找到能跑 winpty 的解释器：返回 {ok, exe, env, error}。结果缓存。

    ⚠ `deep=False`（请求路径用）**只查解释器在不在**——不跑子进程，省掉一次完整解释器启动；
    真正的 `import winpty` 校验交给 `warmup()` 在**后台**做（服务器启动时调一次），
    这样点「面板终端」时不会卡在探测上。若组件其实是坏的，deep 校验会把 `ok` 置假，
    UI 仍会显示"不可用"；万一是 warmup 之后才坏，创建会话时宿主会报错、前端弹提示。
    """
    if _py_cache["checked"] and not deep:
        return _result()

    exe, extra_env, note = "", {}, ""
    v = _venv_python()
    sp = _site_packages()
    if v:
        base = _venv_base_interpreter(v)          # ★ 优先用 base 解释器（避开 venv 转发壳）
        if base:
            exe, extra_env, note = base, {"PYTHONPATH": _venv_site(v)}, "venv-base"
        else:
            exe, extra_env, note = v, {}, "venv"
    elif sp:
        exe, extra_env, note = sys.executable, {"PYTHONPATH": sp}, "site-packages"
    if not exe:
        _py_cache["checked"] = True
        _py_cache["error"] = ("终端组件未安装（缺 runtime/venvs/terminal 或 "
                              "runtime/site-packages/terminal）")
        return _result()

    env = dict(os.environ)
    env.update(extra_env)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env.pop("PYTHONSTARTUP", None)

    _py_cache["checked"] = True
    _py_cache["exe"] = exe
    _py_cache["env"] = env
    _py_cache["error"] = ""
    _py_cache["note"] = note
    if not deep:
        return _result()

    # 深度校验：真跑一次 `import winpty`
    try:
        r = subprocess.run([exe, "-c", "import winpty"], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=20, creationflags=CREATE_NO_WINDOW)
        if r.returncode != 0:
            _py_cache["exe"] = ""
            _py_cache["error"] = "终端组件里 import winpty 失败（%s）" % note
    except Exception as exc:  # noqa: BLE001
        _py_cache["exe"] = ""
        _py_cache["error"] = "探测终端组件失败：%s" % exc
    return _result()


def warmup() -> dict:
    """启动时在后台调一次：做「找解释器 + import winpty 校验」，并**留一个完全就绪的会话**备用。

    ⚠ 以前是"起一个再关掉"（等于白热：下次打开仍要重起 host+pwsh）。现在**保留**它，
    首次点开直接 adopt 复用（实测首次打开 ~0.66s → ~0.15s）。
    """
    info = terminal_python(deep=True)
    if not info["ok"]:
        return info
    _spawn_warm(os.getcwd())
    return info


def terminal_available() -> bool:
    return terminal_python()["ok"]


def component_info() -> dict:
    """给 /pty/component 用：告诉前端终端能不能用、缺什么。"""
    info = terminal_python()
    return {"installed": info["ok"], "python": info["exe"],
            "source": (_py_cache.get("note") or
                       ("venv" if _venv_python() else ("site-packages" if _site_packages() else ""))),
            "error": info["error"]}


_shell_cache = {"checked": False, "exe": ""}


def _default_shell() -> str:
    if _shell_cache["checked"]:
        return _shell_cache["exe"]
    p = ""
    try:
        import external
        p = external.find_pwsh() or ""
    except Exception:  # noqa: BLE001
        p = ""
    _shell_cache["checked"] = True
    _shell_cache["exe"] = p
    return p


class PtySession:
    def __init__(self, cwd: str = "", cols: int = 100, rows: int = 30):
        info = terminal_python()
        if not info["ok"]:
            raise RuntimeError(info["error"])
        self.id = uuid.uuid4().hex[:12]
        self.cwd = cwd or os.getcwd()
        self.cols = max(20, min(400, int(cols or 100)))
        self.rows = max(5, min(200, int(rows or 30)))
        self.created = time.time()
        self.exit_code = None
        self._locked = False
        self._lock = threading.RLock()
        self._q: "queue.Queue" = queue.Queue(maxsize=8192)

        shell = _default_shell()
        argv = [info["exe"], HOST, "--cwd", self.cwd,
                "--cols", str(self.cols), "--rows", str(self.rows)]
        if shell:
            argv += ["--shell", shell]
        self.shell_name = os.path.basename(shell) if shell else "cmd.exe"
        self._proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0, env=info["env"], creationflags=CREATE_NO_WINDOW)
        self._reader = threading.Thread(target=self._read_loop, name="pty-out-" + self.id, daemon=True)
        self._reader.start()

    # ---- 内部 ----

    def _read_loop(self) -> None:
        stream = self._proc.stdout
        try:
            for raw in stream:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                t = msg.get("t")
                if t == "out":
                    try:
                        self._q.put(str(msg.get("d") or ""), timeout=1.0)
                    except queue.Full:
                        pass
                elif t == "exit":
                    if msg.get("code") is not None:
                        self.exit_code = int(msg.get("code") or 0)
                    break
        except Exception:  # noqa: BLE001
            pass
        self._locked = True
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    def _send(self, obj: dict) -> bool:
        with self._lock:
            if self._locked or not self._proc or self._proc.poll() is not None:
                return False
            try:
                self._proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
                self._proc.stdin.flush()
                return True
            except Exception:  # noqa: BLE001
                return False

    # ---- 对外 ----

    def read(self, timeout: float = 15.0):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return ""

    def write(self, data: str) -> bool:
        if not data:
            return True
        return self._send({"t": "in", "d": data})

    def resize(self, cols: int, rows: int) -> bool:
        self.cols = max(20, min(400, int(cols or self.cols)))
        self.rows = max(5, min(200, int(rows or self.rows)))
        return self._send({"t": "resize", "cols": self.cols, "rows": self.rows})

    def adopt(self, cwd: str, cols: int, rows: int) -> None:
        """把**预热好的**会话"让"给新的打开请求：改尺寸、必要时切目录。

        这样首次点开只付 HTTP + 一次 `cd` 的成本，省掉"重起 python+winpty+pwsh"（实测 ~0.66s → ~0.15s）。
        """
        self.resize(cols, rows)
        target = (cwd or "").strip()
        if not target:
            return
        try:
            same = os.path.normcase(os.path.normpath(target)) == os.path.normcase(os.path.normpath(self.cwd))
        except Exception:  # noqa: BLE001
            same = False
        if same:
            return
        sh = (self.shell_name or "").lower()
        if "cmd" in sh:
            self.write('cd /d "%s"\r' % target)
        else:
            self.write("Set-Location -LiteralPath '%s'\r" % target.replace("'", "''"))
        self.cwd = target

    @property
    def closed(self) -> bool:
        return self._locked or (self._proc is not None and self._proc.poll() is not None)

    def close(self) -> None:
        self._send({"t": "close"})
        with self._lock:
            if self._locked:
                return
            self._locked = True
        try:
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(0.05)
                try:
                    self._proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        finally:
            try:
                self._q.put_nowait(None)
            except queue.Full:
                pass

    def info(self) -> dict:
        return {
            "id": self.id, "cwd": self.cwd, "shell": self.shell_name,
            "cols": self.cols, "rows": self.rows,
            "alive": not self.closed, "exitCode": self.exit_code,
            "age": round(time.time() - self.created, 1),
        }


# ---------- 会话管理 ----------

_SESSIONS: dict = {}
_SESS_LOCK = threading.Lock()

# 预热池：一个「完全就绪」的会话（host + pwsh 都已起好），首次点开直接 adopt 复用
_WARM = {"sess": None}
_WARM_LOCK = threading.Lock()


def _spawn_warm(cwd: str = "") -> None:
    try:
        s = PtySession(cwd or os.getcwd(), 120, 30)
    except Exception:  # noqa: BLE001
        return
    with _WARM_LOCK:
        old = _WARM["sess"]
        _WARM["sess"] = s
    if old is not None and old is not s:
        try:
            old.close()
        except Exception:  # noqa: BLE001
            pass


def _take_warm(cwd: str = "", cols: int = 100, rows: int = 30):
    with _WARM_LOCK:
        s = _WARM["sess"]
        _WARM["sess"] = None
    if s is None:
        return None
    if s.closed:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
        return None
    try:
        s.adopt(cwd, cols, rows)
    except Exception:  # noqa: BLE001
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
        return None
    threading.Thread(target=_spawn_warm, args=(cwd,), daemon=True).start()   # 后台补一个，供下次秒开
    return s


def create(cwd: str = "", cols: int = 100, rows: int = 30) -> PtySession:
    with _SESS_LOCK:
        for sid in [k for k, s in _SESSIONS.items() if s.closed]:
            _SESSIONS.pop(sid, None)
        if len(_SESSIONS) >= _MAX_SESSIONS:
            oldest = min(_SESSIONS.values(), key=lambda s: s.created)
            oldest.close()
            _SESSIONS.pop(oldest.id, None)
    s = _take_warm(cwd, cols, rows)          # ★ 先看预热池（首次点开走这里 = 秒开）
    if s is None:
        s = PtySession(cwd, cols, rows)
    with _SESS_LOCK:
        _SESSIONS[s.id] = s
    return s


def get(sid: str):
    with _SESS_LOCK:
        return _SESSIONS.get(sid)


def remove(sid: str) -> bool:
    with _SESS_LOCK:
        s = _SESSIONS.pop(sid, None)
    if not s:
        return False
    s.close()
    return True


def list_all() -> list:
    with _SESS_LOCK:
        return [s.info() for s in _SESSIONS.values()]


def close_all() -> None:
    with _WARM_LOCK:
        w = _WARM["sess"]
        _WARM["sess"] = None
    if w is not None:
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass
    with _SESS_LOCK:
        items = list(_SESSIONS.items())
        _SESSIONS.clear()
    for _, s in items:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
