# -*- coding: utf-8 -*-
r"""无头回归：★主动编排（方向 2）「角色 + 派活台 + 派活汇总卡」（`ui 0.1.79`，2026-10-17）。

背景：并行本来就是**天然**的 —— 每个 AI 成员各自一条 WS + 线程 + AcpService，
一条消息 `@a @b` 就是两家同时干。缺的是**编排层**：

  ① **角色预设**（implementer / reviewer / tester / researcher）：只注入给**那个成员自己**的提示词，
     别人看不到（不污染群聊上下文）；
  ② **派活台**：多选成员 → 拼成一条 `@a @b 任务` 发进群；
  ③ **派活汇总卡**：谁已回复 / 谁还在跑（状态从群里已有的消息推出来，不额外问服务端）。

用 `frontend/index.html` 生成探针页，真跑 `app.js`，把 `/ccagent/*` 换成内存假数据，断言：

    1. 拉到角色表（4 个）+ 成员带 role/roleLabel
    2. 成员行有**角色下拉**（4 个角色 + 无角色），且按成员当前角色选中
    3. 改角色 → `POST /ccagent/update {id, role}`
    4. 「派活」→ 面板打开，chip 列出成员
    5. 选两个 → 「派活」→ **一帧** `@a @b 任务`，且 `mentions` 含两个人（服务端靠它给每人推 → 并行）
    6. 汇总卡出现：两人都「等待中」、标题 0/2
    7. **其中一个回了** → 该行变「已回复」、标题 1/2
    8. 卡片可收起
    9. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\dispatch_test.py
    python tools\dispatch_test.py --shot resources\screenshots\dispatch.png
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
PROBE = os.path.join(WEB, "_probe_dispatch.html")
URL = "http://127.0.0.1:8787/_probe_dispatch.html"

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
  var raw = window.fetch;
  window.__calls = [];
  window.__wsSent = [];

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var ROLES = {
    implementer: { label: "实现者" },
    reviewer: { label: "审查者" },
    tester: { label: "测试者" },
    researcher: { label: "调研者" }
  };
  var AGENTS = [
    { id: 11, name: "yiyi", display: "yiyi", engine: "opencode-acp", chats: ["1"],
      enabled: true, running: true, role: "reviewer", roleLabel: "审查者", warn: "", created: 1 },
    { id: 12, name: "qwerty", display: "qwerty", engine: "codex-node", chats: ["1"],
      enabled: true, running: true, role: "implementer", roleLabel: "实现者", warn: "", created: 2 }
  ];
  var PRESETS = [
    { id: "dev", label: "开发 + 审查 + 测试", builtin: true, rows: [
      { role: "implementer", name: "impl", roleLabel: "实现者" },
      { role: "reviewer", name: "rev", roleLabel: "审查者" },
      { role: "tester", name: "test", roleLabel: "测试者" }] },
    { id: "review", label: "实现 + 审查（对拍）", builtin: true, rows: [
      { role: "implementer", name: "impl", roleLabel: "实现者" },
      { role: "reviewer", name: "rev", roleLabel: "审查者" }] },
    { id: "research", label: "调研 + 审查", builtin: true, rows: [
      { role: "researcher", name: "scan", roleLabel: "调研者" },
      { role: "reviewer", name: "rev", roleLabel: "审查者" }] }
  ];

  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;

    if (path === "/ccagent/list") return J({ ok: true, agents: AGENTS, roles: ROLES, teamPresets: PRESETS });
    if (path === "/ccagent/team") {
      var tb = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["team", tb.preset, tb.rows]);
      var made = (tb.rows || []).map(function (r, i) {
        return { id: 31 + i, name: r.name || ("ai" + i), role: r.role,
                 roleLabel: (ROLES[r.role] || {}).label || "", engine: r.engine || "" };
      });
      return J({ ok: true, created: made, failed: [], agents: AGENTS });
    }
    if (path === "/ccagent/team/preset") {
      var pb = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["team/preset", pb.action, pb.label || pb.id, pb.rows || null]);
      if (pb.action === "delete") {
        PRESETS = PRESETS.filter(function (p) { return p.id !== pb.id; });
      } else {
        PRESETS = PRESETS.filter(function (p) { return p.id !== "mine"; });
        PRESETS.push({ id: "mine", label: pb.label, builtin: false, rows: pb.rows || [] });
      }
      return J({ ok: true, preset: { id: "mine", label: pb.label, builtin: false, rows: pb.rows || [] },
                 teamPresets: PRESETS });
    }
    if (path === "/ccagent/update") {
      var b = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["update", String(b.id), b.role]);
      var hit = AGENTS.filter(function (a) { return String(a.id) === String(b.id); })[0];
      if (hit) { hit.role = b.role || ""; hit.roleLabel = (ROLES[b.role] || {}).label || ""; }
      return J({ ok: true, agent: hit || {}, agents: AGENTS });
    }
    if (path === "/ccagent/create") {
      var c = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["create", c.name, c.role]);
      return J({ ok: true, agent: { id: 99, name: c.name, role: c.role || "" }, agents: AGENTS });
    }
    if (path === "/ccagent/changes") {
      window.__calls.push(["changes", s]);
      var aid = (s.split("aid=")[1] || "").split("&")[0];
      if (aid === "11") {
        return J({ ok: true, files: 2, since: 1, root: "D:/ws",
                   changed: [{ path: "app.js", add: 12, del: 3 }],
                   added: [{ path: "new/x.js" }], deleted: [], renamed: [] });
      }
      return J({ ok: true, files: 0, changed: [], added: [], deleted: [], renamed: [] });
    }
    if (path.indexOf("/ccagent/") === 0) {
      window.__calls.push([path.slice(9)]);
      return J({ ok: true, agents: AGENTS });
    }

    if (path === "/cocraft/_url") return J({ base: "https://example.invalid", ws: "wss://example.invalid/ws" });
    if (path === "/cocraft/chats") {
      return J({ items: [{ id: 1, name: "登录重构", created_at: new Date().toISOString(),
                           members: [1, 11, 12] }] });
    }
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/cocraft/") === 0) return J({ ok: true, items: [], data: [], total: 0 });
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
  window.confirm = function () { return true; };
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
    for (var i = 0; i < 200; i++) { await __s(25); if (window.CC) break; }
    await __s(150);

    CC.token = "TKN";                 // ccLoadAI 没 token 会直接返回
    CC.chatID = "1";
    CC.me = { id: 1, username: "tester" };
    CC.msgs = [];
    // ⚠ `ccSend()` 要求 `CC.wsock.readyState === 1` —— 探针里没走 ccConnect()，
    //   所以这里直接塞一个（桩 WebSocket 的 send 会把帧记进 `window.__wsSent`）。
    CC.wsock = new WebSocket("wss://example.invalid/ws");
    await ccLoadAI();
    await __s(150);

    /* ---------- 1. 角色表 + 成员角色 ---------- */
    out.info.roles = Object.keys(CC.roles || {});
    step("★拉到角色表（4 个）", Object.keys(CC.roles || {}).length === 4, JSON.stringify(CC.roles));
    step("★成员带 role / roleLabel",
         (CC.ai || []).length === 2 && CC.ai[0].role === "reviewer" && CC.ai[0].roleLabel === "审查者",
         JSON.stringify(CC.ai));

    /* ---------- 2. 成员行有角色下拉，并选中当前角色 ---------- */
    await ccLoadAI();
    ccRenderAI();
    var sels = document.querySelectorAll("#cc-ai-list .cc-ai-role");
    out.info.selCount = sels.length;
    step("★每个成员行都有一个角色下拉", sels.length === 2, "sels=" + sels.length);
    var selY = document.querySelector('[data-role-for="11"]');
    step("★下拉里有 4 个角色 + 无角色，且按成员当前角色选中",
         !!selY && selY.options.length === 5 && selY.value === "reviewer",
         selY ? ("n=" + selY.options.length + " v=" + selY.value) : "(没有)");
    var selNew = document.getElementById("cc-ai-role");
    step("★「建成员」那一行也有角色下拉",
         !!selNew && selNew.options.length === 5, selNew ? ("n=" + selNew.options.length) : "(没有)");

    /* ---------- 2b. ☰ 菜单排版：不许横向溢出 / 大脑下拉要看得见 ---------- */
    // 抽屉/主区得先真的显示（探针没走登录流程，否则量到的宽全是 0）
    document.getElementById("cc-auth").hidden = true;
    document.getElementById("cc-main").hidden = false;
    setCocraft(true);
    ccMenuOpen(true);
    await __s(250);
    var pop = document.getElementById("cc-menu-pop");
    var aiRow = document.querySelector("#cc-ai-list .cc-ai");
    var engSel = aiRow ? aiRow.querySelector(".cc-ai-eng") : null;
    step("★☰ 菜单不横向溢出（窄了就换行/滚动，不裁控件）",
         !!pop && pop.scrollWidth <= pop.clientWidth + 1,
         pop ? ("sw=" + pop.scrollWidth + " cw=" + pop.clientWidth) : "(没有)");
    step("★成员行的「大脑」下拉真的可见（以前被 overflow 裁掉）",
         !!engSel && engSel.getBoundingClientRect().width >= 40,
         engSel ? ("w=" + Math.round(engSel.getBoundingClientRect().width)
                  + " pop=" + Math.round(pop.getBoundingClientRect().width)
                  + " list=" + Math.round(document.getElementById("cc-ai-list").getBoundingClientRect().width))
                : "(没有)");
    step("★新建成员分成两行（名字+建 / 大脑+角色+建组）",
         document.querySelectorAll(".cc-ai-add .cc-inline").length === 2,
         "lines=" + document.querySelectorAll(".cc-ai-add .cc-inline").length);
    ccMenuOpen(false);
    setCocraft(false);
    await __s(120);

    /* ---------- 3. 改角色 → POST /ccagent/update ---------- */
    window.__calls.length = 0;
    await ccAiRole("11", "tester");
    await __s(150);
    var upd = (window.__calls || []).filter(function (c) { return c[0] === "update"; });
    out.info.upd = upd;
    step("★改角色 → POST /ccagent/update {id, role}",
         upd.length === 1 && upd[0][1] === "11" && upd[0][2] === "tester", JSON.stringify(upd));
    // 复原（后面「请审查者仲裁」需要 yiyi 仍是审查者）
    CC.ai[0].role = "reviewer";
    CC.ai[0].roleLabel = "审查者";

    /* ---------- 4. 派活台 ---------- */
    ccDisOpen(true);
    await __s(150);
    var panel = document.getElementById("cc-dis");
    step("★「派活」面板打开", !!panel && !panel.hidden);
    var chips = document.querySelectorAll("#cc-dis-members .cc-dis-chip");
    out.info.chips = chips.length;
    step("★chip 列出成员（带角色小标）", chips.length === 2
         && /审查者|实现者/.test(__txt(document.getElementById("cc-dis-members"))),
         __txt(document.getElementById("cc-dis-members")));

    /* ---------- 5. 选两个 → 派活 → 一帧 @a @b ---------- */
    chips[0].click();
    document.querySelectorAll("#cc-dis-members .cc-dis-chip")[1].click();
    await __s(120);
    document.getElementById("cc-dis-task").value = "各出一个方案：把登录拆成独立模块";
    window.__wsSent.length = 0;
    ccDispatchGo();
    await __s(200);
    var fr = (window.__wsSent || [])[0] || {};
    out.info.frame = fr;
    step("★派活发**一帧**，内容是「@a @b 任务」",
         /^@yiyi @qwerty /.test(String(fr.content || ""))
         && /各出一个方案/.test(String(fr.content || "")), JSON.stringify(fr).slice(0, 200));
    step("★mentions 里两个名字都在（服务端靠它给每人推 → 并行）",
         JSON.stringify(fr.mentions || []) === JSON.stringify(["yiyi", "qwerty"]),
         JSON.stringify(fr.mentions || []));
    step("★派活后面板自动收起", !!panel && panel.hidden);

    /* ---------- 6. 汇总卡：都还在等 ---------- */
    var card = document.getElementById("cc-dis-card");
    out.info.cap0 = __txt(card);
    step("★汇总卡出现：2 行都「等待中」、标题 0/2",
         !!card && /0\/2 已回复/.test(__txt(card))
         && (card.textContent.match(/等待中/g) || []).length === 2,
         out.info.cap0);

    /* ---------- 7. 其中一个回了 → 该行变「已回复」 ---------- */
    CC.msgs.push({ id: 9001, chat_id: "1", sender_kind: "agent", sender_id: "11",
                   content: "结论：不通过。理由……", created_at: new Date().toISOString() });
    ccRenderMsgs(false);
    await __s(150);
    card = document.getElementById("cc-dis-card");
    out.info.cap1 = __txt(card);
    step("★某个成员回了 → 该行「已回复」、标题 1/2",
         !!card && /1\/2 已回复/.test(__txt(card))
         && (card.textContent.match(/已回复/g) || []).length >= 1
         && (card.textContent.match(/等待中/g) || []).length === 1,
         out.info.cap1);

    /* ---------- 7b. 两人都回了 → 多方案对比 ---------- */
    CC.msgs.push({ id: 9002, chat_id: "1", sender_kind: "agent", sender_id: "12",
                   content: "方案二：\n1) 抽一个 LoginService\n2) 补单测", created_at: new Date().toISOString() });
    ccRenderMsgs(false);
    await __s(150);
    step("★两人都回了 → 2/2", /2\/2 已回复/.test(__txt(document.getElementById("cc-dis-card"))),
         __txt(document.getElementById("cc-dis-card")));
    var cmpBtn = document.querySelector("#cc-dis-card [data-dis-cmp]");
    step("★汇总卡上有「对比」按钮", !!cmpBtn, cmpBtn ? "" : "(没有)");
    if (cmpBtn) cmpBtn.click();
    await __s(300);
    var cmp = document.getElementById("cmp");
    step("★对比弹窗打开，**两列**都在",
         !!cmp && !cmp.hidden && document.querySelectorAll("#cmp-cols .cmp-col").length === 2,
         cmp ? ("hidden=" + cmp.hidden + " cols=" + document.querySelectorAll("#cmp-cols .cmp-col").length) : "(没有)");
    step("★两列各自渲染正文（Markdown → HTML）",
         /不通过/.test(__txt(document.getElementById("cmp-cols")))
         && /LoginService/.test(__txt(document.getElementById("cmp-cols")))
         && document.querySelectorAll("#cmp-cols .cmp-body").length === 2,
         __txt(document.getElementById("cmp-cols")).slice(0, 140));
    step("★顶部写着这次的任务", /各出一个方案/.test(__txt(document.getElementById("cmp-task"))),
         __txt(document.getElementById("cmp-task")));
    // ★文件改动（接方向 1 的轮快照：派活前 → 现在，**文件级**）
    await __s(250);
    var ccols = document.querySelectorAll("#cmp-cols .cmp-col");
    var chg0 = ccols[0] ? ccols[0].querySelector(".cmp-chg") : null;
    step("★第一列有「文件改动 · N 个」", !!chg0 && /文件改动 · 2 个/.test(__txt(chg0)),
         chg0 ? __txt(chg0) : "(没有)");
    step("★列出改了哪些文件（改 app.js / 增 new/x.js）",
         !!chg0 && /改\s*app\.js/.test(__txt(chg0)) && /增\s*new\/x\.js/.test(__txt(chg0)),
         chg0 ? __txt(chg0) : "(没有)");
    step("★没有改动的成员不显示那一块（不编）",
         !!(ccols[1] && !ccols[1].querySelector(".cmp-chg")),
         ccols[1] ? __txt(ccols[1]).slice(0, 60) : "(没有第二列)");
    document.getElementById("cmp-diff-btn").click();
    await __s(250);
    var dbox = document.getElementById("cmp-diff");
    step("★「逐行差异」画出红绿行（复用 renderDiff）",
         !!dbox && !dbox.hidden && !!dbox.querySelector(".tr-diff")
         && document.querySelectorAll("#cmp-diff .dl.add, #cmp-diff .dl.del").length > 0,
         dbox ? dbox.innerHTML.slice(0, 90) : "(没有)");
    document.getElementById("cmp-close").click();
    await __s(120);
    step("★对比弹窗可关闭", !!cmp && cmp.hidden);

    /* ---------- 7c. 结果仲裁：请 reviewer 拍板 ---------- */
    ccCmpOpen();
    await __s(200);
    var arbGo = document.querySelector("#cmp-arb [data-arb-go]");
    step("★对比弹窗里有「请审查者仲裁」按钮", !!arbGo,
         arbGo ? __txt(arbGo) : ("arb=" + __txt(document.getElementById("cmp-arb"))));
    window.__wsSent.length = 0;
    if (arbGo) arbGo.click();
    await __s(250);
    var arbfr = (window.__wsSent || [])[0] || {};
    out.info.arbFrame = arbfr;
    step("★仲裁 = 给**审查者**发一条 `@yiyi 【仲裁】…`（要求按格式收口）",
         /^@yiyi 【仲裁】/.test(String(arbfr.content || ""))
         && /推荐/.test(String(arbfr.content || "")),
         String(arbfr.content || "").slice(0, 120));
    step("★仲裁区立刻显示「等待中…」", /等待中/.test(__txt(document.getElementById("cmp-arb"))),
         __txt(document.getElementById("cmp-arb")));

    // 审查者回话了（含"结论：推荐 方案二"）
    CC.msgs.push({ id: 9003, chat_id: "1", sender_kind: "agent", sender_id: "11",
                   content: "两份我都看了。\n\n- 方案一：结构对，但没提测试\n- 方案二：拆得干净、带单测\n\n结论：推荐 方案二，因为能立刻落单测。",
                   created_at: new Date().toISOString() });
    ccCmpRender();
    await __s(200);
    var arb = document.getElementById("cmp-arb");
    step("★审查者回话 → 仲裁区「已出结论」",
         /已出结论/.test(__txt(arb)), __txt(arb).slice(0, 120));
    step("★顶部抠出「推荐：方案二」",
         /推荐：\s*方案二/.test(__txt(arb)), __txt(arb).slice(0, 120));
    step("★仲裁正文也渲染出来（Markdown）",
         !!arb.querySelector(".cmp-arb-body") && /拆得干净/.test(__txt(arb)),
         __txt(arb).slice(0, 120));
    // ⚠ 仲裁那次发言**不能**被算进 reviewer 自己的「方案」列（时间窗过滤）
    var yCol = Array.prototype.slice.call(document.querySelectorAll("#cmp-cols .cmp-col"))
      .filter(function (c) { return /yiyi/.test(__txt(c)); })[0];
    step("★仲裁发言**不污染** reviewer 自己的方案列",
         !!yCol && !/推荐 方案二/.test(__txt(yCol)), yCol ? __txt(yCol).slice(0, 90) : "(没有 yiyi 列)");
    ccCmpClose();
    await __s(120);

    /* ---------- 7d. 「推荐 X」可点：跳到那一列 + 记历史 ---------- */
    ccCmpOpen();
    await __s(250);
    var recBtn = document.querySelector("#cmp-arb [data-arb-pick]");
    step("★「推荐：X」是可点的（按钮，不是死文字）",
         !!recBtn && /方案二/.test(__txt(recBtn)), recBtn ? __txt(recBtn) : "(没有)");
    var hist0 = ccDecisionHistory();
    step("★结论一出来就记一份历史（供信任面板复用）",
         hist0.length >= 1 && hist0[0].pick === "方案二" && hist0[0].target === "qwerty",
         JSON.stringify(hist0[0] || null));
    if (recBtn) recBtn.click();
    await __s(300);
    var pickedCols = document.querySelectorAll("#cmp-cols .cmp-col.pick");
    out.info.picked = pickedCols.length ? __txt(pickedCols[0]).slice(0, 80) : "";
    step("★点一下 → 跳到**正文里写着「方案二」的那一列**（qwerty）并高亮",
         pickedCols.length === 1 && /qwerty/.test(__txt(pickedCols[0])), out.info.picked);
    ccCmpClose();
    await __s(120);

    /* ---------- 7e. 一键建组：预设可自定义 + 建完自动派第一活 ---------- */
    ccTeamOpen(true);
    await __s(200);
    step("★建组面板有「存为预设 / 删」和「第一活」输入框",
         !!document.getElementById("cc-team-save") && !!document.getElementById("cc-team-del")
         && !!document.getElementById("cc-team-task"));
    var role0 = document.querySelector('#cc-team-rows [data-team-role="0"]');
    if (role0) { role0.value = "tester"; role0.dispatchEvent(new Event("change", { bubbles: true })); }
    var name0 = document.querySelector('#cc-team-rows [data-team-name="0"]');
    if (name0) { name0.value = "myimpl"; name0.dispatchEvent(new Event("input", { bubbles: true })); }
    window.__calls.length = 0;
    var savedOk = await ccTeamSavePreset("我的手组");
    await __s(150);
    var saveCall = (window.__calls || []).filter(function (c) {
      return c[0] === "team/preset" && c[1] === "save";
    })[0] || [];
    step("★存为预设 → POST /ccagent/team/preset（带角色 + 名字）",
         savedOk === true && saveCall[2] === "我的手组"
         && !!saveCall[3] && saveCall[3][0].role === "tester" && saveCall[3][0].name === "myimpl",
         JSON.stringify(saveCall).slice(0, 160));
    var selPreset = document.getElementById("cc-team-preset");
    step("★存完自动选中新预设（下拉里带 ★ 自定义标）",
         /我的手组/.test(selPreset.textContent) && selPreset.value === "mine",
         selPreset ? ("v=" + selPreset.value + " " + __txt(selPreset)) : "(没有)");

    selPreset.value = "dev"; selPreset.dispatchEvent(new Event("change", { bubbles: true }));
    window.__calls.length = 0;
    var delBuiltin = await ccTeamDeletePreset();
    step("★内置预设不能删（只提示，不发请求）",
         delBuiltin === false
         && !(window.__calls || []).some(function (c) { return c[0] === "team/preset"; })
         && /内置预设不能删/.test(__txt(document.getElementById("cc-team-note"))),
         __txt(document.getElementById("cc-team-note")));

    selPreset.value = "mine"; selPreset.dispatchEvent(new Event("change", { bubbles: true }));
    window.__calls.length = 0;
    var delCustom = await ccTeamDeletePreset();
    await __s(150);
    step("★自定义预设可删（发 delete）",
         delCustom === true && (window.__calls || []).some(function (c) {
           return c[0] === "team/preset" && c[1] === "delete";
         }), JSON.stringify(window.__calls || []).slice(0, 140));
    step("★删完下拉里没有它了", !/我的手组/.test(document.getElementById("cc-team-preset").textContent));

    selPreset.value = "dev"; selPreset.dispatchEvent(new Event("change", { bubbles: true }));
    document.getElementById("cc-team-task").value = "各出一个实现方案";
    window.__wsSent.length = 0;
    window.__calls.length = 0;
    await ccTeamGo();
    await __s(250);
    var teamCall = (window.__calls || []).filter(function (c) { return c[0] === "team"; })[0] || [];
    step("★建组把「面板里那几行」整行发过去（支持临时改角色 / 名字）",
         !!teamCall[2] && teamCall[2].length === 3 && teamCall[2][0].role === "implementer",
         JSON.stringify(teamCall).slice(0, 160));
    var autoFr = (window.__wsSent || [])[0] || {};
    step("★建完自动派第一活：一帧 `@impl @rev @test 各出一个实现方案`",
         /^@impl @rev @test 各出一个实现方案/.test(String(autoFr.content || ""))
         && JSON.stringify(autoFr.mentions || []) === JSON.stringify(["impl", "rev", "test"]),
         JSON.stringify(autoFr).slice(0, 180));
    step("★并挂上派活汇总卡（0/3 等待中）",
         /0\/3 已回复/.test(__txt(document.getElementById("cc-dis-card"))),
         __txt(document.getElementById("cc-dis-card")));
    ccTeamOpen(false);

    /* ---------- 8. 收起卡片 ---------- */
    var hide = document.querySelector("#cc-dis-card [data-dis-hide]");
    if (hide) hide.click();
    await __s(120);
    step("★卡片可收起", !document.getElementById("cc-dis-card"));
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
  html, #cocraft, #cc-dis, #cc-dis * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.CC) break; }
  await __sh(200);
  CC.token = "TKN"; CC.chatID = "1"; CC.me = { id: 1, username: "tester" };
  CC.chats = [{ id: 1, name: "登录重构", created_at: new Date().toISOString() }];
  CC.msgs = [
    { id: 1, chat_id: "1", sender_kind: "user", sender_id: 1, content: "这个登录模块太乱了，拆一下", created_at: new Date().toISOString() }
  ];
  await ccLoadAI();
  try { setCocraft(true); } catch (e) { try { document.documentElement.classList.add("cc-open"); } catch (e2) {} }
  try { ccShowMain(); ccRenderChats(); ccRenderMsgs(); } catch (e) {}
  await __sh(400);
  ccDisOpen(true);
  await __sh(250);
  var chips = document.querySelectorAll("#cc-dis-members .cc-dis-chip");
  if (chips[0]) chips[0].click();
  if (chips[1]) chips[1].click();
  document.getElementById("cc-dis-task").value = "各出一个方案：把登录拆成独立模块";
  await __sh(300);
})();
</script>
"""


CMP_SHOT_JS = r"""<style>
  #cmp, #cmp * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.CC) break; }
  await __sh(200);
  CC.token = "TKN"; CC.chatID = "1"; CC.me = { id: 1, username: "tester" };
  CC.chats = [{ id: 1, name: "登录重构", created_at: new Date().toISOString(), members: [1, 11, 12] }];
  await ccLoadAI();
  try { setCocraft(true); } catch (e) { try { document.documentElement.classList.add("cc-open"); } catch (e2) {} }
  try { ccShowMain(); ccRenderChats(); } catch (e) {}
  await __sh(300);
  // 造一次派活 + 两个人的回答
  CC.dis = { chat: "1", task: "各出一个方案：把登录拆成独立模块", ts: Date.now() - 3000,
             members: [
               { id: "11", name: "yiyi", role: "reviewer", roleLabel: "审查者", status: "wait" },
               { id: "12", name: "qwerty", role: "implementer", roleLabel: "实现者", status: "wait" }
             ] };
  CC.msgs = [
    { id: 1, chat_id: "1", sender_kind: "user", sender_id: 1, content: "各出一个方案：把登录拆成独立模块", created_at: new Date(Date.now() - 4000).toISOString() },
    { id: 2, chat_id: "1", sender_kind: "agent", sender_id: "11",
      content: "结论：不通过（现有实现）。\n\n**问题**\n1. app.js:120 —— 校验和请求写在一起，无法单测\n2. app.js:210 —— 令牌直接塞 localStorage，没做过期处理\n\n**建议**：先抽 LoginService，再补 3 条用例，最后才动 UI。",
      created_at: new Date(Date.now() - 2000).toISOString() },
    { id: 3, chat_id: "1", sender_kind: "agent", sender_id: "12",
      content: "方案二：\n\n1. 新建 `src/services/login.js`，把请求 + 校验搬进去\n2. `app.js` 只留一行 `await login(...)`\n3. 补 `login.spec.js` 两条用例（成功 / 令牌过期）\n\n已改 3 个文件，测试通过。",
      created_at: new Date(Date.now() - 1000).toISOString() }
  ];
  ccRenderMsgs(false);
  ccCmpOpen();
  await __sh(350);
  // 再让审查者仲裁一次（截图里要看到「推荐」结论）
  CC.arb = { chat: "1", ts: Date.now() - 500, task: "各出一个方案：把登录拆成独立模块",
             reviewer: { id: "11", name: "yiyi" } };
  CC.msgs.push({ id: 4, chat_id: "1", sender_kind: "agent", sender_id: "11",
    content: "两份都看了。\n\n- 方案一：结构对，但没提测试\n- 方案二：拆得干净、带单测\n\n结论：推荐 方案二，因为能立刻落单测。",
    created_at: new Date().toISOString() });
  ccCmpRender();
  await __sh(350);
  document.getElementById("cmp-diff-btn").click();
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


def build_probe_cmp() -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + CMP_SHOT_JS + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


MENU_SHOT_JS = r"""<style>
  #cocraft, #cocraft * { transition: none !important; animation: none !important; }
</style>
<script>
function __sm(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sm(25); if (window.CC) break; }
  await __sm(200);
  CC.token = "TKN"; CC.chatID = "1"; CC.me = { id: 1, username: "tester" };
  CC.chats = [{ id: 1, name: "登录重构", created_at: new Date().toISOString(), members: [1, 11, 12] }];
  await ccLoadAI();
  try { ccShowMain(); ccRenderChats(); } catch (e) {}
  try { setCocraft(true); } catch (e) {}
  await __sm(300);
  ccMenuOpen(true);
  await __sm(400);
})();
</script>
"""


def build_probe_menu() -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + MENU_SHOT_JS + '<script src="/app.js')
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
    dom = os.path.join(tmp, "probe_dispatch_dom.html")
    err = os.path.join(tmp, "probe_dispatch_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probedispatch"),
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
    with open(os.path.join(tmp, "probe_dispatch_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probedispatchshot"),
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
    if "--shot-menu" in sys.argv:
        shot = os.path.join(HERE, "resources", "screenshots", "cc_menu.png")
        build_probe_menu()
        try:
            take_shot(edge, shot)
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        return 0
    if "--shot-cmp" in sys.argv:
        shot = os.path.join(HERE, "resources", "screenshots", "compare.png")
        build_probe_cmp()
        try:
            take_shot(edge, shot)
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        return 0
    if "--shot" in sys.argv:
        shot = os.path.join(HERE, "resources", "screenshots", "dispatch.png")
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
    print("\n=== 主动编排「角色 + 派活台」无头回归 ===")
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
