#!/usr/bin/env python3
"""Start the cocraft panel (and OpenCode, only when the panel still needs it).

This is what the "OpenCode (with panel)" desktop shortcut runs. It is meant to
run under pythonw.exe so that no console window flashes.

Engine-aware (2026-09-26): the panel can run **without** OpenCode at all.
  * engine == "opencode"  -> start OpenCode if needed, then the watcher
                             (and keep self-healing the OpenCode junction)
  * engine == "acp" (etc.) -> panel-only: skip OpenCode and the junction heal,
                             just start the watcher, which opens the panel

So the same shortcut keeps working after switching the engine to ACP, and there
is no need to start something the user does not want.

ASCII-only on purpose (see AGENTS.md pitfall #28 for the .vbs/.ps1 variant).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BACKEND = os.path.join(ROOT, "backend")
WATCH = os.path.join(BACKEND, "watch.py")
HEAL = os.path.join(ROOT, "scripts", "heal_opencode_link.py")
ENGINE_FILE = os.path.join(ROOT, "runtime", "state", "_engine.json")

# 进程查询走纯 ctypes（不起 tasklist），并抑制"应用程序无法正常启动"硬错误框
# （子进程——含自愈用的 powershell——会继承这个 error mode）。
try:
    if BACKEND in sys.path:
        sys.path.remove(BACKEND)
    sys.path.insert(0, BACKEND)
    import procutils as _proc      # noqa: PLC0415
    _proc.set_error_mode()
except Exception:  # noqa: BLE001
    _proc = None

# Candidate paths of the OpenCode desktop executable; first existing one wins.
# Only per-machine/per-user locations are probed (no hard-coded user paths).
OPENCODE_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs",
                 "@opencodedesktop", "OpenCode.exe"),
    r"C:\Program Files\@opencodedesktop\OpenCode.exe",
]

CREATE_NO_WINDOW = 0x08000000


def find_opencode():
    for p in OPENCODE_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return None


def opencode_running():
    """Cheap check. Pure ctypes first (no child process → nothing to pop an error box);
    tasklist only as a fallback."""
    if _proc is not None:
        try:
            return _proc.is_running("OpenCode.exe")
        except Exception:  # noqa: BLE001
            pass
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq OpenCode.exe", "/NH"],
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        ).decode(errors="ignore")
        return "OpenCode.exe" in out
    except Exception:
        return False


def active_engine():
    """Current engine id, using the very same rules as backend/engines.

    Falls back to reading the state file (and finally to "opencode") if the
    engine package cannot be imported for any reason.
    """
    try:
        if BACKEND in sys.path:
            sys.path.remove(BACKEND)
        sys.path.insert(0, BACKEND)
        from engines import active_engine_id      # noqa: PLC0415
        return str(active_engine_id() or "")
    except Exception:
        try:
            with open(ENGINE_FILE, "r", encoding="utf-8") as fh:
                return str(json.load(fh).get("engine") or "")
        except Exception:
            return "opencode"


def plan() -> dict:
    """Decide what to do, without doing it (pure enough to unit-test).

    Returns: {"engine", "panel_only", "start_opencode", "heal", "exe"}
    """
    engine = active_engine()
    panel_only = bool(engine) and engine != "opencode"
    exe = None if panel_only else find_opencode()
    return {
        "engine": engine,
        "panel_only": panel_only,
        "heal": not panel_only,
        "start_opencode": bool(not panel_only and exe and not opencode_running()),
        "exe": exe,
    }


def main():
    p = plan()

    if p["heal"]:
        # Self-heal the OpenCode junction *before* OpenCode starts (hidden:
        # pythonw + CREATE_NO_WINDOW). Replaces the old 15-minute task.
        if os.path.isfile(HEAL):
            try:
                subprocess.run([sys.executable, HEAL, "--wait"],
                               cwd=os.path.dirname(HEAL),
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=CREATE_NO_WINDOW, timeout=300)
            except Exception:
                pass
    if p["start_opencode"] and p["exe"]:
        subprocess.Popen([p["exe"]], cwd=os.path.dirname(p["exe"]))
    # panel-only: do NOT start OpenCode and do NOT heal its junction
    # (the heal is about the OpenCode install, which we are not using).

    # The watcher exits by itself if another one is already running (port lock).
    # It also decides whether the panel still needs OpenCode (engine-aware).
    if os.path.isfile(WATCH):
        # PYTHONDONTWRITEBYTECODE：安装目录里不生成 __pycache__（卸载更干净，
        # 也避免程序目录被写字节码）。
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        subprocess.Popen([sys.executable, WATCH], cwd=ROOT, env=env,
                         creationflags=CREATE_NO_WINDOW)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
