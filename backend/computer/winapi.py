"""窗口枚举 / 前台 / DPI / 提权检测。

纯标准库 + ctypes，不依赖 pywin32 / pywinauto。
所有几何值都是**物理像素**（进程已设为 Per-Monitor DPI Aware V2，
所以和 UIA 的 BoundingRectangle 同一套坐标，不会差一个缩放倍率）。
"""

import ctypes
import os
import time
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:  # pragma: no cover
    dwmapi = None


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
GA_ROOT = 2

_dpi_done = False


class TOKEN_ELEVATION(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wt.DWORD)]


def init_dpi():
    """让后续 GetWindowRect / 截图坐标与 UIA 一致（幂等）。"""
    global _dpi_done
    if _dpi_done:
        return True
    _dpi_done = True
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore")
        if shcore.SetProcessDpiAwareness(2) == 0:
            return True
    except Exception:
        pass
    try:
        return bool(user32.SetProcessDPIAware())
    except Exception:
        return False


def _hwnd_int(hwnd):
    """接受 int / ctypes 句柄；⚠ `int(c_void_p)` 会抛 ValueError，必须走 .value。"""
    if not hwnd:
        return 0
    if isinstance(hwnd, int):
        return hwnd
    val = getattr(hwnd, "value", None)
    if isinstance(val, int):
        return val
    return int(hwnd)


def _h(hwnd):
    return wt.HWND(_hwnd_int(hwnd))


def is_window(hwnd):
    return bool(_hwnd_int(hwnd)) and bool(user32.IsWindow(_h(hwnd)))


def foreground_hwnd():
    return _hwnd_int(user32.GetForegroundWindow())


def is_minimized(hwnd):
    return bool(user32.IsIconic(_h(hwnd)))


def is_visible(hwnd):
    return bool(user32.IsWindowVisible(_h(hwnd)))


def is_cloaked(hwnd):
    """UWP/虚拟桌面藏起来的窗口，IsWindowVisible 仍为 True。"""
    if not dwmapi:
        return False
    val = ctypes.c_int(0)
    hr = dwmapi.DwmGetWindowAttribute(_h(hwnd), DWMWA_CLOAKED,
                                     ctypes.byref(val), ctypes.sizeof(val))
    return hr == 0 and val.value != 0


def ex_style(hwnd):
    return user32.GetWindowLongW(_h(hwnd), GWL_EXSTYLE)


def window_text(hwnd):
    h = _h(hwnd)
    n = user32.GetWindowTextLengthW(h)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(h, buf, n + 1)
    return buf.value


def class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(_h(hwnd), buf, 256)
    return buf.value


def pid_of(hwnd):
    pid = wt.DWORD(0)
    user32.GetWindowThreadProcessId(_h(hwnd), ctypes.byref(pid))
    return int(pid.value)


def process_name(pid):
    if not pid:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def is_elevated(hwnd):
    """目标进程是否提权（提权窗口无法被非提权进程 SendInput）。"""
    pid = pid_of(hwnd)
    if not pid:
        return False
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        # 打不开多半是高完整性进程（或受保护进程）→ 当作提权处理，交给 gate 拒绝
        return True
    try:
        tok = wt.HANDLE()
        if not advapi32.OpenProcessToken(h, TOKEN_QUERY, ctypes.byref(tok)):
            return False
        try:
            val = TOKEN_ELEVATION()
            ret = wt.DWORD(0)
            if advapi32.GetTokenInformation(tok, TokenElevation, ctypes.byref(val),
                                            ctypes.sizeof(val), ctypes.byref(ret)):
                return bool(val.TokenIsElevated)
            return False
        finally:
            kernel32.CloseHandle(tok)
    finally:
        kernel32.CloseHandle(h)


def rect_of(hwnd):
    """优先 DWM 扩展边框（不含 Win10 的透明描边），否则 GetWindowRect。"""
    h = _h(hwnd)
    r = RECT()
    if dwmapi:
        hr = dwmapi.DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS,
                                          ctypes.byref(r), ctypes.sizeof(r))
        if hr == 0 and (r.right - r.left) > 0 and (r.bottom - r.top) > 0:
            return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    if not user32.GetWindowRect(h, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def raw_rect(hwnd):
    """GetWindowRect 原值（含不可见的调整边框）。

    截图要用这个：PrintWindow 按窗口矩形绘制，用 DWM 收紧后的矩形会**裁掉内容**
    （实测 WinForms 窗口差 11px/边）。坐标点击仍用 rect_of 的 UIA 对齐值。
    """
    r = RECT()
    if not user32.GetWindowRect(_h(hwnd), ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right - r.left, r.bottom - r.top)


def describe(hwnd, fg=None):
    hwnd = _hwnd_int(hwnd)
    if fg is None:
        fg = foreground_hwnd()
    rect = rect_of(hwnd)
    return {
        "hwnd": hwnd,
        "pid": pid_of(hwnd),
        "process": process_name(pid_of(hwnd)),
        "title": window_text(hwnd),
        "class": class_name(hwnd),
        "rect": list(rect) if rect else None,
        "visible": is_visible(hwnd),
        "minimized": is_minimized(hwnd),
        "foreground": hwnd == fg,
        "elevated": is_elevated(hwnd),
        "tool_window": bool(ex_style(hwnd) & WS_EX_TOOLWINDOW),
        "cloaked": is_cloaked(hwnd),
    }


def list_windows(title=None, visible_only=True, include_untitled=False,
                 include_tool_windows=False, limit=50):
    """枚举顶层窗口。title 为不区分大小写的子串匹配。"""
    init_dpi()
    fg = foreground_hwnd()
    out = []
    needle = (title or "").lower()

    def _cb(hwnd, _lparam):
        hwnd = _hwnd_int(hwnd)
        if not hwnd:
            return True
        # 只要顶层（有 owner 的弹窗也保留，它们也是用户能看见的窗口）
        if user32.GetAncestor(wt.HWND(hwnd), GA_ROOT) != hwnd:
            pass
        vis = is_visible(hwnd)
        if visible_only and not vis:
            return True
        if is_cloaked(hwnd):
            return True
        if not include_tool_windows and (ex_style(hwnd) & WS_EX_TOOLWINDOW):
            return True
        t = window_text(hwnd)
        if not include_untitled and not t:
            return True
        if needle and needle not in t.lower():
            return True
        info = describe(hwnd, fg)
        out.append(info)
        return len(out) < max(1, limit)

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    # 前台窗口排第一，其余按可见 + 面积降序（大窗口通常才是"主窗口"）
    def _key(w):
        r = w.get("rect") or (0, 0, 0, 0)
        return (0 if w["foreground"] else 1, 0 if w["visible"] else 1, -(r[2] * r[3]))

    out.sort(key=_key)
    return out


def find_window(title=None, hwnd=0):
    """按 hwnd 或标题子串取一个窗口（标题匹配取最靠前/最大的那个）。"""
    if hwnd:
        hwnd = int(hwnd)
        if not is_window(hwnd):
            return None
        return describe(hwnd)
    wins = list_windows(title=title, visible_only=True, limit=50)
    return wins[0] if wins else None


def _alt_tap():
    """按一下 Alt：让本线程"刚有过输入"，从而拿到 SetForegroundWindow 的权限。

    Windows 的前台锁规则：只有前台进程 / 刚收到输入事件的进程才能抢前台。
    单独敲一下 Alt 是最常用的无害绕法（实测 SetForegroundWindow 从 False 变 True）。
    """
    VK_MENU = 0x12
    try:
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, 2, 0)
        return True
    except Exception:
        return False


def set_foreground(hwnd, retries=3):
    """把窗口带到前台（尽力而为）。

    顺序：已在最前 → SetForegroundWindow → Alt 轻敲 + 再试 →
    AttachThreadInput 共享输入队列 + 再试。
    拿不到就如实返回 False —— 调用方必须**放弃点击**，否则会点到别的窗口上。
    """
    target = _hwnd_int(hwnd)
    h = _h(target)
    if not target:
        return False
    try:
        my_tid = kernel32.GetCurrentThreadId()
    except Exception:
        my_tid = 0
    for attempt in range(max(1, retries)):
        if foreground_hwnd() == target:
            return True
        if user32.IsIconic(h):
            user32.ShowWindow(h, 9)      # SW_RESTORE
        else:
            user32.ShowWindow(h, 5)      # SW_SHOW
        try:
            user32.BringWindowToTop(h)
            user32.SetForegroundWindow(h)
        except Exception:
            pass
        if foreground_hwnd() == target:
            return True
        if attempt == 0:
            _alt_tap()
            time.sleep(0.03)
            continue
        # 最后一招：临时共享输入队列
        fb = foreground_hwnd()
        fg_tid = 0
        attached = False
        if fb:
            tid = wt.DWORD(0)
            user32.GetWindowThreadProcessId(_h(fb), ctypes.byref(tid))
            fg_tid = int(tid.value)
        try:
            if my_tid and fg_tid and my_tid != fg_tid:
                attached = bool(user32.AttachThreadInput(my_tid, fg_tid, True))
            user32.BringWindowToTop(h)
            user32.SetForegroundWindow(h)
            if attached:
                user32.SetFocus(h)
        except Exception:
            pass
        finally:
            if attached:
                try:
                    user32.AttachThreadInput(my_tid, fg_tid, False)
                except Exception:
                    pass
        time.sleep(0.06)
    return foreground_hwnd() == target

# ---------------------------------------------------------------- 窗口图标 / 应用标识
# 用途：原生面板窗口的标题栏 / 任务栏 / Alt-Tab 图标（安装包与快捷方式已经用 cocraft.ico，
# 但窗口自己不设的话，Windows 会拿进程 exe 的图标 —— 也就是 Python 的那个）。
ICON_SMALL, ICON_BIG = 0, 1
WM_SETICON = 0x0080
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
GCLP_HICON = -14
GCLP_HICONSM = -34

user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int,
                              ctypes.c_int, wt.UINT]
user32.SendMessageW.restype = ctypes.c_void_p
user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, ctypes.c_void_p, ctypes.c_void_p]
user32.SetClassLongPtrW.restype = ctypes.c_void_p
user32.SetClassLongPtrW.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_void_p]


def set_window_icon(hwnd, ico_path):
    """把窗口图标换成 .ico（大/小两个 + 类图标）。成功返回 True。

    ⚠ 只影响**窗口**（标题栏 / 任务栏 / Alt-Tab / 任务管理器「应用」里的图标）；
      任务管理器「详细信息」里进程的图标来自 exe 文件本身，改不了（除非换自己的 exe）。
    """
    try:
        h = user32.LoadImageW(None, str(ico_path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
        if not h:
            return False
        hwnd = wt.HWND(_hwnd_int(hwnd))
        user32.SendMessageW(hwnd, WM_SETICON, ctypes.c_void_p(ICON_BIG), ctypes.c_void_p(h))
        user32.SendMessageW(hwnd, WM_SETICON, ctypes.c_void_p(ICON_SMALL), ctypes.c_void_p(h))
        user32.SetClassLongPtrW(hwnd, GCLP_HICON, ctypes.c_void_p(h))
        user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, ctypes.c_void_p(h))
        return True
    except Exception:  # noqa: BLE001
        return False


def set_app_user_model_id(app_id):
    """给进程一个应用标识（任务栏分组 / 固定到任务栏后显示自己的图标）。"""
    try:
        return ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(str(app_id)) == 0
    except Exception:  # noqa: BLE001
        return False
