# -*- coding: utf-8 -*-
r"""无头回归（桌面半条链路）：B3 权限「强提醒」的真机前因后果。

★ 为什么需要它：B3 的两条腿都**必须真的碰操作系统**，静态体检（check_features）
  只能锁住"代码里有这些调用"，锁不住"真的能弹出来 / 真的闪到我们自己的窗口上"：
    ① **托盘**：`tray_host.py` 轮询 `_notify.json` → `icon.notify()` 弹系统气泡
       → 再写 `{"cmd":"flash"}` 到 `_panel_cmd.json`；
    ② **窗口**：`panel_window.py` 的 `_own_hwnd()`（按 **pid** 找"我自己的"窗口）
       + `_flash_taskbar()`（`FlashWindowEx`，`FLASHW_TRAY | FLASHW_TIMERNOFG`）。

  两条腿都在**隔离环境**里验（临时 state 目录 / 本进程自己的窗口），
  **不碰**用户真实的面板窗口与托盘状态目录。

跑法（会短暂出现一个托盘图标与一个测试窗口，随后自行退出）：
    python tools\notify_desktop_test.py
      T1. 启动时已有一条"遗留"通知（ts 不变）→ 不重弹、也不写 flash 命令
      T2. 新通知（ts 变大）→ 写 `{"cmd":"flash"}`（= 已弹气泡 + 请窗口闪任务栏）
      T3. flash:false → 只弹气泡、不闪任务栏
      T4. 同一条（ts 不变）→ 不重复触发
      W1. `_own_hwnd()` 找的是**本进程**的窗口（不会闪到别人的窗口上）
      W2. `_flash_taskbar()` 把 FlashWindowEx 打在同一个句柄上
      W3. 闪法 = FLASHW_TRAY | FLASHW_TIMERNOFG（0x2 | 0xC = 14）
      W4. 真实 FlashWindowEx(本窗口) 真调得动、不抛异常
          ⚠ 它的返回值**不是"成功与否"**（MSDN：返回的是"调用前该窗口是否处于活动态"）
            → 后台窗口返回 0 是**正常**的，别拿 `!= 0` 当断言（这里踩过一次）。
      W5. 窗口不可见 → 不崩（句柄 0 直接返回）
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYW = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
if not os.path.isfile(PYW):
    PYW = sys.executable
SITE = os.path.join(ROOT, "runtime", "venvs", "panel", "Lib", "site-packages")
TRAY = os.path.join(ROOT, "backend", "tray_host.py")

results = []


def step(name, ok, extra=""):
    results.append((name, bool(ok), str(extra)))


# ---------------------------------------------------------------- ① 托盘那条腿
def test_tray():
    tmp = os.path.join(tempfile.gettempdir(), "cocraft-notify-test")
    os.makedirs(tmp, exist_ok=True)
    nf = os.path.join(tmp, "_notify.json")
    cf = os.path.join(tmp, "_panel_cmd.json")
    for p in (nf, cf):
        if os.path.exists(p):
            os.remove(p)

    def write_notify(ts, body, flash=True, title="cocraft TEST"):
        with open(nf, "w", encoding="utf-8") as fh:
            json.dump({"ts": ts, "title": title, "body": body, "flash": flash}, fh)

    def read_cmd():
        try:
            with open(cf, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    def clear_cmd():
        if os.path.exists(cf):
            os.remove(cf)

    if not os.path.isfile(TRAY):
        step("T0 tray_host.py 存在", False, TRAY)
        return
    write_notify(1.0, "遗留通知（不该重弹）")          # 面板上次写过、托盘已弹过

    env = dict(os.environ)
    env["PYTHONPATH"] = SITE + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.Popen([PYW, TRAY, "--title", "cocraft TEST", "--state", tmp],
                            cwd=ROOT, env=env)
    try:
        time.sleep(4.0)                                # 等图标起来、轮询线程开跑
        step("T1 启动不重弹遗留通知（没写 flash 命令）", read_cmd() is None, read_cmd())

        write_notify(time.time(), "yiyi 请求权限：Run command · execute")
        time.sleep(2.5)
        c = read_cmd()
        step("T2 新通知 → 写 flash 命令（气泡 + 闪任务栏）",
             isinstance(c, dict) and c.get("cmd") == "flash", c)

        clear_cmd()
        write_notify(time.time() + 1, "只有气泡", flash=False)
        time.sleep(2.5)
        step("T3 flash:false → 只气泡、不闪任务栏", read_cmd() is None, read_cmd())

        clear_cmd()
        write_notify(time.time(), "再来一条")
        time.sleep(2.5)
        first = read_cmd()
        clear_cmd()
        time.sleep(2.0)
        step("T4 同一条（ts 不变）不重复触发",
             isinstance(first, dict) and read_cmd() is None,
             "first=%s again=%s" % (first, read_cmd()))
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------- ② 窗口那条腿
class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                ("dwTimeout", ctypes.c_uint)]


def test_window():
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    try:
        import panel_window  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        step("W0 能导入 panel_window", False, exc)
        return
    try:
        import tkinter as tk  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        step("W0 tkinter 可用（用来造一个真实顶层窗口）", False, exc)
        return

    root = tk.Tk()
    root.title("cocraft-b3-probe")
    root.geometry("360x220+80+80")
    root.deiconify()
    root.update()
    try:
        winfo = int(root.winfo_id())
        try:
            frame = int(root.wm_frame(), 16)
        except Exception:  # noqa: BLE001
            frame = 0

        hwnd = panel_window._own_hwnd()
        step("W1 _own_hwnd() 找到本进程的窗口（按 pid 过滤）",
             hwnd != 0 and hwnd in (winfo, frame),
             "found=%s winfo=%s frame=%s" % (hwnd, winfo, frame))

        calls = []
        u32 = ctypes.windll.user32
        orig = u32.FlashWindowEx

        def _stub(pinfo):
            info = ctypes.cast(pinfo, ctypes.POINTER(_FLASHWINFO)).contents
            calls.append((int(info.hwnd or 0), int(info.dwFlags), int(info.uCount)))
            return 1

        u32.FlashWindowEx = _stub
        try:
            panel_window._flash_taskbar()
        finally:
            u32.FlashWindowEx = orig
        step("W2 _flash_taskbar() 把 FlashWindowEx 打在同一个句柄上",
             len(calls) == 1 and calls[0][0] == hwnd, calls)
        step("W3 闪法 = FLASHW_TRAY | FLASHW_TIMERNOFG（只闪任务栏、闪到前台为止）",
             len(calls) == 1 and calls[0][1] == (0x2 | 0xC),
             "flags=%s" % (calls[0][1] if calls else None))

        ret, err = None, None
        try:
            info = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd, 0x2 | 0xC, 5, 0)
            ret = u32.FlashWindowEx(ctypes.byref(info))     # 返回值 = 调用前是否活动态（0 正常）
        except Exception as exc:  # noqa: BLE001
            err = exc
        step("W4 真实 FlashWindowEx(本窗口) 调得动、不抛异常",
             err is None and isinstance(ret, int), "ret=%s err=%s" % (ret, err))

        root.withdraw()
        root.update()
        hidden = panel_window._own_hwnd()
        crash = None
        try:
            panel_window._flash_taskbar()
        except Exception as exc:  # noqa: BLE001
            crash = str(exc)
        step("W5 窗口不可见 → 不崩（句柄 0 直接返回）",
             hidden == 0 and crash is None, "hwnd=%s crash=%s" % (hidden, crash))
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    print("\n=== B3 强提醒 · 桌面半条链路（隔离）：托盘气泡 + 闪任务栏 ===")
    test_tray()
    test_window()
    bad = []
    for name, ok, extra in results:
        print("  %s %s%s" % ("OK  " if ok else "FAIL", name, ("  [" + extra + "]") if extra else ""))
        if not ok:
            bad.append(name)
    print("\n  结论: %s（%d 项）" % ("[OK] 全部通过" if not bad else "[BAD] " + ", ".join(bad),
                                   len(results)))
    return 0 if not bad else 2


if __name__ == "__main__":
    sys.exit(main())
