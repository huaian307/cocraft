# -*- coding: utf-8 -*-
r"""无头回归：消息导航（回到底部 / 问题导航 / 上一问 / 下一问 / 回到本问）。

真跑 `frontend/app.js`，把 `/api/session*` 换成「6 轮提问 + 长回答」的内存假数据：

  1. 在底部时只显示「问题」，不显示「回到底部」
  2. 往上滚 → 出现「回到底部」；点它 → 回到最底并隐藏按钮
  3. 「问题」弹层列出自上次加载以来的每次提问（序号 / 摘要 / 时间），当前那一问高亮
  4. 点某一行 → 跳到那一轮的提问（并闪一下）
  5. `Alt+↑ / Alt+↓` 上一问 / 下一问
  6. 「回到本问」跳到当前视口所在的那一轮
  7. Esc / 点外面收起弹层；全程零 JS 报错

跑法（需要 8787 在跑，只用它发静态文件）：
    python tools\msg_nav_test.py
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
PROBE = os.path.join(WEB, "_probe_msgnav.html")
URL = "http://127.0.0.1:8787/_probe_msgnav.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event",
              "/engine/", "/update/", "/appearance", "/live/", "/themes", "/music/",
              "/fs/", "/pty", "/orchestrate/"];
  var raw = window.fetch;

  var ASKS = [
    "问题一：登录怎么拆成独立模块？",
    "问题二：会话表要不要拆？",
    "问题三：旧接口怎么兼容？",
    "问题四：测试用例写哪些？",
    "问题五：回滚方案是什么？",
    "问题六：最后怎么验收？"
  ];
  var LONG = [];
  for (var k = 0; k < 10; k++) LONG.push("这是第 " + (k + 1) + " 段回答，用来看长回答里的滚动位置。");
  var ALL = [];
  var base = 1700000000000;
  for (var i = 0; i < ASKS.length; i++) {
    var t = base + i * 10000;
    ALL.push({ id: "u" + i, type: "user", text: ASKS[i], time: { created: t } });
    ALL.push({ id: "a" + i, type: "assistant", agent: "build",
               model: { id: "probe-1", providerID: "acp" },
               time: { created: t + 1000, completed: t + 3000 },
               content: [{ type: "text", text: "回答 " + (i + 1) + "：\n" + LONG.join("\n") }] });
  }
  var SESS = { id: "ses_probe", title: "消息导航探针",
               time: { created: base, updated: base + 60000 },
               location: { directory: "D:\\\\probe" },
               model: { id: "probe-1", providerID: "acp" } };

  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" },
    }));
  }

  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/session/ses_probe/message") {
      return J({ data: ALL.slice().reverse(), cursor: null });   // 忠实模仿：最新在前
    }
    if (s.indexOf("/api/session/ses_probe") === 0) {
      if (/\/(model|agent|skill|command|permission|form)$/.test(path)) {
        return Promise.resolve(new Response("", { status: 204 }));
      }
      return J({ data: SESS });
    }
    if (path === "/api/session") return J({ data: [SESS], cursor: null });
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
    if (path.indexOf("/cocraft/") === 0) return J({ ok: true, items: [], data: [], total: 0 });
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
function __scroll(v) {
  var b = document.getElementById("messages");
  b.scrollTop = v;
  b.dispatchEvent(new Event("scroll"));
  return b.scrollTop;
}
function __flashText() {
  var els = document.querySelectorAll("#messages .msg.flash");
  if (!els.length) return "";
  return String(els[els.length - 1].textContent || "").replace(/\s+/g, " ").trim();
}
function __vis(id) {
  var e = document.getElementById(id);
  return !!e && !e.hidden && getComputedStyle(e).display !== "none";
}
function __qRows() { return document.querySelectorAll("#msg-ask-list .msg-q"); }
function __onQ() {
  var e = document.querySelector("#msg-ask-list .msg-q.on");
  return e ? String(e.textContent || "").replace(/\s+/g, " ").trim() : "";
}
function __visibleQ() {
  var box = document.getElementById("messages");
  var br = box.getBoundingClientRect();
  var els = box.querySelectorAll(".msg.user");
  for (var i = 0; i < els.length; i++) {
    var r = els[i].getBoundingClientRect();
    if (r.bottom > br.top + 1) return String(els[i].textContent || "").replace(/\s+/g, " ").trim();
  }
  return "";
}

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    var i;
    for (i = 0; i < 300; i++) {
      try { if (state && state.current && state.messages.length >= 12) break; } catch (e) {}
      await __s(25);
    }
    await __s(250);
    try { MSG_NAV.instant = true; } catch (e) { /* ignore */ }
    var box = document.getElementById("messages");
    out.info.boot = { n: (state.messages || []).length, asks: document.querySelectorAll("#messages .msg.user").length };
    step("首屏加载 6 轮（12 条消息）", state.messages.length === 12
         && document.querySelectorAll("#messages .msg.user").length === 6, JSON.stringify(out.info.boot));

    /* ---- 1. 底部：只显示「问题」，不显示「回到底部」 ---- */
    __scroll(box.scrollHeight);
    await __s(200);
    step("★在底部时显示「问题」、隐藏「回到底部」", __vis("msg-ask") && !__vis("msg-bottom"),
         "ask=" + __vis("msg-ask") + " bottom=" + __vis("msg-bottom"));

    /* ---- 2. 往上滚：出现「回到底部」 ---- */
    __scroll(0);
    await __s(400);
    var gapUp = box.scrollHeight - box.scrollTop - box.clientHeight;
    out.info.up = { top: Math.round(box.scrollTop), gap: Math.round(gapUp),
                    hidden: document.getElementById("msg-bottom").hidden,
                    display: getComputedStyle(document.getElementById("msg-bottom")).display,
                    timer: MSG_NAV.timer };
    step("★往上滚 → 出现「回到底部」", __vis("msg-bottom"), JSON.stringify(out.info.up));
    step("★按钮文案写清楚", /回到底部/.test(String(document.getElementById("msg-bottom").textContent || "")));
    window.__calls = [];
    document.getElementById("msg-bottom").click();
    await __s(900);
    var gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    step("★点「回到底部」→ 真回到最底并隐藏按钮", gap < 100 && !__vis("msg-bottom"),
         "gap=" + Math.round(gap));

    /* ---- 3. 问题弹层：6 行 + 当前高亮 ---- */
    __scroll(0);
    await __s(200);
    document.getElementById("msg-ask").click();
    await __s(250);
    step("★「问题」打开弹层", __vis("msg-ask-pop"));
    var rows = __qRows();
    out.info.rows = rows.length;
    step("★列出全部 6 次提问", rows.length === 6, "n=" + rows.length);
    step("★每行带序号 / 摘要 / 时间",
         /1/.test(rows[0].textContent) && /问题一/.test(rows[0].textContent) && /\d{1,2}:\d{2}/.test(rows[0].textContent),
         String(rows[0].textContent).replace(/\s+/g, " ").trim());
    step("★当前阅读的那一问高亮（在顶部 = 第 1 问）", /问题一/.test(__onQ()), __onQ());

    /* ---- 4. 点第 4 问 → 跳过去 ---- */
    rows[3].click();
    await __s(1000);
    step("★点问题行 → 弹层收起", !__vis("msg-ask-pop"));
    step("★跳到第 4 问（视口里能看到「问题四」）", /问题四/.test(__visibleQ()), __visibleQ());
    step("★跳过去会闪一下那一轮", /问题四/.test(__flashText()), __flashText());

    /* ---- 5. 回到本问 / Alt+↑ / Alt+↓ ---- */
    await __s(1600);                       // 等 flash 退场，避免误判
    __scroll(0);
    await __s(200);
    document.getElementById("msg-ask").click();
    await __s(200);
    document.getElementById("msg-q-cur").click();
    await __s(800);
    step("★「回到本问」→ 回到当前轮（顶部第 1 问）", /问题一/.test(__visibleQ()), __visibleQ());

    await __s(1600);
    __scroll(0);
    await __s(150);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowDown", altKey: true, bubbles: true }));
    await __s(900);
    step("★Alt+↓ → 下一问（问题二）", /问题二/.test(__visibleQ()), __visibleQ());
    await __s(1600);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "ArrowUp", altKey: true, bubbles: true }));
    await __s(900);
    step("★Alt+↑ → 上一问（问题一）", /问题一/.test(__visibleQ()), __visibleQ());

    /* ---- 6. Esc / 点外面收起 ---- */
    document.getElementById("msg-ask").click();
    await __s(150);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(150);
    step("★Esc 收起问题弹层", !__vis("msg-ask-pop"));
    document.getElementById("msg-ask").click();
    await __s(150);
    document.getElementById("session-title").click();
    await __s(150);
    step("★点弹层外面也收起", !__vis("msg-ask-pop"));
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

SHOT_JS = r"""<style>
  #msg-nav, #msg-nav * { transition: none !important; animation: none !important; }
</style>
<script>
function __w(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  var i;
  for (i = 0; i < 300; i++) {
    try { if (state && state.current && state.messages.length >= 12) break; } catch (e) {}
    await __w(25);
  }
  await __w(250);
  try { MSG_NAV.instant = true; } catch (e) {}
  var box = document.getElementById("messages");
  box.scrollTop = Math.round((box.scrollHeight - box.clientHeight) * 0.46);
  box.dispatchEvent(new Event("scroll"));
  await __w(350);
  document.getElementById("msg-ask").click();
  await __w(300);
})();
</script>
"""


def build_probe(shot: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + (SHOT_JS if shot else PROBE_JS) + '<script src="/app.js')
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
    dom = os.path.join(tmp, "probe_msgnav_dom.html")
    err = os.path.join(tmp, "probe_msgnav_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probemsgnav"),
             "--window-size=1280,860",
             "--virtual-time-budget=40000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_msgnav_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probemsgnavshot"),
             "--window-size=1280,900", "--virtual-time-budget=40000",
             "--screenshot=" + path, URL],
            stdout=subprocess.DEVNULL, stderr=fh_err)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  截图:", path, "存在=", os.path.isfile(path))


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", "msg_nav.png")
        shot = os.path.abspath(shot)
        build_probe(shot=True)
        try:
            take_shot(edge, shot)
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
        print("[BAD] 没拿到探针结果（渲染失败）。DOM 前 400 字符：")
        print(html[:400])
        return 1
    d = json.loads(m.group(1))

    print("\n=== 消息导航 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name + (("   [" + extra[:200] + "]") if extra else ""))
        if not okv:
            fails += 1
    if d.get("crash"):
        print("  脚本异常:", d["crash"])
        fails += 1
    print("\n  JS 报错:", d.get("jsErrors"))
    if d.get("jsErrors"):
        fails += 1
    total = len(d.get("steps", []))
    print("\n  total %d, failed %d" % (total, fails))
    print("  result:", "PASS" if fails == 0 else "FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
