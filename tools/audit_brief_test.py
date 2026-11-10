#!/usr/bin/env python
r"""audit_brief_test.py -- 「人话简报」（L2）回归。

口径：**确定性生成** —— 同一批记录 → 同一份正文（只有"生成时间"那行会变）。
所以这里可以直接断言正文：数字、事件选取、章节顺序、附录里的链尾。

覆盖：
  1. brief() 的统计（破坏性 / 未确认 / 隔离 / 已恢复 / 待恢复 / 拒绝 / 敏感读取 / 失败 / 文件改动）
  2. 一句话结论的规则（有未确认 → ⚠；有待恢复 → ⚠；1 次 vs N 次的措辞；0 条记录；断链）
  3. 「需要你知道的 3 件事」：按严重度挑、同一条记录合并、最多 3 条、不重复
  4. render_brief()：章节顺序 / 附录带链尾与复核命令 / 区间导出提示 / 可复现（固定时间 → 逐字相同）
  5. human_csv()：9 列 / 不带 hash·prev / 带 BOM / 恢复点语义 / 序号可回查
  6. CLI（tools/cocraft_audit_brief.py）：导出 JSON 与原始 .jsonl 都能读，md/csv 都能出

⚠ 全程用临时审计文件，不写真实链。
"""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

import audit  # noqa: E402

N = {"ok": 0, "bad": 0}
T0 = 1793000000.0          # 固定时间戳：让"按天"可断言、结果可复现


def check(name, cond, extra=""):
    if cond:
        N["ok"] += 1
        print("[OK] %s" % name)
    else:
        N["bad"] += 1
        print("[BAD] %s%s" % (name, ("  -> " + str(extra)[:300]) if extra is not None else ""))


def seed():
    """七条记录：一次已恢复的破坏性操作、一次**没恢复**的破坏性操作、拒绝、失败、文件改动…"""
    audit.set_file(os.path.join(tempfile.mkdtemp(prefix="brief_"), "a.jsonl"))
    q1 = audit.append(
        "collab", actor={"kind": "agent", "id": "codex", "name": "impl", "member": "m1"},
        action={"kind": "execute", "title": "run", "command": "rm -rf D:/客户资料",
                "targets": ["D:/客户资料"], "destructive": True},
        decision="confirm", result="ok",
        approver={"kind": "user", "id": "local", "name": "张工"},
        quarantine="q1", ref={"run": "r1", "member": "m1", "ts": "t1"}, ts=T0)
    audit.append(
        "snapshot", actor={"kind": "user", "id": "local", "name": "张工"},
        action={"kind": "restore", "title": "从忏悔室恢复"}, decision="restore", result="ok",
        ref={"scope": "q1"}, quarantine="q1", note="恢复 21 项", ts=T0 + 300)
    audit.append(
        "collab", actor={"kind": "agent", "id": "dsh", "name": "rev", "member": "m2"},
        action={"kind": "execute", "title": "run", "command": "del /s D:/旧报表",
                "targets": ["D:/旧报表"], "destructive": True},
        decision="always", result="ok", quarantine="q2",      # 没有 approver → 未人工确认
        ref={"run": "r1", "member": "m2"}, ts=T0 + 600)
    audit.append(
        "panel", actor={"kind": "agent", "id": "codex", "name": "impl"},
        action={"kind": "read", "title": "读凭据", "command": "Get-Content _credentials.json"},
        decision="reject", result="denied", ts=T0 + 700)
    audit.append(
        "panel", actor={"kind": "agent", "id": "codex", "name": "impl"},
        action={"kind": "execute", "title": "run", "command": "npm test"},
        decision="once", result="failed", approver={"kind": "user", "id": "local", "name": "张工"},
        ts=T0 + 800)
    audit.append(
        "ccagent", actor={"kind": "agent", "id": "codex", "name": "impl", "member": "m1"},
        action={"kind": "files", "title": "本轮改了哪些文件",
                "targets": ["app.js", "style.css"]},
        decision="auto", result="ok", ref={"run": "r1", "member": "m1", "scope": "files"},
        ts=T0 + 900)
    audit.append(
        "panel", actor={"kind": "agent", "id": "codex", "name": "impl"},
        action={"kind": "read", "title": "读文件", "command": "Get-Content README.md"},
        decision="once", result="ok", ts=T0 + 1000)
    return q1


def main() -> int:
    seed()
    items = audit.records()

    # ---------------- 1. 统计 ----------------
    f = audit.brief(items=items)
    check("条数 = 7", f["count"] == 7, f["count"])
    check("破坏性 2 次 / 其中未人工确认 1 次",
          f["destructive"] == {"total": 2, "unacked": 1}, f["destructive"])
    check("隔离 2 条 / 已恢复 1 / 待恢复 1",
          f["quarantine"] == {"total": 2, "restored": 1, "pending": 1}, f["quarantine"])
    check("拒绝 1 次 / 敏感读取被拦 1 次 / 失败 1 次",
          (f["rejected"], f["secret_blocked"], f["failed"]) == (1, 1, 1),
          (f["rejected"], f["secret_blocked"], f["failed"]))
    check("文件改动 1 轮 / 涉及 2 个文件",
          f["files"] == {"turns": 1, "count": 2}, f["files"])
    check("按人：impl 5 次（批 2 / 拒 1）、张工 1 次、rev 1 次",
          [(p["name"], p["total"], p["approved"], p["rejected"]) for p in f["people"]]
          == [("impl", 5, 2, 1), ("rev", 1, 0, 0), ("张工", 1, 0, 0)], f["people"])
    check("按天：1 天 / 7 条 / 2 条不可撤销",
          len(f["days"]) == 1 and f["days"][0]["count"] == 7
          and f["days"][0]["destructive"] == 2, f["days"])
    check("链校验通过、seq 范围 1..7", f["ok"] and (f["first"], f["last"]) == (1, 7),
          (f["ok"], f["first"], f["last"]))
    check("不是区间导出（subset=False）", f["subset"] is False, f["subset"])

    # ---------------- 2. 一句话结论（规则） ----------------
    h = f["headline"]
    check("结论里点名「没有人工确认」和「还没有恢复」",
          "没有人工确认" in h and "还没有恢复" in h, h)
    check("结论里有条数/人数", "7" in h and "3 个操作者" in h, h)
    check("结论不会把「已恢复」说成「待恢复」", "1 次误删已从忏悔室恢复" in h, h)

    one = audit.brief(items=[x for x in items if int(x.get("seq") or 0) == 1])
    check("只有 1 次破坏性操作时措辞是「已经你确认」（不是「全部」）",
          "1 次不可撤销操作已经你确认" in one["headline"], one["headline"])
    empty = audit.brief(items=[])
    check("0 条记录 → 直接说「没有任何记录」",
          empty["count"] == 0 and "没有任何记录" in empty["headline"], empty["headline"])
    broken = copy.deepcopy(items)
    for r in broken:
        if int(r.get("seq") or 0) == 2:
            r["decision"] = "reject"          # 改内容 → 哈希对不上
    fb = audit.brief(items=broken)
    check("断链 → 结论第一句就是「校验未通过」",
          (not fb["ok"]) and fb["headline"].startswith("⚠") and "校验未通过" in fb["headline"],
          fb["headline"])

    # ---------------- 3. 需要你知道的 3 件事 ----------------
    hl = f["highlight"]
    check("最多 3 件事", 0 < len(hl) <= 3, len(hl))
    check("第一条是警告（未确认 / 待恢复）", bool(hl and hl[0]["warn"]), hl[:1])
    check("同一条记录只出现一次（不重复）",
          len({x["seq"] for x in hl}) == len(hl), [x["seq"] for x in hl])
    check("「已恢复 + 经确认」合并成一行说明",
          any("经 张工 确认" in x["note"] and "恢复" in x["note"] for x in hl),
          [x["note"] for x in hl])
    check("每条都带 时间 / 谁 / 谁批的 / 做了什么",
          all(x.get("ts") and x.get("who") and x.get("approver") and x.get("what") for x in hl),
          hl[:1])

    # ---------------- 4. render_brief：顺序 / 附录 / 可复现 ----------------
    md = audit.render_brief(f, generated_at=T0)
    order = ["## 一句话", "## 需要你知道的", "## 按人", "## 按天", "## 附录"]
    pos = [md.find(x) for x in order]
    check("章节顺序固定（结论 → 3 件事 → 按人 → 按天 → 附录）",
          all(p >= 0 for p in pos) and pos == sorted(pos), pos)
    check("附录带链尾与独立复核命令",
          "链尾：" in md and "cocraft_audit_verify.py" in md, "")
    check("同一份数据 + 固定生成时间 → 正文逐字相同",
          audit.render_brief(f, generated_at=T0) == md, "")
    f2 = audit.brief(items=audit.records())
    check("brief() 是纯函数（两次 facts 完全一致）", f2 == f, "")
    sub = audit.brief(items=items[:3], subset=True)
    check("区间导出会明确提示 subset",
          "区间导出" in audit.render_brief(sub, generated_at=T0), "")

    # ---------------- 5. human_csv ----------------
    csv_text = audit.human_csv(items)
    lines = [x for x in csv_text.splitlines() if x.strip()]
    check("人话 CSV 有 BOM（Excel 打开中文不乱码）", csv_text.startswith("\ufeff"))
    check("表头 9 列且含「序号」",
          lines[0].lstrip("\ufeff") == "时间,操作人,动作,对象,结论,批准人,不可撤销,恢复点,序号",
          lines[0])
    check("人话 CSV 不含 hash / prev（那是指纹层的事）",
          ("hash" not in csv_text.lower()) and ("prev" not in csv_text.lower()), "")
    check("7 条记录 → 7 行数据", len(lines) == 8, len(lines))
    row_d = [x for x in lines[1:] if "rm -rf" in x]
    cols_d = row_d[0].strip('"').split('","') if row_d else []
    check("破坏性那行标「是」且恢复点是忏悔室",
          len(cols_d) == 9 and cols_d[6] == "是" and cols_d[7] == "忏悔室", cols_d)
    row_r = [x for x in lines[1:] if "从忏悔室恢复" in x]
    cols_r = row_r[0].strip('"').split('","') if row_r else []
    check("恢复那行不再把「恢复点」写成忏悔室（避免误导）",
          len(cols_r) == 9 and cols_r[7] == "", cols_r)
    check("每行都有序号（能回查链）",
          all(x.rstrip('"').split('","')[-1].strip('"').isdigit() for x in lines[1:]), "")

    # ---------------- 6. CLI ----------------
    tmp = tempfile.mkdtemp(prefix="brief_cli_")
    exp = os.path.join(tmp, "export.json")
    with io.open(exp, "w", encoding="utf-8") as fh:
        json.dump(audit.export(), fh, ensure_ascii=False, indent=2)
    tool = os.path.join(HERE, "cocraft_audit_brief.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, tool, exp], capture_output=True, env=env, timeout=120)
    out = (p.stdout or b"").decode("utf-8", "replace")
    check("CLI（导出 JSON）→ 退出码 0 且出简报",
          p.returncode == 0 and "# AI 操作简报" in out, out[:200])
    p2 = subprocess.run([sys.executable, tool, exp, "--format", "csv"],
                        capture_output=True, env=env, timeout=120)
    out2 = (p2.stdout or b"").decode("utf-8", "replace")
    check("CLI --format csv → 表头对",
          p2.returncode == 0 and "操作人" in out2, out2[:160])
    rawl = os.path.join(tmp, "raw.jsonl")
    with io.open(rawl, "w", encoding="utf-8") as fh:
        for r in items:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    p3 = subprocess.run([sys.executable, tool, rawl], capture_output=True, env=env, timeout=120)
    check("CLI 也能读原始 .jsonl",
          p3.returncode == 0 and "# AI 操作简报" in (p3.stdout or b"").decode("utf-8", "replace"),
          (p3.stdout or b"")[:160])
    outfile = os.path.join(tmp, "brief.md")
    p4 = subprocess.run([sys.executable, tool, exp, "--out", outfile],
                        capture_output=True, env=env, timeout=120)
    check("CLI --out 写出文件（UTF-8 BOM）",
          p4.returncode == 0 and os.path.isfile(outfile)
          and io.open(outfile, encoding="utf-8-sig").read().startswith("# AI 操作简报"),
          p4.stdout[:160])
    p5 = subprocess.run([sys.executable, tool, os.path.join(tmp, "nope.json")],
                        capture_output=True, env=env, timeout=60)
    check("CLI 找不到文件 → 明确报错、退出码 1",
          p5.returncode == 1 and "找不到文件" in (p5.stdout or b"").decode("utf-8", "replace"),
          p5.stdout[:160])

    print("\n%d 项 / 通过 %d / 失败 %d" % (len(N.values()) and N["ok"] + N["bad"], N["ok"], N["bad"]))
    return 0 if N["bad"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
