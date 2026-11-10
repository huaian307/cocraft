#!/usr/bin/env python
r"""restart_server.py -- 只重启「面板 HTTP 服务」(8787)，不动窗口 / 不动 OpenCode。

为什么需要它：
  **关掉面板窗口再打开 ≠ 重启服务**。守护进程（watch.py, 8788）会一直把 8787 保活，
  所以"重启面板"通常只是换了个窗口进程，后端仍是几小时前启动的那个 —— 新接口
  （`/audit/brief`、`/cu/config`…）根本不存在，界面就会报 400/404。
  `/healthz` 的 `pid` / `startedAt` 才是"后端新不新"的唯一证据。

做法：
  1. 找到**监听该端口**的进程（按端口认人，不按进程名猜），`taskkill /F`；
  2. 等守护进程把它拉起来（默认最多 20 秒，watch.py 每 2 秒检查一次）；
  3. 起不来就**自己拉起** `backend/server.py`（窗口/OpenCode 依旧不动）；
  4. 打一行结果：新 pid / 启动时间 / ui 版本。

用法：
  python tools\restart_server.py                 :: 重启 8787（默认）
  python tools\restart_server.py --port 17887    :: 安装版
  python tools\restart_server.py --check         :: 只看状态，不重启
  python tools\restart_server.py --kill-only     :: 只杀，不等待/不代拉
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CREATE_NO_WINDOW = 0x08000000


def _force_utf8():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def listener_pid(port: int):
    r"""谁在 LISTEN 这个端口（netstat + 精确匹配端口列，避免 8787 命中 87870）。

    ⚠ Windows 上有时**监听项的属主 pid 报 0**（本机实测过一次：8787 明明在监听、
      `/healthz` 也通，但 netstat 的最后一列是 0）→ 这时只靠端口认人会以为"服务没在跑"，
      然后新进程绑不上端口、脚本却打印**旧 pid** 说"已重启"（误导）。
      所以：端口查不到 pid 时，再按**命令行**兜底找 `backend\server.py`（只认这一个特征）。
    """
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             timeout=15).stdout.decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return 0
    pat = re.compile(r"^\s*TCP\s+\S+:(\d+)\s+\S+\s+LISTENING\s+(\d+)\s*$")
    for line in out.splitlines():
        m = pat.match(line)
        if m and int(m.group(1)) == int(port) and int(m.group(2)):
            return int(m.group(2))
    return 0


def server_pids_by_cmdline(port: int) -> list:
    """按命令行兜底找面板服务（`backend\\server.py` + `--port <port>`）。

    ⚠ 只认**这两个特征同时命中**：别把 watch.py / panel_window.py / tray_host.py 一起杀了
      （它们命令行里也有 `--port 8787`，但没有 `server.py`）。
    """
    try:
        p = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, timeout=25)
        raw = (p.stdout or b"").decode("utf-8", "replace").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for it in data:
        cmd = str((it or {}).get("CommandLine") or "")
        if "server.py" not in cmd.lower():
            continue
        if re.search(r"--port\s+%d\b" % int(port), cmd) or re.search(r"--port\s+%d\b" % int(port), cmd):
            out.append(int(it.get("ProcessId") or 0))
    return [x for x in out if x]


def port_open(port: int, timeout=0.6) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def health(port: int) -> dict:
    """读 /healthz（**不走系统代理** —— 本机请求被代理会直接连不上，见踩坑 #45）。"""
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open("http://127.0.0.1:%d/healthz" % int(port), timeout=4) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:160]}


def kill(pid: int) -> bool:
    try:
        subprocess.run(["taskkill", "/PID", str(int(pid)), "/F", "/T"],
                       capture_output=True, timeout=20)
        return True
    except Exception:  # noqa: BLE001
        return False


def spawn(port: int) -> bool:
    server = os.path.join(ROOT, "backend", "server.py")
    if not os.path.isfile(server):
        return False
    try:
        subprocess.Popen([sys.executable, server, "--port", str(int(port))],
                         cwd=ROOT, creationflags=CREATE_NO_WINDOW,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:  # noqa: BLE001
        return False


def main(argv=None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description="restart the cocraft panel HTTP service only")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--wait", type=float, default=20.0, help="等守护进程拉起的最长秒数")
    ap.add_argument("--check", action="store_true", help="只看状态，不重启")
    ap.add_argument("--kill-only", action="store_true", help="只杀，不等待/不代拉")
    args = ap.parse_args(argv)
    port = int(args.port)

    before = health(port)
    if args.check:
        print(json.dumps(before, ensure_ascii=False, indent=2))
        return 0 if before.get("ok") else 1

    pid = listener_pid(port)
    if not pid:
        # ★端口属主报 0 的兜底（本机实测踩到过）：按命令行找 panel server
        cands = server_pids_by_cmdline(port)
        if cands:
            print("[OK]   端口属主读不到（netstat 报 0）→ 按命令行找到面板服务 pid=%s，正在停掉…"
                  % ",".join(str(x) for x in cands))
            for one in cands:
                kill(one)
            pid = cands[0]
        else:
            print("[INFO] %d 端口没有监听进程（服务没在跑）—— 直接拉起" % port)
    else:
        print("[OK]   找到监听 %d 的进程 pid=%d，正在停掉…" % (port, pid))
        kill(pid)
    if args.kill_only:
        return 0

    # 等守护进程（watch.py）用新代码拉起来
    deadline = time.time() + max(1.0, float(args.wait))
    while time.time() < deadline:
        if port_open(port):
            h = health(port)
            # ★必须确认**换人了**：pid 没变说明还是旧进程（绑着端口、新进程起不来）
            if h.get("ok") and (not pid or int(h.get("pid") or 0) != int(pid)):
                print("[OK]   已重启：pid=%s 启动于 %s（ui %s）"
                      % (h.get("pid"), h.get("startedAt"), h.get("ui")))
                print("       判断后端新不新只看 pid / startedAt，别看 ui。")
                return 0
        time.sleep(0.5)

    print("[WARN] %.0f 秒内没等到新进程（守护进程可能也没在跑）—— 自己起一个（窗口/OpenCode 不动）…"
          % float(args.wait))
    if not spawn(port):
        print("[BAD]  也起不来：请手工跑  pythonw backend\\server.py --port %d" % port)
        return 1
    deadline = time.time() + 25.0
    while time.time() < deadline:
        h = health(port)
        if h.get("ok") and (not pid or int(h.get("pid") or 0) != int(pid)):
            print("[OK]   已由本脚本拉起：pid=%s 启动于 %s（ui %s）"
                  % (h.get("pid"), h.get("startedAt"), h.get("ui")))
            return 0
        time.sleep(0.5)
    print("[BAD]  起来了但 /healthz 不通（或 pid 没变 = 旧进程还占着端口；看 runtime\\logs\\ 下的日志）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
