# -*- coding: utf-8 -*-
r"""把 cocraft 打包成安装包（Inno Setup）要用的 payload。

产物（都在 dist/ 下）：
    dist/payload/app/                      项目核心（backend/frontend/tools/launchers/scripts/docs）
    dist/payload/python/                   嵌入式 Python 3.12 x64（官方便携版，自带 pythonw.exe）
    dist/payload/init_state.py             安装后初始化 runtime/state（写 agent 注册表、引擎、干净模板）
    dist/payload/agents/node/node.exe      node 运行时（随包 agent 用）
    dist/payload/agents/codex/…            codex-acp + 依赖 + 原生 codex.exe + 干净的 codex-home 模板
    dist/payload/agents/claude/…           claude-code-acp 适配器（自带 SDK 的 Claude Code CLI）
    dist/payload/agents/opencode/…        随包 OpenCode（npm opencode-ai；自带的 opencode.exe）
    dist/payload/components/terminal/…    面板内终端（pywinpty）的 site-packages
    dist/payload/components/panel/…       原生窗口（pywebview + pythonnet）的 site-packages
    dist/cocraft-setup-<ver>.exe       由 ISCC 编译出来的安装包

用法：
    python packaging\build.py                  # 默认：核心 + codex/claude/opencode 三个随包 agent + terminal/panel
    python packaging\build.py --agents none     # 只出核心包（不带任何 agent）
    python packaging\build.py --with-music       # ★只有显式要音乐才会带（默认不带，且会自动跑 strip_music.py）
    python packaging\build.py --no-iss          # 只准备 payload，不调 ISCC

⚠ 组件策略（`ui 0.2.123` 起）：安装包**默认不带音乐 / 音频条 / QQ音乐 / 网易云**。
   默认组件只有 `terminal` + `panel`；就算写了 `--components music,...` 也会被摘掉
   （要保留音乐必须用 `--with-music`），并且在 ISS 编译前自动执行
   `packaging/strip_music.py`（把音乐相关的文件 / DOM / JS 桩一并处理）。
   agent（node/codex/claude/opencode）与 terminal/panel 在 cocraft.iss 里全部是**必选（fixed）**。

⚠ 绝不随包带任何密钥：`codex-home` 只放**干净模板**（env_key 指向环境变量），
   用户的 API Key 由首次设置向导写进 runtime/state/_acp_agents.json 的 baseline.env。

输出纯 ASCII（Windows 控制台是 GBK），中文只出现在写进文件的 JSON 里。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")
PAYLOAD = os.path.join(DIST, "payload")
CACHE = os.path.join(DIST, "cache")

LOCK_FILE = os.path.join(HERE, "deps.lock.json")


def _load_lock() -> dict:
    """读构建依赖锁（缺失/坏了不致命，回落到内置默认值；`--check-lock` 会明确报错）。"""
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


LOCK = _load_lock()


def _first_dir(*cands) -> str:
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return ""


NODE_VER = str((LOCK.get("node") or {}).get("version") or "22.23.3")
NODE_SHA256 = str((LOCK.get("node") or {}).get("sha256_node_exe") or "")
NPM_VER = str((LOCK.get("node") or {}).get("npm_version") or "")

PY_VER = str((LOCK.get("python") or {}).get("version") or "3.12.10")
PY_URL = str((LOCK.get("python") or {}).get("url") or
             f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip")
PY_SHA256 = str((LOCK.get("python") or {}).get("sha256") or "")

# 项目核心（要进包的东西）
CORE = ("backend", "frontend", "tools", "launchers", "scripts", "docs")
CORE_FILES = ("AGENTS.md", "README.md")
# 核心里的排除项：只去掉参考图画廊页（assets 里的主题素材**必须保留**）
CORE_SKIP_FILES = (
    "refs.html", "refs_q.html",              # 开发用的参考图画廊页
    # ★同人图不再随包（`0.2.54`）：默认头像=官方图标、空会话主图=内联圣徽、贴纸默认不显示
    "assets/badge.jpg", "assets/hero.jpg", "assets/avatar.png",
    "assets/sticker-duck.jpg", "assets/sticker-couple.jpg", "assets/night.jpg",
)

# 可选组件的 site-packages 来源（本机是 venv）。
# ⚠ 2026-10-12 起安装包**不再随包 music / audio**；**2026-10-14 起 music 又加回来且是必选组件**
#   （用户要求：音乐别让安装的人自己勾），audio（真频谱）仍然不随包（面板会退回 CSS 合成音频条）。
VENV_SITE = {
    # 面板内终端（ConPTY / pywinpty）与原生窗口（pywebview / WebView2 + pystray 托盘）
    "terminal": os.path.join(ROOT, "runtime", "venvs", "terminal", "Lib", "site-packages"),
    "panel": os.path.join(ROOT, "runtime", "venvs", "panel", "Lib", "site-packages"),
    # 音乐服务（网易云 / QQ 搜歌放歌：NeteaseCloudMusic + py-mini-racer）
    "music": os.path.join(ROOT, "runtime", "venvs", "music", "Lib", "site-packages"),
}
# 打包时要踢掉的东西：pip/wheel/缓存/元数据（运行时不看）。
SITE_SKIP_COMMON = ("pip", "pip-*", "wheel", "wheel-*", "__pycache__", "*.dist-info", "*.egg-info")
SITE_SKIP = {
    "terminal": SITE_SKIP_COMMON + ("setuptools", "setuptools-*", "pkg_resources"),
    "panel": SITE_SKIP_COMMON + ("setuptools", "setuptools-*", "pkg_resources"),
    # ★ music **不能**踢 setuptools：NeteaseCloudMusic 要 `pkg_resources`（venv 里就锁了 setuptools<81）
    "music": SITE_SKIP_COMMON,
}

# 随包 agent（codex 完整路线）
# ⚠ 不再写死本机路径：优先环境变量，其次常规位置，最后仓库内 `agents/`。
#   换机器构建：set COCRAFT_NODE_SRC=... / COCRAFT_CODEX_NM=...（见 docs/BUILD.md）
NODE_SRC = _first_dir(
    os.environ.get("COCRAFT_NODE_SRC"),
    r"D:\agentlist\deepseekharness\node\node-v%s-win-x64" % NODE_VER,
    os.path.join(ROOT, "agents", "node", "node-v%s-win-x64" % NODE_VER),
    os.path.join(ROOT, "agents", "node"),
)
CODEX_NM = _first_dir(
    os.environ.get("COCRAFT_CODEX_NM"),
    r"D:\agentlist\codex\node_modules",
    os.path.join(ROOT, "agents", "codex", "node_modules"),
)
CODEX_ACP = os.path.join(CODEX_NM, "@agentclientprotocol", "codex-acp")
# codex-acp 运行需要的依赖（package.json 里的 dependencies + 平台原生包）
CODEX_DEPS = [
    (os.path.join(CODEX_NM, "@agentclientprotocol", "sdk"), "@agentclientprotocol/sdk"),
    (os.path.join(CODEX_NM, "@agentclientprotocol", "codex-acp"), "@agentclientprotocol/codex-acp"),
    (os.path.join(CODEX_NM, "@openai", "codex"), "@openai/codex"),
    (os.path.join(CODEX_NM, "@openai", "codex-win32-x64"), "@openai/codex-win32-x64"),
    (os.path.join(CODEX_NM, "diff"), "diff"),
    (os.path.join(CODEX_NM, "open"), "open"),
    (os.path.join(CODEX_NM, "vscode-jsonrpc"), "vscode-jsonrpc"),
    (os.path.join(CODEX_NM, "zod"), "zod"),
    (os.path.join(CODEX_NM, "define-lazy-prop"), "define-lazy-prop"),
    (os.path.join(CODEX_NM, "default-browser"), "default-browser"),
    (os.path.join(CODEX_NM, "is-inside-container"), "is-inside-container"),
    (os.path.join(CODEX_NM, "is-docker"), "is-docker"),
    (os.path.join(CODEX_NM, "is-wsl"), "is-wsl"),
    (os.path.join(CODEX_NM, "run-applescript"), "run-applescript"),
    (os.path.join(CODEX_NM, "wsl-utils"), "wsl-utils"),
    (os.path.join(CODEX_NM, "bundle-name"), "bundle-name"),
    (os.path.join(CODEX_NM, "default-browser-id"), "default-browser-id"),
]

ISCC_CANDIDATES = [
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
]


def log(msg: str) -> None:
    print(msg.encode("ascii", "backslashreplace").decode("ascii"), flush=True)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _verify_sha256(path: str, expected: str, label: str) -> None:
    """锁文件里有 sha256 就校验；不一致直接停（别把来路不明的二进制打进包）。"""
    if not expected or os.environ.get("COCRAFT_SKIP_HASH"):
        return
    got = _sha256_file(path)
    if got.lower() != str(expected).lower():
        raise SystemExit(
            "[BAD] %s 的 SHA256 与 deps.lock.json 不一致：\n  got: %s\n  want: %s\n"
            "  如果你确实要换版本，先更新 packaging/deps.lock.json；临时跳过设 "
            "COCRAFT_SKIP_HASH=1。" % (label, got, expected))


def copytree(src, dst, skip_names=()):
    """复制目录；`skip_names` 里的名字（目录或文件）跳过。"""
    def ignore(_dir, names):
        out = []
        for n in names:
            for pat in skip_names:
                if pat.startswith("*."):
                    if n.lower().endswith(pat[1:].lower()):
                        out.append(n)
                elif n == pat or n.startswith(pat.rstrip("*")):
                    out.append(n)
        return out
    shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)


def fetch_python() -> str:
    """下载并解开嵌入式 Python（有缓存就不重复下）。返回解压目录。"""
    os.makedirs(CACHE, exist_ok=True)
    zip_path = os.path.join(CACHE, os.path.basename(PY_URL))
    if not os.path.isfile(zip_path):
        log("[1/6] 下载嵌入式 Python %s ..." % PY_VER)
        urllib.request.urlretrieve(PY_URL, zip_path)
    else:
        log("[1/6] 复用缓存的嵌入式 Python")
    _verify_sha256(zip_path, PY_SHA256, "嵌入式 Python %s" % PY_VER)
    out = os.path.join(PAYLOAD, "python")
    if os.path.isfile(os.path.join(out, "pythonw.exe")):
        return out
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(out)
    # ⚠ embeddable 发行版默认**禁用 site-packages**（._pth 里 import site 被注释掉）。
    #   我们要用 runtime/site-packages/<组件> 跑可选组件，所以打开它 + 加一行搜索路径。
    pth = next((os.path.join(out, f) for f in os.listdir(out) if f.endswith("._pth")), "")
    if pth and os.path.isfile(pth):
        with open(pth, "r", encoding="utf-8") as fh:
            txt = fh.read()
        if "import site" not in txt.replace("#import site", "import site"):
            txt = txt + "\nimport site\n"
        if "Lib\\site-packages" not in txt:
            txt = txt.replace("import site", "Lib\\site-packages\nimport site")
        with open(pth, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(txt)
        log("      已启用 embeddable 的 site-packages: %s" % os.path.basename(pth))
    return out


def stage_core() -> None:
    log("[2/6] 收集项目核心 ...")
    app = os.path.join(PAYLOAD, "app")
    if os.path.isdir(app):
        shutil.rmtree(app)
    os.makedirs(app, exist_ok=True)
    total = 0
    for name in CORE:
        src = os.path.join(ROOT, name)
        if not os.path.isdir(src):
            continue
        dst = os.path.join(app, name)
        if name == "frontend":
            # 前端要保留 assets（主题素材 + 模型图标），只去掉参考图画廊页
            copytree(src, dst, skip_names=("__pycache__",))
            for extra in CORE_SKIP_FILES:
                p = os.path.join(dst, extra)
                if os.path.isfile(p):
                    os.remove(p)
        else:
            copytree(src, dst, skip_names=("__pycache__",))
    for name in CORE_FILES:
        src = os.path.join(ROOT, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(app, name))
    # payload 里带一份自检脚本（装完可以直接跑）
    shutil.copy2(os.path.join(ROOT, "tools", "selftest.py"),
                 os.path.join(app, "tools", "selftest.py"))
    shutil.copy2(os.path.join(HERE, "init_state.py"), os.path.join(PAYLOAD, "init_state.py"))
    if _PROTECT:
        _minify_frontend(app)
        _tier1_protect(app, os.path.join(PAYLOAD, "init_state.py"))
    for root, _d, files in os.walk(app):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      核心 %.1f MB" % (total / 1048576))


# ---- PySide6（panel6 组件）裁剪 ----
# 清单来自**运行时实际加载的模块**（抓进程 Modules，不是猜）：窗口 + QtWebEngine 只用
# QtCore/Gui/Widgets/Network/PrintSupport/OpenGL/Positioning/Qml/Quick/QuickWidgets/
# WebChannel/WebEngine(Core+Widgets) + platforms/imageformats/styles/tls 等插件 + resources。
# 其余（3D/Charts/Designer/Pdf/Multimedia/qml/翻译/工具 exe…）全部踢掉。实测 632 → ~421 MB。
_PANEL6_DEL_NAMES = (
    # DLL
    "Qt6Designer*", "Qt6Pdf*", "Qt6Quick3D*", "Qt63D*", "Qt6Charts*", "Qt6Graphs*",
    "Qt6DataVisualization*", "Qt6Multimedia*", "Qt6Bluetooth*", "Qt6Nfc*", "Qt6Sensors*",
    "Qt6SerialBus*", "Qt6SerialPort*", "Qt6TextToSpeech*", "Qt6SpatialAudio*", "Qt6Scxml*",
    "Qt6StateMachine*", "Qt6RemoteObjects*", "Qt6Location*", "Qt6Help*", "Qt6UiTools*",
    "Qt6Test*", "Qt6NetworkAuth*", "Qt6HttpServer*", "Qt6WebView*", "Qt6WebSockets*",
    "Qt6DBus*", "Qt6Sql*", "Qt6CanvasPainter*", "Qt6Lottie*", "Qt6VirtualKeyboard*",
    "Qt6Labs*", "Qt6QuickTest*", "Qt6QuickDialogs2*", "Qt6QuickParticles*", "Qt6QuickTimeline*",
    "Qt6QuickEffects*", "Qt6QuickVectorImage*", "Qt6QuickShapes*", "Qt6QuickLayouts*",
    "Qt6ShaderTools*", "Qt6QuickControls2*",
    # .pyd（Python 绑定；运行时只需下面白名单那些）
    "Qt3D*.pyd", "QtBluetooth*.pyd", "QtCharts*.pyd", "QtDataVisualization*.pyd",
    "QtDesigner*.pyd", "QtGraphs*.pyd", "QtHelp*.pyd", "QtHttpServer*.pyd", "QtLocation*.pyd",
    "QtMultimedia*.pyd", "QtNfc*.pyd", "QtNetworkAuth*.pyd", "QtPdf*.pyd",
    "QtRemoteObjects*.pyd", "QtScxml*.pyd", "QtSensors*.pyd", "QtSerialBus*.pyd",
    "QtSerialPort*.pyd", "QtSpatialAudio*.pyd", "QtSql*.pyd", "QtStateMachine*.pyd",
    "QtTest*.pyd", "QtTextToSpeech*.pyd", "QtUiTools*.pyd", "QtWebEngineQuick*.pyd",
    "QtWebSockets*.pyd", "QtWebView*.pyd", "QtCanvasPainter*.pyd", "QtDBus*.pyd",
    "QtQuick*.pyd", "QtQml*.pyd", "QtConcurrent*.pyd", "QtXml*.pyd", "QtSvgWidgets*.pyd",
)
_PANEL6_KEEP_PLUGINS = {"platforms", "imageformats", "iconengines", "styles", "tls",
                        "networkinformation", "generic", "platforminputcontexts"}
_PANEL6_KEEP_QM = {"qt_zh_CN.qm", "qt_en.qm"}
_PANEL6_KEEP_LOCALES = {"en-US.pak", "zh-CN.pak"}


def _rm(path: str) -> None:
    """删文件/目录，失败不抛（被占用就跳过）。"""
    try:
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def _trim_panel6(dst: str) -> None:
    """裁掉 PySide6 里用不到的部分（见上方清单）。"""
    import fnmatch
    p = os.path.join(dst, "PySide6")
    if not os.path.isdir(p):
        return
    for name in os.listdir(p):                                  # 顶层 DLL / pyd
        fp = os.path.join(p, name)
        if os.path.isfile(fp) and any(fnmatch.fnmatch(name, pat) for pat in _PANEL6_DEL_NAMES):
            _rm(fp)
    for name in os.listdir(p):                                  # 工具 exe（留 WebEngineProcess）
        if name.lower().endswith(".exe") and name != "QtWebEngineProcess.exe":
            _rm(os.path.join(p, name))
    _rm(os.path.join(p, "qml"))                                 # QML 模块（Widgets 不用）
    tr = os.path.join(p, "translations")                        # 翻译：只留中/英 + WebEngine 两语
    if os.path.isdir(tr):
        for name in os.listdir(tr):
            fp = os.path.join(tr, name)
            if os.path.isdir(fp):
                if name != "qtwebengine_locales":
                    _rm(fp)
            elif name not in _PANEL6_KEEP_QM:
                _rm(fp)
        loc = os.path.join(tr, "qtwebengine_locales")
        if os.path.isdir(loc):
            for name in os.listdir(loc):
                if name not in _PANEL6_KEEP_LOCALES:
                    _rm(os.path.join(loc, name))
    pl = os.path.join(p, "plugins")                             # 插件：只留白名单目录
    if os.path.isdir(pl):
        for name in os.listdir(pl):
            if os.path.isdir(os.path.join(pl, name)) and name not in _PANEL6_KEEP_PLUGINS:
                _rm(os.path.join(pl, name))
    for name in ("doc", "include", "typesystems", "support", "scripts", "metatypes", "glue"):
        _rm(os.path.join(p, name))
    for root, dirs, _files in os.walk(dst):                     # __pycache__
        for dname in list(dirs):
            if dname == "__pycache__":
                _rm(os.path.join(root, dname))


# ---- 档 1：去源码（Python → .pyc；JS → 压缩）----
# ⚠ 这不是"加密"：.pyc 仍可被反编译。它只挡住"打开安装目录直接看源码"。
_VENDOR = os.path.join(HERE, "_vendor")          # 项目内 vendor（rjsmin）
_PROTECT = True                                   # --no-protect 可关
PYC_ENTRIES = {                                   # 会被 `python <file>` 启动 → 留一个极小 .py 壳
    "backend/server.py", "backend/watch.py", "backend/panel_window.py",
    "backend/tray_host.py", "backend/pty_host.py",
    "launchers/launch_opencode.py", "scripts/heal_opencode_link.py",
    "tools/spectrum.py", "tools/music_service.py", "tools/selftest.py",
}
PYC_LEAVE = {                                     # 既被 import 又被当脚本跑 → 保留源码（很小、不敏感）
    "backend/cleanup.py", "backend/taskbar.py", "backend/acp_mock_agent.py",
}
_PYC_SHIM = (
    "# -*- coding: utf-8 -*-\n"
    "# 自动生成的启动壳：真正的代码 = 同目录下的同名 .pyc\n"
    "import os, runpy, sys\n"
    "_d = os.path.dirname(os.path.abspath(__file__))\n"
    "sys.path.insert(0, _d)\n"
    "runpy.run_path(os.path.join(_d, os.path.basename(__file__) + 'c'), run_name='__main__')\n"
)


def _minify_frontend(app: str) -> None:
    """JS 压缩（rjsmin，纯 Python，安全）。"""
    try:
        if _VENDOR in sys.path:
            sys.path.remove(_VENDOR)
        sys.path.insert(0, _VENDOR)
        import rjsmin  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log("      [!] rjsmin 不可用，跳过 JS 压缩：%s" % exc)
        return
    js = os.path.join(app, "frontend", "app.js")
    try:
        with open(js, encoding="utf-8") as fh:
            src = fh.read()
        out = rjsmin.jsmin(src)
        with open(js, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(out)
        log("      JS 压缩：app.js %.0fKB -> %.0fKB" % (len(src) / 1024, len(out) / 1024))
    except Exception as exc:  # noqa: BLE001
        log("      [!] JS 压缩失败：%s" % exc)


def _tier1_protect(app: str, init_state_path: str) -> None:
    """Python 去源码：编成 .pyc；入口脚本留极小 .py 壳（启动路径/快捷方式/iss 都不用改）。"""
    import compileall  # noqa: PLC0415
    compileall.compile_dir(app, legacy=True, quiet=1, force=True)
    n_pyc = n_shim = n_keep = 0
    for root, _d, files in os.walk(app):
        for f in files:
            if not f.endswith(".py"):
                continue
            full = os.path.join(root, f)
            rel = os.path.relpath(full, app).replace("\\", "/")
            pyc = full + "c"
            if rel in PYC_LEAVE:
                _rm(pyc)                       # 保留源码 → 删掉 .pyc（免得优先用旧的 .pyc）
                n_keep += 1
            elif rel in PYC_ENTRIES:
                with open(full, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(_PYC_SHIM)
                n_shim += 1
            else:
                _rm(full)                      # 只留 .pyc
                n_pyc += 1
    # init_state.py 在 payload 根（不在 app 下），单独处理：编译 + 换成壳
    try:
        compileall.compile_file(init_state_path, legacy=True, quiet=1, force=True)
        with open(init_state_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(_PYC_SHIM)
    except Exception as exc:  # noqa: BLE001
        log("      [!] init_state 编译失败：%s" % exc)
    log("      档1 去源码：.pyc %d / 启动壳 %d / 保留源码 %d" % (n_pyc, n_shim, n_keep))


ALLOW_DRIFT = False


def _pkg_json_version(pkg_dir: str) -> str:
    pj = os.path.join(pkg_dir, "package.json")
    try:
        with open(pj, "r", encoding="utf-8") as fh:
            return str((json.load(fh) or {}).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


def _check_codex_versions() -> None:
    """本地 codex 依赖必须与 deps.lock.json 一致（除非 --allow-drift）。"""
    want = LOCK.get("codex_deps") or {}
    bad = []
    for _src, rel in CODEX_DEPS:
        got = _pkg_json_version(os.path.join(CODEX_NM, *rel.split("/")))
        exp = str(want.get(rel) or "")
        if exp and got and got != exp:
            bad.append("%s: %s != %s" % (rel, got, exp))
    if bad and not ALLOW_DRIFT:
        raise SystemExit(
            "[BAD] codex 依赖版本与 deps.lock.json 不一致：\n  " + "\n  ".join(bad) +
            "\n  要么更新锁文件，要么临时加 --allow-drift。")


def _site_versions(site: str) -> dict:
    """从 `*.dist-info/METADATA` 读已安装版本（不依赖 pip）。"""
    out = {}
    try:
        names = os.listdir(site)
    except OSError:
        return out
    for d in names:
        if not d.endswith(".dist-info"):
            continue
        meta = os.path.join(site, d, "METADATA")
        name = ver = ""
        try:
            with open(meta, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("Name: "):
                        name = line[6:].strip()
                    elif line.startswith("Version: "):
                        ver = line[9:].strip()
                    if name and ver:
                        break
        except OSError:
            continue
        if name:
            out[name.lower()] = ver
    return out


def _check_component_versions(name: str, site: str) -> None:
    """组件 venv 的关键包版本必须与锁一致（除非 --allow-drift）。"""
    want = (LOCK.get("components") or {}).get(name) or []
    if not want:
        return
    got = _site_versions(site)
    bad = []
    for spec in want:
        n, _, v = str(spec).partition("==")
        g = got.get(n.lower())
        if g and v and g != v:
            bad.append("%s: %s != %s" % (n, g, v))
    if bad and not ALLOW_DRIFT:
        raise SystemExit(
            "[BAD] 组件 %s 的版本与 deps.lock.json 不一致：\n  %s\n"
            "  先按 docs/BUILD.md 重建 venv（或临时加 --allow-drift）。"
            % (name, "\n  ".join(bad)))


def stage_components(which) -> None:
    for name in which:
        src = VENV_SITE.get(name, "")
        if not src or not os.path.isdir(src):
            log("[!] 可选组件 %s 没有可用的 site-packages：%s" % (name, src))
            continue
        _check_component_versions(name, src)
        dst = os.path.join(PAYLOAD, "components", name, "site-packages")
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        log("[3/6] 收集可选组件 %s ..." % name)
        copytree(src, dst, skip_names=SITE_SKIP.get(name, SITE_SKIP_COMMON))
        tot = 0
        for root, _d, files in os.walk(dst):
            for f in files:
                try:
                    tot += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        log("      %s %.1f MB" % (name, tot / 1048576))


def _portable_node() -> str:
    """构建机上可用的 node.exe（便携那份优先）。"""
    p = os.path.join(NODE_SRC, "node.exe") if NODE_SRC else ""
    if p and os.path.isfile(p):
        return p
    import shutil as _sh
    return _sh.which("node") or ""


def _split_pkg(spec: str) -> tuple:
    """`@scope/name@1.2.3` → (`@scope/name`, `1.2.3`)；没版本 → (name, "")。"""
    name, sep, ver = str(spec or "").rpartition("@")
    if sep and name:
        return name, ver
    return str(spec or ""), ""


def _pinned_pkg(pkg: str) -> str:
    """按锁文件补版本：`opencode-ai` → `opencode-ai@1.19.0`。"""
    name, ver = _split_pkg(pkg)
    if ver:
        return pkg
    want = (LOCK.get("npm") or {}).get(name)
    return "%s@%s" % (name, want) if want else pkg


def _npm_install_adapter(pkg: str) -> str:
    """用便携 npm 把某个 ACP 适配器装到 cache 目录，返回它的 `node_modules` 路径。

    ⚠ 只在**构建机**上下载（联网），产物进 payload；目标机器不需要 npm。
      装的是"只有这一个包 + 它的运行依赖"（`--omit=dev`），所以 node_modules 很干净。
    ★ 版本从 `packaging/deps.lock.json` 补（没锁到就退回不指定版本，并打印警告）。
    """
    spec = _pinned_pkg(pkg)
    name, ver = _split_pkg(spec)
    if not ver:
        log("      [!] deps.lock.json 没有 %s 的版本 —— 这次会装最新版（不建议）" % name)
    node = _portable_node()
    if not node:
        log("      [skip] 构建机上没有 node，装不了 %s" % spec)
        return ""
    npm_cli = os.path.join(os.path.dirname(node), "node_modules", "npm", "bin", "npm-cli.js")
    prefix = os.path.join(CACHE, "npm-" + re.sub(r"[^a-z0-9]+", "_", name.lower()))
    nm = os.path.join(prefix, "node_modules")
    pkg_dir = os.path.join(nm, *name.split("/"))
    if os.path.isdir(pkg_dir):
        got = _pkg_json_version(pkg_dir)
        if not ver or not got or got == ver:
            log("      复用已下载的 %s（本地 %s）" % (spec, got or "?"))
            return nm
        log("      版本变了（%s -> %s），重装 %s" % (got, ver, name))
        shutil.rmtree(prefix, ignore_errors=True)
    os.makedirs(prefix, exist_ok=True)
    log("      npm 安装 %s（联网，首次会慢一点）..." % spec)
    cmd = [node, npm_cli, "install", "--prefix", prefix, "--omit=dev",
           "--no-audit", "--no-fund", "--loglevel=error", spec]
    # ⚠ npm 的 postinstall 会用裸 `node`（如 opencode-ai 是 `node ./postinstall.mjs`）——
    #   我们的便携 node 不在 PATH 上，必须把它的目录塞进 PATH，否则 postinstall 直接失败
    #   （实测：opencode-ai 装完 node_modules 是空的）。子进程继承这份 env 即可。
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=3600, cwd=prefix, env=env)
    except Exception as exc:  # noqa: BLE001
        log("      [skip] npm 失败：%r" % (exc,))
        return ""
    if not os.path.isdir(os.path.join(nm, *name.split("/"))):
        log("      [skip] npm 没装成（rc=%s）：%s" % (r.returncode, (r.stderr or "")[-300:]))
        return ""
    return nm


def _stage_node() -> int:
    """随包 node 运行时（两个 agent 适配器共用）。"""
    node_exe = _portable_node()
    if not node_exe or not os.path.isfile(node_exe):
        raise SystemExit(
            "[BAD] 找不到 node.exe（需要 node v%s）。\n"
            "  设 COCRAFT_NODE_SRC 指向解压目录，例如：\n"
            "    set COCRAFT_NODE_SRC=D:\\agentlist\\deepseekharness\\node\\node-v%s-win-x64\n"
            "  详见 docs/BUILD.md。" % (NODE_VER, NODE_VER))
    _verify_sha256(node_exe, NODE_SHA256, "node.exe %s" % NODE_VER)
    node_dir = os.path.dirname(node_exe)
    node_dst = os.path.join(PAYLOAD, "agents", "node")
    os.makedirs(node_dst, exist_ok=True)
    for f in ("node.exe", "LICENSE", "README.md"):
        s = os.path.join(node_dir, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(node_dst, f))
    # ★必须带 npm：面板内「安装 / 更新 agent」用的是 `<node> <npm-cli.js> install`
    #   （以前只拷 node.exe，装出来的包在安装版上**根本没有 npm**，更新按钮必然不可用）。
    npm_src = os.path.join(node_dir, "node_modules", "npm")
    if not os.path.isdir(npm_src):
        raise SystemExit(
            "[BAD] node 目录里没有 node_modules/npm：%s\n"
            "  随包 node 必须带 npm（面板内 agent 更新依赖它）。换一个官方 node 压缩包，"
            "或检查 COCRAFT_NODE_SRC。" % node_dir)
    copytree(npm_src, os.path.join(node_dst, "node_modules", "npm"),
             skip_names=("__pycache__", "docs", "man"))
    got_npm = _pkg_json_version(npm_src)
    if NPM_VER and got_npm and got_npm != NPM_VER:
        raise SystemExit(
            "[BAD] 自带 npm 版本 %s 与 deps.lock.json 的 %s 不一致；"
            "换回对应 node 版本，或更新锁文件。" % (got_npm, NPM_VER))
    log("      node 自带 npm %s 一并打包（面板内安装/更新要用）" % (got_npm or "?"))
    tot = 0
    for root, _d, files in os.walk(node_dst):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      node %.1f MB" % (tot / 1048576))
    return tot


def _stage_codex() -> None:
    """随包 Codex：适配器 + 依赖 + 原生 codex CLI + **干净**的 codex-home 模板。"""
    if not CODEX_NM or not os.path.isdir(CODEX_NM):
        raise SystemExit(
            "[BAD] 找不到 codex 的 node_modules。\n"
            "  设 COCRAFT_CODEX_NM 指向它，例如：\n"
            "    set COCRAFT_CODEX_NM=D:\\agentlist\\codex\\node_modules\n"
            "  详见 docs/BUILD.md。")
    _check_codex_versions()
    nm_dst = os.path.join(PAYLOAD, "agents", "codex", "node_modules")
    os.makedirs(nm_dst, exist_ok=True)
    pj = os.path.join(CODEX_NM, "..", "package.json")
    if os.path.isfile(pj):
        shutil.copy2(pj, os.path.join(PAYLOAD, "agents", "codex", "package.json"))
    for src, rel in CODEX_DEPS:
        if not os.path.isdir(src):
            log("      [skip] 缺：%s" % rel)
            continue
        dst = os.path.join(nm_dst, *rel.split("/"))
        copytree(src, dst, skip_names=("__pycache__",))
    home = os.path.join(PAYLOAD, "agents", "codex", "codex-home-template")
    os.makedirs(home, exist_ok=True)
    with open(os.path.join(home, "config.toml"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            '# cocraft 随包 Codex 配置（干净模板，不含任何密钥）\n'
            '# API Key 由「首次设置」向导写入 runtime/state/_acp_agents.json 的 baseline.env，\n'
            '# 或你自己设成系统环境变量 DEEPSEEK_API_KEY。\n'
            '# 想换成别的 provider：改 base_url / env_key / model 三项即可。\n'
            'model = "deepseek-flash"\n'
            'model_provider = "deepseek"\n'
            'model_context_window = 1048576\n\n'
            '[model_providers.deepseek]\n'
            'name = "DeepSeek"\n'
            'base_url = "https://api.deepseek.com/v1"\n'
            'env_key = "DEEPSEEK_API_KEY"\n'
            'wire_api = "responses"\n'
            '# ⚠ 这个 Codex 版本已移除 wire_api="chat"，只支持 Responses API\n'
        )
    tot = 0
    for root, _d, files in os.walk(os.path.join(PAYLOAD, "agents", "codex")):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      codex %.1f MB" % (tot / 1048576))


def _stage_claude() -> None:
    """随包 Claude **适配器**（`@zed-industries/claude-code-acp`）。

    ⚠ **不带 Claude Code 本体**（用户 2026-09-26 明确要求）：适配器只是 ACP 翻译层，
      调用目标机器上已登录的 Claude Code（或在 baseline.env 配 ANTHROPIC_API_KEY）。
    """
    nm = _npm_install_adapter("@zed-industries/claude-code-acp")
    if not nm:
        log("      [skip] 没拿到 Claude 适配器，包里就不带它（面板仍会列出档案并提示缺适配器）")
        return
    dst = os.path.join(PAYLOAD, "agents", "claude", "node_modules")
    copytree(nm, dst, skip_names=("__pycache__",))
    # 裁掉 SDK 里**别的平台**的 ripgrep（它打包了 6 个平台，白占 ~25MB；我们只跑 win32-x64）
    rg = os.path.join(dst, "@anthropic-ai", "claude-agent-sdk", "vendor", "ripgrep")
    if os.path.isdir(rg):
        for name in sorted(os.listdir(rg)):
            if name != "x64-win32":
                shutil.rmtree(os.path.join(rg, name), ignore_errors=True)
                log("      裁掉非 win32 平台：vendor/ripgrep/%s" % name)
    tot = 0
    for root, _d, files in os.walk(os.path.join(PAYLOAD, "agents", "claude")):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      claude %.1f MB" % (tot / 1048576))


def _stage_opencode() -> None:
    """随包 OpenCode（npm 分发 `opencode-ai`，与 codex 同款的 node_modules 布局）。

    ⚠ 与桌面版自带的 `opencode-cli.exe` 是同一款程序、不同分发：
      - 这里随包的是 npm 包 `opencode-ai`（自包含 `bin/opencode.exe`，可跑 `acp` 子命令）。
      - **只拷 `opencode-ai` 这一个包**：npm 还会顺带拉 `opencode-windows-x64` /
        `-baseline`（各 ~172MB）——都是同一份二进制的副本，拷进去会让 payload 三倍膨胀。
      - 运行时的隔离数据目录（`XDG_DATA_HOME`）由 agents.py / init_state.py 指到
        `<app>\\runtime\\opencode-data`，**不碰用户自己的 OpenCode 数据**（同 codex 的 `CODEX_HOME`）。
    """
    nm = _npm_install_adapter("opencode-ai")
    if not nm:
        log("      [skip] 没拿到 opencode-ai（包里就不带随包 OpenCode）")
        return
    src = os.path.join(nm, "opencode-ai")
    if not os.path.isdir(src):
        log("      [skip] npm 目录里没有 opencode-ai")
        return
    dst = os.path.join(PAYLOAD, "agents", "opencode", "node_modules", "opencode-ai")
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    copytree(src, dst, skip_names=("__pycache__",))
    tot = 0
    for root, _d, files in os.walk(os.path.join(PAYLOAD, "agents", "opencode")):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      opencode %.1f MB" % (tot / 1048576))


def stage_agent(which: str) -> None:
    """随包 agent：`codex` / `claude` / `opencode`（逗号分隔也行）/ `all` / `none`。"""
    wants = [w.strip().lower() for w in str(which).split(",") if w.strip()]
    if not wants or "none" in wants:
        log("[4/6] 不随包 agent")
        return
    if "both" in wants or "all" in wants:
        wants = ["codex", "claude", "opencode"]
    log("[4/6] 收集随包 agent（%s）..." % ",".join(wants))
    _stage_node()
    if "codex" in wants:
        _stage_codex()
    if "claude" in wants:
        _stage_claude()
    if "opencode" in wants:
        _stage_opencode()


def write_version() -> str:
    """版本号从 frontend/index.html 的 ?v= 取（和前端版本一致）。"""
    with open(os.path.join(ROOT, "frontend", "index.html"), "r", encoding="utf-8") as fh:
        import re
        m = re.search(r"/app\.js\?v=([\w.\-]+)", fh.read())
    ver = m.group(1) if m else "0.0.0"
    with open(os.path.join(PAYLOAD, "VERSION"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ver + "\n")
    return ver


def kill_stale_build_helpers() -> None:
    """打包前清掉会占住 payload 的残留进程。

    实测踩到：上一轮 `ISCC.exe` 还在压缩（或挂住）时又起一轮 → `clean_payload` 删
    `codex.exe` 报 `WinError 32 另一个程序正在使用此文件`。这里按**进程名**清
    （⚠ 绝不能按命令行匹配 —— 本脚本自己的命令行里就含 payload 路径，会误杀自己）。
    """
    try:
        r = subprocess.run(["taskkill", "/IM", "ISCC.exe", "/F"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30)
        if "SUCCESS" in (r.stdout or "").upper() or r.returncode == 0:
            log("      已清掉残留的 ISCC.exe")
    except Exception:  # noqa: BLE001
        pass
    # 辅助进程（音乐/频谱/SMTC/ACP agent）也可能占着 payload 里的文件
    try:
        helper = os.path.join(ROOT, "backend", "cleanup.py")
        if os.path.isfile(helper):
            # ⚠ 一定要给 encoding/errors：cleanup.py 打的是 UTF-8，控制台默认 GBK，
            #   不给就会在读取线程里抛 UnicodeDecodeError（看起来像"打包失败"，其实只是噪声）。
            subprocess.run([sys.executable, helper], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=120)
    except Exception:  # noqa: BLE001
        pass


def drop_tree(path: str, tries: int = 3) -> None:
    """删目录：先重试几次，实在被占住就**改名让开**（别让一次扫描锁死整个打包）。"""
    if not os.path.isdir(path):
        return
    for i in range(tries):
        try:
            shutil.rmtree(path)
            return
        except PermissionError as exc:
            if i == tries - 1:
                aside = "%s.stale-%d" % (path, int(time.time()))
                try:
                    os.rename(path, aside)
                    log("      [i] %s 被占用，改名让开 → %s" % (os.path.basename(path),
                                                        os.path.basename(aside)))
                    return
                except Exception:  # noqa: BLE001
                    raise SystemExit("[BAD] 删不掉也改不掉：%s（%s）" % (path, exc))
            time.sleep(2)


def clean_payload() -> None:
    """去掉不该进包的东西：staging 时产生的 runtime/ 与 __pycache__。

    ⚠ `payload/app/runtime` 是**运行时状态**（含 agent 注册表 / 密钥 / 浏览器 profile / 日志）：
      既不该随包（会把开发机的数据带给别人），也必须由安装后 `init_state.py` 现建。
    """
    drop_tree(os.path.join(PAYLOAD, "app", "runtime"))
    for root, dirs, _files in os.walk(PAYLOAD):
        for d in list(dirs):
            if d == "__pycache__":
                drop_tree(os.path.join(root, d), tries=1)


def strip_music_payload() -> bool:
    r"""★精简包（`ui 0.2.123` 起**默认**）：把音乐 / 音频条 / QQ音乐(SMTC) / 网易云从 payload 摘干净。

    复用 `packaging/strip_music.py`（幂等）：删文件 + 删 DOM + `40-music.js` 换成同名空实现桩
    + 设置里那几行绑定改空安全。失败**不静默**——宁可打不出包，也别出一个"一启动就报错"的包。
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        import strip_music  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        log("[BAD] 加载 packaging/strip_music.py 失败：%r" % (exc,))
        return False
    app = os.path.join(PAYLOAD, "app")
    if not os.path.isdir(os.path.join(app, "frontend")):
        log("[BAD] payload 里没有 frontend/，没法做精简包")
        return False
    log("[4b] 精简包：摘掉音乐 / 音频条 / QQ音乐 / 网易云 ...")
    try:
        rc = strip_music.main(["strip_music", app])
    except Exception as exc:  # noqa: BLE001
        log("[BAD] 精简包失败：%r" % (exc,))
        return False
    return rc == 0


def _v4(ver: str) -> str:
    """把 SemVer（可能带 -rc1）折成 Inno 要求的 **4 段数字**文件版本：`0.1.0` → `0.1.0.0`。"""
    base = str(ver or "").split("-")[0].split("+")[0]
    nums = re.findall(r"\d+", base) or ["0"]
    nums = (nums + ["0", "0", "0"])[:4]
    return ".".join(str(min(int(n), 65535)) for n in nums)


def run_iscc(ver: str) -> str:
    iscc = next((p for p in ISCC_CANDIDATES if os.path.isfile(p)), "")
    if not iscc:
        log("[!] 找不到 ISCC.exe（Inno Setup）。装一下：winget install JRSoftware.InnoSetup")
        return ""
    log("[5/6] 编译安装包（Inno Setup）...")
    iss = os.path.join(HERE, "cocraft.iss")
    out = os.path.join(DIST, "cocraft-setup-%s.exe" % ver)
    cmd = [iscc, "/Qp", "/DAppVersion=%s" % ver, "/DVersionInfoVersion=%s" % _v4(ver),
           "/DPayloadDir=%s" % PAYLOAD, "/DOutputDir=%s" % DIST, iss]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not os.path.isfile(out):
        log("[BAD] 编译失败 rc=%s" % r.returncode)
        log((r.stdout or "")[-1500:])
        log((r.stderr or "")[-800:])
        return ""
    log("      安装包 %.1f MB" % (os.path.getsize(out) / 1048576))
    return out


COMPONENTS_GLOBAL: list = []


def check_lock(strict: bool = False) -> int:
    """校验依赖锁 + 打印本机构建素材是否就位（CI 用 `--check-lock`）。"""
    ok = True
    log("[lock] %s" % LOCK_FILE)
    if not LOCK:
        log("[BAD] 读不到 deps.lock.json（构建依赖未锁定）")
        return 1
    log("  python  %s  sha256=%s" % (
        (LOCK.get("python") or {}).get("version"),
        ((LOCK.get("python") or {}).get("sha256") or "")[:16] + "..."))
    log("  node    %s  npm %s  sha256=%s" % (NODE_VER, NPM_VER or "?",
                                                 (NODE_SHA256 or "")[:16] + "..."))
    for k, v in sorted((LOCK.get("npm") or {}).items()):
        log("  npm     %s@%s" % (k, v))
    for name, specs in sorted((LOCK.get("components") or {}).items()):
        log("  comp    %-9s %d packages" % (name, len(specs or [])))

    missing = []
    if not NODE_SRC or not os.path.isfile(os.path.join(NODE_SRC, "node.exe")):
        missing.append("node.exe（设 COCRAFT_NODE_SRC）")
    if not CODEX_NM or not os.path.isdir(CODEX_NM):
        missing.append("codex node_modules（设 COCRAFT_CODEX_NM）")
    for name, site in VENV_SITE.items():
        if name == "music":
            continue          # ★默认不带音乐（`ui 0.2.123`）→ 缺这个 venv 不算问题
        if not os.path.isdir(site):
            missing.append("组件 venv：%s（%s）" % (name, site))
    if not os.path.isdir(VENV_SITE.get("music", "")):
        log("  [i] music 组件 venv 不存在 —— 默认打包不需要它（要带音乐用 --with-music）")
    if not any(os.path.isfile(p) for p in ISCC_CANDIDATES):
        log("  [warn] 没找到 ISCC.exe（只影响出 exe；--no-iss 不需要）")
    if missing:
        log("  [warn] 本机缺这些构建素材（只影响完整打包）：")
        for m in missing:
            log("         - %s" % m)
        if strict:
            ok = False
    log("[lock] %s" % ("OK" if ok else "MISSING（--strict）"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 cocraft 安装包")
    ap.add_argument("--agents", default="codex,claude,opencode",
                    help="随包 agent：codex / claude / opencode（逗号分隔也行）/ all / none")
    ap.add_argument("--components", default="terminal,panel",
                    help="准备哪些组件：terminal,panel（默认；music 已默认不带，要带请用 --with-music）")
    ap.add_argument("--with-music", action="store_true",
                    help="★显式带上音乐（music 组件 + 不执行 strip_music）；默认不带")
    ap.add_argument("--no-iss", action="store_true", help="只准备 payload，不编译安装包")
    ap.add_argument("--no-protect", action="store_true",
                    help="不做「档1 去源码」（调试用，保留 .py 源码）")
    ap.add_argument("--check-lock", action="store_true",
                    help="只校验 packaging/deps.lock.json 与本机构建素材，不打包")
    ap.add_argument("--strict", action="store_true",
                    help="配合 --check-lock：缺构建素材也算失败")
    ap.add_argument("--allow-drift", action="store_true",
                    help="允许组件/codex 依赖版本与锁文件不一致（不推荐）")
    args = ap.parse_args()

    global COMPONENTS_GLOBAL, _PROTECT, ALLOW_DRIFT
    ALLOW_DRIFT = bool(args.allow_drift)
    if args.check_lock:
        return check_lock(args.strict)
    _PROTECT = not args.no_protect
    COMPONENTS_GLOBAL = [c.strip() for c in args.components.split(",") if c.strip()]
    # 兼容旧名：panel6（已弃用的 PySide6 尝试）统一回 panel（pywebview）
    COMPONENTS_GLOBAL = ["panel" if c == "panel6" else c for c in COMPONENTS_GLOBAL]
    # ★音乐默认不带（`ui 0.2.123`）：写了也不给，只有 --with-music 才算数
    with_music = bool(args.with_music)
    if "music" in COMPONENTS_GLOBAL and not with_music:
        log("[i] 已忽略 --components 里的 music（要带请显式加 --with-music）")
    if not with_music:
        COMPONENTS_GLOBAL = [c for c in COMPONENTS_GLOBAL if c != "music"]
    elif "music" not in COMPONENTS_GLOBAL:
        COMPONENTS_GLOBAL.append("music")

    os.makedirs(PAYLOAD, exist_ok=True)
    kill_stale_build_helpers()                 # 清掉会占住 payload 的残留（ISCC / 辅助进程）
    for stale in ("agents", "components", "app", "python"):
        drop_tree(os.path.join(PAYLOAD, stale))
    fetch_python()
    stage_core()
    stage_components(COMPONENTS_GLOBAL)
    stage_agent(args.agents)
    clean_payload()
    # ★精简包：摘掉音乐 / 音频条 / QQ音乐(SMTC) / 网易云（文件 + DOM + JS 桩）
    if not with_music:
        if not strip_music_payload():
            return 1
    ver = write_version()
    log("[6/6] payload 就绪：%s（版本 %s，可选组件 %s，agent %s%s）"
        % (PAYLOAD, ver, ",".join(COMPONENTS_GLOBAL) or "无", args.agents,
           "" if with_music else "，已去音乐"))
    tot = 0
    for root, _d, files in os.walk(PAYLOAD):
        for f in files:
            try:
                tot += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      payload 合计 %.1f MB" % (tot / 1048576))
    if args.no_iss:
        return 0
    exe = run_iscc(ver)
    if not exe:
        return 1
    log("完成：%s" % exe)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
