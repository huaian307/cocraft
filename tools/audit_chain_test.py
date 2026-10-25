# -*- coding: utf-8 -*-
r"""★无头回归：审计链（`backend/audit.py` + 两处接线）

覆盖：
  1. 链本身：seq 自增 / prev 串链 / canonical 哈希可复算 / 链尾
  2. 篡改：改内容 / 删中间 / 乱序 / 撕行 → 必须报出来（且不崩）
  3. 查询与导出：筛选 / 条数上限 / 导出文件头 / 链尾 / 区间导出不算断 / 空文件
  4. 并发：多线程同时追加 → seq 唯一、链仍完整
  5. 失败开放：审计写不进去时，权限流程照常返回（不抛、不卡）
  6. 接线：群聊桥 `_perm_log_add` 与工作台 `_ask_permission` 都会写链（含超时 / 人工批准）

⚠ 全程把 `audit.AUDIT_FILE` / `ca.PERM_LOG_FILE` / `orchestrator.RUNS_DIR` 指到临时目录，
   **不碰真实链**（`runtime/state/_audit.jsonl`）与真实审批日志 —— 见 AGENTS.md 踩坑 #36。
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

import audit  # noqa: E402
import cocraft_agents as ca  # noqa: E402
import orchestrator as orch  # noqa: E402

N = {"ok": 0, "bad": 0}


def check(name: str, cond, extra: str = "") -> None:
    if cond:
        N["ok"] += 1
        print("[OK] %s" % name)
    else:
        N["bad"] += 1
        print("[BAD] %s%s" % (name, ("  -> " + str(extra)) if extra else ""))


def rec(**kw):
    base = {"actor": {"kind": "agent", "id": "codex-node", "name": "impl", "member": "m1"},
            "action": {"kind": "execute", "title": "run", "command": "rm -rf x",
                       "targets": ["D:/ws/x"], "destructive": True},
            "decision": "once", "result": "ok",
            "approver": {"kind": "user", "id": "local", "name": "owner"},
            "ref": {"run": "r1", "member": "m1"}}
    base.update(kw)
    return base


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cocraft_audit_test_")
    real_file = audit.AUDIT_FILE
    real_perm = ca.PERM_LOG_FILE
    real_runs = orch.RUNS_DIR
    real_find = ca.find
    real_level = orch.perm_level
    real_timeout = orch.ASK_TIMEOUT
    try:
        # ---------------- 1. 链本身 ----------------
        audit.set_file(os.path.join(tmp, "a.jsonl"))
        r1 = audit.append("collab", **rec())
        r2 = audit.append("collab", **rec(decision="always"))
        r3 = audit.append("ccagent", **rec(decision="reject", result="denied",
                                          approver=None, ref={"chat": "7", "aid": "11"}))
        check("追加成功并回记录", bool(r1 and r2 and r3))
        check("seq 自增 1/2/3", [r1["seq"], r2["seq"], r3["seq"]] == [1, 2, 3])
        check("prev 串起来（2→1、3→2）", r2["prev"] == r1["hash"] and r3["prev"] == r2["hash"])
        check("链首 prev = GENESIS", r1["prev"] == audit.GENESIS)
        # canonical 哈希可独立复算
        body = {k: v for k, v in r2.items() if k != "hash"}
        check("hash = sha256(canonical(body 去 hash) + prev)",
              audit._digest(body, r2["prev"]) == r2["hash"])
        v = audit.verify()
        check("verify：完整链 ok", v["ok"] and v["count"] == 3 and v["gaps"] == 0, v)
        check("verify：链尾 = 最后一条 hash", v["tail"] == r3["hash"])
        check("记录带机器名 / ISO 时间 / 毫秒时间", bool(r1["machine"] and r1["ts"].endswith("Z")
                                                        and r1["tsu"] > 0), r1)

        # ---------------- 2. 篡改 ----------------
        def lines():
            with open(audit.AUDIT_FILE, "r", encoding="utf-8") as fh:
                return [x for x in fh.read().splitlines() if x.strip()]

        def write_lines(ls):
            with open(audit.AUDIT_FILE, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(ls) + "\n")

        orig = lines()
        o = json.loads(orig[1])
        o["decision"] = "reject"          # 原来是 always（改成一个确实不同的值）
        write_lines([orig[0], json.dumps(o, ensure_ascii=False, separators=(",", ":")), orig[2]])
        vt = audit.verify()
        check("改内容 → 报断（ok=False）", not vt["ok"] and vt["brokenCount"] >= 1, vt["broken"])
        check("报断原因指向哈希", any("哈希" in (b.get("reason") or "") for b in vt["broken"]),
              vt["broken"])

        write_lines([orig[0], orig[2]])
        vd = audit.verify()
        check("删中间一条 → 报断 + 记空洞",
              not vd["ok"] and vd["gaps"] >= 1
              and any("prev" in (b.get("reason") or "") or "前一条" in (b.get("reason") or "")
                      for b in vd["broken"]), vd)

        write_lines([orig[1], orig[0], orig[2]])
        write_lines([orig[0], orig[1], orig[2], "这不是 JSON"])
        vbad = audit.verify()
        check("撕裂 / 坏行 → 不崩且报坏行", not vbad["ok"]
              and any("坏行" in (b.get("reason") or "") for b in vbad["broken"]), vbad["broken"])
        check("stats 如实记 badLines", audit.stats()["badLines"] == 1, audit.stats())

        write_lines(orig)

        # ---------------- 3. 查询 / 导出 ----------------
        got = audit.records(limit=2)
        check("records(limit) 新 → 旧", [x["seq"] for x in got] == [3, 2], got)
        check("records 按 source 过滤", [x["seq"] for x in audit.records(source="ccagent")] == [3])
        check("records 按 approver 过滤（人工批准的才在）",
              [x["seq"] for x in audit.records(approver="local")] == [2, 1])
        check("records 按 run 过滤（ref）", [x["seq"] for x in audit.records(run="r1")] == [2, 1])

        ex = audit.export()
        for k in ("format", "version", "exported", "records", "tail", "engine", "items", "chain"):
            check("导出文件头有 %s" % k, k in ex, list(ex.keys())[:8])
        check("导出格式名 = cocraft-audit", ex["format"] == "cocraft-audit")
        check("导出条数 = 3", ex["records"] == 3 and len(ex["items"]) == 3)
        check("导出按时间正序（seq 1,2,3）", [x["seq"] for x in ex["items"]] == [1, 2, 3])
        check("导出链尾 = 最后一条", ex["tail"] == r3["hash"])
        check("导出 chain.ok", ex["chain"]["ok"] is True and ex["chain"]["subset"] is False)
        check("导出可被 verify 复核（ok）", audit.verify(ex["items"])["ok"])

        sub = audit.export(since=r2["tsu"])            # 只要后两条（首条 prev 指向范围外）
        check("区间导出：subset 标注", sub["chain"]["subset"] is True, sub["chain"])
        check("区间导出：不因首条 prev 在范围外就报断", sub["chain"]["ok"] is True, sub["chain"])
        check("区间导出：条数正确", sub["records"] == 2, sub["records"])

        bad_items = json.loads(json.dumps(ex["items"]))
        bad_items[0]["result"] = "denied"
        vb2 = audit.verify(bad_items)
        check("改导出内容 → verify 报断", not vb2["ok"] and vb2["brokenCount"] >= 1)

        # 空文件
        audit.set_file(os.path.join(tmp, "empty.jsonl"))
        check("空链 verify ok（count 0）", audit.verify()["ok"] and audit.verify()["count"] == 0)
        check("空链 stats", audit.stats()["records"] == 0)
        check("空链导出 records 0 / tail = GENESIS",
              audit.export()["records"] == 0 and audit.export()["tail"] == audit.GENESIS)

        # ---------------- 4. 并发 ----------------
        audit.set_file(os.path.join(tmp, "conc.jsonl"))

        def worker(n):
            for _ in range(30):
                audit.append("collab", **rec(note="t%d" % n))

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        vc = audit.verify()
        seqs = [x["seq"] for x in audit.records(limit=0)]
        check("并发 120 条：seq 唯一且连续",
              vc["ok"] and vc["count"] == 120 and sorted(seqs) == list(range(1, 121)),
              {"count": vc["count"], "ok": vc["ok"]})

        # ---------------- 5. 失败开放 ----------------
        blocked = os.path.join(tmp, "blocked_dir")
        os.makedirs(blocked, exist_ok=True)
        audit.set_file(blocked)                    # 指向目录 → 写不进去
        check("写不进去时 append 回 None（不抛）", audit.append("collab", **rec()) is None)
        check("写不进去时 verify 不抛", isinstance(audit.verify(), dict))

        # ---------------- 6. 接线：群聊桥 ----------------
        audit.set_file(os.path.join(tmp, "wired.jsonl"))
        ca.PERM_LOG_FILE = os.path.join(tmp, "_cc_perm_log.json")
        ca.find = lambda aid: {"id": aid, "name": "yiyi", "engine": "codex-node"}
        ca._perm_log_add("11", "7", {"kind": "execute", "title": "运行命令", "cmd": "rm -rf D:/ws/x",
                                     "locations": ["D:/ws/x"], "destructive": True,
                                     "owner": "3", "verdict": "once", "by": "owner",
                                     "reply": "@yiyi 确认", "quarantine": "chat-7/20261024-1010",
                                     "qitems": 2, "qmissed": 1})
        items = audit.records(source="ccagent")
        check("桥：一次批准 → 链里多一条", len(items) == 1, len(items))
        if items:
            it = items[0]
            check("桥：approver = 拥有者", (it.get("approver") or {}).get("id") == "3", it.get("approver"))
            check("桥：decision/result 正确", it["decision"] == "once" and it["result"] == "ok")
            check("桥：带隔离记录 id", it["quarantine"] == "chat-7/20261024-1010")
            check("桥：note 记隔离项数（含没保住的）",
                  "2" in it["note"] and "1" in it["note"], it["note"])
            check("桥：命令原文 / 目标都被留住",
                  "rm -rf" in it["action"]["command"] and it["action"]["targets"] == ["D:/ws/x"],
                  it["action"])

        ca._perm_log_add("11", "7", {"kind": "execute", "title": "超时", "cmd": "x",
                                     "locations": [], "destructive": False,
                                     "owner": "3", "verdict": "reject", "by": "timeout"})
        to = [x for x in audit.records(source="ccagent") if x["result"] == "timeout"]
        check("桥：超时 → result=timeout、无 approver",
              len(to) == 1 and (to[0].get("approver") is None))

        # ---------------- 7. 接线：工作台（超时 / 人工批准） ----------------
        audit.set_file(os.path.join(tmp, "orch.jsonl"))
        orch.RUNS_DIR = os.path.join(tmp, "runs")
        os.makedirs(orch.RUNS_DIR, exist_ok=True)
        orch.perm_level = lambda: "ask"
        orch.ASK_TIMEOUT = 0.4
        run = {"id": "run-t1", "task": "测试", "cwd": tmp, "members": [], "permissions": []}
        member = {"id": "m1", "name": "实现者", "role": "implementer", "engine": "codex-node",
                  "permLevel": ""}
        d = orch._ask_permission(run, member, "execute", "运行命令",
                                 ["D:/ws/x"], None, {"command": "rm -rf D:/ws/x"})
        check("工作台：超时 → 返回 reject", d == "reject", d)
        it = audit.records(source="collab")
        check("工作台：超时 → 链里一条 result=timeout",
              len(it) == 1 and it[0]["result"] == "timeout", it)
        if it:
            check("工作台：ref 带 run/member/perm",
                  it[0]["ref"]["run"] == "run-t1" and it[0]["ref"]["member"] == "m1"
                  and bool(it[0]["ref"]["perm"]), it[0]["ref"])
            check("工作台：破坏性被标记", it[0]["action"]["destructive"] is True)
            check("工作台：note 写清等了多久", "等待" in it[0]["note"], it[0]["note"])

        # 人工批准（另一个线程等 _PENDING 出现后回复）
        def approver():
            for _ in range(40):
                with orch._LOCK:
                    ids = [k for k, p in orch._PENDING.items() if p.get("run") == "run-t2"]
                if ids:
                    orch.reply_permission(ids[0], "always")
                    return
                time.sleep(0.05)

        run2 = {"id": "run-t2", "task": "测试2", "cwd": tmp, "members": [], "permissions": []}
        orch.ASK_TIMEOUT = 6
        th = threading.Thread(target=approver)
        th.start()
        d2 = orch._ask_permission(run2, member, "execute", "运行命令",
                                  ["D:/ws/y"], None, {"command": "echo hi"})
        th.join()
        check("工作台：人工批准 → 返回 always", d2 == "always", d2)
        ok2 = [x for x in audit.records(source="collab")
               if x["ref"]["run"] == "run-t2" and x["result"] == "ok"]
        check("工作台：人工批准 → result=ok + approver=local",
              len(ok2) == 1 and (ok2[0].get("approver") or {}).get("id") == "local", ok2)

        # 写不进链时：审批流程照常结束（fail-open）
        audit.set_file(blocked)
        orch.ASK_TIMEOUT = 0.3
        run3 = {"id": "run-t3", "task": "测试3", "cwd": tmp, "members": [], "permissions": []}
        d3 = None
        try:
            d3 = orch._ask_permission(run3, member, "execute", "运行命令", [], None,
                                      {"command": "echo hi"})
            raised = False
        except Exception as exc:  # noqa: BLE001
            raised = exc
        check("工作台：链写不进去也不抛、照常返回", not raised and d3 == "reject",
              raised or d3)

        # ---------------- 8. 接线：面板内单会话审批（OpenCode 走反代，ACP 在 service 里写） ----------------
        audit.set_file(os.path.join(tmp, "panel.jsonl"))
        check("面板：不是权限答复 → 不记",
              audit.note_permission_reply("/api/session/x/message", "POST", b'{"text":"hi"}') is False
              and not audit.records())
        check("面板：GET 权限路径 → 不记",
              audit.note_permission_reply("/api/session/s/permission/p/reply", "GET", None) is False)
        check("面板：缺 decision → 不记",
              audit.note_permission_reply("/api/session/s/permission/p/reply", "POST", b"{}") is False)
        check("面板：认出来了（带 query 也认）",
              audit.note_permission_reply("/api/session/ses_9/permission/per_9/reply?r=1", "POST",
                                          b'{"decision":"always"}') is True)
        pit = audit.records(source="panel")
        check("面板：记了 session/perm + approver=local",
              len(pit) == 1 and pit[0]["ref"]["session"] == "ses_9"
              and pit[0]["ref"]["perm"] == "per_9"
              and (pit[0].get("approver") or {}).get("id") == "local", pit)
        check("面板：decision/result 正确",
              pit[0]["decision"] == "always" and pit[0]["result"] == "ok", pit[0])
        check("面板：按 session 过滤（新增 ref 维度）",
              len(audit.records(session="ses_9")) == 1
              and audit.records(session="ses_nope") == [])

        # ---------------- 9. CSV 留档（列头定死 + 能逐行重算） ----------------
        audit.set_file(os.path.join(tmp, "csv.jsonl"))
        audit.append("collab",
                     actor={"kind": "agent", "id": "codex-node", "name": "实现者", "member": "m1"},
                     action={"kind": "execute", "title": "运行命令",
                             "command": 'echo "a,b",\n第二行', "targets": ["D:/x", "D:/y"],
                             "destructive": True},
                     decision="once", result="ok",
                     approver={"kind": "user", "id": "local", "name": "owner"},
                     ref={"run": "r1", "member": "m1"}, note="ok")
        audit.append("snapshot", action={"kind": "undo", "title": "撤销这一轮"},
                     decision="undo", result="ok")
        csv_text = audit.to_csv(audit.records(limit=0)[::-1])
        head = csv_text.splitlines()[0]
        check("CSV：表头就是 CSV_FIELDS（顺序定死）", head == ",".join(audit.CSV_FIELDS), head[:60])
        check("CSV：列头含 prev/hash（所以能逐行重算）",
              "prev" in audit.CSV_FIELDS and "hash" in audit.CSV_FIELDS)
        check("CSV：没有前导说明行（Excel 能直接开）", head.startswith("v,seq,ts"), head[:20])
        back = audit.csv_records(csv_text)
        check("CSV：往返回 2 条", len(back) == 2, len(back))
        check("CSV：命令里的逗号/引号/换行都保住了",
              back[0]["action"]["command"] == 'echo "a,b",\n第二行',
              repr(back[0]["action"]["command"]))
        check("CSV：targets 用 JSON 列（分隔符不打架）",
              back[0]["action"]["targets"] == ["D:/x", "D:/y"], back[0]["action"]["targets"])
        check("CSV：bool / null approver 往返一致",
              back[0]["action"]["destructive"] is True and back[1]["approver"] is None)
        check("CSV：往返后链仍然校验通过", audit.verify(back)["ok"])
        csv_bad = csv_text.replace(",once,ok,execute,", ",always,ok,execute,")
        check("CSV：只改一格也算断（decision once→always）",
              ",always,ok,execute," in csv_bad
              and not audit.verify(audit.csv_records(csv_bad))["ok"])
        # BOM（Excel）也要能读
        check("CSV：带 BOM 也能读（Excel 打开的那份）",
              len(audit.csv_records("\ufeff" + csv_text)) == 2)

        # ---------------- 10. 接线：C3「AI 成员到底改了什么」 ----------------
        audit.set_file(os.path.join(tmp, "c3.jsonl"))
        real_ws_root = ca._ws_root
        real_cs = ca.snapshots.changes_since_latest
        ca._ws_root = lambda aid: tmp
        ca.snapshots.changes_since_latest = lambda root, scope: {
            "ok": True, "files": 3, "since": "20261024-2011", "root": tmp,
            "added": [{"path": "a.py"}], "changed": [{"path": "b.py", "size": 10}],
            "deleted": [{"path": "c.py"}], "renamed": [
                {"from": "old.js", "to": "new.js"}]}
        res = ca._record_turn_changes("11", "7", {"scope": "cc-7-11", "ts": "20261024-2011"})
        check("C3：改了几个文件 → 返回统计", res.get("files") == 3, res.get("files"))
        fits = audit.records(source="ccagent", kind="files")
        check("C3：入链一条（source=ccagent / kind=files）",
              len(fits) == 1 and fits[0]["action"]["kind"] == "files", fits)
        if fits:
            f0 = fits[0]
            check("C3：谁改的（成员名 + 群 + 成员 id）",
                  f0["actor"]["name"] == "yiyi" and f0["ref"]["chat"] == "7"
                  and f0["ref"]["aid"] == "11", f0["actor"])
            check("C3：改了哪些文件（含重命名 →）",
                  "a.py" in f0["action"]["targets"] and "c.py" in f0["action"]["targets"]
                  and any("old.js → new.js" == t for t in f0["action"]["targets"]),
                  f0["action"]["targets"])
            check("C3：带快照 scope/ts（能对回哪一轮）",
                  f0["ref"]["scope"] == "cc-7-11" and f0["ref"]["ts"] == "20261024-2011")
            check("C3：计数写进 note（+1 ~1 -1 ⇄1）",
                  "+1 ~1 -1" in f0["note"] and "⇄1" in f0["note"], f0["note"])
        check("C3：kind 过滤（不是 files 的取不到）",
              audit.records(kind="execute") == [] and len(audit.records(kind="files")) == 1)
        ca.snapshots.changes_since_latest = lambda root, scope: {
            "ok": True, "files": 0, "since": None,
            "added": [], "changed": [], "deleted": [], "renamed": []}
        before = len(audit.records(kind="files"))
        ca._record_turn_changes("11", "7", {"scope": "cc-7-11", "ts": "t2"})
        check("C3：这一轮没改动 → 不入链（别刷空记录）",
              len(audit.records(kind="files")) == before)
        check("C3：没有 scope（没指派/没工作目录）→ 安全返回",
              ca._record_turn_changes("11", "7", {}).get("files") == 0)
        ca._ws_root = real_ws_root
        ca.snapshots.changes_since_latest = real_cs

        # ---------------- 11. 保留期 / 归档 / 报告 ----------------
        audit.set_file(os.path.join(tmp, "rot.jsonl"))
        for i in range(12):
            audit.append("collab", actor={"kind": "agent", "id": "c", "name": "实现者"},
                         action={"kind": "execute", "title": "运行命令",
                                 "command": "echo %d" % i,
                                 "targets": ["D:/x"], "destructive": (i == 2)},
                         decision="once", result="ok",
                         approver={"kind": "user", "id": "local", "name": "owner"})
        check("归档：阈值 0 = 关闭", audit.rotate(max_bytes=0)["rotated"] is False)
        rot = audit.rotate(max_bytes=1, keep_bytes=600)
        check("归档：超阈值就切（moving 最旧的一段）",
              rot.get("rotated") is True and rot.get("moved", 0) >= 5, rot)
        af = audit.archive_files()
        check("归档：归档文件落在 _audit-archive（名字带 seq 区间）",
              len(af) == 1 and os.path.basename(af[0]).endswith(".jsonl")
              and rot.get("archive") in os.path.basename(af[0]), af)
        st2 = audit.stats()
        check("归档：stats 报归档条数/文件数",
              st2["archived"]["records"] == rot.get("moved")
              and st2["archived"]["files"] == 1, st2["archived"])
        seqs2 = sorted(int(x["seq"]) for x in audit.records(limit=0))
        check("归档：记录一条不少（归档 + 主文件合起来连续）",
              seqs2 == list(range(1, 14)), seqs2[:3] + ["..."] + seqs2[-2:])
        v2 = audit.verify()
        check("归档：整条链（跨文件）仍然校验通过", v2["ok"] and v2["gaps"] == 0, v2)
        arc_recs = [x for x in audit.records(limit=0) if x.get("action", {}).get("kind") == "archive"]
        check("归档：链里留了「归档」这条记录（不是偷偷删）",
              len(arc_recs) == 1 and arc_recs[0]["decision"] == "archive"
              and "seq" in arc_recs[0]["note"], arc_recs)
        r_after = audit.append("collab", action={"kind": "drop", "title": "之后"},
                               decision="drop", result="ok")
        check("归档：之后的追加继续接 seq（不断链）", r_after and r_after["seq"] == 14)
        check("归档：之后 verify 依然 ok", audit.verify()["ok"])
        # 归档文件被改 → 必须报出来
        with open(af[0], "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        o = json.loads(lines[1])
        o["result"] = "failed"
        lines[1] = json.dumps(o, ensure_ascii=False, separators=(",", ":"))
        with open(af[0], "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines) + "\n")
        check("归档：改归档文件里的记录 → verify 报断",
              not audit.verify()["ok"], audit.verify()["brokenCount"])

        # ---------------- 12. 一页报告（Markdown） ----------------
        rep = audit.report()
        check("报告：有标题 / 条数 / 链校验结论",
              "审计报告" in rep and "记录条数" in rep and "链校验" in rep, rep[:80])
        check("报告：结论分布（决定 / 结果 / 来源）",
              "结论分布" in rep and "允许一次" in rep and "本地协作" in rep)
        check("报告：破坏性操作单独一节（含命令与是否隔离）",
              "破坏性操作" in rep and "echo 2" in rep)
        check("报告：明细表有列头 + 不含成员正文",
              "| seq | 时间 | 来源 |" in rep and "成员正文" not in rep.split("## ")[1])
        empty_tmp = tempfile.mkdtemp(prefix="audit_rep_")
        audit.set_file(os.path.join(empty_tmp, "e.jsonl"))
        rep0 = audit.report()
        check("报告：空链也能出（写「（无）」不崩）",
              "记录条数 | 0" in rep0 and "（无）" in rep0, rep0[:200])

        # ---------------- 13. 签名（复用 ssh-keygen；没装就 SKIP） ----------------
        try:
            sys.path.insert(0, os.path.join(ROOT, "tools"))
            import cocraft_audit_sign as sign  # noqa: E402
            ssh = sign.ssh_keygen()
        except Exception:  # noqa: BLE001
            ssh, sign = "", None
        if not ssh:
            print("[SKIP] 签名：这台机器没有 ssh-keygen（OpenSSH 8.0+）")
        else:
            sdir = tempfile.mkdtemp(prefix="audit_sign_")
            key = os.path.join(sdir, "k")
            check("签名：keygen 生成密钥对",
                  sign.main(["keygen", "--key", key]) == 0
                  and os.path.isfile(key) and os.path.isfile(key + ".pub"))
            export_path = os.path.join(sdir, "export.json")
            audit.set_file(os.path.join(sdir, "s.jsonl"))
            audit.append("collab", action={"kind": "execute", "title": "run"},
                         decision="once", result="ok",
                         approver={"kind": "user", "id": "local", "name": "owner"})
            with open(export_path, "w", encoding="utf-8") as fh:
                json.dump(audit.export(), fh, ensure_ascii=False, indent=2)
            check("签名：sign 产出 .sig", sign.main(["sign", export_path, "--key", key]) == 0
                  and os.path.isfile(export_path + ".sig"))
            check("签名：verify 通过（未被改过）",
                  sign.main(["verify", export_path, "--pub", key + ".pub"]) == 0)
            with open(export_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            data["items"][0]["decision"] = "always"
            with open(export_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            check("签名：文件被改 → verify 失败（退出码 1）",
                  sign.main(["verify", export_path, "--pub", key + ".pub"]) == 1)

        # ---------------- 14. 「谁干的」：面板点的 vs 本机接口调的 ----------------
        # ⚠ 归档目录是按**目录**算的（`set_file` 换目录才换归档）→ 这一段用全新目录，别串前一段的归档
        audit.set_file(os.path.join(tempfile.mkdtemp(prefix="audit_via_"), "v.jsonl"))
        audit.append("snapshot", **rec(decision="drop", result="ok", approver=None,
                                       actor={"kind": "user", "id": "local",
                                              "name": "owner"}), via="ui")
        audit.append("snapshot", **rec(decision="drop", result="ok", approver=None,
                                       actor={"kind": "system", "id": "local-api",
                                              "name": "本机接口"}), via="api")
        audit.append("snapshot", **rec(decision="drop", result="ok"))
        vias = [(x["via"], x["actor"]["kind"]) for x in audit.records(limit=0)[::-1]]
        check("via：面板点的记 ui + actor=user；接口调的记 api + actor=system；没传默认 system",
              vias == [("ui", "user"), ("api", "system"), ("system", "agent")], vias)
        csvv = audit.to_csv(audit.records(limit=0)[::-1])
        check("via：CSV 也带上（而且能往返）",
              audit.verify(audit.csv_records(csvv))["ok"]
              and [x.get("via") for x in audit.csv_records(csvv)] == ["ui", "api", "system"],
              [x.get("via") for x in audit.csv_records(csvv)])
        # 0.2.42 之前的老记录（没有 via 字段）也要能验、能导
        seq0, prev0 = audit._tail()
        old = {"v": 1, "seq": seq0 + 1, "ts": "2026-10-24T10:00:00.000Z", "tsu": 1.0,
               "machine": "M", "actor": {"kind": "user", "id": "local", "name": "owner",
                                         "member": "", "role": "", "engine": ""},
               "approver": None,
               "action": {"kind": "drop", "title": "老记录", "command": "", "targets": [],
                          "destructive": False},
               "decision": "drop", "result": "ok", "quarantine": "", "source": "snapshot",
               "ref": {"run": "", "member": "", "perm": "", "chat": "", "aid": "",
                       "session": "", "scope": "", "ts": ""},
               "note": "no via", "prev": prev0}
        old["hash"] = audit._digest(old, prev0)
        with open(audit.AUDIT_FILE, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(old, ensure_ascii=False, separators=(",", ":")) + "\n")
        check("via：0.2.42 之前的老记录（没有 via 字段）照样校验通过", audit.verify()["ok"])
        mixed = audit.csv_records(audit.to_csv(audit.records(limit=0)[::-1]))
        check("via：老记录导出 CSV 不硬塞空 via（否则哈希会假报断）",
              audit.verify(mixed)["ok"] and "via" not in mixed[0], mixed[0].get("via"))

        # 真实链没被测试污染
        real = os.path.join(ROOT, "runtime", "state", "_audit.jsonl")
        polluted = False
        if os.path.isfile(real):
            with open(real, "r", encoding="utf-8", errors="replace") as fh:
                polluted = "run-t1" in fh.read()
        check("真实审计链未被测试写入（run-t1 不出现）", not polluted)
    finally:
        audit.set_file(real_file)
        ca.PERM_LOG_FILE = real_perm
        ca.find = real_find
        orch.RUNS_DIR = real_runs
        orch.perm_level = real_level
        orch.ASK_TIMEOUT = real_timeout
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d 项通过 / %d 项失败" % (N["ok"], N["bad"]))
    return 0 if N["bad"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
