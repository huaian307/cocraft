# -*- coding: utf-8 -*-
r"""无头回归：★ACP agent 安全兜底（dsh 不能静默回到 danger-full-access）。

背景（2026-10-19）：dsh 的 `DSH_PERMISSION_MODE` 曾回退成 `danger-full-access` ——
那种模式下它**不请求权限**，面板等级 / 群审批全部失效。旧的 `_acp.json` 里也一直留着这个值，
注册表一旦重建就会把 dsh 连同「全权限」收回来。

本测试**不碰真实注册表**（全部指到临时目录），验证三层兜底：

  1. `load_registry()`：读到危险值 → 就地改回 `workspace-write` 并落盘（保留 API Key）
  2. `update_agent()`：接口写入危险值 → 也不会真的存进去
  3. `sanitize_agent_env()`：拼 env 前再兜一次；非 dsh 的 agent 不动
  4. `public()`：`dangerous` / `dangerousNote` 标记正确
  5. **显式放行**（`_security.json` 或 env）→ 危险值保留（用户明确知道自己在干什么）
  6. 旧 `_acp.json` bootstrap → dsh 仍是 `workspace-write`
  7. 群聊 `guard_note()`：默认静默；放行后给出警告

跑法：python tools\agent_safety_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(HERE, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import engines.acp.agents as agents  # noqa: E402

TMP = tempfile.mkdtemp(prefix="agent-safety-")
agents.STATE_DIR = TMP
agents.ROOT_DIR = TMP
agents.AGENTS_FILE = os.path.join(TMP, "_acp_agents.json")
agents.LEGACY_FILE = os.path.join(TMP, "_acp.json")
agents.SECURITY_FILE = os.path.join(TMP, "_security.json")
os.environ.pop(agents.ALLOW_DANGER_ENV, None)

PASS = 0
FAIL = 0
BAD = []


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s%s" % (label, (" " + extra) if extra else ""))
    else:
        FAIL += 1
        BAD.append(label)
        print("  [BAD]  %s%s" % (label, (" " + extra) if extra else ""))


def write(path, data):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def read(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def reset():
    for p in (agents.AGENTS_FILE, agents.AGENTS_FILE + ".bak", agents.SECURITY_FILE):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass
    os.environ.pop(agents.ALLOW_DANGER_ENV, None)


def main() -> int:
    try:
        print("== 1. load_registry：危险值就地纠正并落盘 ==")
        reset()
        write(agents.AGENTS_FILE, {
            "active": "dsh",
            "agents": [
                {"id": "dsh", "label": "dsh", "command": ["x"],
                 "env": {"DEEPSEEK_API_KEY": "sk-test",
                         "DSH_PERMISSION_MODE": "danger-full-access"}},
                {"id": "other", "label": "other", "command": ["x"],
                 "env": {"SOME_PERMISSION_MODE": "danger-full-access"}},
            ],
            "baseline": {"env": {}},
        })
        reg = agents.load_registry()
        dsh = agents.find_agent(reg, "dsh")
        other = agents.find_agent(reg, "other")
        ok("dsh 被改回 workspace-write", (dsh.get("env") or {}).get("DSH_PERMISSION_MODE") == "workspace-write",
           repr(dsh.get("env")))
        ok("API Key 保留", (dsh.get("env") or {}).get("DEEPSEEK_API_KEY") == "sk-test")
        disk = agents.find_agent(read(agents.AGENTS_FILE), "dsh")
        ok("磁盘也改了（不是只在内存里）",
           (disk.get("env") or {}).get("DSH_PERMISSION_MODE") == "workspace-write", repr(disk.get("env")))
        ok("别的 agent 不瞎动", (other.get("env") or {}).get("SOME_PERMISSION_MODE") == "danger-full-access",
           repr(other.get("env")))

        print("\n== 2. update_agent：写入危险值也存不进去 ==")
        upd = agents.update_agent("dsh", {"env": {"DEEPSEEK_API_KEY": "sk-2",
                                                  "DSH_PERMISSION_MODE": "danger-full-access"}})
        ok("update_agent 返回安全值", (upd.get("env") or {}).get("DSH_PERMISSION_MODE") == "workspace-write",
           repr(upd.get("env")))
        disk = agents.find_agent(read(agents.AGENTS_FILE), "dsh")
        ok("update_agent 落盘也是安全值",
           (disk.get("env") or {}).get("DSH_PERMISSION_MODE") == "workspace-write", repr(disk.get("env")))
        ok("新 Key 没丢", (disk.get("env") or {}).get("DEEPSEEK_API_KEY") == "sk-2")

        print("\n== 3. sanitize_agent_env：拼 env 前再兜一次 ==")
        env, fixes = agents.sanitize_agent_env("dsh", {"DSH_PERMISSION_MODE": "yolo"})
        ok("拼 env 时 danger/yolo 都被纠正", env.get("DSH_PERMISSION_MODE") == "workspace-write"
           and bool(fixes), repr(env))
        env2, fixes2 = agents.sanitize_agent_env("dsh", {})
        ok("没写这个键 → 显式补上安全值", env2.get("DSH_PERMISSION_MODE") == "workspace-write"
           and bool(fixes2), repr(env2))
        env3, fixes3 = agents.sanitize_agent_env("dsh", {"DSH_PERMISSION_MODE": "read-only"})
        ok("已经是更安全的 read-only → 不覆盖", env3.get("DSH_PERMISSION_MODE") == "read-only"
           and not fixes3, repr(env3))
        env4, _ = agents.sanitize_agent_env("codex-node", {"SOME_PERMISSION_MODE": "danger-full-access"})
        ok("别的 agent 不走 dsh 规则", env4.get("SOME_PERMISSION_MODE") == "danger-full-access", repr(env4))

        print("\n== 4. public：给前端红字 ==")
        pub_ok = agents.public(dsh)
        ok("正常 dsh 不报危险", pub_ok.get("dangerous") is False and not pub_ok.get("dangerousNote"),
           repr((pub_ok.get("dangerous"), pub_ok.get("dangerousNote"))))
        pub_bad = agents.public({"id": "dsh", "command": ["x"],
                                 "env": {"DSH_PERMISSION_MODE": "danger-full-access"}})
        ok("危险 env → dangerous=True + 人话", pub_bad.get("dangerous") is True
           and "不请求权限" in str(pub_bad.get("dangerousNote")), repr(pub_bad.get("dangerousNote")))

        print("\n== 5. 显式放行：允许用户自己承担风险 ==")
        write(agents.SECURITY_FILE, {"allowDangerousAgents": True})
        ok("allow_dangerous_agents() 读到放行文件", agents.allow_dangerous_agents() is True)
        env5, fixes5 = agents.sanitize_agent_env("dsh", {"DSH_PERMISSION_MODE": "danger-full-access"})
        ok("放行后不强制改", env5.get("DSH_PERMISSION_MODE") == "danger-full-access" and not fixes5,
           repr(env5))
        write(agents.AGENTS_FILE, {"active": "dsh", "agents": [
            {"id": "dsh", "label": "dsh", "command": ["x"],
             "env": {"DEEPSEEK_PERMISSION_MODE": "danger-full-access",
                     "DSH_PERMISSION_MODE": "danger-full-access"}}]})
        reg2 = agents.load_registry()
        dsh2 = agents.find_agent(reg2, "dsh")
        ok("放行后 load_registry 也保留（用户明确开了）",
           (dsh2.get("env") or {}).get("DSH_PERMISSION_MODE") == "danger-full-access", repr(dsh2.get("env")))
        ok("public 仍然标危险（放行 ≠ 看不见）", agents.public(dsh2).get("dangerous") is True)
        try:
            import cocraft_agents as ca
            note = ca.guard_note("dsh")
            ok("群聊 guard_note 会报警", "不请求权限" in note, repr(note))
        except Exception as exc:  # noqa: BLE001
            ok("群聊 guard_note 会报警", False, repr(exc))
        os.remove(agents.SECURITY_FILE)

        print("\n== 6. 旧 _acp.json bootstrap：不会把全权限带回来 ==")
        reset()
        write(agents.LEGACY_FILE, {
            "command": ["node", "dsh.js"], "cwd": TMP,
            "env": {"DEEPSEEK_API_KEY": "sk-legacy",
                    "DSH_PERMISSION_MODE": "danger-full-access"},
        })
        # 隔离本机探测（否则 bootstrap 会去扫真实目录，慢且不稳定）
        orig_autofill = agents.autofill
        orig_detect = agents.detect_all_cached
        orig_ensure = getattr(agents, "ensure_opencode_agent", None)
        agents.autofill = lambda reg, **kw: {"registry": reg, "changed": []}
        agents.detect_all_cached = lambda: {}
        if orig_ensure is not None:
            agents.ensure_opencode_agent = lambda: None
        try:
            reg3 = agents.load_registry()
            dsh3 = agents.find_agent(reg3, "dsh")
            ok("bootstrap 后 dsh 是 workspace-write",
               (dsh3.get("env") or {}).get("DSH_PERMISSION_MODE") == "workspace-write",
               repr(dsh3.get("env")))
            ok("bootstrap 保留 Key", (dsh3.get("env") or {}).get("DEEPSEEK_API_KEY") == "sk-legacy")
        finally:
            agents.autofill = orig_autofill
            agents.detect_all_cached = orig_detect
            if orig_ensure is not None:
                agents.ensure_opencode_agent = orig_ensure

        print("\n== 7. 默认不放行 ==")
        os.environ.pop(agents.ALLOW_DANGER_ENV, None)
        ok("默认 allow_dangerous_agents=False", agents.allow_dangerous_agents() is False)
        env6, _ = agents.sanitize_agent_env("dsh", {"DSH_PERMISSION_MODE": "danger-full-access"})
        ok("默认一定纠正", env6.get("DSH_PERMISSION_MODE") == "workspace-write", repr(env6))
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
