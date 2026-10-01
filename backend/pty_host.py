# -*- coding: utf-8 -*-
r"""终端宿主进程（跑在 `runtime/venvs/terminal` 里，唯一 import `winpty` 的地方）。

和服务器之间用**极简 JSON 行协议**（都走二进制流，UTF-8）：
  服务器 → 宿主（stdin 每行一个 JSON）：
    {"t":"in","d":"<按键文本>"}         写入终端
    {"t":"resize","cols":100,"rows":30}  改变尺寸
    {"t":"close"}                        关闭
  宿主 → 服务器（stdout 每行一个 JSON）：
    {"t":"out","d":"<终端输出>"}
    {"t":"exit","code":0}

为什么不直接在 server.py 里 import winpty：server 跑在**系统 Python**（纯标准库），
而 winpty 是可选组件、装在隔离 venv 里。分进程也顺带隔离了崩溃。

⚠ 这里不用 ConPTY 的 ctypes 手写：本机实测 ctypes 版子进程**挂不上伪控制台**
（多种 lpValue / 继承 / 对齐组合都试过，读不到任何输出），pywinpty 一次就通。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading

# 抑制「应用程序无法正常启动 (0xc0000142)」硬错误框（本宿主会拉起 shell 子进程）。
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except Exception:  # noqa: BLE001
    pass


def _pick_shell() -> list:
    """优先 pwsh（UTF-8 友好），其次 Windows PowerShell，最后 cmd。"""
    here = os.path.dirname(os.path.abspath(__file__))
    if here in sys.path:
        sys.path.remove(here)
    sys.path.insert(0, here)
    try:
        import external  # noqa: WPS433 - 同目录，纯标准库
        p = external.find_pwsh()
        if p:
            return [p, "-NoLogo"]
    except Exception:  # noqa: BLE001
        pass
    return [os.environ.get("COMSPEC") or "cmd.exe"]


def _emit(obj: dict) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


def main() -> int:
    cwd = ""
    cols, rows = 100, 30
    shell = None
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--cwd" and i + 1 < len(args):
            cwd = args[i + 1]; i += 2
        elif a == "--cols" and i + 1 < len(args):
            cols = int(args[i + 1]); i += 2
        elif a == "--rows" and i + 1 < len(args):
            rows = int(args[i + 1]); i += 2
        elif a == "--shell" and i + 1 < len(args):
            shell = [args[i + 1]]; i += 2
        else:
            i += 1

    import winpty

    argv = shell or _pick_shell()
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    try:
        proc = winpty.PtyProcess.spawn(argv, cwd=cwd or os.getcwd(), env=env,
                                       dimensions=(rows, cols))
    except Exception as exc:  # noqa: BLE001
        _emit({"t": "exit", "code": -1, "error": "%s: %s" % (type(exc).__name__, exc)})
        return 1

    stop = threading.Event()

    def pump() -> None:
        while not stop.is_set():
            try:
                chunk = proc.read(4096)
            except EOFError:
                _emit({"t": "exit", "code": int(getattr(proc, "exitstatus", 0) or 0)})
                return
            except Exception as exc:  # noqa: BLE001
                _emit({"t": "exit", "code": -1, "error": "%s: %s" % (type(exc).__name__, exc)})
                return
            if chunk:
                _emit({"t": "out", "d": chunk})

    t = threading.Thread(target=pump, name="pty-pump", daemon=True)
    t.start()

    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            kind = msg.get("t")
            if kind == "in":
                try:
                    proc.write(str(msg.get("d") or ""))
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "resize":
                try:
                    proc.setwinsize(int(msg.get("rows") or rows), int(msg.get("cols") or cols))
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "close":
                break
    except Exception:  # noqa: BLE001
        pass
    finally:
        stop.set()
        try:
            proc.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
