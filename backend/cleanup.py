# -*- coding: utf-8 -*-
"""清理本项目遗留的辅助进程（音乐服务 / 频谱 / SMTC / ACP agent / 终端宿主）。

为什么需要：这些进程是“脱离式”启动的，服务器被 stop.bat 强杀时跑不到自己的退出钩子，
而 Windows 的 SO_REUSEADDR 又允许旧进程继续占着同一个端口，于是会越积越多。
本模块按**命令行特征**找人，再用 `taskkill /T` 连子进程一起清掉；只匹配本项目自己的路径。

用法：
    python backend/cleanup.py          # 清掉所有遗留辅助进程
    python backend/cleanup.py --list   # 只看会清哪些
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

# 抑制「应用程序无法正常启动 (0xc0000142)」硬错误框：本模块要起 powershell/taskkill，
# 关机收尾时它们可能初始化失败 → 不弹模态框（子进程继承该 error mode）。
try:
    _here = os.path.dirname(os.path.abspath(__file__))
    if _here in sys.path:
        sys.path.remove(_here)
    sys.path.insert(0, _here)
    import procutils as _proc
    _proc.set_error_mode()
except Exception:  # noqa: BLE001
    _proc = None

NO_WINDOW = 0x08000000

# 只匹配本项目自己的辅助进程；**故意不包含 server.py / watch.py**：
# 那由 stop.bat 按端口处理，避免 tools/acp_demo.py 之类第二个服务误杀主服务。
PATTERNS = [
    r"opencode-ui[\\/]tools[\\/]music_service\.py",
    r"opencode-ui[\\/]tools[\\/]spectrum\.py",
    r"opencode-ui[\\/]tools[\\/]smtc-daemon\.ps1",
    r"opencode-ui[\\/]backend[\\/]pty_host\.py",
    r"agentlist[\\/]codex[\\/]node_modules[\\/]@agentclientprotocol[\\/]codex-acp",
    r"agentlist[\\/]deepseekharness[\\/]adapter[\\/]node_modules[\\/]dsh-acp",
]


def _ps_json(script: str):
    """跑一段 PowerShell 并解析 JSON 输出；失败回 None。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW,
        ).stdout or ""
    except Exception:  # noqa: BLE001
        return None
    out = out.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        return None


def find_stale(exclude=None) -> list:
    """返回匹配命令行特征的 PID 列表（排除 exclude 里的 PID）。"""
    exclude = {int(x) for x in (exclude or [])}
    regex = "|".join(PATTERNS)
    script = (
        "$re='" + regex + "'; "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and ($_.CommandLine -match $re) } | "
        "Select-Object -ExpandProperty ProcessId | ConvertTo-Json -Compress"
    )
    data = _ps_json(script)
    if not isinstance(data, list):
        data = [data] if isinstance(data, int) else []
    return [int(p) for p in data if int(p) not in exclude]


def kill_pid(pid: int) -> bool:
    """结束一个进程树（/T 会把 venv 转发壳拉起的真实解释器一起带走）。"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, creationflags=NO_WINDOW, timeout=15)
        return True
    except Exception:  # noqa: BLE001
        return False


def kill_stale(exclude=None) -> int:
    pids = find_stale(exclude=exclude)
    for pid in pids:
        kill_pid(pid)
    return len(pids)


def main() -> int:
    if "--list" in sys.argv:
        pids = find_stale()
        print("[list] %d process(es): %s" % (len(pids), ",".join(str(p) for p in pids)))
        return 0
    n = kill_stale()
    print("[OK] cleaned %d process(es)" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
