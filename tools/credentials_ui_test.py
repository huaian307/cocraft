# -*- coding: utf-8 -*-
r"""无头回归：模型凭据（多 Key）界面 —— 真跑 app.js。

覆盖：
  1. 设置里列出多条凭据（同一 env 两条也能同时显示），只显示掩码 + 默认标记；
  2. 点「＋ 添加凭据」→ 填表 → POST add（带 secret）；
  3. 点「设默认」→ POST default，默认标记移动；
  4. 点「改」→ 留空 Key → POST update **不带 secret**（不误清）；
  5. 点「删」→ POST delete，行消失；
  6. Agent 编辑里能选「用哪个凭据」，保存时 body.credentials 带上选中 id；
  7. 全程零 JS 报错。

跑法：`python tools\credentials_ui_test.py`（需要面板服务在 8787 上跑着）
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
PROBE = os.path.join(WEB, "_probe_cred.html")
URL = "http://127.0.0.1:8787/_probe_cred.html"

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
  window.__posts = [];
  window.__agentPost = null;
  window.__cred = [
    { id: "c1", label: "DeepSeek · 个人", env: "DEEPSEEK_API_KEY", provider: "deepseek",
      masked: "sk-a…11（20 位）", isDefault: true, usedBy: [] },
    { id: "c2", label: "DeepSeek · 公司", env: "DEEPSEEK_API_KEY", provider: "deepseek",
      masked: "sk-b…22（20 位）", isDefault: false, usedBy: ["a1"] },
    { id: "c3", label: "Anthropic", env: "ANTHROPIC_API_KEY", provider: "anthropic",
      masked: "sk-a…33（24 位）", isDefault: true, usedBy: [] }
  ];
  function credData() {
    var envs = {}, defs = {};
    window.__cred.forEach(function (x) {
      envs[x.env] = 1;
      if (x.isDefault) defs[x.env] = x.id;
    });
    return { items: window.__cred.map(function (x) { return Object.assign({}, x); }),
             defaults: defs, envNames: Object.keys(envs).sort() };
  }
  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = String((o && o.method) || "GET").toUpperCase();
    if (path === "/engine/acp/credentials") {
      if (method === "GET") return J({ ok: true, data: credData() });
      var b = {};
      try { b = JSON.parse((o && o.body) || "{}"); } catch (e) {}
      window.__posts.push(b);
      if (b.action === "add") {
        var id = "c" + (window.__cred.length + 1);
        var first = !window.__cred.some(function (x) { return x.env === b.env; });
        window.__cred.push({ id: id, label: b.label || b.env, env: b.env,
                             provider: b.provider || "", masked: "sk-…（测试）",
                             isDefault: first, usedBy: [] });
      } else if (b.action === "update") {
        var it = window.__cred.filter(function (x) { return x.id === b.id; })[0];
        if (it) {
          if (b.label) it.label = b.label;
          if (b.env) it.env = b.env;
          if (b.provider != null) it.provider = b.provider;
          if (b.secret) it.masked = "sk-…（已改）";
        }
      } else if (b.action === "delete") {
        var env = (window.__cred.filter(function (x) { return x.id === b.id; })[0] || {}).env;
        window.__cred = window.__cred.filter(function (x) { return x.id !== b.id; });
        var nxt = window.__cred.filter(function (x) { return x.env === env; })[0];
        if (nxt && !window.__cred.some(function (x) { return x.env === env && x.isDefault; })) nxt.isDefault = true;
      } else if (b.action === "default") {
        var tgt = window.__cred.filter(function (x) { return x.id === b.id; })[0] || {};
        window.__cred.forEach(function (x) { if (x.env === tgt.env) x.isDefault = false; });
        if (tgt.id) tgt.isDefault = true;
      }
      return J({ ok: true, data: credData() });
    }
    if (path === "/engine/acp/agents") {
      if (method === "GET") {
        return J({ ok: true, active: "a1", detail: {}, agents: [
          { id: "a1", label: "codex", command: ["cmd.exe"], cwd: "", provider: "deepseek",
            providerGuess: "", available: true, missing: [], builtin: false, note: "",
            credentials: {} }
        ] });
      }
      try { window.__agentPost = JSON.parse((o && o.body) || "{}"); } catch (e) {}
      return J({ ok: true, active: "a1", detail: {}, agents: [
        { id: "a1", label: "codex", command: ["cmd.exe"], cwd: "", provider: "deepseek",
          providerGuess: "", available: true, missing: [], builtin: false, note: "",
          credentials: (window.__agentPost || {}).credentials || {} }
      ] });
    }
    if (path === "/engine/acp/baseline") return J({ ok: true, data: { envNames: [], env: {} } });
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
function __providers() { return Array.prototype.slice.call(document.querySelectorAll("#cred-list .cred-prov")); }
function __accts() { return Array.prototype.slice.call(document.querySelectorAll("#cred-list .cred-row")); }
function __provBy(text) {
  var all = __providers();
  for (var i = 0; i < all.length; i++) if (__t(all[i]).indexOf(text) >= 0) return all[i];
  return null;
}
function __acctBy(text) {
  var all = __accts();
  for (var i = 0; i < all.length; i++) if (__t(all[i]).indexOf(text) >= 0) return all[i];
  return null;
}
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 300)]); }
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (window.__appLoaded && window.state) break; }
    await __s(300);
    document.getElementById("btn-cfg").click();
    await __s(600);

    out.info.providers = __providers().map(__t);
    step("★默认只列「厂商」，不铺开账号", __providers().length === 2 && __accts().length === 0,
         JSON.stringify(out.info.providers));
    var ds = __provBy("DeepSeek"), an = __provBy("Anthropic");
    step("★每个厂商显示账号数（DeepSeek 2 个）", !!ds && /2 个账号/.test(__t(ds)), ds ? __t(ds) : "");
    step("★只有一个账号的厂商标 1 个", !!an && /1 个账号/.test(__t(an)), an ? __t(an) : "");
    step("★厂商行显示默认账号名", !!ds && /默认：/.test(__t(ds)), ds ? __t(ds) : "");

    if (ds) ds.click();
    await __s(200);
    out.info.accts = __accts().map(__t);
    step("★点厂商 → 展开账号", __accts().length === 2, JSON.stringify(out.info.accts));
    step("★账号行显示掩码（不是明文）",
         !!__acctBy("DeepSeek · 个人") && /…/.test(__t(__acctBy("DeepSeek · 个人")))
         && !/sk-[a-z0-9]{8,}/i.test(__t(__acctBy("DeepSeek · 个人"))),
         __acctBy("DeepSeek · 个人") ? __t(__acctBy("DeepSeek · 个人")) : "");
    step("★默认账号有「默认」标记", /默认/.test(__t(__acctBy("DeepSeek · 个人"))));

    var dsCollapse = __provBy("DeepSeek");
    if (dsCollapse) dsCollapse.click();
    await __s(150);
    step("★再点厂商 → 收起账号", __accts().length === 0, "accts=" + __accts().length);
    var dsReopen = __provBy("DeepSeek");
    if (dsReopen) dsReopen.click();
    await __s(150);

    // 添加
    document.getElementById("cred-add").click();
    await __s(200);
    document.getElementById("cred-label").value = "DeepSeek · 备用";
    document.getElementById("cred-env").value = "DEEPSEEK_API_KEY";
    document.getElementById("cred-provider").value = "deepseek";
    document.getElementById("cred-secret").value = "sk-new-123456789";
    document.getElementById("cred-save").click();
    await __s(500);
    var add = window.__posts[window.__posts.length - 1] || {};
    out.info.add = add;
    step("★添加 → POST add（带 env / secret）",
         add.action === "add" && add.env === "DEEPSEEK_API_KEY" && !!add.secret,
         JSON.stringify(add));
    await __s(200);
    var ds2 = __provBy("DeepSeek");
    step("★添加后厂商账号数变 3", !!ds2 && /3 个账号/.test(__t(ds2)), ds2 ? __t(ds2) : "");
    step("★添加后自动展开那一家", __accts().length === 3, "accts=" + __accts().length);

    // 设默认
    var comp = __acctBy("DeepSeek · 公司");
    var defBtn = comp ? comp.querySelector("[data-cred-def]") : null;
    if (defBtn) defBtn.click();
    await __s(400);
    var last = window.__posts[window.__posts.length - 1] || {};
    step("★设默认 → POST default", last.action === "default" && last.id === "c2", JSON.stringify(last));
    await __s(100);
    step("★默认标记移到新的那条", /默认/.test(__t(__acctBy("DeepSeek · 公司"))),
         __t(__acctBy("DeepSeek · 公司")));

    // 编辑（留空 Key = 不改）
    var comp2 = __acctBy("DeepSeek · 公司");
    var ed = comp2 ? comp2.querySelector("[data-cred-edit]") : null;
    if (ed) ed.click();
    await __s(200);
    document.getElementById("cred-label").value = "DeepSeek · 公司（改）";
    document.getElementById("cred-secret").value = "";
    document.getElementById("cred-save").click();
    await __s(400);
    var up = window.__posts[window.__posts.length - 1] || {};
    step("★编辑：留空 Key 时 POST 不带 secret", up.action === "update" && !("secret" in up),
         JSON.stringify(up));
    step("★编辑后名称已更新", !!__acctBy("公司（改）"));

    // 删除
    var delRow = __acctBy("DeepSeek · 备用");
    var delBtn = delRow ? delRow.querySelector("[data-cred-del]") : null;
    if (delBtn) delBtn.click();
    await __s(400);
    var dl = window.__posts[window.__posts.length - 1] || {};
    step("★删除 → POST delete", dl.action === "delete", JSON.stringify(dl));
    var ds3 = __provBy("DeepSeek");
    step("★删除后厂商账号数回到 2", !!ds3 && /2 个账号/.test(__t(ds3)), ds3 ? __t(ds3) : "");

    // Agent 编辑：选凭据
    await loadAcpAgents();
    await editAgentCommand("a1");
    await __s(300);
    var sel = document.getElementById("ae-cred");
    var opts = sel ? Array.prototype.slice.call(sel.options) : [];
    out.info.opts = opts.map(function (o) { return o.text; });
    step("★Agent 编辑里有「跟随默认」+ 两个 DeepSeek 凭据",
         opts.length >= 3 && /跟随默认/.test(opts[0].text)
         && opts.some(function (o) { return /个人/.test(o.text); })
         && opts.some(function (o) { return /公司/.test(o.text); }),
         JSON.stringify(out.info.opts));
    sel.value = "c2";
    await saveAgentEdit();
    await __s(400);
    var ap = window.__agentPost || {};
    step("★保存 agent 时带上 credentials",
         !!ap.credentials && ap.credentials.DEEPSEEK_API_KEY === "c2", JSON.stringify(ap));
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
    dom = os.path.join(tmp, "probe_cred_dom.html")
    err = os.path.join(tmp, "probe_cred_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probecred"),
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

    print("\n=== 模型凭据（多 Key）· 无头回归 ===")
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
