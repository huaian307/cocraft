# -*- coding: utf-8 -*-
r"""面板内「安装 Agent」—— 联网从 npm 装常见的 ACP 适配器（**不需要用户自己开终端**）。

为什么要有这一层（安装包的补充，不是替代）：
  * 安装包只能随包**固定几个** agent；用户想换 / 想补装别的，不该被迫重跑安装包；
  * 而"本机已装的客户端"已经有另一条路（设置 → ACP 代理 →「扫描本机 Agent」）。

做法（刻意保守）：
  1. **只用 npm**（`<node> <npm-cli.js> install --prefix <目录> <包名>`），**不下载、不执行任何自定义脚本**；
  2. 装到 `<ROOT>/agents/npm/<id>/`（**不碰随包的 `agents/codex|claude|opencode`**）；
  3. 装完**自己从 package.json 的 `bin` 里认出入口**（`.exe` 直接跑、`.js` 用 node 跑），
     再走 `agents.add_agent()` **注册**（和手动添加完全同一条路）；
  4. 只认**目录里预置的那几个包名**或合法的 npm 包名（`@scope/name` / `name`），
     **绝不拼 shell**（`subprocess` 传 list，不走 `shell=True`）；
  5. 网络优先直连 registry，失败**自动换国内镜像** `registry.npmmirror.com` 再试一次。

⚠ 一次只跑一个安装任务（`_JOB`），状态用 `status()` 轮询；日志只留最近 60 行。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

from engines.acp import agents

INSTALL_ROOT = os.path.join(agents.ROOT_DIR, "agents", "npm")
MIRROR = "https://registry.npmmirror.com"
NPM_TIMEOUT = 900.0                      # 最后一次 npm 可能很大（codex 400 MB+）

# 目录：**只放"确认过能装、装完能跑"的**。id 也是安装目录名。
CATALOG = [
    {"id": "codex-acp", "label": "Codex（ACP 适配器）",
     "pkg": "@agentclientprotocol/codex-acp", "provider": "deepseek",
     "note": "自带 codex CLI；需要 API Key（DeepSeek / OpenAI）",
     "codex_home": True},
    {"id": "claude-code-acp", "label": "Claude Code（ACP 适配器）",
     "pkg": "@zed-industries/claude-code-acp", "provider": "anthropic",
     "note": "需要 Claude Code 的登录态（~/.claude）或 ANTHROPIC_API_KEY"},
    {"id": "opencode-ai", "label": "OpenCode（原生 exe）",
     "pkg": "opencode-ai", "provider": "",
     "note": "自带平台原生 exe；不需要 node 运行"},
]

_PKG_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$", re.I)

_LOCK = threading.RLock()
_JOB = {"running": False, "id": "", "phase": "", "log": [], "error": "",
        "started": 0.0, "finished": 0.0, "ok": False, "command": [], "agent": ""}


# ---------------------------------------------------------------- 小工具

def node_and_npm() -> tuple:
    """(node.exe, npm-cli.js)。npm 就在 node 同级的 `node_modules/npm/bin/` 里（随包 node 自带）。"""
    node = agents._glob_node_exe()
    if not node:
        return "", ""
    npm = os.path.join(os.path.dirname(node), "node_modules", "npm", "bin", "npm-cli.js")
    if not os.path.isfile(npm):
        npm = ""
    return node, npm


def _job_log(line: str) -> None:
    s = str(line or "").rstrip()
    if not s:
        return
    with _LOCK:
        _JOB["log"].append(s[:400])
        if len(_JOB["log"]) > 60:
            _JOB["log"] = _JOB["log"][-60:]


def _set(**kv) -> None:
    with _LOCK:
        _JOB.update(kv)


def status() -> dict:
    with _LOCK:
        out = dict(_JOB)
        out["log"] = list(_JOB["log"])
    out["busy"] = bool(out.pop("running", False))
    return out


def entry_for(spec: dict) -> dict:
    """目录里某一项的「现在能不能装 / 装过没有」。"""
    aid = str(spec.get("id") or "")
    dest = os.path.join(INSTALL_ROOT, aid)
    node, npm = node_and_npm()
    # 注册表里是不是已经有这个包对应的 agent？
    reg = agents.load_registry()
    hit = ""
    for a in (reg.get("agents") or []):
        cmd = " ".join(str(x) for x in (a.get("command") or []))
        if dest and dest.lower() in cmd.lower():
            hit = str(a.get("id"))
            break
    return {"id": aid, "label": spec.get("label") or aid, "pkg": spec.get("pkg") or "",
            "provider": spec.get("provider") or "", "note": spec.get("note") or "",
            "installed": bool(hit) or os.path.isdir(dest),
            "registered": hit, "dir": dest,
            "ready": bool(node and npm), "reason": "" if (node and npm)
            else "这台机器没有 Node（随包 node 或系统 node）——先装带 node 的安装包"}


def catalog() -> list:
    return [entry_for(s) for s in CATALOG]


def _dest(aid: str) -> str:
    if not re.match(r"^[a-z0-9][a-z0-9._-]{1,40}$", str(aid or ""), re.I):
        raise ValueError("非法的安装 id：%s" % aid)
    return os.path.join(INSTALL_ROOT, aid)


def _id_for_pkg(pkg: str) -> str:
    """从一个「包名 / 本地目录」推出一个安全的安装 id（**别拿整条路径当 id**）。"""
    name = ""
    if os.path.isdir(str(pkg or "")):
        try:
            with open(os.path.join(pkg, "package.json"), "r", encoding="utf-8") as fh:
                name = str((json.load(fh) or {}).get("name") or "")
        except Exception:                       # noqa: BLE001
            name = ""
        if not name:
            name = os.path.basename(os.path.normpath(pkg))
    else:
        name = str(pkg or "")
    name = name.split("/")[-1]                  # 去掉 @scope/
    s = re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._")
    s = (s or "agent")[:40]
    return s if len(s) >= 2 else (s + "-x")


def _ensure_pkg_json(dest: str, aid: str) -> None:
    """npm 在空目录里用 `--prefix` 装会抱怨；先放一个最小 package.json。"""
    os.makedirs(dest, exist_ok=True)
    pj = os.path.join(dest, "package.json")
    if not os.path.isfile(pj):
        with open(pj, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"name": "cocraft-agent-%s" % aid, "private": True,
                       "version": "1.0.0"}, fh)


def _run_npm(node: str, npm: str, args, registry: str = "") -> tuple:
    cmd = [node, npm] + list(args) + ["--no-audit", "--no-fund", "--loglevel", "error"]
    if registry:
        cmd += ["--registry", registry]
    _job_log("$ " + " ".join(cmd))
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, bufsize=0,
                             env=dict(os.environ, npm_config_yes="true"))
    except OSError as exc:
        return 1, "启动 npm 失败：%s" % exc
    t0 = time.time()
    out = []
    while True:
        if time.time() - t0 > NPM_TIMEOUT:
            try:
                p.kill()
            except Exception:                   # noqa: BLE001
                pass
            return 2, "npm 超时（%.0fs）" % NPM_TIMEOUT
        line = p.stdout.readline()
        if not line:
            if p.poll() is not None:
                break
            time.sleep(0.05)
            continue
        s = line.decode("utf-8", "replace").rstrip()
        if s:
            out.append(s)
            _job_log(s)
    try:
        rc = p.wait(timeout=20)
    except Exception:                           # noqa: BLE001
        rc = 1
    return rc, "\n".join(out[-20:])


def _bin_to_command(base: str, pj: dict, node: str) -> list:
    """从 package.json 的 `bin` 里认出可执行入口 → 返回启动命令。"""
    b = pj.get("bin")
    rel = ""
    if isinstance(b, str):
        rel = b
    elif isinstance(b, dict) and b:
        # 优先挑名字像 acp 的；否则按 key 排序取第一个（稳定）
        keys = sorted(b.keys())
        pick = next((k for k in keys if "acp" in k.lower()), keys[0])
        rel = b.get(pick) or ""
    for cand in ([rel] if rel else []) + ["dist/index.js", "bin/%s.js" % (pj.get("name") or "x")]:
        if not cand:
            continue
        p = os.path.normpath(os.path.join(base, cand))
        if os.path.isfile(p):
            ext = os.path.splitext(p)[1].lower()
            if ext == ".exe":
                return [p]
            return ([node, p] if node else [])
        if os.path.isfile(p + ".exe"):          # bin 写的是 `bin/opencode`，实际给的是 .exe
            return [p + ".exe"]
    return []


def _write_codex_template(dest: str) -> str:
    """给 npm 装的 codex-acp 一份**独立的 CODEX_HOME**（干净模板，不含密钥）。

    ⚠ 不建它的话，适配器会去找 `~/.codex`；没有就 `Authentication required`（踩坑 #50 同款）。
    """
    home = os.path.join(dest, "codex-home")
    os.makedirs(home, exist_ok=True)
    cfg = os.path.join(home, "config.toml")
    if not os.path.isfile(cfg):
        with open(cfg, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(
                "# cocraft：面板内装的 codex-acp 用这份**独立**的 CODEX_HOME（不碰你自己的 ~/.codex）\n"
                "# API Key 由「设置 → 模型 API Key」写入 baseline.env（走 DEEPSEEK_API_KEY 环境变量）\n"
                'model = "deepseek-flash"\n'
                'model_provider = "deepseek"\n'
                'model_context_window = 1048576\n'
                "\n"
                "[model_providers.deepseek]\n"
                'name = "DeepSeek"\n'
                'base_url = "https://api.deepseek.com/v1"\n'
                'env_key = "DEEPSEEK_API_KEY"\n'
                'wire_api = "responses"\n')
    return home


# ---------------------------------------------------------------- 安装

def install(spec_id: str, pkg: str = "", registry: str = "") -> dict:
    """开始安装（**异步**，立刻返回）。`spec_id` 可以是目录 id，也可以是自定义的 `pkg`。"""
    with _LOCK:
        if _JOB["running"]:
            raise RuntimeError("已经有一个安装任务在跑：%s" % (_JOB["id"] or "?"))
        spec = next((s for s in CATALOG if str(s.get("id")) == str(spec_id)), None)
        if spec is None and not pkg:
            pkg = str(spec_id or "").strip()
        if spec is None:
            spec = {"id": _id_for_pkg(pkg), "label": pkg, "pkg": pkg,
                    "provider": "", "note": "自定义 npm 包"}
        pkg_name = str(spec.get("pkg") or pkg or "").strip()
        if not _PKG_RE.match(pkg_name) and not os.path.isdir(pkg_name):
            raise ValueError("不像一个 npm 包名：%s" % pkg_name)
        _dest(str(spec["id"]))                  # 先校验 id（非法就当场抛，别进线程）
        node, npm = node_and_npm()
        if not node or not npm:
            raise RuntimeError("没找到 Node / npm（随包 node 里自带 npm）—— 先装带 node 的安装包")
        _set(running=True, id=str(spec["id"]), phase="准备", log=[], error="", ok=False,
             started=time.time(), finished=0.0, command=[], agent="")
    th = threading.Thread(target=_worker, args=(spec, pkg_name, node, npm, registry), daemon=True)
    th.start()
    return {"ok": True, "id": spec["id"], "dest": os.path.join(INSTALL_ROOT, spec["id"])}


def _worker(spec: dict, pkg_name: str, node: str, npm: str, registry: str) -> None:
    aid = str(spec["id"])
    try:
        dest = _dest(aid)
        _ensure_pkg_json(dest, aid)
        _set(phase="下载安装（npm）")
        rc, tail = _run_npm(node, npm, ["install", "--prefix", dest, pkg_name], registry)
        if rc != 0 and not registry:
            _job_log("[i] 直连 registry 失败（rc=%d）→ 换镜像 registry.npmmirror.com 再试" % rc)
            _set(phase="下载安装（国内镜像）")
            rc, tail = _run_npm(node, npm, ["install", "--prefix", dest, pkg_name], MIRROR)
        if rc != 0:
            _set(running=False, phase="失败", error="npm 失败（rc=%d）：%s" % (rc, tail[-800:]),
                 finished=time.time())
            return
        # 找入口
        _set(phase="识别入口")
        # ⚠ `pkg_name` 是**本地目录**时（`file:` 依赖 / 测试用），不能拿它拼 node_modules 路径 ——
        #   要用那个目录 package.json 里的**包名**（否则会算到源目录去，注册表里就指向临时文件夹）。
        inst_name = pkg_name
        if os.path.isdir(pkg_name):
            try:
                with open(os.path.join(pkg_name, "package.json"), "r", encoding="utf-8") as fh:
                    inst_name = str((json.load(fh) or {}).get("name") or "")
            except Exception:                   # noqa: BLE001
                inst_name = ""
            inst_name = inst_name or os.path.basename(os.path.normpath(pkg_name))
        base = os.path.join(dest, "node_modules", *inst_name.split("/"))
        pj = {}
        try:
            with open(os.path.join(base, "package.json"), "r", encoding="utf-8") as fh:
                pj = json.load(fh) or {}
        except Exception:                       # noqa: BLE001
            pj = {}
        cmd = _bin_to_command(base, pj, node)
        if not cmd:
            _set(running=False, phase="失败",
                 error="装好了，但认不出可执行入口（package.json 的 bin / dist/index.js 都没有）：%s" % base,
                 finished=time.time())
            return
        # ★ 再兜一道：入口必须**在安装目录里**（本地目录依赖时 npm 会做 symlink，别把源目录写进注册表）
        dest_l = os.path.normcase(os.path.normpath(dest)) + os.sep
        if not all(os.path.normcase(os.path.normpath(x)).startswith(dest_l) for x in cmd[1:] or cmd):
            _set(running=False, phase="失败",
                 error="入口不在安装目录里（可能是本地目录依赖的链接）：%r" % (cmd,),
                 finished=time.time())
            return
        env = {}
        if spec.get("codex_home"):
            _set(phase="写 CODEX_HOME 模板")
            env["CODEX_HOME"] = _write_codex_template(dest)
        _set(phase="注册到 agentlist")
        a = agents.add_agent(str(spec.get("label") or aid), cmd, cwd="", env=env,
                             note=str(spec.get("note") or ""), provider=str(spec.get("provider") or ""))
        _set(running=False, phase="完成", ok=True, finished=time.time(),
             command=cmd, agent=str((a or {}).get("id") or ""))
        _job_log("[OK] 已注册：%s" % ((a or {}).get("id") or "?"))
    except Exception as exc:                    # noqa: BLE001
        _set(running=False, phase="失败", error="%s: %s" % (type(exc).__name__, exc),
             finished=time.time())


def uninstall(spec_id: str, delete_files: bool = True) -> dict:
    """卸载：先从注册表删（当前使用中的不允许），再删安装目录。"""
    entry = next((s for s in CATALOG if str(s.get("id")) == str(spec_id)), None)
    aid = str(spec_id)
    dest = _dest(aid)
    # 找到注册表里指向这个目录的 agent（可能被 add_agent 改了 id，例如加了 -2）
    reg = agents.load_registry()
    hit = ""
    for a in (reg.get("agents") or []):
        if dest.lower() in " ".join(str(x) for x in (a.get("command") or [])).lower():
            hit = str(a.get("id"))
            break
    removed_reg = False
    if hit:
        removed_reg = agents.remove_agent(hit)
        if not removed_reg:
            reg2 = agents.load_registry()
            others = [x for x in (reg2.get("agents") or []) if str(x.get("id")) != str(hit)]
            if not others:
                raise ValueError("这是 agentlist 里**最后一个** agent —— 先添加/切换到别的 agent 再卸载"
                                 "（否则面板就没 agent 可用了）")
            raise ValueError("这个 agent 正在使用中（或删不掉）——先在设置里切到别的 agent 再卸载")
    if delete_files and os.path.isdir(dest):
        shutil.rmtree(dest, ignore_errors=True)
    return {"ok": True, "id": aid, "agent": hit, "removed": not os.path.isdir(dest),
            "label": (entry or {}).get("label") or aid}
