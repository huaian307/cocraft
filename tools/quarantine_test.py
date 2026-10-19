# -*- coding: utf-8 -*-
r"""无头回归：可逆执行底座 `backend/snapshots.py`（快照 / 隔离区 / 撤销 / 命令目标猜测）。

全程在**临时目录**里跑（把 `QUARANTINE_DIR` / `SNAPSHOT_DIR` / `STORE_DIR` 指到 temp），
**不碰**真实的 `runtime/quarantine` 与 `runtime/snapshots` —— 结尾有只读断言证明没污染。

跑法：`python tools\quarantine_test.py`
输出纯 ASCII（控制台是 GBK）。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import snapshots as S  # noqa: E402

PASS = 0
FAIL = 0
ERRORS = []


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s%s" % (label, (" " + extra) if extra else ""))
    else:
        FAIL += 1
        ERRORS.append(label)
        print("  [BAD]  %s%s" % (label, (" " + extra) if extra else ""))


def section(t):
    print("\n== %s ==" % t)


def write(p, text, mtime=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    if mtime:
        os.utime(p, (mtime, mtime))


def main():
    tmp = tempfile.mkdtemp(prefix="cocraft-snap-")
    ws = os.path.join(tmp, "ws")
    os.makedirs(ws)

    # ★ 隔离：把三个落盘目录指到临时区（真实 runtime 一个字节都不动）
    S.QUARANTINE_DIR = os.path.join(tmp, "q")
    S.SNAPSHOT_DIR = os.path.join(tmp, "s")
    S.STORE_DIR = os.path.join(S.SNAPSHOT_DIR, "_store")
    S.MAX_FILES = 20000

    try:
        _run(ws, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    real_q = os.path.join(S.RUNTIME, "quarantine")
    section("没有污染真实 runtime")
    leaked = os.path.isdir(real_q) and any(
        x.startswith("ws") or "cocraft-snap" in x for x in os.listdir(real_q))
    ok("真实 runtime/quarantine 里没有本测试的痕迹", not leaked)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    if ERRORS:
        for e in ERRORS:
            print("  - %s" % e)
    return 1 if FAIL else 0


def _run(ws, tmp):
    # ---------------- 1. 扫描准确性 + 哈希缓存
    section("1. 扫描：add / modify（同大小改写）/ delete + 忽略目录")
    write(os.path.join(ws, "a.txt"), "hello\n")
    write(os.path.join(ws, "src", "b.py"), "print(1)\n")
    write(os.path.join(ws, "node_modules", "junk.js"), "noise\n" * 50)
    base = S.scan(ws)
    ok("冷扫列出 2 个文件（node_modules 被忽略）", base["count"] == 2, "count=%d" % base["count"])
    ok("冷扫重算了 2 次 hash", base["rehash"] == 2, "rehash=%d" % base["rehash"])

    hot = S.scan(ws, cache=base["files"])
    ok("热扫零重算（mtime+size 缓存命中）", hot["rehash"] == 0, "rehash=%d" % hot["rehash"])

    os.remove(os.path.join(ws, "a.txt"))
    write(os.path.join(ws, "new.md"), "# new\n")
    time.sleep(0.01)
    write(os.path.join(ws, "src", "b.py"), "print(2)\n")      # 同大小、内容不同
    after = S.scan(ws, cache=hot["files"])
    d = S.diff_manifests(hot, after)
    ok("检出 deleted=['a.txt']", [x["path"] for x in d["deleted"]] == ["a.txt"])
    ok("检出 added=['new.md']", [x["path"] for x in d["added"]] == ["new.md"])
    ok("同大小改写也算 changed", [x["path"] for x in d["changed"]] == ["src/b.py"])

    # ---------------- 2. 重命名启发式
    section("2. 重命名启发式")
    write(os.path.join(ws, "r1.txt"), "same-content\n")
    w1 = S.scan(ws)
    os.remove(os.path.join(ws, "r1.txt"))
    write(os.path.join(ws, "r2.txt"), "same-content\n")
    w2 = S.scan(ws)
    dr = S.diff_manifests(w1, w2)
    ok("同 hash 的一增一删 → renamed", len(dr["renamed"]) == 1
       and dr["renamed"][0]["to"] == "r2.txt", "%r" % (dr["renamed"],))
    os.remove(os.path.join(ws, "r2.txt"))

    # ---------------- 3. 快照（含内容）+ 撤销
    section("3. 快照 + 撤销（被删的恢复 / 被改的写回 / 新增的删掉）")
    write(os.path.join(ws, "keep.txt"), "KEEP-OLD\n")
    write(os.path.join(ws, "edit.txt"), "EDIT-OLD\n")
    snap = S.take_snapshot(ws, "panel", label="轮开始", with_content=True)
    ok("快照落盘且有内容", os.path.isfile(snap["path"]) and snap["stored"] >= 3,
       "stored=%d" % snap["stored"])
    # 第二轮：删一个、改一个、增一个
    os.remove(os.path.join(ws, "keep.txt"))
    write(os.path.join(ws, "edit.txt"), "EDIT-NEW\n")
    write(os.path.join(ws, "brand.txt"), "NEW\n")
    man = S.load_manifest("panel", snap["ts"])
    now = S.scan(ws)
    dd = S.diff_manifests(man, now)
    ok("撤销前 diff 正确（1 删 / 1 改 / 1 增）",
       dd["stats"]["deleted"] == 1 and dd["stats"]["changed"] == 1 and dd["stats"]["added"] == 1,
       "%r" % (dd["stats"],))

    res = S.undo_snapshot("panel", snap["ts"])
    ok("撤销恢复了 2 个（被删 + 被改）", res["restored"] == 2, "%r" % (res,))
    ok("撤销删掉了 1 个新增", res["removed"] == 1)
    ok("被删的文件内容回来了",
       open(os.path.join(ws, "keep.txt"), encoding="utf-8").read().strip() == "KEEP-OLD")
    ok("被改的文件写回了旧内容",
       open(os.path.join(ws, "edit.txt"), encoding="utf-8").read().strip() == "EDIT-OLD")
    ok("新增的文件被撤掉", not os.path.exists(os.path.join(ws, "brand.txt")))
    ok("撤销「新增」时先自动进了隔离区（撤销本身可逆）",
       any("brand.txt" in (it.get("src") or "") for rec in S.list_quarantine()
           for it in (S.load_quarantine(rec["id"]).get("items") or [])))

    # 行级 diff
    write(os.path.join(ws, "diffme.txt"), "line1\nline2\n")
    s2 = S.take_snapshot(ws, "panel", label="d", with_content=True)
    write(os.path.join(ws, "diffme.txt"), "line1\nline2-changed\nline3\n")
    m2 = S.load_manifest("panel", s2["ts"])
    ld = S.line_diff(m2["files"]["diffme.txt"]["hash"],
                     S.scan(ws)["files"]["diffme.txt"]["hash"], "diffme.txt", root=ws)
    ok("行级 diff 可用（+2 -1）", ld.get("ok") and ld["additions"] == 2 and ld["deletions"] == 1,
       "%r" % ({k: ld.get(k) for k in ("ok", "additions", "deletions", "reason")},))

    # ---------------- 4. 隔离区
    section("4. 隔离区：复制 / 清单 / 恢复 / 冲突 / 删除")
    d1 = os.path.join(ws, "victim.txt")
    write(d1, "PRECIOUS\n")
    dr1 = os.path.join(ws, "vdir")
    write(os.path.join(dr1, "inner.txt"), "INNER\n")
    rec = S.quarantine_paths(ws, [d1, dr1], scope="gc", note="Remove-Item", cmd="Remove-Item -Recurse x")
    ok("隔离了 2 项", len(rec["items"]) == 2, "%r" % ([i["src"] for i in rec["items"]],))
    ok("清单里记了命令原文", "Remove-Item" in rec["cmd"])
    ok("清单文件落盘", os.path.isfile(os.path.join(tmp, "q", "gc", rec["ts"], "_manifest.json")))
    os.remove(d1)
    shutil.rmtree(dr1)
    ok("原文件确实没了（模拟删除）", not os.path.exists(d1))
    r1 = S.restore_quarantine(rec["id"])
    ok("恢复成功 2 项", r1["restored"] == 2, "%r" % (r1,))
    ok("文件内容原样回来", open(d1, encoding="utf-8").read().strip() == "PRECIOUS")
    ok("目录（含子文件）也回来", os.path.isfile(os.path.join(dr1, "inner.txt")))
    r2 = S.restore_quarantine(rec["id"])
    ok("目标已存在 → conflict（不静默覆盖）", r2["conflict"] == 2 and r2["restored"] == 0,
       "%r" % (r2,))
    r3 = S.restore_quarantine(rec["id"], override=True)
    ok("override=True 才覆盖", r3["restored"] == 2)
    dq = S.drop_quarantine(rec["id"])
    ok("删记录成功", dq["removed"])
    try:
        S.drop_quarantine("../../etc")
        ok("非法 id 被拒", False)
    except ValueError:
        ok("非法 id 被拒", True)

    # ---------------- 5. 上限 / 保护
    section("5. 上限与保护（如实记录「没保住」）")
    big = os.path.join(ws, "huge.bin")
    with open(big, "wb") as fh:
        fh.write(b"x" * 2048)
    old_max = S.MAX_QUARANTINE_FILE
    S.MAX_QUARANTINE_FILE = 1024
    rec2 = S.quarantine_paths(ws, [big], scope="cap")
    S.MAX_QUARANTINE_FILE = old_max
    ok("超大文件被拒且记进 skipped", not rec2["items"]
       and any("太大" in (s.get("reason") or "") for s in rec2["skipped"]),
       "%r" % (rec2["skipped"],))
    rec3 = S.quarantine_paths(ws, [os.path.join(tmp, "q")], scope="cap2")
    ok("隔离区自己不被当成目标", not rec3["items"] and rec3["skipped"])
    rec4 = S.quarantine_paths(ws, [os.path.join(ws, "nope.txt")], scope="cap3")
    ok("不存在的目标记进 skipped", not rec4["items"] and rec4["skipped"])

    # ---------------- 6. 从命令猜目标
    section("6. 从命令原文猜「要动的东西」")
    write(os.path.join(ws, "del-a.txt"), "a\n")
    write(os.path.join(ws, "del-b.txt"), "b\n")
    write(os.path.join(ws, "sub", "deep.txt"), "d\n")

    g1 = S.guess_targets("Remove-Item -Recurse -Force '%s'" % os.path.join(ws, "sub"), ws)
    ok("Remove-Item + 绝对路径", len(g1["targets"]) == 1 and g1["targets"][0].endswith("sub"),
       "%r" % (g1,))
    g2 = S.guess_targets("del del-a.txt", ws)
    ok("del + 相对路径（相对 root）",
       any(t.endswith("del-a.txt") for t in g2["targets"]), "%r" % (g2,))
    g3 = S.guess_targets("rm -rf sub", ws)
    ok("rm -rf + 相对目录", any(t.endswith("sub") for t in g3["targets"]))
    g4 = S.guess_targets("del *.txt", ws)
    ok("通配符展开成多个", len(g4["targets"]) >= 2, "%r" % (g4["targets"],))
    g5 = S.guess_targets('powershell -NoProfile -Command "Remove-Item -Recurse -Force %s"'
                         % os.path.join(ws, "sub"), ws)
    ok("powershell -Command 套一层也能认", any(t.endswith("sub") for t in g5["targets"]),
       "%r" % (g5,))
    g6 = S.guess_targets("git status --porcelain", ws)
    ok("非破坏性命令不误报目标", not g6["targets"], "%r" % (g6,))
    g7 = S.guess_targets("Remove-Item -Recurse -Force %s" % os.path.join(ws, "ghost"), ws)
    ok("不存在的目标进 unresolved（不假装保住）",
       not g7["targets"] and g7["unresolved"], "%r" % (g7,))
    g8 = S.guess_targets("echo -Recurse /s /q", ws)
    ok("纯 flag 不被当成路径", not g8["targets"], "%r" % (g8,))

    # ---------------- 7. stats
    section("7. 占用统计")
    st = S.stats()
    ok("stats 给出隔离区 / 内容仓目录", st["quarantine_dir"] and st["snapshot_dir"]
       and st["store"] >= 0, "%r" % (st,))

    # ---------------- 8. 桥侧接线：破坏性操作「放行前先隔离」
    section("8. 桥接线：拥有者确认后、放行前先隔离（真调 _quarantine_before_allow）")
    try:
        import cocraft_agents as CA
    except Exception as exc:                                    # noqa: BLE001
        ok("能 import cocraft_agents", False, repr(exc))
        return
    sent, acts = [], []

    def _fake_send(aid, chat, text, reply_to=None):
        sent.append(text)
        return True

    def _fake_act(*a, **k):
        acts.append(a)
        return {}

    CA._send_message = _fake_send
    CA._act_add = _fake_act
    CA._act_finish = lambda *a, **k: None
    CA.acp_log = lambda *a, **k: None
    CA.find = lambda aid: {"name": "yiyi"}
    CA.snapshots.QUARANTINE_DIR = S.QUARANTINE_DIR          # 共用隔离区（同一个模块对象）
    d = os.path.join(ws, "danger")
    write(os.path.join(d, "a.txt"), "A\n")
    write(os.path.join(d, "b.txt"), "B\n")
    res = CA._quarantine_before_allow("7", "3", "Remove-Item -Recurse -Force %s" % d,
                                      "Run command", [], ws)
    ok("识别出目标并隔离", res.get("items", 0) >= 1, "%r" % (res,))
    ok("群里发了「已隔离（可恢复）」提示", any("已隔离" in t for t in sent), "%r" % (sent,))
    ok("进了活动流（隔离备份）",
       any(len(a) > 2 and a[2] == "隔离备份" for a in acts), "%r" % (acts,))
    ok("隔离副本真在盘上", any(os.path.isfile(os.path.join(tmp, "q", "chat-3", t, "_manifest.json"))
                              for t in (os.listdir(os.path.join(tmp, "q", "chat-3"))
                                        if os.path.isdir(os.path.join(tmp, "q", "chat-3")) else [])))

    # 识别不出目标 → 明说「这次没能预先隔离」（不假装成功）
    sent2 = []
    CA._send_message = lambda aid, chat, text, reply_to=None: (sent2.append(text), True)[1]
    res2 = CA._quarantine_before_allow("7", "3", "rm -rf $TARGET", "Run command", [], ws)
    ok("识别不出目标时不假装成功", res2.get("items") == 0
       and any("没能预先隔离" in t for t in sent2), "%r" % (sent2,))

    # 隔离失败也不能打断已确认的操作
    sent3 = []
    CA._send_message = lambda aid, chat, text, reply_to=None: (sent3.append(text), True)[1]
    _save_q = S.QUARANTINE_DIR
    S.QUARANTINE_DIR = os.path.join(tmp, "q") + "\\:*bad*"          # 非法路径 → 复制必失败
    try:
        res3 = CA._quarantine_before_allow("7", "3", "Remove-Item -Recurse -Force %s" % d,
                                           "Run command", [], ws)
        ok("隔离失败也不抛异常（放行照旧，但说清不可恢复）", isinstance(res3, dict)
           and (res3.get("items") == 0), "%r" % (res3,))
    except Exception as exc:                                        # noqa: BLE001
        ok("隔离失败也不抛异常（放行照旧，但说清不可恢复）", False, repr(exc))
    finally:
        S.QUARANTINE_DIR = _save_q

    # ---------------- 9. 内容仓回收（快照是每轮都落的，仓不能无限长）
    section("9. 内容仓回收：裁剪 / 删快照后回收没人引用的块")
    import hashlib as _hl

    def _sha(b):
        return _hl.sha1(b).hexdigest()

    g = os.path.join(ws, "g.txt")
    write(g, "V1\n")
    S.take_snapshot(ws, "gc", label="1", with_content=True, keep=2)
    ok("V1 内容进了仓", S.has_blob(_sha(b"V1\n")))
    write(g, "V2\n")
    S.take_snapshot(ws, "gc", label="2", with_content=True, keep=2)
    write(g, "V3\n")
    S.take_snapshot(ws, "gc", label="3", with_content=True, keep=2)   # → 裁掉第 1 次
    lst = S.list_snapshots("gc")
    ok("只保留 2 次快照（keep=2）", len(lst) == 2, "%r" % ([x["ts"] for x in lst],))
    ok("★被裁掉那次的独有内容块被回收（仓不无限长）", not S.has_blob(_sha(b"V1\n")),
       "V1 blob still there")
    ok("仍被引用的内容块留着", S.has_blob(_sha(b"V3\n")))

    # 删快照也回收（⚠ 只回收"它**独有**"的块：共有内容仍被另一份快照引用，不该删）
    ts_old = lst[-1]["ts"]                                          # 保留里最旧的那次
    man_old = S.load_manifest("gc", ts_old)
    old_hashes = [i.get("hash") for i in (man_old.get("files") or {}).values()]
    man_new = S.load_manifest("gc", lst[0]["ts"])
    still = {i.get("hash") for i in (man_new.get("files") or {}).values()}
    only_old = [h for h in old_hashes
                if h and h not in still and not str(h).startswith("big:")]
    S.drop_snapshot("gc", ts_old)
    ok("删掉一次快照后它**独有**的内容块被回收",
       bool(only_old) and not any(S.has_blob(h) for h in only_old),
       "only_old=%r" % (only_old,))
    ok("共有内容块仍然留着（别的快照还要用）",
       all(S.has_blob(h) for h in still if h and not str(h).startswith("big:")),
       "still=%r" % (still,))
    try:
        S.drop_snapshot("gc", "../../x")
        ok("非法 ts 被拒", False)
    except ValueError:
        ok("非法 ts 被拒", True)

    # ---- 内容仓**硬上限**：超了就丢最旧的快照 + 回收（快照是每轮都落的） ----
    _mb, _min = S.MAX_STORE_BYTES, S.MIN_SNAPSHOTS
    S.MAX_STORE_BYTES = 1                       # 永远"超预算"
    S.MIN_SNAPSHOTS = 2
    try:
        h = os.path.join(ws, "h.txt")
        for i in range(5):
            write(h, "H%d\n" % i)
            S.take_snapshot(ws, "budget", label=str(i), with_content=True, keep=40)
        n_budget = len(S.list_snapshots("budget"))
        ok("★超内容仓上限时丢最旧的快照、只留 MIN_SNAPSHOTS 次", n_budget == 2, "n=%d" % n_budget)
        ok("stats 里给出上限与保留次数",
           S.stats().get("store_limit") == 1 and S.stats().get("keep") == S.KEEP_SNAPSHOTS,
           "%r" % (S.stats(),))
    finally:
        S.MAX_STORE_BYTES, S.MIN_SNAPSHOTS = _mb, _min


if __name__ == "__main__":
    sys.exit(main())
