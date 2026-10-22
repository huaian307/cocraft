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

/** 本文件被加载时带着的 ?v= —— 在脚本同步执行期用 document.currentScript 取到。
 *  用途：和服务端 /healthz 报告的版本比对，不一致就自动刷新，
 *  免得面板窗口一直跑旧代码，让人以为"改了没生效"。 */
const UI_VER = (() => {
  try {
    const s = document.currentScript;
    return (s && s.src ? new URL(s.src).searchParams.get("v") : "") || "";
  } catch { return ""; }
})();

const $ = (id) => document.getElementById(id);

/** 一次性把旧键（opencode-ui.*）搬到新键（cocraft.*）—— 项目从 opencode-ui 改名为 cocraft，
 *  这样老用户升级后主题/侧栏/引擎/音乐等设置都还在（目标键已存在就不覆盖）。 */
function migrateLegacyKeys() {
  try {
    const olds = [];
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (k && k.indexOf("opencode-ui.") === 0) olds.push(k);
    }
    for (const k of olds) {
      let rest = k.slice("opencode-ui.".length);
      // 旧的群聊键叫 opencode-ui.cocraft.* → 新键就是 cocraft.*（去掉中间那层）
      if (rest.indexOf("cocraft.") === 0) rest = rest.slice("cocraft.".length);
      const nk = "cocraft." + rest;
      if (localStorage.getItem(nk) === null) {
        const v = localStorage.getItem(k);
        if (v !== null) localStorage.setItem(nk, v);
      }
    }
  } catch { /* 隐私模式等：忽略 */ }
}

/* ---------------- 基础工具 ---------------- */

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const detail = body && typeof body === "object"
      ? (body.error || body.message || JSON.stringify(body))
      : String(body || res.statusText);
    throw new Error(`${res.status} ${detail}`);
  }
  return body;
}

const unwrap = (b) => (b && typeof b === "object" && "data" in b ? b.data : b);

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Markdown 渲染（无依赖；XSS 安全：先整体 HTML 转义，再套我们自己的标签）。
 *  支持：```代码块```、`行内代码`、**加粗**、*斜体*、~~删除线~~、
 *        标题(#..######)、无序/有序列表、> 引用、--- 分割线、
 *        [文字](链接)（只放行 http(s)/mailto/相对/#）、标准 GFM 表格。
 *  其余按纯文本显示。 */
function renderText(src) {
  const parts = String(src ?? "").split(/```/);
  let out = "";
  for (let i = 0; i < parts.length; i++) {
    if (i % 2 === 1) {
      let body = parts[i];
      const nl = body.indexOf("\n");
      if (nl >= 0) body = body.slice(nl + 1);      // 去掉 ``` 那行的语言标注
      // 代码块外面套一层，好放「复制」按钮（文本从 <pre> 的 textContent 取，原样不丢换行）
      out += `<div class="code-wrap"><button type="button" class="copy-code" title="复制这段代码">复制</button>`
        + `<pre>${esc(body.replace(/\n+$/, ""))}</pre></div>`;
    } else {
      out += renderBlocks(parts[i]);
    }
  }
  return out;
}

/** 行内 Markdown：内部先转义，再只插入我们自己的安全标签。 */
function inlineMd(raw) {
  let s = esc(raw);
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) =>
    /^(https?:|mailto:|\/|#)/i.test(u) ? `<a href="${u}" target="_blank" rel="noreferrer">${t}</a>` : t);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
}

const MD_H = /^(#{1,6})\s+(.*)$/;
const MD_UL = /^\s*[-*+]\s+/;
const MD_OL = /^\s*\d+\.\s+/;
const MD_HR = /^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/;
const MD_BQ = /^\s*>\s?/;
const MD_SEP = /^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$/;

function mdCells(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

/** 块级 Markdown → HTML（表格 / 列表 / 标题 / 引用 / 分割线 / 段落）。 */
function renderBlocks(md) {
  const lines = md.split("\n");
  let out = "", p = [], i = 0;
  const flushP = () => {
    if (p.length) out += `<div class="md-p">${p.map(inlineMd).join("<br>")}</div>`;
    p = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*$/.test(line)) { flushP(); i++; continue; }
    // GFM 表格：本行含 | 且下一行是分隔行
    if (line.indexOf("|") >= 0 && i + 1 < lines.length && MD_SEP.test(lines[i + 1])) {
      flushP();
      const head = mdCells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].indexOf("|") >= 0 && lines[i].trim() !== "") {
        rows.push(mdCells(lines[i]));
        i++;
      }
      out += `<table><thead><tr>${head.map((c) => `<th>${inlineMd(c)}</th>`).join("")}</tr></thead>`
        + `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
      continue;
    }
    const h = line.match(MD_H);
    if (h) { flushP(); const lv = Math.min(h[1].length, 6); out += `<h${lv}>${inlineMd(h[2])}</h${lv}>`; i++; continue; }
    if (MD_HR.test(line)) { flushP(); out += "<hr>"; i++; continue; }
    if (MD_BQ.test(line)) {
      flushP();
      const q = [];
      while (i < lines.length && MD_BQ.test(lines[i])) { q.push(lines[i].replace(MD_BQ, "")); i++; }
      out += `<blockquote>${q.map(inlineMd).join("<br>")}</blockquote>`;
      continue;
    }
    if (MD_UL.test(line)) {
      flushP();
      const items = [];
      while (i < lines.length && MD_UL.test(lines[i])) { items.push(lines[i].replace(MD_UL, "")); i++; }
      out += `<ul>${items.map((x) => `<li>${inlineMd(x)}</li>`).join("")}</ul>`;
      continue;
    }
    if (MD_OL.test(line)) {
      flushP();
      const items = [];
      while (i < lines.length && MD_OL.test(lines[i])) { items.push(lines[i].replace(MD_OL, "")); i++; }
      out += `<ol>${items.map((x) => `<li>${inlineMd(x)}</li>`).join("")}</ol>`;
      continue;
    }
    p.push(line); i++;
  }
  flushP();
  return out;
}

const fmtTime = (ms) => (ms ? new Date(ms).toLocaleString("zh-CN", { hour12: false }) : "");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ---------------- 全局状态 ---------------- */

const state = {
  sessions: [],
  current: null,
  messages: [],         // 服务端「已完成」的消息
  live: {},             // 生成中的消息缓冲（由 SSE 增量驱动）
  reveal: {},           // 打字机已显示字数 { msgID: { text, reasoning } }
  frozen: {},           // 已被用户中止的轮次 { msgID: true } —— 迟到增量一律丢弃
  pending: [],          // 本地乐观回显「我发的消息」
  attachments: [],      // 待发送附件
  named: {},            // 已经自动命名过的会话 { id: true } —— 每个会话最多命名一次
  detailOpen: {},       // 侧栏会话详情展开状态 { id: true }
  // ★会话列表里的「项目文件树」（按会话展开；数据按**根目录**缓存 → 同项目共用，不重复请求）
  treeOpen: {},         // { [sessionID]: true }  哪些会话的树展开了
  treeData: {},         // { [root]: { [path]: items } }  /fs/list 的结果
  treeExp: {},          // { [root]: { [path]: true } }   目录展开状态
  treeErr: {},          // { [root]: { [path]: "原因" } } 读目录失败（在树里显示，别误报"空目录"）
  follow: true,         // 是否跟随最新内容自动往下滚
  refreshing: false,
  pendingRefresh: false,
  msgCursor: null,      // ★往上翻页：更早一页的游标（两个引擎形状不同，见 msgCursorOf）
  msgDone: true,        // 没有更早的消息了
  msgLoading: false,    // 正在加载更早的一页
  msgOlder: false,      // 已经往上翻过（用来显示"没有更早的消息了"）
  openParts: new Set(), // 用户手动展开过的折叠块（key=msgID:idx）—— 重渲染后保留，不再自动展开
};

/* ---------------- 侧栏：会话列表 ---------------- */

async function loadSessions() {
  try {
    const body = await api("/api/session?limit=50&order=desc");
    state.sessions = unwrap(body) || [];
    renderSessions();
  } catch (err) {
    $("session-list").innerHTML = `<div class="error-box">加载会话失败：${esc(err.message)}</div>`;
  }
}

/** 数字千分位；拿不到就返回空串 */
function fmtNum(n) {
  const v = Number(n);
  return Number.isFinite(v) ? v.toLocaleString("zh-CN") : "";
}

/** 会话详情（默认收起；只有点右侧 ▸ 才显示，里面才出现本地目录） */
function sessionDetailHtml(s) {
  const t = s.time || {};
  const tok = s.tokens || {};
  const cache = tok.cache || {};
  const model = s.model || {};
  const rows = [];
  const row = (k, v) => { if (v) rows.push(`<div><b>${k}</b><span>${v}</span></div>`); };
  row("会话", `<code>${esc(s.id)}</code>`);
  const dir = (s.location && s.location.directory) || "";
  row("目录", dir ? `<code>${esc(dir)}</code>` : "");
  row("模型", model.id
    ? esc([model.providerID, model.id].filter(Boolean).join("/"))
      + (model.variant && model.variant !== "default" ? ` · ${esc(model.variant)}` : "")
    : "");
  row("创建", esc(fmtTime(t.created)));
  row("更新", esc(fmtTime(t.updated)));
  const parts = [];
  if (tok.input != null) parts.push(`输入 ${fmtNum(tok.input)}`);
  if (tok.output != null) parts.push(`输出 ${fmtNum(tok.output)}`);
  if (tok.reasoning != null) parts.push(`思考 ${fmtNum(tok.reasoning)}`);
  if (cache.read != null) parts.push(`缓存读 ${fmtNum(cache.read)}`);
  row("Token", esc(parts.join(" · ")));
  if (s.context && Number(s.context.size) > 0) {
    const cu = Number(s.context.used) || 0, cs = Number(s.context.size);
    const left = Math.max(0, Math.round((1 - cu / cs) * 100));
    row("上下文", esc(`${fmtTok(cu)} / ${fmtTok(cs)}（剩 ${left}%）`));
  }
  if (s.cost != null) row("花费", esc(Number(s.cost).toFixed(6)));
  row("结果", esc(s.outcome));
  return rows.join("") || '<div><b>详情</b><span>暂无</span></div>';
}

/* ---- ★上下文长度（“还够用多少”）—— 引擎无关 ----
   * **ACP**：agent 主动上报 `usage_update {used,size}`（codex / opencode-acp 会发；dsh 等不发）
     → `state.current.context` + 事件 `session.usage.update`；
   * **OpenCode**：`/api/model` 的 `limit.context` 是窗口上限；上一条 assistant 的
     `tokens.input + tokens.cache.read + tokens.cache.write` ≈ 本轮实际占用的上下文；
   * 两个都拿不到就显示「上下文 —」并说明**为什么**（不编数字）。 */
const CTX = { used: 0, size: 0, src: "", model: "" };

function fmtTok(n) {
  n = Math.max(0, Number(n) || 0);
  if (n >= 1e8) return (n / 1e8).toFixed(1).replace(/\.0$/, "") + " 亿";
  if (n >= 1e4) return (n / 1e4).toFixed(n >= 1e6 ? 0 : 1).replace(/\.0$/, "") + " 万";
  return String(Math.round(n));
}

/** 模型窗口上限（`limit.context`）。OpenCode 给；ACP 的模型清单没有这一项 → 返回 0。
 *  ⚠ 只认**当前引擎**的清单（切引擎后旧清单不能拿来算上限，否则百分比会莫名其妙地变）。 */
function ctxLimitOf(ref) {
  const id = String((ref && (ref.id || ref.modelID)) || "");
  if (!id) return 0;
  const list = MODEL.list || [];
  if (!list.length || !modelEngineMatches()) return 0;
  const prov = String((ref && ref.providerID) || "");
  const hit = list.find((m) => String(m.id) === id
    && (!prov || !m.providerID || String(m.providerID) === prov));
  const n = Number((hit && hit.limit && hit.limit.context) || 0);
  return n > 0 ? n : 0;
}

/** 从最近一条 assistant 消息推上下文占用（优先 agent 上报的 context，其次 OpenCode 的 tokens）。 */
function ctxFromMessages() {
  const msgs = state.messages || [];
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i];
    if (!m || m.type !== "assistant") continue;
    if (m.context && Number(m.context.size) > 0) {
      return { used: Number(m.context.used) || 0, size: Number(m.context.size) || 0,
               src: "agent", model: String((m.model && m.model.id) || "") };
    }
    const t = m.tokens;
    if (!t) continue;
    const used = Number(t.input || 0) + Number((t.cache && t.cache.read) || 0)
      + Number((t.cache && t.cache.write) || 0);
    if (!used) continue;
    const ref = (state.current && state.current.model && state.current.model.id)
      ? state.current.model : (m.model || {});
    const size = ctxLimitOf(ref);
    return { used: used, size: size, src: size ? "model" : "used",
             model: String((ref && ref.id) || "") };
  }
  return null;
}

function ctxCompute() {
  if (!state.current) return { used: 0, size: 0, src: "", model: "" };
  const mid = String((state.current.model && state.current.model.id) || "");
  const sc = state.current.context;
  // ★size=0 也认：dsh 这类 agent 只在 prompt 结果里给 tokens，后端估一个 used（estimated=true）
  if (sc && (Number(sc.used) > 0 || Number(sc.size) > 0)) {
    let size = Number(sc.size) || 0;
    // ★两个引擎同一口径：agent 没给窗口上限时，用**当前引擎**模型清单里的 limit.context 补一次
    //   （只补真实清单里有的模型；补不到就还是「~N」，绝不编一个数）
    if (!size && Number(sc.used) > 0) size = ctxLimitOf(state.current.model || { id: mid });
    return { used: Number(sc.used) || 0, size: size,
             src: sc.estimated ? "estimate" : "agent", model: mid };
  }
  return ctxFromMessages() || { used: 0, size: 0, src: "", model: "" };
}

function ctxTitle() {
  if (!CTX.used && !CTX.size) {
    return "上下文用量：这个 agent 还没上报\n"
      + "（ACP 里 codex / opencode-acp 会发 usage_update；dsh 等不发，OpenCode 走模型清单 + 上一条回答的 tokens）";
  }
  const bits = ["已用 " + fmtTok(CTX.used) + (CTX.size ? " / " + fmtTok(CTX.size) : "") + " tokens"];
  if (CTX.size) bits.push("剩余 " + Math.max(0, Math.round((1 - CTX.used / CTX.size) * 100)) + "%");
  if (CTX.model) bits.push("模型 " + CTX.model);
  bits.push(CTX.src === "agent" ? "来源：agent 上报的 usage_update"
    : CTX.src === "estimate"
      ? (CTX.size ? "来源：本轮 tokens 估算（窗口上限取自模型清单）"
                  : "来源：本轮 tokens 估算（这个 agent 没上报 usage_update；窗口上限未知）")
    : CTX.src === "model" ? "来源：模型窗口上限 + 上一条回答的 tokens"
    : "窗口上限未知（模型清单没给 limit.context）");
  return bits.join("\n");
}

/** 同步输入框左下角那个「上下文 …」小条（选会话 / 刷新消息 / usage 事件 / 换模型都会调）。 */
function ctxSync() {
  const b = $("ctx-chip");
  if (!b) return;
  if (!state.current) { b.hidden = true; return; }
  // 模型清单还没拉过 → 补一次（拿到 limit.context 才能算“剩多少”）
  if (MODEL.list == null && !MODEL.loading) { loadModels().then(() => ctxSync()).catch(() => {}); }
  const c = ctxCompute();
  CTX.used = c.used; CTX.size = c.size; CTX.src = c.src; CTX.model = c.model;
  b.hidden = false;
  if (CTX.size) {
    const left = Math.max(0, Math.min(100, Math.round((1 - CTX.used / CTX.size) * 100)));
    b.textContent = "上下文 剩 " + left + "%";
  } else {
    b.textContent = CTX.used
      ? ((CTX.src === "estimate" ? "上下文 ~" : "上下文 ") + fmtTok(CTX.used))
      : "上下文 —";
  }
  b.title = ctxTitle();
  const pct = CTX.size ? CTX.used / CTX.size : 0;
  b.classList.toggle("warn", pct >= 0.8 && pct < 0.95);
  b.classList.toggle("danger", pct >= 0.95);
  b.dataset.src = CTX.src || "";
}

function ctxShowInfo() {
  if (!state.current) return;
  if (!CTX.used && !CTX.size) {
    setBanner("这个 agent 还没上报上下文用量（不支持 usage_update）", { ms: 6000 });
    return;
  }
  setBanner(ctxTitle().replace(/\n/g, " · "), { ms: 9000 });
}

/** 会话属于哪个项目：取工作目录的最后一段当项目名（拿不到就留空）。
 *  ⚠ 只显示目录名，完整路径依旧只在「详细信息」里出现。 */
function projectNameOf(dir) {
  const d = String(dir || "").replace(/[\\/]+$/, "");
  if (!d) return "";
  const parts = d.split(/[\\/]/);
  return parts[parts.length - 1] || d;
}

/* ---------------- ★会话列表里的「项目文件树」（2026-10-16） ----------------
   数据走**编码模式同一套后端**：`/fs/list?root=&path=`（引擎无关、`realpath` 越界校验、跳过重目录）。
   ⚠ 数据按**根目录**缓存（`state.treeData[root]`）—— 同一个项目常有多条会话，
     展开第二条同项目会话**不会重复请求**（缓存命中直接画）。
   点文件 → 交给 `codeOpenAt()` 在「编码模式」右侧抽屉里打开（可编辑 / 可一键运行）。 */
/** ★文件树里的图标：**目录用标准文件夹**（合上 / 打开两种形状，SVG、随主题上色）。
 *  ⚠ 别改用 emoji 📁/📂 —— 实测 Segoe UI Emoji 里这两个**看不出区别**，会丢掉「展开/合上」的信息。
 *  会话列表的树与编码模式的树**共用**这一个口径，别再各写一套符号。文件本轮先不配图标（保持干净）。 */
function treeIco(isDir, open) {
  if (!isDir) return "";
  const d = open
    ? "m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2"
      + " 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"
    : "M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2"
      + " 0 0 0-2 2v13a2 2 0 0 0 2 2Z";
  return `<svg class="fold${open ? " open" : ""}" viewBox="0 0 24 24" width="13" height="13" fill="none"`
    + ` stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">`
    + `<path d="${d}"/></svg>`;
}

function sessionTreeHtml(root) {
  const d = state.treeData[root];
  const errs = state.treeErr[root] || {};
  if (errs[root]) return `<div class="empty">读目录失败：${esc(errs[root])}</div>`;
  if (!d || d[root] === undefined) return `<div class="empty">加载中…</div>`;
  const exp = state.treeExp[root] || {};
  let html = "";
  const walk = (list, depth) => {
    for (const it of list) {
      const pad = 4 + depth * 12;
      if (it.dir) {
        const open = !!exp[it.path];
        html += `<div class="tn" data-sdir="${esc(it.path)}" style="padding-left:${pad}px" title="${esc(it.path)}">`
          + `<span class="tw ico">${treeIco(true, open)}</span><span class="nm">${esc(it.name)}</span></div>`;
        if (open) {
          if (errs[it.path]) {
            html += `<div class="empty" style="padding-left:${pad + 12}px">读目录失败：${esc(errs[it.path])}</div>`;
          } else {
            const kids = d[it.path];
            if (kids) walk(kids, depth + 1);
            else html += `<div class="empty" style="padding-left:${pad + 12}px">…</div>`;
          }
        }
      } else {
        const on = (typeof CODE !== "undefined" && CODE.root === root && CODE.sel === it.path) ? " on" : "";
        html += `<div class="tn${on}" data-sfile="${esc(it.path)}" style="padding-left:${pad}px" title="${esc(it.path)}">`
          + `<span class="tw"></span><span class="nm">${esc(it.name)}</span></div>`;
      }
    }
  };
  walk(d[root], 0);
  return html || `<div class="empty">空目录</div>`;
}

/** 就地刷新所有已展开的树（**不重画整个列表** → 不把左侧列表的滚动位置弹回去）。 */
function renderSessionTrees() {
  const list = document.getElementById("session-list");
  if (!list) return;
  list.querySelectorAll(".session-tree").forEach((el) => {
    el.innerHTML = sessionTreeHtml(el.dataset.root);
  });
}

/** 拉一个目录（结果按 root 缓存）；拉完就地刷新。 */
async function sessionTreeLoad(root, path) {
  const box = (state.treeData[root] = state.treeData[root] || {});
  const errs = (state.treeErr[root] = state.treeErr[root] || {});
  delete errs[path];
  try {
    const r = await api(`/fs/list?root=${encodeURIComponent(root)}&path=${encodeURIComponent(path)}`);
    box[path] = (r.data && r.data.items) || [];
  } catch (err) {
    // ⚠ 别把"读不了"显示成"空目录"（目录被删 / 盘没挂 / 没权限时很容易误导）
    errs[path] = String((err && err.message) || err);
    box[path] = [];
    setBanner("读目录失败：" + errs[path]);
  }
  renderSessionTrees();
}

/** 展开 / 收起某条会话的文件树（**首次展开**才拉根目录）。 */
function sessionTreeToggle(id) {
  state.treeOpen[id] = !state.treeOpen[id];
  renderSessions();
  if (!state.treeOpen[id]) return;
  const s = state.sessions.find((x) => String(x.id) === String(id));
  const root = (s && s.location && s.location.directory) || "";
  const d = state.treeData[root];
  if (root && !(d && d[root] !== undefined)) sessionTreeLoad(root, root);
}

function renderSessions() {
  const q = $("search").value.trim().toLowerCase();
  const items = state.sessions.filter((s) => !q || (s.title || "").toLowerCase().includes(q));
  if (!items.length) {
    // ACP 引擎：面板里的会话只是「我们自己的索引」，agent 那边可能还有一堆
    // （典型：用 opencode-acp 接进 OpenCode，它本地早就有的会话不会自动出现在这里）
    const hint = ENGINE.active === "acp"
      ? `<div class="muted" style="padding:10px">没有匹配的会话</div>
         <div style="padding:0 10px 10px"><button type="button" id="empty-import">从 agent 导入会话…</button></div>`
      : `<div class="muted" style="padding:10px">没有匹配的会话</div>`;
    $("session-list").innerHTML = hint;
    const b = $("empty-import");
    if (b) b.addEventListener("click", openImport);
    return;
  }
  $("session-list").innerHTML = items.map((s) => {
    const active = state.current && s.id === state.current.id ? " active" : "";
    const open = !!state.detailOpen[s.id];
    const when = s.time ? fmtTime(s.time.updated || s.time.created) : "";
    const dir = (s.location && s.location.directory) || "";
    const proj = projectNameOf(dir);
    const tip = open ? "收起详细信息" : "展开详细信息";
    const tOpen = !!state.treeOpen[s.id];                 // ★项目文件树是否展开
    return `<div class="session${active}" data-id="${esc(s.id)}" title="${esc(s.title || "(无标题)")}">
      <button type="button" class="info" data-info="${esc(s.id)}"
              title="${tip}" aria-label="${tip}">${open ? "▾" : "▸"}</button>
      <button type="button" class="del" data-del="${esc(s.id)}"
              title="删除这个会话" aria-label="删除会话">×</button>
      ${dir ? `<button type="button" class="tree-btn${tOpen ? " on" : ""}" data-tree="${esc(s.id)}"
              title="${tOpen ? "收起项目文件树" : "展开项目文件树（" + esc(proj) + "）"}"
              aria-label="项目文件树" aria-expanded="${tOpen ? "true" : "false"}">${treeIco(true, false)}</button>` : ""}
      <span class="t">${esc(s.title || "(无标题)")}</span>
      <span class="s-row">
        <span class="s">${esc(when)}${s.outcome ? " · " + esc(s.outcome) : ""}</span>
        ${proj ? `<span class="proj" title="项目：${esc(proj)}">${esc(proj)}</span>` : ""}
      </span>
      <div class="det"${open ? "" : " hidden"}>${sessionDetailHtml(s)}</div>
      ${tOpen && dir ? `<div class="session-tree" data-root="${esc(dir)}">${sessionTreeHtml(dir)}</div>` : ""}
    </div>`;
  }).join("");
}

/** 删除会话：`DELETE /api/session/{id}` → 204（接口一直支持，以前前端没有入口）。
 *  删掉的如果是当前会话，就把界面清回"未选择"再自动选剩下最新的一条。 */
async function deleteSession(id, title) {
  const label = (title || "").trim() || "(无标题)";
  let n = 0;
  try { n = (state.messages || []).length; } catch { /* ignore */ }
  const extra = (state.current && state.current.id === id && n)
    ? `\n（含当前这 ${n} 条消息，无法撤销）` : "（无法撤销）";
  if (!confirm(`删除会话「${label}」？${extra}`)) return;
  try {
    await api(`/api/session/${encodeURIComponent(id)}`, { method: "DELETE" });
    delete state.detailOpen[id];
  } catch (err) {
    alert(`删除失败：${err.message}`);
    return;
  }
  if (state.current && state.current.id === id) {
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
  }
  await loadSessions();
  if (!state.current && state.sessions.length) await openSession(state.sessions[0].id);
}

/* ---------------- 消息渲染 ---------------- */

/** 附件两种形态都要能显示：
 *  已落库 { data(base64), mime, source, name } / 待发送 { dataUrl, mime, name } */
function attachSrc(f) {
  if (!f) return "";
  if (typeof f.dataUrl === "string") return f.dataUrl;
  if (typeof f.data === "string" && typeof f.mime === "string") return `data:${f.mime};base64,${f.data}`;
  return "";
}

function renderAttachments(list) {
  if (!Array.isArray(list) || !list.length) return "";
  return `<div class="files">` + list.map((f) => {
    const name = String((f && (f.name || f.filename || f.path || f.file)) || "附件")
      .split(/[\\/]/).pop();
    const src = attachSrc(f);
    const mime = (f && f.mime) || "";
    if (src && /^image\//.test(mime)) {
      return `<a class="shot" href="${esc(src)}" target="_blank" title="${esc(name)}">`
        + `<img src="${esc(src)}" alt="${esc(name)}"></a>`;
    }
    return `<span class="file">📎 ${esc(name)}</span>`;
  }).join("") + `</div>`;
}

function renderToolPart(part, key) {
  const st = (part.state && part.state.status) || (part.executed ? "completed" : "pending");
  const cls = st === "completed" ? "ok" : st === "error" ? "err" : "run";
  const input = part.state && part.state.input ? JSON.stringify(part.state.input, null, 2) : "";
  const out = (part.state && (part.state.output || part.state.error)) || "";
  return `<details class="part"${state.openParts.has(key) ? " open" : ""} data-k="${esc(key)}">
    <summary><span class="tag ${cls}">${esc(st)}</span> ${esc(part.name || "tool")}</summary>
    <div class="body">${esc(input)}${out ? "\n\n" + esc(String(out).slice(0, 20000)) : ""}</div>
  </details>`;
}

function renderReasoningPart(part, key, msgId) {
  const t = part.text || "";
  const head = t.slice(0, 56).replace(/\s+/g, " ");
  // ★ 思考**默认折叠**（以前一直展开，太占版面）；用户点了才展开，展开状态记在 state.openParts、
  //   重渲染后保留。折叠时 summary 里有开头一小段预览，想看全文点一下即可。
  const open = state.openParts.has(key) ? " open" : "";
  if (part.live) {
    return `<details class="part auto-open"${open} data-k="${esc(key)}">
      <summary><span class="tag thinking">思考中…</span> ${esc(head)}</summary>
      <div class="body stream"><span data-live-reasoning data-msgid="${esc(msgId)}">${esc(t)}</span></div>
    </details>`;
  }
  if (!t.trim()) return "";
  return `<details class="part auto-open"${open} data-k="${esc(key)}">
    <summary><span class="tag">思考</span> ${esc(head)}…</summary>
    <div class="body">${esc(t)}</div>
  </details>`;
}

/** Codex 在自定义 provider 下会把 `Warning: Model metadata for ... not found...` 当成回复正文开头，
 *  纯噪音 —— 展示时去掉开头这一段（实时与落库都过一遍）。 */
const MODEL_WARN_RE = /^\s*Warning: Model metadata for `[^`]*` not found\.[^\n]*\n+/i;
function stripModelWarning(t) { return String(t ?? "").replace(MODEL_WARN_RE, ""); }

/** 人类可读时长：45 → `45s`、95 → `1m35s`、3900 → `1h5m`。
 *  （整轮动辄几分钟，原来的 `671s` 没法读。） */
function fmtDur(secs) {
  const s = Math.max(0, Math.round(secs || 0));
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60 ? (s % 60) + "s" : "");
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h + "h" + (m ? m + "m" : "");
}

/** 每条消息 → 它所在「那一轮」的信息（由 renderMessages 预先算好，见 buildTurnMap）。 */
let TURNMAP = new Map();

/** 一轮（turn）= 一条用户消息之后、到下一条用户消息之前的**所有助手消息**。
 *  预先算好每条的「整轮起点时间 / 这一轮有没有思考 / 是不是这一轮最后一条」。
 *  ⚠ 主界面那条「思考 Ns」要的是**整轮总时长**（从你发问那一刻算到这一条完成），
 *    而不是单条消息自己的时长 —— 一个回合里助手常常拆成十几条消息（每步一条，中间还夹着系统通知），
 *    只看单条会把"等了五分钟"显示成「思考 14s」。 */
function buildTurnMap() {
  const msgs = state.messages || [];
  const map = new Map();
  let turnStart = null, from = 0;
  const flush = (to) => {
    if (turnStart == null || to < from) return;
    let last = -1, hasThink = false;
    for (let i = from; i <= to; i++) {
      const m = msgs[i]; if (!m) continue;
      if (m.type === "assistant") last = i;
      if (!hasThink && Array.isArray(m.content)
          && m.content.some((p) => p && p.type === "reasoning" && String(p.text || "").trim())) {
        hasThink = true;
      }
    }
    if (last < 0) return;
    for (let i = from; i <= to; i++) {
      const m = msgs[i]; if (!m) continue;
      map.set(m.id, { start: turnStart, last: i === last, hasThink });
    }
  };
  for (let i = 0; i < msgs.length; i++) {
    const m = msgs[i] || {};
    if (m.type === "user") {
      flush(i - 1);
      turnStart = ((m.time || {}).created) || null;      // ★整轮从"你发问那一刻"算起
      from = i + 1;
    } else if (turnStart == null) {                      // 会话开头没有用户消息的兜底
      turnStart = ((m.time || {}).created) || null;
      from = i;
    }
  }
  flush(msgs.length - 1);
  return map;
}

/** 主界面那条「思考 · 时长」小条（细节在「轨迹」页）。
 *  时长是**整轮**的（从你发问算到这条完成），不是这条消息自己的。 */
function thinkChipHtml(m) {
  const t = m.time || {};
  if (!t.completed) return `<div class="think-chip live">✦ 思考中…</div>`;
  const info = TURNMAP.get(m.id);
  const start = (info && info.start) || t.created;
  const secs = (start && t.completed) ? Math.max(0, (t.completed - start) / 1000) : 0;
  const title = info ? "整轮用时（从你发问算到最后一条）" : "这一条的用时";
  return `<div class="think-chip" title="${title}">✦ 思考<em>${secs ? " " + fmtDur(secs) : ""}</em></div>`;
}

/** 会话内的一行中文小条（自动续写 / 目录切换 / 注入技能 / 切换模式…）。
 *  `inner` 必须是**已经转义好**的 HTML。 */
function noticeHtml(inner) {
  return `<div class="notice"><span class="line"></span>
    <span class="label">${inner}</span>
    <span class="line"></span></div>`;
}

/** `Model.Ref` → 一行文字（`id · variant`）。
 *  实测上游的 `providerID` 常是面板自己的中性占位 `acp` —— 只在**没有 id** 时才拿它兜底，
 *  免得显示成 `acp/deepseek/deepseek-flash` 这种名字。 */
function modelRefText(ref) {
  if (!ref) return "默认";
  const id = ref.id || ref.modelID || ref.name || "";
  const v = (ref.variant && ref.variant !== "default") ? ` · ${ref.variant}` : "";
  const base = id || ref.providerID || "";
  return base ? base + v : "默认";
}

/** 结构化错误（OpenCode 的 `Session.StructuredError`）→ 一行人话，绝不把 JSON 倒给用户。 */
function textOfError(e) {
  if (!e) return "";
  if (typeof e === "string") return oneLine(e, 300);
  const head = e.type ? `[${e.type}] ` : "";
  let msg = e.message || e.name || "";
  if (!msg && typeof e.data === "string") msg = e.data;
  const body = e.response && (e.response.body || e.response.statusText);
  return oneLine(head + msg + (body ? " · " + String(body) : ""), 400);
}

/** ★OpenCode 的上下文压缩（`compaction` 消息）→ 「会话已压缩」小条 + 可折叠的摘要。
 *  上游字段：`status` running|completed|failed · `reason` auto|manual ·
 *  `summary`（压缩出来的上下文摘要，很长的 Markdown）· `recent` · `tokens{input,output}` · `cost` · `error`。
 *  以前没处理 → 主界面只剩「未知结构 · 原始数据」。 */
function renderCompactionMsg(m) {
  const st = String(m.status || "completed");
  const auto = (m.reason || "auto") !== "manual";
  const tok = m.tokens || {};
  const bits = [auto ? "自动" : "手动"];
  const usage = [];
  if (tok.input != null) usage.push("输入 " + fmtNum(tok.input));
  if (tok.output != null) usage.push("输出 " + fmtNum(tok.output));
  if (usage.length) bits.push(usage.join(" / "));
  if (m.time && m.time.created) bits.push(fmtTime(m.time.created));
  const label = st === "running" ? "正在压缩会话…" : (st === "failed" ? "会话压缩失败" : "会话已压缩");
  const cls = st === "running" ? " run" : (st === "failed" ? " err" : " ok");
  const icon = st === "running" ? "✻" : (st === "failed" ? "⚠" : "⇲");
  const err = st === "failed" ? textOfError(m.error) : "";
  const summary = (typeof m.summary === "string" ? m.summary : "").trim();
  const sumKey = "cp:" + String(m.id || "");
  const sumHtml = summary
    ? `<details class="part cp-sum"${state.openParts.has(sumKey) ? " open" : ""} data-k="${esc(sumKey)}">
        <summary><span class="tag">摘要</span> 压缩出的上下文摘要（${fmtNum(summary.length)} 字）</summary>
        <div class="body cp-md">${renderText(summary)}</div>
      </details>`
    : "";
  return `<div class="notice compact${cls}"><span class="line"></span>
    <span class="label"><span class="tag${cls}">${icon} ${label}</span>
      <span class="cp-sub">${esc(bits.join(" · "))}</span></span>
    <span class="line"></span></div>`
    + (err ? `<div class="cp-err">${esc(err)}</div>` : "") + sumHtml;
}

/** 会话里的 `shell` 消息（在客户端用 `!命令` 跑过的那种）：命令 + 状态 + 可折叠输出。 */
function renderShellMsg(m) {
  const st = String(m.status || "");
  const stZh = { running: "运行中", exited: "已完成", timeout: "超时", killed: "已终止" }[st] || st;
  const cls = st === "running" ? " run" : (st === "exited" || !st ? " ok" : " err");
  const cmd = oneLine(m.command || "", 240) || "(空命令)";
  const exitTxt = (m.exit != null && m.exit !== "") ? `exit ${m.exit}` : "";
  const out = (m.output && typeof m.output.output === "string") ? m.output.output : "";
  const outHtml = out.trim()
    ? `<details class="part cp-sum cp-out"><summary><span class="tag">输出</span> 标准输出（${fmtNum(out.length)} 字）</summary>
        <div class="body">${esc(out.slice(0, 20000))}</div></details>`
    : "";
  return `<div class="notice compact"><span class="line"></span>
    <span class="label"><span class="tag${cls}">命令</span>
      <span class="cp-sub">${esc(cmd)}</span>
      ${stZh ? `<span class="cp-sub">${esc(stZh)}</span>` : ""}
      ${exitTxt ? `<span class="cp-sub">${esc(exitTxt)}</span>` : ""}
      ${m.time && m.time.created ? `<span class="cp-sub">${esc(fmtTime(m.time.created))}</span>` : ""}
    </span><span class="line"></span></div>${outHtml}`;
}

function renderMessage(m) {
  const type = m.type || "assistant";
  if (type === "idle") return "";

  // ★系统注入通知（AGENTS.md 指令变更 / 日期 / 运行环境…）**不进主界面** —— 整条归「轨迹」页。
  //   一个回合里它会出现十几次（每一轮工具调用后都可能来一条），把消息区刷满。
  if (type === "system") return "";
  // 会话内的少量提示仍留在主界面：工作目录切换 / 自动续写
  if (type === "location-switched" || type === "synthetic") {
    const label = type === "synthetic" ? "自动续写" : "工作目录已切换";
    const body = (m.text || "").trim();
    const text = !body ? "" : "：" + esc(body.slice(0, 160));
    return noticeHtml(esc(label) + text);
  }

  // 技能注入：OpenCode 与 ACP 都可能有这种消息，只显示"装了哪个技能"，不倒正文
  if (type === "skill") {
    const nm = m.name || m.skill || "技能";
    return noticeHtml("已注入技能：" + esc(String(nm)));
  }

  // ★上下文压缩（OpenCode 的 `compaction` 消息）：客户端里是一条「会话已压缩」小条 + 可折叠摘要。
  //   `ui 0.1.69` 以前没处理 → 掉进「未知结构 · 原始数据」兜底卡（用户报过）。
  if (type === "compaction") return renderCompactionMsg(m);
  // 同一族的另外三类同样不该掉兜底卡：切换模式 / 切换模型 / 在客户端跑过的 shell 命令
  if (type === "agent-switched") return noticeHtml(`已切换模式：${esc(String(m.agent || "默认"))}`);
  if (type === "model-switched") {
    const from = m.previous ? modelRefText(m.previous) : "";
    return noticeHtml(`已切换模型：${from ? esc(from) + " → " : ""}${esc(modelRefText(m.model))}`);
  }
  if (type === "shell") return renderShellMsg(m);

  let parts = "";
  let hadReasoning = false;
  const contentArr = Array.isArray(m.content) ? m.content : null;
  if (contentArr && contentArr.length) {
    parts = contentArr.map((p, i) => {
      if (!p || typeof p !== "object") return "";
      const key = `${m.id}:${i}`;
      if (p.type === "text") {
        // ★"边做边说"的旁白（这一段后面还跟着工具调用）**不是最终输出** → 主界面不显示。
        if (contentArr.slice(i + 1).some((q) => q && q.type === "tool")) return "";
        if (p.live) {
          return `<div class="text"><span data-live-text data-msgid="${esc(m.id)}">`
            + `${renderText(stripModelWarning(p.text))}</span><span class="caret"></span></div>`;
        }
        return `<div class="text">${renderText(stripModelWarning(p.text))}</div>`;
      }
      // ★思考过程 / 工具调用**移到顶栏「轨迹」页**：主界面只留「思考用时」+ 最终输出。
      if (p.type === "reasoning") { hadReasoning = true; return ""; }
      if (p.type === "tool") return "";
      // 未知 part：兜底显示原始结构，绝不静默丢弃
      return `<details class="part" data-k="${esc(key)}">
        <summary><span class="tag">${esc(p.type || "unknown")}</span> 原始数据</summary>
        <div class="body">${esc(JSON.stringify(p, null, 2).slice(0, 8000))}</div>
      </details>`;
    }).filter(Boolean).join("");
  } else if (typeof m.text === "string" && m.text.trim()) {
    parts = `<div class="text">${renderText(m.text)}</div>`;
  } else if (typeof m.content === "string" && m.content.trim()) {
    parts = `<div class="text">${renderText(m.content)}</div>`;
  }

  const files = renderAttachments((m.files && m.files.length ? m.files : null)
    || (m.metadata && m.metadata.attachments));

  // ★「思考 时长」= **整轮**总时长：这一条自己有思考就显示；否则若是这一轮的最后一条、
  //   而整轮又确实思考过，也显示（否则一长串工具步骤之后，最终答案上就看不到总时长了）。
  const turnInfo = TURNMAP.get(m.id);
  const showThink = hadReasoning || !!(turnInfo && turnInfo.last && turnInfo.hasThink);
  const thinkChip = showThink ? thinkChipHtml(m) : "";
  const finished = !!(m.time && m.time.completed);
  const trulyEmpty = !!contentArr && contentArr.length === 0;

  if (!parts && !files) {
    if (type === "assistant") {
      if (!finished) {                       // 还在生成：给个占位，免得空着
        return `<div class="msg assistant"><div class="who"><strong>助手</strong></div>
          <div class="bubble"><div class="text muted">正在思考…</div></div></div>`;
      }
      // ★已结束、但没有最终输出（只有思考 / 工具 / 旁白）→ **主界面不显示**（细节看「轨迹」）。
      if (!trulyEmpty) return "";
      // 整条 content 真的为空（不是被藏起来的）：给个诊断，别静默
      return `<div class="msg assistant"><div class="who"><strong>助手</strong></div>
        <div class="bubble"><div class="text muted">（这一轮确实没有返回正文。若是 ACP agent：看
        <code>runtime/logs/acp.log</code>，或跑一次「opencode-ui 自检」）</div></div></div>`;
    }
    const raw = JSON.stringify(m, null, 2);
    if (!raw || raw.length < 40) return "";
    return `<div class="msg assistant"><div class="who"><strong>${esc(type)}</strong></div>
      <div class="bubble"><details class="part"><summary><span class="tag">未知结构</span> 原始数据</summary>
      <div class="body">${esc(raw.slice(0, 8000))}</div></details></div></div>`;
  }

  const isUser = type === "user";
  const mm = m.model || (m.metadata && m.metadata.model) || null;
  const modelID = mm ? (mm.id || mm.modelID) : "";
  const agent = (m.agent || (m.metadata && m.metadata.agent)) || "";
  const streaming = type === "assistant" && m.time && !m.time.completed;

  return `<div class="msg ${isUser ? "user" : "assistant"}${m.pending ? " pending" : ""}" data-mid="${esc(m.id)}">
    <div class="who">
      ${isUser ? "" : `<img class="avatar" src="${appearanceUrl("avatar", "/assets/avatar.png")}" alt="">`}
      <strong>${isUser ? "我" : "助手"}</strong>
      ${agent ? `<span class="badge">${esc(agent)}</span>` : ""}
      ${modelID ? `<span class="badge">${esc(modelID)}</span>` : ""}
      ${m.time && m.time.created ? `<span class="muted">${fmtTime(m.time.created)}</span>` : ""}
      ${m.pending ? `<span class="tag run">发送中</span>` : ""}
      ${m.queued ? `<span class="tag run" title="这一轮还在跑；结束后会自动发送。想立刻改用这条：点「■ 中断」，它马上就会发出">已排队</span>` : ""}
      ${m.undelivered ? `<span class="tag warn" title="面板重启时这条还没发出去，请重新发送">未送达</span>` : ""}
      ${streaming ? `<span class="tag run">生成中</span>` : ""}
      ${!isUser ? `<button type="button" class="copy-btn" data-copy="${esc(m.id)}" title="复制这条回答的正文（不含思考过程）">复制</button>` : ""}
    </div>
    <div class="bubble">${thinkChip}${parts}${files}</div>
  </div>`;
}

/* ---------------- 复制（回答正文 / 代码块） ---------------- */

/** 写剪贴板：异步 Clipboard API 优先（localhost 属安全上下文），失败再退化到隐藏 textarea。
 *  返回是否成功。 */
async function copyToClipboard(text) {
  const s = String(text ?? "");
  if (!s) return false;
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch { /* 继续走兜底 */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = s;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch { return false; }
}

/** 一条消息可复制的正文：助手取 text 部分（不要思考 / 工具），用户取顶层 text。 */
function messageCopyText(m) {
  if (!m) return "";
  const parts = [];
  if (Array.isArray(m.content)) {
    for (const p of m.content) {
      if (p && p.type === "text" && typeof p.text === "string") parts.push(p.text);
    }
  }
  let s = parts.join("\n\n");
  if (!s) {
    if (typeof m.text === "string") s = m.text;
    else if (typeof m.content === "string") s = m.content;
  }
  return stripModelWarning(s).trim();
}

/** 按消息 id 取正文：生成中优先用完整实时缓冲（不是打字机已吐出的那截），
 *  否则在已落库消息里找。不靠 DOM 反推，Markdown 结构才不会丢。 */
function copyMessageById(id) {
  if (!id) return "";
  const live = state.live && state.live[id];
  if (live) return stripModelWarning(joinBucket(live.text || {})).trim();
  const m = (state.messages || []).find((x) => x.id === id);
  if (m) return messageCopyText(m);
  const p = (state.pending || []).find((x) => x.id === id);
  return p ? String(p.text || "").trim() : "";
}

/** 复制后给按钮一个瞬时反馈（变字 + 变色，1.2s 复位） */
function flashCopied(btn, label, ok) {
  if (!btn) return;
  if (!btn.dataset.copyLabel) btn.dataset.copyLabel = btn.textContent;
  btn.textContent = label;
  btn.classList.add("copied");
  btn.classList.toggle("copy-fail", !ok);
  clearTimeout(btn._copyTimer);
  btn._copyTimer = setTimeout(() => {
    btn.textContent = btn.dataset.copyLabel || "复制";
    btn.classList.remove("copied", "copy-fail");
  }, 1200);
}

async function doCopy(btn, text) {
  const s = String(text ?? "");
  if (!s.trim()) { flashCopied(btn, "无可复制", false); return; }
  const ok = await copyToClipboard(s);
  flashCopied(btn, ok ? "已复制" : "复制失败", ok);
}

/** 消息级「复制」与代码块级「复制」共用一个委托监听（内容会重建，别绑在按钮上） */
function initCopy() {
  document.addEventListener("click", (e) => {
    const t = e.target;
    if (!t || !t.closest) return;
    const codeBtn = t.closest(".copy-code");
    if (codeBtn) {
      const wrap = codeBtn.closest(".code-wrap");
      const pre = wrap && wrap.querySelector("pre");
      doCopy(codeBtn, pre ? pre.textContent : "");
      return;
    }
    const msgBtn = t.closest(".copy-btn");
    if (msgBtn) doCopy(msgBtn, copyMessageById(msgBtn.dataset.copy || ""));
  });
}

/** 记录折叠块的展开状态（details 的 toggle 不冒泡，用捕获）：重渲染后保留用户的选择。 */
function initPartToggle() {
  document.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d || d.tagName !== "DETAILS" || !d.classList || !d.classList.contains("part")) return;
    const k = d.dataset && d.dataset.k;
    if (!k) return;
    if (d.open) state.openParts.add(k); else state.openParts.delete(k);
  }, true);
}

/** ★选中回答里的任意片段 → 浮出「复制选中」按钮（自由复制片段，不必整条复制）。 */
function initSelCopy() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "sel-copy";
  btn.textContent = "复制选中";
  btn.hidden = true;
  document.body.appendChild(btn);

  const hide = () => { btn.hidden = true; btn.classList.remove("copied"); };
  const selText = () => {
    const s = window.getSelection();
    return (s && !s.isCollapsed && s.rangeCount) ? s.toString() : "";
  };
  const inMessages = () => {
    try {
      const s = window.getSelection();
      let n = s && s.anchorNode;
      while (n && n.nodeType !== 1) n = n.parentNode;
      return !!(n && n.closest && n.closest("#messages"));
    } catch { return false; }
  };
  const placeAt = (x, y) => {
    const z = uiZoom();                      // 真实像素 → 设计像素（见 uiZoom）
    const left = Math.min(Math.max(x, 70 * z), window.innerWidth - 70 * z) / z;
    btn.style.left = left + "px";
    btn.style.top = Math.max(y - 36 * z, 6 * z) / z + "px";
  };
  const placeByRange = () => {
    const s = window.getSelection();
    if (!s || !s.rangeCount) return;
    const r = s.getRangeAt(0).getBoundingClientRect();
    if (!r || (!r.width && !r.height)) return;
    placeAt(r.left + r.width / 2, r.top);
  };
  const showAt = (x, y) => {
    placeAt(x, y);
    btn.hidden = false; btn.classList.remove("copied"); btn.textContent = "复制选中";
  };

  document.addEventListener("mouseup", (e) => {
    if (selText().trim() && inMessages()) showAt(e.clientX, e.clientY);   // 贴着鼠标松手的位置
    else hide();
  });
  // 键盘选择（Shift+方向键 / Ctrl+A）没有 mouseup：用 keyup 兜一下
  document.addEventListener("keyup", (e) => {
    if (!btn.hidden) return;
    if ((e.shiftKey || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "a"))
        && selText().trim() && inMessages()) { placeByRange(); btn.hidden = false; }
  });
  document.addEventListener("selectionchange", () => { if (!selText().trim()) hide(); });
  document.addEventListener("mousedown", (e) => { if (e.target !== btn) hide(); }, true);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") hide(); });
  const box = $("messages");
  if (box) box.addEventListener("scroll", hide, { passive: true });
  window.addEventListener("resize", hide);

  btn.addEventListener("mousedown", (e) => e.preventDefault());   // 保住选区
  btn.addEventListener("click", async () => {
    const t = selText();
    if (!t.trim()) { hide(); return; }
    const ok = await copyToClipboard(t);
    btn.textContent = ok ? "已复制" : "复制失败";
    btn.classList.toggle("copied", ok);
    setTimeout(() => { btn.textContent = "复制选中"; hide(); }, 900);
  });
}

/** 选区是否落在「生成中」的小块里（用于冻结重绘，别把选区冲掉）。 */
function selectionInLiveSlot() {
  try {
    const s = window.getSelection();
    if (!s || s.isCollapsed || !s.rangeCount) return false;
    let n = s.anchorNode;
    while (n && n.nodeType !== 1) n = n.parentNode;
    return !!(n && n.closest && n.closest("#live-slot"));
  } catch { return false; }
}

/** 内容指纹：只有真的变了才重建 DOM（避免流式期间反复重建大块） */
function signatureOf(items) {
  return items.map((m) => {
    let len = (m.text || "").length;
    if (Array.isArray(m.content)) {
      for (const p of m.content) {
        len += (p && p.text ? p.text.length : 0);
        const o = p && p.state && (p.state.output || p.state.error);
        len += o ? String(o).length : 0;
      }
    }
    return `${m.id}:${m.type}:${len}:${(m.time && m.time.completed) || 0}`;
  }).join("|");
}

/* ---------------- 实时流：增量缓冲 → 打字机 ---------------- */

function joinBucket(bucket, limit) {
  const s = Object.keys(bucket).sort((a, b) => a - b).map((k) => bucket[k]).join("");
  return limit == null ? s : s.slice(0, limit);
}

function liveTotal(e) {
  const sum = (o) => Object.values(o).reduce((a, s) => a + s.length, 0);
  return { text: sum(e.text), reasoning: sum(e.reasoning) };
}

/** 把增量缓冲拼成「真实助手消息」的结构；revealed 控制打字机显示到第几个字。
 *  不带 time.completed → 渲染层自动标「生成中」。 */
function liveToMessage(id, e, revealed) {
  const content = [];
  const rt = joinBucket(e.reasoning, revealed ? revealed.reasoning : null);
  // ⚠ 只要收到过该类增量就生成元素（哪怕此刻"已显示字数"还是 0）——
  //   否则 paintLiveSlot 的壳签名不变、不会重建，元素永远不出现 → 看起来"没有打字机、一次性冒出"。
  if (rt || Object.keys(e.reasoning).length) content.push({ type: "reasoning", text: rt, live: true });
  const tt = joinBucket(e.text, revealed ? revealed.text : null);
  if (tt || Object.keys(e.text).length) content.push({ type: "text", text: tt, live: true });
  for (const t of e.tools) {
    content.push({ type: "tool", name: t.name, state: { status: t.status, input: t.input } });
  }
  return {
    id, type: "assistant", agent: e.agent, model: { id: e.model },
    time: { created: e.started || Date.now() },
    content,
  };
}

function pendingLiveIds() {
  const known = new Set(state.messages.map((m) => m.id));
  return Object.keys(state.live).filter((id) => !known.has(id));
}

/** 取「模型刚才在想什么」的节选：优先生成中的实时缓冲，其次最后一条助手消息。
 *  权限弹窗用它回答"为什么要这个权限"（很多 agent 不给现成原因）。 */
function liveContextSnippet(max = 320) {
  let out = "";
  for (const id of Object.keys(state.live || {})) {
    const e = state.live[id];
    const t = joinBucket(e.reasoning || {}) || joinBucket(e.text || {});
    if (t) out = t;
  }
  if (!out) {
    for (let i = state.messages.length - 1; i >= 0; i--) {
      const m = state.messages[i];
      if (!m || m.type !== "assistant") continue;
      const parts = m.content || [];
      out = parts.filter((p) => p.type === "reasoning").map((p) => p.text || "").join("")
        || parts.filter((p) => p.type === "text").map((p) => p.text || "").join("");
      if (out) break;
    }
  }
  return stripModelWarning(String(out || "")).slice(-max).trim();
}

/** 某张自定义素材的 URL（没设就走 fallback 的自带素材）。
 *  `t` 用后端给的版本号破缓存 —— 刚换完图就能看到新的。 */
function appearanceUrl(key, fallback) {
  if (!(APPE.images || {})[key]) return fallback;
  return `/appearance/img?k=${encodeURIComponent(key)}&t=${APPE.version || 0}`;
}

/** 左上角头像（对话里那个头像在 renderMessage 里，会跟消息重建一起换） */
function applyAppearance() {
  const img = document.querySelector(".avatar-brand");
  if (img) img.src = appearanceUrl("brand", "/assets/badge.jpg");
}

/** 气泡球图标 / 音乐条背景（也走自定义素材；没设就回自带） */
function applyChromeAppearance() {
  const pbg = $("p-bg");
  const hasBg = !!(APPE.images || {}).playerbg;
  if (pbg) pbg.style.backgroundImage = hasBg ? `url("${appearanceUrl("playerbg")}")` : "";
  const player = $("player");
  if (player) player.classList.toggle("has-pbg", hasBg);
  const ico = $("lyric-ball-ico");
  if (ico) {
    if ((APPE.images || {}).lyricball) ico.innerHTML = `<img src="${appearanceUrl("lyricball")}" alt="">`;
    else ico.textContent = "词";
  }
}

/** 空会话首屏（主图 / 贴纸 / 三行文案都能改，见设置弹窗） */
function heroHtml() {
  const l1 = String(SET.heroL1 || "").trim() || HERO_DEFAULT.l1;
  const l2 = String(SET.heroL2 || "").trim() || HERO_DEFAULT.l2;
  const credit = String(SET.heroCredit || "").trim() || HERO_DEFAULT.credit;
  return `<div class="empty">
    <img class="hero-card" src="${appearanceUrl("hero", "/assets/hero.jpg")}" alt="絵梨衣">
    <div class="duck-line1">${esc(l1)}</div>
    <div class="duck-line2">${esc(l2)}</div>
    <div class="stickers">
      <img src="${appearanceUrl("sticker1", "/assets/sticker-duck.jpg")}" alt="">
      <img src="${appearanceUrl("sticker2", "/assets/sticker-couple.jpg")}" alt="">
    </div>
    <div class="credit">${esc(credit)}</div>
  </div>`;
}

/** 素材或首屏文案变了 → 重画首屏 + 重建消息区（对话头像顺带换掉） */
function refreshAppearanceArt() {
  applyAppearance();
  applyChromeAppearance();
  staticSig = null;
  heroShown = false;
  try { renderMessages(); } catch { /* 页面还没初始化完 */ }
}

/** 拉一次自定义素材清单（接口没有/失败 → 继续用自带素材，不报错） */
async function loadAppearance() {
  try {
    const r = await api("/appearance");
    APPE.images = (r && r.data) || {};
    APPE.version = (r && r.version) || 0;
  } catch { /* 老后端：保持默认素材 */ }
  return APPE;
}

/** 点「选择…」：弹原生选图框 → 设为某个位置（key 见 index.html 的 data-img） */
async function pickAppearanceImage(key) {
  try {
    const r = await api("/pick/image", { method: "POST" });
    if (!r || !r.ok) {
      if (r && r.canceled) return;
      setBanner("选择图片失败：" + ((r && r.error) || "未知错误"));
      return;
    }
    await setAppearanceImage(key, r.path);
  } catch (err) {
    setBanner("选择图片失败：" + (err && err.message ? err.message : err));
  }
}

/** `path` 为空串 = 恢复自带素材 */
async function setAppearanceImage(key, path) {
  try {
    const r = await api("/appearance", { method: "POST", body: JSON.stringify({ key, path }) });
    APPE.images = (r && r.data) || {};
    APPE.version = (r && r.version) || 0;
    refreshAppearanceArt();
    renderSettings();
    setBanner(path ? "已换上新图（想还原点「默认」）" : "已恢复自带素材");
  } catch (err) {
    setBanner("保存失败：" + (err && err.message ? err.message : err));
  }
}

let staticSig = null;
let staticHtml = "";
let heroShown = false;
let liveShellSig = null;

function swapHtml(el, html) {
  const openKeys = new Set();
  el.querySelectorAll("details[data-k]").forEach((d) => { if (d.open) openKeys.add(d.dataset.k); });
  el.innerHTML = html;
  if (openKeys.size) el.querySelectorAll("details[data-k]").forEach((d) => {
    if (openKeys.has(d.dataset.k)) d.open = true;
  });
}

/** 已完成的消息只在内容变化时重建；生成中的部分交给 paintLiveSlot */
/* ---------------- 轨迹（思考过程 + 工具调用）----------------
 * 顶栏「轨迹」按钮 → 从左侧会话列表边界向右推出的一块面板（见 style.css 的 .trace）。
 * 主会话界面只显示「思考用时 + 输出正文」；思考正文与工具明细都在这里。 */

const TRACE = { open: false };
const TRACE_OPEN = new Set();   // 用户手动展开过的条目（重渲染后保留，别一刷新就收起来）
let TRACE_KEYS = [];            // 当前画出来的条目 key（「全部展开 / 收起」用）

/** 把当前会话里所有「思考 / 工具调用 / 系统通知」按顺序收集出来（含正在生成的那一轮）。 */
function traceItems() {
  const out = [];
  const pushMsg = (m) => {
    if (!m) return;
    // ★系统注入通知（AGENTS.md 指令变更 / 日期 / 运行环境 / 工具目录变更…）整条归轨迹，
    //   主界面不再显示（见 renderMessage 里 `type === "system"` 那条 return ""）。
    if (m.type === "system") {
      out.push({ kind: "note", label: sysNoteLabel(m.text), text: m.text || "",
                 time: (m.time && m.time.created) ? fmtTime(m.time.created) : "" });
      return;
    }
    if (m.type !== "assistant") return;
    const content = Array.isArray(m.content) ? m.content : [];
    for (const p of content) {
      if (!p || typeof p !== "object") continue;
      if (p.type === "reasoning") {
        const t = stripModelWarning(p.text || "");
        if (t.trim()) out.push({ kind: "think", text: t, live: !!p.live });
      } else if (p.type === "tool") {
        out.push({ kind: "tool", name: p.name || "tool", state: p.state || {} });
      }
    }
  };
  for (const m of state.messages) pushMsg(m);
  for (const id of pendingLiveIds()) {            // 正在生成、还没落库的那一轮
    const e = state.live[id];
    if (!e) continue;
    const rt = stripModelWarning(joinBucket(e.reasoning || {}));
    if (rt.trim()) out.push({ kind: "think", text: rt, live: true });
    for (const t of (e.tools || [])) {
      out.push({ kind: "tool", name: t.name || "tool", state: { status: t.status, input: t.input } });
    }
  }
  out.forEach((it, i) => { it._i = i; });         // 稳定序号：折叠状态就靠它跨重渲染记住
  return out;
}

/** 系统通知在轨迹页里的短标题（原文太长，一条能顶十几行）。 */
function sysNoteLabel(text) {
  const t = String(text || "").trim();
  let m = t.match(/^The instructions from (.+?) changed/i);
  if (m) return "指令变更 · " + m[1].replace(/\\/g, "/").split("/").pop();
  m = t.match(/^Today's date is now:\s*(.+)/i);
  if (m) return "日期 · " + m[1].trim();
  if (/tool catalog has changed/i.test(t)) return "工具目录变更";
  if (/^The environment you are running in is now/i.test(t)) return "运行环境更新";
  const first = (t.split("\n")[0] || "").trim();
  return first.length > 46 ? first.slice(0, 46) + "…" : (first || "系统");
}

/* ---------------- 轨迹条目：把工具调用翻成人话 ----------------
 *  以前这里直接把 `state.input` 用 JSON 倒出来（一屏 `{"newString": "…\n…"}`），
 *  而且**输出一个字都不显示** —— OpenCode 的 `output` 恒为 null，真正的输出在 `state.content`。
 *  现在：① 一行中文摘要（运行命令 / 修改 xxx +12 −3 / 搜索「x」→ 6 处）；
 *        ② 把输出接上（shell stdout + exit、read 内容、grep 命中）；
 *        ③ `edit` 用 `metadata.files[].patch` 画**真 diff**（红绿行）；
 *        ④ 默认折叠，点开看详情，原始 JSON 另收一层。 */

/** 工具名（OpenCode）/ ACP ToolKind → 「动词 + 中文名」 */
const TOOL_META = {
  bash: ["exec", "运行命令"], shell: ["exec", "运行命令"], run: ["exec", "运行命令"],
  execute: ["exec", "运行命令"], command: ["exec", "运行命令"], terminal: ["exec", "运行命令"],
  "run command": ["exec", "运行命令"], exec_command: ["exec", "运行命令"],
  read: ["read", "读取文件"], "read file": ["read", "读取文件"], cat: ["read", "读取文件"],
  view: ["read", "读取文件"], read_file: ["read", "读取文件"],
  edit: ["edit", "修改文件"], "edit file": ["edit", "修改文件"], patch: ["edit", "修改文件"],
  multiedit: ["edit", "修改文件"], replace: ["edit", "修改文件"], edit_file: ["edit", "修改文件"],
  delete: ["edit", "删除"], move: ["edit", "移动 / 重命名"],
  write: ["write", "写入文件"], "write file": ["write", "写入文件"], create: ["write", "新建文件"],
  newfile: ["write", "新建文件"], write_file: ["write", "写入文件"],
  grep: ["search", "搜索内容"], search: ["search", "搜索内容"], find: ["search", "搜索内容"],
  ripgrep: ["search", "搜索内容"], codesearch: ["search", "搜索内容"],
  glob: ["glob", "查找文件"], ls: ["list", "列出目录"], list: ["list", "列出目录"],
  webfetch: ["fetch", "抓取网页"], fetch: ["fetch", "抓取网页"], web: ["fetch", "联网"],
  todo: ["plan", "更新计划"], todowrite: ["plan", "更新计划"], todoread: ["plan", "查看计划"],
  plan: ["plan", "更新计划"], task: ["other", "子任务"],
  think: ["other", "思考"], switch_mode: ["other", "切换模式"], other: ["other", "工具"],
};

/** 状态 → 中文（轨迹里那些 completed / running 也得是人话） */
const STATUS_ZH = { completed: "完成", running: "进行中", pending: "等待",
                    error: "失败", failed: "失败", cancelled: "已取消" };

/** 工具名 + ACP kind → `[动词, 中文名]`；认不出就用原始名字兜底（ACP 的 title 常是人话） */
function toolMeta(name, kind) {
  const k1 = String(name || "").trim().toLowerCase();
  const k2 = String(kind || "").trim().toLowerCase();
  return TOOL_META[k1] || TOOL_META[k2] || ["other", String(name || kind || "工具")];
}

/** 工具输出：OpenCode 在 `state.content`（分块数组），ACP 在 `state.output`（字符串） */
function toolOutput(st) {
  if (!st) return "";
  const c = st.content;
  if (typeof c === "string" && c) return c;
  if (Array.isArray(c)) {
    const t = c.filter((p) => p && typeof p.text === "string").map((p) => p.text).join("");
    if (t) return t;
  }
  const o = st.output;
  if (typeof o === "string") return o;
  if (o && typeof o === "object") return JSON.stringify(o, null, 2);
  return "";
}

/** 压成一行（命令 / 预览用） */
function oneLine(s, max) {
  const t = String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  const m = max || 90;
  return t.length > m ? t.slice(0, m) + "…" : t;
}

/** 只留文件名（行内摘要放不下完整路径） */
function shortPath(p) {
  const s = String(p || "").replace(/\\/g, "/");
  const i = s.lastIndexOf("/");
  return i < 0 ? s : s.slice(i + 1);
}

/** 字节数 → 人话 */
function fmtBytes(n) {
  const b = Number(n) || 0;
  if (b < 1024) return b + " B";
  if (b < 1048576) return (b / 1024).toFixed(1) + " KB";
  return (b / 1048576).toFixed(1) + " MB";
}

const TR_DIFF_MAX = 20000;      // diff 最多画这么多字符
const TR_OUT_MAX = 12000;       // 输出最多画这么多字符

/** unified diff（`metadata.files[].patch`）→ 逐行上色 */
function renderDiff(patch) {
  const lines = String(patch || "").split("\n");
  const out = [];
  for (const raw of lines) {
    if (/^(Index:|=+$)/.test(raw)) continue;                 // 头部噪音，不画
    let cls = "ctx";
    if (/^(\+\+\+|---)\s/.test(raw)) cls = "file";
    else if (raw.startsWith("@@")) cls = "hunk";
    else if (raw.startsWith("+")) cls = "add";
    else if (raw.startsWith("-")) cls = "del";
    out.push(`<span class="dl ${cls}">${raw ? esc(raw) : "&nbsp;"}</span>`);
  }
  return `<div class="tr-diff">${out.join("")}</div>`;
}

/** 折叠块外壳：摘要一行 + 点开看详情 + 原始 JSON 再收一层 */
function foldHtml(key, summary, body, rawInput, autoOpen) {
  const open = (autoOpen || TRACE_OPEN.has(key)) ? " open" : "";
  let raw = "";
  if (rawInput !== undefined && rawInput !== null) {
    const js = typeof rawInput === "string" ? rawInput : JSON.stringify(rawInput, null, 2);
    raw = `<details class="tr-raw"><summary>原始数据</summary>
      <pre class="tr-pre">${esc(String(js).slice(0, 8000))}</pre></details>`;
  }
  return `<div class="tr-fold-wrap">
    <details class="tr-fold" data-k="${esc(key)}"${open}>
      <summary class="tr-sum">${summary}</summary>
      ${body || ""}${raw}
    </details>
  </div>`;
}

/** 思考：摘要给「首行预览 + 字数」，点开看全文 */
function traceThinkHtml(it) {
  const t = String(it.text || "");
  const sum = `<span class="tag${it.live ? " thinking" : ""}">${it.live ? "思考中…" : "思考"}</span>
    <span class="tr-preview">${esc(oneLine(t, 110))}</span>
    <span class="tr-meta">${t.length} 字</span>`;
  return `<div class="tr-item tr-think">${foldHtml(
    "k" + it._i, sum, `<div class="tr-body">${esc(t.slice(0, TR_OUT_MAX))}</div>`, null, it.live)}</div>`;
}

/** 工具：一行中文摘要 + 真输出 / 真 diff；出错自动展开 */
function traceToolHtml(it) {
  const st = (it.state && typeof it.state === "object") ? it.state : {};
  const input = (st.input && typeof st.input === "object") ? st.input : {};
  const md = (st.metadata && typeof st.metadata === "object") ? st.metadata : {};
  const [verb, zhName] = toolMeta(it.name, st.kind || it.kind);
  const status = String(st.status || "pending");
  const bad = status === "error" || status === "failed";
  const cls = status === "completed" ? "ok" : bad ? "err" : "run";
  const out = toolOutput(st);

  let obj = "", meta = "", body = "";
  const pre = (s, c) => `<pre class="${c || "tr-pre"}">${esc(String(s || "").slice(0, TR_OUT_MAX))}</pre>`;

  if (verb === "exec") {
    const cmd = String(input.command || input.cmd || input.script || "");
    obj = oneLine(cmd, 70) || (it.name || "");
    if (md.exit !== undefined && md.exit !== null) meta = "exit " + md.exit;
    body = (cmd ? pre(cmd) : "") + (out ? pre(out, "tr-out") : "");
  } else if (verb === "read") {
    const p = input.path || input.filePath || input.file || "";
    obj = shortPath(p) || oneLine(it.name, 40);
    const off = Number(input.offset || 0), lim = Number(input.limit || 0);
    if (lim) meta = `${off ? off + "–" : "1–"}${off + lim - 1} 行`;
    else if (md.truncated) meta = "已截断";
    body = (p ? `<div class="tr-file">${esc(p)}</div>` : "") + (out ? pre(out, "tr-out") : "");
  } else if (verb === "edit" || verb === "write") {
    const files = Array.isArray(md.files) ? md.files : null;
    const list = (files && files.length) ? files
      : [{ file: input.path || input.filePath || "", additions: 0, deletions: 0, patch: "" }];
    const add = list.reduce((a, f) => a + (Number(f && f.additions) || 0), 0);
    const del = list.reduce((a, f) => a + (Number(f && f.deletions) || 0), 0);
    obj = list.length === 1 ? shortPath(list[0].file) : list.length + " 个文件";
    meta = (add || del) ? `+${add} −${del}` : (verb === "write" ? "新建" : "");
    body = list.map((f) => {
      const head = `<div class="tr-file">${esc((f && f.file) || "(未知文件)")}`
        + `<span class="tr-add">+${Number((f && f.additions) || 0)}</span>`
        + `<span class="tr-del">−${Number((f && f.deletions) || 0)}</span></div>`;
      if (f && f.patch) return head + renderDiff(String(f.patch).slice(0, TR_DIFF_MAX));
      if (verb === "write" && input.content) return head + pre(input.content);
      return head + pre(JSON.stringify({ oldString: input.oldString, newString: input.newString }, null, 2));
    }).join("");
  } else if (verb === "search") {
    const pat = input.pattern || input.query || "";
    obj = pat ? `「${oneLine(pat, 40)}」` : oneLine(it.name, 40);
    if (input.path) obj += " · " + shortPath(input.path);
    if (md.matches !== undefined) meta = md.matches + " 处命中";
    body = out ? pre(out, "tr-out") : "";
  } else if (verb === "glob" || verb === "list") {
    obj = oneLine(input.pattern || input.path || it.name, 60);
    const n = out ? out.split("\n").filter((l) => l.trim()).length : 0;
    if (n) meta = n + " 项";
    body = out ? pre(out, "tr-out") : "";
  } else if (verb === "fetch") {
    obj = oneLine(String(input.url || "").replace(/^https?:\/\//, ""), 60) || oneLine(it.name, 40);
    body = out ? pre(out, "tr-out") : "";
  } else if (verb === "plan") {
    const todos = Array.isArray(input.todos) ? input.todos : null;
    obj = todos ? todos.length + " 项" : oneLine(it.name, 40);
    body = pre(JSON.stringify(todos || input, null, 2));
  } else {
    obj = oneLine(it.name, 50);
    const anyIn = Object.keys(input).length ? JSON.stringify(input, null, 2) : "";
    body = (anyIn ? pre(anyIn) : "") + (out ? pre(out, "tr-out") : "");
  }

  const sum = `<span class="tag ${cls}">${esc(STATUS_ZH[status] || status)}</span>
    <span class="tr-zh">${esc(zhName)}</span>
    ${obj ? `<span class="tr-obj" title="${esc(obj)}">${esc(obj)}</span>` : ""}
    ${meta ? `<span class="tr-meta">${esc(meta)}</span>` : ""}`;
  return `<div class="tr-item tr-tool${bad ? " err" : ""}">${foldHtml("k" + it._i, sum, body, input, bad)}</div>`;
}

/** 系统通知：也折叠（标题已经说清是什么了，diff 是细节），跟工具/思考一个观感 */
function traceNoteHtml(it) {
  const sum = `<span class="tag">系统</span><b>${esc(it.label)}</b>`
    + `${it.time ? `<span class="tr-meta">${esc(it.time)}</span>` : ""}`;
  return `<div class="tr-item tr-note">${foldHtml(
    "k" + it._i, sum, `<div class="tr-body">${esc(String(it.text || "").slice(0, 20000))}</div>`, null, false)}</div>`;
}

function traceItemHtml(it) {
  if (it.kind === "think") return traceThinkHtml(it);
  if (it.kind === "note") return traceNoteHtml(it);
  return traceToolHtml(it);
}

/* ---------------- ★主界面「本轮活动卡」：会话里的进展也要看得见 ----------------
   和群聊 C1 那张同款：**一张卡**把"这一轮它都干了什么"列出来（一行一步、中文人话），
   点某一步能看命令原文 / stdout / **真 diff**（直接复用 `traceToolHtml` 那套）。
   设计取舍：
   · **只收工具**（思考已经有「思考 Ns」芯片了，再列一遍太吵）；**没有工具的那一轮不出现卡片**；
   · 卡片**贴在这一轮最后一条消息之后**（所以干活时它就长在消息流末尾，做完原地留着 —— 用户要"保持展开"）；
   · 折叠状态用**独立 key 前缀**（`tc<轮序号>-`），别和轨迹页的 `k<i>` 撞（不然一处展开两处都展开）。 */
function turnCards() {
  const msgs = state.messages || [];
  const out = [];
  let cur = null;
  const startTurn = (t) => {
    cur = { start: t || null, end: null, items: [], afterId: null, live: false };
    out.push(cur);
  };
  for (const m of msgs) {
    if (!m) continue;
    if (m.type === "user") { startTurn(((m.time || {}).created) || null); continue; }
    if (!cur) startTurn(((m.time || {}).created) || null);   // 会话以助手开头（导入 / 接续）的兜底
    if (m.type !== "assistant") continue;
    const cn = Array.isArray(m.content) ? m.content : [];
    for (const p of cn) {
      if (p && p.type === "tool") cur.items.push({ kind: "tool", name: p.name || "tool", state: p.state || {} });
    }
    if (cn.length) cur.afterId = m.id;
    const t = m.time || {};
    if (t.completed) cur.end = t.completed;
  }
  // 正在生成、还没落库的那一轮 → 也来一张（贴 live 槽位末尾）
  const liveIds = pendingLiveIds();
  if (liveIds.length) {
    const g = { start: null, end: null, items: [], afterId: null, live: true };
    for (const id of liveIds) {
      const e = state.live[id];
      if (!e) continue;
      for (const t of (e.tools || [])) {
        g.items.push({ kind: "tool", name: t.name || "tool", state: { status: t.status, input: t.input } });
      }
    }
    if (state.pending.length) g.start = state.pending[state.pending.length - 1].time || null;
    out.push(g);
  }
  out.forEach((g, gi) => g.items.forEach((it, i) => { it._i = gi * 1000 + i; }));
  return out;
}

/** ★主界面「本轮进展」—— **就一行**（用户明确要"大概的进展，不要太详细"）。
 *  干活时：「● 正在执行 · 运行命令 npm test … · 第 3 步」；干完：「本轮 6 步 · 12s · 改了 app.js」。
 *  ⚠ **绝不列步骤** —— 一轮实测能有 **69 步**，一列就把消息区刷满；
 *    想看每一步的命令原文 / stdout / 真 diff，去「轨迹」页（那边是完整版）。 */
function turnLineHtml(g, idx) {
  if (!g || !g.items.length) return "";
  const n = g.items.length;
  const lastIt = g.items[n - 1];
  const st = (lastIt.state && typeof lastIt.state === "object") ? lastIt.state : {};
  const input = (st.input && typeof st.input === "object") ? st.input : {};
  const md = (st.metadata && typeof st.metadata === "object") ? st.metadata : {};
  const [verb, zhName] = toolMeta(lastIt.name, st.kind);
  // 一句话说清"正在"在哪件事上（命令首行 / 文件名 / 搜索词 / 网址）
  let what = "";
  if (verb === "exec") what = oneLine(input.command || input.cmd || input.script || "", 70);
  else if (verb === "edit" || verb === "write" || verb === "read") {
    const fl = Array.isArray(md.files) ? md.files : null;
    what = shortPath((fl && fl.length ? (fl[0] || {}).file : "") || input.path || input.filePath || input.file || "");
  } else if (verb === "search") what = input.pattern || input.query || "";
  else if (verb === "fetch") what = oneLine(String(input.url || "").replace(/^https?:\/\//, ""), 48);

  if (g.live) {
    return `<div class="turn-line live" data-tc="${idx}"><i class="tl-dot"></i>正在${esc(zhName)}`
      + (what ? ` <span class="tl-what" title="${esc(what)}">${esc(what)}</span>` : "")
      + `<span class="tl-n">第 ${n} 步</span></div>`;
  }
  const files = [];
  g.items.forEach((it) => {
    const m2 = (it.state && it.state.metadata) || {};
    (Array.isArray(m2.files) ? m2.files : []).forEach((f) => {
      const nm = shortPath((f && f.file) || "");
      if (nm && files.indexOf(nm) < 0) files.push(nm);
    });
  });
  const bits = [`本轮 ${n} 步`];
  const dur = (g.end && g.start) ? (g.end - g.start) / 1000 : 0;
  if (dur > 0.5) bits.push(fmtDur(dur));
  if (files.length) {
    bits.push("改了 " + files.slice(0, 3).join("、") + (files.length > 3 ? " 等 " + files.length + " 个" : ""));
  }
  return `<div class="turn-line" data-tc="${idx}">${esc(bits.join(" · "))}</div>`;
}

function renderTrace() {
  const box = $("trace-body");
  if (!box) return;
  if (!state.current) { box.innerHTML = `<div class="trace-empty">还没有会话。</div>`; return; }
  const all = traceItems();
  if (!all.length) {
    box.innerHTML = `<div class="trace-empty">这个会话还没有思考过程 / 工具调用 / 系统通知。<br>
      助手的思考、工具明细、以及"指令变更 / 日期 / 环境"这类系统通知都在这里；主界面只留「思考用时 + 正文」。</div>`;
    return;
  }
  const MAX = 250;                                  // 长会话保护：只画最近 N 条
  const items = all.length > MAX ? all.slice(-MAX) : all;
  const head = all.length > MAX
    ? `<div class="trace-empty">（只显示最近 ${MAX} 条，共 ${all.length} 条）</div>` : "";
  TRACE_KEYS = items.map((it) => "k" + it._i);
  box.innerHTML = head + items.map(traceItemHtml).join("");
  syncTraceAllBtn();
}

/** 「全部展开 / 全部收起」按钮的文案跟着实际状态走 */
function syncTraceAllBtn() {
  const btn = $("trace-all");
  if (!btn) return;
  const folds = document.querySelectorAll("#trace-body details.tr-fold");
  const opened = document.querySelectorAll("#trace-body details.tr-fold[open]");
  btn.textContent = (folds.length && folds.length === opened.length) ? "全部收起" : "全部展开";
}

/** 一键展开 / 收起全部条目 */
function traceToggleAll() {
  const box = $("trace-body");
  if (!box) return;
  const folds = [].slice.call(box.querySelectorAll("details.tr-fold"));
  if (!folds.length) return;
  const everyOpen = folds.every((d) => d.open);
  for (const d of folds) {
    d.open = !everyOpen;
    const k = d.dataset.k;
    if (k) { if (d.open) TRACE_OPEN.add(k); else TRACE_OPEN.delete(k); }
  }
  syncTraceAllBtn();
}

/** 记住用户手动展开 / 收起过哪几条（`details` 的 toggle 不冒泡 → 捕获监听） */
function initTraceFold() {
  const box = $("trace-body");
  if (!box) return;
  box.addEventListener("toggle", (e) => {
    const d = e.target;
    if (!d || !d.classList || !d.classList.contains("tr-fold")) return;
    const k = d.dataset.k;
    if (k) { if (d.open) TRACE_OPEN.add(k); else TRACE_OPEN.delete(k); }
    syncTraceAllBtn();
  }, true);
}

function traceOpen() {
  TRACE.open = true;
  const t = $("trace");
  if (t) { t.classList.add("open"); t.setAttribute("aria-hidden", "false"); }
  const b = $("btn-trace"); if (b) b.classList.add("on");
  renderTrace();
  const box = $("trace-body");
  if (box) box.scrollTop = box.scrollHeight;         // 打开时停在最新一条
  syncDrawerOverlap();
}
function traceClose() {
  TRACE.open = false;
  const t = $("trace");
  if (t) { t.classList.remove("open"); t.setAttribute("aria-hidden", "true"); }
  const b = $("btn-trace"); if (b) b.classList.remove("on");
  syncDrawerOverlap();
}
function toggleTrace() { if (TRACE.open) traceClose(); else traceOpen(); }

/** 右侧抽屉「重合」检测：**多个右抽屉同时开**、或**主内容被压得太窄**（说明抽屉已盖在内容上）
 *  → 给页面加一层雾化（`html.drawer-overlap`），一眼看出上面压着东西。 */
function syncDrawerOverlap() {
  const isOpen = (id) => { const el = $(id); return !!(el && el.classList.contains("open")); };
  const widthOf = (id) => { const el = $(id); return (el && el.getBoundingClientRect().width) || 0; };
  const widths = [];
  if (isOpen("cfg")) widths.push(widthOf("cfg"));
  if (isOpen("cocraft")) widths.push(widthOf("cocraft"));
  if (isOpen("code")) widths.push(widthOf("code"));
  if (isOpen("trace")) widths.push(widthOf("trace"));
  const side = $("sidebar");
  const colSide = (side && side.offsetParent !== null) ? side.getBoundingClientRect().width : 0;
  const overlap = widths.length > 1
    || (widths.length === 1 && (window.innerWidth - colSide - widths[0]) < 360);
  document.documentElement.classList.toggle("drawer-overlap", overlap);
}

function initTrace() {
  const b = $("btn-trace"); if (b) b.addEventListener("click", toggleTrace);
  const x = $("trace-close"); if (x) x.addEventListener("click", traceClose);
  const all = $("trace-all"); if (all) all.addEventListener("click", traceToggleAll);
  initTraceFold();
  initTraceResize();
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && TRACE.open) traceClose(); });
}

/** 轨迹面板的**右边缘**可拖宽（它是从左边界往右推的，所以拖的是右边）——
 *  轨迹里有真 diff 和真输出，560px 不够看；宽度记 `cocraft.traceWidth`。 */
function initTraceResize() {
  const grip = $("trace-resize"), box = $("trace");
  if (!grip || !box) return;
  try {
    const w = localStorage.getItem("cocraft.traceWidth");
    if (w) document.documentElement.style.setProperty("--trace-w", w);
  } catch { /* 隐私模式忽略 */ }
  let dragging = false;
  grip.addEventListener("pointerdown", (e) => {
    dragging = true;
    box.classList.add("resizing");
    try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const z = uiZoom();
    const left = box.getBoundingClientRect().left / z;                  // 左边界（= 侧栏宽）不动
    const max = Math.max(340, (window.innerWidth / z) - left - 220);    // 右边给主界面留 220px
    const w = Math.max(340, Math.min(max, (e.clientX / z) - left));
    document.documentElement.style.setProperty("--trace-w", w + "px");
    syncDrawerOverlap();
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false;
    box.classList.remove("resizing");
    try { localStorage.setItem("cocraft.traceWidth", document.documentElement.style.getPropertyValue("--trace-w")); } catch { /* ignore */ }
  };
  grip.addEventListener("pointerup", stop);
  grip.addEventListener("pointercancel", stop);
}

function renderMessages() {
  const pending = pendingLiveIds();

  TURNMAP = buildTurnMap();        // ★「思考 Ns」按整轮算时长（必须在拼 HTML 之前算好）
  const sSig = signatureOf(state.messages);
  if (sSig !== staticSig) {
    staticSig = sSig;
    // ★每一轮的「进展行」贴在该轮**最后一条消息之后**（一行，不列步骤）
    const cards = turnCards();
    const byAfter = new Map();
    cards.forEach((g, i) => { if (!g.live && g.afterId) byAfter.set(g.afterId, [g, i]); });
    staticHtml = state.messages.map((m) => {
      const h = renderMessage(m) || "";
      const c = byAfter.get(m.id);
      return c ? h + turnLineHtml(c[0], c[1]) : h;
    }).join("");
    heroShown = false;
    swapHtml($("msg-static"), staticHtml);
  }
  // ★"空会话"= 真的**没有任何可显示的东西** —— 消息可能全是系统注入通知 / idle（它们不渲染），
  //   那种情况该像新会话一样显示首屏，而不是留一片空白。
  const empty = !staticHtml && !pending.length;
  if (empty && !heroShown) { swapHtml($("msg-static"), heroHtml()); heroShown = true; }
  else if (!empty && heroShown) { swapHtml($("msg-static"), staticHtml); heroShown = false; }

  paintLiveSlot();
  if (TRACE.open) renderTrace();
  ctxSync();                       // ★上下文「还剩多少」跟着当前会话/最新回答走
  msgNavSchedule();                // ★消息导航（回到底部 / 问题列表）跟着重建
}

/** 只重建「生成中」那一小块：我发的消息（乐观回显）→ 思考 → 回答，一路向下生长 */
function paintLiveSlot() {
  const box = $("messages");
  const slot = $("live-slot");
  const ids = pendingLiveIds();

  // ★ 正在选中「生成中」那段文字时先别重绘：否则每帧重建会把选区冲掉（就没法选中复制片段了）
  if (selectionInLiveSlot()) return;

  const shellSig = state.pending.map((p) => `u:${p.id}`).join(",") + "||" + ids.map((id) => {
    const e = state.live[id];
    return `${id}|${e.tools.length}|${e.agent}|${e.model}`;
  }).join(";");

  if (shellSig !== liveShellSig) {
    liveShellSig = shellSig;
    const pendingHtml = state.pending
      .map((p) => renderMessage({
        id: p.id, type: "user", text: p.text, time: { created: p.time }, pending: true,
      }))
      .join("");
    const liveHtml = ids
      .map((id) => renderMessage(liveToMessage(id, state.live[id], state.reveal[id])))
      .join("");
    // ★「本轮进展」一行 —— 正在干活时它长在消息流末尾（干完就落进静态区，留一行小结）
    const cards = turnCards();
    const liveG = cards.length ? cards[cards.length - 1] : null;
    const liveLine = (liveG && liveG.live) ? turnLineHtml(liveG, cards.length - 1) : "";
    slot.innerHTML = pendingHtml + liveHtml + liveLine;
  }

  // 内容按打字机进度就位（只改内容、不动结构，光标闪烁不会被打断）
  slot.querySelectorAll("[data-live-text]").forEach((el) => {
    const e = state.live[el.dataset.msgid];
    if (!e) return;
    const r = state.reveal[el.dataset.msgid];
    el.innerHTML = renderText(stripModelWarning(joinBucket(e.text, r ? r.text : null)));
  });
  slot.querySelectorAll("[data-live-reasoning]").forEach((el) => {
    const e = state.live[el.dataset.msgid];
    if (!e) return;
    const r = state.reveal[el.dataset.msgid];
    el.textContent = joinBucket(e.reasoning, r ? r.reasoning : null);   // 向下生长，不内部滚动
  });

  if (state.follow) box.scrollTop = box.scrollHeight;
  msgNavSchedule();                // 新内容 / 打字机推进时同步「回到底部」的显隐
}

let rafId = null;

function startRevealLoop() {
  if (rafId) return;
  rafId = requestAnimationFrame(revealStep);
}

function revealStep() {
  rafId = null;
  let more = false;
  for (const [id, e] of Object.entries(state.live)) {
    const tgt = liveTotal(e);
    const r = state.reveal[id] || (state.reveal[id] = { text: 0, reasoning: 0 });
    for (const kind of ["text", "reasoning"]) {
      if (r[kind] < tgt[kind]) {
        const gap = tgt[kind] - r[kind];
        // 上限 12 字/帧：ACP 的思考是"很多个小块"、到达很快，没有上限会一次跳完（看着没打字）
        r[kind] = Math.min(tgt[kind], r[kind] + Math.max(2, Math.min(12, Math.ceil(gap / 6))));
        more = true;
      }
    }
  }
  paintLiveSlot();
  if (more) rafId = requestAnimationFrame(revealStep);
}

/** 事件到达时先立刻画一次（工具/结构变化可见），再启动打字机推进 */
function scheduleLiveRender() {
  paintLiveSlot();
  startRevealLoop();
  if (TRACE.open) renderTrace();          // 轨迹页跟着更新（事件驱动，不是每帧）
}

/* ---------------- 会话操作 ---------------- */

async function openSession(id) {
  try {
    state.current = unwrap(await api(`/api/session/${encodeURIComponent(id)}`));
  } catch {
    state.current = state.sessions.find((s) => s.id === id) || { id };
  }
  staticSig = null;
  liveShellSig = null;
  state.live = {};
  state.reveal = {};
  state.pending = [];
  state.attachments = [];
  state.follow = true;
  resetMsgPaging();               // 换会话：分页状态一并复位（否则会把上一个会话的游标带过来）
  renderAttachRow();
  $("session-title").textContent = state.current.title || "(无标题)";
  // 会话名下面不再显示 session id / 本地路径（详情请用侧栏会话项的 ▸ 展开）
  $("session-meta").textContent = "";
  updateModelBtn();                                  // 顶栏模型按钮跟着当前会话走
  renderModeButton();                                // 顶栏模式按钮跟着当前会话走
  ctxSync();                                         // 上下文条先按会话自带的数据显示（ACP 有 context）
  loadModes();                                       // ACP 的模式清单挂在会话上，所以拿到会话后再拉一次
  $("input").disabled = false;
  $("btn-send").disabled = false;
  $("btn-stop").disabled = false;
  renderSessions();
  await refreshMessages();
  repairStaleModel(state.current);   // 会话绑的模型下架了 → 提示 + 自动切默认（否则"发了没反应"）
  pollAsks();
}

/* ---------------- 消息分页：往上翻页读更早的历史 ----------------
 *  `/message` 一次只给一页（默认取最新那一页）。往上滚到顶时再取"更早一页"，
 *  拼到最前面，并把视口**锚在原处**（否则内容一多就会跳到别的地方）。 */

const MSG_PAGE = 80;            // 每页条数（首屏与翻页共用）
const MSG_TOP_TRIGGER = 120;    // 距顶多少像素内就自动取下一页
const MSG_MAX = 2000;           // 上翻的总上限（超长会话保护，别把内存撑爆）

/** 从响应里的 cursor 取出「更早一页」的游标。
 *  ⚠ 两个引擎形状不同：
 *    · OpenCode → `{previous, next}`（desc 排序下 `next` 才是更旧那一边）；
 *    · ACP      → 直接一个字符串（上一页最旧那条消息 id）。
 *  ⚠ OpenCode 的 `cursor.next` **到底了也不为空**（只剩 3 条也给），所以它只能当"翻页令牌"，
 *    绝不能拿它判"还有没有更早"——那条判据见 refreshMessages / loadHistory 里的 `MSG_PAGE`。 */
function msgCursorOf(cursor) {
  if (!cursor) return null;
  if (typeof cursor === "string") return cursor || null;
  if (typeof cursor === "object") return cursor.next || cursor.previous || null;
  return null;
}

/** 升序（旧 → 新）；接口返回是「新 → 旧」，不重排界面就会倒过来。 */
function sortByCreated(list) {
  return (list || []).slice().sort((a, b) =>
    ((((a || {}).time || {}).created) || 0) - ((((b || {}).time || {}).created) || 0));
}

/** 刷新时把"已经翻出来的更早几页"接回来（刷新只重取最新一页，别把历史丢掉）。
 *  判据：不在最新一页里，且时间比最新一页最旧那条还早。 */
function olderKept(page) {
  if (!state.msgOlder) return [];
  const ids = new Set(page.map((m) => m.id));
  const oldest = ((page[0] || {}).time || {}).created || 0;
  const out = [];
  for (const m of state.messages) {
    if (ids.has(m.id)) continue;
    if (!oldest || ((((m.time || {}).created) || 0) < oldest)) out.push(m);
  }
  return out;
}

/** 顶部那条提示 / 按钮：正在加载 / 还能往上翻 / 没有更早了 */
function renderHistoryHint() {
  const el = $("msg-more");
  if (!el) return;
  if (!state.current || !state.messages.length) { el.hidden = true; el.innerHTML = ""; return; }
  if (state.msgLoading) {
    el.hidden = false;
    el.innerHTML = `<span class="mm-hint">正在加载更早的消息…</span>`;
    return;
  }
  if (state.msgDone) {
    if (!state.msgOlder) { el.hidden = true; el.innerHTML = ""; return; }   // 本来就没往上翻过，不打扰
    el.hidden = false;
    el.innerHTML = `<span class="mm-hint">— 没有更早的消息了 —</span>`;
    return;
  }
  el.hidden = false;
  el.innerHTML = `<button type="button" class="mm-btn">↑ 加载更早的消息</button>`;
}

let histLastTop = 0;            // 上一次 scrollTop（只认"真的往上滚"，免得首屏渲染时误触发）

/** 复位分页状态（换会话 / 换引擎 / 换 agent 时都要调，否则会把旧游标带过去） */
function resetMsgPaging() {
  state.msgCursor = null;
  state.msgDone = true;
  state.msgLoading = false;
  state.msgOlder = false;
  histLastTop = 0;
  renderHistoryHint();
}

/** 往上翻一页：接更早的消息，并保持当前视口不动。 */
async function loadHistory() {
  if (!state.current || state.msgLoading || state.msgDone || !state.msgCursor) return;
  if (state.messages.length >= MSG_MAX) { state.msgDone = true; renderHistoryHint(); return; }
  const sid = state.current.id;
  state.msgLoading = true;
  state.follow = false;          // 用户在往上读历史 → 明确停止跟随（别被 paintLiveSlot 拽回底部）
  renderHistoryHint();
  const box = $("messages");
  const beforeTop = box ? box.scrollTop : 0;
  const beforeH = box ? box.scrollHeight : 0;
  try {
    const body = await api(`/api/session/${encodeURIComponent(sid)}/message`
      + `?limit=${MSG_PAGE}&cursor=${encodeURIComponent(state.msgCursor)}`);
    if (!state.current || state.current.id !== sid) return;      // 期间切了会话 → 丢弃这一页
    const older = sortByCreated(unwrap(body));
    const known = new Set(state.messages.map((m) => m.id));
    const fresh = older.filter((m) => !known.has(m.id));
    if (!fresh.length) {
      state.msgDone = true;                                     // 到底了（服务端回空 / 整页都是已有的）
    } else {
      state.messages = fresh.concat(state.messages);
      state.msgOlder = true;
      const next = msgCursorOf(body && body.cursor);
      // 页没填满（或游标没往前走）→ 说明没有更早的了
      if (next && next !== state.msgCursor && fresh.length >= MSG_PAGE) state.msgCursor = next;
      else state.msgDone = true;
      staticSig = null;                                         // 强制重建（renderMessages 里按签名比对）
      renderMessages();
      // ★锚定：把"新增的高度"补进 scrollTop，视口停在原来那条消息上
      const fix = () => {
        const b = $("messages");
        if (!b) return;
        b.scrollTop = beforeTop + (b.scrollHeight - beforeH);
      };
      fix();
      requestAnimationFrame(fix);                               // 字体/图片就位后高度还会变，再补一次
    }
  } catch (err) {
    setBanner("加载更早的消息失败：" + (err && err.message ? err.message : err));
  } finally {
    state.msgLoading = false;
    renderHistoryHint();
  }
}

/** 滚到接近顶部时自动翻页（只在"真的往上滚"时触发，见下面的 scroll 监听） */
function maybeLoadHistory() {
  const b = $("messages");
  if (!b || !state.current) return;
  if (state.msgDone || state.msgLoading || !state.msgCursor) return;
  if (b.scrollTop > MSG_TOP_TRIGGER) return;
  if (b.scrollHeight - b.clientHeight < 200) return;            // 内容还没超出一屏，翻了也没意义
  loadHistory();
}

async function refreshMessages() {
  if (!state.current) return;
  if (state.refreshing) { state.pendingRefresh = true; return; }
  state.refreshing = true;
  try {
    const body = await api(`/api/session/${encodeURIComponent(state.current.id)}/message?limit=${MSG_PAGE}`);
    // ⚠ 接口返回是「新 → 旧」（最新在索引 0），必须按 time.created 升序重排；
    //   少了这一步，界面就会变成「最新在顶、最旧在底」。
    const page = sortByCreated(unwrap(body));
    // 已经往上翻过：把更早那几页接回来（只重取最新一页，别把翻出来的历史丢掉）
    state.messages = olderKept(page).concat(page);
    if (!state.msgOlder) {
      // ★"有没有更早的"：不能看 cursor（OpenCode 到底了也不为空），要看**这一页有没有填满**
      //   （ACP 的 cursor 在到顶时会是 null，这里一并覆盖）。
      state.msgCursor = msgCursorOf(body && body.cursor);
      state.msgDone = !state.msgCursor || page.length < MSG_PAGE;
    }
    renderHistoryHint();
    // 对账：权威数据里已有的消息，就丢掉对应的生成缓冲
    const known = new Set(state.messages.map((m) => m.id));
    for (const id of Object.keys(state.live)) if (known.has(id)) delete state.live[id];
    for (const id of Object.keys(state.reveal)) if (known.has(id)) delete state.reveal[id];
    for (const id of Object.keys(state.frozen)) if (known.has(id)) delete state.frozen[id];
    // 已收尾、但权威数据里没有同 id 的「生成中」（id 换了）→ 丢掉，避免和权威消息重复显示
    for (const id of Object.keys(state.live)) {
      if (state.live[id] && state.live[id].done && !known.has(id)) {
        delete state.live[id];
        delete state.reveal[id];
      }
    }
    // 我发的消息：服务端已收录就撤掉本地回显（按出现次数配对，避免同文重复误删）
    if (state.pending.length) {
      const left = new Map();
      for (const m of state.messages) {
        if (m.type !== "user") continue;
        const t = (m.text || "").trim();
        left.set(t, (left.get(t) || 0) + 1);
      }
      state.pending = state.pending.filter((p) => {
        const n = left.get(p.text.trim()) || 0;
        if (n > 0) { left.set(p.text.trim(), n - 1); return false; }
        return true;
      });
      liveShellSig = null;
    }
    renderMessages();
  } catch (err) {
    // ⚠ 不要动 $("messages") 的 innerHTML —— 那会把 #msg-static / #live-slot 一起抹掉，
    //   之后每次渲染都拿不到容器，界面永久卡死。只覆盖内容区。
    const box = $("msg-static");
    if (box) box.innerHTML = `<div class="error-box">加载消息失败：${esc(err.message)}</div>`;
    staticSig = null;
  } finally {
    state.refreshing = false;
    if (state.pendingRefresh) { state.pendingRefresh = false; refreshMessages(); }
  }
}

/** 会话的"还没起名"默认标题（这些才算没名字，用户自己改过的一律不碰） */
const DEFAULT_TITLES = ["", "新会话", "(无标题)", "untitled", "new session", "新对话"];
const TITLE_MAX = 30;

/** 第一句提问 → 会话名：压平空白、去掉 markdown 前缀、截断 */
function titleFromQuestion(text) {
  let t = String(text || "").replace(/\s+/g, " ").trim();
  t = t.replace(/^[#>\-*・\s]+/, "");
  if (!t) return "";
  if (t.length > TITLE_MAX) t = t.slice(0, TITLE_MAX).trimEnd() + "…";
  return t;
}

/** 这个会话是否还"没名字"：标题是默认值，且里面还没有用户消息。
 *  两个条件都满足才自动命名 —— 已经聊过的会话不会被改名。 */
function shouldAutoName() {
  if (!state.current) return false;
  if (state.named[state.current.id]) return false;      // 本页已经给它命名过一次了
  const t = String(state.current.title || "").trim().toLowerCase();
  if (!DEFAULT_TITLES.includes(t)) return false;
  return !state.messages.some((m) => m.type === "user") && !state.pending.length;
}

/** 用第一句提问命名：PATCH /api/session/{id} {title} → 204。
 *  失败就算了（不影响发消息）—— 后端还有自己的命名逻辑，SSE 的 session.renamed 会再同步一次。 */
async function autoNameSession(text) {
  const title = titleFromQuestion(text);
  if (!title || !state.current) return;
  const id = state.current.id;
  try {
    await api(`/api/session/${encodeURIComponent(id)}`, {
      method: "PATCH", body: JSON.stringify({ title }),
    });
  } catch { return; }
  if (state.current && state.current.id === id) {
    state.current.title = title;
    $("session-title").textContent = title;
  }
  const s = state.sessions.find((x) => x.id === id);
  if (s) { s.title = title; renderSessions(); }
}

async function send() {
  const input = $("input");
  const typed = input.value.trim();
  const files = outgoingFiles();
  if ((!typed && !files.length) || !state.current) return;

  const atts = state.attachments.map((a) => ({
    name: a.name, mime: a.mime, size: a.size, dataUrl: a.dataUrl,
  }));
  const text = typed || `（附件：${atts.map((a) => a.name).join("、")}）`;
  // 这个会话还没名字的话，就用这句提问命名（用户改过名 / 已经聊过的，绝不覆盖）
  const firstAsk = shouldAutoName() ? text : "";
  if (firstAsk) state.named[state.current.id] = true;   // 记账：本页只命名一次，别依赖消息是否已回传

  input.value = "";
  input.style.height = "auto";
  state.attachments = [];
  renderAttachRow();

  // 先本地回显 —— 让「我的问题」（含附件缩略图）立刻出现，服务端确认后对账撤掉
  const localId = "local_" + Date.now().toString(36);
  state.pending.push({ id: localId, text, time: Date.now(), files: atts });
  state.follow = true;
  paintLiveSlot();

  try {
    await ensureSessionModel(state.current.id);       // ★ 没有模型先补上，别让这一轮空转
    // ★可逆执行：**动手前**给工作目录落一次快照（失败不影响发消息；热扫 ~10ms）
    await snapTurnStart(state.current.id, currentDir());
    await api(`/api/session/${encodeURIComponent(state.current.id)}/prompt`, {
      method: "POST",
      body: JSON.stringify(files.length ? { text, files } : { text }),
    }).then((r) => {
      const rr = (r && typeof r === "object" && r.data && typeof r.data === "object") ? r.data : r;
      if (rr && rr.queued) {
        setBanner(`已加入队列（第 ${rr.position || 1} 条）—— 这一轮结束后自动发送；想立刻用它，点「■ 中断」`, { ms: 7000 });
      }
    });
    if (firstAsk) await autoNameSession(firstAsk);
    await refreshMessages();
  } catch (err) {
    // 失败就把附件还回输入区，别让用户白选一次
    state.attachments = atts.map((a, i) => ({ id: `att_r${Date.now()}_${i}`, ...a }));
    renderAttachRow();
    state.pending = state.pending.filter((p) => p.id !== localId);
    liveShellSig = null;
    paintLiveSlot();
    const box = $("messages");
    box.insertAdjacentHTML("beforeend", `<div class="error-box">发送失败：${esc(err.message)}</div>`);
    box.scrollTop = box.scrollHeight;
  }
}

/* ---------------- 新建会话：选项目 / 新建项目 ---------------- */

const NS_DIR_KEY = "cocraft.newsession.dir";
const NS_BASE_KEY = "cocraft.newsession.base";

function lastNewSessionDir() {
  try { return localStorage.getItem(NS_DIR_KEY) || ""; } catch { return ""; }
}
function saveNewSessionDir(dir) {
  try {
    if (dir) localStorage.setItem(NS_DIR_KEY, dir);
    else localStorage.removeItem(NS_DIR_KEY);
  } catch { /* 隐私模式 */ }
}
function lastNewSessionBase() {
  try { return localStorage.getItem(NS_BASE_KEY) || ""; } catch { return ""; }
}
function saveNewSessionBase(dir) {
  try {
    if (dir) localStorage.setItem(NS_BASE_KEY, dir);
    else localStorage.removeItem(NS_BASE_KEY);
  } catch { /* 隐私模式 */ }
}

const NS_PROJECTS_KEY = "cocraft.newsession.projects";

/** 面板里「建过 / 选过」的项目目录（持久化）——即使还没有会话，也能在「项目」列表里看到。 */
function savedProjects() {
  try {
    const a = JSON.parse(localStorage.getItem(NS_PROJECTS_KEY) || "[]");
    return Array.isArray(a) ? a.filter((x) => x && typeof x.path === "string" && x.path) : [];
  } catch { return []; }
}
function rememberProject(dir, ts) {
  const d = String(dir || "").trim().replace(/[\\/]+$/, "");
  if (!d) return;
  const t = Number(ts) || Date.now();
  try {
    const list = savedProjects().filter((x) => normDir(x.path) !== normDir(d));
    list.unshift({ path: d, ts: t });
    localStorage.setItem(NS_PROJECTS_KEY, JSON.stringify(list.slice(0, 40)));
  } catch { /* 隐私模式 */ }
}

/** 去掉结尾分隔符；比较目录是否同一个（Windows 大小写不敏感 + 分隔符混用） */
function normDir(p) {
  return String(p || "").trim().replace(/[\\/]+$/, "").replace(/\//g, "\\").toLowerCase();
}

/** 看起来像系统临时目录？（提醒用户别把项目建在会被清理的地方） */
function looksTempPath(p) {
  const s = String(p || "").toLowerCase().replace(/\//g, "\\");
  return s.includes("\\appdata\\local\\temp") || s.endsWith("\\temp") || s.includes("\\windows\\temp");
}

/** 当前 agent 管理的「项目」= 该 agent 会话用过的工作目录（去重，按最近使用排序）。 */
function agentProjects() {
  const map = new Map();
  const add = (dir, upd) => {
    const d = String(dir || "").trim().replace(/[\\/]+$/, "");
    if (!d) return;
    const key = normDir(d);
    const prev = map.get(key);
    if (!prev || (upd || 0) > (prev.updated || 0)) {
      map.set(key, { path: d, name: projectNameOf(d), updated: upd || 0 });
    }
  };
  // ① 面板里「建过 / 选过」的项目（持久化）——即使还没有会话，也看得见
  savedProjects().forEach((p) => add(p.path, p.ts));
  // ② ACP 的会话列表已按当前 agent 过滤；OpenCode 一个 agent 管全部会话
  (state.sessions || []).forEach((s) =>
    add(s.location && s.location.directory, ((s.time || {}).updated) || ((s.time || {}).created)));
  if (state.current && state.current.location && state.current.location.directory) {
    add(state.current.location.directory, Date.now());
  }
  return Array.from(map.values()).sort((a, b) => (b.updated || 0) - (a.updated || 0));
}

/** 重画「项目」列表（单选）；末尾常驻「＋ 新建项目…」 */
function renderNewSessionProjects() {
  const box = $("ns-projects");
  if (!box) return;
  const cur = normDir($("ns-dir").value);
  const projs = agentProjects();
  const rows = projs.map((p) => {
    const on = normDir(p.path) === cur && cur !== "";
    return `<button type="button" class="ns-proj${on ? " on" : ""}" data-dir="${esc(p.path)}" title="${esc(p.path)}">`
      + `<span class="np-name">${esc(p.name || projectNameOf(p.path))}</span>`
      + `<span class="np-path mono">${esc(p.path)}</span></button>`;
  }).join("");
  const newOn = $("ns-newproj") && !$("ns-newproj").hidden;
  box.innerHTML = `<button type="button" class="ns-proj ns-new${newOn ? " on" : ""}" id="ns-proj-new">＋ 新建项目…</button>`
    + (rows || `<div class="muted ns-empty">还没有用过的项目目录（用上面的「新建项目」来一个）</div>`);
  box.querySelectorAll(".ns-proj[data-dir]").forEach((b) => {
    b.addEventListener("click", () => {
      $("ns-dir").value = b.dataset.dir;
      $("ns-newproj").hidden = true;
      $("ns-note").textContent = "";
      renderNewSessionProjects();
    });
  });
  const nb = $("ns-proj-new");
  if (nb) nb.addEventListener("click", () => {
    $("ns-newproj").hidden = !$("ns-newproj").hidden;
    renderNewSessionProjects();
    if (!$("ns-newproj").hidden) setTimeout(() => { try { $("ns-newname").focus(); } catch { /* ignore */ } }, 0);
  });
}

/** 「新建项目」的默认位置 = **对应 agent 的目录**：
 *   ACP → 当前 agent 的生效工作目录（后端 `/projectroot`；agent 没设就用共享基线 shared）；
 *   其它引擎（opencode）→ 当前会话的目录（= agent 正在工作的目录）；
 *   都没有再退回「上次用的位置」。 */
async function defaultProjectRoot() {
  const cur = (state.current && state.current.location && state.current.location.directory)
    || (state.sessions[0] && state.sessions[0].location && state.sessions[0].location.directory)
    || "";
  if (ENGINE.active === "acp") {
    try {
      const r = await api("/projectroot");
      if (r && r.root) return r.root;
    } catch { /* 拿不到就退回当前目录 */ }
  }
  return cur;
}

/** 点「＋ 新会话」→ 弹窗：选当前 agent 用过的项目，或新建一个长期项目 */
async function openNewSession() {
  const box = $("newsess");
  if (!box) return;
  const cur = (state.current && state.current.location
    && state.current.location.directory) || "";
  const dir = lastNewSessionDir() || cur;
  $("ns-dir").value = dir;
  $("ns-title").value = "";
  $("ns-newproj").hidden = true;
  $("ns-newname").value = "";
  renderNewSessionProjects();
  box.hidden = false;
  setTimeout(() => { try { $("ns-title").focus(); } catch { /* ignore */ } }, 0);
  // 默认「项目位置」要等后端回答（ACP 要问 /projectroot），先填上次的、拿到后再覆盖
  $("ns-newbase").value = lastNewSessionBase() || "";
  let root = "";
  try { root = await defaultProjectRoot(); } catch { root = ""; }
  const base = root || lastNewSessionBase() || "";
  $("ns-newbase").value = base;
  if (root) $("ns-note").textContent = "新项目默认建在 agent 目录：" + root + "（可改）";
  else if (base) $("ns-note").textContent = "已沿用上次的项目位置：" + base + "（可改）";
  else if (dir) $("ns-note").textContent = "已沿用上次的目录（可改；清空 = 引擎默认）";
  else $("ns-note").textContent = "";
}

function closeNewSession() { const b = $("newsess"); if (b) b.hidden = true; }

/** 「选择…」→ 原生文件夹框（POST /pick/folder） */
async function pickNewSessionDir() {
  try {
    const r = await api("/pick/folder", { method: "POST" });
    if (r && r.ok && r.path) { $("ns-dir").value = r.path; $("ns-note").textContent = ""; renderNewSessionProjects(); }
    else if (r && r.canceled) { /* 用户取消，什么都不做 */ }
    else setBanner("选择目录失败：" + ((r && r.error) || "未知错误"));
  } catch (err) {
    setBanner("选择目录失败：" + (err && err.message ? err.message : err));
  }
}

/** 「选择位置…」→ 原生文件夹框：给「新建项目」选父目录 */
async function pickNewProjectBase() {
  try {
    const r = await api("/pick/folder", { method: "POST" });
    if (r && r.ok && r.path) {
      $("ns-newbase").value = r.path;
      $("ns-note").textContent = looksTempPath(r.path)
        ? "⚠ 这个位置像是临时目录（可能被系统清理）——建议换到文档 / 盘符下，项目才能长期保留"
        : "";
    } else if (r && r.canceled) { /* 取消 */ }
    else setBanner("选择位置失败：" + ((r && r.error) || "未知错误"));
  } catch (err) {
    setBanner("选择位置失败：" + (err && err.message ? err.message : err));
  }
}

/** 「创建项目」→ 在磁盘上真正建目录（POST /mkdir），成功后选中它 */
async function createProjectDir() {
  const base = ($("ns-newbase").value || "").trim();
  const name = ($("ns-newname").value || "").trim();
  const note = $("ns-note");
  if (!base) { note.textContent = "请先选「项目位置」（一个长期保存的父目录）"; try { $("ns-newbase").focus(); } catch { /* ignore */ } return; }
  if (!name) { note.textContent = "请输入项目名"; try { $("ns-newname").focus(); } catch { /* ignore */ } return; }
  const btn = $("ns-newname-create");
  if (btn) btn.disabled = true;
  note.textContent = "创建中…";
  try {
    const r = await api("/mkdir", { method: "POST", body: JSON.stringify({ base, name }) });
    if (!r || !r.ok || !r.path) throw new Error((r && r.error) || "未知错误");
    $("ns-dir").value = r.path;
    saveNewSessionBase(base);
    rememberProject(r.path);
    $("ns-newproj").hidden = true;
    const temp = looksTempPath(r.path)
      ? "；⚠ 这个位置是临时目录，可能不长期保留" : "";
    note.textContent = (r.existed ? "项目已存在，已选中：" : "已创建项目：") + r.path + temp;
    renderNewSessionProjects();
    try { $("ns-title").focus(); } catch { /* ignore */ }
  } catch (err) {
    note.textContent = "创建项目失败：" + (err && err.message ? err.message : err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 用指定目录/标题建会话；目录不可用时兜底用引擎默认目录（并说明）。 */
async function createSessionWith(dir, title) {
  const def = await resolveDefaultModel();          // ★ 保证新会话一定有模型
  const base = { title: title || "新会话" };
  if (def) base.model = def;
  const withLoc = dir ? Object.assign({}, base, { location: { directory: dir } }) : base;
  try {
    return unwrap(await api("/api/session", { method: "POST", body: JSON.stringify(withLoc) }));
  } catch (err) {
    if (!dir) throw err;
    const created = unwrap(await api("/api/session", { method: "POST", body: JSON.stringify(base) }));
    setBanner(`工作目录不可用（${dir}），已改用引擎默认目录建会话`);
    return created;
  }
}

async function doCreateSession() {
  const dir = ($("ns-dir").value || "").trim();
  const title = ($("ns-title").value || "").trim();
  const btn = $("ns-create");
  if (btn) btn.disabled = true;
  $("ns-note").textContent = "创建中…";
  try {
    const created = await createSessionWith(dir, title);
    if (!created || !created.id) throw new Error("没拿到会话 id");
    saveNewSessionDir(dir);
    if (dir) rememberProject(dir);
    closeNewSession();
    await loadSessions();
    await openSession(created.id);
    await ensureSessionModel(created.id);           // ★ 双保险：确保已绑定模型
    await ensureSessionMode(created.id);            // ★ 同理：应用"新会话默认模式"
  } catch (err) {
    $("ns-note").textContent = "创建失败：" + (err && err.message ? err.message : err);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** 中断后立刻收起「生成中」气泡，别干等服务端终态事件（否则界面像卡住没停）。 */
function stopLive(sid) {
  if (!state.current || state.current.id !== sid) return;
  // 冻结这一轮：即便还有在途的迟到增量，也不再往界面上画
  for (const id of Object.keys(state.live)) state.frozen[id] = true;
  state.live = {};
  state.reveal = {};
  liveShellSig = null;
  if (rafId) { cancelAnimationFrame(rafId); rafId = null; }   // 停掉打字机
  renderMessages();
}

async function interrupt() {
  if (!state.current) return;
  const sid = state.current.id;
  const btn = $("btn-stop");
  if (btn) btn.disabled = true;
  try {
    await api(`/api/session/${encodeURIComponent(sid)}/interrupt`,
      { method: "POST", body: "{}" });
    // 乐观停止：先把「生成中」收掉、给个提示，再对账一次权威消息。
    // 若这一轮其实已经正常结束（竞态），刷新会把落库的消息显示回来，不丢内容。
    stopLive(sid);
    setBanner("已请求中断…");
    setTimeout(() => refreshMessages(), 400);
    setTimeout(() => { refreshMessages(); setBanner(""); }, 1400);
  } catch (err) {
    alert("中断失败：" + err.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* ---------------- 实时事件流（增量驱动）---------------- */

let es = null;
let refreshTimer = null;
let sessionsTimer = null;

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; refreshMessages(); }, 300);
}

function scheduleSessions() {
  if (sessionsTimer) return;
  sessionsTimer = setTimeout(() => { sessionsTimer = null; loadSessions(); }, 1200);
}

/** 终态：这些事件之后才值得去取一次权威数据 */
const TERMINAL = /^session\.(text\.ended|step\.ended|step\.streamed|step\.failed|tool\.success|tool\.error|execution\.succeeded|execution\.failed|execution\.interrupted|inbox\.delivered|inbox\.enqueued|created|renamed)$/;

function handleEvent(p) {
  const t = p.type || "";
  const d = p.data || {};
  const sid = d.sessionID || (d.session && d.session.id) || null;
  // ⚠ 没选中会话时（state.current == null）绝不能把「带会话 ID 的事件」当成本会话：
  //   否则切到 acp 后停在"创建新会话"空状态时，后台会话的增量会被画到空状态下面。
  const mine = !sid || (state.current && sid === state.current.id);

  if (t === "server.connected") { setConn("online", "已连接"); return; }

  // 需要你选择 / 权限请求：SSE 一到就立刻去看，不用等轮询
  if (t.startsWith("form.") || t.startsWith("permission.")) { pollAsks(); return; }

  // ★上下文窗口占用（ACP 的 `usage_update`）：agent 每轮上报 used/size → 更新会话 + 小条
  //   ⚠ 这是**上下文**，不是账单 token（账单累计在 session.tokens 里）。
  if (t === "session.usage.update") {
    if (mine && state.current) {
      state.current.context = { used: Number(d.used) || 0, size: Number(d.size) || 0,
                                estimated: !!d.estimated };
      ctxSync();
    }
    return;
  }

  // ① 文本 / 思考增量 —— 直接追加，绝不重取
  if (t === "session.text.delta" || t === "session.reasoning.delta") {
    if (!mine || !d.assistantMessageID) return;
    if (state.frozen[d.assistantMessageID]) return;    // 用户已中止这一轮：丢弃迟到的增量
    const e = liveEnsure(d.assistantMessageID);
    const bucket = t === "session.text.delta" ? e.text : e.reasoning;
    const k = d.ordinal || 0;
    bucket[k] = (bucket[k] || "") + (d.delta || "");
    scheduleLiveRender();
    return;
  }

  // ①b 本轮出错（ACP 引擎在 prompt 失败时发；以前前端不处理 → "出错了却一片安静"）
  if (t === "session.error") {
    const msg = (d && d.error) || "未知错误";
    if (mine) {
      setBanner((d && d.modelUnavailable ? "模型问题：" : "本轮出错：") + msg
        + "（详见 runtime/logs/acp.log）");
      scheduleRefresh();
      if (d && d.modelUnavailable) refreshCurrentModel();   // 后端可能已自动换模型 → 同步按钮
    }
    return;
  }

  // ①c ★上下文压缩（OpenCode 的 `session.compaction.*`）：以前只有兜底的"刷会话列表"，
  //     消息区看不到任何变化。`delta` 是摘要流（很多条）→ 不重取；started/ended/failed 才值得对账。
  if (t.startsWith("session.compaction.")) {
    if (!mine) return;
    if (t === "session.compaction.delta") return;
    if (t === "session.compaction.failed") {
      setBanner("上下文压缩失败：" + (textOfError(d.error) || "未知原因"), { ms: 12000 });
    } else if (t === "session.compaction.started") {
      setBanner("正在压缩上下文…", { ms: 60000 });
    }
    scheduleRefresh();
    return;
  }

  // ② 步骤开始：记录 agent / model，让「生成中」气泡有身份
  if (t === "session.step.started" && mine && d.assistantMessageID) {
    if (state.frozen[d.assistantMessageID]) return;
    const e = liveEnsure(d.assistantMessageID);
    e.agent = d.agent || e.agent;
    e.model = (d.model && d.model.id) || e.model;
    e.started = d.started || Date.now();
    scheduleLiveRender();
    return;
  }

  // ③ 工具调用
  if (t.startsWith("session.tool.") && mine && d.assistantMessageID) {
    if (state.frozen[d.assistantMessageID]) return;
    const e = liveEnsure(d.assistantMessageID);
    if (t === "session.tool.called") {
      e.tools.push({ name: d.name || d.tool || "tool", input: d.input, status: "running" });
    } else if (t === "session.tool.success" || t === "session.tool.error") {
      const last = e.tools[e.tools.length - 1];
      if (last) last.status = t === "session.tool.success" ? "completed" : "error";
    }
    scheduleLiveRender();
    return;
  }

  // ③′ 中断：立刻收起「生成中」气泡。
  //    服务端被中断时未必落库"半截"助手消息，只等终态刷新的话，
  //    界面会一直停在「思考中」——看起来就像没停下来。
  if (t === "session.execution.interrupted") {
    if (mine) { stopLive(state.current.id); scheduleRefresh(); }
    return;
  }

  // ④ 终态：取权威数据对账
  if (TERMINAL.test(t)) {
    if (mine) {
      // 这一轮的「生成中」标记为已结束 —— 刷新后若权威数据里没有同 id（opencode-acp 读库后 id 变了），就丢掉，别重复
      if (d && d.assistantMessageID && state.live[d.assistantMessageID]) {
        state.live[d.assistantMessageID].done = true;
      }
      scheduleRefresh();
    }
    // 本轮失败：常见原因是会话绑的模型被下架（ModelUnavailableError）→ 体检一下并说清楚
    if (mine && (t === "session.step.failed" || t === "session.execution.failed")) {
      repairStaleModel(state.current);
    }
    if (t === "session.created" || t === "session.renamed"
        || t === "session.execution.succeeded" || t === "session.inbox.enqueued") {
      scheduleSessions();
    }
    return;
  }

  // ⑤ 其它 session.* 事件：只刷会话列表（不重取消息，避免洪泛）
  if (t.startsWith("session.") && !t.startsWith("shell.")) scheduleSessions();
}

function liveEnsure(id) {
  if (!state.live[id]) {
    state.live[id] = { text: {}, reasoning: {}, tools: [], agent: "", model: "", started: 0 };
  }
  return state.live[id];
}

function connectEvents() {
  if (es) es.close();
  es = new EventSource("/api/event");
  es.onopen = () => setConn("online", "已连接");
  es.onerror = () => setConn("error", "连接断开，重连中…");
  es.onmessage = (ev) => {
    let p = null;
    try { p = JSON.parse(ev.data); } catch { return; }
    try { handleEvent(p); } catch (err) { console.warn("事件处理异常", err, p); }
  };
}

function setConn(cls, text) {
  $("conn").className = "dot " + cls;
  $("conn-text").textContent = text;
}

/* ---------------- 连接 OpenCode：自动重试，永不要求手动刷新 ---------------- */

let connected = false;
let reloading = false;          // 正在自动刷新（刷新期间不发自家的关窗信号）
let updateReady = "";           // 服务端有更新的前端版本（只提示，不自动刷新）

/** 服务端报告的前端版本和本页加载的不一致 → **只提示，不自动刷新**。
 *  ⚠ 实测：原生 WebView2 里，"窗口卡死(未响应)"多次都**紧跟自动 `location.reload()`** 之后；
 *    为稳，改成提示用户按 Ctrl+Shift+R（浏览器模式同样安全、只是多按一下）。 */
function syncUiVersion(served) {
  if (!served || !UI_VER || served === UI_VER) return false;
  updateReady = served;
  const v = $("ui-ver");
  if (v) {
    v.textContent = "ui " + UI_VER + " → " + served;
    v.classList.add("stale");
    v.title = "界面已更新到 " + served + "，点顶部横幅或按 Ctrl+Shift+R 刷新";
  }
  // ★ 走通知聚合器（优先级 50）：权限申请 / @我 在时**不会**被这条顶掉
  NOTIFY.set("ui-update", {
    priority: 50,
    text: `界面已更新到 ${served}：点这里刷新（或 Ctrl+Shift+R）`,
    onClick: safeReload,
  });
  return false;
}

/** 可靠刷新整个界面。
 *  ⚠ 原生（WebView2）窗口里 Ctrl+Shift+R 不一定会触发重新加载 → 必须有应用内入口。
 *  刷新期间置 reloading=true，跳过 /bye（否则守护进程会以为面板关了）。 */
function safeReload() {
  try { reloading = true; } catch { /* ignore */ }
  const el = $("banner");
  if (el) { el.textContent = "正在刷新界面…"; el.hidden = false; }
  const url = location.pathname + "?r=" + Date.now();   // 加个查询串彻底绕开缓存
  setTimeout(() => {
    try { location.replace(url); } catch { try { location.reload(); } catch { /* ignore */ } }
  }, 60);
}

let bannerTimer = null;
let bannerBase = { text: "", opts: {} };   // "普通"广播条（底）：被 NOTIFY 里更高优先级的项压住
let bannerAction = null;                   // 当前广播条的点击动作（点横幅时执行）

/** ★ 通知聚合器（`ui 0.1.67`）。
 *  ⚠ 横幅**同时只能显示一条** → "谁该显示"必须由优先级决定，否则会互相顶掉
 *  （权限申请 / @我 / 更新提示 / 连接状态 都会想说话）。优先级：权限 100 > @我 80 > 更新 50 > 普通 0。 */
const NOTIFY = {
  items: {},
  seq: 0,
  /** 放一条（同 key 覆盖）。`text` 为空视为不显示。 */
  set(key, item) {
    if (!key) return;
    this.items[key] = Object.assign({ key, priority: 0, sticky: true, seq: ++this.seq }, item || {});
    renderBanner();
  },
  clear(key) {
    if (this.items[key]) { delete this.items[key]; renderBanner(); }
  },
  /** 当前该显示的那一条（优先级最高；同优先级取更晚来的）。 */
  top() {
    let best = null;
    for (const k in this.items) {
      const it = this.items[k];
      if (!it || !it.text) continue;
      if (!best || it.priority > best.priority
          || (it.priority === best.priority && it.seq > best.seq)) best = it;
    }
    return best;
  },
};

/** 底层：把一段文字真正画进 `#banner`。 */
function paintBanner(text, opts) {
  const el = $("banner");
  if (!el) return;
  const o = opts || {};
  const clickable = !!o.clickable && !!text;
  el.classList.toggle("clickable", clickable);
  clearTimeout(bannerTimer);
  bannerTimer = null;
  if (!text) { el.hidden = true; el.textContent = ""; bannerAction = null; return; }
  if (el.textContent !== text) el.textContent = text;
  el.hidden = false;
  bannerAction = clickable ? (o.onClick || null) : null;
  if (!o.sticky && !clickable) {
    bannerTimer = setTimeout(restoreBanner, Math.max(1200, o.ms || 5000));
  }
}

/** 决定"现在显示哪一条"：NOTIFY 的最高优先级项优先，否则普通广播条。 */
function renderBanner() {
  const top = NOTIFY.top();
  if (top) { paintBanner(top.text, { clickable: true, sticky: true, onClick: top.onClick }); return; }
  paintBanner(bannerBase.text, bannerBase.opts);
}

/** 顶部广播条（**调用方接口**，语义与以前一致）。
 *  `opts.clickable` = 可点（**配 `opts.onClick` 才有动作**）；`opts.sticky` = 常驻不自动消失；
 *  都不加 → **几秒后自己收起**（`opts.ms` 可调）。以前非点击的广播条会一直挂着，用户反馈过。 */
function setBanner(text, opts) {
  bannerBase = { text: text || "", opts: opts || {} };
  renderBanner();
}

/** 广播条自动收起后：NOTIFY 里还有东西就让它显示，否则清空。 */
function restoreBanner() {
  bannerBase = { text: "", opts: {} };
  renderBanner();
}

async function checkHealth() {
  try {
    const body = await api("/healthz");
    $("upstream").textContent = `OpenCode ${body.version || ""}`;
    // ★ 后端进程身份放 tooltip：改完后端后**悬停这里**就能确认"新代码加载了没有"
    //   （`ui` 是现读 index.html 的，看不出后端新不新 —— 2026-10-14 踩过）
    if (body.pid) {
      $("upstream").title = `后台服务 pid ${body.pid}｜启动于 ${body.startedAt || body.started || "?"}`
        + `（已运行 ${Math.round((body.uptime || 0) / 60)} 分钟）`;
    }
    syncUiVersion(body.ui);            // 服务端的前端版本变了就自动刷新
    return true;
  } catch {
    $("upstream").textContent = "";
    return false;
  }
}

async function ensureConnected() {
  const ok = await checkHealth();
  if (ok) {
    if (connected) return;
    connected = true;
    setConn("online", "已连接");
    setBanner("");            // 清掉"正在连接…"（更新提示由 NOTIFY 常驻，不受影响）
    await loadSessions();
    connectEvents();
    if (state.sessions.length) {
      if (state.current) await refreshMessages();
      else await openSession(state.sessions[0].id);
    }
    pollAsks();
    return;
  }
  if (connected) {
    connected = false;
    if (es) { es.close(); es = null; }
    setConn("error", "已断开");
    setBanner("与 OpenCode 的连接已断开，正在重连…（不需要手动刷新）", { sticky: true });
  } else {
    setConn("error", "连接中");
    setBanner("正在连接 OpenCode…（它可能还在启动，本页面会自动重连）", { sticky: true });
  }
}

async function connectionLoop() {
  await ensureConnected();
  setTimeout(connectionLoop, connected ? 5000 : 1500);
}

/** 心跳 + 关窗信号：守护进程靠这个判断面板是否还开着。不要删。 */
function startHeartbeat() {
  const beat = () => fetch("/heartbeat", { method: "POST", cache: "no-store" }).catch(() => {});
  beat();
  setInterval(beat, 4000);

  const bye = () => {
    if (reloading) return;          // 自动刷新不是"关窗"，别发关窗信号（否则守护进程可能杀掉 OpenCode）
    try {
      if (navigator.sendBeacon) { navigator.sendBeacon("/bye"); return; }
    } catch { /* 落到下面的 fetch */ }
    fetch("/bye", { method: "POST", keepalive: true, cache: "no-store" }).catch(() => {});
  };
  window.addEventListener("pagehide", bye);
  window.addEventListener("beforeunload", bye);

  // 页面卸载（刷新/关窗）时把终端会话也删掉，免得宿主进程一直留着。
  // ⚠ 只是**收起抽屉**不会触发这里（那是有意保留、下次秒开）。
  window.addEventListener("pagehide", () => {
    try {
      if (TERM && TERM.id) {
        fetch("/pty/" + TERM.id, { method: "DELETE", keepalive: true }).catch(() => {});
      }
    } catch { /* ignore */ }
  });
}

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
}

function closeThemes() { const b = $("themes"); if (b) b.hidden = true; }

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
const TITLE_FALLBACK = "cocraft · 絵梨衣";
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
  CC.actTimer = setInterval(ccActBeat, 500);
  ccActBeat();
}
function ccActStop() {
  if (CC.actTimer) { clearInterval(CC.actTimer); CC.actTimer = null; }
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
  if (CC.floorTimer) { clearInterval(CC.floorTimer); CC.floorTimer = null; }
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
    CC.floorTimer = setInterval(() => {
      const left = ccFloorLeft();
      if (!CC.floor || left === null || left <= 0) {
        clearInterval(CC.floorTimer); CC.floorTimer = null;
        if (CC.floor && CC.floor.deadline && left === 0) CC.floor = { holder_agent_id: null, deadline: null };
      }
      ccRenderRoomMeta();
    }, 1000);
  } else if (!need && CC.floorTimer) {
    clearInterval(CC.floorTimer); CC.floorTimer = null;
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
  if (on && !ccPermT) ccPermT = setInterval(() => ccPermRender(), 1000);   // 每秒刷倒计时，到点自动收
  if (!on && ccPermT) { clearInterval(ccPermT); ccPermT = null; }
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
  clearInterval(CC.wsPing); CC.wsPing = null;
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
    clearInterval(CC.wsPing);
    CC.wsPing = setInterval(() => { try { sock.send(JSON.stringify({ type: "ping" })); } catch { /* ignore */ } }, 25000);
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
    clearInterval(CC.wsPing); CC.wsPing = null;
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
  CC.poll = setInterval(() => {
    if (!CC.token || !cocraftIsOpen()) return;
    const wsUp = CC.wsock && CC.wsock.readyState === 1;
    if (wsUp) return;                     // WebSocket 正常 → 完全靠推送，不轮询
    ccLoadChats();
    if (CC.chatID) ccLoadMsgs(false);
  }, 30000);
}
function ccStopPoll() { clearInterval(CC.poll); CC.poll = null; }

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
  let depth = 0;
  window.addEventListener("dragenter", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    depth += 1;
    $("drop-veil").hidden = false;
  });
  window.addEventListener("dragover", (e) => {
    if (!isFileDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
  });
  window.addEventListener("dragleave", (e) => {
    if (!isFileDrag(e)) return;
    depth = Math.max(0, depth - 1);
    if (!depth) $("drop-veil").hidden = true;
  });
  window.addEventListener("drop", (e) => {
    if (!isFileDrag(e)) return;
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
  const playing = !!(MUSIC.on || MUSIC.panel);
  const want = lyricsWanted();
  const withLyrics = want && playing && MUSIC.lyrics.length > 0;
  const wasOn = document.documentElement.classList.contains("lyrics-on");
  document.documentElement.classList.toggle("lyrics-on", withLyrics);
  // ⚠ 只有"用户没明确关闭"时才在收起侧栏时把浮层放出来（否则又变成关不掉）
  document.documentElement.classList.toggle("collapsed-focus", collapsed && want);
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

  const onMove = (e) => {
    if (!drag || e.pointerId !== drag.id) return;
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
    drag = null;
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", onUp);
    window.removeEventListener("pointercancel", onUp);
    b.classList.remove("dragging");
    // ⚠ 不要先把 left 清成 auto（那会瞬移）；让 applyLyricBall 从"当前拖动位置"平滑过渡到贴边位置
    b.style.right = "auto";
    if (!moved) {                                      // 只是点了一下 = 开关歌词
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
  });
  b.addEventListener("pointerenter", () => {
    lbCancelIdle();
    if (LB.idle && !drag) { LB.idle = false; saveLyricBall(); applyLyricBall(); }
  });
  b.addEventListener("pointerleave", () => { if (!drag) lbScheduleIdle(); });
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

  setInterval(() => {
    updateSeek();
    if (document.documentElement.classList.contains("lyrics-on")) highlightLyrics();
  }, 250);
  buildVis($("vis-bar"), VIS_WEIGHT);                    // 收起侧栏时输入框上方那条先摆好柱子
  buildVis($("spec-left"), VIS_WEIGHT);                  // 会话区左右两侧的真频谱先摆好柱子
  buildVis($("spec-right"), VIS_WEIGHT);
  setInterval(drawSpectrum, 70);                         // 真频谱：70ms 一帧
  updatePlayModeButtons();

  pollMusic();
}

/* ---------------- 弹窗：需要你选择（表单 / 权限请求）----------------
 * 这两类交互原本只由官方客户端弹窗 —— 自建界面不实现的话 agent 会被静默卡住，
 * 而本项目还会最小化官方窗口，用户根本看不到。 */

const ASK = { form: null, perm: null, picks: {}, sig: "", banner: "" };

function askHide() {
  $("ask").hidden = true;
  ASK.form = null;
  ASK.perm = null;
  ASK.picks = {};
  ASK.sig = "";
}

function fieldHtml(f) {
  const head = `<div class="t">${esc(f.title || f.key)}${f.required ? " *" : ""}</div>`
    + (f.description ? `<div class="d">${esc(f.description)}</div>` : "");
  const k = esc(f.key);
  if (Array.isArray(f.options) && f.options.length) {
    const multi = f.type === "multiselect";
    const opts = f.options.map((o) => `<button type="button" class="ask-opt"`
      + ` data-key="${k}" data-value="${esc(o.value)}"${multi ? ' data-multi="1"' : ""}`
      + ` title="${esc(o.description || "")}">${esc(o.label)}</button>`).join("");
    // ★ custom=true：允许"自己说" —— 选项下面补一个文本框，填了就以它为准
    const custom = f.custom
      ? `<input type="text" class="ask-custom" data-key="${k}" data-custom="1"`
        + ` placeholder="自己说…（填这里就以它为准）">`
      : "";
    return `<div class="ask-field">${head}<div class="ask-opts">${opts}</div>${custom}</div>`;
  }
  if (f.type === "boolean") {
    return `<div class="ask-field">${head}<div class="ask-opts">`
      + `<button type="button" class="ask-opt" data-key="${k}" data-value="true">是</button>`
      + `<button type="button" class="ask-opt" data-key="${k}" data-value="false">否</button>`
      + `</div></div>`;
  }
  if (f.type === "external") {
    return `<div class="ask-field">${head}<a href="${esc(f.url)}" target="_blank" rel="noreferrer">打开链接</a></div>`;
  }
  const it = (f.type === "number" || f.type === "integer") ? "number" : "text";
  return `<div class="ask-field">${head}<input type="${it}" data-key="${k}"`
    + (f.placeholder ? ` placeholder="${esc(f.placeholder)}"` : "")
    + (f.default !== undefined && f.default !== null ? ` value="${esc(String(f.default))}"` : "")
    + `></div>`;
}

/** 把已选中的项重新标出来（重建 DOM 后恢复选择） */
function restorePicks() {
  const body = $("ask-body");
  for (const [key, val] of Object.entries(ASK.picks || {})) {
    const vals = Array.isArray(val) ? val : [val];
    for (const v of vals) {
      const el = body.querySelector(`.ask-opt[data-key="${CSS.escape(key)}"][data-value="${CSS.escape(String(v))}"]`);
      if (el) el.classList.add("sel");
    }
  }
}

function renderAsk(forms, perms) {
  const perm = perms[0] || null;
  const form = forms[0] || null;
  if (!perm && !form) { askHide(); return; }

  // ★ 同一条待处理项不重建 DOM：否则每次轮询（1.2s）都会重写 innerHTML，
  //   用户刚点的选择、输入的文字全被清掉，表现就是「能看见弹窗但选不中」。
  const sig = perm ? `p:${perm.id}` : `f:${form.id}`;
  if (sig === ASK.sig && !$("ask").hidden) {
    restorePicks();
    return;
  }
  ASK.sig = sig;
  ASK.picks = {};

  if (perm) {
    ASK.perm = perm;
    ASK.form = null;
    $("ask-kind").textContent = "权限请求";
    $("ask-title").textContent = perm.action || "需要授权";
    const res = (perm.resources || []).map((r) => `<li>${esc(r)}</li>`).join("");
    // 说明「为什么要这个权限」：优先用接口给的原因（ACP 的 _meta / OpenCode 的 message），
    // 没有就按 action + 涉及资源合成一段；再补「模型刚才在想什么」作为背景（任何引擎都适用）。
    let why = String(perm.message || "").trim();
    if (!why) {
      const list = (perm.resources || []).slice(0, 4).join("、");
      why = "请求权限：" + (perm.action || "工具调用") + (list ? "；涉及：" + list : "") + "。";
    }
    let metaTxt = "";
    if (perm.metadata && typeof perm.metadata === "object") {
      const ks = Object.keys(perm.metadata);
      if (ks.length) {
        metaTxt = ks.slice(0, 6).map((k) => {
          const v = perm.metadata[k];
          const s = (v && typeof v === "object") ? JSON.stringify(v) : String(v);
          return k + " = " + String(s || "").slice(0, 160);
        }).join("\n");
      }
    }
    const ctx = /背景（/.test(perm.detail || "") ? "" : liveContextSnippet(320);
    $("ask-body").innerHTML =
      `<div class="d">${esc(why)}</div>`
      + (perm.detail ? `<div class="d ask-why">${esc(perm.detail)}</div>` : "")
      + (metaTxt ? `<div class="d ask-why">${esc(metaTxt)}</div>` : "")
      + (ctx ? `<div class="d ask-why">背景（模型刚才的想法节选）：${esc(ctx)}</div>` : "")
      + (res ? `<ul class="ask-res">${res}</ul>` : "");
    $("ask-actions").innerHTML =
      `<span id="ask-msg" class="ask-msg"></span>`
      + `<button data-dec="reject" class="danger">拒绝</button>`
      + `<button data-dec="once">允许一次</button>`
      + `<button data-dec="always" class="primary">始终允许</button>`;
  } else {
    ASK.form = form;
    ASK.perm = null;
    $("ask-kind").textContent = "需要你选择";
    $("ask-title").textContent = form.title || "请选择";
    $("ask-body").innerHTML = (form.fields || []).filter((f) => !f.hidden).map(fieldHtml).join("");
    $("ask-actions").innerHTML = `<span id="ask-msg" class="ask-msg"></span>`
      + `<button id="ask-ignore">忽略</button>`
      + `<button id="ask-submit" class="primary">确定</button>`;
  }
  $("ask").hidden = false;
}

function coerce(f, v) {
  if (f.type === "boolean") return v === true || v === "true";
  if (f.type === "number" || f.type === "integer") return Number(Array.isArray(v) ? v[0] : v);
  if (f.type === "multiselect") return Array.isArray(v) ? v : [v];
  return Array.isArray(v) ? v[0] : v;
}

/** 按字段类型把界面上的选择收成 answer 对象；优先用 ASK.picks（DOM 重建也不丢） */
function collectAnswer(form) {
  const body = $("ask-body");
  const answer = {};
  for (const f of (form.fields || [])) {
    if (f.hidden) continue;
    // ★ "自己说"（custom）：文本框有内容就以它为准
    const cin = body.querySelector(`input.ask-custom[data-key="${CSS.escape(f.key)}"]`);
    if (cin && cin.value.trim() !== "") {
      const v = cin.value.trim();
      if (f.type === "multiselect") {
        const picked = Array.isArray(ASK.picks[f.key]) ? ASK.picks[f.key].slice() : [];
        picked.push(v);
        answer[f.key] = picked;
      } else {
        answer[f.key] = coerce(f, v);
      }
      continue;
    }
    const picked = ASK.picks[f.key];
    const hasPicked = Array.isArray(picked) ? picked.length > 0
      : (picked !== undefined && picked !== "");
    if (hasPicked) { answer[f.key] = coerce(f, picked); continue; }

    const inp = body.querySelector(`input[data-key="${CSS.escape(f.key)}"]`);
    if (inp) {
      const raw = inp.value;
      if (raw !== "") { answer[f.key] = coerce(f, raw); continue; }
    }
    if (f.default !== undefined && f.default !== null) answer[f.key] = coerce(f, f.default);
  }
  return answer;
}

/** 弹窗里回一句话；isErr=true 时标红，并同步到顶部横幅（弹窗被轮询关掉也看得到）。 */
function askMsg(text, isErr) {
  const msg = $("ask-msg");
  if (msg) { msg.textContent = text; msg.classList.toggle("err", !!isErr); }
  if (isErr) { ASK.banner = text; setBanner(text, { sticky: true }); }
  return text;
}

/** 清掉"因为 ask 提示"挂上的横幅（不碰连接状态等无关横幅）。 */
function askClearBanner() {
  if (ASK.banner) { ASK.banner = ""; setBanner(""); }
}

/** 提交表单。失败原因直接写在弹窗里（并同步到横幅，方便复制排查） */
async function submitAsk() {
  if (!ASK.form) { askMsg("内部错误：没有待提交的表单", true); return; }
  const form = ASK.form;
  const answer = collectAnswer(form);
  const missing = (form.fields || []).filter((f) => !f.hidden && f.required
    && (answer[f.key] === undefined || answer[f.key] === ""));
  if (missing.length) {
    askMsg(`还差：${missing.map((f) => f.title || f.key).join("、")}`, true);
    return;
  }
  askMsg("提交中…");
  const url = `/api/session/${encodeURIComponent(form.sessionID)}`
    + `/form/${encodeURIComponent(form.id)}/reply`;
  try {
    const res = await fetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answer }),
    });
    if (!res.ok) {
      const t = await res.text();
      if (res.status === 404 || res.status === 409) {      // 已失效 / 已结算
        askMsg(`该选择请求已失效（${res.status}），已刷新`, true);
        askHide();
        pollAsks();
        return;
      }
      askMsg(`提交失败 ${res.status}：${t.slice(0, 200)}`, true);
      return;
    }
    askClearBanner();
    askHide();
    pollAsks();
  } catch (err) {
    askMsg(`提交异常：${err.message}`, true);
  }
}

/** 忽略一个待处理的表单（DELETE → 取消，不回答）。失败会在弹窗 + 横幅提示。 */
async function ignoreForm() {
  if (!ASK.form) return;
  const form = ASK.form;
  try {
    const res = await fetch(`/api/session/${encodeURIComponent(form.sessionID)}`
      + `/form/${encodeURIComponent(form.id)}`, { method: "DELETE" });
    if (!res.ok && res.status !== 404) {
      const t = await res.text();
      askMsg(`忽略失败 ${res.status}：${t.slice(0, 160)}`, true);
      return;
    }
    setBanner("已忽略该请求");
    ASK.banner = "已忽略该请求";
    askHide();
    pollAsks();
  } catch (err) {
    askMsg(`忽略异常：${err.message}`, true);
  }
}

/* ---- 权限授予等级（通用分级；命中则自动放行、不弹窗、不需要理由） ---- */
const PERM = { level: "ask", levels: [
  { id: "ask",  label: "全部询问" },
  { id: "read", label: "只读自动" },
  { id: "edit", label: "读 + 改（限工作区内）" },
  { id: "exec", label: "读 + 改 + 执行（限工作区内）" },
  { id: "all",  label: "全部放行（含工作区外）" },
] };

function permLevelLabel(id) {
  const it = PERM.levels.find((x) => x.id === id);
  return it ? it.label : (id || "全部询问");
}
function permBtnSync() {
  const b = $("btn-perm");
  if (!b) return;
  b.textContent = "权限：" + permLevelLabel(PERM.level) + " ▾";
  b.dataset.level = PERM.level;
}
async function loadPermLevel() {
  try {
    const r = await api("/api/permission/level");
    if (r && r.level) PERM.level = r.level;
    if (r && Array.isArray(r.levels) && r.levels.length) PERM.levels = r.levels;
  } catch { /* ignore */ }
  permBtnSync();
}
async function setPermLevel(lv) {
  PERM.level = lv;
  permBtnSync();
  // ⚠ `api()` 直接把 options 交给 fetch —— body 必须是**字符串**（传对象会变成 "[object Object]"，
  //   后端读不到 level 会回落成 ask：等级切换看起来生效、其实没写进去）。
  try { await api("/api/permission/level", { method: "POST", body: JSON.stringify({ level: lv }) }); }
  catch { /* ignore */ }
}
function permMenuToggle() {
  const old = document.getElementById("perm-menu");
  if (old) { old.remove(); return; }
  const b = $("btn-perm");
  if (!b) return;
  const m = document.createElement("div");
  m.id = "perm-menu"; m.className = "perm-menu";
  m.innerHTML = PERM.levels.map((it) =>
    `<button type="button" data-lv="${esc(it.id)}" class="${it.id === PERM.level ? "on" : ""}">${esc(it.label)}</button>`
  ).join("") + `<button type="button" class="perm-more" data-trust-open="1">信任面板…</button>`;
  document.body.appendChild(m);
  const r = b.getBoundingClientRect();
  m.style.left = Math.max(8, r.left) + "px";
  m.style.top = Math.max(8, r.top - m.offsetHeight - 6) + "px";
  m.addEventListener("click", (e) => {
    const more = e.target.closest("button[data-trust-open]");
    if (more) { m.remove(); trustOpen(true); return; }
    const t = e.target.closest("button[data-lv]");
    if (!t) return;
    setPermLevel(t.dataset.lv);
    m.remove();
  });
  setTimeout(() => {
    const close = (ev) => {
      if (!m.contains(ev.target) && ev.target !== b) {
        m.remove(); document.removeEventListener("pointerdown", close, true);
      }
    };
    document.addEventListener("pointerdown", close, true);
  }, 0);
}
/* ---- ★信任面板（方向 3 第一块）：权限等级 / 待批准 / 最近「始终允许」 ----
   三条红线：① 只读 + 展示为主（换等级是**逐档**点，不给"一键全放行"）；
   ② 「谁批准了什么」要能追溯到**具体那条规则**（每条都能单独撤销）；
   ③ 撤销复用现有接口（`/api/permission/always`、`/api/permission/level`），不另造一套。 */
const TRUST = { open: false, busy: false, pend: [], pendErr: "",
                always: null, alwaysErr: "", group: null,
                quar: null, quarErr: "", snaps: null, snapsErr: "",
                permLog: null, permLogErr: "", acts: null, actsErr: "",
                dec: [],
                orch: null, orchErr: "" };

/** 列表数据一律过这一道：接口形状万一变了（对象 / null）也不会把面板整块打崩。 */
function trustList(x) { return Array.isArray(x) ? x : []; }

/** 秒 / 毫秒都认的时间格式化（审计记录里两种都出现过）。 */
function trustWhen(ts) {
  const n = Number(ts || 0);
  if (!n) return "";
  return fmtTime(n < 1e12 ? n * 1000 : n);
}

/** 活动状态 → 中文（与活动卡同口径）。 */
function trustActZh(st) {
  return {
    running: "进行中", completed: "完成", error: "失败",
    asking: "等待批准", allowed: "已允许", denied: "已拒绝", timeout: "超时",
  }[String(st || "")] || String(st || "");
}

function trustIsOpen() { const b = $("trust"); return !!b && !b.hidden; }

function trustOpen(open) {
  const box = $("trust");
  if (!box) return;
  TRUST.open = !!open;
  box.hidden = !open;
  if (open) trustRefresh();
}

function trustClose() { trustOpen(false); }

/** 每一档**实际**会自动放行什么（和后端 allowed_by_level / 前端 permAllowedByLevel 保持一致）。 */
function trustLevelDesc(id) {
  return {
    ask:  "所有权限请求都弹出来问你（默认，最安全）",
    read: "纯读 / 搜索 / 联网 自动放行；写、执行、工作区外仍会问",
    edit: "再加「工作区内写」自动；执行、工作区外仍会问",
    exec: "再加「工作区内执行」自动；工作区外仍会问（破坏性操作永远要确认）",
    all:  "连工作区外也自动放行（破坏性操作仍要确认）。⚠ 2026-10-13 丢 21 个文件就在这个档",
  }[id] || "";
}

function trustRender() {
  // 当前等级
  const cur = $("trust-cur");
  if (cur) {
    cur.innerHTML = `<span class="trust-cur-k">当前等级</span><b>${esc(permLevelLabel(PERM.level))}</b>`
      + `<span class="trust-cur-a">面板 + 群聊 AI 成员共用这一份（<code>_perm_level.json</code> · 改完立刻生效）</span>`;
  }
  // 5 档
  const lvBox = $("trust-levels");
  if (lvBox) {
    lvBox.innerHTML = PERM.levels.map((it, i) => `<button type="button"
      class="trust-lv${it.id === PERM.level ? " on" : ""}${it.id === "all" ? " danger" : ""}"
      data-lv="${esc(it.id)}" title="${esc(it.label)}">
      <span class="trust-lv-n">${i + 1}</span>
      <span class="trust-lv-t"><b>${esc(it.label)}</b><i>${esc(trustLevelDesc(it.id))}</i></span>
      <span class="trust-lv-mark">${it.id === PERM.level ? "当前" : "切到这一档"}</span>
    </button>`).join("");
  }
  // 待批准（当前会话 + 群聊）
  const pbox = $("trust-pend");
  const pnote = $("trust-pend-note");
  if (pbox) {
    const rows = [];
    (TRUST.pend || []).forEach((pm) => {
      const paths = (pm.resources || []).join("、");
      rows.push(`<div class="trust-pend-row"><i>会话</i>`
        + `<b>${esc(pm.action || pm.kind || "权限")}</b>`
        + `<span title="${esc(paths || pm.message || "")}">${esc(paths || pm.message || "")}</span>`
        + `<button type="button" data-trust-go="ask">去处理</button></div>`);
    });
    if (TRUST.group) {
      const pr = ccPermParse(TRUST.group.content);
      const who = ccPermAgent(TRUST.group);
      rows.push(`<div class="trust-pend-row"><i>群聊</i>`
        + `<b>${esc(who.label || who.name || "AI 成员")} · ${esc(pr.title)}</b>`
        + `<span title="${esc(pr.paths || pr.cmd || "")}">${esc(pr.paths || pr.cmd || "")}</span>`
        + `<button type="button" data-trust-go="chat">去群聊处理</button></div>`);
    }
    pbox.innerHTML = rows.length ? rows.join("") : `<div class="trust-empty">没有待批准的请求。</div>`;
    if (pnote) {
      pnote.textContent = TRUST.pendErr ? ("拿不到： " + TRUST.pendErr)
        : ((TRUST.pend || []).length + (TRUST.group ? " + 群聊 1" : "") + " 条");
    }
  }
  // 最近「始终允许」
  const abox = $("trust-always");
  if (abox) {
    if (TRUST.always === null) {
      abox.innerHTML = `<div class="trust-empty">${esc(TRUST.alwaysErr || "拿不到「始终允许」列表")}</div>`;
    } else if (!(TRUST.always || []).length) {
      abox.innerHTML = `<div class="trust-empty">还没有「始终允许」的记忆 —— 在权限弹窗里点「始终允许」才会记一条（默认只在本会话内有效）。</div>`;
    } else {
      abox.innerHTML = (TRUST.always || []).map((r) => {
        const hit = (state.sessions || []).find((s) => String(s.id) === String(r.session));
        const title = (hit && hit.title) || String(r.session || "").slice(0, 18) || "（不限会话）";
        return `<div class="trust-alw-row">`
          + `<span class="trust-alw-k">${esc(r.kind || "*")}</span>`
          + `<span class="trust-alw-p" title="${esc(r.path || "")}">${esc(r.path || "（不限路径）")}</span>`
          + `<span class="trust-alw-s" title="${esc(r.session || "")}">${esc(title)}</span>`
          + `<span class="trust-alw-h">${esc(String(r.hits || 0))} 次</span>`
          + `<span class="trust-alw-ts">${esc(fmtTime(r.ts))}</span>`
          + `<button type="button" class="trust-alw-x" data-alw-del="${esc(r.id)}" title="撤销这条 —— 以后这类请求会重新问你">撤销</button>`
          + `</div>`;
      }).join("")
        + `<div class="trust-alw-foot"><button type="button" data-alw-clear="1" title="只清当前 agent 的全部「始终允许」记忆">清空全部「始终允许」</button></div>`;
    }
  }
  // ★群聊审批时间线（落盘审计：谁批准/拒绝了哪条、命令原文、什么时候）
  const plbox = $("trust-permlog");
  const plnote = $("trust-permlog-note");
  if (plbox) {
    if (TRUST.permLog === null) {
      plbox.innerHTML = `<div class="trust-empty">${esc(TRUST.permLogErr || "拿不到群聊审批记录")}</div>`;
      if (plnote) plnote.textContent = TRUST.permLogErr ? "不可用" : "";
    } else if (!TRUST.permLog.length) {
      plbox.innerHTML = `<div class="trust-empty">还没有审批记录 —— 群聊 AI 成员第一次需要你批准/拒绝（或超时、拿不到拥有者）时会记一条；按等级自动放行的操作不记。</div>`;
      if (plnote) plnote.textContent = "0 条";
    } else {
      plbox.innerHTML = TRUST.permLog.slice(0, 30).map((r) => {
        const v = String(r.verdict || "");
        const cls = v === "reject" ? "v-deny" : (v === "auto" ? "v-auto" : "v-allow");
        const label = v === "reject" ? "拒绝"
          : v === "auto" ? "自动放行"
          : v === "always" ? "始终允许" : "允许一次";
        const by = r.by === "owner" ? (`拥有者 ${r.owner || "?"}`)
          : r.by === "level" ? "按等级"
          : r.by === "timeout" ? "超时未批"
          : r.by === "no-ctx" ? "拿不到拥有者"
          : r.by === "send" ? "发不出去" : "";
        const what = r.cmd || (r.paths || []).join("、") || r.title || "";
        return `<div class="trust-row">`
          + `<i class="${cls}">${esc(label)}</i>`
          + `<b title="${esc(r.agent || r.aid || "")}">${esc(r.agent || r.aid || "成员")}</b>`
          + `<span title="${esc(what)}">${esc(what)}</span>`
          + `<em>${esc(by)}${r.destructive ? " · 破坏性" : ""}</em>`
          + `<em>${esc(trustWhen(r.ts))}</em>`
          + `</div>`;
      }).join("");
      if (plnote) plnote.textContent = TRUST.permLog.length + " 条（最近 30 条）";
    }
  }
  // ★可撤销：隔离区（破坏性操作放行前留的原件）+ 当前会话的每轮快照
  const ubox = $("trust-undo");
  const unote = $("trust-undo-note");
  if (ubox) {
    const rows = [];
    if (TRUST.quarErr) {
      rows.push(`<div class="trust-empty">隔离区读不到：${esc(TRUST.quarErr)}</div>`);
    } else {
      (TRUST.quar || []).slice(0, 3).forEach((q) => {
        rows.push(`<div class="trust-row"><i>隔离区</i>`
          + `<b title="${esc(q.id || "")}">${esc(q.note || "破坏性操作")}</b>`
          + `<span title="${esc(q.cmd || "")}">${esc(q.cmd || (String(q.count || 0) + " 项被隔离"))}</span>`
          + `<em>${esc(trustWhen(q.taken_at || q.ts))}</em>`
          + `<button type="button" data-trust-jump="quar">打开</button></div>`);
      });
      if (!(TRUST.quar || []).length) {
        rows.push(`<div class="trust-empty">隔离区是空的 —— 破坏性命令放行前会自动留一份（不占 OS 回收站）。</div>`);
      }
    }
    if (!state.current) {
      rows.push(`<div class="trust-empty">先选一个会话，才能看/撤销它的每轮快照。</div>`);
    } else if (TRUST.snapsErr) {
      rows.push(`<div class="trust-empty">快照读不到：${esc(TRUST.snapsErr)}</div>`);
    } else {
      const snaps = TRUST.snaps || [];
      const latest = snaps[0];
      rows.push(`<div class="trust-row"><i>快照</i>`
        + `<b>${esc(String(snaps.length) + " 次")}</b>`
        + `<span>${latest ? ("最近 " + esc(trustWhen(latest.taken_at)) + " · " + esc(String(latest.count || 0)) + " 个文件") : "还没有快照"}</span>`
        + `<button type="button" data-trust-jump="undo">${latest ? "撤销这一轮" : "查看"}</button></div>`);
    }
    ubox.innerHTML = rows.join("");
    if (unote) {
      unote.textContent = TRUST.quarErr ? "隔离区不可用"
        : (TRUST.snapsErr ? "快照不可用"
          : `${(TRUST.quar || []).length} 条隔离 · ${(TRUST.snaps || []).length} 次快照`);
    }
  }
  // ★agent 活动（群聊 AI 成员最近在做什么；桥的内存缓冲，面板重启后从零开始）
  const abox2 = $("trust-act");
  const anote2 = $("trust-act-note");
  if (abox2) {
    if (TRUST.acts === null) {
      abox2.innerHTML = `<div class="trust-empty">${esc(TRUST.actsErr || "拿不到 agent 活动")}</div>`;
      if (anote2) anote2.textContent = TRUST.actsErr ? "不可用" : "";
    } else if (!TRUST.acts.length) {
      abox2.innerHTML = `<div class="trust-empty">还没有活动 —— AI 成员被 @ 后干活（或申请权限）才会记一条；面板重启后缓冲从零开始。</div>`;
      if (anote2) anote2.textContent = "0 条";
    } else {
      abox2.innerHTML = TRUST.acts.slice(0, 12).map((r) => {
        const st = trustActZh(r.status);
        const cls = r.status === "error" || r.status === "denied" || r.status === "timeout"
          ? "v-deny" : (r.status === "running" || r.status === "asking" ? "v-auto" : "v-allow");
        const what = [r.tool, r.summary].filter(Boolean).join(" · ");
        return `<div class="trust-row">`
          + `<i class="${cls}">${esc(st)}</i>`
          + `<b>${esc(r.agent || "成员")}</b>`
          + `<span title="${esc(r.detail || what)}">${esc(what)}</span>`
          + `<em>${esc(trustWhen(r.ts))}</em>`
          + `</div>`;
      }).join("");
      if (anote2) anote2.textContent = TRUST.acts.length + " 条（最近 12 条）";
    }
  }
  // ★派活 / 仲裁的结论（0.1.86 起记在 localStorage；这里只读展示）
  const dbox = $("trust-dec");
  const dnote = $("trust-dec-note");
  if (dbox) {
    const dec = TRUST.dec || [];
    if (!dec.length) {
      dbox.innerHTML = `<div class="trust-empty">还没有仲裁结论 —— 群聊「对比」里请审查者仲裁、并点过「推荐 X」后会记一条。</div>`;
      if (dnote) dnote.textContent = "0 条";
    } else {
      dbox.innerHTML = dec.slice(0, 20).map((r) => `<div class="trust-row">`
        + `<i class="v-allow">推荐</i>`
        + `<b title="${esc(r.target || "")}">${esc(r.pick || "?")}</b>`
        + `<span title="${esc(r.task || "")}">${esc(r.task || "")}</span>`
        + `<em>${esc(r.reviewer || "审查者")}</em>`
        + `<em>${esc(trustWhen(r.ts))}</em>`
        + `</div>`).join("");
      if (dnote) dnote.textContent = dec.length + " 条（最近 20 条）";
    }
  }
  // 本地协作：待批 + 最近任务（只读展示；逐条处理仍在工作台里）
  const obox = $("trust-orch");
  const onote = $("trust-orch-note");
  if (obox) {
    if (TRUST.orch === null) {
      obox.innerHTML = `<div class="trust-empty">${esc(TRUST.orchErr || "拿不到本地协作任务")}</div>`;
      if (onote) onote.textContent = TRUST.orchErr ? "不可用" : "";
    } else {
      const runs = (TRUST.orch.runs || []).slice(0, 3);
      const pend = TRUST.orch.pending || [];
      const rows = [];
      pend.slice(0, 4).forEach((p) => {
        rows.push(`<div class="collab-orch-row"><i class="trust-alw-k">待批</i>`
          + `<b>${esc(p.memberName || p.member || "成员")} · ${esc(p.title || p.kind || "权限")}</b>`
          + `<span title="${esc(p.command || (p.locations || []).join("、") || "")}">${esc(p.command || (p.locations || []).join("、") || "")}</span>`
          + `<button type="button" data-orch-run="${esc(p.run)}">去处理</button></div>`);
      });
      runs.forEach((r) => {
        const st = COLLAB_ST[String(r.status || "")] || String(r.status || "");
        rows.push(`<div class="collab-orch-row"><i class="trust-alw-k">${esc(st)}</i>`
          + `<b>${esc(r.title || r.task || r.id)}</b>`
          + `<span>${esc(String(r.done || 0) + "/" + String(r.members || 0) + " 完成 · " + fmtTime(r.created))}</span>`
          + `<button type="button" data-orch-run="${esc(r.id)}">打开工作台</button></div>`);
      });
      obox.innerHTML = rows.length ? rows.join("")
        : `<div class="trust-empty">还没有本地协作任务 —— 顶栏「协作」可以本机多 agent 并行派活（不登录）。</div>`;
      if (onote) onote.textContent = `${pend.length} 条待批 · ${(TRUST.orch.runs || []).length} 个任务`;
    }
  }
}

async function trustRefresh() {
  if (TRUST.busy) return;
  TRUST.busy = true;
  try {
    await loadPermLevel();
    // 待批（当前会话）
    TRUST.pend = []; TRUST.pendErr = "";
    if (state.current) {
      try {
        const r = await api(`/api/session/${encodeURIComponent(state.current.id)}/permission`);
        TRUST.pend = trustList(r && r.data);
      } catch (e) { TRUST.pendErr = (e && e.message) || String(e); }
    }
    // 待批（群聊，从已有消息推；不额外问服务端）
    TRUST.group = (typeof ccPermPending === "function" && CC.chatID) ? ccPermPending() : null;
    // 最近「始终允许」（只有 ACP 引擎有这个接口）
    try {
      const r = await api("/api/permission/always");
      TRUST.always = trustList(r && r.data);
      TRUST.alwaysErr = "";
    } catch (e) {
      TRUST.always = null;
      const msg = String((e && e.message) || e);
      TRUST.alwaysErr = /^404/.test(msg)
        ? "当前引擎不支持「始终允许」记忆（只有 ACP 引擎有；opencode 引擎没有这个接口）"
        : ("拿不到「始终允许」列表：" + msg);
    }
    // 本地协作：最近任务 + 待批（只读展示；逐条处理在工作台里）
    try {
      const [rr, pp] = await Promise.all([api("/orchestrate/runs"), api("/orchestrate/pending")]);
      TRUST.orch = { runs: trustList(rr && rr.runs), pending: trustList(pp && pp.pending) };
      TRUST.orchErr = "";
    } catch (e) {
      TRUST.orch = null;
      const msg = String((e && e.message) || e);
      TRUST.orchErr = /^404/.test(msg)
        ? "当前版本没有本地协作接口（需要 0.2.1+ 的后端；重启面板服务）"
        : ("拿不到本地协作任务：" + msg);
    }
    // ★可撤销：隔离区记录 + 当前会话的每轮快照（都用现成接口，不另造一套）
    TRUST.quar = null; TRUST.quarErr = "";
    try {
      const r = unwrap(await api("/quarantine/list")) || {};
      TRUST.quar = trustList(r && r.items);
    } catch (e) { TRUST.quarErr = (e && e.message) || String(e); }
    TRUST.snaps = null; TRUST.snapsErr = "";
    if (state.current) {
      try {
        const r = unwrap(await api(`/snapshot/list?scope=${encodeURIComponent(state.current.id)}`)) || {};
        TRUST.snaps = trustList(r && r.items);
      } catch (e) { TRUST.snapsErr = (e && e.message) || String(e); }
    } else TRUST.snaps = [];
    // ★群聊审批时间线（落盘审计；没有就是还没发生过）
    TRUST.permLog = null; TRUST.permLogErr = "";
    try {
      const r = await api("/ccagent/permissions?limit=80");
      TRUST.permLog = trustList(r && r.items);
    } catch (e) {
      const msg = String((e && e.message) || e);
      TRUST.permLogErr = /^404/.test(msg)
        ? "当前版本还没有审批审计接口（重启面板服务）" : ("拿不到审批记录：" + msg);
    }
    // ★agent 活动（桥的内存环形缓冲；面板重启后从零开始）
    TRUST.acts = null; TRUST.actsErr = "";
    try {
      const r = await api("/ccagent/activity?since=0");
      TRUST.acts = trustList(r && r.items);
    } catch (e) { TRUST.actsErr = (e && e.message) || String(e); }
    // ★派活 / 仲裁结论（localStorage 历史；只读）
    TRUST.dec = trustList((typeof ccDecisionHistory === "function") ? ccDecisionHistory() : []);
    trustRender();
  } finally {
    TRUST.busy = false;
  }
}

/** 切档：放宽到 exec / all 要二次确认（历史事故见 AGENTS 踩坑）；收紧不问。 */
async function trustPickLevel(lv) {
  if (!lv || lv === PERM.level) return;
  const order = { ask: 0, read: 1, edit: 2, exec: 3, all: 4 };
  const wider = (order[lv] || 0) > (order[PERM.level] || 0);
  if (wider && (lv === "exec" || lv === "all")) {
    const msg = lv === "all"
      ? "「全部放行」= 连工作区外也自动放行（破坏性操作仍会确认）。\n2026-10-13 曾因等级过宽 + 盲目放行丢失 21 个文件。\n确定切到这一档？"
      : "「读 + 改 + 执行」会在工作区内自动跑命令（删除等破坏性操作仍会确认）。\n确定切到这一档？";
    if (!confirm(msg)) return;
  }
  await setPermLevel(lv);
  trustRender();
  setBanner("权限等级已切到：" + permLevelLabel(lv));
}

async function trustForget(id) {
  try {
    await api("/api/permission/always", { method: "DELETE", body: JSON.stringify({ id: id }) });
    setBanner("已撤销一条「始终允许」—— 以后这类请求会重新问你");
    await trustRefresh();
  } catch (e) { setBanner("撤销失败：" + ((e && e.message) || e), { sticky: true }); }
}

async function trustForgetAll() {
  if (!confirm("清空当前 agent 的全部「始终允许」记忆？以后这类请求都会重新问你（收紧，可随时再授权）。")) return;
  try {
    await api("/api/permission/always", { method: "DELETE", body: JSON.stringify({}) });
    setBanner("已清空「始终允许」记忆");
    await trustRefresh();
  } catch (e) { setBanner("清空失败：" + ((e && e.message) || e), { sticky: true }); }
}

function initTrust() {
  // 入口统一收在「权限 ▾」菜单底部（顶栏盾牌已按用户要求删除）
  const close = $("trust-close");
  if (close) close.addEventListener("click", trustClose);
  const reload = $("trust-reload");
  if (reload) reload.addEventListener("click", trustRefresh);
  const tighten = $("trust-tighten");
  if (tighten) tighten.addEventListener("click", async () => {
    if (PERM.level === "ask") { setBanner("已经是「全部询问」了"); return; }
    await setPermLevel("ask");
    trustRender();
    setBanner("已收紧到「全部询问」");
  });
  const box = $("trust");
  if (box) {
    box.addEventListener("click", (e) => {
      if (e.target === box) { trustClose(); return; }
      const lv = e.target.closest("[data-lv]");
      if (lv && box.contains(lv)) { trustPickLevel(lv.dataset.lv); return; }
      const go = e.target.closest("[data-trust-go]");
      if (go) {
        trustClose();
        if (go.dataset.trustGo === "chat") {
          try { setCocraft(true); ccPermRender(); } catch { /* 没登录就算了 */ }
        } else {
          try { pollAsks(); } catch { /* ignore */ }
        }
        return;
      }
      const jump = e.target.closest("[data-trust-jump]");
      if (jump) {
        trustClose();
        if (jump.dataset.trustJump === "quar") { try { quarOpen(); } catch { /* ignore */ } }
        else { try { undoOpen(); } catch { /* ignore */ } }
        return;
      }
      const del = e.target.closest("[data-alw-del]");
      if (del) { trustForget(del.dataset.alwDel); return; }
      if (e.target.closest("[data-alw-clear]")) { trustForgetAll(); return; }
      const orch = e.target.closest("[data-orch-run]");
      if (orch) {
        trustClose();
        collabOpen(true);
        collabOpenRun(orch.dataset.orchRun);
        return;
      }
    });
  }
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && trustIsOpen()) trustClose(); });
}

/** 资源是不是都在当前工作区内。
 *  ⚠ **没给路径 → false（范围未知，不能算"区内"）** —— 与后端 `_in_dir` 逐条一致。
 *    2026-10-13 改：以前 fail-open（空=区内），codex 从不带路径 → 连"删桌面"都被当成区内自动放行。 */
function inWorkspace(paths) {
  if (!paths || !paths.length) return false;        // 范围未知 → 不算区内（未知就去问）
  const root = (typeof currentDir === "function" ? currentDir() : "") || "";
  if (!root) return false;
  const norm = (p) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const r = norm(root);
  for (const p of paths) {
    const n = norm(p);
    if (!n) return false;
    if (!(n === r || n.startsWith(r + "/"))) return false;
  }
  return true;
}
function permAllowedByLevel(pm) {
  const lv = PERM.level;
  if (!lv || lv === "ask") return false;
  if (lv === "all") return true;
  const hay = (String(pm.kind || "") + " " + String(pm.action || "") + " " + String(pm.message || "")).toLowerCase();
  // ★联网：**独立能力**（引擎无关）—— 只看文本里的网络字样 / 资源里带 `://`；不受工作区约束
  const isNet = /network|internet|联网|网络|外网|上网|域名|domain|\bhost\b|\burl\b|http|socket|\bproxy\b|\bweb\b|fetch|dns|curl|wget/.test(hay)
    || (pm.resources || []).some((x) => String(x || "").indexOf("://") >= 0);
  if (isNet) return true;                       // 等级不是 ask → 联网放行（与后端 allowed_by_level 一致）
  const isRead = /read|search|fetch|think|list|glob|grep|view|cat|ls\b|dir\b/.test(hay);
  const isWrite = /edit|write|create|delete|move|rename|patch|mkdir|rm\b|touch|save/.test(hay);
  const isExec = /execute|exec|bash|shell|run\b|command|terminal|powershell|cmd/.test(hay);
  const pureRead = isRead && !isWrite && !isExec;
  const ws = inWorkspace(pm.resources);          // ★ 现在"没给路径"→ false（未知就问）
  if (lv === "read") return pureRead;
  if (lv === "edit") return pureRead || ((isRead || isWrite) && !isExec && ws);
  if (lv === "exec") return pureRead || ((isRead || isWrite || isExec) && ws);
  return false;
}

async function pollAsks() {
  try {
    if (state.current) {
      const sid = encodeURIComponent(state.current.id);
      const [f, p] = await Promise.all([
        api(`/api/session/${sid}/form`).catch(() => null),
        api(`/api/session/${sid}/permission`).catch(() => null),
      ]);
      let perms = (p && p.data) || [];
      // ★ 权限等级：命中就直接自动放行（不弹窗、不需要理由）
      if (PERM.level && PERM.level !== "ask" && perms.length) {
        const keep = [];
        for (const pm of perms) {
          if (permAllowedByLevel(pm)) {
            try {
              await api(`/api/session/${sid}/permission/${encodeURIComponent(pm.id)}/reply`,
                        { method: "POST", body: { decision: "once" } });
              setBanner(`已按权限等级「${permLevelLabel(PERM.level)}」自动放行：${pm.action || pm.kind || "权限"}`);
            } catch { keep.push(pm); }
          } else { keep.push(pm); }
        }
        perms = keep;
      }
      renderAsk((f && f.data) || [], perms);
    }
  } catch { /* 轮询失败就下一轮再说 */ }
  setTimeout(pollAsks, 1200);
}

function initAsk() {
  window.__askSubmit = submitAsk;                       // 内联 onclick 兜底用
  const _pb = $("btn-perm");
  if (_pb) _pb.addEventListener("click", (e) => { e.stopPropagation(); permMenuToggle(); });
  loadPermLevel();

  $("ask-body").addEventListener("click", (e) => {
    const b = e.target.closest(".ask-opt");
    if (!b) return;
    const key = b.dataset.key;
    const val = b.dataset.value;
    if (b.dataset.multi) {                              // 多选：切换并记账
      b.classList.toggle("sel");
      const cur = Array.isArray(ASK.picks[key]) ? ASK.picks[key].slice() : [];
      const i = cur.indexOf(val);
      if (b.classList.contains("sel")) { if (i < 0) cur.push(val); }
      else if (i >= 0) cur.splice(i, 1);
      ASK.picks[key] = cur;
    } else {
      $("ask-body").querySelectorAll(`.ask-opt[data-key="${CSS.escape(key)}"]`)
        .forEach((x) => x.classList.remove("sel"));
      b.classList.add("sel");
      ASK.picks[key] = val;                             // 单选：记账
      // 选了选项 → 清掉同字段的"自己说"（以最后一次操作为准）
      const ci = $("ask-body").querySelector(`input.ask-custom[data-key="${CSS.escape(key)}"]`);
      if (ci) ci.value = "";
    }
    if (e.detail >= 2) submitAsk();                      // 选项上双击 = 直接提交
  });

  // "自己说"输入框：一开始输入就取消该字段的选项选择（以最后一次操作为准）
  $("ask-body").addEventListener("input", (e) => {
    const ci = e.target.closest("input.ask-custom");
    if (!ci) return;
    const key = ci.dataset.key;
    if (ci.value.trim() !== "") {
      $("ask-body").querySelectorAll(`.ask-opt[data-key="${CSS.escape(key)}"]`)
        .forEach((x) => x.classList.remove("sel"));
      ASK.picks[key] = "";
    }
  });

  $("ask-actions").addEventListener("click", async (e) => {
    if (e.target.closest("#ask-ignore")) { ignoreForm(); return; }
    const dec = e.target.closest("[data-dec]");
    if (dec && ASK.perm) {
      const perm = ASK.perm;
      try {
        const res = await fetch(`/api/session/${encodeURIComponent(perm.sessionID)}`
          + `/permission/${encodeURIComponent(perm.id)}/reply`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ decision: dec.dataset.dec }),
        });
        if (!res.ok) {
          const t = await res.text();
          if (res.status === 404 || res.status === 409) {    // 已失效 / 已处理
            askMsg(`该权限请求已失效（${res.status}），已刷新`, true);
            askHide();
            pollAsks();
            return;
          }
          askMsg(`回复失败 ${res.status}：${t.slice(0, 160)}`, true);
          return;
        }
      } catch (err) { askMsg(`回复异常：${err.message}`, true); return; }
      askClearBanner();
      askHide();
      pollAsks();
      return;
    }
    if (e.target.closest("#ask-submit")) submitAsk();
  });
}

/* ---------------- 动态壁纸：Wallpaper Engine 的 mp4 原片 ----------------
 * 收起侧栏 → **缓慢启动**：转速由静止慢慢加上去；
 * 展开会话 → **缓慢停止**：转速慢慢减到停 —— **停住的那一帧就是会话界面的静态背景**
 *            （直接把视频暂停在那里，不再抓帧、不再有 #bg-freeze 层）。
 * 没有可用原片（没装 WE / 没订阅 / 系统要求减少动态）→ 两侧都用主题自带的静态壁纸，不做动画。
 * 素材与选择由 server.py 提供：/live/wallpapers（生效清单）、/live/list + /live/pick（选择器）。 */

let liveRate = 0.8;             // 正常播放速度（1 = 原速）。可在设置面板里调（SET.rate）
const LIVE_RAMP_IN = 600;       // 缓慢启动：从静止加到 liveRate 用多久（ms）
const LIVE_RAMP_OUT = 1100;     // 缓慢停止：从当前速度减到静止用多久（ms）
// 起步速率：⚠ Chromium 的倍速**下限是 0.0625**（1/16），给更小会抛 NotSupportedError，
// 而且那个异常会把整个流程打断（踩过：0.06 导致"收起后完全没反应"）。所以取 0.3。
const LIVE_START_RATE = 0.3;
let liveReload = null;          // initLiveBg 里赋值，供壁纸选择器选完立刻换片
let liveApply = null;           // initLiveBg 里赋值：设置一变就立刻作用到动态壁纸

/* ---------------- 界面设置（顶栏「设」按钮）----------------
 * 存在 localStorage，改完立刻生效。以后新增的可调项也往这里放。
 * ⚠ SET 必须在 initLiveBg 之前声明（sync 里会读 SET.live），而 boot() 在文件末尾才跑，所以没问题。 */

const SET_KEY = "cocraft.settings";
const SET_DEFAULT = {
  dimHiru: 100, blur: 28, rate: 80,
  zoom: 100,                         // ★界面缩放（%）：整页等比放大/缩小（文字/间距/图标一起）
  live: true, taskbar: true, spectrumSides: true, spectrumSidesH: 100, spectrumSidesGain: 120,
  petals: true, zh: true,
  glass: true,                       // ★液态玻璃皮肤（面板半透明磨砂 + 壁纸透出）
  // ★可逆执行：每轮工作区快照（发消息前留底 → 「撤销这一轮」）。
  //   ⚠ **默认关**（用户 2026-10-16 反馈：把整个工作区镜像进内容仓太重）——
  //   关掉后**只保留破坏性操作的隔离区**（那条不走这个开关）。
  turnSnap: false,
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
  l2: "Sakura ＆ 絵梨衣 のDuck",
  credit: "同人图 · 仅本地自用，版权归原作者所有",
};

function loadSettings() {
  try { Object.assign(SET, JSON.parse(localStorage.getItem(SET_KEY) || "{}")); } catch { /* 隐私模式 */ }
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
async function setEngine(id) {
  if (!id || id === ENGINE.active) return;
  // ★ 跨引擎接续：记下当前这条会话在 **agent 侧**的 id（ACP 用 agentSessionId，OpenCode 就是 id 本身），
  //   切完引擎后优先把"同一条会话"再接回来。
  const prev = state.current
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
      } else if (id === "acp") {
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
    if (!resumed && state.sessions.length && !state.current) {
      await openSession(state.sessions[0].id);
    }
  } catch (err) {
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
  const prov = String(a.provider || a.providerGuess || "").toLowerCase();
  return KEY_NAME_BY_PROVIDER[prov] || "DEEPSEEK_API_KEY";
}

/** 拉基线里的 Key（只拿掩码）并回填到某一行：where = "cfg" | "setup"
 *  ⚠ 变量名**不让手填**（用户反馈"怎么有两个输入框"）：按当前 agent 的品牌自动决定，
 *    界面只显示一个只读小标签 + 一个密码框。 */
async function loadApiKeyRow(where) {
  const p = where === "setup" ? "setup" : "cfg";
  const varEl = $(p + "-key-var"), noteEl = $(p + "-key-note");
  if (!varEl && !noteEl) return;
  try {
    // 变量名要按**当前 agent 的品牌**决定：所以先把 agentlist 拉回来（设置弹窗里可能还没加载过）
    if (!(ACP.list || []).length) { try { await loadAcpAgents(); } catch { /* 老后端忽略 */ } }
    const r = await api("/engine/acp/baseline");
    const d = (r && r.data) || {};
    const names = d.envNames || [];
    const shown = names.map((n) => n + " = " + ((d.env || {})[n] || "")).join("　");
    if (noteEl) {
      noteEl.textContent = names.length
        ? `已设置：${shown}（改完点保存；值留空 = 清除）`
        : "写进共享基线的环境变量；只存本机 runtime/state，不进安装包";
    }
    // ⚠ 始终显示**当前 agent 对应的变量名**，不要因为"已经有别的 Key"就显示那一个 ——
    //   否则切到 codex 后标签还停在 ANTHROPIC_API_KEY，用户会把 DeepSeek Key 填到错的地方。
    if (varEl) varEl.textContent = guessKeyName();
  } catch {
    if (noteEl) noteEl.textContent = "拿不到基线（后端需支持 /engine/acp/baseline）";
    if (varEl && !varEl.textContent) varEl.textContent = "DEEPSEEK_API_KEY";
  }
}

/** 保存/清除 Key：name + value（value 为空 = 删除该变量）。
 *  name 取小标签里那个（按品牌自动决定，见 loadApiKeyRow）。 */
async function saveApiKey(where) {
  const p = where === "setup" ? "setup" : "cfg";
  const name = String((($(p + "-key-var") || {}).textContent) || "").trim() || guessKeyName();
  const value = String($(p + "-key-value").value || "").trim();
  const noteEl = $(p + "-key-note");
  try {
    const r = await api("/engine/acp/baseline", { method: "POST",
      body: JSON.stringify({ name, value }) });
    const d = (r && r.data) || {};
    $(p + "-key-value").value = "";
    const names = d.envNames || [];
    const shown = names.map((n) => n + " = " + ((d.env || {})[n] || "")).join("　");
    if (noteEl) {
      noteEl.textContent = (value ? `已保存 ${name}。` : `已清除 ${name}。`)
        + (names.length ? `当前：${shown}` : "当前没有设置任何 Key");
    }
    setBanner(value ? `已保存模型 Key：${name}（改完会重启 ACP 子进程）` : `已清除模型 Key：${name}`);
    return true;
  } catch (err) {
    if (noteEl) noteEl.textContent = "保存失败：" + (err && err.message ? err.message : err);
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
}

function closeModes() { const b = $("mode"); if (b) b.hidden = true; }

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
}

function closeSkills() { const b = $("skill"); if (b) b.hidden = true; }

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
  $("imp-cur").textContent = "读取中…";
  $("imp-list").innerHTML = "";
  await loadImport();
  // 后端会在列会话时按 agent 侧自愈"接续会话的目录"，这里顺手刷新徽章
  loadSessions();
}

function closeImport() { const b = $("import"); if (b) b.hidden = true; }

/* ---------------- ★可逆执行：隔离区（工具 → 隔离区） ----------------
 * 破坏性操作**放行前**先把要动的目标复制进 runtime/quarantine/（见 backend/snapshots.py）。
 * 这里只做三件事：列出 / 一键恢复 / 删除记录（「撤销这一轮」建在同一个快照底座上，后续接）。 */

const QUAR = { items: [], stats: null, err: "", busy: false };

async function quarOpen() {
  const box = $("quar");
  if (!box) return;
  box.hidden = false;
  await quarLoad();
}

function quarClose() { const b = $("quar"); if (b) b.hidden = true; }

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
    foot.textContent = st.quarantine_dir
      ? `共 ${items.length} 条记录 · 隔离区占用 ${fmtBytes(st.quarantine || 0)}（${st.quarantine_dir}）`
      : `共 ${items.length} 条记录`;
  }
  if (!items.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">还没有隔离记录。<br>`
      + `群聊里的 AI 成员执行**破坏性操作**时，放行前会把要动的文件备份到这里（面板侧的直接操作暂未接入）。</div>`;
    return;
  }
  box.innerHTML = items.map((it) => {
    const missed = Number(it.missed || 0);
    return `<div class="q-row" data-qid="${esc(it.id)}">
      <div class="q-top">
        <b>🛡 ${it.count} 项</b>
        <span class="q-size">${fmtBytes(it.bytes || 0)}</span>
        <span class="q-time" title="${esc(it.ts || "")}">${esc(quarWhen(it.taken_at))}</span>
      </div>
      <div class="q-note">${esc(it.note || "（无说明）")}</div>
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
        + `要用隔离区的备份**覆盖**它们吗？（取消 = 保留现在的文件）`)) {
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

/* ---------------- ★可逆执行：「撤销这一轮」（工具 → 撤销这一轮） ----------------
 * 每发一条消息、**在 prompt 之前**给会话工作目录落一次快照（后端 `/snapshot/take`，走 hash 缓存，热扫 ~10ms）。
 * 这里显示「这一轮改了哪些文件」→ 点开看行级 diff → 一键退回（后端 `/snapshot/undo`）。 */

const UNDO = { scope: "", root: "", ts: "", diff: null, err: "", busy: false, note: "" };

/** 轮开始落快照。**失败绝不挡住发消息**（返回 null）。开关关着就直接跳过。 */
async function snapTurnStart(sid, dir) {
  if (!SET.turnSnap) return null;      // ★默认关：只留破坏性操作的隔离区
  if (!sid || !dir) return null;
  try {
    const r = unwrap(await api("/snapshot/take", { method: "POST",
      body: JSON.stringify({ root: dir, scope: String(sid), label: "turn", content: true }) }));
    if (r && r.ts) {
      UNDO.scope = String(sid); UNDO.root = dir; UNDO.ts = r.ts;
    }
    return r;
  } catch (e) {
    return null;                       // 安静失败：快照只是安全网，不该挡住干活
  }
}

async function undoOpen() {
  const box = $("undo");
  if (!box) return;
  box.hidden = false;
  await undoLoad();
}

function undoClose() { const b = $("undo"); if (b) b.hidden = true; }

async function undoLoad() {
  const list = $("undo-list");
  if (list) list.innerHTML = `<div class="muted" style="padding:10px">对比中…</div>`;
  UNDO.diff = null;
  UNDO.err = "";
  UNDO.note = "";
  UNDO.ts = "";                        // ★ 先清掉：否则"这次没有快照"时按钮还是可点的
  UNDO.scope = state.current ? String(state.current.id) : "";
  UNDO.root = currentDir();
  if (!UNDO.scope) {
    UNDO.err = "先选一个会话 —— 「撤销这一轮」是**按会话**记的。";
    renderUndo();
    return;
  }
  if (!UNDO.root) {
    UNDO.err = "这个会话没有工作目录，没法对比工作区。";
    renderUndo();
    return;
  }
  try {
    const lst = unwrap(await api(`/snapshot/list?scope=${encodeURIComponent(UNDO.scope)}`));
    const latest = ((lst && lst.items) || [])[0];
    if (!latest) {
      UNDO.note = SET.turnSnap
        ? "还没有快照 —— 发出第一条消息时面板会自动记录一次。"
        : "还没有快照。「每轮工作区快照」现在是关着的 —— 在 设置 →「每轮工作区快照」打开它，"
          + "之后每发一条消息就会先留一次底；破坏性操作的隔离区不受这个开关影响。";
      renderUndo();
      return;
    }
    UNDO.ts = latest.ts;
    const d = unwrap(await api(`/snapshot/diff?scope=${encodeURIComponent(UNDO.scope)}`
      + `&root=${encodeURIComponent(UNDO.root)}`));
    UNDO.diff = d;
  } catch (e) {
    UNDO.err = (e && e.message) ? e.message : String(e);
  }
  renderUndo();
}

function undoWhen(ts) {
  const t = Number(ts || 0);
  if (!t) return "";
  const d = new Date(t * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 一行改动：⚠ 不给它生成"按路径拼出来的 id" —— `getElementById` 收到的是**字面量**，
 *  用 `CSS.escape()` 转义过的字符串必然找不到元素（这次踩到过）。diff 块就取它的**下一个兄弟**。 */
function undoRow(kind, cls, label, extra, rel) {
  return `<div class="u-row" data-upath="${esc(rel)}" data-ukind="${esc(kind)}">
    <span class="u-kind ${cls}">${label}</span>
    <span class="u-path" title="${esc(rel)}">${esc(rel)}</span>
    <span class="u-meta">${extra || ""}</span>
  </div>`;
}

function renderUndo() {
  const box = $("undo-list");
  const foot = $("undo-foot");
  const runBtn = $("undo-run");
  if (!box) return;
  if (runBtn) runBtn.disabled = !!UNDO.busy || !UNDO.ts;
  if (UNDO.err) {
    box.innerHTML = `<div class="muted" style="padding:10px">${esc(UNDO.err)}</div>`;
    if (foot) foot.textContent = "";
    return;
  }
  if (UNDO.note) {
    box.innerHTML = `<div class="muted" style="padding:10px">${esc(UNDO.note)}</div>`;
    if (foot) foot.textContent = "工作目录：" + (UNDO.root || "");
    return;
  }
  const d = UNDO.diff || {};
  const st = d.stats || {};
  const n = (st.added || 0) + (st.deleted || 0) + (st.changed || 0) + (st.renamed || 0);
  if (foot) {
    foot.textContent = n
      ? `这一轮：改 ${st.changed || 0} · 增 ${st.added || 0} · 删 ${st.deleted || 0}`
        + ` · 改名 ${st.renamed || 0}（基准 ${undoWhen(st.base_at)}）`
      : `没有改动（基准 ${undoWhen(st.base_at)}）· ${UNDO.root}`;
  }
  if (!n) {
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + `工作区**没有变化** —— 这一轮 agent 没动文件（只聊了天 / 只跑了只读命令）。</div>`;
    return;
  }
  let html = "";
  const row = (kind, cls, label, extra, rel) =>
    undoRow(kind, cls, label, extra, rel) + `<div class="u-diff" hidden></div>`;
  for (const x of (d.changed || [])) {
    const dl = Number(x.size_before || 0) - Number(x.size || 0);
    html += row("changed", "ch", "改", `${dl === 0 ? "" : (dl > 0 ? "−" + dl + " B" : "+" + (-dl) + " B")}`, x.path);
  }
  for (const x of (d.added || [])) html += row("added", "ad", "增", fmtBytes(x.size || 0), x.path);
  for (const x of (d.deleted || [])) html += row("deleted", "dl", "删", fmtBytes(x.size || 0), x.path);
  for (const x of (d.renamed || [])) html += row("renamed", "mv", "名", esc(x.from || ""), x.to);
  box.innerHTML = html;
}

/** 点一行：展开 / 收起该文件的行级 diff（diff 块 = 这一行的下一个兄弟）。 */
async function undoToggleFile(rowEl) {
  if (!rowEl) return;
  const rel = rowEl.dataset.upath || "";
  const box = rowEl.nextElementSibling;
  if (!box || !box.classList.contains("u-diff")) return;
  if (!box.hidden) {
    box.hidden = true;
    rowEl.classList.remove("open");
    return;
  }
  rowEl.classList.add("open");
  box.hidden = false;
  await undoLoadFile(box, rel);
}

async function undoLoadFile(box, rel) {
  if (!box) return;
  box.hidden = false;
  if (box.dataset.loaded === "1") return;
  box.innerHTML = `<div class="muted" style="padding:6px 0">读取…</div>`;
  try {
    const r = unwrap(await api(`/snapshot/file?scope=${encodeURIComponent(UNDO.scope)}`
      + `&ts=${encodeURIComponent(UNDO.ts)}&path=${encodeURIComponent(rel)}`));
    if (r && r.ok && r.lines && r.lines.length) {
      box.innerHTML = renderDiff(r.lines.join("\n"));
      box.dataset.loaded = "1";
    } else {
      box.innerHTML = `<div class="muted" style="padding:6px 0">${esc((r && r.reason) || "拿不到行级 diff")}</div>`;
    }
  } catch (e) {
    box.innerHTML = `<div class="muted" style="padding:6px 0">读取失败：${esc(e && e.message ? e.message : e)}</div>`;
  }
}

async function undoRun() {
  if (UNDO.busy || !UNDO.ts) return;
  if (!confirm(`把「${UNDO.root}」退回这一轮开始时的状态？\n\n`
    + `被删的会恢复、被改的写回旧内容、这一轮新增的会被删掉\n`
    + `（新增的文件会先自动进隔离区，所以撤销本身也可以再撤回来）`)) return;
  UNDO.busy = true;
  renderUndo();
  try {
    const r = await api("/snapshot/undo", { method: "POST",
      body: JSON.stringify({ scope: UNDO.scope, ts: UNDO.ts }) });
    setBanner(`已撤销这一轮：恢复 ${r.restored || 0} · 删掉新增 ${r.removed || 0}`
      + ((r.skipped || 0) ? ` · 跳过 ${r.skipped}（内容仓里没有那一版）` : ""));
    UNDO.busy = false;
    await undoLoad();
  } catch (e) {
    UNDO.busy = false;
    setBanner("撤销失败：" + (e && e.message ? e.message : e));
    renderUndo();
  }
}

async function loadImport() {
  try {
    // ★internal=1：一次拿全，前端再按 `internal` 标记拆成两个标签
    const r = await api("/api/session/remote?internal=1");
    IMP.supported = !!(r && r.supported);
    IMP.all = (r && r.sessions) || [];
    IMP.list = IMP.all.filter((x) => !x.internal);
    IMP.internal = IMP.all.filter((x) => !!x.internal);
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
  const tabs = $("imp-tabs");
  if (!box) return;
  const np = $("imp-n-project"), ni = $("imp-n-internal");
  if (np) np.textContent = String((IMP.list || []).length);
  if (ni) ni.textContent = String((IMP.internal || []).length);
  if (tabs) {
    Array.prototype.forEach.call(tabs.querySelectorAll("[data-imp-tab]"), (b) => {
      b.classList.toggle("on", String(b.dataset.impTab) === String(IMP.tab));
    });
  }
  if (IMP.supported === false) {
    if (tabs) tabs.hidden = true;
    if (cur) cur.textContent = IMP.err || "当前 agent / 引擎不支持列会话（session/list）";
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + `只有 ACP 引擎 + 支持 <code>session/list</code> 的 agent 才能列出来。`
      + `（OpenCode 自带的 ACP 支持；Codex 也支持）</div>`;
    return;
  }
  if (tabs) tabs.hidden = false;
  const internal = IMP.tab === "internal";
  const rows = internal ? (IMP.internal || []) : (IMP.list || []);
  const n = rows.length;
  const fresh = rows.filter((x) => !x.imported).length;
  if (cur) {
    if (IMP.err) cur.textContent = IMP.err;
    else if (internal) {
      cur.textContent = n
        ? `内部会话 ${n} 条（群聊 AI 成员 / 本地协作——通常不需要接续到项目列表）`
        : "没有内部会话（群聊 AI 成员 / 本地协作）";
    } else {
      cur.textContent = n
        ? `项目会话 ${n} 条${fresh ? `，其中 ${fresh} 条还没接续` : "（都已在面板里）"}`
          + ((IMP.internal || []).length ? `　·　另有 ${IMP.internal.length} 条内部会话（另一个标签）` : "")
        : ((IMP.internal || []).length
          ? `没有项目会话；内部会话有 ${IMP.internal.length} 条（点上面的「内部会话」看）`
          : "agent 侧没有会话");
    }
  }
  if (!n) {
    box.innerHTML = `<div class="muted" style="padding:10px">`
      + (internal ? "没有内部会话。" : "agent 那边没有项目会话。")
      + `</div>`;
    return;
  }
  box.innerHTML = rows.map((s) => {
    const title = s.title || "(无标题)";
    const when = s.updatedAt ? String(s.updatedAt).replace("T", " ").slice(0, 16) : "";
    const hist = Number(s.messages || 0);
    const histTip = hist ? `<span class="muted" style="font-size:11px">对面 ${hist} 条历史</span>` : "";
    return `<div class="agents-row${s.imported ? " on" : ""}${s.internal ? " imp-internal" : ""}" data-imp="${esc(s.id)}">`
      + `<div class="an"><span class="dot ${s.internal ? "" : "ok"}"></span>${esc(title)}`
      + (s.internal ? `<span class="badge">内部</span>` : "")
      + `<span class="muted" style="font-size:11px">${esc(s.id)}</span>`
      + histTip
      + `<span class="muted" style="font-size:11px;margin-left:auto">`
      + `${s.imported ? "已接续 · 点开" : "点一下接续"}</span></div>`
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

/* ---------------- 首次设置向导（默认引擎 / agent / 模型）---------------- */

const SETUP_KEY = "cocraft.setup.v1";
const SETUP = { open: false, engine: "", models: [] };

/** 首次启动 / 手动重跑：选默认引擎与 agent，并把模型清单拉出来 */
async function openSetup() {
  const box = $("setup");
  if (!box || SETUP.open) return;
  SETUP.open = true;
  SETUP.models = [];
  box.hidden = false;
  $("setup-model-row").hidden = true;
  $("setup-agent-row").hidden = true;
  $("setup-done").hidden = true;
  $("setup-next").hidden = false;
  $("setup-next").disabled = false;
  $("setup-note").textContent = "首次设置只做一次；可随时在设置里重跑。";
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
      + (ok ? `；已先帮你选到可用的「${ok.label || ok.id}」。` : "；下面没有可用的引擎，可先「跳过」。");
  } else {
    $("setup-cur").textContent = "选一个引擎；以后可在设置里重跑这个向导。";
  }
  await setupEngineChanged();
}

/** 引擎变化：ACP 才需要选 agent（和填模型 Key） */
async function setupEngineChanged() {
  const engine = $("setup-engine").value;
  SETUP.engine = engine;
  const row = $("setup-agent-row");
  const keyRow = $("setup-key-row");
  if (engine !== "acp") {
    row.hidden = true;
    if (keyRow) keyRow.hidden = true;
    return;
  }
  row.hidden = false;
  if (keyRow) keyRow.hidden = false;
  const sel = $("setup-agent");
  sel.innerHTML = `<option value="">加载中…</option>`;
  try {
    await loadAcpAgents();
    await loadApiKeyRow("setup");       // Key 的变量名按当前 agent 的 provider 猜
    const list = (ACP.list || []).filter((a) => a.available);
    if (!list.length) {
      sel.innerHTML = `<option value="">没有可用 agent</option>`;
      $("setup-note").textContent = "没有检测到可用的 ACP agent；可以跳过，之后到设置 → ACP 代理里添加。";
      return;
    }
    sel.innerHTML = list.map((a) =>
      `<option value="${esc(a.id)}">${esc(a.label || a.id)}</option>`).join("");
  } catch {
    sel.innerHTML = `<option value="">拿不到 agent 列表</option>`;
  }
}

/** 下一步：应用引擎 / agent，并尝试把模型清单取回来 */
async function setupNext() {
  const engine = $("setup-engine").value;
  const btn = $("setup-next");
  btn.disabled = true;
  $("setup-note").textContent = "正在应用…";
  try {
    if (ENGINE.active !== engine) await setEngine(engine);
    if (engine === "acp") {
      const aid = $("setup-agent").value;
      if (aid && aid !== ACP.active) {
        const r = await api("/engine/acp/agents", { method: "POST", body: JSON.stringify({ id: aid }) });
        ACP.active = (r && r.active) || aid;
        ACP.list = (r && r.agents) || ACP.list;
        if (typeof updateAcpRow === "function") updateAcpRow();
      }
    }
    let list = [];
    try { list = unwrap(await api("/api/model")) || []; } catch { list = []; }
    if (!list.length && engine === "acp") {
      // ACP 的模型清单挂在会话上：一条都没有就先建一条，把 agent 拉起来
      try {
        const existing = unwrap(await api("/api/session?limit=1")) || [];
        if (!existing.length) await api("/api/session", { method: "POST", body: "{}" });
        list = unwrap(await api("/api/model")) || [];
      } catch { list = []; }
    }
    SETUP.models = list;
    $("setup-model-row").hidden = false;
    const msel = $("setup-model");
    if (list.length) {
      msel.innerHTML = list.map((m) =>
        `<option value="${esc(m.id)}">${esc(m.name || m.id)}</option>`).join("");
      $("setup-note").textContent = "选好默认模型后点「完成」。";
    } else {
      msel.innerHTML = `<option value="">（暂时拿不到模型清单）</option>`;
      $("setup-note").textContent = "没拿到模型清单，也可以先完成；之后打开模型弹窗会再拉。";
    }
    $("setup-next").hidden = true;
    $("setup-done").hidden = false;
  } catch (err) {
    $("setup-note").textContent = "应用失败：" + (err && err.message ? err.message : err);
  } finally {
    btn.disabled = false;
  }
}

/** 完成：记住默认模型 + 可选保存 Key + 标记已设置，然后刷新 */
async function setupDone() {
  const mid = $("setup-model").value;
  if (mid) {
    const m = (SETUP.models || []).find((x) => String(x.id) === String(mid)) || {};
    saveDefaultModel({ id: mid, providerID: m.providerID || "", variant: "default" });
  }
  // 向导里填了 Key 就顺手保存（留空则不动现有的）
  try {
    const kv = String(($("setup-key-value") || {}).value || "").trim();
    if (kv) await saveApiKey("setup");
  } catch { /* 存不上也不拦着走完向导 */ }
  setupMarkDone();
  closeSetup();
  setBanner(`首次设置完成：引擎 ${SETUP.engine || $("setup-engine").value}${mid ? " · 模型 " + mid : ""}`);
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
}
function closeAgentsNew() {
  $("agents-new").hidden = true;
  AINST.stop();                  // 关掉弹窗就别再轮询安装进度（装还是会继续）
}

/* ---------------- ★面板内安装 Agent（联网 npm；装到 agents\npm\<id>） ---------------- */

const AINST = {
  items: [], job: null, timer: null, err: "",
  stop() { if (this.timer) { clearTimeout(this.timer); this.timer = null; } },
};

async function loadInstallCatalog() {
  const note = $("anew-i-note");
  try {
    const r = unwrap(await api("/agent/catalog")) || {};
    AINST.items = r.items || [];
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
}

function renderInstallCatalog() {
  const box = $("anew-i-list");
  if (!box) return;
  if (!AINST.items.length) {
    box.innerHTML = `<div class="muted" style="padding:4px 0">${esc(AINST.err || "没有可安装项")}</div>`;
    return;
  }
  const busy = !!(AINST.job && AINST.job.busy);
  box.innerHTML = AINST.items.map((it) => {
    const tag = it.installed
      ? `<span class="anew-i-tag ok">已装</span>`
      : (it.ready ? "" : `<span class="anew-i-tag bad">缺 Node</span>`);
    const btn = it.installed
      ? `<button type="button" class="anew-i-b" data-ainst-off="${esc(it.id)}" title="从注册表移除并删掉装的文件">卸载</button>`
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
  const head = j.busy ? `正在安装 · ${esc(j.id)}（${esc(j.phase || "…")}）`
    : (j.ok ? `装好了：${esc(j.agent || j.id)}` : `安装失败（${esc(j.error || j.phase || "")}）`);
  box.innerHTML = `<div class="anew-i-lh${j.ok ? " ok" : (j.busy ? "" : " bad")}">${head}</div>`
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
        setBanner(`已安装并注册：${j.agent || j.id}（可以在「选择 Agent」里切过去）`);
      } else if (j.error) {
        setBanner("安装失败：" + j.error, { sticky: true });
      }
    } catch (e) {
      AINST.stop();
    }
  }, 1000);
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
}
function closeAgents() {
  $("agents").hidden = true;
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
function editAgentCommand(id) {
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
  $("ae-msg").textContent = a.provider || !(a.providerGuess)
    ? "改命令 / provider 会重启这个 agent 的子进程"
    : `推断出的 provider 是「${a.providerGuess}」（点「自动」填入）`;
  $("agent-edit").hidden = false;
}

function closeAgentEdit() { $("agent-edit").hidden = true; AE.id = ""; }

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
   ["cfg-petals", SET.petals], ["cfg-zh", SET.zh], ["cfg-glass", SET.glass],
   ["cfg-turnsnap", SET.turnSnap]].forEach(([id, on]) => {
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
}
function cfgClose() {
  $("cfg").classList.remove("open");
  document.documentElement.classList.remove("cfg-open");
  syncDrawerOverlap();
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
  $("setup-next").addEventListener("click", setupNext);
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
  $("anew-folder").addEventListener("click", addFromFolder);
  $("anew-addall").addEventListener("click", addAllCandidates);
  // ★面板内安装 agent：点「安装」/「卸载」
  $("anew-i-list").addEventListener("click", (e) => {
    const go = e.target.closest && e.target.closest("[data-ainst-go]");
    if (go && !go.disabled) { agentInstall(go.dataset.ainstGo); return; }
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
  const impTabs = $("imp-tabs");
  if (impTabs) impTabs.addEventListener("click", (e) => {
    const b = e.target.closest("[data-imp-tab]");
    if (!b) return;
    IMP.tab = String(b.dataset.impTab) === "internal" ? "internal" : "project";
    renderImport();
  });
  $("import").addEventListener("click", (e) => { if (e.target === $("import")) closeImport(); });
  $("imp-list").addEventListener("click", (e) => {
    const row = e.target.closest("[data-imp]");
    if (row) doImport(row.dataset.imp);
  });
  $("quar-close").addEventListener("click", quarClose);
  $("quar-reload").addEventListener("click", quarLoad);
  $("quar").addEventListener("click", (e) => { if (e.target === $("quar")) quarClose(); });
  $("quar-open").addEventListener("click", async () => {
    const dir = (QUAR.stats && QUAR.stats.quarantine_dir) || "";
    if (!dir) { setBanner("隔离区目录还没生成（还没隔离过任何东西）"); return; }
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
  $("undo-close").addEventListener("click", undoClose);
  $("undo-reload").addEventListener("click", undoLoad);
  $("undo-run").addEventListener("click", undoRun);
  $("undo").addEventListener("click", (e) => { if (e.target === $("undo")) undoClose(); });
  $("undo-list").addEventListener("click", (e) => {
    const row = e.target.closest("[data-upath]");
    if (row) undoToggleFile(row);
  });
  $("cfg-key-save").addEventListener("click", () => saveApiKey("cfg"));
  $("setup-key-save").addEventListener("click", () => saveApiKey("setup"));
  $("ae-close").addEventListener("click", closeAgentEdit);
  $("ae-cancel").addEventListener("click", closeAgentEdit);
  $("ae-save").addEventListener("click", saveAgentEdit);
  $("ae-guess").addEventListener("click", guessAgentProvider);
  $("agent-edit").addEventListener("click", (e) => { if (e.target === $("agent-edit")) closeAgentEdit(); });
  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!$("quar").hidden) { quarClose(); return; }
    if (!$("undo").hidden) { undoClose(); return; }
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
  $("cfg-glass").addEventListener("click", () => setSetting("glass", !SET.glass));
  $("cfg-turnsnap").addEventListener("click", () => {
    setSetting("turnSnap", !SET.turnSnap);
    setBanner(SET.turnSnap
      ? "已打开「每轮工作区快照」：下次发消息前会先给工作目录留一次底（工具 →「撤销这一轮」）"
      : "已关闭「每轮工作区快照」：只保留破坏性操作的隔离区");
  });
  $("cfg-zh").addEventListener("click", () => {
    setSetting("zh", !SET.zh);
    MUSIC.lyrKey = "";                       // 开关变了要重新取一次歌词（tr=0/1 不同）
    ensureLyrics();
  });
  $("cfg-wall-root-pick").addEventListener("click", pickWallpaperRoot);
  $("cfg-wall-root-auto").addEventListener("click", () => saveWallpaperRoot(""));
  $("cfg-wall").addEventListener("click", () => { close(); openWallpapers(); });
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
}

async function initLiveBg() {
  const root = document.documentElement;
  const box = $("live-bg");
  const A = $("live-video"), B = $("live-video2");
  if (!box || !A || !B) return;
  const vids = [A, B];
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)");
  let urls = null;
  try { urls = await api("/live/wallpapers"); } catch { urls = null; }

  let seq = 0;                 // 切换序号：快速反复切时让上一轮流程自己作废
  let active = A;              // 当前显示的那一层（正在播 / 暂停着当背景）
  let curTheme = null;
  let rampTimer = null;
  let fadeTimer = null;

  const themeNow = () => "hiru";      // 夜主题已删除
  const urlOf = (t) => ((urls && urls[t]) || "");
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

  const sync = () => {
    const t = themeNow();
    const url = urlOf(t);
    const collapsed = root.dataset.sidebar === "closed";
    const mine = ++seq;

    if (!url || reduce.matches || !SET.live) { hideAll(); return; }
    box.style.backgroundImage = "";
    root.classList.add("has-live");

    if (active.dataset.src === url) {    // 已经是这张：只调播放状态
      if (active.readyState >= 2) active.classList.add("ready");
      play(t, collapsed, mine);
      return;
    }
    swap(t, url, collapsed, mine);
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
  setInterval(reload, 5 * 60 * 1000);
  sync();
}

/** 静态背景：两层图按主题交叉淡入（换主题只切 opacity，避免大图重解码/光栅化卡顿） */
function initStaticBg() {
  const root = document.documentElement;
  const apply = () => {
    const t = "hiru";                    // 夜主题已删除：静态背景只有一个槽
    document.querySelectorAll("#static-bg .sb").forEach((el) => el.classList.toggle("on", el.dataset.bg === t));
  };
  new MutationObserver(apply).observe(root, { attributes: true, attributeFilter: ["data-theme"] });
  apply();
}

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
}

async function pickWallpaper(theme, id) {
  const r = await api("/live/pick", { method: "POST", body: JSON.stringify({ theme, id }) });
  if (WALL.data) WALL.data.pick = (r && r.pick) || {};
  renderWallpapers();
  if (liveReload) await liveReload();            // 立刻换片，不用等 5 分钟
}

function initWallpaperPicker() {
  const close = () => { $("wp").hidden = true; };
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
}

function initModelPicker() {
  const close = () => { $("mdl").hidden = true; };
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
  if (!on) { if (updPollT) { clearInterval(updPollT); updPollT = null; } return; }
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
  updPollT = setInterval(tick, 2000);
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
  runWfFilter: "",            // 运行历史只看某个工作流
  canvas: { zoom: 1, pan: { x: 20, y: 14 }, nodes: {}, sel: "", drag: null, panning: null,
            saveTimer: 0 },
};

const COLLAB_ST = {
  wait: "等待中", running: "运行中", done: "完成", failed: "失败",
  timeout: "超时", cancelled: "已取消",
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
  } else {
    collabStopPoll();
    collabStopRunsPoll();
    collabPermReset();
  }
}

function collabClose() { collabOpen(false); }

/** 左栏历史每 4s 轻刷一次：**同时跑多个任务**时，不选中的那些也能看到状态变化。 */
function collabStartRunsPoll() {
  collabStopRunsPoll();
  if (!COLLAB.open) return;
  COLLAB.runsPoll = setInterval(() => {
    if (COLLAB.open) collabLoadRuns(false);
  }, 4000);
}
function collabStopRunsPoll() {
  if (COLLAB.runsPoll) { clearInterval(COLLAB.runsPoll); COLLAB.runsPoll = 0; }
}

function collabStartPoll() {
  collabStopPoll();
  if (!COLLAB.open || !COLLAB.runId) return;
  COLLAB.wasActive = collabRunActive(COLLAB.run);
  COLLAB.poll = setInterval(collabTick, 1500);
}

function collabStopPoll() {
  if (COLLAB.poll) { clearInterval(COLLAB.poll); COLLAB.poll = 0; }
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

/** 画布布局：缺失的节点自动给一个位置；清理已经不存在的成员。 */
function collabWfLayout(wf) {
  const L = (wf && wf.layout) || {};
  const nodes = Object.assign({}, L.nodes || {});
  const ms = (wf && wf.members) || [];
  const ids = new Set(["task"]);
  ms.forEach((m) => ids.add(String(m.id)));
  Object.keys(nodes).forEach((k) => { if (!ids.has(k)) delete nodes[k]; });
  if (!nodes.task) {
    const n = Math.max(1, ms.length);
    nodes.task = { x: 40, y: Math.round(28 + ((n - 1) * CV_NODE.gapY) / 2 - 4) };
  }
  let y = 28;
  ms.forEach((m) => {
    const id = String(m.id);
    if (!nodes[id]) nodes[id] = { x: m.role === "reviewer" ? 620 : 360, y: y };
    y += CV_NODE.gapY;
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
    const first = COLLAB.workflows[0];
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

function collabRenderWfList() {
  const box = $("collab-wf-list");
  const cnt = $("collab-wf-count");
  if (!box) return;
  const list = COLLAB.workflows || [];
  if (cnt) cnt.textContent = list.length ? (list.length + " 个") : "";
  if (COLLAB.wfErr) {
    box.innerHTML = `<div class="collab-empty"><span>${esc(COLLAB.wfErr)}</span></div>`;
    return;
  }
  if (!list.length) {
    box.innerHTML = `<div class="collab-empty"><span>还没有工作流 —— 点左栏「工作流 ＋」建一个（成员 / 目录 / 默认任务都可复用）。</span></div>`;
    return;
  }
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
  box.innerHTML = groups.map((g) => `<div class="collab-wf-group" title="${esc(g.dir)}">${esc(g.name)}</div>`
    + g.items.map((w) => {
      const n = (w.members || []).length;
      const last = collabWfLastRun(w);
      const st = last ? (COLLAB_ST[String(last.status || "")] || String(last.status || "")) : "未跑过";
      return `<div class="collab-wf-row${String(w.id) === String(COLLAB.wfId) ? " on" : ""}"`
        + ` data-wf="${esc(w.id)}" title="${esc((w.name || "") + (g.dir ? "\n" + g.dir : ""))}">`
        + `<span class="collab-dot ${last ? esc(String(last.status || "")) : ""}"></span>`
        + `<span class="collab-wf-t"><b>${esc(w.name || "未命名工作流")}</b>`
        + `<i>${esc(n + " 成员 · 跑过 " + (w.runCount || 0) + " 次 · " + st)}</i></span>`
        + `<button type="button" class="wrun" data-wf-run="${esc(w.id)}" title="用默认任务一键跑">▶</button>`
        + `</div>`;
    }).join("")).join("");
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
    const bits = [cnt + " 成员", "跑过 " + (wf.runCount || 0) + " 次"];
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
  collabRenderCanvas();
  collabRenderInspector();
}

/** 主区三选一：工作流 / 运行 / 空态。 */
function collabRenderMain() {
  const empty = $("collab-empty");
  const runBox = $("collab-run");
  const wfBox = $("collab-wf");
  if (!empty || !runBox || !wfBox) return;
  if (COLLAB.view === "run" && COLLAB.run) {
    empty.hidden = true; wfBox.hidden = true; runBox.hidden = false;
    return;
  }
  if (COLLAB.view === "wf" && collabWf()) {
    empty.hidden = true; runBox.hidden = true; wfBox.hidden = false;
    return;
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
    members: (wf.members || []).map((m, i) => ({
      id: m.id || ("m" + (i + 1)), role: m.role || "",
      name: m.name || "", engine: m.engine || "",
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
  const wf = {
    name: "新工作流", cwd: (typeof currentDir === "function" ? currentDir() : "") || "",
    taskTemplate: "",
    members: [
      { id: "m1", role: "implementer", name: "impl", engine: eng },
      { id: "m2", role: "reviewer", name: "rev",
        engine: String((agents[1] && agents[1].id) || eng) },
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
  wf.members = wf.members || [];
  wf.members.push({ id: id, role: "implementer", name: "m" + n, engine: eng });
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

/* ---- 画布渲染 ---- */

function collabNodePos(lay, id, dx, dy) {
  const p = (lay.nodes || {})[id] || { x: dx, y: dy };
  return p;
}

function collabRefreshCanvasStatus() {
  if (COLLAB.view !== "wf") return;
  const wf = collabWf();
  if (!wf) return;
  const run = collabWfRun(wf) || collabWfLastRun(wf);
  const nodes = document.querySelectorAll("#collab-cv-nodes [data-node]");
  nodes.forEach((el) => {
    const id = String(el.dataset.node || "");
    if (id === "task") return;
    const i = (wf.members || []).findIndex((m) => String(m.id) === id);
    if (i < 0) return;
    const rm = (run && (run.members || [])[i]) || null;
    const st = rm ? String(rm.status || "") : "";
    let stat = el.querySelector(".cv-n-stat");
    if (!st) { if (stat) stat.remove(); return; }
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
  const lay = collabWfLayout(wf);
  COLLAB.canvas.nodes = lay.nodes;
  const inner = $("collab-canvas-inner");
  if (inner) {
    inner.style.transform = `translate(${COLLAB.canvas.pan.x}px, ${COLLAB.canvas.pan.y}px) scale(${COLLAB.canvas.zoom})`;
  }
  const run = collabWfRun(wf) || collabWfLastRun(wf);
  const task = collabNodePos(lay, "task", 40, 130);
  const nodes = [`<div class="cv-node cv-task${COLLAB.canvas.sel === "task" ? " sel" : ""}"`
    + ` data-node="task" style="left:${task.x}px;top:${task.y}px">`
    + `<span class="cv-tag">任务</span><b>${esc(wf.name || "工作流")}</b>`
    + `<span class="cv-n-eng" title="${esc(wf.taskTemplate || "")}">${esc(wf.taskTemplate || "（运行时临时输入）")}</span>`
    + `</div>`];
  (wf.members || []).forEach((m, i) => {
    const p = collabNodePos(lay, String(m.id), m.role === "reviewer" ? 620 : 360, 28 + i * CV_NODE.gapY);
    const rm = (run && (run.members || [])[i]) || null;
    const st = rm ? String(rm.status || "") : "";
    const zh = st ? (COLLAB_ST[st] || st) : "";
    nodes.push(`<div class="cv-node cv-member${COLLAB.canvas.sel === String(m.id) ? " sel" : ""}"`
      + ` data-node="${esc(m.id)}" style="left:${p.x}px;top:${p.y}px">`
      + `<button type="button" class="cv-x" data-cv-del="${esc(m.id)}" title="删掉这个成员">×</button>`
      + `<span class="cv-role">${esc(collabRoleLabel(m.role) || "成员")}</span>`
      + `<b>${esc(m.name || m.id)}</b>`
      + `<span class="cv-n-eng">${esc(m.engine || "（未选大脑）")}</span>`
      + (zh ? `<span class="cv-n-stat ${esc(st)}">${esc(zh)}</span>` : "")
      + `</div>`);
  });
  box.innerHTML = nodes.join("");
  collabPaintEdges();
}

function collabPaintEdges() {
  const svg = $("collab-cv-edges");
  const wf = collabWf();
  if (!svg || !wf) return;
  const nodes = COLLAB.canvas.nodes || {};
  const ms = (wf.members || []).map((m) => ({ id: String(m.id), role: m.role }));
  const task = nodes.task || { x: 40, y: 130 };
  const tR = task.x + CV_NODE.taskW, tC = task.y + CV_NODE.h / 2;
  const parts = [];
  ms.forEach((m) => {
    const p = nodes[m.id];
    if (!p) return;
    const x2 = p.x, y2 = p.y + CV_NODE.h / 2;
    parts.push(`<path d="M${tR} ${tC} C${tR + 80} ${tC}, ${x2 - 80} ${y2}, ${x2} ${y2}"/>`);
  });
  const rev = ms.find((m) => m.role === "reviewer");
  if (rev && nodes[rev.id]) {
    const rp = nodes[rev.id];
    const ax = rp.x, ay = rp.y + CV_NODE.h / 2;
    ms.filter((m) => m.role !== "reviewer").forEach((m) => {
      const p = nodes[m.id];
      if (!p) return;
      const x1 = p.x + CV_NODE.w, y1 = p.y + CV_NODE.h / 2;
      parts.push(`<path class="dash" d="M${x1} ${y1} C${x1 + 60} ${y1}, ${ax - 60} ${ay}, ${ax} ${ay}"/>`);
    });
  }
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
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  keys.forEach((k) => {
    const p = nodes[k];
    const w = k === "task" ? CV_NODE.taskW : CV_NODE.w;
    minX = Math.min(minX, p.x); minY = Math.min(minY, p.y);
    maxX = Math.max(maxX, p.x + w); maxY = Math.max(maxY, p.y + CV_NODE.h);
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
  box.hidden = false;
  box.innerHTML = `<b>成员</b>`
    + `<label><span>角色</span><select data-insp-role>${roleOpts}</select></label>`
    + `<label><span>名字</span><input data-insp-name maxlength="24" value="${esc(m.name || "")}" autocomplete="off"></label>`
    + `<label><span>大脑</span><select data-insp-engine>${engOpts}</select></label>`
    + `<label><span>权限</span><select data-insp-perm title="只对这一个成员生效">${permOpts}</select></label>`
    + `<div class="insp-msg">「信任」= 非破坏性不再逐条问；破坏性仍要确认。改完自动保存。</div>`;
}

function collabInspectorChanged(e) {
  const wf = collabWf();
  if (!wf) return;
  const t = e.target;
  const sel = COLLAB.canvas.sel;
  const m = (wf.members || []).find((x) => String(x.id) === String(sel));
  let hit = false;
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
  const nodeEl = e.target.closest("[data-node]");
  if (nodeEl && !e.target.closest("button,input,textarea,select")) {
    e.preventDefault();
    const id = nodeEl.dataset.node;
    const p = (COLLAB.canvas.nodes || {})[id];
    if (!p) return;
    const start = { x: e.clientX, y: e.clientY, ox: p.x, oy: p.y };
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
  const nodeEl = e.target.closest("[data-node]");
  if (!nodeEl) { collabAddMember(); return; }
  COLLAB.canvas.sel = nodeEl.dataset.node;
  collabRenderCanvas();
  collabRenderInspector();
}

function collabCanvasClick(e) {
  const del = e.target.closest("[data-cv-del]");
  if (del) {
    e.stopPropagation();
    collabDeleteMember(del.dataset.cvDel);
    return;
  }
  if (COLLAB.canvas.justDragged) return;
  const nodeEl = e.target.closest("[data-node]");
  if (!nodeEl) return;
  COLLAB.canvas.sel = nodeEl.dataset.node;
  collabRenderCanvas();
  collabRenderInspector();
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
  const strip = $("collab-mem");
  if (strip) {
    strip.hidden = !multi;
    if (multi) {
      strip.innerHTML = ms.map((m) => {
        const st = String(m.status || "wait");
        const on = showAll ? "" : (String(m.id) === sel ? " on" : "");
        return `<button type="button" class="collab-mem-chip${on}" data-collab-mem="${esc(m.id)}"`
          + ` title="${esc((m.roleLabel ? m.roleLabel + " · " : "") + (m.engine || "") + " · 权限：" + collabPermLabel(m.permLevel))}">`
          + `<i class="dot ${esc(st)}"></i>`
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

/* ---------------- 事件接线 ---------------- */

function initCollab() {
  const btn = $("btn-collab");
  if (btn) btn.addEventListener("click", () => collabOpen(!collabIsOpen()));
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
  const runsAll = $("collab-runs-all");
  if (runsAll) runsAll.addEventListener("click", () => {
    COLLAB.runWfFilter = "";
    collabRenderRuns();
  });

  // ★左栏：工作流（点选中；▶ 用默认任务一键跑）
  const wfList = $("collab-wf-list");
  if (wfList) wfList.addEventListener("click", (e) => {
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
  if (insp) insp.addEventListener("change", collabInspectorChanged);

  // 窗口尺寸变化 → 画布重新适应（节流）
  window.addEventListener("resize", () => {
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
    const rerun = e.target.closest("[data-collab-rerun]");
    if (rerun) { collabRerun(); return; }
  });
  if (root) root.addEventListener("change", (e) => {
    const sel = e.target.closest("#collab-arb-engine");
    if (sel) COLLAB.arbEngine = String(sel.value || "");
    const lv = e.target.closest("[data-perm-level]");
    if (lv) collabSetMemberPerm(lv.dataset.permLevel, lv.value);
  });


  window.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (collabIsOpen() && COLLAB.permOpen) { collabPermToggle(false); return; }
    if (collabIsOpen()) collabClose();
  });
}


(async function boot() {
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
  connectionLoop();
  setInterval(loadSessions, 20000);
  // 首次启动：要求选默认引擎 / agent / 模型（跳过或完成后不再自动弹）
  try { if (localStorage.getItem(SETUP_KEY) !== "1") setTimeout(openSetup, 350); } catch { /* 隐私模式 */ }
})();

/* 标记：app.js 已成功执行。index.html 里的兜底脚本靠它判断要不要自动重试。 */
window.__appLoaded = true;
try { sessionStorage.removeItem("cocraft.appReloads"); } catch (e) { /* ignore */ }
