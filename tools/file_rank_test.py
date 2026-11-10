# -*- coding: utf-8 -*-
r"""文件重要性评级（`backend/file_rank.py`）离线回归。

覆盖：
  * 星级映射；
  * ★ 级（构建产物 / 缓存 / 日志 / 临时）能自动隔离放行；
  * 源码 / 配置 / 文档 / git 跟踪 / 区外 / .git / runtime/state / 密钥 **一律不放行**；
  * 关机、杀进程、格式化、注册表、移动、复制 **一律不放行**（没有可恢复的"原件"）；
  * 范围解析不出（unresolved）→ 不放行；
  * `max_star=2` 只多放行"2 星"，不放行 3 星。

不联网、只用临时目录；需要 git 的用例在没装 git 时自动跳过。
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import file_rank as fr  # noqa: E402

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


def touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def ok(root, cmd, max_star=1):
    return bool(fr.triage(root, cmd, max_star=max_star).get("ok"))


def reason(root, cmd, max_star=1):
    return fr.triage(root, cmd, max_star=max_star).get("reason") or ""


def main():
    base = tempfile.mkdtemp(prefix="cocraft-rank-")
    try:
        # ---------- 星级映射 ----------
        for score, star in [(0, 1), (20, 1), (21, 2), (40, 2), (41, 3),
                            (60, 3), (61, 4), (80, 4), (81, 5), (100, 5)]:
            check("stars_of(%d)=%d" % (score, star), fr.stars_of(score) == star)

        # ---------- 工作区 ----------
        root = os.path.join(base, "ws")
        os.makedirs(root)
        touch(os.path.join(root, "node_modules", "pkg", "index.js"))
        touch(os.path.join(root, "dist", "bundle.js"))
        touch(os.path.join(root, "__pycache__", "m.pyc"))
        touch(os.path.join(root, "debug.log"))
        touch(os.path.join(root, "temp", "a.tmp"))
        touch(os.path.join(root, "backend", "app.py"))
        touch(os.path.join(root, "frontend", "main.js"))
        touch(os.path.join(root, "docs", "guide.md"))
        touch(os.path.join(root, "README.md"))
        touch(os.path.join(root, "package.json"))
        touch(os.path.join(root, "data.csv"))
        touch(os.path.join(root, "runtime", "state", "_engine.json"))
        touch(os.path.join(root, ".git", "config"))
        touch(os.path.join(root, "_credentials.json"))
        touch(os.path.join(root, "key.pem"))
        os.makedirs(os.path.join(root, "empty_dir"))
        os.makedirs(os.path.join(root, "src"))
        touch(os.path.join(root, "src", "core.py"))

        # ---------- 能自动放行（★） ----------
        for cmd in ["rm -rf node_modules",
                    "rm -rf dist",
                    "rm -rf __pycache__",
                    "Remove-Item -Recurse -Force .\\__pycache__",
                    "del /f /q debug.log",
                    "rm -rf temp",
                    "rm -rf empty_dir"]:
            check("放行: %s" % cmd, ok(root, cmd), reason(root, cmd))

        # ---------- 不放行：源码 / 配置 / 文档 / 数据 ----------
        for cmd in ["rm -rf backend/app.py",
                    "rm -rf frontend",
                    "rm -rf docs",
                    "rm -rf README.md",
                    "rm -rf package.json",
                    "rm -rf data.csv",
                    "rm -rf src"]:
            check("拦住: %s" % cmd, not ok(root, cmd), reason(root, cmd))

        # ---------- 硬禁止 ----------
        for cmd in ["rm -rf .",
                    "rm -rf .git",
                    "rm -rf runtime/state",
                    "rm -rf _credentials.json",
                    "rm -rf key.pem",
                    "rm -rf ..\\outside",
                    "rm -rf C:\\Windows"]:
            check("硬禁止: %s" % cmd, not ok(root, cmd), reason(root, cmd))

        # ---------- 非删除 / 其它破坏性 ----------
        for cmd in ["shutdown /s /t 0",
                    "taskkill /F /IM node.exe",
                    "Stop-Process -Name node",
                    "format D: /q",
                    "diskpart",
                    "reg delete HKLM\\Software\\X /f",
                    "Move-Item a.txt b.txt",
                    "rename-item a b",
                    "Copy-Item a b",
                    "Set-Content a.txt hello",
                    "git clean -fdx"]:
            check("不是安全删除: %s" % cmd, not ok(root, cmd), reason(root, cmd))

        # ---------- 复合命令 ----------
        for cmd in ["rm -rf dist && shutdown /s",
                    "rm -rf node_modules; taskkill /F /IM node.exe",
                    "del a.log & format D:"]:
            check("复合命令拦住: %s" % cmd, not ok(root, cmd), reason(root, cmd))

        # ---------- 范围不确定 ----------
        check("解析不出目标 → 拦", not ok(root, "rm -rf"))
        check("目标不存在 → 拦", not ok(root, "rm -rf does-not-exist-xyz"))
        check("空命令 → 拦", not ok(root, ""))

        # ---------- 阈值 ----------
        # data.csv 是 3 星：max_star=2 仍拦，放宽到 3 才放（证明阈值真的生效）
        check("★2 阈值仍拦 3 星", not ok(root, "rm -rf data.csv", max_star=2),
              reason(root, "rm -rf data.csv", max_star=2))
        check("★3 阈值放 3 星", ok(root, "rm -rf data.csv", max_star=3),
              reason(root, "rm -rf data.csv", max_star=3))
        # 日志是 1 星：两级阈值都放
        check("★1 阈值放 1 星", ok(root, "rm -rf debug.log", max_star=1))
        check("★2 阈值放 1 星", ok(root, "rm -rf debug.log", max_star=2))

        # ---------- 单个评分可解释 ----------
        d = fr.describe(os.path.join(root, "backend", "app.py"), root)
        check("describe: 源码 >= 4 星", int(d.get("stars") or 0) >= 4, d)
        check("describe: 有 reasons", bool(d.get("reasons")), d)
        d2 = fr.describe(os.path.join(root, "node_modules"), root)
        check("describe: node_modules = 1 星", int(d2.get("stars") or 0) == 1, d2)

        # ---------- git 跟踪（没装 git 就跳过） ----------
        try:
            g = subprocess.run(["git", "--version"], capture_output=True, timeout=5)
            have_git = g.returncode == 0
        except Exception:  # noqa: BLE001
            have_git = False
        if have_git:
            repo = os.path.join(base, "repo")
            os.makedirs(repo)
            touch(os.path.join(repo, ".gitignore"), "cache/\n*.log\n")
            touch(os.path.join(repo, "dist", "keep.js"), "// committed artifact\n")
            touch(os.path.join(repo, "cache", "tmp.bin"), "cache\n")
            touch(os.path.join(repo, "debug.log"), "log\n")
            touch(os.path.join(repo, "src", "main.py"), "print(1)\n")
            subprocess.run(["git", "init", "-q"], cwd=repo, timeout=10)
            subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, timeout=10)
            subprocess.run(["git", "config", "user.name", "t"], cwd=repo, timeout=10)
            subprocess.run(["git", "add", "-A"], cwd=repo, timeout=10)
            subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, timeout=20)
            check("git: 被跟踪的 dist 不放行", not ok(repo, "rm -rf dist"),
                  reason(repo, "rm -rf dist"))
            check("git: 被 ignore 的缓存仍放行", ok(repo, "rm -rf cache"),
                  reason(repo, "rm -rf cache"))
            check("git: 被 ignore 的日志仍放行", ok(repo, "rm -rf debug.log"),
                  reason(repo, "rm -rf debug.log"))
            check("git: 跟踪的源码不放行", not ok(repo, "rm -rf src"),
                  reason(repo, "rm -rf src"))
            st = fr.git_state(repo, [os.path.join(repo, "src", "main.py")])
            check("git_state: tracked=True", bool(st and st[0].get("tracked")), st)
            st2 = fr.git_state(repo, [os.path.join(repo, "debug.log")])
            check("git_state: ignored=True", bool(st2 and st2[0].get("ignored")), st2)
        else:
            print("SKIP  git 未安装 —— 跳过 5 条跟踪/忽略用例")
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
