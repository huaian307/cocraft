# -*- coding: utf-8 -*-
r"""Agent 更新核心回归：回滚 / 自动检查配置 / Node 运行时更新。

不联网、不真的跑 npm：
  * `agent_install` 的备份 / 回滚（目录级 rename）；
  * `auto_config` 默认值 + 落盘 + `_is_patch`；
  * `updates_summary(cached_only=True)` 不联网；
  * `node_update` 的版本比较 / LTS 选择 / 替换 / 回滚。

真实 npm 安装/更新仍由 `tools/agent_install_test.py` 覆盖（本机跑）。
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import agent_install as ai          # noqa: E402
import node_update as nu            # noqa: E402

N_PASS = 0
N_FAIL = 0


def check(name, cond, extra=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("PASS  %s" % name)
    else:
        N_FAIL += 1
        print("FAIL  %s%s" % (name, ("  | " + str(extra)) if extra else ""))


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def main():
    # ---------------- auto config ----------------
    old_auto = ai.AUTO_FILE
    tmp = tempfile.mkdtemp(prefix="cocraft-agentcore-")
    try:
        ai.AUTO_FILE = os.path.join(tmp, "_agent_auto.json")
        cfg = ai.auto_config()
        check("自动检查默认开", cfg.get("check") is True, cfg)
        check("自动更新默认关", cfg.get("autoInstall") is False, cfg)
        check("默认只装 patch", cfg.get("channel") == "patch", cfg)
        cfg = ai.set_auto_config(autoInstall=True)
        check("自动更新可打开", cfg.get("autoInstall") is True, cfg)
        check("配置落盘", os.path.isfile(ai.AUTO_FILE))
        cfg = ai.set_auto_config(autoInstall=False)
        check("自动更新可关闭", cfg.get("autoInstall") is False, cfg)

        # ---------------- patch 判断 ----------------
        check("_is_patch 1.2.3→1.2.4", ai._is_patch("1.2.3", "1.2.4") is True)
        check("_is_patch 1.2.3→1.3.0 不算 patch", ai._is_patch("1.2.3", "1.3.0") is False)
        check("_is_patch 1.2.3→2.0.0 不算 patch", ai._is_patch("1.2.3", "2.0.0") is False)

        # ---------------- cached_only 不联网 ----------------
        summ = ai.updates_summary(cached_only=True)
        check("updates_summary 结构", isinstance(summ, dict) and "items" in summ and "auto" in summ, summ)
        check("cached_only 是 list", isinstance(summ.get("items"), list))

        # ---------------- 备份 / 恢复 ----------------
        dest = os.path.join(tmp, "agents", "npm", "demo")
        write(os.path.join(dest, "package.json"), '{"dependencies":{"demo-pkg":"1.0.0"}}')
        write(os.path.join(dest, "node_modules", "demo-pkg", "package.json"), '{"version":"1.0.0"}')
        bak = ai._backup_dest(dest)
        check("备份后原目录不在", not os.path.isdir(dest), bak)
        check("备份目录存在", bool(bak) and os.path.isdir(bak), bak)
        check("能找回最近备份", ai._latest_backup(dest) == bak, ai._latest_backup(dest))
        write(os.path.join(dest, "package.json"), '{"dependencies":{"demo-pkg":"2.0.0"}}')
        write(os.path.join(dest, "node_modules", "demo-pkg", "package.json"), '{"version":"2.0.0"}')
        ai._restore_backup(dest, bak)
        with open(os.path.join(dest, "node_modules", "demo-pkg", "package.json"), encoding="utf-8") as fh:
            v = json.load(fh).get("version")
        check("恢复后回到 1.0.0", v == "1.0.0", v)

        # ---------------- 完整 rollback（不碰真实注册表） ----------------
        old_root = ai.INSTALL_ROOT
        ai.INSTALL_ROOT = os.path.join(tmp, "agents", "npm")
        aid = "demo-rollback"
        dest = os.path.join(ai.INSTALL_ROOT, aid)
        write(os.path.join(dest, "package.json"),
              '{"dependencies":{"demo-pkg":"1.0.0"},"bin":{"demo":"index.js"}}')
        write(os.path.join(dest, "index.js"), "// v1")
        write(os.path.join(dest, "node_modules", "demo-pkg", "package.json"),
              '{"name":"demo-pkg","version":"1.0.0","bin":"index.js"}')
        write(os.path.join(dest, "node_modules", "demo-pkg", "index.js"), "// v1")
        bak = ai._backup_dest(dest)
        write(os.path.join(dest, "package.json"),
              '{"dependencies":{"demo-pkg":"2.0.0"},"bin":{"demo":"index.js"}}')
        write(os.path.join(dest, "index.js"), "// v2")
        write(os.path.join(dest, "node_modules", "demo-pkg", "package.json"),
              '{"name":"demo-pkg","version":"2.0.0","bin":"index.js"}')
        write(os.path.join(dest, "node_modules", "demo-pkg", "index.js"), "// v2")
        old = (ai._release_agent, ai.node_and_npm, ai._register)
        ai._release_agent = lambda *a, **k: None
        ai.node_and_npm = lambda: (sys.executable, "")
        ai._register = lambda *a, **k: {"id": "demo-rollback"}
        try:
            res = ai.rollback(aid)
            check("rollback 返回 ok", res.get("ok") is True, res)
            check("rollback 版本回到 1.0.0", res.get("version") == "1.0.0", res)
            with open(os.path.join(dest, "node_modules", "demo-pkg", "package.json"),
                      encoding="utf-8") as fh:
                check("rollback 文件真的回来了", json.load(fh).get("version") == "1.0.0")
        finally:
            ai._release_agent, ai.node_and_npm, ai._register = old
            ai.INSTALL_ROOT = old_root

        # ---------------- node_update：版本 / LTS ----------------
        check("_ver v22.23.3", nu._ver("v22.23.3") == (22, 23, 3), nu._ver("v22.23.3"))
        fake_index = [
            {"version": "v22.23.3", "lts": "Jod", "files": ["win-x64-zip"]},
            {"version": "v24.11.0", "lts": "Krypton", "files": ["win-x64-zip"]},
            {"version": "v25.0.0", "lts": False, "files": ["win-x64-zip"]},
            {"version": "v24.12.0", "lts": "Krypton", "files": ["linux-x64"]},
        ]
        old_http = nu._http
        old_cur = nu.current
        nu._http = lambda url, timeout=20: json.dumps(fake_index).encode("utf-8")
        nu.current = lambda: {"version": "v22.23.3", "path": "x"}
        try:
            nu._CACHE.update({"ts": 0.0, "latest": "", "lts": ""})
            c = nu.check(force=True)
            check("选中最新 LTS + win-x64", c.get("latest") == "v24.11.0", c)
            check("hasUpdate 正确", c.get("hasUpdate") is True, c)
        finally:
            nu._http = old_http
            nu.current = old_cur
        try:
            nu._download_verify("not-a-version")
            check("非法 Node 版本被拒", False)
        except ValueError:
            check("非法 Node 版本被拒", True)

        # ---------------- node_update：替换 / 回滚（离线） ----------------
        old = (nu.NODE_DIR, nu.BACKUP_DIR, nu.CACHE_DIR, nu.STATE_FILE,
               nu.current, nu._kill_node_processes)
        nd = os.path.join(tmp, "agents", "node")
        bk = os.path.join(tmp, "runtime", "node-backup")
        nu.NODE_DIR, nu.BACKUP_DIR = nd, bk
        nu.CACHE_DIR = os.path.join(tmp, "runtime", "cache", "node")
        nu.STATE_FILE = os.path.join(tmp, "runtime", "state", "_node.json")

        def fake_current():
            p = os.path.join(nd, "marker")
            v = ""
            try:
                with open(p, encoding="utf-8") as fh:
                    v = fh.read().strip()
            except OSError:
                pass
            return {"version": v, "path": os.path.join(nd, "node.exe")}

        nu.current = fake_current
        nu._kill_node_processes = lambda: None
        try:
            write(os.path.join(nd, "node.exe"), "old")
            write(os.path.join(nd, "marker"), "v22.23.3")
            write(os.path.join(nd, "node_modules", "npm", "bin", "npm-cli.js"), "// npm")
            src = os.path.join(tmp, "new", "node-v24.11.0-win-x64")
            write(os.path.join(src, "node.exe"), "new")
            write(os.path.join(src, "marker"), "v24.11.0")
            write(os.path.join(src, "node_modules", "npm", "bin", "npm-cli.js"), "// npm")
            backup = nu._replace_tree(src)
            check("替换后版本 v24.11.0", fake_current()["version"] == "v24.11.0", fake_current())
            check("旧版本有备份", bool(backup) and os.path.isdir(backup), backup)
            res = nu.rollback()
            check("Node 回滚到 v22.23.3", res.get("version") == "v22.23.3", res)
        finally:
            (nu.NODE_DIR, nu.BACKUP_DIR, nu.CACHE_DIR, nu.STATE_FILE,
             nu.current, nu._kill_node_processes) = old

        # ---------------- job status / 忙碌保护 ----------------
        nu._set(running=True, phase="下载", percent=42, bytesDone=10,
                bytesTotal=100, started=time.time(), detail="x")
        st = nu.status()
        check("node status busy", st.get("busy") is True, st)
        check("node status percent", st.get("percent") == 42, st)
        check("node status elapsed", isinstance(st.get("elapsed"), (int, float)), st)
        try:
            nu.apply_async("v1.2.3")
            check("忙碌时拒绝新任务", False)
        except RuntimeError:
            check("忙碌时拒绝新任务", True)
        finally:
            nu._set(running=False, started=0.0, percent=None)
    finally:
        ai.AUTO_FILE = old_auto
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
