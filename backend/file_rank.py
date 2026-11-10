# -*- coding: utf-8 -*-
r"""文件重要性评级（★ 1~5）——「低风险删除」自动隔离放行的唯一判据。

为什么需要它：agent 一次重构会发几十条 `rm` / `Remove-Item`，每条都弹窗会把
「可逆执行」变成「可烦执行」。但**破坏性操作永不自动放行**是铁律，所以这里只做
一件事：把「**明确可再生成**的东西」识别出来（构建产物 / 缓存 / 日志 / 临时文件），
并且**只有在能完整隔离、范围完全确定、且命令里没有别的破坏性动作**时才允许自动放行。

设计纪律（不许放松）：
  * **确定性**：纯规则 + 本地 git 状态，不调模型、不联网；同样输入永远同样输出。
  * **fail-closed**：任何一条不确定（路径解析不出 / 在区外 / 是 git 跟踪文件 /
    像密钥 / 命令里还有 shutdown·taskkill·format·reg delete / 隔离会跳过）
    → 一律回「不确定」，上层照旧弹窗。
  * **可解释**：每个评分都带 `reasons`，面板上能说清"为什么这次没问你"。
  * **只认删除**：`DELETE_RE` 之外一律不碰（`shutdown` / `taskkill` / `format` /
    `reg delete` 没有可恢复的"原件"，走这条路就是假安全）。

星级与分数（0~100）：
  ★   ≤ 20   明确可再生成（node_modules / __pycache__ / dist / *.log / *.tmp …）
  ★★  21~40  未知但看起来像数据（未跟踪、非常规扩展名）
  ★★★ 41~60  普通文件（可能是源码/文档，但没命中强信号）
  ★★★★ 61~80 项目源码 / 配置 / 文档
  ★★★★★ 81~100 硬禁止（.git / 密钥 / runtime 状态 / 工作区根 / 区外）
"""
from __future__ import annotations

import os
import re
import subprocess

# ---------------------------------------------------------------- 命令判据

# 只认「删除」。⚠ 不要拿去替代 `cocraft_agents._RE_DESTRUCTIVE`（那个还包含关机/杀进程，
# 用来"要不要弹窗"；这里用来"能不能自动隔离放行"，范围必须更窄）。
DELETE_RE = re.compile(
    r"(?ix)"
    r"(?:^|[\s;&|()\"'`])(?:rm|rd|del|erase|rmdir)\b"
    r"|remove-item(?!property)"
    r"|(?:file|directory)\s*\]?\s*::\s*delete\s*\("
    r"|(?:file|directory)\.delete\s*\("
    r"|(?:os|shutil)\.(?:remove|unlink|rmtree|removedirs)\s*\("
    r"|(?:fs|fsp)\.(?:rmSync|rm|unlinkSync|unlink|rmdirSync|rmdir)\s*\("
    r"|\.(?:rmSync|unlinkSync|rmdirSync)\s*\("
    r"|\.unlink\s*\("
)

# 命令里只要有这些，就**绝不**自动放行（它们没有可恢复的"原件"，或者会改内容）。
# 说明：`taskkill` 杀进程、`shutdown` 关机会话，内存状态无法复制；`format`/`diskpart`
# 破坏范围是整卷；`move-item`/`set-content` 不是"删了还能从忏悔室放回来"那么简单。
OTHER_DESTRUCTIVE_RE = re.compile(
    r"(?ix)"
    r"\bformat\b(?!-(?:table|list|wide|custom|hex))"
    r"|diskpart|shutdown|taskkill|stop-process|\bkill\b"
    r"|reg\s+delete|\bremove-itemproperty|\bnew-itemproperty|\bset-itemproperty"
    r"|\bclear-disk|\bremove-partition|\bsdelete\b|cipher\s+/w"
    r"|\bmove-item|\brename-item|\bmv\b|\bmove\b|\bren\b"
    r"|\bcopy-item|robocopy|xcopy|\bcp\b"
    r"|\bset-content|\bclear-content|\badd-content|\bout-file|\btee\b"
    r"|truncate|dd\s+if=|git\s+clean"
)

# ---------------------------------------------------------------- 文件分类

# 明确"可再生成"的目录名（任意层级）。⚠ 不含 bin / env / .vscode（可能是源码/配置）。
DISPOSABLE_DIRS = {
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".nox", ".venv", "venv", "dist", "build", "out", "output",
    "coverage", "htmlcov", ".next", ".nuxt", ".svelte-kit", ".parcel-cache",
    ".turbo", ".cache", "cache", "tmp", "temp", ".tmp", "logs",
    "obj", "debug", "release", ".eggs", ".idea", ".vs",
}

# 明确"可再生成"的扩展名。⚠ 不含 .map / .lock / .zip（有时是交付物）。
DISPOSABLE_EXT = {
    ".log", ".tmp", ".temp", ".bak", ".orig", ".rej", ".swp", ".swo",
    ".pyc", ".pyo", ".class", ".o", ".obj", ".pdb", ".ilk", ".cache",
    ".tsbuildinfo", ".suo", ".ncb", ".sdf", ".opensdf", ".ipch", ".pid",
}

DISPOSABLE_NAMES = {
    ".ds_store", "thumbs.db", "desktop.ini", ".directory",
    "npm-debug.log", "yarn-error.log", "pnpm-debug.log",
    ".eslintcache", ".stylelintcache", ".coverage", "coverage.xml",
}

# 源码 / 脚本 / 文档扩展名 —— 命中就至少 ★★★★。
SOURCE_EXT = {
    ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".css", ".scss", ".sass", ".less", ".html", ".htm", ".vue", ".svelte",
    ".go", ".rs", ".java", ".kt", ".kts", ".cs", ".fs", ".vb",
    ".c", ".h", ".cc", ".cpp", ".hpp", ".m", ".mm", ".swift",
    ".php", ".rb", ".lua", ".r", ".jl",
    ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".bash", ".zsh", ".fish",
    ".sql", ".graphql", ".proto", ".iss", ".nsi", ".rc",
    ".csproj", ".sln", ".vcxproj", ".fsproj",
    ".md", ".mdx", ".rst",
}

# 项目"门面 / 配置"文件名 —— 命中直接 ★★★★★（删了项目就跑不起来 / 说不清是什么）。
CONFIG_NAMES = {
    ".gitignore", ".gitattributes", ".gitmodules", ".editorconfig",
    ".npmrc", ".nvmrc", ".python-version",
    "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt",
    "pipfile", "pipfile.lock", "poetry.lock", "uv.lock",
    "cargo.toml", "cargo.lock", "go.mod", "go.sum",
    "tsconfig.json", "jsconfig.json", "vite.config.js", "vite.config.ts",
    "webpack.config.js", "rollup.config.js", "eslint.config.js",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml", "makefile",
    "cmakelists.txt", "version", "license", "notice", "copying",
    "agents.md", "readme", "changelog.md", "contributing.md", "security.md",
    "cocraft.iss",
}

# 门面类"前缀"（README.zh-CN.md / LICENSE-MIT 之类）。
CONFIG_PREFIX = ("readme", "license", "licence", "changelog", "contributing",
                 "security", "code_of_conduct", "notice", "copying")

# 绝不允许自动放行的路径分量（密钥 / 版本库 / 我们自己的状态）。
HARD_DIRS = {".git", ".hg", ".svn", ".ssh", ".aws", ".azure", ".kube", ".gnupg"}

SECRET_NAME_RE = re.compile(
    r"(?ix)^(?:"
    r"_credentials\.json|_acp_agents\.json|_music_cookie\.json|_github_token\.json|"
    r"_quarantine\.json|_perm_level\.json|_acp_always\.json|_mail\.json|"
    r"\.env(?:\.[\w.-]+)?|\.npmrc|\.git-credentials|credentials|secrets?|"
    r"id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?"
    r")$"
)
SECRET_EXT = {".pem", ".key", ".pfx", ".p12", ".jks", ".keystore", ".kdbx", ".ovpn"}

# 工作区里的"核心目录"：命中再 +15（同样 ★ 的文件，在 backend/ 里比在根目录更要紧）。
PROJECT_DIRS = {"backend", "frontend", "docs", "tools", "packaging",
                "scripts", "launchers", ".github", ".opencode", ".agents", ".claude"}

# 目录采样上限：超过就按"重要"处理（未知的大目录不许自动删）。
DIR_SAMPLE_LIMIT = 400
DIR_SAMPLE_DEPTH = 2


# ---------------------------------------------------------------- 小工具

def _inside(path: str, parent: str) -> bool:
    p = os.path.normcase(os.path.realpath(path))
    q = os.path.normcase(os.path.realpath(parent))
    return p == q or p.startswith(q + os.sep)


def _drive_root(path: str) -> bool:
    p = os.path.abspath(path)
    return os.path.dirname(p) == p


def stars_of(score: int) -> int:
    """分数 → 星级（1~5）。"""
    s = max(0, min(100, int(score)))
    if s <= 20:
        return 1
    if s <= 40:
        return 2
    if s <= 60:
        return 3
    if s <= 80:
        return 4
    return 5


def _parts(rel: str) -> list:
    return [p for p in str(rel or "").replace("\\", "/").split("/") if p]


def _disposable_name(name: str) -> bool:
    n = str(name or "").lower()
    if n in DISPOSABLE_NAMES:
        return True
    if n.startswith("~$"):                 # Office 临时文件
        return True
    if n.endswith(".tmp") or n.endswith(".temp"):
        return True
    return False


def _git_run(root: str, args, data=None, timeout: float = 3.0):
    """跑一次 git（无窗口）。失败一律返回 None —— 调用方按"不知道"处理。"""
    try:
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x08000000          # CREATE_NO_WINDOW
        return subprocess.run(["git", "-C", root] + list(args),
                              input=data, capture_output=True,
                              timeout=timeout, **kw)
    except Exception:                                  # noqa: BLE001
        return None


def git_state(root: str, paths) -> list:
    """批量查 `[{tracked, ignored}]`（一次 `ls-files` + 一次 `check-ignore`）。

    * 不是 git 仓库 / 没装 git → 全 `False`（**不因此放行**，源码扩展名照样高分）。
    * `tracked=True` → 上层会抬到至少 ★★★，绝不允许自动删。
    """
    out = [{"tracked": False, "ignored": False} for _ in (paths or [])]
    if not root or not os.path.isdir(root) or not paths:
        return out
    rels = []
    for p in paths:
        try:
            rels.append(os.path.relpath(str(p), root).replace("\\", "/"))
        except Exception:                              # noqa: BLE001
            rels.append("")
    live = [r for r in rels if r and not r.startswith("..")]
    if not live:
        return out

    # ① 被跟踪的文件（目录也能查：`ls-files -- dir` 会列目录下的文件）
    p = _git_run(root, ["ls-files", "-z", "--"] + live)
    tracked = set()
    if p is not None and p.returncode == 0:
        tracked = {x for x in p.stdout.decode("utf-8", "replace").split("\0") if x}
    # 目录命中：把每个被跟踪文件的**祖先目录**也记下来，避免 O(n*m)
    parents = set()
    for t in tracked:
        parts = t.split("/")
        for i in range(1, len(parts)):
            parents.add("/".join(parts[:i]))
    for i, r in enumerate(rels):
        if r and (r in tracked or r in parents):
            out[i]["tracked"] = True

    # ② 被忽略的文件
    try:
        data = ("\0".join(live)).encode("utf-8", "replace")
        q = _git_run(root, ["check-ignore", "-z", "--stdin"], data=data)
        if q is not None and q.returncode == 0:
            ign = {x for x in q.stdout.decode("utf-8", "replace").split("\0") if x}
            for i, r in enumerate(rels):
                if not r:
                    continue
                # 文件本身被忽略，或目录里有被忽略的子文件（`dist/` 这种）
                if r in ign or any(x.startswith(r.rstrip("/") + "/") for x in ign):
                    out[i]["ignored"] = True
    except Exception:                                  # noqa: BLE001
        pass
    return out


def _walk_stats(path: str):
    """目录采样：返回 (是否有重要文件, 是否为空, 字节数是否可估算)。

    只看两层、最多 400 个文件 —— 足够区分 `dist/`（全是 hash 产物）和 `src/`。
    超过采样上限 → 视为"重要"（未知的大目录不许自动删）。
    """
    files = 0
    important = False
    try:
        base_depth = path.rstrip("\\/").count(os.sep)
        for dp, dns, fns in os.walk(path):
            depth = dp.rstrip("\\/").count(os.sep) - base_depth
            if depth >= DIR_SAMPLE_DEPTH:
                dns[:] = []
            for f in fns:
                files += 1
                if files > DIR_SAMPLE_LIMIT:
                    return True, False
                low = f.lower()
                ext = os.path.splitext(low)[1]
                if ext in SOURCE_EXT or low in CONFIG_NAMES:
                    important = True
            if files > DIR_SAMPLE_LIMIT:
                return True, False
    except Exception:                                  # noqa: BLE001
        return True, False
    return important, files == 0


# ---------------------------------------------------------------- 单个路径评分

def score_path(path: str, root: str, git: dict = None) -> dict:
    """给**一个**路径打分。返回 `{path, rel, score, stars, reasons, tracked, ignored, blocked}`。

    `git` = `{"tracked": bool, "ignored": bool}`（由 `git_state()` 批量算好传进来）。
    """
    reasons = []
    g = git or {}
    try:
        real = os.path.realpath(str(path))
    except Exception:                                  # noqa: BLE001
        real = str(path)
    try:
        rel = os.path.relpath(real, os.path.realpath(root)).replace("\\", "/")
    except Exception:                                  # noqa: BLE001
        rel = ""

    def blocked(reason, why=None):
        return {"path": real, "rel": rel, "score": 100, "stars": 5,
                "reasons": [reason] + ([why] if why else []),
                "tracked": bool(g.get("tracked")), "ignored": bool(g.get("ignored")),
                "blocked": True}

    # ① 硬禁止：区外 / 根 / 盘符 / 版本库 / 密钥 / 我们自己的状态
    if not root or not os.path.isdir(root):
        return blocked("没有工作目录", "范围未知一律去问")
    if not _inside(real, root):
        return blocked("不在工作区内", "区外路径一律去问")
    if os.path.normcase(real) == os.path.normcase(os.path.realpath(root)):
        return blocked("这是工作区根目录", "删根目录没有可恢复的意义")
    if _drive_root(real):
        return blocked("这是盘符根目录", "范围过大")
    low_parts = [p.lower() for p in _parts(rel)]
    for d in low_parts:
        if d in HARD_DIRS:
            return blocked("在版本库 / 密钥目录里（%s）" % d, "不自动删除")
    if ".git" in low_parts:
        return blocked("在 .git 里", "不自动删除")
    # 我们自己的状态（引擎、凭据、权限等级、审计…）：删了不是"项目文件"
    if len(low_parts) >= 2 and low_parts[0] == "runtime" and low_parts[1] == "state":
        return blocked("在 runtime/state（引擎 / 凭据 / 权限状态）里", "不自动删除")

    name = os.path.basename(real)
    low = name.lower()
    ext = os.path.splitext(low)[1]
    if SECRET_NAME_RE.match(low) or ext in SECRET_EXT:
        return blocked("像密钥 / 凭据文件", "不自动删除")

    # ② 明确可再生成：★
    for d in low_parts[:-1]:
        if d in DISPOSABLE_DIRS:
            return {"path": real, "rel": rel, "score": 8, "stars": 1,
                    "reasons": ["在可再生成目录 `%s` 里" % d],
                    "tracked": bool(g.get("tracked")), "ignored": bool(g.get("ignored")),
                    "blocked": False}
    if ext in DISPOSABLE_EXT or _disposable_name(low):
        return {"path": real, "rel": rel, "score": 10, "stars": 1,
                "reasons": ["可再生成的缓存 / 日志 / 临时文件"],
                "tracked": bool(g.get("tracked")), "ignored": bool(g.get("ignored")),
                "blocked": False}
    if os.path.isdir(real):
        # 目录名本身也可能就是 node_modules 之类（最后一层）
        if low in DISPOSABLE_DIRS:
            return {"path": real, "rel": rel, "score": 8, "stars": 1,
                    "reasons": ["目录名 `%s` 是可再生成的" % low],
                    "tracked": bool(g.get("tracked")), "ignored": bool(g.get("ignored")),
                    "blocked": False}
        important, empty = _walk_stats(real)
        if empty:
            return {"path": real, "rel": rel, "score": 15, "stars": 1,
                    "reasons": ["空目录"],
                    "tracked": bool(g.get("tracked")), "ignored": bool(g.get("ignored")),
                    "blocked": False}
        if important:
            return {"path": real, "rel": rel, "score": 88, "stars": 5,
                    "reasons": ["目录里有源码 / 配置 / 大量未知文件"],
                    "tracked": bool(g.get("tracked")), "ignored": bool(g.get("ignored")),
                    "blocked": False}

    # ③ 普通文件：按信号累加
    score = 45
    if ext in SOURCE_EXT:
        score += 30
        reasons.append("源码 / 文档 / 脚本")
    if low in CONFIG_NAMES or low.startswith(CONFIG_PREFIX):
        score += 35
        reasons.append("项目门面 / 配置")
    if low_parts and low_parts[0] in PROJECT_DIRS:
        score += 15
        reasons.append("在工作区核心目录 `%s/` 里" % low_parts[0])
    if g.get("tracked"):
        score += 20
        reasons.append("被 git 跟踪")
    elif g.get("ignored"):
        score -= 25
        reasons.append("被 .gitignore 忽略")
    # 刚写过的文件（2 小时内）多半是 agent 正在改，抬一档 —— "可能还在用"
    try:
        if (not os.path.isdir(real)) and (time_now() - os.path.getmtime(real) < 7200):
            score += 5
            reasons.append("2 小时内刚改过")
    except Exception:                                  # noqa: BLE001
        pass
    score = max(0, min(100, int(score)))
    stars = stars_of(score)
    if not reasons:
        reasons.append("普通文件")
    return {"path": real, "rel": rel, "score": score, "stars": stars,
            "reasons": reasons[:4], "tracked": bool(g.get("tracked")),
            "ignored": bool(g.get("ignored")), "blocked": False}


def time_now() -> float:
    """单独抽出来，方便测试打桩。"""
    import time
    return time.time()


# ---------------------------------------------------------------- 批量判定

def triage(root: str, cmd: str, max_star: int = 1) -> dict:
    """判断这条删除命令能不能"先隔离、再自动放行"。

    返回：
      `{ok, reason, cmd, targets[], items[], max_star, unresolved[], blocked[]}`

    `ok=True` 的**必要条件**（缺一不可）：
      1. 是删除类命令，且命令里没有别的破坏性动作；
      2. 能解析出至少一个目标，且**没有**解析不出的 token；
      3. 每个目标都 ≤ `max_star` 星，且没有被硬禁止；
      4. 所有目标都在工作区内（`score_path` 里已判）。
    """
    import snapshots                                    # 延迟导入，避免环

    text = str(cmd or "")
    out = {"ok": False, "reason": "", "cmd": text, "targets": [], "items": [],
           "max_star": int(max_star or 1), "unresolved": [], "blocked": []}
    if not text.strip():
        out["reason"] = "没有命令原文"
        return out
    if not DELETE_RE.search(text):
        out["reason"] = "不是删除类命令"
        return out
    if OTHER_DESTRUCTIVE_RE.search(text):
        out["reason"] = "命令里还含其它破坏性 / 改写动作（关机、杀进程、格式化、移动、写文件…）"
        return out
    if not root or not os.path.isdir(root):
        out["reason"] = "没有工作目录"
        return out

    try:
        g = snapshots.guess_targets(text, str(root)) or {}
    except Exception as exc:                            # noqa: BLE001
        out["reason"] = "解析目标失败：%s" % exc
        return out
    targets = list(g.get("targets") or [])
    unresolved = list(g.get("unresolved") or [])
    out["unresolved"] = unresolved[:20]
    if unresolved:
        out["reason"] = "有 %d 个目标解析不出来（范围不确定）" % len(unresolved)
        return out
    if not targets:
        out["reason"] = "解析不出要删什么（范围不确定）"
        return out
    # 去重 + 去掉父子重复（父目录已删，子项不用再算）
    uniq = sorted({os.path.realpath(t) for t in targets}, key=len)
    keep = []
    for p in uniq:
        if any(_inside(p, k) and p != k for k in keep):
            continue
        keep.append(p)
    out["targets"] = keep[:200]

    gs = git_state(str(root), out["targets"])
    items = [score_path(p, str(root), gs[i] if i < len(gs) else {})
             for i, p in enumerate(out["targets"])]
    # ★ 被 git 跟踪的东西，哪怕在 dist/ 里也不许自动删（可能是交付物 / 仓库里的一部分）
    for it in items:
        if it.get("tracked") and int(it["stars"]) < 3:
            it["stars"] = 3
            it["score"] = max(60, int(it["score"]))
            it["reasons"] = (["被 git 跟踪（即使看起来可再生成）"] + list(it["reasons"]))[:4]
    out["items"] = items
    bad = [it for it in items if it["blocked"] or int(it["stars"]) > int(max_star)]
    if bad:
        out["blocked"] = [{"path": it["path"], "stars": it["stars"],
                           "reasons": it["reasons"]} for it in bad[:10]]
        worst = bad[0]
        out["reason"] = ("有 %d 项不够低风险（最高 ★%d：%s）"
                         % (len(bad), max(int(x["stars"]) for x in bad),
                            "；".join(worst["reasons"][:2])))
        return out
    out["ok"] = True
    out["reason"] = "全部为 ★%d 及以下，可先隔离再放行" % int(max_star)
    return out


def describe(path: str, root: str) -> dict:
    """单文件/目录评级（给设置页 / 忏悔室展示用）。"""
    gs = git_state(root, [path])
    return score_path(path, root, gs[0] if gs else {})
