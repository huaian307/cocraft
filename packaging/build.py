# -*- coding: utf-8 -*-
r"""把 cocraft 打包成安装包（Inno Setup）要用的 payload。

产物（都在 dist/ 下）：
    dist/payload/app/                      项目核心（backend/frontend/tools/launchers/scripts/docs）
    dist/payload/python/                   嵌入式 Python 3.12 x64（官方便携版，自带 pythonw.exe）
    dist/payload/init_state.py             安装后初始化 runtime/state（写 agent 注册表、引擎、干净模板）
    dist/payload/agents/node/node.exe      node 运行时（随包 agent 用）
    dist/payload/agents/codex/…            codex-acp + 依赖 + 原生 codex.exe + 干净的 codex-home 模板
    dist/payload/components/music/…        可选：音乐服务的 site-packages
    dist/payload/components/audio/…        可选：音频频谱的 site-packages
    dist/cocraft-setup-<ver>.exe       由 ISCC 编译出来的安装包

用法：
    python packaging\build.py                 # 默认：核心 + codex agent（可选组件不预置）
    python packaging\build.py --agents none    # 只出核心包
    python packaging\build.py --components music,audio   # 顺手把可选组件的 site-packages 也备好
    python packaging\build.py --no-iss          # 只准备 payload，不调 ISCC

⚠ 绝不随包带任何密钥：`codex-home` 只放**干净模板**（env_key 指向环境变量），
   用户的 API Key 由首次设置向导写进 runtime/state/_acp_agents.json 的 baseline.env。

输出纯 ASCII（Windows 控制台是 GBK），中文只出现在写进文件的 JSON 里。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DIST = os.path.join(ROOT, "dist")
PAYLOAD = os.path.join(DIST, "payload")
CACHE = os.path.join(DIST, "cache")

PY_VER = "3.12.10"
PY_URL = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"

# 项目核心（要进包的东西）
CORE = ("backend", "frontend", "tools", "launchers", "scripts", "docs")
CORE_FILES = ("AGENTS.md", "README.md")
# 核心里的排除项：只去掉参考图画廊页（assets 里的主题素材**必须保留**）
CORE_SKIP_FILES = ("refs.html", "refs_q.html")

# 可选组件的 site-packages 来源（本机是 venv）
VENV_SITE = {
    "music": os.path.join(ROOT, "runtime", "venvs", "music", "Lib", "site-packages"),
    "audio": os.path.join(ROOT, "runtime", "venvs", "audio", "Lib", "site-packages"),
    # 面板内终端（ConPTY / pywinpty）与原生窗口（WebView2 / pywebview+pythonnet）
    "terminal": os.path.join(ROOT, "runtime", "venvs", "terminal", "Lib", "site-packages"),
    "panel": os.path.join(ROOT, "runtime", "venvs", "panel", "Lib", "site-packages"),
}
# 打包时要踢掉的东西：pip/wheel/缓存/元数据（运行时不看）。
# ⚠ **每个组件不一样**：`NeteaseCloudMusic` 运行时要 `pkg_resources`（来自 setuptools）——
#   实测：裁掉 setuptools 后 `import NeteaseCloudMusic` 直接 ModuleNotFoundError。
#   所以音乐组件必须**保留 setuptools/pkg_resources**（+6.7MB），音频组件可以踢。
SITE_SKIP_COMMON = ("pip", "pip-*", "wheel", "wheel-*", "__pycache__", "*.dist-info", "*.egg-info")
SITE_SKIP = {
    "music": SITE_SKIP_COMMON,
    "audio": SITE_SKIP_COMMON + ("setuptools", "setuptools-*", "pkg_resources"),
    "terminal": SITE_SKIP_COMMON + ("setuptools", "setuptools-*", "pkg_resources"),
    "panel": SITE_SKIP_COMMON + ("setuptools", "setuptools-*", "pkg_resources"),
}

# 随包 agent（codex 完整路线）
NODE_SRC = r"D:\agentlist\deepseekharness\node\node-v22.23.3-win-x64"
CODEX_NM = r"D:\agentlist\codex\node_modules"
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
    for root, _d, files in os.walk(app):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    log("      核心 %.1f MB" % (total / 1048576))


def stage_components(which) -> None:
    for name in which:
        src = VENV_SITE.get(name, "")
        if not src or not os.path.isdir(src):
            log("[!] 可选组件 %s 没有可用的 site-packages：%s" % (name, src))
            continue
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
    p = os.path.join(NODE_SRC, "node.exe")
    if os.path.isfile(p):
        return p
    import shutil as _sh
    return _sh.which("node") or ""


def _npm_install_adapter(pkg: str) -> str:
    """用便携 npm 把某个 ACP 适配器装到 cache 目录，返回它的 `node_modules` 路径。

    ⚠ 只在**构建机**上下载（联网），产物进 payload；目标机器不需要 npm。
      装的是"只有这一个包 + 它的运行依赖"（`--omit=dev`），所以 node_modules 很干净。
    """
    node = _portable_node()
    if not node:
        log("      [skip] 构建机上没有 node，装不了 %s" % pkg)
        return ""
    npm_cli = os.path.join(os.path.dirname(node), "node_modules", "npm", "bin", "npm-cli.js")
    prefix = os.path.join(CACHE, "npm-" + re.sub(r"[^a-z0-9]+", "_", pkg.lower()))
    nm = os.path.join(prefix, "node_modules")
    if os.path.isdir(os.path.join(nm, *pkg.split("/"))):
        log("      复用已下载的 %s" % pkg)
        return nm
    os.makedirs(prefix, exist_ok=True)
    log("      npm 安装 %s（联网，首次会慢一点）..." % pkg)
    cmd = [node, npm_cli, "install", "--prefix", prefix, "--omit=dev",
           "--no-audit", "--no-fund", "--loglevel=error", pkg]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=3600, cwd=prefix)
    except Exception as exc:  # noqa: BLE001
        log("      [skip] npm 失败：%r" % (exc,))
        return ""
    if not os.path.isdir(os.path.join(nm, *pkg.split("/"))):
        log("      [skip] npm 没装成（rc=%s）：%s" % (r.returncode, (r.stderr or "")[-300:]))
        return ""
    return nm


def _stage_node() -> int:
    """随包 node 运行时（两个 agent 适配器共用）。"""
    node_dst = os.path.join(PAYLOAD, "agents", "node")
    os.makedirs(node_dst, exist_ok=True)
    for f in ("node.exe", "LICENSE", "README.md"):
        s = os.path.join(NODE_SRC, f)
        if os.path.isfile(s):
            shutil.copy2(s, os.path.join(node_dst, f))
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


def stage_agent(which: str) -> None:
    """随包 agent：`codex` / `claude` / `both`（逗号分隔也行）/ `none`。"""
    wants = [w.strip().lower() for w in str(which).split(",") if w.strip()]
    if not wants or "none" in wants:
        log("[4/6] 不随包 agent")
        return
    if "both" in wants:
        wants = ["codex", "claude"]
    log("[4/6] 收集随包 agent（%s）..." % ",".join(wants))
    _stage_node()
    if "codex" in wants:
        _stage_codex()
    if "claude" in wants:
        _stage_claude()


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
                           capture_output=True, text=True, timeout=30)
        if "SUCCESS" in (r.stdout or "").upper() or r.returncode == 0:
            log("      已清掉残留的 ISCC.exe")
    except Exception:  # noqa: BLE001
        pass
    # 辅助进程（音乐/频谱/SMTC/ACP agent）也可能占着 payload 里的文件
    try:
        helper = os.path.join(ROOT, "backend", "cleanup.py")
        if os.path.isfile(helper):
            subprocess.run([sys.executable, helper], capture_output=True, timeout=120)
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


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 cocraft 安装包")
    ap.add_argument("--agents", default="codex",
                    help="随包 agent：codex / claude / both（逗号分隔也行）/ none")
    ap.add_argument("--components", default="", help="顺带准备哪些可选组件：music,audio")
    ap.add_argument("--no-iss", action="store_true", help="只准备 payload，不编译安装包")
    args = ap.parse_args()

    global COMPONENTS_GLOBAL
    COMPONENTS_GLOBAL = [c.strip() for c in args.components.split(",") if c.strip()]

    os.makedirs(PAYLOAD, exist_ok=True)
    kill_stale_build_helpers()                 # 清掉会占住 payload 的残留（ISCC / 辅助进程）
    for stale in ("agents", "components", "app", "python"):
        drop_tree(os.path.join(PAYLOAD, stale))
    fetch_python()
    stage_core()
    stage_components(COMPONENTS_GLOBAL)
    stage_agent(args.agents)
    clean_payload()
    ver = write_version()
    log("[6/6] payload 就绪：%s（版本 %s，可选组件 %s，agent %s）"
        % (PAYLOAD, ver, ",".join(COMPONENTS_GLOBAL) or "无", args.agents))
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
