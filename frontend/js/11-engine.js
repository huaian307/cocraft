/* 11-engine.js —— 设置 / 引擎 / 模式 / 技能 / 可逆执行 / 向导 / ACP agent / 安装 / 编辑（P1 从 app.js 拆出，纯搬运不改逻辑） */

/* ---------------- 动态壁纸：Wallpaper Engine 的 mp4 原片 / Scene 场景 ----------------
 * 收起侧栏 → **缓慢启动**：转速由静止慢慢加上去（场景壁纸则开始渲染）。
 * 展开会话 → **缓慢停止**：转速慢慢减到停 —— **停住的那一帧就是会话界面的静态背景**
 *            （直接把视频暂停在那里，不再抓帧、不再有 #bg-freeze 层；场景壁纸直接 pause）。
 * 没有可用原片（没装 WE / 没订阅 / 系统要求减少动态）→ 两侧都用主题自带的静态壁纸，不做动画。
 * 素材与选择由 server.py 提供：/live/wallpapers（生效清单）、/live/list + /live/pick（选择器）。
 * ⚠ scene 壁纸走 **WebWallGL**（第三方 MIT 构建产物，见 frontend/vendor/webwallgl/NOTICE.md）：
 *   浏览器 WebGL2 实时渲染 scene.pkg，**不需要 Wallpaper Engine 在后台跑**、不抓帧。 */

let liveRate = 0.8;             // 正常播放速度（1 = 原速）。可在设置面板里调（SET.rate）
const LIVE_RAMP_IN = 600;       // 缓慢启动：从静止加到 liveRate 用多久（ms）
const LIVE_RAMP_OUT = 1100;     // 缓慢停止：从当前速度减到静止用多久（ms）
// 起步速率：⚠ Chromium 的倍速**下限是 0.0625**（1/16），给更小会抛 NotSupportedError，
// 而且那个异常会把整个流程打断（踩过：0.06 导致"收起后完全没反应"）。所以取 0.3。
const LIVE_START_RATE = 0.3;
let liveReload = null;          // initLiveBg 里赋值，供壁纸选择器选完立刻换片
let liveApply = null;           // initLiveBg 里赋值：设置一变就立刻作用到动态壁纸
const SCENE_LIB = "/vendor/webwallgl/webwallgl.global.min.js";   // 懒加载，不进首屏

/* ---------------- 界面设置（顶栏「设」按钮）----------------
 * 存在 localStorage，改完立刻生效。以后新增的可调项也往这里放。
 * ⚠ SET 必须在 initLiveBg 之前声明（sync 里会读 SET.live），而 boot() 在文件末尾才跑，所以没问题。 */

const SET_KEY = "cocraft.settings";
const SET_DEFAULT = {
  dimHiru: 100, blur: 28, rate: 80,
  zoom: 100,                         // ★界面缩放（%）：整页等比放大/缩小（文字/间距/图标一起）
  live: true, taskbar: true, spectrumSides: true, spectrumSidesH: 100, spectrumSidesGain: 120,
  // ★「会话界面也动」：开启后**展开侧栏也继续渲染/播放**（视频与场景壁纸都适用）；关掉 = 只有收起侧栏才动
  liveAlways: false,
  petals: true, gold: true, bamboo: true, zh: true,
  glass: true,                       // ★液态玻璃皮肤（面板半透明磨砂 + 壁纸透出）
  // ★可逆执行：每轮工作区快照（发消息前留底 → 忏悔室版本时间线）。
  //   ⚠ **默认关**（用户 2026-10-16 反馈：把整个工作区镜像进内容仓太重）——
  //   关掉后**只保留破坏性操作的忏悔室**（那条不走这个开关）。
  turnSnap: false,
  // ★文件查看（`ui 0.2.86`）：agent 生成 / 修改文件后，自动在右侧抽屉里打开
  //   （当前文件有未保存改动时不切；自动打开不抢焦点）
  autoOpen: true,
  // 空会话首屏那几行文案（留空 = 用自带的）
  heroL1: "", heroL2: "", heroCredit: "",
};
const SET = Object.assign({}, SET_DEFAULT);

/** 自定义素材（头像 / 空会话主图与贴纸）—— 路径存在后端 `runtime/state/_appearance.json`，
 *  图片本身经 `/appearance/img?k=…` 代理出来（面板页是 http 的，直接引用 file:// 会被浏览器拦）。
 *  ⚠ 必须在 applySettings 之前声明：applySettings 里会调 applyAppearance()。 */
const APPE = { images: {}, version: 0 };
const HERO_DEFAULT = {
  l1: "这个会话还是一张白纸。",
  l2: "烛火未熄，静候你的话",
  credit: "烛在暗处，光在其中",
};

function loadSettings() {
  try { Object.assign(SET, JSON.parse(localStorage.getItem(SET_KEY) || "{}")); } catch { /* 隐私模式 */ }
  // ★老默认文案迁移：以前默认那句带「絵梨衣」（已经整体去掉）→ 存着旧默认的当"没设过"
  if (String(SET.heroL2 || "").trim() === "Sakura ＆ 絵梨衣 のDuck") SET.heroL2 = "";
}

function saveSettings() {
  try { localStorage.setItem(SET_KEY, JSON.stringify(SET)); } catch { /* 隐私模式 */ }
}

// 任务栏开关要告诉守护进程（它每轮读 /alive）；相同值不重复发。
let taskbarPrefSent = null;
function syncTaskbarPref() {
  const enabled = !!SET.taskbar;
  if (taskbarPrefSent === enabled) return;
  taskbarPrefSent = enabled;
  fetch("/panel/taskbar", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled }),
    cache: "no-store",
  }).catch(() => { taskbarPrefSent = null; });   // 失败就允许下次重试
}

/** 把设置写进页面：亮度变量 / 播放速度 / 落樱 / 动态壁纸 / 任务栏开关 */
function applySettings() {
  const root = document.documentElement;
  root.style.setProperty("--bg-dim", (SET.dimHiru / 100).toFixed(2));
  // ★统一的「模糊程度」—— 会话界面 / 消息气泡 / 群聊界面共用这一个值（0 = 全透明不模糊）
  root.style.setProperty("--g-blur", Math.max(0, SET.blur) + "px");
  // ★界面缩放：整页等比（文字 / 间距 / 图标一起），像浏览器缩放那样。
  //   `--ui-z` 是给 CSS 用的 —— `zoom` 会把 vw/vh 也一起放大（100vh 变 1.5 倍高 → 溢出），
  //   所以 style.css 里所有视口相对单位都写成 `calc(N / var(--ui-z))` 除回来。
  const _z = Math.max(0.5, Math.min(1.5, (Number(SET.zoom) || 100) / 100));
  root.style.setProperty("--ui-z", String(_z));
  root.style.zoom = String(_z);
  liveRate = Math.min(1.3, Math.max(0.5, SET.rate / 100));
  document.body.classList.toggle("no-petals", !SET.petals);
  document.body.classList.toggle("no-gold", !SET.gold);
  document.body.classList.toggle("no-bamboo", !SET.bamboo);
  root.classList.toggle("glass", !!SET.glass);               // ★液态玻璃皮肤：整页开关
  document.body.classList.toggle("no-lyrics-zh", !SET.zh);   // 歌词翻译：关掉就不显示小字译文
  document.documentElement.classList.toggle("spec-sides", !!SET.spectrumSides);
  root.style.setProperty("--spec-side-h", Math.max(20, Math.min(100, Number(SET.spectrumSidesH) || 100)) + "%");
  root.style.setProperty("--spec-side-gain", String(Math.max(20, Math.min(300, Number(SET.spectrumSidesGain) || 120))));
  ["cfg-spec-sides-h", "cfg-spec-sides-gain"].forEach((id) => {
    const row = $(id);
    if (row) row.hidden = !SET.spectrumSides;             // 只有开启两侧音频条才显示子项
  });
  if (liveApply) liveApply();
  if (typeof drawSpectrum === "function") drawSpectrum();    // 开关后立刻画一次两侧音频条
  applyAppearance();                                         // 左上角头像（可能被用户换过）
  syncTaskbarPref();                                         // 通知守护进程：任务栏是否隐藏
}

/** 当前界面缩放倍率（1 = 100%）。
 *  ★ CSS `zoom` 下：`getBoundingClientRect()` / `clientX` / `innerWidth` 给的是**真实像素**，
 *    而 CSS 变量里的 px 是**设计像素**（渲染时还会被 zoom 再放大一次）→ 拖动/定位类交互
 *    必须除以它换算，否则在非 100% 缩放下会错位（抽屉宽度、气泡球位置）。 */
function uiZoom() {
  return Math.max(0.5, Math.min(1.5, (Number(SET.zoom) || 100) / 100));
}

/** 改一项设置：落盘 → 生效 → 刷新设置面板 */
function setSetting(key, value) {
  SET[key] = value;
  saveSettings();
  applySettings();
  renderSettings();
  if (String(key).startsWith("hero")) refreshAppearanceArt();   // 空会话文案：立刻能预览
}

/* ---------------- 对话引擎（后端 /engine）---------------- */

const ENGINE = { active: "", available: [], engines: [] };

/** 引擎下拉的选项文案：不可用的标出来（`/engine/status.engines` 给的自述） */
function engineOptionsHtml(ids) {
  const byId = {};
  (ENGINE.engines || []).forEach((e) => { byId[e.id] = e; });
  return (ids || []).map((id) => {
    const d = byId[id] || {};
    const ok = d.available !== false;          // 老后端没有 engines 字段 → 视为可用
    const label = `${d.label || id}${ok ? "" : "（未就绪）"}`;
    return `<option value="${esc(id)}"${ok ? "" : " disabled"}>${esc(label)}</option>`;
  }).join("");
}

/** ★预热 ACP agent：冷启动实测 ~5.4s，热起来之后 ~0.5s。
 *  服务端在「启动 / 切引擎 / 切 agent」时也会预热；这里补「服务早起了但 agent 进程已死」那个缺口。
 *  只在引擎是 acp 时发，安静失败（预热只是加速，失败不影响任何功能）。 */
function acpPrewarm() {
  if (!ENGINE || ENGINE.active !== "acp") return;
  api("/engine/acp/prewarm", { method: "POST" }).catch(() => {});
}

/** 拉取可用引擎并填充设置里的下拉；老后端没这些接口就优雅降级（禁用 + 说明） */
async function loadEngineStatus() {
  const sel = $("cfg-engine");
  if (!sel) return;
  const note = $("cfg-engine-note");
  try {
    const st = await api("/engine/status");
    ENGINE.available = (st && st.available) || [];
    ENGINE.active = (st && st.active) || "";
    ENGINE.engines = (st && st.engines) || [];
    sel.innerHTML = engineOptionsHtml(ENGINE.available);
    sel.value = ENGINE.active;
    sel.disabled = ENGINE.available.length < 2;
    // 模式清单跟着引擎走（opencode 是 agent；acp 是审批模式，且要等会话）
    loadModes();
    // 「导入…」只有 ACP 引擎才有（opencode 引擎的会话列表本来就是它自己的全部会话）
    const impBtn = $("btn-import");
    if (impBtn) impBtn.hidden = ENGINE.active !== "acp";
    if (note) {
      const cur = ENGINE.engines.find((e) => e.id === ENGINE.active) || {};
      const parts = [];
      if (cur.available === false) parts.push("当前引擎未就绪");
      if (ENGINE.available.length < 2) parts.push("仅此一个可选");
      const hint = ENGINE.available.length
        ? `当前：${cur.label || ENGINE.active}${parts.length ? "（" + parts.join("；") + "）" : ""}`
        : "后端没有注册任何引擎";
      note.textContent = hint;
    }
  } catch {
    ENGINE.available = []; ENGINE.active = ""; ENGINE.engines = [];
    sel.innerHTML = "<option>不可用</option>";
    sel.disabled = true;
    if (note) note.textContent = "当前面板服务不支持引擎切换（重启后端后可用）";
  }
  loadAcpAgents();
}

/** 切换引擎：POST /engine，再清空当前会话并重载列表（不同引擎的会话不通用） */
/** 能不能把「上一条会话」接续进 ACP？
 *  ★只有 `opencode-acp` 与 OpenCode **共用同一个历史库**；codex / dsh 之类根本不认识那条 sessionId
 *    → 以前硬接会在列表里多出一条**同名空会话**（用户报过）。 */
async function setupCanContinueFromOpenCode() {
  try {
    const r = await api("/engine/acp/agents");
    const active = String((r && r.active) || "");
    if (active) { ACP.active = active; ACP.list = (r && r.agents) || ACP.list; }
    return active === "opencode-acp";
  } catch { return false; }
}

async function setEngine(id) {
  if (!id || id === ENGINE.active) return;
  // ★ 跨引擎接续：记下当前这条会话在 **agent 侧**的 id（ACP 用 agentSessionId，OpenCode 就是 id 本身），
  //   切完引擎后优先把"同一条会话"再接回来。
  //   ⚠ 主控（策划）会话不是 agent 侧会话：别拿 `planner:…` 去接续 —— 否则切完引擎又被拉回
  //     策划面板，看起来像"引擎切不了 / 会话切不走"。
  const prev = (state.current && state.current._kind !== "planner")
    ? { agent: state.current.agentSessionId || state.current.id,
        title: state.current.title || "" }
    : null;
  const sel = $("cfg-engine");
  if (sel) sel.disabled = true;
  try {
    await api("/engine", { method: "POST", body: JSON.stringify({ engine: id }) });
    // ★ 换引擎的第一件事：把「旧引擎的模型清单」作废，并按新引擎重拉。
    //   必须赶在 openSession()（它会触发 repairStaleModel）**之前** —— 否则会拿 ACP 的清单
    //   去判 OpenCode 的模型引用，把 `providerID:"acp"` 写进会话 → 提问 17ms 就 idle、没有任何回复。
    ENGINE.active = id;
    resetModels(id);
    // ★ 后台重拉，**不 await**：ACP 的 /api/model 要先 ensure_model_config（resume 最近会话/冷启动，
    //   实测数秒）—— 等它会把"切引擎"卡住。清单已同步作废（list=null），
    //   这段时间任何归一/写入都会走 fail-closed（不猜、不写非法引用），不会重现跨引擎写坏模型。
    loadModels().catch(() => {});
    setBanner(`已切换对话引擎：${id}`);
    // 换引擎 = 换后端：清空当前会话与消息区，并重连 SSE。
    // ⚠ /api/event 在「连接那一刻」就绑定了当时的引擎；不重连会一直收到旧引擎的事件。
    state.current = null;
    state.messages = [];
    state.live = {};
    state.reveal = {};
    state.pending = [];
    state.attachments = [];
    staticSig = null;
    liveShellSig = null;
    heroShown = false;
    resetMsgPaging();
    renderAttachRow();
    ctxSync();                                  // ★ 换引擎：旧引擎的上下文数字立刻清掉（别让它继续显示）
    $("session-title").textContent = "未选择会话";
    $("session-meta").textContent = "";
    $("input").disabled = true;
    $("btn-send").disabled = true;
    $("btn-stop").disabled = true;
    $("input").placeholder = "写在这里…… Enter 送出，Shift+Enter 换行（可粘贴/拖入图片）";
    renderMessages();
    if (typeof connectEvents === "function") connectEvents();
    await loadSessions();
    // ★ 先尝试"接续同一条会话"：对面已经有它就打开；没有、且切到的是 ACP，就把它「接续」进来。
    let resumed = false;
    if (prev && prev.agent) {
      const same = state.sessions.find((s) => s.id === prev.agent);
      if (same) {
        await openSession(same.id);
        resumed = true;
        setBanner(`已切换对话引擎：${id}（已接续当前会话）`);
      } else if (id === "acp" && await setupCanContinueFromOpenCode()) {
        try {
          const r = await api("/api/session/import", { method: "POST",
            body: JSON.stringify({ id: prev.agent, title: prev.title }) });
          const sid = (unwrap(r) || r || {}).id || "";
          if (sid) {
            await loadSessions();
            await openSession(sid);
            resumed = true;
            setBanner(`已切换对话引擎：${id}（已把上一条会话接续进来）`);
          }
        } catch { /* 接不上就算了，按普通流程打开最近一条 */ }
      }
    }
    // 换引擎后像开机一样自动打开最近一个会话，别停留在"未选择会话"空状态
    // （否则期间若有 SSE 事件会被误画到空状态下面）。
    // ⚠ 只挑**真实聊天会话**：策划会话排在最前面，打开它又会把输入框变成主控视图。
    const firstChat = (state.sessions || []).find((s) => !String(s.id || "").startsWith("planner:"));
    if (!resumed && firstChat && !state.current) {
      await openSession(firstChat.id);
    }
  } catch (err) {
    console.error("[engine] 切换引擎失败", err);
    alert("切换引擎失败：" + err.message);
  }
  await loadEngineStatus();
  acpPrewarm();                            // ★引擎是 acp 就先预热 agent 子进程（冷启动实测 ~5.4s）
}

/* ---------------- 模型 API Key（写进 ACP 共享基线；后端只回掩码）---------------- */

/** provider → 常用环境变量名（填 Key 时自动带出来） */
const KEY_NAME_BY_PROVIDER = {
  deepseek: "DEEPSEEK_API_KEY", openai: "OPENAI_API_KEY", anthropic: "ANTHROPIC_API_KEY",
  google: "GEMINI_API_KEY", xai: "XAI_API_KEY", moonshotai: "MOONSHOT_API_KEY",
  zhipuai: "ZHIPUAI_API_KEY", alibaba: "DASHSCOPE_API_KEY", mistral: "MISTRAL_API_KEY",
  groq: "GROQ_API_KEY",
};

/** 当前 ACP agent 大概是哪家 → 猜一个环境变量名 */
function guessKeyName() {
  const a = (ACP.list || []).find((x) => x.id === ACP.active) || {};
  return keyEnvForAgent(a);
}

/** 某个 agent 对应的 Key 环境变量名（同一套 provider 映射） */
function keyEnvForAgent(a) {
  const prov = String((a || {}).provider || (a || {}).providerGuess || "").toLowerCase();
  return KEY_NAME_BY_PROVIDER[prov] || "DEEPSEEK_API_KEY";
}

/* ---------------- ★模型凭据（多 Key）：同一家可以有多个账号 ---------------- */

const CRED = { items: [], defaults: {}, envNames: [], editId: "", itemsErr: "", open: {} };

const PROVIDER_LABELS = {
  deepseek: "DeepSeek", openai: "OpenAI", anthropic: "Anthropic", google: "Google",
  xai: "xAI", moonshotai: "Moonshot", zhipuai: "智谱", alibaba: "通义",
  mistral: "Mistral", groq: "Groq",
};

function credGroupKey(it) {
  return String((it && (it.provider || it.env)) || "其他");
}

function credGroupLabel(key, items) {
  const k = String(key || "");
  if (PROVIDER_LABELS[k.toLowerCase()]) return PROVIDER_LABELS[k.toLowerCase()];
  const env = String((items && items[0] && items[0].env) || k);
  const hit = Object.keys(KEY_NAME_BY_PROVIDER).find((p) => KEY_NAME_BY_PROVIDER[p] === env);
  return (hit && PROVIDER_LABELS[hit]) || env.replace(/_API_?KEY$|_KEY$|_TOKEN$/, "") || k;
}

async function loadCredentials() {
  try {
    const r = await api("/engine/acp/credentials");
    const d = (r && r.data) || {};
    CRED.items = d.items || [];
    CRED.defaults = d.defaults || {};
    CRED.envNames = d.envNames || [];
    CRED.itemsErr = "";
  } catch (e) {
    CRED.items = [];
    CRED.itemsErr = (e && e.message) ? e.message : String(e);
  }
  renderCredList();
}

function credAccountHtml(it) {
  const used = (it.usedBy || []).length ? "用于 " + it.usedBy.join("、") : "";
  return `<div class="cred-row">`
    + `<b title="${esc(it.label)}">${esc(it.label)}</b>`
    + `<span class="cred-env" title="${esc(it.env)}">${esc(it.env)}</span>`
    + `<span class="cred-mask" title="只显示掩码">${esc(it.masked)}</span>`
    + (it.isDefault ? `<span class="cred-tag def">默认</span>` : "")
    + `<span class="cred-used" title="${esc(used)}">${esc(used)}</span>`
    + `<span class="cred-acts">`
    + (it.isDefault ? "" : `<button type="button" data-cred-def="${esc(it.id)}">设默认</button>`)
    + `<button type="button" data-cred-edit="${esc(it.id)}">改</button>`
    + `<button type="button" data-cred-del="${esc(it.id)}">删</button>`
    + `</span></div>`;
}

/** 两层清单：厂商（默认收起，显示「N 个账号」）→ 点开看各家账号的掩码。 */
function renderCredList() {
  const box = $("cred-list");
  if (!box) return;
  if (!(CRED.items || []).length) {
    box.innerHTML = `<div class="cred-empty">${esc(CRED.itemsErr || "还没有凭据——点「＋ 添加凭据」")}</div>`;
    return;
  }
  const groups = {};
  (CRED.items || []).forEach((it) => {
    const k = credGroupKey(it);
    (groups[k] = groups[k] || []).push(it);
  });
  const keys = Object.keys(groups).sort((a, b) => a.localeCompare(b));
  if (keys.length === 1) CRED.open[keys[0]] = true;      // 只有一个厂商 → 直接展开（别让用户以为没东西）
  box.innerHTML = keys.map((k) => {
    const arr = groups[k];
    const open = !!CRED.open[k];
    const def = arr.find((x) => x.isDefault);
    return `<div class="cred-group">`
      + `<button type="button" class="cred-prov" data-cred-prov="${esc(k)}" aria-expanded="${open ? "true" : "false"}">`
      + `<span class="cred-caret">${open ? "▾" : "▸"}</span>`
      + `<b>${esc(credGroupLabel(k, arr))}</b>`
      + `<span class="cred-count">${arr.length} 个账号</span>`
      + (def ? `<span class="cred-defname">默认：${esc(def.label)}</span>` : "")
      + `</button>`
      + (open ? `<div class="cred-accts">${arr.map(credAccountHtml).join("")}</div>` : "")
      + `</div>`;
  }).join("");
}

function applyCred(r, focus) {
  const d = (r && r.data) || {};
  CRED.items = d.items || [];
  CRED.defaults = d.defaults || {};
  CRED.envNames = d.envNames || [];
  CRED.itemsErr = "";
  if (focus && (focus.env || focus.provider)) {
    CRED.open[credGroupKey(focus)] = true;               // 刚动过的那家展开，看得见结果
  }
  renderCredList();
}

async function openCredEdit(id) {
  if (!(CRED.items || []).length && !CRED.itemsErr) {
    try { await loadCredentials(); } catch { /* 后端老版本 */ }
  }
  const it = (CRED.items || []).find((x) => x.id === id) || {};
  CRED.editId = id || "";
  $("cred-title").textContent = id ? "编辑凭据" : "添加凭据";
  $("cred-label").value = it.label || "";
  const cur = (ACP.list || []).find((x) => x.id === ACP.active) || {};
  $("cred-env").value = it.env || keyEnvForAgent(cur);
  $("cred-provider").value = it.provider || "";
  $("cred-secret").value = "";
  $("cred-secret").placeholder = id ? "留空 = 不修改" : "sk-…";
  $("cred-msg").textContent = "保存后会重启 ACP 子进程（下一条消息用新 Key）";
  $("cred").hidden = false;
  OVERLAY.push("cred", closeCredEdit);
  try { $("cred-label").focus(); } catch { /* ignore */ }
}

function closeCredEdit() {
  $("cred").hidden = true;
  CRED.editId = "";
  OVERLAY.pop("cred");
}

async function saveCred() {
  const label = $("cred-label").value.trim();
  const env = $("cred-env").value.trim();
  const provider = $("cred-provider").value.trim();
  const secret = $("cred-secret").value.trim();
  const btn = $("cred-save");
  if (btn) btn.disabled = true;
  try {
    if (CRED.editId) {
      const body = { action: "update", id: CRED.editId, label, env, provider };
      if (secret) body.secret = secret;          // 留空 = 不修改 Key
      const r = await api("/engine/acp/credentials", { method: "POST", body: JSON.stringify(body) });
      applyCred(r, { env, provider });
      setBanner("已更新凭据：" + (label || env));
    } else {
      if (!secret) { $("cred-msg").textContent = "Key 不能为空"; return; }
      const r = await api("/engine/acp/credentials", { method: "POST",
        body: JSON.stringify({ action: "add", label, env, provider, secret }) });
      applyCred(r, { env, provider });
      setBanner("已添加凭据：" + (label || env));
    }
    closeCredEdit();
  } catch (e) {
    $("cred-msg").textContent = "保存失败：" + (e && e.message ? e.message : e);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function setCredDefault(id) {
  const it = (CRED.items || []).find((x) => x.id === id) || {};
  try {
    const r = await api("/engine/acp/credentials", { method: "POST",
      body: JSON.stringify({ action: "default", id }) });
    applyCred(r, { env: it.env, provider: it.provider });
    setBanner("已设为默认凭据");
  } catch (e) {
    setBanner("设默认失败：" + (e && e.message ? e.message : e), { sticky: true });
  }
}

async function deleteCred(id) {
  const it = (CRED.items || []).find((x) => x.id === id) || {};
  if (!window.confirm(`删除凭据「${it.label || id}」？只删本机保存的 Key，不影响已保存的会话。`)) return;
  try {
    const r = await api("/engine/acp/credentials", { method: "POST",
      body: JSON.stringify({ action: "delete", id }) });
    applyCred(r, { env: it.env, provider: it.provider });
    setBanner("已删除凭据：" + (it.label || id));
  } catch (e) {
    setBanner("删除失败：" + (e && e.message ? e.message : e), { sticky: true });
  }
}

/** 拉凭据并回填到某一行：where = "cfg"（列表）/ "setup"（向导里的快速添加） */
async function loadApiKeyRow(where) {
  const p = where === "setup" ? "setup" : "cfg";
  const varEl = $(p + "-key-var"), noteEl = $(p + "-key-note");
  if (!varEl && !noteEl) return;
  try {
    if (!(ACP.list || []).length) { try { await loadAcpAgents(); } catch { /* 老后端忽略 */ } }
    await loadCredentials();
    const env = guessKeyName();
    if (varEl) varEl.textContent = env;
    if (p === "cfg") return;                    // 设置里是列表，不再有单输入框
    const same = (CRED.items || []).filter((x) => x.env === env);
    const def = same.find((x) => x.isDefault) || same[0];
    if (noteEl) {
      noteEl.textContent = same.length
        ? `这个变量已有 ${same.length} 个 Key（默认：${def ? def.label : "—"}）；向导里填会新增一条并设为默认，留空跳过`
        : "还没有这个变量的 Key；可以留空，之后到设置 →「模型凭据」里添加";
    }
  } catch {
    if (noteEl) noteEl.textContent = "拿不到凭据列表（后端需支持 /engine/acp/credentials）";
    if (varEl && !varEl.textContent) varEl.textContent = "DEEPSEEK_API_KEY";
  }
}

/** 向导里的快速添加：新增一条凭据（并设为该 env 的默认）；留空 = 不动。 */
async function saveApiKey(where) {
  const p = where === "setup" ? "setup" : "cfg";
  const name = String((($(p + "-key-var") || {}).textContent) || "").trim() || guessKeyName();
  const value = String((($(p + "-key-value") || {}).value) || "").trim();
  const noteEl = $(p + "-key-note");
  if (!value) {
    if (noteEl) noteEl.textContent = "留空 = 不修改（要删除请到设置 →「模型凭据」）";
    return true;
  }
  try {
    const r = await api("/engine/acp/credentials", { method: "POST",
      body: JSON.stringify({ action: "add", label: name.replace(/_/g, " ") + " · 向导",
                             env: name, secret: value }) });
    applyCred(r);
    $(p + "-key-value").value = "";
    if (noteEl) noteEl.textContent = `已保存 ${name}（可在设置 →「模型凭据」里管理）`;
    setBanner("已保存模型 Key：" + name + "（改完会重启 ACP 子进程）");
    return true;
  } catch (e) {
    if (noteEl) noteEl.textContent = "保存失败：" + (e && e.message ? e.message : e);
    return false;
  }
}

/* ---------------- 智能体 / 模式切换（/api/agent + POST /api/session/{id}/agent）----------------
 * 两个引擎的语义都用同一个接口：
 *   opencode → OpenCode 的 agent（build / plan / general / explore…）
 *   acp      → ACP 的审批模式（read-only / agent / auto…，我们后端映射成同形状）
 * ⚠ 列表里 `hidden: true` 的是内部 agent（compaction / title / summary），**必须过滤**，
 *   否则弹窗里会混进一堆看不懂的东西（实测 OpenCode 返回 7 条、其中 3 条是 hidden）。
 * 模式是「会话属性」：没选会话时存成"新会话默认"，建完会话再补一次 POST 应用。 */

const MODE = { list: [], active: "", err: "" };
const MODE_KEY = "cocraft.agent";

function modeDefault() {
  try { return localStorage.getItem(MODE_KEY) || ""; } catch { return ""; }
}

/** 当前该显示哪个模式：优先"这个会话自己的"（会话对象上有 agent），再看 /api/agent 的 current */
function modeCurrent() {
  if (state.current && state.current.agent) return String(state.current.agent);
  return MODE.active || "";
}

function modeName(id) {
  const a = MODE.list.find((x) => x.id === id);
  return a ? (a.name || a.id) : (id || "");
}

async function loadModes() {
  try {
    const r = await api("/api/agent");
    MODE.list = (unwrap(r) || []).filter((a) => a && a.id && !a.hidden);   // ★ 过滤内部 agent
    MODE.active = ((MODE.list.find((a) => a.current) || {}).id) || "";
    MODE.err = "";
  } catch (e) {
    MODE.list = []; MODE.active = "";
    MODE.err = (e && e.message) ? e.message : String(e);
  }
  renderModeButton();
  renderModes();
}

function renderModeButton() {
  const b = $("btn-agent");
  if (!b) return;
  const cur = modeCurrent();
  const local = modeDefault();
  if (state.current) {
    b.textContent = cur || "模式";
    b.title = cur ? `当前模式：${modeName(cur)}（点开切换）` : "选择智能体 / 模式";
  } else {
    b.textContent = local || "模式";
    b.title = local ? `新会话默认模式：${modeName(local)}（点开修改）` : "选择智能体 / 模式（没选会话时 = 新会话默认）";
  }
  b.disabled = MODE.list.length === 0;
}

function openModes() {
  $("mode").hidden = false;
  renderModes();
  loadModes();
  OVERLAY.push("mode", closeModes);
}

function closeModes() { const b = $("mode"); if (b) b.hidden = true; OVERLAY.pop("mode"); }

function renderModes() {
  const box = $("mode-list");
  if (!box) return;
  const cur = modeCurrent();
  const local = modeDefault();
  const curEl = $("mode-cur");
  if (!MODE.list.length) {
    if (curEl) {
      curEl.textContent = MODE.err
        ? `拿不到模式清单：${MODE.err}`
        : "当前 agent / 引擎没有可切换的模式（或还没有会话）";
    }
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + `ACP 的模式清单来自会话（先建一条会话或选一条已有的），OpenCode 的模式来自 /api/agent。</div>`;
    return;
  }
  if (curEl) {
    curEl.textContent = state.current
      ? `当前会话的模式：${cur ? modeName(cur) : "（未知）"}`
      : `没选会话 → 这里的点击会记成「新会话默认」${local ? "（现在：" + modeName(local) + "）" : ""}`;
  }
  box.innerHTML = MODE.list.map((a) => {
    const on = a.id === cur && state.current;
    const isLocal = a.id === local;
    // 紧凑小圆片：只显示名字；id / 说明放 title（悬停才显现，且不占版面）
    const tip = [a.id, a.description].filter(Boolean).join(" · ");
    return `<button type="button" class="mode-chip${on ? " on" : ""}${isLocal ? " local" : ""}"`
      + ` data-mode="${esc(a.id)}" title="${esc(tip || a.id)}">${esc(a.name || a.id)}</button>`;
  }).join("");
}

async function pickMode(id) {
  if (!id) return;
  if (!state.current) {                       // 没选会话 → 记成新会话默认
    try { localStorage.setItem(MODE_KEY, id); } catch { /* 隐私模式 */ }
    setBanner(`已设为新会话默认模式：${modeName(id)}`);
    closeModes();
    renderModeButton();
    return;
  }
  const curEl = $("mode-cur");
  try {
    await api(`/api/session/${state.current.id}/agent`, {
      method: "POST", body: JSON.stringify({ agent: id }) });
    MODE.active = id;
    state.current.agent = id;
    try { localStorage.setItem(MODE_KEY, id); } catch { /* 隐私模式 */ }  // ★ 默认模式跟随最后一次选择
    setBanner(`已切换模式：${modeName(id)}（以后新会话也用这个）`);
    closeModes();
    renderModeButton();
    loadSessions();                            // 列表/详情里的 agent 也跟着变
  } catch (e) {
    if (curEl) curEl.textContent = "切换失败：" + (e && e.message ? e.message : e);
    setBanner("切换模式失败：" + (e && e.message ? e.message : e));
  }
}

/** 新建会话后：把"新会话默认模式"补一次（create 的 body 目前不吃 agent，和模型同款双保险） */
async function ensureSessionMode(sid) {
  const want = modeDefault();
  if (!want || !sid) return;
  try {
    if (!MODE.list.length) await loadModes();
    if (!MODE.list.some((x) => x.id === want)) return;   // 不同引擎的模式名不一样，不认识就不发
    if (MODE.active === want) return;
    await api(`/api/session/${sid}/agent`, { method: "POST",
      body: JSON.stringify({ agent: want }) });
    MODE.active = want;
    if (state.current && state.current.id === sid) {
      state.current.agent = want;
      renderModeButton();
    }
  } catch { /* 设不上就算了，不拦着用 */ }
}

/* ---------------- 技能 / 斜杠命令 ----------------
 * 面板统一接口（两个引擎都实现，见 backend/engines/base.py）：
 *   GET  /api/skill?directory=                     技能列表（不带正文）
 *   GET  /api/skill/{id}                           技能详情（含正文，备用）
 *   GET  /api/command?directory=                   斜杠命令
 *   POST /api/session/{id}/skill {id, resume?}     装填（默认不立刻跑）/ resume=true 立刻跑
 *   POST /api/session/{id}/command {name, text?}   执行命令
 * ⚠ ACP 的技能是**面板自己扫文件系统**发现的（协议没有 skill）；OpenCode 用它的 /api/skill。
 */

const SKILL = { list: [], cmds: [], loading: false, err: "" };

function skillDir() { return currentDir() || ""; }

function skillSourceLabel(src) {
  return ({
    "~/.config/opencode": "全局", "~/.claude": "全局·claude", "~/.agents": "全局·agents",
    "~/.codex": "全局·codex", ".codex": "项目·codex",
    ".opencode": "项目", ".claude": "项目·claude", ".agents": "项目·agents",
    builtin: "内置", opencode: "opencode",
  })[src] || src || "";
}

function openSkills() {
  const b = $("skill");
  if (!b) return;
  b.hidden = false;
  // 每次打开回到「列表」视图
  const main = $("skill-main"), inst = $("skill-install"), imp = $("skill-import");
  if (main) main.hidden = false;
  if (inst) inst.hidden = true;
  if (imp) imp.textContent = "＋ 安装技能";
  renderSkills();
  loadSkills();
  OVERLAY.push("skill", closeSkills);
}

function closeSkills() { const b = $("skill"); if (b) b.hidden = true; OVERLAY.pop("skill"); }

function renderSkills() {
  const cur = $("skill-cur");
  const sl = $("skill-list");
  const cl = $("cmd-list");
  if (!sl || !cl) return;
  const dir = skillDir();
  const hasSession = !!state.current;
  if (cur) {
    cur.textContent = SKILL.err
      ? ("读取失败：" + SKILL.err)
      : `${dir ? dir : "（无工作目录，只列全局）"}${hasSession ? "" : " · 未选会话"}`;
  }

  if (!SKILL.list.length) {
    sl.innerHTML = `<div class="muted skill-empty">`
      + `${SKILL.loading ? "读取中…" : "没有发现技能（放一个 <code>.opencode/skills/&lt;id&gt;/SKILL.md</code>）"}</div>`;
  } else {
    sl.innerHTML = SKILL.list.map((s) => {
      const tip = [s.name || s.id, s.id, s.description, s.path].filter(Boolean).join("\n");
      const p = s.path || "";
      const onDisk = p && !/^\/builtin\//i.test(p);   // 内置技能不是磁盘文件，不能更新/删
      return `<div class="skill-item" title="${esc(tip)}">`
        + `<b class="si-name">${esc(s.name || s.id)}</b>`
        + (s.source ? `<span class="si-src">${esc(skillSourceLabel(s.source))}</span>` : "")
        + `<span class="si-act">`
        + `<button type="button" data-skill-load="${esc(s.id)}"${hasSession ? "" : " disabled"}>装填</button>`
        + `<button type="button" class="si-run" data-skill-run="${esc(s.id)}"${hasSession ? "" : " disabled"} title="装填并立刻跑一轮">▶</button>`
        + (onDisk ? `<button type="button" class="si-upd" data-skill-update="${esc(p)}" title="按安装来源重新下载覆盖">更新</button>`
          + `<button type="button" class="si-del" data-skill-remove="${esc(p)}" title="从磁盘删除这个技能">删</button>` : "")
        + `</span></div>`;
    }).join("");
  }

  if (!SKILL.cmds.length) {
    cl.innerHTML = `<div class="muted skill-empty">`
      + `${SKILL.loading ? "读取中…" : "这个 agent 没有上报斜杠命令（ACP 里不是每个 adapter 都实现；dsh 就没有）。"
        + "仍可用顶栏「模式 / 模型」和左边的技能。"}</div>`;
  } else {
    cl.innerHTML = SKILL.cmds.map((c) => {
      const tip = [c.name, c.description].filter(Boolean).join("\n");
      return `<div class="skill-item" title="${esc(tip)}">`
        + `<b class="si-name">/${esc(c.name)}</b>`
        + `<span class="si-act"><button type="button" data-cmd-run="${esc(c.name)}"${hasSession ? "" : " disabled"}>执行</button></span>`
        + `</div>`;
    }).join("");
  }
}

async function loadSkills() {
  SKILL.loading = true;
  SKILL.err = "";
  renderSkills();
  const dir = skillDir();
  const q = dir ? ("?directory=" + encodeURIComponent(dir)) : "";
  try {
    const [sk, cm] = await Promise.all([
      api("/api/skill" + q).then((r) => (r && (r.data || unwrap(r))) || []).catch((e) => { SKILL.err = e.message; return []; }),
      api("/api/command" + q).then((r) => (r && (r.data || unwrap(r))) || []).catch(() => []),
    ]);
    SKILL.list = sk || [];
    SKILL.cmds = cm || [];
  } finally {
    SKILL.loading = false;
    renderSkills();
  }
}

async function useSkill(id, resume) {
  if (!state.current) { setBanner("先选中一个会话：技能要装填进会话里"); return; }
  try {
    await api(`/api/session/${encodeURIComponent(state.current.id)}/skill`,
      { method: "POST", body: JSON.stringify({ id, resume: !!resume }) });
    setBanner(resume ? `已激活并运行技能：${id}` : `已装填技能「${id}」：下一条消息会带上它`);
    closeSkills();
    await refreshMessages();
  } catch (e) {
    setBanner("技能失败：" + (e && e.message ? e.message : e));
    if ($("skill-cur")) $("skill-cur").textContent = "技能失败：" + (e && e.message ? e.message : e);
  }
}

async function runSlash(name) {
  if (!state.current) { setBanner("先选中一个会话"); return; }
  try {
    await api(`/api/session/${encodeURIComponent(state.current.id)}/command`,
      { method: "POST", body: JSON.stringify({ name, text: "" }) });
    setBanner(`已执行命令：/${name}`);
    closeSkills();
    await refreshMessages();
  } catch (e) {
    setBanner("命令失败：" + (e && e.message ? e.message : e));
    if ($("skill-cur")) $("skill-cur").textContent = "命令失败：" + (e && e.message ? e.message : e);
  }
}

/* 技能安装（通用 GitHub / URL）—— 引擎无关，写进跨工具的技能目录 */
let SKILL_CAND = [];

function siToggle() {
  const main = $("skill-main"), inst = $("skill-install"), btn = $("skill-import");
  if (!main || !inst) return;
  const opening = inst.hidden;
  inst.hidden = !opening;
  main.hidden = opening;
  if (btn) btn.textContent = opening ? "← 返回列表" : "＋ 安装技能";
}

async function siPreview() {
  const src = ($("si-src") && $("si-src").value || "").trim();
  const msg = $("si-msg"), list = $("si-list"), go = $("si-go");
  if (!src) { if (msg) msg.textContent = "请输入 owner/repo 或链接"; return; }
  if (msg) msg.textContent = "读取中…（拉取技能列表，仓库大时稍等）";
  if (go) go.disabled = true;
  if (list) list.innerHTML = "";
  SKILL_CAND = [];
  try {
    const r = await api("/skills/preview", { method: "POST", body: JSON.stringify({ source: src }) });
    SKILL_CAND = (r && r.skills) || [];
    if (!SKILL_CAND.length) { if (msg) msg.textContent = "没找到 SKILL.md（来源对吗？）"; return; }
    if (msg) msg.textContent = `${r.label || src} · 找到 ${SKILL_CAND.length} 个技能，勾选后安装`;
    if (list) list.innerHTML = SKILL_CAND.map((s, i) =>
      `<label class="si-item"><input type="checkbox" data-i="${i}" checked>`
      + `<span class="si-name">${esc(s.name || s.id)}<span class="si-id">${esc(s.id)}</span></span>`
      + `<span class="si-meta">${s.files} 文件 · ${Math.max(1, Math.round((s.bytes || 0) / 1024))} KB</span></label>`).join("");
    if (go) go.disabled = false;
  } catch (e) {
    if (msg) msg.textContent = "读取失败：" + (e && e.message ? e.message : e);
  }
}

async function siGo() {
  const src = ($("si-src") && $("si-src").value || "").trim();
  const msg = $("si-msg"), go = $("si-go");
  const ids = [...document.querySelectorAll("#si-list input[type=checkbox]")]
    .filter((c) => c.checked).map((c) => (SKILL_CAND[+c.dataset.i] || {}).id).filter(Boolean);
  if (!ids.length) { if (msg) msg.textContent = "先勾选要装的技能"; return; }
  const body = {
    source: src, ids,
    target: ($("si-target") && $("si-target").value) || "global-agents",
    force: !!($("si-force") && $("si-force").checked),
    directory: skillDir(),
  };
  if (go) go.disabled = true;
  if (msg) msg.textContent = "安装中…";
  try {
    const r = await api("/skills/install", { method: "POST", body: JSON.stringify(body) });
    const ok = (r.installed || []).length;
    const sk = (r.skipped || []).length;
    const er = (r.errors || []).length;
    if (msg) msg.textContent = `装好 ${ok} 个${sk ? "，跳过 " + sk : ""}${er ? "，失败 " + er : ""} → ${r.root || ""}`;
    setBanner(`技能安装完成：成功 ${ok}${sk ? " / 跳过 " + sk : ""}${er ? " / 失败 " + er : ""}`);
    await loadSkills();     // 刷新列表（返回时能看到新技能）
    if (go) go.disabled = false;
  } catch (e) {
    if (msg) msg.textContent = "安装失败：" + (e && e.message ? e.message : e);
    if (go) go.disabled = false;
  }
}

/** 更新技能：按安装时记下的来源重新下载覆盖。 */
async function updateSkill(path) {
  if (!path) return;
  setBanner("正在更新技能…");
  try {
    const r = await api("/skills/update", { method: "POST",
      body: JSON.stringify({ path, directory: skillDir() }) });
    setBanner(`技能已更新：${((r && r.installed) || []).map((x) => x.id).join("、") || path}`);
    await loadSkills();
  } catch (e) {
    setBanner("更新失败：" + (e && e.message ? e.message : e));
  }
}

/** 卸载技能：从磁盘删掉它的目录（二次确认）。 */
async function removeSkill(path) {
  if (!path) return;
  const parts = String(path).replace(/[\\/]+$/, "").split(/[\\/]/);
  let name = parts.pop() || path;
  if (/^skill\.(md|markdown)$/i.test(name) && parts.length) name = parts.pop();   // path 指向 SKILL.md → 用目录名
  if (!window.confirm(`卸载技能「${name}」？会从磁盘删除，不可恢复。`)) return;
  try {
    const r = await api("/skills/uninstall", { method: "POST",
      body: JSON.stringify({ path, directory: skillDir() }) });
    setBanner(`已卸载技能：${(r && r.id) || name}`);
    await loadSkills();
  } catch (e) {
    setBanner("卸载失败：" + (e && e.message ? e.message : e));
  }
}

/** 输入框左下角的「上下文 剩 X%」：点一下把详细用量打到横幅（悬停也有 tooltip）。 */
function initCtxChip() {
  const b = $("ctx-chip");
  if (b) b.addEventListener("click", ctxShowInfo);
}

function initSkillPicker() {
  const b = $("btn-skill");
  if (b) b.addEventListener("click", () => ($("skill").hidden ? openSkills() : closeSkills()));
  const x = $("skill-close");
  if (x) x.addEventListener("click", closeSkills);
  const rl = $("skill-reload");
  if (rl) rl.addEventListener("click", loadSkills);
  const imp = $("skill-import");
  if (imp) imp.addEventListener("click", siToggle);
  const pv = $("si-preview");
  if (pv) pv.addEventListener("click", siPreview);
  const go = $("si-go");
  if (go) go.addEventListener("click", siGo);
  const srcIn = $("si-src");
  if (srcIn) srcIn.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); siPreview(); }
  });
  const box = $("skill");
  if (box) {
    box.addEventListener("click", (e) => {
      if (e.target === box) { closeSkills(); return; }
      const load = e.target.closest("[data-skill-load]");
      if (load) { useSkill(load.dataset.skillLoad, false); return; }
      const run = e.target.closest("[data-skill-run]");
      if (run) { useSkill(run.dataset.skillRun, true); return; }
      const upd = e.target.closest("[data-skill-update]");
      if (upd) { updateSkill(upd.dataset.skillUpdate); return; }
      const rem = e.target.closest("[data-skill-remove]");
      if (rem) { removeSkill(rem.dataset.skillRemove); return; }
      const cmd = e.target.closest("[data-cmd-run]");
      if (cmd) { runSlash(cmd.dataset.cmdRun); return; }
    });
  }
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("skill") && !$("skill").hidden) closeSkills();
  });
}

/* ---------------- 从 agent 导入会话（ACP 的 session/list）----------------
 * 背景：ACP 引擎的面板会话列表 = **我们自己的索引**（`_acp_sessions.json`），
 * 所以用 `opencode-acp` 接进 OpenCode 时，OpenCode 那边**早就有的会话看不到**
 * （反过来在 ACP 里新建的会话会写进 OpenCode 的库，所以"本地看的到"）。
 * 这里把 agent 侧的会话列出来，点一下就领进面板（历史留在 agent 那边，接着聊即可）。 */

const IMP = { all: [], list: [], internal: [], tab: "project",
              cursor: null, supported: null, err: "" };

async function openImport() {
  const box = $("import");
  if (!box) return;
  IMP.tab = "project";
  box.hidden = false;
  OVERLAY.push("import", closeImport);
  $("imp-cur").textContent = "读取中…";
  $("imp-list").innerHTML = "";
  await loadImport();
  // 后端会在列会话时按 agent 侧自愈"接续会话的目录"，这里顺手刷新徽章
  loadSessions();
}

function closeImport() { const b = $("import"); if (b) b.hidden = true; OVERLAY.pop("import"); }

/* ---------------- ★可逆执行：忏悔室（工具 → 忏悔室） ----------------
 * 破坏性操作**放行前**先把要动的目标复制进 runtime/quarantine/（见 backend/snapshots.py）。
 * 这里只做三件事：列出 / 一键恢复 / 删除记录（忏悔室版本时间线与它共用快照底座）。 */

const QUAR = { items: [], stats: null, policy: null, plan: null, err: "", busy: false };

async function quarOpen() {
  const box = $("quar");
  if (!box) return;
  box.hidden = false;
  OVERLAY.push("quar", quarClose);           // ★P3：Esc 只关栈顶
  await quarLoad();
}

function quarClose() { const b = $("quar"); if (b) b.hidden = true; OVERLAY.pop("quar"); }

async function quarLoad() {
  const list = $("quar-list");
  if (list) list.innerHTML = `<div class="muted" style="padding:10px">读取中…</div>`;
  try {
    const r = unwrap(await api("/quarantine/list")) || {};
    QUAR.items = r.items || [];
    QUAR.stats = r.stats || null;
    QUAR.err = "";
  } catch (e) {
    QUAR.items = [];
    QUAR.stats = null;
    QUAR.err = (e && e.message) ? e.message : String(e);
  }
  renderQuar();
  // ★轮快照带内容开关（`ui 0.2.85`）：群聊 / 工作流的每一轮要不要连内容一起存
  try {
    const cfg = unwrap(await api("/snapshot/config")) || {};
    const btn = $("quar-snap-toggle");
    if (btn) {
      const on = !!cfg.groupContent;
      btn.classList.toggle("on", on);
      btn.textContent = on ? "轮快照带内容：已开（可一键退回）" : "轮快照带内容：关（只记文件名）";
    }
  } catch { /* 老后端没有这个接口 → 保持默认文案 */ }
  // ★低风险删除策略 + 保留期计划（`ui 0.2.95`）
  try {
    const p = unwrap(await api("/quarantine/plan")) || {};
    QUAR.plan = p;
    QUAR.policy = p.config || null;
  } catch { /* 老后端没有这个接口 → 保持 null（策略条显示为不可用） */ }
  quarRenderPolicy();
}

/** 切换「轮快照带内容」：开了才能把群聊 / 工作流的一轮退回去（代价是磁盘占用）。 */
async function quarToggleSnap() {
  try {
    const cur = unwrap(await api("/snapshot/config")) || {};
    const next = !cur.groupContent;
    await api("/snapshot/config", { method: "POST",
      body: JSON.stringify({ groupContent: next }) });
    setBanner(next
      ? "已打开「轮快照带内容」：群聊 / 工作流每一轮动手前连内容一起存 —— 之后能在审计页一键退回（会占磁盘）"
      : "已关闭「轮快照带内容」：之后只记文件名（省磁盘，但这一轮撤不回）", { ms: 9000 });
    await quarLoad();
  } catch (e) {
    setBanner("切换失败：" + ((e && e.message) || e)
      + (typeof backendStaleHint === "function" ? backendStaleHint("") : ""), { sticky: true });
  }
}

/* ---------------- ★低风险删除：策略（`ui 0.2.95`） ----------------
 * 评分在 `backend/file_rank.py`：★ 级 = 构建产物 / 缓存 / 日志 / 临时文件。
 * 只有「可完整隔离」时才自动放行；源码 / 配置 / git 跟踪文件、关机·杀进程·格式化
 * 一律照旧弹窗（它们没有可恢复的"原件"）。 */
function quarStars(n) {
  const k = Math.max(1, Math.min(5, Number(n) || 1));
  return "★".repeat(k) + "☆".repeat(5 - k);
}

function quarRenderPolicy() {
  const cfg = QUAR.policy || {};
  const btn = $("quar-auto");
  const days = $("quar-days");
  const note = $("quar-plan-note");
  if (btn) {
    const on = !!cfg.enabled;
    btn.classList.toggle("on", on);
    btn.textContent = on ? "低风险删除：开（★ 自动隔离）" : "低风险删除：关";
  }
  const cfgBtn = $("cfg-auto-delete");
  if (cfgBtn && QUAR.policy) {
    cfgBtn.classList.toggle("on", !!cfg.enabled);
    cfgBtn.textContent = cfg.enabled ? "开" : "关";
  }
  if (days && document.activeElement !== days) days.value = String(cfg.days || 7);
  const p = QUAR.plan || {};
  const c = p.counts || {};
  if (note) {
    if (!QUAR.policy) {
      note.textContent = "后端还没有这个接口（可能是旧服务）—— 重启一次面板服务再试。";
    } else {
      note.textContent = `到期前 ${cfg.remindHours || 24} 小时提醒 · 当前：`
        + `${c.soon || 0} 条快到期 / ${c.due || 0} 条可清 / ${c.hold || 0} 条需你确认`;
    }
  }
}

async function quarToggleAuto() {
  try {
    const cfg = QUAR.policy || {};
    const next = !cfg.enabled;
    const r = unwrap(await api("/quarantine/policy", { method: "POST",
      body: JSON.stringify({ enabled: next }) })) || {};
    QUAR.policy = r;
    const t = $("cfg-auto-delete");
    if (t) { t.classList.toggle("on", !!r.enabled); t.textContent = r.enabled ? "开" : "关"; }
    setBanner(next
      ? "已开启「低风险删除」：★ 级（构建产物 / 缓存 / 日志 / 临时文件）先隔离再自动放行；其它照旧问你"
      : "已关闭「低风险删除」：所有删除都照旧弹窗", { ms: 9000 });
    await quarLoad();
  } catch (e) {
    setBanner("切换失败：" + ((e && e.message) || e)
      + (typeof backendStaleHint === "function" ? backendStaleHint("") : ""), { sticky: true });
  }
}

async function quarSaveDays() {
  const el = $("quar-days");
  if (!el) return;
  const days = Math.max(1, Math.min(90, Number(el.value) || 7));
  el.value = String(days);
  try {
    const r = unwrap(await api("/quarantine/policy", { method: "POST",
      body: JSON.stringify({ days }) })) || {};
    QUAR.policy = r;
    setBanner(`忏悔室保留期已改为 ${days} 天（到期前 24 小时提醒）`, { ms: 7000 });
    await quarLoad();
  } catch (e) {
    setBanner("保存失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function quarCleanupNow() {
  if (QUAR.busy) return;
  const p = QUAR.plan || {};
  const c = p.counts || {};
  const n = Number(c.due || 0);
  if (!n) {
    setBanner(`现在没有可清理的到期项（${c.hold || 0} 条风险项需要你手动确认）`);
    return;
  }
  if (!confirm(`清理 ${n} 条到期记录？\n`
    + "只清「安全项」（原件已恢复 / 本来就是可再生成的东西）。\n"
    + "涉及 git 跟踪文件或无法判断的会保留。")) return;
  QUAR.busy = true;
  try {
    const r = unwrap(await api("/quarantine/cleanup", { method: "POST",
      body: JSON.stringify({}) })) || {};
    setBanner(`已清理 ${(r.dropped || []).length} 条 · 释放 ${fmtBytes(r.freed || 0)}`
      + ((r.held || []).length ? ` · ${r.held.length} 条风险项保留` : ""));
    await quarLoad();
  } catch (e) {
    setBanner("清理失败：" + ((e && e.message) || e), { sticky: true });
  } finally {
    QUAR.busy = false;
  }
}

/** 启动时检查一次：有快到期 / 需确认的记录就提示（点横幅直接开忏悔室）。 */
async function quarBootCheck() {
  try {
    const p = unwrap(await api("/quarantine/plan")) || {};
    QUAR.plan = p;
    QUAR.policy = p.config || null;
    quarRenderPolicy();
    const c = p.counts || {};
    const soon = Number(c.soon || 0), hold = Number(c.hold || 0), due = Number(c.due || 0);
    if (!soon && !hold && !due) return;
    const parts = [];
    if (soon) parts.push(`${soon} 条将在 ${(p.config || {}).remindHours || 24} 小时内到期`);
    if (due) parts.push(`${due} 条已到期（${(p.config || {}).remindHours || 24} 小时后清理安全项）`);
    if (hold) parts.push(`${hold} 条需要你确认（不会自动清）`);
    setBanner(`忏悔室：${parts.join(" · ")} —— 点这里检查项目文件是否完整`, {
      clickable: true, sticky: true, onClick: () => quarOpen(),
    });
  } catch { /* 老后端 / 没数据 → 安静 */ }
}

/** 启动后延迟检查（别和首屏抢带宽）：有快到期 / 需确认的记录就给一条可点横幅。 */
function initQuarantine() {
  setTimeout(() => { quarBootCheck(); }, 2500);
}

function quarWhen(ts) {
  const t = Number(ts || 0) * 1000;
  if (!t) return "";
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function renderQuar() {
  const box = $("quar-list");
  const foot = $("quar-foot");
  if (!box) return;
  if (QUAR.err) {
    box.innerHTML = `<div class="muted" style="padding:10px">读取失败：${esc(QUAR.err)}</div>`;
    if (foot) foot.textContent = "";
    return;
  }
  const items = QUAR.items || [];
  const st = QUAR.stats || {};
  if (foot) {
    const c = (QUAR.plan || {}).counts || {};
    const tail = QUAR.policy
      ? ` · 到期：${c.due || 0} 条可清 / ${c.hold || 0} 条需确认`
      : "";
    foot.textContent = (st.quarantine_dir
      ? `共 ${items.length} 条记录 · 忏悔室占用 ${fmtBytes(st.quarantine || 0)}（${st.quarantine_dir}）`
      : `共 ${items.length} 条记录`) + tail;
  }
  if (!items.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">还没有隔离记录。<br>`
      + `群聊里的 AI 成员执行**破坏性操作**时，放行前会把要动的文件备份到这里（面板侧的直接操作暂未接入）。</div>`;
    return;
  }
  box.innerHTML = items.map((it) => {
    const missed = Number(it.missed || 0);
    const auto = !!it.auto;
    const star = Number(it.max_star || 0);
    const ends = auto && it.expires_at ? quarWhen(it.expires_at) : "";
    return `<div class="q-row" data-qid="${esc(it.id)}">
      <div class="q-top">
        <b>🛡 ${it.count} 项</b>
        ${auto ? `<span class="q-badge auto" title="低风险删除：先隔离后自动放行">自动</span>` : ""}
        ${auto && star ? `<span class="q-stars" title="最高星级（越低越可再生成）">${quarStars(star)}</span>` : ""}
        ${it.tombstone ? `<span class="q-badge tomb" title="超过 64MB，只记了清单、没有复制内容（可再生文件）">仅清单</span>` : ""}
        <span class="q-size">${fmtBytes(it.bytes || 0)}</span>
        <span class="q-time" title="${esc(it.ts || "")}">${esc(quarWhen(it.taken_at))}</span>
      </div>
      <div class="q-note">${esc(it.note || "（无说明）")}</div>
      ${ends ? `<div class="q-ends">保留至 ${esc(ends)}（到期只清安全项；风险项永久保留）</div>` : ""}
      ${it.cmd ? `<div class="q-cmd mono" title="${esc(it.cmd)}">${esc(it.cmd)}</div>` : ""}
      ${missed ? `<div class="q-warn">⚠️ 另有 ${missed} 项没保住（太大 / 不存在 / 范围过大）—— 这几项找不回来</div>` : ""}
      <div class="q-acts">
        <button type="button" class="q-restore" data-qrestore="${esc(it.id)}"
          title="把备份放回原路径（目标已存在则不覆盖）">恢复全部</button>
        <button type="button" class="q-drop" data-qdrop="${esc(it.id)}" title="删掉这条隔离记录（**永久**，之后再也恢复不了）">删除记录</button>
      </div>
    </div>`;
  }).join("");
}

async function quarRestore(qid, override = false) {
  if (QUAR.busy) return;
  QUAR.busy = true;
  try {
    const r = await api("/quarantine/restore", { method: "POST",
      body: JSON.stringify({ id: qid, override }) });
    const n = Number(r.restored || 0);
    const cf = Number(r.conflict || 0);
    const bad = Number(r.failed || 0) + Number(r.missing || 0);
    if (cf && !override) {
      if (confirm(`有 ${cf} 项的目标位置**已经有文件**了（可能被重建过）。\n`
        + `要用忏悔室的备份**覆盖**它们吗？（取消 = 保留现在的文件）`)) {
        QUAR.busy = false;
        return quarRestore(qid, true);
      }
      setBanner(`已恢复 ${n} 项；${cf} 项因目标已存在跳过（保留现在的文件）`);
    } else {
      setBanner(`已恢复 ${n} 项` + (cf ? `，覆盖 ${cf} 项` : "") + (bad ? `，失败/缺失 ${bad} 项` : ""));
    }
    await quarLoad();
  } catch (e) {
    setBanner("恢复失败：" + (e && e.message ? e.message : e), { sticky: true });
  } finally {
    QUAR.busy = false;
  }
}

async function quarDrop(qid) {
  if (!confirm("删掉这条隔离记录？备份会被**永久**删除，之后再也恢复不了。")) return;
  try {
    await api("/quarantine/drop", { method: "POST", body: JSON.stringify({ id: qid }) });
    setBanner("已删除那条隔离记录");
    await quarLoad();
  } catch (e) {
    setBanner("删除失败：" + (e && e.message ? e.message : e));
  }
}

/* ---------------- ★可逆执行：每轮工作区快照（供忏悔室版本时间线） ----------------
 * 每发一条消息、**在 prompt 之前**给会话工作目录落一次快照（后端 `/snapshot/take`）。
 * ⚠ 「撤销这一轮」已删除（`ui 0.2.102`）：快照只作为忏悔室的版本来源，不再提供整轮批量撤销。 */

/** 轮开始落快照。**失败绝不挡住发消息**（返回 null）。开关关着就直接跳过。 */
async function snapTurnStart(sid, dir) {
  if (!SET.turnSnap) return null;      // 默认关；关着时仍有审批前自动留存
  if (!sid || !dir) return null;
  try {
    return unwrap(await api("/snapshot/take", { method: "POST",
      body: JSON.stringify({ root: dir, scope: String(sid), label: "turn", content: true }) }));
  } catch (e) {
    return null;                       // 安静失败：快照只是安全网，不该挡住干活
  }
}

async function loadImport() {
  try {
    // ★只列单聊/项目会话；工作流成员 / 群聊 AI 成员有各自的管理界面（不混进来）
    const r = await api("/api/session/remote");
    IMP.supported = !!(r && r.supported);
    IMP.all = (r && r.sessions) || [];
    IMP.list = IMP.all.filter((x) => !x.internal);
    IMP.internal = [];
    IMP.cursor = (r && r.cursor) || null;
    IMP.err = (r && r.error) || "";
  } catch (e) {
    IMP.supported = false;
    IMP.all = [];
    IMP.list = [];
    IMP.internal = [];
    IMP.err = (e && e.message) ? e.message : String(e);
  }
  renderImport();
}

function renderImport() {
  const box = $("imp-list");
  const cur = $("imp-cur");
  if (!box) return;
  if (IMP.supported === false) {
    if (cur) cur.textContent = IMP.err || "当前 agent / 引擎不支持列会话（session/list）";
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + `只有 ACP 引擎 + 支持 <code>session/list</code> 的 agent 才能列出来。`
      + `（OpenCode 自带的 ACP 支持；Codex 也支持）</div>`;
    return;
  }
  const rows = IMP.list || [];
  const n = rows.length;
  const fresh = rows.filter((x) => !x.imported).length;
  if (cur) {
    if (IMP.err) cur.textContent = IMP.err;
    else cur.textContent = n
      ? `单聊 / 项目会话 ${n} 条${fresh ? `，其中 ${fresh} 条还没接续` : "（都已在面板里）"}`
      : "agent 侧没有单聊 / 项目会话";
  }
  if (!n) {
    box.innerHTML = `<div class="muted" style="padding:10px">agent 那边没有单聊 / 项目会话。</div>`;
    return;
  }
  box.innerHTML = rows.map((s) => {
    const title = s.title || "(无标题)";
    const when = s.updatedAt ? String(s.updatedAt).replace("T", " ").slice(0, 16) : "";
    const hist = Number(s.messages || 0);
    const histTip = hist ? `<span class="muted" style="font-size:11px">对面 ${hist} 条历史</span>` : "";
    return `<div class="agents-row${s.imported ? " on" : ""}" data-imp="${esc(s.id)}">`
      + `<div class="an"><span class="dot ok"></span>${esc(title)}`
      + `<span class="muted" style="font-size:11px">${esc(s.id)}</span>`
      + histTip
      + `<span class="muted" style="font-size:11px;margin-left:auto">`
      + `${s.imported ? "已接续 · 点开" : "点一下接续"}</span>`
      // ★在 agent 侧删除（测试留下的 / 不想再接续的会话）；不碰面板本地记录
      + `<button type="button" class="p-x" data-imp-del="${esc(s.id)}" `
      + `title="从 agent 侧删除这条会话（不可撤销）">×</button>`
      + `</div>`
      + `<div class="ab">${esc(s.cwd || "")}${when ? "　·　" + esc(when) : ""}</div>`
      + `</div>`;
  }).join("");
}

/** 点一条：没接续过就 POST /api/session/import，然后打开它。
 *  后端会回 `attach`：bound=能否接回；historyImported=是否真读进了历史。 */
async function doImport(remoteId) {
  const s = (IMP.all || []).find((x) => x.id === remoteId)
    || (IMP.list || []).find((x) => x.id === remoteId)
    || (IMP.internal || []).find((x) => x.id === remoteId);
  if (!s) return;
  if (s.imported && s.localID) { closeImport(); await openSession(s.localID); return; }
  if (s.internal && !confirm("这条是群聊 AI 成员 / 本地协作创建的**内部会话**。\n"
    + "接续后它会作为普通会话出现在左边的项目会话列表里。确定要接续吗？")) return;
  const cur = $("imp-cur");
  if (cur) cur.textContent = "接续中…";
  try {
    const r = await api("/api/session/import", { method: "POST",
      body: JSON.stringify({ id: s.id, title: s.title || "" }) });
    const local = (r && r.data) || {};
    const at = local.attach || {};
    const name = local.title || s.id;
    if (!at.bound) {
      setBanner(`⚠ 这条会话在 agent 侧已无法接回（可能已删除），继续聊会新建一条：${name}`);
    } else if (at.historyImported) {
      setBanner(`已接续会话：${name}（读入 ${at.historyCount} 条历史）`);
    } else {
      setBanner(`已接续会话：${name}（历史留在 agent 那边，可直接接着聊）`);
    }
    closeImport();
    await loadSessions();
    if (local.id) await openSession(local.id);
  } catch (e) {
    if (cur) cur.textContent = "接续失败：" + (e && e.message ? e.message : e);
  }
}

/** ★从 **agent 侧**删除一条会话（「接续」列表里的 ×）。
 *  只删 agent 那边；如果这条已经在面板里接续过，左边列表里那条要单独删。 */
async function impDeleteRemote(id) {
  const s = (IMP.all || []).find((x) => String(x.id) === String(id)) || {};
  const title = s.title || id;
  const warn = s.imported
    ? "\n\n⚠ 这条已经接续进面板：这里只删 agent 那边，左边列表里的那条要单独删。"
    : "";
  if (!confirm(`从 agent 侧删除会话「${title}」？\n\n删了它就不会再出现在「接续」列表（对话历史也一并删除，无法撤销）。${warn}`)) return;
  try {
    await api("/api/session/remote/delete", { method: "POST",
      body: JSON.stringify({ id: s.id || id }) });
    setBanner(`已从 agent 侧删除：${title}`, { ms: 6000 });
    await loadImport();
    loadSessions();
  } catch (e) {
    setBanner("删除失败：" + ((e && e.message) || e)
      + (typeof backendStaleHint === "function" ? backendStaleHint("") : ""), { sticky: true });
  }
}

/* ---------------- 首次设置向导（默认引擎 / agent / 模型）---------------- */

const SETUP_KEY = "cocraft.setup.v1";
const SETUP = { open: false, engine: "", models: [] };

/** 首次启动 / 手动重跑：**一页填完**（引擎 / Agent / Key / 默认模型） */
async function openSetup() {
  const box = $("setup");
  if (!box || SETUP.open) return;
  SETUP.open = true;
  SETUP.models = [];
  box.hidden = false;
  OVERLAY.push("setup", closeSetup);
  $("setup-agent-row").hidden = true;
  $("setup-key-row").hidden = true;
  const done = $("setup-done");
  if (done) { done.hidden = false; done.disabled = false; }
  $("setup-note").textContent = "填完点「完成并重启」；只做一次，之后在设置里可重跑。";
  let st = null;
  try { st = await api("/engine/status"); } catch { st = null; }
  const avail = (st && st.available) || ["opencode", "acp"];
  ENGINE.engines = (st && st.engines) || [];
  ENGINE.available = avail;
  ENGINE.active = (st && st.active) || "";
  $("setup-engine").innerHTML = engineOptionsHtml(avail);
  const cur = ENGINE.active;
  if (cur && avail.includes(cur)) $("setup-engine").value = cur;
  // ⚠ 中性化：当前引擎不可用时，自动落到第一个「真的能用」的引擎，并把话说清楚
  const byId = {};
  ENGINE.engines.forEach((e) => { byId[e.id] = e; });
  if (cur && byId[cur] && byId[cur].available === false) {
    const ok = ENGINE.engines.find((e) => e.available !== false);
    if (ok) $("setup-engine").value = ok.id;
    $("setup-cur").textContent = `当前引擎「${byId[cur].label || cur}」未就绪`
      + `${byId[cur].error ? "：" + byId[cur].error : ""}`
      + (ok ? `；已先帮你选到可用的「${ok.label || ok.id}」。` : "；下面没有可用的引擎，可先「稍后再说」。");
  }
  await setupEngineChanged();
}

/** 引擎变化：ACP 才需要 agent + Key；选完立刻套用（否则模型清单还是旧引擎的） */
async function setupEngineChanged() {
  const engine = $("setup-engine").value;
  SETUP.engine = engine;
  const isAcp = engine === "acp";
  $("setup-agent-row").hidden = !isAcp;
  $("setup-key-row").hidden = !isAcp;
  if (isAcp) {
    await setupFillAgents();
    await loadApiKeyRow("setup");            // 只更新变量名 / 说明，**不动已输入的值**
  }
  // ★说明跟着引擎变：OpenCode 只用它自己的登录/配置，不需要在这里填 Key
  if (!SETUP.curDefault) SETUP.curDefault = $("setup-cur").textContent;
  if (isAcp) {
    $("setup-cur").textContent = "对话引擎 / Agent / 模型 Key / 默认模型 —— 一页填完，点「完成并重启」即可。";
  } else {
    $("setup-cur").textContent = "OpenCode 引擎：只需要选好默认模型（模型 Key 走 OpenCode 自己的登录/配置，不在这里填）。";
  }
  await setupApplyChoice(isAcp);
  await setupLoadModels();
  $("setup-note").textContent = isAcp
    ? "Key 留空 = 稍后再填；模型清单拉不到也能先完成。"
    : "选好默认模型后点「完成并重启」。";
}

/** 列 agent（保留当前选择，别把用户刚选的冲掉） */
async function setupFillAgents() {
  const sel = $("setup-agent");
  const prev = sel.value;
  sel.innerHTML = `<option value="">加载中…</option>`;
  try {
    await loadAcpAgents();
    const list = (ACP.list || []).filter((a) => a.available);
    if (!list.length) {
      sel.innerHTML = `<option value="">没有可用 agent</option>`;
      $("setup-note").textContent = "没有检测到可用的 ACP agent；可以「稍后再说」，之后到设置 → ACP 代理里添加。";
      return;
    }
    sel.innerHTML = list.map((a) =>
      `<option value="${esc(a.id)}">${esc(a.label || a.id)}</option>`).join("");
    const keep = prev || ACP.active;
    if (keep && list.some((a) => a.id === keep)) sel.value = keep;
  } catch {
    sel.innerHTML = `<option value="">拿不到 agent 列表</option>`;
  }
}

/** 把「引擎 / agent」套到后端（幂等；已经一致就什么都不做） */
async function setupApplyChoice(withAgent) {
  const engine = $("setup-engine").value;
  if (engine && ENGINE.active !== engine) {
    try { await setEngine(engine); } catch { /* 失败留给「完成」再报 */ }
  }
  if (!withAgent) return;
  const aid = $("setup-agent").value;
  if (aid && aid !== ACP.active) {
    try {
      const r = await api("/engine/acp/agents", { method: "POST", body: JSON.stringify({ id: aid }) });
      ACP.active = (r && r.active) || aid;
      ACP.list = (r && r.agents) || ACP.list;
      if (typeof updateAcpRow === "function") updateAcpRow();
    } catch { /* 同上 */ }
  }
}

/** 拉模型清单填进 ④（拿不到就留一句人话，不挡着完成） */
async function setupLoadModels() {
  const engine = $("setup-engine").value;
  const msel = $("setup-model");
  if (!msel) return;
  msel.innerHTML = `<option value="">加载中…</option>`;
  let list = [];
  try { list = unwrap(await api("/api/model")) || []; } catch { list = []; }
  if (!list.length && engine === "acp") {
    // ACP 的清单挂在会话上：一条都没有就先建一条，把 agent 拉起来
    try {
      const existing = unwrap(await api("/api/session?limit=1")) || [];
      if (!existing.length) await api("/api/session", { method: "POST", body: "{}" });
      list = unwrap(await api("/api/model")) || [];
    } catch { list = []; }
  }
  SETUP.models = list;
  if (!list.length) {
    msel.innerHTML = `<option value="">（暂时拿不到模型清单，可以先完成）</option>`;
    return;
  }
  msel.innerHTML = list.map((m) =>
    `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`).join("");
  // 优先选本地记着的默认模型，没有就第一个
  let want = "";
  try { want = (defaultModel() || {}).id || ""; } catch { want = ""; }
  if (want && list.some((m) => String(m.id) === String(want))) msel.value = want;
}

/** 完成：存 Key + 默认模型 → 标记已设置 → 重启页面（一次搞定，不再「下一步」） */
async function setupDone() {
  const btn = $("setup-done");
  if (btn) btn.disabled = true;
  const engine = $("setup-engine").value;
  let mid = "";
  try {
    await setupApplyChoice(engine === "acp");
    // 填了 Key 才保存；留空 = 不动现有凭据
    const kv = String(($("setup-key-value") || {}).value || "").trim();
    if (kv) {
      const ok = await saveApiKey("setup");
      if (!ok) {
        if (btn) btn.disabled = false;
        $("setup-note").textContent = "Key 没存上（" + (($("setup-key-note") || {}).textContent || "") + "）；改一下再试，或留空跳过。";
        return;
      }
    }
    mid = $("setup-model").value || "";
    if (mid) {
      const m = (SETUP.models || []).find((x) => String(x.id) === String(mid)) || {};
      saveDefaultModel({ id: mid, providerID: m.providerID || "", variant: "default" });
    }
  } catch (err) {
    if (btn) btn.disabled = false;
    $("setup-note").textContent = "保存失败：" + (err && err.message ? err.message : err);
    return;
  }
  setupMarkDone();
  closeSetup();
  setBanner(`首次设置完成：引擎 ${engine}${mid ? " · 模型 " + mid : ""}`);
  try { reloading = true; } catch { /* 旧版没有这个变量 */ }
  setTimeout(() => { try { location.reload(); } catch { /* ignore */ } }, 400);
}

/** 跳过 / 关闭：也标记完成，别每次启动都弹 */
function setupSkip() {
  setupMarkDone();
  closeSetup();
}

function setupMarkDone() {
  try { localStorage.setItem(SETUP_KEY, "1"); } catch { /* 隐私模式 */ }
}

function closeSetup() {
  const box = $("setup");
  if (box) box.hidden = true;
  SETUP.open = false;
  OVERLAY.pop("setup");
}

/* ---------------- ACP agentlist（/engine/acp/agents）---------------- */

const ACP = { active: "", list: [] };

async function loadAcpAgents() {
  try {
    const r = await api("/engine/acp/agents");
    ACP.active = (r && r.active) || "";
    ACP.list = (r && r.agents) || [];
  } catch {
    ACP.active = ""; ACP.list = [];
  }
  updateAcpRow();
  renderAgents();
}

function updateAcpRow() {
  const row = $("cfg-acp-row"), note = $("cfg-acp-note"), btn = $("cfg-acp-btn");
  if (!row) return;
  row.hidden = ENGINE.active !== "acp";
  const a = ACP.list.find((x) => x.id === ACP.active);
  if (btn) btn.textContent = a ? a.label : "选择 agent…";   // 按钮直接显示"当前用的是哪个"
  if (!note) return;
  note.textContent = a
    ? `当前使用：${a.label}${a.provider ? " · provider " + a.provider : ""}${a.available ? "" : "（缺：" + ((a.missing || []).join("、") || "依赖") + "）"} · 点右侧更换或新增`
    : "选择要驱动的 ACP agent";
}

function renderAgents() {
  const box = $("agents-list");
  if (!box) return;
  const cur = $("agents-cur");
  if (cur) cur.textContent = ACP.active ? `当前：${ACP.active}` : "（没有 agent）";
  if (!ACP.list.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">拿不到 agent 列表（后端需支持 /engine/acp/agents）</div>`;
    return;
  }
  box.innerHTML = ACP.list.map((a) => {
    const canDel = !a.builtin && a.id !== ACP.active;
    const prov = a.provider || "";
    const guess = !prov && a.providerGuess ? a.providerGuess : "";
    return `<div class="agents-row${a.id === ACP.active ? " on" : ""}" data-aid="${esc(a.id)}"`
      + ` title="${esc((a.command || []).join(" ") + (a.cwd ? "\n" + a.cwd : ""))}">`
      + `<div class="an"><span class="dot${a.available ? " ok" : ""}"></span>${esc(a.label)}`
      + `<span class="muted" style="font-size:11px">${esc(a.id)}</span>`
      + (prov ? `<span class="badge">${esc(prov)}</span>` : "")
      // provider 没配时给个"猜的"提示（点「编辑当前 agent…」可一键填入）
      + (guess ? `<span class="muted" style="font-size:11px" title="推断出的 provider，点「编辑当前 agent…」可填入">≈${esc(guess)}</span>` : "")
      + `<span class="muted" style="font-size:11px;margin-left:auto">`
      + `${a.available ? "可用" : esc("缺：" + ((a.missing || []).join("、") || "依赖"))}</span>`
      + (canDel ? `<button type="button" class="p-x" data-del="${esc(a.id)}" title="删除这个 agent">×</button>` : "")
      + `</div>`
      + (a.note ? `<div class="ab">${esc(a.note)}</div>` : "")
      + (a.dangerous ? `<div class="ab danger" title="${esc(a.dangerousNote || "")}">⚠ ${esc(a.dangerousNote || "该 agent 不请求权限，权限等级/群审批对它无效")}</div>` : "")
      + `</div>`;
  }).join("");
  // 每张卡片右上角补一个「改」（编辑名称/命令/目录/provider）
  box.querySelectorAll(".agents-row[data-aid] .an").forEach((an) => {
    const aid = an.parentElement.dataset.aid;
    if (!aid || an.querySelector("[data-edit]")) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "p-x";
    b.dataset.edit = aid;
    b.title = "编辑这个 agent（名称 / 命令 / 目录 / provider）";
    b.textContent = "改";
    const del = an.querySelector("[data-del]");
    if (del) an.insertBefore(b, del); else an.appendChild(b);
  });
}

/* ---------------- 新增 ACP agent：先列「发现的新 agent」，再退到「选文件夹」 ---------------- */

const ANEW = { list: [], summary: null, hints: [], busy: false };
const ANEW_KIND = { adapter: "适配器", subcommand: "子命令", client: "客户端", registered: "已登记" };

function openAgentsNew() {
  $("agents-new").hidden = false;
  renderCandidates();
  loadCandidates();
  loadInstallCatalog();          // ★面板内安装：先拉「能装什么 / 装过没有」
  OVERLAY.push("agents-new", closeAgentsNew);
}
function closeAgentsNew() {
  $("agents-new").hidden = true;
  AINST.stop();                  // 关掉弹窗就别再轮询安装进度（装还是会继续）
  OVERLAY.pop("agents-new");
}

/* ---------------- ★面板内安装 Agent（联网 npm；装到 agents\npm\<id>） ---------------- */

const AINST = {
  items: [], bundled: [], job: null, timer: null, err: "", node: null, auto: null, nodeBusy: false, nodeJob: null,
  stop() { if (this.timer) { clearTimeout(this.timer); this.timer = null; } },
};

async function loadInstallCatalog() {
  const note = $("anew-i-note");
  try {
    const r = unwrap(await api("/agent/catalog")) || {};
    AINST.items = r.items || [];
    AINST.bundled = r.bundled || [];
    AINST.err = "";
    if (r.status && r.status.busy) { AINST.job = r.status; AINST.poll(); }
    else if (r.status && r.status.phase) AINST.job = r.status;
    if (note) {
      note.textContent = (r.root ? "装到 " + r.root + " · " : "")
        + "需要 Node（随包自带）；国内会自动走镜像";
    }
  } catch (e) {
    AINST.items = [];
    AINST.err = (e && e.message) ? e.message : String(e);
    if (note) note.textContent = "读不到安装目录：" + AINST.err;
  }
  renderInstallCatalog();
  renderInstallLog();
  loadNodeInfo();                 // ★ Node 运行时：当前版本 / 更新 / 回滚
  loadAgentAuto();                // ★ 后台检查 / 自动更新配置
  loadAgentVersions();            // ★ 顺带问一下「有没有新版」（网络，慢也没关系：先显示已装）
}

/** 问 registry：装了的这些 agent 最新版是多少（后端缓存 10 分钟；force=1 强制刷新）。 */
async function loadAgentVersions(force) {
  try {
    const r = unwrap(await api("/agent/latest" + (force ? "?force=1" : ""))) || {};
    const items = r.items || [];
    const by = {};
    items.forEach((it) => { by[it.id] = it; });
    AINST.items = AINST.items.map((it) => {
      const v = by[it.id];
      if (!v) return it;
      return Object.assign({}, it, {
        installed: !!v.installed || it.installed,
        version: v.version || it.version || "",
        latest: v.latest || "",
        hasUpdate: !!v.hasUpdate,
        pkg: v.pkg || it.pkg || "",
        hasBackup: !!v.hasBackup || !!it.hasBackup,
        backupVersion: v.backupVersion || it.backupVersion || "",
      });
    });
    // 目录里有、但「可装目录」里没列出来的（自定义 npm 包）也补进来，让它们同样能更新
    items.forEach((v) => {
      if (!AINST.items.some((x) => x.id === v.id)) AINST.items.push(v);
    });
    renderInstallCatalog();
  } catch (e) {
    /* 查不到新版不影响安装 / 卸载 */ }
}

/** 「检查更新」按钮：强制刷新（不等 10 分钟缓存），并把结果写在说明那一行。 */
async function agentCheckUpdates() {
  const note = $("anew-i-note"), btn = $("anew-i-check");
  if (btn) btn.disabled = true;
  if (note) note.textContent = "正在问 registry 有没有新版…";
  await loadAgentVersions(true);
  if (btn) btn.disabled = false;
  if (note) {
    const n = (AINST.items || []).filter((x) => x.installed && x.hasUpdate).length;
    note.textContent = n ? `有 ${n} 个 agent 可以更新` : "已是最新（10 分钟内不再问）";
  }
}

/* ---------------- ★Node 运行时 + 自动更新（2026-11-03） ---------------- */
async function loadNodeInfo() {
  try {
    const r = unwrap(await api("/node/info")) || {};
    AINST.node = r;
    renderNode();
  } catch { /* 老后端没有这个接口 */ }
  // 如果上次关掉弹窗时更新还在跑，这里恢复轮询
  try {
    const s = unwrap(await api("/node/status")) || {};
    if (s.busy) {
      AINST.nodeJob = s;
      AINST.nodeBusy = true;
      renderNode();
      nodePoll();
    }
  } catch { /* 老后端 */ }
}

function renderNode() {
  const n = AINST.node || {};
  const job = AINST.nodeJob || {};
  const busy = !!(AINST.nodeBusy || job.busy);
  const cur = (n.current && n.current.version) || "";
  const latest = n.latest || "";
  const curEl = $("anew-node-cur"), latEl = $("anew-node-latest"), note = $("anew-node-note");
  if (curEl) curEl.textContent = cur ? `当前 ${cur}` : "当前未知";
  if (latEl) latEl.textContent = latest ? `最新 ${latest}` : "";
  const hasNew = !!(cur && latest
    && latest.replace(/^v/, "").localeCompare(cur.replace(/^v/, ""), undefined, { numeric: true }) > 0);
  const up = $("anew-node-up"), back = $("anew-node-back"), chk = $("anew-node-check");
  if (up) up.disabled = !hasNew || busy;
  if (back) back.disabled = !(n.backups && n.backups.length) || busy;
  if (chk) chk.disabled = busy;
  if (note) {
    if (busy && job.busy) {
      const pct = typeof job.percent === "number" ? job.percent : null;
      const mb = job.bytesTotal
        ? ` · ${(job.bytesDone / 1048576).toFixed(1)}/${(job.bytesTotal / 1048576).toFixed(1)} MB`
        : "";
      note.textContent = `${job.phase || "更新中"}${pct !== null ? " · " + pct + "%" : ""}` + mb
        + (job.elapsed ? ` · ${job.elapsed}s` : "")
        + (job.detail ? ` · ${job.detail}` : "");
    } else {
      const b = (n.backups || [])[0];
      note.textContent = b ? `旧版本备份：${b.name}（可回滚）`
        : "更新会先收起 ACP 子进程；旧版本会备份，可回滚。";
    }
  }
  const bar = $("anew-node-bar"), fill = bar && bar.querySelector("i");
  if (bar && fill) {
    bar.hidden = !busy;
    bar.classList.toggle("indet", busy && typeof job.percent !== "number");
    fill.style.width = (typeof job.percent === "number") ? `${job.percent}%` : "40%";
  }
}

async function nodeCheck() {
  try {
    const r = unwrap(await api("/node/check?force=1")) || {};
    AINST.node = Object.assign({}, AINST.node || {}, r);
    renderNode();
    setBanner(r.hasUpdate ? `Node 有新版 ${r.latest}（当前 ${r.current || "?"}）` : "Node 已是最新（LTS）", { ms: 7000 });
  } catch (e) { setBanner("检查 Node 失败：" + ((e && e.message) || e), { sticky: true }); }
}

async function nodeApply() {
  if (AINST.nodeBusy) return;
  const n = AINST.node || {};
  const curMajor = String((n.current && n.current.version) || "").replace(/^v/, "").split(".")[0];
  const newMajor = String(n.latest || "").replace(/^v/, "").split(".")[0];
  if (curMajor && newMajor && curMajor !== newMajor
    && !confirm(`Node ${curMajor}.x → ${newMajor}.x 是跨大版本升级。\n\n`
      + "上游 agent 未必马上兼容；更新后如果跑不起来，可以点「回滚」回到旧版本。继续吗？")) return;
  if (!confirm(`更新 Node 运行时${n.latest ? " 到 " + n.latest : ""}？\n\n`
    + "会先收起正在跑的 ACP 子进程（会话可能中断）；旧版本会备份，可回滚。")) return;
  AINST.nodeBusy = true;
  AINST.nodeJob = { busy: true, phase: "准备", percent: 0, elapsed: 0, detail: "" };
  renderNode();
  try {
    await api("/node/apply", { method: "POST",
      body: JSON.stringify({ version: n.latest || "" }) });
    nodePoll();
  } catch (e) {
    AINST.nodeBusy = false; AINST.nodeJob = null; renderNode();
    setBanner("更新 Node 失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function nodePoll() {
  try {
    const s = unwrap(await api("/node/status")) || {};
    AINST.nodeJob = s;
    AINST.nodeBusy = !!s.busy;
    renderNode();
    if (s.busy) { setTimeout(nodePoll, 500); return; }
    AINST.nodeBusy = false;
    await loadNodeInfo();
    if (s.ok) {
      setBanner(`Node 已更新到 ${s.version || "?"}（旧版本已备份，可回滚）`, { ms: 9000 });
    } else if (s.error) {
      setBanner("更新 Node 失败：" + s.error, { sticky: true });
    }
  } catch (e) {
    AINST.nodeBusy = false;
    renderNode();
    setBanner("读取 Node 进度失败：" + ((e && e.message) || e), { sticky: true });
  }
}

async function nodeRollback() {
  if (AINST.nodeBusy) return;
  if (!confirm("回滚 Node 到上一版？会先收起 ACP 子进程。")) return;
  AINST.nodeBusy = true; renderNode();
  try {
    const r = unwrap(await api("/node/rollback", { method: "POST", body: JSON.stringify({}) })) || {};
    setBanner(`已回滚 Node 到 ${r.version || "上一版"}`, { ms: 9000 });
    await loadNodeInfo();
  } catch (e) { setBanner("回滚 Node 失败：" + ((e && e.message) || e), { sticky: true }); }
  finally { AINST.nodeBusy = false; renderNode(); }
}

async function loadAgentAuto() {
  try { AINST.auto = unwrap(await api("/agent/auto")) || {}; renderAgentAuto(); } catch { /* ignore */ }
}

function renderAgentAuto() {
  const a = AINST.auto || {};
  const c = $("anew-auto-check"), i = $("anew-auto-install");
  if (c) { c.classList.toggle("on", !!a.check); c.textContent = "后台检查：" + (a.check ? "开" : "关"); }
  if (i) {
    i.classList.toggle("on", !!a.autoInstall);
    i.textContent = "自动更新：" + (a.autoInstall ? "开（" + (a.channel === "all" ? "全部" : "patch") + "）" : "关");
  }
}

async function toggleAgentAuto(kind) {
  const cur = AINST.auto || {};
  const body = { [kind]: !cur[kind] };
  if (kind === "autoInstall" && body[kind] && !cur.check) body.check = true;
  try {
    AINST.auto = unwrap(await api("/agent/auto", { method: "POST", body: JSON.stringify(body) })) || {};
    renderAgentAuto();
    setBanner(kind === "autoInstall"
      ? (AINST.auto.autoInstall ? "已开启自动更新：只在空闲时更新，默认只装 patch" : "已关闭自动更新")
      : (AINST.auto.check ? "已开启后台检查（每 6 小时）" : "已关闭后台检查"), { ms: 7000 });
  } catch (e) { setBanner("保存失败：" + ((e && e.message) || e), { sticky: true }); }
}

async function agentRollback(id) {
  if (!confirm(`回滚 ${id} 到上一版？\n\n会先收起正在跑的 ACP 子进程。`)) return;
  try {
    const r = unwrap(await api("/agent/rollback", { method: "POST",
      body: JSON.stringify({ id }) })) || {};
    setBanner(`已回滚 ${id} 到 ${r.version || "上一版"}`, { ms: 9000 });
    await loadInstallCatalog();
  } catch (e) { setBanner("回滚失败：" + ((e && e.message) || e), { sticky: true }); }
}

/** 启动时优先读缓存（不联网）；没有缓存就 20 秒后再联网查一次。 */
async function agentBootCheck(net) {
  try {
    const r = unwrap(await api("/agent/updates" + (net ? "" : "?cached=1"))) || {};
    const n = (r.items || []).length + (r.node && r.node.hasUpdate ? 1 : 0);
    if (n) {
      setBanner(`有 ${n} 项可以更新（Agent / Node）—— 点这里打开`, {
        clickable: true, sticky: true, onClick: () => openAgentsNew(),
      });
      return;
    }
    if (!net) setTimeout(() => agentBootCheck(true), 20000);
  } catch { /* 老后端 */ }
}

function initAgentUpdates() {
  setTimeout(() => { agentBootCheck(false); }, 4000);
}

function renderInstallCatalog() {
  const box = $("anew-i-list");
  if (!box) return;
  const busy = !!(AINST.job && AINST.job.busy);
  const bundled = (AINST.bundled || []).map((b) => {
    const btn = b.installed
      ? `<button type="button" class="anew-i-b" data-ainst-go="${esc(b.managed)}" title="装一份可独立更新的 npm 版到 agents/npm/（不覆盖随包目录）">装可更新版</button>`
      : "";
    return `<div class="anew-i-row"><b>${esc(b.label)}</b><span class="anew-i-tag ok">随包</span>`
      + (b.version ? `<span class="anew-i-ver">v${esc(b.version)}</span>` : "")
      + `<span class="anew-i-note">${esc(b.note)}</span>${btn}</div>`;
  }).join("");
  const head = bundled
    ? `<div class="anew-bundled-hd">随包 agent（默认随 cocraft 更新；要独立更新就装「可更新版」）</div>${bundled}`
    : "";
  if (!AINST.items.length && !bundled) {
    box.innerHTML = `<div class="muted" style="padding:4px 0">${esc(AINST.err || "没有可安装项")}</div>`;
    return;
  }
  box.innerHTML = head + AINST.items.map((it) => {
    const hasNew = !!(it.hasUpdate && it.latest);
    const tag = it.installed
      ? `<span class="anew-i-tag ok">已装</span>`
        + (it.version ? `<span class="anew-i-ver" title="已装版本">v${esc(it.version)}</span>` : "")
        + (hasNew
          ? `<span class="anew-i-tag new" title="registry 上的最新版本">最新 v${esc(it.latest)}</span>`
          : (it.latest ? `<span class="anew-i-tag" title="${esc(it.latest)}">已是最新</span>` : ""))
      : (it.ready ? "" : `<span class="anew-i-tag bad">缺 Node</span>`);
    const off = `<button type="button" class="anew-i-b" data-ainst-off="${esc(it.id)}" title="从注册表移除并删掉装的文件">卸载</button>`;
    const back = it.hasBackup
      ? `<button type="button" class="anew-i-b" data-ainst-back="${esc(it.id)}" title="回滚到 ${esc(it.backupVersion || "上一版")}">回滚</button>`
      : "";
    const btn = it.installed
      ? (hasNew
        ? `<button type="button" class="anew-i-b up" data-ainst-up="${esc(it.id)}" title="npm 装最新版（${esc(it.pkg || it.id)}@latest）"${busy ? " disabled" : ""}>更新</button>`
        : "") + back + off
      : `<button type="button" class="anew-i-b" data-ainst-go="${esc(it.id)}"${(busy || !it.ready) ? " disabled" : ""}>安装</button>`;
    return `<div class="anew-i-row">`
      + `<b>${esc(it.label)}</b>${tag}`
      + `<span class="anew-i-pkg" title="${esc(it.pkg)}">${esc(it.pkg)}</span>`
      + `<span class="anew-i-note" title="${esc(it.note)}">${esc(it.note)}</span>`
      + btn + `</div>`;
  }).join("");
}

function renderInstallLog() {
  const box = $("anew-i-log");
  if (!box) return;
  const j = AINST.job;
  if (!j || (!j.busy && !j.phase)) { box.hidden = true; box.innerHTML = ""; return; }
  const lines = (j.log || []).slice(-8);
  const upd = j.kind === "update";
  const head = j.busy
    ? `正在${upd ? "更新" : "安装"} · ${esc(j.id)}（${esc(j.phase || "…")}）`
    : (j.ok
      ? `${upd ? "更新完成" : "装好了"}：${esc(j.agent || j.id)}${j.version ? " · v" + esc(j.version) : ""}`
      : `${upd ? "更新" : "安装"}失败（${esc(j.error || j.phase || "")}）`);
  const pct = (typeof j.percent === "number") ? Math.max(0, Math.min(100, j.percent)) : null;
  const meta = [];
  if (j.detail) meta.push(j.detail);
  if (j.elapsed) meta.push("已用 " + j.elapsed + "s");
  if (j.packages) meta.push(j.packages + " 个包");
  const bar = j.busy
    ? `<div class="anew-i-bar${pct === null ? " indet" : ""}"><i style="width:${pct === null ? 40 : pct}%"></i></div>`
    : "";
  const metaLine = (j.busy && meta.length)
    ? `<div class="anew-i-meta">${esc(meta.join(" · "))}</div>` : "";
  box.innerHTML = `<div class="anew-i-lh${j.ok ? " ok" : (j.busy ? "" : " bad")}">${head}</div>`
    + bar + metaLine
    + `<pre class="anew-i-pre">${esc(lines.join("\n") || "…")}</pre>`;
  box.hidden = false;
}

/** 轮询安装进度（装完自动刷新目录 + agentlist）。 */
function AINST_poll() {
  AINST.stop();
  AINST.timer = setTimeout(async () => {
    try {
      const j = unwrap(await api("/agent/install/status")) || {};
      AINST.job = j;
      renderInstallLog();
      if (j.busy) { AINST_poll(); return; }
      // 装完 / 失败 → 刷新两处
      await loadInstallCatalog();
      if (j.ok) {
        await loadAcpAgents();
        setBanner(j.kind === "update"
          ? `已更新：${j.agent || j.id}${j.version ? "（v" + j.version + "）" : ""}`
          : `已安装并注册：${j.agent || j.id}（可以在「选择 Agent」里切过去）`);
      } else if (j.error) {
        setBanner((j.kind === "update" ? "更新失败：" : "安装失败：") + j.error, { sticky: true });
      }
    } catch (e) {
      AINST.stop();
    }
  }, 500);
}
AINST.poll = AINST_poll;

async function agentInstall(id) {
  try {
    await api("/agent/install", { method: "POST", body: JSON.stringify({ id: id }) });
    AINST.job = { busy: true, id: id, phase: "准备", log: [] };
    renderInstallCatalog();
    renderInstallLog();
    AINST_poll();
  } catch (e) {
    setBanner("安装失败：" + (e && e.message ? e.message : e), { sticky: true });
  }
}

async function agentUninstall(id) {
  if (!confirm("卸载这个 agent？会从 agentlist 移除，并删掉它装出来的文件。")) return;
  try {
    await api("/agent/uninstall", { method: "POST", body: JSON.stringify({ id: id }) });
    setBanner("已卸载：" + id);
    await loadInstallCatalog();
    await loadAcpAgents();
  } catch (e) {
    setBanner("卸载失败：" + (e && e.message ? e.message : e), { sticky: true });
  }
}

/** ★把已装的 npm agent 更新到最新（后端异步：立刻回，进度轮询 /agent/install/status）。 */
async function agentUpdate(id) {
  if (!confirm("更新这个 agent 到最新版？\n\n会重新下载 npm 包；它正在跑的会话可能被中断"
    + "（更新前面板会先收掉占用文件的子进程）。")) return;
  try {
    await api("/agent/update", { method: "POST", body: JSON.stringify({ id: id }) });
    AINST.job = { busy: true, id: id, phase: "准备更新", kind: "update", log: [] };
    renderInstallCatalog();
    renderInstallLog();
    AINST_poll();
  } catch (e) {
    setBanner("更新失败：" + (e && e.message ? e.message : e), { sticky: true });
  }
}

/** 「扫描本机」：POST {action:"scan"} → 本机发现报告（含已登记 / 可用性 / 客户端缺适配器提示） */
async function loadCandidates() {
  const cur = $("anew-cur");
  if (cur) cur.textContent = "正在扫描本机…";
  ANEW.busy = true;
  try {
    const r = await api("/engine/acp/agents", { method: "POST",
      body: JSON.stringify({ action: "scan" }) });
    ANEW.list = (r && r.found) || [];
    ANEW.summary = (r && r.summary) || null;
    ANEW.hints = (r && r.hints) || [];
  } catch { ANEW.list = []; ANEW.summary = null; ANEW.hints = []; }
  ANEW.busy = false;
  renderCandidates();
}

function renderCandidates() {
  const box = $("anew-list"), cur = $("anew-cur");
  if (!box) return;
  const s = ANEW.summary;
  if (cur) {
    const base = s
      ? `本机发现 ${s.total} 个：可用 ${s.usable}（新的 ${s.newUsable}）· 已登记 ${s.registered} · 仅客户端 ${s.clients}`
      : (ANEW.busy ? "正在扫描本机…" : "没有发现");
    cur.textContent = base + (ANEW.hints.length ? "　⚠ " + ANEW.hints.join("；") : "");
  }
  if (!ANEW.list.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + `本机没扫到 ACP agent —— 用下面的「选择文件夹…」指向它的目录`
      + `（自动识别 node 适配器 / *acp*.exe）。</div>`;
    return;
  }
  const rows = ANEW.list.map((c, i) => {
    const addable = !c.registered && c.kind !== "client";
    const status = c.registered
      ? `<span class="muted" style="font-size:11px;margin-left:auto">已登记`
        + `${c.registeredAs && c.registeredAs !== c.id ? "：" + esc(c.registeredAs) : ""}</span>`
      : (c.available
        ? `<span class="muted" style="font-size:11px;margin-left:auto">点一下加入</span>`
        : `<span class="muted" style="font-size:11px;margin-left:auto">缺：`
          + `${esc((c.missing || []).join("、") || "依赖")}</span>`);
    return `<div class="agents-row" data-i="${i}"${addable ? "" : ' data-noop="1"'}`
      + ` title="${esc((c.command || []).join(" ") + (c.dir ? "\n" + c.dir : ""))}">`
      + `<div class="an"><span class="dot${c.available && c.kind !== "client" ? " ok" : ""}"></span>`
      + `${esc(c.label)} <span class="muted" style="font-size:11px">`
      + `${esc(ANEW_KIND[c.kind] || c.kind || "")}${addable ? "" : " · 不可加"}</span>`
      + (c.provider ? `<span class="badge">${esc(c.provider)}</span>` : "")
      + status + `</div>`
      + `<div class="ab">${esc(c.note || "")}${c.dir ? (c.note ? " · " : "") + esc(c.dir) : ""}</div>`
      + `</div>`;
  }).join("");
  box.innerHTML = rows;
}

async function addCandidate(i) {
  const c = ANEW.list[Number(i)];
  if (!c || c.registered || c.kind === "client") return;
  try {
    const r = await api("/engine/acp/agents", { method: "POST",
      body: JSON.stringify({ action: "add", label: c.label, command: c.command,
                             cwd: c.cwd || "", note: c.note || "", activate: false }) });
    c.registered = true;                     // 就地标记，不整页重扫
    ACP.list = (r && r.agents) || ACP.list;
    ACP.active = (r && r.active) || ACP.active;
    const added = ACP.list.find((x) => x.id === c.id) || {};
    setBanner(`已添加 agent：${c.label}${added.provider ? "（provider: " + added.provider + "）" : ""}`
      + `（回上一页点卡片即可切换）`);
    renderCandidates();
    renderAgents();
    updateAcpRow();
  } catch (err) { setBanner("添加失败：" + (err && err.message ? err.message : err)); }
}

/** 「可用的一键加入」：把所有"没登记 + 可用 + 不是纯客户端"的扫到的都加进来 */
async function addAllCandidates() {
  const todo = ANEW.list.filter((c) => !c.registered && c.kind !== "client" && c.available);
  if (!todo.length) { setBanner("没有可一键加入的（要么已登记、要么缺依赖）"); return; }
  const btn = $("anew-addall");
  if (btn) { btn.disabled = true; btn.textContent = "加入中…"; }
  let ok = 0;
  for (const c of todo) {
    try {
      const r = await api("/engine/acp/agents", { method: "POST",
        body: JSON.stringify({ action: "add", label: c.label, command: c.command,
                               cwd: c.cwd || "", note: c.note || "", activate: false }) });
      c.registered = true; ok++;
      ACP.list = (r && r.agents) || ACP.list;
      ACP.active = (r && r.active) || ACP.active;
    } catch { /* 跳过这一个 */ }
  }
  if (btn) { btn.disabled = false; btn.textContent = "可用的一键加入"; }
  setBanner(`已加入 ${ok} 个 agent（回上一页点卡片切换使用）`);
  renderCandidates();
  renderAgents();
  updateAcpRow();
}

/** 没发现 → 打开 Windows 文件夹选择框，指到 agent 目录，由后端推断启动命令。 */
async function addFromFolder() {
  const btn = $("anew-folder");
  if (btn) { btn.disabled = true; btn.textContent = "选择中…"; }
  try {
    const p = await api("/live/root/pick", { method: "POST", body: "{}" });
    if (!p || !p.ok) {
      setBanner(p && p.canceled ? "已取消选择" : "选择文件夹失败");
      return;
    }
    const r = await api("/engine/acp/agents", { method: "POST",
      body: JSON.stringify({ action: "add-folder", dir: p.path, activate: false }) });
    ACP.list = (r && r.agents) || ACP.list;
    ACP.active = (r && r.active) || ACP.active;
    setBanner(`已从文件夹添加：${r.added}${r.provider ? "（provider: " + r.provider + "）" : ""}`
      + `${r.note ? "（" + r.note + "）" : ""}`);
    closeAgentsNew();
    renderAgents();
    updateAcpRow();
  } catch (err) {
    setBanner("添加失败：" + (err && err.message ? err.message : err));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "选择文件夹…"; }
  }
}

async function delAgent(id) {
  if (!id) return;
  if (!confirm(`删除 agent「${id}」？`)) return;
  try {
    const r = await api("/engine/acp/agents", { method: "POST",
      body: JSON.stringify({ action: "delete", id }) });
    ACP.list = (r && r.agents) || ACP.list;
    if (ACP.active === id) ACP.active = (r && r.active) || ACP.active;
    setBanner(`已删除 agent：${id}`);
    updateAcpRow();
    renderAgents();
  } catch (err) { setBanner("删除失败：" + err.message); }
}

async function detectAgents() {
  const btn = $("agents-detect");
  if (btn) { btn.disabled = true; btn.textContent = "检测中…"; }
  try {
    const r = await api("/engine/acp/agents", { method: "POST", body: JSON.stringify({ action: "detect" }) });
    ACP.active = (r && r.active) || ACP.active;
    ACP.list = (r && r.agents) || ACP.list;
    const found = ACP.list.filter((x) => x.available).length;
    setBanner(`已自动检测 agent 位置（可用 ${found}/${ACP.list.length}）`);
    updateAcpRow();
    renderAgents();
  } catch (err) {
    setBanner("自动检测失败：" + err.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "自动检测"; }
  }
}

let AGENTS_BACK_TO_CFG = false;
function openAgents() {
  AGENTS_BACK_TO_CFG = cfgIsOpen();          // 从设置里进来的 → 把设置收起，专心选 agent
  cfgClose();
  $("agents").hidden = false;
  renderAgents();
  loadAcpAgents();
  OVERLAY.push("agents", closeAgents);       // ★P3：Esc 只关栈顶
}
function closeAgents() {
  $("agents").hidden = true;
  OVERLAY.pop("agents");
  if (AGENTS_BACK_TO_CFG) cfgOpen();         // 关掉 agentlist 回到设置
  AGENTS_BACK_TO_CFG = false;
}

/** 换 ACP agent 后：旧会话属于旧 agent，必须清空界面 + 重连 SSE
 *（否则拿旧 sessionId 去 prompt，对方回 -32603 Session not found）。 */
async function resetAfterAgentSwitch(msg) {
  setBanner(msg);
  // ★ 换 ACP agent = 换模型清单（每个 agent 能用的模型不同）→ 旧清单立刻作废。
  //   ⚠ 重拉**不 await**：ACP 拿清单要 resume 最近会话（冷启动时 5s+），等它会把"切 agent"卡死。
  //   清单为空期间 normalizeModelRef 返回 null（不猜）、writeSessionModel 不写非法引用 → 安全。
  resetModels(ENGINE.active);
  loadModels().catch(() => {});
  state.current = null;
  state.messages = [];
  state.live = {};
  state.reveal = {};
  state.pending = [];
  state.attachments = [];
  staticSig = null;
  liveShellSig = null;
  heroShown = false;
  resetMsgPaging();
  renderAttachRow();
  $("session-title").textContent = "未选择会话";
  $("session-meta").textContent = "";
  $("input").disabled = true;
  $("btn-send").disabled = true;
  $("btn-stop").disabled = true;
  renderMessages();
  if (typeof connectEvents === "function") connectEvents();
  await loadSessions();
  if (state.sessions.length && !state.current) await openSession(state.sessions[0].id);
}

async function pickAgent(id) {
  const prev = ACP.active;
  try {
    const r = await api("/engine/acp/agents", { method: "POST", body: JSON.stringify({ id }) });
    ACP.active = (r && r.active) || id;
    ACP.list = (r && r.agents) || ACP.list;
    updateAcpRow();
    renderAgents();
    acpPrewarm();                          // ★换了大脑 → 提前把新 agent 的子进程热起来
    if (prev !== ACP.active && ENGINE.active === "acp") {
      closeAgents();
      await resetAfterAgentSwitch(`已切换 ACP 代理：${ACP.active}`);
    } else {
      setBanner(`已选择 ACP 代理：${id}（切到 acp 引擎后生效）`);
    }
  } catch (err) {
    setBanner("切换 ACP 代理失败：" + err.message);
  }
}

/* ---------------- 编辑 agent（名称 / 命令 / 目录 / provider / 说明）---------------- */

const AE = { id: "" };

/** 打开编辑弹窗；`id` 不给就编辑「当前使用中」的那个 */
async function editAgentCommand(id) {
  const aid = id || ACP.active;
  if (!aid) { setBanner("还没有选中的 agent"); return; }
  const a = ACP.list.find((x) => x.id === aid);
  if (!a) { setBanner("找不到 agent：" + aid); return; }
  AE.id = aid;
  $("ae-title").textContent = `编辑 Agent · ${aid}`;
  $("ae-label").value = a.label || "";
  $("ae-command").value = (a.command || []).join(" ");
  $("ae-cwd").value = a.cwd || "";
  $("ae-provider").value = a.provider || "";
  $("ae-note").value = a.note || "";
  // ★凭据：这个 agent 用哪个 Key（只列当前 agent 那个 env 的；留空 = 跟随默认）
  try {
    if (!(CRED.items || []).length) await loadCredentials();
    const env = keyEnvForAgent(a);
    const same = (CRED.items || []).filter((x) => x.env === env);
    const cur = (a.credentials || {})[env] || "";
    const sel = $("ae-cred");
    if (sel) {
      sel.dataset.env = env;
      sel.innerHTML = `<option value="">跟随默认（${esc(env)}）</option>`
        + same.map((x) => `<option value="${esc(x.id)}"${x.id === cur ? " selected" : ""}>`
          + `${esc(x.label)}${x.isDefault ? "（默认）" : ""} · ${esc(x.masked)}</option>`).join("");
      if (!same.length) {
        sel.innerHTML += `<option value="" disabled>（还没有 ${esc(env)} 的凭据）</option>`;
      }
    }
  } catch { /* 凭据拉不到也不挡编辑 */ }
  $("ae-msg").textContent = a.provider || !(a.providerGuess)
    ? "改命令 / provider / 凭据 会重启这个 agent 的子进程"
    : `推断出的 provider 是「${a.providerGuess}」（点「自动」填入）`;
  $("agent-edit").hidden = false;
  OVERLAY.push("agent-edit", closeAgentEdit);
}

function closeAgentEdit() { $("agent-edit").hidden = true; AE.id = ""; OVERLAY.pop("agent-edit"); }

/** 「自动」：让后端按命令 / env / CODEX_HOME 猜 provider（只填输入框，不保存） */
async function guessAgentProvider() {
  const btn = $("ae-guess");
  if (btn) btn.disabled = true;
  try {
    const r = await api("/engine/acp/agents", { method: "POST",
      body: JSON.stringify({ action: "guess-provider", id: AE.id,
                             command: parseCmd($("ae-command").value) }) });
    const p = (r && r.provider) || "";
    if (p) {
      $("ae-provider").value = p;
      $("ae-msg").textContent = `已推断 provider：${p}（保存后生效）`;
    } else {
      $("ae-msg").textContent = "推断不出 provider；留空也行（模型用字母徽章，分组显示 acp）";
    }
  } catch (err) {
    $("ae-msg").textContent = "推断失败：" + (err && err.message ? err.message : err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 命令字符串 → 数组（空格分隔；含空格的路径用英文双引号） */
function parseCmd(v) {
  return (String(v || "").match(/"[^"]*"|\S+/g) || []).map((s) => s.replace(/^"|"$/g, ""));
}

async function saveAgentEdit() {
  if (!AE.id) return;
  const cmd = parseCmd($("ae-command").value);
  if (!cmd.length) { $("ae-msg").textContent = "命令不能为空"; return; }
  const btn = $("ae-save");
  if (btn) btn.disabled = true;
  try {
    const body = { id: AE.id, label: $("ae-label").value.trim(), command: cmd,
                   cwd: $("ae-cwd").value.trim(), provider: $("ae-provider").value.trim(),
                   note: $("ae-note").value.trim(), activate: false };
    // ★凭据选择：只改这个 agent 对应 env 的那一项，别的 env 覆盖保留
    const sel = $("ae-cred");
    if (sel && sel.dataset && sel.dataset.env) {
      const a = ACP.list.find((x) => x.id === AE.id) || {};
      const creds = Object.assign({}, (a.credentials || {}));
      if (sel.value) creds[sel.dataset.env] = sel.value;
      else delete creds[sel.dataset.env];
      body.credentials = creds;
    }
    const r = await api("/engine/acp/agents", { method: "POST", body: JSON.stringify(body) });
    ACP.list = (r && r.agents) || ACP.list;
    ACP.active = (r && r.active) || ACP.active;
    setBanner(`已保存 agent：${AE.id}${body.provider ? " · provider " + body.provider : ""}`);
    closeAgentEdit();
    updateAcpRow();
    renderAgents();
  } catch (err) {
    $("ae-msg").textContent = "保存失败：" + (err && err.message ? err.message : err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderSettings() {
  const put = (id, val, text) => {
    const el = $(id);
    if (el) el.value = String(val);
    const out = $(id + "-out");
    if (out) out.textContent = text;
  };
  const bn = $("cfg-brand");
  if (bn) bn.value = $("brand-name").textContent || BRAND_DEFAULT;
  const bs = $("cfg-brand-sub");
  if (bs) bs.value = ($("brand-sub") ? $("brand-sub").textContent : "") || BRAND_SUB_DEFAULT;
  [["cfg-hero-l1", SET.heroL1], ["cfg-hero-l2", SET.heroL2],
   ["cfg-hero-credit", SET.heroCredit]].forEach(([id, v]) => {
    const el = $(id);
    if (el) el.value = String(v || "");
  });
  // 自定义素材那一排：显示当前用的是哪个文件（只显示文件名，路径放 tooltip）
  document.querySelectorAll("[data-img-name]").forEach((el) => {
    const p = String((APPE.images || {})[el.dataset.imgName] || "");
    el.textContent = p ? (p.split(/[\\/]/).pop() || "已设置") : "默认";
    el.title = p || "默认（自带素材）";
  });
  put("cfg-dim-hiru", SET.dimHiru, SET.dimHiru + "%");
  put("cfg-blur", SET.blur, SET.blur + "px");
  put("cfg-zoom", SET.zoom, SET.zoom + "%");
  put("cfg-rate", SET.rate, (SET.rate / 100).toFixed(2) + "×");
  put("cfg-spec-h", SET.spectrumSidesH, SET.spectrumSidesH + "%");
  put("cfg-spec-gain", SET.spectrumSidesGain, SET.spectrumSidesGain + "%");
  [["cfg-live", SET.live], ["cfg-taskbar", SET.taskbar], ["cfg-spec-sides", SET.spectrumSides],
   ["wpc-always", SET.liveAlways],
   ["cfg-petals", SET.petals], ["cfg-gold", SET.gold], ["cfg-bamboo", SET.bamboo],
   ["cfg-zh", SET.zh], ["cfg-glass", SET.glass],
   ["cfg-turnsnap", SET.turnSnap], ["cfg-auto-delete", true]].forEach(([id, on]) => {
    const b = $(id);
    if (!b) return;
    b.classList.toggle("on", !!on);
    b.textContent = on ? "开" : "关";
  });
}

/** 面板窗口形态：GET 显示当前值；改完 POST（下次开窗生效）。原生组件没装就只能用浏览器。 */
async function loadWindowPref() {
  const sel = $("cfg-window");
  if (!sel) return;
  const note = $("cfg-window-note");
  try {
    const r = await api("/panel/window");
    const native = !!(r && r.nativeAvailable);
    sel.value = (r && r.mode) || "auto";
    if (note) {
      note.textContent = native
        ? "开启面板时用哪种窗口；改完关掉面板再开生效"
        : "未安装「软件窗口」组件（pywebview / WebView2）→ 只能用浏览器窗口";
    }
    const on = sel.querySelector('option[value="native"]');
    const oa = sel.querySelector('option[value="auto"]');
    if (on) on.disabled = !native;
    if (oa) oa.disabled = !native;
    if (!native) sel.value = "browser";
  } catch {
    if (note) note.textContent = "拿不到窗口设置（后端需支持 /panel/window）";
  }
}

/** 设置抽屉：跟群聊一样的展开形式（右滑入 + 分屏让位；左边缘可拖宽）。 */
function cfgIsOpen() { return $("cfg").classList.contains("open"); }
function cfgOpen() {
  $("cfg").classList.add("open");
  document.documentElement.classList.add("cfg-open");
  syncDrawerOverlap();
  OVERLAY.push("cfg", cfgClose);             // ★P3：Esc 只关栈顶
}
function cfgClose() {
  $("cfg").classList.remove("open");
  document.documentElement.classList.remove("cfg-open");
  syncDrawerOverlap();
  OVERLAY.pop("cfg");
}

/** ★哪些弹层打开时**先把设置抽屉收起来**（关掉再还原）。
 *  它们都是居中 / 铺满的对话框 —— 不收起就会压在右侧设置抽屉上，两层字叠字，
 *  用户实测直接踩到两个：「＋ 添加凭据」和「权限与批准（信任面板）」。
 *  ⚠ **不收** 的那几个是有意的：`trace` 是左侧独立页（不和右侧抽屉打架）、
 *  `copy-fb`（剪贴板兜底）与 `editq`（把提问放回输入框）不是对话框、`newsess` 见下（其实也收）。
 *  以后新增居中弹层：把 id 加进这个集合就行（`OVERLAY.push` 会自动调用）。 */
const CFG_YIELD_OVERLAYS = new Set([
  "cred", "trust", "audit", "mode", "skill", "import", "quar", "setup",
  "agents", "agents-new", "agent-edit", "themes", "wpc", "wp", "mdl", "cu", "newsess", "restore", "sessmgr",
]);
let CFG_YIELD = 0;                        // 计数：A→B 连着开两层时，只有最后一层关掉才还原

/** 弹层要打开了 → 若设置开着就先收起来。返回**是否真的收过**（`OVERLAY.push` 记这笔账）。 */
function cfgYieldToOverlay(id) {
  if (!CFG_YIELD_OVERLAYS.has(String(id))) return false;
  if (!cfgIsOpen()) return false;
  cfgClose();
  CFG_YIELD += 1;
  return true;
}

/** 弹层关掉了 → 把刚才收起的设置还原（只还原我们自己收的那次）。 */
function cfgRestoreFromOverlay() {
  if (CFG_YIELD <= 0) return;
  CFG_YIELD -= 1;
  if (CFG_YIELD > 0) return;
  if (cfgIsOpen()) return;                // 用户自己又打开了 → 别重复开
  cfgOpen();
}

/** ★壁纸与外观独立页（`#wpc`）：那些行**静态就写在 `#wpc-body` 里**（背景 / 外观 / 身份与素材 /
 *  空会话首屏 / 主题）。不再运行时 appendChild 搬 —— 早先那套（`WPC_ROWS` + `wpcBuild()`）必须在
 *  "正确时机"搬一次，否则每次页面重载后设置抽屉里又会冒出整套壁纸设置（`0.2.52` 的 bug）。
 *  ⚠ 以后加新的外观项：**直接写进 `#wpc-body`**（分节用 `.wpc-sec`），别再回到搬节点那条路。 */

function wpcOpen() {
  $("wpc").hidden = false;
  renderSettings();
  OVERLAY.push("wpc", wpcClose);
}

function wpcClose() {
  $("wpc").hidden = true;
  OVERLAY.pop("wpc");
}

/** ★设置项里的「说明」收进 tooltip：面板上只留标题，**鼠标悬停那一行**才显示（别把面板塞满碎碎念）。
 *  动态状态（当前引擎 / 更新进度 / 面板窗口说明…）在 index.html 里带 `data-live`，保持原样可见。
 *  对所有 `.cfg-row`（设置抽屉 / 新建会话 / agent 编辑 / 首次向导）都生效。 */
function initCfgNotes() {
  document.querySelectorAll(".cfg-row > label > i:not([data-live])").forEach((el) => {
    const row = el.closest(".cfg-row");
    const txt = String(el.textContent || "").trim();
    if (row && txt) row.title = txt;        // 放到整行上：悬停该行任何位置都能看到
    el.remove();                            // 从版面上彻底拿掉，不再占位
  });
}

function initSettings() {
  initCfgNotes();                           // ★说明收进悬停 tooltip
  const close = () => cfgClose();
  const open = () => {
    cfgOpen();
    renderSettings();
    musicStatus();
    musicComponent();                       // 音乐服务是可选项：没装就明说
    loadApiKeyRow("cfg");                   // 模型 API Key（只显示掩码）
    loadWallpaperRoot();                    // 目录可能被外部改过，每次打开都刷新
    loadEngineStatus();                     // 可用引擎 / 当前引擎（老后端会优雅降级）
    loadWindowPref();                       // 面板窗口形态（原生 / 浏览器）
  };
  $("btn-cfg").addEventListener("click", () => (cfgIsOpen() ? close() : open()));
  $("cfg-close").addEventListener("click", close);
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && cfgIsOpen()) close(); });

  // 宽度记忆 + 左边缘拖拽（跟群聊一致的展开形式）
  try {
    const w = localStorage.getItem("cocraft.cfgWidth");
    if (w) document.documentElement.style.setProperty("--cfg-w", w);
  } catch { /* 隐私模式忽略 */ }
  (function initCfgResize() {
    const grip = $("cfg-resize"), box = $("cfg");
    if (!grip || !box) return;
    let dragging = false;
    grip.addEventListener("pointerdown", (e) => {
      dragging = true; box.classList.add("resizing");
      document.documentElement.classList.add("cfg-resizing");
      try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    });
    grip.addEventListener("pointermove", (e) => {
      if (!dragging) return;
      const w = Math.max(320, Math.min((window.innerWidth - 300) / uiZoom(),
                                       (window.innerWidth - e.clientX) / uiZoom()));
      document.documentElement.style.setProperty("--cfg-w", w + "px");
    });
    const stop = () => {
      if (!dragging) return;
      dragging = false; box.classList.remove("resizing");
      document.documentElement.classList.remove("cfg-resizing");
      try { localStorage.setItem("cocraft.cfgWidth", document.documentElement.style.getPropertyValue("--cfg-w")); } catch { /* ignore */ }
    };
    grip.addEventListener("pointerup", stop);
    grip.addEventListener("pointercancel", stop);
  })();

  $("cfg-brand").addEventListener("input", (e) => applyBrandName(e.target.value));
  $("cfg-brand-sub").addEventListener("input", (e) => applyBrandSub(e.target.value));
  $("cfg-hero-l1").addEventListener("input", (e) => setSetting("heroL1", e.target.value));
  $("cfg-hero-l2").addEventListener("input", (e) => setSetting("heroL2", e.target.value));
  $("cfg-hero-credit").addEventListener("input", (e) => setSetting("heroCredit", e.target.value));
  // 自定义素材那一排：选择… / 默认（事件委托，按钮是静态的但用 data-img 更省代码）
  $("cfg").addEventListener("click", (e) => {
    const pick = e.target.closest(".cfg-img-pick");
    if (pick) { pickAppearanceImage(pick.dataset.img); return; }
    const clr = e.target.closest(".cfg-img-clear");
    if (clr) { setAppearanceImage(clr.dataset.img, ""); }
  });
  $("cfg-engine").addEventListener("change", (e) => setEngine(e.target.value));
  $("cfg-acp-btn").addEventListener("click", openAgents);
  $("cfg-setup").addEventListener("click", () => { close(); openSetup(); });
  $("setup-engine").addEventListener("change", setupEngineChanged);
  $("setup-done").addEventListener("click", setupDone);
  $("setup-skip").addEventListener("click", setupSkip);
  $("setup-close").addEventListener("click", setupSkip);
  $("setup").addEventListener("click", (e) => { if (e.target === $("setup")) setupSkip(); });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("setup").hidden) setupSkip(); });
  $("agents-close").addEventListener("click", closeAgents);
  $("agents").addEventListener("click", (e) => { if (e.target === $("agents")) closeAgents(); });
  $("agents-list").addEventListener("click", (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); delAgent(del.dataset.del); return; }
    const ed = e.target.closest("[data-edit]");
    if (ed) { e.stopPropagation(); editAgentCommand(ed.dataset.edit); return; }
    const row = e.target.closest(".agents-row[data-aid]");
    if (row) pickAgent(row.dataset.aid);
  });
  $("agents-add").addEventListener("click", openAgentsNew);
  $("anew-close").addEventListener("click", closeAgentsNew);
  $("agents-new").addEventListener("click", (e) => { if (e.target === $("agents-new")) closeAgentsNew(); });
  $("anew-list").addEventListener("click", (e) => {
    const row = e.target.closest(".agents-row[data-i]");
    if (row && !row.dataset.noop) addCandidate(row.dataset.i);
  });
  $("anew-reload").addEventListener("click", loadCandidates);
  $("anew-i-check").addEventListener("click", agentCheckUpdates);  $("anew-folder").addEventListener("click", addFromFolder);
  $("anew-node-check").addEventListener("click", nodeCheck);
  $("anew-node-up").addEventListener("click", nodeApply);
  $("anew-node-back").addEventListener("click", nodeRollback);
  $("anew-auto-check").addEventListener("click", () => toggleAgentAuto("check"));
  $("anew-auto-install").addEventListener("click", () => toggleAgentAuto("autoInstall"));
  $("anew-addall").addEventListener("click", addAllCandidates);
  // ★面板内安装 agent：点「安装」/「更新」/「卸载」
  $("anew-i-list").addEventListener("click", (e) => {
    const go = e.target.closest && e.target.closest("[data-ainst-go]");
    if (go && !go.disabled) { agentInstall(go.dataset.ainstGo); return; }
    const up = e.target.closest && e.target.closest("[data-ainst-up]");
    if (up && !up.disabled) { agentUpdate(up.dataset.ainstUp); return; }
    const bk = e.target.closest && e.target.closest("[data-ainst-back]");
    if (bk && !bk.disabled) { agentRollback(bk.dataset.ainstBack); return; }
    const off = e.target.closest && e.target.closest("[data-ainst-off]");
    if (off) agentUninstall(off.dataset.ainstOff);
  });
  $("agents-reload").addEventListener("click", loadAcpAgents);
  $("agents-detect").addEventListener("click", detectAgents);
  $("agents-edit").addEventListener("click", editAgentCommand);
  $("btn-agent").addEventListener("click", openModes);
  $("mode-close").addEventListener("click", closeModes);
  $("mode").addEventListener("click", (e) => { if (e.target === $("mode")) closeModes(); });
  $("mode-list").addEventListener("click", (e) => {
    const row = e.target.closest("[data-mode]");
    if (row) pickMode(row.dataset.mode);
  });
  $("mode-default").addEventListener("click", () => {
    try { localStorage.removeItem(MODE_KEY); } catch { /* 隐私模式 */ }
    setBanner("已清除「新会话默认模式」（跟随引擎默认）");
    renderModeButton();
    renderModes();
  });
  $("btn-import").addEventListener("click", openImport);
  // 新会话：选项目 / 新建项目 / 选工作目录
  $("ns-close").addEventListener("click", closeNewSession);
  $("ns-cancel").addEventListener("click", closeNewSession);
  $("ns-pick").addEventListener("click", pickNewSessionDir);
  $("ns-clear").addEventListener("click", () => {
    $("ns-dir").value = ""; $("ns-note").textContent = "已清空 → 用引擎默认目录";
    renderNewSessionProjects();
  });
  $("ns-newbase-pick").addEventListener("click", pickNewProjectBase);
  $("ns-newname-create").addEventListener("click", createProjectDir);
  $("ns-dir").addEventListener("input", renderNewSessionProjects);
  $("ns-create").addEventListener("click", doCreateSession);
  $("newsess").addEventListener("click", (e) => { if (e.target === $("newsess")) closeNewSession(); });
  ["ns-dir", "ns-title"].forEach((id) => $(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doCreateSession(); }
    if (e.key === "Escape") { e.preventDefault(); closeNewSession(); }
  }));
  ["ns-newbase", "ns-newname"].forEach((id) => $(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); createProjectDir(); }
    if (e.key === "Escape") { e.preventDefault(); closeNewSession(); }
  }));
  $("imp-close").addEventListener("click", closeImport);
  $("imp-reload").addEventListener("click", loadImport);
  $("import").addEventListener("click", (e) => { if (e.target === $("import")) closeImport(); });
  $("imp-list").addEventListener("click", (e) => {
    const del = e.target.closest("[data-imp-del]");
    if (del) { e.stopPropagation(); impDeleteRemote(del.dataset.impDel); return; }
    const row = e.target.closest("[data-imp]");
    if (row) doImport(row.dataset.imp);
  });
  $("quar-close").addEventListener("click", quarClose);
  $("quar-reload").addEventListener("click", quarLoad);
  $("quar-snap-toggle").addEventListener("click", quarToggleSnap);
  // ★低风险删除：策略开关 / 保留天数 / 立即清理（`ui 0.2.95`）
  $("quar-auto").addEventListener("click", quarToggleAuto);
  $("quar-days").addEventListener("change", quarSaveDays);
  $("quar-cleanup").addEventListener("click", quarCleanupNow);
  $("cfg-auto-delete").addEventListener("click", quarToggleAuto);
  $("cfg-quar-open").addEventListener("click", () => quarOpen());
  $("quar").addEventListener("click", (e) => { if (e.target === $("quar")) quarClose(); });
  $("quar-open").addEventListener("click", async () => {
    const dir = (QUAR.stats && QUAR.stats.quarantine_dir) || "";
    if (!dir) { setBanner("忏悔室目录还没生成（还没隔离过任何东西）"); return; }
    try {
      await api("/open/dir", { method: "POST", body: JSON.stringify({ what: "explorer", dir }) });
    } catch (e) {
      setBanner("打开失败：" + (e && e.message ? e.message : e));
    }
  });
  $("quar-list").addEventListener("click", (e) => {
    const r = e.target.closest("[data-qrestore]");
    if (r) { quarRestore(r.dataset.qrestore); return; }
    const d = e.target.closest("[data-qdrop]");
    if (d) quarDrop(d.dataset.qdrop);
  });
  // ★模型凭据（多 Key）：添加 / 编辑 / 删除 / 设默认
  $("cred-add").addEventListener("click", () => openCredEdit(""));
  $("cred-close").addEventListener("click", closeCredEdit);
  $("cred-cancel").addEventListener("click", closeCredEdit);
  $("cred-save").addEventListener("click", saveCred);
  $("cred").addEventListener("click", (e) => { if (e.target === $("cred")) closeCredEdit(); });
  $("cred-list").addEventListener("click", (e) => {
    const pv = e.target.closest && e.target.closest("[data-cred-prov]");
    if (pv) {
      const k = pv.dataset.credProv;
      CRED.open[k] = !CRED.open[k];
      renderCredList();
      return;
    }
    const d = e.target.closest && e.target.closest("[data-cred-def]");
    if (d) { setCredDefault(d.dataset.credDef); return; }
    const ed = e.target.closest && e.target.closest("[data-cred-edit]");
    if (ed) { openCredEdit(ed.dataset.credEdit); return; }
    const del = e.target.closest && e.target.closest("[data-cred-del]");
    if (del) deleteCred(del.dataset.credDel);
  });
  $("ae-close").addEventListener("click", closeAgentEdit);
  $("ae-cancel").addEventListener("click", closeAgentEdit);
  $("ae-save").addEventListener("click", saveAgentEdit);
  $("ae-guess").addEventListener("click", guessAgentProvider);
  $("agent-edit").addEventListener("click", (e) => { if (e.target === $("agent-edit")) closeAgentEdit(); });
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("quar").hidden) { quarClose(); return; }
    if (!$("mode").hidden) { closeModes(); return; }
    if (!$("import").hidden) { closeImport(); return; }
    if (!$("agent-edit").hidden) { closeAgentEdit(); return; }
    if (!$("agents-new").hidden) { closeAgentsNew(); return; }
    if (!$("agents").hidden) closeAgents();
  });
  $("cfg-dim-hiru").addEventListener("input", (e) => setSetting("dimHiru", Number(e.target.value)));
  $("cfg-blur").addEventListener("input", (e) => setSetting("blur", Number(e.target.value)));
  $("cfg-zoom").addEventListener("input", (e) => setSetting("zoom", Number(e.target.value)));
  $("cfg-rate").addEventListener("input", (e) => setSetting("rate", Number(e.target.value)));
  $("cfg-live").addEventListener("click", () => setSetting("live", !SET.live));
  $("cfg-taskbar").addEventListener("click", () => setSetting("taskbar", !SET.taskbar));
  $("cfg-window").addEventListener("change", async (e) => {
    try {
      const r = await api("/panel/window", { method: "POST",
        body: JSON.stringify({ mode: e.target.value }) });
      const name = { auto: "自动", native: "软件窗口（WebView2）", browser: "浏览器窗口（Edge）" };
      setBanner("窗口已设为：" + (name[(r && r.mode) || e.target.value] || e.target.value)
        + "（关掉面板窗口再打开才生效）");
      loadWindowPref();
    } catch (err) {
      setBanner("设置失败：" + (err && err.message ? err.message : err));
    }
  });
  $("cfg-spec-sides").addEventListener("click", () => setSetting("spectrumSides", !SET.spectrumSides));
  $("cfg-spec-h").addEventListener("input", (e) => setSetting("spectrumSidesH", Number(e.target.value)));
  $("cfg-spec-gain").addEventListener("input", (e) => setSetting("spectrumSidesGain", Number(e.target.value)));
  $("cfg-petals").addEventListener("click", () => setSetting("petals", !SET.petals));
  $("cfg-gold").addEventListener("click", () => setSetting("gold", !SET.gold));
  $("cfg-bamboo").addEventListener("click", () => setSetting("bamboo", !SET.bamboo));
  $("cfg-glass").addEventListener("click", () => setSetting("glass", !SET.glass));
  $("cfg-turnsnap").addEventListener("click", () => {
    setSetting("turnSnap", !SET.turnSnap);
    setBanner(SET.turnSnap
      ? "已打开「每轮工作区快照」：下次发消息前会先给工作目录留一次底（供忏悔室显示版本）"
      : "已关闭「每轮工作区快照」：只保留破坏性操作的忏悔室");
  });
  $("cfg-zh").addEventListener("click", () => {
    setSetting("zh", !SET.zh);
    MUSIC.lyrKey = "";                       // 开关变了要重新取一次歌词（tr=0/1 不同）
    ensureLyrics();
  });
  $("cfg-wall-root-pick").addEventListener("click", pickWallpaperRoot);
  $("cfg-wall-root-auto").addEventListener("click", () => saveWallpaperRoot(""));
  $("cfg-wall").addEventListener("click", () => { wpcClose(); openWallpapers(); });
  $("cfg-wallpaper").addEventListener("click", () => { close(); wpcOpen(); });
  $("wpc-close").addEventListener("click", () => wpcClose());
  $("wpc").addEventListener("click", (e) => { if (e.target === $("wpc")) wpcClose(); });
  $("wpc-always").addEventListener("click", () => setSetting("liveAlways", !SET.liveAlways));
  $("cfg-ck-netease").addEventListener("click", () => pasteCookie("netease"));
  $("cfg-ck-qq").addEventListener("click", () => pasteCookie("qq"));
  $("cfg-reset").addEventListener("click", () => {
    Object.assign(SET, SET_DEFAULT);
    saveSettings();
    applyBrandName("");                    // 名字 / 小标签也回到默认
    applyBrandSub("");
    saveWallpaperRoot("");                 // 动态壁纸目录恢复自动查找
    applySettings();
    renderSettings();
  });

  // 粒子效果清单：可收起；记住上次是开还是收（默认展开）
  const plist = $("cfg-particle-list");
  if (plist) {
    try {
      if (localStorage.getItem("cocraft.particles.open") === "0") plist.open = false;
    } catch { /* 隐私模式 */ }
    plist.addEventListener("toggle", () => {
      try { localStorage.setItem("cocraft.particles.open", plist.open ? "1" : "0"); } catch { /* ignore */ }
    });
  }
}

async function initLiveBg() {
  const root = document.documentElement;
  const box = $("live-bg");
  const A = $("live-video"), B = $("live-video2");
  if (!box || !A || !B) return;
  const vids = [A, B];
  const customBg = $("custom-bg");
  const hideCustomBg = () => {
    root.classList.remove("has-custom-bg");
    if (customBg) customBg.style.backgroundImage = "";
  };
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let urls = null;
  try { urls = await api("/live/wallpapers"); } catch { urls = null; }

  let seq = 0;                 // 切换序号：快速反复切时让上一轮流程自己作废
  let active = A;              // 当前显示的那一层（正在播 / 暂停着当背景）
  let curTheme = null;
  let rampTimer = null;
  let fadeTimer = null;

  const themeNow = () => "hiru";      // 壁纸槽位（只有一个槽；`hiru` 是历史 id，后端 `/live/pick` 仍认它）
  const urlOf = (t) => ((urls && urls[t]) || "");
  const kindOf = (t) => ((urls && urls[t + "Kind"]) || "video");   // video | scene
  const other = (v) => (v === A ? B : A);

  /** 量当前帧的平均亮度（只在判"是不是片头黑场"时才算：**不截图、不编码**） */
  const DARK = 12;                 // 平均亮度低于它就当黑场（0-255）
  const frameMean = (v) => {
    if (!v.videoWidth || v.readyState < 2) return -1;
    try {
      const c = document.createElement("canvas");
      c.width = 64; c.height = 36;
      const ctx = c.getContext("2d");
      ctx.drawImage(v, 0, 0, c.width, c.height);
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      let sum = 0;
      for (let i = 0; i < d.length; i += 4) sum += (d[i] + d[i + 1] + d[i + 2]) / 3;
      return sum / (d.length / 4);
    } catch { return -1; }
  };

  const seekTo = (v, s) => new Promise((done) => {
    if (!v.seekable || !v.seekable.length || !v.paused) return done();
    if (Math.abs(v.currentTime - s) < 0.2) return done();
    const h = () => { v.removeEventListener("seeked", h); done(); };
    v.addEventListener("seeked", h);
    try { v.currentTime = s; } catch { v.removeEventListener("seeked", h); done(); }
    setTimeout(h, 1500);             // 兜底：seek 没回音也别卡住
  });

  /** 设倍速（带保护：超出浏览器支持范围时别把整个流程炸掉） */
  const setRate = (r) => {
    try { active.playbackRate = r; } catch { /* NotSupportedError：忽略 */ }
  };

  /** 把 playbackRate 从当前值线性推到 to */
  const ramp = (to, ms) => new Promise((done) => {
    clearInterval(rampTimer);
    const from = active.playbackRate;
    const t0 = performance.now();
    rampTimer = setInterval(() => {
      const k = Math.min(1, (performance.now() - t0) / ms);
      setRate(from + (to - from) * k);
      if (k >= 1) { clearInterval(rampTimer); rampTimer = null; done(); }
    }, 40);
  });

  /** 暂停时若停在**片头黑场**（很多动态壁纸开头是淡入黑场），往前挪一点再停。
   *  实在全黑：藏起视频，让主题自带的静态壁纸顶着（任何情况下都不会出现黑底）。 */
  const ensureVisibleFrame = async (t) => {
    const v = active;
    if (v.dataset.src !== urlOf(t)) return;
    const isDark = () => { const m = frameMean(v); return m >= 0 && m < DARK; };
    if (!isDark()) return;
    for (const at of [1.0, 2.5, 4.0]) {
      await seekTo(v, at);
      if (v.dataset.src !== urlOf(t)) return;
      if (!isDark()) return;
    }
    v.classList.remove("ready");
  };

  const play = (t, collapsed, mine) => {
    if (collapsed) {                     // 收起：从静止慢慢加速
      setRate(LIVE_START_RATE);
      active.play().then(() => ramp(liveRate, LIVE_RAMP_IN)).catch(() => {});
    } else {                             // 展开：慢慢减速 → **暂停**（暂停那帧就是静态背景）
      (async () => {
        if (!active.paused) await ramp(LIVE_START_RATE, LIVE_RAMP_OUT);
        if (mine !== seq) return;        // 期间又切了，这一轮作废
        try { active.pause(); } catch { /* 忽略 */ }
        setRate(liveRate);
        ensureVisibleFrame(t);
      })();
    }
  };

  const hideAll = () => {
    root.classList.remove("has-live");
    sceneDrop();
    clearInterval(rampTimer); rampTimer = null;
    clearTimeout(fadeTimer); fadeTimer = null;
    vids.forEach((v) => { try { v.pause(); } catch { /* 忽略 */ } v.classList.remove("ready"); });
    box.style.backgroundImage = "";      // 交回 CSS 里那张主题静态图
  };

  /** 把**另一层**装上新主题的片子；一出第一帧就交叉淡入（旧层在此之前一直可见 → 不露静态底） */
  const swap = (t, url, collapsed, mine) => {
    const next = other(active);
    const prev = active;
    if (next.dataset.src !== url) {
      next.dataset.src = url;
      next.src = url;
      next.classList.remove("ready");
      next.load();
    }
    next.style.zIndex = "2"; prev.style.zIndex = "1";
    const ready = () => {
      if (mine !== seq) return;
      next.classList.add("ready");       // 新层淡入（盖在旧层上）
      active = next; curTheme = t;
      play(t, collapsed, mine);
      clearTimeout(fadeTimer);
      fadeTimer = setTimeout(() => {     // 淡完再撤掉旧层、释放解码
        prev.classList.remove("ready");
        try { prev.pause(); } catch { /* 忽略 */ }
        prev.dataset.src = "";
        prev.removeAttribute("src");
      }, 340);
    };
    if (next.readyState >= 2) ready();
    else {
      next.addEventListener("loadeddata", ready, { once: true });
      setTimeout(() => { if (mine === seq && next.readyState >= 2) ready(); }, 1600);
    }
  };

  /* ---------- 场景（scene）壁纸：WebWallGL 实时渲染 ----------
   * 只在**收起重侧栏**时跑（和视频壁纸同一套节奏）；展开即 pause（保留最后一帧）。
   * 挂载失败 / WebGL2 不可用 / 渲染报错 → 扣掉 has-live，退回主题静态图（只提示一次）。 */
  let sceneLibP = null;            // 懒加载 vendor 脚本（失败可重试）
  let sceneInst = null;            // WebWallGL 实例
  let sceneBase = "";              // 当前挂载的目录 URL
  let sceneBusy = false;           // 正在挂载（防重入）
  let sceneBad = "";               // 已知挂不上的目录（同一张不反复重试）

  const loadSceneLib = () => {
    if (window.WebWallGL) return Promise.resolve(window.WebWallGL);
    if (sceneLibP) return sceneLibP;
    sceneLibP = new Promise((done, fail) => {
      const s = document.createElement("script");
      s.src = SCENE_LIB;
      s.async = true;
      s.onload = () => (window.WebWallGL ? done(window.WebWallGL) : fail(new Error("WebWallGL 未挂到 window")));
      s.onerror = () => fail(new Error("vendor 脚本加载失败"));
      document.head.appendChild(s);
    });
    sceneLibP.catch(() => { sceneLibP = null; });   // 失败不固化，下次还能重试
    return sceneLibP;
  };

  const sceneCanvas = () => {
    let c = box.querySelector(".lb-scene");
    if (!c) {
      c = document.createElement("canvas");
      c.className = "lb-scene";
      box.appendChild(c);
    }
    return c;
  };

  /** 定格层：把 WebGL 最后一帧画进 2D 画布（展开侧栏时当静态背景，和视频"暂停在那一帧"对齐）。
   *  ⚠ WebGL 画布的绘制缓冲默认在合成后被清空 → **必须和库的绘制同一帧里 drawImage**，
   *  所以这里用 rAF（库的循环先注册 → 同帧里我们先画完再 pause）。抓不到就退回静态图。 */
  const freezeCanvas = () => {
    let c = box.querySelector(".lb-freeze");
    if (!c) {
      c = document.createElement("canvas");
      c.className = "lb-scene lb-freeze";
      box.appendChild(c);
    }
    return c;
  };

  const sceneFreeze = (inst) => {
    const gl = inst && inst.canvas;
    if (!gl) return;
    requestAnimationFrame(() => {
      const fz = freezeCanvas();
      try {
        const r = gl.getBoundingClientRect();
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const w = Math.max(1, Math.round((r.width || 1280) * dpr));
        const h = Math.max(1, Math.round((r.height || 800) * dpr));
        if (fz.width !== w) fz.width = w;
        if (fz.height !== h) fz.height = h;
        const g = fz.getContext("2d");
        g.clearRect(0, 0, w, h);
        g.drawImage(gl, 0, 0, w, h);          // 同帧读：此刻缓冲还在
        // 抓没抓到：缩到 16×9 看一眼，全黑当失败（别把黑帧当背景）
        const t = document.createElement("canvas");
        t.width = 16; t.height = 9;
        const tg = t.getContext("2d");
        tg.drawImage(fz, 0, 0, 16, 9);
        const d = tg.getImageData(0, 0, 16, 9).data;
        let sum = 0;
        for (let i = 0; i < d.length; i += 4) sum += d[i] + d[i + 1] + d[i + 2];
        if (!sum) throw new Error("blank frame");
        fz.classList.add("ready");
        gl.classList.remove("ready");
      } catch {
        fz.classList.remove("ready");
        gl.classList.remove("ready");        // 抓不到 → 交回主题静态图（不会黑）
        root.classList.remove("has-scene");
      } finally {
        try { inst.pause(); } catch { /* 忽略 */ }
      }
    });
  };

  const sceneDrop = () => {
    if (sceneInst) {
      try { sceneInst.pause(); } catch { /* 忽略 */ }
      try { sceneInst.destroy({ releasePkgCache: true }); } catch { /* 忽略 */ }
      sceneInst = null;
    }
    sceneBase = "";
    [".lb-scene", ".lb-freeze"].forEach((sel) => {
      const c = box.querySelector(sel);
      if (c) { c.classList.remove("ready"); c.remove(); }   // 连画布一起撤：下次重挂拿干净上下文
    });
    root.classList.remove("has-scene");
  };

  const sceneApply = (base, collapsed, mine) => {
    if (sceneBad === base) { hideAll(); return; }        // 这张已知挂不上 → 退回静态图
    if (sceneInst) {
      if (sceneBase !== base) { sceneDrop(); }            // 换片
      else {                                             // 已经挂好：只切播放状态
        const gl = box.querySelector(".lb-scene");
        const fz = box.querySelector(".lb-freeze");
        if (collapsed) {
          if (fz) fz.classList.remove("ready");
          try { sceneInst.resume(); } catch { /* 忽略 */ }
          if (gl) gl.classList.add("ready");
        } else {
          // 展开：把**最后一帧**抓进定格层再 pause —— 和视频"暂停在那一帧"同一观感
          sceneFreeze(sceneInst);
        }
        return;
      }
    }
    if (sceneBusy) return;
    sceneBusy = true;
    sceneBase = base;
    (async () => {
      try {
        const lib = await loadSceneLib();
        if (mine !== seq || sceneBase !== base) return;
        const inst = await lib.mount(sceneCanvas(), {
          source: lib.httpSource(base),
          fit: "cover",
          // 只做背景：DPR 封到 1.5、30fps 封顶，省显存与电（场景壁纸比视频吃 GPU）
          renderDpr: Math.min(window.devicePixelRatio || 1, 1.5),
          fps: 30,
          volume: 0,
          autoplay: true,         // 先跑起来：展开状态也要先出一帧才能"定格"
          scripts: false,        // ⚠ 默认不执行壁纸自带脚本：它是同源 eval，能直接调本机 API
        });
        if (mine !== seq || sceneBase !== base) {
          try { inst.destroy({ releasePkgCache: true }); } catch { /* 忽略 */ }
          return;
        }
        sceneInst = inst;
        inst.on("error", (err) => {
          sceneBad = base;
          sceneDrop();
          root.classList.remove("has-live");
          setBanner("场景壁纸渲染出错，已回退静态背景：" + ((err && err.message) || err));
        });
        root.classList.add("has-scene");
        if (collapsed) {
          box.querySelector(".lb-scene").classList.add("ready");
        } else {
          sceneFreeze(inst);                  // 展开状态挂载：抓一帧定格，再 pause
        }
      } catch (e) {
        sceneBad = base;
        sceneDrop();
        root.classList.remove("has-live");
        setBanner("场景壁纸渲染失败，已回退静态背景：" + ((e && e.message) || e));
      } finally {
        sceneBusy = false;
      }
    })();
  };

  const sync = () => {
    const t = themeNow();
    const url = urlOf(t);
    const kind = kindOf(t);
    const collapsed = root.dataset.sidebar === "closed";
    // ★「会话界面也动」：开着时展开侧栏也继续渲染/播放（视频与场景都适用）
    const playing = collapsed || !!SET.liveAlways;
    const mine = ++seq;

    if (!url) { hideCustomBg(); hideAll(); return; }
    if (kind === "image") {              // ★自定义静态图：不受「动态壁纸」开关 / reduce-motion 影响
      hideAll();
      if (customBg) customBg.style.backgroundImage = 'url("' + url + '")';
      root.classList.add("has-custom-bg");
      return;
    }
    hideCustomBg();
    if (reduce.matches || !SET.live) { hideAll(); return; }
    box.style.backgroundImage = "";
    root.classList.add("has-live");

    if (kind === "scene") {              // 场景：视频层让位，交给 WebWallGL
      clearInterval(rampTimer); rampTimer = null;
      vids.forEach((v) => { try { v.pause(); } catch { /* 忽略 */ } v.classList.remove("ready"); });
      sceneApply(url, playing, mine);
      return;
    }
    sceneDrop();                         // 换回视频：销毁场景、释放 GL

    if (active.dataset.src === url) {    // 已经是这张：只调播放状态
      if (active.readyState >= 2) active.classList.add("ready");
      play(t, playing, mine);
      return;
    }
    swap(t, url, playing, mine);
  };

  // 加载失败（文件被删 / Steam 换盘）：撤掉那一层；若它是当前层就退回静态壁纸
  vids.forEach((v) => v.addEventListener("error", () => {
    if (v.dataset.src) { v.dataset.src = ""; v.classList.remove("ready"); }
    if (v === active) root.classList.remove("has-live");
  }));
  new MutationObserver(sync).observe(root,
    { attributes: true, attributeFilter: ["data-theme", "data-sidebar"] });

  const reload = async () => {           // 重拉清单：新订阅的壁纸 / 面板里改选
    try { urls = await api("/live/wallpapers"); } catch { /* 拉不到就继续用旧的 */ }
    sync();
  };
  liveReload = reload;
  liveApply = sync;                      // 设置面板改了（速度/开关）立刻生效
  POLL.interval("live-reload", 5 * 60 * 1000, reload);
  sync();
}

/** 静态背景：两层图按主题交叉淡入（换主题只切 opacity，避免大图重解码/光栅化卡顿） */
function initStaticBg() {
  const root = document.documentElement;
  const apply = () => {
    // ★静态背景槽：`ui 0.2.123` 起只有一个内置主题 → 恒用 chapel 槽（暗调版，同一张官方壁纸）
    const t = "chapel";
    document.querySelectorAll("#static-bg .sb").forEach((el) => el.classList.toggle("on", el.dataset.bg === t));
  };
  new MutationObserver(apply).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  apply();
}
