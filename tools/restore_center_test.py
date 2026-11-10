# -*- coding: utf-8 -*-
r"""恢复中心回归（`backend/restore_center.py`）。

覆盖：
  * 三类 scope 识别（群聊 / 工作流 / 单聊）；
  * 按 `root` 归到项目；
  * 轮快照 → 文件版本时间线（同一 hash 去重）；
  * 隔离区 → 「删除前」版本；
  * **指定文件回滚到指定版本**（快照 / 隔离区两条）；
  * 回滚前自动备份当前文件；
  * 只记清单（墓碑）/ 只记文件名（无内容）→ 明确拒绝；
  * 非法分类拒绝。

不联网、只用临时目录。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import restore_center as rc      # noqa: E402
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


def main():
    base = tempfile.mkdtemp(prefix="cocraft-restore-")
    old = (S.SNAPSHOT_DIR, S.QUARANTINE_DIR, S.RUNTIME, S.QUAR_CFG_FILE)
    try:
        ws = os.path.join(base, "ws")
        os.makedirs(ws)
        write(os.path.join(ws, "a.py"), "v1\n")
        write(os.path.join(ws, "b.txt"), "b1\n")
        write(os.path.join(ws, "dir", "c.log"), "c1\n")

        S.SNAPSHOT_DIR = os.path.join(base, "runtime", "snapshots")
        S.QUARANTINE_DIR = os.path.join(base, "runtime", "quarantine")
        S.RUNTIME = os.path.join(base, "runtime")
        S.QUAR_CFG_FILE = os.path.join(base, "runtime", "state", "_quarantine.json")
        S.set_quarantine_config(days=7, maxStar=1, remindHours=24, enabled=True, cleanup=True)
        rc._SESS_CACHE["map"] = {}
        rc._RUN_CACHE["map"] = {}

        # 会话 / 运行元数据（名称解析用）
        write(os.path.join(S.RUNTIME, "state", "_acp_sessions.json"),
              json.dumps({"sessions": [{"id": "ses_test", "title": "测试会话",
                                        "agent": "dsh", "location": {"directory": ws}}]}))
        write(os.path.join(S.RUNTIME, "state", "_orchestrator", "runs", "run_test.json"),
              json.dumps({"id": "run_test", "title": "测试工作流", "workflowName": "发版前检查",
                          "cwd": ws, "members": [{"id": "m1", "name": "impl"}]}))

        # 1) 单聊轮快照：v1 → v2
        S.take_snapshot(ws, "ses_test", label="turn", with_content=True)
        time.sleep(0.02)
        write(os.path.join(ws, "a.py"), "v2\n")
        S.take_snapshot(ws, "ses_test", label="turn", with_content=True)

        # 2) 工作流快照
        S.take_snapshot(ws, "wf-run_test-m1", label="工作流派活", with_content=True)

        # 3) 群聊快照
        S.take_snapshot(ws, "cc-1-2", label="群聊派活", with_content=True)

        # 4) 隔离区：删除前副本
        write(os.path.join(ws, "del.txt"), "before delete\n")
        q = S.quarantine_paths(ws, [os.path.join(ws, "del.txt")],
                               scope="panel-ses_test", note="删除文件", cmd="del del.txt")
        os.remove(os.path.join(ws, "del.txt"))

        # 4b) 修改前旧版（忏悔室：权限放行前自动留存）
        mod_p = os.path.join(ws, "mod.py")
        write(mod_p, "old-mod\n")
        qm = EB.capture_before_allow(ws, "", "Edit file", [mod_p],
                                     scope="panel-ses_test", note="Edit file", kind="edit")
        write(mod_p, "new-mod\n")
        qread = EB.capture_before_allow(ws, "cat mod.py", "Read file", [mod_p],
                                        scope="panel-ses_test", note="Read file", kind="read")

        # 5) 墓碑（只记清单）
        old_limit = S.AUTO_COPY_MAX_BYTES
        S.AUTO_COPY_MAX_BYTES = 1
        write(os.path.join(ws, "big.bin"), "0123456789")
        qt = S.quarantine_paths(ws, [os.path.join(ws, "big.bin")], scope="panel-ses_test",
                                auto=True, scores=[{"path": os.path.join(ws, "big.bin"),
                                                    "stars": 1, "score": 8, "reasons": ["tmp"]}])
        S.AUTO_COPY_MAX_BYTES = old_limit
        os.remove(os.path.join(ws, "big.bin"))

        # ---------- 分类 ----------
        check("scope panel → 单聊", rc.category_of("panel-abc") == "single")
        check("scope ses → 单聊", rc.category_of("ses_abc") == "single")
        check("scope run → 工作流", rc.category_of("run_2026") == "workflow")
        check("scope wf → 工作流", rc.category_of("wf-run_2026-m1") == "workflow")
        check("scope chat → 群聊", rc.category_of("chat-3") == "chat")
        check("scope cc → 群聊", rc.category_of("cc-1-2") == "chat")

        # ---------- overview ----------
        ov = rc.overview()
        cats = {c["id"]: c for c in ov["categories"]}
        check("overview 三类都在", set(cats) == {"chat", "workflow", "single"}, list(cats))
        check("单聊有项目", len(cats["single"]["projects"]) >= 1, cats["single"])
        check("工作流有项目", len(cats["workflow"]["projects"]) >= 1, cats["workflow"])
        check("群聊有项目", len(cats["chat"]["projects"]) >= 1, cats["chat"])
        proj = cats["single"]["projects"][0]["key"]

        # ---------- timeline ----------
        tl = rc.timeline("single", proj)
        by = {f["rel"]: f for f in tl["files"]}
        check("时间线有 a.py", "a.py" in by, list(by)[:8])
        check("a.py 至少两个版本", by.get("a.py", {}).get("count", 0) >= 2, by.get("a.py"))
        check("时间线有删除前 del.txt", "del.txt" in by, list(by)[:8])
        check("时间线有墓碑 big.bin", "big.bin" in by, list(by)[:8])
        a_versions = by.get("a.py", {}).get("versions") or []
        check("a.py 版本按新→旧", len(a_versions) >= 2 and a_versions[0]["ts"] >= a_versions[-1]["ts"], a_versions)
        check("快照版本可回滚", any(v["restorable"] for v in a_versions), a_versions)
        qv = next((v for v in (by.get("del.txt", {}).get("versions") or [])), None)
        check("删除前版本来自 quarantine", bool(qv and qv["source"] == "quarantine"), qv)
        check("删除前记录 kind=delete", bool(qv and qv.get("kind") == "delete"), qv)
        mv = next((v for v in (by.get("mod.py", {}).get("versions") or [])), None)
        check("修改前版本来自 quarantine", bool(mv and mv["source"] == "quarantine"), mv)
        check("修改前记录 kind=modify", bool(mv and mv.get("kind") == "modify"), mv)
        check("修改前详情写「修改前」", bool(mv and "修改前" in str(mv.get("detail"))), mv)
        check("修改前副本可回滚", bool(mv and mv.get("restorable")), mv)
        check("编辑前留存命中", bool(qm), qm)
        check("只读操作不留存", qread == "", qread)
        check("墓碑版本不可回滚",
              all(not v["restorable"] for v in (by.get("big.bin", {}).get("versions") or [])),
              by.get("big.bin"))

        # ---------- 快照单文件回滚 ----------
        v1 = next(v for v in a_versions if v["ts"] < a_versions[0]["ts"] or v is not a_versions[0])
        # 用最旧那个版本（v1）；找 hash 对应内容
        oldest = a_versions[-1]
        res = rc.restore_file({"source": "snapshot", "scope": oldest["scope"],
                               "ts": oldest["ts_tag"], "rel": "a.py"})
        with io.open(os.path.join(ws, "a.py"), encoding="utf-8") as fh:
            got = fh.read()
        check("快照回滚成功", res.get("ok") is True, res)
        check("快照回滚到 v1", got == "v1\n", got)
        check("回滚前自动备份", bool(res.get("backup")), res)

        # ---------- 隔离区单文件回滚 ----------
        res2 = rc.restore_file({"source": "quarantine", "id": q["id"],
                                "path": os.path.join(ws, "del.txt")})
        with io.open(os.path.join(ws, "del.txt"), encoding="utf-8") as fh:
            got2 = fh.read()
        check("隔离区回滚成功", res2.get("ok") is True, res2)
        check("隔离区回滚内容正确", got2 == "before delete\n", got2)

        # ---------- 修改前旧版回滚 ----------
        res3 = rc.restore_file({"source": "quarantine", "id": qm, "path": mod_p})
        with io.open(mod_p, encoding="utf-8") as fh:
            got3 = fh.read()
        check("修改前旧版回滚成功", res3.get("ok") is True, res3)
        check("修改前旧版内容正确", got3 == "old-mod\n", got3)

        # ---------- 墓碑 / 无内容拒绝 ----------
        try:
            rc.restore_file({"source": "quarantine", "id": qt["id"],
                             "path": os.path.join(ws, "big.bin")})
            check("墓碑拒绝回滚", False)
        except ValueError as exc:
            check("墓碑拒绝回滚", "清单" in str(exc), exc)

        # 无内容快照
        S.take_snapshot(ws, "ses_nocontent", label="turn", with_content=False)
        try:
            rc.restore_file({"source": "snapshot", "scope": "ses_nocontent",
                             "ts": S.list_snapshots("ses_nocontent")[0]["ts"], "rel": "a.py"})
            check("无内容快照拒绝回滚", False)
        except ValueError as exc:
            check("无内容快照拒绝回滚", "没存内容" in str(exc), exc)

        # ---------- 非法分类 ----------
        try:
            rc.timeline("nope")
            check("非法分类被拒", False)
        except ValueError:
            check("非法分类被拒", True)

        # ---------- index 没污染真实目录 ----------
        check("真实快照目录没被写", not os.path.isdir(os.path.join(ROOT, "runtime", "snapshots", "ses_test")))
    finally:
        (S.SNAPSHOT_DIR, S.QUARANTINE_DIR, S.RUNTIME, S.QUAR_CFG_FILE) = old
        shutil.rmtree(base, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
