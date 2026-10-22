# -*- coding: utf-8 -*-
r"""无头回归：★本地协作工作台（本机多 agent，不登录）。

真跑 `frontend/app.js`，把 `/orchestrate/*` 换成内存假数据，断言：

  1. 顶栏「协作」能打开全页工作台；左栏有运行历史
  2. 新建任务：预设填充 / 成员行（角色 + 名字 + 引擎）/ 提交的 payload 正确
  3. 三列结果并排 + 状态 chip + Markdown 正文
  4. 待批权限逐条卡片：命令原文 / 破坏性（只给「确认执行」，没有「始终允许」）/
     允许一次 / 拒绝 / 倒计时 / 审批历史
  5. 正好 2 人完成 → 逐行差异（真红绿）
  6. 仲裁：选 agent → POST；结果列 + 「推荐 X」可点跳列 + 高亮
  7. 取消全部 / 取消单个成员 / 重跑（用同样成员发新任务）
  8. Esc 先关新建弹窗、再关工作台；全程零 JS 报错

跑法（需要 8787 在跑，只用它发静态文件）：
    python tools\collab_ui_test.py
    python tools\collab_ui_test.py --shot resources\screenshots\collab.png
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
PROBE = os.path.join(WEB, "_probe_collab.html")
URL = "http://127.0.0.1:8787/_probe_collab.html"

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

  window.__calls = [];
  window.__confirmOk = true;
  window.__confirms = [];
  window.confirm = function (msg) { window.__confirms.push(String(msg || "")); return window.__confirmOk; };

  var now = Date.now();
  window.CATALOG = {
    ok: true, askTimeout: 180, runTimeout: 600,
    agents: [
      { id: "mock-a", label: "Mock 实现", available: true },
      { id: "mock-b", label: "Mock 审查", available: true },
      { id: "mock-c", label: "Mock 缺依赖", available: false, missing: ["Node"] }
    ],
    roles: { implementer: { label: "实现者" }, reviewer: { label: "审查者" },
             tester: { label: "测试者" }, researcher: { label: "调研者" } },
    presets: [
      { id: "dev", label: "开发 + 审查 + 测试", builtin: true, rows: [
        { role: "implementer", name: "impl" }, { role: "reviewer", name: "rev" },
        { role: "tester", name: "test" } ] },
      { id: "review", label: "实现 + 审查（对拍）", builtin: true, rows: [
        { role: "implementer", name: "impl" }, { role: "reviewer", name: "rev" } ] }
    ]
  };

  function M(id, name, role, roleLabel, engine, status, output, extra) {
    var m = { id: id, name: name, role: role, roleLabel: roleLabel, engine: engine,
              status: status, output: output || "", error: "", created: now - 30000,
              started: now - 29000, ended: status === "running" ? 0 : now - 1000,
              tokens: { input: 1200, output: 340 }, context: { used: 1200, size: 100000 },
              activities: [], kind: "member", sessionID: "sess_" + id, cancelled: false };
    return Object.assign(m, extra || {});
  }

  var RUN1 = {
    id: "run_1", task: "把登录拆成独立模块", title: "把登录拆成独立模块",
    cwd: "D:/ws", status: "done", created: now - 120000, updated: now - 10000,
    members: [
      M("m1", "实现者", "implementer", "实现者", "mock-a", "done",
        "## 方案 A\n\n- 拆出 `auth/login.py`\n- 保持旧接口\n\n```py\nprint('hi')\n```"),
      M("m2", "审查者", "reviewer", "审查者", "mock-b", "done",
        "## 方案 B\n\n- 拆出 `auth/session.py`\n- 风险：循环依赖"),
      M("m3", "测试者", "tester", "测试者", "mock-a", "failed", "",
        { error: "agent 缺依赖" })
    ],
    permissions: [], arbitration: null, error: ""
  };
  var RUN2 = {
    id: "run_2", task: "写个 README", title: "写个 README", cwd: "D:/ws2",
    status: "running", created: now - 5000, updated: now - 1000,
    members: [
      M("m1", "实现者", "implementer", "实现者", "mock-a", "running", ""),
      M("m2", "审查者", "reviewer", "审查者", "mock-b", "wait", "")
    ],
    permissions: [], arbitration: null, error: ""
  };

  window.RUNMAP = { run_1: RUN1, run_2: RUN2,
    run_3: { id: "run_3", task: "旧任务（可删除）", title: "旧任务（可删除）", cwd: "",
             status: "done", created: now - 300000, updated: now - 200000,
             members: [M("m1", "旧成员", "", "", "mock-a", "done", "旧结果")],
             permissions: [], arbitration: null, error: "" } };
  window.WORKFLOWS = [];
  window.RUNS = [    { id: "run_1", title: RUN1.title, task: RUN1.task, cwd: RUN1.cwd, status: RUN1.status,
      created: RUN1.created, updated: RUN1.updated, members: 3, done: 2, failed: 1, running: 0 },
    { id: "run_2", title: RUN2.title, task: RUN2.task, cwd: RUN2.cwd, status: RUN2.status,
      created: RUN2.created, updated: RUN2.updated, members: 2, done: 0, failed: 0, running: 2 },
    { id: "run_3", title: "旧任务（可删除）", task: "旧任务（可删除）", cwd: "", status: "done",
      created: now - 300000, updated: now - 200000, members: 1, done: 1, failed: 0, running: 0 }
  ];

  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/engine/", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/music/"];

  var raw = window.fetch;
  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var qs = qi >= 0 ? s.slice(qi + 1) : "";
    var method = (o && o.method) || "GET";
    var body = {};
    try { body = JSON.parse((o && o.body) || "{}"); } catch (e) {}

    if (path === "/orchestrate/catalog") return J(window.CATALOG);
    if (path === "/orchestrate/workflows") return J({ ok: true, workflows: window.WORKFLOWS || [] });
    if (path === "/orchestrate/workflows/save" && method === "POST") {
      window.__calls.push(["wf-save", body]);
      var wf = Object.assign({ id: "wf_test" }, body);
      window.WORKFLOWS = [wf];
      return J({ ok: true, workflow: wf });
    }
    if (path === "/orchestrate/runs") return J({ ok: true, runs: window.RUNS });
    if (path === "/orchestrate/run") {
      var mm = /(?:^|&)id=([^&]+)/.exec(qs);
      var id = mm ? decodeURIComponent(mm[1]) : "";
      var run = window.RUNMAP[id];
      return run ? J({ ok: true, run: run }) : J({ ok: false, error: "没有这个任务" }, 404);
    }
    if (path === "/orchestrate/pending") {
      var out = [];
      Object.keys(window.RUNMAP).forEach(function (k) {
        (window.RUNMAP[k].permissions || []).forEach(function (p) {
          if (p.status === "pending") out.push(p);
        });
      });
      return J({ ok: true, pending: out });
    }
    if (path === "/orchestrate/start" && method === "POST") {
      window.__calls.push(["start", body]);
      var rid = "run_new" + (Object.keys(window.RUNMAP).length + 1);
      var msrc = body.members || [];
      if (!msrc.length && body.workflowId) {
        var wf0 = (window.WORKFLOWS || []).filter(function (x) {
          return String(x.id) === String(body.workflowId);
        })[0];
        msrc = (wf0 && wf0.members) || [];
      }
      var ms = msrc.map(function (m, i) {
        return M("m" + (i + 1), m.name, m.role || "", m.role || "", m.engine, "running", "");
      });
      var nr = { id: rid, task: body.task, title: body.task, cwd: body.cwd || "",
                 workflowId: body.workflowId || "", workflowName: body.workflowId ? "新工作流" : "",
                 status: "running", created: Date.now(), updated: Date.now(),
                 members: ms, permissions: [], arbitration: null, error: "" };
      window.RUNMAP[rid] = nr;
      window.RUNS.unshift({ id: rid, title: nr.title, task: nr.task, status: "running",
        cwd: nr.cwd, workflowId: nr.workflowId, workflowName: nr.workflowName,
        created: nr.created, updated: nr.updated, members: ms.length, done: 0,
        failed: 0, running: ms.length });
      window.LAST_NEW = rid;
      return J({ ok: true, run: nr });
    }
    if (path === "/orchestrate/member" && method === "POST") {
      window.__calls.push(["member", body]);
      var r = window.RUNMAP[body.run];
      if (r) {
        (r.members || []).forEach(function (m) {
          if (String(m.id) === String(body.member)) m.permLevel = body.permLevel;
        });
      }
      return J({ ok: true, run: r });
    }
    if (path === "/orchestrate/cancel" && method === "POST") {
      window.__calls.push(["cancel", body]);
      var cr = window.RUNMAP[body.id];
      if (cr) {
        (cr.members || []).forEach(function (m) {
          if (!body.member || String(m.id) === String(body.member)) {
            if (m.status === "running" || m.status === "wait") { m.status = "cancelled"; m.cancelled = true; }
          }
        });
        if (!body.member) cr.status = "cancelled";
      }
      return J({ ok: true, run: cr });
    }
    if (path === "/orchestrate/permission" && method === "POST") {
      window.__calls.push(["permission", body]);
      Object.keys(window.RUNMAP).forEach(function (k) {
        (window.RUNMAP[k].permissions || []).forEach(function (p) {
          if (p.id === body.id) { p.status = body.decision === "reject" ? "denied" : "allowed";
                                  p.decision = body.decision; p.decidedAt = Date.now(); }
        });
      });
      return J({ ok: true });
    }
    if (path === "/orchestrate/arbitrate" && method === "POST") {
      window.__calls.push(["arbitrate", body]);
      var ar = window.RUNMAP[body.id];
      if (ar) {
        ar.members.push(M("arb", body.name || "审查者", "reviewer", "审查者", body.engine, "done",
          "两个方案都能用，但 B 有循环依赖风险。\n\n结论：推荐 实现者", { kind: "arbitration" }));
        ar.arbitration = { member: "arb", status: "done", pick: "实现者", created: Date.now() };
      }
      return J({ ok: true, run: ar });
    }

    if (path === "/orchestrate/delete" && method === "POST") {
      window.__calls.push(["delete", body]);
      delete window.RUNMAP[body.id];
      window.RUNS = window.RUNS.filter(function (r) { return String(r.id) !== String(body.id); });
      return J({ ok: true });
    }

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
    this.readyState = 1; this.send = function () {}; this.close = function () {};
  };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __vis(el) { return !!el && !el.hidden && getComputedStyle(el).display !== "none"; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (typeof collabOpen === "function") break; }
    await __s(400);

    /* ---------- 1. 入口 / 历史 / 自动选中 ---------- */
    document.getElementById("btn-collab").click();
    await __s(700);
    step("★顶栏「协作」打开全页工作台", __vis(document.getElementById("collab"))
         && document.getElementById("body-stage").classList.contains("collab-open"));
    step("★对话区让位（chat-stage 隐藏）",
         getComputedStyle(document.getElementById("chat-stage")).display === "none");
    var rows = document.querySelectorAll("#collab-runs .collab-run-row");
    out.info.runs = rows.length;
    step("★左栏列出运行历史（3 条）", rows.length === 3, "n=" + rows.length);
    step("★历史行写清「完成数/成员数」", /2\/3 完成/.test(__txt(rows[0])), __txt(rows[0]));
    step("★左栏显示「N 运行中」（同时跑的任务看得见）",
         /1 运行中/.test(__txt(document.getElementById("collab-count"))),
         __txt(document.getElementById("collab-count")));
    step("★默认选中最新一条并显示详情", __vis(document.getElementById("collab-run")),
         __txt(document.getElementById("collab-task-title")));

    /* ---------- 1a. ★每条工作流属于哪个项目 ---------- */
    var pj = document.querySelectorAll("#collab-runs .collab-run-proj");
    var pjTxt = Array.prototype.map.call(pj, function (x) { return __txt(x); }).join(" | ");
    step("★每条历史都标出项目（工作目录末段；没有目录写「未指定项目」）",
         pj.length === 3 && /ws/.test(pjTxt) && /ws2/.test(pjTxt) && /未指定项目/.test(pjTxt), pjTxt);
    var chips = document.querySelectorAll("#collab-proj-filter [data-collab-proj]");
    step("★左栏有「全部 / 按项目」筛选（同项目多组工作流一眼分开）",
         chips.length === 4, "chips=" + chips.length + " | " + __txt(document.getElementById("collab-proj-filter")));
    var chipWs2 = null;
    Array.prototype.forEach.call(chips, function (c) { if (/ws2/.test(__txt(c))) chipWs2 = c; });
    if (chipWs2) chipWs2.click();
    await __s(220);
    var visRows = document.querySelectorAll("#collab-runs .collab-run-row");
    step("★按项目筛选：点 ws2 只剩 1 条（run_2）",
         visRows.length === 1 && /写个 README/.test(__txt(visRows[0])), "rows=" + visRows.length);
    var chipAll = document.querySelector('#collab-proj-filter [data-collab-proj=""]');
    if (chipAll) chipAll.click();
    await __s(220);
    step("★点「全部」恢复 3 条",
         document.querySelectorAll("#collab-runs .collab-run-row").length === 3);

    await collabOpenRun("run_1");
    await __s(320);
    var metaTxt = __txt(document.getElementById("collab-task-meta"));
    step("★任务头写明「项目 + 完整目录」", /项目/.test(metaTxt) && /D:\/ws/.test(metaTxt), metaTxt);
    step("★有「打开」按钮（在编码模式打开这个项目）",
         !!document.querySelector('[data-collab-open-proj="D:/ws"]'));
    var openBtn = document.querySelector('[data-collab-open-proj="D:/ws"]');
    step("★「打开」按钮真的可见（有宽度，不是 0px）",
         !!openBtn && openBtn.getBoundingClientRect().width > 10,
         openBtn ? ("w=" + Math.round(openBtn.getBoundingClientRect().width)) : "missing");

    /* ---------- 1b. 跑着的任务不能删（先取消） ---------- */
    window.__calls.length = 0;
    document.querySelector('[data-collab-del="run_2"]').click();
    await __s(200);
    step("★跑着的任务点删除 → 提示先取消、不发请求",
         /先点「取消全部」/.test(__txt(document.getElementById("banner")))
         && !(window.__calls || []).some(function (c) { return c[0] === "delete"; }),
         __txt(document.getElementById("banner")));

    /* ---------- 2. 入口统一：只剩工作流（一次性「新建任务」已移除） ---------- */
    step("★旧的一次性入口已移除（没有 #collab-new / #collab-new-task）",
         !document.getElementById("collab-new") && !document.getElementById("collab-new-task"));
    step("★工作流入口仍在（左栏工作流旁的「＋」）", !!document.getElementById("collab-new-wf"));

    window.__calls.length = 0;
    document.getElementById("collab-new-wf").click();
    await __s(600);
    step("★新建工作流 → 直接进画布（不再先弹「选成员」弹窗）",
         __vis(document.getElementById("collab-wf")) && !__vis(document.getElementById("collab-run")),
         __txt(document.getElementById("collab-wf-name")));
    var qi = document.getElementById("collab-quick-task");
    step("★画布上有快速任务条（输入任务就能跑，不用再选一次成员）",
         !!qi && !!document.getElementById("collab-quick-run"));
    qi.value = "写个 README，并给出目录结构";
    document.getElementById("collab-quick-run").click();
    await __s(800);
    var startCall = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    var sb = startCall[1] || {};
    out.info.start = JSON.stringify(sb);
    step("★开跑只发 {task, workflowId}（成员 / 目录取工作流）",
         sb.task === "写个 README，并给出目录结构" && sb.workflowId === "wf_test",
         out.info.start);
    step("★启动后切到运行详情（标题 = 刚写的任务）",
         __vis(document.getElementById("collab-run"))
         && /写个 README/.test(__txt(document.getElementById("collab-task-title"))),
         __txt(document.getElementById("collab-task-title")));
    var allBtn = document.getElementById("collab-runs-all");
    if (allBtn) allBtn.click();
    await __s(200);
    step("★开始新任务后左栏显示「2 运行中」（可以并行）",
         /2 运行中/.test(__txt(document.getElementById("collab-count"))),
         __txt(document.getElementById("collab-count")));
    step("★点「全部」后左栏 4 条（3 条旧 + 1 条刚跑的）",
         document.querySelectorAll("#collab-runs .collab-run-row").length === 4,
         "rows=" + document.querySelectorAll("#collab-runs .collab-run-row").length);

    /* ---------- 3. 结果：默认只看选中的成员 ---------- */
    window.__calls.length = 0;
    await collabOpenRun("run_1");
    await __s(500);
    var cols = document.querySelectorAll("#collab-cols .collab-col");
    step("★默认只看选中的节点（1 列）+ 成员选择条",
         cols.length === 1 && __vis(document.getElementById("collab-mem")),
         "n=" + cols.length);
    var allBtn = document.querySelector("#collab-mem [data-collab-all]");
    step("★选择条上有「并排」开关", !!allBtn);
    if (allBtn) allBtn.click();
    await __s(250);
    cols = document.querySelectorAll("#collab-cols .collab-col");
    step("★点「并排」→ 3 列", cols.length === 3, "n=" + cols.length);
    step("★状态 chip：完成 / 失败", /完成/.test(__txt(cols[0])) && /失败/.test(__txt(cols[1]).replace(/完成/g, "")) || /失败/.test(__txt(cols[2])), "");
    step("★正文按 Markdown 渲染（标题 + 列表 + 代码块）",
         !!cols[0].querySelector(".collab-col-body h2")
         && !!cols[0].querySelector(".collab-col-body ul")
         && !!cols[0].querySelector(".collab-col-body pre"));
    step("★失败原因红字写在那一列", /agent 缺依赖/.test(__txt(cols[2])));
    step("★有「导出审计」按钮（JSON：时间线 + 审批记录）",
         !!document.querySelector("#collab-task-actions [data-collab-export]"));

    /* ---------- 4. 待批权限：命令原文 / 破坏性 / 逐条 ---------- */
    window.RUNMAP.run_1.permissions = [
      { id: "p1", run: "run_1", member: "m1", memberName: "实现者", kind: "execute",
        title: "Run command", command: "Remove-Item -Recurse -Force D:/ws/build",
        locations: ["D:/ws/build"], destructive: true, status: "pending",
        decision: null, created: Date.now() - 20000 },
      { id: "p2", run: "run_1", member: "m2", memberName: "审查者", kind: "edit",
        title: "Edit file", command: "", locations: ["D:/ws/a.py"],
        destructive: false, status: "pending", decision: null, created: Date.now() - 5000 }
    ];
    await collabFetchRun();
    await __s(300);
    var pend = document.getElementById("collab-pending");
    step("★有窄条提示「N 条待你批准」+ 去处理", /2 条待你批准/.test(__txt(document.getElementById("collab-pend-bar"))), __txt(document.getElementById("collab-pend-bar")));
    document.querySelector('#collab-task-actions [data-collab-perm-open]').click();
    await __s(220);
    step("★权限抽屉打得开、位置够宽（命令不用挤）",
         pend.classList.contains("open") && pend.getBoundingClientRect().width >= 380,
         "w=" + Math.round(pend.getBoundingClientRect().width));
    var lvSel = pend.querySelector('[data-perm-level="m1"]');
    step("★抽屉里能单独给成员设权限等级", !!lvSel, lvSel ? lvSel.value : "missing");
    if (lvSel) {
      window.__calls.length = 0;
      lvSel.value = "trust";
      lvSel.dispatchEvent(new Event("change", { bubbles: true }));
      await __s(450);
      var mv = (window.__calls || []).filter(function (c) { return c[0] === "member"; })[0] || [];
      step("★改成员权限 → POST /orchestrate/member（立刻生效）",
           mv[1] && mv[1].member === "m1" && mv[1].permLevel === "trust", JSON.stringify(mv));
    }
    out.info.pend = __txt(pend);
    step("★待批卡片逐条列出（2 条）", pend.querySelectorAll(".collab-perm-i").length === 2);
    step("★卡片显示**命令原文**", /Remove-Item -Recurse -Force D:\/ws\/build/.test(out.info.pend), out.info.pend.slice(0, 120));
    step("★破坏性：红字 + 只给「确认执行」，**没有「始终允许」**",
         !!(function () {
           var card = pend.querySelector('[data-perm-card="p1"]');
           return card && /破坏性操作/.test(__txt(card)) && /确认执行/.test(__txt(card))
             && !/始终允许/.test(__txt(card));
         })(), "");
    step("★非破坏性：有「始终允许」", /始终允许/.test(__txt(pend)));
    step("★有倒计时", /剩余 \d+s/.test(out.info.pend), out.info.pend.slice(0, 80));
    window.__calls.length = 0;
    pend.querySelector('[data-perm-go="p1"]').click();
    await __s(500);
    var pc = (window.__calls || []).filter(function (c) { return c[0] === "permission"; })[0] || [];
    step("★「确认执行」→ POST /orchestrate/permission（once）",
         pc[1] && pc[1].id === "p1" && pc[1].decision === "once", JSON.stringify(pc));
    await collabFetchRun();
    await __s(250);
    step("★处理后进入「审批历史」，可追溯命令原文",
         /审批历史/.test(__txt(document.getElementById("collab-pending")))
         && /Remove-Item/.test(__txt(document.getElementById("collab-pending"))), "");
    var closeP = document.querySelector('#collab-pending [data-collab-perm-close]');
    if (closeP) closeP.click();
    await __s(180);

    /* ---------- 5. 逐行差异（正好 2 人） ---------- */
    window.RUNMAP.run_1.permissions = [];
    window.RUNMAP.run_1.members[2].status = "cancelled";
    await collabFetchRun();
    await __s(300);
    var diffBtn = document.querySelector("#collab-task-actions [data-collab-diff]");
    step("★正好 2 人完成 → 出现「逐行差异」", !!diffBtn);
    if (diffBtn) diffBtn.click();
    await __s(250);
    var diff = document.getElementById("collab-diff");
    step("★差异真画成红绿行", __vis(diff)
         && diff.querySelectorAll(".tr-diff .dl.add").length > 0
         && diff.querySelectorAll(".tr-diff .dl.del").length > 0, "");

    /* ---------- 6. 仲裁：选 agent → 推荐 X 可点跳列 ---------- */
    var sel = document.getElementById("collab-arb-engine");
    step("★仲裁区有 agent 下拉（只列可用的）", !!sel && sel.options.length === 2, sel ? "n=" + sel.options.length : "");
    window.__calls.length = 0;
    var arbBtn = document.querySelector("#collab-task-actions [data-collab-arb]");
    if (arbBtn) arbBtn.click();
    await __s(700);
    var ac = (window.__calls || []).filter(function (c) { return c[0] === "arbitrate"; })[0] || [];
    step("★「请它仲裁」→ POST /orchestrate/arbitrate", !!(ac[1] && ac[1].id === "run_1" && ac[1].engine), JSON.stringify(ac));
    await collabFetchRun();
    await __s(300);
    var pick = document.querySelector("#collab-pick [data-collab-pick]");
    step("★抠到「推荐 X」并做成按钮", !!pick && /推荐/.test(__txt(pick)), __txt(document.getElementById("collab-pick")));
    if (pick) pick.click();
    await __s(250);
    var picked = document.querySelector("#collab-cols .collab-col.pick");
    step("★点「推荐 X」跳到那一列并高亮", !!picked && /实现者/.test(__txt(picked)), picked ? __txt(picked).slice(0, 40) : "");
    step("★仲裁者自己有独立一列", document.querySelectorAll("#collab-cols .collab-col").length === 4);

    /* ---------- 7. 取消 / 重跑 ---------- */
    await collabOpenRun("run_2");
    await __s(500);
    step("★运行中的任务：顶部「取消全部」，成员列有「取消」",
         !!document.querySelector('#collab-task-actions [data-collab-cancel=""]')
         && !!document.querySelector('#collab-cols [data-collab-cancel="m1"]'));
    window.__calls.length = 0;
    document.querySelector('#collab-cols [data-collab-cancel="m1"]').click();
    await __s(500);
    var cc = (window.__calls || []).filter(function (c) { return c[0] === "cancel"; })[0] || [];
    step("★取消单个成员 → POST cancel {member:m1}", cc[1] && cc[1].id === "run_2" && cc[1].member === "m1", JSON.stringify(cc));
    window.__calls.length = 0;
    document.querySelector('#collab-task-actions [data-collab-cancel=""]').click();
    await __s(500);
    var ca = (window.__calls || []).filter(function (c) { return c[0] === "cancel"; })[0] || [];
    step("★取消全部 → POST cancel（member 为空）", ca[1] && ca[1].id === "run_2" && ca[1].member === "", JSON.stringify(ca));

    await collabOpenRun("run_1");
    await __s(450);
    window.__confirms.length = 0; window.__calls.length = 0;
    var rerun = document.querySelector("#collab-task-actions [data-collab-rerun]");
    step("★完成后有「重跑」", !!rerun);
    if (rerun) rerun.click();
    await __s(700);
    var rs = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    step("★重跑 = 用同样成员发一条新任务（不含仲裁者）",
         rs[1] && rs[1].task === "把登录拆成独立模块"
         && (rs[1].members || []).length === 3
         && (rs[1].members || []).every(function (m) {
           return ["implementer", "reviewer", "tester"].indexOf(m.role) >= 0;
         }),
         JSON.stringify(rs[1] || {}));

    /* ---------- 7b. 删除已结束的运行记录 ---------- */
    window.__calls.length = 0;
    document.querySelector('[data-collab-del="run_3"]').click();
    await __s(600);
    var delCall = (window.__calls || []).filter(function (c) { return c[0] === "delete"; })[0] || [];
    step("★删除已结束任务 → POST /orchestrate/delete {id}",
         delCall[1] && delCall[1].id === "run_3", JSON.stringify(delCall));
    step("★删除后左栏不再有那一条",
         !document.querySelector('[data-collab-run="run_3"]')
         && document.querySelectorAll("#collab-runs .collab-run-row").length === 4,
         "rows=" + document.querySelectorAll("#collab-runs .collab-run-row").length
         + " state=" + (COLLAB.runs || []).map(function (r) { return r.id; }).join(",")
         + " stub=" + (window.RUNS || []).map(function (r) { return r.id; }).join(",")
         + " banner=" + __txt(document.getElementById("banner")));

    /* ---------- 7c. 回画布时权限抽屉自动收起 ---------- */
    var permBtn = document.querySelector('#collab-task-actions [data-collab-perm-open]');
    if (permBtn) permBtn.click();
    await __s(220);
    var drw = document.getElementById("collab-pending");
    step("★权限抽屉能打开（成员权限在这里改）", drw.classList.contains("open"));
    collabSelectWf("wf_test");
    await __s(280);
    step("★回工作流画布时抽屉自动收起（不会盖在画布上）",
         !drw.classList.contains("open") && drw.hidden);

    /* ---------- 8. Esc：新建弹窗已移除 → 一次就关工作台 ---------- */
    step("★没有两级 Esc 了（没有那个一次性新建弹窗）",
         !document.getElementById("collab-new-task"));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(250);
    step("★Esc 关工作台", !document.getElementById("body-stage").classList.contains("collab-open"));
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
  #collab, #collab * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 240; i++) { await __sh(25); if (typeof collabOpen === "function") break; }
  await __sh(400);
  document.getElementById("btn-collab").click();
  await __sh(700);
  await collabOpenRun("run_1");
  await __sh(500);
  window.RUNMAP.run_1.permissions = [
    { id: "p1", run: "run_1", member: "m1", memberName: "实现者", kind: "execute",
      title: "Run command", command: "Remove-Item -Recurse -Force D:/ws/build",
      locations: ["D:/ws/build"], destructive: true, status: "pending",
      decision: null, created: Date.now() - 20000 }
  ];
  await collabFetchRun();
  await __sh(400);
})();
</script>
"""


SHOT_PERM_JS = r"""<script>
(async function () {
  function w(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  for (var i = 0; i < 240; i++) { await w(25); if (typeof collabPermToggle === "function") break; }
  // ⚠ 必须等 SHOT_JS 把任务打开、把假的待批塞进去之后再开抽屉
  //   （collabOpenRun 会重置 permOpen；早开会被它关掉）
  for (var j = 0; j < 80; j++) {
    await w(50);
    var bar = document.getElementById("collab-pend-bar");
    if (bar && !bar.hidden) break;
  }
  await w(450);
  try { collabPermToggle(true); } catch (e) { /* ignore */ }
  await w(400);
})();
</script>
"""


def build_probe(shot: bool = False, shot_perm: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    extra = (SHOT_JS if shot else PROBE_JS) + (SHOT_PERM_JS if shot_perm else "")
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
    dom = os.path.join(tmp, "probe_collab_dom.html")
    err = os.path.join(tmp, "probe_collab_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probecollab"),
             "--window-size=1440,940",
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
    with open(os.path.join(tmp, "probe_collab_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probecollabshot"),
             "--window-size=1440,980", "--virtual-time-budget=40000",
             "--screenshot=" + path, URL],
            stdout=subprocess.DEVNULL, stderr=fh_err)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  截图:", path, "存在=", os.path.isfile(path))


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv or "--shot-perm" in sys.argv:
        perm = "--shot-perm" in sys.argv
        flag = "--shot-perm" if perm else "--shot"
        i = sys.argv.index(flag)
        default_name = "collab_perm.png" if perm else "collab.png"
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--") else os.path.join(
            HERE, "resources", "screenshots", default_name)
        shot = os.path.abspath(shot)
        build_probe(shot=True, shot_perm=perm)
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

    print("\n=== 本地协作工作台 · 无头回归 ===")
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
