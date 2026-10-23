# -*- coding: utf-8 -*-
r"""无头回归：权限自动放行的请求体 + 复制（WebView2 顺序）

背景（2026-10-23 用户报）：
  * 权限等级 = 全部放行时，命令 0.1 秒内被取消，agent 说 "aborted by user" ——
    根因是自动放行把 `body` 传成**对象**（api() 不序列化 → "[object Object]"）→ 后端读不到
    decision → 按 reject → agent 取消。
  * 复制失败 —— WebView2 里 clipboard API 常 reject；旧实现先 await 它，失败后再 execCommand
    已经**丢掉用户手势**。现在改成**先同步 execCommand**。

本测试真跑 app.js，断言：
  1. 自动放行 POST 的 body 是**合法 JSON** 且 decision=once（不是 [object Object]）；
  2. 复制**先走 execCommand**（Clipboard API 一次都不调），按钮变「已复制」；
  3. 代码块「复制」只复制代码本身；
  4. 零 JS 报错。

跑法：`python tools\permission_copy_ui_test.py`（需要面板服务在 8787 上跑着）
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
PROBE = os.path.join(WEB, "_probe_permcopy.html")
URL = "http://127.0.0.1:8787/_probe_permcopy.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  // ★ 记录 execCommand 复制到的文本；Clipboard API 故意 reject（模拟 WebView2 没权限）
  window.__copied = [];
  window.__clipCalls = 0;
  var rawExec = document.execCommand ? document.execCommand.bind(document) : function () { return false; };
  document.execCommand = function (cmd) {
    if (String(cmd) === "copy") {
      if (window.__execFail) return false;
      var all = document.querySelectorAll("textarea[readonly]");
      var ta = all.length ? all[all.length - 1] : null;   // 临时框是最后 append 的那个
      window.__copied.push(ta ? ta.value : "");
      return true;
    }
    return rawExec.apply(document, arguments);
  };
  try {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText: function () { window.__clipCalls += 1; return Promise.reject(new Error("denied")); } },
    });
  } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/ccagent/", "/orchestrate/",
              "/snapshot/", "/quarantine/", "/themes"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  var NOW = Date.now();
  window.__permReply = null;
  window.__permBodyText = "";
  window.__MESSAGES = [
    { id: "u1", type: "user", text: "帮我复制一段", time: { created: NOW - 5000 } },
    { id: "a1", type: "assistant", model: { id: "mock-fast", providerID: "mock" },
      time: { created: NOW - 4000, completed: NOW - 1000 },
      content: [
        { type: "reasoning", text: "（思考不该被复制）" },
        { type: "text", text: "第一段要复制的内容。\n\n```js\nconst a = 1;\n```\n" }
      ] },
    // ★Codex 常见形态：最终正文后面还跟着一条 tool 状态更新 → 完成后的正文必须显示
    { id: "a2", type: "assistant", model: { id: "mock-fast", providerID: "mock" },
      time: { created: NOW - 3000, completed: NOW - 2000 },
      content: [
        { type: "reasoning", text: "（思考）" },
        { type: "text", text: "完成后的反馈文字" },
        { type: "tool", name: "shell", state: { status: "completed", input: { command: "echo" } } }
      ] },
    // 还在生成：这段是"边做边说"的旁白 → 主界面不该显示
    { id: "a3", type: "assistant", model: { id: "mock-fast", providerID: "mock" },
      time: { created: NOW - 800 },
      content: [
        { type: "reasoning", text: "（思考）" },
        { type: "text", text: "进行中的旁白" },
        { type: "tool", name: "shell", state: { status: "running", input: { command: "echo" } } }
      ] },
    // ★Codex：多段「碎碎念」+ 最终回答 → 碎碎念收进「过程」，最终单独显示
    { id: "a4", type: "assistant", agent: "codex-node",
      model: { id: "mock-fast", providerID: "mock" },
      time: { created: NOW - 7000, completed: NOW - 6000 },
      content: [
        { type: "reasoning", text: "（思考）" },
        { type: "text", text: "我先看看环境。" },
        { type: "tool", name: "shell", state: { status: "completed", input: { command: "dir" } } },
        { type: "text", text: "再下载模型……" },
        { type: "tool", name: "shell", state: { status: "completed", input: { command: "curl" } } },
        { type: "text", text: "最终：图已经生成好了。" }
      ] }
  ];
  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = String((o && o.method) || "GET").toUpperCase();
    if (path === "/api/permission/level") {
      return J({ level: "all", levels: ["ask", "read", "edit", "exec", "all"] });
    }
    if (/^\/api\/session\/[^/]+\/permission$/.test(path)) {
      return J({ data: [{ id: "perm_1", sessionID: "s1", kind: "execute",
                          action: "Run command", resources: [], message: "跑个命令" }] });
    }
    if (/^\/api\/session\/[^/]+\/permission\/[^/]+\/reply$/.test(path) && method === "POST") {
      window.__permBodyText = String((o && o.body) || "");
      try { window.__permReply = JSON.parse(window.__permBodyText); } catch (e) { window.__permReply = null; }
      return J({ ok: true });
    }
    if (/^\/api\/session\/[^/]+\/form$/.test(path)) return J({ data: [] });
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) {
      return J({ data: window.__MESSAGES.slice().reverse(), cursor: null });
    }
    if (/^\/api\/session\/[^/]+$/.test(path)) {
      return J({ data: { id: "s1", title: "复制测试", location: { directory: "D:/ws" },
                         model: { id: "mock-fast", providerID: "mock" } } });
    }
    if (path === "/api/session") return J({ data: [], cursor: null });
    if (path === "/api/model") return J({ data: [] });
    if (path === "/engine/status") return J({ active: "acp", available: ["opencode", "acp"], engines: [] });
    if (path === "/api/skill") return J({ data: [] });
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
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 300)]); }
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (window.__appLoaded && window.state) break; }
    await __s(300);
    await openSession("s1");
    await __s(400);

    // 0) Codex 的最终反馈：正文后面跟一条 tool 状态更新，也要显示
    var boxText = __t(document.getElementById("messages"));
    step("★完成后的正文（后面跟 tool 状态）仍显示", /完成后的反馈文字/.test(boxText), boxText.slice(-160));
    step("★还在生成时的旁白不显示", !/进行中的旁白/.test(boxText), boxText.slice(-160));

    // Codex 碎碎念：过程进折叠块，最终回答单独显示
    var ch = document.querySelector("#messages .chatter");
    step("★Codex 的碎碎念收进「过程」折叠块", !!ch && /过程/.test(__t(ch)) && /2 段/.test(__t(ch)),
         ch ? __t(ch) : "(没有 .chatter)");
    step("★折叠块里保留碎碎念原文", !!ch && /我先看看环境/.test(__t(ch)), ch ? __t(ch).slice(0, 120) : "");
    step("★Codex 最终回答单独显示", /最终：图已经生成好了/.test(boxText), boxText.slice(-160));
    var cbtn = document.querySelector('#messages [data-copy="a4"]');
    if (cbtn) cbtn.click();
    await __s(300);
    var ctext = (window.__copied || [])[window.__copied.length - 1] || "";
    step("★复制 Codex 回答只取最终一段", /最终：图已经生成好了/.test(ctext) && !/我先看看环境/.test(ctext),
         ctext.slice(0, 120));

    // 1) 自动放行：请求体必须是合法 JSON
    await pollAsks();
    for (var k = 0; k < 20; k++) { await __s(50); if (window.__permReply) break; }
    out.info.bodyText = window.__permBodyText;
    out.info.permReply = window.__permReply;
    step("★自动放行的 body 是合法 JSON（不是 [object Object]）",
         !!window.__permReply && typeof window.__permReply === "object", out.info.bodyText);
    step("★decision=once（放行，不是取消）",
         !!window.__permReply && window.__permReply.decision === "once", JSON.stringify(window.__permReply));
    out.info.banner = __t(document.getElementById("banner"));
    step("★横幅提示已按等级自动放行", /自动放行/.test(out.info.banner), out.info.banner);

    // 2) 消息复制：先 execCommand（Clipboard API 一次都不调）
    var msgBtn = document.querySelector('#messages [data-copy="a1"]');
    out.info.hasCopy = !!msgBtn;
    step("★助手消息有「复制」按钮", !!msgBtn);
    if (msgBtn) msgBtn.click();
    await __s(400);
    var copied = (window.__copied || []).join("\n---\n");
    out.info.copied = copied;
    step("★复制走 execCommand（不依赖 Clipboard API）",
         (window.__copied || []).length >= 1 && window.__clipCalls === 0,
         "copied=" + (window.__copied || []).length + " clipCalls=" + window.__clipCalls);
    step("★复制到的是正文（不含思考）",
         /第一段要复制的内容/.test(copied) && !/思考不该被复制/.test(copied), copied.slice(0, 160));
    step("★按钮变「已复制」", /已复制/.test(__t(msgBtn)), __t(msgBtn));

    // 3) 代码块复制：只复制代码
    var codeBtn = document.querySelector("#messages .copy-code");
    step("★代码块有「复制」", !!codeBtn);
    if (codeBtn) codeBtn.click();
    await __s(300);
    var last = (window.__copied || [])[window.__copied.length - 1] || "";
    out.info.code = last;
    step("★代码块复制只取代码本身", /const a = 1;/.test(last) && !/第一段/.test(last), last);

    // 4) 剪贴板两条路都失败 → 兜底文本框（全选 + Ctrl+C）
    window.__execFail = true;
    await __s(1300);                       // 等按钮从「已复制」复位
    if (msgBtn) msgBtn.click();
    await __s(500);
    var fb = document.getElementById("copy-fb");
    var ta = document.getElementById("copy-fb-text");
    out.info.fb = !!fb && !fb.hidden;
    step("★两条路都失败 → 弹兜底文本框", out.info.fb);
    step("★兜底框里是完整正文", !!ta && /第一段要复制的内容/.test(ta.value), ta ? ta.value.slice(0, 120) : "");
    var okBtn = document.getElementById("copy-fb-ok");
    if (okBtn) okBtn.click();
    await __s(200);
    step("★「知道了」能关掉兜底框", !!fb && fb.hidden);
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
    dom = os.path.join(tmp, "probe_permcopy_dom.html")
    err = os.path.join(tmp, "probe_permcopy_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probepermcopy"),
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

    print("\n=== 自动放行请求体 + 复制 · 无头回归 ===")
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
