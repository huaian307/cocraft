# -*- coding: utf-8 -*-
r"""cocraft 守护进程（无窗口运行）

职责：
  1. 确保本地面板服务（server.py）在 127.0.0.1:8787 上活着，挂了就拉起来；
  2. 面板窗口该开就开 —— ⚠ **是否依赖 OpenCode 由当前引擎决定**（见 opencode_required()）：
       引擎 = opencode → 等 OpenCode 起来再开窗，开完最小化它，关窗联动关掉它；
       引擎 = acp    → **完全独立**：不看/不等/不碰 OpenCode，关窗也不关任何东西。
  3. 面板窗口用【独立浏览器 profile】打开（完全独立实例，不干扰日常浏览器）；
  4. 关掉面板窗口 → 一并关掉 OpenCode（仅 opencode 模式；可用 --no-kill 或 no-kill 文件关闭）；
  5. 面板窗口开 → 任务栏**自动隐藏**（鼠标贴底边才浮现），关 → 恢复**常驻可见**。
     （只有常驻守护负责这件事：--once 自己马上退出，没人能把它恢复回来。）

"面板窗口还开着吗" 怎么判断：
  不靠进程句柄（会漏、会误判），而是靠**页面心跳** —— 页面每 5 秒 POST 一次
  /heartbeat，窗口一关心跳即停。刚开窗后有一段时间等页面加载，用一个 settle 窗口兜住。
  兜底：如果心跳没有但浏览器确实在跑（例如页面还是旧版、没发心跳），仍然算"开着"。

用法：
    pythonw watch.py               # 常驻（由 launch_opencode.py / 启动文件夹调用）
    python  watch.py --once        # 只做一次：确保服务 + 需要就开窗口，然后退出
    python  watch.py --no-kill     # 关窗口时不关 OpenCode
    python  watch.py --restore-taskbar   # 只把任务栏恢复常驻可见，然后退出（stop.bat 兜底）
"""

from __future__ import annotations

import atexit
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RUNTIME_DIR = os.path.join(ROOT, "runtime")
LOGS_DIR = os.path.join(RUNTIME_DIR, "logs")


def _argv_int(flag: str, default: int) -> int:
    """从命令行取一个整数端口（`--port 17887`）。取不到就用 default。"""
    try:
        i = sys.argv.index(flag)
        return int(sys.argv[i + 1])
    except Exception:  # noqa: BLE001
        return default


# 端口：命令行 > runtime/state/_panel.json（安装包写的） > 默认 8787/8788/8790
try:
    if HERE in sys.path:
        sys.path.remove(HERE)
    sys.path.insert(0, HERE)
    from panel_port import read_ports as _read_ports
    _PORTS = _read_ports()
except Exception:  # noqa: BLE001
    _PORTS = {"port": 8787, "lock": 8788, "music": 8790}
HOST = "127.0.0.1"
PORT = _argv_int("--port", int(_PORTS.get("port", 8787)))
LOCK_PORT = _argv_int("--lock-port", int(_PORTS.get("lock", 8788)))
MUSIC_PORT = _argv_int("--music-port", int(_PORTS.get("music", 8790)))
URL = f"http://{HOST}:{PORT}"
ALIVE_URL = f"{URL}/alive"
os.makedirs(LOGS_DIR, exist_ok=True)
LOG = os.path.join(LOGS_DIR, "watch.log")

# 任务栏联动：同目录的 taskbar.py（缺失/出错都不影响守护主流程）
try:
    if HERE in sys.path:
        sys.path.remove(HERE)
    sys.path.insert(0, HERE)
    import taskbar as _taskbar
except Exception:  # noqa: BLE001
    _taskbar = None

# 进程查询走纯 ctypes 的 procutils（不再起 tasklist —— 关机时它会弹 0xc0000142 模态框）。
# 顺手打开"错误模式抑制"：本进程 + 所有子进程起不来时都不弹那个硬错误框。
try:
    if HERE in sys.path:
        sys.path.remove(HERE)
    sys.path.insert(0, HERE)
    import procutils as _proc
    _proc.set_error_mode()
except Exception:  # noqa: BLE001
    _proc = None

TICK_SECONDS = 0.4           # 面板心跳轮询间隔（很便宜：一次本机 HTTP）—— 决定"发现关窗"有多快
POLL_SECONDS = 2.0           # OpenCode 进程检查间隔（较贵：要起一次 tasklist）
GRACE_SECONDS = 1.0          # 关窗后留给"反悔"的时间（固定 1 秒，无随机）
SETTLE_SECONDS = 40.0        # 刚开窗后，等页面加载并发第一个心跳的时间
MINIMIZE_DELAY = 1.5         # 开完面板窗口后，等它露面再把 OpenCode 最小化
# ⚠ 我们自己（重新）拉起过面板服务后的这段"静默期"内，**不做关窗联动**：
#   服务重启会让页面心跳断档几秒，很容易被误判成"用户把窗口关了"→ 把 OpenCode 一起杀掉
#   （实测事故：2026-10-01 12:01 我重启服务，守护在 12:01:40 执行联动把 OpenCode 杀了）。
RESTART_QUIET = 45.0
_server_restarted_at = 0.0
OPENCODE_IMAGES = ("OpenCode.exe", "opencode-cli.exe", "opencode.exe")

PROFILE_DIR = os.path.join(RUNTIME_DIR, "browser-profile")

# 关掉面板窗口时是否一并关掉 OpenCode。
# 想关掉这个行为：命令行加 --no-kill，或在 runtime/ 放一个名为 no-kill 的空文件。
KILL_OPENCODE = ("--no-kill" not in sys.argv) and not os.path.exists(os.path.join(RUNTIME_DIR, "no-kill"))

# 可选：OpenCode 退出时是否也关掉面板窗口。默认关，避免"重启 OpenCode 时窗口闪一下"。
CLOSE_WINDOW_WHEN_OPENCODE_EXITS = False

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
]

DETACHED = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW
NO_WINDOW = 0x08000000

# ---- 原生窗口（WebView2 / pywebview，可选组件）----
STATE_DIR = os.path.join(RUNTIME_DIR, "state")
PANEL_WINDOW_PY = os.path.join(HERE, "panel_window.py")
PANEL_TITLE = "cocraft · 絵梨衣"
_NATIVE_MARK = "panel_window.py"

# 原生窗口"起手卡死"看门狗：WebView2 有时初始化要几十秒（期间窗口是白屏），
# 超过这个时间还没收到页面心跳就判定卡死 → 结束它、改用浏览器窗口（本次运行不再试原生）。
NATIVE_LOAD_TIMEOUT = 30.0
_native_disabled = False


def panel_python() -> tuple:
    """跑原生窗口用哪个解释器 → `(exe, extra_env, ok)`。没装这个可选组件就 `("", {}, False)`。

    ⚠ venv 的 `Scripts\\pythonw.exe` 是**转发壳**：它会把命令再交给 base 解释器（多一个进程），
      窗口挂在**子进程**上 → 守护用 `proc.pid` 找不到窗口（任务栏/最小化判定会失灵，实测踩到）。
      所以这里改成**直接用 base 解释器 + `PYTHONPATH=<venv 的 site-packages>`**（单进程、窗口归自己）。
    """
    vroot = os.path.join(RUNTIME_DIR, "venvs", "panel")
    if os.path.isfile(os.path.join(vroot, "Scripts", "pythonw.exe")) \
            or os.path.isfile(os.path.join(vroot, "Scripts", "python.exe")):
        base = ""
        try:
            with open(os.path.join(vroot, "pyvenv.cfg"), "r", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip().lower().startswith("home"):
                        base = line.split("=", 1)[1].strip()
                        break
        except Exception:  # noqa: BLE001
            base = ""
        exe = ""
        for cand in (os.path.join(base, "pythonw.exe") if base else "",
                     os.path.join(base, "python.exe") if base else "",
                     sys.executable):
            if cand and os.path.isfile(cand):
                exe = cand
                break
        site = os.path.join(vroot, "Lib", "site-packages")
        cur = os.environ.get("PYTHONPATH") or ""
        return exe or sys.executable, {"PYTHONPATH": site + (os.pathsep + cur if cur else "")}, True
    site = os.path.join(RUNTIME_DIR, "site-packages", "panel")
    if os.path.isdir(site):
        cur = os.environ.get("PYTHONPATH") or ""
        return sys.executable, {"PYTHONPATH": site + (os.pathsep + cur if cur else "")}, True
    return "", {}, False


def native_ok() -> bool:
    return os.path.isfile(PANEL_WINDOW_PY) and bool(panel_python()[0])


def panel_window_pref() -> str:
    """设置里的窗口模式：`native` / `browser` / `auto`（默认）。"""
    try:
        with open(os.path.join(STATE_DIR, "_panel_window.json"), "r", encoding="utf-8") as fh:
            v = str(json.load(fh).get("mode") or "").strip().lower()
            return v if v in ("native", "browser") else "auto"
    except Exception:  # noqa: BLE001
        return "auto"


def want_native() -> bool:
    """该不该用原生窗口：用户选了 browser 就永远不用；选了 native/auto 则**装了组件才用**。"""
    pref = panel_window_pref()
    if pref == "browser":
        return False
    return native_ok()


def _panel_proc_filter() -> str:
    """PowerShell 条件：是不是「我们的面板进程」。

    两种形态都算：① Edge/Chrome 的专属 profile 窗口；② 原生窗口（`panel_window.py`）。
    ⚠ 必须限定进程名，否则执行这段 PowerShell 的自己会因命令行含该路径而被误数。
    """
    return ("(($_.Name -match 'msedge|chrome' -and $_.CommandLine -like '*" + PROFILE_DIR + "*') "
            "-or ($_.Name -match 'python' -and $_.CommandLine -like '*" + _NATIVE_MARK + "*'))")


def log(msg: str) -> None:
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}  {msg}\n")
    except Exception:
        pass


_ps_state = {"broken": False}


def ps(command: str) -> str:
    """跑一段 PowerShell（拿进程列表 / 关窗等兜底判据用）。

    ⚠ 有些机器（尤其 VM）里 `powershell.exe` **起不来**，会弹 `0xc0000142` 模态框；
    一旦遇到就**永久停用**，不再反复尝试 —— 否则会不停弹框、还会把守护循环卡住。
    """
    if _ps_state["broken"]:
        return ""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
        )
        rc = int(r.returncode or 0) & 0xFFFFFFFF
        if rc == 0xC0000142:                     # STATUS_DLL_INIT_FAILED：加载器就失败了
            _ps_state["broken"] = True
            log("PowerShell 无法启动(0xc0000142)，后续不再调用它")
            return ""
        return r.stdout or ""
    except Exception as exc:  # noqa: BLE001
        _ps_state["broken"] = True
        log(f"PowerShell 不可用，后续不再调用: {exc}")
        return ""


# ---------------- 任务栏联动 ----------------
# 面板开着且未最小化、并且设置开关打开 → 任务栏自动隐藏（鼠标贴底边才浮现）；
# 面板最小化 / 设置开关关闭 / 面板关闭 → 恢复常驻可见。
# "是不是我们藏的"由 taskbar.py 的标记文件记账，
# 所以守护重启/被强杀都不会覆盖用户自己的任务栏设置。

def taskbar_sync(hidden: bool) -> None:
    """把任务栏同步到目标状态，但只在状态真的变化时动作。

    - 要隐藏：仅当当前不是我们藏的时候才写标记 + ABM_SETSTATE；
    - 要显示：仅当标记存在（= 我们藏过）才恢复，**不覆盖用户自己的任务栏设置**。
    """
    if _taskbar is None:
        return
    try:
        marked = _taskbar.is_marked()
        if hidden and not marked:
            if _taskbar.set_hidden(True):
                log("任务栏 → 自动隐藏")
        elif not hidden and marked:
            if _taskbar.set_hidden(False):
                log("任务栏 → 常驻可见")
    except Exception as exc:  # noqa: BLE001
        log(f"任务栏同步失败: {exc}")


def restore_taskbar_if_marked() -> None:
    if _taskbar is None:
        return
    try:
        if _taskbar.restore_if_marked():
            log("任务栏 → 常驻可见（兜底恢复）")
    except Exception as exc:  # noqa: BLE001
        log(f"任务栏兜底恢复失败: {exc}")


# ---------------- 面板服务 ----------------

def port_open(host: str = HOST, port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def ensure_server() -> bool:
    global _server_restarted_at
    if port_open():
        return False
    log("面板服务不在，启动 server.py")
    _server_restarted_at = time.time()          # 记下"我们刚重启过" → 静默期内不联动
    subprocess.Popen(
        [sys.executable, os.path.join(HERE, "server.py"),
         "--port", str(PORT), "--music-port", str(MUSIC_PORT)],
        cwd=ROOT, creationflags=DETACHED,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(40):
        time.sleep(0.5)
        if port_open():
            log("面板服务已就绪")
            return True
    log("面板服务启动超时")
    return False


def panel_status() -> dict:
    """面板状态：心跳 + 用户的任务栏开关（由 server.py 提供）。"""
    try:
        with urllib.request.urlopen(ALIVE_URL, timeout=3) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def panel_alive() -> bool:
    """页面心跳还在吗（= 面板窗口还开着）。"""
    return bool(panel_status().get("alive"))


def upstream_ready() -> bool:
    """当前引擎是否已经可用（我们的 /healthz 会去问它）。

    ⚠ 这里问的是**我们的** 8787，不是 OpenCode —— server.py 的 /healthz 是按当前引擎答的，
      所以 ACP 模式下它照样能就绪（不需要 OpenCode）。
    """
    try:
        with urllib.request.urlopen(f"{URL}/healthz", timeout=4) as r:
            return bool(json.loads(r.read().decode("utf-8")).get("ok"))
    except Exception:
        return False


def engine_status() -> dict:
    """问面板服务：当前引擎 + 每个引擎是否可用（server.py 的 /engine/status）。"""
    try:
        with urllib.request.urlopen(f"{URL}/engine/status", timeout=4) as r:
            d = json.loads(r.read().decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def opencode_required(status: dict = None) -> bool:
    """面板是否**必须**依赖 OpenCode（决定"要不要等它 / 最小化它 / 关窗时杀它"）。

    - 引擎 = `acp`（或别的）→ **False**：面板独立跑，完全不碰 OpenCode；
    - 引擎 = `opencode` 且它确实可用 → True（保留原来的启动/最小化/关窗联动）；
    - 引擎 = `opencode` 但**没装**（`engines[].available == False`）→ False：别死等一个不存在的后端；
    - 拿不到引擎信息（服务还没起来 / 老后端没有 engines 字段）→ 回落到"OpenCode 在跑就当它需要"，
      这样探测失败时行为与改造前一致。
    """
    st = status if isinstance(status, dict) else engine_status()
    active = str((st or {}).get("active") or "")
    if not active:
        return opencode_running()
    if active != "opencode":
        return False
    for e in ((st or {}).get("engines") or []):
        if isinstance(e, dict) and str(e.get("id")) == "opencode" and e.get("available") is False:
            return False
    return True


def should_open_now(required: bool, running: bool, alive: bool,
                    skip_open: bool, closed_at) -> bool:
    """该不该开面板窗口（纯函数，方便单测；main() 直接用它）。

    ⚠ 关键区别：`required=True`（opencode 模式）时**必须** OpenCode 在跑才开；
      `required=False`（ACP 模式）只看"窗口没开 + 用户没主动关过 + 不在反悔期"。
    """
    if alive or skip_open or closed_at is not None:
        return False
    return bool(running) if required else True


def should_kill_on_close(required: bool, browser_alive: bool = False) -> bool:
    """关窗后要不要执行"联动关 OpenCode"（ACP 模式不关任何东西）。

    ⚠ `browser_alive` 是**第二道确认**：心跳超时可能只是页面被节流/卡住（2026-09-26 实测踩过：
      并行跑测试把页面定时器压到 8s 没发心跳 → 守护进程判定"窗口已关" → 差点在反悔期后
      杀掉 OpenCode，连带杀掉正在跑的会话）。所以只有"浏览器进程也真的没了"才联动。
    """
    return bool(required and KILL_OPENCODE and not browser_alive)


def panel_browser_alive(proc=None, adopted_pid: int = 0) -> tuple:
    """面板浏览器还活着吗 → `(alive, pid)`。心跳失灵时的兜底（较贵，只在反悔期结束时用）。

    判据按可靠性：① 我们自己拉起的窗口进程句柄（最准，0 成本）
    → ② 采纳的外部窗口 PID（廉价存活检查）→ ③ 按专属 profile 现查一次（要起 PowerShell）。
    """
    # ⚠ 判据回到「句柄最准」：**只有**拿到句柄/采纳 PID，或现查到了，才下结论；
    #   查不到就是"没开"（保持老行为 —— 否则 `fresh` 会被永远置真，
    #   既不会联动关 OpenCode、也永远不再重开窗口，实测踩到）。
    #   "服务刚重启导致心跳断档"的防误杀由 `RESTART_QUIET` 负责，不在这里兜。
    if proc is not None and proc.poll() is None:
        return True, int(getattr(proc, "pid", 0) or 0)
    if adopted_pid and pid_alive(adopted_pid):
        return True, int(adopted_pid)
    found = panel_main_pid()
    return (bool(found), found)


def should_minimize_opencode(required: bool) -> bool:
    """开完面板后要不要最小化 OpenCode 自己的窗口。"""
    return bool(required)


def wait_upstream(timeout: float = 45.0, required: bool = True) -> bool:
    """等后端就绪再开窗，避免开出一个"连不上"的页面。
    等待期间若（opencode 模式下）OpenCode 退出了，就直接放弃开窗。"""
    if upstream_ready():
        return True
    log("后端尚未就绪，等它起来再开窗…")
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(1.0)
        if upstream_ready():
            log(f"后端已就绪（等待 {time.time() - t0:.0f}s）")
            return True
        if required and not opencode_running():
            log("等待期间 OpenCode 已退出，取消开窗")
            return False
    log(f"等待后端超时（{timeout:.0f}s），仍然开窗（页面会自动重连）")
    return False


# ---------------- OpenCode ----------------

def opencode_running() -> bool:
    # 首选纯 ctypes（不起子进程 → 关机时不会弹 tasklist 的 0xc0000142 框）
    if _proc is not None:
        try:
            return _proc.is_running("OpenCode.exe")
        except Exception:  # noqa: BLE001
            pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq OpenCode.exe", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        ).stdout or ""
        return "opencode.exe" in out.lower()
    except Exception as exc:  # noqa: BLE001
        log(f"检测 OpenCode 失败: {exc}")
        return False


def kill_opencode() -> None:
    log("关闭 OpenCode（含后台服务）")
    for image in OPENCODE_IMAGES:
        subprocess.run(["taskkill", "/IM", image, "/F", "/T"],
                       capture_output=True, creationflags=NO_WINDOW)


# ---------------- 最小化 OpenCode 自己的窗口 ----------------

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
SW_MINIMIZE = 6
_user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
_user32.FindWindowW.restype = wintypes.HWND
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.IsIconic.restype = wintypes.BOOL


def opencode_pids() -> set[int]:
    if _proc is not None:
        try:
            return set(_proc.pids_named("OpenCode.exe"))
        except Exception:  # noqa: BLE001
            pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq OpenCode.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW,
        ).stdout or ""
    except Exception as exc:  # noqa: BLE001
        log(f"取 OpenCode PID 失败: {exc}")
        return set()
    pids = set()
    for raw in out.splitlines():
        line = raw.strip()
        if not line.startswith('"'):
            continue
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "opencode.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def minimize_opencode() -> int:
    """把 OpenCode 桌面版的主窗口最小化（按 PID 归属找窗口，避免误伤别的窗口）。"""
    pids = opencode_pids()
    if not pids:
        log("没有 OpenCode.exe 进程，跳过最小化")
        return 0
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if (pid.value in pids
                and _user32.IsWindowVisible(hwnd)
                and _user32.GetWindowTextLengthW(hwnd) > 0):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_cb, 0)
    for hwnd in found:
        _user32.ShowWindow(hwnd, SW_MINIMIZE)
    log(f"已最小化 OpenCode 窗口 {len(found)} 个")
    return len(found)


def panel_minimized(pid: int) -> bool:
    """面板主窗口是否被最小化（只看该 PID 下可见的顶层窗口）。

    最小化时页面心跳可能还在，但用户此刻并不需要"沉浸式面板"，
    所以任务栏应该恢复可见；恢复窗口后再隐藏。
    """
    if not pid:
        return False
    state = {"visible": False, "minimized": False}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if (wpid.value == pid
                and _user32.IsWindowVisible(hwnd)
                and _user32.GetWindowTextLengthW(hwnd) > 0):
            state["visible"] = True
            if _user32.IsIconic(hwnd):
                state["minimized"] = True
        return True

    _user32.EnumWindows(_cb, 0)
    return bool(state["visible"] and state["minimized"])


def panel_window_state(pid: int) -> tuple:
    """面板主窗口**在不在**、是否最小化 → `(has_window, minimized)`。

    ⚠ 与"浏览器进程还在吗"不是一回事：窗口真关了、但 Edge 还留着后台/残留进程时，
      进程活着而窗口没了。任务栏联动必须按**窗口**判断 —— 否则残留进程会让任务栏一直藏着不恢复。
    """
    if not pid:
        return False, False
    state = {"visible": False, "minimized": False}

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        wpid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
        if (wpid.value == pid
                and _user32.IsWindowVisible(hwnd)
                and _user32.GetWindowTextLengthW(hwnd) > 0):
            state["visible"] = True
            if _user32.IsIconic(hwnd):
                state["minimized"] = True
        return True

    _user32.EnumWindows(_cb, 0)
    return bool(state["visible"]), bool(state["visible"] and state["minimized"])


# ---------------- 面板窗口 ----------------

_profile_cache = {"t": 0.0, "val": False}


def profile_in_use(max_age: float = 5.0) -> bool:
    """专属 profile 是否已有浏览器在跑。

    这是"兜底"判据（比心跳慢、比心跳贵），只在心跳不可信时才需要：
      · 页面还是旧版、不发心跳；
      · 浏览器最小化时 Chromium 会节流定时器，心跳可能断档。
    加 5 秒缓存，避免每次轮询都起一个 PowerShell。
    注意必须限定进程名，否则执行这段 PowerShell 的自己会因命令行含该路径而被误数。
    """
    now = time.time()
    if now - _profile_cache["t"] < max_age:
        return _profile_cache["val"]
    out = ps("(Get-CimInstance Win32_Process | Where-Object { " + _panel_proc_filter()
             + " } | Measure-Object).Count")
    out = out.strip()
    val = out.isdigit() and int(out) > 0
    _profile_cache["t"] = now
    _profile_cache["val"] = val
    return val


def pid_alive(pid: int) -> bool:
    """进程存活检查（纯 ctypes，本进程内；用于"采纳的外部窗口"这种没有句柄的情况）。"""
    if not pid:
        return False
    if _proc is not None:
        try:
            return _proc.pid_alive(pid)
        except Exception:  # noqa: BLE001
            pass
    try:
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=NO_WINDOW).stdout or ""
        return str(pid) in out
    except Exception:
        return False


def panel_main_pid() -> int:
    """面板专属 profile 的浏览器主进程 PID（取创建最早的那个），用于廉价存活检查。"""
    out = ps("Get-CimInstance Win32_Process | Where-Object { " + _panel_proc_filter() + " } "
             "| Sort-Object CreationDate | Select-Object -First 1 -ExpandProperty ProcessId")
    out = out.strip()
    return int(out) if out.isdigit() else 0


def close_window() -> None:
    ps("Get-CimInstance Win32_Process | Where-Object { " + _panel_proc_filter() + " } "
       "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")


def _kill_proc(proc) -> None:
    """结束我们拉起的窗口进程（拿不到句柄也无所谓）。"""
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _wait_page_loaded(proc, timeout: float) -> bool:
    """等页面真的发出第一个心跳，用来尽早发现"原生窗口卡成白屏"。

    返回 True = 页面已加载；False = 超时/窗口进程自己退了（都该换浏览器窗口）。
    """
    t0 = time.time()
    while time.time() - t0 < timeout:
        if panel_alive():
            return True
        if proc is not None and proc.poll() is not None:
            return False                     # WebView2 没起来，窗口进程已经退了
        time.sleep(0.4)
    return panel_alive()


def open_window(hide_taskbar: bool = True, required: bool = True):
    global _native_disabled
    wait_upstream(required=required)         # ★ 先等后端可用，避免开出连不上的页面
    if want_native() and not _native_disabled:
        exe, extra, _ = panel_python()
        env = dict(os.environ)
        env.update(extra)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        taskbar_sync(hide_taskbar)
        log(f"打开面板窗口（原生 WebView2）: {exe}")
        proc = subprocess.Popen([exe, PANEL_WINDOW_PY, "--port", str(PORT), "--title", PANEL_TITLE],
                                cwd=ROOT, env=env, creationflags=NO_WINDOW)
        time.sleep(MINIMIZE_DELAY)
        if should_minimize_opencode(required):
            try:
                minimize_opencode()
            except Exception as exc:  # noqa: BLE001
                log(f"最小化 OpenCode 失败: {exc}")
        if _wait_page_loaded(proc, NATIVE_LOAD_TIMEOUT):
            return proc
        # ★ 原生窗口卡死（白屏）：结束它，本次运行改用浏览器窗口，不再反复试原生。
        log(f"原生窗口 {NATIVE_LOAD_TIMEOUT:.0f}s 内没等到页面心跳 → 判定卡死，改用浏览器窗口")
        _kill_proc(proc)
        _native_disabled = True
    os.makedirs(PROFILE_DIR, exist_ok=True)
    for exe in BROWSERS:
        if os.path.isfile(exe):
            taskbar_sync(hide_taskbar)       # 开关关掉时这里不会隐藏，只会自愈恢复
            log(f"打开面板窗口: {exe}")
            proc = subprocess.Popen(
                [exe, f"--app={URL}", f"--user-data-dir={PROFILE_DIR}",
                 "--start-maximized", "--no-first-run", "--no-default-browser-check",
                 "--disable-background-mode", "--disable-sync",
                 "--disable-extensions",     # ★ 面板窗口不该跑第三方扩展（实测：profile 里的
                                             #   XDown 扩展会拉起 XDownHosts.exe；关掉避免连带）
                 "--autoplay-policy=no-user-gesture-required",   # 刷新后能接着播上一首
                 "--disable-features=Translate,MediaRouter"],
                creationflags=NO_WINDOW,
            )
            # 等面板窗口先露面，再把 OpenCode 自己的窗口最小化（ACP 模式下不碰它）
            time.sleep(MINIMIZE_DELAY)
            if should_minimize_opencode(required):
                try:
                    minimize_opencode()
                except Exception as exc:  # noqa: BLE001
                    log(f"最小化 OpenCode 失败: {exc}")
            return proc
    log("没找到 Edge/Chrome，退回默认浏览器打开")
    taskbar_sync(hide_taskbar)               # 开关关掉时这里不会隐藏
    os.startfile(URL)  # noqa: S606
    time.sleep(MINIMIZE_DELAY)
    if should_minimize_opencode(required):
        try:
            minimize_opencode()
        except Exception as exc:  # noqa: BLE001
            log(f"最小化 OpenCode 失败: {exc}")
    return None


# ---------------- 主流程 ----------------

def main() -> int:
    if "--restore-taskbar" in sys.argv:
        # stop.bat 兜底：守护是被强制结束的，跑不到退出钩子，这里补一刀
        restore_taskbar_if_marked()
        return 0

    once = "--once" in sys.argv
    log(f"守护进程启动（面板 {PORT} / 锁 {LOCK_PORT} / 音乐 {MUSIC_PORT}；"
        f"关窗联动关 OpenCode: {KILL_OPENCODE}）")
    if not once:
        lock = socket.socket()
        try:
            lock.bind((HOST, LOCK_PORT))
            lock.listen(1)
        except OSError:
            log(f"端口 {LOCK_PORT} 已被占用 → 判定已有守护进程在运行，退出"
                f"（若这是另一个副本：开发版用 8787/8788，安装版用 17887/17888，"
                f"想换端口就改 runtime/state/_panel.json）")
            return 0
        atexit.register(restore_taskbar_if_marked)   # 守护退出（正常路径）时兜底恢复

    ensure_server()

    # 判断窗口是否开着：心跳优先；没心跳时用浏览器进程兜底（兼容旧页面/最小化被节流）
    status = panel_status()
    alive = bool(status.get("alive"))
    taskbar_pref = bool(status.get("taskbar", True))   # 设置里"任务栏是否隐藏"
    proc = None                                  # 我们自己拉起的窗口进程句柄
    adopted_pid = 0                              # 采纳的外部窗口：主进程 PID（廉价存活判据）
    if not alive and profile_in_use():
        alive = True
        adopted_pid = panel_main_pid()
        log(f"启动时检测到已有面板窗口（外部打开，主进程 PID={adopted_pid}）")
    opened_at = 0.0
    log(f"启动时面板窗口: {'开着' if alive else '没开'}（任务栏开关: {taskbar_pref}）")
    if alive and taskbar_pref:
        taskbar_sync(True)                   # 接管已开着的面板 → 任务栏跟着隐藏
    else:
        restore_taskbar_if_marked()          # 开关关掉 / 上次留下"隐藏"标记 → 自愈恢复

    running = opencode_running()
    # 面板要不要依赖 OpenCode：引擎是 acp 时**完全不依赖**（可以脱离 OpenCode 单独跑）
    required = opencode_required()
    log(f"面板是否需要 OpenCode: {required}"
        + ("（引擎 = acp → 独立运行）" if not required else ""))

    if once:
        if not alive:
            open_window(hide_taskbar=taskbar_pref, required=required)
        return 0

    if should_open_now(required, running, alive, False, None):
        proc = open_window(hide_taskbar=taskbar_pref, required=required)
        opened_at = time.time()
        alive = True

    closed_at = None
    skip_open = False                            # 用户主动关窗后，别自动重开
    was_open = alive
    last_oc = time.time()
    quiet_log_at = 0.0                           # 「刚重启过、不联动」提示的防抖节流
    last_launch_try = 0.0                        # 「把 OpenCode 拉起来」的节流（引擎=opencode 时）

    while True:
        # 反悔期内把 tick 收窄到"刚好够判定"，让联动准点执行（误差 < 50ms）
        nap = TICK_SECONDS
        if closed_at is not None:
            nap = max(0.05, min(TICK_SECONDS, GRACE_SECONDS - (time.time() - closed_at)))
        time.sleep(nap)
        now = time.time()
        if not port_open():
            ensure_server()

        # ---- ① 面板窗口还开着吗 ----
        status = panel_status()
        taskbar_pref = bool(status.get("taskbar", True))
        if opened_at and now - opened_at < SETTLE_SECONDS:
            fresh = True                         # 刚开窗，等页面加载
        else:
            fresh = bool(status.get("alive"))
            if not fresh and proc is not None and proc.poll() is None:
                fresh = True                     # 我们拉起的浏览器进程还活着（最准）
            elif not fresh and proc is None and adopted_pid and pid_alive(adopted_pid):
                fresh = True                     # 采纳的外部窗口：用便宜的 PID 存活检查兜底
        if proc is not None and proc.poll() is not None:
            proc = None                          # 句柄已失效

        # ---- ①′ 任务栏联动 ----
        # 目标：设置允许 + 面板还在 + 面板未最小化 时才隐藏。
        # 最小化、设置关掉、面板关掉，都会走 taskbar_sync(False) 恢复（只恢复"我们藏过的"）。
        panel_pid = proc.pid if proc is not None else adopted_pid
        # ⚠ 任务栏按"窗口真的在不在"判断，而不是"进程还在不在"：窗口关了但 Edge 残留进程时，
        #   残留进程会让 fresh 持续为真、任务栏一直藏着不恢复（用户实测报的 bug）。
        has_win, minimized = (panel_window_state(panel_pid) if (fresh and panel_pid)
                              else (False, False))
        taskbar_sync(bool(taskbar_pref and fresh and has_win and not minimized))

        # ---- ② 由开 → 关：进入反悔期 ----
        if was_open and not fresh:
            # 必须用"发现关闭的那一刻"，不能用本轮循环开头的时间戳：
            # 本轮可能已经花了上百毫秒到 1 秒（进程兜底要起 PowerShell），
            # 用旧时间戳会让反悔期瞬间"超时"，等于没有反悔期。
            closed_at = time.time()
            log(f"面板窗口已关闭 → 反悔 {GRACE_SECONDS:.1f} 秒")

        # ---- ③ 反悔期（固定时长，无随机；用当下时间判定，保证实际不短于设定值）----
        if closed_at is not None:
            if fresh:
                log("窗口又开回来了 → 取消关闭")
                closed_at = None
            elif time.time() - closed_at >= GRACE_SECONDS:
                # ⚠ 心跳说"关了"还不够：可能只是页面被节流/卡住。先确认浏览器进程真没了，
                #    否则不联动（宁可这次不关，也不能误杀正在跑的会话）。
                browser_alive, bpid = panel_browser_alive(proc, adopted_pid)
                if browser_alive:
                    log("心跳断了但面板窗口还在 → 当作还开着，不联动")
                    adopted_pid = bpid or adopted_pid
                    if proc is not None and proc.poll() is not None:
                        proc = None
                    closed_at = None
                    fresh = True          # ★ 本轮别再把它当"没开"：否则 ACP 模式会立刻重开一次面板
                elif time.time() - _server_restarted_at < RESTART_QUIET:
                    # ★ 刚重启过面板服务：心跳断档**是我们自己造成的**，绝不能因此联动杀掉 OpenCode
                    if time.time() - quiet_log_at > 20:
                        log("面板服务刚重启过（%.0fs 前）→ 本次不联动（防误杀 OpenCode）"
                            % (time.time() - _server_restarted_at))
                        quiet_log_at = time.time()
                    closed_at = None
                    fresh = True
                else:
                    taskbar_sync(False)          # 面板确实关了 → 恢复（只恢复我们藏过的）
                    if should_kill_on_close(required, browser_alive=False):
                        kill_opencode()
                    closed_at = None
                    skip_open = True             # 这是"我要退出"的意图
                    log("反悔期结束 → 已执行联动")
                    if not required:
                        # 面板独立运行（ACP）：用户把窗口关了就是结束了 —— 恢复任务栏后**退出守护**。
                        # 否则旧守护一直占着锁 + skip_open，用户再点快捷方式会"没反应"。
                        restore_taskbar_if_marked()
                        log("面板已关闭且不依赖 OpenCode → 守护退出")
                        return 0

        # ---- ④ OpenCode 状态（较贵，降频检查）----
        if now - last_oc >= POLL_SECONDS:
            prev_running = running
            running = opencode_running()
            last_oc = now
            required = opencode_required()       # 用户可能中途换了引擎（opencode ↔ acp）
            # 引擎 = opencode 但它没在跑 → 主动拉起桌面端（否则面板一直 502）。
            # ⚠ `not skip_open`：用户**主动关窗**（= 退出意图）后绝不能再把它拉起来 ——
            #   否则界面表现为"关掉面板、OpenCode 却自己又回来了"（2026-10-01 用户实测）。
            #   30s 节流，免得它一直起不来时疯跑。
            if required and not running and not skip_open and (now - last_launch_try >= 30):
                last_launch_try = now
                try:
                    if HERE in sys.path:
                        sys.path.remove(HERE)
                    sys.path.insert(0, HERE)
                    import engines.opencode as _oc
                    if _oc.launch_desktop():
                        log("引擎 = opencode 但 OpenCode 没在跑 → 已拉起桌面端")
                except Exception:  # noqa: BLE001
                    pass
            # ★ 解除「别开窗」抑制：**只有 OpenCode 从「没跑」变成「在跑」**（= 用户/启动器
            #   把它带回来了）才解除。⚠ 绝不能写成 `if not running: skip_open = False` ——
            #   那会与"我们刚把它杀掉"打架：杀掉 → 检测到没跑 → 解除抑制 → 又拉起 → 又开窗
            #   → 死循环（用户实测报的就是这个）。用"上升沿"而不是"时间窗"，慢速 kill 也安全。
            if running and not prev_running and skip_open:
                skip_open = False

        # ---- ⑤ 该开窗但没开 → 开窗 ----
        # 关键：反悔期内（closed_at 未清）绝不能抢着重开，否则会"关掉就被重开"、
        # 反悔期被新窗口的心跳取消，kill 永远执行不到 —— 必须等反悔期走完。
        if should_open_now(required, running, fresh, skip_open, closed_at):
            log("面板窗口未开且允许打开 → 打开面板窗口")
            proc = open_window(hide_taskbar=taskbar_pref, required=required)
            opened_at = time.time()
            fresh = True

        # ---- ⑥ 可选：OpenCode 退出时也关掉窗口（默认关，见模块顶部常量）----
        if CLOSE_WINDOW_WHEN_OPENCODE_EXITS and required and was_open and not running:
            close_window()
            fresh = False

        was_open = fresh


if __name__ == "__main__":
    raise SystemExit(main())
