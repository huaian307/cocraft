# -*- coding: utf-8 -*-
r"""无头回归：面板内「安装 Agent」（`backend/agent_install.py`）。

**完全离线**：用**本地假 npm 包**（一个带 `bin` 的小目录）走真实 `npm install`，
验证整条链：**装 → 认入口 → 注册 → 卸载**（不需要网络、不碰真实注册表）。

跑法：`python tools\agent_install_test.py`
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import agent_install as ai               # noqa: E402
from engines.acp import agents as ag     # noqa: E402

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


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cocraft-ai-")
    root = os.path.join(tmp, "agents-npm")
    fake = os.path.join(tmp, "fake-acp-pkg")
    os.makedirs(os.path.join(fake, "dist"), exist_ok=True)
    with io.open(os.path.join(fake, "package.json"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"name": "fake-acp", "version": "1.0.0",
                             "bin": {"fake-acp": "dist/index.js"}}, indent=1))
    with io.open(os.path.join(fake, "dist", "index.js"), "w", encoding="utf-8") as fh:
        fh.write("// fake ACP adapter for tests\n")

    # ★ 隔离：安装目录 + 注册表都进临时区（绝不碰真实的 runtime/state 与 agents/）
    ai.INSTALL_ROOT = root
    mem = {"agents": [{"id": "keep-me", "label": "占位", "command": ["cmd.exe"],
                       "cwd": "", "env": {}, "note": "", "provider": ""}],
           "baseline": {}, "active": "keep-me"}

    def _load():
        return json.loads(json.dumps(mem)) if mem else {"agents": [], "baseline": {}}

    def _save(d):
        mem.clear()
        mem.update(json.loads(json.dumps(d)))

    ag.load_registry = _load
    ag.save_registry = _save

    try:
        print("== 1. 目录 / 就绪判断 ==")
        cats = ai.catalog()
        ok("目录里有 3 个可装的 agent", len(cats) == 3, "%r" % ([c["id"] for c in cats],))
        node, npm = ai.node_and_npm()
        ok("找得到 node + 自带的 npm-cli.js", bool(node) and bool(npm), "%s | %s" % (node, npm))
        ok("每项都报 ready=True（这台机器能装）", all(c["ready"] for c in cats))
        ok("装之前 installed=False", all(not c["installed"] for c in cats))

        print("\n== 2. 真跑一次 npm install（本地包，离线）==")
        r = ai.install("fake-acp", pkg=fake)
        ok("install() 立刻返回（不等安装）", r.get("ok") is True and r.get("id") == "fake-acp")
        t0 = time.time()
        st = {}
        while time.time() - t0 < 180:
            st = ai.status()
            if not st.get("busy"):
                break
            time.sleep(0.3)
        ok("安装任务跑完了（不忙）", not st.get("busy"), "phase=%s" % st.get("phase"))
        ok("phase=完成 且 ok=True", st.get("phase") == "完成" and st.get("ok") is True,
           "err=%s" % (st.get("error") or "")[:200])
        cmd = st.get("command") or []
        ok("认出了入口（node + …/dist/index.js）",
           len(cmd) == 2 and cmd[0].lower().endswith("node.exe")
           and cmd[1].replace("/", "\\").lower().endswith("dist\\index.js"), "%r" % (cmd,))
        ok("装到了隔离目录里", root.lower() in " ".join(cmd).lower(), root)
        ok("文件真在盘上", os.path.isfile(os.path.join(root, "fake-acp", "node_modules",
                                                      "fake-acp", "dist", "index.js")))

        print("\n== 3. 注册进了 agentlist ==")
        reg = ai.agents.load_registry()
        hit = [a for a in (reg.get("agents") or []) if "fake-acp" in " ".join(a.get("command") or [])]
        ok("注册表里多了一条", len(hit) == 1, "%r" % ([a.get("id") for a in reg.get("agents") or []],))
        ok("注册的 id 和 status 对得上", (hit and str(hit[0].get("id")) == str(st.get("agent"))),
           "%r vs %r" % (hit and hit[0].get("id"), st.get("agent")))
        ent = next((c for c in ai.catalog() if c["id"] == "fake-acp"), None) or \
            ai.entry_for({"id": "fake-acp", "label": "fake"})
        ok("装完再看目录项：installed=True 且指回注册的 agent",
           ent.get("installed") is True and bool(ent.get("registered")), "%r" % (ent,))

        print("\n== 4. 版本 / 有没有新版（本地读 + 离线桩）==")
        ok("installed_version 读得到（1.0.0）",
           ai.installed_version({"id": "fake-acp", "pkg": "fake-acp"}) == "1.0.0",
           repr(ai.installed_version({"id": "fake-acp", "pkg": "fake-acp"})))
        ok("_pkg_of_dest 认得包名", ai._pkg_of_dest(os.path.join(root, "fake-acp")) == "fake-acp")
        ok("同版本不算有新版", ai.has_update("1.2.3", "1.2.3") is False)
        ok("更高版本算有新版", ai.has_update("1.2.3", "1.3.0") is True)
        ok("v 前缀不影响比较", ai.has_update("v2.2.2", "2.2.2") is False)
        ok("预发布低于同号正式版", ai.has_update("1.2.3-beta.1", "1.2.3") is True)
        ok("拿不到最新版就不提示", ai.has_update("1.2.3", "") is False)
        ok("更新目标：普通包拼 @latest", ai._update_target("fake-acp") == "fake-acp@latest")
        ok("更新目标：本地目录不拼", ai._update_target(fake) == fake)
        real_latest = ai._latest_version
        try:
            ai._latest_version = lambda pkg, timeout=6.0, fetch=None: "9.9.9"
            vv = ai.versions(force=True)
            one = next((x for x in (vv.get("items") or []) if x["id"] == "fake-acp"), None) or {}
            ok("versions() 标出有新版（9.9.9）",
               one.get("hasUpdate") is True and one.get("latest") == "9.9.9", "%r" % (one,))
            ok("catalog() 也跟着显示最新版",
               (next((c for c in ai.catalog() if c["id"] == "fake-acp"), {}) or {}).get("hasUpdate") is True)
            ok("第二次问走缓存（cached=True）", ai.versions().get("cached") is True)
        finally:
            ai._latest_version = real_latest

        print("\n== 5. 更新到最新（本地包，离线）==")
        with io.open(os.path.join(fake, "package.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"name": "fake-acp", "version": "2.0.0",
                                 "bin": {"fake-acp": "dist/index.js"}}, indent=1))
        u = ai.update("fake-acp", pkg=fake)
        ok("update() 立刻返回（异步）", u.get("ok") is True and u.get("kind") == "update")
        t0 = time.time()
        st2 = {}
        while time.time() - t0 < 180:
            st2 = ai.status()
            if not st2.get("busy"):
                break
            time.sleep(0.3)
        ok("更新任务跑完且 ok", st2.get("phase") == "完成" and st2.get("ok") is True,
           "err=%s" % (st2.get("error") or "")[:200])
        ok("更新后版本是 2.0.0", st2.get("version") == "2.0.0", repr(st2.get("version")))
        ok("任务类型是 update", st2.get("kind") == "update")
        reg2 = ai.agents.load_registry()
        hits2 = [a for a in (reg2.get("agents") or [])
                 if root.lower() in " ".join(a.get("command") or []).lower()]
        ok("注册表里仍然只有一条（没有重复注册）", len(hits2) == 1,
           "%r" % ([a.get("id") for a in reg2.get("agents") or []],))
        ok("命令还指向安装目录",
           bool(hits2) and root.lower() in " ".join(hits2[0].get("command") or []).lower())
        try:
            ai.update("nope-not-installed")
            ok("没装的不能更新", False)
        except RuntimeError:
            ok("没装的不能更新", True)

        print("\n== 6. 卸载（注册表 + 目录）==")
        u = ai.uninstall("fake-acp")
        ok("卸载返回 ok", u.get("ok") is True, "%r" % (u,))
        left = ai.agents.load_registry().get("agents") or []
        ok("注册表里只剩占位那条（没把别的 agent 一起删）",
           len(left) == 1 and left[0].get("id") == "keep-me",
           "%r" % ([a.get("id") for a in left],))
        ok("安装目录也删了", not os.path.isdir(os.path.join(root, "fake-acp")))

        print("\n== 7. 拒绝非法输入 ==")
        for bad_id in ("../../etc", "a b", ""):
            try:
                ai._dest(bad_id)
                ok("非法 id 被拒：%r" % bad_id, False)
            except ValueError:
                ok("非法 id 被拒：%r" % (bad_id or "(空)"), True)
        try:
            ai.install("not a package name!!", pkg="not a package name!!")
            ok("不像 npm 包名的被拒", False)
        except ValueError:
            ok("不像 npm 包名的被拒", True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    real = os.path.join(ag.ROOT_DIR, "agents", "npm")
    print("\n== 8. 没污染真实目录 ==")
    ok("真实 agents/npm 里没有测试留下的东西",
       not os.path.isdir(real) or not any("fake-acp" in x for x in os.listdir(real)))

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
