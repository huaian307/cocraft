#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""project_roots_test.py -- 新建项目的默认目录决策（`backend/project_roots.py`）。

用户 2026-11-05 的要求：
  · agent 有**自己的客户端/桌面端目录** → 项目建在 `<home>/projects`；
  · 没有（随包 node / 面板内装的 agent）→ cocraft 里，**按 agent 分类**；
  · 工作流 → cocraft 的 `runtime/workflow-projects`（所有成员共用的项目大文件夹）。

全程用临时"用户目录 / cocraft 目录"，不碰真实运行态。
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import project_roots as PR  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("[OK]  " if cond else "[BAD] ") + name + (("   " + str(detail)) if (detail and not cond) else ""))


def prep():
    tmp = tempfile.mkdtemp(prefix="projroots_")
    home = os.path.join(tmp, "home")
    for d in (".codex", ".claude", os.path.join(".local", "share", "opencode")):
        os.makedirs(os.path.join(home, d), exist_ok=True)
    cocraft = os.path.join(tmp, "cocraft")
    os.makedirs(os.path.join(cocraft, "runtime", "state"), exist_ok=True)
    # 模块级路径重定向（resolve() 里读的是模块全局）
    PR.ROOT = cocraft
    PR.STATE_DIR = os.path.join(cocraft, "runtime", "state")
    PR.OVERRIDE_FILE = os.path.join(PR.STATE_DIR, "_project_roots.json")
    os.environ["USERPROFILE"] = home
    os.environ["HOME"] = home
    os.environ.pop("CODEX_HOME", None)
    return tmp, home, cocraft


def t_kinds():
    check("codex-node → codex", PR.agent_kind("codex-node", {"command": ["node", "x"]}) == "codex")
    check("claude-node → claude", PR.agent_kind("claude-node") == "claude")
    check("opencode-acp → opencode", PR.agent_kind("opencode-acp") == "opencode")
    check("dsh → 没有客户端类型", PR.agent_kind("dsh") == "")
    check("从命令里认（绝对路径含 codex）",
          PR.agent_kind("custom", {"command": [r"D:\x\node.exe", r"D:\codex\acp.js"]}) == "codex")


def t_session(tmp, home, cocraft):
    # 1) codex：优先用注册表 env 里的 CODEX_HOME
    codex_home = os.path.join(tmp, "codex-home")
    os.makedirs(codex_home)
    r = PR.resolve("session", "codex-node", {"command": ["node"], "env": {"CODEX_HOME": codex_home}})
    check("codex 用自己的 CODEX_HOME → <home>/projects",
          r["root"] == os.path.join(codex_home, "projects") and r["source"] == "client", r)
    check("返回的目录已创建", os.path.isdir(r["root"]))

    # 2) claude → ~/.claude/projects
    r = PR.resolve("session", "claude-node", {"command": ["node"]})
    check("claude → ~/.claude/projects",
          r["root"] == os.path.join(home, ".claude", "projects") and r["source"] == "client", r)

    # 3) opencode → ~/.local/share/opencode/projects
    r = PR.resolve("session", "opencode-acp", {"command": ["opencode-cli", "acp"]})
    check("opencode → ~/.local/share/opencode/projects",
          r["root"] == os.path.join(home, ".local", "share", "opencode", "projects")
          and r["source"] == "client", r)

    # 4) 没有客户端目录 → cocraft，按 agent 分类
    r = PR.resolve("session", "dsh", {"command": ["node", "dsh-acp/bin.js"]})
    check("dsh（没有客户端）→ cocraft/runtime/projects/dsh",
          r["root"] == os.path.join(cocraft, "runtime", "projects", "dsh")
          and r["source"] == "cocraft", r)
    check("cocraft 分支目录已创建", os.path.isdir(r["root"]))

    # 4b) 随包 opencode（XDG_DATA_HOME 指到 cocraft 里）→ 项目也跟着进 cocraft
    bundled = os.path.join(cocraft, "runtime", "opencode-data")
    os.makedirs(os.path.join(bundled, "opencode"), exist_ok=True)
    r = PR.resolve("session", "opencode-node",
                   {"command": ["node", "x"], "env": {"XDG_DATA_HOME": bundled}})
    check("随包 opencode（XDG_DATA_HOME 在 cocraft 里）→ 跟随它",
          r["root"] == os.path.join(bundled, "opencode", "projects")
          and r["source"] == "client", r)

    # 4c) claude 的 CLAUDE_CONFIG_DIR 优先于 ~/.claude
    cdir = os.path.join(tmp, "claude-config")
    os.makedirs(cdir)
    r = PR.resolve("session", "claude-node", {"command": ["node"], "env": {"CLAUDE_CONFIG_DIR": cdir}})
    check("claude 认 CLAUDE_CONFIG_DIR",
          r["root"] == os.path.join(cdir, "projects") and r["source"] == "client", r)

    # 5) 连 agent id 都没有 → default
    r = PR.resolve("session", "", None)
    check("无 agent → cocraft/runtime/projects/default",
          r["root"] == os.path.join(cocraft, "runtime", "projects", "default"), r)

    # 6) 工作流 → cocraft 的专用大文件夹
    r = PR.resolve("workflow", "codex-node", {"command": ["node"]})
    check("工作流 → cocraft/runtime/workflow-projects（不看 agent）",
          r["root"] == os.path.join(cocraft, "runtime", "workflow-projects")
          and r["source"] == "workflow", r)
    check("工作流目录已创建", os.path.isdir(r["root"]))


def t_no_home(tmp):
    """用户目录里没有 .claude → 退回 cocraft（不能编一个不存在的路径）。"""
    empty = os.path.join(tmp, "empty-home")
    os.makedirs(empty, exist_ok=True)
    saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOME", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA")}
    try:
        os.environ["USERPROFILE"] = empty
        os.environ["HOME"] = empty
        for k in ("HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA"):
            os.environ.pop(k, None)
        r = PR.resolve("session", "claude-node", {"command": ["node"]})
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    check("客户端目录不存在 → 退回 cocraft 分类",
          r["source"] == "cocraft" and r["root"].replace("\\", "/").endswith(
              "runtime/projects/claude-node"), r)


def t_override(tmp, home, cocraft):
    """`_project_roots.json` 覆盖：agents（最高） / workflow / cocraft 父目录。"""
    custom = os.path.join(tmp, "custom-codex")
    wf = os.path.join(tmp, "custom-workflows")
    cocraft_base = os.path.join(tmp, "custom-projects")
    os.makedirs(PR.STATE_DIR, exist_ok=True)
    with open(PR.OVERRIDE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"agents": {"codex-node": custom}, "workflow": wf, "cocraft": cocraft_base}, fh)

    r = PR.resolve("session", "codex-node", {"command": ["node"], "env": {"CODEX_HOME": home}})
    check("agents 覆盖优先级最高（即使有 CODEX_HOME）",
          r["root"] == os.path.abspath(custom) and r["source"] == "override", r)

    r = PR.resolve("session", "dsh", None)
    check("cocraft 父目录可覆盖", r["root"] == os.path.join(cocraft_base, "dsh"), r)

    r = PR.resolve("workflow", "", None)
    check("workflow 目录可覆盖", r["root"] == os.path.abspath(wf) and r["source"] == "workflow", r)

    os.remove(PR.OVERRIDE_FILE)


def main() -> int:
    print("=== 新建项目默认目录（agent 客户端目录 / cocraft 分类 / 工作流大文件夹）===")
    tmp, home, cocraft = prep()
    t_kinds()
    t_session(tmp, home, cocraft)
    t_no_home(tmp)
    t_override(tmp, home, cocraft)
    bad = [n for n, ok in RESULTS if not ok]
    print("\n  total %d, failed %d" % (len(RESULTS), len(bad)))
    for n in bad:
        print("  [BAD] " + n)
    print("  result: " + ("PASS" if not bad else "FAIL"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
