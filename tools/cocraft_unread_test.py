# -*- coding: utf-8 -*-
r"""无头回归：群聊「存在感」A 组（未读 A1/A2 + @我/权限提醒 A3）+ B3（权限「强提醒」）。

做法与 history_page_test.py 同一套：由 frontend/index.html 生成探针页，
拦掉会惊动守护进程 / 卡死无头渲染的请求，把 `/cocraft/*` 换成假数据，
再用一个**假 WebSocket** 注入真实形状的推送帧，真跑一遍 app.js，断言：

  A1/A2「未读」
  1. 自己发的消息**不算**未读；
  2. 别人 / AI 成员发的消息**算**未读（当前会话、抽屉关着时也记）；
  3. 别的会话的消息记到**那个**会话；
  4. 顶栏 `#btn-hub` 出现未读徽章（总数），会话列表每条也有徽章；
  5. 打开会话 / 打开抽屉（在看它）→ **清零**；
  6. **正在看**的当前会话来新消息 → 不记未读；
  7. 未读落 `localStorage`；登出后清空；

  A3「@我 / 权限提醒」（走通知聚合器 NOTIFY）
  8. 被 @（`mentions` 命中我）→ 抽屉关着也出现**可点**横幅；
  9. 点横幅 → 开抽屉 + 打开那个会话 + 提醒撤下；
  10. 没 @我的普通消息 → 不弹；
  11. 有权限申请待批准 → 出现**可点**横幅；
  12. 权限 与 @我 同时存在 → 显示**权限**（优先级 100 > 80）；
  13. 权限结算 → 自动回到 @我 提醒；

  B3「强提醒」（标题栏计数 + 托盘气泡 + 闪任务栏）
  14. 未读 > 0 → `document.title` 带 `(N) ` 前缀；
  15. 待批准权限也算进 N（未读 2 + 权限 1 = `(3) `）；
  16. 面板**不在前台** → `POST /panel/notify`（托盘弹气泡 + 让窗口闪任务栏）；
  17. **同一条**申请反复重渲染 → 只提醒一次（去重落盘）；
  18. 面板**在前台** → 不发系统通知（页面里的横幅已够），且**不记**已提醒 →
      回到后台会补发；
  19. 权限结算 → 标题栏计数回落；登出 → 不留计数前缀；

  20. 全程零 JS 报错。

用法（需要面板服务已在 8787 运行 —— 只提供静态文件）：
    python tools\cocraft_unread_test.py
    python tools\cocraft_unread_test.py --shot resources\screenshots\cocraft_unread.png
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
PROBE = os.path.join(WEB, "_probe_cc.html")
URL = "http://127.0.0.1:8787/_probe_cc.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  // ★ 让 app.js 一启动就"已登录"（token 从 localStorage 读）+ 跳过首次设置向导
  try {
    localStorage.setItem("cocraft.token", "PROBE-TKN");
    localStorage.setItem("cocraft.setup.v1", "1");
    // ★ 未读是落盘的：探针必须从 0 起算（截图用的 user-data-dir 会跨次复用 → 否则数字会叠加上去）
    localStorage.removeItem("cocraft.cc.unread");
    localStorage.removeItem("cocraft.users");   // 同理：用户缓存也别跨次带进来
    localStorage.removeItem("cocraft.cc.permAlert");  // ★B3：强提醒去重键
  } catch (e) {}

  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event"];
  var raw = window.fetch;
  window.__notify = [];                       // ★B3：拦下 /panel/notify（否则真写 runtime 状态、真弹气泡）

  function jsonResp(obj, status) {
    return Promise.resolve(new Response(JSON.stringify(obj), {
      status: status || 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var CHATS = [
    { id: 1, name: "会话一", members: [{ id: 7 }, { id: 8 }] },
    { id: 2, name: "会话二", members: [{ id: 11 }] },
  ];

  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (s.indexOf("/panel/notify") >= 0) {    // ★B3
      var nb = {};
      try { nb = JSON.parse((o && o.body) || "{}"); } catch (e) {}
      window.__notify.push(nb);
      return jsonResp({ ok: true });
    }
    if (s.indexOf("/cocraft/") === 0) {
      var qi = s.indexOf("?");
      var path = qi >= 0 ? s.slice(0, qi) : s;
      if (path === "/cocraft/_url") return jsonResp({ base: "https://x", ws: "wss://x/ws" });
      if (path === "/cocraft/auth/me") return jsonResp({ user: { id: 7, username: "me" } });
      if (path === "/cocraft/chats") return jsonResp({ items: CHATS });
      if (/^\/cocraft\/chats\/\d+\/messages$/.test(path)) return jsonResp({ items: [], total: 0, limit: 100, offset: 0 });
      if (/^\/cocraft\/chats\/\d+\/(members|floor)$/.test(path)) return jsonResp({ items: [] });
      if (/^\/cocraft\/users\//.test(path)) return jsonResp({ id: 8, username: "bob" });
      return jsonResp({});
    }
    return raw.apply(this, arguments);
  };

  // ---- 假 WebSocket：记录实例，供 __wsDeliver 注入帧 ----
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
function __hub() { return document.getElementById("btn-hub"); }
function __frame(id, chat, sid, kind) {
  return { type: "message", id: id, chat_id: chat, sender_kind: kind || "user",
           sender_id: sid, content: "msg " + id, created_at: "2026-10-15T00:00:00Z" };
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
    out.info.boot = { me: CC.me && CC.me.id, user: CC.me && CC.me.username,
                      chats: (CC.chats || []).map(function (c) { return c.id; }) };
    step("登录后带出会话列表（2 条）", (CC.chats || []).length === 2, JSON.stringify(out.info.boot));

    setCocraft(false);                 // 抽屉关着
    CC.chatID = "1"; CC.msgs = [];     // 当前会话 = 1

    // 1. 自己发的不算
    __wsDeliver(__frame("m1", 1, 7));
    await __s(40);
    step("★自己发的消息不算未读", (CC.unread["1"] || 0) === 0, "unread=" + JSON.stringify(CC.unread));

    // 2. 别人发的（当前会话、抽屉关着）→ 算
    __wsDeliver(__frame("m2", 1, 8));
    await __s(40);
    step("★别人发的（抽屉关着）算未读", (CC.unread["1"] || 0) === 1, "unread=" + JSON.stringify(CC.unread));

    // 3. 别的会话（AI 成员）的消息
    __wsDeliver(__frame("m3", 2, 11, "agent"));
    await __s(40);
    step("★别的会话的消息记到那个会话", (CC.unread["2"] || 0) === 1, "unread=" + JSON.stringify(CC.unread));

    // 4. 顶栏徽章 = 总数 2；会话列表两个徽章
    var hub = __hub();
    out.info.hub = { cls: hub.className, data: hub.getAttribute("data-unread") };
    step("★顶栏按钮出现未读徽章（总数=2）",
         hub.classList.contains("has-unread") && hub.getAttribute("data-unread") === "2",
         JSON.stringify(out.info.hub));
    var badges = document.querySelectorAll("#cc-chats .cc-badge");
    step("★会话列表出现 2 个未读徽章", badges.length === 2, "badges=" + badges.length);

    // 5. 未读落 localStorage
    var ls = localStorage.getItem("cocraft.cc.unread") || "";
    step("★未读落 localStorage", /"1":1/.test(ls) && /"2":1/.test(ls), ls);

    // 6. 打开会话 1 → 清零（会话 2 不动）
    await ccOpenChat("1");
    await __s(40);
    step("★打开会话 → 该会话未读清零", (CC.unread["1"] || 0) === 0 && (CC.unread["2"] || 0) === 1,
         "unread=" + JSON.stringify(CC.unread));

    // 7. 切到会话 2 并打开抽屉 → 清零
    CC.chatID = "2"; setCocraft(true);
    await __s(40);
    step("★打开抽屉（在看会话2）→ 会话2 未读清零", (CC.unread["2"] || 0) === 0,
         "unread=" + JSON.stringify(CC.unread));

    // 8. 正在看当前会话 → 新消息不记
    __wsDeliver(__frame("m4", 2, 8));
    await __s(40);
    step("★正在看当前会话 → 新消息不记未读", (CC.unread["2"] || 0) === 0,
         "unread=" + JSON.stringify(CC.unread));

    // 9. 清零后徽章消失
    step("★清零后顶栏徽章消失",
         !hub.classList.contains("has-unread") && !hub.getAttribute("data-unread"),
         "data=" + hub.getAttribute("data-unread"));

    /* ---- ★A3：@我 与 权限申请的**可点横幅**（走通知聚合器 NOTIFY）---- */
    var bn = function () {
      var b = document.getElementById("banner");
      return { hidden: b.hidden, text: b.textContent, clickable: b.classList.contains("clickable") };
    };

    // 11. 被 @ → 抽屉关着也看得见横幅（可点）
    setCocraft(false); CC.chatID = "1";
    __wsDeliver({ type: "message", id: "m6", chat_id: 1, sender_kind: "user", sender_id: 8,
                  content: "喂 @me 看一下", mentions: ["me"], created_at: "2026-10-15T00:00:00Z" });
    await __s(60);
    var b1 = bn();
    step("★@我 → 顶栏横幅出现（可点）",
         !b1.hidden && b1.text.indexOf("@了你") >= 0 && b1.clickable, JSON.stringify(b1));

    // 12. 点横幅 → 开抽屉 + 打开那个会话 + 提醒撤下
    document.getElementById("banner").click();
    await __s(90);
    step("★点横幅 → 开抽屉并打开那个会话",
         cocraftIsOpen() && String(CC.chatID) === "1" && bn().text.indexOf("@了你") < 0,
         JSON.stringify({ open: cocraftIsOpen(), chat: CC.chatID, bn: bn() }));

    // 13. 没 @我 的普通消息 → 不弹
    setCocraft(false);
    __wsDeliver({ type: "message", id: "m7", chat_id: 2, sender_kind: "user", sender_id: 8,
                  content: "自己人聊两句", mentions: [], created_at: "2026-10-15T00:00:00Z" });
    await __s(60);
    step("★没 @我 的普通消息 → 不弹横幅",
         bn().hidden || bn().text.indexOf("@了你") < 0, JSON.stringify(bn()));

    // 14. 权限申请 → 横幅提醒（可点）
    setCocraft(false); CC.chatID = "1";
    CC.msgs = [{ id: 999, chat_id: 1, sender_kind: "agent", sender_id: 11,
                 content: "🔐 **权限申请**：Run command\n类型：execute\n涉及：D:\\x\n（90 秒内有效）",
                 created_at: new Date().toISOString() }];
    ccPermRender();
    await __s(40);
    var b2 = bn();
    step("★权限申请 → 横幅提醒（可点）",
         !b2.hidden && b2.text.indexOf("权限申请待批准") >= 0 && b2.clickable, JSON.stringify(b2));

    // 15. 权限 与 @我 同时存在 → 显示权限（优先级 100 > 80）
    __wsDeliver({ type: "message", id: "m8", chat_id: 2, sender_kind: "user", sender_id: 8,
                  content: "@me 在吗", mentions: ["me"], created_at: "2026-10-15T00:00:00Z" });
    await __s(60);
    step("★权限 与 @我 同时存在 → 显示权限（优先级高）",
         bn().text.indexOf("权限申请待批准") >= 0 && NOTIFY.top().key === "cc-perm",
         JSON.stringify(NOTIFY.top()));

    // 16. 权限结算 → 自动回到 @我 提醒
    CC.msgs = [];
    ccPermRender();
    await __s(40);
    step("★权限结算后 → 回到 @我 提醒", bn().text.indexOf("@了你") >= 0, JSON.stringify(bn()));

    /* ---- ★B3：权限申请「强提醒」（标题栏计数 + 系统通知 /panel/notify）---- */
    window.__notify = [];
    try { localStorage.removeItem("cocraft.cc.permAlert"); } catch (e) {}
    try { Object.defineProperty(document, "hidden", { get: function () { return false; }, configurable: true }); } catch (e) {}
    document.hasFocus = function () { return false; };     // 面板"不在前台"
    var __permMsg = function (id) {
      return { id: id, chat_id: 1, sender_kind: "agent", sender_id: 11,
               content: "🔐 **权限申请**：Run command\n类型：execute\n涉及：D:\\x\n（90 秒内有效）",
               created_at: new Date().toISOString() };
    };

    // 17. 未读 → 标题栏计数
    setCocraft(false);
    CC.unread = { "1": 2 }; CC.msgs = [];
    ccSyncUnreadUI();
    await __s(40);
    step("★B3 标题栏显示未读计数 (2)", /^\(2\) /.test(document.title), document.title);

    // 18. 待批准权限 → 计数再 +1
    CC.msgs = [__permMsg(8888)];
    ccPermRender();
    await __s(60);
    step("★B3 待批准权限 → 标题栏计数含它 (3)", /^\(3\) /.test(document.title), document.title);

    // 19. 面板不在前台 → 交给托盘发系统强提醒（气泡 + 闪任务栏）
    step("★B3 面板不在前台 → 发系统强提醒（POST /panel/notify）",
         window.__notify.length === 1
           && String(window.__notify[0].body || "").indexOf("权限") >= 0
           && window.__notify[0].flash === true,
         JSON.stringify(window.__notify));

    // 20. 同一条申请反复重渲染 → 只提醒一次
    ccPermRender(); ccPermRender();
    await __s(40);
    step("★B3 同一条申请只强提醒一次（去重落盘）",
         window.__notify.length === 1 && (localStorage.getItem("cocraft.cc.permAlert") === "8888"),
         "n=" + window.__notify.length + " key=" + localStorage.getItem("cocraft.cc.permAlert"));

    // 21. 面板在前台 → 不弹系统通知（新的一条申请也不弹）
    document.hasFocus = function () { return true; };
    CC.msgs = [__permMsg(9999)];
    ccPermRender();
    await __s(40);
    step("★B3 面板在前台 → 不发系统通知", window.__notify.length === 1, "n=" + window.__notify.length);

    // 22. 回到后台 → 这一条补发（说明前台那次没被记成"已提醒"）
    document.hasFocus = function () { return false; };
    ccPermRender();
    await __s(40);
    step("★B3 回到后台 → 补发（前台那次不记已提醒）",
         window.__notify.length === 2 && String(window.__notify[1].body || "").indexOf("权限") >= 0,
         "n=" + window.__notify.length);

    // 23. 权限结算 → 标题栏计数回落
    CC.msgs = [];
    ccPermRender();
    await __s(40);
    step("★B3 权限结算 → 标题栏只剩未读计数 (2)", /^\(2\) /.test(document.title), document.title);

    // 10. 登出 → 清空
    __wsDeliver(__frame("m5", 1, 8));
    await __s(30);
    ccShowAuth("已退出");
    await __s(30);
    step("★登出后未读清空", ccUnreadTotal() === 0, "unread=" + JSON.stringify(CC.unread));
    step("★B3 登出后标题栏不留计数前缀", !/^\(\d+\) /.test(document.title), document.title);
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
  /* 无头 + 虚拟时间不推进 CSS 过渡 → 抽屉会「卡在起始帧」；截图前把这些过渡关掉 */
  #cocraft, #cocraft *, #app, #main, body { transition: none !important; animation: none !important; }
</style>
<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) {
    try { if (CC && CC.me && CC.chats && CC.chats.length) break; } catch (e) {}
    await __sh(25);
  }
  CC.chatID = "3";   // 不在未读集合里 → 打开抽屉不会清掉它们
  try { ccCacheUser({ id: 8, username: "bob" }); } catch (e) {}   // 名字缓存（截图里 @我 那行显示名字而不是「用户 8」）
  window.__wsDeliver({ type: "message", id: "s1", chat_id: 2, sender_kind: "agent", sender_id: 11, content: "x", created_at: "2026-10-15T00:00:00Z" });
  await __sh(200);
  setCocraft(true);
  await __sh(300);
  // ★@我（会话一，不是当前看的会话三）→ 顶部可点横幅 + 会话一未读 1（截图里都该看得到）
  window.__wsDeliver({ type: "message", id: "s3", chat_id: 1, sender_kind: "user", sender_id: 8, content: "@me 看下这个", mentions: ["me"], created_at: "2026-10-15T00:00:00Z" });
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


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_cc_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-ccshot"),
             "--window-size=1400,920", "--virtual-time-budget=30000",
             "--screenshot=" + path, URL],
            stdout=subprocess.DEVNULL, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  截图:", path, "存在=", os.path.isfile(path))


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str) -> str:
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_cc_dom.html")
    err = os.path.join(tmp, "probe_cc_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-ccprobe"),
             "--window-size=1400,920",
             "--virtual-time-budget=40000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    shot = os.path.join(HERE, "resources", "screenshots", "cocraft_unread.png")
    edge = find_edge()
    if "--shot" in sys.argv:
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
    print("\n=== 群聊「存在感」无头回归（A 组 + B3）===")
    if d.get("crash"):
        print("  探针异常:", d["crash"])
    for name, ok, extra in d.get("steps", []):
        print("  %s %s%s" % ("OK  " if ok else "FAIL", name, ("  [" + str(extra) + "]") if extra else ""))
    print("\n  JS 运行错误:", d.get("jsErrors"))
    bad = [n for n, ok, _ in d.get("steps", []) if not ok]
    if d.get("crash") or d.get("jsErrors") or bad:
        print("\n  结论: [BAD] %s" % (bad or "有 JS 错误 / 探针异常"))
        return 2
    print("\n  结论: [OK] 群聊「存在感」A 组 + B3 全部通过"
          "（未读：自己不算 / 别人算 / 分会话 / 徽章 / 清零 / 登出清；"
          "@我 与 权限：可点横幅 + 优先级聚合；B3：标题栏计数 + 不在前台才发系统提醒 + 同一申请只提醒一次）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
