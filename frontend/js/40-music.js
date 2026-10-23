/* 40-music.js —— 音乐 / 歌词气泡球 / 频谱（P1 从 app.js 拆出，纯搬运不改逻辑） */

/* ---------------- QQ音乐联动 ----------------
 * 后端：/qq/state（曲目/状态/音量/频谱可用性）、/qq/control、/qq/volume、/qq/lyrics、
 *       /qq/search、/qq/app（启停客户端）、/qq/spectrum（真频谱 12 个频段）
 * 侧栏收起时，画面中央显示滚动歌词。 */

const MUSIC = {
  // lyrWanted：歌词浮层的「用户意愿」三态 —— null=自动（收起侧栏才显示）/ true=强制显示 / false=明确关闭
  //（以前只有 true/false，收起侧栏时被 collapsed-focus 强制显示且「词」按钮被隐藏 → 关不掉）
  on: false, qqRunning: false, real: false, folded: false, shown: false, panel: null, panelError: "", lyrWanted: null,
  seekDragging: false, cur: 0, dur: 0,
  state: {}, lyrics: [], lyrKey: "", lyIdx: -1,
  posAt: 0, posStale: false, vol: 100, volDragging: false, volTimer: null,
};
const LYR_PREF_KEY = "cocraft.lyrics.wanted";     // "1"/"0"/""（空=自动）

function loadLyricsPref() {
  try {
    const v = localStorage.getItem(LYR_PREF_KEY);
    if (v === "1") return true;
    if (v === "0") return false;
  } catch { /* 隐私模式 */ }
  return null;
}
function saveLyricsPref() {
  try {
    localStorage.setItem(LYR_PREF_KEY,
      MUSIC.lyrWanted === true ? "1" : (MUSIC.lyrWanted === false ? "0" : ""));
  } catch { /* 隐私模式 */ }
}

/** QQ音乐 的 SMTC 状态不一定报 Playing（实测见过 Opened / Paused），
 *  所以「跳动」的判定放宽：只要不是在暂停/停止，就认为在放。 */
/** 当前在放什么：面板内播放（网易云/QQ 搜索）优先，否则 QQ音乐 SMTC */
function currentTrack() {
  if (MUSIC.panel) return { title: MUSIC.panel.name || "", artist: MUSIC.panel.artists || "" };
  const st = MUSIC.state || {};
  return { title: st.title || "", artist: st.artist || "" };
}

const musicPlaying = () => {
  const a = $("mus-audio");
  if (MUSIC.panel) return !!(a && !a.paused && !a.ended);
  const s = String((MUSIC.state || {}).status || "");
  return MUSIC.on && s !== "Paused" && s !== "Stopped" && s !== "Closed";
};

/** 播放位置：面板内用 <audio>.currentTime；QQ SMTC 用服务端位置 + 两次轮询之间本地补时。
 *
 * ⚠ 补时要从 `_music.json` 的采样时间 `ts` 起算，而不是从"收到响应的时刻"起算：
 * SMTC 采集每 500ms 才更新一次，按响应时刻算会把位置整体估快最多 0.5s，歌词会提前。
 * 数据 stale（采集进程挂了）时不再补时，避免误差一路滚大。 */
function musicPos() {
  const a = $("mus-audio");
  if (MUSIC.panel && a) return a.currentTime || 0;
  const base = Number((MUSIC.state || {}).pos || 0);
  if (!musicPlaying() || MUSIC.posStale) return base;
  const elapsed = (Date.now() - (MUSIC.posAt || Date.now())) / 1000;
  return base + Math.max(0, Math.min(elapsed, 3));
}

/** 播放条折叠状态（全局：词按钮开启歌词时要把它拉开；p-fold 按钮也用它） */
function setPlayerFold(folded) {
  MUSIC.folded = !!folded;
  const p = $("player");
  if (p) p.classList.toggle("folded", MUSIC.folded);
  try { localStorage.setItem("cocraft.player.folded2", MUSIC.folded ? "1" : "0"); } catch { /* 隐私模式 */ }
}

/** 顶栏「群聊 / 音乐」按钮：任一开着就高亮；菜单项也标一下。 */
function syncHubBtn() {
  const b = $("btn-hub");
  const chatOn = (typeof cocraftIsOpen === "function") && cocraftIsOpen();
  if (b) {
    b.classList.toggle("on", !!(chatOn || MUSIC.shown));
    // ★未读徽章（群聊）
    const u = (typeof ccUnreadTotal === "function") ? ccUnreadTotal() : 0;
    b.classList.toggle("has-unread", u > 0);
    if (u) b.setAttribute("data-unread", u > 99 ? "99+" : String(u));
    else b.removeAttribute("data-unread");
  }
  if (typeof ccActBadge === "function") ccActBadge();   // ★C1：有成员在干活 → 「工作中」角标
  const c = $("hm-chat"), m = $("hm-music");
  if (c) c.classList.toggle("on", !!chatOn);
  if (m) m.classList.toggle("on", !!MUSIC.shown);
}

/** 播放条是否显示：默认**隐藏**，从顶栏「群聊 / 音乐」里选「音乐」才出现在输入框上方；× 可隐去。 */
function setPlayerShown(on) {
  MUSIC.shown = !!on;
  try { localStorage.setItem("cocraft.player.shown2", MUSIC.shown ? "1" : "0"); } catch { /* 隐私模式 */ }
  renderPlayer();
  syncHubBtn();
}

function renderPlayer() {
  const st = MUSIC.state || {};
  const bar = $("player");
  const audio = $("mus-audio");
  const panel = MUSIC.panel;                       // 面板内播放（网易云/QQ 搜索）优先显示
  const playing = panel ? !!(audio && !audio.paused) : musicPlaying();
  bar.classList.toggle("on", !!MUSIC.shown);   // 默认不显示：选「音乐」才出现（见 setPlayerShown）
  bar.classList.toggle("folded", !!MUSIC.folded);
  bar.classList.toggle("playing", playing);
  if (panel) {
    $("p-title").textContent = panel.name || "（未知曲目）";
    $("p-artist").textContent = (panel.artists ? panel.artists + " · " : "")
      + (MUSIC.panelError || "面板播放");
  } else {
    $("p-title").textContent = MUSIC.on ? (st.title || "（未知曲目）") : "未在播放";
    $("p-artist").textContent = MUSIC.on ? (st.artist || "")
      : (MUSIC.qqRunning ? "QQ音乐已启动" : "");
  }
  $("p-toggle").classList.toggle("playing", playing);
  $("p-toggle").title = playing ? "暂停" : "播放";
  const canQueue = !!panel && (MUS.list || []).length > 1;
  $("p-prev").disabled = panel ? !canQueue : (!MUSIC.on || st.canPrev === false);
  $("p-next").disabled = panel ? !canQueue : (!MUSIC.on || st.canNext === false);
  updateSeek();
}

/** 进度条：面板内播放可拖动跳转；QQ音乐(SMTC) 只显示进度（客户端没有跳转接口） */
function updateSeek() {
  const seek = $("p-seek");
  if (!seek) return;
  const a = $("mus-audio");
  const panel = !!MUSIC.panel;
  let cur = 0, dur = 0;
  if (panel && a) { cur = a.currentTime || 0; dur = a.duration || 0; }
  else { cur = musicPos(); dur = Number((MUSIC.state || {}).end || 0); }
  MUSIC.cur = cur; MUSIC.dur = dur;
  if (!MUSIC.seekDragging) {
    seek.value = dur ? String(Math.round(cur / dur * Number(seek.max))) : "0";
  }
  const ec = $("p-cur"); if (ec) ec.textContent = musTime(cur);
  const ed = $("p-dur"); if (ed) ed.textContent = dur ? musTime(dur) : "0:00";
  seek.disabled = !(panel && dur);
  seek.title = panel ? "拖动跳转到任意位置" : "QQ音乐客户端不支持拖动";
}

function renderLyrics() {
  MUSIC.lyIdx = -1;
  highlightLyrics(true);
}

/** 当前该显示哪一句：给画面中央浮层（上一句/当前句/下一句）用 */
function lyricsIndexAt(pos) {
  if (!MUSIC.lyrics.length) return -1;
  let idx = 0;
  for (let i = 0; i < MUSIC.lyrics.length; i++) {
    if (MUSIC.lyrics[i].t <= pos) idx = i; else break;
  }
  return idx;
}

/** 展开会话时，歌词占「歌名」那一格横向滚动；超长才滚动，短句静止居中不了（左对齐） */
function updatePlayerLyric(idx, force) {
  const box = $("p-lyric"), inner = $("p-lyric-inner");
  if (!box || !inner) return;
  const l = (idx >= 0 && idx < MUSIC.lyrics.length) ? MUSIC.lyrics[idx] : null;
  const txt = l ? (l.s || "") : "";
  const zh = (l && l.zh && SET.zh) ? l.zh : "";
  const sig = txt + "\u0001" + zh;
  if (!force && box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  inner.innerHTML = esc(txt) + (zh ? `<span class="zh">${esc(zh)}</span>` : "");
  box.classList.remove("scroll");
  box.style.removeProperty("--lyr-shift");
  box.style.removeProperty("--lyr-dur");
  requestAnimationFrame(() => {
    const over = inner.scrollWidth - box.clientWidth;    // 超出可视宽度多少
    if (over > 6) {
      box.classList.add("scroll");
      box.style.setProperty("--lyr-shift", "-" + over + "px");
      box.style.setProperty("--lyr-dur", Math.max(7, over / 30 + 5).toFixed(1) + "s");
    }
  });
}

/** 中央浮层（收起侧栏）：只显示三行：上一句 / 当前句 / 下一句 */
function highlightLyrics(force) {
  const pos = musicPos();
  const idx = lyricsIndexAt(pos);
  if (!force && idx === MUSIC.lyIdx) return;             // 没换句就不动 DOM
  MUSIC.lyIdx = idx;
  // 三行：上一句 / 当前句 / 下一句；有中文翻译时在原文下面加一行小字（设置里可关）
  const line = (k, cls) => {
    const l = (k >= 0 && k < MUSIC.lyrics.length) ? MUSIC.lyrics[k] : null;
    if (!l) return `<p class="${cls}"></p>`;
    const zh = l.zh ? `<span class="zh">${esc(l.zh)}</span>` : "";
    return `<p class="${cls}">${esc(l.s)}${zh}</p>`;
  };
  $("ly-scroll").innerHTML = MUSIC.lyrics.length
    ? line(idx - 1, "prev") + line(idx, "cur") + line(idx + 1, "next")
    : `<p class="noly">${MUSIC.lyrKey ? "这首歌没找到歌词" : "正在找歌词…"}</p>`;
  updatePlayerLyric(idx, force);
}

async function ensureLyrics() {
  const { title, artist } = currentTrack();
  const key = `${title}|${artist}`;
  if (!title || key === MUSIC.lyrKey) return;
  MUSIC.lyrKey = key;
  MUSIC.lyrics = [];
  renderLyrics();
  try {
    const r = await api(`/qq/lyrics?title=${encodeURIComponent(title)}`
      + `&artist=${encodeURIComponent(artist || "")}`
      + `&tr=${SET.zh ? 1 : 0}`);
    if (MUSIC.lyrKey !== key) return;                    // 期间换歌了，丢弃这次结果
    MUSIC.lyrics = (r.lines || []).filter((l) => l && l.s);
  } catch { /* 拿不到歌词就不显示，不影响播放 */ }
  renderLyrics();
}

/** 「现在该不该显示歌词」：只有明确开启（true）才显示。
 *  收起侧栏不再自动冒歌词 —— 收起后由「气泡球」显式开关（点击展开 / 收起）。 */
function lyricsWanted() {
  return MUSIC.lyrWanted === true;
}

/** 收起侧栏 = 纯听歌模式：歌词浮层显示；没有歌词时给一行提示，别留一片空白 */
function syncLyricsVisibility() {
  const collapsed = document.documentElement.dataset.sidebar === "closed";
  // 「有播放会话」不等于「正在放」：暂停 / 停止时球也应该收起来（面板内播放还要看 audio 状态）
  const audio = $("mus-audio");
  const panelPlaying = !!(MUSIC.panel && audio && !audio.paused && !audio.ended);
  const smtcStatus = String((MUSIC.state || {}).status || "").toLowerCase();
  const smtcPlaying = !!MUSIC.on && !/paused|stopped|none|closed/.test(smtcStatus);
  const playing = panelPlaying || smtcPlaying;
  const want = lyricsWanted();
  const withLyrics = want && playing && MUSIC.lyrics.length > 0;
  const wasOn = document.documentElement.classList.contains("lyrics-on");
  const wasPlaying = _lyricWasPlaying, wasCollapsed = _lyricWasCollapsed;
  _lyricWasPlaying = playing;
  _lyricWasCollapsed = collapsed;
  // ← 没开音乐时歌词球也应该隐藏（不能只在 sidebar=closed 时无条件显示）
  document.documentElement.classList.toggle("music-playing", playing);
  document.documentElement.classList.toggle("lyrics-on", withLyrics);
  // ⚠ 只有"用户没明确关闭"时才在收起侧栏时把浮层放出来（否则又变成关不掉）
  document.documentElement.classList.toggle("collapsed-focus", collapsed && want);
  // 只在「刚开音乐 / 刚收起侧栏」时摆一次球；每秒 poll 不能重置 idle 计时
  if (playing && collapsed && (!wasPlaying || !wasCollapsed)) {
    applyLyricBall();
    lbScheduleIdle(2800);
  }
  // 刚由关变开时，播放条可能刚从 display:none 变可见 → 重新量一次宽度（滚动才准）
  if (withLyrics && !wasOn) highlightLyrics(true);
  const showHint = want && playing && !MUSIC.lyrics.length;
  if (showHint) {
    if (!MUSIC.hintShown) {
      $("ly-scroll").innerHTML = `<p class="noly">${MUSIC.lyrKey ? "这首歌没找到歌词" : "正在找歌词…"}</p>`;
      MUSIC.hintShown = true;
    }
  } else {
    MUSIC.hintShown = false;
  }
  updateLyricBtn();
  updateLyricBallState();
}

/* ---------------- 歌词「气泡球」（收起会话时用） ----------------
 * 空闲 → 贴最近的一边缩成小箭头；悬停/拖动 → 圆球；点击 → 开关歌词；可拖到任意位置。
 * 位置与贴边（left/right + 竖直比例 y）存 localStorage，刷新后还在。 */
const LB_KEY = "cocraft.lyricball";
const LB = { edge: "right", y: 0.5, idle: true, timer: null };
let _lyricWasPlaying = false, _lyricWasCollapsed = false;

function loadLyricBall() {
  try {
    const d = JSON.parse(localStorage.getItem(LB_KEY) || "{}") || {};
    if (d.edge === "left" || d.edge === "right") LB.edge = d.edge;
    if (typeof d.y === "number" && isFinite(d.y)) LB.y = Math.min(0.94, Math.max(0.06, d.y));
    if (typeof d.idle === "boolean") LB.idle = d.idle;
  } catch { /* 隐私模式 */ }
}
function saveLyricBall() {
  try {
    localStorage.setItem(LB_KEY, JSON.stringify({ edge: LB.edge, y: LB.y, idle: LB.idle }));
  } catch { /* 隐私模式 */ }
}

/** 贴边 / 竖直位置 / 空闲形态 都按 LB 落到 DOM 上（拖动中直接跳过，别跟手抢）。
 *  ⚠ 水平位置**统一用 left**：这样从"拖动中的位置"贴到边上时能走 CSS 过渡（平滑滑动），
 *    而用 right 定位的话 `left→auto`/`right→12px` 不可过渡 → 会瞬移（用户实测报的）。 */
function applyLyricBall() {
  const b = $("lyric-ball");
  if (!b) return;
  if (b.classList.contains("dragging")) return;   // ★ 拖动中不要改 top/class，否则会跳/黏
  const w = window.innerWidth || 1200, h = window.innerHeight || 800;
  const bw = LB.idle ? 24 : 48;                   // 空闲是窄片、圆球是 48
  const cy = Math.min(h - 24, Math.max(24, LB.y * h));
  const gap = LB.idle ? 0 : 12;                   // 空闲贴边不留缝
  b.style.right = "auto";
  b.style.left = (LB.edge === "left" ? gap : Math.max(gap, w - bw - gap)) + "px";
  b.style.top = cy + "px";
  b.style.transform = "";                         // 恢复 translateY(-50%)（球心定位）
  b.classList.toggle("left", LB.edge === "left");
  b.classList.toggle("right", LB.edge === "right");
  b.classList.toggle("idle", !!LB.idle);
  b.classList.toggle("on", lyricsWanted());
}

function updateLyricBallState() {
  const b = $("lyric-ball");
  if (b) { b.classList.toggle("on", lyricsWanted()); b.title = "歌词：" + (lyricsWanted() ? "开" : "关") + "（点击开关 · 拖动换位置）"; }
}

function lbCancelIdle() {
  if (LB.timer) { clearTimeout(LB.timer); LB.timer = null; }
}
function lbScheduleIdle(delay) {
  lbCancelIdle();
  LB.timer = setTimeout(() => {
    LB.timer = null;
    LB.idle = true; saveLyricBall(); applyLyricBall();
  }, delay == null ? 2600 : delay);
}

function initLyricBall() {
  const b = $("lyric-ball");
  if (!b) return;
  loadLyricBall();
  applyLyricBall();
  lbScheduleIdle(3000);

  let drag = null;   // {id, sx, sy, gx, gy, moved}
  let dragWatch = 0;

  const stopDragListeners = () => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    if (dragWatch) { clearTimeout(dragWatch); dragWatch = 0; }
  };

  /** 拖动没收尾（松手在窗口外 / WebView2 丢了 pointerup）→ 强制回到 idle，
   *  否则 `.dragging` 会一直留着，applyLyricBall 每次 return，球就再也收不起来。 */
  const abandonDrag = () => {
    if (!drag) return;
    drag = null;
    stopDragListeners();
    b.classList.remove("dragging");
    b.style.right = "auto";
    LB.idle = true;
    saveLyricBall();
    applyLyricBall();
  };

  const onMove = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    if (dragWatch) { clearTimeout(dragWatch); dragWatch = setTimeout(abandonDrag, 3500); }
    if (!drag.moved) {
      if (Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) < 4) return;  // 没动够 = 仍算点击
      drag.moved = true;
    }
    const w = window.innerWidth || 1200, h = window.innerHeight || 800;
    const bw = b.offsetWidth || 48, bh = b.offsetHeight || 48;
    // ★ 按"抓住球时鼠标在球内的那个点"跟随：鼠标怎么动，球就怎么动，不会把球心吸到鼠标上
    const left = Math.max(2, Math.min(w - bw - 2, e.clientX - drag.gx));
    const top = Math.max(2, Math.min(h - bh - 2, e.clientY - drag.gy));
    b.style.left = left + "px";
    b.style.top = top + "px";
    b.style.right = "auto";
    b.style.transform = "none";                        // 拖动期间用"左上角"坐标，别叠 translateY(-50%)
    LB.y = (top + bh / 2) / h;                         // 记球的中心，松手后停在同一处
    LB.edge = (left + bw / 2) < w / 2 ? "left" : "right";
  };

  const onUp = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const moved = drag.moved;
    const cancelled = e.type === "pointercancel";
    drag = null;
    stopDragListeners();
    b.classList.remove("dragging");
    // ⚠ 不要先把 left 清成 auto（那会瞬移）；让 applyLyricBall 从"当前拖动位置"平滑过渡到贴边位置
    b.style.right = "auto";
    if (!moved && !cancelled) {                        // 只是点了一下 = 开关歌词（取消不算点击）
      const on = lyricsWanted();
      MUSIC.lyrWanted = !on;
      saveLyricsPref();
      if (!on && !MUSIC.lyrics.length) ensureLyrics();
    }
    LB.idle = false; saveLyricBall(); applyLyricBall(); syncLyricsVisibility();
    lbScheduleIdle();
  };

  b.addEventListener("pointerdown", (e) => {
    if (e.button != null && e.button !== 0) return;    // 只响应左键
    e.preventDefault();
    lbCancelIdle();
    b.classList.add("dragging");                       // 立刻变圆球 + 关过渡，再量尺寸（不会跳）
    const rect = b.getBoundingClientRect();
    drag = { id: e.pointerId, sx: e.clientX, sy: e.clientY,
             gx: e.clientX - rect.left, gy: e.clientY - rect.top, moved: false };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    clearTimeout(dragWatch);
    dragWatch = setTimeout(abandonDrag, 3500);          // 3.5s 没有 pointermove/up = 视为松手丢了
  });
  b.addEventListener("pointerenter", () => {
    lbCancelIdle();
    if (LB.idle && !drag) { LB.idle = false; saveLyricBall(); applyLyricBall(); }
    if (!drag) lbScheduleIdle(2800);                   // 悬停也不是永久圆球：停 2.8s 就收回
  });
  b.addEventListener("pointermove", () => {
    if (!drag && !LB.idle) lbScheduleIdle(2800);        // 还在动 → 重新计时
  });
  b.addEventListener("pointerleave", () => { if (!drag) lbScheduleIdle(); });
  window.addEventListener("blur", () => { if (drag) abandonDrag(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden && drag) abandonDrag();
  });
  window.addEventListener("resize", () => { if (!drag) applyLyricBall(); });
}

/** 「词」按钮的选中态：让用户一眼看出歌词现在是开还是关 */
function updateLyricBtn() {
  const b = $("p-lyric-btn");
  if (b) {
    b.classList.toggle("on", lyricsWanted());
    b.title = "歌词：" + (lyricsWanted() ? "开" : "关")
      + "（展开时在播放条歌名处滚动；收起侧栏时居中显示。点击切换）";
  }
}

async function pollMusic() {
  try {
    const r = await api("/qq/state");
    MUSIC.on = !!(r.music && Number(r.music.sessions || 0) > 0);
    MUSIC.state = r.music || {};
    MUSIC.qqRunning = !!r.qqRunning;
    MUSIC.real = !!r.spectrum;                           // 真频谱采样是否可用
    MUSIC.posStale = !!r.stale;
    // SMTC 的 pos 是采样时刻 ts 的值；补时从 ts 起算，歌词才对得准
    MUSIC.posAt = Number((MUSIC.state || {}).ts) || Date.now();
    if (typeof r.volume === "number" && r.volume >= 0 && !MUSIC.volDragging) {
      MUSIC.vol = r.volume;
      $("p-vol").value = String(r.volume);
      $("p-vol-num").textContent = String(r.volume);
    }
  } catch {
    MUSIC.on = false;
    MUSIC.state = {};
  }
  renderPlayer();
  await ensureLyrics();
  syncLyricsVisibility();
  setTimeout(pollMusic, 1000);
}

/* ---- 音频条：优先用真频谱；拿不到就退回合成动画 ---- */

let spectrumBusy = false;

function buildVis(box, n) {
  if (!box || box.childElementCount === n) return;
  box.innerHTML = Array.from({ length: n }, () => "<span></span>").join("");
  [...box.children].forEach((el, i) => {
    el.style.animationDuration = (0.5 + (i % 5) * 0.13).toFixed(2) + "s";
    el.style.animationDelay = (-(i * 0.07)).toFixed(2) + "s";
  });
}

const VIS_WEIGHT = 48;               // 柱子数，与 spectrum.py 的 BANDS 对齐（没数据时先摆这些）
let visIdleAt = 0;

/* 频谱值(0~1) → 柱高百分比。
   不做压缩的话等于把 0~1 直接当 0~100%，中等音量就顶到天（"一点声音就拉满"）；
   所以先按 gamma 把小声压矮、低于 gate 的直接当静音，再封顶留出余量 ——
   结果是"峰更高、谷更空"，起伏更明显。想更灵敏/更钝就调这三个常数。 */
const VIS_TOP = 88;                  // 上限百分比：再响也留一截，不会拉满
const VIS_GAMMA = 1.7;               // >1 → 小声音压得更矮（起伏更大）
const VIS_GATE = 0.08;               // 低于此值当静音，让"谷"落到底
function visHeight(v, min) {
  const floor = min || 4;
  const x = Math.max(0, Math.min(1, Number(v) || 0));
  if (x < VIS_GATE) return floor;
  return Math.max(floor, Math.round(Math.pow(x, VIS_GAMMA) * VIS_TOP));
}

/** 播放条里那条比较窄，柱子太多会糊成一片 —— 等间隔抽稀 */
function pickEven(arr, n) {
  if (!arr || arr.length <= n) return arr || [];
  const out = [];
  for (let i = 0; i < n; i++) out.push(arr[Math.round(i * (arr.length - 1) / (n - 1))]);
  return out;
}

/** 音频条：展开时在播放条里（#p-vis），收起侧栏时在输入框上方（#vis-bar）。
 *  数据优先用真频谱（系统回环，任何声音都会跳）；拿不到就退回 CSS 合成动画。 */
/** 横向条：柱高 = 频谱值 */
function paintBars(box, values, min) {
  if (!box || !values || !values.length) return;
  buildVis(box, values.length);
  [...box.children].forEach((el, i) => {
    el.style.height = visHeight(values[i], min) + "%";
  });
}

/** 会话区两侧：纵向堆叠的横条，宽度 = 频谱值 × 起伏倍数（左贴左、右贴右）。
 *  倍数来自设置「音频条起伏」（20–300%，默认 120%）；倍数越大柱子伸得越远。 */
function paintSides(values) {
  if (!values || !values.length) return;
  const gain = Math.max(0.2, Math.min(3.0, (Number(SET.spectrumSidesGain) || 120) / 100));
  ["spec-left", "spec-right"].forEach((id) => {
    const box = $(id);
    if (!box) return;
    buildVis(box, values.length);
    [...box.children].forEach((el, i) => {
      el.style.width = Math.min(100, visHeight(values[i], 4) * gain) + "%";
    });
  });
}

async function drawSpectrum() {
  const bar = $("player");
  const vis = $("vis-bar");
  const collapsed = document.documentElement.dataset.sidebar === "closed";
  const playing = musicPlaying();
  const sidesOn = document.documentElement.classList.contains("spec-sides");
  // 三个目标：播放条里的 p-vis、收起时输入框上方的 vis-bar、会话区两侧的 spec-*
  const activePlayer = MUSIC.real && !collapsed && playing && bar.classList.contains("on");
  const activeVis = MUSIC.real && collapsed;
  const activeSides = MUSIC.real && !collapsed && sidesOn;
  const active = activePlayer || activeVis || activeSides;
  bar.classList.toggle("real", activePlayer);
  vis.classList.toggle("real", activeVis);
  vis.classList.toggle("playing", activeVis && playing);
  if (!active || spectrumBusy) return;
  if (!playing) {                                  // 没在放歌时降频，别 70ms 打一次接口
    const now = Date.now();
    if (now - visIdleAt < 600) return;
    visIdleAt = now;
  }
  spectrumBusy = true;
  try {
    const r = await api("/qq/spectrum");
    const bars = (r && r.bars) || [];
    if (!bars.length) return;
    if (activeVis) paintBars(vis, bars, 5);
    if (activePlayer) paintBars($("p-vis"), pickEven(bars, 12), 8);
    if (activeSides) paintSides(bars);
  } catch { /* 拿不到就继续用合成动画 */ } finally {
    spectrumBusy = false;
  }
}

function initMusic() {
  MUSIC.lyrWanted = loadLyricsPref();          // 歌词开/关是用户明确表态过就记住
  const ctl = (action) => api("/qq/control", {
    method: "POST", body: JSON.stringify({ action }),
  }).catch(() => { /* 失败无所谓，下一轮轮询会刷新状态 */ });

  $("p-prev").addEventListener("click", () => (MUSIC.panel ? musicPrev() : ctl("prev")));
  $("p-next").addEventListener("click", () => (MUSIC.panel ? musicNext(false) : ctl("next")));
  $("p-toggle").addEventListener("click", () => {
    const audio = $("mus-audio");
    if (MUSIC.panel && audio) {                   // 面板内播放：直接控制 <audio>
      if (audio.paused) {
        if (audio.getAttribute("src")) audio.play().catch(() => {});
        else if (MUS.playing) musicPlay(MUS.playing, { play: true });
      } else {
        audio.pause();
      }
      setTimeout(renderPlayer, 60);
      return;
    }
    ctl("playpause");
  });
  $("p-shuffle").addEventListener("click", () => {
    MUS.shuffle = !MUS.shuffle;
    MUS.bag = [];                          // 重开随机：重新洗一轮
    updatePlayModeButtons();
    saveMusicState();
  });
  $("p-loop").addEventListener("click", () => {
    MUS.loop = MUS.loop === "off" ? "all" : (MUS.loop === "all" ? "one" : "off");
    updatePlayModeButtons();
    saveMusicState();
  });
  $("p-lyric-btn").addEventListener("click", () => {
    const on = lyricsWanted();
    MUSIC.lyrWanted = !on;                     // 以"当前是否可见"为准，明确表态开 / 关
    saveLyricsPref();
    if (!on && !MUSIC.lyrics.length) ensureLyrics();
    // 展开侧栏时歌词在播放条歌名处滚动 → 把播放条拉开，否则折叠着看不见
    if (!on && document.documentElement.dataset.sidebar !== "closed") setPlayerFold(false);
    syncLyricsVisibility();
  });
  initLyricBall();                             // 收起会话时的歌词「气泡球」

  // 播放条自身：收起 / 展开（像会话列表那样，状态存本地）
  // 默认折叠（只留 ▾ 箭头）；用户展开过才记住（换新 key：旧的 "0"=展开 不再算数）
  try { MUSIC.folded = localStorage.getItem("cocraft.player.folded2") !== "0"; } catch { MUSIC.folded = true; }
  setPlayerFold(MUSIC.folded);
  $("p-fold").addEventListener("click", () => setPlayerFold(!MUSIC.folded));
  // ⚠ 用新键 shown2：旧键 `cocraft.player.shown` 曾被 musicPlay() 的 bug 擅自写成 "1"
  // （刷新面板就弹音乐条）→ 不再继承旧值，只有用户**主动**开/关（选「音乐」/ ×）才记。
  try { MUSIC.shown = localStorage.getItem("cocraft.player.shown2") === "1"; } catch { MUSIC.shown = false; }
  renderPlayer();
  const _pc = $("p-close");
  if (_pc) _pc.addEventListener("click", () => setPlayerShown(false));

  // 进度条：拖动即跳（面板内播放）；QQ SMTC 只显示
  const seek = $("p-seek");
  seek.addEventListener("input", () => {
    const ratio = Number(seek.value) / Number(seek.max || 1000);
    const a = $("mus-audio");
    MUSIC.seekDragging = true;
    if (MUSIC.panel && a && a.duration) { try { a.currentTime = ratio * a.duration; } catch { /* 忽略 */ } }
    const ec = $("p-cur"); if (ec) ec.textContent = musTime(ratio * (MUSIC.dur || 0));
  });
  seek.addEventListener("change", () => { MUSIC.seekDragging = false; updateSeek(); });

  // 条内搜索 = 和「乐」弹窗同一套（搜到即点播，走面板内播放）
  $("p-search-btn").addEventListener("click", () => {
    const box = $("p-search");
    const show = box.hidden;
    box.hidden = !show;
    hideBarResults();                      // 结果区跟着收起（重开时是干净的）
    if (show) $("p-q").focus();
  });
  $("p-q-go").addEventListener("click", () => musicSearch($("p-q").value));
  $("p-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); musicSearch($("p-q").value); }
  });
  $("p-results").addEventListener("click", (e) => {
    if (e.target.closest("[data-close]")) { hideBarResults(); return; }
    const it = e.target.closest(".it[data-play]");
    if (it) musicPlay(it.dataset.play, { manual: true });
  });

  // 音乐主页（全页）
  $("p-home").addEventListener("click", openMusicHome);
  $("p-queue").addEventListener("click", () => {
    const box = $("p-results");
    if (MUS.view === "queue" && box && !box.hidden) { hideBarResults(); return; }  // 再点一次收起
    renderQueue();
  });
  $("mus-close").addEventListener("click", closeMusicHome);
  $("mus-go").addEventListener("click", () => musicSearch($("mus-q").value));
  $("mus-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); musicSearch($("mus-q").value); }
  });
  document.querySelectorAll("#mus-home .mus-nav-i").forEach((b) => {
    b.addEventListener("click", () => setHomeView(b.dataset.view));
  });
  $("mus-songs").addEventListener("click", (e) => {
    const pl = e.target.closest(".mus-pl[data-pl]");
    if (pl) { musicPlaylistOpen(pl.dataset.pl, pl.dataset.name); return; }
    const it = e.target.closest(".mus-song[data-play]");
    if (it) musicPlay(it.dataset.play, { manual: true });
  });

  // 平台切换（条内 + 弹窗共用同一状态）
  document.querySelectorAll(".wp-pb").forEach((b) => {
    b.addEventListener("click", () => setProvider(b.dataset.p || "netease"));
  });
  renderProvider();

  $("p-vol").addEventListener("input", (e) => {
    MUSIC.vol = Number(e.target.value);
    $("p-vol-num").textContent = String(MUSIC.vol);
    MUSIC.volDragging = true;
    clearTimeout(MUSIC.volTimer);
    MUSIC.volTimer = setTimeout(() => {
      api("/qq/volume", { method: "POST", body: JSON.stringify({ value: MUSIC.vol }) })
        .catch(() => {})
        .finally(() => { MUSIC.volDragging = false; });
    }, 120);
  });

  POLL.interval("music-seek", 250, () => {
    updateSeek();
    if (document.documentElement.classList.contains("lyrics-on")) highlightLyrics();
  });
  buildVis($("vis-bar"), VIS_WEIGHT);                    // 收起侧栏时输入框上方那条先摆好柱子
  buildVis($("spec-left"), VIS_WEIGHT);                  // 会话区左右两侧的真频谱先摆好柱子
  buildVis($("spec-right"), VIS_WEIGHT);
  POLL.interval("music-spectrum", 70, drawSpectrum);        // 真频谱：70ms 一帧
  updatePlayModeButtons();

  pollMusic();
}
