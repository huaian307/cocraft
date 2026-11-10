# -*- coding: utf-8 -*-
r"""离线回归：**报错分类**（`ui 0.2.87`）。

起因（用户实测）：DeepSeek 明明回的是 `Insufficient Balance`，面板却只显示一串英文，
完全没有「余额不足，去充值」这种人话 —— 因为以前只有工作流里有一份**只认 4 种**的判据，
单聊 / 群聊各写各的，新写法（`402 Payment Required` / `credit balance is too low` /
`exceeded your current quota`）一个都不认。

现在统一到 `backend/errkind.py`，前端 `errKindOf()` 是它的**镜像**（老后端 / opencode 引擎也认得）。
本测试：
  1. 后端 22 个真实报错样例 → `kind` / `account` 全对（纯 Python，0 依赖）；
  2. **两边口径一致**：同一批文本交给前端 `errKindOf()`，`kind` 必须和后端一样
     （在无头 Edge 里跑真前端代码；Edge 不在就跳过这一半并说明）；
  3. `looks_error()` 的窄判据：正常回答里"解释"这些词不算故障。

用法：
    python tools\errkind_test.py
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                      # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(HERE, "backend")
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_errkind.html")
URL = "http://127.0.0.1:8787/_probe_errkind.html"
sys.path.insert(0, BACKEND)

import errkind  # noqa: E402

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# (期望 kind, 原始报错, 是否账户类)
CASES = [
    ("balance", "AcpError ACP 错误 -32603: Internal error: turn failed: Insufficient Balance (request_id: cefc94cc)", True),
    ("balance", "unexpected status 402 Payment Required: {\"error\":\"Insufficient Balance\"}", True),
    ("balance", "余额不足，请先充值再试", True),
    ("balance", "credit balance is too low", True),
    ("quota", "You exceeded your current quota, please check your plan", True),
    ("quota", "本月额度用尽", True),
    ("nokey", "Missing environment variable: DEEPSEEK_API_KEY.", True),
    ("nokey", "No API key found for provider deepseek", True),
    ("auth", "unexpected status 401 Unauthorized: Incorrect API key provided", True),
    ("auth", "invalid api key", True),
    ("model", "ModelUnavailableError: deepseek/deepseek-v3 is not available", False),
    ("model", "model not found: gpt-x", False),
    ("rate", "Rate limit reached for requests: please try again later", False),
    ("rate", "429 Too Many Requests", False),
    ("context", "This model's maximum context length is 128000 tokens", False),
    ("context", "上下文超限，请开新会话", False),
    ("cancelled", "session cancelled by user", False),
    ("timeout", "Request timed out after 60s", False),
    ("network", "Endpoint is unavailable", False),
    ("network", "502 Bad Gateway", False),
    ("network", "[Errno 104] ECONNRESET", False),
    ("network", "ConnectionRefused: Unable to connect. Is the computer able to access the url?", False),
    ("network", "fetch failed", False),
    ("agent", "ACP 错误 -32603: Internal error", False),
    ("unknown", "今天天气不错，出去走走吧", False),
]

# 正常回答里"解释"这些词，**不该**被当成故障
NOT_ERRORS = [
    "这次报错是 Insufficient Balance（余额不足）导致的，建议你充值后再试。",
    "我把 402 的处理逻辑改好了，现在会提示充值。",
    "这个接口在 401 时会返回 unauthorized，我们已经重试了。",
]


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
setTimeout(function () {
  var out = { kinds: [], err: null };
  try {
    var cases = __CASES__;
    for (var i = 0; i < cases.length; i++) {
      out.kinds.push(errKindOf(cases[i]).kind);
    }
  } catch (e) { out.err = String(e); }
  out.jsErrors = window.__probeErr;
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}, 2200);
</script>
""".replace("__CASES__", json.dumps([c[1] for c in CASES], ensure_ascii=False))


def build_probe() -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    stub = ("<script>window.fetch = function () { return Promise.resolve("
            "new Response('{}', {status: 200})); };"
            "try { navigator.sendBeacon = function () { return true; }; } catch (e) {}"
            "window.EventSource = function () { this.close = function () {}; };"
            "window.WebSocket = function () { this.send = function () {};"
            " this.close = function () {}; this.readyState = 1; };"
            "try { localStorage.setItem('cocraft.setup.v1','1'); } catch (e) {}"
            "</script>")
    out = src.replace('<script src="/app.js', stub + PROBE_JS + '<script src="/app.js')
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def front_kinds(edge: str):
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_errkind_dom.html")
    if os.path.exists(dom):
        os.remove(dom)
    build_probe()
    with open(dom, "wb") as fh:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-errkind"),
             "--virtual-time-budget=20000", "--dump-dom", URL],
            stdout=fh, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    html = io.open(dom, encoding="utf-8", errors="replace").read()
    m = re.search(r'<pre id="__probe__">(.*?)</pre>', html, re.S)
    if not m:
        return None
    d = json.loads(m.group(1))
    if d.get("err") or d.get("jsErrors"):
        print("    （前端探针异常：%s / %s）" % (d.get("err"), d.get("jsErrors")))
    return d.get("kinds")


def main() -> int:
    bad = 0
    total = 0
    print("\n=== 报错分类 · 后端（backend/errkind.py） ===")
    for want, text, account in CASES:
        total += 1
        got = errkind.classify(text)
        ok = (got["kind"] == want) and (bool(got["account"]) == bool(account))
        print("  %s %-9s ← %s%s" % ("OK  " if ok else "BAD ",
                                    got["kind"], text[:58],
                                    "" if ok else ("   [期望 %s / account=%s]" % (want, account))))
        if not ok:
            bad += 1
    print("\n=== 窄判据：正常回答里「提到」不算故障 ===")
    for t in NOT_ERRORS:
        total += 1
        ok = not errkind.looks_error(t)
        print("  %s %s" % ("OK  " if ok else "BAD ", t[:56]))
        if not ok:
            bad += 1
    total += 1
    ok_err = errkind.looks_error("AcpError ACP 错误 -32603: Insufficient Balance (request_id: 1)")
    print("  %s 真正的报错仍然认得出" % ("OK  " if ok_err else "BAD "))
    if not ok_err:
        bad += 1

    edge = find_edge()
    print("\n=== 前端镜像 errKindOf() 与后端同口径 ===")
    if not edge:
        print("  SKIP 没找到 msedge.exe（跳过后半段）")
    else:
        try:
            kinds = front_kinds(edge)
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        if kinds is None:
            total += 1
            bad += 1
            print("  BAD  拿不到前端探针结果")
        else:
            for (want, text, _a), got in zip(CASES, kinds):
                total += 1
                ok = got == want
                print("  %s %-9s ← %s" % ("OK  " if ok else "BAD ", got, text[:56]))
                if not ok:
                    bad += 1

    print("\n  total %d, failed %d" % (total, bad))
    print("  result: %s" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
