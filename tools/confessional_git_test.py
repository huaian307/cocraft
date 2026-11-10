# -*- coding: utf-8 -*-
r"""忏悔室 · 按 git 提交出清（`backend/confessional_git.py`）。

覆盖：
  * 已提交过的文件：修改前旧版在 git 对象库 → 提交后清掉；
  * 未跟踪文件：旧版不在 git → 保留（fail-closed）；
  * 删除的文件：旧版在 git → 删除提交后清掉；
  * 轮快照：所有内容都在 git → 清掉；
  * 首次见到项目只记基线（不突然清空）；
  * 第二次 HEAD 变化后才清。

不联网，只用临时 git 仓库。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import confessional_git as CG   # noqa: E402
import snapshots as S            # noqa: E402
from engines import base as EB   # noqa: E402

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
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def git(root, *args):
    p = subprocess.run(["git", "-C", root] + list(args), capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), p.stderr.strip()))
    return p.stdout.strip()


def commit(root, msg):
    git(root, "add", "-A")
    git(root, "commit", "-m", msg)


def main():
    if not shutil.which("git"):
        print("SKIP: 本机没有 git")
        return 0
    base = tempfile.mkdtemp(prefix="cocraft-gitclean-")
    old = (S.SNAPSHOT_DIR, S.QUARANTINE_DIR, S.RUNTIME, S.STORE_DIR, S.QUAR_CFG_FILE,
           CG.STATE_FILE)
    try:
        ws = os.path.join(base, "ws")
        os.makedirs(ws)
        git(ws, "init")
        git(ws, "config", "user.email", "t@example.com")
        git(ws, "config", "user.name", "tester")

        S.SNAPSHOT_DIR = os.path.join(base, "runtime", "snapshots")
        S.STORE_DIR = os.path.join(S.SNAPSHOT_DIR, "_store")
        S.QUARANTINE_DIR = os.path.join(base, "runtime", "quarantine")
        S.RUNTIME = os.path.join(base, "runtime")
        S.QUAR_CFG_FILE = os.path.join(S.RUNTIME, "state", "_quarantine.json")
        S.set_quarantine_config(days=7, maxStar=1, remindHours=24, enabled=True, cleanup=True)
        CG.STATE_FILE = os.path.join(S.RUNTIME, "state", "_confessional_git.json")

        a = os.path.join(ws, "a.txt")
        write(a, "a-v1\n")
        commit(ws, "v1")
        q1 = EB.capture_before_allow(ws, "", "Edit file", [a], scope="panel-git", kind="edit")
        write(a, "a-v2\n")
        commit(ws, "v2")
        clean = CG.clean_project(ws, force=True)
        check("已提交文件的修改前旧版被清", q1 in (clean.get("dropped") or []), clean)
        check("已提交文件的忏悔记录真的没了", not os.path.isdir(S._q_dir(q1)))

        # 未跟踪：旧内容从未进 git → 必须保留
        b = os.path.join(ws, "b.txt")
        write(b, "b-v1\n")
        q2 = EB.capture_before_allow(ws, "", "Edit file", [b], scope="panel-git", kind="edit")
        write(b, "b-v2\n")
        commit(ws, "b v2 (old v1 never tracked)")
        clean2 = CG.clean_project(ws, force=True)
        check("未跟踪文件的旧版不被清", q2 not in (clean2.get("dropped") or []), clean2)
        check("未跟踪文件的旧版仍可回滚", os.path.isdir(S._q_dir(q2)))

        # 删除文件：旧内容在 git → 清
        c = os.path.join(ws, "c.txt")
        write(c, "c-v1\n")
        commit(ws, "add c")
        q3 = EB.capture_before_allow(ws, "", "Delete file", [c], scope="panel-git", kind="delete")
        os.remove(c)
        commit(ws, "delete c")
        clean3 = CG.clean_project(ws, force=True)
        check("删除文件的旧版被清", q3 in (clean3.get("dropped") or []), clean3)
        check("删除文件的忏悔记录真的没了", not os.path.isdir(S._q_dir(q3)))

        # 快照：内容都在 git → 清
        d = os.path.join(ws, "d.txt")
        write(d, "d-v1\n")
        commit(ws, "add d")
        snap = S.take_snapshot(ws, "ses_git", label="turn", with_content=True)
        write(d, "d-v2\n")
        commit(ws, "d v2")
        clean4 = CG.clean_project(ws, force=True)
        check("已提交内容的旧快照被清",
              any(x.get("ts") == snap.get("ts") for x in (clean4.get("snapshots_dropped") or [])),
              clean4)
        check("旧快照清单真的没了", not S.list_snapshots("ses_git"))

        # 基线：第一次不清理；HEAD 变化后才清
        ws2 = os.path.join(base, "ws2")
        os.makedirs(ws2)
        git(ws2, "init")
        git(ws2, "config", "user.email", "t@example.com")
        git(ws2, "config", "user.name", "tester")
        e = os.path.join(ws2, "e.txt")
        write(e, "e-v1\n")
        commit(ws2, "e v1")
        q4 = EB.capture_before_allow(ws2, "", "Edit file", [e], scope="panel-git2", kind="edit")
        write(e, "e-v2\n")
        base_clean = CG.clean_project(ws2)
        check("第一次只记基线、不清", base_clean.get("baseline") is True and os.path.isdir(S._q_dir(q4)), base_clean)
        commit(ws2, "e v2")
        after = CG.clean_project(ws2)
        check("HEAD 变化后才清", q4 in (after.get("dropped") or []), after)

    finally:
        (S.SNAPSHOT_DIR, S.QUARANTINE_DIR, S.RUNTIME, S.STORE_DIR, S.QUAR_CFG_FILE,
         CG.STATE_FILE) = old
        shutil.rmtree(base, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
