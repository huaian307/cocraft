/* app.js —— 事件接线 + boot 启动（P1 拆文件收尾；**必须最后加载**）
 * boot 会依次调用各模块的 init*()，并设置 window.__appLoaded 供 index.html 兜底脚本判断。
 * 依赖：前面所有文件的全局函数 / 状态 / 常量（加载顺序见 index.html）。
 *
 * 本文件曾是一个 15k 行的单文件巨石；P1 已按模块拆到 js/（00-util / 01-chat / 02-shell /
 * 21-cocraft / 40-music / 12-asks / 11-engine / 41-visual / 30-code / 20-collab）。
 */

/* OpenCode UI —— 前端逻辑（零依赖，无构建）
 *
 * 数据流（全部经 server.py 代理，代理会自动附加 Basic 鉴权）：
 *   会话列表   GET  /api/session?limit=50&order=desc
 *   消息列表   GET  /api/session/{id}/message?limit=80     ← 返回「新→旧」，必须重排
 *   发消息     POST /api/session/{id}/prompt  { text, files? }
 *   中断       POST /api/session/{id}/interrupt
 *   新建       POST /api/session  {}
 *   实时事件   GET  /api/event  (SSE)
 *   QQ音乐     /qq/state /qq/control /qq/volume /qq/lyrics /qq/search /qq/app /qq/spectrum
 *   选择/权限  /api/session/{id}/form  /permission   （详见 AGENTS.md）
 *
 * ⚠ 消息结构有两种形态，混用会让「用户提问」整条消失：
 *   助手：{ type:"assistant", content:[{type:"text"|"reasoning"|"tool", ...}] }
 *   用户：{ type:"user", text:"...", files:[], metadata:{...} }  ← 顶层 text，没有 content
 *
 * ⚠ /api/session/{id}/message 只返回「已完成」的消息；生成中的内容只能靠 SSE 增量事件。
 */



/* ---------------- 事件接线 ---------------- */

/** 焦点在输入控件里？——Delete / 快捷键不能抢打字。 */
function collabTypingTarget(e) {
  const t = e && e.target;
  if (!t) return false;
  const tag = String(t.tagName || "").toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || !!t.isContentEditable;
}

function initCollab() {
  const btn = $("btn-collab");
  if (btn) btn.addEventListener("click", () => collabOpen(!collabIsOpen()));
  // ★整窗 / 底部任务框：记下任务输入条原来的位置，并监听祈祷书高度变化
  collabComposerInit();
  const quickHome = $("collab-quick");
  if (quickHome) {
    COLLAB_QUICK_HOME = { parent: quickHome.parentNode, next: quickHome.nextSibling };
  }
  try {
    const comp = $("composer");
    if (comp && typeof ResizeObserver !== "undefined") {
      new ResizeObserver(() => { if (collabIsOpen()) collabSyncFullscreen(); })
        .observe(comp);
    }
  } catch { /* ignore */ }
  const close = $("collab-close");
  if (close) close.addEventListener("click", collabClose);
  const refresh = $("collab-refresh");
  if (refresh) refresh.addEventListener("click", async () => {
    await collabLoadCatalog();
    await collabLoadWorkflows(false);
    await collabLoadRuns(!COLLAB.runId && !(COLLAB.workflows || []).length);
    if (COLLAB.runId) await collabFetchRun();
    collabStartPoll();
    setBanner("已刷新");
  });
  const nw = $("collab-new-wf");
  if (nw) nw.addEventListener("click", collabNewWorkflow);
  // ★空状态三步引导（P3）：按钮用**事件委托** —— 空态会被错误提示覆盖/还原，直接绑按钮会失效
  const emptyBox = $("collab-empty");
  if (emptyBox) {
    COLLAB.emptyHtml = emptyBox.innerHTML;
    emptyBox.addEventListener("click", (e) => {
      if (e.target.closest("[data-collab-empty-new]")) collabNewWorkflow();
    });
  }
  const runsAll = $("collab-runs-all");
  if (runsAll) runsAll.addEventListener("click", () => {
    COLLAB.runWfFilter = "";
    collabRenderRuns();
  });

  // ★左栏：工作流（点选中；▶ 用默认任务一键跑；「已归档」可折叠）
  const wfList = $("collab-wf-list");
  if (wfList) wfList.addEventListener("click", (e) => {
    if (e.target.closest("[data-wf-arch-toggle]")) {
      COLLAB.wfArchOpen = !COLLAB.wfArchOpen;
      collabRenderWfList();
      return;
    }
    const runBtn = e.target.closest("[data-wf-run]");
    if (runBtn) { e.stopPropagation(); collabQuickRun(runBtn.dataset.wfRun); return; }
    const row = e.target.closest("[data-wf]");
    if (row) collabSelectWf(row.dataset.wf);
  });

  // ★工作流头 / 快速运行
  const wfName = $("collab-wf-name");
  if (wfName) wfName.addEventListener("click", () => {
    const wf = collabWf();
    if (!wf) return;
    const v = prompt("工作流名字", wf.name || "");
    if (v == null) return;
    wf.name = String(v).trim() || wf.name;
    collabSaveWfSoon();
    collabRenderWf();
    collabRenderWfList();
  });
  const wfCwd = $("collab-wf-cwd");
  if (wfCwd) wfCwd.addEventListener("click", collabPickWfCwd);
  const wfAdd = $("collab-wf-add");
  if (wfAdd) wfAdd.addEventListener("click", collabAddMember);
  const wfAddStage = $("collab-wf-add-stage");
  if (wfAddStage) wfAddStage.addEventListener("click", collabAddStage);
  const wfSplit = $("collab-wf-split");
  if (wfSplit) wfSplit.addEventListener("click", collabSplitStagesByRole);
  const wfLayout = $("collab-wf-layout");
  if (wfLayout) wfLayout.addEventListener("click", () => {
    const wf = collabWf();
    if (!wf) return;
    wf.layout = { zoom: COLLAB.canvas.zoom, nodes: {} };
    COLLAB.canvas.sel = "";
    collabRenderCanvas();
    collabRenderInspector();
    collabFitCanvas();
    collabSaveWfSoon();
  });
  const wfDup = $("collab-wf-dup");
  if (wfDup) wfDup.addEventListener("click", collabDuplicateWf);
  const wfArc = $("collab-wf-archive");
  if (wfArc) wfArc.addEventListener("click", collabArchiveWf);
  const wfDel = $("collab-wf-del");
  if (wfDel) wfDel.addEventListener("click", collabDeleteWf);
  const quickRun = $("collab-quick-run");
  if (quickRun) quickRun.addEventListener("click", () => collabQuickRun());
  const quickInput = $("collab-quick-task");
  if (quickInput) quickInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      collabQuickRun();
    }
  });

  // ★画布：拖动 / 缩放 / 双击 / 删除
  const cv = $("collab-canvas");
  if (cv) {
    cv.addEventListener("pointerdown", collabCanvasPointerDown);
    cv.addEventListener("wheel", collabCanvasWheel, { passive: false });
    cv.addEventListener("dblclick", collabCanvasDblClick);
    cv.addEventListener("click", collabCanvasClick);
  }
  const insp = $("collab-inspector");
  if (insp) {
    insp.addEventListener("change", collabInspectorChanged);
    insp.addEventListener("click", (e) => {
      const openRun = e.target.closest("[data-insp-open-run]");
      if (openRun) collabOpenMemberResult(openRun.dataset.inspOpenRun);
    });
  }

  // 窗口尺寸变化 → 画布重新适应（节流）+ 整窗底部重新让位
  window.addEventListener("resize", () => {
    if (collabIsOpen()) collabSyncFullscreen();
    if (!COLLAB.open || COLLAB.view !== "wf") return;
    if (COLLAB.canvas.resizeTimer) clearTimeout(COLLAB.canvas.resizeTimer);
    COLLAB.canvas.resizeTimer = setTimeout(collabFitCanvas, 250);
  });

  // 左栏：按项目筛选（同一个项目可能有多组工作流）
  const pf = $("collab-proj-filter");
  if (pf) pf.addEventListener("click", (e) => {
    const chip = e.target.closest("[data-collab-proj]");
    if (!chip) return;
    COLLAB.projFilter = chip.dataset.collabProj || "";
    collabRenderRuns();
  });

  // 左栏：点一条历史 / 删一条历史
  const runsBox = $("collab-runs");
  if (runsBox) runsBox.addEventListener("click", (e) => {
    const del = e.target.closest("[data-collab-del]");
    if (del) {
      e.stopPropagation();
      collabDeleteRun(del.dataset.collabDel);
      return;
    }
    const row = e.target.closest("[data-collab-run]");
    if (row) collabOpenRun(row.dataset.collabRun);
  });

  // 右栏：动作 / 权限 / 取消
  const root = $("collab");
  if (root) root.addEventListener("click", (e) => {
    const openProj = e.target.closest("[data-collab-open-proj]");
    if (openProj) {
      const dir = openProj.dataset.collabOpenProj || "";
      if (dir) { collabClose(); codeOpenAt(dir, ""); }
      return;
    }
    const backWf = e.target.closest("[data-collab-back-wf]");
    if (backWf) { collabSelectWf(backWf.dataset.collabBackWf); return; }
    const saveWf = e.target.closest("[data-collab-save-wf]");
    if (saveWf) { collabSaveRunAsWf(); return; }
    const cancel = e.target.closest("[data-collab-cancel]");
    if (cancel) { collabCancel(cancel.dataset.collabCancel || ""); return; }
    const perm = e.target.closest("[data-perm-go]");
    if (perm) { collabPerm(perm.dataset.permGo, perm.dataset.decision); return; }
    if (e.target.closest("[data-collab-perm-open]")) { collabPermToggle(true); return; }
    if (e.target.closest("[data-collab-perm-close]")) { collabPermToggle(false); return; }
    const memChip = e.target.closest("[data-collab-mem]");
    if (memChip) {
      COLLAB.memSel = String(memChip.dataset.collabMem || "");
      COLLAB.memAll = false;
      if (COLLAB.run) collabRenderCols(COLLAB.run);
      return;
    }
    if (e.target.closest("[data-collab-all]")) {
      COLLAB.memAll = true;
      if (COLLAB.run) collabRenderCols(COLLAB.run);
      return;
    }
    const diff = e.target.closest("[data-collab-diff]");
    if (diff) { collabToggleDiff(); return; }
    const arb = e.target.closest("[data-collab-arb]");
    if (arb) { collabArbitrate(); return; }
    const pick = e.target.closest("[data-collab-pick]");
    if (pick) { collabJumpPick(pick.dataset.collabPick); return; }
    const exp = e.target.closest("[data-collab-export]");
    if (exp) { collabExport(); return; }
    const expRun = e.target.closest("[data-collab-run-export]");
    if (expRun) { collabExportRun(); return; }
    const rerun = e.target.closest("[data-collab-rerun]");
    if (rerun) { collabRerun(); return; }
  });
  if (root) root.addEventListener("change", (e) => {
    const sel = e.target.closest("#collab-arb-engine");
    if (sel) COLLAB.arbEngine = String(sel.value || "");
    const lv = e.target.closest("[data-perm-level]");
    if (lv) collabSetMemberPerm(lv.dataset.permLevel, lv.value);
  });


  // ★键盘（P3）：Esc 先收检查器 / 权限抽屉，再退工作台；Ctrl+Enter 开跑；Delete 删选中成员
  window.addEventListener("keydown", (e) => {
    if (!collabIsOpen()) return;
    if (e.key === "Escape") {
      if (COLLAB.permOpen) { collabPermToggle(false); return; }
      if (COLLAB.canvas.sel) {
        COLLAB.canvas.sel = "";
        collabRenderCanvas();
        collabRenderInspector();
        return;
      }
      collabClose();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if (COLLAB.view === "wf" && collabWf()) { e.preventDefault(); collabQuickRun(); }
      return;
    }
    if (e.key === "Delete" && COLLAB.view === "wf" && !collabTypingTarget(e)) {
      const sel = String(COLLAB.canvas.sel || "");
      if (sel && sel !== "task") {
        e.preventDefault();
        if (collabStageById(collabWf(), sel)) collabDeleteStage(sel);
        else collabDeleteMember(sel);
      }
    }
  });
}


(async function boot() {
  PERF.mark("boot:start");          // ★P5：性能埋点（只在 localStorage.debug=1 时输出）
  // 版本号：自动取本文件被加载时带的 ?v=（与服务端 /healthz 报告的版本比对，用来触发自动刷新）
  $("ui-ver").textContent = "ui " + (UI_VER || "?");

  migrateLegacyKeys();                   // 旧键（opencode-ui.*）→ 新键（cocraft.*）
  loadSettings();                        // 先应用设置（亮度/速度/落樱），再初始化各部件
  applySettings();

  initTheme();
  initToolsMenu();
  initHubMenu();
  initTerm();
  initCode();
  initUpdate();
  // 更新提示：点横幅 / Ctrl+Shift+R 都能刷新（原生 WebView2 里快捷键不一定触发重载）
  const bnEl = $("banner");
  if (bnEl) bnEl.addEventListener("click", () => { if (bannerAction) bannerAction(); else if (updateReady) safeReload(); });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "R" || e.key === "r") && updateReady) {
      e.preventDefault();
      safeReload();
    }
  });
  // ★Ctrl + 滚轮 = 界面缩放（和设置里的「界面缩放」滑块**同一个值**，一起落 localStorage）
  (function initZoomWheel() {
    let want = null, raf = -1;
    window.addEventListener("wheel", (e) => {
      if (!(e.ctrlKey || e.metaKey)) return;
      e.preventDefault();                     // 关键是拦住默认行为：别让浏览器/WebView2 再叠一层原生缩放
      want = Math.max(50, Math.min(150, (Number(SET.zoom) || 100) + (e.deltaY < 0 ? 5 : -5)));
      if (raf !== -1) return;                 // rAF 节流：滚轮一秒几十个事件，别每次都重算设置
      raf = requestAnimationFrame(() => {
        raf = -1;
        if (want != null) { setSetting("zoom", want); want = null; }
      });
    }, { passive: false });
  })();
  initPetals();
  initSidebar();
  initBrandName();
  initAttachments();
  initCopy();
  initMsgNav();                          // 回到底部 / 问题导航（跳到某次提问）
  initPartToggle();                      // 折叠块展开状态记忆
  initSelCopy();                         // 选中片段 → 浮出「复制选中」
  initTrace();                           // 顶栏「轨迹」：思考 / 工具调用
  window.addEventListener("resize", () => { try { syncDrawerOverlap(); } catch { /* ignore */ } });
  initMusic();
  initLiveBg();
  initStaticBg();
  initCocraft();
  initWallpaperPicker();
  initMusicAudio();
  restoreMusicState();                   // 刷新后面板内歌曲信息 / 队列 / 进度恢复
  initSettings();
  initTrust();                           // 顶栏「信任」：权限等级 / 待批准 / 始终允许
  initCollab();                          // 顶栏「协作」：本地多 agent 工作台（不登录）
  // 自定义素材（头像 / 空会话主图与贴纸）：拉回来后再画一次首屏（接口没有就安静跳过）
  loadAppearance().then(() => refreshAppearanceArt());
  initModelPicker();
  initCtxChip();                         // 上下文「还剩多少」小条（点一下看详细）
  initAsk();
  initSkillPicker();
  initThemes();
  pollAsks();
  loadModels().then(updateModelBtn);      // 预取模型清单：顶栏按钮才能立刻显示官方图标
  loadModes();                            // 预取模式清单（opencode 的 build/plan…；ACP 的要等会话）
  startHeartbeat();
  setBanner("正在连接引擎…");
  renderMessages();      // ⚠ 必须先渲染一次：没有会话时也要把消息区换成空态，否则会一直留着 index.html 的兜底横幅
  PERF.mark("boot:firstPaint");     // ★P5：首屏（消息区）画出
  connectionLoop();
  POLL.interval("load-sessions", 20000, loadSessions);
  // 首次启动：要求选默认引擎 / agent / 模型（跳过或完成后不再自动弹）
  try { if (localStorage.getItem(SETUP_KEY) !== "1") setTimeout(openSetup, 350); } catch { /* 隐私模式 */ }
  PERF.mark("boot:end");            // ★P5：启动流程结束
})();

/* 标记：app.js 已成功执行。index.html 里的兜底脚本靠它判断要不要自动重试。 */
window.__appLoaded = true;
try { sessionStorage.removeItem("cocraft.appReloads"); } catch (e) { /* ignore */ }
