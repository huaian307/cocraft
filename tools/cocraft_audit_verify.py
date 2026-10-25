# -*- coding: utf-8 -*-
r"""★校验审计链：`tools/cocraft_audit_verify.py <文件>`

两种输入都认：

1. **导出文件**（`cocraft-audit-*.json`，面板「导出审计」或 `GET /audit/export` 的产物）：
   逐条重算哈希、对照文件头的 `records` / `tail`，区间导出（`chain.subset=true`）会如实标注。
2. **原始链文件**（`runtime/state/_audit.jsonl`）：整条链全量校验。

输出纯文本（`[OK]` / `[BAD]`），退出码 0 = 通过 / 1 = 有问题 —— 方便放进 CI 或交给甲方自行复核。

用法：
    python tools\cocraft_audit_verify.py runtime\state\_audit.jsonl
    python tools\cocraft_audit_verify.py 下载\cocraft-audit-2026-10-24T12-30-00.123Z.json
    python tools\cocraft_audit_verify.py export.json --quiet
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

try:
    sys.stdout.reconfigure(errors="replace")   # 控制台是 GBK，别因为一个字符崩掉
except Exception:  # noqa: BLE001
    pass

import audit  # noqa: E402


def _load(path: str):
    """→ (记录列表, 文件头, 是否区间导出, 读文件的方式)"""
    low = path.lower()
    if low.endswith(".csv"):
        # ★CSV（Excel 留档用）：每行都带 prev/hash，逐条重算即可；文件级链尾看响应头
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
        items = audit.csv_records(text)
        return items, {}, (bool(items) and int(items[0].get("seq") or 0) > 1), "csv"
    with open(path, "r", encoding="utf-8") as fh:
        raw = fh.read()
    if low.endswith(".jsonl"):
        # 主文件（`_audit.jsonl`）旁边有归档目录 → 一起读（归档 + 主文件拼起来才是完整链）
        paths = [path]
        sib = os.path.join(os.path.dirname(os.path.abspath(path)), "_audit-archive")
        kind = "chain"
        if os.path.isdir(sib):
            arch = [os.path.join(sib, n) for n in sorted(os.listdir(sib))
                    if n.endswith(".jsonl")]
            if arch:
                paths = arch + [path]
                kind = "chain+archive"
        items = []
        for p in paths:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    items.append(json.loads(line))
        return items, {}, False, kind
    data = json.loads(raw)
    if not isinstance(data, dict) or "items" not in data:
        raise ValueError("不是 cocraft 审计导出文件（缺少 items）")
    items = [x for x in (data.get("items") or []) if isinstance(x, dict)]
    header = {k: data.get(k) for k in
              ("format", "version", "exported", "records", "tail", "engine", "machine")}
    chain = data.get("chain") or {}
    subset = bool(chain.get("subset"))
    if not subset and items and int(items[0].get("seq") or 0) > 1:
        subset = True
    return items, header, subset, "export"


def main(argv: list) -> int:
    args = [a for a in argv if not a.startswith("--")]
    quiet = "--quiet" in argv
    target = args[0] if args else os.path.join(ROOT, "runtime", "state", "_audit.jsonl")
    if not os.path.exists(target):
        print("[BAD] 找不到文件：%s" % target)
        return 1
    try:
        items, header, subset, kind = _load(target)
    except Exception as exc:  # noqa: BLE001
        print("[BAD] 读不懂这个文件：%s: %s" % (type(exc).__name__, exc))
        return 1

    res = audit.verify(items, subset=subset)
    bad = 0

    if not quiet:
        if header:
            print("文件      ：%s" % target)
            print("格式      ：%s v%s（%s）"
                  % (header.get("format") or "-", header.get("version") or "-",
                     header.get("engine") or "-"))
            print("导出时间  ：%s" % (header.get("exported") or "-"))
            print("条数      ：%s（文件头写 %s）" % (res["count"], header.get("records")))
        if kind == "csv":
            print("文件      ：%s（CSV 留档，逐行重算哈希）" % target)
            print("提示      ：文件级链尾/条数在响应头 X-Audit-Tail / X-Audit-Records")
        elif kind == "chain+archive":
            print("文件      ：%s（主文件 + 旁边的归档目录一起校验）" % target)
        elif not header:
            print("文件      ：%s（原始链，全量校验）" % target)
        print("序号      ：%s .. %s" % (res["first"], res["last"]))
        print("链尾      ：%s" % res["tail"])
        if res.get("subset"):
            print("范围      ：区间导出（首条 prev=%s… 只当锚点，无法就地证明衔接）"
                  % (res.get("anchor") or "")[:23])
        if res["gaps"]:
            print("空洞      ：%d 处（区间导出的正常现象；完整导出应为 0）" % res["gaps"])

    if header and header.get("tail") and header["tail"] != res["tail"]:
        print("[BAD] 文件头链尾与内容对不上：头=%s 实算=%s" % (header["tail"], res["tail"]))
        bad += 1
    if header and header.get("records") is not None and int(header["records"]) != res["count"]:
        print("[BAD] 文件头条数对不上：头=%s 实际=%s" % (header["records"], res["count"]))
        bad += 1

    if res["ok"] and not bad:
        print("[OK] 链完整：%d 条，哈希逐条对得上，prev 全部可解析" % res["count"])
        return 0

    for b in res["broken"]:
        if b.get("line"):
            print("[BAD] 第 %s 行：%s" % (b["line"], b.get("reason")))
        else:
            print("[BAD] 第 %d 条（seq %s）：%s" % (b.get("index") or 0, b.get("seq"),
                                                  b.get("reason")))
        if not quiet and b.get("expected"):
            print("      应为 %s" % b["expected"])
            print("      实为 %s" % b["actual"])
    print("[BAD] 共 %d 处问题（%s）" % (res["brokenCount"] + bad, kind))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
