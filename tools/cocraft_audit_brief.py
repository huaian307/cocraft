#!/usr/bin/env python
r"""cocraft_audit_brief.py -- 把审计导出变成「一页人话简报」。

给谁用：
  · 老板 / 合规 / 甲方看不见 JSONL 和 34 列 CSV，他们要看的是
    「这周 AI 干了什么、有没有出事、要不要你管」；
  · 这份简报由**规则确定性生成**（同一份导出 → 同一份正文，只有生成时间会变），
    不调用任何模型 —— 所以它能当证据，也能被复核。

用法：
  python tools\cocraft_audit_brief.py 导出.json                    :: Markdown 打到屏幕
  python tools\cocraft_audit_brief.py 导出.json --out 周报.md
  python tools\cocraft_audit_brief.py 导出.json --format csv --out 明细.csv
  type 导出.jsonl | python tools\cocraft_audit_brief.py -           :: 原始 .jsonl 也行

输入支持：
  · `GET /audit/export?format=json&download=1` 的导出文件（{format, items:[...]}）
  · 原始 `_audit.jsonl`（每行一条；把归档文件按顺序拼起来也认）

⚠ 本工具**只读**：不改导出、不写审计链、不联网。
"""

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def load_items(path: str):
    """读导出 JSON 或原始 JSONL → (记录列表, 是否区间导出)。"""
    if path in ("-", "/dev/stdin", "stdin"):
        text = sys.stdin.buffer.read().decode("utf-8-sig", "replace")
    else:
        with io.open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read()
    s = text.lstrip()
    if s.startswith("{"):
        try:
            data = json.loads(s)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                subset = bool((data.get("chain") or {}).get("subset"))
                return [x for x in data["items"] if isinstance(x, dict)], subset
        except Exception:  # noqa: BLE001
            pass                              # 不是整份导出 → 当 JSONL 逐行读
    items = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and obj.get("seq") is not None:
            items.append(obj)
    return items, False


def main(argv=None) -> int:
    _force_utf8()
    ap = argparse.ArgumentParser(description="cocraft audit -> human brief (deterministic)")
    ap.add_argument("file", help="导出 JSON / 原始 .jsonl / -（stdin）")
    ap.add_argument("--format", default="md", choices=["md", "csv"],
                    help="md = 一页简报（默认）；csv = 9 列人话版（给 Excel）")
    ap.add_argument("--out", default="", help="写到文件（默认打到 stdout）")
    args = ap.parse_args(argv)

    if not (args.file in ("-", "stdin") or os.path.isfile(args.file)):
        print("[BAD] 找不到文件：%s" % args.file)
        return 1
    try:
        import audit
        items, subset = load_items(args.file)
    except Exception as exc:  # noqa: BLE001
        print("[BAD] 读不了这份审计：%s" % exc)
        return 1
    if not items:
        print("[BAD] 里面没有审计记录（是不是导出成了别的东西？）")
        return 1

    if args.format == "csv":
        text = audit.human_csv(items)
        body = text.encode("utf-8")
    else:
        facts = audit.brief(items=items, subset=subset)
        text = audit.render_brief(facts)
        body = text.encode("utf-8-sig")
        print("[OK] %d 条记录 · 链%s · 破坏性 %d（未确认 %d）· 待恢复 %d"
              % (facts["count"], "完整" if facts["ok"] else "断裂 %d 处" % facts["broken"],
                 facts["destructive"]["total"], facts["destructive"]["unacked"],
                 facts["quarantine"]["pending"]))
    if args.out:
        with open(args.out, "wb") as fh:
            fh.write(body)
        print("[OK] 已写出：%s（%d 字节）" % (args.out, len(body)))
        return 0
    sys.stdout.write(text if args.format == "csv" else text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
