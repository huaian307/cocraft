/* 02-shell.js —— 主题 / 主题工坊 / 外部打开 / 落樱 / 侧栏 / 名字 / 附件（P1 从 app.js 拆出，纯搬运不改逻辑） */

/* ---------------- 内置主题 + 用户主题（主题工坊） ----------------
 * 两条属性：
 *   html[data-theme]      = hiru            —— 「底盘」（**夜主题已删除，只剩昼**），决定内置样式与背景槽
 *   html[data-theme-user] = <用户主题 id>    —— 用户主题身份（变量 / 图标 / 背景覆盖）
 * 用户变量注入在 [data-theme-user] 下（特异性更高）覆盖内置变量。详见 docs/THEMES.md。 */

const THEME_KEY = "cocraft.theme";
const THEME_USER_KEY = "cocraft.themeUser";
const THEME_USER_MODE_KEY = "cocraft.themeMode";

let _userThemeModes = [];   // 当前用户主题支持的明暗（[] = 无 / 内置）
let _userThemeMode = "";    // 当前用户主题正在用的 mode

const THEME_ICON_SLOTS = {
  sidebar: "#btn-sidebar", tools: "#btn-tools", agent: "#btn-agent", model: "#btn-model",
  refresh: "#btn-refresh", stop: "#btn-stop", home: "#p-home",
};

function themeUser() {
  try { return localStorage.getItem(THEME_USER_KEY) || ""; } catch { return ""; }
}

function themeUserMode() {
  try { return localStorage.getItem(THEME_USER_MODE_KEY) || ""; } catch { return ""; }
}

function _setBaseTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* 隐私模式忽略 */ }
}

/** 顶栏「夜/昼」按钮 —— **夜主题已删除**，这里保留为空实现（按钮也不在了）。 */
function _syncThemeButton() { /* no-op：夜主题已移除 */ }

function clearThemeIcons() {
  document.querySelectorAll("[data-ico-on]").forEach((el) => {
    el.removeAttribute("data-ico-on");
    el.style.removeProperty("--ico-val");
  });
}

function setThemeIcons(icons) {
  clearThemeIcons();
  for (const [slot, url] of Object.entries(icons || {})) {
    const sel = THEME_ICON_SLOTS[slot];
    if (!sel || !url) continue;
    const el = document.querySelector(sel);
    if (!el) continue;
    el.style.setProperty("--ico-val", 'url("' + url + '")');
    el.setAttribute("data-ico-on", "1");
  }
}

function setThemeStyle(css) {
  let el = document.getElementById("cocraft-theme");
  if (!el) { el = document.createElement("style"); el.id = "cocraft-theme"; document.head.appendChild(el); }
  el.textContent = css || "";
}

/** 主题自带背景：覆盖静态背景槽与 #live-bg 的兜底图。 */
function _themeBgCss(bg) {
  let css = "";
  if (bg && bg.light) {
    css += '#static-bg .sb[data-bg="hiru"]{background-image:url("' + bg.light + '");}\n';
    css += 'html[data-theme="hiru"] #live-bg{background-image:url("' + bg.light + '");}\n';
  }
  return css;
}

function _themeCss(id, payload) {
  const vars = Object.entries(payload.vars || {}).map(([k, v]) => k + ":" + v + ";").join("");
  return 'html[data-theme-user="' + id + '"]{' + vars + '}\n'
    + (payload.css || "") + "\n" + _themeBgCss(payload.background);
}

/** 清掉用户主题的注入（回到内置主题外观）。 */
function clearUserTheme() {
  delete document.documentElement.dataset.themeUser;
  setThemeStyle("");
  setThemeIcons({});
  _userThemeModes = [];
  _userThemeMode = "";
  try {
    localStorage.removeItem(THEME_USER_KEY);
    localStorage.removeItem(THEME_USER_MODE_KEY);
  } catch { /* ignore */ }
}

/** 应用内置主题（并清掉任何用户主题）—— **只有一个「昼」底盘**（夜主题已删除）。 */
function applyTheme(_theme) {
  clearUserTheme();
  _setBaseTheme("hiru");
  syncThemesActive();
}

/** 应用一个用户主题（从后端取变量 / CSS / 图标 / 背景）；`mode` 指定明或暗。 */
async function applyUserTheme(id, mode) {
  const url = "/themes/get?id=" + encodeURIComponent(id)
    + (mode ? ("&mode=" + encodeURIComponent(mode)) : "");
  const r = await api(url);
  if (!r || !r.ok) throw new Error((r && r.error) || "主题读取失败");
  _userThemeModes = (Array.isArray(r.modes) && r.modes.length) ? r.modes : [r.mode];
  _userThemeMode = r.mode;
  _setBaseTheme("hiru");                 // 只有一个「昼」底盘
  document.documentElement.dataset.themeUser = id;
  setThemeStyle(_themeCss(id, r));
  setThemeIcons(r.icons);
  try {
    localStorage.setItem(THEME_USER_KEY, id);
    localStorage.setItem(THEME_USER_MODE_KEY, r.mode);
  } catch { /* ignore */ }
  _syncThemeButton();
  syncThemesActive();
  return r;
}

function initTheme() {
  let user = "", umode = "";
  try {
    user = localStorage.getItem(THEME_USER_KEY) || "";
    umode = localStorage.getItem(THEME_USER_MODE_KEY) || "";
  } catch { /* ignore */ }
  applyTheme("hiru");                     // 夜主题已删除：只有昼
  if (user) applyUserTheme(user, umode).catch(() => { clearUserTheme(); });
}

/* ---------------- 主题工坊（浏览 / 安装 / 套用 / 卸载） ---------------- */

const TH = { builtin: [], installed: [], active: "", official: "", tab: "installed",
             remote: [], cand: [], offLoaded: false, offLoading: false, err: "" };

function openThemes() {
  const b = $("themes");
  if (!b) return;
  b.hidden = false;
  const f = $("th-install");
  if (f) f.hidden = true;
  const imp = $("th-import");
  if (imp) imp.textContent = "＋ 从 GitHub 安装…";
  loadThemes();
  OVERLAY.push("themes", closeThemes);
}

function closeThemes() { const b = $("themes"); if (b) b.hidden = true; OVERLAY.pop("themes"); }

function syncThemesActive() {
  const b = $("themes");
  if (b && !b.hidden) renderThemes();
}

async function loadThemes() {
  const cur = $("th-cur");
  if (cur) cur.textContent = "读取中…";
  try {
    const r = await api("/themes");
    TH.builtin = (r && r.builtin) || [];
    TH.installed = (r && r.installed) || [];
    TH.active = (r && r.active) || "";
    TH.official = (r && r.official) || "huaian307/cocraft-themes";
    TH.err = "";
  } catch (e) {
    TH.err = (e && e.message) ? e.message : String(e);
  }
  renderThemes();
}

function _themeThumb(t, builtin) {
  if (!builtin && t.hasPreview && t.preview) {
    const u = "/themes/asset?id=" + encodeURIComponent(t.id) + "&path=" + encodeURIComponent(t.preview);
    return '<img class="wp-thumb" src="' + esc(u) + '" alt="" loading="lazy">';
  }
  const dark = t.mode === "dark";
  return '<div class="wp-thumb th-swatch ' + (dark ? "dark" : "light") + '">'
    + (builtin ? "内置" : (dark ? "深色" : "浅色")) + "</div>";
}

function _themeCard(t, builtin) {
  const user = themeUser();
  const isCur = builtin ? (!user && document.documentElement.dataset.theme === t.id) : (user === t.id);
  const modes = Array.isArray(t.modes) ? t.modes : [t.mode];
  const modesLbl = modes.length > 1 ? "明/暗双模式" : (t.mode === "dark" ? "深色" : "浅色");
  const meta = '<div class="wp-meta"><b>' + esc(t.name || t.id) + "</b>"
    + "<i>" + esc(t.id) + " · " + esc(t.author || "匿名")
    + (t.version ? " · " + esc(t.version) : "") + " · " + modesLbl
    + (builtin ? " · 内置" : "") + "</i>"
    + (t.description ? '<i class="th-desc">' + esc(t.description) + "</i>" : "")
    + "</div>";
  const acts = ['<button type="button" data-th-apply="' + esc(t.id) + '" class="' + (isCur ? "sel" : "") + '">'
    + (isCur ? "当前" : "套用") + "</button>"];
  if (!builtin) {
    if (t.source) acts.push('<button type="button" data-th-update="' + esc(t.id) + '" title="按安装来源重装">更新</button>');
    acts.push('<button type="button" class="si-del" data-th-del="' + esc(t.id) + '" title="卸载">删</button>');
  }
  return '<div class="wp-it' + (isCur ? " th-on" : "") + '">'
    + _themeThumb(t, builtin) + meta + '<div class="wp-act">' + acts.join("") + "</div></div>";
}

function renderThemes() {
  const list = $("th-list");
  if (!list) return;
  const cur = $("th-cur");
  if (cur) {
    const user = themeUser();
    cur.textContent = TH.err ? ("读取失败：" + TH.err)
      : (user ? ("当前：用户主题 " + user) : ("当前：内置 " + (TH.active || "hiru")));
  }
  const foot = $("th-foot");
  if (foot) foot.textContent = "内置 " + TH.builtin.length + " · 已安装 " + TH.installed.length;
  document.querySelectorAll("#themes .th-tab").forEach((el) => el.classList.toggle("on", el.dataset.tab === TH.tab));

  if (TH.tab === "online") { renderThemesOnline(); return; }

  const cards = [];
  for (const t of TH.builtin) cards.push(_themeCard(t, true));
  for (const t of TH.installed) cards.push(_themeCard(t, false));
  list.innerHTML = cards.join("") || '<div class="muted skill-empty">没有主题</div>';
}

function renderThemesOnline() {
  const list = $("th-list");
  if (!list) return;
  if (TH.offLoading) { list.innerHTML = '<div class="muted skill-empty">正在读取官方仓库…</div>'; return; }
  if (!TH.remote.length) {
    list.innerHTML = '<div class="muted skill-empty">没有加载到在线主题。点「＋ 从 GitHub 安装…」粘贴来源，'
      + "或点「重新读取」再试官方仓库 <code>" + esc(TH.official || "") + "</code>。</div>";
    return;
  }
  list.innerHTML = TH.remote.map((t) => {
    const on = TH.installed.some((x) => x.id === t.id);
    const img = t.previewUrl
      ? '<img class="wp-thumb" src="' + esc(t.previewUrl) + '" alt="" loading="lazy">'
      : '<div class="wp-thumb th-swatch ' + (t.mode === "dark" ? "dark" : "light") + '">'
        + (t.mode === "dark" ? "深色" : "浅色") + "</div>";
    const meta = '<div class="wp-meta"><b>' + esc(t.name || t.id) + "</b>"
      + "<i>" + esc(t.id) + " · " + esc(t.author || "匿名") + (t.version ? " · " + esc(t.version) : "")
      + (t.bytes ? " · " + Math.max(1, Math.round(t.bytes / 1024)) + " KB" : "") + "</i>"
      + (t.description ? '<i class="th-desc">' + esc(t.description) + "</i>" : "")
      + (on ? '<i class="th-installed">已安装</i>' : "") + "</div>";
    return '<div class="wp-it">' + img + meta + '<div class="wp-act">'
      + '<button type="button" data-th-install="' + esc(t.id) + '"' + (on ? ' data-force="1"' : "") + ">"
      + (on ? "覆盖" : "安装") + "</button>"
      + '<button type="button" data-th-apply="' + esc(t.id) + '"' + (on ? "" : " disabled") + ">套用</button>"
      + "</div></div>";
  }).join("");
}

async function ensureOfficial() {
  if (TH.offLoaded) { renderThemes(); return; }
  TH.offLoading = true;
  renderThemes();
  try {
    const r = await api("/themes/preview", { method: "POST",
      body: JSON.stringify({ source: TH.official || "huaian307/cocraft-themes" }) });
    TH.remote = (r && r.themes) || [];
    TH.offLoaded = true;
    TH.err = "";
  } catch (e) {
    TH.remote = [];
    TH.err = (e && e.message) ? e.message : String(e);
  } finally {
    TH.offLoading = false;
    renderThemes();
  }
}

async function themesPreview() {
  const src = (($("th-src") && $("th-src").value) || "").trim();
  const msg = $("th-msg"), list = $("th-list2"), go = $("th-go");
  if (!src) { if (msg) msg.textContent = "请输入 owner/repo 或链接"; return; }
  if (msg) msg.textContent = "读取中…（拉取主题列表，仓库大时稍等）";
  if (go) go.disabled = true;
  if (list) list.innerHTML = "";
  TH.cand = [];
  try {
    const r = await api("/themes/preview", { method: "POST", body: JSON.stringify({ source: src }) });
    TH.cand = (r && r.themes) || [];
    if (!TH.cand.length) { if (msg) msg.textContent = "没找到主题（要有 theme.json；来源对吗？）"; return; }
    if (msg) msg.textContent = "找到 " + TH.cand.length + " 个主题，勾选后安装";
    if (list) list.innerHTML = TH.cand.map((t, i) =>
      '<label class="si-item"><input type="checkbox" data-i="' + i + '" checked>'
      + '<span class="si-name">' + esc(t.name || t.id) + '<span class="si-id">' + esc(t.id) + "</span></span>"
      + '<span class="si-meta">' + (t.files || 0) + " 文件 · "
      + Math.max(1, Math.round((t.bytes || 0) / 1024)) + " KB</span></label>").join("");
    if (go) go.disabled = false;
  } catch (e) {
    if (msg) msg.textContent = "读取失败：" + ((e && e.message) ? e.message : e);
    if (list) list.innerHTML = "";
  }
}

async function themesInstallIds(ids, force) {
  if (!ids.length) return;
  const src = (($("th-src") && $("th-src").value) || "").trim();
  const msg = $("th-msg");
  if (msg) msg.textContent = "安装中…";
  try {
    const r = await api("/themes/install", { method: "POST",
      body: JSON.stringify({ source: src, ids, force: !!force }) });
    const ok = (r.installed || []).length;
    const er = (r.errors || []).length;
    if (msg) msg.textContent = "已安装 " + ok + (er ? "，失败 " + er : "");
    if (er) setBanner("主题安装失败：" + (r.errors || []).map((x) => x.id).join("、"));
    else setBanner("已安装主题：" + ok + " 个");
    await loadThemes();
    TH.tab = "installed";
    renderThemes();
  } catch (e) {
    if (msg) msg.textContent = "安装失败：" + ((e && e.message) ? e.message : e);
    setBanner("主题安装失败：" + ((e && e.message) ? e.message : e));
  }
}

async function themesApply(id) {
  const builtin = TH.builtin.some((t) => t.id === id);
  try {
    if (builtin) applyTheme(id);
    else await applyUserTheme(id);
    api("/themes/apply", { method: "POST", body: JSON.stringify({ id }) }).catch(() => {});
    setBanner("已套用主题：" + id);
    renderThemes();
  } catch (e) {
    setBanner("套用失败：" + ((e && e.message) ? e.message : e));
  }
}

async function themesDelete(id) {
  if (!window.confirm("卸载主题「" + id + "」？会从磁盘删除，不可恢复。")) return;
  try {
    await api("/themes/uninstall", { method: "POST", body: JSON.stringify({ id }) });
    if (themeUser() === id) applyTheme(document.documentElement.dataset.theme);
    await loadThemes();
    setBanner("已卸载主题：" + id);
  } catch (e) {
    setBanner("卸载失败：" + ((e && e.message) ? e.message : e));
  }
}

async function themesUpdate(id) {
  try {
    const r = await api("/themes/update", { method: "POST", body: JSON.stringify({ id }) });
    if ((r.errors || []).length) setBanner("更新失败：" + r.errors[0].error);
    else setBanner("已更新主题：" + id);
    if (themeUser() === id) await applyUserTheme(id);
    await loadThemes();
  } catch (e) {
    setBanner("更新失败：" + ((e && e.message) ? e.message : e));
  }
}

function initThemes() {
  const open = $("cfg-themes");
  if (open) open.addEventListener("click", () => {
    const c = $("cfg"); if (c) cfgClose();
    openThemes();
  });
  const x = $("th-close"); if (x) x.addEventListener("click", closeThemes);
  const rl = $("th-reload"); if (rl) rl.addEventListener("click", () => {
    TH.offLoaded = false;
    if (TH.tab === "online") ensureOfficial(); else loadThemes();
  });
  const rst = $("th-reset");
  if (rst) rst.addEventListener("click", () => {
    applyTheme("hiru");
    setBanner("已恢复内置主题");
    renderThemes();
  });
  const imp = $("th-import");
  if (imp) imp.addEventListener("click", () => {
    const f = $("th-install");
    if (!f) return;
    f.hidden = !f.hidden;
    imp.textContent = f.hidden ? "＋ 从 GitHub 安装…" : "收起安装";
  });
  const pv = $("th-preview"); if (pv) pv.addEventListener("click", themesPreview);
  const srcIn = $("th-src");
  if (srcIn) srcIn.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); themesPreview(); } });
  const go = $("th-go");
  if (go) go.addEventListener("click", () => {
    const ids = [...document.querySelectorAll("#th-list2 input[type=checkbox]")]
      .filter((c) => c.checked).map((c) => (TH.cand[+c.dataset.i] || {}).id).filter(Boolean);
    const msg = $("th-msg");
    if (!ids.length) { if (msg) msg.textContent = "先勾选要安装的主题"; return; }
    themesInstallIds(ids, !!($("th-force") && $("th-force").checked));
  });
  document.querySelectorAll("#themes .th-tab").forEach((el) => el.addEventListener("click", () => {
    TH.tab = el.dataset.tab;
    if (TH.tab === "online") ensureOfficial(); else renderThemes();
  }));
  const box = $("themes");
  if (box) box.addEventListener("click", (e) => {
    if (e.target === box) { closeThemes(); return; }
    const a = e.target.closest("[data-th-apply]");
    if (a) { themesApply(a.dataset.thApply); return; }
    const ins = e.target.closest("[data-th-install]");
    if (ins) { themesInstallIds([ins.dataset.thInstall], ins.dataset.force === "1"); return; }
    const del = e.target.closest("[data-th-del]");
    if (del) { themesDelete(del.dataset.thDel); return; }
    const upd = e.target.closest("[data-th-update]");
    if (upd) { themesUpdate(upd.dataset.thUpdate); return; }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("themes") && !$("themes").hidden) closeThemes();
  });
}

/* ---------------- 在外部程序里打开当前会话目录 ---------------- */

/** 当前会话的工作目录（拿不到就空串）。 */
function currentDir() {
  const s = state.current;
  return (s && s.location && s.location.directory) || "";
}

/** 在外部程序里打开目录：what ∈ terminal / editor / explorer；不传 dir 就用当前会话目录。 */
async function openCurrentDir(what, dirArg) {
  const dir = dirArg || currentDir();
  if (!dir) { setBanner("当前会话没有工作目录：新建会话时可以指定一个"); return; }
  try {
    const r = await api("/open/dir", { method: "POST", body: JSON.stringify({ what, dir }) });
    setBanner(`已在「${(r && r.used) || what}」中打开：${dir}`);
  } catch (err) {
    setBanner("打开失败：" + (err && err.message ? err.message : err));
  }
}

/** 顶栏「工具」按钮的高亮：终端或编码模式任一打开就亮。 */
function syncToolsBtn() {
  const b = $("btn-tools");
  if (b) b.classList.toggle("on", !!(TERM.open || CODE.open));
}

/** 工具菜单（扳手）：面板终端 / 编码模式。打开期间保持展开，方便两个都切。 */
function initToolsMenu() {
  const btn = $("btn-tools");
  const menu = $("tools-menu");
  if (!btn || !menu) return;
  const refresh = () => {
    $("tm-dir").textContent = (CODE.open && CODE.root) ? CODE.root : (currentDir() || "（当前会话没有工作目录）");
    const t = $("tm-term"), c = $("tm-code"), q = $("tm-quar"), u = $("tm-undo");
    if (t) t.classList.toggle("on", !!TERM.open);
    if (c) c.classList.toggle("on", !!CODE.open);
    if (q) q.classList.toggle("on", !!($("quar") && !$("quar").hidden));
    if (u) u.classList.toggle("on", !!($("undo") && !$("undo").hidden));
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) { refresh(); menu.hidden = false; } else { menu.hidden = true; }
  });
  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tool]");
    if (!b) return;
    e.stopPropagation();
    if (b.dataset.tool === "term") termToggle();
    else if (b.dataset.tool === "code") codeToggle();
    else if (b.dataset.tool === "quar") { menu.hidden = true; quarOpen(); return; }
    else if (b.dataset.tool === "undo") { menu.hidden = true; undoOpen(); return; }
    refresh();
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) menu.hidden = true;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.hidden = true; });
}

/** 顶栏「群聊 / 音乐」小抽屉（取代原来右边那条透明触发带）。 */
function initHubMenu() {
  const btn = $("btn-hub"), menu = $("hub-menu");
  if (!btn || !menu) return;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) { syncHubBtn(); menu.hidden = false; } else { menu.hidden = true; }
  });
  menu.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    e.stopPropagation();
    const tool = b.dataset.tool || "";
    if (tool === "chat") { setCocraft(!cocraftIsOpen()); }
    else if (tool === "music") {
      const on = !MUSIC.shown;
      setPlayerShown(on);
      if (on) { try { setPlayerFold(false); } catch (err) { /* ignore */ } }   // 打开时展开，别只剩个 ▾
    }
    menu.hidden = true;
    syncHubBtn();
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) menu.hidden = true;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.hidden = true; });
  syncHubBtn();
}

/* ---------------- 落樱 + 落水涟漪 ----------------
 * 一片花瓣与它激起的涟漪同属一个 unit：计时与漂移由 unit 下发，天然严格同步。 */

function initPetals() {
  const box = $("petals");
  for (let i = 0; i < 16; i++) {
    const unit = document.createElement("span");
    unit.className = "petal-unit";
    unit.style.left = (Math.random() * 100).toFixed(1) + "vw";
    unit.style.setProperty("--dur", (11 + Math.random() * 12).toFixed(1) + "s");
    unit.style.setProperty("--delay", (-Math.random() * 22).toFixed(1) + "s");
    unit.style.setProperty("--drift", (Math.random() * 170 - 85).toFixed(0) + "px");

    const size = 7 + Math.random() * 8;
    const petal = document.createElement("span");
    petal.className = "petal";
    petal.style.width = size.toFixed(1) + "px";
    petal.style.height = size.toFixed(1) + "px";

    const ripple = document.createElement("span");
    ripple.className = "ripple";
    ripple.style.width = (size * 2.4).toFixed(1) + "px";
    ripple.style.height = (size * 0.85).toFixed(1) + "px";

    const ripple2 = document.createElement("span");
    ripple2.className = "ripple r2";
    ripple2.style.width = (size * 2.4).toFixed(1) + "px";
    ripple2.style.height = (size * 0.85).toFixed(1) + "px";

    unit.appendChild(petal);
    unit.appendChild(ripple);
    unit.appendChild(ripple2);

    if (Math.random() < 0.75) {                     // 不是每滴都溅起水花，随机更像真的
      const spike = document.createElement("span");
      spike.className = "spike";
      spike.style.width = "2.5px";
      spike.style.height = (size * 0.9).toFixed(1) + "px";
      unit.appendChild(spike);
    }
    box.appendChild(unit);
  }
  // 落樱 / 壁纸 已并入设置弹窗（顶栏不再单独放按钮）
}

/* ---------------- 侧栏收起 / 展开 ----------------
 * 收起 = 纯听歌模式：侧栏 + 对话消息都隐藏，只留歌词浮层 + 播放条 + 输入框。 */

const SIDEBAR_KEY = "cocraft.sidebar";

function applySidebar(mode) {
  document.documentElement.dataset.sidebar = mode;
  $("btn-sidebar").title = (mode === "closed" ? "展开（Ctrl+B）" : "收起（Ctrl+B）");
  try { localStorage.setItem(SIDEBAR_KEY, mode); } catch { /* 隐私模式忽略 */ }
  syncLyricsVisibility();
}

function initSidebar() {
  let mode = "open";
  try { mode = localStorage.getItem(SIDEBAR_KEY) || "open"; } catch { /* ignore */ }
  applySidebar(mode);
  const flip = () => applySidebar(
    document.documentElement.dataset.sidebar === "closed" ? "open" : "closed");
  $("btn-sidebar").addEventListener("click", flip);
  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && (e.key === "b" || e.key === "B")) {
      e.preventDefault();
      flip();
    }
  });
}

/* ---------------- 左上角名字 / 小标签（点击名字可改，设置里也能改） ---------------- */

const BRAND_KEY = "cocraft.brandName";
const BRAND_SUB_KEY = "cocraft.brandSub";
const BRAND_DEFAULT = "絵梨衣";
const BRAND_SUB_DEFAULT = "落尽红樱君不见，轻绘梨花泪沾衣";

function readBrand(key) {
  try { return localStorage.getItem(key) || ""; } catch { return ""; }
}

/** 名字：空值 = 恢复默认「絵梨衣」，并删掉本机记录 */
function applyBrandName(name) {
  const v = String(name ?? "").trim().slice(0, 24);
  const use = v || BRAND_DEFAULT;
  $("brand-name").textContent = use;
  syncTitle(`${use} · OpenCode`);          // ★B3：标题栏计数（未读 / 待批准）会加在它前面
  try {
    if (v) localStorage.setItem(BRAND_KEY, v);
    else localStorage.removeItem(BRAND_KEY);
  } catch { /* 隐私模式忽略 */ }
}

/** 小标签：空值 = 恢复默认题词，并删掉本机记录 */
function applyBrandSub(sub) {
  const v = String(sub ?? "").trim().slice(0, 40);
  const use = v || BRAND_SUB_DEFAULT;
  const el = $("brand-sub");
  if (el) el.textContent = use;
  try {
    if (v) localStorage.setItem(BRAND_SUB_KEY, v);
    else localStorage.removeItem(BRAND_SUB_KEY);
  } catch { /* 隐私模式忽略 */ }
}

function initBrandName() {
  const el = $("brand-name");
  applyBrandName(readBrand(BRAND_KEY));                 // 没有记录时就是 BRAND_DEFAULT
  applyBrandSub(readBrand(BRAND_SUB_KEY));

  el.addEventListener("click", () => {
    if (el.isContentEditable) return;
    el.contentEditable = "plaintext-only";            // 只收纯文本，不会粘进 HTML
    el.focus();
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  });
  el.addEventListener("blur", () => {
    el.contentEditable = "false";
    const v = (el.textContent || "").trim().slice(0, 24);
    applyBrandName(v || BRAND_DEFAULT);
  });
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); el.blur(); }
    if (e.key === "Escape") {
      e.preventDefault();
      el.textContent = readBrand(BRAND_KEY) || BRAND_DEFAULT;
      el.blur();
    }
  });
}

/* ---------------- 附件：粘贴 / 拖拽 / 选择文件 ----------------
 * prompt 接口的 files[].uri 接受 data: URL，服务端会规范化成 inline 附件。 */

const MAX_ATTACH_BYTES = 12 * 1024 * 1024;
const DOWNSCALE_OVER = 1.2 * 1024 * 1024;
const MAX_EDGE = 1600;
const MAX_ATTACH_COUNT = 8;

let attachSeq = 0;

/** 附件真实 mime 缺失时（某些粘贴来源 type 为空）按扩展名猜一个 */
function guessMime(name) {
  const ext = (String(name || "").toLowerCase().match(/\.([a-z0-9]+)$/) || [])[1] || "";
  return { png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif",
           webp: "image/webp", bmp: "image/bmp", avif: "image/avif" }[ext]
    || "application/octet-stream";
}

function humanSize(n) {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

const readAsDataURL = (blob) => new Promise((res, rej) => {
  const fr = new FileReader();
  fr.onload = () => res(String(fr.result));
  fr.onerror = () => rej(new Error("读取失败"));
  fr.readAsDataURL(blob);
});

/** 大图先缩到 MAX_EDGE 内再编码 —— 否则粘贴一张全屏截图就是十几 MB 的 base64 */
async function shrinkImage(file) {
  const needDownscale = file.size > DOWNSCALE_OVER;
  const bmp = await createImageBitmap(file);
  const scale = Math.min(1, MAX_EDGE / Math.max(bmp.width, bmp.height));
  if (!needDownscale && scale === 1) return file;
  const w = Math.max(1, Math.round(bmp.width * scale));
  const h = Math.max(1, Math.round(bmp.height * scale));
  const canvas = document.createElement("canvas");
  canvas.width = w; canvas.height = h;
  canvas.getContext("2d").drawImage(bmp, 0, 0, w, h);
  const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.86));
  if (!blob) return file;
  const base = (file.name || "image").replace(/\.[^.]+$/, "");
  return new File([blob], `${base}.jpg`, { type: "image/jpeg" });
}

async function addFiles(fileList) {
  const problems = [];
  for (const original of Array.from(fileList || [])) {
    if (state.attachments.length >= MAX_ATTACH_COUNT) {
      problems.push(`最多 ${MAX_ATTACH_COUNT} 个附件`);
      break;
    }
    let file = original;
    const looksImage = (file.type || "").startsWith("image/")
      || /\.(png|jpe?g|gif|webp|bmp|avif)$/i.test(file.name || "");
    if (looksImage) {
      try { file = await shrinkImage(file); } catch { /* 压缩失败就用原图 */ }
    }
    if (file.size > MAX_ATTACH_BYTES) {
      problems.push(`${original.name} 超过 ${humanSize(MAX_ATTACH_BYTES)}`);
      continue;
    }
    let dataUrl = "";
    try {
      dataUrl = await readAsDataURL(file);
    } catch {
      problems.push(`${original.name} 读取失败`);
      continue;
    }
    state.attachments.push({
      id: `att_${++attachSeq}`,
      name: file.name || "粘贴的图片.jpg",
      mime: file.type || guessMime(file.name),
      size: file.size,
      dataUrl,
    });
  }
  renderAttachRow();
  if (problems.length) alert(problems.join("\n"));
}

function renderAttachRow() {
  const row = $("attach-row");
  const list = state.attachments;
  row.hidden = !list.length;
  row.innerHTML = list.map((a) => {
    const isImg = /^image\//.test(a.mime) || /\.(png|jpe?g|gif|webp|bmp|avif)$/i.test(a.name || "");
    const thumb = isImg
      ? `<img class="thumb" src="${esc(a.dataUrl)}" alt="">`
      : `<span class="thumb">📄</span>`;
    return `<span class="attach" data-id="${esc(a.id)}" data-mime="${esc(a.mime || "")}"`
      + ` data-src="${esc(a.dataUrl || "")}" title="${isImg ? "点击预览大图" : ""}">${thumb}`
      + `<span class="meta"><b>${esc(a.name)}</b><i>${humanSize(a.size)}</i></span>`
      + `<button type="button" class="rm" title="移除">×</button></span>`;
  }).join("");
}

/** 图片大图预览（附件缩略图 / 消息里的图片都可以点开） */
function openImageView(src) {
  const box = $("img-view"), img = $("img-view-img");
  if (!box || !img || !src) return;
  img.src = src;
  box.hidden = false;
}
function closeImageView() {
  const box = $("img-view"), img = $("img-view-img");
  if (!box) return;
  box.hidden = true;
  if (img) img.src = "";
}
