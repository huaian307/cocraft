# -*- coding: utf-8 -*-
r"""安装后初始化 cocraft 的运行时状态（由安装包调用，也可手工跑）。

它只做三件事，且**只在文件不存在时写**（重装不会覆盖用户数据）：
  1. `runtime/state/_engine.json`     → 默认引擎（装了随包 agent 就用 acp，否则 opencode 自动判定）
  2. `runtime/state/_acp_agents.json` → agent 注册表：把**随包 agent**（node + codex-acp）
     注册成 `codex-bundled`，cwd 用共享工作区；若本机已装 OpenCode 则一并给出 `opencode-acp` 候选
  3. `runtime/state/_acp_agents.json` 的 `baseline` → 共享 cwd / 审批模式（read-only）
     ⚠ **绝不写 API Key**：`baseline.env` 留空，等首次设置向导或用户自己填

用法（安装包 postinstall 会调）：
    <安装目录>\python\python.exe <安装目录>\init_state.py --app-dir <安装目录>

输出纯 ASCII。
"""
from __future__ import annotations

import argparse
import json
import os
import sys


LOGF = ""
HERE_BACKEND = ""      # 由 main() 设成 <app>\backend（给 panel_port 用）


def log(msg: str) -> None:
    line = msg.encode("ascii", "backslashreplace").decode("ascii")
    print(line, flush=True)
    # ⚠ 安装器里是 runhidden：stdout 看不到。写一份日志，出问题能查。
    if LOGF:
        try:
            os.makedirs(os.path.dirname(LOGF), exist_ok=True)
            with open(LOGF, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def env_path(name: str) -> str:
    """稳健地取用户目录里的路径：环境变量没有就按 USERPROFILE 推。

    ⚠ 安装器（Inno 的 [Run]）环境里 `LOCALAPPDATA` 未必有 —— 实测：写不写注册表
      取决于这里，所以不能只认 %LOCALAPPDATA%。
    """
    la = os.environ.get("LOCALAPPDATA", "")
    if not la:
        prof = os.environ.get("USERPROFILE", "")
        if prof:
            la = os.path.join(prof, "AppData", "Local")
    return os.path.join(la, *name.split("/")) if la else ""


def read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fix_pth(app: str) -> None:
    """让嵌入式 Python 能找到 <app> 与 <app>\\backend（以及**已安装的可选组件**）。

    嵌入的 `._pth` 会让解释器进入"隔离模式"：
      * **不**把脚本目录加进 sys.path（普通 python 会加）→ `import cleanup` 会失败；
      * **完全忽略 `PYTHONPATH`**（实测：`import sys; 'music' in ' '.join(sys.path)` 为 False）——
        所以可选组件的 site-packages **必须写进 `._pth`**，光设 PYTHONPATH 没用（踩过）。
    `._pth` 里的相对路径是相对**该文件所在目录**（<app>\\python），所以：
        ..                              → <app>
        ..\\backend                      → <app>\\backend
        ..\\runtime\\site-packages\\music → 装了音乐组件才写
    """
    pydir = os.path.join(app, "python")
    if not os.path.isdir(pydir):
        return
    pth = next((os.path.join(pydir, f) for f in os.listdir(pydir) if f.endswith("._pth")), "")
    if not pth:
        return
    with open(pth, "r", encoding="utf-8") as fh:
        lines = [ln.rstrip("\n") for ln in fh]
    changed = False

    def ensure(entry: str) -> None:
        nonlocal changed
        if entry not in [ln.strip() for ln in lines]:
            i = next((n for n, ln in enumerate(lines) if ln.strip() == "import site"), len(lines))
            lines.insert(i, entry)
            changed = True

    if not any(ln.strip() == "import site" for ln in lines):
        lines = [ln for ln in lines if ln.strip() != "#import site"] + ["import site"]
        changed = True
    ensure("..")
    ensure("..\\backend")
    # 可选组件：装了哪个就把哪个加进来（._pth 是"隔离模式"下唯一有效的 sys.path 来源）
    # ⚠ 不要写死组件名：直接扫 `runtime\site-packages\*` 的全部子目录。
    #   （实测漏过：写死 ("music","audio") 时，新增的 terminal/panel 没进 ._pth → 嵌入式 Python 忽略 PYTHONPATH
    #    → 终端组件 import winpty 失败、原生窗口也用不上。）
    comp_root = os.path.join(app, "runtime", "site-packages")
    try:
        comps = sorted(d for d in os.listdir(comp_root)
                       if os.path.isdir(os.path.join(comp_root, d)))
    except OSError:
        comps = []
    for comp in comps:
        ensure("..\\runtime\\site-packages\\" + comp)
    if changed:
        with open(pth, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        log("[OK] 已修 %s（补 .. / ..\\backend 与已装组件）" % pth)


def find_opencode() -> list:
    """本机 OpenCode 的 ACP 启动命令 `[opencode-cli.exe, "acp"]`。找不到回 `[]`。

    ① 先借**后端自己的探测器**（`agents.find_subcommand_acp()`）：它认「CLI 的 acp 子命令」，
       扫 PATH / `%LOCALAPPDATA%\\Programs` / 同盘 `agentlist`，并按 `realpath` 去重
       —— 本机 `@opencodedesktop` 是指向 D: 的 junction，不归一化会扫出重影。
       ⚠ 以前只试两个写死的路径，安装那一刻链接恰好不在/被升级破坏 → 安装版里就没有
       `opencode-acp` 这个 agent（用户实测："agentlist 里加 opencode" 就是这个）。
    ② 探测器用不了（后端 import 失败）再回落固定路径。
    """
    try:
        if HERE_BACKEND and HERE_BACKEND in sys.path:
            sys.path.remove(HERE_BACKEND)
        if HERE_BACKEND:
            sys.path.insert(0, HERE_BACKEND)
        from engines.acp import agents as ag          # noqa: PLC0415
        cmd = ag.find_subcommand_acp()
        if cmd:
            return [str(x) for x in cmd]
    except Exception as exc:  # noqa: BLE001
        log("[i] 后端探测器不可用（%s），改用固定路径兜底" % type(exc).__name__)
    cands = [
        env_path("Programs/@opencodedesktop/resources/opencode-cli.exe"),
        env_path("Programs/@opencodedesktop/app/resources/opencode-cli.exe"),
        r"C:\Program Files\@opencodedesktop\resources\opencode-cli.exe",
    ]
    for p in cands:
        if p and os.path.isfile(p):
            return [os.path.realpath(p), "acp"]
    return []


def find_bundled_opencode() -> list:
    """随包 OpenCode（npm / Node 分发）的启动命令 `[<exe>, "acp"]`；没随包就回 `[]`。

    与 `find_opencode()`（本机客户端）**是两条**：随包那份在 `<app>\\agents\\opencode\\`，
    注册成 `opencode-node`，不和用户的客户端混在一起。布局由后端探测器决定。
    """
    try:
        if HERE_BACKEND and HERE_BACKEND in sys.path:
            sys.path.remove(HERE_BACKEND)
        if HERE_BACKEND:
            sys.path.insert(0, HERE_BACKEND)
        from engines.acp import agents as ag          # noqa: PLC0415
        cmd = ag.find_bundled_opencode()
        if cmd:
            return [str(x) for x in cmd]
    except Exception as exc:  # noqa: BLE001
        log("[i] 随包 OpenCode 探测器不可用（%s）" % type(exc).__name__)
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="初始化 cocraft 运行时状态")
    ap.add_argument("--app-dir", required=True, help="安装目录（含 backend/frontend/...）")
    ap.add_argument("--workspace", default="", help="共享工作区目录（默认 <app>/runtime/workspace）")
    args = ap.parse_args()

    app = os.path.abspath(args.app_dir)
    global LOGF, HERE_BACKEND
    HERE_BACKEND = os.path.join(app, "backend")
    LOGF = os.path.join(app, "runtime", "logs", "install.log")
    log("=== init_state 开始（app=%s）===" % app)
    log("[i] 环境探测：LOCALAPPDATA=%r USERPROFILE=%r" %
        (os.environ.get("LOCALAPPDATA", ""), os.environ.get("USERPROFILE", "")))
    fix_pth(app)
    # 端口：安装版用自己的一组（17887/17888/17990），避免和「开发目录里的副本」抢
    # （实测：开发副本占着 8788 时，安装版的守护进程会"已有守护进程在运行"直接退出、窗口不开）
    try:
        if HERE_BACKEND in sys.path:
            sys.path.remove(HERE_BACKEND)
        sys.path.insert(0, HERE_BACKEND)
        from panel_port import write_ports, read_ports
        wrote = write_ports({"port": 17887, "lock": 17888, "music": 17990})
        ports = read_ports()
        log("[%s] 面板端口：%s（port/lock/music）" %
            ("OK" if wrote else "i", "/".join(str(ports[k]) for k in ("port", "lock", "music"))))
    except Exception as exc:  # noqa: BLE001
        log("[!] 写端口配置失败（会退回默认 8787/8788/8790）：%r" % (exc,))
    runtime = os.path.join(app, "runtime")
    state = os.path.join(runtime, "state")
    for d in (runtime, state, os.path.join(runtime, "logs"), os.path.join(runtime, "cache")):
        os.makedirs(d, exist_ok=True)

    workspace = args.workspace or os.path.join(runtime, "workspace")
    os.makedirs(workspace, exist_ok=True)

    agents_file = os.path.join(state, "_acp_agents.json")
    engine_file = os.path.join(state, "_engine.json")

    node = os.path.join(app, "agents", "node", "node.exe")
    acp_js = os.path.join(app, "agents", "codex", "node_modules",
                          "@agentclientprotocol", "codex-acp", "dist", "index.js")
    codex_home = os.path.join(app, "agents", "codex", "codex-home")

    agents: list = []
    if os.path.isfile(node) and os.path.isfile(acp_js):
        # 首次安装：把"干净模板"复制成可写的 codex-home（用户密钥会写在这里之外的地方）
        tpl = os.path.join(app, "agents", "codex", "codex-home-template", "config.toml")
        cfg = os.path.join(codex_home, "config.toml")
        if os.path.isfile(tpl) and not os.path.isfile(cfg):
            os.makedirs(codex_home, exist_ok=True)
            with open(tpl, "r", encoding="utf-8") as fh:
                txt = fh.read()
            with open(cfg, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(txt)
        agents.append({
            "id": "codex-bundled",
            "label": "Codex（随包）",
            "builtin": True,
            "command": [node, acp_js],
            "cwd": "",
            "env": {"CODEX_HOME": codex_home},
            "note": "安装包自带的 codex-acp；需要自己的 API Key（首次设置里填，或用 DEEPSEEK_API_KEY 环境变量）",
            "provider": "deepseek",
        })

    # Claude：随包**适配器** + 自带 node。⚠ 不含 Claude Code 本体 —— 适配器依赖的
    # `@anthropic-ai/claude-agent-sdk` 自带 CLI，只要有登录态（~/.claude）或在
    # baseline.env 里配 ANTHROPIC_API_KEY 就能跑。
    claude_js = os.path.join(app, "agents", "claude", "node_modules",
                             "@zed-industries", "claude-code-acp", "dist", "index.js")
    if os.path.isfile(node) and os.path.isfile(claude_js):
        agents.append({
            "id": "claude-node",
            "label": "Claude Code（随包适配器）",
            "builtin": True,
            "command": [node, claude_js],
            "cwd": "",
            "env": {},
            "note": "安装包自带的 claude-code-acp；需要 Claude Code 的登录态，"
                    "或在首次设置里填 ANTHROPIC_API_KEY",
            "provider": "anthropic",
        })

    oc = find_opencode()
    if oc:
        agents.append({
            "id": "opencode-acp",
            "label": "OpenCode 客户端",
            "builtin": True,
            "command": oc,
            "cwd": "",
            "env": {},
            "note": "本机安装的 OpenCode 客户端自带的 ACP server",
            "provider": "",
            "mode": "",     # 不写死模式：默认跟随用户在面板里最后一次选择
        })
        log("[OK] 已加入 opencode-acp（%s）" % oc[0])
    else:
        # ⚠ 这一刻探测不到不代表没有：面板每次启动还会补一次（agents.ensure_opencode_agent）
        log("[i] 本次没探测到本机 OpenCode；面板启动时会再补一次，也可以在 agentlist 里手动加")

    # 随包 OpenCode（npm / Node 分发）—— 与上面的本机客户端是**两条**，互不覆盖。
    # 装上把 `agents\opencode\` 铺进去后这里就会命中；没铺就什么也不做。
    bo = find_bundled_opencode()
    if bo:
        agents.append({
            "id": "opencode-node",
            "label": "OpenCode（随包 Node 版）",
            "builtin": True,
            "command": bo,
            "cwd": "",
            "env": {},
            "note": "安装包自带的 OpenCode（npm / Node 分发）",
            "provider": "",
            "mode": "",     # 不写死模式：默认跟随用户在面板里最后一次选择
        })
        log("[OK] 已加入 opencode-node（%s）" % bo[0])

    if agents:
        reg = read_json(agents_file)
        have = {str(a.get("id")) for a in (reg.get("agents") or [])}
        merged = list(reg.get("agents") or [])
        for a in agents:
            if a["id"] not in have:
                merged.append(a)
        reg = {
            "active": reg.get("active") or agents[0]["id"],
            "baseline": {"cwd": workspace, "env": {}, "mode": "read-only"},
            "agents": merged,
        }
        write_json(agents_file, reg)
        log("[OK] agent 注册表已写入：%s（active=%s）" % (agents_file, reg["active"]))
    else:
        log("[!] 没找到随包 agent，也没找到本机 OpenCode：保持向导模式（首次启动会让你选/加 agent）")

    # 引擎默认值：**总是**写成 acp（面板独立运行）。用户在设置里随时能切回 opencode。
    eng = read_json(engine_file)
    if not eng.get("engine"):
        write_json(engine_file, {"engine": "acp"})
        log("[OK] 默认引擎 = acp（面板独立运行，不依赖 OpenCode）")
    else:
        log("[i] 保留已有引擎设置：%s" % eng.get("engine"))

    # 提示（不写密钥）
    log("[i] API Key 不随包：第一次打开面板时在「首次设置」里填，或设环境变量 DEEPSEEK_API_KEY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
