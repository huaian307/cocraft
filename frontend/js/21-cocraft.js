/* 21-cocraft.js —— 群聊抽屉 + AI 成员 + 派活/对比/仲裁（P1 从 app.js 拆出，纯搬运不改逻辑）
 * 依赖：00-util.js 的 $ / api / esc / renderText / POLL；运行时会访问 state / SET / MODEL / NOTIFY
 *       （这些在 app.js 里定义，函数运行时已加载，安全）。
 */

/* ---------------- 群聊（cocraft）：面板内自建 UI ----------------
   - 登录/注册/会话/历史：REST 走我们后端代理 `/cocraft/*`（避开跨域）
   - 发消息 / 实时收：浏览器直连 `wss://<host>/ws?token=`（实测跨源 101 可连）
   - 抽屉左边缘可拖动改宽度（--cc-w，落 localStorage）                        */

const CC_OPEN_KEY = "cocraft.open";
const CC_TOKEN_KEY = "cocraft.token";
const CC_W_KEY = "cocraft.w";
const CC_USERS_KEY = "cocraft.users";   // {id: {id,username,avatar,email,created_at}}
const CC_UNREAD_KEY = "cocraft.cc.unread";  // {chatID: n} 未读计数（自己发的不算；打开即清零）
const CC_W_DEFAULT = 520;

const CC = {
  base: "https://www.animaker.cc.cd",
  ws: "",                 // 由 GET /cocraft/_url 取（可被后端 env 覆盖）
  token: "",
  me: null,
  users: {},              // 用户信息缓存（见 ccCacheUser / ccUserName）
  ai: [],                 // AI 成员（/ccagent/list；后端桥）
  permDone: {},           // 已处理过的权限申请（群消息 id → true；点了按钮就不再显示弹窗）
  unread: {},             // 未读：{chatID: n}（自己发的不算；打开会话 / 抽屉即清零）
  chats: [],
  chatID: "",
  msgs: [],
  msgsTotal: 0,           // 服务端该会话的消息总数（用来算"最后一页"的 offset）
  members: [],            // 当前会话成员（含 agent；带 presence）
  floor: null,            // 当前发言权 {holder_agent_id, deadline}
  floorTimer: null,       // 发言权倒计时（只在有 deadline 时转）
  wsock: null,
  wsPing: null,
  wsRetry: 0,
  poll: null,
  activity: {},           // ★C1：活动卡 {[chatID]: [{id,seq,rev,agent,tool,summary,status,ts,detail}]}
  actRev: {},             // ★C1：{[chatID]: 已拉到的最大 rev（`since` 比的就是它）}
  actTimer: null,         // ★C1：轮询心跳（抽屉关着时慢跑，只为顶栏「工作中」角标）
  actAt: 0,
  roles: {},              // ★主动编排：角色表 {id: {label}}（由 /ccagent/list 给）
  dis: null,              // ★主动编排：最近一次派活 {chat, task, ts, members:[{id,name,role,roleLabel,status}]}
  disSel: new Set(),      // 派活台里已勾选的成员 id
  disOpen: false,
  teamPresets: [],        // ★一键建组：内置 + 自定义预设（/ccagent/list 的 teamPresets）
  teamDraft: null,        // ★当前面板里那几行（改过角色/名字就存这里；换预设即清空）
  pickId: null,           // ★刚点过「推荐 X」跳到的那一列（成员 id；重画后仍高亮）
};

const ccEl = (id) => document.getElementById(id);
const ccLoad = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
const ccStore = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 隐私模式 */ } };

/* ---- 未读（A1/A2）：自己发的不算；正在看（抽屉开着 + 当前会话）不算；打开会话 / 抽屉即清零 ---- */
function ccLoadUnread() {
  try { CC.unread = JSON.parse(localStorage.getItem(CC_UNREAD_KEY) || "{}") || {}; }
  catch { CC.unread = {}; }
}
function ccSaveUnread() {
  try { localStorage.setItem(CC_UNREAD_KEY, JSON.stringify(CC.unread || {})); } catch { /* ignore */ }
}
function ccUnreadOf(id) { return Number((CC.unread || {})[String(id)] || 0); }
function ccUnreadTotal() {
  return Object.keys(CC.unread || {}).reduce((a, k) => a + (Number(CC.unread[k]) || 0), 0);
}
/** 收到一条别人 / AI 的消息 → 记一条未读（带守卫）。 */
function ccBumpUnread(id, msg) {
  if (id == null) return;
  const key = String(id);
  if (CC.me && msg && String(msg.sender_id) === String(CC.me.id)) return;   // 自己发的
  if (key === String(CC.chatID) && cocraftIsOpen()) return;                 // 正在看这一条
  CC.unread[key] = (Number(CC.unread[key]) || 0) + 1;
  ccSaveUnread();
  ccSyncUnreadUI();
}
/** 打开一个会话 / 抽屉 → 清掉它的未读。 */
function ccUnreadClear(id) {
  const key = String(id == null ? "" : id);
  if (!key || !(CC.unread || {})[key]) return;
  delete CC.unread[key];
  ccSaveUnread();
  ccSyncUnreadUI();
}
/** 未读变化 → 重画会话列表 + 顶栏按钮徽章。 */
function ccSyncUnreadUI() {
  try { ccRenderChats(); } catch { /* ignore */ }
  if (typeof syncHubBtn === "function") syncHubBtn();
  try { syncTitle(); } catch { /* ignore */ }
}

/* ---- ★B3：权限申请「强提醒」----
   ① **标题栏计数**：`(N) 标题`，N = 群聊未读 + 待批准权限（各 1）。
   ② **系统级提醒**：面板**不在前台**（切走 / 最小化）时，把提醒交给独立托盘进程 ——
      `POST /panel/notify` → 面板写 `runtime/state/_notify.json` → `tray_host.py` 轮询它
      弹系统气泡（`icon.notify`）并让窗口闪任务栏（`panel_window.py` 的 `flash` 命令）。
      面板就在眼前时不弹系统通知（页面里的横幅 / 小弹窗已经够用），顺便天然节流。
      ⚠ 浏览器窗口模式（Edge --app）没有托盘进程 → 只写文件不弹气泡，标题栏计数照常。 */
var SYNC_TITLE_BASE = "";
const TITLE_FALLBACK = "cocraft";
const CC_PERM_ALERT_KEY = "cocraft.cc.permAlert";     // 已强提醒过的申请 id

/** 需要用户注意的数量：群聊未读 + 待批准权限（各算 1）。 */
function attentionCount() {
  let n = 0;
  try { n += ccUnreadTotal(); } catch { /* ignore */ }
  try { if (ccPermPending()) n += 1; } catch { /* ignore */ }
  return n;
}

/** 写 `document.title`：N>0 时加 `(N) ` 前缀；传 base 时顺手更新标题本体。 */
function syncTitle(base) {
  if (base) SYNC_TITLE_BASE = String(base);
  const b = SYNC_TITLE_BASE || TITLE_FALLBACK;
  const n = attentionCount();
  try { document.title = n > 0 ? "(" + n + ") " + b : b; } catch { /* ignore */ }
}

function ccPermAlertSeen(id) {
  try { return String(localStorage.getItem(CC_PERM_ALERT_KEY) || "") === String(id); }
  catch { return false; }
}
function ccPermAlertMark(id) {
  try { localStorage.setItem(CC_PERM_ALERT_KEY, String(id)); } catch { /* ignore */ }
}

/** 面板不在前台时，为一条**新**的权限申请请求系统级强提醒（同一申请只提醒一次）。 */
function ccPermStrongAlert(m, info, label) {
  if (!m || m.id == null) return;
  if (ccPermAlertSeen(m.id)) return;
  let fg = true;
  try { fg = !document.hidden && document.hasFocus(); } catch { fg = true; }
  if (fg) return;                       // 面板就在眼前 → 页面里的提醒已经够
  ccPermAlertMark(m.id);
  const what = info.destructive ? "请求执行破坏性操作" : "请求权限";
  try {
    fetch("/panel/notify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: "cocraft · 群聊权限申请",
        body: `${label} ${what}：${info.title}${info.kind ? " · " + info.kind : ""}（点托盘图标打开面板）`,
        flash: true,
      }),
    }).catch(() => {});
  } catch { /* ignore */ }
}

/* ---- ★C1：进行中活动卡（让「AI 真的在干活」看得见；见 `docs/群聊存在感-方案.md` §12）----
   数据源：`GET /ccagent/activity?chat=<id>&since=<seq>` —— 桥的内存环形缓冲（§12.1）。
   ⚠ 走**面板轮询**，**不让桥往群里发消息** —— 既不污染聊天历史，也不碰"只认拥有者批准"的审计语义。
   ⚠ 抽屉关着时**慢轮询**（只更新顶栏「工作中」角标、不渲染卡片）：§12.2 #7 的原话是"关着不轮询"，
     但 #10 又要求"关着时角标显示工作中" —— 折中就是慢跑一点点（本机端点，几乎不要钱）。 */
const CC_ACT_BUSY_MS = 2000;      // 有成员在干活 → 2s
const CC_ACT_IDLE_MS = 6000;      // 空闲 / 抽屉关着 → 6s
const CC_ACT_KEEP = 200;          // 与服务端 deque(maxlen=200) 对齐
const CC_ACT_SHOW = 8;            // 卡片最多显示最近几条
const CC_ACT_BUSY_ST = { running: 1, asking: 1 };
const CC_ACT_ZH = {
  running: "进行中", asking: "等待批准", completed: "完成",
  error: "失败", allowed: "已批准", denied: "已拒绝", timeout: "超时未批",
};

/** 某个会话的活动列表。 */
function ccActItems(chatID) { return (CC.activity || {})[String(chatID || "")] || []; }

/** 还有没有"没干完"的活动（决定轮询节奏 / 忙碌标记）。 */
function ccActBusy(chatID) {
  return ccActItems(chatID || CC.chatID).some((a) => CC_ACT_BUSY_ST[a.status]);
}

/** 正在干活的成员名集合（当前会话）→ C2 的「工作中」小圆片。 */
function ccActBusyAgents(chatID) {
  const s = new Set();
  ccActItems(chatID || CC.chatID).forEach((a) => {
    if (CC_ACT_BUSY_ST[a.status] && a.agent) s.add(String(a.agent));
  });
  return s;
}

/** 把新拉到的活动并进内存。
 *  ⚠ 按 **id**（稳定的工具调用标识）**替换**同一条，比较的是 **rev**（修改版本）——
 *  因为桥收尾是**原地改**那条记录（running → completed），只按 seq 去重会永远看不到它变。
 *  `seq`（创建顺序）只用来排序，保证收尾后那一行不乱跳。 */
function ccActMerge(chatID, items) {
  const key = String(chatID);
  const cur = (CC.activity[key] = CC.activity[key] || []);
  CC.actRev = CC.actRev || {};
  const byId = {};
  cur.forEach((x) => { byId[String(x.id)] = x; });
  let mx = Number(CC.actRev[key] || 0);
  (items || []).forEach((it) => {
    if (!it || it.id == null) return;
    const rv = Number(it.rev) || 0;
    if (rv > mx) mx = rv;
    const id = String(it.id);
    const old = byId[id];
    if (!old) { cur.push(it); byId[id] = it; }
    else if (rv > (Number(old.rev) || 0)) { Object.assign(old, it); }
  });
  cur.sort((a, b) => (Number(a.seq) || 0) - (Number(b.seq) || 0));
  if (cur.length > CC_ACT_KEEP) cur.splice(0, cur.length - CC_ACT_KEEP);
  CC.actRev[key] = mx;
  return mx;
}

/** 拉一次活动（抽屉关着时也在跑，为了顶栏角标）。 */
async function ccActTick() {
  if (!CC.token || !CC.chatID) return;
  const chat = String(CC.chatID);
  const since = Number(((CC.actRev || {})[chat] || 0));
  try {
    const r = await ccAgentFetch("/activity?chat=" + encodeURIComponent(chat) + "&since=" + since);
    if (String(CC.chatID) !== chat) return;         // 中途换了会话 → 这次结果丢掉
    ccActMerge(chat, (r && r.items) || []);
  } catch { /* 桥没跑 / 未登录 → 静默（不打扰聊天） */ }
  ccActRender();
  ccActBadge();
  if (typeof ccRenderRoomMeta === "function") ccRenderRoomMeta();   // 顺手刷「工作中」小圆片
}

/** 定频心跳：按"有没有在干活 / 抽屉开没开"决定这次要不要真去拉。 */
function ccActBeat() {
  const open = cocraftIsOpen();
  const ms = (open && ccActBusy()) ? CC_ACT_BUSY_MS : CC_ACT_IDLE_MS;
  const now = Date.now();
  if (now - (CC.actAt || 0) < ms) return;
  CC.actAt = now;
  ccActTick();
}
function ccActStart() {
  if (CC.actTimer) return;
  CC.actTimer = POLL.interval("cc-act", 500, ccActBeat);
  ccActBeat();
}
function ccActStop() {
  if (CC.actTimer) { POLL.clear("cc-act"); CC.actTimer = null; }
}
/** 登录态 / 当前会话变化时同步轮询开关。 */
function ccActSync() {
  if (!CC.token || !CC.chatID) { ccActStop(); return; }
  ccActStart();
  // ★即使心跳定时器已经在跑，也允许"立刻拉一次"（是否真拉由 `actAt` 节流决定）——
  //   否则换了会话要干等 2~6s 才刷新（实测：app 启动时自动恢复上次会话，把节流占住了）。
  ccActBeat();
}

/** 活动卡 HTML（中文口径与轨迹页一致；`status` 决定图标与颜色）。 */
function ccActHtml(items) {
  const row = (a) => {
    const st = String(a.status || "");
    const cls = CC_ACT_BUSY_ST[st] ? "running"
      : ((st === "error" || st === "denied" || st === "timeout") ? "bad" : "done");
    const ico = st === "asking" ? "🔐"
      : (st === "running" ? '<i class="cc-act-spin"></i>' : (cls === "bad" ? "✕" : "✓"));
    return `<div class="cc-act-row ${cls}"><span class="cc-act-ico">${ico}</span>`
      + `<b class="cc-act-tool">${esc(a.tool || "工具")}</b>`
      + `<span class="cc-act-sum" title="${esc(a.detail || a.summary || "")}">${esc(a.summary || "")}</span>`
      + `<span class="cc-act-st">${esc(CC_ACT_ZH[st] || st)}</span></div>`;
  };
  const recent = (items || []).slice(-CC_ACT_SHOW);
  const busyItems = recent.filter((a) => CC_ACT_BUSY_ST[a.status]);
  const who = [...new Set(busyItems.map((a) => a.agent))].filter(Boolean).join("、");
  const cap = busyItems.length ? `执行中${who ? " · " + esc(who) : ""}` : "刚刚做过";
  return `<div class="cc-act-cap">${busyItems.length ? '<i class="cc-act-dot"></i>' : ""}${cap}</div>`
    + recent.map(row).join("");
}

/** 把活动卡画到**消息流末尾**。
 *  ⚠ `ccRenderMsgs` 会重建 `#cc-msgs` 的 innerHTML → 每次都要重新 append（否则卡片被冲掉）。 */
function ccActRender() {
  const box = ccEl("cc-msgs");
  if (!box) return;
  let el = ccEl("cc-act-card");
  const items = ccActItems(CC.chatID);
  if (!items.length) { if (el) el.remove(); return; }
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  if (!el) { el = document.createElement("div"); el.id = "cc-act-card"; el.className = "cc-act"; }
  el.innerHTML = ccActHtml(items);
  if (box.lastChild !== el) box.appendChild(el);        // 永远贴最后
  if (atBottom) ccScrollBottom(box);
}

/** 顶栏群聊入口的「工作中」角标（抽屉关着时也看得见）。 */
function ccActBadge() {
  const b = $("btn-hub");
  if (!b) return;
  const busy = ccActItems(CC.chatID).some((a) => CC_ACT_BUSY_ST[a.status]);
  b.classList.toggle("cc-busy", !!busy);
  if (busy) b.setAttribute("data-busy", "工作中"); else b.removeAttribute("data-busy");
}

/* ---- ★A3：@我 → 抽屉关着时给一条**可点**横幅（点了就开抽屉 + 打开那个会话） ----
   横幅同时只能显示一条 → 走通知聚合器 NOTIFY（权限 100 > @我 80 > 更新 50 > 普通 0）。
   `mentions` 服务端给的是**名字数组**（见 docs/COCRAFT.md）；另外 `mention` 帧本身就是"点名了我"。 */
function ccMentionedMe(d, force) {
  if (!d) return false;
  if (CC.me && String(d.sender_id) === String(CC.me.id)) return false;     // 自己说的不算
  if (force) return true;
  const xs = [];
  if (Array.isArray(d.mentions)) xs.push.apply(xs, d.mentions);
  else if (d.mentions != null) xs.push(d.mentions);
  if (d.mention_id != null) xs.push(d.mention_id);
  const myId = CC.me && CC.me.id != null ? String(CC.me.id) : "";
  const myName = (CC.me && (CC.me.username || CC.me.name)) || "";
  for (const x of xs) {
    if (x && typeof x === "object") {
      const oid = x.id != null ? String(x.id) : "";
      const onm = String(x.username || x.name || "");
      if ((myId && oid === myId) || (myName && onm === myName)) return true;
      continue;
    }
    const s = String(x == null ? "" : x);
    if ((myId && s === myId) || (myName && s === myName)) return true;
  }
  const c = String(d.content || "");
  return !!(myName && (c.indexOf("@" + myName) >= 0 || c.indexOf("@" + myId) >= 0));
}
function ccChatNameOf(id) {
  const c = (CC.chats || []).find((x) => String(x.id) === String(id));
  return (c && ccChatName(c)) || ("会话 #" + id);
}
/** 被 @ → 顶栏可点横幅（点 = 开抽屉 + 打开那个会话 + 贴底）。 */
function ccMentionNotify(d, force) {
  if (!ccMentionedMe(d, force)) return;
  // 正在看那个会话（抽屉开着）→ 消息就在眼前，不用再提醒
  if (cocraftIsOpen() && String(CC.chatID) === String(d.chat_id)) return;
  const who = ccUserName(d.sender_id) || "有人";
  NOTIFY.set("cc-mention", {
    priority: 80,
    text: `群聊：${who} 在「${ccChatNameOf(d.chat_id)}」@了你 —— 点这里打开`,
    onClick: () => {
      NOTIFY.clear("cc-mention");
      setCocraft(true);
      ccOpenChat(String(d.chat_id));
      setTimeout(() => { try { ccScrollBottom(ccEl("cc-msgs")); } catch { /* ignore */ } }, 120);
    },
  });
}
/** 打开群聊（或在看那个会话）→ 撤下 @我 提醒。 */
function ccClearMentionNotify() { NOTIFY.clear("cc-mention"); }

/* ---- 用户信息缓存（用 GET /users/{id} 补全用户名 ── 见 docs/COCRAFT.md） ----
   登录/注册返回的 user 就是自己 → 直接缓存；其它人（消息发送者）缺名字时按 id 拉一次。 */
function ccLoadUsers() {
  try { CC.users = JSON.parse(localStorage.getItem(CC_USERS_KEY) || "{}") || {}; }
  catch { CC.users = {}; }
}
function ccSaveUsers() {
  try { localStorage.setItem(CC_USERS_KEY, JSON.stringify(CC.users || {})); } catch { /* ignore */ }
}
/** 把 {id,username,avatar,email,created_at} 记进缓存（登录/注册/接口都走它）。 */
function ccCacheUser(u) {
  if (!u || u.id == null) return;
  const id = String(u.id);
  CC.users[id] = Object.assign({}, CC.users[id] || {}, u);
  ccSaveUsers();
}
/** 取显示名：缓存 → 缓存里没有就发一次 /users/{id} 并先兜底显示「用户 id」。 */
function ccUserName(id) {
  if (id == null || id === "") return "";
  const key = String(id);
  if (CC.me && String(CC.me.id) === key && CC.me.username) return CC.me.username;
  const u = CC.users[key];
  if (u && (u.username || u.email)) return u.username || u.email;
  ccFetchUser(key);
  return "用户 " + key;
}
const ccUserBusy = {};
/** GET /users/{id}（Bearer）补全用户名；成功后就地重画（不刷新整页）。 */
async function ccFetchUser(id) {
  const key = String(id);
  if (!CC.token || ccUserBusy[key]) return;
  if (CC.users[key] && CC.users[key].username) return;
  ccUserBusy[key] = true;
  try {
    const u = await ccFetch("/users/" + encodeURIComponent(key));
    ccCacheUser(u && (u.user || u));          // 兼容 {user:{…}} 或直接对象
    ccRefreshUserUI();
  } catch { /* 拉不到就用「用户 id」兜底 */ }
  finally { setTimeout(() => { delete ccUserBusy[key]; }, 5000); }
}
/** 用户名补全后，就地刷新「我是谁」和消息里的发送者名（保持滚动位置）。 */
function ccRefreshUserUI() {
  if (!cocraftIsOpen()) return;
  const who = ccEl("cc-who");
  if (who && CC.me) who.textContent = ccUserName(CC.me.id);
  ccUpdateMyId();
  const box = ccEl("cc-msgs");
  const top = box ? box.scrollTop : 0;
  ccRenderMsgs(false);
  if (box) box.scrollTop = top;
}

/** 「我的 ID」：本机显示、仅供自己看/复制（不发给别人，聊天里别人看不到）。 */
function ccUpdateMyId() {
  const el = ccEl("cc-myid");
  if (!el) return;
  if (CC.me && CC.me.id != null) { el.textContent = "ID " + CC.me.id; el.hidden = false; }
  else { el.textContent = ""; el.hidden = true; }
}

function ccCopyMyId(el) {
  if (!CC.me || CC.me.id == null) return;
  const t = String(CC.me.id);
  const done = () => {
    if (!el) return;
    el.textContent = "已复制 " + t;
    setTimeout(ccUpdateMyId, 1000);
  };
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(t).then(done).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = t; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      document.body.removeChild(ta); done();
    }
  } catch { /* ignore */ }
}

function cocraftIsOpen() {
  const b = $("cocraft");
  return !!(b && b.classList.contains("open"));
}

function toggleCocraft() { setCocraft(!cocraftIsOpen()); }

/** 开 / 收抽屉（不再加载任何第三方 iframe；内容是我们自己的 UI） */
function setCocraft(open) {
  const box = $("cocraft");
  if (!box) return;
  // 群聊与编码模式都是右侧抽屉 → 互斥（同时开会互相压住）
  if (open) { try { if (CODE.open) codeClose(); } catch { /* ignore */ } }
  box.classList.toggle("open", !!open);
  box.setAttribute("aria-hidden", open ? "false" : "true");
  document.documentElement.classList.toggle("cc-open", !!open);   // ← 分屏：面板内容让位
  if (open) ccApplyWidth(parseInt(ccLoad(CC_W_KEY), 10) || CC_W_DEFAULT, true);  // 打开时把让位宽度同步好
  try { localStorage.setItem(CC_OPEN_KEY, open ? "1" : "0"); } catch { /* 隐私模式 */ }
  if (open && CC.chatID) ccUnreadClear(CC.chatID);   // ★打开抽屉 = 在看当前会话 → 未读清零
  if (open) ccClearMentionNotify();                  // ★抽屉开了 → 撤下 @我 提醒
  if (typeof syncHubBtn === "function") syncHubBtn();
  if (open && CC.token && CC.chatID && !CC.msgs.length) ccLoadMsgs();
  ccActSync();                                       // ★C1：抽屉开合 → 活动卡轮询节奏跟着变
  // 打开时重算用户名（缓存里可能已经有；没有的就补一次 /users/{id}）——
  // 否则名字可能是在抽屉关着的时候拉回来的，界面还停在「用户 id」。
  if (open) setTimeout(() => {
    ccRefreshUserUI();
    if (CC.me && CC.me.id != null && !((CC.users[String(CC.me.id)] || {}).username)) ccFetchUser(CC.me.id);
    CC.msgs.forEach((m) => ccSenderName(m));
  }, 0);
  syncDrawerOverlap();
}

/** 抽屉顶部的一行提示 */
function ccNote(text, isErr) {
  const n = ccEl("cc-note");
  if (!n) return;
  n.textContent = text || "";
  n.hidden = !text;
  n.classList.toggle("err", !!isErr);
}

/** 调 cocraft（经后端代理）。失败抛带 detail 的 Error。 */
async function ccFetch(path, opts) {
  opts = opts || {};
  const headers = { "Content-Type": "application/json" };
  if (CC.token) headers["Authorization"] = "Bearer " + CC.token;
  const res = await fetch("/cocraft" + path, {
    method: opts.method || "GET", headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined, cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const d = (data && (data.detail || data.error)) || res.statusText || ("HTTP " + res.status);
    const err = new Error(typeof d === "string" ? d : JSON.stringify(d));
    err.status = res.status;
    throw err;
  }
  return data;
}

/* ---- 视图切换 ---- */
function ccShowAuth(msg) {
  ccEl("cc-auth").hidden = false;
  ccEl("cc-main").hidden = true;
  ccEl("cc-who").textContent = "";
  ccEl("cc-myid").hidden = true;
  ccEl("cc-myid").textContent = "";
  ccEl("cc-logout").hidden = true;
  ccMenuOpen(false);
  ccAuthMsg(msg || "", !!msg);
  CC.me = null;
  CC.members = [];
  CC.floor = null;
  if (CC.floorTimer) { POLL.clear("cc-floor"); CC.floorTimer = null; }
  CC.unread = {};                    // ★登出 / 未登录 → 清掉未读，别残留
  CC.activity = {};                  // ★C1：活动卡也跟着清（否则顶栏会留个「工作中」角标）
  CC.actRev = {};
  ccActStop();
  ccSaveUnread();
  ccClearMentionNotify();            // ★登出 → 也撤下 @我 提醒
  NOTIFY.clear("cc-perm");           // ★登出 → 权限提醒一并撤下
  try { syncTitle(); } catch { /* ignore */ }   // ★B3：登出 → 标题栏不留计数
  if (typeof syncHubBtn === "function") syncHubBtn();
  ccStopPoll();
  ccCloseWS();
}

function ccShowMain() {
  ccEl("cc-auth").hidden = true;
  ccEl("cc-main").hidden = false;
  ccCacheUser(CC.me);
  ccEl("cc-who").textContent = CC.me ? ccUserName(CC.me.id) : "";
  ccUpdateMyId();
  ccEl("cc-logout").hidden = false;
  ccConnectWS();
  ccLoadChats();
  ccStartPoll();
  ccActSync();               // ★C1：登录后如果已经有选中的会话，活动卡轮询也跟着起来
}

function ccAuthMsg(text, isErr) {
  const m = ccEl("cc-auth-msg");
  if (!m) return;
  m.textContent = text || "";
  m.classList.toggle("err", !!isErr);
}

/* ---- 登录态 ---- */
function ccSetToken(t) {
  CC.token = t || "";
  if (t) ccStore(CC_TOKEN_KEY, t);
  else { try { localStorage.removeItem(CC_TOKEN_KEY); } catch { /* ignore */ } }
}

async function ccInit() {
  try {
    const r = await fetch("/cocraft/_url", { cache: "no-store" });
    const j = await r.json();
    if (j && j.ws) { CC.ws = j.ws; CC.base = j.base || CC.base; }
  } catch { /* 用默认 */ }
  if (!CC.ws) CC.ws = "wss://www.animaker.cc.cd/ws";
  ccLoadUsers();
  ccLoadUnread();
  syncTitle();                          // ★B3：启动就把未读计数写进标题栏
  CC.token = ccLoad(CC_TOKEN_KEY);
  if (CC.token) {
    try {
      const me = await ccFetch("/auth/me");
      CC.me = (me && me.user) || me || null;
      ccCacheUser(CC.me);
      ccShowMain();
      // 有些实现 /auth/me 只给 id → 用 /users/{id} 补全用户名
      if (CC.me && CC.me.id != null && !CC.me.username) ccFetchUser(CC.me.id);
    } catch (e) {
      ccSetToken("");
      ccShowAuth(e.status === 401 ? "登录已过期，请重新登录" : ("连不上服务：" + e.message));
    }
  } else {
    ccShowAuth("");
  }
}

/* ---- 会话列表 ---- */
const ccChatName = (c) => c.name || ("会话 #" + c.id);

/** 新建群聊会话：POST /chats（{name?, avatar?, members?}）→ 刷新列表并打开它。 */
async function ccNewChat() {
  let name = "新会话";
  try {
    const r = prompt("新建会话：给它起个名字（可留空）", "新会话");
    if (r === null) return;                       // 取消
    name = String(r).trim().slice(0, 128) || "新会话";
  } catch { /* prompt 不可用就用默认名 */ }
  try {
    const c = await ccFetch("/chats", { method: "POST", body: { name } });
    await ccLoadChats();
    if (c && c.id != null) await ccOpenChat(c.id);
    ccNote("已新建会话：" + (c && (c.name || c.id) ? (c.name || c.id) : name));
  } catch (e) {
    ccNote("新建会话失败：" + e.message, true);
  }
}

async function ccLoadChats() {
  try {
    const list = await ccFetch("/chats");
    // 服务端返回形状兼容：裸数组 / {items:[…]}（新 ChatList）/ {data:[…]}（旧）
    CC.chats = Array.isArray(list) ? list : ((list && (list.items || list.data)) || []);
    ccRenderChats();
    if (!CC.chatID && CC.chats.length) ccOpenChat(CC.chats[0].id);
    else ccRenderRoomHead();
  } catch (e) {
    if (e.status === 401) { ccSetToken(""); ccShowAuth("登录已过期，请重新登录"); return; }
    ccNote("会话列表加载失败：" + e.message, true);
  }
}

function ccRenderChats() {
  const box = ccEl("cc-chats");
  if (!box) return;
  if (!CC.chats.length) { box.innerHTML = `<div class="cc-empty">还没有会话</div>`; return; }
  box.innerHTML = CC.chats.map((c) => {
    const n = (c.members || []).length;
    const u = ccUnreadOf(c.id);                                        // ★未读数
    const badge = u ? `<em class="cc-badge">${u > 99 ? "99+" : u}</em>` : "";
    return `<div class="cc-chat${String(c.id) === String(CC.chatID) ? " on" : ""}" data-chat="${esc(c.id)}">`
      + `<b>${esc(ccChatName(c))}</b>${badge}<i>${n ? n + " 人" : ""}</i></div>`;
  }).join("");
}

/* ---- 聊天内容 ---- */
async function ccOpenChat(id) {
  CC.chatID = String(id);
  ccUnreadClear(id);                   // ★打开会话 → 未读清零
  ccClearMentionNotify();              // ★已经点开来看它了 → 撤下 @我 提醒
  CC.msgs = [];
  CC.members = [];
  CC.floor = null;
  // ★C1：换会话 → 该会话的活动**从头拉**（since=0），卡片立刻有内容
  CC.activity[String(id)] = [];
  CC.actRev[String(id)] = 0;
  CC.actAt = 0;           // ★清掉节流 → ccActSync 会**立刻**拉一次（否则要等 2~6s 才刷新）
  ccMenuOpen(false);
  ccRenderChats();
  ccRenderRoomHead();
  ccLoadMembers();        // 成员在线状态（不 await：慢也不该挡住消息）
  ccLoadFloor();          // 当前发言权
  ccActSync();            // ★C1：活动卡轮询
  await ccLoadMsgs();
}

function ccRenderRoomHead() {
  const title = ccEl("cc-room-title");
  if (!title) return;
  const c = CC.chats.find((x) => String(x.id) === String(CC.chatID));
  if (!c) { title.textContent = "选一个会话"; ccRenderRoomMeta(); return; }
  title.textContent = ccChatName(c);
  ccRenderRoomMeta();
}

/* ---- 成员在线状态（presence）与发言权（floor）----
   数据来源：GET /chats/{id}/members、GET /chats/{id}/floor，
   以及 WebSocket **主动推送**的 presence / floor 事件（服务端已恢复推送，见 tools/cocraft_ws_check.py）。 */
function ccMemberName(m) {
  return (m && (m.username || m.name || m.display)) || ("用户 " + (m && m.id));
}
function ccFloorLeft() {
  const dl = CC.floor && CC.floor.deadline;
  if (!dl) return null;
  const t = Date.parse(dl);
  if (isNaN(t)) return null;
  const s = Math.round((t - Date.now()) / 1000);
  return s > 0 ? s : 0;
}
function ccFloorChip() {
  if (!CC.floor) return "";
  const hid = CC.floor.holder_agent_id;
  if (hid == null) return `<span class="cc-floor idle" title="当前无人持有发言权">发言权：空闲</span>`;
  const m = (CC.members || []).find((x) => x.kind === "agent" && String(x.id) === String(hid));
  const nm = m ? ccMemberName(m) : ("agent " + hid);
  const left = ccFloorLeft();
  return `<span class="cc-floor" title="${esc(CC.floor.deadline || "无期限")}">发言权：${esc(nm)}`
    + (left != null ? `（${left}s）` : "") + `</span>`;
}
function ccRenderRoomMeta() {
  const box = ccEl("cc-room-meta");
  if (!box) return;
  const mem = CC.members || [];
  if (!CC.chatID || (!mem.length && !CC.floor)) { box.hidden = true; box.innerHTML = ""; return; }
  const on = mem.filter((m) => m.presence === "online").length;
  const busyAg = ccActBusyAgents(CC.chatID);      // ★C1/C2：正在干活的成员（前端推断，**不动服务端 presence**）
  const chips = mem.map((m) => {
    const isAgent = m.kind === "agent";
    const onl = m.presence === "online";
    const fl = isAgent && CC.floor && String(CC.floor.holder_agent_id) === String(m.id);
    const bz = isAgent && (busyAg.has(String(m.name || "")) || busyAg.has(String(m.display || ""))
                           || busyAg.has(String(ccMemberName(m))));
    return `<span class="cc-chip${isAgent ? " agent" : ""}${fl ? " floor" : ""}${bz ? " busy" : ""}"`
      + ` title="${esc(ccMemberName(m))}${isAgent ? "（agent）" : ""}${onl ? " · 在线" : " · 离线"}${bz ? " · 工作中" : ""}">`
      + `<i class="cc-dot${onl ? " on" : ""}"></i>${isAgent ? "🤖" : ""}${esc(ccMemberName(m))}`
      + (bz ? `<em class="cc-busy-tag">工作中</em>` : "") + `</span>`;
  }).join("");
  box.innerHTML = `<span class="cc-meta-on">在线 ${on}/${mem.length}</span>` + chips + ccFloorChip();
  box.hidden = false;
  ccEnsureFloorTimer();
}
/** 发言权倒计时：只在有未到期的 deadline 时每秒重画，到期即清空。 */
function ccEnsureFloorTimer() {
  const need = !!CC.floor && ccFloorLeft() > 0;
  if (need && !CC.floorTimer) {
    CC.floorTimer = POLL.interval("cc-floor", 1000, () => {
      const left = ccFloorLeft();
      if (!CC.floor || left === null || left <= 0) {
        POLL.clear("cc-floor"); CC.floorTimer = null;
        if (CC.floor && CC.floor.deadline && left === 0) CC.floor = { holder_agent_id: null, deadline: null };
      }
      ccRenderRoomMeta();
    });
  } else if (!need && CC.floorTimer) {
    POLL.clear("cc-floor"); CC.floorTimer = null;
  }
}
async function ccLoadMembers() {
  if (!CC.chatID) return;
  try {
    const r = await ccFetch("/chats/" + encodeURIComponent(CC.chatID) + "/members");
    CC.members = (r && r.items) || (Array.isArray(r) ? r : []);
    ccRenderRoomHead();
  } catch { /* 头部信息刷新失败不打扰 */ }
}
async function ccLoadFloor() {
  if (!CC.chatID) return;
  try {
    const f = await ccFetch("/chats/" + encodeURIComponent(CC.chatID) + "/floor");
    CC.floor = (f && typeof f === "object") ? f : null;
    ccRenderRoomHead();
  } catch { /* 忽略 */ }
}
/** WebSocket `presence`：更新某成员的在线状态（没见过的就补进来）。 */
function ccApplyPresence(d) {
  if (!d) return;
  const kind = d.kind || "user";
  const list = CC.members || (CC.members = []);
  const i = list.findIndex((m) => (m.kind || "user") === kind && String(m.id) === String(d.id));
  if (i >= 0) list[i] = Object.assign({}, list[i], { presence: d.presence, name: d.name || list[i].name });
  else list.push({ kind: kind, id: d.id, name: d.name, display: d.display, presence: d.presence });
  ccRenderRoomMeta();
}
/** WebSocket `floor`：更新当前发言权（只认当前会话）。 */
function ccApplyFloor(d) {
  if (!d) return;
  if (CC.chatID && String(d.chat_id) !== String(CC.chatID)) return;
  CC.floor = { holder_agent_id: d.holder_agent_id, deadline: d.deadline };
  ccRenderRoomMeta();
}

/** 消息页大小。⚠ 服务端 `/chats/{id}/messages` **没有 order 参数**：
 *  它按 **id 升序、从 `offset` 开始**给（返回 `{items,total,limit,offset}`）。 */
const CC_MSG_PAGE = 100;

async function ccLoadMsgs(toBottom) {
  if (!CC.chatID) return;
  const url = (off) => "/chats/" + encodeURIComponent(CC.chatID)
    + "/messages?limit=" + CC_MSG_PAGE + "&offset=" + off;
  try {
    // ⚠ 先问 total、再取**最后一页**。直接 `limit=100&offset=0` 只会拿到**最旧**的 100 条 ——
    //   会话一超过 100 条，新消息就永远看不到（实测 total=115 时面板停在 id 102）。
    let page = await ccFetch(url(0));
    const total = (page && page.total) || ((page && page.items) || []).length;
    CC.msgsTotal = total;
    if (total > CC_MSG_PAGE) page = await ccFetch(url(Math.max(0, total - CC_MSG_PAGE)));
    CC.msgs = (page && page.items) || [];
    ccRenderMsgs(toBottom !== false);
  } catch (e) {
    if (e.status === 401) { ccSetToken(""); ccShowAuth("登录已过期，请重新登录"); return; }
    ccNote("消息加载失败：" + e.message, true);
  }
}

/** cocraft 文件消息：content 是服务端保存路径；下载要 basename + token（见 docs/COCRAFT.md）。 */
function ccFileName(path) {
  const s = String(path || "");
  const parts = s.split(/[\\/]/);
  return parts[parts.length - 1] || s;
}
function ccFileUrl(chatID, content) {
  const name = ccFileName(content);
  return `/cocraft/chats/${encodeURIComponent(chatID)}/files/${encodeURIComponent(name)}`
    + `?token=${encodeURIComponent(CC.token || "")}`;
}
function ccRenderFileBody(m) {
  const name = ccFileName(m.content);
  const url = ccFileUrl(m.chat_id, m.content);
  const img = /\.(png|jpe?g|gif|webp|bmp|avif|svg)$/i.test(name);
  if (img) {
    // 图片：缩略图与文件名都**在面板内放大预览**（复用 #img-view，不跳新标签）
    return `<img class="cc-img" src="${esc(url)}" alt="${esc(name)}" loading="lazy"`
      + ` data-cc-img="${esc(url)}" title="点击放大预览">`
      + `<a class="cc-file cc-file-img" href="${esc(url)}" data-cc-img="${esc(url)}"`
      + ` title="点击放大预览">📎 ${esc(name)}</a>`;
  }
  return `<a class="cc-file" href="${esc(url)}" target="_blank" rel="noreferrer"`
    + ` title="下载 / 打开">📎 ${esc(name)}</a>`;
}

/** 某成员是不是 AI（agent）——按当前会话成员表判断。 */
function ccAgentById(id) {
  return (CC.members || []).find((x) => x.kind === "agent" && String(x.id) === String(id)) || null;
}
/** 消息发送者的显示名：**agent 用它自己的名字**，别再拿 agent id 去当用户查（会和用户 id 撞名）。 */
function ccSenderName(m) {
  const kind = (m && m.sender_kind) || "";
  const id = m && m.sender_id;
  const isAgent = kind === "agent" || (!kind && !!ccAgentById(id));
  if (isAgent) {
    const a = ccAgentById(id);
    return (a && (a.name || a.display)) || ("agent " + id);
  }
  return ccUserName(id);
}
function ccIsAgentMsg(m) {
  const kind = (m && m.sender_kind) || "";
  return kind === "agent" || (!kind && !!ccAgentById(m && m.sender_id));
}

/** cocraft 的时间是 **UTC**（ISO 带 `+00:00`）→ 面板要显示**本地时间**。
 *  ⚠ 以前直接 `replace("T"," ").slice(0,16)` → 比本地慢 8 小时（东八区）。
 *  没有时区信息的老数据按 UTC 解释；解析不出来就退回原样截断。 */
function ccLocalTime(iso) {
  const s = String(iso || "").trim();
  if (!s) return "";
  const hasTz = /[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s);
  const d = new Date(hasTz ? s : s + "Z");
  if (isNaN(d.getTime())) return s.replace("T", " ").slice(0, 16);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function ccRenderMsgs(toBottom) {
  const box = ccEl("cc-msgs");
  if (!box) return;
  if (!CC.msgs.length) { box.innerHTML = `<div class="cc-empty">还没有消息</div>`; ccDisRender(); ccActRender(); return; }
  box.innerHTML = CC.msgs.map((m) => {
    const isAgent = ccIsAgentMsg(m);
    const mine = !isAgent && CC.me && String(m.sender_id) === String(CC.me.id);
    const who = (isAgent ? "🤖 " : "") + (mine ? "我" : ccSenderName(m));
    const t = ccLocalTime(m.created_at);
    const body = m.message_type === "file" ? ccRenderFileBody(m) : esc(m.content || "");
    return `<div class="cc-m${mine ? " me" : ""}${isAgent ? " agent" : ""}"><span class="cc-mt">${esc(who)}${t ? " · " + esc(t) : ""}</span>`
      + `<div class="cc-mb">${body}</div></div>`;
  }).join("");
  if (toBottom !== false) ccScrollBottom(box);
  ccPermRender();                 // ★ 顺手刷新「权限申请」小弹窗（输入框上方那个）
  ccDisRender();                  // ★主动编排：派活汇总卡（在活动卡**之前**）
  ccActRender();                  // ★C1：活动卡永远贴在消息流末尾（innerHTML 重建后要补回去）
}

/* ---- ★主动编排（方向 2）：角色预设 + 派活台 ----
   并行是**天然**的：每个 AI 成员各自一条 WS + 线程 + AcpService，一条消息 @ 三个就是三家同时干。
   这里补的是编排层：① 角色（只注入给本人，不污染别人的上下文）② 多选派活 ③ 一张"谁回了"的汇总卡。 */

/** 角色中文名（拿不到就回 id / 空）。 */
function ccRoleLabel(role) {
  const r = String(role || "");
  return (CC.roles && CC.roles[r] && CC.roles[r].label) || r;
}

/** 角色下拉的选项（`cur` = 当前值）。 */
function ccRoleOptionsHtml(cur) {
  const c = String(cur || "");
  let html = `<option value=""${c ? "" : " selected"}>（无角色）</option>`;
  Object.keys(CC.roles || {}).forEach((k) => {
    html += `<option value="${esc(k)}"${k === c ? " selected" : ""}>${esc(ccRoleLabel(k))}</option>`;
  });
  return html;
}

/** 把角色表填进「建成员」那个下拉（顺带重画成员行里的角色下拉）。 */
function ccFillRoles() {
  const sel = ccEl("cc-ai-role");
  if (sel) sel.innerHTML = ccRoleOptionsHtml(sel.value || "");
  ccRenderAI();
}

/** 给某个成员改角色（服务端只影响它自己的提示词）。 */
async function ccAiRole(id, role) {
  try {
    const r = await ccAgentFetch("/update", { method: "POST", body: { id: id, role: role } });
    CC.ai = (r && r.agents) || CC.ai;
    ccRenderAI();
    ccDisRenderMembers();
    ccNote(role ? `已设角色：${ccRoleLabel(role)}` : "已清掉角色");
  } catch (e) {
    ccNote("改角色失败：" + (e && e.message ? e.message : e), true);
  }
}

/* ---- 派活台 ---- */

function ccDisOpen(open) {
  const p = ccEl("cc-dis");
  if (!p) return;
  CC.disOpen = !!open;
  p.hidden = !open;
  if (open) {
    ccDisRenderMembers();
    const t = ccEl("cc-dis-task");
    if (t) t.focus();
  }
}

function ccDisRenderMembers() {
  const box = ccEl("cc-dis-members");
  if (!box) return;
  const list = CC.ai || [];
  if (!list.length) {
    box.innerHTML = `<div class="cc-dis-empty">还没有 AI 成员 —— ☰ →「AI 成员」里先建一个并加群。</div>`;
    return;
  }
  box.innerHTML = list.map((a) => {
    const id = String(a.id);
    const on = CC.disSel.has(id);
    const inChat = ccIsInChat(a);
    return `<button type="button" class="cc-dis-chip${on ? " on" : ""}${inChat ? "" : " off"}"
      data-dis="${esc(id)}" title="${esc(a.name)} · 大脑 ${esc(a.engine)}${inChat ? "" : " · 还没加进本群"}">`
      + `${on ? "✓ " : ""}${esc(a.name)}`
      + (a.roleLabel ? `<i>${esc(a.roleLabel)}</i>` : "") + `</button>`;
  }).join("");
}

/** 派活：把选中的成员拼成一条 `@a @b 任务` 发进群（服务端按 mentions 给每人推一条 → 并行）。 */
function ccDispatchGo() {
  if (!CC.chatID) { ccDisNote("先选一个会话", true); return; }
  const ms = Array.from(CC.disSel)
    .map((id) => (CC.ai || []).find((a) => String(a.id) === String(id)))
    .filter(Boolean);
  if (!ms.length) { ccDisNote("先选至少一个 AI 成员", true); return; }
  const task = String((ccEl("cc-dis-task") || {}).value || "").trim();
  if (!task) { ccDisNote("写一句要他们做什么", true); return; }
  const text = ms.map((a) => "@" + a.name).join(" ") + " " + task;
  if (!ccSend(text)) return;
  CC.dis = {
    chat: String(CC.chatID), task: task, ts: Date.now(),
    members: ms.map((a) => ({ id: String(a.id), name: a.name, role: a.role || "",
                              roleLabel: a.roleLabel || "", status: "wait" })),
  };
  CC.pickId = null;                      // 新派活：清掉上一轮「推荐 X」的高亮
  const t = ccEl("cc-dis-task");
  if (t) t.value = "";
  ccDisNote("");
  ccDisOpen(false);
  ccDisRender();
  ccNote(`已派活给 ${ms.length} 个成员（各自并行，结果看下面的「派活」卡）`);
}

function ccDisNote(text, isErr) {
  const n = ccEl("cc-dis-note");
  if (!n) return;
  n.textContent = text || "";
  n.classList.toggle("err", !!isErr);
}

/** 派活汇总卡：谁已回复 / 谁还在跑。
 *  状态**从群里已有的消息里推**（不额外问服务端）：派活之后这个成员在本群说过话 = 已回复。 */
function ccDisRender() {
  const box = ccEl("cc-msgs");
  if (!box) return;
  let el = ccEl("cc-dis-card");
  const d = CC.dis;
  if (!d || !d.members || String(d.chat) !== String(CC.chatID)) {
    if (el) el.remove();
    return;
  }
  d.members.forEach((m) => {
    if (m.status === "done") return;
    const t0 = Number(d.ts) - 2000;                 // 给 2s 容差（本地时钟 / 服务端时间戳）
    const hit = (CC.msgs || []).some((x) => String(x.sender_id) === String(m.id)
      && ccEpoch(x.created_at) >= t0);
    if (hit) m.status = "done";
  });
  const done = d.members.filter((m) => m.status === "done").length;
  if (!el) { el = document.createElement("div"); el.id = "cc-dis-card"; el.className = "cc-dis-card"; }
  el.innerHTML = `<div class="cc-dis-cap">`
    + `<i class="cc-dis-dot${done < d.members.length ? " busy" : ""}"></i>`
    + `派活 · ${done}/${d.members.length} 已回复`
    + (done ? `<button type="button" class="cc-dis-cmp" data-dis-cmp="1" title="并排看他们的回答">对比</button>` : "")
    + `<button type="button" class="cc-dis-x" data-dis-hide="1" title="收起这张卡">×</button></div>`
    + d.members.map((m) => `<div class="cc-dis-row ${m.status === "done" ? "done" : "wait"}">`
      + `<span class="cc-dis-ico">${m.status === "done" ? "✓" : '<i class="cc-act-spin"></i>'}</span>`
      + (m.roleLabel ? `<b class="cc-dis-role">${esc(m.roleLabel)}</b>` : "")
      + `<span class="cc-dis-name">${esc(m.name)}</span>`
      + `<span class="cc-dis-st">${m.status === "done" ? "已回复" : "等待中"}</span></div>`).join("");
  if (el.parentNode !== box) box.appendChild(el);
}

/* ---- ★多方案对比：把同一次派活里每个人的回答**并排**摆出来（2 人时可逐行看差异） ---- */

/** 这次派活里每个成员的回复（按消息时间拼起来；没有就空串）。
 *  ⚠ 仲裁开始**之后** reviewer 说的话算「仲裁」，不算方案（否则会污染它那一列）。 */
function ccCmpRows() {
  const d = CC.dis;
  if (!d) return [];
  const t0 = Number(d.ts) - 2000;
  const arb = (CC.arb && String(CC.arb.chat) === String(CC.chatID)) ? CC.arb : null;
  return (d.members || []).map((m) => {
    const hit = (CC.msgs || []).filter((x) => String(x.sender_id) === String(m.id)
      && ccEpoch(x.created_at) >= t0
      && !(arb && String(arb.reviewer.id) === String(m.id) && ccEpoch(x.created_at) >= arb.ts));
    return { id: m.id, name: m.name, role: m.role || "", roleLabel: m.roleLabel || "",
             count: hit.length, text: hit.map((x) => String(x.content || "")).join("\n\n") };
  });
}

/* ---- ★结果仲裁：让 **reviewer 角色**的成员给这批方案拍板（打分 / 挑错 / 推荐） ---- */

/** 本群里的 reviewer 角色成员（没有就空）。 */
function ccArbReviewers() {
  return (CC.ai || []).filter((a) => String(a.role) === "reviewer" && ccIsInChat(a));
}

/** 仲裁区里 reviewer 的回复（纯文本）。 */
function ccArbReply() {
  const a = CC.arb;
  if (!a || String(a.chat) !== String(CC.chatID)) return "";
  return (CC.msgs || [])
    .filter((x) => String(x.sender_id) === String(a.reviewer.id) && ccEpoch(x.created_at) >= a.ts)
    .map((x) => String(x.content || "")).join("\n\n").trim();
}

/** 从仲裁文本里**尽量**抠出「推荐 X」（抠不到就不显示，别编）。 */
function ccArbPick(text) {
  const m = String(text || "").match(/推荐\s*[:：]?\s*[*_`]*([^\n，,。;；]{1,24})/);
  return m ? m[1].replace(/[*_`]/g, "").trim() : "";
}

/** 「推荐 X」落在**哪一列**：先按成员名，再按正文里提到这个名字（方案名常写在回答里）。
 *  返回列下标；对不上就 -1（**不硬猜**，前端会明说对不上）。 */
function ccArbPickTarget(pick) {
  const key = String(pick || "").toLowerCase().replace(/\s+/g, "");
  if (!key) return -1;
  const rows = ccCmpRows();
  const norm = (s) => String(s || "").toLowerCase().replace(/\s+/g, "");
  let i = rows.findIndex((r) => norm(r.name) === key);
  if (i < 0) i = rows.findIndex((r) => norm(r.name) && (norm(r.name).includes(key) || key.includes(norm(r.name))));
  if (i < 0) i = rows.findIndex((r) => norm(r.text).includes(key));
  return i;
}

/** 点「推荐：X」→ 跳到那一列（高亮）。对不上就明说，不假装。 */
function ccArbJump() {
  const pick = ccArbPick(ccArbReply());
  if (!pick) return;
  const rows = ccCmpRows();
  const idx = ccArbPickTarget(pick);
  const cols = document.querySelectorAll("#cmp-cols .cmp-col");
  if (idx < 0 || !cols[idx]) {
    setBanner(`结论里的「${pick}」对不上任何一列（成员：${rows.map((r) => r.name).join("、") || "无"}）`);
    return;
  }
  CC.pickId = String(rows[idx].id);
  Array.prototype.forEach.call(cols, (c) => c.classList.remove("pick"));
  cols[idx].classList.add("pick");
  try { cols[idx].scrollIntoView({ block: "nearest", inline: "center", behavior: "smooth" }); }
  catch (e) { cols[idx].scrollIntoView(); }
  setBanner(`已跳到「${rows[idx].name}」那一列`);
}

/* ★每次「派活 + 仲裁」的结论记一份历史（本机 localStorage，最多 50 条）。
   现在只给「信任面板（方向 3）」攒素材；等那边做的时候直接读这个键。 */
const CC_DEC_KEY = "cocraft.cc.decisions";

function ccDecisionRemember(chat, task, pick, reviewer, target) {
  try {
    const v = JSON.parse(localStorage.getItem(CC_DEC_KEY) || "[]");
    const arr = Array.isArray(v) ? v : [];
    arr.unshift({ chat: String(chat || ""), task: String(task || "").slice(0, 200),
                  pick: String(pick || ""), reviewer: String(reviewer || ""),
                  target: String(target || ""), ts: Date.now() });
    localStorage.setItem(CC_DEC_KEY, JSON.stringify(arr.slice(0, 50)));
  } catch (e) { /* 隐私模式 / 配额满：历史不是功能前置，安静跳过 */ }
}

function ccDecisionHistory() {
  try {
    const v = JSON.parse(localStorage.getItem(CC_DEC_KEY) || "[]");
    return Array.isArray(v) ? v : [];
  } catch (e) { return []; }
}

function ccArbAsk() {
  const d = CC.dis;
  if (!d) { setBanner("先派活，再让 reviewer 仲裁"); return; }
  const list = ccArbReviewers();
  if (!list.length) {
    setBanner("本群没有「审查者」角色的 AI 成员 —— 在 ☰ →「AI 成员」里把某个成员的角色设成「审查者」");
    return;
  }
  const sel = $("cmp-arb-sel");
  const want = sel ? String(sel.value || "") : "";
  const who = list.find((a) => String(a.id) === want) || list[0];
  const names = (d.members || []).map((m) => m.name).filter(Boolean);
  const text = "@" + who.name + " 【仲裁】请评审上面这次派活的方案（" + names.join(" / ") + "）。\n"
    + "要求：① 每个方案各给 1-2 句评价，指出**具体问题**（别写场面话）；② 结尾单独一行用"
    + "「结论：推荐 <方案名>」收口，并说一句为什么。\n"
    + "你只做评审、**不要改代码**；拿不准的地方直接说不确定。";
  if (!ccSend(text)) return;
  CC.pickId = null;                      // 新一轮仲裁：上一轮的高亮不该留着
  CC.arb = { chat: String(CC.chatID), ts: Date.now(), task: d.task,
             reviewer: { id: String(who.id), name: who.name } };
  ccCmpRender();
  ccNote("已请 " + who.name + " 仲裁（结果会出现在对比弹窗的「仲裁」区）");
}

function ccArbRender() {
  const box = $("cmp-arb");
  if (!box) return;
  const d = CC.dis;
  if (!d) { box.innerHTML = ""; box.hidden = true; return; }
  const list = ccArbReviewers();
  const a = (CC.arb && String(CC.arb.chat) === String(CC.chatID)) ? CC.arb : null;
  const reply = ccArbReply();
  const pick = ccArbPick(reply);
  let head = `<div class="cmp-arb-h"><b>仲裁</b>`;
  if (a) {
    head += `<span class="cmp-arb-who">${esc(a.reviewer.name)}</span>`;
    head += `<span class="cmp-arb-st">${reply ? "已出结论" : "等待中…"}</span>`;
  }
  head += `<span class="cmp-arb-sp"></span>`;
  if (list.length) {
    if (list.length > 1) {
      head += `<select id="cmp-arb-sel" class="cmp-arb-sel">`
        + list.map((x) => `<option value="${esc(x.id)}"${a && String(a.reviewer.id) === String(x.id) ? " selected" : ""}>${esc(x.name)}</option>`).join("")
        + `</select>`;
    }
    head += `<button type="button" class="cmp-arb-go" data-arb-go="1">${a ? "再问一次" : "请 " + (list.length === 1 ? esc(list[0].name) : "审查者") + " 仲裁"}</button>`;
  } else {
    head += `<span class="muted">本群没有「审查者」角色的成员</span>`;
  }
  head += `</div>`;
  const body = reply
    ? `<div class="cmp-arb-body">`
      + (pick ? `<button type="button" class="cmp-arb-rec" data-arb-pick="${esc(pick)}"
            title="跳到这一列（对照着看）">推荐：<b>${esc(pick)}</b><span class="cmp-arb-jump">看这一列 →</span></button>` : "")
      + renderText(reply) + `</div>`
    : (a ? `<div class="muted" style="padding:4px 0">还在评……（点「刷新」看最新）</div>` : "");
  box.innerHTML = head + body;
  box.hidden = false;
  // ★结论一出来就记一份历史（同一份结论只记一次；target 对不上就留空，不编）
  if (pick && a && a.pickSeen !== pick) {
    a.pickSeen = pick;
    const idx = ccArbPickTarget(pick);
    const rows = ccCmpRows();
    ccDecisionRemember(d.chat, d.task, pick, a.reviewer.name, idx >= 0 ? rows[idx].name : "");
  }
}

function ccCmpOpen() {
  const box = $("cmp");
  if (!box) return;
  box.hidden = false;
  ccCmpRender();
}

function ccCmpClose() { const b = $("cmp"); if (b) b.hidden = true; }

function ccCmpRender() {
  const cols = $("cmp-cols");
  const task = $("cmp-task");
  const note = $("cmp-note");
  const diffBtn = $("cmp-diff-btn");
  const diffBox = $("cmp-diff");
  if (!cols) return;
  const d = CC.dis;
  if (diffBox) { diffBox.hidden = true; diffBox.innerHTML = ""; }
  if (!d) {
    if (task) task.textContent = "还没有派活记录 —— 先在群聊里「派活」。";
    cols.innerHTML = "";
    if (note) note.textContent = "";
    if (diffBtn) diffBtn.hidden = true;
    ccArbRender();
    return;
  }
  if (task) task.textContent = "任务：" + d.task;
  const rows = ccCmpRows();
  const got = rows.filter((r) => r.text.trim());
  if (note) {
    note.textContent = `${got.length}/${rows.length} 个成员已回复`
      + (got.length === 2 ? " · 可以点「逐行差异」看两边的差别" : "");
  }
  if (diffBtn) diffBtn.hidden = got.length !== 2;
  if (!got.length) {
    cols.innerHTML = `<div class="muted" style="padding:10px">还没有人回复 —— 等等再看（点「刷新」）。</div>`;
    ccArbRender();
    return;
  }
  cols.innerHTML = rows.map((r) => {
    const empty = !r.text.trim();
    return `<div class="cmp-col${empty ? " empty" : ""}">`
      + `<h4>${r.roleLabel ? `<i class="cmp-role">${esc(r.roleLabel)}</i>` : ""}`
      + `${esc(r.name)}<span>${empty ? "等待中" : (r.count > 1 ? r.count + " 条" : "已回复")}</span></h4>`
      + `<div class="cmp-body">${empty ? `<span class="muted">（还没回复）</span>` : renderText(r.text)}</div>`
      + `</div>`;
  }).join("");
  // ★点过「推荐 X」之后重画也要保住高亮（列顺序不变，按 id 找回那一列）
  if (CC.pickId) {
    const at = rows.findIndex((r) => String(r.id) === String(CC.pickId));
    const pickEl = at >= 0 ? cols.querySelectorAll(".cmp-col")[at] : null;
    if (pickEl) pickEl.classList.add("pick");
  }
  ccArbRender();
  ccCmpLoadChanges(rows);          // ★每列补一块「文件改动」（异步，失败就算了）
}

/** 每列补「派活之前 → 现在」改了哪些文件（**文件级**；接方向 1 的轮快照，不占磁盘）。 */
async function ccCmpLoadChanges(rows) {
  const cols = document.querySelectorAll("#cmp-cols .cmp-col");
  for (let i = 0; i < rows.length; i++) {
    const col = cols[i];
    if (!col) continue;
    const old = col.querySelector(".cmp-chg");
    if (old) old.remove();
    try {
      const r = await ccAgentFetch("/changes?chat=" + encodeURIComponent((CC.dis && CC.dis.chat) || CC.chatID)
        + "&aid=" + encodeURIComponent(rows[i].id));
      if (!r || r.ok === false || !r.files) continue;
      col.appendChild(ccChgNode(r));
    } catch (e) { /* 没快照 / 拿不到就算了，不要打扰用户 */ }
  }
}

function ccChgNode(r) {
  const lines = []
    .concat((r.changed || []).map((x) => ["改", x.path]))
    .concat((r.added || []).map((x) => ["增", x.path]))
    .concat((r.deleted || []).map((x) => ["删", x.path]))
    .concat((r.renamed || []).map((x) => ["名", (x.from || "") + " → " + (x.to || "")]));
  const d = document.createElement("details");
  d.className = "cmp-chg";
  d.innerHTML = `<summary>文件改动 · ${r.files} 个</summary>`
    + lines.slice(0, 40).map(([k, p]) => `<div class="cmp-chg-row"><i>${esc(k)}</i>`
      + `<span title="${esc(p)}">${esc(p)}</span></div>`).join("")
    + (lines.length > 40 ? `<div class="muted">…还有 ${lines.length - 40} 个</div>` : "");
  return d;
}

/** 两个回答的**逐行差异**（LCS；太长就不算，免得卡）。 */
function ccDiffLines(a, b) {
  const A = String(a || "").split("\n");
  const B = String(b || "").split("\n");
  const n = A.length, m = B.length;
  if (n * m > 400000) return null;                       // 太长：别把界面算死
  const dp = [];
  for (let i = 0; i <= n; i++) dp.push(new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (A[i] === B[j]) { out.push(" " + A[i]); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push("-" + A[i]); i++; }
    else { out.push("+" + B[j]); j++; }
  }
  while (i < n) out.push("-" + A[i++]);
  while (j < m) out.push("+" + B[j++]);
  return out;
}

function ccCmpToggleDiff() {
  const box = $("cmp-diff");
  const btn = $("cmp-diff-btn");
  if (!box) return;
  if (!box.hidden) { box.hidden = true; if (btn) btn.textContent = "逐行差异"; return; }
  const got = ccCmpRows().filter((r) => r.text.trim());
  if (got.length !== 2) { setBanner("需要正好 2 个成员回复才能看逐行差异"); return; }
  const lines = ccDiffLines(got[0].text, got[1].text);
  if (!lines) { setBanner("两边回复太长，算差异会卡 —— 直接并排看吧"); return; }
  const add = lines.filter((l) => l.startsWith("+")).length;
  const del = lines.filter((l) => l.startsWith("-")).length;
  box.innerHTML = `<div class="cmp-diff-h">逐行差异：<b>${esc(got[0].name)}</b> → <b>${esc(got[1].name)}</b>`
    + `<span class="muted">（+${add} / −${del} 行）</span></div>`
    + renderDiff(lines.join("\n"));
  box.hidden = false;
  if (btn) btn.textContent = "收起差异";
}

/* ---- ★群聊权限申请：**输入框上方的小弹窗** ----
   桥在群里发的「🔐 权限申请…」那条消息驱动这个弹窗；点按钮**仍然在群里发同一句**
   `@<agent> 可以 / 始终可以 / 不行`（行为与手打完全一致，群里留有审计痕迹）。
   它只是把「手打 @名字 + 记得要说可以」变成一次点击。 */

/** UTC ISO → epoch（没带时区就按 UTC 算；与 ccLocalTime 同一套规则）。 */
function ccEpoch(iso) {
  const s = String(iso || "").trim();
  if (!s) return NaN;
  return Date.parse(/[zZ]$/.test(s) || /[+-]\d{2}:?\d{2}$/.test(s) ? s : s + "Z");
}

/** 从申请文案里抠出「动作 / 类型 / 涉及路径 / 等多少秒 / 命令 / 是否破坏性」。 */
function ccPermParse(text) {
  const s = String(text || "");
  const pick = (re) => String((s.match(re) || [])[1] || "").trim();
  return {
    title: pick(/权限申请\**：\s*([^\n]+)/) || "请求权限",
    kind: pick(/类型：\s*([^\n]+)/),
    paths: pick(/涉及：\s*([^\n]+)/),
    cmd: pick(/\*\*要执行的命令\*\*：`([^`]+)`/),
    destructive: /破坏性操作/.test(s),
    secs: Number(pick(/（(\d+)\s*秒内/) || 90) || 90,
  };
}

/* ★ 只认桥发的**真正申请文案**：固定带 `🔐 **权限申请**：`。
   ⚠ 以前用 `c.indexOf("权限申请") >= 0` —— 太宽：AI 成员**解释自己规则**时随口提到
   "发权限申请 / 破坏性操作"也会被当成一次待批申请 → 误弹"请求（破坏性）权限"卡片
   （2026-10-13：用户问 yiyi"为什么申请破坏性操作"，它的回答本身就把弹窗顶了出来）。 */
const CC_PERM_ASK_RE = /🔐\s*\*\*权限申请\*\*：/;

/** 当前待批准的申请：最近一条「🔐 权限申请」；若其后已有「已允许/已拒绝/超时/已确认」→ 视为已结算。 */
function ccPermPending() {
  const msgs = CC.msgs || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    const c = String((m && m.content) || "");
    if (m.sender_kind === "agent" && /已允许|已拒绝|权限申请超时|已确认，开始执行/.test(c)) return null;
    if (m.sender_kind === "agent" && CC_PERM_ASK_RE.test(c) && m.id != null) return m;
  }
  return null;
}

/** 申请来自哪个 AI 成员（要拿它的 **name** 去 @）。 */
function ccPermAgent(m) {
  const hit = (CC.ai || []).find((a) => String(a.id) === String(m && m.sender_id));
  return { name: (hit && hit.name) || "",
           label: (hit && (hit.display || hit.name)) || ccSenderName(m || {}) };
}

function ccPermRender() {
  const box = ccEl("cc-perm");
  if (!box) return;
  const m = ccPermPending();
  if (!m || (CC.permDone || {})[String(m.id)]) {
    if (!box.hidden) { box.hidden = true; box.innerHTML = ""; }
    ccPermTimer(false);
    NOTIFY.clear("cc-perm");           // ★没有待批准的了 → 撤下提醒
    syncTitle();                       // ★B3：待批准没了 → 标题栏计数跟着减
    return;
  }
  const info = ccPermParse(m.content);
  const a = ccPermAgent(m);
  const left = Math.max(0, Math.round(info.secs - (Date.now() - ccEpoch(m.created_at)) / 1000));
  if (!(left > 0)) {                    // 到点（或时间戳异常）→ 桥那边会自行拒绝，这里也收起来
    box.hidden = true; box.innerHTML = ""; ccPermTimer(false);
    NOTIFY.clear("cc-perm");
    syncTitle();
    return;
  }
  box.hidden = false;
  ccPermStrongAlert(m, info, a.label);   // ★B3：面板不在前台 → 托盘气泡 + 闪任务栏（同一申请只提醒一次）
  const cmdLine = info.cmd
    ? `<div class="cc-perm-cmd" title="${esc(info.cmd)}">${esc(info.cmd)}</div>` : "";
  box.innerHTML = `<div class="cc-perm-h${info.destructive ? " danger" : ""}">`
    + `${info.destructive ? "⚠️" : "🔐"} <b>${esc(a.label)}</b> `
    + `${info.destructive ? "请求执行破坏性操作" : "请求权限"}`
    + `<span class="cc-perm-t">${left}s</span></div>`
    + `<div class="cc-perm-b">${esc(info.title)}${info.kind ? " · " + esc(info.kind) : ""}`
    + (info.paths ? `<span class="cc-perm-p" title="${esc(info.paths)}">${esc(info.paths)}</span>` : "")
    + `</div>` + cmdLine
    + `<div class="cc-perm-a">`
    + (info.destructive
        ? `<button type="button" class="danger" data-perm="once">确认执行</button>`
          + `<button type="button" data-perm="reject">拒绝</button>`
        : `<button type="button" class="primary" data-perm="once">允许</button>`
          + `<button type="button" data-perm="always">始终允许</button>`
          + `<button type="button" data-perm="reject">拒绝</button>`)
    + `<button type="button" class="cc-perm-x" data-perm="hide" title="先不管（到点自动拒绝）">×</button>`
    + `</div>`;
  ccPermTimer(true);
  // 抽屉没开着 → 用顶部横幅提醒（否则根本看不到这个弹窗）。
  // ★A3：走通知聚合器（优先级 100 = 最高），并且**可点**（点一下直接开抽屉）。
  if (!cocraftIsOpen()) {
    NOTIFY.set("cc-perm", {
      priority: 100,
      text: `群聊：${a.label} 有权限申请待批准 —— 点这里打开，一键「允许」`,
      onClick: () => { NOTIFY.clear("cc-perm"); setCocraft(true); },
    });
  } else {
    NOTIFY.clear("cc-perm");
  }
  syncTitle();                          // ★B3：出现待批准 → 标题栏计数 +1
}

let ccPermT = null;
function ccPermTimer(on) {
  if (on && !ccPermT) ccPermT = POLL.interval("cc-perm", 1000, () => ccPermRender());   // 每秒刷倒计时，到点自动收
  if (!on && ccPermT) { POLL.clear("cc-perm"); ccPermT = null; }
}

/** 点按钮：在群里发 `@<agent> 可以 / 始终可以 / 不行`（跟手打等价）。 */
function ccPermAnswer(kind) {
  const m = ccPermPending();
  if (!m) return;
  if (kind === "hide") {
    (CC.permDone || (CC.permDone = {}))[String(m.id)] = true;
    ccPermRender();
    return;
  }
  const a = ccPermAgent(m);
  if (!a.name) { ccNote("拿不到这个 AI 成员的名字，没法回批准", true); return; }
  const info = ccPermParse(m.content);
  // ★ 破坏性操作：只有「确认」才算（跟桥那边的判据一致）；「可以」不算
  const word = kind === "reject" ? "不行"
    : (info.destructive ? "确认" : (kind === "always" ? "始终可以" : "可以"));
  if (!ccSend("@" + a.name + " " + word)) return;      // 失败时 ccSend 自己提示过
  (CC.permDone || (CC.permDone = {}))[String(m.id)] = true;
  ccNote("已发送：" + (kind === "reject" ? "拒绝"
    : (info.destructive ? "确认执行" : (kind === "always" ? "始终允许" : "允许"))));
  ccPermRender();
}

function initCcPerm() {
  const box = ccEl("cc-perm");
  if (!box) return;
  box.addEventListener("click", (ev) => {
    const b = ev.target.closest("[data-perm]");
    if (b) ccPermAnswer(b.dataset.perm);
  });
}

/** 滚到底部。
 *  ⚠ 不能只写 `box.scrollTop = box.scrollHeight`：`.cc-m` 曾带 `content-visibility:auto`，
 *    刚 `innerHTML` 完时屏外那些消息还没布局 → `scrollHeight` 被低估
 *    → scrollTop 被设成一个很小的值（常常是 0）→ 看起来「一有新消息就跳到最上面」。
 *  修法：去掉 `content-visibility`（根因）+ 多次补设 scrollTop。 */
function ccScrollBottom(box) {
  const go = () => { box.scrollTop = box.scrollHeight; };
  go();
  setTimeout(go, 0);      // 布局稳定后补一次（字体/图片到位后 scrollHeight 会变大）
  setTimeout(go, 80);
}

/** WS 推来的帧**就是消息本体** → 直接追加（已存在就忽略，防重复）。
 *  只有"补历史"才交给 `ccLoadMsgs()`（它会取**最后一页**）。 */
function ccPushMsg(m) {
  if (!m || m.id == null) return;
  if ((CC.msgs || []).some((x) => String(x.id) === String(m.id))) return;
  // ★C1：群里来了新消息 → 很可能马上就有成员开始干活 → 让活动卡**立刻**拉一次
  //   （否则空闲节奏是 6s，「正在执行」会迟到好几秒才出现）
  CC.actAt = 0;
  // 帧里理论上带 created_at；万一没带就用「现在」（推送是实时的，正好对）。
  if (!m.created_at) m.created_at = new Date().toISOString();
  const box = ccEl("cc-msgs");
  // 本来就在底部 → 跟着到底；正在往上翻旧消息 → 不动它
  const atBottom = !box || (box.scrollHeight - box.scrollTop - box.clientHeight < 80);
  CC.msgs.push(m);
  CC.msgsTotal = (CC.msgsTotal || 0) + 1;
  if (CC.msgs.length > 400) CC.msgs.splice(0, CC.msgs.length - 400);
  ccRenderMsgs(atBottom);
}

/** 发文件：POST multipart /chats/{id}/files，字段名 `file`。
 *  服务端会**自动写一条 message_type=file 的消息并 WS 广播**，所以这里只上传、不额外发 WS。
 *  ⚠ 绝不能手动设 Content-Type（要浏览器带 multipart boundary）。 */
async function ccSendFile(file) {
  if (!file) return;
  if (!CC.chatID) { ccNote("先选一个会话", true); return; }
  if (!CC.token) { ccNote("未登录", true); return; }
  const btn = ccEl("cc-attach");
  if (btn) btn.disabled = true;
  ccNote(`上传中：${file.name}（${Math.max(1, Math.round(file.size / 1024))} KB）…`);
  try {
    const form = new FormData();
    form.append("file", file, file.name);
    const res = await fetch(`/cocraft/chats/${encodeURIComponent(CC.chatID)}/files`, {
      method: "POST",
      headers: { Authorization: "Bearer " + CC.token },   // 只带鉴权，别带 Content-Type
      body: form,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!res.ok) {
      const d = (data && (data.detail || data.error)) || res.statusText || ("HTTP " + res.status);
      throw new Error(typeof d === "string" ? d : JSON.stringify(d));
    }
    const sent = ccFileName((data && data.content) || file.name);
    await ccLoadMsgs(true);
    // ★ 上传接口**不能带说明/@**（只收 file 字段）→ 若输入框里有文字，**紧接着把这段文字也发出去**。
    //   这样「@yiyi 看下这张图」一步到位：图片先进群，@ 随后发出；agent 被 @ 时会带上刚发的图片。
    const inp = ccEl("cc-input");
    const cap = inp ? String(inp.value || "").trim() : "";
    if (cap) {
      if (ccSend(cap)) {
        inp.value = "";
        ccNote(`已发送文件 + 说明：${sent}`);
      } else {
        ccNote(`已发送文件：${sent}（说明没发出去：WebSocket 未就绪，请手动再发一次）`, true);
      }
    } else {
      ccNote(`已发送文件：${sent}`);
    }
  } catch (e) {
    ccNote("上传失败：" + (e && e.message ? e.message : e), true);
  } finally {
    if (btn) btn.disabled = false;
    const fi = ccEl("cc-file");
    if (fi) fi.value = "";
  }
}

/* ---- 发消息（只能走 WS） ---- */
function ccSend(text) {
  const t = String(text || "").trim();
  if (!t || !CC.chatID) return false;
  if (!CC.wsock || CC.wsock.readyState !== 1) { ccNote("还没连上（WebSocket 未就绪），稍等再发", true); return false; }
  // ★ 解析 `@名字` → `mentions`：服务端**靠 mentions 数组**才知道该推 `mention` 给哪个 agent
  //   （只写 "@xxx" 文本、不带 mentions，服务端不会推 —— 实测）。@ 后面的名字按「字母/数字/_/-」取。
  const mentions = [];
  const re = /@([A-Za-z0-9_\-]+)/g;
  let m;
  while ((m = re.exec(t))) { if (mentions.indexOf(m[1]) < 0) mentions.push(m[1]); }
  try {
    const frame = { type: "message", chat_id: Number(CC.chatID) || CC.chatID, content: t };
    if (mentions.length) frame.mentions = mentions;
    CC.wsock.send(JSON.stringify(frame));
    ccNote("");
    return true;
  } catch (e) { ccNote("发送失败：" + e.message, true); return false; }
}

/* ---- 会话操作菜单（☰）：邀请 / 移出 ---- */
function ccMenuOpen(open) {
  const pop = ccEl("cc-menu-pop");
  if (!pop) return;
  pop.hidden = !open;
  if (open) { ccFillAiEngines(); ccLoadAI(); }   // 打开菜单时刷一次「AI 成员」
}

/* ---- AI 成员（把面板里的 ACP agent 接进群聊；后端 backend/cocraft_agents.py 跑桥）----
   ⚠ 管理接口在**本地** `/ccagent/*`（不是 /cocraft 代理），要带**用户令牌**。 */
async function ccAgentFetch(path, opts) {
  opts = opts || {};
  const headers = { "Content-Type": "application/json" };
  if (CC.token) headers["Authorization"] = "Bearer " + CC.token;
  const res = await fetch("/ccagent" + path, {
    method: opts.method || "GET", headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined, cache: "no-store",
  });
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    const d = (data && (data.error || data.detail)) || res.statusText || ("HTTP " + res.status);
    const err = new Error(typeof d === "string" ? d : JSON.stringify(d));
    err.status = res.status;
    throw err;
  }
  return data;
}

/** 把可用的 ACP agent 填进「用哪个大脑」下拉（只在空的时候填）。 */
function ccFillAiEngines() {
  const sel = ccEl("cc-ai-engine");
  if (!sel) return;
  if (sel.dataset.filled === "1" && sel.options.length) return;
  const run = () => {
    const list = (ACP.list || []).filter((a) => a.available);
    sel.innerHTML = list.map((a) =>
      `<option value="${esc(a.id)}">${esc(a.label || a.id)}</option>`).join("")
      || `<option value="">（没有可用的 ACP agent）</option>`;
    sel.dataset.filled = "1";
  };
  if ((ACP.list || []).length) run();
  else loadAcpAgents().then(run).catch(run);
}

const ccIsInChat = (a) => String(CC.chatID) && (a.chats || []).some((c) => String(c) === String(CC.chatID));

/* ---- ★主动编排：一键建组 ----
   后端 `/ccagent/team`：按预设建几个 agent（各带角色 + 大脑）→ 自动进群 → 打开应答。
   名字必须能被 `@` 解析（`[A-Za-z0-9_-]`），重名后端会自动加 `-2`。 */

function ccTeamOpen(open) {
  const box = ccEl("cc-team");
  if (!box) return;
  box.hidden = !open;
  if (open) { ccTeamFillPreset(); ccTeamRender(); }
}

function ccTeamPreset() {
  const sel = ccEl("cc-team-preset");
  if (sel && sel.options.length) return sel.value || "dev";
  const first = (CC.teamPresets || [])[0];
  return (first && first.id) || "dev";
}

function ccTeamPresetInfo() {
  const id = ccTeamPreset();
  return (CC.teamPresets || []).find((x) => x.id === id) || null;
}

/** 填预设下拉。`want` = 存完/删完要选中的 id；没有就保住当前选择。
 *  ⚠ 换预设会把 **草稿（CC.teamDraft）清空** —— 面板里改过的角色/名字属于"这一次"，不跨预设带。 */
function ccTeamFillPreset(want) {
  const sel = ccEl("cc-team-preset");
  CC.teamDraft = null;
  if (!sel) return;
  const list = CC.teamPresets || [];
  const cur = want || sel.value;
  sel.innerHTML = list.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}${p.builtin ? "" : " ★"}</option>`).join("")
    || `<option value="dev">开发 + 审查 + 测试</option>`;
  if (cur) sel.value = cur;
  if (list.length && !sel.value) sel.value = list[0].id;
}

/** 面板里当前这几行（草稿优先：用户在面板上改过角色/名字就用改过的）。 */
function ccTeamRows() {
  if (CC.teamDraft) return CC.teamDraft;
  const p = ccTeamPresetInfo();
  return ((p && p.rows) || []).map((r) => ({ role: r.role, name: r.name,
                                             roleLabel: r.roleLabel || ccRoleLabel(r.role) }));
}

function ccTeamRoleSelectHtml(i, role) {
  const c = String(role || "");
  const opts = Object.keys(CC.roles || {}).map((k) =>
    `<option value="${esc(k)}"${k === c ? " selected" : ""}>${esc(ccRoleLabel(k))}</option>`).join("");
  return `<select class="cc-team-role" data-team-role="${i}" title="角色（只注入它自己的提示词）">${opts}</select>`;
}

function ccTeamRender() {
  const box = ccEl("cc-team-rows");
  if (!box) return;
  ccFillAiEngines();                       // 先把「大脑」清单填好
  const engines = Array.from((ccEl("cc-ai-engine") || {}).options || []);
  const engOpts = (cur) => `<option value="">（默认引擎）</option>`
    + engines.map((o) => `<option value="${esc(o.value)}"${o.value === cur ? " selected" : ""}>${esc(o.textContent)}</option>`).join("");
  if (!CC.teamDraft) CC.teamDraft = ccTeamRows().map((r) => ({ role: r.role, name: r.name }));
  const rows = CC.teamDraft;
  if (!rows.length) { box.innerHTML = `<div class="muted">这个预设没有成员</div>`; return; }
  box.innerHTML = rows.map((r, i) => `<div class="cc-team-row">`
    + ccTeamRoleSelectHtml(i, r.role)
    + `<input type="text" class="cc-team-name" data-team-name="${i}" value="${esc(r.name)}"
         maxlength="24" autocomplete="off" title="@ 用它（字母/数字/_/-）">`
    + `<select class="cc-team-eng" data-team-eng="${i}" title="这个角色用哪个大脑">${engOpts(r.engine || "")}</select>`
    + `</div>`).join("");
}

/** 把面板里的值读回草稿（建组 / 存预设都用它）。 */
function ccTeamSyncDraft() {
  const box = ccEl("cc-team-rows");
  if (!box) return CC.teamDraft || [];
  const old = CC.teamDraft || [];
  const rows = [];
  box.querySelectorAll(".cc-team-row").forEach((el, i) => {
    const role = String((el.querySelector("[data-team-role]") || {}).value || (old[i] || {}).role || "");
    const name = String((el.querySelector("[data-team-name]") || {}).value || "").trim();
    const engine = String((el.querySelector("[data-team-eng]") || {}).value || "");
    rows.push({ role: role, name: name, engine: engine });
  });
  CC.teamDraft = rows;
  return rows;
}

async function ccTeamGo() {
  if (!CC.chatID) { ccTeamNote("先选一个群（会话）", true); return; }
  const rows = ccTeamSyncDraft().filter((r) => r.role);
  if (!rows.length) { ccTeamNote("至少留一个角色", true); return; }
  const task = String((ccEl("cc-team-task") || {}).value || "").trim();
  const btn = ccEl("cc-team-go");
  if (btn) btn.disabled = true;
  ccTeamNote("建组中…（每个人都要上游建 agent + 进群，可能要几秒）");
  try {
    const r = await ccAgentFetch("/team", { method: "POST",
      body: { chat_id: CC.chatID, preset: ccTeamPreset(), rows: rows } });
    CC.ai = (r && r.agents) || CC.ai;
    ccRenderAI();
    ccDisRenderMembers();
    const created = (r && r.created) || [];
    const bad = (r && r.failed) || [];
    let note = `建好 ${created.length} 个` + (bad.length
      ? `；失败 ${bad.length} 个：` + bad.map((x) => x.name + "（" + (x.stage || "") + "）").join("、")
      : "，已进群并开应答");
    // ★建完自动派第一活：一条 `@a @b 任务`（服务端按 mentions 各推 → 并行），并挂上派活汇总卡
    if (created.length && task) {
      const text = created.map((x) => "@" + x.name).join(" ") + " " + task;
      if (ccSend(text)) {
        CC.dis = {
          chat: String(CC.chatID), task: task, ts: Date.now(),
          members: created.map((x) => ({ id: String(x.id), name: x.name, role: x.role || "",
                                         roleLabel: x.roleLabel || "", status: "wait" })),
        };
        ccDisRender();
        note += `；已派第一活给 ${created.length} 个成员`;
        const t = ccEl("cc-team-task");
        if (t) t.value = "";
      } else {
        note += "；第一活没发出去（WebSocket 未就绪）";
      }
    }
    ccTeamNote(note, bad.length > 0);
    if (created.length) setBanner(`已建组：${created.map((x) => x.name + "(" + (x.roleLabel || x.role) + ")").join("、")}`);
  } catch (e) {
    ccTeamNote("建组失败：" + (e && e.message ? e.message : e), true);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** ★把当前面板里的「角色 + 名字」存成自定义预设（后端 `POST /ccagent/team/preset`）。 */
async function ccTeamSavePreset(label) {
  const name = String(label || "").trim();
  if (!name) { ccTeamNote("先给预设起个名字", true); return false; }
  const rows = ccTeamSyncDraft().filter((r) => r.role)
    .map((r) => ({ role: r.role, name: r.name }));
  if (!rows.length) { ccTeamNote("至少留一个角色", true); return false; }
  try {
    const r = await ccAgentFetch("/team/preset", { method: "POST",
      body: { action: "save", label: name, rows: rows } });
    CC.teamPresets = (r && r.teamPresets) || CC.teamPresets;
    const saved = (r && r.preset) || {};
    ccTeamFillPreset(saved.id);        // 选中刚存的那个（草稿跟着预设重置）
    ccTeamRender();
    ccTeamNote(`已存为预设「${saved.label || name}」` + (saved.builtin === false ? "（自定义，可删）" : ""));
    return true;
  } catch (e) {
    ccTeamNote("存预设失败：" + (e && e.message ? e.message : e), true);
    return false;
  }
}

/** 删除当前选中的**自定义**预设（内置的不许删）。 */
async function ccTeamDeletePreset() {
  const p = ccTeamPresetInfo();
  if (!p) { ccTeamNote("没有选中的预设", true); return false; }
  if (p.builtin) { ccTeamNote("内置预设不能删 —— 可以「存为预设」另存一份再改", true); return false; }
  try {
    const r = await ccAgentFetch("/team/preset", { method: "POST",
      body: { action: "delete", id: p.id } });
    CC.teamPresets = (r && r.teamPresets) || CC.teamPresets;
    const first = (CC.teamPresets || [])[0];
    ccTeamFillPreset(first && first.id);
    ccTeamRender();
    ccTeamNote(`已删除预设「${p.label}」`);
    return true;
  } catch (e) {
    ccTeamNote("删预设失败：" + (e && e.message ? e.message : e), true);
    return false;
  }
}

function ccTeamNote(text, isErr) {
  const n = ccEl("cc-team-note");
  if (!n) return;
  n.textContent = text || "";
  n.classList.toggle("err", !!isErr);
}

function ccRenderAI() {
  const box = ccEl("cc-ai-list");
  if (!box) return;
  const list = CC.ai || [];
  if (!list.length) { box.innerHTML = `<div class="cc-ai-empty">还没有 AI 成员</div>`; return; }
  box.innerHTML = list.map((a) => {
    const inChat = ccIsInChat(a);
    return `<div class="cc-ai" data-id="${esc(a.id)}">`
      + `<div class="cc-ai-head">`
      + `<span class="cc-ai-name" title="id ${esc(a.id)}">`
      + `<i class="cc-dot${a.enabled && a.running ? " on" : ""}"></i>${esc(a.name)}</span>`
      + `<span class="cc-ai-sp"></span>`
      + `<button type="button" class="cc-ai-b" data-act="join" ${inChat ? "disabled" : ""}>加群</button>`
      + `<button type="button" class="cc-ai-b" data-act="leave" ${inChat ? "" : "disabled"}>退群</button>`
      + `<button type="button" class="cc-ai-b${a.enabled ? " on" : ""}" data-act="toggle">`
      + `${a.enabled ? "应答中" : "已停"}</button>`
      + `<button type="button" class="cc-ai-b del" data-act="del" title="删除这个 AI 成员">×</button>`
      + `</div>`
      + `<div class="cc-ai-opts">`
      + `<span class="cc-ai-lbl">大脑</span>${ccAiEngineSelect(a)}`
      + `<span class="cc-ai-lbl">角色</span>`
      + `<select class="cc-ai-role" data-role-for="${esc(a.id)}" title="角色：只注入它自己的提示词（别人看不到）">`
      + ccRoleOptionsHtml(a.role || "") + `</select>`
      + `</div>`
      + `<div class="cc-ai-opts">`
      + `<span class="cc-ai-lbl">权限上限</span>${ccAiPermSelect(a)}`
      + `<span class="cc-ai-eff" title="${esc("来源：" + (a.effectivePermLevelFrom || "跟随面板"))}">`
      + `生效：${esc(permLevelLabel(a.effectivePermLevel || a.panelPermLevel || "ask"))}</span>`
      + `</div>`
      + `</div>`
      + (a.warn ? `<div class="cc-ai-warn" title="权限：${esc(a.warn)}">⚠ ${esc(a.warn)}</div>` : "");
  }).join("");
}

async function ccLoadAI() {
  if (!CC.token) return;
  try {
    const r = await ccAgentFetch("/list");
    CC.ai = (r && r.agents) || [];
    CC.roles = (r && r.roles) || CC.roles || {};
    CC.teamPresets = (r && r.teamPresets) || CC.teamPresets || [];
    ccFillRoles();
    ccDisRenderMembers();
  } catch (e) { /* 拿不到就保持 */ }
}

async function ccAiAdd() {
  const name = String((ccEl("cc-ai-name") || {}).value || "").trim();
  const engine = String((ccEl("cc-ai-engine") || {}).value || "");
  const role = String((ccEl("cc-ai-role") || {}).value || "");
  if (!name) { ccNote("给 AI 成员起个名字（@ 它用）", true); return; }
  if (!engine) { ccNote("没有可用的 ACP agent 当大脑（设置 → ACP 代理里先加一个）", true); return; }
  try {
    const r = await ccAgentFetch("/create", { method: "POST",
      body: { name: name, display: name, engine: engine, role: role } });
    ccEl("cc-ai-name").value = "";
    const a = r && r.agent;
    if (a && CC.chatID) {
      try { await ccAgentFetch("/join", { method: "POST", body: { id: a.id, chat_id: CC.chatID } }); } catch { /* ignore */ }
      try { await ccAgentFetch("/enabled", { method: "POST", body: { id: a.id, on: true } }); } catch { /* ignore */ }
    }
    CC.ai = (r && r.agents) || CC.ai;
    ccRenderAI();
    ccDisRenderMembers();
    ccNote("已建 AI 成员：" + name + "（用 " + engine + " 当大脑"
      + (role ? "，角色 " + ccRoleLabel(role) : "") + "）");
  } catch (e) { ccNote("建 AI 成员失败：" + e.message, true); }
}

/** ★方向 3 收尾：每个群聊成员自己的权限上限（只会比面板更严）。 */
const CC_AI_PERM = [
  { id: "", label: "跟随面板" },
  { id: "ask", label: "全部询问" },
  { id: "read", label: "只读自动" },
  { id: "edit", label: "读写自动" },
  { id: "exec", label: "执行自动" },
  { id: "all", label: "全部放行" },
];

function ccAiPermSelect(a) {
  const cur = String(a.permLevel || "");
  const eff = String(a.effectivePermLevel || "");
  const tip = "这个成员自己的权限上限（只会比面板更严，不会更宽）；当前生效："
    + permLevelLabel(eff || "ask");
  return `<select class="cc-ai-perm" data-perm-for="${esc(a.id)}" title="${esc(tip)}">`
    + CC_AI_PERM.map((x) => `<option value="${esc(x.id)}"`
      + `${x.id === cur ? " selected" : ""}>${esc(x.label)}</option>`).join("")
    + `</select>`;
}

async function ccAiPerm(id, level) {
  const a = ccAgentById(id);
  const who = (a && a.name) || id;
  try {
    const r = await ccAgentFetch("/update", { method: "POST",
      body: { id: id, permLevel: String(level || "") } });
    if (r && r.agents) CC.ai = r.agents;
    ccRenderAI();
    ccNote(`「${who}」的权限上限：` + (level ? permLevelLabel(level) : "跟随面板"));
  } catch (e) {
    ccNote("设权限上限失败：" + e.message, true);
  }
}

/** 每行的「大脑」下拉：列出可用的 ACP agent，当前绑定的即使不可用也保留。 */
function ccAiEngineSelect(a) {
  const list = (ACP.list || []).filter((x) => x.available);
  const opts = list.map((x) =>
    `<option value="${esc(x.id)}"${String(x.id) === String(a.engine) ? " selected" : ""}>${esc(x.label || x.id)}</option>`).join("");
  const extra = (a.engine && !list.some((x) => String(x.id) === String(a.engine)))
    ? `<option value="${esc(a.engine)}" selected>${esc(a.engine)}</option>` : "";
  return `<select class="cc-ai-eng" data-act="engine" title="换大脑（用哪个 ACP agent 生成回复）">${extra}${opts}</select>`;
}

async function ccAiEngine(id, engine) {
  const a = (CC.ai || []).find((x) => String(x.id) === String(id)) || {};
  try {
    await ccAgentFetch("/update", { method: "POST", body: { id: id, engine: engine } });
    ccNote("已把 " + (a.name || id) + " 的大脑换成 " + engine);
    await ccLoadAI();
  } catch (e) { ccNote("换大脑失败：" + e.message, true); }
}

async function ccAiAct(id, act) {
  const a = (CC.ai || []).find((x) => String(x.id) === String(id)) || { id: id };
  try {
    if (act === "join") {
      await ccAgentFetch("/join", { method: "POST", body: { id: id, chat_id: CC.chatID } });
      await ccAgentFetch("/enabled", { method: "POST", body: { id: id, on: true } });
      ccNote("已把 " + a.name + " 拉进本群并开始应答");
    } else if (act === "leave") {
      await ccAgentFetch("/leave", { method: "POST", body: { id: id, chat_id: CC.chatID } });
      await ccAgentFetch("/enabled", { method: "POST", body: { id: id, on: false } });
      ccNote(a.name + " 已退出本群");
    } else if (act === "toggle") {
      await ccAgentFetch("/enabled", { method: "POST", body: { id: id, on: !a.enabled } });
    } else if (act === "del") {
      if (!window.confirm("删除 AI 成员「" + a.name + "」？（同时从 cocraft 服务端删掉这个 agent）")) return;
      await ccAgentFetch("/remove", { method: "POST", body: { id: id, hard: true } });
      ccNote("已删除 " + a.name);
    }
    await ccLoadAI();
  } catch (e) { ccNote("操作失败：" + e.message, true); }
}

function ccParseIds(s) {
  return String(s || "").split(/[,，\s]+/).map((x) => parseInt(x, 10)).filter((x) => Number.isFinite(x));
}

async function ccInvite() {
  const ids = ccParseIds(ccEl("cc-invite-ids").value);
  if (!CC.chatID) { ccNote("先选一个会话", true); return; }
  if (!ids.length) { ccNote("填要邀请的用户 ID（逗号分隔）", true); return; }
  try {
    await ccFetch(`/chats/${encodeURIComponent(CC.chatID)}/invite`, { method: "POST", body: { members: ids } });
    ccEl("cc-invite-ids").value = "";
    ccNote("已邀请：" + ids.join("、"));
    ccMenuOpen(false);
    await ccLoadChats();
    if (CC.chatID) await ccRefreshChat();
  } catch (e) { ccNote("邀请失败：" + e.message, true); }
}

async function ccRemoveMember() {
  const ids = ccParseIds(ccEl("cc-remove-id").value);
  if (!CC.chatID) { ccNote("先选一个会话", true); return; }
  if (!ids.length) { ccNote("填要移出的用户 ID", true); return; }
  const id = ids[0];
  const mine = CC.me && String(CC.me.id) === String(id);
  try {
    await ccFetch(`/chats/${encodeURIComponent(CC.chatID)}/members/${encodeURIComponent(id)}`, { method: "DELETE" });
    ccEl("cc-remove-id").value = "";
    ccMenuOpen(false);
    ccNote(mine ? "已退出该会话" : ("已移出：" + id));
    if (mine) { CC.chatID = ""; CC.msgs = []; ccRenderMsgs(); }
    await ccLoadChats();
    ccRenderRoomHead();
  } catch (e) { ccNote("移出失败：" + e.message, true); }
}

/** 拉一次当前会话详情（成员变化后刷新成员行） */
async function ccRefreshChat() {
  if (!CC.chatID) return;
  try {
    const c = await ccFetch(`/chats/${encodeURIComponent(CC.chatID)}`);
    if (c && c.id != null) {
      const i = CC.chats.findIndex((x) => String(x.id) === String(c.id));
      if (i >= 0) CC.chats[i] = c; else CC.chats.push(c);
      ccRenderChats(); ccRenderRoomHead();
    }
  } catch { /* 忽略：列表/头只是刷新失败 */ }
}

/** 抽屉头部「↻」：看得见地在刷新（未登录就先重新校验；有反馈；图标转起来） */
async function ccRefreshAll() {
  const btn = ccEl("cc-refresh");
  if (btn) btn.classList.add("busy");
  try {
    if (!CC.token) {
      await ccInit();                         // 没登录：重新校验（可能已登录/已过期）
      ccNote(CC.token ? "已登录" : "还没登录，请先登录或注册");
      return;
    }
    await ccLoadChats();
    if (CC.chatID) { await ccLoadMsgs(); await ccRefreshChat(); ccLoadMembers(); ccLoadFloor(); }
    ccNote("已刷新");
    setTimeout(() => { if (ccEl("cc-note").textContent === "已刷新") ccNote(""); }, 1500);
  } catch (e) {
    ccNote("刷新失败：" + e.message, true);
  } finally {
    if (btn) setTimeout(() => btn.classList.remove("busy"), 450);
  }
}

/* ---- WebSocket ---- */
function ccCloseWS() {
  POLL.clear("cc-ws-ping"); CC.wsPing = null;
  if (CC.wsock) { try { CC.wsock.onclose = null; CC.wsock.close(); } catch { /* ignore */ } CC.wsock = null; }
}

function ccConnectWS() {
  if (!CC.token || !CC.ws) return;
  ccCloseWS();
  let sock;
  try { sock = new WebSocket(CC.ws + "?token=" + encodeURIComponent(CC.token)); }
  catch (e) { ccNote("WebSocket 建连失败：" + e.message, true); return; }
  CC.wsock = sock;
  sock.onopen = () => {
    CC.wsRetry = 0; ccNote("");
    POLL.clear("cc-ws-ping");
    CC.wsPing = POLL.interval("cc-ws-ping", 25000, () => { try { sock.send(JSON.stringify({ type: "ping" })); } catch { /* ignore */ } }, { heartbeat: true });
  };
  sock.onmessage = (ev) => {
    let d = null; try { d = JSON.parse(ev.data); } catch { return; }
    const t = d && d.type;
    if (t === "message") {
      // ★ 推送帧**本身就是消息本体** → 直接追加（省一次往返；也不会再"只看得到最旧的 100 条"）
      if (String(d.chat_id) === String(CC.chatID)) ccPushMsg(d);
      ccBumpUnread(d.chat_id, d);        // ★未读（自己发的不算；正在看的也不算）
      ccMentionNotify(d);                // ★@我 → 可点横幅（抽屉关着时也能看见）
      ccLoadChats();
    } else if (t === "mention") {
      // 服务端定向推给"被 @ 的连接"（对 agent 是它的 agent_token 连接）。人一般走 message 广播，
      // 但真收到就说明确实点了我 → 强制按 @我 处理（顺带也记一条未读）。
      if (String(d.chat_id) === String(CC.chatID)) ccPushMsg(d);
      ccBumpUnread(d.chat_id, d);
      ccMentionNotify(d, true);
      ccLoadChats();
    } else if (t === "presence") {
      ccApplyPresence(d);                       // agent/用户上线、下线
    } else if (t === "floor") {
      ccApplyFloor(d);                          // 发言权变更
    } else if (t === "chat_invite" || t === "chat_removed" || t === "chat_deleted") {
      ccLoadChats();
    } else if (t === "error") {
      ccNote("服务端：" + (d.detail || "错误"), true);
    }
  };
  sock.onclose = (ev) => {
    POLL.clear("cc-ws-ping"); CC.wsPing = null;
    if (!CC.token) return;
    if (ev && ev.code === 1008) { ccSetToken(""); ccShowAuth("登录已过期（token 无效）"); return; }
    CC.wsRetry = Math.min(CC.wsRetry + 1, 6);
    ccNote("连接断开，重连中…", true);
    setTimeout(() => { if (CC.token) ccConnectWS(); }, Math.min(1000 * CC.wsRetry, 8000));
  };
  sock.onerror = () => { /* onclose 会跟来 */ };
}

/* ---- 兜底轮询（WS 已恢复主动推送 → 只在**连接断了**时做慢速兜底） ---- */
function ccStartPoll() {
  ccStopPoll();
  CC.poll = POLL.interval("cc-poll", 30000, () => {
    if (!CC.token || !cocraftIsOpen()) return;
    const wsUp = CC.wsock && CC.wsock.readyState === 1;
    if (wsUp) return;                     // WebSocket 正常 → 完全靠推送，不轮询
    ccLoadChats();
    if (CC.chatID) ccLoadMsgs(false);
  });
}
function ccStopPoll() { POLL.clear("cc-poll"); CC.poll = null; }

/* ---- 登录 / 注册 ---- */
async function ccLogin(account, password) {
  const r = await ccFetch("/auth/login", { method: "POST", body: { account, password } });
  ccSetToken(r.token); CC.me = r.user; ccShowMain(); ccNote("");
}
async function ccRegister(username, email, password, code) {
  const r = await ccFetch("/auth/register", { method: "POST", body: { username, email, password, code } });
  ccSetToken(r.token); CC.me = r.user; ccShowMain(); ccNote("");
}
async function ccSendCode(email, username) {
  await ccFetch("/auth/send_code", { method: "POST", body: { email, username } });
}

/* ---- 抽屉宽度可拖动 ---- */
function ccApplyWidth(w, commit) {
  // 分屏：给面板至少留 300px（抽屉最大 = 视口 - 300）—— 都是**设计像素**（见 uiZoom）
  const z = uiZoom();
  const max = Math.max(300, (window.innerWidth - 300) / z);
  const nw = Math.max(300, Math.min(w, max));
  document.documentElement.style.setProperty("--cc-w", nw + "px");
  // ★ 只有"提交"时才同步 #app 的 padding —— 拖动过程中不重排整个主面板（否则每帧重排，很卡）
  if (commit) document.documentElement.style.setProperty("--cc-pad", nw + "px");
}

function ccInitResize() {
  const box = $("cocraft"), grip = $("cc-resize");
  if (!box || !grip) return;
  let dragging = false, startX = 0, startW = 0, rafId = -1, lastW = 0;
  grip.addEventListener("pointerdown", (e) => {
    dragging = true; startX = e.clientX; startW = box.getBoundingClientRect().width / uiZoom();
    box.classList.add("resizing");
    document.documentElement.classList.add("cc-resizing");
    try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    lastW = startW + (startX - e.clientX) / uiZoom();   // 往左拖 = 变宽（换算成设计像素）
    if (rafId !== -1) return;                        // ★ rAF 节流：每帧最多改一次 CSS 变量
    rafId = requestAnimationFrame(() => { rafId = -1; ccApplyWidth(lastW, true); });
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false; box.classList.remove("resizing");
    if (rafId !== -1) { cancelAnimationFrame(rafId); rafId = -1; ccApplyWidth(lastW, true); }
    document.documentElement.classList.remove("cc-resizing");
    try { grip.releasePointerCapture(e.pointerId); } catch { /* ignore */ }
    ccStore(CC_W_KEY, String(Math.round(box.getBoundingClientRect().width)));
  };
  grip.addEventListener("pointerup", end);
  grip.addEventListener("pointercancel", end);
}

function initCocraft() {
  if (!$("cocraft")) return;
  $("cc-close").addEventListener("click", () => setCocraft(false));
  const _mid = ccEl("cc-myid");
  if (_mid) _mid.addEventListener("click", () => ccCopyMyId(_mid));
  $("cc-refresh").addEventListener("click", ccRefreshAll);
  $("cc-logout").addEventListener("click", () => { ccSetToken(""); ccCloseWS(); ccShowAuth("已退出登录"); });
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const pop = ccEl("cc-menu-pop");
    if (pop && !pop.hidden) { ccMenuOpen(false); return; }
    if (cocraftIsOpen()) setCocraft(false);
  });

  document.querySelectorAll(".cc-tab").forEach((b) => b.addEventListener("click", () => {
    document.querySelectorAll(".cc-tab").forEach((x) => x.classList.toggle("on", x === b));
    $("cc-login").hidden = b.dataset.tab !== "login";
    $("cc-reg").hidden = b.dataset.tab !== "reg";
  }));

  $("cc-login").addEventListener("submit", async (e) => {
    e.preventDefault();
    ccAuthMsg("登录中…");
    try { await ccLogin($("cc-login-account").value.trim(), $("cc-login-password").value); ccAuthMsg(""); }
    catch (err) { ccAuthMsg("登录失败：" + err.message, true); }
  });
  $("cc-reg-send").addEventListener("click", async () => {
    ccAuthMsg("发送中…");
    try {
      await ccSendCode($("cc-reg-email").value.trim(), $("cc-reg-username").value.trim());
      ccAuthMsg("验证码已发送，请查收邮箱");
    } catch (err) { ccAuthMsg("发送失败：" + err.message, true); }
  });
  $("cc-reg").addEventListener("submit", async (e) => {
    e.preventDefault();
    ccAuthMsg("注册中…");
    try {
      await ccRegister($("cc-reg-username").value.trim(), $("cc-reg-email").value.trim(),
                       $("cc-reg-password").value, $("cc-reg-code").value.trim());
      ccAuthMsg("");
    } catch (err) { ccAuthMsg("注册失败：" + err.message, true); }
  });

  $("cc-chats").addEventListener("click", (e) => {
    const row = e.target.closest && e.target.closest(".cc-chat");
    if (row) ccOpenChat(row.dataset.chat);
  });
  $("cc-new").addEventListener("click", (e) => { e.stopPropagation(); ccNewChat(); });
  $("cc-menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    ccMenuOpen(ccEl("cc-menu-pop").hidden);
  });
  $("cc-invite-go").addEventListener("click", ccInvite);
  $("cc-remove-go").addEventListener("click", ccRemoveMember);
  $("cc-ai-add").addEventListener("click", ccAiAdd);
  // ★一键建组
  const teamOpen = $("cc-team-open");
  if (teamOpen) teamOpen.addEventListener("click", () => ccTeamOpen($("cc-team").hidden));
  const teamX = $("cc-team-x");
  if (teamX) teamX.addEventListener("click", () => ccTeamOpen(false));
  const teamGo = $("cc-team-go");
  if (teamGo) teamGo.addEventListener("click", ccTeamGo);
  const teamPreset = $("cc-team-preset");
  if (teamPreset) teamPreset.addEventListener("change", () => { CC.teamDraft = null; ccTeamRender(); ccTeamNote(""); });
  const teamSave = $("cc-team-save");
  if (teamSave) teamSave.addEventListener("click", () => {
    const n = window.prompt("给这个预设起个名字（含角色 + 名字，到时候一键复用）", "我的组");
    if (n) ccTeamSavePreset(n);
  });
  const teamDel = $("cc-team-del");
  if (teamDel) teamDel.addEventListener("click", () => ccTeamDeletePreset());
  const teamRows = $("cc-team-rows");
  if (teamRows) {
    teamRows.addEventListener("input", (e) => {
      const el = e.target.closest && e.target.closest("[data-team-name]");
      if (!el) return;
      const i = Number(el.dataset.teamName);
      if (CC.teamDraft && CC.teamDraft[i]) CC.teamDraft[i].name = String(el.value || "");
    });
    teamRows.addEventListener("change", (e) => {
      if (!CC.teamDraft) ccTeamSyncDraft();
      const rl = e.target.closest && e.target.closest("[data-team-role]");
      if (rl) {
        const i = Number(rl.dataset.teamRole);
        if (CC.teamDraft[i]) { CC.teamDraft[i].role = rl.value; CC.teamDraft[i].roleLabel = ccRoleLabel(rl.value); }
        return;
      }
      const en = e.target.closest && e.target.closest("[data-team-eng]");
      if (en) {
        const i = Number(en.dataset.teamEng);
        if (CC.teamDraft[i]) CC.teamDraft[i].engine = en.value;
      }
    });
  }
  initCcPerm();                         // ★ 权限申请小弹窗（输入框上方）：按钮 → 发 `@agent 可以`
  $("cc-ai-list").addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest(".cc-ai-b");
    const row = e.target.closest && e.target.closest(".cc-ai");
    if (b && row && !b.disabled) ccAiAct(row.dataset.id, b.dataset.act);
  });
  $("cc-ai-list").addEventListener("change", (e) => {
    const sel = e.target.closest && e.target.closest(".cc-ai-eng");
    const row = e.target.closest && e.target.closest(".cc-ai");
    if (sel && row) { ccAiEngine(row.dataset.id, sel.value); return; }
    const ps = e.target.closest && e.target.closest("[data-perm-for]");
    if (ps && row) { ccAiPerm(row.dataset.id, ps.value); return; }
    const rs = e.target.closest && e.target.closest(".cc-ai-role");
    if (rs && row) { ccAiRole(row.dataset.id, rs.value); }
  });
  // ★主动编排：派活台
  const disBtn = $("cc-dispatch");
  if (disBtn) disBtn.addEventListener("click", () => ccDisOpen(!CC.disOpen));
  const disClose = $("cc-dis-close");
  if (disClose) disClose.addEventListener("click", () => ccDisOpen(false));
  const disGo = $("cc-dis-go");
  if (disGo) disGo.addEventListener("click", ccDispatchGo);
  $("cc-dis-members").addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("[data-dis]");
    if (!b) return;
    const id = String(b.dataset.dis);
    if (CC.disSel.has(id)) CC.disSel.delete(id); else CC.disSel.add(id);
    ccDisRenderMembers();
    ccDisNote("");
  });
  $("cc-msgs").addEventListener("click", (e) => {
    const d = e.target.closest && e.target.closest("[data-dis-hide]");
    if (d) { CC.dis = null; ccDisRender(); return; }
    const c = e.target.closest && e.target.closest("[data-dis-cmp]");
    if (c) { ccCmpOpen(); }
  });
  // ★多方案对比
  const cmpClose = $("cmp-close");
  if (cmpClose) cmpClose.addEventListener("click", ccCmpClose);
  const cmpReload = $("cmp-reload");
  if (cmpReload) cmpReload.addEventListener("click", ccCmpRender);
  const cmpDiff = $("cmp-diff-btn");
  if (cmpDiff) cmpDiff.addEventListener("click", ccCmpToggleDiff);
  const cmpBox = $("cmp");
  if (cmpBox) cmpBox.addEventListener("click", (e) => { if (e.target === cmpBox) ccCmpClose(); });
  const arbBox = $("cmp-arb");
  if (arbBox) arbBox.addEventListener("click", (e) => {
    const p = e.target.closest && e.target.closest("[data-arb-pick]");
    if (p) { ccArbJump(); return; }
    const b = e.target.closest && e.target.closest("[data-arb-go]");
    if (b) ccArbAsk();
  });
  document.addEventListener("click", (e) => {
    const pop = ccEl("cc-menu-pop");
    if (!pop || pop.hidden) return;
    if (e.target.closest && (e.target.closest("#cc-menu-pop") || e.target.closest("#cc-menu-btn"))) return;
    ccMenuOpen(false);
  });
  $("cc-send-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const box = $("cc-send");
    if (ccSend(box.value)) { box.value = ""; box.style.height = "auto"; }
  });
  $("cc-send").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("cc-send-form").requestSubmit(); }
  });
  $("cc-send").addEventListener("input", (e) => {
    const t = e.target; t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 120) + "px";
  });
  // 发文件：📎 → 选文件 → 上传（服务端自动发一条 file 消息）
  const attach = $("cc-attach"), fileInput = $("cc-file");
  if (attach && fileInput) {
    attach.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      const f = fileInput.files && fileInput.files[0];
      if (f) ccSendFile(f);
    });
  }

  // 群聊图片：点一下在面板内**放大预览**（复用 #img-view；遮罩 / × / Esc 关闭由全局处理）
  document.addEventListener("click", (e) => {
    const t = e.target.closest && e.target.closest("[data-cc-img]");
    if (!t) return;
    e.preventDefault();
    openImageView(t.getAttribute("data-cc-img") || "");
  });

  ccInitResize();
  ccApplyWidth(parseInt(ccLoad(CC_W_KEY), 10) || CC_W_DEFAULT, true);
  window.addEventListener("resize", () => {                       // 窗口尺寸变了要重算让位宽度
    ccApplyWidth(parseInt(ccLoad(CC_W_KEY), 10) || CC_W_DEFAULT, true);
  });
  ccInit();     // 取 /cocraft/_url + 校验已存 token（不加载任何第三方 iframe）
}

function initAttachments() {
  $("btn-attach").addEventListener("click", () => $("file-input").click());
  $("file-input").addEventListener("change", (e) => {
    addFiles(e.target.files);
    e.target.value = "";
  });

  $("attach-row").addEventListener("click", (e) => {
    const btn = e.target.closest(".rm");
    if (btn) {
      const id = btn.closest(".attach").dataset.id;
      state.attachments = state.attachments.filter((a) => a.id !== id);
      renderAttachRow();
      return;
    }
    const chip = e.target.closest(".attach");
    if (chip && /^image\//.test(chip.dataset.mime || "")) openImageView(chip.dataset.src || "");
  });

  // 图片大图预览：点消息里的图片打开；点遮罩 / × / Esc 关闭
  document.addEventListener("click", (e) => {
    const a = e.target.closest && e.target.closest("a.shot");
    if (a) { e.preventDefault(); openImageView(a.getAttribute("href") || ""); return; }
    const box = $("img-view");
    if (box && !box.hidden && (e.target === box
        || (e.target.closest && e.target.closest("#img-view-close")))) {
      closeImageView();
    }
  });
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    const box = $("img-view");
    if (box && !box.hidden) closeImageView();
  });

  // 粘贴图片（截图后直接 Ctrl+V）
  $("input").addEventListener("paste", (e) => {
    const cd = e.clipboardData;
    if (!cd) return;
    const files = [];
    for (const it of cd.items || []) {
      if (it.kind === "file") {
        const f = it.getAsFile();
        if (f) files.push(f);
      }
    }
    if (!files.length) return;
    const hasText = (cd.getData("text/plain") || "").length > 0;
    if (!hasText) e.preventDefault();
    addFiles(files);
  });

  // 拖拽到窗口任意位置
  const isFileDrag = (e) => Array.from((e.dataTransfer && e.dataTransfer.types) || []).includes("Files");
  // ⚠ 壁纸选择器（#wp）开着时**让路**：那边有自己的拖拽导入（图片/视频 → 背景）。
  //   以前不判断 → 拖进壁纸选择器的图被当成聊天附件塞进输入框（用户实测踩到）。
  const wallPickerOpen = () => {
    const w = document.getElementById("wp");
    return !!(w && !w.hidden);
  };
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    if (wallPickerOpen() || !isFileDrag(e)) return;
    e.preventDefault();
    depth += 1;
    $("drop-veil").hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (wallPickerOpen() || !isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (wallPickerOpen() || !isFileDrag(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) $("drop-veil").hidden = true;
  });
  window.addEventListener("drop", (e) => {
    if (wallPickerOpen() || !isFileDrag(e)) return;
    e.preventDefault();
    depth = 0;
    $("drop-veil").hidden = true;
    addFiles(e.dataTransfer.files);
  });
}

/** 附件 → prompt 接口需要的 files[] */
function outgoingFiles() {
  return state.attachments.map((a) => ({ uri: a.dataUrl, name: a.name }));
}
