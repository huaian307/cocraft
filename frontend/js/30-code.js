/* 30-code.js —— 编码模式 + 语法高亮 + 面板终端（PTY/SSE/ANSI）（P1 从 app.js 拆出，纯搬运不改逻辑） */

/* ---------------- 事件绑定 / 启动 ---------------- */

$("session-list").addEventListener("click", (e) => {
  // ★项目文件树（先判，别让它同时触发"打开会话"）
  const tb = e.target.closest(".tree-btn");
  if (tb) {
    e.preventDefault();
    e.stopPropagation();
    sessionTreeToggle(tb.dataset.tree);
    return;
  }
  const sdir = e.target.closest("[data-sdir]");            // 展开/收起子目录（懒加载）
  if (sdir) {
    e.preventDefault();
    e.stopPropagation();
    const host = sdir.closest(".session-tree");
    const root = host ? host.dataset.root : "";
    const path = sdir.dataset.sdir;
    if (!root || !path) return;
    const ex = (state.treeExp[root] = state.treeExp[root] || {});
    ex[path] = !ex[path];
    renderSessionTrees();
    const d = state.treeData[root] || {};
    if (ex[path] && d[path] === undefined) sessionTreeLoad(root, path);
    return;
  }
  const sfile = e.target.closest("[data-sfile]");          // 点文件 → 编码模式里打开
  if (sfile) {
    e.preventDefault();
    e.stopPropagation();
    const host = sfile.closest(".session-tree");
    const root = host ? host.dataset.root : "";
    if (root) codeOpenAt(root, sfile.dataset.sfile);
    return;
  }
  // 树 / 详情里的空白处：不算"打开会话"
  if (e.target.closest(".session-tree") || e.target.closest(".det")) return;
  const info = e.target.closest(".info");          // 详情展开按钮：先判断，别让它同时触发"打开会话"
  if (info) {
    e.preventDefault();
    e.stopPropagation();
    const id = info.dataset.info;
    state.detailOpen[id] = !state.detailOpen[id];
    renderSessions();
    return;
  }
  const del = e.target.closest(".del");            // 删除按钮：先判断，别让它同时触发"打开会话"
  if (del) {
    e.preventDefault();
    e.stopPropagation();
    const item = del.closest(".session");
    const t = item && item.querySelector(".t");
    deleteSession(del.dataset.del, t ? t.textContent : "");
    return;
  }
  const el = e.target.closest(".session");
  if (el) openSession(el.dataset.id);
});
$("search").addEventListener("input", renderSessions);
$("btn-new").addEventListener("click", openNewSession);
  $("btn-refresh").addEventListener("click", async () => {
    const b = $("btn-refresh");
    if (b) b.classList.add("busy");
    staticSig = null;
    try { await refreshMessages(); setBanner("已重新加载消息"); }
    catch (err) { setBanner("重新加载失败：" + (err && err.message ? err.message : err)); }
    if (b) setTimeout(() => b.classList.remove("busy"), 450);
  });
$("btn-stop").addEventListener("click", interrupt);
$("composer").addEventListener("submit", (e) => { e.preventDefault(); send(); });
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  if (e.key === "Escape") {
    if (state.attachments.length) {                      // 先清附件，再考虑中断生成
      state.attachments = [];
      renderAttachRow();
      return;
    }
    if (state.current) interrupt();
  }
});
$("input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px";
});

// 跟随策略：默认一路往下跟着最新内容；离开底部就暂停跟随，滚回底部再恢复。
// ⚠ 原来只在 wheel 时置 false（拖滚动条 / 键盘翻页不会触发 wheel）→ 往上读历史时
//   一有增量就被拽回底部。现在按"离底部的距离"判，够不着底部就不再跟随。
$("messages").addEventListener("wheel", (e) => {
  if (e.deltaY < 0) state.follow = false;
});
$("messages").addEventListener("scroll", () => {
  const b = $("messages");
  if (!b) return;
  const top = b.scrollTop;
  const prev = histLastTop;
  histLastTop = top;
  state.follow = (b.scrollHeight - top - b.clientHeight) < 60;
  msgNavSchedule();                // ★回到底部 / 问题导航跟着滚动更新
  // ★往上翻页：滚到顶附近、且这次确实是"往上"滚的（首屏渲染时 scrollTop 从 0 跳到最底，不算）
  if (top <= MSG_TOP_TRIGGER && top < prev) maybeLoadHistory();
});
// 「↑ 加载更早的消息」按钮（内容会被重建，所以用事件委托挂在容器上）
$("msg-more").addEventListener("click", (e) => {
  if (e.target.closest(".mm-btn")) loadHistory();
});

/* ============================================================================
   ★消息导航：回到底部 + 问题导航（跳到某次提问 / 上一问 / 下一问 / 回到本问）
   ---------------------------------------------------------------------------
   * 「回到底部」只在离开底部 >80px 时出现（正在读历史时它就是个明确的回位入口）；
   * 「问题」始终列出自上次加载以来的每次**提问**（含刚发出去、还在 pending 的那条），
     点一行就跳到那一轮；`Alt+↑ / Alt+↓` 直接上一问 / 下一问；
   * 「回到本问」= 跳到**当前视口所在那一轮**的提问（长回答读一半时最好用）；
   * 跳过去会给那一轮的 `.bubble` 闪一下，否则滚过去了也看不出停在哪。
   ============================================================================ */

const MSG_NAV = { open: false, timer: 0, instant: false };
try {
  MSG_NAV.instant = !!(navigator.webdriver
    || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches));
} catch { /* 隐私模式 / 老浏览器：保持平滑滚动 */ }

/** 提问原文：用户消息正文在 `text` / `metadata.displayText`（和 messageCopyText 同一套兜底）。 */
function msgUserText(m) {
  if (!m) return "";
  const t = m.text
    || (m.metadata && (m.metadata.displayText || m.metadata.text))
    || "";
  return String(t || "").trim();
}

function msgClock(ts) {
  const n = Number(ts) || 0;
  if (!n) return "";
  try {
    return new Date(n).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return ""; }
}

/** 当前 DOM 里的每一条提问（顺序 = 从上到下）。DOM 是权威：消息重建 / 翻页 / pending 都跟着变。 */
function msgQuestionItems() {
  const box = $("messages");
  if (!box) return [];
  const meta = new Map();
  (state.messages || []).forEach((m) => {
    if (!m || m.type !== "user") return;
    meta.set(String(m.id), { text: msgUserText(m), ts: Number((m.time || {}).created) || 0 });
  });
  (state.pending || []).forEach((p) => {
    if (!p || !p.id) return;
    meta.set(String(p.id), { text: String(p.text || ""), ts: Number(p.time) || 0 });
  });
  const out = [];
  Array.prototype.forEach.call(box.querySelectorAll(".msg.user[data-mid]"), (el) => {
    const id = String(el.dataset.mid || "");
    if (!id) return;
    const mm = meta.get(id) || {};
    let text = mm.text || "";
    if (!text) {
      const b = el.querySelector(".bubble");
      text = b ? String(b.innerText || b.textContent || "").trim() : "";
    }
    text = oneLine(text, 4000);
    out.push({ id, el, index: out.length, ts: mm.ts || 0, full: text, text: oneLine(text, 120) });
  });
  return out;
}

/** 当前视口所在的是第几问（按提问的顶边 <= 消息区顶边 + 26px 的最后一条算；没有就 -1）。 */
function msgCurrentQuestionIndex(items) {
  const box = $("messages");
  if (!box || !items.length) return -1;
  const anchor = box.getBoundingClientRect().top + 26;
  let idx = -1;
  for (let i = 0; i < items.length; i++) {
    if (items[i].el.getBoundingClientRect().top <= anchor) idx = i;
    else break;
  }
  return idx;
}

function msgNavSync() {
  const box = $("messages");
  if (!box) return;
  const btn = $("msg-bottom");
  const ask = $("msg-ask");
  const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
  if (btn) btn.hidden = !(gap > 80);
  const items = msgQuestionItems();
  if (ask) {
    ask.hidden = !items.length;
    const cur = items.length ? msgCurrentQuestionIndex(items) : -1;
    ask.title = items.length
      ? `问题导航：第 ${Math.max(1, cur + 1)}/${items.length} 问（Alt+↑ / Alt+↓）`
      : "问题导航";
  }
  if (MSG_NAV.open) msgRenderQuestions(items);
}

/** 定时器节流：滚动时最多每 40ms 算一次（提问多的时候别在 scroll 里做几十次 rect）。
 *  ⚠ 这里**不用 requestAnimationFrame**：无头 + 虚拟时间里 rAF 不推进（终端渲染踩过同一个坑）→
 *    状态会一直卡在"已排程但没执行"。setTimeout 在真实浏览器 / 无头里都可靠。 */
function msgNavSchedule() {
  if (MSG_NAV.timer) return;
  MSG_NAV.timer = setTimeout(() => {
    MSG_NAV.timer = 0;
    try { msgNavSync(); } catch { /* 导航不是功能前置 */ }
  }, 40);
}

/** 平滑滚动（尊重「减少动态效果」/ 无头环境 → 直接落位）。 */
function msgScrollToY(box, top) {
  const y = Math.max(0, Math.min(Math.max(0, box.scrollHeight - box.clientHeight), Number(top) || 0));
  try { box.scrollTo({ top: y, behavior: MSG_NAV.instant ? "auto" : "smooth" }); }
  catch { box.scrollTop = y; }
}

function msgRenderQuestions(items) {
  const list = $("msg-ask-list");
  if (!list) return;
  items = items || msgQuestionItems();
  if (!items.length) {
    list.innerHTML = `<div class="msg-q-empty">还没有提问 —— 发出一句后就能在这里跳回来。</div>`;
    return;
  }
  const cur = msgCurrentQuestionIndex(items);
  list.innerHTML = items.map((it, i) => `<button type="button"
      class="msg-q${i === cur ? " on" : ""}" data-q-mid="${esc(it.id)}"
      title="${esc(oneLine(it.full || it.text, 300))}">`
    + `<i>${i + 1}</i><span>${esc(it.text || "（无文字）")}</span>`
    + `<em>${esc(msgClock(it.ts))}</em></button>`).join("");
  // 打开时把「当前那一问」滚进弹层自己的视口（别用 scrollIntoView：会连带滚动消息区）
  const on = list.querySelector(".msg-q.on");
  if (on) {
    const top = on.offsetTop, h = on.offsetHeight;
    if (top < list.scrollTop || top + h > list.scrollTop + list.clientHeight) {
      list.scrollTop = Math.max(0, top - list.clientHeight / 2);
    }
  }
}

/** 把某条消息滚到消息区顶部（留 12px 余量），并闪一下。 */
function msgJumpToEl(el, flash) {
  const box = $("messages");
  if (!box || !el) return false;
  const y = el.getBoundingClientRect().top - box.getBoundingClientRect().top + box.scrollTop - 12;
  state.follow = false;
  msgScrollToY(box, y);
  if (flash !== false) {
    el.classList.remove("flash");
    void el.offsetWidth;                        // 强制重排，连续点同一问也能重新闪
    el.classList.add("flash");
    setTimeout(() => el.classList.remove("flash"), 1500);
  }
  msgNavSchedule();
  return true;
}

function msgJumpById(id) {
  const items = msgQuestionItems();
  const hit = items.find((x) => String(x.id) === String(id));
  if (!hit) { setBanner("这条提问已经不在当前消息里了（可能刚刷新过）"); return; }
  msgJumpToEl(hit.el);
}

/** delta=-1 上一问 / +1 下一问；到头就停在第一条 / 最后一条。 */
function msgStepQuestion(delta) {
  const items = msgQuestionItems();
  if (!items.length) { setBanner("当前会话还没有提问"); return; }
  const cur = msgCurrentQuestionIndex(items);
  let idx = cur < 0 ? (delta > 0 ? 0 : items.length - 1) : cur + delta;
  idx = Math.max(0, Math.min(items.length - 1, idx));
  msgJumpToEl(items[idx].el);
  if (MSG_NAV.open) msgRenderQuestions(items);
}

/** 回到**当前阅读的这一轮**的提问（长回答读一半时用）。 */
function msgJumpCurrentQuestion() {
  const items = msgQuestionItems();
  if (!items.length) { setBanner("当前会话还没有提问"); return; }
  const cur = msgCurrentQuestionIndex(items);
  msgJumpToEl(items[cur < 0 ? items.length - 1 : cur].el);
}

function msgScrollBottom() {
  const box = $("messages");
  if (!box) return;
  state.follow = true;
  msgScrollToY(box, box.scrollHeight);
  msgNavSchedule();
}

function msgAskOpen(open) {
  const pop = $("msg-ask-pop");
  const btn = $("msg-ask");
  MSG_NAV.open = !!open;
  if (pop) pop.hidden = !open;
  if (btn) btn.classList.toggle("on", !!open);
  if (open) msgRenderQuestions();
}

function initMsgNav() {
  const bottom = $("msg-bottom");
  if (bottom) bottom.addEventListener("click", msgScrollBottom);
  const ask = $("msg-ask");
  if (ask) ask.addEventListener("click", () => msgAskOpen(!MSG_NAV.open));
  const close = $("msg-q-close");
  if (close) close.addEventListener("click", () => msgAskOpen(false));
  const prev = $("msg-q-prev");
  if (prev) prev.addEventListener("click", () => msgStepQuestion(-1));
  const next = $("msg-q-next");
  if (next) next.addEventListener("click", () => msgStepQuestion(1));
  const cur = $("msg-q-cur");
  if (cur) cur.addEventListener("click", () => { msgJumpCurrentQuestion(); msgAskOpen(false); });
  const list = $("msg-ask-list");
  if (list) list.addEventListener("click", (e) => {
    const row = e.target.closest("[data-q-mid]");
    if (!row) return;
    msgJumpById(row.dataset.qMid);
    msgAskOpen(false);
  });
  // 点弹层外面 / Esc 收起（点「问题」按钮自己不算外面）
  document.addEventListener("click", (e) => {
    if (!MSG_NAV.open) return;
    const nav = $("msg-nav");
    if (nav && nav.contains(e.target)) return;
    msgAskOpen(false);
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && MSG_NAV.open) { msgAskOpen(false); return; }
    if (!e.altKey || e.ctrlKey || e.metaKey || e.shiftKey) return;
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    const ae = document.activeElement || {};
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(String(ae.tagName || ""))) return;
    e.preventDefault();
    msgStepQuestion(e.key === "ArrowUp" ? -1 : 1);
  });
  msgNavSchedule();
}

/* ============================================================================
   面板内终端：真·ConPTY（后端 /pty/*），前端做一个「够用」的 ANSI 渲染器
   ---------------------------------------------------------------------------
   * 数据：EventSource(/pty/{id}/stream) 收 ANSI 文本，POST /pty/{id}/input 送按键；
   * 渲染：维护一个「格子」屏（行 × 列），支持 SGR 颜色/加粗、光标定位、清屏、
     滚屏；只渲染视口附近的行，用 padding 撑出滚动高度，避免长输出卡顿；
   * ⚠ 这是**基础**终端：整屏 TUI（vim / htop 一类）不保证；普通命令/回显没问题。
   ============================================================================ */

const TERM_BASE = { fg: null, bg: null, b: 0, d: 0, i: 0, u: 0 };
// ANSI 16 色用**主题变量**：具体色值在 style.css 里按昼/夜分别定义，
// 这样切主题时终端里的颜色会跟着变，不用重渲染。
const TERM_PALETTE = [
  "var(--t0)", "var(--t1)", "var(--t2)", "var(--t3)", "var(--t4)", "var(--t5)", "var(--t6)", "var(--t7)",
  "var(--t8)", "var(--t9)", "var(--t10)", "var(--t11)", "var(--t12)", "var(--t13)", "var(--t14)", "var(--t15)",
];

const TERM = {
  open: false, embedded: false, id: "", es: null, info: null, dead: false,
  cols: 100, rows: 30, chW: 8, lh: 16,
  grid: [], cur: { r: 0, c: 0 }, sty: { ...TERM_BASE }, pending: "",
  startRow: 0, raf: 0, focused: false,
};

function term256(n) {
  n = n | 0;
  if (n < 0 || n > 255) return null;
  if (n < 16) return TERM_PALETTE[n];
  if (n >= 232) { const v = 8 + (n - 232) * 10; return `rgb(${v},${v},${v})`; }
  n -= 16;
  const c = (x) => (x === 0 ? 0 : 55 + x * 40);
  return `rgb(${c(Math.floor(n / 36))},${c(Math.floor((n % 36) / 6))},${c(n % 6)})`;
}

function termStyleCss(s) {
  if (!s) return "";
  let css = "";
  if (s.fg != null) css += "color:" + s.fg + ";";
  if (s.bg != null) css += "background:" + s.bg + ";";
  if (s.b) css += "font-weight:600;";
  if (s.i) css += "font-style:italic;";
  if (s.d) css += "opacity:.68;";
  if (s.u) css += "text-decoration:underline;";
  return css;
}
function termSameStyle(a, b) {
  const A = a || TERM_BASE, B = b || TERM_BASE;
  return A.fg === B.fg && A.bg === B.bg && A.b === B.b && A.d === B.d && A.i === B.i && A.u === B.u;
}

function termEnsureRow(r) { while (TERM.grid.length <= r) TERM.grid.push([]); }
function termScrollCap() {
  const MAX = 2000;
  if (TERM.grid.length > MAX) {
    const drop = TERM.grid.length - MAX;
    TERM.grid.splice(0, drop);
    TERM.cur.r = Math.max(0, TERM.cur.r - drop);
    TERM.startRow = Math.max(0, TERM.startRow - drop);
  }
}
function termPut(ch) {
  if (TERM.cur.c >= TERM.cols) { TERM.cur.c = 0; TERM.cur.r++; }
  termEnsureRow(TERM.cur.r);
  const row = TERM.grid[TERM.cur.r];
  row[TERM.cur.c] = {
    c: ch, fg: TERM.sty.fg, bg: TERM.sty.bg,
    b: TERM.sty.b, d: TERM.sty.d, i: TERM.sty.i, u: TERM.sty.u,
  };
  TERM.cur.c++;
}
function termEraseLine(mode) {
  termEnsureRow(TERM.cur.r);
  if (mode === 2) { TERM.grid[TERM.cur.r] = []; return; }
  const row = TERM.grid[TERM.cur.r];
  const from = mode === 1 ? 0 : TERM.cur.c;
  const to = mode === 0 ? TERM.cols : TERM.cur.c + 1;
  for (let c = from; c < to; c++) row[c] = null;
}
function termEraseDisplay(mode) {
  if (mode === 2) { TERM.grid = []; TERM.cur = { r: 0, c: 0 }; termEnsureRow(0); return; }
  if (mode === 1) {
    for (let r = 0; r < TERM.cur.r; r++) TERM.grid[r] = [];
    termEraseLine(1);
  } else {
    for (let r = TERM.cur.r + 1; r < TERM.grid.length; r++) TERM.grid[r] = [];
    termEraseLine(0);
  }
}

function termSgr(nums) {
  if (!nums.length) nums = [0];
  for (let k = 0; k < nums.length; k++) {
    const p = nums[k];
    if (p === 0) TERM.sty = { ...TERM_BASE };
    else if (p === 1) TERM.sty.b = 1;
    else if (p === 2) TERM.sty.d = 1;
    else if (p === 3) TERM.sty.i = 1;
    else if (p === 4) TERM.sty.u = 1;
    else if (p === 22) { TERM.sty.b = 0; TERM.sty.d = 0; }
    else if (p === 23) TERM.sty.i = 0;
    else if (p === 24) TERM.sty.u = 0;
    else if (p >= 30 && p <= 37) TERM.sty.fg = TERM_PALETTE[p - 30];
    else if (p >= 90 && p <= 97) TERM.sty.fg = TERM_PALETTE[p - 90 + 8];
    else if (p === 39) TERM.sty.fg = null;
    else if (p >= 40 && p <= 47) TERM.sty.bg = TERM_PALETTE[p - 40];
    else if (p >= 100 && p <= 107) TERM.sty.bg = TERM_PALETTE[p - 100 + 8];
    else if (p === 49) TERM.sty.bg = null;
    else if (p === 38 || p === 48) {
      const isFg = p === 38;
      if (nums[k + 1] === 5) {
        const c = term256(nums[k + 2]);
        if (c != null) { if (isFg) TERM.sty.fg = c; else TERM.sty.bg = c; }
        k += 2;
      } else if (nums[k + 1] === 2) {
        const c = `rgb(${nums[k + 2] | 0},${nums[k + 3] | 0},${nums[k + 4] | 0})`;
        if (isFg) TERM.sty.fg = c; else TERM.sty.bg = c;
        k += 4;
      }
    }
  }
}

function termCsi(body) {
  const final = body[body.length - 1];
  let params = body.slice(0, -1);
  let priv = "";
  if (/^[?>!=]/.test(params)) { priv = params[0]; params = params.slice(1); }
  const nums = params === "" ? [] : params.split(";").map((x) => (x === "" ? 0 : parseInt(x, 10)));
  const P = (i, d) => (nums[i] === undefined || isNaN(nums[i]) ? d : nums[i]);
  switch (final) {
    case "m": termSgr(nums); break;
    case "H": case "f": TERM.cur.r = P(0, 1) - 1; TERM.cur.c = P(1, 1) - 1; termEnsureRow(TERM.cur.r); break;
    case "A": TERM.cur.r = Math.max(0, TERM.cur.r - P(0, 1)); break;
    case "B": TERM.cur.r += P(0, 1); termEnsureRow(TERM.cur.r); break;
    case "C": TERM.cur.c += P(0, 1); break;
    case "D": TERM.cur.c = Math.max(0, TERM.cur.c - P(0, 1)); break;
    case "E": TERM.cur.r += P(0, 1); TERM.cur.c = 0; termEnsureRow(TERM.cur.r); break;
    case "F": TERM.cur.r = Math.max(0, TERM.cur.r - P(0, 1)); TERM.cur.c = 0; break;
    case "G": TERM.cur.c = P(0, 1) - 1; break;
    case "d": TERM.cur.r = P(0, 1) - 1; termEnsureRow(TERM.cur.r); break;
    case "J": termEraseDisplay(P(0, 0)); break;
    case "K": termEraseLine(P(0, 0)); break;
    case "L": for (let n = 0; n < P(0, 1); n++) TERM.grid.splice(TERM.cur.r, 0, []); break;
    case "M": for (let n = 0; n < P(0, 1); n++) TERM.grid.splice(TERM.cur.r, 1); break;
    case "P": { termEnsureRow(TERM.cur.r); TERM.grid[TERM.cur.r].splice(TERM.cur.c, P(0, 1)); break; }
    case "@": {
      termEnsureRow(TERM.cur.r);
      TERM.grid[TERM.cur.r].splice(TERM.cur.c, 0, ...new Array(P(0, 1)).fill(null));
      break;
    }
    // ★ 终端查询必须应答：DA（`CSI c`）不回，pwsh/PSReadLine 会等 ~3.5s 超时才画提示符
    //   —— 这就是"终端启动慢"的真凶（实测 3.75s → 0.28s）。
    case "c":                                            // DA（设备属性）
      termReply(priv === ">" ? "\x1b[>0;95;0c" : "\x1b[?1;2c");
      break;
    case "n": {                                          // DSR（设备状态 5n / 光标位置 6n）
      const q = P(0, 0);
      if (q === 5) termReply("\x1b[0n");
      else if (q === 6) termReply(`\x1b[${TERM.cur.r + 1};${TERM.cur.c + 1}R`);
      break;
    }
    // h / l（私有模式：光标显隐、备用屏、括号粘贴…）基础实现直接忽略
    default: break;
  }
}

/** 处理 ESC 序列；返回最后吃掉的字符下标，-1 = 本块结尾不完整（留到下一块）。 */
function termEsc(text, i) {
  const next = text[i + 1];
  if (next === undefined) return -1;
  if (next === "[") {
    let j = i + 2;
    while (j < text.length && !/[@-~]/.test(text[j])) j++;
    if (j >= text.length) return -1;
    termCsi(text.slice(i + 2, j + 1));
    return j;
  }
  if (next === "]") {                                  // OSC：吃到 BEL 或 ESC \
    let j = i + 2;
    while (j < text.length) {
      if (text[j] === "\x07") return j;
      if (text[j] === "\x1b" && text[j + 1] === "\\") return j + 1;
      j++;
    }
    return -1;
  }
  if ("() *+-./".indexOf(next) >= 0) return text[i + 2] === undefined ? -1 : i + 2;
  if (next === "c") { TERM.grid = []; TERM.cur = { r: 0, c: 0 }; TERM.sty = { ...TERM_BASE }; return i + 1; }
  if (next === "D") { TERM.cur.r++; termEnsureRow(TERM.cur.r); return i + 1; }
  if (next === "M") { TERM.cur.r = Math.max(0, TERM.cur.r - 1); return i + 1; }
  return i + 1;                                        // 其余单字符转义：忽略
}

function termFeed(text) {
  if (TERM.pending) { text = TERM.pending + text; TERM.pending = ""; }
  let i = 0;
  for (; i < text.length; i++) {
    const ch = text[i];
    if (ch === "\x1b") {
      const r = termEsc(text, i);
      if (r < 0) { TERM.pending = text.slice(i); break; }
      i = r;
      continue;
    }
    if (ch === "\n") { TERM.cur.r++; TERM.cur.c = 0; termEnsureRow(TERM.cur.r); continue; }
    if (ch === "\r") { TERM.cur.c = 0; continue; }
    if (ch === "\b") { if (TERM.cur.c > 0) TERM.cur.c--; continue; }
    if (ch === "\t") { TERM.cur.c = Math.min(TERM.cols - 1, (Math.floor(TERM.cur.c / 8) + 1) * 8); continue; }
    if (ch === "\x07" || ch === "\x00") continue;
    termPut(ch);
  }
  termScrollCap();
  termSchedule();
}

function termSchedule() {
  if (TERM.raf) return;
  // ⚠ 不用 requestAnimationFrame：无头虚拟时间不推进 rAF，会导致渲染一直不执行
  //（项目里踩过同类坑）。setTimeout 在真实浏览器 / WebView2 / 无头里都可靠。
  TERM.raf = setTimeout(() => { TERM.raf = 0; termRender(); }, 16);
}

function termRender() {
  const screen = $("term-screen"), wrap = $("term-wrap");
  if (!screen || !wrap) return;
  const lh = TERM.lh, total = TERM.grid.length;
  const viewH = wrap.clientHeight || 0;
  const viewRows = Math.max(1, Math.round(viewH / lh));
  const follow = wrap.scrollHeight - wrap.scrollTop - viewH < lh * 1.5;
  let start = follow ? Math.max(0, total - viewRows) : Math.floor(wrap.scrollTop / lh);
  start = Math.max(0, Math.min(start, Math.max(0, total - viewRows)));
  const end = Math.min(total, start + viewRows + 1);
  TERM.startRow = start;

  let html = "";
  for (let r = start; r < end; r++) {
    const row = TERM.grid[r] || [];
    let line = "", run = "", runCell = null;
    const flush = () => {
      if (!run) return;
      const css = termStyleCss(runCell);
      line += css ? `<span style="${css}">${esc(run)}</span>` : esc(run);
      run = ""; runCell = null;
    };
    for (let c = 0; c < TERM.cols; c++) {
      const cell = row[c] || null;
      if (run && !termSameStyle(runCell, cell)) { flush(); }
      if (!run) runCell = cell;
      run += cell ? cell.c : " ";
    }
    flush();
    html += line + (r < end - 1 ? "\n" : "");
  }
  screen.style.paddingTop = (6 + start * lh) + "px";
  screen.style.paddingBottom = (Math.max(0, total - end) * lh + 6) + "px";
  screen.innerHTML = html;

  const caret = $("term-caret");
  if (caret) {
    const show = TERM.focused && !TERM.dead;
    caret.hidden = !show;
    if (show) {
      caret.style.top = (6 + TERM.cur.r * lh) + "px";
      caret.style.left = (10 + TERM.cur.c * TERM.chW) + "px";
    }
  }
  if (follow) wrap.scrollTop = wrap.scrollHeight;
}

function termMeasure() {
  const m = $("term-measure");
  if (m) { m.textContent = "MMMMMMMMMM"; TERM.chW = (m.offsetWidth / 10) || 8; m.textContent = "M"; }
  const screen = $("term-screen");
  if (screen) {
    const cs = getComputedStyle(screen);
    TERM.lh = Math.round(parseFloat(cs.lineHeight)) || 16;
  }
}

function termFit() {
  const wrap = $("term-wrap");
  if (!wrap) return;
  const cols = Math.max(20, Math.floor((wrap.clientWidth - 20) / TERM.chW));
  const rows = Math.max(5, Math.floor(wrap.clientHeight / TERM.lh));
  if (cols === TERM.cols && rows === TERM.rows) return;
  TERM.cols = cols; TERM.rows = rows;
  termSchedule();
  if (TERM.id && !TERM.dead) {
    api(`/pty/${TERM.id}/resize`, { method: "POST", body: JSON.stringify({ cols, rows }) }).catch(() => {});
  }
}

function termResetScreen() {
  TERM.grid = []; TERM.cur = { r: 0, c: 0 }; TERM.sty = { ...TERM_BASE };
  TERM.pending = ""; TERM.startRow = 0; TERM.dead = false;
  termEnsureRow(0);
}

function termMeta() {
  const m = $("term-meta");
  if (!m) return;
  const info = TERM.info || {};
  m.textContent = (info.cwd || "?") + "  ·  " + (info.shell || "") + (TERM.dead ? "  ·  已退出" : "");
  m.title = m.textContent;
}

function termSendInput(data) {
  if (!TERM.id || TERM.dead || !data) return;
  api(`/pty/${TERM.id}/input`, { method: "POST", body: JSON.stringify({ data }) }).catch(() => {});
}

/** 回应终端的查询序列（DA/DSR）。⚠ 不回应的话 pwsh 会等 ~3.5s 超时才画提示符（"启动慢"的真凶）。 */
function termReply(seq) { termSendInput(seq); }

function termConnect() {
  if (TERM.es) { try { TERM.es.close(); } catch { /* ignore */ } TERM.es = null; }
  const es = new EventSource(`/pty/${TERM.id}/stream`);
  TERM.es = es;
  es.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch { return; }
    if (m.t === "out") termFeed(m.d || "");
    else if (m.t === "exit") {
      TERM.dead = true;
      termFeed("\r\n\x1b[2m[进程已退出" + (m.code != null ? "，代码 " + m.code : "") + "]\x1b[0m\r\n");
      termMeta();
      try { es.close(); } catch { /* ignore */ }
      if (TERM.es === es) TERM.es = null;
    }
  };
  es.onerror = () => {                                  // 不自动重连，免得重复流
    try { es.close(); } catch { /* ignore */ }
    if (TERM.es === es) TERM.es = null;
  };
}

async function termStart() {
  if (TERM.id) return;
  // 「run」（嵌入式终端）跟着编码模式的根目录走 —— 编辑和运行在同一个文件夹
  const cwd = (TERM.embedded && typeof CODE !== "undefined" && CODE.root) ? CODE.root : currentDir();
  const r = await api("/pty", { method: "POST", body: JSON.stringify({ cwd, cols: TERM.cols, rows: TERM.rows }) });
  TERM.id = r.data.id;
  TERM.info = r.data;
  termMeta();
  termConnect();
}

async function termDestroy() {
  if (TERM.es) { try { TERM.es.close(); } catch { /* ignore */ } TERM.es = null; }
  if (TERM.id) {
    const id = TERM.id;
    TERM.id = "";
    try { await api(`/pty/${id}`, { method: "DELETE" }); } catch { /* ignore */ }
  }
}

async function termComponentOk() {
  try {
    const c = await api("/pty/component");
    const comp = (c && c.component) || {};
    if (!comp.installed) {
      setBanner("面板内终端不可用：" + (comp.error || "终端组件未安装")
        + "（可先“在外部终端打开”）");
      return false;
    }
    return true;
  } catch (err) {
    setBanner("终端组件检查失败：" + (err && err.message ? err.message : err));
    return false;
  }
}

async function termOpen() {
  if (TERM.open) { syncCodeRunBtn(); return; }
  if (!(await termComponentOk())) return;
  const box = $("term");
  // 「允许代码」时把终端挂进编码抽屉的槽（宽度 = 抽屉宽度）；否则还是底部抽屉
  const host = TERM.embedded ? $("code-term-slot") : document.body;
  if (box && host && box.parentElement !== host) host.appendChild(box);
  if (box) {
    box.classList.toggle("embedded", !!TERM.embedded);
    box.classList.add("open"); box.setAttribute("aria-hidden", "false");
  }
  if (!TERM.embedded) document.documentElement.classList.add("term-open");  // ← 分屏：给面板让出底部
  TERM.open = true;
  syncToolsBtn();
  syncCodeRunBtn();
  termMeasure();
  // ⚠ 收起抽屉**不销毁**终端（像 VS Code）：这里如果还有活着的会话就直接复用 → 秒开。
  const reuse = !!TERM.id && !TERM.dead;
  if (!reuse) { TERM.dead = false; termResetScreen(); }
  termFit();
  termSchedule();
  if (reuse) {
    termConnect();        // 重新接上输出（隐藏期间攒在服务端的输出会补回来）
    termMeta();
  } else {
    try { await termStart(); } catch (err) {
      setBanner("开终端失败：" + (err && err.message ? err.message : err));
      termClose();
      return;
    }
  }
  termSchedule();
  termFocus();
}

function termClose() {
  // 只隐藏、不杀掉终端进程（下次打开秒开）；要真正重开用头部「重开」。
  if (TERM.es) { try { TERM.es.close(); } catch { /* ignore */ } TERM.es = null; }
  const box = $("term");
  if (box) { box.classList.remove("open"); box.setAttribute("aria-hidden", "true"); }
  document.documentElement.classList.remove("term-open");
  TERM.open = false;
  syncToolsBtn();
  syncCodeRunBtn();
}

function termToggle() { TERM.open ? termClose() : termOpen(); }

/** 把键盘焦点交给终端输入区（多次重试：移动 DOM / 抽屉滑入都会丢焦点）。 */
function termFocus() {
  const wrap = $("term-wrap");
  if (!wrap) return;
  const f = () => { try { wrap.focus({ preventScroll: true }); } catch { try { wrap.focus(); } catch { /* ignore */ } } };
  f();
  setTimeout(f, 60);
  setTimeout(f, 220);
}

async function termRestart() {
  await termDestroy();
  termResetScreen();
  termFit();
  try { await termStart(); } catch (err) {
    setBanner("重开终端失败：" + (err && err.message ? err.message : err));
  }
  termSchedule();
  termFocus();
}

function termClear() {
  TERM.grid = []; TERM.cur = { r: 0, c: 0 }; TERM.pending = ""; TERM.startRow = 0;
  termEnsureRow(0);
  termSchedule();
}

function termKey(e) {
  if (e.isComposing || e.keyCode === 229) return;      // 输入法组合中：先不管
  const k = e.key;
  if (e.ctrlKey && !e.altKey && !e.metaKey && !e.shiftKey) {
    const low = k.toLowerCase();
    if (low === "v") return;                            // 留给 paste 事件
    if (low.length === 1 && low >= "a" && low <= "z") {
      termSendInput(String.fromCharCode(low.charCodeAt(0) - 96));
      e.preventDefault(); return;
    }
    if (k === " " || k === "@") { termSendInput("\x00"); e.preventDefault(); return; }
    if (k === "[") { termSendInput("\x1b"); e.preventDefault(); return; }
    return;
  }
  if (e.altKey && k.length === 1) { termSendInput("\x1b" + k); e.preventDefault(); return; }
  const map = {
    Enter: "\r", Backspace: "\x7f", Tab: "\t", Escape: "\x1b",
    ArrowUp: "\x1b[A", ArrowDown: "\x1b[B", ArrowRight: "\x1b[C", ArrowLeft: "\x1b[D",
    Home: "\x1b[H", End: "\x1b[F", Delete: "\x1b[3~",
    PageUp: "\x1b[5~", PageDown: "\x1b[6~", Insert: "\x1b[2~",
  };
  if (map[k] != null) { termSendInput(map[k]); e.preventDefault(); return; }
  if (k.length === 1 && !e.ctrlKey && !e.metaKey) { termSendInput(k); e.preventDefault(); }
}

const TERM_H_KEY = "cocraft.term.h";

/** 设定终端抽屉高度（写 --term-h；分屏 padding 用的也是它）。 */
function termApplyHeight(px) {
  if (TERM.embedded) {                                   // 嵌入式（编码模式）用另一套高度变量
    const max = Math.max(140, Math.round(window.innerHeight * 0.7));
    const h = Math.max(100, Math.min(Math.round(px), max));
    document.documentElement.style.setProperty("--code-term-h", h + "px");
    return h;
  }
  const max = Math.max(160, Math.round(window.innerHeight * 0.86));
  const h = Math.max(140, Math.min(Math.round(px), max));
  document.documentElement.style.setProperty("--term-h", h + "px");
  return h;
}

/** 顶边缘拖动改高度（跟群聊抽屉的宽度拖动同一套写法）。 */
function termInitResize() {
  const box = $("term"), grip = $("term-resize");
  if (!box || !grip) return;
  try {
    const saved = parseInt(localStorage.getItem(TERM_H_KEY) || "", 10);
    if (Number.isFinite(saved) && saved > 0) termApplyHeight(saved);
  } catch { /* 隐私模式 */ }
  let dragging = false, startY = 0, startH = 0;
  grip.addEventListener("pointerdown", (e) => {
    dragging = true; startY = e.clientY; startH = box.getBoundingClientRect().height;
    box.classList.add("resizing");
    document.documentElement.classList.add("term-resizing");
    try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (dragging) termApplyHeight(startH + (startY - e.clientY));   // 往上拖 = 变高
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    box.classList.remove("resizing");
    document.documentElement.classList.remove("term-resizing");
    try { grip.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    try {
      const key = TERM.embedded ? CODE_TERM_H_KEY : TERM_H_KEY;
      localStorage.setItem(key, String(Math.round(box.getBoundingClientRect().height)));
    } catch { /* ignore */ }
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

function initTerm() {
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  on("term-close", () => { if (TERM.embedded) codeTermOff(); else termClose(); });
  on("term-clear", termClear);
  on("term-restart", termRestart);
  on("term-ext", () => openCurrentDir("terminal"));
  termInitResize();

  const wrap = $("term-wrap");
  if (wrap) {
    wrap.addEventListener("keydown", termKey);
    wrap.addEventListener("paste", (e) => {
      const t = (e.clipboardData || window.clipboardData).getData("text");
      if (t) termSendInput(t);
      e.preventDefault();
    });
    wrap.addEventListener("focus", () => { TERM.focused = true; termSchedule(); });
    wrap.addEventListener("blur", () => { TERM.focused = false; termSchedule(); });
    wrap.addEventListener("pointerdown", termFocus);
    wrap.addEventListener("mousedown", termFocus);
  }
  // 点终端任意处（标题栏 / 空白）也把焦点交给输入区
  const termBox = $("term");
  if (termBox) termBox.addEventListener("pointerdown", (e) => {
    const t = e.target;
    if (t && t.closest && (t.closest("button") || t.closest(".term-resize"))) return;
    termFocus();
  });

  // 兜底：终端开着、但当前**没聚焦到任何输入框**（例如刚点完 run / 点了空白）时直接敲字，
  // 也把按键转给终端并聚焦 —— 免得"看起来能开，但打不了字"。
  document.addEventListener("keydown", (e) => {
    if (!TERM.open || TERM.dead) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && t.closest && t.closest("#term")) return;                 // 终端自己会处理
    if (t && t.closest && t.closest("input, textarea, select, [contenteditable=''], [contenteditable='true']")) return;
    if (document.querySelector(".wp:not([hidden]), .cfg:not([hidden]), .ask:not([hidden])")) return;  // 有弹窗时别抢
    const ae = document.activeElement;
    const onBody = !ae || ae === document.body || ae === document.documentElement;
    const onTermBtn = !!(ae && ae.closest && ae.closest("#term, #code-menu, #code-exec"));
    if (!onBody && !onTermBtn) return;
    termFocus();
    termKey(e);
  });
  if (window.ResizeObserver && wrap) {
    try { TERM.ro = new ResizeObserver(() => { if (TERM.open) termFit(); }); TERM.ro.observe(wrap); }
    catch { /* ignore */ }
  }
  window.addEventListener("resize", () => {
    if (!TERM.open) return;
    termMeasure();
    termFit();
  });
}

/* ============================================================================
   编码模式（仿 VS Code 的简便版）：文件树 + 看代码 + 直接改并保存
   ---------------------------------------------------------------------------
   * 后端 /fs/*（引擎无关）：list / read / write，全部限制在当前会话目录内；
   * 编辑后「保存」或 Ctrl+S 写回（UTF-8）；不做语法高亮（"简便"）。
   ============================================================================ */

/* ============================================================================
   语法高亮（仿 VS Code 的 token 配色；纯正则、无依赖）
   ---------------------------------------------------------------------------
   * 规则表按语言给；组合成一个正则一次扫完（字符串/注释整段吃掉，里面的关键字不会再被匹配）；
   * 颜色走 CSS 变量：昼主题 = VS Code Light+，夜主题 = Dark+；见 style.css 的 --c-*。
   * ⚠ 只是"够看"的高亮：不做完整解析（比如 JS 正则字面量可能被当除法）。
   ============================================================================ */

const HL_RULES = {
  js: [
    ["tk-comment", /\/\/[^\n]*/],
    ["tk-comment", /\/\*[\s\S]*?\*\//],
    ["tk-string", /"(?:\\.|[^"\\])*"/],
    ["tk-string", /'(?:\\.|[^'\\])*'/],
    ["tk-string", /`(?:\\.|[^`\\])*`/],
    ["tk-control", /\b(?:if|else|for|while|do|switch|case|break|continue|return|try|catch|finally|throw|new|await|yield|of|in|with)\b/],
    ["tk-keyword", /\b(?:const|let|var|function|class|extends|super|import|export|from|default|async|static|get|set|typeof|instanceof|void|delete|this|as)\b/],
    ["tk-const", /\b(?:true|false|null|undefined|NaN|Infinity)\b/],
    ["tk-func", /\b[A-Za-z_$][\w$]*(?=\s*\()/],
    ["tk-type", /\b[A-Z][A-Za-z0-9_$]*\b/],
    ["tk-var", /(?<=\.)[A-Za-z_$][\w$]*/],
    ["tk-number", /0[xX][0-9a-fA-F]+n?|\b\d[\d_]*(?:\.[\d_]+)?(?:[eE][+-]?\d+)?\b/],
    ["tk-op", /[+\-*/%=<>!&|^~?:]+/],
    ["tk-op", /[{}()\[\];,.]/],
  ],
  json: [
    ["tk-string", /"(?:\\.|[^"\\])*"/],
    ["tk-const", /\b(?:true|false|null)\b/],
    ["tk-number", /-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/],
    ["tk-op", /[{}\[\],:]/],
  ],
  html: [
    ["tk-comment", /<!--[\s\S]*?-->/],
    ["tk-meta", /<!DOCTYPE[^>]*>/i],
    ["tk-tag", /<\/?[A-Za-z][\w:-]*/],
    ["tk-attr", /[A-Za-z_:][\w:.-]*(?=\s*=)/],
    ["tk-string", /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/],
    ["tk-tag", /\/?>/],
    ["tk-const", /&(?:[a-zA-Z]+|#\d+|#x[0-9a-fA-F]+);/],
  ],
  css: [
    ["tk-comment", /\/\*[\s\S]*?\*\//],
    ["tk-string", /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/],
    ["tk-meta", /@[A-Za-z-]+/],
    ["tk-const", /#[0-9a-fA-F]{3,8}\b/],
    ["tk-attr", /[a-zA-Z-]+(?=\s*:)/],
    ["tk-func", /\b[a-zA-Z-]+(?=\()/],
    ["tk-number", /-?\b\d+(?:\.\d+)?(?:px|em|rem|%|vh|vw|vmin|vmax|s|ms|deg|fr|pt)?\b/],
    ["tk-op", /[{}();:,>~+]/],
  ],
  py: [
    ["tk-comment", /#[^\n]*/],
    ["tk-string", /"""[\s\S]*?"""|'''[\s\S]*?'''/],
    ["tk-string", /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/],
    ["tk-meta", /@[A-Za-z_][\w.]*/],
    ["tk-control", /\b(?:if|elif|else|for|while|return|yield|break|continue|try|except|finally|raise|with|as|pass|import|from|in|is|not|and|or|lambda|assert|del|global|nonlocal|await|async)\b/],
    ["tk-keyword", /\b(?:def|class|self|cls)\b/],
    ["tk-const", /\b(?:None|True|False)\b/],
    ["tk-func", /\b[A-Za-z_]\w*(?=\s*\()/],
    ["tk-type", /\b[A-Z][A-Za-z0-9_]*\b/],
    ["tk-number", /\b\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?\b/],
    ["tk-op", /[+\-*/%=<>!&|^~?:]+/],
    ["tk-op", /[{}()\[\];,.]/],
  ],
  md: [
    ["tk-meta", /^#{1,6}[ \t].*$/],
    ["tk-keyword", /\*\*(?:\\.|[^*])*\*\*|__(?:\\.|[^_])*__/],
    ["tk-string", /`[^`\n]*`/],
    ["tk-func", /\[[^\]]*\]\([^)]*\)/],
    ["tk-comment", /^>[ \t].*$/],
    ["tk-const", /^\s*[-*+][ \t]/],
    ["tk-tag", /^(?:---|\*\*\*|___)\s*$/],
  ],
  sh: [
    ["tk-comment", /#[^\n]*/],
    ["tk-string", /"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/],
    ["tk-var", /\$\{[^}]*\}|\$[A-Za-z_]\w*|\$[?#@!$*0-9]/],
    ["tk-control", /\b(?:if|then|else|elif|fi|for|while|do|done|case|esac|in|return|function|local|export|echo|cd|exit|set|source|read)\b/],
    ["tk-number", /\b\d+\b/],
    ["tk-op", /[|&;<>(){}[\]]+/],
  ],
  // C / C++（含 .h，语言名统一用 cpp 规则）
  cpp: [
    ["tk-comment", /\/\/[^\n]*/],
    ["tk-comment", /\/\*[\s\S]*?\*\//],
    ["tk-meta", /^[ \t]*#[ \t]*[A-Za-z_]\w*/m],
    ["tk-string", /"(?:\\.|[^"\\])*"/],
    ["tk-string", /'(?:\\.|[^'\\])*'/],
    ["tk-control", /\b(?:if|else|for|while|do|switch|case|default|break|continue|return|goto|try|catch|throw|new|delete|co_await|co_return|co_yield)\b/],
    ["tk-keyword", /\b(?:const|constexpr|consteval|constinit|volatile|static|extern|inline|virtual|override|final|class|struct|union|enum|namespace|using|typedef|template|typename|public|private|protected|friend|operator|this|nullptr|true|false|auto|register|mutable|explicit|export|import|module|noexcept|decltype|sizeof|static_cast|dynamic_cast|const_cast|reinterpret_cast)\b/],
    ["tk-type", /\b(?:void|bool|char|wchar_t|char8_t|char16_t|char32_t|short|int|long|float|double|signed|unsigned|size_t|ssize_t|string|wstring|vector|map|set|unordered_map|unordered_set|multiset|pair|tuple|deque|queue|stack|priority_queue|bitset|array|std|cin|cout|cerr|endl)\b/],
    ["tk-func", /\b[A-Za-z_]\w*(?=\s*\()/],
    ["tk-number", /0[xX][0-9a-fA-F]+[uUlLzZ]*|\b\d[\d']*(?:\.[\d']+)?(?:[eE][+-]?\d+)?[fFuUlLzZ]*\b/],
    ["tk-op", /[+\-*/%=<>!&|^~?:]+/],
    ["tk-op", /[{}()\[\];,.]/],
  ],
};
HL_RULES.ts = HL_RULES.js;
HL_RULES.c = HL_RULES.cpp;
HL_RULES.cc = HL_RULES.cpp;
HL_RULES.cxx = HL_RULES.cpp;

const HL_CACHE = {};
function hlCompile(lang) {
  if (lang in HL_CACHE) return HL_CACHE[lang];
  const rules = HL_RULES[lang];
  if (!rules) { HL_CACHE[lang] = null; return null; }
  const src = rules.map((r) => "(" + r[1].source + ")").join("|");
  HL_CACHE[lang] = { rx: new RegExp(src, "gm"), cls: rules.map((r) => r[0]) };
  return HL_CACHE[lang];
}

/** 把代码变成带 <span class="tk-*"> 的 HTML（字符数不变，行数不变）。 */
function codeHighlightHtml(code, lang) {
  const t = hlCompile(lang);
  if (!t) return esc(code);
  const rx = t.rx;
  rx.lastIndex = 0;
  let out = "", last = 0, m;
  while ((m = rx.exec(code)) !== null) {
    if (m.index > last) out += esc(code.slice(last, m.index));
    let gi = 0;
    for (let i = 1; i < m.length; i++) { if (m[i] !== undefined) { gi = i - 1; break; } }
    out += `<span class="${t.cls[gi]}">${esc(m[0])}</span>`;
    last = m.index + m[0].length;
    if (m[0].length === 0) rx.lastIndex++;
  }
  if (last < code.length) out += esc(code.slice(last));
  return out;
}

function codeLangOf(path) {
  const n = String(path || "").toLowerCase();
  const ext = n.slice(n.lastIndexOf(".") + 1);
  if (["js", "mjs", "cjs", "jsx", "ts", "tsx", "mts", "cts"].includes(ext)) return "js";
  if (ext === "json") return "json";
  if (["html", "htm", "xml", "svg", "vue"].includes(ext)) return "html";
  if (["css", "scss", "less"].includes(ext)) return "css";
  if (["py", "pyw"].includes(ext)) return "py";
  if (["md", "markdown"].includes(ext)) return "md";
  if (["sh", "bash", "zsh"].includes(ext)) return "sh";
  if (["c", "h"].includes(ext)) return "c";
  if (["cc", "cpp", "cxx", "c++", "hh", "hpp", "hxx", "h++", "inl", "ipp", "tcc"].includes(ext)) return "cpp";
  return "plain";
}

const CODE_HL_MAX = 200000;       // 超过这个长度就不高亮了（保持流畅）

const CODE_W_KEY = "cocraft.code.w";
const CODE_W_DEFAULT = 760;
const CODE_TERM_H_KEY = "cocraft.code.term.h";
const CODE_TERM_H_DEFAULT = 240;

const CODE = {
  open: false, root: "", data: {}, expanded: {}, sel: "", dirty: false, lang: "plain",
};

function codeSetStatus(text, dirty) {
  const el = $("code-status");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("dirty", !!dirty);
}

function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  return (n / 1048576).toFixed(2) + " MB";
}

/** 用高亮层重画（textarea 文字透明，颜色来自下层 <pre>）。 */
function codeRenderHighlight() {
  const ed = $("code-editor"), pre = $("code-highlight");
  if (!ed || !pre) return;
  const code = ed.value;
  // 末尾补一个换行，让最后一行在 <pre> 里也有高度（与 textarea 对齐）
  if (ed.disabled || code.length > CODE_HL_MAX) pre.innerHTML = esc(code) + "\n";
  else pre.innerHTML = codeHighlightHtml(code, CODE.lang) + "\n";
  pre.scrollTop = ed.scrollTop;
  pre.scrollLeft = ed.scrollLeft;
}

let codeHlTimer = 0;
function codeScheduleHighlight() {
  if (codeHlTimer) return;
  codeHlTimer = setTimeout(() => { codeHlTimer = 0; codeRenderHighlight(); }, 60);
}

function codeToggle() { CODE.open ? codeClose() : codeOpen(); }

function codeOpen() {
  const root = currentDir();
  if (!root) { setBanner("当前会话没有工作目录：新建会话时可以指定一个"); return; }
  try { if (typeof MUS !== "undefined" && MUS.homeOpen) closeMusicHome(); } catch { /* ignore */ }
  try { if (cocraftIsOpen()) setCocraft(false); } catch { /* ignore */ }  // 两个都是右侧抽屉 → 互斥
  const box = $("code");
  if (box) { box.classList.add("open"); box.setAttribute("aria-hidden", "false"); }
  document.documentElement.classList.add("code-open");     // ← 分屏：面板内容让位
  CODE.open = true;
  syncToolsBtn();
  { const ml = $("code-menu-list"); if (ml) ml.hidden = true; }   // 收起上次没关的合并菜单
  codeResetTo(root);       // 每次打开都从「当前会话目录」开始（可用「打开文件夹」临时换）
  syncDrawerOverlap();
}

/** ★从**会话列表的文件树**打开：指定根目录 + 文件（不依赖「当前会话目录」）。
 *  ⚠ 先把上一个文件挂起的自动保存补交掉 —— `codeResetTo()` 会清掉编辑器，别静默丢改动。 */
async function codeOpenAt(root, path) {
  if (CODE.open && CODE.dirty) {
    await codeFlushSave();
    if (CODE.dirty && !confirm("上一个文件还有没保存的改动，仍要打开这个文件吗？")) return;
  }
  try { if (typeof MUS !== "undefined" && MUS.homeOpen) closeMusicHome(); } catch { /* ignore */ }
  try { if (cocraftIsOpen()) setCocraft(false); } catch { /* ignore */ }   // 都是右侧抽屉 → 互斥
  const box = $("code");
  if (box) { box.classList.add("open"); box.setAttribute("aria-hidden", "false"); }
  document.documentElement.classList.add("code-open");     // 分屏：面板内容让位
  CODE.open = true;
  syncToolsBtn();
  { const ml = $("code-menu-list"); if (ml) ml.hidden = true; }
  codeResetTo(root);
  syncDrawerOverlap();
  if (path) await codeOpenFile(path);       // ★空 path = 只打开项目根目录（列文件树）
  renderSessionTrees();          // 把左侧树里「当前打开的那个文件」高亮上
}

/** 把编码模式切到某个根目录：清空文件树 / 编辑器状态，重新列目录。 */
function codeResetTo(dir) {
  CODE.root = dir;
  CODE.data = {}; CODE.expanded = {}; CODE.sel = ""; CODE.dirty = false;
  const r = $("code-root");
  if (r) { r.textContent = dir; r.title = dir; }
  const ed = $("code-editor");
  if (ed) { ed.value = ""; ed.disabled = true; ed.placeholder = "← 从左边选一个文件"; }
  const pre = $("code-highlight"); if (pre) pre.innerHTML = "";
  const p = $("code-path"); if (p) { p.textContent = "未打开文件"; p.title = ""; }
  const wrap = document.querySelector(".code-edit-wrap"); if (wrap) wrap.classList.remove("disabled");
  codeSetStatus("");
  codeLoadDir(dir);
  // 若「run」终端正开着，让它跟着换到新目录（重开一次 cwd 才是新根）
  if (TERM.embedded && TERM.open && TERM.id) termRestart();
}

/** 「打开文件夹」：原生选文件夹框 → 换掉编码模式的根目录。 */
async function codeOpenFolder() {
  try {
    const r = await api("/pick/folder", { method: "POST" });
    if (r && r.ok && r.path) {
      if (r.path === CODE.root) return;
      if (CODE.dirty) {
        await codeFlushSave();
        if (CODE.dirty && !confirm("自动保存失败，切换文件夹会放弃改动，继续？")) return;
      }
      codeResetTo(r.path);
    } else if (r && r.canceled) {
      /* 用户取消，什么都不做 */
    } else {
      setBanner("打开文件夹失败：" + ((r && r.error) || "未知错误"));
    }
  } catch (err) {
    setBanner("打开文件夹失败：" + (err && err.message ? err.message : err));
  }
}

async function codeClose() {
  if (CODE.dirty) {                          // 关之前先把挂起的自动保存补交掉
    await codeFlushSave();
    if (CODE.dirty && !confirm("自动保存失败，仍要关闭编码模式并放弃改动吗？")) return;
  }
  if (TERM.embedded) codeTermOff();          // 关编码模式 → 把嵌入的终端也收回去
  const box = $("code");
  if (box) { box.classList.remove("open"); box.setAttribute("aria-hidden", "true"); }
  document.documentElement.classList.remove("code-open");
  CODE.open = false;
  syncToolsBtn();
  { const ml = $("code-menu-list"); if (ml) ml.hidden = true; }   // 关抽屉顺手收起菜单
  syncDrawerOverlap();
}

/* ---- 内嵌终端：在编码抽屉底部嵌入终端（宽度 = 抽屉宽度，高度可拖） ---- */
function syncCodeRunBtn() {
  const b = $("code-menu");                  // 终端开着时把「菜单」按钮点亮
  if (b) b.classList.toggle("on", !!(TERM.embedded && TERM.open));
}

/** 允许代码：把 #term 移到编码抽屉底部的槽里，并确保终端已启动。 */
async function codeTermOn() {
  if (TERM.embedded && TERM.open) return;
  if (!(await termComponentOk())) return;
  const slot = $("code-term-slot"), term = $("term");
  if (!slot || !term) return;
  const wasOpen = TERM.open;
  TERM.embedded = true;
  slot.hidden = false;
  if (wasOpen) termClose();                  // 已开着（底部抽屉）→ 先收起，保证走完整“打开”路径
  if (term.parentElement !== slot) slot.appendChild(term);
  let h = 0;
  try { h = parseInt(localStorage.getItem(CODE_TERM_H_KEY) || "", 10) || 0; } catch { /* 隐私模式 */ }
  termApplyHeight(h || CODE_TERM_H_DEFAULT);
  await termOpen();
  syncCodeRunBtn();
}

/** 关掉「允许代码」：收起终端并放回 body（PTY 进程保留，下次秒开）。 */
function codeTermOff() {
  const term = $("term"), slot = $("code-term-slot");
  TERM.embedded = false;
  if (term) term.classList.remove("embedded");
  termClose();
  if (term && term.parentElement !== document.body) document.body.appendChild(term);
  if (slot) slot.hidden = true;
  syncCodeRunBtn();
}

/** 菜单项「新建终端」：没开就开一个；已经开着就重开一个新的。 */
async function codeNewTerminal() {
  if (!(await termComponentOk())) return;
  if (TERM.embedded && TERM.open) {
    if (TERM.id && !TERM.dead) await termRestart();
  } else {
    await codeTermOn();
  }
  termFocus();
}

/* ---- 「运行」：按扩展名挑解释器，把命令送进「run」终端（先存盘） ---- */
function codeRunCommand(path) {
  const s = String(path || "");
  const ext = s.slice(s.lastIndexOf(".") + 1).toLowerCase();
  const q = '"' + s.replace(/"/g, '""') + '"';           // 路径可能有空格 → 一律加引号
  const TABLE = {
    py:   ["Python", "python " + q],
    pyw:  ["Python", "python " + q],
    js:   ["Node", "node " + q],
    mjs:  ["Node", "node " + q],
    cjs:  ["Node", "node " + q],
    ps1:  ["PowerShell", "pwsh -NoProfile -File " + q],
    bat:  ["cmd", "cmd /c " + q],
    cmd:  ["cmd", "cmd /c " + q],
    sh:   ["bash", "bash " + q],
    bash: ["bash", "bash " + q],
    c:    ["C（gcc）", "gcc " + q + " -o .cocraft-run.exe && .\\.cocraft-run.exe"],
    cc:   ["C++（g++）", "g++ " + q + " -o .cocraft-run.exe && .\\.cocraft-run.exe"],
    cpp:  ["C++（g++）", "g++ " + q + " -o .cocraft-run.exe && .\\.cocraft-run.exe"],
    cxx:  ["C++（g++）", "g++ " + q + " -o .cocraft-run.exe && .\\.cocraft-run.exe"],
  };
  const hit = TABLE[ext];
  return hit ? { label: hit[0], cmd: hit[1] } : null;
}

/** 「运行」按钮：存盘 → 确保 run 终端就绪 → 把命令 + Enter 送进去。 */
async function codeExec() {
  if (!CODE.open) { setBanner("先打开编码模式（顶栏扳手 → 编码模式）"); return; }
  if (!CODE.sel) { setBanner("先在左边打开一个文件，再点「运行」"); return; }
  const ed = $("code-editor");
  if (ed && ed.disabled) { setBanner("二进制文件不能直接运行"); return; }
  const spec = codeRunCommand(CODE.sel);
  if (!spec) {
    setBanner("这个文件类型不能直接运行（点 run 打开终端可手动执行）");
    return;
  }
  if (CODE.dirty) {                              // 先把挂起的自动保存补交掉，保证跑的是刚改的内容
    await codeFlushSave();
    if (CODE.dirty) { setBanner("自动保存失败，已取消运行"); return; }
  }
  try {
    if (!(TERM.embedded && TERM.open)) await codeTermOn();
    else if (TERM.dead) await termRestart();
  } catch { /* codeTermOn / termOpen 内部已提示 */ }
  if (!TERM.id || TERM.dead) { setBanner("终端没就绪，无法运行（可点 run 查看原因）"); return; }
  termSendInput(spec.cmd + "\r");
  termFocus();
  setBanner("运行（" + spec.label + "）：" + spec.cmd);
}

async function codeLoadDir(path) {
  try {
    const r = await api(`/fs/list?root=${encodeURIComponent(CODE.root)}&path=${encodeURIComponent(path)}`);
    CODE.data[path] = (r.data && r.data.items) || [];
    if (path === CODE.root) CODE.expanded[path] = true;
    codeRenderTree();
  } catch (err) {
    if (path === CODE.root) {
      $("code-tree").innerHTML = `<div class="empty">读目录失败：${esc(err.message)}</div>`;
    } else {
      setBanner("读目录失败：" + err.message);
    }
  }
}

function codeRenderTree() {
  const box = $("code-tree");
  if (!box) return;
  const items = CODE.data[CODE.root];
  if (!items) { box.innerHTML = `<div class="empty">加载中…</div>`; return; }
  let html = "";
  const walk = (list, depth) => {
    for (const it of list) {
      const pad = 6 + depth * 12;
      if (it.dir) {
        const open = !!CODE.expanded[it.path];
        html += `<div class="tn" data-dir="${esc(it.path)}" style="padding-left:${pad}px" title="${esc(it.path)}">`
          + `<span class="tw ico">${treeIco(true, open)}</span><span class="nm">${esc(it.name)}</span></div>`;
        if (open) {
          const kids = CODE.data[it.path];
          if (kids) walk(kids, depth + 1);
          else html += `<div class="empty" style="padding-left:${pad + 12}px">…</div>`;
        }
      } else {
        const on = it.path === CODE.sel ? " on" : "";
        const dirty = it.path === CODE.sel && CODE.dirty ? " dirty" : "";
        html += `<div class="tn${on}${dirty}" data-file="${esc(it.path)}" style="padding-left:${pad}px" `
          + `title="${esc(it.path)}"><span class="tw"></span><span class="nm">${esc(it.name)}</span></div>`;
      }
    }
  };
  walk(items, 0);
  box.innerHTML = html || `<div class="empty">空目录</div>`;
}

async function codeOpenFile(path) {
  if (CODE.dirty) {                          // 切文件前先把挂起的自动保存补交掉
    await codeFlushSave();
    if (CODE.dirty && !confirm("自动保存失败，放弃改动并打开别的文件？")) return;
  }
  const ed = $("code-editor");
  try {
    const r = await api(`/fs/read?root=${encodeURIComponent(CODE.root)}&path=${encodeURIComponent(path)}`);
    const d = r.data || {};
    ed.disabled = !!d.binary;
    ed.value = d.binary ? "" : (d.text || "");
    ed.placeholder = d.binary ? "（二进制文件，不能在这里编辑）" : "";
    CODE.sel = path;
    CODE.dirty = false;
    CODE.lang = codeLangOf(path);
    const wrap = document.querySelector(".code-edit-wrap");
    if (wrap) wrap.classList.toggle("disabled", !!d.binary);
    codeSetStatus(d.binary ? "二进制" : (fmtBytes(d.size) + " · " + CODE.lang), false);
    const p = $("code-path");
    if (p) { p.textContent = path; p.title = path; }
    codeRenderTree();
    codeRenderHighlight();
    if (!d.binary) setTimeout(() => { try { ed.focus(); } catch { /* ignore */ } }, 30);
  } catch (err) {
    setBanner("打开文件失败：" + err.message);
  }
}

async function codeSave() {
  if (!CODE.sel) return;
  const ed = $("code-editor");
  if (!ed || ed.disabled) return;
  try {
    const r = await api("/fs/write", {
      method: "POST",
      body: JSON.stringify({ root: CODE.root, path: CODE.sel, text: ed.value }),
    });
    CODE.dirty = false;
    codeSetStatus("已保存 · " + fmtBytes((r.data || {}).size), false);
    codeRenderTree();
  } catch (err) {
    setBanner("自动保存失败：" + err.message);
  }
}

/* ---- 自动保存：编辑后防抖写回（没有「保存」按钮了；Ctrl+S 仍可立即保存） ---- */
let codeSaveTimer = 0;
function codeScheduleSave() {
  if (codeSaveTimer) clearTimeout(codeSaveTimer);
  codeSaveTimer = setTimeout(() => { codeSaveTimer = 0; codeSave(); }, 700);
}

/** 立刻补交挂起的自动保存（切文件 / 关抽屉 / 换目录 / 运行前调用）。 */
async function codeFlushSave() {
  if (codeSaveTimer) { clearTimeout(codeSaveTimer); codeSaveTimer = 0; }
  if (CODE.dirty) await codeSave();
}

/* ---- 抽屉宽度可拖动（跟群聊一样：拖左边缘，落 localStorage） ---- */
function codeApplyWidth(w) {
  const z = uiZoom();
  const max = Math.max(300, (window.innerWidth - 300) / z);   // 给面板至少留 300px
  const nw = Math.max(300, Math.min(w, max));
  document.documentElement.style.setProperty("--code-w", nw + "px");
}

function codeInitResize() {
  const box = $("code"), grip = $("code-resize");
  if (!box || !grip) return;
  let dragging = false, startX = 0, startW = 0;
  grip.addEventListener("pointerdown", (e) => {
    dragging = true; startX = e.clientX; startW = box.getBoundingClientRect().width / uiZoom();
    box.classList.add("resizing");
    document.documentElement.classList.add("code-resizing");
    try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (dragging) codeApplyWidth(startW + (startX - e.clientX) / uiZoom());   // 往左拖 = 变宽
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false; box.classList.remove("resizing");
    document.documentElement.classList.remove("code-resizing");
    try { grip.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    try { localStorage.setItem(CODE_W_KEY, String(Math.round(box.getBoundingClientRect().width))); } catch { /* 隐私模式 */ }
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

function initCode() {
  const on = (id, fn) => { const el = $(id); if (el) el.addEventListener("click", fn); };
  on("code-close", codeClose);
  on("code-exec", codeExec);
  on("code-refresh", () => {
    if (!CODE.root) return;
    CODE.data = {}; CODE.expanded = {};
    codeLoadDir(CODE.root);
  });
  // 合并菜单：打开文件夹 / 新建终端 / 在 VS Code 打开
  const mbtn = $("code-menu"), mlist = $("code-menu-list");
  if (mbtn && mlist) {
    mbtn.addEventListener("click", (e) => { e.stopPropagation(); mlist.hidden = !mlist.hidden; });
    mlist.addEventListener("click", (e) => {
      const b = e.target.closest("button"); if (!b) return;
      e.stopPropagation(); mlist.hidden = true;
      if (b.id === "cm-folder") codeOpenFolder();
      else if (b.id === "cm-term") codeNewTerminal();
      else if (b.id === "cm-vsc") openCurrentDir("editor", CODE.root);
    });
    document.addEventListener("click", (e) => {
      if (!mlist.hidden && !mlist.contains(e.target) && !mbtn.contains(e.target)) mlist.hidden = true;
    });
  }

  const tree = $("code-tree");
  if (tree) tree.addEventListener("click", (e) => {
    const d = e.target.closest("[data-dir]");
    if (d) {
      const p = d.dataset.dir;
      CODE.expanded[p] = !CODE.expanded[p];
      if (CODE.expanded[p] && !CODE.data[p]) codeLoadDir(p);
      else codeRenderTree();
      return;
    }
    const f = e.target.closest("[data-file]");
    if (f) codeOpenFile(f.dataset.file);
  });

  const ed = $("code-editor");
  if (ed) {
    ed.addEventListener("input", () => {
      if (ed.disabled) return;
      CODE.dirty = true;
      codeSetStatus("● 未保存", true);
      codeScheduleHighlight();
      codeScheduleSave();
      codeRenderTree();
    });
    ed.addEventListener("scroll", () => {
      const pre = $("code-highlight");
      if (pre) { pre.scrollTop = ed.scrollTop; pre.scrollLeft = ed.scrollLeft; }
    });
    ed.addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault(); codeSave(); return;
      }
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
        e.preventDefault(); codeExec(); return;    // Ctrl+Enter：运行当前文件
      }
      if (ed.disabled) return;
      if (e.ctrlKey || e.metaKey || e.altKey) return;   // 其它组合键交给浏览器/系统
      if (e.isComposing || e.keyCode === 229) return;   // 输入法组合中

      const val = ed.value, s = ed.selectionStart, en = ed.selectionEnd;
      const PAIRS = { "(": ")", "[": "]", "{": "}", '"': '"', "'": "'", "`": "`" };
      const CLOSERS = { ")": 1, "]": 1, "}": 1, '"': 1, "'": 1, "`": 1 };
      // 就地替换并同步「脏标记 + 语法高亮」
      const apply = (text, caret) => {
        ed.value = text; ed.selectionStart = ed.selectionEnd = caret;
        ed.dispatchEvent(new Event("input"));
      };

      if (e.key === "Tab") {                            // Tab 插制表符，不跳焦点
        e.preventDefault();
        apply(val.slice(0, s) + "\t" + val.slice(en), s + 1);
        return;
      }

      // 光标后已经是同一个闭括号 → 直接跳过（不重复输入）
      if (s === en && CLOSERS[e.key] && val.slice(s, s + 1) === e.key) {
        e.preventDefault();
        ed.selectionStart = ed.selectionEnd = s + 1;
        return;
      }

      if (PAIRS[e.key]) {                               // 括号 / 引号自动配对
        e.preventDefault();
        const ch = e.key, close = PAIRS[ch];
        const nx = val.slice(s, s + 1);
        if (s !== en) {                                 // 有选区：把选区包起来
          apply(val.slice(0, s) + ch + val.slice(s, en) + close + val.slice(en), en + 1);
        } else if ((ch === '"' || ch === "'" || ch === "`") && /[A-Za-z0-9_$]/.test(nx)) {
          apply(val.slice(0, s) + ch + val.slice(s), s + 1);   // 引号紧挨单词：不补全
        } else {
          apply(val.slice(0, s) + ch + close + val.slice(s), s + 1);
        }
        return;
      }

      if (e.key === "Enter") {                          // 自动缩进（承接缩进；开括号后多缩一级）
        e.preventDefault();
        const lineStart = val.lastIndexOf("\n", s - 1) + 1;
        const indent = (val.slice(lineStart).match(/^[ \t]*/) || [""])[0];
        const before = val.slice(0, s).replace(/[ \t]+$/, "");
        const lastCh = before.slice(-1), nx = val.slice(s, s + 1);
        const extra = (lastCh === "{" || lastCh === "(" || lastCh === "[") ? "\t" : "";
        const pair = (lastCh === "{" && nx === "}") || (lastCh === "(" && nx === ")") || (lastCh === "[" && nx === "]");
        if (pair) {
          const ins = "\n" + indent + extra + "\n" + indent;
          apply(val.slice(0, s) + ins + val.slice(en), s + 1 + indent.length + extra.length);
        } else {
          const ins = "\n" + indent + extra;
          apply(val.slice(0, s) + ins + val.slice(en), s + ins.length);
        }
        return;
      }

      if (e.key === "Backspace" && s === en && s > 0) {  // 成对删除
        const a = val.slice(s - 1, s), b = val.slice(s, s + 1);
        if (PAIRS[a] === b) { e.preventDefault(); apply(val.slice(0, s - 1) + val.slice(s + 1), s - 1); }
      }
    });
  }

  codeInitResize();
  let cw = 0;
  try { cw = parseInt(localStorage.getItem(CODE_W_KEY), 10) || 0; } catch { /* 隐私模式 */ }
  codeApplyWidth(cw || CODE_W_DEFAULT);
  syncCodeRunBtn();
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && CODE.open) codeClose(); });
}

/* ============================================================================
   软件更新（GitHub Releases）：检查 / 一键静默更新（装完自动重启面板）
   ============================================================================ */

let UPD = { busy: false, latest: "", phase: "", lastError: "" };

/** 设置里「软件更新」那一行的文案：**进度 / 失败原因一直可见**。
 *  ⚠ 广播条会自动收（0.1.53 起非点击类型 5s），光靠它用户根本看不到失败原因 ——
 *    而这条更新失败是**可操作**的信息，必须留一个长期可见的地方。 */
function updNote(checkText) {
  const note = $("upd-note");
  if (!note) return;
  const bits = [];
  if (UPD.phase) bits.push(UPD.phase);
  if (UPD.lastError) bits.push("⚠ " + UPD.lastError);
  if (checkText) bits.push(checkText);
  const s = bits.join("\u3000·\u3000") || "安装版可从 GitHub Releases 一键更新到最新；开发环境不支持";
  note.textContent = s;
  note.title = s;
}

async function updCheck(force) {
  const cur = $("upd-cur"), apply = $("upd-apply"), chk = $("upd-check");
  if (chk) chk.disabled = true;
  try {
    const r = await api("/update/check" + (force ? "?force=1" : ""));
    const d = (r && r.data) || {};
    if (cur) { cur.textContent = d.current ? ("v" + d.current) : "开发环境"; cur.title = d.current || ""; }
    if (!d.supported) {
      updNote(d.reason || "开发环境 / 非安装版，不支持在线更新");
      if (apply) apply.hidden = true;
      return d;
    }
    if (!d.ok) {
      updNote(d.reason || "检查失败");
      if (apply) apply.hidden = true;
      return d;
    }
    if (d.hasUpdate) {
      UPD.latest = d.latest || "";
      updNote("发现新版本 v" + d.latest + "（当前 v" + d.current + "）");
      if (apply) { apply.hidden = false; apply.textContent = "立即更新到 v" + d.latest; }
      if (force) setBanner("软件有新版本 v" + d.latest + "：点「立即更新」自动更新并重启");
    } else {
      updNote("已是最新版本（v" + d.current + "）");
      if (apply) apply.hidden = true;
    }
    return d;
  } catch (err) {
    UPD.lastError = "检查失败：" + (err && err.message ? err.message : err);
    updNote();
    if (apply) apply.hidden = true;
    return null;
  } finally {
    if (chk) chk.disabled = false;
  }
}

async function updApply() {
  if (UPD.busy) return;
  const latest = UPD.latest ? ("v" + UPD.latest) : "最新版";
  if (!confirm("下载并安装 " + latest + " 吗？\n安装是静默的，装完面板会自动重启。")) return;
  UPD.busy = true;
  UPD.lastError = "";
  const apply = $("upd-apply");
  if (apply) { apply.disabled = true; apply.textContent = "正在下载…"; }
  // ★ 下载 ~200MB 要好几分钟：广播条必须**常驻**，否则 5 秒后自己消失，用户看着像"没反应"
  UPD.phase = "正在下载并静默更新（约 200 MB，请勿关闭面板）…";
  setBanner(UPD.phase, { sticky: true });
  updNote();
  updPollStatus(true);
  try {
    const r = await api("/update/apply", { method: "POST" });
    if (apply) apply.textContent = "更新中…（装完自动重启）";
    UPD.phase = (r && r.message) || "已开始更新…";
    setBanner(UPD.phase + (r && r.source ? "（下载源：" + r.source + "）" : ""), { sticky: true });
    updNote();
  } catch (err) {
    UPD.busy = false;
    updPollStatus(false);
    if (apply) { apply.disabled = false; apply.textContent = "立即更新"; }
    UPD.phase = "";
    UPD.lastError = "更新失败：" + (err && err.message ? err.message : err);
    setBanner(UPD.lastError + "（详见 设置 →「软件更新」）", { sticky: true });   // 原因常驻，别 5 秒就没了
    updNote();
  }
}

/** 更新期间每 2s 看一次后端阶段（下载中 / 安装中），把进度写进设置那一行 + 广播条。 */
let updPollT = null;
function updPollStatus(on) {
  if (!on) { if (updPollT) { POLL.clear("upd-poll"); updPollT = null; } return; }
  if (updPollT) return;
  const PH = { downloading: "正在下载安装包…", installing: "正在安装（面板即将自动重启）…",
               started: "正在安装（面板即将自动重启）…" };
  const tick = async () => {
    try {
      const r = await api("/update/status");
      const d = (r && r.data) || {};
      if (d.phase === "done") {
        UPD.phase = "已更新完成"; UPD.lastError = "";
        setBanner("已更新到 v" + (d.version || "?") + (d.fromVersion ? "（原 v" + d.fromVersion + "）" : ""),
                  { sticky: true });
        updNote(); updPollStatus(false); return;
      }
      if (d.phase === "failed") {
        UPD.phase = ""; UPD.lastError = "上次更新失败：" + (d.error || "未知原因");
        setBanner(UPD.lastError + "（详见 设置 →「软件更新」）", { sticky: true });
        updNote(); updPollStatus(false); return;
      }
      const label = PH[d.phase];
      if (label) {
        UPD.phase = label + "（约 200 MB，请勿关闭面板）";
        setBanner(UPD.phase, { sticky: true });
        updNote();
      }
    } catch { /* 面板正在被安装器杀掉 → 忽略 */ }
  };
  updPollT = POLL.interval("upd-poll", 2000, tick);
  tick();
}

/** GitHub Token 那一行：**只显示掩码**（明文不进界面，跟「模型 API Key」同一个规矩）。 */
async function updLoadToken() {
  const v = $("upd-token-var");
  try {
    const r = await api("/update/token");
    const d = (r && r.data) || {};
    if (v) {
      v.textContent = d.set ? (d.mask + (d.source === "env" ? "（环境变量）" : "")) : "未设置";
      v.title = d.set ? "已设置：更新检查额度 5000 次/小时" : "未设置：匿名只有 60 次/小时/IP";
    }
  } catch {
    if (v) v.textContent = "读取失败";
  }
}

async function updSaveToken() {
  const inp = $("upd-token-value");
  const btn = $("upd-token-save");
  if (!inp) return;
  const t = String(inp.value || "").trim();
  if (btn) btn.disabled = true;
  try {
    await api("/update/token", { method: "POST", body: JSON.stringify({ token: t }) });
    inp.value = "";
    await updLoadToken();
    setBanner(t ? "已保存 GitHub Token：更新检查额度提到 5000 次/小时" : "已清除 GitHub Token", { ms: 3000 });
    updCheck(true);                        // 后端已清缓存 → 立刻重查一次
  } catch (err) {
    setBanner("保存 GitHub Token 失败：" + (err && err.message ? err.message : err), { sticky: true });
  } finally {
    if (btn) btn.disabled = false;
  }
}

function initUpdate() {
  const chk = $("upd-check"), apply = $("upd-apply");
  if (chk) chk.addEventListener("click", () => updCheck(true));
  if (apply) apply.addEventListener("click", updApply);
  const ts = $("upd-token-save"), tv = $("upd-token-value");
  if (ts) ts.addEventListener("click", updSaveToken);
  if (tv) tv.addEventListener("keydown", (e) => { if (e.key === "Enter") updSaveToken(); });
  updLoadToken();                          // 只回掩码
  // 后台自动查一次（不打扰）；有新版就只在横幅提示，去设置里点更新
  setTimeout(() => {
    updReportStatus();                     // 先报告"上一次更新"的结果（如有）
    updCheck(false).then((d) => {
      if (d && d.hasUpdate && d.latest) {
        setBanner("软件有新版本 v" + d.latest + "：设置 →「软件更新」可一键更新");
      }
    }).catch(() => {});
  }, 5000);
}

/** 报告"上一次更新"的结果：安装器是独立进程，装完/失败我们只能靠状态文件知道（读完就清）。 */
async function updReportStatus() {
  try {
    const r = await api("/update/status");
    const d = (r && r.data) || {};
    const phase = d.phase;
    if (!phase) return;
    if (phase === "done") {
      UPD.phase = "已更新到 v" + (d.version || "?") + (d.fromVersion ? "（原 v" + d.fromVersion + "）" : "");
      setBanner(UPD.phase, { sticky: true });
    } else if (phase === "failed") {
      UPD.lastError = "上次更新失败：" + (d.error || "未知原因");
      setBanner(UPD.lastError + "（详见 设置 →「软件更新」）", { sticky: true });
    } else {
      UPD.lastError = "上次更新似乎未完成（" + phase + "）：" + (d.error || "可能被中断");
      setBanner(UPD.lastError + "；可到 设置 →「软件更新」重试", { sticky: true });
    }
    updNote();                                 // ★ 同时落到设置那一行（长期可见，广播条会收）
    await api("/update/ack", { method: "POST" });
  } catch { /* ignore */ }
}

/* ================================================================================
   ★本地协作工作台（本机多 agent；不登录、不经过云端）
   ================================================================================
   后端：backend/orchestrator.py + /orchestrate/*
   入口：顶栏「协作」→ 全页工作台（像音乐主页那样替换消息区）
   形态：左栏运行历史；右栏任务头 + 待批权限（逐条）+ 成员列（并排）+ 逐行差异/仲裁
   轮询：只有任务还在跑时才 1.5s 拉一次，完成即停（别一直打接口）。
   ⚠ 三条红线：① 不给「一键全放行」（权限逐条）；② 破坏性操作永远要确认；
               ③ 只读/展示为主，撤销/恢复入口复用现有接口。
   ================================================================================ */

const COLLAB = {
  open: false, cat: null, catErr: "", runs: [], runsErr: "", busy: false,
  run: null, runId: "", runErr: "", poll: 0, tickBusy: false, wasActive: false,
  arbEngine: "", pickId: "", diffOpen: false,
  memSel: "", memAll: false, permOpen: false,        // ★运行详情：只看选中的成员 / 权限抽屉
  projFilter: "",                                        // ★左栏「按项目筛选」：空 = 全部
  runsPoll: 0, runsBusy: false, runsSeq: 0,
  // ★工作流（0.2.11）：可复用配置 + 画布
  view: "wf",                 // "wf" | "run"
  workflows: [], wfErr: "", wfId: "", wfBusy: false, wfSeq: 0,
  wfArchOpen: false,          // ★左栏「已归档」折叠区（P3）
  splitHinted: {},            // ★「这个工作流还是单阶段」提示只弹一次（按工作流 id）
  runWfFilter: "",            // 运行历史只看某个工作流
  canvas: { zoom: 1, pan: { x: 20, y: 14 }, nodes: {}, sel: "", drag: null, panning: null,
            saveTimer: 0 },
};

const COLLAB_ST = {
  wait: "等待中", running: "运行中", done: "完成", failed: "失败",
  timeout: "超时", cancelled: "已取消", skipped: "已跳过",
  partial: "部分完成",
};
const COLLAB_DEC = {
  once: "允许一次", always: "始终允许", reject: "拒绝",
  allowed: "已允许", denied: "已拒绝", timeout: "已超时", pending: "待批准",
};

// ★节点级权限等级（后端 catalog 会回同一份；这里留兜底，后端没重启也能用）
const COLLAB_PERM_FALLBACK = [
  { id: "", label: "跟随面板", note: "用面板当前等级" },
  { id: "ask", label: "全部询问", note: "每条命令都问" },
  { id: "read", label: "只读自动", note: "读自动；写/执行要问" },
  { id: "edit", label: "读写自动", note: "工作区内读写自动" },
  { id: "exec", label: "执行自动", note: "工作区内读写执行自动" },
  { id: "trust", label: "信任（非破坏性自动）", note: "不再逐条问；破坏性仍然必须确认" },
];

function collabPermLevels() {
  const l = (COLLAB.cat && COLLAB.cat.permLevels) || [];
  return (l.length ? l : COLLAB_PERM_FALLBACK);
}

function collabStText(s) { return COLLAB_ST[String(s || "")] || String(s || "—"); }
function collabAgents() { return (COLLAB.cat && COLLAB.cat.agents) || []; }
function collabAvailAgents() { return collabAgents().filter((a) => a && a.available); }
function collabDefaultEngine() {
  const a = collabAvailAgents()[0];
  return a ? String(a.id) : "";
}
function collabRoleLabel(role) {
  const r = COLLAB.cat && COLLAB.cat.roles && COLLAB.cat.roles[role];
  return (r && r.label) || "";
}
function collabRunActive(run) {
  if (!run) return false;
  if (String(run.status || "") === "running") return true;
  if ((run.members || []).some((m) => m.status === "wait" || m.status === "running")) return true;
  return !!(run.arbitration && run.arbitration.status === "running");
}
function collabFindMemberName(id, fallback) {
  const run = COLLAB.run;
  const m = run && (run.members || []).find((x) => String(x.id) === String(id));
  return (m && m.name) || fallback || String(id || "");
}

function collabIsOpen() {
  const bs = $("body-stage");
  return !!bs && bs.classList.contains("collab-open");
}
