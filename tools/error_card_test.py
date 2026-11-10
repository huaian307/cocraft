# -*- coding: utf-8 -*-
r"""无头回归：**上游报错要说出人话**（`ui 0.2.92`）。

用户实测两次：「余额不足的报错依旧没有修复」。查 OpenCode 自己的 SQLite 才看清真凶——
失败消息**不是**"一段错误正文"，而是：

```json
{ "type": "assistant", "content": [], "finish": "error",
  "error": { "type": "provider.quota",
             "message": "Insufficient Balance (request_id: …)", "status": 402,
             "response": { "body": "{\"error\":{\"type\":\"insufficient_balance_error\",…}}" } } }
```

面板只读 `content` → 空数组 → 走"这一轮确实没有返回正文"的兜底 → 用户永远看不出是余额不足。

本测试用这条**真实形状**喂给渲染器，断言：
  1. 出的是**错误卡**（`.err-card`），不是"没有返回正文"；
  2. 标题是「余额不足」，带 HTTP 402；
  3. 有「充值 / 换 Key」这条建议（账户类）；
  4. 原始报错收进 `<details>`（可展开，不糊一屏）；
  5. 别的错误类型也认得（429 限流 / 连不上上游）；
  6. `finish:"error"` 但**没有 error 对象**时不崩；
  7. 正常消息不会被误判成失败；
  8. 零 JS 报错。

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\error_card_test.py
    python tools\error_card_test.py --shot resources\screenshots\error_card.png
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
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_errcard.html")
URL = "http://127.0.0.1:8787/_probe_errcard.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 真实形状（取自 OpenCode 的 session_message.data）
ERR_BALANCE = {
    "id": "msg_err_balance", "type": "assistant", "agent": "build",
    "model": {"id": "deepseek-flash", "providerID": "deepseek"},
    "time": {"created": 1793539760412, "completed": 1793539762171},
    "content": [], "finish": "error",
    "error": {"type": "provider.quota",
              "message": "Insufficient Balance (request_id: d51fc1b1-32f0-44c1-9e70-6cae47c1c007)",
              "status": 402,
              "response": {"body": "{\"error\":{\"message\":\"Insufficient Balance\","
                                   "\"type\":\"insufficient_balance_error\"}}"}},
}
ERR_RATE = {
    "id": "msg_err_rate", "type": "assistant", "agent": "build",
    "time": {"created": 1793539700000, "completed": 1793539701000},
    "content": [], "finish": "error",
    "error": {"type": "provider.quota",
              "message": "Free usage limit reached. Subscribe to OpenCode Go to keep going",
              "status": 429},
}
ERR_NET = {
    "id": "msg_err_net", "type": "assistant", "agent": "build",
    "time": {"created": 1793539600000, "completed": 1793539601000},
    "content": [], "finish": "error",
    "error": {"type": "provider.transport",
              "message": "ConnectionRefused: Unable to connect. Is the computer able to access the url?"},
}
ERR_BARE = {                            # finish=error 但没有 error 对象（别崩）
    "id": "msg_err_bare", "type": "assistant", "agent": "build",
    "time": {"created": 1793539500000, "completed": 1793539501000},
    "content": [], "finish": "error",
}
OK_MSG = {
    "id": "msg_ok", "type": "assistant", "agent": "build",
    "time": {"created": 1793539400000, "completed": 1793539401000},
    "content": [{"type": "text", "text": "正常回答，什么都没坏。"}],
}

STUB = r"""<script>
(function () {
  try {
    localStorage.setItem("cocraft.setup.v1", "1");
    localStorage.setItem("opencode-ui.setup.v1", "1");
  } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event",
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/audit/",
              "/snapshot/", "/quarantine/", "/ccagent/", "/orchestrate/", "/fs/"];
  var raw = window.fetch;
  var T = 1760000000000;
  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }
  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ location: { directory: "D:/ws" }, data: [] });
    if (path === "/api/model/default") return J({ data: null });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path === "/api/session/ses_err/message") return J({ data: window.__MSGS.slice().reverse(), cursor: null });
    if (s.indexOf("/api/session/ses_err") === 0) {
      if (/\/(model|agent|skill|command|permission|form)$/.test(path)) {
        return Promise.resolve(new Response("", { status: 204 }));
      }
      return J({ data: { id: "ses_err", title: "错误卡探针", time: { created: T, updated: T + 9000 },
                         location: { directory: "D:/ws" },
                         model: { id: "deepseek-flash", providerID: "deepseek" } } });
    }
    if (path === "/api/session") return J({ data: [{ id: "ses_err", title: "错误卡探针",
                                                    time: { created: T, updated: T + 9000 },
                                                    location: { directory: "D:/ws" } }], cursor: null });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () { this.send = function () {}; this.close = function () {}; this.readyState = 1; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
window.__MSGS = __MSGS__;

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 300; i++) {
      await __s(25);
      if ((state.messages || []).length >= 5) break;
    }
    await __s(200);

    var cards = document.querySelectorAll("#msg-static .err-card");
    out.info.cards = cards.length;
    out.info.first = __txt(cards[0]).slice(0, 120);
    step("★失败消息渲染成**错误卡**（不是「这一轮确实没有返回正文」）",
         cards.length >= 3 && !/确实没有返回正文/.test(__txt(document.getElementById("msg-static"))),
         "cards=" + cards.length + " | " + out.info.first);

    /* 1. 余额不足 */
    var acc = document.querySelector('#msg-static [data-mid="msg_err_balance"] .err-card');
    out.info.balance = __txt(acc).slice(0, 160);
    step("★余额不足：标题写「余额不足」+ HTTP 402",
         !!acc && /余额不足/.test(__txt(acc)) && /402/.test(__txt(acc)), out.info.balance);
    step("★余额不足：给「充值 / 换 Key」建议（账户类，且有 account 样式）",
         !!acc && /充值/.test(__txt(acc)) && /模型凭据/.test(__txt(acc))
         && acc.classList.contains("account"), acc ? acc.className : "(没有)");
    step("★原始报错收进 details（可展开，不糊一屏）",
         !!acc && !!acc.querySelector("details.err-raw"), acc ? acc.innerHTML.slice(0, 60) : "");

    /* 2. 限流 */
    var rate = document.querySelector('#msg-static [data-mid="msg_err_rate"] .err-card');
    step("★429 额度/限流也认得", !!rate && /额度|限流/.test(__txt(rate)), __txt(rate).slice(0, 80));

    /* 3. 网络 */
    var net = document.querySelector('#msg-static [data-mid="msg_err_net"] .err-card');
    step("★ConnectionRefused → 「连不上上游」", !!net && /连不上上游/.test(__txt(net)),
         __txt(net).slice(0, 80));

    /* 4. finish=error 但没有 error 对象：不崩、仍给一张卡 */
    var bare = document.querySelector('#msg-static [data-mid="msg_err_bare"] .err-card');
    step("★finish=error 但没有 error 对象 → 仍给卡、不崩", !!bare, bare ? __txt(bare).slice(0, 60) : "(没有)");

    /* 5. 正常消息不被误判 */
    step("★正常消息不会被当成失败", !document.querySelector('#msg-static [data-mid="msg_ok"] .err-card'));
  } catch (err) {
    out.crash = String(err && err.stack ? err.stack : err);
  }
  out.jsErrors = window.__probeErr;
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
""".replace("__MSGS__", json.dumps([ERR_RATE, ERR_NET, ERR_BARE, OK_MSG, ERR_BALANCE], ensure_ascii=False))

SHOT_JS = r"""<script>
window.__MSGS = __MSGS__;
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) { await __sh(25); if ((state.messages || []).length >= 5) break; }
  await __sh(400);
  try { document.getElementById("messages").scrollTop = 0; } catch (e) {}
})();
</script>
""".replace("__MSGS__", json.dumps([ERR_BALANCE, ERR_RATE, ERR_NET, ERR_BARE], ensure_ascii=False))


def build_probe(shot: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    extra = (STUB + (SHOT_JS if shot else PROBE_JS))
    out = src.replace('<script src="/app.js', extra + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str, shot: str = "") -> str:
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_errcard_dom.html")
    if os.path.exists(dom):
        os.remove(dom)
    args = [edge, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check",
            "--user-data-dir=" + os.path.join(tmp, "edge-errcard"),
            "--window-size=1400,1000", "--virtual-time-budget=30000"]
    args += (["--screenshot=" + shot] if shot else ["--dump-dom"])
    with open(dom, "wb") as fh:
        proc = subprocess.Popen(args + [URL], stdout=fh, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    if shot:
        return ""
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot = os.path.abspath(sys.argv[i + 1] if i + 1 < len(sys.argv)
                               else os.path.join(HERE, "resources", "screenshots", "error_card.png"))
        build_probe(shot=True)
        try:
            render(edge, shot=shot)
            print("  截图:", shot, "存在=", os.path.isfile(shot))
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        return 0

    build_probe()
    print("探针页已生成，正在无头渲染（真跑 app.js）…")
    try:
        html = render(edge)
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)
            print("探针页已清理")

    m = re.search(r'<pre id="__probe__">(.*?)</pre>', html, re.S)
    if not m:
        print("没拿到探针结果。DOM 前 600 字符：")
        print(html[:600])
        return 1
    d = json.loads(m.group(1))
    print("\n=== 上游报错要说人话（错误卡） · 无头回归 ===")
    bad = 0
    for name, ok, extra in d.get("steps", []):
        print("  %s %s%s" % ("OK  " if ok else "MISS", name, ("   [" + extra + "]") if extra and not ok else ""))
        if not ok:
            bad += 1
    if d.get("crash"):
        print("\n!! 探针自身抛异常：\n" + d["crash"])
        bad += 1
    errs = d.get("jsErrors") or []
    print("\n  %s 全程零 JS 报错%s" % ("OK  " if not errs else "MISS", ("  " + str(errs[:3])) if errs else ""))
    if errs:
        bad += 1
    if os.environ.get("PROBE_INFO"):
        print("\n  info: " + json.dumps(d.get("info"), ensure_ascii=False))
    total = len(d.get("steps", [])) + 1
    print("\n  total %d, failed %d" % (total, bad))
    print("  result: %s" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
