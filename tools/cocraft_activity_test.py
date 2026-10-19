# -*- coding: utf-8 -*-
r"""无头回归：群聊「存在感」C1 —— **进行中活动卡**（`docs/群聊存在感-方案.md` §12）。

和 `cocraft_unread_test.py` 同一套：用 `frontend/index.html` 生成探针页，真跑 `app.js`，
把 `/cocraft/*` 与 `/ccagent/*` 都挡成假数据 + 假 WebSocket，然后断言：

  C1 活动卡（后端内存缓冲 → `/ccagent/activity` → 前端卡片）
   1. 打开会话就按 `since=0` 拉一次（走**本地** `/ccagent/activity`，不是 /cocraft 代理）
   2. 拉回来的「运行中」活动 → 消息流**末尾**出现活动卡（`#cc-msgs` 的最后一个子节点）
   3. 卡片上是**中文**：`运行命令` + 命令首行 + 「进行中」+ 标题「执行中 · yiyi」
   4. `since` 按返回的 `max` 递增（第二次请求带上次的 max）
   5. ★收尾（running → completed）**原地改的那条也能同步**（按 id/rev 替换，不是新加一行）
   6. 三种状态：running / completed / error 各自的行 class 与中文
   7. 权限申请（asking）也进活动流，显示 🔐 + 「等待批准」
   8. 忙碌小圆片：该 agent 的成员 chip 带 `busy` + 「工作中」（**前端推断，不动服务端 presence**）
   9. 顶栏群聊入口出现「工作中」角标（`cc-busy`）；全部干完 → 角标消失
  10. 换到没有活动的会话 → 卡片消失
  11. 抽屉关着时**不再按 2s 轮询**（落到慢节奏）
  12. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\cocraft_activity_test.py
    python tools\cocraft_activity_test.py --shot resources\screenshots\cocraft_activity.png
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
PROBE = os.path.join(WEB, "_probe_ccact.html")
URL = "http://127.0.0.1:8787/_probe_ccact.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  // 与 app.js 一致：装作"已登录"（token 放 localStorage）+ 跳过首次向导
  try {
    localStorage.setItem("cocraft.token", "PROBE-TKN");
    localStorage.setItem("cocraft.setup.v1", "1");
    localStorage.removeItem("cocraft.cc.unread");
    localStorage.removeItem("cocraft.users");
    localStorage.removeItem("cocraft.cc.permAlert");
  } catch (e) {}

  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event"];
  var raw = window.fetch;

  function jsonResp(obj, status) {
    return Promise.resolve(new Response(JSON.stringify(obj), {
      status: status || 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var CHATS = [{ id: 3, name: "干活群", members: [{ id: 11 }] },
               { id: 4, name: "闲聊群", members: [{ id: 8 }] }];

  // ★C1：活动按脚本给（每次请求 shift 一条；用完就回"无新增"）
  window.__actReq = [];
  window.__actQ = [];
  window.__actLog = [];
  window.__actPush = function (items, max) { window.__actQ.push({ items: items, max: max }); };
  window.__actPoll = function () { return window.__actQ.length; };

  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (s.indexOf("/ccagent/activity") >= 0) {           // ★C1
      var q = {};
      var parts = (s.split("?")[1] || "").split("&");
      parts.forEach(function (kv) {
        var a = kv.split("=");
        if (a[0]) q[decodeURIComponent(a[0])] = decodeURIComponent(a[1] || "");
      });
      window.__actReq.push({ chat: q.chat || "", since: Number(q.since || 0) });
      var nx = window.__actQ.length ? window.__actQ.shift() : null;
      window.__actLog.push({ chat: q.chat, since: Number(q.since || 0),
                             had: nx ? (nx.items || []).length : -1 });
      if (!nx) return jsonResp({ ok: true, items: [], max: Number(q.since || 0) });
      return jsonResp({ ok: true, items: nx.items || [], max: nx.max });
    }
    if (s.indexOf("/ccagent") >= 0) {                    // 其它 /ccagent/*（AI 成员管理）
      if (s.indexOf("/ccagent/list") >= 0 || s.indexOf("/ccagent?") >= 0 || s.slice(-9) === "/ccagent") {
        return jsonResp({ ok: true, agents: [{ id: 11, name: "yiyi", display: "yiyi", engine: "codex-node" }] });
      }
      return jsonResp({ ok: true });
    }
    if (s.indexOf("/cocraft/") === 0) {
      var qi = s.indexOf("?");
      var path = qi >= 0 ? s.slice(0, qi) : s;
      if (path === "/cocraft/_url") return jsonResp({ base: "https://x", ws: "wss://x/ws" });
      if (path === "/cocraft/auth/me") return jsonResp({ user: { id: 7, username: "me" } });
      if (path === "/cocraft/chats") return jsonResp({ items: CHATS });
      if (/^\/cocraft\/chats\/\d+\/messages$/.test(path)) return jsonResp({ items: [], total: 0, limit: 100, offset: 0 });
      if (/^\/cocraft\/chats\/\d+\/members$/.test(path)) {
        return jsonResp({ items: [{ kind: "agent", id: 11, name: "yiyi", display: "yiyi", presence: "online" }] });
      }
      if (/^\/cocraft\/chats\/\d+\/floor$/.test(path)) return jsonResp({ items: [] });
      if (/^\/cocraft\/users\//.test(path)) return jsonResp({ id: 8, username: "bob" });
      return jsonResp({});
    }
    return raw.apply(this, arguments);
  };

  // ---- 假 WebSocket ----
  window.__wsInsts = [];
  window.WebSocket = function (url) {
    var self = this;
    this.url = url; this.readyState = 1;
    this.send = function () {};
    this.close = function () { self.readyState = 3; };
    window.__wsInsts.push(this);
    setTimeout(function () { if (self.onopen) self.onopen({}); }, 0);
  };
  window.__wsDeliver = function (obj) {
    var s = window.__wsInsts[window.__wsInsts.length - 1];
    if (s && s.onmessage) s.onmessage({ data: JSON.stringify(obj) });
  };

  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () {
    this.close = function () {}; this.addEventListener = function () {};
    this.onerror = null; this.onmessage = null;
  };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __act() { return document.getElementById("cc-act-card"); }
function __rows() { var c = __act(); return c ? c.querySelectorAll(".cc-act-row") : []; }
function __rowText(i) { var r = __rows()[i]; return r ? r.textContent : ""; }
function __hub() { return document.getElementById("btn-hub"); }
function __busyChip() {
  var box = document.getElementById("cc-room-meta");
  return box ? box.querySelector(".cc-chip.busy") : null;
}

/* 一条活动记录（服务端形状：id/seq/rev + 展示字段） */
function A(id, seq, rev, tool, sum, st, agent) {
  return { id: id, seq: seq, rev: rev, chat: "3", agent: agent || "yiyi",
           tool: tool, summary: sum, status: st, detail: "", ts: 0 };
}

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    var i;
    for (i = 0; i < 300; i++) {
      try { if (CC && CC.me && CC.me.id != null && CC.chats && CC.chats.length) break; } catch (e) {}
      await __s(25);
    }
    out.info.boot = { me: CC.me && CC.me.id, chats: (CC.chats || []).map(function (c) { return c.id; }) };

    /* ---- 1/2/3：开一个会话，拉回一条「运行中」活动 ---- */
    // 先把轮询停掉（app 启动时会自动恢复上次打开的会话 → 那一下会把脚本队列消费掉）
    ccActStop();
    CC.chatID = "";
    CC.actAt = 0;
    window.__actPush([A("t1", 1, 1, "运行命令", "python a.py", "running")], 1);
    setCocraft(false);
    await ccOpenChat("3");
    setCocraft(true);
    await __s(120);

    out.info.req0 = window.__actReq[0];
    step("★打开会话就按 since=0 拉一次活动",
         !!window.__actReq.length && window.__actReq[0].chat === "3" && window.__actReq[0].since === 0,
         JSON.stringify(window.__actReq.slice(0, 2)));

    var box = document.getElementById("cc-msgs");
    out.info.dbg = {
      hasAct: !!__act(), last: box.lastElementChild && (box.lastElementChild.id || box.lastElementChild.className),
      activity: CC.activity, rev: CC.actRev, reqs: window.__actReq.length,
      fn: typeof ccActRender, items: (typeof ccActItems === "function") ? ccActItems(CC.chatID) : "no-fn",
      chatID: CC.chatID, msgs: (CC.msgs || []).length,
      log: window.__actLog,
    };
    step("★活动卡贴在消息流**末尾**（#cc-msgs 最后一个子节点）",
         !!__act() && box.lastElementChild === __act(),
         "act=" + !!__act() + " last=" + (box.lastElementChild && box.lastElementChild.id));

    step("★卡片三要素：中文工具名 + 命令首行 + 「进行中」",
         /运行命令/.test(__rowText(0)) && /python a\.py/.test(__rowText(0)) && /进行中/.test(__rowText(0)),
         __rowText(0));
    step("★运行中的行是 running + 有转圈",
         __rows().length === 1 && __rows()[0].classList.contains("running")
         && !!__rows()[0].querySelector(".cc-act-spin"),
         "rows=" + __rows().length + " cls=" + (__rows()[0] && __rows()[0].className));
    step("★标题行写清「执行中 · 谁」",
         /执行中/.test(__act().textContent) && /yiyi/.test(__act().textContent),
         __act().querySelector(".cc-act-cap").textContent);

    /* ---- 8/9：忙碌小圆片 + 顶栏角标 ---- */
    step("★成员小圆片标「工作中」（前端推断，不动 presence）",
         !!__busyChip() && /工作中/.test(__busyChip().textContent),
         __busyChip() ? __busyChip().textContent : "(没有 busy chip)");
    step("★顶栏出现「工作中」角标", __hub().classList.contains("cc-busy"), __hub().className);

    /* ---- 5：收尾（原地改那条）也能同步 ---- */
    window.__actPush([A("t1", 1, 2, "运行命令", "python a.py", "completed")], 2);
    await __s(2600);                       // 干活时的节奏是 2s

    out.info.reqN = window.__actReq.slice(-2);
    step("★since 按返回的 max 递增（第二次带 since=1）",
         window.__actReq.some(function (r) { return r.since === 1; }),
         JSON.stringify(window.__actReq.slice(0, 4)));
    step("★收尾是**替换同一行**（不是新增一行）", __rows().length === 1, "rows=" + __rows().length);
    step("★「运行中 → 完成」同步过来了（中文「完成」+ done）",
         __rows()[0].classList.contains("done") && /完成/.test(__rowText(0)),
         __rows()[0].className + " | " + __rowText(0));
    step("★全干完 → 标题变「刚刚做过」、角标消失",
         /刚刚做过/.test(__act().textContent) && !__hub().classList.contains("cc-busy"),
         __act().querySelector(".cc-act-cap").textContent + " | " + __hub().className);

    /* ---- 6/7：三种状态 + 权限申请 ---- */
    // 群里来了新消息（= 有人 @ 了成员）→ 面板会**立刻**拉一次活动（kick），不用等 6s 空闲节奏
    __wsDeliver({ type: "message", id: "w1", chat_id: 3, sender_kind: "agent", sender_id: 11,
                  content: "开始干活", created_at: "2026-10-16T00:00:00Z" });
    window.__actPush([A("t2", 2, 3, "修改文件", "app.js", "running"),
                      A("t3", 3, 4, "运行命令", "git status", "error"),
                      A("p1", 4, 5, "破坏性操作", "rm -rf tmp", "asking")], 5);
    await __s(1200);

    var cls = [];
    __rows().forEach(function (r) { cls.push(r.querySelector(".cc-act-tool").textContent + ":" + r.className.split(" ").pop()); });
    out.info.rows = cls;
    step("★三种状态各画各的（running / bad / running+🔐）",
         cls.join(",") === "运行命令:done,修改文件:running,运行命令:bad,破坏性操作:running",
         cls.join(","));
    step("★失败行是红字（bad）", __rows()[2].classList.contains("bad") && /失败/.test(__rowText(2)), __rowText(2));
    step("★权限申请显示「等待批准」+ 🔐",
         /等待批准/.test(__rowText(3)) && /🔐/.test(__rowText(3)), __rowText(3));

    /* ---- 10：换到没有活动的会话 → 卡片消失 ---- */
    await ccOpenChat("4");
    setCocraft(true);
    await __s(200);
    step("★换到没活动的会话 → 卡片消失", !__act() || __act().hidden, "act=" + !!__act());

    /* ---- 11：抽屉关着 → 不再 2s 轮询 ---- */
    var before = window.__actReq.length;
    setCocraft(false);
    await __s(3000);
    var got = window.__actReq.length - before;
    out.info.idle = { before: before, after: window.__actReq.length };
    step("★抽屉关着时不按 2s 轮询（3s 内最多 1 次）", got <= 1, "新增请求=" + got);
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
  /* 无头 + 虚拟时间不推进 CSS 过渡 → 截图前关掉 */
  #cocraft, #cocraft *, #app, #main, body { transition: none !important; animation: none !important; }
</style>
<script>
window.__probeErr = [];
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) {
    try { if (CC && CC.me && CC.chats && CC.chats.length) break; } catch (e) {}
    await __sh(25);
  }
  try { ccCacheUser({ id: 11, username: "yiyi" }); } catch (e) {}
  window.__wsDeliver({ type: "message", id: "m1", chat_id: 3, sender_kind: "agent",
                       sender_id: 11, content: "我来帮你改一下工作区里的文件。",
                       created_at: "2026-10-16T00:00:00Z" });
  window.__actPush([
    { id: "t1", seq: 1, rev: 2, chat: "3", agent: "yiyi", tool: "修改文件",
      summary: "app.js", status: "completed", detail: "", ts: 0 },
    { id: "t2", seq: 2, rev: 3, chat: "3", agent: "yiyi", tool: "运行命令",
      summary: "python tools/jsbalance.py frontend/app.js", status: "running", detail: "", ts: 0 },
    { id: "p1", seq: 3, rev: 4, chat: "3", agent: "yiyi", tool: "破坏性操作",
      summary: "Remove-Item -Recurse tmp", status: "asking", detail: "", ts: 0 }
  ], 4);
  await ccOpenChat("3");
  setCocraft(true);
  await __sh(400);
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
    dom = os.path.join(tmp, "probe_ccact_dom.html")
    err = os.path.join(tmp, "probe_ccact_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-ccact"),
             "--window-size=1400,920",
             "--virtual-time-budget=60000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_ccact_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-ccactshot"),
             "--window-size=1400,920", "--virtual-time-budget=30000",
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
        shot = os.path.join(HERE, "resources", "screenshots", "cocraft_activity.png")
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
    print("\n=== 群聊「存在感」C1 无头回归（进行中活动卡）===")
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
