# -*- coding: utf-8 -*-
r"""无头回归：★上下文估算（dsh 这类只给 prompt usage、不发 usage_update 的 agent）。

dsh 的 `session/prompt` 结果里有 `usage`（totalTokens / inputTokens / outputTokens …），
但没有 `usage_update` 通知。后端用它估一个 `context {used, size:0, estimated:true}`，
前端显示「上下文 ~N」（不编窗口百分比）。

覆盖：
  1. `context_from_usage()`：totalTokens 优先 / input+output 兜底 / 没有数字返回 None
  2. `_record_usage()`：没有真实 context 时写入 estimate
  3. 有真实 `usage_update`（size>0）时**绝不覆盖**
  4. 已有 estimate 时下一轮更新它
  5. 正常 tokens 累加不受影响
  6. 返回值只在真的写入 estimate 时非空（前端靠它推事件）

跑法：python tools\context_estimate_test.py
"""
from __future__ import annotations

import os
import sys
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(HERE, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from engines.acp.service import AcpService, context_from_usage  # noqa: E402

PASS = 0
FAIL = 0
BAD = []


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s%s" % (label, (" " + extra) if extra else ""))
    else:
        FAIL += 1
        BAD.append(label)
        print("  [BAD]  %s%s" % (label, (" " + extra) if extra else ""))


def dummy_service():
    svc = object.__new__(AcpService)
    svc._lock = threading.RLock()
    return svc


def main() -> int:
    print("== 1. context_from_usage ==")
    c = context_from_usage({"totalTokens": 20937, "inputTokens": 20924, "outputTokens": 13})
    ok("优先用 totalTokens", c and c["used"] == 20937, repr(c))
    c2 = context_from_usage({"inputTokens": 1200, "outputTokens": 300})
    ok("没有 totalTokens → input+output", c2 and c2["used"] == 1500, repr(c2))
    c3 = context_from_usage({"totalTokens": 0, "inputTokens": 0, "outputTokens": 0})
    ok("全是 0 → None（不编数字）", c3 is None, repr(c3))
    ok("非 dict → None", context_from_usage(None) is None)

    print("\n== 2. _record_usage：没有真实 context → 写 estimate ==")
    svc = dummy_service()
    s = {"tokens": {}}
    est = svc._record_usage(s, {"totalTokens": 900, "inputTokens": 800, "outputTokens": 100}, "end_turn")
    ok("写入 estimate（used/size/estimated）",
       est and est.get("used") == 900 and est.get("size") == 0 and est.get("estimated") is True,
       repr(est))
    ok("会话上的 context 也是 estimate",
       (s.get("context") or {}).get("used") == 900 and (s.get("context") or {}).get("estimated") is True,
       repr(s.get("context")))
    ok("tokens 累加正常", s["tokens"].get("input") == 800 and s["tokens"].get("output") == 100,
       repr(s.get("tokens")))

    print("\n== 3. 有真实 usage_update（size>0）→ 绝不覆盖 ==")
    s2 = {"tokens": {}, "context": {"used": 500000, "size": 1000000}}
    est2 = svc._record_usage(s2, {"totalTokens": 900}, None)
    ok("不返回 estimate（前端不该推错事件）", est2 is None, repr(est2))
    ok("真实 context 原样保留", s2["context"] == {"used": 500000, "size": 1000000}, repr(s2["context"]))

    print("\n== 4. 已有 estimate → 下一轮更新 ==")
    s3 = {"tokens": {}, "context": {"used": 100, "size": 0, "estimated": True}}
    est3 = svc._record_usage(s3, {"totalTokens": 200}, None)
    ok("estimate 被更新到 200", est3 and est3.get("used") == 200, repr(est3))
    ok("仍然是 estimated", (s3["context"] or {}).get("estimated") is True, repr(s3.get("context")))

    print("\n== 5. 没有 usage → 什么都不做 ==")
    s4 = {"tokens": {}}
    est4 = svc._record_usage(s4, None, None)
    ok("None usage → 返回 None", est4 is None)
    ok("不会凭空造 context", not s4.get("context"), repr(s4.get("context")))

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
