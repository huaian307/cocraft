# -*- coding: utf-8 -*-
"""ACP 侧的技能发现（协议没有 skill，由面板自己扫文件系统）。

发现规则**对齐 OpenCode**（https://opencode.ai/v2/docs/skills，2026-10 版）：

    全局：~/.config/opencode/skills、~/.claude/skills、~/.agents/skills
    项目：从工作目录**向上到根**逐级找 .opencode/skills、.claude/skills、.agents/skills
    形态：目录下的**根级** `*.md`；或**任意深度**名为 `SKILL.md` 的文件
    id  ：`SKILL.md` 取所在目录名；根级 `*.md` 取文件名（去扩展名）
    name/description/autoinvoke：取自 frontmatter（`---` 包裹的头部）

覆盖（后注册的同 id 覆盖先注册的，与 OpenCode 的优先级一致）：
    ~/.claude 与项目 .claude  <  ~/.agents 与项目 .agents  <  ~/.config/opencode  <  项目 .opencode（最高）

⚠ 只**读**、只解析 frontmatter —— 不执行技能里的脚本，也不碰网络。
"""
from __future__ import annotations

import os
import re

# 一个技能根目录最多往下找几层（防极端目录把这里拖慢）
_MAX_DEPTH = 6

# frontmatter 里的 `metadata.opencode/autoinvoke: false` 或 `disable-model-invocation: true`
_AUTO_OFF = re.compile(r"(opencode/autoinvoke\s*:\s*false|disable-model-invocation\s*:\s*true)", re.I)
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", re.S)


def _home_dirs() -> list:
    """所有可能的用户目录（存在、去重）。

    ⚠ 与 `engines/opencode.py` 同款：只用 `expanduser("~")` 不够 —— 缺
      USERPROFILE/HOME 的进程里它会回字面量 `'~'`（安装版实测过）。
    """
    cands = []
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v:
            cands.append(v)
    hd, hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if hd and hp:
        cands.append(hd.rstrip("\\/") + hp)
    la = os.environ.get("LOCALAPPDATA") or ""
    tail = os.path.join("AppData", "Local")
    if la.lower().endswith(tail.lower()):
        cands.append(la[: -len(tail)].rstrip("\\/"))
    try:
        exp = os.path.expanduser("~")
        if exp and exp != "~":
            cands.append(exp)
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for p in cands:
        try:
            p = os.path.normpath(str(p))
        except Exception:  # noqa: BLE001
            continue
        key = p.lower()
        if p and key not in seen and os.path.isdir(p):
            seen.add(key)
            out.append(p)
    return out


def _ancestors(cwd: str) -> list:
    """从**根 → 当前目录**的祖先链（后面的更靠近当前目录 = 更高优先级）。

    cwd 为空时回空列表。Windows 上到 `D:\\` 这种盘根为止。
    """
    cwd = str(cwd or "").strip()
    if not cwd:
        return []
    try:
        cwd = os.path.abspath(cwd)
    except Exception:  # noqa: BLE001
        return []
    chain, cur = [], cwd
    while True:
        chain.append(cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    chain.reverse()
    return chain


def _front(text: str):
    """拆 frontmatter：回 `(meta_dict, body)`。

    ⚠ 只做**够用**的解析（`key: value` 一行一条 + autoinvoke 特判），不引 YAML 依赖。
    """
    m = _FM.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        line = line.rstrip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        k = k.strip()
        if not k or k in ("metadata",):
            continue
        meta[k] = v.strip().strip('"').strip("'")
    return meta, text[m.end():]


def _iter_root(root: str):
    """遍历一个技能根目录，回 `(文件路径, 技能 id)`。

    · 根级 `*.md`（`SKILL.md` 除外，交给下面统一处理）→ id = 文件名
    · 任意深度的 `SKILL.md` → id = 所在目录名（根目录下的回 "SKILL"）
    """
    if not os.path.isdir(root):
        return
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return
    for name in entries:
        p = os.path.join(root, name)
        if os.path.isfile(p) and name.lower().endswith(".md") and name.lower() != "skill.md":
            yield p, os.path.splitext(name)[0]

    base_depth = root.rstrip("\\/").count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.rstrip("\\/").count(os.sep) - base_depth > _MAX_DEPTH:
            dirnames[:] = []
            continue
        if "SKILL.md" in filenames:
            sid = os.path.basename(dirpath) if os.path.abspath(dirpath) != os.path.abspath(root) else "SKILL"
            yield os.path.join(dirpath, "SKILL.md"), sid


def discover(cwd: str = None) -> list:
    """发现所有技能。回 `[{id,name,description,autoinvoke,source,path,content}]`（同名 id 后者胜）。"""
    out, order = {}, []

    def add(root, source):
        for path, sid in _iter_root(root):
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    raw = fh.read()
            except OSError:
                continue
            meta, body = _front(raw)
            item = {
                "id": sid,
                "name": meta.get("name") or sid,
                "description": meta.get("description") or "",
                "autoinvoke": not bool(_AUTO_OFF.search(raw)),
                "source": source,
                "path": path,
                "content": body.strip(),
            }
            if sid not in out:
                order.append(sid)
            out[sid] = item

    homes = _home_dirs()
    anc = _ancestors(cwd)
    # 低 → 高（后面的覆盖前面的）
    for h in homes:
        add(os.path.join(h, ".claude", "skills"), "~/.claude")
    for d in anc:
        add(os.path.join(d, ".claude", "skills"), ".claude")
    for h in homes:
        add(os.path.join(h, ".agents", "skills"), "~/.agents")
    for d in anc:
        add(os.path.join(d, ".agents", "skills"), ".agents")
    for h in homes:
        add(os.path.join(h, ".codex", "skills"), "~/.codex")       # Codex 的技能目录
    for d in anc:
        add(os.path.join(d, ".codex", "skills"), ".codex")
    for h in homes:
        add(os.path.join(h, ".config", "opencode", "skills"), "~/.config/opencode")
    for d in anc:
        add(os.path.join(d, ".opencode", "skills"), ".opencode")

    return [out[i] for i in order]


def find(skill_id: str, cwd: str = None):
    """按 id 找一个技能（含正文）；找不到回 None。"""
    want = str(skill_id or "").strip()
    if not want:
        return None
    for it in discover(cwd):
        if str(it.get("id")) == want:
            return it
    return None


def public(item: dict) -> dict:
    """给列表/详情用的对外字段（跟引擎基类约定一致）。"""
    return {k: item.get(k) for k in ("id", "name", "description", "autoinvoke",
                                     "source", "path", "content")}


def injection_text(item: dict) -> str:
    """把技能拼成一段可注入会话的文本（正文不带 frontmatter）。"""
    name = item.get("name") or item.get("id") or "技能"
    body = (item.get("content") or "").strip()
    return "【已激活技能：%s】\n\n%s" % (name, body)
