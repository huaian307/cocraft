# -*- coding: utf-8 -*-
r"""无头回归：★本地协作「工作流画布」（0.2.11）。

真跑 `frontend/app.js`，把 `/orchestrate/*` 换成内存假数据，断言：

  1. 打开工作台 → **默认选中工作流**（不是直接跳运行详情）；左栏按项目分组
  2. 画布：任务节点 + 成员节点 + 连线（reviewer 有虚线）
  3. 双击/单击成员 → 检查器能改角色 / 名字 / 大脑（改完自动保存）
  4. 拖动节点 → 坐标变化并**保存**（防抖后发 workflows/save）
  5. 画布「＋ 成员」→ 成员 +1、节点 +1
  6. 快速任务条 ▶ / Enter → POST /orchestrate/start 带 `workflowId`，并切到运行详情
  7. 运行详情「← 工作流」→ 回到画布
  8. 左栏工作流 ▶ → 用默认任务一键跑
  9. 新建工作流 → workflows/save，列表 +1 并选中
  10. 全程零 JS 报错

跑法：python tools\workflow_canvas_ui_test.py
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_wfcanvas.html")
URL = "http://127.0.0.1:8787/_probe_wfcanvas.html"

# 控制台是 GBK 时，步骤名里的 ▶ / ∎ 这类符号会抛 UnicodeEncodeError（不是测试失败）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  try { localStorage.removeItem("cocraft.collab.canvasTip"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/engine/", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/music/",
              "/ccagent/", "/snapshot/", "/quarantine/", "/themes", "/pick/"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  function now() { return Date.now(); }
  window.__calls = [];
  function call(kind, body) { window.__calls.push([kind, body || {}]); }

  window.CATALOG = {
    agents: [{ id: "mock-a", label: "Mock 实现", available: true },
             { id: "mock-b", label: "Mock 审查", available: true }],
    roles: { implementer: { label: "实现者" }, reviewer: { label: "审查者" },
             tester: { label: "测试者" }, researcher: { label: "调研者" } },
    presets: [], askTimeout: 180, runTimeout: 600
  };
  window.WFS = [{
    id: "wf_1", name: "登录重构", cwd: "D:/ws", taskTemplate: "把登录拆开",
    stages: [{ id: "s1", name: "实现", when: "always", onFail: "stop", retries: 0 },
             { id: "s2", name: "审查", when: "always", onFail: "stop", retries: 0 }],
    members: [{ id: "m1", role: "implementer", name: "impl", engine: "mock-a", stage: "s1" },
              { id: "m2", role: "reviewer", name: "rev", engine: "mock-b", stage: "s2" }],
    layout: { zoom: 1, nodes: { task: { x: 40, y: 120 },
                                m1: { x: 360, y: 30 }, m2: { x: 760, y: 30 } } },
    runCount: 1, lastRunId: "run_1", created: now() - 60000, updated: now() - 1000
  }];
  window.RUNS = [{
    id: "run_1", title: "登录重构", task: "把登录拆开", cwd: "D:/ws",
    workflowId: "wf_1", workflowName: "登录重构", status: "done",
    created: now() - 40000, updated: now() - 1000,
    members: 2, done: 2, failed: 0, running: 0
  }, {
    id: "run_2", title: "旧任务", task: "旧任务的正文", cwd: "D:/ws",
    workflowId: "", workflowName: "", status: "done",
    created: now() - 90000, updated: now() - 60000,
    members: 2, done: 2, failed: 0, running: 0
  }];
  function M(id, name, role, roleLabel, engine, status, output, stage) {
    return { id: id, name: name, role: role, roleLabel: roleLabel, engine: engine,
             status: status, output: output || "完成", error: "", created: now() - 30000,
             started: now() - 29000, ended: now() - 1000,
             tokens: { input: 100, output: 20 }, context: { used: 100, size: 100000 },
             activities: [], kind: "member", sessionID: "sess_" + id, cancelled: false,
             stage: stage || "s1", retryCount: 0 };
  }
  window.RUN = {
    id: "run_1", title: "登录重构", task: "把登录拆开", cwd: "D:/ws",
    workflowId: "wf_1", workflowName: "登录重构", status: "done",
    created: now() - 40000, updated: now() - 1000,
    stages: [{ id: "s1", name: "实现", status: "done", started: now() - 39000, ended: now() - 20000 },
             { id: "s2", name: "审查", status: "done", started: now() - 20000, ended: now() - 1000 }],
    members: [M("m1", "impl", "implementer", "实现者", "mock-a", "done", "完成", "s1"),
              M("m2", "rev", "reviewer", "审查者", "mock-b", "done", "完成", "s2")],
    permissions: [], arbitration: null, error: ""
  };
  window.RUN_NEW = JSON.parse(JSON.stringify(window.RUN));
  window.RUN_NEW.id = "run_new";
  window.RUN_NEW.status = "running";
  window.RUN_NEW.stages = [{ id: "s1", name: "实现", status: "running", started: now() - 3000, ended: 0 },
                           { id: "s2", name: "审查", status: "wait", started: 0, ended: 0 }];
  window.RUN_NEW.members[0].status = "running";
  window.RUN_NEW.members[1].status = "wait";

  // ★旧 run（没有 workflowId）：用来验证「存为工作流」
  window.RUN_ORPHAN = JSON.parse(JSON.stringify(window.RUN));
  window.RUN_ORPHAN.id = "run_2";
  window.RUN_ORPHAN.title = "旧任务";
  window.RUN_ORPHAN.task = "旧任务的正文";
  window.RUN_ORPHAN.workflowId = "";
  window.RUN_ORPHAN.workflowName = "";

  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var qs = qi >= 0 ? s.slice(qi + 1) : "";
    var method = String((o && o.method) || "GET").toUpperCase();
    var body = {};
    try { body = JSON.parse((o && o.body) || "{}"); } catch (e) {}

    if (path === "/orchestrate/catalog") return J(window.CATALOG);
    if (path === "/orchestrate/runs") return J({ ok: true, runs: window.RUNS });
    if (path === "/orchestrate/workflows" && method === "GET") {
      return J({ ok: true, workflows: window.WFS });
    }
    if (path === "/orchestrate/workflows/save" && method === "POST") {
      call("save", body);
      var wf = null;
      for (var i = 0; i < window.WFS.length; i++) {
        if (window.WFS[i].id === body.id) { wf = window.WFS[i]; break; }
      }
      if (!wf) {
        wf = { id: "wf_new_" + window.WFS.length, runCount: 0, lastRunId: "",
               created: now(), updated: now() };
        window.WFS.unshift(wf);
      }
      Object.assign(wf, {
        name: body.name || wf.name || "未命名工作流", cwd: body.cwd || "",
        taskTemplate: body.taskTemplate || "", members: body.members || [],
        stages: body.stages || [],
        layout: body.layout || { zoom: 1, nodes: {} }, updated: now()
      });
      return J({ ok: true, workflow: wf });
    }
    if (path === "/orchestrate/workflows/duplicate" && method === "POST") {
      call("duplicate", body);
      var src = window.WFS[0];
      var cp = JSON.parse(JSON.stringify(src));
      cp.id = "wf_dup"; cp.name = (src.name || "") + " 副本";
      window.WFS.unshift(cp);
      return J({ ok: true, workflow: cp });
    }
    if (path === "/orchestrate/workflows/delete" && method === "POST") {
      call("wfdel", body);
      window.WFS = window.WFS.filter(function (w) { return w.id !== body.id; });
      return J({ ok: true });
    }
    if (path === "/orchestrate/workflows/archive" && method === "POST") {
      call("archive", body);
      var aw = null;
      for (var a = 0; a < window.WFS.length; a++) {
        if (window.WFS[a].id === body.id) { aw = window.WFS[a]; break; }
      }
      if (!aw) return J({ ok: false, error: "no wf" }, 404);
      aw.archived = !!body.archived;
      return J({ ok: true, workflow: aw });
    }
    if (path === "/orchestrate/workflows/from-run" && method === "POST") {
      call("fromrun", body);
      var srcRun = body.id === "run_2" ? window.RUN_ORPHAN : window.RUN;
      var fw = {
        id: "wf_fromrun", name: (srcRun.title || "另存") + " 工作流", cwd: srcRun.cwd,
        taskTemplate: srcRun.task, runCount: 1, lastRunId: srcRun.id, archived: false,
        created: now(), updated: now(),
        members: (srcRun.members || []).map(function (m, i) {
          return { id: "m" + (i + 1), role: m.role, name: m.name, engine: m.engine };
        }),
        layout: { zoom: 1, nodes: {} }
      };
      window.WFS.unshift(fw);
      return J({ ok: true, workflow: fw, existing: false });
    }
    if (path === "/orchestrate/start" && method === "POST") {
      call("start", body);
      return J({ ok: true, run: window.RUN_NEW });
    }
    if (path === "/orchestrate/run") {
      var mm = /(?:^|&)id=([^&]+)/.exec(qs);
      var id = mm ? decodeURIComponent(mm[1]) : "";
      var runOut = window.RUN;
      if (id === "run_new") runOut = window.RUN_NEW;
      else if (id === "run_2") runOut = window.RUN_ORPHAN;
      return J({ ok: true, run: runOut });
    }
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) return J({ data: [], cursor: null });
    if (/^\/api\/session\/[^/]+$/.test(path)) {
      return J({ data: { id: "s1", title: "t", location: { directory: "D:/ws" },
                         model: { id: "m", providerID: "p" } } });
    }
    if (path === "/api/session") return J({ data: [], cursor: null });
    if (path === "/api/model") return J({ data: [] });
    if (path === "/engine/status") return J({ active: "acp", available: [], engines: [] });
    for (var d = 0; d < DEAD.length; d++) {
      if (s.indexOf(DEAD[d]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
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
window.confirm = function () { return true; };   // 「分阶段」用 confirm（无头里别弹模态卡住）
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __t(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __vis(el) { return !!el && !el.hidden && el.getBoundingClientRect().width > 0; }
function __node(id) { return document.querySelector('#collab-cv-nodes [data-node="' + id + '"]'); }
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 300)]); }
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (typeof collabOpen === "function") break; }
    await __s(400);

    /* 1. 打开：默认工作流视图 */
    document.getElementById("btn-collab").click();
    await __s(1000);
    step("★打开工作台默认选中工作流（不是直接跳运行）",
         __vis(document.getElementById("collab-wf")) && !__vis(document.getElementById("collab-run")),
         "wf=" + __vis(document.getElementById("collab-wf")) + " run=" + __vis(document.getElementById("collab-run")));
    var wfRows = document.querySelectorAll("#collab-wf-list [data-wf]");
    step("★左栏工作流按项目分组", wfRows.length === 1
         && /ws/.test(__t(document.getElementById("collab-wf-list"))), __t(document.getElementById("collab-wf-list")));

    /* 1b. ★整窗 + 任务输入条搬到「小本子」的位置 */
    var collabEl = document.getElementById("collab");
    var cst = getComputedStyle(collabEl);
    var compH = document.getElementById("composer").getBoundingClientRect().height;
    var ch = collabEl.getBoundingClientRect().height;
    step("★工作台整窗（fixed；底部给输入区让位）",
         cst.position === "fixed"
         && Math.abs(ch - (window.innerHeight - compH)) < 8,
         "pos=" + cst.position + " h=" + Math.round(ch)
         + " expect=" + Math.round(window.innerHeight - compH));
    step("★任务输入条搬到小本子（composer 里，画布上方不再有）",
         !!document.querySelector("#composer .collab-quick")
         && !document.querySelector("#collab-wf .collab-quick")
         && /这次要做什么/.test(__t(document.querySelector("#composer .composer-label"))),
         __t(document.querySelector("#composer .composer-label")));

    /* 2. 画布节点 / 连线 */
    var nodes = document.querySelectorAll("#collab-cv-nodes [data-node]");
    var edges = document.querySelectorAll("#collab-cv-edges path");
    out.info.nodes = nodes.length; out.info.edges = edges.length;
    step("★画布：任务 + 2 个阶段 + 2 个成员节点", nodes.length === 5, "n=" + nodes.length);
    step("★连线：任务→阶段→成员（阶段之间串行 + 阶段内扇出）",
         edges.length >= 4
         && document.querySelectorAll("#collab-cv-edges path.flow").length >= 2
         && document.querySelectorAll("#collab-cv-edges path.fan").length >= 2,
         "edges=" + edges.length
         + " flow=" + document.querySelectorAll("#collab-cv-edges path.flow").length
         + " fan=" + document.querySelectorAll("#collab-cv-edges path.fan").length);
    // ★连线锚点：按相对方位选最近边（节点被拖到阶段上方/右侧也能连得明白）
    var pAbove = cvEdgePath({ x: 0, y: 0 }, 200, 100, { x: 0, y: -200 }, 200, 100);
    var pRight = cvEdgePath({ x: 0, y: 0 }, 200, 100, { x: 400, y: 0 }, 200, 100);
    step("★连线按方位选锚点（上方 → 顶边；右侧 → 右边）",
         /^M100 0/.test(pAbove) && /^M200 50/.test(pRight),
         pAbove + " | " + pRight);

    /* 3. 检查器 */
    var m1 = __node("m1");
    m1.click();
    await __s(250);
    var insp = document.getElementById("collab-inspector");
    step("★点成员 → 检查器（角色 / 名字 / 大脑）",
         __vis(insp) && !!insp.querySelector("[data-insp-role]")
         && !!insp.querySelector("[data-insp-name]") && !!insp.querySelector("[data-insp-engine]"),
         __t(insp));
    var roleSel = insp.querySelector("[data-insp-role]");
    roleSel.value = "tester";
    roleSel.dispatchEvent(new Event("change", { bubbles: true }));
    await __s(200);
    step("★改角色 → 工作流成员跟着变", COLLAB.workflows[0].members[0].role === "tester",
         COLLAB.workflows[0].members[0].role);
    roleSel = document.getElementById("collab-inspector").querySelector("[data-insp-role]");
    roleSel.value = "implementer";
    roleSel.dispatchEvent(new Event("change", { bubbles: true }));
    await __s(200);

    /* 3a. ★P4：成员检查器有「阶段」；换阶段能存下来 */
    var mStageSel = document.querySelector("#collab-inspector [data-insp-stage]");
    step("★成员检查器有「阶段」下拉", !!mStageSel && mStageSel.value === "s1",
         mStageSel && mStageSel.value);
    if (mStageSel) {
      mStageSel.value = "s2";
      mStageSel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    await __s(900);
    var m1Now = (COLLAB.workflows[0].members || []).find(function (m) { return m.id === "m1"; });
    step("★成员换阶段 → 成员 stage 跟着变", !!m1Now && m1Now.stage === "s2",
         m1Now && m1Now.stage);
    var m1Back = __node("m1");
    if (m1Back) m1Back.click();
    await __s(200);
    var mStageSel2 = document.querySelector("#collab-inspector [data-insp-stage]");
    if (mStageSel2) {
      mStageSel2.value = "s1";
      mStageSel2.dispatchEvent(new Event("change", { bubbles: true }));
    }
    await __s(400);

    /* 3a-2. ★阶段检查器（名字 / 条件 / 失败策略 / 重试） */
    var s1n = __node("s1");
    if (s1n) s1n.click();
    await __s(250);
    var sinsp = document.getElementById("collab-inspector");
    step("★点阶段节点 → 阶段检查器（名字/条件/失败/重试）",
         __vis(sinsp) && !!sinsp.querySelector("[data-insp-stage-name]")
         && !!sinsp.querySelector("[data-insp-stage-when]")
         && !!sinsp.querySelector("[data-insp-stage-onfail]")
         && !!sinsp.querySelector("[data-insp-stage-retries]"), __t(sinsp));
    window.__calls.length = 0;
    var ofSel = sinsp.querySelector("[data-insp-stage-onfail]");
    ofSel.value = "continue";
    ofSel.dispatchEvent(new Event("change", { bubbles: true }));
    await __s(150);
    var rtSel = document.querySelector("#collab-inspector [data-insp-stage-retries]");
    rtSel.value = "2";
    rtSel.dispatchEvent(new Event("change", { bubbles: true }));
    await __s(900);
    var ssaves = (window.__calls || []).filter(function (c) { return c[0] === "save"; });
    var sstage = ssaves.length ? ((ssaves[ssaves.length - 1][1] || {}).stages || [])[0] : null;
    step("★改阶段「失败继续 / 重试 2」→ 自动保存",
         !!sstage && sstage.onFail === "continue" && Number(sstage.retries) === 2,
         JSON.stringify(sstage));

    /* 3a-3. ★「＋ 阶段」/ Delete 删阶段 */
    var beforeStages = collabWfStages(COLLAB.workflows[0]).length;
    var beforeNodes = document.querySelectorAll("#collab-cv-nodes [data-node]").length;
    document.getElementById("collab-wf-add-stage").click();
    await __s(400);
    var afterStages = collabWfStages(COLLAB.workflows[0]).length;
    step("★「＋ 阶段」→ 阶段 +1、节点 +1 并选中",
         afterStages === beforeStages + 1
         && document.querySelectorAll("#collab-cv-nodes [data-node]").length === beforeNodes + 1
         && !!collabStageById(COLLAB.workflows[0], COLLAB.canvas.sel),
         "stages=" + afterStages + " sel=" + COLLAB.canvas.sel);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));
    await __s(400);
    step("★Delete 删掉选中的空阶段（阶段数恢复）",
         collabWfStages(COLLAB.workflows[0]).length === beforeStages
         && document.querySelectorAll("#collab-cv-nodes [data-node]").length === beforeNodes,
         "stages=" + collabWfStages(COLLAB.workflows[0]).length);

    /* 3b. ★检查器改权限 → 自动保存带 permLevel（别被拖节点/改名的自动保存抹掉） */
    window.__calls.length = 0;
    var mPermNode = __node("m1");
    if (mPermNode) mPermNode.click();
    await __s(250);
    var iperm = document.querySelector("#collab-inspector [data-insp-perm]");
    if (iperm) { iperm.value = "trust"; iperm.dispatchEvent(new Event("change", { bubbles: true })); }
    await __s(1000);                    // 保存防抖 600ms
    var pSaves = (window.__calls || []).filter(function (c) { return c[0] === "save"; });
    var pLast = pSaves.length ? ((pSaves[pSaves.length - 1][1] || {}).members || [])[0] : null;
    out.info.permMember = pLast;
    step("★检查器改权限 → 自动保存带上 permLevel（不被抹掉）",
         !!pLast && pLast.permLevel === "trust", JSON.stringify(pLast));

    /* 3c. ★从检查器跳到「最近一次运行」里这个成员的结果列 */
    var m1Link = __node("m1");
    if (m1Link) m1Link.click();
    await __s(250);
    var openBtn = document.querySelector("#collab-inspector [data-insp-open-run]");
    step("★成员检查器有「在运行结果里看」", !!openBtn);
    if (openBtn) openBtn.click();
    await __s(800);
    step("★点它 → 切到运行详情并选中这个成员",
         __vis(document.getElementById("collab-run"))
         && String(COLLAB.runId) === "run_1" && String(COLLAB.memSel) === "m1",
         "run=" + COLLAB.runId + " mem=" + COLLAB.memSel);
    var backRun = document.querySelector("[data-collab-back-wf]");
    if (backRun) backRun.click();
    await __s(450);

    /* 4. 拖动节点 → 保存布局 */
    window.__calls.length = 0;
    document.getElementById("collab-wf-layout").click();   // ★「排布」= 阶段在左、成员在右侧竖排
    await __s(600);
    var s1p = __node("s1"), m1p = __node("m1");
    var rS = s1p ? s1p.getBoundingClientRect() : null;
    var rM = m1p ? m1p.getBoundingClientRect() : null;
    step("★「排布」→ 阶段在左、成员在右侧竖排",
         !!rS && !!rM && rM.left > rS.right - 2,
         "stageRight=" + (rS && Math.round(rS.right)) + " memberLeft=" + (rM && Math.round(rM.left)));
    m1 = __node("m1");                 // ★上面的检查器改动会重绘画布，旧引用已脱离 DOM
    var r = m1.getBoundingClientRect();
    var sx = r.left + 30, sy = r.top + 20;
    m1.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, clientX: sx, clientY: sy, button: 0 }));
    window.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: sx + 70, clientY: sy + 45 }));
    window.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, clientX: sx + 70, clientY: sy + 45 }));
    await __s(1000);                    // 保存防抖 600ms
    var saves = (window.__calls || []).filter(function (c) { return c[0] === "save"; });
    var lastSave = saves[saves.length - 1] || [];
    var pos = lastSave[1] && lastSave[1].layout && lastSave[1].layout.nodes
      && lastSave[1].layout.nodes.m1;
    out.info.savePos = pos;
    step("★拖节点后坐标变了并自动保存", !!pos && pos.x > 380, JSON.stringify(pos));

    /* 5. 加成员 */
    var before = (COLLAB.workflows[0].members || []).length;
    var nodesBeforeAdd = document.querySelectorAll("#collab-cv-nodes [data-node]").length;
    document.getElementById("collab-wf-add").click();
    await __s(350);
    var added = (COLLAB.workflows[0].members || [])[(COLLAB.workflows[0].members || []).length - 1];
    step("★画布「＋ 成员」→ 成员 +1、节点 +1（挂到最后一个阶段）",
         (COLLAB.workflows[0].members || []).length === before + 1
         && document.querySelectorAll("#collab-cv-nodes [data-node]").length === nodesBeforeAdd + 1,
         "members=" + (COLLAB.workflows[0].members || []).length
         + " nodes=" + document.querySelectorAll("#collab-cv-nodes [data-node]").length);
    step("★新成员默认进最后一个阶段", !!added && added.stage === "s2",
         added && added.stage);

    /* 6. 快速任务 → 运行 */
    window.__calls.length = 0;
    document.getElementById("collab-quick-task").value = "补充一条任务";
    document.getElementById("collab-quick-run").click();
    await __s(900);
    var start = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    out.info.start = start[1];
    step("★快速任务 ▶ → POST /start 带 workflowId + 正文",
         start[1] && start[1].workflowId === "wf_1" && start[1].task === "补充一条任务",
         JSON.stringify(start[1]));
    step("★开跑后切到运行详情", __vis(document.getElementById("collab-run")),
         "run=" + __vis(document.getElementById("collab-run")));

    /* 7. 返回画布 */
    var back = document.querySelector("[data-collab-back-wf]");
    step("★运行详情有「← 工作流」", !!back);
    if (back) back.click();
    await __s(500);
    step("★点「← 工作流」回到画布", __vis(document.getElementById("collab-wf"))
         && !__vis(document.getElementById("collab-run")));

    /* 8. 左栏 ▶ 一键跑（用默认任务） */
    window.__calls.length = 0;
    var play = document.querySelector('#collab-wf-list [data-wf-run="wf_1"]');
    if (play) play.click();
    await __s(800);
    var start2 = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    step("★左栏工作流 ▶ 用默认任务开跑",
         start2[1] && start2[1].workflowId === "wf_1" && start2[1].task === "把登录拆开",
         JSON.stringify(start2[1]));
    await __s(300);
    var back2 = document.querySelector("[data-collab-back-wf]");
    if (back2) back2.click();
    await __s(400);

    /* 8b. ★运行中：画布上的阶段节点显示状态 */
    collabRefreshCanvasStatus();
    await __s(250);
    step("★运行中画布阶段状态：阶段 1 运行中 / 阶段 2 等待中",
         /运行中/.test(__t(__node("s1"))) && /等待中/.test(__t(__node("s2"))),
         "s1=" + __t(__node("s1")) + " s2=" + __t(__node("s2")));

    /* 9. 新建工作流 */
    window.__calls.length = 0;
    document.getElementById("collab-new-wf").click();
    await __s(700);
    var saves2 = (window.__calls || []).filter(function (c) { return c[0] === "save"; });
    step("★新建工作流 → workflows/save，列表 +1 并选中",
         saves2.length >= 1 && (COLLAB.workflows || []).length === 2
         && String(COLLAB.wfId) === String(window.WFS[0].id),
         "n=" + (COLLAB.workflows || []).length + " id=" + COLLAB.wfId);
    var newWf = window.WFS[0] || {};
    step("★新建工作流默认两阶段（实现 → 审查）",
         (newWf.stages || []).length === 2
         && (newWf.members || []).map(function (m) { return m.stage; }).join(",") === "s1,s2",
         JSON.stringify((newWf.stages || []).map(function (s) { return s.name; })));

    /* 10. ★P3：归档 / 取消归档（不删除） */
    window.__calls.length = 0;
    var curWf = String(COLLAB.wfId);
    document.getElementById("collab-wf-archive").click();
    await __s(700);
    var arcCall = (window.__calls || []).filter(function (c) { return c[0] === "archive"; })[0] || [];
    var curObj = (window.WFS || []).filter(function (w) { return String(w.id) === curWf; })[0] || {};
    step("★归档：POST workflows/archive + 变 archived",
         arcCall[1] && arcCall[1].archived === true && curObj.archived === true,
         JSON.stringify(arcCall[1] || {}));
    step("★归档后左栏出现「已归档」折叠区",
         !!document.querySelector("[data-wf-arch-toggle]")
         && /已归档 1 个/.test(__t(document.querySelector("[data-wf-arch-toggle]"))),
         __t(document.querySelector("[data-wf-arch-toggle]")));
    document.getElementById("collab-wf-archive").click();
    await __s(700);
    step("★取消归档 → 回到正常列表（折叠区消失）",
         curObj.archived === false && !document.querySelector("[data-wf-arch-toggle]"),
         "archived=" + curObj.archived);

    /* 11. ★Esc：先收检查器，工作台还开着 */
    var nEsc = __node("m1");
    if (nEsc) nEsc.click();
    await __s(250);
    var insp2 = document.getElementById("collab-inspector");
    var inspOpen = __vis(insp2);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(250);
    step("★Esc 先收检查器（工作台不关）",
         inspOpen && !__vis(document.getElementById("collab-inspector")) && collabIsOpen(),
         "openBefore=" + inspOpen);

    /* 12. ★Delete 删选中成员（输入框里不抢键） */
    var wfNow0 = (COLLAB.workflows || []).filter(function (w) { return String(w.id) === String(COLLAB.wfId); })[0] || {};
    var beforeDel = (wfNow0.members || []).length;
    var nDel = __node("m1");
    if (nDel) nDel.click();
    await __s(250);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Delete", bubbles: true }));
    await __s(350);
    var wfNow = (COLLAB.workflows || []).filter(function (w) { return String(w.id) === String(COLLAB.wfId); })[0] || {};
    step("★Delete 删掉选中的成员节点",
         (wfNow.members || []).length === beforeDel - 1,
         "before=" + beforeDel + " after=" + (wfNow.members || []).length);

    /* 13. ★Ctrl+Enter 开跑 */
    window.__calls.length = 0;
    document.getElementById("collab-quick-task").value = "键盘开跑";
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", ctrlKey: true, bubbles: true }));
    await __s(900);
    var ks = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    step("★Ctrl+Enter 开跑（带 workflowId + 正文）",
         ks[1] && ks[1].workflowId === curWf && ks[1].task === "键盘开跑",
         JSON.stringify(ks[1] || {}));

    /* 14. ★旧 run「存为工作流」（按成员去重，后端负责） */
    window.__calls.length = 0;
    document.getElementById("collab-runs-all").click();
    await __s(250);
    var orph = document.querySelector('#collab-runs [data-collab-run="run_2"]');
    if (orph) orph.click();
    await __s(800);
    var saveBtn = document.querySelector("[data-collab-save-wf]");
    step("★旧 run（没有 workflowId）有「存为工作流」",
         !!saveBtn && !document.querySelector("[data-collab-back-wf]"));
    var wfBefore = (COLLAB.workflows || []).length;
    if (saveBtn) saveBtn.click();
    await __s(1000);
    var fr = (window.__calls || []).filter(function (c) { return c[0] === "fromrun"; })[0] || [];
    step("★存为工作流：POST from-run + 切到画布并选中",
         fr[1] && fr[1].id === "run_2" && (COLLAB.workflows || []).length === wfBefore + 1
         && COLLAB.view === "wf" && String(COLLAB.wfId) === "wf_fromrun",
         JSON.stringify(fr[1] || {}) + " n=" + (COLLAB.workflows || []).length + " wf=" + COLLAB.wfId);

    /* 14b. ★旧 workflow（单阶段混角色）→「分阶段」按角色拆开 */
    var splitBtn = document.getElementById("collab-wf-split");
    step("★单阶段混角色时「分阶段」按钮高亮提示",
         !!splitBtn && splitBtn.classList.contains("attention"),
         splitBtn && splitBtn.className);
    window.__calls.length = 0;
    if (splitBtn) splitBtn.click();
    await __s(1000);
    var fwObj = (window.WFS || []).filter(function (w) { return w.id === "wf_fromrun"; })[0] || {};
    var byRole = {};
    (fwObj.members || []).forEach(function (m) { byRole[m.role] = m.stage; });
    step("★「分阶段」按角色拆成 实现(s1) → 审查(s2)",
         (fwObj.stages || []).length === 2
         && byRole.implementer === "s1" && byRole.reviewer === "s2"
         && !splitBtn.classList.contains("attention"),
         JSON.stringify(byRole) + " stages=" + (fwObj.stages || []).length);

    /* 15. ★画布首用提示 + 空状态三步引导 */
    var tip = document.getElementById("collab-cv-tip");
    step("★第一次进画布有首用提示", __vis(tip), "hidden=" + (tip && tip.hidden));
    var tipBtn = tip && tip.querySelector("[data-cv-tip-close]");
    if (tipBtn) tipBtn.click();
    await __s(250);
    step("★点「知道了」收起并记住（localStorage）",
         !!tip && tip.hidden && localStorage.getItem("cocraft.collab.canvasTip") === "1",
         "hidden=" + (tip && tip.hidden));
    var emptyEl = document.getElementById("collab-empty");
    step("★空状态三步引导（3 步 + 新建按钮）",
         !!emptyEl && emptyEl.querySelectorAll(".collab-steps li").length === 3
         && !!emptyEl.querySelector("[data-collab-empty-new]"), "");

    /* 15b. ★底部输入框（送出）→ 工作流开跑，而不是发聊天 */
    window.__calls.length = 0;
    var qi = document.getElementById("collab-quick-task");
    qi.value = "底部输入开跑";
    document.getElementById("composer").dispatchEvent(
      new Event("submit", { bubbles: true, cancelable: true }));
    await __s(1000);
    var fs = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    step("★底部输入框送出 → 用当前工作流开跑（不发聊天）",
         fs[1] && fs[1].task === "底部输入开跑" && fs[1].workflowId === "wf_fromrun",
         JSON.stringify(fs[1] || {}) + " left=" + JSON.stringify(qi.value));

    /* 16. ★关闭工作台 → 任务输入条搬回画布，聊天输入恢复 */
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(450);
    step("★关闭工作台：任务输入条搬回画布、聊天输入恢复",
         !document.querySelector("#composer .collab-quick")
         && !!document.querySelector("#collab-wf .collab-quick")
         && !document.documentElement.classList.contains("collab-open")
         && !document.getElementById("composer").classList.contains("collab-input"),
         "composerQuick=" + !!document.querySelector("#composer .collab-quick")
         + " wfQuick=" + !!document.querySelector("#collab-wf .collab-quick"));
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
  await __sh(1400);
  var lay = document.getElementById("collab-wf-layout");
  if (lay) lay.click();           // 截图用「排布」后的推荐布局（阶段在左、成员在右）
  await __sh(800);
})();
</script>
"""

SHOT_INSP_JS = SHOT_JS.replace(
    '  await __sh(1400);\n})();',
    '  await __sh(1400);\n'
    '  var n = document.querySelector(\'#collab-cv-nodes [data-node="m1"]\');\n'
    '  if (n) n.click();\n'
    '  await __sh(500);\n})();')


def build_probe(mode: str = "") -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    inject = PROBE_JS if not mode else (SHOT_INSP_JS if mode == "insp" else SHOT_JS)
    out = src.replace('<script src="/app.js', STUB + inject + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_wfcanvas_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probewfcanvasshot"),
             "--window-size=1500,980", "--virtual-time-budget=30000",
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
    dom = os.path.join(tmp, "probe_wfcanvas_dom.html")
    err = os.path.join(tmp, "probe_wfcanvas_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probewfcanvas"),
             "--window-size=1500,960",
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
    if "--shot" in sys.argv or "--shot-insp" in sys.argv:
        insp = "--shot-insp" in sys.argv
        flag = "--shot-insp" if insp else "--shot"
        i = sys.argv.index(flag)
        default_name = "workflow_canvas_inspector.png" if insp else "workflow_canvas.png"
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", default_name)
        build_probe("insp" if insp else "shot")
        try:
            take_shot(edge, os.path.abspath(shot))
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

    print("\n=== 工作流画布 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name
              + (("   [" + extra[:240] + "]") if extra else ""))
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
