# -*- coding: utf-8 -*-
r"""无头回归：面板内「更新 agent」（node 模式 npm agent 的自主更新）。

真跑 `app.js`（无头 Edge），把 `/agent/catalog`、`/agent/latest`、`/agent/update`、
`/agent/install/status` 全 stub 掉，只验**界面行为**：

1. 已装的显示版本号（`v2.0.0`）；
2. 有新版 → 显示「最新 v2.2.2」+ **更新**按钮；
3. 已是最新 → **没有**更新按钮（不摆空按钮）；
4. 点「更新」→ `POST /agent/update {id}`（带确认）；
5. 过程中日志头写「正在更新」；完成后横幅「已更新：…（v…）」；
6. 全程零 JS 报错。

跑法：`python tools\agent_update_ui_test.py`（需要面板服务在 8787 上跑着）
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_agentup.html")
URL = "http://127.0.0.1:8787/_probe_agentup.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/ccagent/", "/orchestrate/",
              "/snapshot/", "/quarantine/", "/themes"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  window.__upd = null;
  window.__statusCalls = 0;
  window.__latestUrls = [];
  window.__catalog = [
    { id: "codex-acp", label: "Codex（ACP 适配器）", pkg: "@agentclientprotocol/codex-acp",
      provider: "deepseek", note: "自带 codex CLI", installed: true, registered: "codex-acp",
      dir: "D:/ws/agents/npm/codex-acp", version: "2.0.0", latest: "", hasUpdate: false,
      ready: true, reason: "" },
    { id: "claude-code-acp", label: "Claude Code（ACP 适配器）",
      pkg: "@zed-industries/claude-code-acp", provider: "anthropic", note: "需要登录态",
      installed: true, registered: "claude-code-acp", dir: "D:/ws/agents/npm/claude-code-acp",
      version: "0.16.2", latest: "", hasUpdate: false, ready: true, reason: "" },
    { id: "opencode-ai", label: "OpenCode（原生 exe）", pkg: "opencode-ai",
      provider: "", note: "自带平台原生 exe", installed: false, registered: "",
      dir: "D:/ws/agents/npm/opencode-ai", version: "", latest: "", hasUpdate: false,
      ready: true, reason: "" }
  ];
  window.__latest = [
    { id: "codex-acp", installed: true, version: "2.0.0",
      pkg: "@agentclientprotocol/codex-acp", latest: "2.2.2", hasUpdate: true },
    { id: "claude-code-acp", installed: true, version: "0.16.2",
      pkg: "@zed-industries/claude-code-acp", latest: "0.16.2", hasUpdate: false },
    { id: "opencode-ai", installed: false, version: "", pkg: "opencode-ai",
      latest: "", hasUpdate: false }
  ];
  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = String((o && o.method) || "GET").toUpperCase();
    if (path === "/agent/catalog") {
      return J({ ok: true, data: { items: window.__catalog, root: "D:/ws/agents/npm",
                                   status: { busy: false } } });
    }
    if (path === "/agent/latest") {
      window.__latestUrls.push(s);
      return J({ ok: true, data: { items: window.__latest, cached: false } });
    }
    if (path === "/agent/update" && method === "POST") {
      var b = {};
      try { b = JSON.parse((o && o.body) || "{}"); } catch (e) {}
      window.__upd = b;
      return J({ ok: true, id: b.id, kind: "update" });
    }
    if (path === "/agent/install/status") {
      window.__statusCalls += 1;
      if (window.__statusCalls === 1) {
        return J({ ok: true, data: { busy: true, id: "codex-acp", phase: "下载安装（npm）",
                                     kind: "update", log: ["$ npm install --prefix … @latest"] } });
      }
      return J({ ok: true, data: { busy: false, phase: "完成", ok: true, kind: "update",
                                   id: "codex-acp", agent: "codex-acp", version: "2.2.2",
                                   command: ["node", "x"], log: ["[OK] 已更新到 2.2.2"] } });
    }
    if (path === "/api/model") return J({ data: [] });
    if (path === "/engine/status") return J({ active: "acp", available: ["opencode", "acp"], engines: [] });
    if (path === "/api/session") return J({ data: [], cursor: null });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () { this.readyState = 1; this.send = function () {}; this.close = function () {}; };
  window.confirm = function () { return true; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __t(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __row(id) {
  var all = document.querySelectorAll("#anew-i-list .anew-i-row");
  for (var i = 0; i < all.length; i++) {
    var b = all[i].querySelector("b");
    if (b && __t(b) === id) return all[i];
  }
  return all.length ? all[0] : null;
}
function __rowByName(name) {
  var all = document.querySelectorAll("#anew-i-list .anew-i-row");
  for (var i = 0; i < all.length; i++) {
    var b = all[i].querySelector("b");
    if (b && __t(b).indexOf(name) >= 0) return all[i];
  }
  return null;
}
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 300)]); }
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (window.__appLoaded && window.state) break; }
    await __s(300);
    openAgentsNew();
    for (var k = 0; k < 40; k++) { await __s(50); if (document.querySelectorAll("#anew-i-list .anew-i-row").length) break; }
    await __s(250);

    var cx = __rowByName("Codex");
    var cl = __rowByName("Claude");
    out.info.codex = __t(cx);
    out.info.claude = __t(cl);
    step("★已装的那行显示版本号", !!cx && /v2\.0\.0/.test(__t(cx)), out.info.codex);
    step("★有新版 → 显示「最新 v2.2.2」", !!cx && /最新 v2\.2\.2/.test(__t(cx)), out.info.codex);
    var up = cx ? cx.querySelector("[data-ainst-up]") : null;
    step("★有新版 → 出现「更新」按钮", !!up && __t(up) === "更新");
    var upCl = cl ? cl.querySelector("[data-ainst-up]") : null;
    step("★已是最新 → 没有「更新」按钮", !upCl, cl ? __t(cl) : "(没有这一行)");
    step("★已是最新 → 有「已是最新」文字", !!cl && /已是最新/.test(__t(cl)), cl ? __t(cl) : "");

    if (up) { up.click(); }
    await __s(160);
    out.info.post = window.__upd;
    step("★点更新 → POST /agent/update（带 id）",
         !!window.__upd && window.__upd.id === "codex-acp", JSON.stringify(window.__upd));
    var lh = document.querySelector("#anew-i-lh") || document.querySelector(".anew-i-lh");
    out.info.head1 = __t(lh);
    step("★过程中日志头写「正在更新」", /正在更新/.test(out.info.head1), out.info.head1);

    // 轮询是 1s 一跳：第一次看到 busy、第二次看到完成 → 至少等 ~2.4s
    await __s(2700);
    var lh2 = document.querySelector(".anew-i-lh");
    out.info.head2 = __t(lh2);
    step("★完成后日志头写「更新完成」+ 版本", /更新完成/.test(out.info.head2) && /2\.2\.2/.test(out.info.head2),
         out.info.head2);
    out.info.banner = __t(document.getElementById("banner"));
    step("★横幅提示「已更新」", /已更新/.test(out.info.banner), out.info.banner);

    var chk = document.getElementById("anew-i-check");
    step("★有「检查更新」按钮", !!chk);
    if (chk) chk.click();
    await __s(400);
    out.info.latestUrls = window.__latestUrls;
    step("★检查更新 → 带 force=1（不等 10 分钟缓存）",
         (window.__latestUrls || []).some(function (u) { return /force=1/.test(u); }),
         JSON.stringify((window.__latestUrls || []).slice(-2)));
    out.info.note = __t(document.getElementById("anew-i-note"));
    step("★检查完说明行写「有 N 个可以更新」", /有 1 个 agent 可以更新/.test(out.info.note), out.info.note);
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
    dom = os.path.join(tmp, "probe_agentup_dom.html")
    err = os.path.join(tmp, "probe_agentup_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probeagentup"),
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

    print("\n=== 更新 agent（node 模式 npm agent）· 无头回归 ===")
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
