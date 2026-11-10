"""`cu` 的 PATH 薄壳 —— 让所有 ACP agent 直接敲 `cu ...`。

ACP 协议没有"客户端自定义工具"这回事（agent 的工具是它自己的），所以"像 skill
那样人人可用"只有两条路：**把命令放进 PATH** + **每轮 prompt 里说一句**。
这个模块负责第一条：生成 runtime/bin/cu.cmd，并把该目录注入子进程 PATH。

薄壳内容只有三行（纯 ASCII）：用 COCRAFT_PY（= 当前面板解释器）跑仓库里的
tools/cu.py。⚠ 不用 pyinstaller/复制脚本 —— 薄壳永远指向源码，改完立即生效。
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
BIN_DIR = os.path.join(ROOT, "runtime", "bin")

# %~dp0 = runtime\bin\ → 上两级是仓库根
SHIM = (
    "@echo off\r\n"
    "rem cocraft computer-use shim (generated, do not edit)\r\n"
    "setlocal\r\n"
    'set "PY=%COCRAFT_PY%"\r\n'
    'if "%PY%"=="" set "PY=python"\r\n'
    '"%PY%" "%~dp0..\\..\\tools\\cu.py" %*\r\n'
)

PLANNER_SHIM = (
    "@echo off\r\n"
    "rem cocraft planner shim (generated, do not edit)\r\n"
    "setlocal\r\n"
    'set "PY=%COCRAFT_PY%"\r\n'
    'if "%PY%"=="" set "PY=python"\r\n'
    '"%PY%" "%~dp0..\\..\\tools\\planner.py" %*\r\n'
)


def _ensure_one(name, content, force=False):
    try:
        os.makedirs(BIN_DIR, exist_ok=True)
        path = os.path.join(BIN_DIR, name)
        cur = ""
        if os.path.isfile(path) and not force:
            try:
                with open(path, "r", encoding="ascii", errors="replace") as f:
                    cur = f.read()
            except Exception:
                cur = ""
        if force or cur != content:
            with open(path, "w", encoding="ascii", newline="") as f:
                f.write(content)
        return path
    except Exception:
        return None


def ensure_bin_dir(force=False):
    """生成/更新 cu.cmd + planner.cmd，返回 bin 目录。失败返回 None。"""
    try:
        os.makedirs(BIN_DIR, exist_ok=True)
        _ensure_one("cu.cmd", SHIM, force=force)
        _ensure_one("planner.cmd", PLANNER_SHIM, force=force)
        return BIN_DIR
    except Exception:
        return None


def inject_path(env):
    """把 runtime/bin 补进 env 的 PATH，并告诉薄壳用哪个解释器。"""
    env = env if env is not None else {}
    bin_dir = ensure_bin_dir()
    if not bin_dir:
        return False
    env["COCRAFT_ROOT"] = ROOT
    env["COCRAFT_PY"] = sys.executable or "python"
    key = next((k for k in env if k.lower() == "path"), "PATH")
    cur = str(env.get(key) or "")
    low = cur.lower()
    if bin_dir.lower() not in low:
        env[key] = (bin_dir + ";" + cur) if cur else bin_dir
    return True
