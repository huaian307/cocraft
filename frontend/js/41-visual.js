/* 41-visual.js —— 壁纸选择器 / 搜歌 / 音乐主页 / 模型切换（P1 从 app.js 拆出，纯搬运不改逻辑） */

/* ---------------- 动态壁纸选择器（面板里自己挑）----------------
 * 数据全部来自 server.py：/live/list 列可用壁纸、/live/pick 落盘保存（_wallpapers.json）。 */

const WALL = { data: null, q: "" };

async function loadWallpaperList() {
  try { WALL.data = await api("/live/list"); } catch { WALL.data = null; }
  return WALL.data;
}

/** 动态壁纸读取目录：留空 = 自动查找本机创意工坊 431960 */
async function loadWallpaperRoot() {
  try {
    const r = await api("/live/root");
    const input = $("cfg-wall-root");
    if (!input) return;
    input.value = (r && r.override) || "";
    input.placeholder = r && r.auto ? "留空 = " + r.auto : "留空 = 自动查找";
    input.title = r && r.effective ? "当前生效：" + r.effective : "";
  } catch { /* 服务没起来就保持原样 */ }
}

async function saveWallpaperRoot(value) {
  try {
    const r = await api("/live/root", { method: "POST", body: JSON.stringify({ root: value || "" }) });
    if (!r || !r.ok) {
      alert((r && r.message) || "保存动态壁纸目录失败");
      await loadWallpaperRoot();
      return;
    }
    await loadWallpaperRoot();
    WALL.data = null;                       // 目录变了，壁纸清单要重取
    if (liveReload) await liveReload();
    if (!$("wp").hidden) await openWallpapers();
  } catch (e) {
    alert("保存动态壁纸目录失败：" + (e && e.message ? e.message : e));
    await loadWallpaperRoot();
  }
}

/** 点「选择…」→ 服务端弹 Windows 原生文件夹选择框 → 选完自动保存并换片 */
async function pickWallpaperRoot() {
  const btn = $("cfg-wall-root-pick");
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "选择中…"; }
  try {
    const r = await api("/live/root/pick", { method: "POST" });
    if (r && r.ok && r.path) {
      await saveWallpaperRoot(r.path);
    } else if (r && r.canceled) {
      /* 用户取消：什么都不做 */
    } else {
      alert((r && r.error) || "选择文件夹失败");
    }
  } catch (e) {
    alert("选择文件夹失败：" + (e && e.message ? e.message : e));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || "选择…"; }
    await loadWallpaperRoot();
  }
}

function renderWallpapers() {
  const d = WALL.data || {};
  const pick = d.pick || {};
  const all = d.items || [];
  const q = (WALL.q || "").trim().toLowerCase();
  const items = all.filter((it) => {
    if (!q) return true;
    return [it.title, it.file, it.id].some((x) => String(x || "").toLowerCase().includes(q));
  });
  const box = $("wp-list");
  const nameOf = (id) => {
    const it = items.find((x) => x.id === id);
    return it ? (it.title || it.id) : id;
  };
  $("wp-cur").textContent = "当前：白昼 " + (pick.hiru ? nameOf(pick.hiru) : "（默认）")
    + "　｜　共 " + all.length + " 张"
    + (q ? "（筛出 " + items.length + " 张）" : "");
  if (!items.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">没有匹配的壁纸`
      + `（换个筛选，或清空上面的搜索框）。</div>`;
    return;
  }
  box.innerHTML = items.map((it) => {
    const on = (t) => (pick[t] === it.id ? " on-" + t : "");
    const sel = (t) => (pick[t] === it.id ? ' class="sel"' : "");
    // 缩略图优先用壁纸自带的 preview（gif/jpg，稳）；没有才退回 <video> 取第 1 秒那帧
    const thumb = it.preview
      ? `<img class="wp-thumb" src="${esc(it.preview)}" alt="" loading="lazy">`
      : `<video class="wp-thumb" muted preload="metadata" playsinline src="${esc(it.url)}#t=1"></video>`;
    return `<div class="wp-it${on("hiru")}">` + thumb
      + `<div class="wp-meta"><b>${esc(it.title || it.id)}</b>`
      + `<i>${it.size_mb} MB · ${esc(String(it.file).slice(0, 26))}</i></div>`
      + `<div class="wp-act">`
      + `<button data-slot="hiru" data-id="${esc(it.id)}"${sel("hiru")}>用这张</button>`
      + `</div></div>`;
  }).join("");
}

async function openWallpapers() {
  $("wp").hidden = false;
  renderWallpapers();
  await loadWallpaperList();
  renderWallpapers();
  OVERLAY.push("wp", closeWallpapers);
}

function closeWallpapers() { $("wp").hidden = true; OVERLAY.pop("wp"); }

async function pickWallpaper(theme, id) {
  const r = await api("/live/pick", { method: "POST", body: JSON.stringify({ theme, id }) });
  if (WALL.data) WALL.data.pick = (r && r.pick) || {};
  renderWallpapers();
  if (liveReload) await liveReload();            // 立刻换片，不用等 5 分钟
}

function initWallpaperPicker() {
  const close = () => closeWallpapers();
  // 入口改到设置弹窗里的「选壁纸…」（cfg-wall）；顶栏不再单独放按钮
  $("wp-close").addEventListener("click", close);
  $("wp").addEventListener("click", (e) => { if (e.target === $("wp")) close(); });
  $("wp-q").addEventListener("input", (e) => { WALL.q = e.target.value; renderWallpapers(); });
  $("wp-list").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-slot]");    // ⚠ 别用 data-theme：那会套上主题变量
    if (!b) return;
    pickWallpaper(b.dataset.slot, b.dataset.id)
      .catch((err) => alert("设置壁纸失败：" + err.message));
  });
  $("wp-reset").addEventListener("click", () => {
    pickWallpaper("hiru", null)
      .catch((err) => alert("恢复默认失败：" + err.message));
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("wp").hidden) close();
  });
}

/* ---------------- 面板内搜歌/放歌（网易云 · 非官方接口，走 /music/* 代理）---------------- */

const MUS = { list: [], playing: null, provider: "netease", loop: "all", shuffle: false, bag: [], queueName: "", playlists: [], recommend: [], playlistId: "", homeOpen: false, homeView: "recommend", homeSig: "", playlistsErr: "", recommendErr: "", view: "", seq: 0,
  installed: null, audioBar: null };   // installed: 音乐服务（可选组件）装没装
const MUS_KEY = "cocraft.music";
const MUS_SAVE_EVERY = 2000;                  // 播放中每隔 2s 落一次盘，刷新后能接着放

/** 面板刷新后要能恢复：把「曲目 + 队列 + 播放模式 + 进度」存 localStorage */
function musicSnapshot() {
  const a = $("mus-audio");
  return {
    v: 1,
    provider: MUS.provider,
    list: MUS.list || [],
    playing: MUS.playing || null,
    loop: MUS.loop,
    shuffle: !!MUS.shuffle,
    queueName: MUS.queueName || "",
    panel: MUSIC.panel ? { ...MUSIC.panel } : null,
    pos: a ? (a.currentTime || 0) : 0,
    wasPlaying: !!(MUSIC.panel && a && !a.paused && !a.ended),
  };
}

function saveMusicState() {
  try { localStorage.setItem(MUS_KEY, JSON.stringify(musicSnapshot())); } catch { /* 隐私模式 */ }
}

let musicSavedAt = 0;
function saveMusicStateThrottled() {
  const now = Date.now();
  if (now - musicSavedAt < MUS_SAVE_EVERY) return;
  musicSavedAt = now;
  saveMusicState();
}

function loadMusicState() {
  try { return JSON.parse(localStorage.getItem(MUS_KEY) || "{}") || {}; } catch { return {}; }
}

function setMusStatus(t) { /* 兼容旧调用：状态改在设置/结果区显示 */ }

async function pasteCookie(provider) {
  const hint = provider === "qq"
    ? "粘贴 y.qq.com 的【完整】Cookie（含 uin 与 qm_keyst，别截断）："
    : "粘贴 music.163.com 的 Cookie（含 MUSIC_U）：";
  const ck = prompt(hint, "");
  if (ck === null) return;
  try {
    const r = await api("/music/cookie", { method: "POST", body: JSON.stringify({ provider, cookie: ck.trim() }) });
    const keys = (r && r.keys) || [];
    const miss = (r && r.missing) || [];
    alert(miss.length
      ? "已保存，但缺少关键字段：" + miss.join("、") + "；检测到：" + (keys.join("、") || "（没有 key=value，像不是 Cookie）")
      : "已保存；检测到：" + keys.join("、"));
    musicStatus();
  } catch (e) { alert("保存失败：" + (e && e.message ? e.message : e)); }
}

function setProvider(p) {
  MUS.provider = p === "qq" ? "qq" : "netease";
  renderProvider();
  MUS.list = []; MUS.playing = null; MUS.bag = []; MUS.queueName = "";
  MUS.playlists = []; MUS.recommend = []; MUS.playlistId = "";
  MUS.homeView = "recommend"; MUS.homeSig = "";
  if (MUS.homeOpen) { hideBarResults(); renderHome(); loadHomePlaylists(); loadHomeRecommend(); }
  else renderBarResults();
  musicStatus();
  saveMusicState();
}

function renderProvider() {
  document.querySelectorAll(".wp-pb").forEach((b) => b.classList.toggle("on", b.dataset.p === MUS.provider));
}

/** 封面统一走服务端代理（只放行音乐站；落到临时缓存，选完 / 播放后清）。 */
function coverSrc(url) {
  return `/music/cover?p=${encodeURIComponent(MUS.provider)}&u=${encodeURIComponent(url)}`;
}

/** 生成一个封面缩略图；没有 cover 时也留个占位，避免行高跳动。 */
function coverHtml(item, cls) {
  return item && item.cover
    ? `<img class="${cls}" loading="lazy" alt="" src="${esc(coverSrc(item.cover))}">`
    : `<span class="${cls} is-empty" aria-hidden="true"></span>`;
}

/** 新插入的封面图加载失败就移除，避免出现「破图」图标。 */
function bindCoverFallback(root) {
  (root || document).querySelectorAll("img.it-cover, img.s-cover, img.pl-cover").forEach((im) => {
    const fail = () => im.remove();
    im.addEventListener("error", fail, { once: true });
    if (im.complete && im.naturalWidth === 0) fail();
  });
}

/** 条内结果列表（紧凑 .it 行；点一下即在面板里播） */
function renderBarResults() {
  const box = $("p-results");
  if (!box) return;
  if (!MUS.list.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  const pf = MUS.provider === "qq" ? "QQ音乐" : "网易云";
  const head = MUS.queueName
    ? `歌单：${esc(MUS.queueName)} · ${MUS.list.length} 首（循环/随机作用于它）`
    : `点一条即在面板里播放（${pf}）：`;
  MUS.view = "songs";
  box.innerHTML = `<div class="hint p-results-h"><span>${head}</span>`
    + `<button type="button" class="p-x" data-close="1" title="收起结果">×</button></div>`
    + MUS.list.map((it) => `<div class="it${String(it.id) === String(MUS.playing) ? " on" : ""}"`
      + ` data-play="${esc(it.id)}">`
      + coverHtml(it, "it-cover")
      + `<b>${esc(it.name)}</b><i>${esc(it.artists)}</i>`
      + `<i>${esc(it.album || "")}</i></div>`).join("");
  bindCoverFallback(box);
}

/** 收起播放条里的结果区（搜索 / 歌单都收）。 */
function hideBarResults() {
  const box = $("p-results");
  if (box) { box.hidden = true; box.innerHTML = ""; }
  MUS.view = "";
}

/* ---------------- 音乐主页（全页；布局仿平台、风格随面板）---------------- */

function openMusicHome() {
  try { if (typeof CODE !== "undefined" && CODE.open) codeClose(); } catch { /* ignore */ }
  try { if (typeof collabIsOpen === "function" && collabIsOpen()) collabOpen(false); } catch { /* ignore */ }
  const bs = $("body-stage");
  if (bs) bs.classList.add("mus-open");
  MUS.homeOpen = true;
  MUS.homeSig = "";
  if (!(MUS.playlists || []).length) loadHomePlaylists();
  if (!(MUS.recommend || []).length) loadHomeRecommend();
  renderHome();
  const q = $("mus-q");
  if (q) setTimeout(() => q.focus(), 50);
}

function closeMusicHome() {
  const bs = $("body-stage");
  if (bs) bs.classList.remove("mus-open");
  MUS.homeOpen = false;
}

/** 选完歌单后清掉服务端临时封面缓存；下次再看推荐时重新抓（封面本来就是看完即弃）。 */
function clearCoverCache() {
  try { api("/music/cover/clear", { method: "POST", body: "{}" }).catch(() => {}); } catch { /* 忽略 */ }
}

/** 主页视图：recommend（推荐）/ mine（我的歌单）/ songs（当前队列） */
function setHomeView(v) {
  MUS.homeView = (v === "mine" || v === "songs") ? v : "recommend";
  MUS.homeSig = "";
  if (MUS.homeView === "mine" && !(MUS.playlists || []).length) loadHomePlaylists();
  if (MUS.homeView === "recommend" && !(MUS.recommend || []).length) loadHomeRecommend();
  renderHome();
}

/** 主页渲染（签名去重：播放状态微变时不重建长列表） */
function renderHome() {
  if (!MUS.homeOpen) return;
  const head = $("mus-main-h"), box = $("mus-songs");
  if (!head || !box) return;
  const sig = [MUS.homeView, MUS.provider, MUS.queueName || "", MUS.playing || "",
    MUS.list.length, (MUS.list[0] && MUS.list[0].id) || "",
    (MUS.playlists || []).length, (MUS.playlists[0] && MUS.playlists[0].id) || "",
    (MUS.recommend || []).length, (MUS.recommend[0] && MUS.recommend[0].id) || "",
    MUS.playlistsErr || "", MUS.recommendErr || ""].join("|");
  if (sig === MUS.homeSig) return;
  MUS.homeSig = sig;

  document.querySelectorAll("#mus-home .mus-nav-i")
    .forEach((b) => b.classList.toggle("on", b.dataset.view === MUS.homeView));
  const pf = MUS.provider === "qq" ? "QQ音乐" : "网易云";

  // 推荐 / 我的 → 歌单列表
  if (MUS.homeView === "recommend" || MUS.homeView === "mine") {
    const isMine = MUS.homeView === "mine";
    const items = (isMine ? MUS.playlists : MUS.recommend) || [];
    const err = isMine ? MUS.playlistsErr : MUS.recommendErr;
    head.innerHTML = `<b>${isMine ? "我的歌单" : "推荐歌单"}</b><span>${pf} · ${items.length} 个</span>`
      + (isMine ? "" : `<button id="mus-recommend-refresh" title="重新拉一批推荐歌单">换一批</button>`);
    if (!isMine) {
      const rf = head.querySelector("#mus-recommend-refresh");
      if (rf) rf.addEventListener("click", () => loadHomeRecommend(true));
    }
    if (err) { box.innerHTML = `<div class="mus-empty">${esc(err)}</div>`; return; }
    if (!items.length) { box.innerHTML = `<div class="mus-empty">加载中…</div>`; return; }
    box.innerHTML = items.map((p) => `<div class="mus-pl" data-pl="${esc(p.id)}" data-name="${esc(p.name || "")}">`
      + coverHtml(p, "pl-cover")
      + `<b>${esc(p.name || "(无标题)")}</b>`
      + `<i>${p.count ? fmtCount(p.count) + " 播放" : ""}</i></div>`).join("");
    bindCoverFallback(box);
    return;
  }

  // songs → 当前队列
  const title = MUS.queueName || (MUS.list.length ? "搜索结果" : "当前队列");
  head.innerHTML = `<b>${esc(title)}</b><span>${pf} · ${MUS.list.length} 首</span>`
    + (MUS.list.length > 1 ? `<button id="mus-playall">随机播放</button>` : "");
  const all = head.querySelector("#mus-playall");
  if (all) all.addEventListener("click", () => {
    if (!MUS.list.length) return;
    musicPlay(MUS.list[Math.floor(Math.random() * MUS.list.length)].id, { play: true });
  });
  if (!MUS.list.length) {
    box.innerHTML = `<div class="mus-empty">左侧「我的」选歌单、或「推荐」挑一个、或上方搜索。<br>`
      + `<b>循环 / 随机作用于当前队列</b>；队列也能在音乐条的「≡」里看。</div>`;
    return;
  }
  box.innerHTML = MUS.list.map((it, i) => `<div class="mus-song${String(it.id) === String(MUS.playing) ? " on" : ""}"`
    + ` data-play="${esc(it.id)}">`
    + coverHtml(it, "s-cover")
    + `<span class="s-no">${i + 1}</span>`
    + `<span class="s-name">${esc(it.name || "")}</span>`
    + `<span class="s-artist">${esc(it.artists || "")}</span>`
    + `<span class="s-album">${esc(it.album || "")}</span>`
    + `<span class="s-dur">${it.duration ? musTime(it.duration) : ""}</span></div>`).join("");
  bindCoverFallback(box);
}

/** 播放量：>=1 万折成「x 万」 */
function fmtCount(n) {
  n = Number(n) || 0;
  return n >= 10000 ? Math.round(n / 10000) + "万" : String(n);
}

async function loadHomePlaylists() {
  MUS.playlistsErr = "";
  try {
    const r = await api("/music/playlists?p=" + MUS.provider);
    if (r && r.ok) MUS.playlists = r.playlists || [];
    else { MUS.playlists = []; MUS.playlistsErr = (r && r.error) || "拿不到歌单"; }
  } catch (e) {
    MUS.playlists = [];
    MUS.playlistsErr = "歌单加载失败：" + (e && e.message ? e.message : e);
  }
  MUS.homeSig = "";
  renderHome();
}

let recommendLoading = false;
async function loadHomeRecommend(force) {
  if (recommendLoading) return;                 // 连点「换一批」不叠加请求
  recommendLoading = true;
  MUS.recommendErr = "";
  try {
    const r = await api("/music/recommend?p=" + MUS.provider + (force ? "&r=" + Date.now() : ""));
    if (r && r.ok) MUS.recommend = r.playlists || [];
    else { MUS.recommend = []; MUS.recommendErr = (r && r.error) || "拿不到推荐"; }
  } catch (e) {
    MUS.recommend = [];
    MUS.recommendErr = "推荐加载失败：" + (e && e.message ? e.message : e);
  }
  recommendLoading = false;
  MUS.homeSig = "";
  renderHome();
}

/** 音乐条里的「当前播放列表」（队列）—— 点「≡」展开 */
function renderQueue() {
  const box = $("p-results");
  if (!box) return;
  MUS.view = "queue";
  box.hidden = false;
  const close = `<button type="button" class="p-x" data-close="1" title="收起">×</button>`;
  const pf = MUS.provider === "qq" ? "QQ音乐" : "网易云";
  const head = `当前播放列表 · ${MUS.list.length} 首`
    + (MUS.queueName ? `（${esc(MUS.queueName)}）` : `（${pf}）`);
  if (!MUS.list.length) {
    box.innerHTML = `<div class="hint p-results-h"><span>当前播放列表是空的</span>${close}</div>`;
    return;
  }
  box.innerHTML = `<div class="hint p-results-h"><span>${head}</span>${close}</div>`
    + MUS.list.map((it, i) => `<div class="it${String(it.id) === String(MUS.playing) ? " on" : ""}"`
      + ` data-play="${esc(it.id)}">`
      + coverHtml(it, "it-cover")
      + `<b>${i + 1}. ${esc(it.name || "")}</b><i>${esc(it.artists || "")}</i></div>`).join("");
  bindCoverFallback(box);
}

/** 打开一个歌单：把它作为播放队列并开始播放（随机开启则随机起播；循环/随机都作用于它）。 */
async function musicPlaylistOpen(id, name) {
  const box = $("p-results");
  if (box) { box.hidden = false; box.innerHTML = '<div class="hint">加载歌单歌曲…</div>'; }
  try {
    const r = await api("/music/playlist?p=" + MUS.provider + "&id=" + encodeURIComponent(id));
    if (!r || !r.ok) {
      if (box) box.innerHTML = '<div class="hint">' + esc((r && r.error) || "拿不到歌单歌曲") + '</div>';
      return;
    }
    const songs = r.songs || [];
    if (!songs.length) { if (box) box.innerHTML = '<div class="hint">这个歌单是空的</div>'; return; }
    MUS.list = songs;
    MUS.queueName = name || "";
    MUS.playlistId = id;
    MUS.bag = [];
    MUS.homeView = "songs";
    MUS.homeSig = "";
    if (MUS.homeOpen) hideBarResults();
    else renderBarResults();
    renderHome();
    clearCoverCache();                     // 选完歌单：推荐封面已完成使命，清掉临时缓存
    const first = MUS.shuffle ? songs[Math.floor(Math.random() * songs.length)].id : songs[0].id;
    musicPlay(first, { play: true });
  } catch (e) {
    if (box) box.innerHTML = '<div class="hint">歌单加载失败：' + esc(e && e.message ? e.message : e) + '</div>';
  }
}

function musTime(s) {
  s = Math.max(0, Math.floor(s || 0));
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

async function musicSearch(qRaw) {
  const q = String(qRaw || "").trim();
  if (!q) return;
  if (MUS.installed === false) {          // 可选组件没装：直说，别去撞 502
    setBanner("音乐服务未安装（安装包里的可选组件；勾上「音乐服务」重装即可）");
    return;
  }
  const box = $("p-results");
  if (MUS.homeOpen) { const s = $("mus-songs"); if (s) s.innerHTML = `<div class="mus-empty">搜索中…</div>`; }
  else if (box) { box.hidden = false; box.innerHTML = '<div class="hint">搜索中…</div>'; }
  try {
    const r = await api("/music/search?p=" + MUS.provider + "&q=" + encodeURIComponent(q) + "&limit=20");
    MUS.list = (r && r.songs) || [];
    MUS.bag = [];                          // 新一批结果 → 随机队列重洗
    MUS.queueName = "";                    // 搜索结果不是歌单
    MUS.playlistId = "";
    MUS.homeSig = "";
    if (MUS.homeOpen) MUS.homeView = "songs";
    if (MUS.homeOpen) { hideBarResults(); renderHome(); }
    else renderBarResults();
    saveMusicState();
    if (!MUS.list.length) {
      const msg = '没搜到「' + esc(q) + '」';
      if (MUS.homeOpen) { const s = $("mus-songs"); if (s) s.innerHTML = '<div class="mus-empty">' + msg + '</div>'; }
      else if (box) box.innerHTML = '<div class="hint">' + msg + '</div>';
    }
  } catch (e) {
    const msg = '搜索失败：' + esc(e && e.message ? e.message : e);
    if (MUS.homeOpen) { const s = $("mus-songs"); if (s) s.innerHTML = '<div class="mus-empty">' + msg + '</div>'; }
    else if (box) box.innerHTML = '<div class="hint">' + msg + '</div>';
  }
}

async function musicPlay(id, opts = {}) {
  const a = $("mus-audio");
  const box = $("p-results");
  id = String(id || "");
  let it = (MUS.list || []).find((x) => String(x.id) === id);
  if (!it && MUSIC.panel && String(MUSIC.panel.id) === id) it = { ...MUSIC.panel };
  if (!it || !id) return false;
  const seq = ++MUS.seq;

  MUS.playing = id;
  MUSIC.panel = {
    name: it.name || "（未知曲目）",
    artists: it.artists || "",
    album: it.album || "",
    id,
  };
  MUSIC.panelError = "";
  // 播放条默认不显示（选「音乐」才出现）。⚠ 恢复上次曲目时不要把它顶开：
  // 刷新页面会调 restoreMusicState() → musicPlay()，若这里强行 shown=true，
  // 就会变成「每次刷新面板都自动弹出音乐条」（0.1.67 修）。
  if (!opts.keepShown && !MUSIC.shown) { MUSIC.shown = true; try { localStorage.setItem("cocraft.player.shown2", "1"); } catch (e) { /* ignore */ } }
  if (MUS.homeOpen) hideBarResults();
  else renderBarResults();
  renderHome();
  renderPlayer();

  const mid2 = it.mediaMid ? "&mid2=" + encodeURIComponent(it.mediaMid) : "";
  if (box && !opts.silent) box.innerHTML = '<div class="hint">解析音源…（' + esc(it.name || "") + '）</div>';
  try {
    const r = await api("/music/url?p=" + MUS.provider + "&id=" + encodeURIComponent(id) + mid2);
    if (seq !== MUS.seq) return false;             // 期间又切歌了，丢弃这次结果
    if (!r || !r.ok) {
      MUSIC.panelError = (r && r.error) || "该曲目无可用音源";
      if (box && !opts.silent) box.innerHTML = '<div class="hint">' + esc(MUSIC.panelError) + '</div>';
      renderPlayer();
      saveMusicState();
      return false;
    }
  } catch (e) {
    if (seq !== MUS.seq) return false;
    MUSIC.panelError = "解析失败：" + (e && e.message ? e.message : e);
    if (box && !opts.silent) box.innerHTML = '<div class="hint">' + esc(MUSIC.panelError) + '</div>';
    renderPlayer();
    saveMusicState();
    return false;
  }

  const pos = Number(opts.position) || 0;
  const applyPos = () => { if (pos > 0) { try { a.currentTime = pos; } catch { /* 元数据可能已换 */ } } };
  a.addEventListener("loadedmetadata", applyPos, { once: true });
  a.src = "/music/stream?p=" + MUS.provider + "&id=" + encodeURIComponent(id) + mid2;
  try { a.load(); } catch { /* 老浏览器忽略 */ }
  if (box && !opts.silent) box.hidden = true;     // 播起来就把结果区收起来
  renderPlayer();
  ensureLyrics();
  if (opts.play !== false) {
    // 刷新后自动续播：watch.py 已给面板窗口加 --autoplay-policy=no-user-gesture-required；
    // 万一仍被拦，播放条会停在同一首，用户点一下播放即可。
    a.play().catch(() => {});
  }
  if (opts.manual) clearCoverCache();      // 用户点一首歌：封面看完即弃（自动连播不反复清）
  saveMusicState();
  return true;
}

/** 随机：洗牌袋里取下一个（一轮内不重复；取空后重洗，尽量避开刚放完的那首） */
function shufflePick(cur, total) {
  if (!Array.isArray(MUS.bag) || MUS.bag.length === 0) {
    const arr = [];
    for (let i = 0; i < total; i++) arr.push(i);
    for (let i = arr.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      const t = arr[i]; arr[i] = arr[j]; arr[j] = t;
    }
    if (arr.length > 1 && arr[0] === cur) { arr[0] = arr[1]; arr[1] = cur; }
    MUS.bag = arr;
  }
  return MUS.bag.shift();
}

/**
 * 下一首（面板队列）。auto=true 表示歌曲自然放完触发。
 * 循环：off=放完列表就停 / all=列表循环 / one=单曲循环。
 * 随机开启时优先随机（洗牌袋，一轮内不重复）。
 */
async function musicNext(auto, dir = 1) {
  const list = MUS.list || [];
  if (!list.length) return false;
  let idx = list.findIndex((x) => String(x.id) === String(MUS.playing));
  if (idx < 0) idx = dir > 0 ? -1 : 0;
  const total = list.length;

  // 单曲循环：自然放完就重放当前曲（随机开启时随机优先）
  if (auto && dir > 0 && !MUS.shuffle && MUS.loop === "one" && idx >= 0) {
    return await musicPlay(list[idx].id, { play: true, keepShown: !!auto });
  }

  for (let tries = 0; tries < total; tries++) {
    let n;
    if (MUS.shuffle && total > 1) {
      n = shufflePick(idx, total);
      if (n === undefined || n < 0) return false;
    } else {
      const next = idx + dir;
      if (auto && dir > 0 && MUS.loop === "off" && next >= total) return false;  // 到队尾且不循环 → 停
      n = ((next % total) + total) % total;
    }
    // 自动续播（auto）不把播放条顶开：用户可能特意关掉了条形、只留着听
    const ok = await musicPlay(list[n].id, { play: true, keepShown: !!auto });
    if (ok) return true;
    idx = n;
  }
  return false;
}

function musicPrev() { return musicNext(false, -1); }

/** 循环模式归一化：兼容旧值 true/false；默认列表循环 */
function normalizeLoop(v) {
  if (v === "one" || v === "all" || v === "off") return v;
  if (v === false) return "off";
  return "all";
}

function updatePlayModeButtons() {
  const s = $("p-shuffle"), l = $("p-loop");
  if (s) {
    s.classList.toggle("on", !!MUS.shuffle);
    s.title = "随机播放：" + (MUS.shuffle ? "开" : "关");
  }
  if (l) {
    l.classList.toggle("on", MUS.loop !== "off");
    l.classList.toggle("one", MUS.loop === "one");
    l.textContent = MUS.loop === "one" ? "↻¹" : (MUS.loop === "all" ? "↻" : "循");
    l.title = "循环：" + (MUS.loop === "one" ? "单曲循环" : MUS.loop === "all" ? "列表循环" : "关") + "（点击切换）";
  }
}

/** 刷新后把上次的曲目/队列/进度恢复回来；能不能自动续播由浏览器策略决定。 */
async function restoreMusicState() {
  const st = loadMusicState();
  MUS.provider = st.provider === "qq" ? "qq" : "netease";
  MUS.loop = normalizeLoop(st.loop);
  MUS.shuffle = !!st.shuffle;
  MUS.bag = [];
  MUS.queueName = st.queueName || "";
  renderProvider();
  updatePlayModeButtons();
  if (!st.panel || !st.playing) return;
  MUS.playing = String(st.playing);
  if (Array.isArray(st.list) && st.list.length) {
    MUS.list = st.list;
  } else {
    MUS.list = [{
      id: MUS.playing,
      name: st.panel.name || "",
      artists: st.panel.artists || "",
      album: st.panel.album || "",
    }];
  }
  renderBarResults();
  await musicPlay(MUS.playing, {
    play: !!st.wasPlaying,
    position: Number(st.pos) || 0,
    silent: true,
    keepShown: true,        // 恢复上次曲目 ≠ 要显示播放条（条形状态由用户上次的开/关决定）
  });
}

/** 音乐服务（可选组件）装没装 —— 没装就明说，别让人以为面板坏了。
 *  安装包里「音乐服务」是可选组件，不勾就不会有 runtime/venvs/music 或 site-packages/music。 */
async function musicComponent() {
  try {
    const c = await api("/music/component");
    // ⚠ 接口形状是 {ok, audio, music:{installed,...}} —— installed 在 music 里，不在顶层。
    //   以前误读顶层 c.installed（undefined）→ 明明装了也每次都报「未安装」。
    MUS.installed = !!(c && c.music && c.music.installed);
    MUS.audioBar = !!(c && c.audio);
    if (!MUS.installed) {
      const why = ((c && c.music && c.music.reason) || "未安装音乐服务");
      const box = $("p-results");
      if (box) {
        box.innerHTML = `<div class="muted" style="padding:10px">${esc(why)}`
          + `（安装包里的可选组件；勾上「音乐服务」重装即可）</div>`;
        box.classList.add("on");
      }
      const q = $("p-q");
      if (q) { q.disabled = true; q.placeholder = "音乐服务未安装"; }
      setBanner(why + " —— 面板里的搜索/播放需要这个可选组件");
    }
  } catch { MUS.installed = null; }
}

async function musicStatus() {
  try {
    const r = await api("/music/status");
    const p = (r && r.providers) || {};
    const put = (id, st) => { const b = $(id); if (b) b.textContent = (st && st.loggedIn) ? "已登录 · 重贴" : "贴 Cookie"; };
    put("cfg-ck-netease", p.netease);
    put("cfg-ck-qq", p.qq);
  } catch { /* 服务没起来（多半是没装这个可选组件）→ musicComponent() 已经解释过了 */ }
}

/** 音频事件：播放 / 暂停 / 结束 都刷新播放条与歌词；并负责落盘进度 */
function initMusicAudio() {
  const audio = $("mus-audio");
  ["play", "pause", "ended", "error"].forEach((ev) => audio.addEventListener(ev, async () => {
    if (ev === "ended" && (MUS.list || []).length) {
      // 自然放完 → 交给 musicNext 决定：单曲循环重放、列表循环回绕、随机取洗牌袋；
      // 「循环关」= 列表放完就停（musicNext 到队尾返回 false），**不是**每首都停。
      // ⚠ 以前这里写了 `MUS.loop !== "off"`，结果循环关掉后一首都不续播，与"列表放完才停"语义相反。
      if (await musicNext(true)) return;
    }
    renderPlayer();
    syncLyricsVisibility();
    saveMusicState();
  }));
  audio.addEventListener("timeupdate", saveMusicStateThrottled);
  window.addEventListener("pagehide", saveMusicState);
  window.addEventListener("beforeunload", saveMusicState);
}

/* ---------------- 模型切换（顶栏「模型」按钮）----------------
 * 模型是「会话属性」：POST /api/session/{id}/model { model:{id,providerID,variant?} } → 204。
 * 没选中会话时，选择存成"新会话默认模型"（localStorage），新建会话时带上。
 * 清单来自 GET /api/model（{location,data:[...]}）；variants[] 也一并做成可点的变体按钮。
 * ⚠ 切模型后 GET /api/session/{id} 的 model 会更新，但"列表"更可靠（详情也是 {data:...} 包着的）。 */

const MODEL = { list: null, engine: "", loading: false, pending: null, pendingEngine: "" };
const MODEL_KEY = "cocraft.model";

/* ★模型清单是「某个引擎的」清单，不是全局清单（2026-10-20 修跨引擎写坏模型）。
 *
 * 事故：在 ACP 下建的会话里模型是 `{id:"deepseek/deepseek-flash", providerID:"acp"}`
 * （ACP 注册表没配 provider → 后端兜底成中性 `"acp"`）。切到 opencode 后：
 *   ① `MODEL.list` 还是 ACP 那份 → `repairStaleModel()` 把**合法**的 opencode 引用判成"失效"；
 *   ② `normalizeModelRef()` 只比 `id`、不比 `providerID` → 把 `providerID:"acp"` 原样吐出来；
 *   ③ 于是 POST /api/session/{id}/model 写进去 → OpenCode `ModelUnavailableError`
 *     → 那一轮 17ms 就 `idle`，提问**没有任何回复**。
 *
 * 铁律：清单必须带引擎标记；**不属于当前引擎的清单，一律不拿来做判断**（宁可不动，不许猜）。 */
function modelEngineNow() { return ENGINE.active || ""; }

/** 清单是否属于当前引擎（任一侧未知时按"匹配"处理，避免启动早期误杀）。 */
function modelEngineMatches() {
  const cur = modelEngineNow();
  return !MODEL.engine || !cur || MODEL.engine === cur;
}

/** 作废模型清单（切引擎时调用）：旧引擎的清单绝不能再参与任何判断。 */
function resetModels(engineId) {
  MODEL.list = null;
  MODEL.engine = engineId === undefined ? modelEngineNow() : (engineId || "");
  MODEL.pending = null;
  MODEL.pendingEngine = "";
  MODEL.loading = false;
}

/** 清单没拿到 / 不属于当前引擎 → 重拉一次（写模型前都要过这一道）。 */
async function ensureModelList() {
  if (!MODEL.list || !MODEL.list.length || !modelEngineMatches()) {
    try { await loadModels(); } catch { /* ignore */ }
  }
}

/** 这个引用在当前引擎里「制度上」是否合法：ACP 的中性 providerID `"acp"` 只对 ACP 引擎有效。 */
function modelRefUsable(ref) {
  if (!ref || !ref.id) return false;
  const acpNeutral = String(ref.providerID || "") === "acp";
  if (acpNeutral && modelEngineNow() !== "acp") return false;
  return true;
}

/* 模型官方图标（本地素材，由 tools/fetch_model_icons.py 下载；OpenCode 接口本身没有图标字段）。
   解析顺序：模型 id → family → family(去掉 -free) → providerID；都没有就用「青铜字母徽章」。
   muse-spark / big-pickle 没有公开品牌标 → 走字母徽章。 */
const MODEL_ICONS = {
  "deepseek": "deepseek", "deepseek-flash": "deepseek", "deepseek-thinking": "deepseek",
  "anthropic": "anthropic", "claude": "anthropic",
  "mimo": "xiaomi", "ling": "antgroup", "nemotron": "nvidia", "nemotron-free": "nvidia",
};
const MODEL_ICON_DIR = "/assets/models/";

/** 把会话里的轻量 model ref 与模型清单里的完整条目合并（拿 name / family）
 *  ⚠ 清单属于当前引擎、却命中不到 → 标记 `engineForeign`（别的引擎留下的引用）。
 *    绝不能把它当权威 ref 展示，免得顶栏一直显示一个当前引擎不认的模型。 */
function modelFull(ref) {
  if (!ref) return null;
  const list = MODEL.list || [];
  const hit = list.find((m) => m.id === ref.id && m.providerID === ref.providerID);
  if (hit) return Object.assign({}, hit, ref);
  if (list.length && modelEngineMatches()) return Object.assign({}, ref, { engineForeign: true });
  return ref;
}

function iconKeyFor(ref) {
  if (!ref) return "";
  const id = String(ref.id || "");
  const prefix = id.includes("/") ? id.split("/")[0] : "";   // `deepseek/xxx` → `deepseek`
  const fam = String(ref.family || "");
  const tries = [id, prefix, fam, fam.replace(/-free$/, ""), ref.providerID];
  for (const k of tries) if (k && MODEL_ICONS[k]) return MODEL_ICONS[k];
  return "";
}

/** 图标 HTML：有官方图标用图标；没有就用字母徽章（都不显示模型名，名字在 tooltip / 列表里） */
function modelIconHtml(ref, small) {
  const cls = "micon" + (small ? " sm" : "");
  if (!ref) {   // 还没选模型 → 用龙族校徽占位
    return `<svg class="${cls} micon-tree" aria-hidden="true"><use href="#mk-tree"></use></svg>`;
  }
  const key = iconKeyFor(modelFull(ref));
  if (key) {
    return `<img class="${cls}" src="${MODEL_ICON_DIR}${encodeURIComponent(key)}.svg" alt="" loading="lazy">`;
  }
  const f = modelFull(ref);
  const letter = String((f && (f.name || f.id)) || "?").trim().charAt(0).toUpperCase() || "?";
  return `<span class="${cls} micon-mono" aria-hidden="true">${esc(letter)}</span>`;
}

function defaultModel() {
  try { return JSON.parse(localStorage.getItem(MODEL_KEY) || "null"); } catch { return null; }
}

function saveDefaultModel(ref) {
  try { localStorage.setItem(MODEL_KEY, JSON.stringify(ref)); } catch { /* 隐私模式 */ }
}

/**
 * 解析「新会话默认模型」：本地存的 → 服务端默认 → 清单第一个；解析到就记住。
 * ⚠ 本地存的若已不在模型清单里（比如模型下线），视为失效，重新解析 ——
 *   否则新会话会挂一个无效模型，发出去又是「发了没反应」。
 */
async function resolveDefaultModel() {
  await ensureModelList();
  const list = MODEL.list || [];
  const usable = (r) => !!r && !!r.id && !!r.providerID && modelRefUsable(r)
    && (!list.length || list.some((m) => m.id === r.id && m.providerID === r.providerID));
  // ★ 本地存的引用**只有**在「清单属于当前引擎、且真的命中」时才能直接用；
  //   否则它多半是上一个引擎留下的（例如 ACP 的 providerID="acp"）→ 重新解析。
  const saved = defaultModel();
  if (usable(saved)) return saved;
  let ref = null;
  try {
    const d = unwrap(await api("/api/model/default"));
    if (d && d.id) ref = { id: d.id, providerID: d.providerID };
  } catch { /* 后端不支持就算了 */ }
  if (ref && !usable(ref)) ref = null;
  if (!ref) {
    const fresh = await loadModels();
    const first = (fresh || []).find((m) => m && m.id && m.providerID && modelRefUsable(m));
    if (first) ref = { id: first.id, providerID: first.providerID };
  }
  if (ref) saveDefaultModel(ref);
  return ref;
}

/** 把模型 ref 写进会话并同步本地状态（顶栏按钮/列表）；返回落地后的 ref，失败返回 null。 */
async function writeSessionModel(sid, ref) {
  if (!sid || !ref || !ref.id || !ref.providerID) return null;
  // ★ 最后一道闸（事故就是漏了这一道）：当前引擎不认的引用**绝不写进会话**。
  //   写坏比不写危险得多 —— 写坏了提问会直接 `ModelUnavailableError`，界面上没有任何回复。
  if (!modelRefUsable(ref)) {
    setBanner(`模型 ${modelText(ref)} 不属于当前引擎，未写入；请点「模型」另选一个`, { ms: 9000 });
    return null;
  }
  const list = MODEL.list || [];
  if (list.length && modelEngineMatches()
      && !list.some((m) => m.id === ref.id && m.providerID === ref.providerID)) {
    setBanner(`当前引擎没有模型 ${modelText(ref)}，未写入；请点「模型」另选一个`, { ms: 9000 });
    return null;
  }
  try {
    await api(`/api/session/${encodeURIComponent(sid)}/model`,
      { method: "POST", body: JSON.stringify({ model: { id: ref.id, providerID: ref.providerID, variant: ref.variant || "default" } }) });
  } catch { return null; }
  const m = { id: ref.id, providerID: ref.providerID, variant: ref.variant || "default" };
  if (state.current && state.current.id === sid) state.current.model = m;
  const s = state.sessions.find((x) => x.id === sid);
  if (s) s.model = m;
  updateModelBtn();
  return m;
}

/**
 * 确保某个会话已绑定**可用**的模型：没有就补默认；已绑但**已下架**也换成默认。
 * send() 之前调用，避免「没模型 / 模型失效 → 发出去空转、没有任何回答」。
 * 失败不抛异常（交给发送流程照常走，真失败会有 error-box）。
 */
async function ensureSessionModel(sid) {
  if (!sid) return null;
  const cur = (state.current && state.current.id === sid) ? state.current.model : null;
  await ensureModelList();                        // ★ 清单必须属于当前引擎，才拿它做判断
  if (cur && cur.id) {
    if (modelAvailable(cur)) return cur;   // 已有且仍在清单里 → 不动
    // ★ 跨引擎残留（如 providerID=acp、id=deepseek/deepseek-flash）：能归一就就地改写，不换模型
    const norm = normalizeModelRef(cur);
    if (norm) {
      const fixed = await writeSessionModel(sid, norm);
      if (fixed) return fixed;
    }
    // 真下架 / 归一不了：落到下面重新解析默认并写回（否则发出去只会空转）
  }
  const ref = await resolveDefaultModel();
  if (!ref) { setBanner("没有可用的模型；请点「模型」另选一个", { ms: 9000 }); return null; }
  return await writeSessionModel(sid, ref);
}

/**
 * ★把「跨引擎残留」的模型引用归一成本引擎能识别的一项。
 *   场景：会话在 ACP 引擎下建立时，模型 id 可能长成 `provider/model`、providerID 兜底成 `acp`；
 *   切到 opencode 引擎后，这个引用会被解析成 `acp/deepseek/...` → `ModelUnavailableError`
 *   → 那一轮 **17 毫秒就 idle=failed、界面上没有任何提示**。
 *   这里按「本引擎模型清单里真实存在的项」重写 providerID/id（同一个模型，只是把 provider 修对）。
 *   清单还没加载时返回原值（不确定，不做误判）；确实匹配不到返回 null（= 真下架了）。
 */
function normalizeModelRef(ref) {
  if (!ref || !ref.id) return ref || null;
  const list = MODEL.list;
  // ⚠ 不知道清单属于谁 / 清单不属于当前引擎 → **不猜**（返回 null 由调用方提示用户）
  if (!list || !list.length) return null;
  if (!modelEngineMatches()) return null;
  const pick = (m) => ({ id: m.id, providerID: m.providerID, variant: ref.variant });
  const ok = (m) => !!m && !!m.providerID && modelRefUsable(m);
  // ① 精确命中（本引擎里就有这一项）
  let hit = list.find((m) => ok(m) && m.id === ref.id && m.providerID === ref.providerID);
  if (hit) return pick(hit);
  // ② id 形如 `<provider>/<model>`（ACP 形态）
  const slash = ref.id.indexOf("/");
  if (slash > 0) {
    const prov = ref.id.slice(0, slash), mid = ref.id.slice(slash + 1);
    hit = list.find((m) => ok(m) && m.id === mid && m.providerID === prov);
    if (hit) return pick(hit);
    // ④ id 带前缀、本引擎的 id 是最后一段
    hit = list.find((m) => ok(m) && m.id === ref.id.slice(ref.id.lastIndexOf("/") + 1));
    if (hit) return pick(hit);
  }
  // ③ providerID 兜底成了中性值（如 "acp"），但 id 本身是对的
  //   ⚠ `ok()` 已经挡掉「非 acp 引擎却产出 providerID=acp」——事故就是从这里漏出去的
  hit = list.find((m) => ok(m) && m.id === ref.id);
  if (hit) return pick(hit);
  return null;
}

/** 模型 ref 是否仍在**当前引擎**的清单里；清单没拿到 / 不属于当前引擎时不做判断（避免误杀）。 */
function modelAvailable(ref) {
  if (!ref || !ref.id || !ref.providerID) return true;
  const list = MODEL.list;
  if (!list || !list.length) return true;
  if (!modelEngineMatches()) return true;
  return list.some((m) => m.id === ref.id && m.providerID === ref.providerID && modelRefUsable(m));
}

/** 后端可能因「模型不可用」自动换过模型 → 把这个会话的最新模型同步到顶栏按钮。 */
async function refreshCurrentModel() {
  if (!state.current) return;
  try {
    const s = unwrap(await api(`/api/session/${encodeURIComponent(state.current.id)}`));
    if (s && s.id && s.model) {
      state.current.model = s.model;
      const hit = state.sessions.find((x) => x.id === s.id);
      if (hit) hit.model = s.model;
      updateModelBtn();
    }
  } catch { /* ignore */ }
}

/**
 * 会话绑的模型已被下架（提供方删了/改名了）时：提示 + 自动切到默认模型。
 * ⚠ 不修的话，OpenCode 会 `ModelUnavailableError` → 只留一个 idle、**没有任何回复**，
 *   界面上完全看不出原因（只有服务端日志能查到）。返回是否真的切过。
 */
async function repairStaleModel(sess) {
  if (!sess || !sess.model || !sess.model.id) return false;
  await ensureModelList();                       // ★ 拿不到当前引擎的清单就不动（不许拿旧清单误判）
  if (modelAvailable(sess.model)) return false;
  const oldText = modelText(sess.model);
  // ★ 跨引擎残留：能归一就就地改写引用（同一个模型），别误当成"下架"去换默认
  const norm = normalizeModelRef(sess.model);
  if (norm) {
    const fixed = await writeSessionModel(sess.id, norm);
    if (fixed) {
      setBanner(`已修正这个会话的模型引用：${oldText} → ${modelText(fixed)}`);
      return true;
    }
    setBanner(`这个会话的模型引用 ${oldText} 不属于当前引擎，未改动；请点「模型」另选一个`, { ms: 9000 });
    return false;
  }
  const ref = await resolveDefaultModel();
  if (!ref) {
    // ⚠ 归一不了、也解析不出默认模型 → **什么都不写**（写进去就是"发了没反应"）
    setBanner(`这个会话的模型 ${oldText} 不属于当前引擎，且没有可用的替代；请点「模型」另选一个`, { ms: 9000 });
    return false;
  }
  const m = await writeSessionModel(sess.id, ref);
  if (!m) {
    setBanner(`这个会话的模型 ${oldText} 已不可用，自动切换失败；请点「模型」另选一个`, { ms: 9000 });
    return false;
  }
  setBanner(`这个会话原来的模型 ${oldText} 已下架，已自动切到 ${modelText(m)}（可在「模型」里改）`);
  return true;
}

/** 当前生效的模型：优先当前会话，其次"新会话默认" */
function currentModel() {
  return (state.current && state.current.model) || defaultModel() || null;
}

const sameModel = (a, b) => !!a && !!b && a.id === b.id && a.providerID === b.providerID;

function modelText(ref, withVariant) {
  if (!ref) return "（跟随引擎默认）";
  const base = `${ref.providerID}/${ref.id}`;
  return (withVariant && ref.variant && ref.variant !== "default") ? `${base} · ${ref.variant}` : base;
}

function updateModelBtn() {
  const b = $("btn-model");
  if (!b) return;
  const cur = currentModel();
  const f = modelFull(cur);
  b.innerHTML = modelIconHtml(cur);          // 用官方图标代替模型名（名字放 tooltip）
  b.title = cur
    ? `当前模型：${modelText(cur, true)}`
      + (f && f.engineForeign ? "（当前引擎的清单里没有它 —— 请点这里重选一个）" : "")
      + "（点一下切换）"
    : "切换模型（对当前会话生效）";
}

async function loadModels() {
  // ★ 清单必须绑定引擎。ENGINE.active 在「没打开过设置」时可能还是空 → 先问一次后端，
  //   否则会拿一份不知道自己属于谁、也不知道属于哪个引擎的清单去判模型（就是事故的入口）。
  if (!ENGINE.active) {
    try {
      const st = await api("/engine/status");
      if (st) {
        if (st.active) ENGINE.active = st.active;
        if (st.available) ENGINE.available = st.available;
        if (st.engines) ENGINE.engines = st.engines;
      }
    } catch { /* 老后端没有这个接口：按"引擎未知"处理 */ }
  }
  const eng = modelEngineNow();
  // ★ 并发调用共享同一次请求的结果：以前并发时会直接返回 MODEL.list（可能还是空 []），
  //   调用方据此误判"模型清单为空 → 模型一定可用" → 跨引擎残留的引用没被归一 → 发出即失败。
  if (MODEL.pending && MODEL.pendingEngine === eng) return MODEL.pending;
  MODEL.loading = true;
  const p = (async () => {
    let list = null;
    try { list = unwrap(await api("/api/model")) || []; }
    catch { list = null; }                     // ★ 失败**不再沿用旧清单**（旧清单可能属于别的引擎）
    // 请求期间引擎被切走了 → 这份结果不属于当前引擎，直接丢弃，别写进 MODEL.list
    if (modelEngineNow() !== eng) {
      return modelEngineMatches() ? (MODEL.list || []) : [];
    }
    MODEL.list = list || [];
    MODEL.engine = eng;
    return MODEL.list;
  })();
  MODEL.pending = p;
  MODEL.pendingEngine = eng;
  try {
    return await p;
  } finally {
    if (MODEL.pending === p) {
      MODEL.pending = null;
      MODEL.pendingEngine = "";
      MODEL.loading = false;
    }
  }
}

/** 按 provider 分组渲染；标出当前；带 variants 的给一排变体小按钮 */
function renderModels() {
  const box = $("mdl-list");
  if (!box) return;
  const cur = currentModel();
  const q = ($("mdl-q").value || "").trim().toLowerCase();
  const all = MODEL.list || [];
  const items = all.filter((m) => !q || [m.name, m.id, m.providerID, m.family]
    .some((x) => String(x || "").toLowerCase().includes(q)));
  const curRef = state.current ? state.current.model : cur;
  const curForeign = curRef && (modelFull(curRef) || {}).engineForeign;
  $("mdl-cur").textContent = (state.current
    ? "当前会话：" + modelText(state.current.model, true)
    : (cur ? "未选会话 · 新会话默认：" + modelText(cur, true) : "未选会话 · 跟随引擎默认"))
    + (curForeign ? "　⚠ 当前引擎不认这个模型，请在下面重选" : "");
  if (!items.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + (all.length ? "没有匹配的模型" : "拿不到模型清单") + `</div>`;
    return;
  }
  const byProv = {};
  items.forEach((m) => { (byProv[m.providerID] = byProv[m.providerID] || []).push(m); });
  box.innerHTML = Object.keys(byProv).sort().map((prov) => {
    const rows = byProv[prov].map((m) => {
      const on = sameModel(cur, m);
      const c = (m.cost && m.cost[0]) || null;
      const paid = c ? ((c.input || 0) > 0 || (c.output || 0) > 0) : null;
      const fee = c ? (paid ? `付费 in ${c.input}/out ${c.output}` : "免费") : "费用未知";
      const meta = [m.id, fee,
        (m.capabilities && m.capabilities.tools) ? "工具" : ""].filter(Boolean).join(" · ");
      const vars = (m.variants || []).map((v) => {
        const von = on && (cur.variant || "default") === v.id;
        return `<button type="button" data-id="${esc(m.id)}" data-prov="${esc(m.providerID)}"`
          + ` data-var="${esc(v.id)}"${von ? ' class="sel"' : ""}>${esc(v.id)}</button>`;
      }).join("");
      return `<div class="wp-it mdl-it${on ? " on" : ""}">`
        + `<button type="button" class="mdl-main" data-id="${esc(m.id)}" data-prov="${esc(m.providerID)}">`
        + modelIconHtml(m)
        + `<span class="mdl-txt"><b>${esc(m.name || m.id)}</b><i>${esc(meta)}</i></span>`
        + `</button>`
        + (vars ? `<div class="mdl-vars">${vars}</div>` : "")
        + `</div>`;
    }).join("");
    return `<div class="mdl-group">${esc(prov)}</div>` + rows;
  }).join("");
}

/** 切换：有会话 → 写进会话；没会话 → 存成"新会话默认" */
async function pickModel(ref) {
  const sid = state.current && state.current.id;
  // ★ 用户点的那一项必须真的在当前引擎的清单里（防止界面上还挂着上一个引擎的清单）
  if (!sid && !modelRefUsable(ref)) {
    setBanner(`模型 ${modelText(ref)} 不属于当前引擎；请刷新后重选`, { ms: 9000 });
    return;
  }
  if (!sid) {
    saveDefaultModel(ref);
    setBanner(`已设为新会话默认模型：${modelText(ref, true)}`);
    renderModels();
    updateModelBtn();
    return;
  }
  await ensureModelList();
  const list = MODEL.list || [];
  if (!modelRefUsable(ref) || (list.length && modelEngineMatches()
      && !list.some((m) => m.id === ref.id && m.providerID === ref.providerID))) {
    setBanner(`当前引擎没有模型 ${modelText(ref)}，未切换；请刷新后重选`, { ms: 9000 });
    return;
  }
  await api(`/api/session/${encodeURIComponent(sid)}/model`,
    { method: "POST", body: JSON.stringify({ model: ref }) });
  // 服务端在没给 variant 时会填 "default"，这里保持一致
  state.current.model = { id: ref.id, providerID: ref.providerID, variant: ref.variant || "default" };
  saveDefaultModel(state.current.model);            // ★ 顺带记为"新会话默认模型"，下次新建自动跟随
  const s = state.sessions.find((x) => x.id === sid);
  if (s) s.model = state.current.model;
  renderModels();
  updateModelBtn();
  setBanner(`已切换模型：${modelText(state.current.model, true)}`);
}

async function openModels() {
  $("mdl").hidden = false;
  renderModels();
  const q = $("mdl-q");
  if (q) { q.value = ""; q.focus(); }
  await loadModels();
  renderModels();
  OVERLAY.push("mdl", closeModels);
}

function closeModels() {
  $("mdl").hidden = true;
  OVERLAY.pop("mdl");
}

function initModelPicker() {
  const close = () => closeModels();
  $("btn-model").addEventListener("click", () => ($("mdl").hidden ? openModels() : close()));
  $("mdl-close").addEventListener("click", close);
  $("mdl").addEventListener("click", (e) => { if (e.target === $("mdl")) close(); });
  $("mdl-q").addEventListener("input", renderModels);
  $("mdl-list").addEventListener("click", (e) => {
    // ⚠ 用 data-prov / data-id / data-var；别用 data-theme（会被主题变量规则命中）
    const b = e.target.closest("button[data-id]");
    if (!b) return;
    const ref = { id: b.dataset.id, providerID: b.dataset.prov };
    if (b.dataset.var) ref.variant = b.dataset.var;
    pickModel(ref).then(close).catch((err) => alert("切换模型失败：" + err.message));
  });
  $("mdl-default").addEventListener("click", async () => {
    try {
      const d = unwrap(await api("/api/model/default"));
      if (!d || !d.id) throw new Error("没拿到默认模型");
      await pickModel({ id: d.id, providerID: d.providerID });
      close();
    } catch (err) { alert("用默认模型失败：" + err.message); }
  });
  window.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("mdl").hidden) close();
  });
}
