# -*- coding: utf-8 -*-
"""ACP 子进程 + stdio 传输。

只干一件事：把 agent 子进程 stdout 上的 JSONL 解析成 dict 交给回调，并把 dict
紧凑地写回它的 stdin。JSON-RPC 的请求/响应配对在 client.py 里做。

ACP 传输约束（https://agentclientprotocol.com/protocol/v1/transports）：
  - 消息是 UTF-8 的 JSON-RPC 2.0，按 \\n 分帧，消息内不得含裸换行；
  - agent 的 stderr 只用于日志，客户端可转发或忽略；
  - agent 不得往 stdout 写非 ACP 内容。

⚠ Windows：必须带 CREATE_NO_WINDOW，否则会闪一个控制台窗口。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading

CREATE_NO_WINDOW = 0x08000000


def _git_bin_dirs() -> list:
    """自动找本机 Git 的 bin 目录（dsh 等 agent 需要 bash）。找不到就返回空。

    优先按 PATH 里的 git.exe 反推 Git 根目录，再补几个常见安装位置。
    """
    roots = []
    git = shutil.which("git")
    if git:
        d = os.path.dirname(git)
        base = os.path.basename(d).lower()
        # git.exe 常见位置：<root>\cmd、<root>\bin、<root>\mingw64\bin
        roots.append(os.path.dirname(d) if base in ("cmd", "bin", "mingw64") else d)
    roots += [r"D:\Git", r"C:\Program Files\Git", r"C:\Program Files (x86)\Git"]
    out = []
    for r in roots:
        for sub in ("bin", os.path.join("usr", "bin"), os.path.join("mingw64", "bin"), "cmd"):
            p = os.path.join(r, sub)
            if os.path.isdir(p) and p not in out:
                out.append(p)
    return out


def _pwsh_dirs() -> list:
    """自动找本机 PowerShell 7（pwsh）所在目录。

    为什么要它：Codex 在 Windows 上**优先用 pwsh**（没有才退回 powershell 5.1），
    而 PS7 默认 UTF-8 —— 读中文文件、工具输出都不会再乱码（PS5.1 默认 ANSI 会乱）。
    """
    out = []
    w = shutil.which("pwsh")
    if w:
        out.append(os.path.dirname(w))
    cands = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WindowsApps"),
        r"C:\Program Files\PowerShell\7",
    ]
    # 本项目所在盘的 agentlist\pwsh（便携安装位置）
    try:
        root = os.path.splitdrive(os.path.abspath(__file__))[0] + os.sep
        cands.append(os.path.join(root, "agentlist", "pwsh"))
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "pwsh.exe")) and c not in out:
            out.append(c)
    return out


def _inject_pwsh_path(env: dict) -> None:
    """把 pwsh 目录补进 env 的 PATH（已存在则跳过）。"""
    dirs = _pwsh_dirs()
    if not dirs:
        return
    key = next((k for k in env if k.lower() == "path"), "PATH")
    cur = str(env.get(key) or "")
    low = cur.lower()
    add = [d for d in dirs if d.lower() not in low]
    if add:
        env[key] = (";".join(add) + ";" + cur) if cur else ";".join(add)


def _inject_git_path(env: dict) -> None:
    """把自动找到的 Git bin 目录补进 env 的 PATH（已存在则跳过）。"""
    bins = _git_bin_dirs()
    if not bins:
        return
    key = next((k for k in env if k.lower() == "path"), "PATH")
    cur = str(env.get(key) or "")
    low = cur.lower()
    add = [b for b in bins if b.lower() not in low]
    if add:
        env[key] = (cur.rstrip(";") + ";" + ";".join(add)) if cur else ";".join(add)


def _inject_cu_path(env: dict) -> None:
    """把 cocraft 的 `cu`（桌面工具）薄壳目录补进 PATH。

    ACP 没有"客户端自定义工具"，所以给 agent 一双"手"的办法就是：
    命令在 PATH 里 + prompt 里讲一句（见 service.py 的工具卡）。
    """
    try:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
        sys.path.insert(0, os.path.join(root, "backend"))
        from computer.shim import inject_path  # noqa: WPS433
        inject_path(env)
    except Exception:  # noqa: BLE001
        pass


class AcpProcess:
    """一个 ACP agent 子进程。回调都在后台读线程里触发。"""

    def __init__(self, command, cwd=None, env=None,
                 on_message=None, on_log=None, on_exit=None):
        self.command = list(command)
        self.cwd = cwd
        self.env = env
        self._on_message = on_message or (lambda msg: None)
        self._on_log = on_log or (lambda line: None)
        self._on_exit = on_exit or (lambda code: None)
        self._proc = None
        self._lock = threading.Lock()
        self._threads = []
        self._dead = threading.Event()

    # ---- 生命周期 ----

    def start(self):
        if self._proc is not None:
            return
        # ⚠ env 必须是「叠加」而不是「替换」：只传 {DEEPSEEK_API_KEY:...} 会把
        #   PATH / USERPROFILE / SystemRoot 等全丢掉，子进程会出各种怪问题。
        env = dict(os.environ)
        if self.env:
            env.update({str(k): str(v) for k, v in self.env.items()})
        # ⚠ ACP 规定消息是 **UTF-8** 的 JSONL（读侧就是 utf-8 解码）。但 Windows 上
        #   Python 写的 agent（dsh 之类、以及我们自己的假 agent）在 stdout 是管道时
        #   会用**系统 ANSI 代码页**（本机 GBK）→ 中文全变 U+FFFD 乱码。
        #   这两个变量让「Python 写的 agent」也强制 UTF-8；Node 系 agent 不受影响。
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        _inject_git_path(env)          # 自动把本机 Git 的 bin 补进 PATH（bash 要用）
        _inject_pwsh_path(env)         # 自动把 pwsh 补进 PATH（Codex 优先用它，UTF-8 不乱码）
        _inject_cu_path(env)           # 自动把 `cu`（桌面工具）放进 PATH
        self._proc = subprocess.Popen(
            self.command, cwd=self.cwd, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            bufsize=0, creationflags=CREATE_NO_WINDOW,
        )
        self._spawn(self._read_stdout, "acp-stdout")
        self._spawn(self._read_stderr, "acp-stderr")

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 3.0):
        """先关 stdin 让它收尾，超时再强杀。"""
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._proc.wait(timeout=timeout)
        except Exception:  # noqa: BLE001
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
            try:
                self._proc.wait(timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        self._mark_dead()

    # ---- 读 ----

    def _read_stdout(self):
        stream = self._proc.stdout
        try:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception as exc:  # noqa: BLE001
                    self._on_log("[acp] 非法 JSON（%s）：%s" % (type(exc).__name__, line[:200]))
                    continue
                if not isinstance(msg, dict):
                    self._on_log("[acp] 非对象消息，忽略：%s" % line[:200])
                    continue
                try:
                    self._on_message(msg)
                except Exception as exc:  # noqa: BLE001
                    self._on_log("[acp] on_message 抛错：%r" % (exc,))
        finally:
            self._mark_dead()

    def _read_stderr(self):
        stream = self._proc.stderr
        try:
            while True:
                raw = stream.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    self._on_log("[agent] " + line)
        except Exception:  # noqa: BLE001
            pass

    def _mark_dead(self):
        if self._dead.is_set():
            return
        self._dead.set()
        code = None
        try:
            code = self._proc.poll()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._on_exit(code)
        except Exception:  # noqa: BLE001
            pass

    # ---- 写 ----

    def send(self, obj: dict) -> bool:
        """把 dict 紧凑写成一行 JSON 并 flush；管道断则返回 False。"""
        if self._proc is None or self._proc.poll() is not None:
            return False
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self._lock:
                # ⚠ bufsize=0 的 stdin 是无缓冲的：write() 可能**短写**（只写一部分），
                #   必须循环写满，否则大消息会写出半截 JSON、破坏 JSONL 分帧。
                view = memoryview(data)
                while view:
                    n = self._proc.stdin.write(view)
                    if not n:
                        break
                    view = view[n:]
                self._proc.stdin.flush()
            return True
        except Exception:  # noqa: BLE001
            self._mark_dead()
            return False
