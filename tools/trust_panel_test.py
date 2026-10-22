# -*- coding: utf-8 -*-
r"""无头回归：★信任面板（方向 3 第一块）—— 权限等级 / 待批准 / 最近「始终允许」。

真跑 `frontend/app.js`，把 `/api/permission/*` 换成内存假数据，断言：

  1. 顶栏「信任」按钮能打开面板；五档都列出来、当前档位高亮
  2. 每档有**人话说明**（联网 / 工作区 / 破坏性）
  3. 收紧（→ ask）直接切；放宽到 exec / all 要二次确认，取消就不发请求
  4. 「立即收紧」一键回到 ask
  5. 待批准：列出当前会话的权限请求 + 群聊待批（从已有消息推）
  6. 最近「始终允许」：逐条列出；**逐条撤销**（DELETE {id}）/ 清空全部
  7. 引擎不支持（404）时给出人话，不假装成功
  8. Esc 关闭；全程零 JS 报错

跑法（需要 8787 在跑，只用它发静态文件）：
    python tools\trust_panel_test.py
    python tools\trust_panel_test.py --shot resources\screenshots\trust.png
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
PROBE = os.path.join(WEB, "_probe_trust.html")
URL = "http://127.0.0.1:8787/_probe_trust.html"

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

  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/engine/", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/"];
  window.__calls = [];
  window.__confirms = [];
  window.__confirmOk = true;
  window.__always404 = false;
  window.__badShapes = false;      // ★把列表接口都换成"错形状"，验证前端不会被打崩
  window.LEVEL = "edit";
  window.ALWAYS = [
    { id: "ar_execute_d_ws", agent: "codex-node", session: "s1", kind: "execute",
      path: "D:/ws", hits: 3, ts: Date.now() - 60000, action: "Run command" }
  ];
  window.ASKS = [
    { id: "p1", action: "Run command", kind: "execute", resources: ["D:/ws/build.ps1"],
      message: "agent 请求权限：Run command" }
  ];
  window.confirm = function (msg) { window.__confirms.push(String(msg || "")); return window.__confirmOk; };

  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" },
    }));
  }

  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = (o && o.method) || "GET";
    var body = {};
    try { body = JSON.parse((o && o.body) || "{}"); } catch (e) {}

    if (path === "/api/permission/level") {
      if (method === "POST") {
        window.__calls.push(["level", body.level]);
        window.LEVEL = body.level;
        return J({ ok: true, level: window.LEVEL });
      }
      return J({ ok: true, level: window.LEVEL, levels: [
        { id: "ask", label: "全部询问（默认）" },
        { id: "read", label: "只读自动" },
        { id: "edit", label: "读 + 改（限工作区内）" },
        { id: "exec", label: "读 + 改 + 执行（限工作区内）" },
        { id: "all", label: "全部放行（含工作区外）" }
      ] });
    }
    if (path === "/api/permission/always") {
      if (window.__always404) return J({ error: "Not Found" }, 404);
      if (method === "DELETE") {
        window.__calls.push(["always/del", body.id || "*"]);
        if (body.id) window.ALWAYS = window.ALWAYS.filter(function (r) { return r.id !== body.id; });
        else window.ALWAYS = [];
        return J({ ok: true, removed: 1 });
      }
      return J({ ok: true, agent: "codex-node", data: window.ALWAYS });
    }
    if (/^\/api\/session\/[^/]+\/permission$/.test(path)) {
      return J({ data: window.ASKS });
    }
    if (/^\/api\/session\/[^/]+\/(form|message)/.test(path)) {
      return J({ data: [] });
    }
    // ★信任面板新增四块的数据源（可撤销 / 审批时间线 / agent 活动 / 本地协作）
    if (window.__badShapes) {
      if (path === "/quarantine/list" || path === "/snapshot/list" || path === "/ccagent/permissions"
          || path === "/ccagent/activity" || path === "/orchestrate/runs" || path === "/orchestrate/pending") {
        return J({ ok: true, items: "nope", data: "nope", runs: 5, pending: {} });
      }
    }
    if (path === "/quarantine/list") {
      return J({ ok: true, data: { items: [
        { id: "3/20261021-101010", ts: "20261021-101010", scope: "3",
          taken_at: Math.floor(Date.now() / 1000) - 120,
          note: "破坏性操作", cmd: "Remove-Item -Recurse -Force build",
          count: 12, bytes: 4096, missed: 1 },
      ], stats: { store_bytes: 1, quarantine: 1 } } });
    }
    if (path === "/snapshot/list") {
      return J({ ok: true, data: { items: [
        { ts: "20261021-101500", taken_at: Math.floor(Date.now() / 1000) - 60,
          label: "turn", count: 37, with_content: false },
      ] } });
    }
    if (path === "/ccagent/permissions") {
      return J({ ok: true, items: [
        { id: "pm-1", ts: Math.floor(Date.now() / 1000) - 30, agent: "yiyi", owner: "8",
          kind: "execute", title: "Run command", cmd: "npm test", verdict: "once",
          by: "owner", reply: "@yiyi 可以", destructive: false },
        { id: "pm-2", ts: Math.floor(Date.now() / 1000) - 300, agent: "yiyi", owner: "8",
          kind: "execute", title: "Run command", cmd: "Remove-Item -Force build",
          verdict: "reject", by: "timeout", destructive: true },
      ] });
    }
    if (path === "/ccagent/activity") {
      return J({ ok: true, max: 3, items: [
        { id: "t1", rev: 3, chat: "7", agent: "yiyi", tool: "运行命令", summary: "npm test",
          status: "completed", ts: Math.floor(Date.now() / 1000) - 20 },
        { id: "t2", rev: 2, chat: "7", agent: "yiyi", tool: "修改文件", summary: "app.js",
          status: "running", ts: Math.floor(Date.now() / 1000) - 10 },
        { id: "t3", rev: 1, chat: "7", agent: "yiyi", tool: "权限申请", summary: "删除 build",
          status: "denied", ts: Math.floor(Date.now() / 1000) - 5 },
      ] });
    }
    if (path === "/orchestrate/runs") return J({ ok: true, runs: [] });
    if (path === "/orchestrate/pending") return J({ ok: true, pending: [] });

    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/cocraft/") === 0) return J({ ok: true, items: [], data: [], total: 0 });
    if (path === "/engine/status") return J({ engines: [], active: "opencode" });
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
    if (path.indexOf("/themes") === 0) return J({ ok: true, themes: [], active: "hiru", builtin: [] });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () {
    var self = this;
    this.readyState = 1;
    this.send = function (raw) { try { window.__wsSent.push(JSON.parse(raw)); } catch (e) {} };
    this.close = function () {};
    setTimeout(function () { if (self.onopen) self.onopen({}); }, 0);
  };
  window.__wsSent = [];
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 200; i++) { await __s(25); if (window.CC && window.state) break; }
    await __s(300);

    state.current = { id: "s1", title: "登录重构" };
    state.sessions = [{ id: "s1", title: "登录重构" }];
    CC.chatID = "";

    /* ---------- 1. 打开 + 五档 + 当前档（入口：权限 ▾ → 信任面板…） ---------- */
    step("★顶栏盾牌已删除（入口收进权限菜单）", !document.getElementById("btn-trust"));
    document.getElementById("btn-perm").click();
    await __s(200);
    var trustEntry = document.querySelector("[data-trust-open]");
    step("★权限菜单底部有「信任面板…」", !!trustEntry);
    if (trustEntry) trustEntry.click();
    await __s(400);
    var box = document.getElementById("trust");
    step("★从权限菜单能打开信任面板", !!box && !box.hidden);
    var lvs = document.querySelectorAll("#trust-levels .trust-lv");
    out.info.lv = lvs.length;
    step("★五档都列出来", lvs.length === 5, "n=" + lvs.length);
    var curLv = document.querySelector("#trust-levels .trust-lv.on");
    out.info.cur = __txt(document.getElementById("trust-cur"));
    step("★当前档位高亮 = 读 + 改（限工作区内）",
         !!curLv && curLv.dataset.lv === "edit" && /读 \+ 改/.test(__txt(document.getElementById("trust-cur"))),
         out.info.cur);
    step("★每档都有**人话说明**（联网 / 工作区 / 破坏性）",
         /联网/.test(__txt(document.getElementById("trust-levels")))
         && /工作区/.test(__txt(document.getElementById("trust-levels")))
         && /破坏性/.test(__txt(document.getElementById("trust-levels"))),
         __txt(document.getElementById("trust-levels")).slice(0, 120));
    step("★生效范围写明「面板 + 群聊 AI 成员共用」",
         /群聊 AI 成员共用/.test(__txt(document.getElementById("trust-cur"))));

    /* ---------- 2. 切档：收紧不问 / 放宽要确认 ---------- */
    window.__calls.length = 0; window.__confirms.length = 0;
    document.querySelector('#trust-levels .trust-lv[data-lv="ask"]').click();
    await __s(350);
    var lvCall = (window.__calls || []).filter(function (c) { return c[0] === "level"; })[0] || [];
    step("★收紧到「全部询问」直接切（不弹确认）+ POST /api/permission/level",
         lvCall[1] === "ask" && (window.__confirms || []).length === 0,
         JSON.stringify({ call: lvCall, confirms: window.__confirms }));
    step("★切完当前档位跟着变",
         document.querySelector('#trust-levels .trust-lv.on').dataset.lv === "ask",
         __txt(document.getElementById("trust-cur")));

    window.__confirmOk = false;
    window.__calls.length = 0; window.__confirms.length = 0;
    document.querySelector('#trust-levels .trust-lv[data-lv="exec"]').click();
    await __s(300);
    step("★放宽到 exec 先问一句；取消就不发请求",
         (window.__confirms || []).length === 1
         && !(window.__calls || []).some(function (c) { return c[0] === "level"; }),
         JSON.stringify(window.__confirms));
    window.__confirmOk = true;
    window.__calls.length = 0; window.__confirms.length = 0;
    document.querySelector('#trust-levels .trust-lv[data-lv="all"]').click();
    await __s(350);
    var allCall = (window.__calls || []).filter(function (c) { return c[0] === "level"; })[0] || [];
    step("★放宽到 all：确认文案带历史事故（21 个文件），确认后才切",
         allCall[1] === "all" && /21 个文件/.test(String((window.__confirms || [])[0] || "")),
         JSON.stringify({ call: allCall, confirms: window.__confirms }));

    /* ---------- 3. 一键收紧 ---------- */
    window.__calls.length = 0;
    document.getElementById("trust-tighten").click();
    await __s(350);
    step("★「立即收紧」一键回到全部询问",
         (window.LEVEL === "ask")
         && (window.__calls || []).some(function (c) { return c[0] === "level" && c[1] === "ask"; }),
         "LEVEL=" + window.LEVEL);

    /* ---------- 4. 待批准（当前会话） ---------- */
    await trustRefresh();
    await __s(250);
    var pend = __txt(document.getElementById("trust-pend"));
    step("★待批准：列出当前会话的权限请求（带「去处理」）",
         /Run command/.test(pend) && !!document.querySelector("#trust-pend [data-trust-go]"),
         pend);
    step("★待批准条数写清楚", /1 条/.test(__txt(document.getElementById("trust-pend-note"))),
         __txt(document.getElementById("trust-pend-note")));

    /* ---------- 5. 群聊待批（从已有消息推） ---------- */
    CC.chatID = "7";
    CC.msgs = [{ id: 501, chat_id: "7", sender_kind: "agent", sender_id: "11",
                 content: "🔐 **权限申请**：删除 build 目录\n类型：execute\n涉及：D:/ws/build\n（90 秒内回复）",
                 created_at: new Date().toISOString() }];
    await trustRefresh();
    await __s(250);
    var pend2 = __txt(document.getElementById("trust-pend"));
    step("★群聊待批也进面板（和会话分开标）",
         /群聊/.test(pend2) && /去群聊处理/.test(pend2), pend2);

    /* ---------- 6. 最近「始终允许」：逐条 + 撤销 ---------- */
    var alw = __txt(document.getElementById("trust-always"));
    step("★最近「始终允许」列出规则（kind / 路径 / 次数）",
         /execute/.test(alw) && /D:\/ws/.test(alw) && /3 次/.test(alw), alw);
    window.__calls.length = 0;
    document.querySelector("#trust-always [data-alw-del]").click();
    await __s(400);
    var delCall = (window.__calls || []).filter(function (c) { return c[0] === "always/del"; })[0] || [];
    step("★逐条撤销 → DELETE /api/permission/always {id}",
         delCall[1] === "ar_execute_d_ws", JSON.stringify(delCall));
    step("★撤销后列表空、显示没有记忆",
         /还没有「始终允许」/.test(__txt(document.getElementById("trust-always"))),
         __txt(document.getElementById("trust-always")));

    /* ---------- 7. 清空全部 ---------- */
    window.ALWAYS = [{ id: "a1", agent: "codex-node", session: "s1", kind: "edit", path: "D:/ws", hits: 1, ts: Date.now() }];
    await trustRefresh();
    await __s(250);
    window.__calls.length = 0;
    document.querySelector("#trust-always [data-alw-clear]").click();
    await __s(350);
    var clearCall = (window.__calls || []).filter(function (c) { return c[0] === "always/del"; })[0] || [];
    step("★清空全部 → DELETE 不带 id（只清当前 agent）", clearCall[1] === "*", JSON.stringify(clearCall));

    /* ---------- 8. 引擎不支持（404）时给人话 ---------- */
    window.__always404 = true;
    await trustRefresh();
    await __s(250);
    step("★接口 404（opencode 引擎）→ 明说不支持，不假装成功",
         /不支持「始终允许」/.test(__txt(document.getElementById("trust-always"))),
         __txt(document.getElementById("trust-always")));
    window.__always404 = false;

    /* ---------- 8.5 ★信任面板新增四块（方向 3 收尾） ---------- */
    var pl = __txt(document.getElementById("trust-permlog"));
    step("★群聊审批时间线：谁批准了什么（命令原文 + 拥有者 + 时间）",
         /yiyi/.test(pl) && /npm test/.test(pl) && /拥有者 8/.test(pl) && /允许一次/.test(pl), pl);
    step("★拒绝/超时也留痕，破坏性单独标出来",
         /拒绝/.test(pl) && /超时未批/.test(pl) && /破坏性/.test(pl), pl);
    var uu = __txt(document.getElementById("trust-undo"));
    step("★可撤销：隔离区记录 + 当前会话快照（复用现有入口）",
         /隔离区/.test(uu) && /Remove-Item/.test(uu) && /快照/.test(uu)
         && !!document.querySelector('#trust-undo [data-trust-jump="quar"]')
         && !!document.querySelector('#trust-undo [data-trust-jump="undo"]'), uu);
    var ac = __txt(document.getElementById("trust-act"));
    step("★agent 活动：中文状态 + 谁 + 做了什么",
         /完成/.test(ac) && /进行中/.test(ac) && /已拒绝/.test(ac) && /npm test/.test(ac), ac);
    try {
      localStorage.setItem("cocraft.cc.decisions", JSON.stringify([
        { chat: "7", task: "重构登录", pick: "方案二", reviewer: "yiyi", target: "qwerty",
          ts: Date.now() - 1000 },
      ]));
    } catch (e) {}
    await trustRefresh();
    await __s(300);
    var dec = __txt(document.getElementById("trust-dec"));
    step("★派活/仲裁结论：推荐谁 + 哪个任务 + 谁审的",
         /推荐/.test(dec) && /方案二/.test(dec) && /重构登录/.test(dec), dec);
    document.querySelector('#trust-undo [data-trust-jump="quar"]').click();
    await __s(400);
    step("★点「打开」复用现有隔离区入口（信任面板收起）",
         !!document.getElementById("trust").hidden
         && !document.getElementById("quar").hidden);
    var qx = document.getElementById("quar-close");
    if (qx) qx.click();
    await __s(150);

    /* ---------- 8.6 ★接口返回错形状也不崩（列表归一成空） ---------- */
    window.__badShapes = true;
    await trustRefresh();
    await __s(400);
    var badTxt = __txt(document.getElementById("trust-permlog")) + " | "
      + __txt(document.getElementById("trust-undo")) + " | " + __txt(document.getElementById("trust-act"));
    step("★接口返回错形状（对象/字符串）也不崩，列表归一成空",
         /还没有审批记录/.test(badTxt) && /隔离区是空的/.test(badTxt) && /还没有活动/.test(badTxt),
         badTxt);
    window.__badShapes = false;

    /* ---------- 9. Esc 关闭 ---------- */
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(120);
    step("★Esc 关闭面板", box.hidden);
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
  #trust, #trust * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.CC && window.state) break; }
  await __sh(300);
  state.current = { id: "s1", title: "登录重构" };
  state.sessions = [{ id: "s1", title: "登录重构" }];
  CC.chatID = "7";
  CC.msgs = [{ id: 501, chat_id: "7", sender_kind: "agent", sender_id: "11",
               content: "🔐 **权限申请**：删除 build 目录\n类型：execute\n涉及：D:/ws/build\n（90 秒内回复）",
               created_at: new Date().toISOString() }];
  window.LEVEL = "edit";
  window.ALWAYS = [
    { id: "ar_1", agent: "codex-node", session: "s1", kind: "execute", path: "D:/ws", hits: 3, ts: Date.now() - 60000 },
    { id: "ar_2", agent: "codex-node", session: "s1", kind: "edit", path: "D:/ws/app.js", hits: 1, ts: Date.now() - 30000 }
  ];
  document.getElementById("btn-perm").click();
  await __sh(200);
  var te = document.querySelector("[data-trust-open]");
  if (te) te.click();
  await __sh(400);
  // 滚到新增的四块（可撤销 / 审批时间线 / 活动 / 结论）—— 截图核对用
  var tb = document.querySelector("#trust .trust-body");
  if (tb) tb.scrollTop = tb.scrollHeight;
  await __sh(200);
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
    dom = os.path.join(tmp, "probe_trust_dom.html")
    err = os.path.join(tmp, "probe_trust_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probetrust"),
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
    with open(os.path.join(tmp, "probe_trust_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probetrustshot"),
             "--window-size=1400,980", "--virtual-time-budget=30000",
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
        i = sys.argv.index("--shot")
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", "trust.png")
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
        print("[BAD] 没拿到探针结果（渲染失败）。DOM 前 300 字符：")
        print(html[:300])
        return 1
    d = json.loads(m.group(1))

    print("\n=== 信任面板 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name + (("   [" + extra[:160] + "]") if extra else ""))
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
