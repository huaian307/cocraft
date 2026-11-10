# -*- coding: utf-8 -*-
r"""无头回归：多步一轮时「生成中」气泡**只该有一个**（`ui 0.2.83`）。

用户实测（2026-10-27，发图）：一轮 17 步跑在后台（窗口失焦、在看群聊），回到面板后
消息流里叠着 **4 个「正在思考…」** + 一个「正在 tool」。

真因不是"渲染坏了"，而是**两条设计咬在一起**：
  ① 多步一轮里**每一步都是一个新的 `assistantMessageID`** → `state.live` 会依次攒下很多条；
  ② 思考 / 工具在主界面**不渲染** → 每条 live 都只剩 `renderMessage` 的占位「正在思考…」。
本来的兜底是"`refreshMessages()` 把已落库的 id 从 `state.live` 删掉"，可
**失焦 / 窗口被遮挡时浏览器会节流定时器**（Chromium 后台可到 1 次/分钟）→ 那段时间里就攒出一串。

修法（`01-chat.js`）：
  * `liveMainIds()`：有正文的照画；**已收尾（`done`）又没正文的直接收起**；**只有正在跑的那一步**才给占位。
  * `liveToMessage()`：`done` 的带上 `time.completed`（不再当成"还在生成"）。
  * `handleEvent()` 的 `step.ended` 立刻记 `done + endedAt` → **不等刷新**就收。
  * `initFocusRefresh()`：回到前台补一次对账（失焦期间定时器被节流）。
  * 轨迹页 / 本轮进展行**仍用 `pendingLiveIds()`** —— 那边要看到每一步，不能被主界面的过滤连坐。

断言（探针真跑 `app.js`，直接给 `state.live` 塞"失焦攒下的 4 条"）：
    1. 全屏只有**一个**「正在思考…」
    2. 有正文的那条照样显示（不能连带把答案藏掉）
    3. 已收尾又没正文的步骤不占位
    4. 轨迹页仍看得到每一步（think / tool 都在）
    5. `step.ended` 一到就收（不等刷新）
    6. 最新一步**有正文**时不会再叠一个占位
    7. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\live_slot_test.py
    python tools\live_slot_test.py --shot resources\screenshots\live_slot.png
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

try:                                   # 控制台是 GBK 时别让 emoji / 特殊符号把脚本自己打挂
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                      # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_live.html")
URL = "http://127.0.0.1:8787/_probe_live.html"

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

  // 已落库：一次提问 + 两条"只有思考/工具"的已结束步骤（主界面本来就不显示它们）
  var MSGS = [
    { id: "m2", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1000, completed: T + 2000 },
      content: [{ type: "reasoning", text: "先读一下目录。" },
                { type: "tool", name: "bash", state: { status: "completed", input: { command: "ls" } } }] },
    { id: "m1", type: "user", text: "帮我把这个项目梳理一遍", time: { created: T } },
  ];

  var SESS = { id: "ses_probe", title: "生成中气泡探针",
               time: { created: T, updated: T + 2000 },
               location: { directory: "D:\\\\proj" },
               model: { id: "probe-1", providerID: "acp" } };

  // ★探针可以往"服务端"塞额外消息（模拟 ACP 在 user 消息前面注入工具卡）
  window.__extraMsgs = [];

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
      var all = (window.__extraMsgs || []).concat(MSGS);
      return J({ data: all.slice().reverse(), cursor: null });       // 新→旧（照真实形状）
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
function __live() { return document.querySelectorAll("#live-slot .msg"); }
function __thinkCount() {
  var n = 0;
  document.querySelectorAll("#messages .text.muted").forEach(function (el) {
    if (/正在思考/.test(el.textContent)) n++;
  });
  return n;
}
function __cap(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __liveCap() {
  var out = [];
  __live().forEach(function (m) { out.push(__cap(m)); });
  return out.join(" // ");
}

/** 造出"失焦期间攒下的 4 条"：1 条已收尾无正文、1 条漏了收尾事件、1 条有正文、1 条正在跑 */
function __seed() {
  var now = Date.now();
  state.reveal = {};
  liveShellSig = null;
  state.live = {
    L1: { text: {}, reasoning: { 0: "第一步：读目录。" }, tools: [], agent: "build",
          model: "probe-1", started: now - 6000, done: true, endedAt: now - 5000 },
    L2: { text: {}, reasoning: { 0: "第二步：找入口文件。" }, tools: [], agent: "build",
          model: "probe-1", started: now - 4000 },
    L3: { text: { 0: "已读到第 3 页，稍等。" }, reasoning: {}, tools: [], agent: "build",
          model: "probe-1", started: now - 3000, done: true, endedAt: now - 2500 },
    L4: { text: {}, reasoning: { 0: "正在跑测试。" }, agent: "build", model: "probe-1",
          tools: [{ name: "bash", status: "running", input: { command: "npm test" } }],
          started: now - 1000 },
  };
  paintLiveSlot();
}

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 300; i++) {
      await __s(25);
      if ((state.messages || []).length >= 2) break;
    }
    await __s(150);
    out.info.msgs = (state.messages || []).map(function (m) { return m.id + ":" + m.type; });

    /* 1 ~ 3：失焦攒下的 4 条 → 只留 1 个占位 + 有正文的那条 */
    __seed();
    await __s(120);
    out.info.liveParts = __liveCap();
    out.info.think = __thinkCount();
    step("★失焦攒下的 4 条缓冲 → 全屏只有**一个**「正在思考…」", __thinkCount() === 1,
         "正在思考 x" + __thinkCount());
    step("★有正文的那条**照样显示**（不能连带把答案藏掉）",
         /已读到第 3 页/.test(__liveCap()), __liveCap());
    step("★主界面只剩 2 个气泡（正文那条 + 正在跑那条），不是 4 个",
         __live().length === 2, "气泡 x" + __live().length);

    /* 4：轨迹页要看到每一步（不能被主界面的过滤连坐） */
    try {
      var items = traceItems();
      out.info.traceThink = items.filter(function (x) { return x.kind === "think"; }).length;
      out.info.traceTool = items.filter(function (x) { return x.kind === "tool"; }).length;
      step("★轨迹页仍看得到每一步（think x" + out.info.traceThink + " / tool x" + out.info.traceTool + "）",
           out.info.traceThink >= 3 && out.info.traceTool >= 1,
           "think=" + out.info.traceThink + " tool=" + out.info.traceTool);
    } catch (e) {
      step("★轨迹页仍看得到每一步（没抛异常）", false, String(e));
    }

    /* 5：step.ended 一到就收，不用等 300ms 刷新（拿"正在跑的那一步"验：它一收尾就不该再占位） */
    handleEvent({ type: "session.step.ended", data: { sessionID: "ses_probe", assistantMessageID: "L4" } });
    paintLiveSlot();
    await __s(60);
    out.info.afterEnded = __liveCap() + " || think=" + __thinkCount();
    step("★step.ended 一到就收（不等刷新）：L4 记 done + 立刻没有占位、正文那条还在",
         !!state.live.L4 && state.live.L4.done === true
         && __thinkCount() === 0 && /已读到第 3 页/.test(__liveCap()),
         out.info.afterEnded);

    /* 6：最新一步**有正文**时不该再叠一个占位 */
    state.live.L5 = { text: { 0: "结论：都跑通了。" }, reasoning: { 0: "收尾。" }, tools: [],
                      agent: "build", model: "probe-1", started: Date.now() };
    liveShellSig = null;
    paintLiveSlot();
    await __s(60);
    out.info.last = __liveCap() + " || think=" + __thinkCount();
    step("★最新一步有正文时不叠加占位（正文照常显示、占位为 0）",
         /结论：都跑通了/.test(__liveCap()) && __thinkCount() === 0, out.info.last);

    /* 7：全部收尾后 live 槽位不空壳 */
    ["L3", "L4", "L5"].forEach(function (id) {
      if (state.live[id]) { state.live[id].done = true; state.live[id].endedAt = Date.now(); }
    });
    liveShellSig = null;
    paintLiveSlot();
    await __s(60);
    step("★全部收尾后主界面不留空壳（0 个占位）", __thinkCount() === 0, "think=" + __thinkCount());

    /* 8：★服务端在 user 消息前面加了注入前缀（ACP 桌面工具卡 / 本地补记）时，
          本地回显也必须撤掉 —— 否则界面会在**回答下面**再挂一遍你的问题（看着像"回复错位"）。 */
    var utext = "现在来解决客户端的前端唤起问题，也就是说我们是不需要去唤起所使用的agent的前端的对吧";
    var NB = Date.now();
    window.__extraMsgs = [
      { id: "u2", type: "user", time: { created: NB + 5000 },
        text: "【桌面/网页】原生窗口用 `cu windows/tree/find/click/type/shot`（选择器，别截图猜坐标）；"
              + "先 `cu macro list` 看有没有现成宏；返回 JSON，先查再动。\n" + utext },
      { id: "m3", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
        time: { created: NB + 6000, completed: NB + 7000 },
        content: [{ type: "text", text: "回答：不需要。" }] },
    ];
    state.pending = [{ id: "local_probe", text: utext, time: Date.now() }];
    liveShellSig = null;
    await refreshMessages();
    var sameBubble = 0;
    document.querySelectorAll("#messages .msg").forEach(function (el) {
      if (__cap(el).indexOf("现在来解决客户端的前端唤起问题") >= 0) sameBubble++;
    });
    step("★服务端 user 消息带注入前缀时，本地回显也要撤掉（底部不重复一条）",
         state.pending.length === 0 && sameBubble === 1,
         "pending=" + state.pending.length + " 命中气泡=" + sameBubble);
    window.__extraMsgs = [];

    /* 9：★正在生成的那一步会先落一行"未完成"的助手消息（opencode-acp）→ 不能和 live 再画一遍 */
    var T9 = Date.now();
    state.messages = (state.messages || []).concat([{
      id: "msg_inflight", type: "assistant", inflight: true,
      time: { created: T9 },
      content: [
        { type: "reasoning", text: "先看看" },
        { type: "tool", name: "bash", state: { status: "running", input: { command: "ls" } } },
      ],
    }]);
    state.live = { L9: { text: {}, reasoning: { 0: "先看看" },
      tools: [{ name: "bash", status: "running", input: { command: "ls" } }],
      agent: "build", model: "probe-1", started: T9 } };
    state.reveal = {};
    liveShellSig = null;
    staticSig = null;
    renderMessages();
    await __s(80);
    step("★未完成的助手消息 + live 缓冲 → 只画一个「正在思考…」",
         __thinkCount() === 1, "think=" + __thinkCount() + " live=" + __live().length);

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

SHOT_JS = r"""<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) {
    await __sh(25);
    if ((state.messages || []).length >= 2) break;
  }
  await __sh(200);
  var now = Date.now();
  state.reveal = {};
  state.live = {
    L1: { text: {}, reasoning: { 0: "第一步：读目录。" }, tools: [], agent: "build",
          model: "probe-1", started: now - 6000, done: true, endedAt: now - 5000 },
    L2: { text: {}, reasoning: { 0: "第二步：找入口文件。" }, tools: [], agent: "build",
          model: "probe-1", started: now - 4000 },
    L3: { text: { 0: "已读到第 3 页，稍等。" }, reasoning: {}, tools: [], agent: "build",
          model: "probe-1", started: now - 3000, done: true, endedAt: now - 2500 },
    L4: { text: {}, reasoning: { 0: "正在跑测试。" }, agent: "build", model: "probe-1",
          tools: [{ name: "bash", status: "running", input: { command: "npm test" } }],
          started: now - 1000 },
  };
  liveShellSig = null;
  paintLiveSlot();
  await __sh(300);
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
    dom = os.path.join(tmp, "probe_live_dom.html")
    err = os.path.join(tmp, "probe_live_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probelive"),
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
    with open(os.path.join(tmp, "probe_live_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeliveshot"),
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
        shot = os.path.join(HERE, "resources", "screenshots", "live_slot.png")
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
    print("\n=== 「生成中」气泡只该有一个 · 无头回归 ===")
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
