# -*- coding: utf-8 -*-
"""ACP agent 注册表（给面板的 agentlist 用）。

注册表：runtime/state/_acp_agents.json
    {"active": "<id>", "agents": [{"id","label","command","cwd","env","note"}, ...]}

兼容旧的单 agent 配置 runtime/state/_acp.json（{"command","cwd","env"}）：
没有注册表文件时，用它 bootstrap 出唯一一个 agent（id="dsh"），
并顺带补上 Codex 的两个候选：
  - codex-node   ：Node 适配器（用便携 Node 跑 npx @agentclientprotocol/codex-acp）
  - codex-client ：本机可执行文件（GitHub Releases 的 codex-acp.exe 绝对路径）

只做配置读写，不碰子进程。
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import time

from .. import STATE_DIR

# 项目根（安装包里 = <app>，里面有 `agents\node\`、`agents\codex\`、`agents\claude\`）。
# ⚠ 用 STATE_DIR 反推（= <root>\runtime\state），**别自己数 dirname 层数** ——
#   之前写成 dirname×3 算成了 `<app>\backend`（少一层），安装版因此把自带的 node/适配器判成"缺"。
ROOT_DIR = os.path.dirname(os.path.dirname(STATE_DIR))

AGENTS_FILE = os.path.join(STATE_DIR, "_acp_agents.json")
LEGACY_FILE = os.path.join(STATE_DIR, "_acp.json")


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _node_from(legacy: dict) -> str:
    """尽量拿到一个可用的 node.exe（优先旧命令里那个便携 Node）。"""
    cmd = legacy.get("command") or []
    if cmd and str(cmd[0]).lower().endswith("node.exe") and os.path.isfile(str(cmd[0])):
        return str(cmd[0])
    return shutil.which("node") or ""


def _codex_defaults(legacy: dict) -> list:
    node = _node_from(legacy)
    npx = ""
    if node:
        cand = os.path.join(os.path.dirname(node), "node_modules", "npm", "bin", "npx-cli.js")
        if os.path.isfile(cand):
            npx = cand
    node_cmd = ([node, npx, "-y", "@agentclientprotocol/codex-acp"]
                if (node and npx) else ["npx", "-y", "@agentclientprotocol/codex-acp"])
    cwd = legacy.get("cwd") or ""
    # ⚠ 中性化：这里**不塞** `OPENAI_API_KEY` 那种预设，也不假设"必须登录 OpenAI" ——
    #   Codex 可以指向任意 provider（本机就是直连 DeepSeek，见 CODEX_HOME/config.toml）。
    #   provider 留空，由 guess_provider() 从 env / command / config.toml 推断。
    return [
        {"id": "codex-node", "label": "Codex · Node 适配器", "builtin": True,
         "command": node_cmd, "cwd": cwd, "env": {},
         "note": "npm 适配器 @agentclientprotocol/codex-acp；provider 由它的 config.toml / env 推断"},
        {"id": "codex-client", "label": "Codex · 本机客户端", "builtin": True,
         "command": ["codex-acp"], "cwd": cwd, "env": {},
         "note": "指向本机 codex-acp 可执行文件（GitHub Releases 下载后填绝对路径），或你的 Codex 客户端命令"},
    ]


def _adapter_entry(pkg: str, prefer: str = "dist/index.js") -> str:
    """在常见 npm 安装位置里找某个包的**入口 JS**（优先 `prefer`，其次 package.json 的 bin）。

    用途：codex / claude 这些"适配器是 JS、需要 node 跑"的 agent 都要定位入口。
    """
    name = "\\" + pkg.replace("/", "\\").lower()
    for base in _search_dirs():
        if not base or not os.path.isdir(base):
            continue
        d0 = base.rstrip("\\/").count(os.sep)
        for root, subdirs, files in os.walk(base):
            if root.count(os.sep) - d0 > 6:
                subdirs[:] = []
                continue
            if not root.replace("/", "\\").lower().endswith(name):
                continue
            p = os.path.join(root, *prefer.split("/"))
            if os.path.isfile(p):
                return p
            pj = os.path.join(root, "package.json")     # 布局变了就照着 package.json 的 bin 找
            if os.path.isfile(pj):
                try:
                    meta = json.load(open(pj, encoding="utf-8"))
                    bins = meta.get("bin") or {}
                    if isinstance(bins, str):
                        bins = {str(meta.get("name") or "bin").split("/")[-1]: bins}
                    for rel in (bins.values() if isinstance(bins, dict) else []):
                        cand = os.path.join(root, str(rel))
                        if os.path.isfile(cand):
                            return cand
                except Exception:  # noqa: BLE001
                    pass
    return ""


def _claude_defaults(legacy: dict) -> list:
    """Claude Code 的两个候选 —— **与 `_codex_defaults()` 完全同构**。

    - `claude-node`：本地 npm 安装的 `@zed-industries/claude-code-acp` 适配器（用便携 node 跑）
    - `claude-client`：本机 `claude-code-acp` 命令（用户自己装在 PATH 上的）

    ⚠ 我们**不随包/不下载 Claude Code 本体**：适配器只是"ACP ↔ Claude Code"的翻译层，
      它调用的是**目标机器上已登录的 Claude Code**（也可以在 baseline.env 里配 `ANTHROPIC_API_KEY`）。
      所以"缺 Claude Code CLI"会像 codex 缺 Codex CLI 一样，明确显示在 `missing` 里。
    ⚠ 中性化：不写死 provider —— 由 `guess_provider()` 从 label/命令/env 推断（"claude" → anthropic）。
    """
    node = _node_from(legacy)
    npx = ""
    if node:
        cand = os.path.join(os.path.dirname(node), "node_modules", "npm", "bin", "npx-cli.js")
        if os.path.isfile(cand):
            npx = cand
    node_cmd = ([node, npx, "-y", "@zed-industries/claude-code-acp"]
                if (node and npx) else ["npx", "-y", "@zed-industries/claude-code-acp"])
    cwd = legacy.get("cwd") or ""
    return [
        {"id": "claude-node", "label": "Claude Code · Node 适配器", "builtin": True,
         "command": node_cmd, "cwd": cwd, "env": {},
         "note": "npm 适配器 @zed-industries/claude-code-acp；用本机 Claude Code 的登录"
                 "（也可在 baseline.env 配 ANTHROPIC_API_KEY）"},
        {"id": "claude-client", "label": "Claude Code · 本机客户端", "builtin": True,
         "command": ["claude-code-acp"], "cwd": cwd, "env": {},
         "note": "指向本机 claude-code-acp 可执行文件，或你的 Claude Code 客户端命令"},
    ]


def bootstrap() -> dict:
    """没有注册表时，从 _acp.json 生成一份（含 Codex / Claude 候选）并落盘。

    ⚠ 中性化：**不预设"默认用哪个"**。先自动补全命令，再在**真的可用**的 agent 里挑第一个
      当 active；一个都不可用就留空（`active_agent()` 会回落到列表第一个，但界面会显示"没选"）。
       以前这里直接 `active = agents[0]`，而 `agents[0]` 往往是旧 `_acp.json` 收进来的 dsh
       → 新装用户的默认 ACP agent 被绑死成 DeepSeek Harness，这不是中性。
    """
    legacy = _read_json(LEGACY_FILE)
    agents = []
    if legacy.get("command"):
        agents.append({
            "id": "dsh", "label": "DeepSeek Harness（dsh-acp）", "builtin": True,
            "command": legacy.get("command"), "cwd": legacy.get("cwd") or "",
            "env": legacy.get("env") or {}, "note": "来自旧的 _acp.json 配置",
        })
    agents.extend(_codex_defaults(legacy))
    agents.extend(_claude_defaults(legacy))
    reg = {"active": "", "agents": agents}
    save_registry(reg)
    try:
        reg = autofill(reg)["registry"]     # 首次就自动检测本机的 node / codex / dsh 位置
        for a in (reg.get("agents") or []):  # 顺手补 provider（能认出来就填）
            if not a.get("provider"):
                a["provider"] = guess_provider(a)
    except Exception:  # noqa: BLE001
        pass
    det = {}
    try:
        det = detect_all_cached()
    except Exception:  # noqa: BLE001
        pass
    pick = next((str(a.get("id")) for a in (reg.get("agents") or [])
                 if status_of(a, det)["available"]), "")
    reg["active"] = pick
    save_registry(reg)
    try:
        ensure_opencode_agent()        # 顺手补「子命令式 ACP」（本机装了 OpenCode 才有）
    except Exception:  # noqa: BLE001
        pass
    try:
        ensure_bundled_opencode_agent()   # 顺手补「随包 OpenCode（node 版）」（随包文件在才有）
    except Exception:  # noqa: BLE001
        pass
    return load_registry()             # 重新读，让调用方看到自愈后的结果


def load_registry() -> dict:
    reg = _read_json(AGENTS_FILE)
    if not (isinstance(reg.get("agents"), list) and reg.get("agents")):
        reg = bootstrap()
    return reg


def save_registry(reg: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    tmp = AGENTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False, indent=2)
    # ⚠ 先留一份上一版（`.bak`）：本文件被误写（我把假注册表喂给 autofill 过，实测覆盖过一次
    #   真实配置）时，至少还有一份能捞回来。只保留一个版本，够用又不占地方。
    try:
        if os.path.isfile(AGENTS_FILE):
            shutil.copy2(AGENTS_FILE, AGENTS_FILE + ".bak")
    except Exception:  # noqa: BLE001
        pass
    os.replace(tmp, AGENTS_FILE)


def find_agent(reg: dict, aid: str) -> dict:
    for a in (reg.get("agents") or []):
        if str(a.get("id")) == str(aid):
            return a
    return {}


def active_agent(reg: dict = None) -> dict:
    reg = reg or load_registry()
    a = find_agent(reg, reg.get("active"))
    if a:
        return a
    agents = reg.get("agents") or []
    return agents[0] if agents else {}


def baseline() -> dict:
    """所有 agent 共享的基线（cwd / env / mode）——避免每个 agent 各配一份。"""
    b = load_registry().get("baseline")
    return b if isinstance(b, dict) else {}


def _slug(label: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(label or "").lower()).strip("-")
    return (s[:24] or "agent")


# ---------------- 「子命令式」ACP（CLI 的 `acp` 子命令）----------------
#
# 有些 agent 的 ACP 不是单独的可执行文件，而是**主 CLI 的一个子命令**。
# 已知：OpenCode —— `opencode-cli.exe acp` 是一个完整的 ACP v1 server
#   （实测 2026-09-26：agentInfo={"name":"OpenCode","version":"2.0.13"}、
#    sessionCapabilities 含 list/resume/close/delete/fork、promptCapabilities 含 image，
#    能正常 `session/new` + `session/prompt`）。
# ⚠ 这类命令**文件名里没有 acp**、也不是 node 包，所以老的两条识别规则（`*acp*.exe` /
#   node 包 bin 名含 acp）永远扫不到 —— 这就是"OpenCode 明明在本机、ACP 却找不到它"的原因。
_SUBCOMMAND_ACP = [
    {"id": "opencode-acp", "label": "OpenCode 客户端",
     "exe_names": ("opencode-cli.exe", "opencode.exe", "opencode"),
     "args": ["acp"],
     # ⚠ 这里**不再写死 mode**：默认模式跟随"用户最后一次在面板里选过的模式"
     #   （见 service.remember_mode / _acp_mode.json）；没选过就用 agent 自己的默认。
     "note": "本机安装的 OpenCode 客户端（桌面端自带 CLI 的 `opencode-cli.exe acp`）："
             "模型清单很全，支持 session list/resume/close/delete/fork"},
]


def ensure_opencode_agent() -> str:
    """注册表里还没有「子命令式 ACP」（如 opencode-acp）就补一个；返回新增的 id，没动就回 ""。

    为什么要有这个"自愈"：`packaging/init_state.py` 只在**安装那一刻**探测一次 —— 而那一刻
    可能正好摸不到（环境变量不全 / `_walk_find` 撞上文件数上限 / 链接还没就绪 …… 本机实测
    就漏过：装完只有 codex-bundled + claude-node，OpenCode 明明在却不在 agentlist 里）。
    放到**每次面板启动**再补一次，就不再依赖那一次运气。
    """
    try:
        cmd = find_subcommand_acp()
    except Exception:  # noqa: BLE001
        return ""
    if not cmd:
        return ""
    spec = _spec_for_command(cmd) or {}
    reg = load_registry()
    key = " ".join(str(x) for x in cmd).lower().strip()
    legacy_label = "OpenCode · ACP（子命令）"
    for a in (reg.get("agents") or []):
        same = " ".join(str(x) for x in (a.get("command") or [])).lower().strip() == key
        if same or (spec and str(a.get("id")) == str(spec.get("id"))):
            # 已有它 → 什么都不加。⚠ 但顺手把**旧名字**迁移成新名字（老注册表里叫
            #   「OpenCode · ACP（子命令）」，现在统一叫「OpenCode 客户端」）。
            if spec and str(a.get("label") or "") == legacy_label:
                a["label"] = str(spec.get("label") or "OpenCode 客户端")
                save_registry(reg)
            return ""
    # ⚠ 用规格里的**显式 id**（`opencode-acp`），不能靠 add_agent 从 label 生成 slug ——
    #   改名成「OpenCode 客户端」后 slug 会变（`opencode`），id 一飘，老注册表就对不上了。
    aid = str(spec.get("id") or "opencode-acp")
    entry = {
        "id": aid,
        "label": str(spec.get("label") or "OpenCode 客户端"),
        "builtin": True,                               # 内置的不给删
        "command": list(cmd),
        "cwd": "",
        "env": {},
        "note": str(spec.get("note") or "OpenCode 客户端自带 ACP server"),
        "mode": str(spec.get("mode") or ""),
    }
    entry["provider"] = guess_provider(entry)
    reg.setdefault("agents", []).append(entry)
    save_registry(reg)
    return aid


def find_subcommand_acp(dirs=None, limit: int = 6) -> list:
    """在给定目录（默认 `_search_dirs()` + PATH）里找"带 acp 子命令"的 CLI。

    返回启动命令 `[<exe 真实路径>, "acp"]`；找不到回 `[]`。
    ⚠ 路径一律走 `os.path.realpath`：本机 OpenCode 在 D:，而 C: 那个
      `@opencodedesktop` 是指向它的 junction —— 不归一化就会"同一个 exe 扫出两条"。
    """
    bases = list(dirs) if dirs else (list(_search_dirs())
                                     + [os.path.dirname(shutil.which("opencode") or "")])
    for spec in _SUBCOMMAND_ACP:
        for name in spec["exe_names"]:
            p = shutil.which(name) or ""
            if not p:
                # ⚠ 跳过**随包**的那份（`<app>\agents\opencode`）：它由 find_bundled_opencode()
                #   单独登记成 `opencode-node`，不能被当成"本机客户端"，否则两者会互相抢占。
                p = _walk_find([b for b in bases if b], (name,), max_depth=4,
                               skip_roots=[BUNDLED_OPENCODE_DIR])
            if p and os.path.isfile(p) and not _is_bundled_opencode_path(p):
                return [os.path.realpath(p)] + list(spec["args"])
    return []


def _spec_for_command(cmd):
    """命令是不是某个已知「子命令式 ACP」规格（按 exe 名 + 子命令比对）。"""
    if not cmd:
        return None
    exe = os.path.basename(str(cmd[0])).lower()
    tail = [str(x).lower() for x in (cmd[1:] or [])]
    # ⚠ 先判随包那份：它也是「CLI + acp 子命令」的形态，但它是**另一个** agent，
    #   不能被当成"本机客户端"（否则两者会互相抢占）。
    if tail == list(_BUNDLED_OPENCODE["args"]) and _is_bundled_opencode_path(cmd[0]):
        return _BUNDLED_OPENCODE
    for spec in _SUBCOMMAND_ACP:
        if exe in tuple(str(n).lower() for n in spec["exe_names"]) and tail == list(spec["args"]):
            return spec
    return None


# ---------------- 随包 OpenCode（Node / npm 分发）----------------
#
# 与上面的 `_SUBCOMMAND_ACP`（**本机客户端**自带 `acp` 子命令）**故意分开**：
#   这里指安装包**自带**的那份 OpenCode（npm 包 `opencode-ai` 解到 `<app>\agents\opencode\`），
#   两者是不同的 agent（id / label 都不同），互不覆盖。
#
# 布局允许几种常见形态，按顺序取第一个存在的（打包时按实际摆放对齐即可）：
#   agents\opencode\opencode.exe
#   agents\opencode\bin\opencode.exe
#   agents\opencode\node_modules\opencode-ai\bin\opencode.exe
#   agents\opencode\node_modules\.bin\opencode.exe
#   若入口是 JS 脚本 → 用随包 node 跑：agents\node\node.exe <js> acp
_BUNDLED_OPENCODE = {
    "id": "opencode-node",
    "label": "OpenCode（随包 Node 版）",
    "args": ["acp"],
    "mode": "plan",                 # OpenCode 的只读模式（与客户端那条同一个语义）
    "note": "安装包自带的 OpenCode（npm / Node 分发）：模型清单很全，"
            "支持 session list/resume/close/delete/fork",
}

BUNDLED_OPENCODE_DIR = os.path.join(ROOT_DIR, "agents", "opencode")


def _is_bundled_opencode_path(p) -> bool:
    """这个路径是不是「随包 OpenCode」目录里的（用来把它和"本机客户端"区分开）。"""
    try:
        root = os.path.realpath(BUNDLED_OPENCODE_DIR)
        q = os.path.realpath(str(p))
        return q == root or q.startswith(root + os.sep)
    except Exception:  # noqa: BLE001
        return False


def find_bundled_opencode() -> list:
    """随包 OpenCode 的启动命令 `[<exe>, "acp"]`；没随包 / 没装就回 `[]`。"""
    root = BUNDLED_OPENCODE_DIR
    if not os.path.isdir(root):
        return []
    exe_cands = [
        os.path.join(root, "opencode.exe"),
        os.path.join(root, "bin", "opencode.exe"),
        os.path.join(root, "node_modules", "opencode-ai", "bin", "opencode.exe"),
        os.path.join(root, "node_modules", ".bin", "opencode.exe"),
    ]
    for p in exe_cands:
        if os.path.isfile(p):
            return [os.path.realpath(p), "acp"]
    # 入口是 JS 脚本的形态：用随包 node 跑
    node = _glob_node_exe()
    js_cands = [
        os.path.join(root, "node_modules", "opencode-ai", "bin", "opencode"),
        os.path.join(root, "node_modules", "opencode-ai", "bin", "opencode.js"),
        os.path.join(root, "opencode.js"),
    ]
    for p in js_cands:
        if os.path.isfile(p) and node:
            return [node, os.path.realpath(p), "acp"]
    return []


def ensure_bundled_opencode_agent() -> str:
    """注册表里还没有「随包 OpenCode」就补一个；返回新增的 id，没动就回 ""。

    与 `ensure_opencode_agent()` 同款自愈：随包文件在就登记，不在就什么都不做。
    """
    try:
        cmd = find_bundled_opencode()
    except Exception:  # noqa: BLE001
        return ""
    if not cmd:
        return ""
    reg = load_registry()
    key = " ".join(str(x) for x in cmd).lower().strip()
    for a in (reg.get("agents") or []):
        same = " ".join(str(x) for x in (a.get("command") or [])).lower().strip() == key
        if same or str(a.get("id")) == str(_BUNDLED_OPENCODE["id"]):
            return ""                              # 已经有它了
    entry = {
        "id": _BUNDLED_OPENCODE["id"],
        "label": _BUNDLED_OPENCODE["label"],
        "builtin": True,
        "command": list(cmd),
        "cwd": "",
        "env": {},
        "note": _BUNDLED_OPENCODE["note"],
        "mode": _BUNDLED_OPENCODE["mode"],
    }
    entry["provider"] = guess_provider(entry)
    reg.setdefault("agents", []).append(entry)
    save_registry(reg)
    return str(entry["id"])


# ---------------- provider 推断（只用于模型图标 / 分组，认不出就留空）----------------
#
# 中性化的要求是「没有 provider 也能跑」：模型清单照常返回、图标回落到字母徽章。
# 但能认出来时填上更好（品牌图标 + 分组名）。识别依据按可靠性排序：
#   ① 显式配置的 provider  → ② agent 自己的 env / command 里的主机名或 API key 名
#   → ③ Codex 的 `CODEX_HOME/config.toml` 里的 model_providers（base_url / env_key）
# ⚠ 只扫 **agent 自己的 env**，不扫共享基线（否则所有 agent 都会被同一个 key 带偏）。
_PROVIDER_HINTS = [
    ("deepseek", ("deepseek", "DEEPSEEK_API_KEY")),
    ("openai", ("api.openai.com", "OPENAI_API_KEY", "openai")),
    ("anthropic", ("anthropic", "claude", "ANTHROPIC_API_KEY")),
    ("google", ("generativelanguage", "GEMINI_API_KEY", "GOOGLE_API_KEY")),
    ("xai", ("api.x.ai", "XAI_API_KEY", "grok")),
    ("moonshotai", ("moonshot", "MOONSHOT_API_KEY", "kimi")),
    ("zhipuai", ("bigmodel.cn", "zhipu", "ZHIPUAI_API_KEY")),
    ("alibaba", ("dashscope", "DASHSCOPE_API_KEY")),
    ("mistral", ("mistral", "MISTRAL_API_KEY")),
    ("groq", ("api.groq.com", "GROQ_API_KEY")),
]


def _kw_hit(text: str, kw: str) -> bool:
    """关键词命中（大小写不敏感，但必须是**整词**：前后不能是字母/数字）。

    ⚠ 为什么要整词：本机便携 node 的路径里有 `D:\\agentlist\\deepseekharness\\...`，
      子串匹配会把"路径里带 deepseek 目录名"的 agent 一律判成 deepseek（实测踩到：
      `claude-node` 被 autofill 换成便携 node 的绝对路径后，provider 变成了 deepseek）。
    """
    low, k = str(text or "").lower(), str(kw or "").lower()
    if not k:
        return False
    i = low.find(k)
    while i >= 0:
        before = low[i - 1] if i > 0 else ""
        after = low[i + len(k)] if (i + len(k)) < len(low) else ""
        if not before.isalnum() and not after.isalnum():
            return True
        i = low.find(k, i + 1)
    return False


def provider_from_text(blob: str) -> str:
    """从任意文本里认 provider（主机名 / API key 名 / 品牌词）。认不出回空字符串。"""
    low = str(blob or "")
    if not low.strip():
        return ""
    for pid, keys in _PROVIDER_HINTS:
        if any(_kw_hit(low, k) for k in keys):
            return pid
    return ""


def _provider_from_codex_home(codex_home: str) -> str:
    """读 Codex 的 `config.toml`（base_url / env_key）猜 provider。"""
    try:
        p = os.path.join(str(codex_home), "config.toml")
        if not os.path.isfile(p):
            return ""
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            return provider_from_text(fh.read())
    except Exception:  # noqa: BLE001
        return ""


def guess_provider(a: dict) -> str:
    """猜这个 agent 背后是哪家模型。**已配的 provider 优先**；认不出就回空（中性）。

    ⚠ 只看**有值**的 env（`OPENAI_API_KEY: ""` 这种占位键不算依据）——否则一个空的
      OPENAI_API_KEY 就会把任意 agent 猜成 openai。
    """
    if not isinstance(a, dict):
        return ""
    if str(a.get("provider") or "").strip():
        return str(a["provider"]).strip()
    env = a.get("env") if isinstance(a.get("env"), dict) else {}
    # 键名与值都只在「有值」时才参与判断
    env_bits = []
    for k, v in env.items():
        if str(v or "").strip():
            env_bits.append(str(k))
            env_bits.append(str(v))
    # ⚠ 命令只看**文件名**：绝对路径里的目录名（如 …\deepseekharness\…）不该当 provider 线索
    cmd_bits = []
    for x in (a.get("command") or []):
        sx = str(x)
        cmd_bits.append(os.path.basename(sx) if (os.sep in sx or "/" in sx) else sx)
    blob = " ".join([
        " ".join(cmd_bits),
        " ".join(env_bits),
        str(a.get("id") or ""), str(a.get("label") or ""), str(a.get("note") or ""),
    ])
    hit = provider_from_text(blob)
    if hit:
        return hit
    for key in ("CODEX_HOME", "codex_home"):
        v = env.get(key)
        if v and str(v).strip():
            hit = _provider_from_codex_home(str(v))
            if hit:
                return hit
    return ""


def _node_exe() -> str:
    """找一个可用的 node.exe（优先注册表里便携 Node 那条）。"""
    for a in (load_registry().get("agents") or []):
        cmd = a.get("command") or []
        if cmd and str(cmd[0]).lower().endswith("node.exe") and os.path.isfile(str(cmd[0])):
            return str(cmd[0])
    return shutil.which("node") or ""


def infer_from_dir(d: str) -> tuple:
    """从一个目录推断怎么启动其中的 ACP agent。返回 (command|None, note)。

    认得三种常见形态：
      - node 包：`node_modules/.../package.json` 的 bin 里有含 "acp" 的入口（如 @agentclientprotocol/codex-acp）
      - 可执行文件：目录（或其一层子目录）里的 `*acp*.exe`
      - **子命令式**：目录里是已知"自带 acp 子命令"的 CLI（如 `opencode-cli.exe acp`）
        ⚠ 这种文件名里没有 "acp"、也不是 node 包，只靠前两条永远扫不到（2026-09-26 用户问到才补）
    """
    d = os.path.normpath(str(d or ""))
    if not d or not os.path.isdir(d):
        return None, ""
    node = _node_exe()
    # ⓪ 随包 OpenCode 目录：单独识别（和"本机客户端"区分）
    if _is_bundled_opencode_path(d):
        b = find_bundled_opencode()
        if b:
            return b, "随包 OpenCode（node 版）"
    # ③ 已知的子命令式 ACP（先判，免得被下面的 node 包规则盖掉）
    hit = find_subcommand_acp([d])
    if hit:
        return hit, "子命令式 ACP：%s" % os.path.basename(str(hit[0]))
    nm = os.path.join(d, "node_modules")
    if os.path.isdir(nm):
        for scope in sorted(os.listdir(nm)):
            sp = os.path.join(nm, scope)
            if not os.path.isdir(sp):
                continue
            subs = ([os.path.join(scope, x) for x in sorted(os.listdir(sp))]
                    if scope.startswith("@") else [scope])
            for rel in subs:
                pj = os.path.join(nm, rel, "package.json")
                if not os.path.isfile(pj):
                    continue
                try:
                    meta = json.load(open(pj, encoding="utf-8"))
                except Exception:  # noqa: BLE001
                    continue
                name = str(meta.get("name") or "")
                bins = meta.get("bin") or {}
                if isinstance(bins, str):
                    bins = {name.split("/")[-1] or "bin": bins}
                for bname, brel in bins.items():
                    blob = ("%s %s" % (bname, brel)).lower()
                    if "acp" not in blob and "acp" not in name.lower():
                        continue
                    entry = os.path.normpath(os.path.join(nm, rel, str(brel)))
                    if not os.path.isfile(entry):
                        continue
                    label = name or bname
                    if node:
                        return [node, entry], "node 包：%s" % label
                    return [entry], "node 包（未找到 node，可能要靠 PATH）：%s" % label
    for base in [d] + [os.path.join(d, x) for x in sorted(os.listdir(d))
                       if os.path.isdir(os.path.join(d, x))]:
        try:
            for f in sorted(os.listdir(base)):
                if f.lower().endswith(".exe") and "acp" in f.lower():
                    return [os.path.join(base, f)], "可执行文件：%s" % f
        except OSError:
            continue
    return None, ""


def scan_candidates(limit: int = 12) -> list:
    """扫描本机可能装有 ACP agent 的地方，列出**还没登记**的候选（给「新增 agent」选）。"""
    reg = load_registry()
    have_ids = {str(a.get("id")) for a in (reg.get("agents") or [])}
    have_cmds = {" ".join(str(x) for x in (a.get("command") or [])).lower().strip()
                 for a in (reg.get("agents") or []) if a.get("command")}
    out = []
    seen = set()

    def push(label, cmd, where, note):
        if not cmd:
            return
        # ⚠ 去重键用「真实路径」：本机 C: 的 @opencodedesktop 是 D: 的 junction，
        #    不归一化的话同一个 exe 会被当成两条候选（路径文本不同）。
        norm = []
        for x in cmd:
            sx = str(x)
            if os.path.isabs(sx) or ("\\" in sx) or ("/" in sx):
                norm.append(os.path.realpath(sx).lower())
            else:
                norm.append(sx.lower())
        key = " ".join(norm).strip()
        if key in have_cmds or key in seen:
            return
        seen.add(key)
        cid = _slug(label) or "agent"
        n = 2
        while cid in have_ids or any(x["id"] == cid for x in out):
            cid = "%s-%d" % (_slug(label), n)
            n += 1
        st = status_of({"id": cid, "command": cmd})
        out.append({"id": cid, "label": label, "command": cmd, "cwd": "",
                    "dir": where, "note": note, "available": st["available"],
                    "missing": st["missing"],
                    "provider": guess_provider({"id": cid, "label": label, "command": cmd,
                                                "note": note, "env": {}})})

    # ① PATH 上常见的 ACP 适配器
    for name in ("codex-acp", "claude-code-acp", "gemini", "goose", "crush", "amp-acp", "iflow"):
        w = shutil.which(name)
        if w:
            push(name, [w], os.path.dirname(w), "PATH 上找到")
    # ② 同盘 agentlist\* 目录（我们放 agent 的地方）
    base = os.path.join(local_root(), "agentlist")
    if os.path.isdir(base):
        for d in sorted(os.listdir(base)):
            p = os.path.join(base, d)
            if not os.path.isdir(p):
                continue
            cmd, note = infer_from_dir(p)
            if cmd:
                spec = _spec_for_command(cmd)
                push((spec or {}).get("label") or d, cmd, p, (spec or {}).get("note") or note)
    # ③ 全局 npm bin 里的 *acp* 命令（npx/npm -g 装的适配器）
    for nmdir in (os.path.join(os.environ.get("APPDATA", ""), "npm"),
                  os.path.join(os.path.expanduser("~"), ".local", "bin")):
        if not os.path.isdir(nmdir):
            continue
        for f in sorted(os.listdir(nmdir)):
            low = f.lower()
            if "acp" in low and low.endswith((".exe", ".cmd")) and not low.endswith(".ps1"):
                push(f, [os.path.join(nmdir, f)], nmdir, "全局命令")
    # ④ 子命令式 ACP：CLI 的 `acp` 子命令（如 `opencode-cli.exe acp`）
    #    ⚠ 这条是为 OpenCode 补的：它自带完整 ACP server，但文件名里没有 "acp"、
    #      也不是 node 包，前三条规则永远扫不到。
    for spec in _SUBCOMMAND_ACP:
        cmd = find_subcommand_acp()
        if cmd:
            push(spec["label"], cmd, os.path.dirname(str(cmd[0])), spec["note"])
    # ⑤ Claude Code（与 codex 同构：本地 npm 适配器 + 本机客户端）
    det = detect_all_cached()
    if det.get("node") and det.get("claudeAdapter"):
        push("Claude Code · Node 适配器", [det["node"], det["claudeAdapter"]],
             os.path.dirname(det["claudeAdapter"]),
             "本地 npm 安装的 @zed-industries/claude-code-acp")
    if det.get("claudeAcpExe"):
        push("Claude Code · 本机客户端", [det["claudeAcpExe"]],
             os.path.dirname(det["claudeAcpExe"]), "PATH 上找到")
    return out[:limit]


# ---------------- 扫描本机有哪些 agent（「扫描本机」按钮用）----------------
#
# 与 scan_candidates 的区别：这不是"只列没登记的候选"，而是一份**本机发现报告** ——
# 找到什么、在哪、能不能直接当 ACP agent 用、是不是已经登记过；也把"只装了客户端但缺适配器"点出来。
# 扫描位置比原来更全：PATH / npm / agentlist / 子命令式 / 本地适配器，外加
# `%LOCALAPPDATA%\Programs`、`%ProgramFiles%\<agent>`、`~/.codex\bin`、`~/.claude\bin`、随包 `agents\`。

_SCAN_KIND = {
    "codex": ("Codex CLI（客户端本体，需 ACP 适配器）", "client"),
    "codex-acp": ("Codex ACP 适配器", "adapter"),
    "claude": ("Claude Code（客户端本体，需 ACP 适配器）", "client"),
    "claude-code-acp": ("Claude Code ACP 适配器", "adapter"),
    "opencode": ("OpenCode（自带 acp 子命令）", "subcommand"),
    "opencode-cli": ("OpenCode CLI（自带 acp 子命令）", "subcommand"),
    "dsh": ("DeepSeek Harness（客户端）", "client"),
    "dsh-acp": ("DeepSeek Harness ACP", "adapter"),
    "gemini": ("Gemini CLI", "client"),
    "goose": ("Goose", "client"),
    "crush": ("Crush", "client"),
    "amp-acp": ("Amp ACP", "adapter"),
    "iflow": ("iFlow", "client"),
    "aider": ("Aider", "client"),
}
# 目录名里有这些词才值得进去推断（避免在 Program Files 里把每个目录都翻一遍）
_AGENTISH_DIR = ("codex", "claude", "opencode", "dsh", "acp", "goose", "crush", "gemini")
# 明确不探的目录（用户要求：不碰 VS Code 的缓存）
_SCAN_SKIP_DIR = ("visual studio code", "vscode", "microsoft vs code", "cursor", "windsurf")


def _cmd_key(cmd) -> str:
    """命令去重键：绝对路径按 realpath 归一化（junction 不会出两条）。"""
    norm = []
    for x in (cmd or []):
        sx = str(x)
        if os.path.isabs(sx) or ("\\" in sx) or ("/" in sx):
            norm.append(os.path.realpath(sx).lower())
        else:
            norm.append(sx.lower())
    return " ".join(norm).strip()


def _scan_roots() -> list:
    """扫描本机该去哪些目录找 agent。"""
    home = os.path.expanduser("~")
    la = os.environ.get("LOCALAPPDATA", "")
    ad = os.environ.get("APPDATA", "")
    pf = os.environ.get("ProgramFiles", "")
    pf86 = os.environ.get("ProgramFiles(x86)", "")
    cands = [
        os.path.join(ad, "npm"),
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".codex", "bin"),
        os.path.join(home, ".claude", "bin"),
        os.path.join(la, "Programs"),
        os.path.join(local_root(), "agentlist"),
        os.path.join(ROOT_DIR, "agents"),
        pf, pf86,
    ]
    out = []
    for d in cands:
        if d and os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def deep_scan(limit: int = 80, budget: float = 20.0) -> dict:
    """扫描本机有哪些 agent，返回「发现报告」（同步；带 20s 时间预算，别把请求拖死）。

    返回 `{found:[...], summary:{...}, hints:[...], detected:{...}}`。
    `found` 每一项：id/label/command/dir/source/kind/note/available/missing/registered/provider。
    `kind`：adapter（可直接当 ACP agent）/ subcommand / client（只是客户端，需适配器）/ registered。
    """
    deadline = time.time() + max(3.0, budget)
    det = detect_all_cached()          # 用 15s 缓存，避免和 scan_candidates 各扫一遍（实测能省一半时间）
    reg = load_registry()
    reg_key = {}
    for a in (reg.get("agents") or []):
        if a.get("command"):
            reg_key[_cmd_key(a["command"])] = str(a.get("id"))
    found = []
    seen = set()

    def push(label, cmd, where, source, kind, note=""):
        if not cmd:
            return
        key = _cmd_key(cmd)
        if not key or key in seen:
            return
        seen.add(key)
        st = status_of({"id": _slug(label), "command": cmd})
        found.append({
            "id": _slug(label), "label": label, "command": [str(x) for x in cmd],
            "dir": where or "", "source": source, "kind": kind, "note": note,
            "available": st["available"], "missing": st["missing"],
            "registered": key in reg_key, "registeredAs": reg_key.get(key, ""),
            "provider": guess_provider({"id": _slug(label), "label": label,
                                        "command": cmd, "note": note, "env": {}}),
        })

    # ① 现有候选扫描（PATH / agentlist / npm / 子命令式 / 本地 Claude 适配器）
    try:
        for c in scan_candidates(limit=40):
            spec = _spec_for_command(c.get("command"))
            push(c["label"], c.get("command"), c.get("dir"),
                 "自动发现", "subcommand" if spec else "adapter", c.get("note") or "")
    except Exception:  # noqa: BLE001
        pass

    # ② 更广的目录：已知 CLI 名 + *acp*.exe + agent 目录里的 node 包
    for root in _scan_roots():
        if time.time() > deadline:
            break
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        rbase = os.path.basename(root.rstrip("\\/")).lower()
        for n in names:
            if time.time() > deadline:
                break
            low = n.lower()
            if any(k in low for k in _SCAN_SKIP_DIR):
                continue
            p = os.path.join(root, n)
            stem = os.path.splitext(low)[0]
            if os.path.isfile(p):
                if low.endswith((".exe", ".cmd", ".bat")):
                    if stem in _SCAN_KIND:
                        lab, kind = _SCAN_KIND[stem]
                        push(lab, [p], root, "在 %s 找到" % root, kind)
                    elif "acp" in low and low.endswith((".exe", ".cmd")):
                        push(n, [p], root, "在 %s 找到" % root, "adapter")
            elif os.path.isdir(p):
                # ⚠ 只对"名字像 agent"的目录做推断：Programs 下什么都试一遍会沿 junction
                #   走进 OpenCode 之类的大目录，扫描要十几秒（实测 2.7s → 19s）。
                if rbase not in ("agentlist", "agents") \
                        and not any(k in low for k in _AGENTISH_DIR):
                    continue
                try:
                    cmd, note = infer_from_dir(p)
                except Exception:  # noqa: BLE001
                    cmd, note = None, ""
                if cmd:
                    spec = _spec_for_command(cmd)
                    if spec:
                        push(spec["label"], cmd, p, "在 %s 找到" % root, "subcommand",
                             spec.get("note") or note)
                    else:
                        push(n, cmd, p, "在 %s 找到" % root, "adapter", note)

    # ③ 本机检测到的适配器 / 包裹
    if det.get("node") and det.get("codexAdapter"):
        push("Codex · 本地 ACP 适配器", [det["node"], det["codexAdapter"]],
             os.path.dirname(det["codexAdapter"]), "本机 npm", "adapter")
    if det.get("node") and det.get("claudeAdapter"):
        push("Claude · 本地 ACP 适配器", [det["node"], det["claudeAdapter"]],
             os.path.dirname(det["claudeAdapter"]), "本机 npm", "adapter")
    if det.get("node") and det.get("dsh"):
        push("DeepSeek Harness", [det["node"], det["dsh"]],
             os.path.dirname(det["dsh"]), "本机 npm", "adapter")

    # ④ 已登记但上面没扫到的（保证报告里能看到全部）
    for a in (reg.get("agents") or []):
        cmd = a.get("command")
        if cmd and _cmd_key(cmd) not in seen:
            push(a.get("label") or a.get("id") or "agent", cmd, a.get("cwd") or "",
                 "已登记", "registered", a.get("note") or "")

    hints = []
    if det.get("codex") and not (det.get("codexAcp") or det.get("codexAcpExe") or det.get("codexAdapter")):
        hints.append("装了 Codex CLI，但没找到 ACP 适配器 —— 需要 @agentclientprotocol/codex-acp")
    if det.get("claude") and not (det.get("claudeAcp") or det.get("claudeAcpExe") or det.get("claudeAdapter")):
        hints.append("装了 Claude Code，但没找到 ACP 适配器 —— 需要 @zed-industries/claude-code-acp")
    usable = [f for f in found if f["available"] and f["kind"] != "client"]
    return {
        "found": found[:limit],
        "summary": {
            "total": len(found),
            "usable": len(usable),
            "newUsable": len([f for f in usable if not f["registered"]]),
            "registered": len([f for f in found if f["registered"]]),
            "clients": len([f for f in found if f["kind"] == "client"]),
        },
        "hints": hints,
        "detected": det,
    }


def add_agent(label: str, command, cwd: str = "", env: dict = None, note: str = "",
              provider: str = "") -> dict:
    """用户自己添加一个 agent（label + command 必填）。id 由 label 生成、重名自动加序号。"""
    cmd = [str(x) for x in (command or []) if str(x).strip()]
    if not cmd:
        return {}
    reg = load_registry()
    key = " ".join(cmd).lower().strip()
    for a in (reg.get("agents") or []):          # 已有同样命令的 agent → 直接复用，别加重复项
        if " ".join(str(x) for x in (a.get("command") or [])).lower().strip() == key:
            return a
    ids = {str(a.get("id")) for a in (reg.get("agents") or [])}
    aid = _slug(label)
    n = 2
    while aid in ids:
        aid = "%s-%d" % (_slug(label), n)
        n += 1
    a = {"id": aid, "label": str(label or aid), "command": cmd,
         "cwd": cwd or "", "env": env or {}, "note": note or "用户添加",
         "provider": str(provider or "")}
    if not a["provider"]:                      # 没手填 → 从命令/env 猜一个（猜不出就留空）
        a["provider"] = guess_provider(a)
    reg.setdefault("agents", []).append(a)
    save_registry(reg)
    return a


def remove_agent(aid: str) -> bool:
    """删掉一个 agent；**当前使用中的不许删**（否则引擎会指向不存在的配置）。"""
    reg = load_registry()
    agents = reg.get("agents") or []
    if str(reg.get("active")) == str(aid):
        return False
    left = [a for a in agents if str(a.get("id")) != str(aid)]
    if len(left) == len(agents) or not left:
        return False
    reg["agents"] = left
    save_registry(reg)
    return True


def set_active(aid: str) -> bool:
    reg = load_registry()
    if not find_agent(reg, aid):
        return False
    reg["active"] = str(aid)
    save_registry(reg)
    return True


def update_baseline_env(patch: dict) -> dict:
    """合并**共享基线**的环境变量（模型 API Key 这类"所有 agent 共用"的东西）。

    - `patch = {"DEEPSEEK_API_KEY": "sk-..."}` → 写入/覆盖；
    - 值为 `None` 或空字符串 → **删除**该变量；
    - 返回更新后的 baseline（`env` 里就是明文，调用方负责别回显）。
    这是安装包"填 Key"的落点：`runtime/state/_acp_agents.json` 的 `baseline.env`
    （只在**本机**，不进仓库、不进安装包）。
    """
    reg = load_registry()
    b = reg.get("baseline") if isinstance(reg.get("baseline"), dict) else {}
    env = dict(b.get("env") or {})
    for k, v in (patch or {}).items():
        k = str(k or "").strip()
        if not k:
            continue
        if v is None or str(v).strip() == "":
            env.pop(k, None)
        else:
            env[k] = str(v).strip()
    b["env"] = env
    reg["baseline"] = b
    save_registry(reg)
    return b


def mask_secret(value: str) -> str:
    """给界面看的"掩码"：只露头 4 位 + 长度，绝不出明文。"""
    s = str(value or "")
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return "%s…%s（%d 位）" % (s[:4], s[-2:], len(s))


def update_agent(aid: str, patch: dict) -> dict:
    reg = load_registry()
    a = find_agent(reg, aid)
    if not a:
        return {}
    for k in ("label", "command", "cwd", "env", "note", "provider", "mode"):
        if k in patch and patch[k] is not None:
            a[k] = patch[k]
    save_registry(reg)
    return a


def cmd_available(command) -> bool:
    """命令的第一个词是否真的存在（绝对路径看文件，否则查 PATH）。"""
    if not command:
        return False
    exe = str(command[0])
    if os.path.isabs(exe) or ("\\" in exe) or ("/" in exe):
        return os.path.isfile(exe)
    return shutil.which(exe) is not None


# ---------------- 自动检测（不用手填路径）----------------

_CODEX_NAMES = ("codex.exe", "codex.cmd", "codex.bat", "codex")
_CODEX_ACP_NAMES = ("codex-acp.exe", "codex-acp.cmd", "codex-acp")
_CODEX_ACP_EXE = ("codex-acp.exe",)
# Claude Code（与 codex 同构）：`claude` = 客户端本体；`claude-code-acp` = ACP 适配器
_CLAUDE_NAMES = ("claude.exe", "claude.cmd", "claude.bat", "claude")
_CLAUDE_ACP_NAMES = ("claude-code-acp.exe", "claude-code-acp.cmd", "claude-code-acp")
_CLAUDE_ACP_EXE = ("claude-code-acp.exe",)


def _search_dirs() -> list:
    home = os.path.expanduser("~")
    la = os.environ.get("LOCALAPPDATA", "")
    ad = os.environ.get("APPDATA", "")
    cands = [
        os.path.join(ad, "npm"),
        os.path.join(home, ".codex", "bin"),
        os.path.join(home, ".codex"),
        os.path.join(home, ".local", "bin"),
        os.path.join(la, "Programs"),
        os.path.join(la, "Microsoft", "WindowsApps"),
        os.path.join(local_root(), "agentlist"),
        # 安装包自带的 agent 目录（node 单独放前面：那个目录里 claude/codex 文件多，
        # 走 _walk_find 时可能在找到 node.exe 之前就撞上文件数上限）
        os.path.join(ROOT_DIR, "agents", "node"),
        os.path.join(ROOT_DIR, "agents"),
    ]
    return [d for d in cands if d]


def local_root() -> str:
    """本项目所在的盘根（例如 D:\\）——用来找同盘的 agentlist 等。"""
    root = os.path.splitdrive(os.path.abspath(STATE_DIR))[0] + os.sep
    return root


def _walk_find(dirs, names, max_depth=3, limit=4000, skip_roots=()) -> str:
    """在若干目录里（限定深度）找匹配某个文件名的可执行文件。

    `skip_roots`：整个跳过的目录（连子孙一起）——用来把**随包**的那份 OpenCode 从
    「本机客户端」的探测里排除掉，免得两者互相抢占。
    """
    want = {n.lower() for n in names}
    skips = [os.path.realpath(s).lower().rstrip("\\/") for s in (skip_roots or []) if s]
    seen = 0
    for base in dirs:
        if not base or not os.path.isdir(base):
            continue
        d0 = base.rstrip("\\/").count(os.sep)
        for root, subdirs, files in os.walk(base):
            if skips:
                rl = os.path.realpath(root).lower().rstrip("\\/")
                if any(rl == s or rl.startswith(s + os.sep) for s in skips):
                    subdirs[:] = []
                    continue
            if root.count(os.sep) - d0 >= max_depth:
                subdirs[:] = []
            for f in files:
                seen += 1
                if seen > limit:
                    return ""
                if f.lower() in want:
                    return os.path.join(root, f)
    return ""


def _has_claude_auth(det: dict = None) -> bool:
    """本机有没有 Claude 的登录态（`~/.claude` / `~/.claude.json`）或 API Key 线索。

    ⚠ 只判断"能不能跑起来"的前置之一；真正的 auth 在运行时（适配器用 SDK 自带的 CLI + 登录态）。
    """
    if det and (det.get("claude") or det.get("claudeAcp")):
        return True
    home = os.path.expanduser("~")
    if os.path.isdir(os.path.join(home, ".claude")) or os.path.isfile(os.path.join(home, ".claude.json")):
        return True
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _glob_node_exe() -> str:
    """定点找便携 node（`_walk_find` 会被 D:\\agentlist 这种大目录吃光文件上限，实测踩到）。

    试的顺序：同盘 `agentlist\\*\\node\\*\\node.exe`（本机 deepseekharness 就是这种）→
    同盘 `agentlist\\*\\node.exe` → 系统安装位置。
    """
    root = local_root()
    pats = [
        os.path.join(ROOT_DIR, "agents", "node", "node.exe"),      # 安装包自带的那份（最确定）
        os.path.join(root, "agentlist", "*", "node", "*", "node.exe"),
        os.path.join(root, "agentlist", "*", "*", "node", "*", "node.exe"),
        os.path.join(root, "agentlist", "*", "node.exe"),
        os.path.join(os.environ.get("ProgramFiles", ""), "nodejs", "node.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe"),
    ]
    for pat in pats:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[0]
    return ""


def _find_dsh_bin() -> str:
    legacy = _read_json(LEGACY_FILE)
    for a in (legacy.get("command") or []):
        if "dsh-acp" in str(a) and os.path.isfile(str(a)):
            return str(a)
    for base in (_search_dirs() + [os.path.join(os.environ.get("APPDATA", ""), "npm")]):
        if not base or not os.path.isdir(base):
            continue
        d0 = base.rstrip("\\/").count(os.sep)
        for root, subdirs, files in os.walk(base):
            if root.count(os.sep) - d0 > 6:
                subdirs[:] = []
                continue
            if root.replace("/", "\\").lower().endswith("node_modules\\dsh-acp\\lib") and "bin.js" in files:
                return os.path.join(root, "bin.js")
    return ""


def detect_all() -> dict:
    """扫描本机，找出 node / npx / codex / codex-acp / claude / claude-code-acp / dsh-acp 的位置。"""
    node = shutil.which("node") or ""
    if not node:
        node = _walk_find([os.path.join(local_root(), "agentlist"),
                           os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")],
                          ("node.exe",), max_depth=4, limit=12000)
    if not node:
        node = _glob_node_exe()      # 定点找便携 node（见下：大目录会把 _walk_find 的文件上限吃光）
    npx = ""
    if node:
        cand = os.path.join(os.path.dirname(node), "node_modules", "npm", "bin", "npx-cli.js")
        if os.path.isfile(cand):
            npx = cand
    dirs = _search_dirs()
    return {
        "node": node, "npx": npx,
        "codex": shutil.which("codex") or _walk_find(dirs, _CODEX_NAMES),
        "codexAcp": shutil.which("codex-acp") or _walk_find(dirs, _CODEX_ACP_NAMES),
        "codexAcpExe": _walk_find(dirs, _CODEX_ACP_EXE),      # 只认真 exe（.cmd 需要 node 在 PATH）
        "codexAdapter": _find_codex_adapter(),                 # 本地 npm 安装的适配器入口
        "dsh": _find_dsh_bin(),
        # Claude Code：与 codex 完全同构（客户端本体 + 适配器 + 本地 npm 安装的适配器入口）
        "claude": shutil.which("claude") or _walk_find(dirs, _CLAUDE_NAMES),
        "claudeAcp": shutil.which("claude-code-acp") or _walk_find(dirs, _CLAUDE_ACP_NAMES),
        "claudeAcpExe": _walk_find(dirs, _CLAUDE_ACP_EXE),
        "claudeAdapter": _adapter_entry("@zed-industries/claude-code-acp"),
    }


def _find_codex_adapter() -> str:
    """找本地 npm 安装的 @agentclientprotocol/codex-acp 入口（dist/index.js）。"""
    return _adapter_entry("@agentclientprotocol/codex-acp")


def autofill(reg: dict = None, save: bool = True) -> dict:
    """补全「命令不可用」的 agent；另外把 codex-node 从 npx 形态升级成本地适配器。

    可用的一律不动（尊重用户手填），只有两种情况会改：
      ① 命令的第一个词找不到；② codex-node 还在用 npx 形态但本地已经装了适配器。
    另外**只补空着的 provider**（猜出来的品牌，用于模型图标）；已配的绝不覆盖。
    ⚠ `save=False`：只算不落盘（测试/探针传假注册表时必须用，否则会覆盖真实配置 —— 实测踩过）。
    """
    reg = reg or load_registry()
    det = detect_all()
    changed = []
    filled = []
    for a in (reg.get("agents") or []):
        aid = str(a.get("id"))
        cur = a.get("command") or []
        if aid == "codex-node" and det.get("node") and det.get("codexAdapter"):
            if (not cmd_available(cur)) or any("npx-cli.js" in str(x) for x in cur):
                a["command"] = [det["node"], det["codexAdapter"]]
                changed.append(aid)
        elif aid == "claude-node" and det.get("node") and det.get("claudeAdapter"):
            # 与 codex-node 同款：本地装了适配器就从 npx 形态升级成本地直跑
            if (not cmd_available(cur)) or any("npx-cli.js" in str(x) for x in cur):
                a["command"] = [det["node"], det["claudeAdapter"]]
                changed.append(aid)
        elif not cmd_available(cur):
            cmd = None
            if aid == "codex-node" and det["node"] and det["npx"]:
                cmd = [det["node"], det["npx"], "-y", "@agentclientprotocol/codex-acp"]
            elif aid == "codex-client" and det.get("codexAcpExe"):
                cmd = [det["codexAcpExe"]]
            elif aid == "claude-node" and det["node"] and det["npx"]:
                cmd = [det["node"], det["npx"], "-y", "@zed-industries/claude-code-acp"]
            elif aid == "claude-client" and det.get("claudeAcpExe"):
                cmd = [det["claudeAcpExe"]]
            elif aid == "dsh" and det["node"] and det["dsh"]:
                cmd = [det["node"], det["dsh"]]
            if cmd:
                a["command"] = cmd
                changed.append(aid)
        if not str(a.get("provider") or "").strip():
            g = guess_provider(a)
            if g:
                a["provider"] = g
                filled.append(aid)
    if (changed or filled) and save:
        # ⚠ `save=False` 是给"拿假注册表做实验"的调用方（测试 / 探针）用的：
        #   以前它们传进假 dict、这里照写不误 → 真实 _acp_agents.json 被覆盖（实测踩到）。
        save_registry(reg)
    return {"registry": reg, "detected": det, "changed": changed, "filledProviders": filled}


# ---------------- 「可用」到底怎么算（按类型看真依赖）----------------

_det_cache = {"t": 0.0, "val": None}


def detect_all_cached(max_age: float = 15.0) -> dict:
    """detect_all 会扫目录，做个 15s 缓存，别每次轮询都扫。"""
    now = time.time()
    if _det_cache["val"] and (now - _det_cache["t"]) < max_age:
        return _det_cache["val"]
    val = detect_all()
    _det_cache["t"] = now
    _det_cache["val"] = val
    return val


def _kind(a: dict) -> str:
    aid = str(a.get("id") or "")
    if aid.startswith("codex-node"):
        return "codex-node"
    if aid.startswith("codex-client"):
        return "codex-client"
    if aid.startswith("claude-node"):
        return "claude-node"
    if aid.startswith("claude-client"):
        return "claude-client"
    if aid.startswith("dsh"):
        return "dsh"
    return "custom"


def status_of(a: dict, det: dict = None) -> dict:
    """按 agent 类型判断是否真的能用；返回 {available, missing:[原因]}。

    ⚠ 只看 command[0] 存在是不够的：codex-node 的 command[0] 只是 node.exe，
    真正的依赖是 npm 适配器 + Codex CLI；codex-client 依赖 codex-acp / codex 可执行文件。
    """
    det = det or detect_all_cached()
    cmd = a.get("command") or []
    missing = []
    if not cmd:
        missing.append("命令未配置")
    elif not cmd_available(cmd):
        missing.append("命令找不到")
    kind = _kind(a)
    if kind == "codex-node":
        if not det.get("node"):
            missing.append("Node")
        if not det.get("codex"):
            missing.append("Codex CLI")
        if not (det.get("codexAdapter") or det.get("npx")):
            missing.append("适配器")
    elif kind == "codex-client":
        if not (det.get("codexAcp") or det.get("codex")):
            missing.append("codex-acp / Codex CLI")
    elif kind == "claude-node":
        # ⚠ 与 codex-node **不完全一样**：这个适配器依赖 `@anthropic-ai/claude-agent-sdk`，
        #   而 SDK **自带** Claude Code 的 CLI（`cli.js`）→ 目标机器**不需要**另装 Claude Code 本体。
        #   真正的前置是**登录态**（`~/.claude`）或 `ANTHROPIC_API_KEY`；两者都没有时给一条提示。
        if not det.get("node"):
            missing.append("Node")
        if not (det.get("claudeAdapter") or det.get("npx")):
            missing.append("适配器")
        if not det.get("claude") and not _has_claude_auth(det):
            missing.append("Claude Code 登录（或 ANTHROPIC_API_KEY）")
    elif kind == "claude-client":
        if not (det.get("claudeAcp") or det.get("claude")):
            missing.append("claude-code-acp / Claude Code CLI")
    elif kind == "dsh":
        if not det.get("dsh"):
            missing.append("dsh-acp")
    uniq = []
    for m in missing:
        if m not in uniq:
            uniq.append(m)
    return {"available": not uniq, "missing": uniq}


def public(a: dict, det: dict = None) -> dict:
    cmd = a.get("command") or []
    st = status_of(a, det)
    return {
        "id": a.get("id"), "label": a.get("label") or a.get("id"),
        "available": st["available"], "missing": st["missing"],
        "builtin": bool(a.get("builtin")),
        "note": a.get("note") or "", "command": cmd,
        "cwd": a.get("cwd") or "", "hasEnv": bool(a.get("env")),
        # provider 只用于模型图标 / 分组（中性化：没配就留空，前端走字母徽章）
        "provider": str(a.get("provider") or ""),
        # 审批模式（覆盖共享 baseline.mode；不同 agent 的模式名不一样，留空=用基线）
        "mode": str(a.get("mode") or ""),
        # 没配时给一个「猜出来的」建议（前端编辑框可一键填入；不写入注册表）
        "providerGuess": "" if str(a.get("provider") or "").strip() else guess_provider(a),
    }
