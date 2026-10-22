# -*- coding: utf-8 -*-
r"""无头回归：★ACP「一轮进行中补充要求」的 UI 表现。

背景（2026-10-22 用户反馈）：ACP 引擎以前**一轮没跑完就拒绝新 prompt**（后端 409），
用户必须等整轮跑完才能把新条件发过去。现在后端把这条**排队**，本轮结束自动发送；
前端要能看出来「已排队」，以及「面板重启时没发出去的」要标「未送达」。

本测试用假后端（stub fetch），断言：
  1. 历史里 `queued:true` 的用户消息 → 显示「已排队」小圆片
  2. `undelivered:true` → 显示「未送达」（红字）
  3. 正在生成时点发送 → POST /prompt 带正文、**不出现 error-box**、横幅提示「已加入队列」
  4. 全程零 JS 报错

跑法：python tools\inbox_queue_test.py
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_inbox.html")
URL = "http://127.0.0.1:8787/_probe_inbox.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/engine/", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/ccagent/", "/orchestrate/",
              "/snapshot/", "/quarantine/", "/themes"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  var NOW = Date.now();
  window.__prompts = [];
  window.__MODELS = [{ id: "mock-fast", modelID: "mock-fast", providerID: "mock",
                       name: "Mock", variants: [], cost: null, capabilities: { tools: true } }];
  window.__MSGS = [
    { id: "u1", type: "user", text: "先做基础工作", time: { created: NOW - 5000 } },
    { id: "a1", type: "assistant", model: { id: "mock-fast", providerID: "mock" },
      time: { created: NOW - 4000, completed: NOW - 1000 },
      content: [{ type: "text", text: "第一轮做完了。" }] },
    { id: "u2", type: "user", text: "补充要求：再加一条",
      time: { created: NOW - 800 }, queued: true },
    { id: "u3", type: "user", text: "重启前没发出去",
      time: { created: NOW - 700 }, undelivered: true }
  ];
  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = String((o && o.method) || "GET").toUpperCase();
    if (path === "/api/model") return J({ data: window.__MODELS });
    if (path === "/api/model/default") return J({ data: window.__MODELS[0] });
    if (/^\/api\/session\/[^/]+\/prompt$/.test(path) && method === "POST") {
      var b = {};
      try { b = JSON.parse((o && o.body) || "{}"); } catch (e) {}
      window.__prompts.push(b);
      return J({ ok: true, queued: true, position: 1, messageID: "u_new" });
    }
    if (/^\/api\/session\/[^/]+\/model$/.test(path)) {
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) {
      // 新→旧（和真实接口一致）
      return J({ data: window.__MSGS.slice().reverse(), cursor: null });
    }
    if (/^\/api\/session\/[^/]+$/.test(path)) {
      return J({ data: { id: "s1", title: "排队测试", location: { directory: "D:/ws" },
                         model: { id: "mock-fast", providerID: "mock" } } });
    }
    if (path === "/api/session") {
      return J({ data: [{ id: "s1", title: "排队测试", location: { directory: "D:/ws" },
                          model: { id: "mock-fast", providerID: "mock" } }], cursor: null });
    }
    if (path === "/engine/status") return J({ active: "acp", available: ["opencode", "acp"], engines: [] });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () { this.readyState = 1; this.send = function () {}; this.close = function () {}; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __t(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __msgWith(text) {
  var all = document.querySelectorAll("#messages .msg.user");
  for (var i = 0; i < all.length; i++) {
    if (__t(all[i]).indexOf(text) >= 0) return all[i];
  }
  return null;
}
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 300)]); }
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (window.__appLoaded && window.state) break; }
    await __s(300);
    await loadSessions();
    await openSession("s1");
    await __s(500);

    var qm = __msgWith("补充要求");
    out.info.queued = qm ? __t(qm) : null;
    step("★排队中的用户消息显示「已排队」", !!qm && /已排队/.test(__t(qm)), out.info.queued);
    var um = __msgWith("重启前没发出去");
    out.info.undelivered = um ? __t(um) : null;
    step("★重启时没发出去的显示「未送达」", !!um && /未送达/.test(__t(um)), out.info.undelivered);

    // 生成中点发送：后端返回 queued → 横幅提示、不报错
    var box = document.getElementById("messages");
    out.info.errBefore = box.querySelectorAll(".error-box").length;
    document.getElementById("input").value = "再补充一条";
    await send();
    await __s(500);
    var sent = window.__prompts[window.__prompts.length - 1] || {};
    out.info.sent = sent;
    step("★点发送 → POST /prompt 带上正文", sent.text === "再补充一条", JSON.stringify(sent));
    step("★横幅提示「已加入队列」",
         /已加入队列/.test(__t(document.getElementById("banner"))),
         __t(document.getElementById("banner")));
    step("★排队不是错误：没有 error-box",
         box.querySelectorAll(".error-box").length === out.info.errBefore,
         "before=" + out.info.errBefore + " after=" + box.querySelectorAll(".error-box").length);
    step("★输入框已清空（消息没丢在框里）",
         (document.getElementById("input").value || "") === "",
         document.getElementById("input").value);
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
"""


def build_probe() -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + PROBE_JS + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str) -> str:
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_inbox_dom.html")
    err = os.path.join(tmp, "probe_inbox_err.txt")
    for f in (dom, err):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeinbox"),
             "--window-size=1400,920",
             "--virtual-time-budget=30000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    edge = find_edge()
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
        print("[BAD] 没拿到探针结果（渲染失败）。DOM 前 300 字符：")
        print(html[:300])
        return 1
    d = json.loads(m.group(1))

    print("\n=== ACP 排队补充要求 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name
              + (("   [" + extra[:220] + "]") if extra else ""))
        if not okv:
            fails += 1
    if d.get("crash"):
        print("\n  [BAD] 探针崩了：\n" + d["crash"])
        fails += 1
    errs = d.get("jsErrors") or []
    if errs:
        print("\n  JS 报错:", errs)
        fails += 1
    print("\n  total %d, failed %d" % (len(d.get("steps", [])), fails))
    print("  result:", "PASS" if fails == 0 else "FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
