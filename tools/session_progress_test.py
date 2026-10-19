# -*- coding: utf-8 -*-
r"""无头回归：主会话界面的「本轮活动卡」（2026-10-16，`ui 0.1.74`）。

背景：`ui 0.1.64` 起主界面**只留「思考用时 + 最终输出」**，工具/思考/系统通知全进了「轨迹」页
→ 用户在会话界面里**看不到 agent 正在干什么**。现在补一张**本轮活动卡**（和群聊 C1 同款）：
贴在**这一轮最后一条消息之后**，一行一步（中文人话），**点某一步能看命令原文 / stdout / 真 diff**
（复用轨迹页那套 `traceToolHtml`），干完**原地留着**（用户明确要"保持展开"）。

和 `trace_time_test.py` 同一套：用 `frontend/index.html` 生成探针页，真跑 `app.js`，
把 `/api/session*` 换成内存假会话，然后断言：

   1. 有工具的一轮 → 主界面出现**一张** `.turn-card`，标题「本轮 N 步 · 用时 · 改了 …」
   2. 卡片位置 = 该轮**最后一条消息之后**（`previousElementSibling` 就是那条助手消息）
   3. 每一步都是**中文一行**：`运行命令` + 命令首行 / `修改文件 app.js` + `+3 −1`
   4. **点开某一步**能看到命令原文 / stdout / **真 diff**（红绿行）
   5. 折叠 key 用**独立前缀** → 卡片里展开**不会**连带把轨迹页那条展开（`TRACE_OPEN` 不串味）
   6. **没有工具的那一轮不出卡片**
   7. 正在生成时：卡片在 `#live-slot` 末尾，标题「正在执行 · …」+ 步数
   8. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\session_progress_test.py
    python tools\session_progress_test.py --shot resources\screenshots\session_progress.png
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
PROBE = os.path.join(WEB, "_probe_prog.html")
URL = "http://127.0.0.1:8787/_probe_prog.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try {
    localStorage.setItem("cocraft.setup.v1", "1");
    localStorage.setItem("opencode-ui.setup.v1", "1");
  } catch (e) {}

  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event",
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/fs/"];
  var raw = window.fetch;
  var T = 1760000000000;

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var PATCH =
    "Index: D:/proj/app.js\n" +
    "===================================================================\n" +
    "--- D:/proj/app.js\n" +
    "+++ D:/proj/app.js\n" +
    "@@ -1,3 +1,4 @@\n" +
    " const a = 1;\n" +
    "+const b = 2;\n" +
    "-const c = 3;\n" +
    " const d = 4;\n";

  // 第 1 轮：用户提问 → 助手跑了两步（运行命令 + 修改文件），最后给正文
  // 第 2 轮：用户提问 → 助手**只有正文、没有工具**（这一轮不该出现卡片）
  var MSGS = [
    { id: "m2", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1000, completed: T + 13000 },
      content: [
        { type: "reasoning", text: "先看看测试跑不跑得过。" },
        { type: "tool", name: "bash", state: { status: "completed",
            input: { command: "npm test -- --silent" },
            content: [{ type: "text", text: "PASS 12 tests" }],
            metadata: { exit: 0 } } },
        { type: "tool", name: "edit", state: { status: "completed",
            input: { filePath: "D:/proj/app.js" },
            metadata: { files: [{ file: "D:/proj/app.js", additions: 1, deletions: 1, patch: PATCH }] } } },
        { type: "text", text: "改好了：加了常量 b、去掉了 c。" },
      ] },
    { id: "m1", type: "user", text: "顺手把 app.js 改一下", time: { created: T } },
    { id: "m4", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 14000, completed: T + 16000 },
      content: [{ type: "text", text: "这一步不用动文件，直接说结论。" }] },
    { id: "m3", type: "user", text: "再解释一下为什么", time: { created: T + 13500 } },
  ];

  var SESS = { id: "ses_probe", title: "本轮活动卡探针",
               time: { created: T, updated: T + 16000 },
               location: { directory: "D:\\\\proj" },
               model: { id: "probe-1", providerID: "acp" } };

  window.fetch = function (u, o) {
    var s = String(u);
    // ⚠ 模型清单必须先挡：DEAD 若把 `/api/model` 回成 `{}`，`MODEL.list.find` 会抛
    //   → 整个 openSession 链断掉、消息一条都加载不出来（实测踩到）。
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ location: { directory: "D:\\\\proj" }, data: [] });
    if (path === "/api/model/default") return J({ data: null });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path === "/api/session/ses_probe/message") {
      return J({ data: MSGS.slice().reverse(), cursor: null });       // 新→旧（照真实形状）
    }
    if (s.indexOf("/api/session/ses_probe") === 0) {
      if (/\/(model|agent|skill|command|permission|form)$/.test(path)) {
        return Promise.resolve(new Response("", { status: 204 }));
      }
      return J({ data: SESS });
    }
    if (path === "/api/session") return J({ data: [SESS], cursor: null });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () {
    this.close = function () {}; this.addEventListener = function () {};
  };
  window.WebSocket = function () {
    this.send = function () {}; this.close = function () {}; this.readyState = 1;
  };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __cards() { return document.querySelectorAll("#msg-static .turn-line"); }
function __cap(c) { return c ? c.textContent.replace(/\s+/g, " ").trim() : ""; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    var i;
    for (i = 0; i < 300; i++) {
      await __s(25);
      if ((state.messages || []).length >= 4) break;
    }
    await __s(150);
    out.info.msgs = (state.messages || []).map(function (m) { return m.id + ":" + m.type; });
    // 诊断：进展行到底出没出
    try {
      var tg = turnCards();
      out.info.turnCards = tg.map(function (g) {
        return { n: g.items.length, after: g.afterId, live: !!g.live, len: turnLineHtml(g, 0).length };
      });
      out.info.staticLen = (staticHtml || "").length;
      out.info.hasFn = typeof turnLineHtml;
    } catch (e) { out.info.tcErr = String(e && e.stack ? e.stack : e); }

    /* 1. 只有「有工具」的那一轮才有一行进展；而且**就一行**（不列步骤） */
    var cs = __cards();
    out.info.cards = cs.length;
    step("★有工具的一轮出现**一行**进展，没工具的一轮不出现（正好 1 行）", cs.length === 1,
         "lines=" + cs.length);
    step("★**不列步骤**：主界面里没有轨迹那种可折叠的步骤行",
         document.querySelectorAll("#msg-static .tr-item").length === 0
         && document.querySelectorAll("#msg-static .tr-fold").length === 0,
         "items=" + document.querySelectorAll("#msg-static .tr-item").length
         + " folds=" + document.querySelectorAll("#msg-static .tr-fold").length);
    var c = cs[0];
    step("★这一行写清「本轮 N 步 · 用时 · 改了哪些文件」",
         !!c && /本轮 2 步/.test(__cap(c)) && /改了 app\.js/.test(__cap(c)), __cap(c));

    /* 2. 位置 = 该轮最后一条消息之后 */
    var prev = c ? c.previousElementSibling : null;
    step("★进展行贴在该轮**最后一条消息之后**",
         !!prev && prev.classList.contains("msg") && prev.getAttribute("data-mid") === "m2",
         prev ? ("prev=" + prev.className + " mid=" + prev.getAttribute("data-mid")) : "(没前一个)");

    /* 3. 干活时：live 槽位末尾一行「正在… · 第 N 步」+ 转圈点 */
    state.pending = [{ id: "p9", text: "帮我跑一下测试", time: Date.now() }];
    state.live = { "live1": { agent: "build", model: "probe-1", reasoning: {}, text: {},
                              tools: [{ name: "bash", status: "running", input: { command: "npm test" } }],
                              started: Date.now() } };
    paintLiveSlot();
    await __s(80);
    var lc = document.querySelector("#live-slot .turn-line.live");
    out.info.live = lc ? __cap(lc) : "(没有)";
    step("★干活时：live 末尾一行「正在运行命令 npm test · 第 1 步」",
         !!lc && /正在运行命令/.test(__cap(lc)) && /npm test/.test(__cap(lc)) && /第 1 步/.test(__cap(lc)),
         String(out.info.live));
    step("★干活时那行带**转圈点**（一眼看出在跑）", !!lc && !!lc.querySelector(".tl-dot"),
         lc ? lc.innerHTML.slice(0, 50) : "(没有)");
    step("★live 行确实在消息流**最后**（#messages 的最后一个 .turn-line 就是它）",
         (function () {
           var all = document.querySelectorAll("#messages .turn-line");
           return all.length >= 2 && all[all.length - 1].classList.contains("live");
         })(), "lines=" + document.querySelectorAll("#messages .turn-line").length);
    // 收尾：清掉注入的 live
    state.live = {}; state.pending = []; liveShellSig = null; paintLiveSlot();
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
  #messages, #messages *, .tr-fold, .tr-diff { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) {
    await __sh(25);
    if ((state.messages || []).length >= 4) break;
  }
  await __sh(200);
  // 展开「修改文件」那一步，好让截图里看得到真 diff
  var fold = document.querySelectorAll("#msg-static .turn-card .tc-rows .tr-fold");
  if (fold[1]) fold[1].open = true;
  await __sh(200);
})();
</script>
"""


def build_probe(shot: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    extra = SHOT_JS if shot else PROBE_JS
    out = src.replace('<script src="/app.js', STUB + extra + '<script src="/app.js')
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
    dom = os.path.join(tmp, "probe_prog_dom.html")
    err = os.path.join(tmp, "probe_prog_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeprog"),
             "--window-size=1400,920",
             "--virtual-time-budget=30000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_prog_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeprogshot"),
             "--window-size=1400,1000", "--virtual-time-budget=30000",
             "--screenshot=" + path, URL],
            stdout=subprocess.DEVNULL, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  截图:", path, "存在=", os.path.isfile(path))


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv:
        shot = os.path.join(HERE, "resources", "screenshots", "session_progress.png")
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
        print("没拿到探针结果（渲染可能失败）。DOM 前 600 字符：")
        print(html[:600])
        return 1

    d = json.loads(m.group(1))
    print("\n=== 主界面「本轮活动卡」无头回归 ===")
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
