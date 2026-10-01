# -*- coding: utf-8 -*-
"""在“外部程序”里打开某个目录：终端 / 编辑器（VS Code）/ 资源管理器。

为什么单独一个模块：这几件事都只跟本机有关，跟对话引擎（opencode / acp）无关，
放在 server.py 里会让路由文件更臃肿。这里的函数只做“尽力而为”的启动，
失败时回一句人话，绝不让面板崩。

启动方式都带 `CREATE_NEW_CONSOLE` / `DETACHED_PROCESS`，_Popen 出来的子进程
**不继承面板的生命周期**（关掉面板不等于关掉你打开的那个终端）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

CREATE_NEW_CONSOLE = 0x00000010
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008

# 启动外部 GUI/控制台程序时不弹黑框、不抢焦点用这套 flags
_SPAWN = 0
if sys.platform == "win32":
    _SPAWN = CREATE_NEW_CONSOLE | CREATE_NEW_PROCESS_GROUP


def _local_appdata() -> str:
    v = os.environ.get("LOCALAPPDATA") or ""
    if v:
        return v
    up = os.environ.get("USERPROFILE") or ""
    if up:
        return os.path.join(up, "AppData", "Local")
    return ""


def _pf(env: str) -> str:
    v = os.environ.get(env) or ""
    return v


def find_windows_terminal() -> str:
    """Windows Terminal 的 wt.exe：优先 PATH，其次 WindowsApps（Store 版装在这里）。"""
    for cand in ("wt.exe",):
        p = shutil.which(cand)
        if p:
            return p
    la = _local_appdata()
    if la:
        p = os.path.join(la, "Microsoft", "WindowsApps", "wt.exe")
        if os.path.isfile(p):
            return p
    return ""


def find_pwsh() -> str:
    """便携 / 系统 PowerShell 7；找不到就退回自带 powershell.exe（5.1）。"""
    p = shutil.which("pwsh.exe") or shutil.which("pwsh")
    if p:
        return p
    # 同盘 agentlist\pwsh（本项目便携安装位置）
    try:
        drive = os.path.splitdrive(os.path.abspath(__file__))[0]
        cand = os.path.join(drive + os.sep, "agentlist", "pwsh", "pwsh.exe")
        if os.path.isfile(cand):
            return cand
    except Exception:  # noqa: BLE001
        pass
    p = shutil.which("powershell.exe") or shutil.which("powershell")
    return p or ""


def find_vscode() -> str:
    """VS Code 的可执行文件：PATH 上的 code，或常见安装位置。
    ⚠ 优先返回真正的 `Code.exe` —— `code.cmd` 是批处理，直接 Popen 在部分环境会失败。"""
    p = shutil.which("code.cmd") or shutil.which("code")
    if p:
        ex = _cmd_to_exe(p)
        return ex or p
    cands = [
        os.path.join(_local_appdata(), "Programs", "Microsoft VS Code", "Code.exe"),
        os.path.join(_pf("ProgramFiles"), "Microsoft VS Code", "Code.exe"),
        os.path.join(_pf("ProgramFiles(x86)"), "Microsoft VS Code", "Code.exe"),
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return ""


def _cmd_to_exe(p: str) -> str:
    """`…\\bin\\code.cmd` → `…\\Code.exe`（同级上一层的真实程序）。"""
    if not p.lower().endswith(".cmd"):
        return ""
    base = os.path.dirname(os.path.dirname(p))
    cand = os.path.join(base, "Code.exe")
    return cand if os.path.isfile(cand) else ""


def _spawn(argv, cwd: str = "") -> str:
    """启动一个脱离面板生命周期的进程，返回空串（成功）或错误文本。
    ⚠ `.cmd` / `.bat` 不能直接 CreateProcess → 走 `cmd.exe /c`。"""
    try:
        argv = list(argv)
        first = str(argv[0])
        if first.lower().endswith((".cmd", ".bat")):
            comspec = os.environ.get("COMSPEC") or "cmd.exe"
            argv = [comspec, "/c"] + argv
        kw = {}
        if sys.platform == "win32":
            kw["creationflags"] = _SPAWN
        if cwd:
            kw["cwd"] = cwd
        subprocess.Popen(argv, close_fds=True, **kw)
        return ""
    except Exception as exc:  # noqa: BLE001
        return "%s: %s" % (type(exc).__name__, exc)


def open_terminal(directory: str) -> dict:
    """在 directory 里开一个终端。

    默认开 **PowerShell**（优先 PowerShell 7 / `pwsh`，其次 Windows PowerShell）—— 跟 VS Code 的
    集成终端一致；并且把它作为**显式命令**传给 Windows Terminal，**不让 WT 用它自己的默认 profile**
    （用户的默认 profile 可能是 cmd，看着就像"开出来是 cmd"）。实在没有 PowerShell 才退回 cmd。
    """
    sh = find_pwsh()
    wt = find_windows_terminal()
    if wt:
        argv = [wt, "-d", directory]
        if sh:
            argv.append(sh)              # ← 关键：显式指定 shell，别用 WT 默认 profile
        err = _spawn(argv)
        if not err:
            used = "Windows Terminal" + (" · " + os.path.basename(sh) if sh else "")
            return {"ok": True, "used": used, "cmd": wt, "shell": sh}
    if sh:
        # 直接以 directory 为工作目录开一个交互式 shell（PowerShell 5 要 -NoExit 才留在窗口里）
        argv = [sh, "-NoExit"] if sh.lower().endswith("powershell.exe") else [sh, "-NoLogo"]
        err = _spawn(argv, cwd=directory)
        if not err:
            return {"ok": True, "used": os.path.basename(sh), "cmd": sh, "shell": sh}
    # 最后兜底 cmd
    comspec = os.environ.get("COMSPEC") or "cmd.exe"
    err = _spawn([comspec], cwd=directory)
    if not err:
        return {"ok": True, "used": "cmd.exe", "cmd": comspec, "shell": ""}
    return {"ok": False, "error": "打不开终端：" + err}


def open_editor(directory: str) -> dict:
    code = find_vscode()
    if not code:
        return {"ok": False, "error": "未找到 VS Code（PATH 上没有 code，常见安装位置也没有）"}
    err = _spawn([code, directory])
    if err:
        return {"ok": False, "error": "启动 VS Code 失败：" + err}
    return {"ok": True, "used": "VS Code", "cmd": code}


def open_explorer(directory: str) -> dict:
    try:
        os.startfile(directory)  # noqa: S606 - Windows 专用，打开资源管理器
        return {"ok": True, "used": "资源管理器"}
    except AttributeError:
        pass
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "打开资源管理器失败：%s" % exc}
    ex = os.environ.get("WINDIR") or "C:\\Windows"
    ex = os.path.join(ex, "explorer.exe")
    err = _spawn([ex, directory])
    if err:
        return {"ok": False, "error": "打开资源管理器失败：" + err}
    return {"ok": True, "used": "资源管理器"}


_HANDLERS = {
    "terminal": open_terminal,
    "editor": open_editor,
    "explorer": open_explorer,
}


def open_in(what: str, directory: str) -> dict:
    """统一入口：what ∈ terminal / editor / explorer。"""
    fn = _HANDLERS.get(what)
    if not fn:
        return {"ok": False, "error": "不认识的目标：%s（可选 terminal / editor / explorer）" % what}
    d = (directory or "").strip().strip('"')
    if not d or not os.path.isdir(d):
        return {"ok": False, "error": "目录不存在：%s" % (d or "(空)")}
    return fn(d)
