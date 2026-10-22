/* 20-collab.js —— 本地协作工作台 + 工作流画布（P1 从 app.js 拆出，纯搬运不改逻辑） */

/* ---------------- ★整窗 + 底部输入区（小本子）当任务框 ---------------- */

let COLLAB_COMPOSER_DEF = null;
let COLLAB_QUICK_HOME = null;      // 任务输入条在原工作流视图里的位置（收工时搬回去）

/** 记住小本子的原始占位/按钮文案（第一次打开工作台时记一次）。 */
function collabComposerInit() {
  if (COLLAB_COMPOSER_DEF) return;
  const input = $("input");
  const btn = $("btn-send");
  const label = document.querySelector("#composer .composer-label");
  COLLAB_COMPOSER_DEF = {
    ph: input ? String(input.getAttribute("placeholder") || "") : "",
    send: btn ? String(btn.textContent || "送出") : "送出",
    sendTitle: btn ? String(btn.title || "") : "",
    label: label ? String(label.textContent || "") : "",
  };
}

/** 工作台开着时：整窗底部给 #composer 让位（设计像素 = 实际高度 / 界面缩放）。 */
function collabSyncFullscreen() {
  if (!collabIsOpen()) return;
  const c = $("composer");
  const z = (typeof uiZoom === "function" ? uiZoom() : 1) || 1;
  const h = c ? c.getBoundingClientRect().height : 0;
  document.documentElement.style.setProperty(
    "--collab-bottom", Math.max(0, Math.round(h / z)) + "px");
}

/** 小本子进入 / 退出「工作台任务框」模式：**把任务输入条整条搬进去**。 */
function collabComposerSync() {
  collabComposerInit();
  const on = collabIsOpen();
  document.documentElement.classList.toggle("collab-open", on);
  const comp = $("composer");
  const label = document.querySelector("#composer .composer-label");
  const quick = $("collab-quick");
  const wrap = document.querySelector("#composer .composer-wrap");
  if (!on) {
    if (comp) comp.classList.remove("collab-input");
    // 搬回工作流视图原来的位置（下次打开还能再搬）
    if (quick && COLLAB_QUICK_HOME && quick.parentNode !== COLLAB_QUICK_HOME.parent) {
      try {
        COLLAB_QUICK_HOME.parent.insertBefore(quick, COLLAB_QUICK_HOME.next);
      } catch { /* 原位置没了就留在原地，不崩 */ }
    }
    if (label && COLLAB_COMPOSER_DEF) label.textContent = COLLAB_COMPOSER_DEF.label;
    return;
  }
  // ★搬到小本子的位置：标签 + 任务输入 + ▶ 运行 就是底部这一行
  if (quick && wrap && quick.parentNode !== wrap) wrap.appendChild(quick);
  if (comp) comp.classList.add("collab-input");
  if (label) {
    const hasWf = !!collabWf();
    label.textContent = COLLAB.view === "wf" && hasWf ? "协作 · 这次要做什么"
      : (hasWf ? "协作 · 运行详情" : "协作 · 先新建一个工作流");
  }
  collabSyncFullscreen();
}

/** 关掉权限抽屉并清空（切回画布 / 离开工作台时用；否则宽抽屉会盖在画布上）。 */
function collabPermReset() {
  COLLAB.permOpen = false;
  const box = $("collab-pending");
  if (box) {
    box.classList.remove("open");
    box.hidden = true;
    box.innerHTML = "";
  }
  const bar = $("collab-pend-bar");
  if (bar) { bar.hidden = true; bar.innerHTML = ""; }
}

/* ---------------- 打开 / 关闭 ---------------- */

async function collabOpen(open) {
  const bs = $("body-stage");
  if (!bs) return;
  if (open) {
    try { closeMusicHome(); } catch { /* ignore */ }
    try { if (typeof CODE !== "undefined" && CODE.open) codeClose(); } catch { /* ignore */ }
  }
  COLLAB.open = !!open;
  bs.classList.toggle("collab-open", !!open);
  const btn = $("btn-collab");
  if (btn) btn.classList.toggle("on", !!open);
  if (open) {
    if (!COLLAB.cat && !COLLAB.catErr && !COLLAB.busy) collabLoadCatalog();
    if (!(COLLAB.workflows || []).length) await collabLoadWorkflows(true);
    else { collabRenderWfList(); collabRenderWf(); collabRenderMain(); }
    collabLoadRuns(!(COLLAB.workflows || []).length);
    collabStartPoll();
    collabStartRunsPoll();
    collabComposerSync();
    const q = $("collab-quick-task");
    if (q) setTimeout(() => q.focus(), 60);
  } else {
    collabStopPoll();
    collabStopRunsPoll();
    collabPermReset();
    collabComposerSync();
  }
}

function collabClose() { collabOpen(false); }

/** 左栏历史每 4s 轻刷一次：**同时跑多个任务**时，不选中的那些也能看到状态变化。 */
function collabStartRunsPoll() {
  collabStopRunsPoll();
  if (!COLLAB.open) return;
  COLLAB.runsPoll = POLL.interval("collab-runs", 4000, () => {
    if (COLLAB.open) collabLoadRuns(false);
  });
}
function collabStopRunsPoll() {
  if (COLLAB.runsPoll) { POLL.clear("collab-runs"); COLLAB.runsPoll = 0; }
}

function collabStartPoll() {
  collabStopPoll();
  if (!COLLAB.open || !COLLAB.runId) return;
  COLLAB.wasActive = collabRunActive(COLLAB.run);
  COLLAB.poll = POLL.interval("collab-tick", 1500, collabTick);
}

function collabStopPoll() {
  if (COLLAB.poll) { POLL.clear("collab-tick"); COLLAB.poll = 0; }
}

async function collabTick() {
  if (!COLLAB.open || !COLLAB.runId) return;
  await collabFetchRun();
  const active = collabRunActive(COLLAB.run);
  if (!active) {
    collabStopPoll();
    if (COLLAB.wasActive) { COLLAB.wasActive = false; collabLoadRuns(); }
  } else {
    COLLAB.wasActive = true;
  }
}

/* ---------------- 数据 ---------------- */

async function collabLoadCatalog() {
  COLLAB.busy = true;
  try {
    const r = await api("/orchestrate/catalog");
    COLLAB.cat = {
      agents: (r && r.agents) || [],
      roles: (r && r.roles) || {},
      presets: (r && r.presets) || [],
      askTimeout: Number((r && r.askTimeout) || 180) || 180,
    };
    COLLAB.catErr = "";
  } catch (e) {
    COLLAB.cat = null;
    const msg = String((e && e.message) || e);
    COLLAB.catErr = /^404/.test(msg)
      ? "后端没有 /orchestrate 接口（本地协作需要 0.2.1+ 的后端；请重启面板服务）"
      : ("拿不到 agent 清单：" + msg);
  } finally {
    COLLAB.busy = false;
  }
  collabRenderRun();
}

async function collabLoadRuns(autoOpen) {
  if (COLLAB.runsBusy) return;
  const seq = ++COLLAB.runsSeq;
  COLLAB.runsBusy = true;
  let list = null;
  let err = "";
  try {
    const r = await api("/orchestrate/runs");
    list = (r && r.runs) || [];
  } catch (e) {
    list = [];
    const msg = String((e && e.message) || e);
    err = /^404/.test(msg)
      ? "后端没有 /orchestrate 接口（需要重启面板服务加载新代码）"
      : ("拿不到任务列表：" + msg);
  } finally {
    COLLAB.runsBusy = false;
  }
  // ★期间删过任务 / 有更新的请求 → 这次结果作废（否则会把刚删掉的那条又画回来）
  if (seq !== COLLAB.runsSeq) return;
  COLLAB.runs = list;
  COLLAB.runsErr = err;
  collabRenderRuns();
  collabRenderWfList();
  collabRefreshCanvasStatus();
  if (!COLLAB.runId && autoOpen !== false && COLLAB.runs.length
      && !(COLLAB.workflows || []).length) {
    await collabOpenRun(COLLAB.runs[0].id);
    return;
  }
  if (!COLLAB.runId) { COLLAB.run = null; collabRenderMain(); }
}

/* ---------------- ★工作流（可复用配置 + 画布） ---------------- */

const CV_NODE = { w: 196, h: 92, taskW: 200, gapY: 108 };
// 默认排布：阶段在左、成员在**右侧竖排**（一行一个阶段）
const CV_STAGE = { w: 210, h: 78, gapX: 300, dx: 6, memberY: 140, gapY: 100,
                   memberX: 250, rowGap: 42 };
const COLLAB_TIP_KEY = "cocraft.collab.canvasTip";   // ★画布首用提示只弹一次

/** 阶段清单（老工作流没有 stages → 一个默认阶段，和以前一样是扇出）。 */
function collabWfStages(wf) {
  const list = (wf && Array.isArray(wf.stages) ? wf.stages : [])
    .filter((s) => s && s.id);
  if (list.length) return list;
  return [{ id: "s1", name: "全部成员", when: "always", onFail: "stop", retries: 0 }];
}
function collabStageById(wf, id) {
  return collabWfStages(wf).find((s) => String(s.id) === String(id)) || null;
}
function collabStageWhenText(v) {
  return v === "onSuccess" ? "仅上一阶段成功"
    : v === "onFailure" ? "仅上一阶段失败" : "总是执行";
}
function collabStageStageText(s) {
  const bits = [collabStageWhenText(s && s.when)];
  bits.push((s && s.onFail) === "continue" ? "失败继续" : "失败即停");
  if (Number(s && s.retries)) bits.push("重试 " + Number(s.retries));
  return bits.join(" · ");
}

/** 这个工作流是不是「旧的单阶段 + 审查者和别人混在一起」？（按钮会高亮提示） */
function collabSplitNeeded(wf) {
  const stages = collabWfStages(wf);
  if (stages.length !== 1) return false;
  const ms = (wf && wf.members) || [];
  const rev = ms.filter((m) => String(m.role || "") === "reviewer").length;
  return rev > 0 && rev < ms.length;
}

/** ★按角色拆成两个阶段：实现/测试/调研（s1）→ 审查（s2），并重置画布排布。 */
function collabSplitStagesByRole() {
  const wf = collabWf();
  if (!wf) return;
  const ms = wf.members || [];
  if (ms.length < 2) { setBanner("至少两个成员才需要拆阶段"); return; }
  const revs = ms.filter((m) => String(m.role || "") === "reviewer");
  const others = ms.filter((m) => String(m.role || "") !== "reviewer");
  if (!revs.length) {
    setBanner("没有「审查者」角色的成员 —— 先在成员检查器里把角色改成审查者");
    return;
  }
  if (!others.length) { setBanner("全是审查者，没有可放在前一阶段的成员"); return; }
  const okv = !window.confirm
    || window.confirm("按角色拆成两个阶段？\n\n"
      + `阶段 1（实现 / 测试 / 调研）：${others.map((m) => m.name || m.id).join("、")}\n`
      + `阶段 2（审查）：${revs.map((m) => m.name || m.id).join("、")}\n\n`
      + "会重置画布排布（成员坐标）。");
  if (!okv) return;
  wf.stages = [
    { id: "s1", name: "实现", when: "always", onFail: "stop", retries: 0 },
    { id: "s2", name: "审查", when: "always", onFail: "stop", retries: 0 },
  ];
  others.forEach((m) => { m.stage = "s1"; });
  revs.forEach((m) => { m.stage = "s2"; });
  wf.layout = { zoom: 1, nodes: {} };
  COLLAB.canvas.sel = "";
  collabSaveWfSoon();
  collabRenderCanvas();
  collabRenderInspector();
  collabRenderWfList();
  collabRenderWf();
  setBanner(`已分阶段：${others.length} 人先做 → ${revs.length} 人审查`, { ms: 6000 });
}

function collabWfById(id) {
  return (COLLAB.workflows || []).find((w) => String(w.id) === String(id)) || null;
}
function collabWf() { return collabWfById(COLLAB.wfId); }

/** 这个工作流**最近一次**运行（从运行历史里找；拿它显示节点状态 / 上次结果）。 */
function collabWfLastRun(wf) {
  if (!wf) return null;
  return (COLLAB.runs || []).find((r) => String(r.workflowId || "") === String(wf.id)) || null;
}

/** 当前打开的那条 run 若属于这个工作流 → 用它做**实时**节点状态。 */
function collabWfRun(wf) {
  if (!wf) return null;
  if (COLLAB.run && String(COLLAB.run.workflowId || "") === String(wf.id)) return COLLAB.run;
  return null;
}

/** 画布布局：任务 → 阶段（横排）→ 阶段内成员（竖排）；缺失的节点自动给位置，清理已删除的。 */
function collabWfLayout(wf) {
  const L = (wf && wf.layout) || {};
  const nodes = Object.assign({}, L.nodes || {});
  const ms = (wf && wf.members) || [];
  const stages = collabWfStages(wf);
  const ids = new Set(["task"]);
  stages.forEach((s) => ids.add(String(s.id)));
  ms.forEach((m) => ids.add(String(m.id)));
  Object.keys(nodes).forEach((k) => { if (!ids.has(k)) delete nodes[k]; });
  // ★默认排布：**阶段在左、成员在右侧竖排**（一行一个阶段）。
  //   已有坐标一律保留；缺阶段的按它成员的现有位置放到**左侧**（老布局不推倒重来）。
  let cursor = 30;
  stages.forEach((s) => {
    const sid = String(s.id);
    const mem = ms.filter((m) => String(m.stage || stages[0].id) === sid);
    if (!nodes[sid]) {
      const ps = mem.map((m) => nodes[String(m.id)]).filter(Boolean);
      if (ps.length) {
        const minX = Math.min.apply(null, ps.map((p) => p.x));
        const minY = Math.min.apply(null, ps.map((p) => p.y));
        nodes[sid] = { x: Math.max(8, minX - (CV_STAGE.w + 46)), y: minY };
      } else {
        nodes[sid] = { x: 340, y: cursor };
      }
    }
    const sp = nodes[sid];
    const rowH = Math.max(CV_STAGE.h + 46, mem.length * CV_NODE.gapY + 24);
    mem.forEach((m, j) => {
      const id = String(m.id);
      if (!nodes[id]) {
        nodes[id] = { x: sp.x + CV_STAGE.memberX, y: sp.y + 24 + j * CV_NODE.gapY };
      }
    });
    cursor = Math.max(cursor, sp.y) + rowH + CV_STAGE.rowGap;
  });
  if (!nodes.task) {
    const s1 = nodes[String(stages[0].id)] || { x: 340, y: 30 };
    nodes.task = { x: 40, y: s1.y };
  }
  // 脏数据兜底：没归到任何阶段的成员放到第一个阶段右侧
  ms.forEach((m, j) => {
    const id = String(m.id);
    if (!nodes[id]) {
      const sp = nodes[String(stages[0].id)] || { x: 340, y: 30 };
      nodes[id] = { x: sp.x + CV_STAGE.memberX, y: sp.y + 24 + j * CV_NODE.gapY };
    }
  });
  let zoom = Number(L.zoom) || 1;
  zoom = Math.max(0.6, Math.min(1.6, zoom));
  return { zoom: zoom, nodes: nodes };
}

async function collabLoadWorkflows(autoSelect) {
  const seq = ++COLLAB.wfSeq;
  try {
    const r = await api("/orchestrate/workflows");
    if (seq !== COLLAB.wfSeq) return (COLLAB.workflows || []).length > 0;
    COLLAB.workflows = (r && r.workflows) || [];
    COLLAB.wfErr = "";
  } catch (e) {
    if (seq !== COLLAB.wfSeq) return false;
    COLLAB.workflows = [];
    const msg = String((e && e.message) || e);
    COLLAB.wfErr = /^404/.test(msg)
      ? "当前版本没有工作流接口（重启面板服务）" : ("拿不到工作流：" + msg);
  }
  if (COLLAB.wfId && !collabWfById(COLLAB.wfId)) { COLLAB.wfId = ""; COLLAB.runWfFilter = ""; }
  if (autoSelect && !COLLAB.wfId && (COLLAB.workflows || []).length) {
    // ★归档的默认不选中（除非整个列表都归档了 —— 那就还选第一条，别让界面空着）
    const first = (COLLAB.workflows || []).find((w) => !w.archived)
      || (COLLAB.workflows || [])[0];
    COLLAB.wfId = String(first.id);
    COLLAB.runWfFilter = String(first.id);
    COLLAB.view = "wf";
    const lay = collabWfLayout(first);
    COLLAB.canvas.zoom = lay.zoom;
    COLLAB.canvas.pan = { x: 20, y: 14 };
  }
  collabRenderWfList();
  collabRenderWf();
  collabRenderMain();
  collabRenderRuns();
  if (autoSelect) setTimeout(collabFitCanvas, 120);
  return (COLLAB.workflows || []).length > 0;
}

function collabSelectWf(id, opts) {
  const wf = collabWfById(id);
  if (!wf) return;
  collabPermReset();
  COLLAB.wfId = String(wf.id);
  COLLAB.runWfFilter = String(wf.id);
  COLLAB.view = "wf";
  COLLAB.canvas.sel = "";
  COLLAB.canvas.pan = { x: 20, y: 14 };
  COLLAB.canvas.zoom = collabWfLayout(wf).zoom;
  const qi = $("collab-quick-task");
  if (qi) qi.value = wf.taskTemplate || "";
  collabRenderWfList();
  collabRenderRuns();
  collabRenderWf();
  collabRenderMain();
  setTimeout(collabFitCanvas, 100);
}

/** 左栏一行工作流（带 ▶ 一键跑）。 */
function collabWfRowHtml(w) {
  const n = (w.members || []).length;
  const last = collabWfLastRun(w);
  const st = last ? (COLLAB_ST[String(last.status || "")] || String(last.status || "")) : "未跑过";
  return `<div class="collab-wf-row${String(w.id) === String(COLLAB.wfId) ? " on" : ""}"`
    + ` data-wf="${esc(w.id)}" title="${esc((w.name || "") + (w.cwd ? "\n" + w.cwd : ""))}">`
    + `<span class="collab-dot ${last ? esc(String(last.status || "")) : ""}"></span>`
    + `<span class="collab-wf-t"><b>${esc(w.name || "未命名工作流")}</b>`
    + `<i>${esc(n + " 成员 · 跑过 " + (w.runCount || 0) + " 次 · " + st)}</i></span>`
    + `<button type="button" class="wrun" data-wf-run="${esc(w.id)}" title="用默认任务一键跑">▶</button>`
    + `</div>`;
}

/** 按项目（cwd）分组渲染一组工作流。 */
function collabWfGroupsHtml(list) {
  const groups = [];
  const by = {};
  list.forEach((w) => {
    const dir = String(w.cwd || "").trim();
    const key = dir ? dir.replace(/\\/g, "/").toLowerCase() : "__none__";
    if (!by[key]) {
      by[key] = { key: key, name: dir ? (projectNameOf(dir) || dir) : "未指定项目",
                  dir: dir, items: [] };
      groups.push(by[key]);
    }
    by[key].items.push(w);
  });
  return groups.map((g) => `<div class="collab-wf-group" title="${esc(g.dir)}">${esc(g.name)}</div>`
    + g.items.map(collabWfRowHtml).join("")).join("");
}

function collabRenderWfList() {
  const box = $("collab-wf-list");
  const cnt = $("collab-wf-count");
  if (!box) return;
  const list = COLLAB.workflows || [];
  const archived = list.filter((w) => !!w.archived);
  if (cnt) {
    cnt.textContent = list.length
      ? (list.length + " 个" + (archived.length ? "（归档 " + archived.length + "）" : ""))
      : "";
  }
  if (COLLAB.wfErr) {
    box.innerHTML = `<div class="collab-empty"><span>${esc(COLLAB.wfErr)}</span></div>`;
    return;
  }
  if (!list.length) {
    box.innerHTML = `<div class="collab-empty"><span>还没有工作流 —— 点左栏「工作流 ＋」建一个（成员 / 目录 / 默认任务都可复用）。</span></div>`;
    return;
  }
  // ★归档的收进折叠区（P3）：默认收起，点一下展开；**不删除**，随时能点回来
  let html = collabWfGroupsHtml(list.filter((w) => !w.archived));
  if (archived.length) {
    html += `<button type="button" class="collab-wf-arch${COLLAB.wfArchOpen ? " open" : ""}"`
      + ` data-wf-arch-toggle="1" title="归档只是收起来：成员 / 布局 / 运行历史都还在">`
      + `已归档 ${archived.length} 个 ${COLLAB.wfArchOpen ? "▾" : "▸"}</button>`;
    if (COLLAB.wfArchOpen) {
      html += `<div class="collab-wf-archbox">${collabWfGroupsHtml(archived)}</div>`;
    }
  }
  box.innerHTML = html;
}

function collabRenderWf() {
  const box = $("collab-wf");
  if (!box) return;
  const wf = collabWf();
  if (!wf || COLLAB.view !== "wf") { box.hidden = true; return; }
  box.hidden = false;
  const name = $("collab-wf-name");
  if (name) name.textContent = wf.name || "未命名工作流";
  const cnt = (wf.members || []).length;
  const meta = $("collab-wf-meta");
  if (meta) {
    const last = collabWfLastRun(wf);
    const bits = [cnt + " 成员", collabWfStages(wf).length + " 阶段",
                  "跑过 " + (wf.runCount || 0) + " 次"];
    if (wf.cwd) bits.push(wf.cwd);
    if (last) bits.push("上次 " + (COLLAB_ST[String(last.status || "")] || last.status));
    meta.textContent = bits.join(" · ");
    meta.title = wf.cwd || "";
  }
  const input = $("collab-quick-task");
  if (input && !input.value) input.value = wf.taskTemplate || "";
  const note = $("collab-quick-note");
  if (note) {
    note.textContent = COLLAB.catErr ? COLLAB.catErr
      : (!collabAvailAgents().length ? "没有可用的 ACP agent（先去设置里加一个）" : "");
  }
  const arc = $("collab-wf-archive");
  if (arc) {
    arc.textContent = wf.archived ? "取消归档" : "归档";
    arc.title = wf.archived ? "放回左栏工作流列表"
      : "归档：收进左栏「已归档」，不删除（成员 / 布局 / 运行历史都保留）";
    arc.classList.toggle("on", !!wf.archived);
  }
  const splitBtn = $("collab-wf-split");
  if (splitBtn) {
    const need = collabSplitNeeded(wf);
    splitBtn.classList.toggle("attention", need);
    splitBtn.title = need
      ? "这个工作流还是单阶段：审查者会和实现者并行 —— 点一下按角色拆成 实现 → 审查"
      : "按角色拆成阶段：实现 / 测试 → 审查";
    // 只在**选中这个工作流后**提示一次，别每次重绘都弹
    if (need && !COLLAB.splitHinted[String(wf.id)]) {
      COLLAB.splitHinted[String(wf.id)] = 1;
      setBanner("这个工作流还是单阶段：审查者会和实现者并行 —— 点顶部「分阶段」按角色拆开", { ms: 9000 });
    }
  }
  collabSyncTip();
  collabRenderCanvas();
  collabRenderInspector();
}

/** ★画布首用提示（P3）：只在第一次进画布时显示；点「知道了」后记 localStorage。 */
function collabTipSeen() {
  try { return localStorage.getItem(COLLAB_TIP_KEY) === "1"; } catch { return false; }
}
function collabSyncTip() {
  const tip = $("collab-cv-tip");
  if (!tip) return;
  tip.hidden = collabTipSeen();
}
function collabCloseTip() {
  const tip = $("collab-cv-tip");
  if (tip) tip.hidden = true;
  try { localStorage.setItem(COLLAB_TIP_KEY, "1"); } catch { /* 隐私模式 */ }
}

/** 主区三选一：工作流 / 运行 / 空态。 */
function collabRenderMain() {
  const empty = $("collab-empty");
  const runBox = $("collab-run");
  const wfBox = $("collab-wf");
  if (!empty || !runBox || !wfBox) return;
  if (collabIsOpen()) collabComposerSync();      // 底部任务框的文案跟着视图变
  if (COLLAB.view === "run" && COLLAB.run) {
    empty.hidden = true; wfBox.hidden = true; runBox.hidden = false;
    return;
  }
  if (COLLAB.view === "wf" && collabWf()) {
    empty.hidden = true; runBox.hidden = true; wfBox.hidden = false;
    return;
  }
  // 空态被「打不开这条任务」覆盖过 → 还原成三步引导（按钮是委托的，还原后仍能点）
  if (COLLAB.emptyHtml && empty.dataset.kind === "err") {
    empty.innerHTML = COLLAB.emptyHtml;
    delete empty.dataset.kind;
  }
  empty.hidden = false; wfBox.hidden = true; runBox.hidden = true;
}

async function collabSaveWfNow() {
  const wf = collabWf();
  if (!wf) return;
  const payload = {
    id: wf.id,
    name: wf.name || "未命名工作流",
    cwd: wf.cwd || "",
    taskTemplate: wf.taskTemplate || "",
    stages: collabWfStages(wf).map((s) => ({
      id: s.id, name: s.name || "", when: s.when || "always",
      onFail: s.onFail || "stop", retries: Number(s.retries) || 0,
    })),
    members: (wf.members || []).map((m, i) => ({
      id: m.id || ("m" + (i + 1)), role: m.role || "",
      name: m.name || "", engine: m.engine || "",
      // ★别漏 permLevel：拖节点 / 改名字都会走自动保存，漏了就会把节点权限抹回「跟随面板」
      permLevel: m.permLevel || "",
      // ★阶段（P4）：阶段之间串行、阶段内并行
      stage: m.stage || "s1",
    })),
    layout: { zoom: COLLAB.canvas.zoom, nodes: COLLAB.canvas.nodes },
  };
  try {
    const r = await api("/orchestrate/workflows/save",
      { method: "POST", body: JSON.stringify(payload) });
    const saved = (r && r.workflow) || null;
    if (!saved || !saved.id) return;
    const i = (COLLAB.workflows || []).findIndex((w) => String(w.id) === String(wf.id));
    if (i >= 0) COLLAB.workflows[i] = saved; else COLLAB.workflows.unshift(saved);
    COLLAB.wfId = String(saved.id);
    COLLAB.runWfFilter = String(saved.id);
    collabRenderWfList();
  } catch (e) {
    setBanner("保存工作流失败：" + ((e && e.message) || e), { sticky: true });
  }
}

function collabSaveWfSoon() {
  const c = COLLAB.canvas;
  if (c.saveTimer) clearTimeout(c.saveTimer);
  c.saveTimer = setTimeout(() => { c.saveTimer = 0; collabSaveWfNow(); }, 600);
}

async function collabNewWorkflow() {
  const agents = collabAvailAgents();
  if (!agents.length) { setBanner("没有可用的 ACP agent —— 先去设置里加一个"); return; }
  const eng = String(agents[0].id);
  const eng2 = String((agents[1] && agents[1].id) || eng);
  const wf = {
    name: "新工作流", cwd: (typeof currentDir === "function" ? currentDir() : "") || "",
    taskTemplate: "",
    // ★默认就是「阶段 1 实现 → 阶段 2 审查」：能直接跑，也能改成并行
    stages: [
      { id: "s1", name: "实现", when: "always", onFail: "stop", retries: 0 },
      { id: "s2", name: "审查", when: "always", onFail: "stop", retries: 0 },
    ],
    members: [
      { id: "m1", role: "implementer", name: "impl", engine: eng, stage: "s1" },
      { id: "m2", role: "reviewer", name: "rev", engine: eng2, stage: "s2" },
    ],
    layout: { zoom: 1, nodes: {} },
  };
  try {
    const r = await api("/orchestrate/workflows/save",
      { method: "POST", body: JSON.stringify(wf) });
    const saved = (r && r.workflow) || wf;
    COLLAB.workflows.unshift(saved);
    collabSelectWf(saved.id);
    setBanner("已新建工作流 —— 在画布上改成员，写好任务点「▶ 运行」", { ms: 6000 });
  } catch (e) {
    setBanner("新建工作流失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabDuplicateWf() {
  const wf = collabWf();
  if (!wf) return;
  try {
    const r = await api("/orchestrate/workflows/duplicate",
      { method: "POST", body: JSON.stringify({ id: wf.id }) });
    const saved = (r && r.workflow) || null;
    if (saved && saved.id) {
      await collabLoadWorkflows(false);
      collabSelectWf(saved.id);
      setBanner("已复制工作流：" + (saved.name || ""));
    }
  } catch (e) {
    setBanner("复制失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabDeleteWf() {
  const wf = collabWf();
  if (!wf) return;
  if (!confirm(`删除工作流「${wf.name}」？\n历史运行记录会保留（只是不再归属它）。`)) return;
  try {
    await api("/orchestrate/workflows/delete",
      { method: "POST", body: JSON.stringify({ id: wf.id }) });
    COLLAB.workflows = (COLLAB.workflows || []).filter((w) => String(w.id) !== String(wf.id));
    COLLAB.wfId = "";
    COLLAB.runWfFilter = "";
    COLLAB.view = "wf";
    collabRenderWfList();
    collabRenderWf();
    collabRenderMain();
    collabRenderRuns();
    setBanner("已删除工作流（运行记录保留）");
  } catch (e) {
    setBanner("删除失败：" + ((e && e.message) || e), { sticky: true });
  }
}

/** ★归档 / 取消归档（P3）：不删除任何东西，只收进左栏折叠区。 */
async function collabArchiveWf() {
  const wf = collabWf();
  if (!wf) return;
  const to = !wf.archived;
  try {
    const r = await api("/orchestrate/workflows/archive", {
      method: "POST", body: JSON.stringify({ id: wf.id, archived: to }),
    });
    const saved = (r && r.workflow) || null;
    if (saved && saved.id) {
      const i = (COLLAB.workflows || []).findIndex((w) => String(w.id) === String(saved.id));
      if (i >= 0) COLLAB.workflows[i] = saved; else COLLAB.workflows.unshift(saved);
    } else {
      wf.archived = to;
    }
    if (to) COLLAB.wfArchOpen = true;            // 刚归档 → 展开，让人看得见它去哪了
    collabRenderWfList();
    collabRenderWf();
    collabRenderRuns();
    setBanner(to ? "已归档（收进左栏「已归档」，不删除）" : "已取消归档");
  } catch (e) {
    setBanner("归档失败：" + ((e && e.message) || e), { sticky: true });
  }
}

/** ★旧 run「另存为工作流」（P3）：按成员组合去重；存完直接切到画布。 */
async function collabSaveRunAsWf() {
  const run = COLLAB.run;
  if (!run || !run.id) { setBanner("先打开一条运行记录"); return; }
  try {
    const r = await api("/orchestrate/workflows/from-run", {
      method: "POST", body: JSON.stringify({ id: run.id }),
    });
    const wf = (r && r.workflow) || null;
    if (!wf || !wf.id) throw new Error("后端没有返回工作流");
    await collabLoadWorkflows(false);
    await collabLoadRuns(false);
    if (r && r.existing) setBanner(`这套成员组合已经有工作流「${wf.name}」—— 直接用它`, { ms: 6000 });
    else setBanner(`已存为工作流「${wf.name}」—— 之后不用再选成员`, { ms: 6000 });
    collabSelectWf(wf.id);
  } catch (e) {
    setBanner("另存为工作流失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabPickWfCwd() {
  const wf = collabWf();
  if (!wf) return;
  try {
    const r = await api("/pick/folder", { method: "POST" });
    if (r && r.ok && r.path) {
      wf.cwd = r.path;
      collabSaveWfSoon();
      collabRenderWf();
      collabRenderWfList();
      setBanner("工作流目录：" + r.path);
    }
  } catch (e) {
    setBanner("选目录失败：" + ((e && e.message) || e), { sticky: true });
  }
}

function collabAddMember() {
  const wf = collabWf();
  if (!wf) return;
  if ((wf.members || []).length >= 8) { setBanner("最多 8 个成员"); return; }
  const agents = collabAgents();
  const eng = String((agents.find((a) => a && a.available) || {}).id || "");
  if (!eng) { setBanner("没有可用的 ACP agent"); return; }
  let n = 1;
  (wf.members || []).forEach((m) => {
    const num = Number(String(m.id || "").replace(/^m/, ""));
    if (num >= n) n = num + 1;
  });
  const id = "m" + n;
  const stages = collabWfStages(wf);
  const selStage = collabStageById(wf, COLLAB.canvas.sel);
  const stageId = String((selStage && selStage.id) || stages[stages.length - 1].id);
  wf.members = wf.members || [];
  wf.members.push({ id: id, role: "implementer", name: "m" + n, engine: eng, stage: stageId });
  const lay = collabWfLayout(wf);
  wf.layout = { zoom: COLLAB.canvas.zoom, nodes: lay.nodes };
  COLLAB.canvas.nodes = lay.nodes;
  COLLAB.canvas.sel = id;
  collabSaveWfSoon();
  collabRenderCanvas();
  collabRenderInspector();
  collabRenderWfList();
}

function collabDeleteMember(id) {
  const wf = collabWf();
  if (!wf) return;
  if ((wf.members || []).length <= 1) { setBanner("至少要留一个成员"); return; }
  wf.members = (wf.members || []).filter((m) => String(m.id) !== String(id));
  if (wf.layout && wf.layout.nodes) delete wf.layout.nodes[id];
  if (COLLAB.canvas.sel === String(id)) COLLAB.canvas.sel = "";
  collabSaveWfSoon();
  collabRenderCanvas();
  collabRenderInspector();
  collabRenderWfList();
}

/* ---- ★阶段（P4）：加 / 删 ---- */

function collabAddStage() {
  const wf = collabWf();
  if (!wf) return;
  const stages = collabWfStages(wf);
  if (stages.length >= 8) { setBanner("最多 8 个阶段"); return; }
  let n = 1;
  stages.forEach((s) => {
    const num = Number(String(s.id || "").replace(/^s/, ""));
    if (num >= n) n = num + 1;
  });
  const id = "s" + n;
  wf.stages = stages.concat([{
    id: id, name: "阶段 " + (stages.length + 1), when: "always", onFail: "stop", retries: 0,
  }]);
  wf.layout = { zoom: COLLAB.canvas.zoom, nodes: COLLAB.canvas.nodes };
  COLLAB.canvas.sel = id;
  collabSaveWfSoon();
  collabRenderCanvas();
  collabRenderInspector();
  collabRenderWfList();
  collabRenderWf();
  setBanner(`已加「阶段 ${stages.length + 1}」—— 在成员检查器里把成员选到这个阶段`);
}

function collabDeleteStage(id) {
  const wf = collabWf();
  if (!wf) return;
  const stages = collabWfStages(wf);
  if (stages.length <= 1) { setBanner("至少要留一个阶段"); return; }
  const idx = stages.findIndex((s) => String(s.id) === String(id));
  if (idx < 0) return;
  const target = stages[idx > 0 ? idx - 1 : idx + 1];
  (wf.members || []).forEach((m) => {
    if (String(m.stage || stages[0].id) === String(id)) {
      m.stage = String(target.id);
      if (wf.layout && wf.layout.nodes) delete wf.layout.nodes[m.id];
      delete COLLAB.canvas.nodes[m.id];
    }
  });
  wf.stages = stages.filter((s) => String(s.id) !== String(id));
  if (wf.layout && wf.layout.nodes) delete wf.layout.nodes[id];
  delete COLLAB.canvas.nodes[id];
  if (COLLAB.canvas.sel === String(id)) COLLAB.canvas.sel = "";
  collabSaveWfSoon();
  collabRenderCanvas();
  collabRenderInspector();
  collabRenderWfList();
  collabRenderWf();
  setBanner(`已删除阶段，成员移到「${target.name || target.id}」`);
}

/* ---- 画布渲染 ---- */

function collabNodePos(lay, id, dx, dy) {
  const p = (lay.nodes || {})[id] || { x: dx, y: dy };
  return p;
}

function collabRefreshCanvasStatus() {
  if (COLLAB.view !== "wf") return;
  const wf = collabWf();
  if (!wf) return;
  // ⚠ 只有「当前打开发完整运行」才有成员/阶段明细；左栏历史是**摘要**（members 是数字）
  const run = collabWfRun(wf);
  const stages = collabWfStages(wf);
  const stageById = {}, memberById = {};
  (run && Array.isArray(run.stages) ? run.stages : []).forEach((s) => {
    stageById[String(s.id)] = s;
  });
  (run && Array.isArray(run.members) ? run.members : []).forEach((m) => {
    memberById[String(m.id)] = m;
  });
  const stageIds = new Set(stages.map((s) => String(s.id)));
  const nodes = document.querySelectorAll("#collab-cv-nodes [data-node]");
  nodes.forEach((el) => {
    const id = String(el.dataset.node || "");
    if (id === "task") return;
    const st = stageIds.has(id)
      ? String((stageById[id] || {}).status || "")
      : String((memberById[id] || {}).status || "");
    let stat = el.querySelector(".cv-n-stat");
    if (!st) { if (stat && !stageIds.has(id)) stat.remove(); return; }
    if (!stat) {
      stat = document.createElement("span");
      el.appendChild(stat);
    }
    stat.className = "cv-n-stat " + st;
    stat.textContent = COLLAB_ST[st] || st;
  });
}

function collabRenderCanvas() {
  const wf = collabWf();
  const box = $("collab-cv-nodes");
  if (!wf || !box) return;
  if (PERF.marks["wf:first"] == null) PERF.mark("wf:first");   // ★P5：工作流首次渲染
  const lay = collabWfLayout(wf);
  COLLAB.canvas.nodes = lay.nodes;
  const inner = $("collab-canvas-inner");
  if (inner) {
    inner.style.transform = `translate(${COLLAB.canvas.pan.x}px, ${COLLAB.canvas.pan.y}px) scale(${COLLAB.canvas.zoom})`;
  }
  const run = collabWfRun(wf);
  const stages = collabWfStages(wf);
  const stageRun = {}, memberRun = {};
  (run && Array.isArray(run.stages) ? run.stages : []).forEach((s) => {
    stageRun[String(s.id)] = s;
  });
  (run && Array.isArray(run.members) ? run.members : []).forEach((m) => {
    memberRun[String(m.id)] = m;
  });
  const task = collabNodePos(lay, "task", 40, 40);
  const nodes = [`<div class="cv-node cv-task${COLLAB.canvas.sel === "task" ? " sel" : ""}"`
    + ` data-node="task" style="left:${task.x}px;top:${task.y}px">`
    + `<span class="cv-tag">任务</span><b>${esc(wf.name || "工作流")}</b>`
    + `<span class="cv-n-eng" title="${esc(wf.taskTemplate || "")}">${esc(wf.taskTemplate || "（运行时临时输入）")}</span>`
    + `</div>`];
  // ★阶段节点（P4）：横排；阶段之间串行，阶段内成员并行
  stages.forEach((s, i) => {
    const p = collabNodePos(lay, String(s.id), 340 + i * CV_STAGE.gapX, 30);
    const rs = stageRun[String(s.id)] || null;
    const st = rs ? String(rs.status || "") : "";
    const zh = st ? (COLLAB_ST[st] || st) : "";
    const n = (wf.members || []).filter((m) => String(m.stage || stages[0].id) === String(s.id)).length;
    nodes.push(`<div class="cv-node cv-stage${COLLAB.canvas.sel === String(s.id) ? " sel" : ""}"`
      + ` data-node="${esc(s.id)}" style="left:${p.x}px;top:${p.y}px">`
      + `<button type="button" class="cv-x" data-cv-del-stage="${esc(s.id)}"`
      + ` title="删除阶段（成员会移到相邻阶段）">×</button>`
      + `<span class="cv-tag">阶段 ${i + 1}</span>`
      + `<b>${esc(s.name || ("阶段 " + (i + 1)))}</b>`
      + `<span class="cv-n-eng" title="${esc(collabStageStageText(s))}">`
      + `${esc(n + " 成员 · " + collabStageStageText(s))}</span>`
      + (zh ? `<span class="cv-n-stat ${esc(st)}">${esc(zh)}</span>` : "")
      + `</div>`);
  });
  (wf.members || []).forEach((m, i) => {
    const stageId = String(m.stage || stages[0].id);
    const sp = collabNodePos(lay, stageId, 340, 30);
    const idx = (wf.members || []).filter((x) => String(x.stage || stages[0].id) === stageId)
      .findIndex((x) => String(x.id) === String(m.id));
    const p = collabNodePos(lay, String(m.id), sp.x + CV_STAGE.memberX,
      sp.y + 24 + Math.max(0, idx) * CV_NODE.gapY);
    const rm = memberRun[String(m.id)] || null;
    const st = rm ? String(rm.status || "") : "";
    const zh = st ? (COLLAB_ST[st] || st) : "";
    const stageName = (stages.find((s) => String(s.id) === stageId) || {}).name || "";
    nodes.push(`<div class="cv-node cv-member${COLLAB.canvas.sel === String(m.id) ? " sel" : ""}"`
      + ` data-node="${esc(m.id)}" style="left:${p.x}px;top:${p.y}px">`
      + `<button type="button" class="cv-x" data-cv-del="${esc(m.id)}" title="删掉这个成员">×</button>`
      + `<span class="cv-role">${esc(collabRoleLabel(m.role) || "成员")}</span>`
      + `<b>${esc(m.name || m.id)}</b>`
      + `<span class="cv-n-eng" title="阶段：${esc(stageName)}">${esc(m.engine || "（未选大脑）")}</span>`
      + (zh ? `<span class="cv-n-stat ${esc(st)}">${esc(zh)}</span>` : "")
      + `</div>`);
  });
  box.innerHTML = nodes.join("");
  collabPaintEdges();
}

/** 两张卡片之间的连线：**按相对方位选最近的边**（上/下/左/右），
 *  不再假设「成员一定在阶段正下方」—— 用户把节点拖到哪都能连得明白。 */
function cvEdgePath(a, aw, ah, b, bw, bh) {
  const acx = a.x + aw / 2, acy = a.y + ah / 2;
  const bcx = b.x + bw / 2, bcy = b.y + bh / 2;
  const dx = bcx - acx, dy = bcy - acy;
  let x1, y1, x2, y2, k;
  if (Math.abs(dx) >= Math.abs(dy)) {
    // 左右为主
    if (dx >= 0) { x1 = a.x + aw; y1 = acy; x2 = b.x; y2 = bcy; }
    else { x1 = a.x; y1 = acy; x2 = b.x + bw; y2 = bcy; }
    k = Math.max(28, Math.min(80, Math.abs(x2 - x1) / 2));
    return `M${x1} ${y1} C${x1 + (dx >= 0 ? k : -k)} ${y1}, `
      + `${x2 - (dx >= 0 ? k : -k)} ${y2}, ${x2} ${y2}`;
  }
  // 上下为主
  if (dy >= 0) { x1 = acx; y1 = a.y + ah; x2 = bcx; y2 = b.y; }
  else { x1 = acx; y1 = a.y; x2 = bcx; y2 = b.y + bh; }
  k = Math.max(28, Math.min(80, Math.abs(y2 - y1) / 2));
  return `M${x1} ${y1} C${x1} ${y1 + (dy >= 0 ? k : -k)}, `
    + `${x2} ${y2 - (dy >= 0 ? k : -k)}, ${x2} ${y2}`;
}

function collabPaintEdges() {
  const svg = $("collab-cv-edges");
  const wf = collabWf();
  if (!svg || !wf) return;
  const nodes = COLLAB.canvas.nodes || {};
  const stages = collabWfStages(wf);
  const parts = [];
  const box = (id, w, h) => {
    const p = nodes[id];
    return p ? { x: p.x, y: p.y } : null;
  };
  // 任务 → 阶段 1
  const task = box("task");
  const first = stages[0] && box(String(stages[0].id));
  if (task && first) {
    parts.push(`<path class="flow" d="${cvEdgePath(task, CV_NODE.taskW, CV_NODE.h,
      first, CV_STAGE.w, CV_STAGE.h)}"/>`);
  }
  stages.forEach((s, i) => {
    const aId = String(s.id);
    const a = box(aId);
    if (!a) return;
    // 阶段 i → 阶段 i+1（串行主链）
    const nx = stages[i + 1] && box(String(stages[i + 1].id));
    if (nx) {
      parts.push(`<path class="flow" d="${cvEdgePath(a, CV_STAGE.w, CV_STAGE.h,
        nx, CV_STAGE.w, CV_STAGE.h)}"/>`);
    }
    // 阶段 → 它的成员（扇出）：各自按方位连最近边
    const mem = (wf.members || []).filter((m) => String(m.stage || stages[0].id) === aId);
    mem.forEach((m) => {
      const p = box(String(m.id));
      if (!p) return;
      parts.push(`<path class="fan" d="${cvEdgePath(a, CV_STAGE.w, CV_STAGE.h,
        p, CV_NODE.w, CV_NODE.h)}"/>`);
    });
  });
  svg.innerHTML = parts.join("");
}

function collabFitCanvas() {
  const cv = $("collab-canvas");
  const inner = $("collab-canvas-inner");
  if (!cv || !inner) return;
  const vw = cv.clientWidth || 0, vh = cv.clientHeight || 0;
  if (vw < 80 || vh < 80) {
    // 刚切到工作流视图时画布可能还没布局（clientWidth=0）→ 稍后再试，别用 0 去算
    COLLAB.canvas.fitTries = (COLLAB.canvas.fitTries || 0) + 1;
    if (COLLAB.canvas.fitTries < 12) setTimeout(collabFitCanvas, 150);
    return;
  }
  COLLAB.canvas.fitTries = 0;
  const nodes = COLLAB.canvas.nodes || {};
  const keys = Object.keys(nodes);
  if (!keys.length) return;
  const wfFit = collabWf();
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  keys.forEach((k) => {
    const p = nodes[k];
    const isStage = !!collabStageById(wfFit, k);
    const w = k === "task" ? CV_NODE.taskW : (isStage ? CV_STAGE.w : CV_NODE.w);
    const h = isStage ? CV_STAGE.h : CV_NODE.h;
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + w); maxY = Math.max(maxY, p.y + h);
  });
  const pad = 36;
  const z = Math.max(0.6, Math.min(1.4, Math.min(
    (vw - pad * 2) / Math.max(1, maxX - minX),
    (vh - pad * 2) / Math.max(1, maxY - minY))));
  COLLAB.canvas.zoom = z;
  COLLAB.canvas.pan = {
    x: (vw - (maxX - minX) * z) / 2 - minX * z,
    y: (vh - (maxY - minY) * z) / 2 - minY * z,
  };
  inner.style.transform = `translate(${COLLAB.canvas.pan.x}px, ${COLLAB.canvas.pan.y}px) scale(${z})`;
}

function collabCanvasZoomAt(factor, clientX, clientY) {
  const cv = $("collab-canvas");
  if (!cv) return;
  const rect = cv.getBoundingClientRect();
  const px = clientX - rect.left, py = clientY - rect.top;
  const z0 = COLLAB.canvas.zoom;
  const z1 = Math.max(0.6, Math.min(1.6, z0 * factor));
  if (Math.abs(z1 - z0) < 1e-4) return;
  const pan = COLLAB.canvas.pan;
  pan.x = px - (px - pan.x) * (z1 / z0);
  pan.y = py - (py - pan.y) * (z1 / z0);
  COLLAB.canvas.zoom = z1;
  const inner = $("collab-canvas-inner");
  if (inner) inner.style.transform = `translate(${pan.x}px, ${pan.y}px) scale(${z1})`;
  collabSaveWfSoon();
}

/* ---- 检查器（改角色 / 名字 / 大脑；任务节点改默认任务） ---- */

function collabRenderInspector() {
  const box = $("collab-inspector");
  if (!box) return;
  const wf = collabWf();
  const sel = COLLAB.canvas.sel;
  if (!wf || !sel) { box.hidden = true; box.innerHTML = ""; return; }
  const agents = collabAgents();
  const roles = (COLLAB.cat && COLLAB.cat.roles) || {};
  if (sel === "task") {
    box.hidden = false;
    box.innerHTML = `<b>工作流</b>`
      + `<label><span>名字</span><input data-insp-wfname maxlength="40" value="${esc(wf.name || "")}" autocomplete="off"></label>`
      + `<label><span>目录</span><input data-insp-cwd value="${esc(wf.cwd || "")}" autocomplete="off" spellcheck="false"></label>`
      + `<div class="insp-cap">默认任务</div>`
      + `<textarea data-insp-task rows="3" placeholder="默认任务（运行时可直接改）">${esc(wf.taskTemplate || "")}</textarea>`
      + `<div class="insp-msg">改完自动保存；点画布空白处收起。</div>`;
    return;
  }
  // ★阶段（P4）：名字 / 执行条件 / 失败策略 / 重试
  const stages = collabWfStages(wf);
  const stage = collabStageById(wf, sel);  if (stage) {
    const idx = stages.findIndex((s) => String(s.id) === String(stage.id));
    const n = (wf.members || []).filter((m) => String(m.stage || stages[0].id) === String(stage.id)).length;
    const whenOpts = [["always", "总是执行"], ["onSuccess", "仅上一阶段成功"],
                      ["onFailure", "仅上一阶段失败"]]
      .map(([v, t]) => `<option value="${v}"${String(stage.when || "always") === v ? " selected" : ""}>${t}</option>`).join("");
    const failOpts = [["stop", "失败即停"], ["continue", "失败继续"]]
      .map(([v, t]) => `<option value="${v}"${String(stage.onFail || "stop") === v ? " selected" : ""}>${t}</option>`).join("");
    const retryOpts = [0, 1, 2].map((v) => `<option value="${v}"${Number(stage.retries || 0) === v ? " selected" : ""}>${v} 次</option>`).join("");
    box.hidden = false;
    box.innerHTML = `<b>阶段 ${idx + 1}</b>`
      + `<label><span>名字</span><input data-insp-stage-name maxlength="24" value="${esc(stage.name || "")}" autocomplete="off"></label>`
      + `<label><span>条件</span><select data-insp-stage-when${idx === 0 ? " disabled" : ""} title="看**上一阶段**的结果">${whenOpts}</select></label>`
      + `<label><span>失败</span><select data-insp-stage-onfail>${failOpts}</select></label>`
      + `<label><span>重试</span><select data-insp-stage-retries>${retryOpts}</select></label>`
      + `<div class="insp-msg">${n} 个成员 · 阶段内并行、阶段之间串行。`
      + `${idx === 0 ? "第 1 阶段总是执行（条件不适用）。" : "失败策略决定后面的阶段跑不跑。"}</div>`;
    return;
  }
  const m = (wf.members || []).find((x) => String(x.id) === String(sel));
  if (!m) { box.hidden = true; box.innerHTML = ""; return; }
  const roleOpts = `<option value="">（无角色）</option>`
    + Object.keys(roles).map((k) => `<option value="${esc(k)}"${String(k) === String(m.role) ? " selected" : ""}>`
      + `${esc(roles[k].label || k)}</option>`).join("");
  const engOpts = agents.map((a) => `<option value="${esc(a.id)}"${String(a.id) === String(m.engine) ? " selected" : ""}`
    + `${a.available ? "" : " disabled"}>${esc(a.label || a.id)}${a.available ? "" : "（缺依赖）"}</option>`).join("");
  const levels = collabPermLevels();
  const permOpts = levels.map((x) => `<option value="${esc(x.id || "")}"${String(x.id || "") === String(m.permLevel || "") ? " selected" : ""}`
    + ` title="${esc(x.note || "")}">${esc(x.label || x.id || "跟随面板")}</option>`).join("");
  const stageOpts = stages.map((s, i) => `<option value="${esc(s.id)}"`
    + `${String(s.id) === String(m.stage || stages[0].id) ? " selected" : ""}>`
    + `阶段 ${i + 1} · ${esc(s.name || "")}</option>`).join("");
  const lastRun = collabWfLastRun(wf);
  box.hidden = false;
  box.innerHTML = `<b>成员</b>`
    + `<label><span>阶段</span><select data-insp-stage title="阶段之间串行：上一阶段跑完才轮到下一阶段">${stageOpts}</select></label>`
    + `<label><span>角色</span><select data-insp-role>${roleOpts}</select></label>`
    + `<label><span>名字</span><input data-insp-name maxlength="24" value="${esc(m.name || "")}" autocomplete="off"></label>`
    + `<label><span>大脑</span><select data-insp-engine>${engOpts}</select></label>`
    + `<label><span>权限</span><select data-insp-perm title="只对这一个成员生效">${permOpts}</select></label>`
    + (lastRun
      ? `<button type="button" class="insp-link" data-insp-open-run="${esc(m.id)}"`
        + ` title="打开这个工作流最近一次运行，并定位到这个成员">在运行结果里看 →</button>`
      : "")
    + `<div class="insp-msg">阶段之间串行、阶段内并行。「信任」= 非破坏性不再逐条问；破坏性仍要确认。</div>`;
}

function collabInspectorChanged(e) {
  const wf = collabWf();
  if (!wf) return;
  const t = e.target;
  const sel = COLLAB.canvas.sel;
  const m = (wf.members || []).find((x) => String(x.id) === String(sel));
  const stage = collabStageById(wf, sel);
  let hit = false;
  if (stage) {
    if (t.matches("[data-insp-stage-name]")) {
      stage.name = String(t.value || "").trim() || stage.name; hit = true;
    } else if (t.matches("[data-insp-stage-when]")) {
      stage.when = String(t.value || "always"); hit = true;
    } else if (t.matches("[data-insp-stage-onfail]")) {
      stage.onFail = (String(t.value) === "continue") ? "continue" : "stop"; hit = true;
    } else if (t.matches("[data-insp-stage-retries]")) {
      stage.retries = Math.max(0, Math.min(2, Number(t.value) || 0)); hit = true;
    }
    if (!hit) return;
    collabSaveWfSoon();
    collabRenderCanvas();
    collabRenderInspector();
    collabRenderWfList();
    if (t.matches("[data-insp-stage-name]")) collabRenderWf();
    return;
  }
  if (m && t.matches("[data-insp-stage]")) {
    m.stage = String(t.value || "s1");
    // 换阶段后位置要重新算：把这个成员的自定义坐标清掉（只清它一个）
    if (wf.layout && wf.layout.nodes) delete wf.layout.nodes[m.id];
    delete COLLAB.canvas.nodes[m.id];
    collabSaveWfSoon();
    collabRenderCanvas();
    collabRenderInspector();
    return;
  }
  if (m && t.matches("[data-insp-role]")) { m.role = t.value; hit = true; }
  else if (m && t.matches("[data-insp-name]")) { m.name = String(t.value || "").trim() || m.id; hit = true; }
  else if (m && t.matches("[data-insp-engine]")) { m.engine = t.value; hit = true; }
  else if (m && t.matches("[data-insp-perm]")) { m.permLevel = String(t.value || ""); hit = true; }
  else if (sel === "task" && t.matches("[data-insp-wfname]")) {
    wf.name = String(t.value || "").trim() || wf.name; hit = true;
  } else if (sel === "task" && t.matches("[data-insp-cwd]")) {
    wf.cwd = String(t.value || "").trim(); hit = true;
  } else if (sel === "task" && t.matches("[data-insp-task]")) {
    wf.taskTemplate = String(t.value || ""); hit = true;
  }
  if (!hit) return;
  collabSaveWfSoon();
  collabRenderCanvas();
  collabRenderWfList();
  if (sel === "task") collabRenderWf();
  else collabRenderInspector();
}

/* ---- 启动 ---- */

async function collabQuickRun(wfId) {
  const wf = collabWfById(wfId || COLLAB.wfId);
  if (!wf) { setBanner("先选一个工作流"); return; }
  if (!collabAvailAgents().length) { setBanner("没有可用的 ACP agent —— 先去设置里加一个"); return; }
  const input = $("collab-quick-task");
  const typed = String((input && input.value) || "").trim();
  const task = typed || String(wf.taskTemplate || "").trim();
  if (!task) {
    setBanner("先写一句任务（或给这个工作流设一个默认任务）");
    if (input) input.focus();
    return;
  }
  try {
    const r = await api("/orchestrate/start", {
      method: "POST",
      body: JSON.stringify({ task: task, workflowId: wf.id }),
    });
    const run = (r && r.run) || null;
    if (input && typed) input.value = "";
    await collabLoadRuns(false);
    await collabLoadWorkflows(false);
    if (run && run.id) await collabOpenRun(run.id);
    setBanner(`已用工作流「${wf.name}」开跑`);
  } catch (e) {
    setBanner("启动失败：" + ((e && e.message) || e), { sticky: true });
  }
}

/* ---- 画布交互：拖动节点 / 平移 / 缩放 / 双击 / 删除 ---- */

function collabCanvasPointerDown(e) {
  const cv = $("collab-canvas");
  if (!cv || e.button !== 0) return;
  if (e.target.closest(".cv-tip")) return;      // ★首用提示上的按钮：别当成平移/拖节点
  const nodeEl = e.target.closest("[data-node]");
  if (nodeEl && !e.target.closest("button,input,textarea,select")) {
    e.preventDefault();
    const id = nodeEl.dataset.node;
    const p = (COLLAB.canvas.nodes || {})[id];
    if (!p) return;
    const start = { x: e.clientX, y: e.clientY, ox: p.x, oy: p.y };
    // ★拖动阶段 = 连它下面的成员一起拖（否则连线会被拉成长条）
    const wfNow = collabWf();
    const isStage = !!collabStageById(wfNow, id);
    const kidStart = isStage
      ? (wfNow.members || []).filter((m) => String(m.stage || "s1") === String(id))
        .map((m) => {
          const kp = (COLLAB.canvas.nodes || {})[String(m.id)] || { x: 0, y: 0 };
          return { id: String(m.id), x: kp.x, y: kp.y };
        })
      : [];
    nodeEl.classList.add("dragging");
    let moved = false;
    const move = (ev) => {
      const z = COLLAB.canvas.zoom || 1;
      const dx = (ev.clientX - start.x) / z, dy = (ev.clientY - start.y) / z;
      if (Math.abs(dx) + Math.abs(dy) > 2) moved = true;
      p.x = start.ox + dx;
      p.y = start.oy + dy;
      nodeEl.style.left = p.x + "px";
      nodeEl.style.top = p.y + "px";
      kidStart.forEach((k) => {
        const kp = (COLLAB.canvas.nodes || {})[k.id];
        if (!kp) return;
        kp.x = k.x + dx;
        kp.y = k.y + dy;
        const kel = document.querySelector(`#collab-cv-nodes [data-node="${k.id}"]`);
        if (kel) {
          kel.style.left = kp.x + "px";
          kel.style.top = kp.y + "px";
        }
      });
      collabPaintEdges();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      nodeEl.classList.remove("dragging");
      if (moved) {
        COLLAB.canvas.justDragged = true;
        setTimeout(() => { COLLAB.canvas.justDragged = false; }, 0);
        collabSaveWfSoon();
      } else {
        COLLAB.canvas.sel = id;
        collabRenderCanvas();
        collabRenderInspector();
      }
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return;
  }
  // 空白处 = 平移画布；松手收起检查器
  e.preventDefault();
  const start = { x: e.clientX, y: e.clientY,
                  px: COLLAB.canvas.pan.x, py: COLLAB.canvas.pan.y };
  cv.classList.add("panning");
  const move = (ev) => {
    COLLAB.canvas.pan.x = start.px + (ev.clientX - start.x);
    COLLAB.canvas.pan.y = start.py + (ev.clientY - start.y);
    const inner = $("collab-canvas-inner");
    if (inner) {
      inner.style.transform = `translate(${COLLAB.canvas.pan.x}px, ${COLLAB.canvas.pan.y}px)`
        + ` scale(${COLLAB.canvas.zoom})`;
    }
  };
  const up = () => {
    window.removeEventListener("pointermove", move);
    window.removeEventListener("pointerup", up);
    cv.classList.remove("panning");
    COLLAB.canvas.sel = "";
    collabRenderInspector();
  };
  window.addEventListener("pointermove", move);
  window.addEventListener("pointerup", up);
}

function collabCanvasWheel(e) {
  if (e.ctrlKey) return;                 // Ctrl+滚轮 = 界面缩放（全局），别抢
  e.preventDefault();
  collabCanvasZoomAt(e.deltaY < 0 ? 1.1 : 0.9, e.clientX, e.clientY);
}

function collabCanvasDblClick(e) {
  if (e.target.closest(".cv-tip")) return;      // 首用提示里双击别当成「加成员」
  const nodeEl = e.target.closest("[data-node]");
  if (!nodeEl) { collabAddMember(); return; }
  COLLAB.canvas.sel = nodeEl.dataset.node;
  collabRenderCanvas();
  collabRenderInspector();
}

function collabCanvasClick(e) {
  if (e.target.closest("[data-cv-tip-close]")) {
    e.stopPropagation();
    collabCloseTip();
    return;
  }
  const del = e.target.closest("[data-cv-del]");
  if (del) {
    e.stopPropagation();
    collabDeleteMember(del.dataset.cvDel);
    return;
  }
  const delStage = e.target.closest("[data-cv-del-stage]");
  if (delStage) {
    e.stopPropagation();
    collabDeleteStage(delStage.dataset.cvDelStage);
    return;
  }
  if (COLLAB.canvas.justDragged) return;
  const nodeEl = e.target.closest("[data-node]");
  if (!nodeEl) return;
  COLLAB.canvas.sel = nodeEl.dataset.node;
  collabRenderCanvas();
  collabRenderInspector();
}

/** ★从画布检查器跳到「这个成员在最近一次运行里的结果列」。 */
async function collabOpenMemberResult(mid) {
  const wf = collabWf();
  if (!wf) return;
  const last = collabWfRun(wf) || collabWfLastRun(wf);
  if (!last || !last.id) { setBanner("这个工作流还没有运行记录"); return; }
  await collabOpenRun(last.id);
  COLLAB.memSel = String(mid || "");
  COLLAB.memAll = false;
  if (COLLAB.run) collabRenderCols(COLLAB.run);
}

async function collabOpenRun(id, keepPoll) {
  const rid = String(id || "");
  if (!rid) return;
  COLLAB.runId = rid;
  COLLAB.view = "run";
  COLLAB.runErr = "";
  COLLAB.pickId = "";
  COLLAB.diffOpen = false;
  COLLAB.memSel = "";
  COLLAB.memAll = false;
  COLLAB.permOpen = false;
  const diff = $("collab-diff");
  if (diff) { diff.hidden = true; diff.innerHTML = ""; }
  collabRenderRuns();
  await collabFetchRun();
  collabRenderMain();
  if (!keepPoll) collabStartPoll();
}

async function collabFetchRun() {
  if (!COLLAB.runId || COLLAB.tickBusy) return;
  COLLAB.tickBusy = true;
  const rid = COLLAB.runId;
  try {
    const r = await api("/orchestrate/run?id=" + encodeURIComponent(rid));
    if (COLLAB.runId !== rid) return;
    COLLAB.run = (r && r.run) || null;
    COLLAB.runErr = "";
  } catch (e) {
    if (COLLAB.runId !== rid) return;
    COLLAB.run = null;
    const msg = String((e && e.message) || e);
    COLLAB.runErr = /^404/.test(msg) ? "这条任务不存在（可能被清理了）" : ("拿不到任务详情：" + msg);
  } finally {
    COLLAB.tickBusy = false;
  }
  collabRenderRun();
  collabRefreshCanvasStatus();
}

/* ---------------- 渲染：历史 ---------------- */

/** 一条运行属于哪个项目：看它的工作目录（`cwd`）；没有就归到「未指定项目」。
 *  ⚠ 以前 `list_runs()` 不带 cwd，左栏只能看到任务名 —— 多条工作流混在一起，分不清各自属于哪个项目。 */
function collabProjectOf(run) {
  const dir = String((run && run.cwd) || "").trim();
  if (!dir) return { key: "__none__", name: "未指定项目", dir: "" };
  return { key: dir.toLowerCase(), name: projectNameOf(dir) || dir, dir: dir };
}

function collabRenderRuns() {
  const box = $("collab-runs");
  const cnt = $("collab-count");
  const fbox = $("collab-proj-filter");
  if (!box) return;
  const all = COLLAB.runs || [];
  const wfFilter = String(COLLAB.runWfFilter || "");
  const wfSel = wfFilter ? collabWfById(wfFilter) : null;
  const scope = wfFilter
    ? all.filter((r) => String(r.workflowId || "") === wfFilter)
    : all;
  const running = scope.filter((r) => String(r.status) === "running").length;

  // 项目清单（按出现顺序；同名不同路径按完整路径区分）
  const order = [];
  const byKey = {};  scope.forEach((r) => {
    const p = collabProjectOf(r);
    if (!byKey[p.key]) { byKey[p.key] = { key: p.key, name: p.name, dir: p.dir, n: 0 }; order.push(p.key); }
    byKey[p.key].n += 1;
  });
  // 选中的项目已经不在列表里了 → 回到"全部"
  if (COLLAB.projFilter && !byKey[COLLAB.projFilter]) COLLAB.projFilter = "";

  if (fbox) {
    fbox.innerHTML = order.length > 1
      ? `<button type="button" class="collab-proj-chip${COLLAB.projFilter ? "" : " on"}"`
        + ` data-collab-proj="">全部 <i>${scope.length}</i></button>`
        + order.map((k) => {
          const p = byKey[k];
          return `<button type="button" class="collab-proj-chip${COLLAB.projFilter === k ? " on" : ""}"`
            + ` data-collab-proj="${esc(p.key)}" title="${esc(p.dir || "未指定工作目录")}">`
            + `${esc(p.name)} <i>${p.n}</i></button>`;
        }).join("")
      : "";
  }

  const visible = COLLAB.projFilter
    ? scope.filter((r) => collabProjectOf(r).key === COLLAB.projFilter)
    : scope;

  if (cnt) {
    const bits = [];
    if (scope.length) bits.push(scope.length + " 条");
    if (wfSel) bits.push("工作流：" + (wfSel.name || ""));
    if (order.length > 1) bits.push(order.length + " 个项目");
    if (visible.length !== scope.length) bits.push("筛出 " + visible.length);
    if (running) bits.push(running + " 运行中");
    cnt.textContent = bits.join(" · ");
    cnt.classList.toggle("busy", running > 0);
  }
  if (COLLAB.runsErr) {
    box.innerHTML = `<div class="collab-empty"><span>${esc(COLLAB.runsErr)}</span></div>`;
    return;
  }
  if (!scope.length) {
    box.innerHTML = `<div class="collab-empty"><span>${wfSel ? "这个工作流还没有运行。" : "还没有任务。"}</span></div>`;
    return;
  }
  if (!visible.length) {
    box.innerHTML = `<div class="collab-empty"><span>这个项目还没有任务。</span></div>`;
    return;
  }
  box.innerHTML = visible.map((r) => {
    const st = String(r.status || "running");
    const done = Number(r.done || 0);
    const failed = Number(r.failed || 0);
    const n = Number(r.members || 0);
    const bits = [`${done}/${n} 完成`];
    if (failed) bits.push(failed + " 失败");
    if (st === "running") bits.push("运行中");
    const p = collabProjectOf(r);
    return `<div class="collab-run-row${String(r.id) === String(COLLAB.runId) ? " on" : ""}"`
      + ` data-collab-run="${esc(r.id)}" title="${esc((r.task || "") + (p.dir ? "\n项目：" + p.dir : ""))}">`
      + `<span class="collab-dot ${esc(st)}"></span>`
      + `<span class="collab-run-t"><b>${esc(r.title || r.task || r.id)}</b>`
      + `<span class="collab-run-tags">`
      + `<span class="collab-run-proj" title="${esc(p.dir || "未指定工作目录")}">`
      + `${treeIco(true, false)}<em>${esc(p.name)}</em></span>`
      + (r.workflowName
        ? `<span class="collab-run-wf" title="工作流：${esc(r.workflowName)}">${esc(r.workflowName)}</span>`
        : "")
      + `</span>`
      + `<i>${esc(fmtTime(r.created))} · ${esc(bits.join(" · "))}</i></span>`
      + `<button type="button" class="collab-run-del" data-collab-del="${esc(r.id)}" title="删除这条运行记录（已结束的才能删）">×</button>`
      + `</div>`;
  }).join("");
}

/* ---------------- 渲染：任务详情 ---------------- */

function collabRenderRun() {
  const empty = $("collab-empty");
  const runBox = $("collab-run");
  if (!empty || !runBox) return;
  const run = COLLAB.run;
  if (!run || COLLAB.view !== "run") {
    collabRenderMain();
    if (!run && COLLAB.view === "run") {
      let why = "从左栏选一条运行记录，或新建一个工作流再跑。";
      if (COLLAB.catErr) why = COLLAB.catErr;
      else if (COLLAB.runErr) why = COLLAB.runErr;
      else if (COLLAB.runsErr) why = COLLAB.runsErr;
      empty.dataset.kind = "err";
      empty.innerHTML = `<b>${COLLAB.catErr || !COLLAB.runId ? "本地协作" : "打不开这条任务"}</b>`
        + `<span>${esc(why)}</span>`;
    }
    return;
  }
  collabRenderMain();

  const st = String(run.status || "running");
  const title = $("collab-task-title");
  if (title) {
    title.textContent = String(run.title || run.task || run.id || "任务");
    title.title = String(run.task || "");
  }
  const stEl = $("collab-task-st");
  if (stEl) {
    stEl.className = "collab-st " + st;
    stEl.textContent = collabStText(st);
  }

  const ms = run.members || [];
  const done = ms.filter((m) => m.status === "done").length;
  const bad = ms.filter((m) => ["failed", "timeout", "cancelled"].includes(m.status)).length;
  const meta = $("collab-task-meta");
  if (meta) {
    const bits = [];
    if (run.workflowName) {
      bits.push(`工作流 <b class="collab-proj-name">${esc(run.workflowName)}</b>`);
    }
    // ★阶段进度（P4）：几段跑完了 / 当前在哪一段
    const rstages = run.stages || [];
    if (rstages.length) {
      const fin = rstages.filter((s) => ["done", "partial", "failed", "skipped",
        "cancelled"].includes(String(s.status || ""))).length;
      const cur = rstages.find((s) => s.status === "running")
        || rstages.find((s) => s.status === "wait");
      bits.push(`阶段 ${fin}/${rstages.length}`
        + (cur ? ` · 当前「${esc(cur.name || "")}」` : ""));
    }
    const pj = collabProjectOf(run);
    if (run.cwd) {
      bits.push(`项目 <b class="collab-proj-name">${esc(pj.name)}</b>`
        + ` <code title="${esc(run.cwd)}">${esc(run.cwd)}</code>`
        + ` <button type="button" class="collab-proj-open" data-collab-open-proj="${esc(run.cwd)}"`
        + ` title="在编码模式里打开这个项目目录">打开</button>`);
    } else {
      bits.push(`项目 <b class="collab-proj-name muted">未指定</b>（用各成员的默认工作目录）`);
    }
    bits.push(`开始 ${esc(fmtTime(run.created))}`);
    bits.push(`成员 ${ms.length} · 完成 ${done}` + (bad ? ` · 失败/取消 ${bad}` : ""));
    if (run.updated) bits.push(`更新 ${esc(fmtTime(run.updated))}`);
    meta.innerHTML = bits.join(" · ") + `<div>任务：${esc(String(run.task || ""))}</div>`;
  }

  // 「推荐 X」：可点，跳到那一列（对不上就明说，不硬猜）
  const pickEl = $("collab-pick");
  if (pickEl) {
    const pick = run.arbitration && String(run.arbitration.pick || "").trim();
    if (pick) {
      pickEl.innerHTML = `<button type="button" class="collab-pick" data-collab-pick="${esc(pick)}" `
        + `title="跳到被推荐的那一列">推荐：<b>${esc(pick)}</b></button>`;
    } else if (run.arbitration && run.arbitration.status === "running") {
      pickEl.innerHTML = `<span class="collab-st running">审查中…</span>`;
    } else {
      pickEl.innerHTML = "";
    }
  }

  collabRenderActions(run);
  collabRenderPending(run);
  collabRenderCols(run);
}

function collabRenderActions(run) {
  const box = $("collab-task-actions");
  if (!box) return;
  const ms = run.members || [];
  const doneOut = ms.filter((m) => m.kind !== "arbitration" && m.status === "done" && String(m.output || "").trim());
  const st = String(run.status || "");
  const parts = [];
  if (run.workflowId) {
    parts.push(`<button type="button" data-collab-back-wf="${esc(run.workflowId)}"`
      + ` title="回到这个工作流的画布（可改成员/再跑）">← 工作流</button>`);
  } else {
    // ★旧 run（P3）：把当次成员组合存成可复用工作流（按成员去重，重复点不会造一堆）
    parts.push(`<button type="button" data-collab-save-wf="1"`
      + ` title="把这条运行的成员 / 目录 / 任务存成工作流，之后不用再选成员">存为工作流</button>`);
  }
  if (st === "running") {
    parts.push(`<button type="button" class="danger" data-collab-cancel="" title="中断所有成员（已产出的内容保留）">取消全部</button>`);
  } else {
    parts.push(`<button type="button" data-collab-rerun="1" title="用同样成员再跑一遍（原记录保留）">重跑</button>`);
  }
  if (doneOut.length === 2) {
    parts.push(`<button type="button" data-collab-diff="1">${COLLAB.diffOpen ? "收起差异" : "逐行差异"}</button>`);
  }
  const arbRunning = !!(run.arbitration && run.arbitration.status === "running");
  if (doneOut.length >= 2 && !arbRunning) {
    const avail = collabAvailAgents();
    if (avail.length) {
      const reviewer = ms.find((m) => m.kind !== "arbitration" && m.role === "reviewer" && m.engine);
      const saved = COLLAB.arbEngine || (reviewer && reviewer.engine) || (avail[0] && avail[0].id) || "";
      const opts = avail.map((a) => `<option value="${esc(a.id)}"${String(a.id) === String(saved) ? " selected" : ""}>${esc(a.label || a.id)}</option>`).join("");
      parts.push(`<span class="collab-hint">仲裁者</span>`
        + `<select id="collab-arb-engine" class="collab-arb-sel" title="让一个 agent 评审并给出「结论：推荐 X」">${opts}</select>`
        + `<button type="button" data-collab-arb="1" title="再跑一个 reviewer 成员：评审全部方案并收口推荐 X">请它仲裁</button>`);
    } else {
      parts.push(`<span class="collab-hint">没有可用的 agent，无法仲裁</span>`);
    }
  } else if (arbRunning) {
    parts.push(`<span class="collab-hint">审查者正在评……</span>`);
  }
  const pendN = (run.permissions || []).filter((p) => p.status === "pending").length;
  parts.push(`<button type="button" data-collab-perm-open="1" title="成员权限 + 待批申请（位置更宽）">`
    + `权限${pendN ? ` · ${pendN}` : ""}</button>`);
  parts.push(`<button type="button" data-collab-export="1" title="导出这次任务的时间线 + 审批记录（JSON，不含成员正文）">导出审计</button>`);
  parts.push(`<span class="collab-sp"></span><span class="collab-hint">本地运行 · 不经过云端</span>`);
  box.innerHTML = parts.join("");
}

function collabRenderPending(run) {
  const box = $("collab-pending");
  const bar = $("collab-pend-bar");
  if (!box) return;
  const all = run.permissions || [];
  const pend = all.filter((p) => p.status === "pending");
  const settled = all.filter((p) => p.status !== "pending");
  const ms = (run.members || []).filter((m) => m.kind !== "arbitration");

  // ---- 窄条：只说「有几条、是谁、去处理」 ----
  if (bar) {
    if (!pend.length) { bar.hidden = true; bar.innerHTML = ""; }
    else {
      const p0 = pend[0];
      bar.hidden = false;
      bar.innerHTML = `<span class="collab-pend-dot"></span>`
        + `<b>${pend.length} 条待你批准</b>`
        + `<span class="collab-pend-first">${esc(p0.memberName || collabFindMemberName(p0.member))}`
        + ` · ${esc(p0.title || p0.kind || "请求权限")}${p0.destructive ? " · 破坏性" : ""}</span>`
        + `<span class="collab-sp"></span>`
        + `<button type="button" class="primary" data-collab-perm-open="1">去处理…</button>`;
    }
  }

  // ---- 抽屉：成员权限 + 待批卡片 + 审批历史 ----
  if (!all.length && !ms.length) {
    box.hidden = true;
    box.innerHTML = "";
    COLLAB.permOpen = false;
    return;
  }
  box.hidden = false;
  const open = !!COLLAB.permOpen;
  box.classList.toggle("open", open);
  const ask = Number((COLLAB.cat && COLLAB.cat.askTimeout) || 180) || 180;
  const now = Date.now();
  const levels = collabPermLevels();
  const lvOpts = (cur) => levels.map((x) => `<option value="${esc(x.id || "")}"${String(x.id || "") === String(cur || "") ? " selected" : ""}>`
    + `${esc(x.label || x.id || "跟随面板")}</option>`).join("");
  let h = `<div class="collab-perm-drawer-h">`
    + `<b>权限与批准</b><span class="muted">${pend.length ? pend.length + " 条待批" : "没有待批"}</span>`
    + `<span class="collab-sp"></span><button type="button" class="wp-x" data-collab-perm-close="1" title="收起">×</button></div>`
    + `<div class="collab-perm-drawer-b">`;

  h += `<div class="collab-perm-sec"><div class="collab-perm-sec-h"><b>成员权限</b>`
    + `<span class="muted">只影响这个成员；改完立刻生效</span></div>`;
  h += ms.map((m) => `<div class="collab-perm-mrow">`
    + `<div class="collab-perm-mname"><b>${esc(m.name || m.id)}</b>`
    + `<span class="muted">${esc([m.roleLabel, m.engine].filter(Boolean).join(" · "))}</span></div>`
    + `<select data-perm-level="${esc(m.id)}" title="这个成员的权限等级">${lvOpts(m.permLevel)}</select>`
    + `</div>`).join("");
  h += `<div class="collab-perm-note">「信任」= 非破坏性操作不再逐条问；<b>破坏性操作永远要你确认</b>（先隔离再放行）。</div></div>`;

  h += `<div class="collab-perm-sec"><div class="collab-perm-sec-h"><b>待你批准</b>`
    + `<span class="muted">逐条处理 · 破坏性只给「确认执行」</span></div>`;
  if (!pend.length) {
    h += `<div class="collab-perm-none">现在没有待批的申请。</div>`;
  } else {
    h += pend.map((p) => {
      const created = Number(p.created || 0) / 1000;
      const left = created ? Math.max(0, Math.round(created + ask - now / 1000)) : ask;
      const cmd = String(p.command || "").trim();
      const paths = (p.locations || []).join("、");
      const danger = !!p.destructive;
      return `<div class="collab-perm-i${danger ? " danger" : ""}" data-perm-card="${esc(p.id)}">`
        + `<div class="collab-perm-h"><b>🔐 ${esc(p.memberName || collabFindMemberName(p.member))}</b>`
        + `<span>${esc(p.title || p.kind || "请求权限")}</span>`
        + (danger ? `<span class="collab-danger">破坏性操作</span>` : "")
        + `<span class="collab-left">剩余 ${left}s</span></div>`
        + (cmd ? `<div class="collab-perm-cmd">${esc(cmd)}</div>` : "")
        + (paths ? `<div class="collab-perm-paths">涉及：${esc(paths)}</div>` : "")
        + `<div class="collab-perm-ft">`
        + `<button type="button" class="ok" data-perm-go="${esc(p.id)}" data-decision="once" title="只放行这一次">${danger ? "确认执行" : "允许一次"}</button>`
        + (danger ? "" : `<button type="button" data-perm-go="${esc(p.id)}" data-decision="always" title="同样条件下以后不再问（写进 ACP 的「始终允许」记忆）">始终允许</button>`)
        + `<button type="button" class="danger" data-perm-go="${esc(p.id)}" data-decision="reject" title="拒绝这一条（agent 会收到拒绝继续）">拒绝</button>`
        + `</div></div>`;
    }).join("");
  }
  h += `</div>`;

  if (settled.length) {
    h += `<div class="collab-perm-sec"><details class="collab-audit"><summary>审批历史 · ${settled.length} 条（谁批准了哪一条、命令原文都在）</summary>`
      + settled.slice().reverse().map((p) => {
        const st = String(p.status || "");
        const label = st === "allowed" ? "已允许" : st === "denied" ? "已拒绝" : st === "timeout" ? "已超时" : st;
        const cmd = String(p.command || "").trim();
        return `<div class="collab-audit-row">`
          + `<i>${esc(label)}</i><b>${esc(p.memberName || p.member || "")}</b>`
          + `<span title="${esc(cmd || p.title || "")}">${esc(p.title || "")}${cmd ? " · " + esc(cmd) : ""}</span>`
          + `<em>${esc(COLLAB_DEC[p.decision] || "")} · 你（本机） · ${esc(fmtTime(p.decidedAt))}</em></div>`;
      }).join("") + `</details></div>`;
  }
  h += `</div>`;
  box.innerHTML = h;
}

function collabPermToggle(open) {
  COLLAB.permOpen = !!open;
  if (COLLAB.run) collabRenderPending(COLLAB.run);
  else {
    const box = $("collab-pending");
    if (box) box.classList.toggle("open", !!open);
  }
}

async function collabSetMemberPerm(mid, level) {
  const run = COLLAB.run;
  if (!run) return;
  const m = (run.members || []).find((x) => String(x.id) === String(mid));
  try {
    const r = await api("/orchestrate/member", {
      method: "POST",
      body: JSON.stringify({ run: run.id, member: mid, permLevel: level }),
    });
    if (r && r.run) COLLAB.run = r.run;
    collabRenderRun();
    setBanner(`已把「${(m && m.name) || mid}」的权限改成：${collabPermLabel(level)}`);
  } catch (e) {
    setBanner("改成员权限失败：" + ((e && e.message) || e), { sticky: true });
    collabRenderRun();
  }
}

function collabPermLabel(id) {
  const want = String(id || "");
  const hit = collabPermLevels().find((x) => String(x.id || "") === want);
  return hit ? String(hit.label || want) : (want || "跟随面板");
}

/** 一个成员的完整内容块（正文 / 错误 / 最近工具 / 底部用量）。 */
function collabColHtml(m) {
  const st = String(m.status || "wait");
  const out = String(m.output || "").trim();
  const err = String(m.error || "").trim();
  const acts = (m.activities || []).slice(-6);
  const running = st === "wait" || st === "running";
  const tok = m.tokens || {};
  const ctx = m.context || null;
  const stageName = ((COLLAB.run && (COLLAB.run.stages || []).find(
    (s) => String(s.id) === String(m.stage)) || {}).name) || "";
  const foot = [];
  if (tok.input != null || tok.output != null) {
    foot.push(`tokens ${tok.input != null ? fmtTok(tok.input) + " 入" : ""}${tok.output != null ? " / " + fmtTok(tok.output) + " 出" : ""}`);
  }
  if (ctx && ctx.size) foot.push(`上下文 ${fmtTok(ctx.used || 0)} / ${fmtTok(ctx.size)}`);
  if (m.started) foot.push("起 " + fmtTime(m.started));
  if (m.ended) foot.push("止 " + fmtTime(m.ended));
  const activity = acts.length
    ? `<div class="collab-acts">` + acts.map((a) => {
      const k = a.kind === "permission" ? "权限" : "工具";
      const ast = String(a.status || "");
      return `<div class="collab-act ${ast === "done" ? "done" : ast === "error" ? "error" : ""}" title="${esc(a.command || a.detail || "")}">`
        + `<b>${esc(k)}</b><span>${esc(a.name || a.detail || "")}</span>`
        + `<em>${ast === "done" ? "✓" : ast === "error" ? "失败" : ast === "waiting" ? "等待" : "…"}</em></div>`;
    }).join("") + `</div>`
    : "";
  return `<div class="collab-col${m.kind === "arbitration" ? " arb" : ""}" data-mid="${esc(m.id)}">`
    + `<div class="collab-col-head">`
    + (stageName ? `<i class="collab-stage-tag" title="阶段（阶段之间串行）">${esc(stageName)}</i>` : "")
    + (m.roleLabel ? `<i class="collab-role">${esc(m.roleLabel)}</i>` : "")
    + `<b>${esc(m.name || m.id)}</b><span class="collab-engine">${esc(m.engine || "")}</span>`
    + `<span class="collab-st ${esc(st)}">${esc(collabStText(st))}</span>`
    + `<span class="collab-perm-tag" title="这个成员的权限等级（在检查器 / 权限抽屉里改）">${esc(collabPermLabel(m.permLevel))}</span>`
    + (running ? `<button type="button" class="collab-cancel" data-collab-cancel="${esc(m.id)}" title="只取消这一个成员">取消</button>` : "")
    + `</div>`
    + `<div class="collab-col-body">`
    + (out ? renderText(stripModelWarning(out)) : `<div class="collab-col-wait">${running ? "还在做……" : "（没有返回正文）"}</div>`)
    + (err ? `<div class="collab-col-err">${esc(err)}</div>` : "")
    + activity
    + `</div>`
    + (foot.length ? `<div class="collab-col-foot">${foot.map(esc).join("<span>·</span>")}</div>` : "")
    + `</div>`;
}

function collabRenderCols(run) {
  const box = $("collab-cols");
  if (!box) return;
  const ms = run.members || [];
  if (!ms.length) {
    box.innerHTML = `<div class="collab-empty"><span>这个任务还没有成员。</span></div>`;
    if ($("collab-mem")) $("collab-mem").hidden = true;
    return;
  }
  // ★默认只看一个成员：选中的那个；没选过就挑正在跑的，否则第一个
  let sel = String(COLLAB.memSel || "");
  if (!ms.some((m) => String(m.id) === sel)) {
    const first = ms.find((m) => m.status === "running" || m.status === "wait") || ms[0];
    sel = String(first.id);
    COLLAB.memSel = sel;
  }
  const multi = ms.length > 1;
  const showAll = !!COLLAB.memAll;
  const stageMap = {};
  (run.stages || []).forEach((s) => { stageMap[String(s.id)] = s; });
  const strip = $("collab-mem");
  if (strip) {
    strip.hidden = !multi;
    if (multi) {
      strip.innerHTML = ms.map((m) => {
        const st = String(m.status || "wait");
        const on = showAll ? "" : (String(m.id) === sel ? " on" : "");
        const stName = (stageMap[String(m.stage)] || {}).name || "";
        return `<button type="button" class="collab-mem-chip${on}" data-collab-mem="${esc(m.id)}"`
          + ` title="${esc((stName ? "阶段：" + stName + " · " : "") + (m.roleLabel ? m.roleLabel + " · " : "") + (m.engine || "") + " · 权限：" + collabPermLabel(m.permLevel))}">`
          + `<i class="dot ${esc(st)}"></i>`
          + (stName ? `<i class="collab-mem-stage">${esc(stName)}</i>` : "")
          + (m.roleLabel ? `<em>${esc(m.roleLabel)}</em>` : "")
          + `<b>${esc(m.name || m.id)}</b>`
          + `<span>${esc(collabStText(st))}</span>`
          + `</button>`;
      }).join("")
        + `<button type="button" class="collab-mem-chip all${showAll ? " on" : ""}" data-collab-all="1"`
        + ` title="并排显示全部成员（对比用）">⊞ 并排</button>`;
    } else {
      strip.innerHTML = "";
    }
  }
  const single = multi && !showAll;
  box.classList.toggle("single", single);
  const list = single ? ms.filter((m) => String(m.id) === sel) : ms;
  box.innerHTML = list.map(collabColHtml).join("");
  // 点过「推荐 X」之后重画也要保住高亮
  if (COLLAB.pickId) {
    Array.prototype.forEach.call(box.querySelectorAll(".collab-col"), (c) => {
      c.classList.toggle("pick", String(c.dataset.mid) === String(COLLAB.pickId));
    });
  }
}

/* ---------------- 操作：取消 / 权限 / 仲裁 / 差异 / 重跑 ---------------- */

/** 删除一条**已结束**的运行记录（后端拒绝删除还在跑 / 还在收尾的任务）。 */
async function collabDeleteRun(id) {
  const rid = String(id || "");
  if (!rid) return;
  const row = (COLLAB.runs || []).find((r) => String(r.id) === rid);
  if (row && String(row.status) === "running") {
    setBanner("任务还在跑：先点「取消全部」，等它停下来再删除", { sticky: true });
    return;
  }
  if (!confirm("删除这条运行记录？\n成员输出、审批历史和这个任务的成员会话文件都会删掉，不能恢复。")) return;
  try {
    await api("/orchestrate/delete", { method: "POST", body: JSON.stringify({ id: rid }) });
    const wasCurrent = String(COLLAB.runId) === rid;
    // 作废任何正在飞的历史请求，再本地删掉（4s 轮询可能刚发出去）
    COLLAB.runsSeq += 1;
    COLLAB.runs = (COLLAB.runs || []).filter((r) => String(r.id) !== rid);
    if (wasCurrent) {
      COLLAB.runId = "";
      COLLAB.run = null;
      COLLAB.pickId = "";
      COLLAB.diffOpen = false;
      collabStopPoll();
      const diff = $("collab-diff");
      if (diff) { diff.hidden = true; diff.innerHTML = ""; }
    }
    collabRenderRuns();
    if (wasCurrent) {
      if (COLLAB.runs.length) await collabOpenRun(COLLAB.runs[0].id);
      else { collabStopPoll(); collabRenderRun(); }
    } else {
      collabRenderRun();
    }
    // 后台再对一次账（失败也不用打扰用户）
    collabLoadRuns(false);
    setBanner("已删除运行记录");
  } catch (e) {
    setBanner("删除失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabCancel(memberId) {  const run = COLLAB.run;
  if (!run) return;
  const who = memberId ? collabFindMemberName(memberId) : "全部成员";
  if (!confirm("取消 " + who + "？已经产出的内容会保留。")) return;
  try {
    const r = await api("/orchestrate/cancel", {
      method: "POST",
      body: JSON.stringify({ id: run.id, member: memberId || "" }),
    });
    if (r && r.run) COLLAB.run = r.run;
    collabRenderRun();
    setBanner("已取消：" + who);
    if (!collabRunActive(COLLAB.run)) { collabStopPoll(); collabLoadRuns(); }
  } catch (e) {
    setBanner("取消失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabPerm(permId, decision) {
  if (!permId) return;
  // 先禁用这张卡的按钮：避免连点发出两次（第二次会 404「已结算」，看着像失败）
  Array.prototype.forEach.call(document.querySelectorAll("[data-perm-card]"), (c) => {
    if (String(c.dataset.permCard) !== String(permId)) return;
    Array.prototype.forEach.call(c.querySelectorAll("button"), (b) => { b.disabled = true; });
  });
  try {
    await api("/orchestrate/permission", {
      method: "POST",
      body: JSON.stringify({ id: permId, decision: decision }),
    });
    setBanner(decision === "reject" ? "已拒绝这一条" : (decision === "always" ? "已允许（并记住这一条）" : "已允许这一次"));
    await collabFetchRun();
    collabStartPoll();
  } catch (e) {
    setBanner("批复失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabArbitrate() {
  const run = COLLAB.run;
  if (!run) return;
  const sel = $("collab-arb-engine");
  const engine = String((sel && sel.value) || COLLAB.arbEngine || "").trim();
  if (!engine) { setBanner("先选一个仲裁者（一个可用的 agent）"); return; }
  try {
    const r = await api("/orchestrate/arbitrate", {
      method: "POST",
      body: JSON.stringify({ id: run.id, engine: engine, name: "审查者" }),
    });
    if (r && r.run) COLLAB.run = r.run;
    collabRenderRun();
    collabStartPoll();
    setBanner("已请审查者仲裁（结果会出现在最右侧那一列；抠到「推荐 X」会高亮）");
  } catch (e) {
    setBanner("仲裁失败：" + ((e && e.message) || e), { sticky: true });
  }
}

function collabToggleDiff() {
  const box = $("collab-diff");
  if (!box) return;
  if (!box.hidden) {
    box.hidden = true; box.innerHTML = ""; COLLAB.diffOpen = false;
    collabRenderActions(COLLAB.run || {});
    return;
  }
  const done = ((COLLAB.run && COLLAB.run.members) || [])
    .filter((m) => m.kind !== "arbitration" && m.status === "done" && String(m.output || "").trim());
  if (done.length !== 2) { setBanner("需要正好 2 个成员完成才能看逐行差异"); return; }
  const lines = ccDiffLines(stripModelWarning(done[0].output), stripModelWarning(done[1].output));
  if (!lines) { setBanner("两边回复太长，算差异会卡 —— 直接并排看吧"); return; }
  const add = lines.filter((l) => l.startsWith("+")).length;
  const del = lines.filter((l) => l.startsWith("-")).length;
  box.innerHTML = `<div class="collab-diff-h">逐行差异：<b>${esc(done[0].name)}</b> → <b>${esc(done[1].name)}</b>`
    + `<span class="muted">（+${add} / −${del} 行 · 按原文比）</span></div>`
    + renderDiff(lines.join("\n"));
  box.hidden = false;
  COLLAB.diffOpen = true;
  collabRenderActions(COLLAB.run || {});
}

function collabJumpPick(pick) {
  const key = String(pick || "").toLowerCase().replace(/\s+/g, "");
  if (!key) return;
  const ms = ((COLLAB.run && COLLAB.run.members) || []);
  const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, "");
  let hit = ms.find((m) => norm(m.name) === key);
  if (!hit) hit = ms.find((m) => norm(m.name) && (norm(m.name).includes(key) || key.includes(norm(m.name))));
  if (!hit) hit = ms.find((m) => norm(m.output).includes(key));
  if (!hit) {
    setBanner(`结论里的「${pick}」对不上任何一列（成员：${ms.map((m) => m.name).join("、") || "无"}）`);
    return;
  }
  COLLAB.pickId = String(hit.id);
  COLLAB.memSel = String(hit.id);
  if (COLLAB.run) collabRenderCols(COLLAB.run);
  const cols = document.querySelectorAll("#collab-cols .collab-col");
  Array.prototype.forEach.call(cols, (c) => c.classList.toggle("pick", String(c.dataset.mid) === String(COLLAB.pickId)));
  const el = Array.prototype.find.call(cols, (c) => String(c.dataset.mid) === String(COLLAB.pickId));
  if (el) { try { el.scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" }); } catch { el.scrollIntoView(); } }
  setBanner(`已跳到「${hit.name}」那一列`);
}

/** 导出审计（JSON）：时间线 + 谁批准了哪条 + 命令原文；**不含成员正文**（正文可能很大/敏感）。 */
function collabExport() {
  const run = COLLAB.run;
  if (!run) { setBanner("先把任务打开再导出"); return; }
  const data = {
    exportedAt: new Date().toISOString(),
    run: { id: run.id, task: run.task, title: run.title, cwd: run.cwd,
           status: run.status, created: run.created, updated: run.updated },
    members: (run.members || []).map((m) => ({
      id: m.id, name: m.name, role: m.role, engine: m.engine, kind: m.kind,
      status: m.status, created: m.created, started: m.started, ended: m.ended,
      tokens: m.tokens || {}, context: m.context || null,
      error: m.error || "", activities: m.activities || [],
    })),
    permissions: run.permissions || [],
    arbitration: run.arbitration || null,
  };
  try {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "cocraft-audit-" + String(run.id || "run") + ".json";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { try { URL.revokeObjectURL(url); } catch { /* ignore */ } a.remove(); }, 0);
    setBanner("已导出审计（JSON）");
  } catch (e) {
    setBanner("导出失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function collabRerun() {  const run = COLLAB.run;
  if (!run) return;
  const members = (run.members || [])
    .filter((m) => m.kind !== "arbitration")
    .map((m) => ({ engine: m.engine, name: m.name, role: m.role }));
  if (!members.length) { setBanner("这条任务没有可重跑的成员"); return; }
  if (!confirm("用同样的成员再跑一遍？会新建一条任务记录（原来那条保留）。")) return;
  try {
    const r = await api("/orchestrate/start", {
      method: "POST",
      body: JSON.stringify({ task: run.task, cwd: run.cwd || "", members: members }),
    });
    const nr = r && r.run;
    if (!nr) throw new Error("后端没有返回新任务");
    await collabLoadRuns(false);
    await collabOpenRun(nr.id);
    setBanner("已重跑（新任务）");
  } catch (e) {
    setBanner("重跑失败：" + ((e && e.message) || e), { sticky: true });
  }
}

/* 一次性「新建任务」弹窗已移除：工作流是唯一入口
   （左栏「工作流 +」新建；画布快速任务条 = 一次性开跑，不保存流程）。 */
