#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""isolation_test.py -- 工作流并行隔离（P0-1）回归。

覆盖：
  1. git 仓库 → 每个成员一个 **worktree**（独立目录 + 分支），互不覆盖
  2. 应用回主目录：写入 / 新增 / 删除（先隔离旧版）
  3. **冲突**：主目录在运行期间被改过 → 默认拒绝，`force=True` 才覆盖
  4. 非 git 目录 → **影子副本**（真复制，不是硬链接；主目录在应用前不被改动）
  5. 目录太大 / 只有一个成员 → `mode=off` + 明确原因（不假装隔离了）
  6. 清理：worktree / 影子目录 / 分支都删干净

全程临时目录，不碰真实运行态，不联网。
"""
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import isolation as I  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("[OK]  " if cond else "[BAD] ") + name + (("   " + str(detail)) if (detail and not cond) else ""))


def skip(name, why):
    print("[SKIP] " + name + " —— " + why)


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd] + list(args),
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode


def make_repo(tmp):
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "src"))
    open(os.path.join(repo, "src", "app.txt"), "w", encoding="utf-8").write("v1\n")
    open(os.path.join(repo, "README.md"), "w", encoding="utf-8").write("readme\n")
    git(repo, "init")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "tester")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "init")
    return repo


def t_worktree(tmp):
    repo = make_repo(tmp)
    if not I.toplevel(repo):
        skip("git worktree 隔离", "本机没有 git")
        return
    plan = I.prepare(repo, "run_wt", ["m1", "m2", "m3"])
    check("git 仓库 → mode=worktree", plan.get("mode") == "worktree", plan.get("reason"))
    cwds = [I.member_cwd(plan, x) for x in ("m1", "m2", "m3")]
    check("每个成员一个独立目录", len(set(os.path.normcase(c) for c in cwds)) == 3, cwds)
    check("三个工作区都真的存在", all(os.path.isdir(c) for c in cwds), cwds)

    # m1 / m2 改同一个文件；m1 还新增一个
    open(os.path.join(cwds[0], "src", "app.txt"), "w", encoding="utf-8").write("m1\n")
    open(os.path.join(cwds[1], "src", "app.txt"), "w", encoding="utf-8").write("m2\n")
    open(os.path.join(cwds[0], "src", "new.txt"), "w", encoding="utf-8").write("n\n")

    main_app = open(os.path.join(repo, "src", "app.txt"), encoding="utf-8").read().strip()
    check("成员改动不会直接写主目录", main_app == "v1", main_app)

    ch1 = I.changes(plan, "m1")
    ch2 = I.changes(plan, "m2")
    check("m1 改动统计（1 改 + 1 增）",
          ch1.get("files") == 2 and len(ch1.get("changed") or []) == 1
          and len(ch1.get("added") or []) == 1, ch1)
    check("m2 改动统计（1 改）", ch2.get("files") == 1, ch2)

    res = I.apply_member(plan, "m1")
    check("应用 m1 → ok", res.get("ok") is True and not res.get("conflicts"), res)
    check("主目录拿到 m1 的内容",
          open(os.path.join(repo, "src", "app.txt"), encoding="utf-8").read().strip() == "m1")
    check("m1 新增的文件也过去了", os.path.isfile(os.path.join(repo, "src", "new.txt")))

    # 冲突：主目录又改了一次
    open(os.path.join(repo, "src", "app.txt"), "w", encoding="utf-8").write("main-changed\n")
    bad = I.apply_member(plan, "m2")
    check("主目录被改过 → 默认拒绝（返回 conflicts）",
          bad.get("ok") is False and bad.get("conflicts"), bad)
    check("拒绝时不写主目录",
          open(os.path.join(repo, "src", "app.txt"), encoding="utf-8").read().strip() == "main-changed")
    forced = I.apply_member(plan, "m2", force=True)
    check("force=True → 覆盖成功", forced.get("ok") is True, forced)
    check("force 后主目录 = m2",
          open(os.path.join(repo, "src", "app.txt"), encoding="utf-8").read().strip() == "m2")

    # 没有改动的成员
    clean = I.apply_member(plan, "m3")
    check("没改动的成员应用 → 0 个文件", clean.get("ok") is True
          and not clean.get("applied") and not clean.get("deleted"), clean)

    # 清理
    I.cleanup("run_wt")
    check("清理后隔离目录消失", not os.path.isdir(os.path.join(I.ISO_ROOT, "run_wt")))
    left = subprocess.run(["git", "-C", repo, "worktree", "list"],
                          capture_output=True).stdout.decode("utf-8", "replace")
    check("清理后只剩主工作区（没有残留 worktree）", len([l for l in left.splitlines() if l.strip()]) == 1, left)
    branches = subprocess.run(["git", "-C", repo, "branch"],
                              capture_output=True).stdout.decode("utf-8", "replace")
    check("清理后没有 cocraft 分支残留", "cocraft/" not in branches, branches)


def t_shadow(tmp):
    plain = os.path.join(tmp, "plain")
    os.makedirs(os.path.join(plain, "d"))
    open(os.path.join(plain, "d", "x.txt"), "w", encoding="utf-8").write("original\n")
    plan = I.prepare(plain, "run_sh", ["a", "b"])
    if plan.get("mode") != "shadow":
        skip("影子副本隔离", "这个目录被识别成了 git 仓库")
        return
    check("非 git 目录 → mode=shadow", plan.get("mode") == "shadow", plan.get("reason"))
    ca = I.member_cwd(plan, "a")
    cb = I.member_cwd(plan, "b")
    check("影子副本是两个不同目录", os.path.normcase(ca) != os.path.normcase(cb))
    open(os.path.join(ca, "d", "x.txt"), "w", encoding="utf-8").write("a-version\n")
    check("影子副本里写文件不影响主目录（不是硬链接）",
          open(os.path.join(plain, "d", "x.txt"), encoding="utf-8").read().strip() == "original",
          open(os.path.join(plain, "d", "x.txt"), encoding="utf-8").read()[:20])
    ch = I.changes(plan, "a")
    check("影子模式能算出改动", ch.get("files") == 1, ch)
    res = I.apply_member(plan, "a")
    check("影子模式应用回主目录",
          res.get("ok") is True
          and open(os.path.join(plain, "d", "x.txt"), encoding="utf-8").read().strip() == "a-version", res)
    I.cleanup("run_sh")
    check("影子目录清理干净", not os.path.isdir(os.path.join(I.ISO_ROOT, "run_sh")))


def t_subdir(tmp):
    """工作目录是仓库**子目录** → v1 不用 worktree（git status 路径相对仓库根），退回影子副本。"""
    repo = make_repo(os.path.join(tmp, "subcase"))
    sub = os.path.join(repo, "src")
    plan = I.prepare(sub, "run_sub", ["m1", "m2"])
    check("子目录 → 退回 shadow（不硬上 worktree）", plan.get("mode") in ("shadow", "off"), plan.get("mode"))
    I.cleanup("run_sub")


def t_limits(tmp):
    # 只有一个成员 → 不隔离
    one = os.path.join(tmp, "one")
    os.makedirs(one)
    p = I.prepare(one, "run_one", ["m1"])
    check("只有一个成员 → off + 原因",
          p.get("mode") == "off" and "只有一个成员" in (p.get("reason") or ""), p.get("reason"))

    # 目录太大 → off + 原因
    big = os.path.join(tmp, "big")
    os.makedirs(big)
    for i in range(5):
        open(os.path.join(big, "f%d.txt" % i), "w", encoding="utf-8").write("x")
    old = I.MAX_SHADOW_FILES
    I.MAX_SHADOW_FILES = 2
    try:
        p2 = I.prepare(big, "run_big", ["a", "b"])
    finally:
        I.MAX_SHADOW_FILES = old
    check("目录太大 → off + 说明（不假装隔离）",
          p2.get("mode") == "off" and "太大" in (p2.get("reason") or ""), p2.get("reason"))

    # 关闭开关
    p3 = I.prepare(big, "run_off", ["a", "b"], enabled=False)
    check("enabled=False → off", p3.get("mode") == "off" and "关闭" in (p3.get("reason") or ""), p3.get("reason"))


def main() -> int:
    print("=== 工作流并行隔离（worktree / 影子副本 / 冲突 / 清理）===")
    tmp = tempfile.mkdtemp(prefix="iso_test_")
    I.ISO_ROOT = os.path.join(tmp, "iso")
    try:
        t_worktree(tmp)
        t_shadow(tmp)
        t_subdir(tmp)
        t_limits(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = [n for n, ok in RESULTS if not ok]
    print("\n  total %d, failed %d" % (len(RESULTS), len(bad)))
    for n in bad:
        print("  [BAD] " + n)
    print("  result: " + ("PASS" if not bad else "FAIL"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
