# -*- coding: utf-8 -*-
r"""进程工具（纯 ctypes，**不起任何子进程**）。

为什么不用 `tasklist` / `powershell`：
  它们都是控制台程序；在某些时刻（**关机收尾**、VM、被安全软件拦）新进程会以
  `0xc0000142`（STATUS_DLL_INIT_FAILED）初始化失败，Windows 会弹一个**模态**的
  「应用程序无法正常启动」对话框。实测（2026-10-01 关机日志）：
  `tasklist.exe` / `powershell.exe` 各弹一次 —— 而 `tasklist` 是守护进程**每 2 秒**都要起一次的。
  本模块用 Toolhelp32 在**本进程内**枚举进程：没有可失败的东西、不弹框、还更快。

另外提供 `set_error_mode()`：让本进程及其子进程**即使真的起不来也不弹那个硬错误框**
（子进程默认继承父进程的 error mode；Python 的 subprocess 不会加 CREATE_DEFAULT_ERROR_MODE）。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

# 错误模式：不弹「严重错误」框 / 不弹 GPF 框 / 不弹「找不到文件」框
SEM_FAILCRITICALERRORS = 0x0001
SEM_NOGPFAULTERRORBOX = 0x0002
SEM_NOOPENFILEERRORBOX = 0x8000

TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
_INVALID_HANDLE = ctypes.c_void_p(-1).value

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_k32.GetExitCodeProcess.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.SetErrorMode.argtypes = [wintypes.UINT]
_k32.SetErrorMode.restype = wintypes.UINT


def set_error_mode() -> None:
    """抑制「应用程序无法正常启动 (0xc0000142)」这类硬错误模态框（本进程 + 子进程继承）。

    幂等、无副作用；失败也不抛（最坏就是回到默认行为）。
    """
    try:
        _k32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX)
    except Exception:  # noqa: BLE001
        pass


def iter_processes():
    """产出 `(pid, exe_name)`；纯内存枚举（Toolhelp32），不起子进程。"""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            yield int(entry.th32ProcessID), str(entry.szExeFile)
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        try:
            _k32.CloseHandle(snap)
        except Exception:  # noqa: BLE001
            pass


def pids_named(name: str) -> set:
    """名字（如 `OpenCode.exe`，大小写不敏感）匹配的 PID 集合。"""
    want = str(name or "").strip().lower()
    if not want:
        return set()
    return {pid for pid, exe in iter_processes() if exe.lower() == want}


def is_running(name: str) -> bool:
    return bool(pids_named(name))


def pid_alive(pid: int) -> bool:
    """进程还活着吗（OpenProcess + GetExitCodeProcess，完全在本进程内）。"""
    try:
        pid = int(pid)
    except Exception:  # noqa: BLE001
        return False
    if pid <= 0:
        return False
    handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if _k32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        _k32.CloseHandle(handle)
