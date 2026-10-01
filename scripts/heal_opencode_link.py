# -*- coding: utf-8 -*-
"""Run scripts/heal-opencode-link.ps1 without any visible console window.

Used by the OpenCodeLinkHeal scheduled task: `pythonw.exe` is a GUI-subsystem
binary, and the PowerShell child is started with CREATE_NO_WINDOW, so neither
cmd.exe nor powershell.exe can flash a window.

ASCII only on purpose (the project keeps launcher scripts ASCII).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys

# 抑制「应用程序无法正常启动 (0xc0000142)」硬错误框：本脚本要起 powershell，
# 在某些机器/时刻它会初始化失败 → 不弹模态框（子进程继承该 error mode）。
try:
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
except Exception:  # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
PS1 = os.path.join(HERE, "heal-opencode-link.ps1")
CREATE_NO_WINDOW = 0x08000000


def main() -> int:
    if not os.path.isfile(PS1):
        return 1
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
           "-WindowStyle", "Hidden", "-File", PS1]
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                  stdin=subprocess.DEVNULL, cwd=HERE, creationflags=CREATE_NO_WINDOW)
    try:
        if "--wait" in sys.argv:          # launcher needs it done before OpenCode starts
            subprocess.run(cmd, timeout=300, **kwargs)
        else:
            subprocess.Popen(cmd, **kwargs)
    except Exception:  # noqa: BLE001
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
