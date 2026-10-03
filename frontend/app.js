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
  follow: true,         // 是否跟随最新内容自动往下滚
  refreshing: false,
  pendingRefresh: false,
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
  if (s.cost != null) row("花费", esc(Number(s.cost).toFixed(6)));
  row("结果", esc(s.outcome));
  return rows.join("") || '<div><b>详情</b><span>暂无</span></div>';
}

/** 会话属于哪个项目：取工作目录的最后一段当项目名（拿不到就留空）。
 *  ⚠ 只显示目录名，完整路径依旧只在「详细信息」里出现。 */
function projectNameOf(dir) {
  const d = String(dir || "").replace(/[\\/]+$/, "");
  if (!d) return "";
  const parts = d.split(/[\\/]/);
  return parts[parts.length - 1] || d;
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
    return `<div class="session${active}" data-id="${esc(s.id)}" title="${esc(s.title || "(无标题)")}">
      <button type="button" class="info" data-info="${esc(s.id)}"
              title="${tip}" aria-label="${tip}">${open ? "▾" : "▸"}</button>
      <button type="button" class="del" data-del="${esc(s.id)}"
              title="删除这个会话" aria-label="删除会话">×</button>
      <span class="t">${esc(s.title || "(无标题)")}</span>
      <span class="s-row">
        <span class="s">${esc(when)}${s.outcome ? " · " + esc(s.outcome) : ""}</span>
        ${proj ? `<span class="proj" title="项目：${esc(proj)}">${esc(proj)}</span>` : ""}
      </span>
      <div class="det"${open ? "" : " hidden"}>${sessionDetailHtml(s)}</div>
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
  return `<details class="part" data-k="${esc(key)}">
    <summary><span class="tag ${cls}">${esc(st)}</span> ${esc(part.name || "tool")}</summary>
    <div class="body">${esc(input)}${out ? "\n\n" + esc(String(out).slice(0, 20000)) : ""}</div>
  </details>`;
}

function renderReasoningPart(part, key, msgId) {
  const t = part.text || "";
  const head = t.slice(0, 56).replace(/\s+/g, " ");
  // ⚠ `auto-open` 只是样式，**不会**把 <details> 打开（要 `open` 属性）——
  //   以前思考被默认折叠、看着像"没有思考过程"。现在生成中与完成后都默认展开
  //   （`.body` 有 max-height:420px，长思考内部滚动，不会撑爆版面）。
  if (part.live) {
    return `<details class="part auto-open" open data-k="${esc(key)}">
      <summary><span class="tag thinking">思考中…</span> ${esc(head)}</summary>
      <div class="body stream"><span data-live-reasoning data-msgid="${esc(msgId)}">${esc(t)}</span></div>
    </details>`;
  }
  if (!t.trim()) return "";
  return `<details class="part auto-open" open data-k="${esc(key)}">
    <summary><span class="tag">思考</span> ${esc(head)}…</summary>
    <div class="body">${esc(t)}</div>
  </details>`;
}

/** Codex 在自定义 provider 下会把 `Warning: Model metadata for ... not found...` 当成回复正文开头，
 *  纯噪音 —— 展示时去掉开头这一段（实时与落库都过一遍）。 */
const MODEL_WARN_RE = /^\s*Warning: Model metadata for `[^`]*` not found\.[^\n]*\n+/i;
function stripModelWarning(t) { return String(t ?? "").replace(MODEL_WARN_RE, ""); }

function renderMessage(m) {
  const type = m.type || "assistant";
  if (type === "idle") return "";

  // 系统类提示：做成细分隔线
  if (type === "system" || type === "location-switched" || type === "synthetic") {
    const label = type === "synthetic" ? "自动续写"
      : type === "location-switched" ? "工作目录已切换" : "系统";
    const body = (m.text || "").trim();
    return `<div class="notice"><span class="line"></span>
      <span class="label">${esc(label)}${body ? "：" + esc(body.slice(0, 160)) : ""}</span>
      <span class="line"></span></div>`;
  }

  let parts = "";
  if (Array.isArray(m.content) && m.content.length) {
    parts = m.content.map((p, i) => {
      if (!p || typeof p !== "object") return "";
      const key = `${m.id}:${i}`;
      if (p.type === "text") {
        if (p.live) {
          return `<div class="text"><span data-live-text data-msgid="${esc(m.id)}">`
            + `${renderText(stripModelWarning(p.text))}</span><span class="caret"></span></div>`;
        }
        return `<div class="text">${renderText(stripModelWarning(p.text))}</div>`;
      }
      if (p.type === "reasoning") return renderReasoningPart(p, key, m.id);
      if (p.type === "tool") return renderToolPart(p, key);
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

  if (!parts && !files) {
    const finished = !!(m.time && m.time.completed);
    // 助手这一轮 content 为空：**没结束就先别说"没有回答"**（流式还没出字时就是空的）
    if (type === "assistant" && Array.isArray(m.content) && m.content.length === 0) {
      if (!finished) {
        return `<div class="msg assistant"><div class="who"><strong>${esc(type)}</strong></div>
          <div class="bubble"><div class="text muted">正在思考…</div></div></div>`;
      }
      return `<div class="msg assistant"><div class="who"><strong>${esc(type)}</strong></div>
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

  return `<div class="msg ${isUser ? "user" : "assistant"}${m.pending ? " pending" : ""}">
    <div class="who">
      ${isUser ? "" : `<img class="avatar" src="${appearanceUrl("avatar", "/assets/avatar.png")}" alt="">`}
      <strong>${isUser ? "我" : "助手"}</strong>
      ${agent ? `<span class="badge">${esc(agent)}</span>` : ""}
      ${modelID ? `<span class="badge">${esc(modelID)}</span>` : ""}
      ${m.time && m.time.created ? `<span class="muted">${fmtTime(m.time.created)}</span>` : ""}
      ${m.pending ? `<span class="tag run">发送中</span>` : ""}
      ${streaming ? `<span class="tag run">生成中</span>` : ""}
      ${!isUser ? `<button type="button" class="copy-btn" data-copy="${esc(m.id)}" title="复制这条回答的正文（不含思考过程）">复制</button>` : ""}
    </div>
    <div class="bubble">${parts}${files}</div>
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
function renderMessages() {
  const pending = pendingLiveIds();
  const empty = !state.messages.length && !pending.length;

  const sSig = signatureOf(state.messages);
  if (sSig !== staticSig) {
    staticSig = sSig;
    staticHtml = state.messages.map(renderMessage).filter(Boolean).join("");
    heroShown = false;
    swapHtml($("msg-static"), staticHtml);
  }
  if (empty && !heroShown) { swapHtml($("msg-static"), heroHtml()); heroShown = true; }
  else if (!empty && heroShown) { swapHtml($("msg-static"), staticHtml); heroShown = false; }

  paintLiveSlot();
}

/** 只重建「生成中」那一小块：我发的消息（乐观回显）→ 思考 → 回答，一路向下生长 */
function paintLiveSlot() {
  const box = $("messages");
  const slot = $("live-slot");
  const ids = pendingLiveIds();

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
    slot.innerHTML = pendingHtml + liveHtml;
    slot.querySelectorAll("details.part.auto-open").forEach((d) => { d.open = true; });
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
  renderAttachRow();
  $("session-title").textContent = state.current.title || "(无标题)";
  // 会话名下面不再显示 session id / 本地路径（详情请用侧栏会话项的 ▸ 展开）
  $("session-meta").textContent = "";
  updateModelBtn();                                  // 顶栏模型按钮跟着当前会话走
  renderModeButton();                                // 顶栏模式按钮跟着当前会话走
  loadModes();                                       // ACP 的模式清单挂在会话上，所以拿到会话后再拉一次
  $("input").disabled = false;
  $("btn-send").disabled = false;
  $("btn-stop").disabled = false;
  renderSessions();
  await refreshMessages();
  repairStaleModel(state.current);   // 会话绑的模型下架了 → 提示 + 自动切默认（否则"发了没反应"）
  pollAsks();
}

async function refreshMessages() {
  if (!state.current) return;
  if (state.refreshing) { state.pendingRefresh = true; return; }
  state.refreshing = true;
  try {
    const body = await api(`/api/session/${encodeURIComponent(state.current.id)}/message?limit=80`);
    // ⚠ 接口返回是「新 → 旧」（最新在索引 0），必须按 time.created 升序重排；
    //   少了这一步，界面就会变成「最新在顶、最旧在底」。
    state.messages = (unwrap(body) || []).slice().sort((a, b) =>
      (((a.time || {}).created) || 0) - (((b.time || {}).created) || 0));
    // 对账：权威数据里已有的消息，就丢掉对应的生成缓冲
    const known = new Set(state.messages.map((m) => m.id));
    for (const id of Object.keys(state.live)) if (known.has(id)) delete state.live[id];
    for (const id of Object.keys(state.reveal)) if (known.has(id)) delete state.reveal[id];
    for (const id of Object.keys(state.frozen)) if (known.has(id)) delete state.frozen[id];
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
    await api(`/api/session/${encodeURIComponent(state.current.id)}/prompt`, {
      method: "POST",
      body: JSON.stringify(files.length ? { text, files } : { text }),
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

/* ---------------- 新建会话：先选工作目录 ---------------- */

const NS_DIR_KEY = "cocraft.newsession.dir";

function lastNewSessionDir() {
  try { return localStorage.getItem(NS_DIR_KEY) || ""; } catch { return ""; }
}
function saveNewSessionDir(dir) {
  try {
    if (dir) localStorage.setItem(NS_DIR_KEY, dir);
    else localStorage.removeItem(NS_DIR_KEY);
  } catch { /* 隐私模式 */ }
}

/** 点「＋ 新会话」→ 弹目录弹窗（不直接建：先让用户确认/选择工作目录） */
function openNewSession() {
  const box = $("newsess");
  if (!box) return;
  const cur = (state.current && state.current.location
    && state.current.location.directory) || "";
  const dir = lastNewSessionDir() || cur;
  $("ns-dir").value = dir;
  $("ns-title").value = "";
  $("ns-note").textContent = dir ? "沿用上次用的目录（可改；清空 = 引擎默认）" : "";
  box.hidden = false;
  setTimeout(() => { try { $("ns-dir").focus(); } catch { /* ignore */ } }, 0);
}

function closeNewSession() { const b = $("newsess"); if (b) b.hidden = true; }

/** 「选择…」→ 原生文件夹框（POST /pick/folder） */
async function pickNewSessionDir() {
  try {
    const r = await api("/pick/folder", { method: "POST" });
    if (r && r.ok && r.path) { $("ns-dir").value = r.path; $("ns-note").textContent = ""; }
    else if (r && r.canceled) { /* 用户取消，什么都不做 */ }
    else setBanner("选择目录失败：" + ((r && r.error) || "未知错误"));
  } catch (err) {
    setBanner("选择目录失败：" + (err && err.message ? err.message : err));
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
      setBanner("本轮出错：" + msg + "（详见安装目录 runtime/logs/acp.log）");
      scheduleRefresh();
    }
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
    if (mine) scheduleRefresh();
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
  setBanner(`界面已更新到 ${served}：点这里刷新（或 Ctrl+Shift+R）`);
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

function setBanner(text) {
  const el = $("banner");
  if (el) el.classList.toggle("clickable", !!updateReady && !!text);
  if (!text) { el.hidden = true; el.textContent = ""; return; }
  if (el.textContent !== text) el.textContent = text;
  el.hidden = false;
}

async function checkHealth() {
  try {
    const body = await api("/healthz");
    $("upstream").textContent = `OpenCode ${body.version || ""}`;
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
    setBanner(updateReady ? `界面已更新到 ${updateReady}：按 Ctrl+Shift+R 刷新本页` : "");
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
    setBanner("与 OpenCode 的连接已断开，正在重连…（不需要手动刷新）");
  } else {
    setConn("error", "连接中");
    setBanner("正在连接 OpenCode…（它可能还在启动，本页面会自动重连）");
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

/* ---------------- 昼 / 夜 主题 ---------------- */

const THEME_KEY = "cocraft.theme";

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("btn-theme").textContent = theme === "yoru" ? "昼" : "夜";
  $("btn-theme").title = theme === "yoru"
    ? "切回：昼（白衣绯袴）"
    : "切到：夜（黑纹付羽织 · 黄金瞳）";
  try { localStorage.setItem(THEME_KEY, theme); } catch { /* 隐私模式忽略 */ }
}

function initTheme() {
  let theme = "hiru";
  try { theme = localStorage.getItem(THEME_KEY) || "hiru"; } catch { /* ignore */ }
  applyTheme(theme);
  $("btn-theme").addEventListener("click", () => {
    applyTheme(document.documentElement.dataset.theme === "yoru" ? "hiru" : "yoru");
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
    const t = $("tm-term"), c = $("tm-code");
    if (t) t.classList.toggle("on", !!TERM.open);
    if (c) c.classList.toggle("on", !!CODE.open);
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
    refresh();
  });
  document.addEventListener("click", (e) => {
    if (!menu.hidden && !menu.contains(e.target) && !btn.contains(e.target)) menu.hidden = true;
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") menu.hidden = true; });
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
  document.title = `${use} · OpenCode`;
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
const CC_W_DEFAULT = 520;

const CC = {
  base: "https://www.animaker.cc.cd",
  ws: "",                 // 由 GET /cocraft/_url 取（可被后端 env 覆盖）
  token: "",
  me: null,
  users: {},              // 用户信息缓存（见 ccCacheUser / ccUserName）
  chats: [],
  chatID: "",
  msgs: [],
  wsock: null,
  wsPing: null,
  wsRetry: 0,
  poll: null,
};

const ccEl = (id) => document.getElementById(id);
const ccLoad = (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } };
const ccStore = (k, v) => { try { localStorage.setItem(k, v); } catch { /* 隐私模式 */ } };

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
  const strip = $("chat-strip");
  if (strip) strip.setAttribute("aria-expanded", open ? "true" : "false");
  try { localStorage.setItem(CC_OPEN_KEY, open ? "1" : "0"); } catch { /* 隐私模式 */ }
  if (open && CC.token && CC.chatID && !CC.msgs.length) ccLoadMsgs();
  // 打开时重算用户名（缓存里可能已经有；没有的就补一次 /users/{id}）——
  // 否则名字可能是在抽屉关着的时候拉回来的，界面还停在「用户 id」。
  if (open) setTimeout(() => {
    ccRefreshUserUI();
    if (CC.me && CC.me.id != null && !((CC.users[String(CC.me.id)] || {}).username)) ccFetchUser(CC.me.id);
    CC.msgs.forEach((m) => ccUserName(m.sender_id));
  }, 0);
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

async function ccLoadChats() {
  try {
    const list = await ccFetch("/chats");
    CC.chats = Array.isArray(list) ? list : ((list && list.data) || []);
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
    return `<div class="cc-chat${String(c.id) === String(CC.chatID) ? " on" : ""}" data-chat="${esc(c.id)}">`
      + `<b>${esc(ccChatName(c))}</b><i>${n ? n + " 人" : ""}</i></div>`;
  }).join("");
}

/* ---- 聊天内容 ---- */
async function ccOpenChat(id) {
  CC.chatID = String(id);
  CC.msgs = [];
  ccMenuOpen(false);
  ccRenderChats();
  ccRenderRoomHead();
  await ccLoadMsgs();
}

function ccRenderRoomHead() {
  const title = ccEl("cc-room-title");
  if (!title) return;
  const c = CC.chats.find((x) => String(x.id) === String(CC.chatID));
  if (!c) { title.textContent = "选一个会话"; return; }
  const names = (c.members || []).map((m) => (m && typeof m === "object") ? (m.username || m.id) : m);
  title.innerHTML = `${esc(ccChatName(c))} <span class="muted">${esc(names.join("、"))}</span>`;
}

async function ccLoadMsgs(toBottom) {
  if (!CC.chatID) return;
  try {
    const page = await ccFetch("/chats/" + encodeURIComponent(CC.chatID) + "/messages?limit=100&offset=0");
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

function ccRenderMsgs(toBottom) {
  const box = ccEl("cc-msgs");
  if (!box) return;
  if (!CC.msgs.length) { box.innerHTML = `<div class="cc-empty">还没有消息</div>`; return; }
  box.innerHTML = CC.msgs.map((m) => {
    const mine = CC.me && String(m.sender_id) === String(CC.me.id);
    const who = mine ? "我" : ccUserName(m.sender_id);
    const t = m.created_at ? String(m.created_at).replace("T", " ").slice(0, 16) : "";
    const body = m.message_type === "file" ? ccRenderFileBody(m) : esc(m.content || "");
    return `<div class="cc-m${mine ? " me" : ""}"><span class="cc-mt">${esc(who)}${t ? " · " + esc(t) : ""}</span>`
      + `<div class="cc-mb">${body}</div></div>`;
  }).join("");
  if (toBottom !== false) box.scrollTop = box.scrollHeight;
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
    ccNote(`已发送文件：${ccFileName((data && data.content) || file.name)}`);
    await ccLoadMsgs(true);
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
  try {
    CC.wsock.send(JSON.stringify({ type: "message", chat_id: Number(CC.chatID) || CC.chatID, content: t }));
    ccNote("");
    return true;
  } catch (e) { ccNote("发送失败：" + e.message, true); return false; }
}

/* ---- 会话操作菜单（☰）：邀请 / 移出 ---- */
function ccMenuOpen(open) {
  const pop = ccEl("cc-menu-pop");
  if (!pop) return;
  pop.hidden = !open;
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
    if (CC.chatID) { await ccLoadMsgs(); await ccRefreshChat(); }
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
      if (String(d.chat_id) === String(CC.chatID)) ccLoadMsgs();
      ccLoadChats();
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

/* ---- 兜底轮询（WS 漏了也能补上） ---- */
function ccStartPoll() {
  ccStopPoll();
  CC.poll = setInterval(() => {
    if (!CC.token || !cocraftIsOpen()) return;
    ccLoadChats();
    if (CC.chatID) ccLoadMsgs(false);
  }, 15000);
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
  // 分屏：给面板至少留 300px（抽屉最大 = 视口 - 300）
  const max = Math.max(300, window.innerWidth - 300);
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
    dragging = true; startX = e.clientX; startW = box.getBoundingClientRect().width;
    box.classList.add("resizing");
    document.documentElement.classList.add("cc-resizing");
    try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    lastW = startW + (startX - e.clientX);          // 往左拖 = 变宽
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
  if (!$("cocraft") || !$("chat-strip")) return;
  $("chat-strip").addEventListener("click", toggleCocraft);
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
  $("cc-menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    ccMenuOpen(ccEl("cc-menu-pop").hidden);
  });
  $("cc-invite-go").addEventListener("click", ccInvite);
  $("cc-remove-go").addEventListener("click", ccRemoveMember);
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
  on: false, qqRunning: false, real: false, folded: false, panel: null, panelError: "", lyrWanted: null,
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

function renderPlayer() {
  const st = MUSIC.state || {};
  const bar = $("player");
  const audio = $("mus-audio");
  const panel = MUSIC.panel;                       // 面板内播放（网易云/QQ 搜索）优先显示
  const playing = panel ? !!(audio && !audio.paused) : musicPlaying();
  bar.classList.add("on");                     // 播放条常驻（折叠时只剩 ▾ 箭头）
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
  if (isErr) { ASK.banner = text; setBanner(text); }
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
  try { await api("/api/permission/level", { method: "POST", body: { level: lv } }); } catch { /* ignore */ }
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
  ).join("");
  document.body.appendChild(m);
  const r = b.getBoundingClientRect();
  m.style.left = Math.max(8, r.left) + "px";
  m.style.top = Math.max(8, r.top - m.offsetHeight - 6) + "px";
  m.addEventListener("click", (e) => {
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
function inWorkspace(paths) {
  const root = (typeof currentDir === "function" ? currentDir() : "") || "";
  if (!root) return true;
  const norm = (p) => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  const r = norm(root);
  for (const p of (paths || [])) {
    const n = norm(p);
    if (n && !(n === r || n.startsWith(r + "/"))) return false;
  }
  return true;
}
function permAllowedByLevel(pm) {
  const lv = PERM.level;
  if (!lv || lv === "ask") return false;
  if (lv === "all") return true;
  const hay = (String(pm.kind || "") + " " + String(pm.action || "") + " " + String(pm.message || "")).toLowerCase();
  const isRead = /read|search|fetch|think|list|glob|grep|view|cat|ls\b|dir\b/.test(hay);
  const isWrite = /edit|write|create|delete|move|rename|patch|mkdir|rm\b|touch|save/.test(hay);
  const isExec = /execute|exec|bash|shell|run\b|command|terminal|powershell|cmd/.test(hay);
  const ws = inWorkspace(pm.resources);
  if (lv === "read") return isRead && !isWrite && !isExec;
  if (lv === "edit") return (isRead || isWrite) && !isExec && ws;
  if (lv === "exec") return (isRead || isWrite || isExec) && ws;
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
  dimHiru: 100, dimYoru: 80, blur: 18, rate: 80,
  live: true, taskbar: true, spectrumSides: true, spectrumSidesH: 100, spectrumSidesGain: 120,
  petals: true, zh: true,
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
  root.style.setProperty("--bg-dim-hiru", (SET.dimHiru / 100).toFixed(2));
  root.style.setProperty("--bg-dim-yoru", (SET.dimYoru / 100).toFixed(2));
  root.style.setProperty("--bg-blur", Math.max(0, SET.blur) + "px");
  root.classList.toggle("bg-blurred", SET.blur > 0);   // 开模糊时把背景层撑出视口，避免四边糊边
  liveRate = Math.min(1.3, Math.max(0.5, SET.rate / 100));
  document.body.classList.toggle("no-petals", !SET.petals);
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
    renderAttachRow();
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

/* ---------------- 从 agent 导入会话（ACP 的 session/list）----------------
 * 背景：ACP 引擎的面板会话列表 = **我们自己的索引**（`_acp_sessions.json`），
 * 所以用 `opencode-acp` 接进 OpenCode 时，OpenCode 那边**早就有的会话看不到**
 * （反过来在 ACP 里新建的会话会写进 OpenCode 的库，所以"本地看的到"）。
 * 这里把 agent 侧的会话列出来，点一下就领进面板（历史留在 agent 那边，接着聊即可）。 */

const IMP = { list: [], cursor: null, supported: null, err: "" };

async function openImport() {
  const box = $("import");
  if (!box) return;
  box.hidden = false;
  $("imp-cur").textContent = "读取中…";
  $("imp-list").innerHTML = "";
  await loadImport();
  // 后端会在列会话时按 agent 侧自愈"接续会话的目录"，这里顺手刷新徽章
  loadSessions();
}

function closeImport() { const b = $("import"); if (b) b.hidden = true; }

async function loadImport() {
  try {
    const r = await api("/api/session/remote");
    IMP.supported = !!(r && r.supported);
    IMP.list = (r && r.sessions) || [];
    IMP.cursor = (r && r.cursor) || null;
    IMP.err = (r && r.error) || "";
  } catch (e) {
    IMP.supported = false;
    IMP.list = [];
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
  const n = IMP.list.length;
  const fresh = IMP.list.filter((x) => !x.imported).length;
  if (cur) cur.textContent = n
    ? `agent 侧共 ${n} 条${fresh ? `，其中 ${fresh} 条还没接续` : "（都已在面板里）"}`
    : "agent 侧没有会话";
  if (!n) {
    box.innerHTML = `<div class="muted" style="padding:10px">agent 那边也没有历史会话。</div>`;
    return;
  }
  box.innerHTML = IMP.list.map((s) => {
    const title = s.title || "(无标题)";
    const when = s.updatedAt ? String(s.updatedAt).replace("T", " ").slice(0, 16) : "";
    const hist = Number(s.messages || 0);
    const histTip = hist ? `<span class="muted" style="font-size:11px">对面 ${hist} 条历史</span>` : "";
    return `<div class="agents-row${s.imported ? " on" : ""}" data-imp="${esc(s.id)}">`
      + `<div class="an"><span class="dot ok"></span>${esc(title)}`
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
  const s = IMP.list.find((x) => x.id === remoteId);
  if (!s) return;
  if (s.imported && s.localID) { closeImport(); await openSession(s.localID); return; }
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
}
function closeAgentsNew() { $("agents-new").hidden = true; }

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
  AGENTS_BACK_TO_CFG = !$("cfg").hidden;      // 从设置里进来的 → 把设置收起，专心选 agent
  $("cfg").hidden = true;
  $("agents").hidden = false;
  renderAgents();
  loadAcpAgents();
}
function closeAgents() {
  $("agents").hidden = true;
  if (AGENTS_BACK_TO_CFG) $("cfg").hidden = false;   // 关掉 agentlist 回到设置
  AGENTS_BACK_TO_CFG = false;
}

/** 换 ACP agent 后：旧会话属于旧 agent，必须清空界面 + 重连 SSE
 *（否则拿旧 sessionId 去 prompt，对方回 -32603 Session not found）。 */
async function resetAfterAgentSwitch(msg) {
  setBanner(msg);
  state.current = null;
  state.messages = [];
  state.live = {};
  state.reveal = {};
  state.pending = [];
  state.attachments = [];
  staticSig = null;
  liveShellSig = null;
  heroShown = false;
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
  put("cfg-dim-yoru", SET.dimYoru, SET.dimYoru + "%");
  put("cfg-blur", SET.blur, SET.blur + "px");
  put("cfg-rate", SET.rate, (SET.rate / 100).toFixed(2) + "×");
  put("cfg-spec-h", SET.spectrumSidesH, SET.spectrumSidesH + "%");
  put("cfg-spec-gain", SET.spectrumSidesGain, SET.spectrumSidesGain + "%");
  [["cfg-live", SET.live], ["cfg-taskbar", SET.taskbar], ["cfg-spec-sides", SET.spectrumSides],
   ["cfg-petals", SET.petals], ["cfg-zh", SET.zh]].forEach(([id, on]) => {
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

function initSettings() {
  const close = () => { $("cfg").hidden = true; };
  const open = () => {
    $("cfg").hidden = false;
    renderSettings();
    musicStatus();
    musicComponent();                       // 音乐服务是可选项：没装就明说
    loadApiKeyRow("cfg");                   // 模型 API Key（只显示掩码）
    loadWallpaperRoot();                    // 目录可能被外部改过，每次打开都刷新
    loadEngineStatus();                     // 可用引擎 / 当前引擎（老后端会优雅降级）
    loadWindowPref();                       // 面板窗口形态（原生 / 浏览器）
  };
  $("btn-cfg").addEventListener("click", () => ($("cfg").hidden ? open() : close()));
  $("cfg-close").addEventListener("click", close);
  $("cfg").addEventListener("click", (e) => { if (e.target === $("cfg")) close(); });
  window.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("cfg").hidden) close(); });

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
  // 新会话：先弹「选择工作目录」
  $("ns-close").addEventListener("click", closeNewSession);
  $("ns-cancel").addEventListener("click", closeNewSession);
  $("ns-pick").addEventListener("click", pickNewSessionDir);
  $("ns-clear").addEventListener("click", () => {
    $("ns-dir").value = ""; $("ns-note").textContent = "已清空 → 用引擎默认目录";
  });
  $("ns-create").addEventListener("click", doCreateSession);
  $("newsess").addEventListener("click", (e) => { if (e.target === $("newsess")) closeNewSession(); });
  ["ns-dir", "ns-title"].forEach((id) => $(id).addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); doCreateSession(); }
    if (e.key === "Escape") { e.preventDefault(); closeNewSession(); }
  }));
  $("imp-close").addEventListener("click", closeImport);
  $("imp-reload").addEventListener("click", loadImport);
  $("import").addEventListener("click", (e) => { if (e.target === $("import")) closeImport(); });
  $("imp-list").addEventListener("click", (e) => {
    const row = e.target.closest("[data-imp]");
    if (row) doImport(row.dataset.imp);
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
    if (!$("mode").hidden) { closeModes(); return; }
    if (!$("import").hidden) { closeImport(); return; }
    if (!$("agent-edit").hidden) { closeAgentEdit(); return; }
    if (!$("agents-new").hidden) { closeAgentsNew(); return; }
    if (!$("agents").hidden) closeAgents();
  });
  $("cfg-dim-hiru").addEventListener("input", (e) => setSetting("dimHiru", Number(e.target.value)));
  $("cfg-dim-yoru").addEventListener("input", (e) => setSetting("dimYoru", Number(e.target.value)));
  $("cfg-blur").addEventListener("input", (e) => setSetting("blur", Number(e.target.value)));
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

  const themeNow = () => (root.dataset.theme === "yoru" ? "yoru" : "hiru");
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
    const t = root.dataset.theme === "yoru" ? "yoru" : "hiru";
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
    + "　｜　夜晚 " + (pick.yoru ? nameOf(pick.yoru) : "（默认）")
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
    return `<div class="wp-it${on("hiru")}${on("yoru")}">` + thumb
      + `<div class="wp-meta"><b>${esc(it.title || it.id)}</b>`
      + `<i>${it.size_mb} MB · ${esc(String(it.file).slice(0, 26))}</i></div>`
      + `<div class="wp-act">`
      + `<button data-slot="hiru" data-id="${esc(it.id)}"${sel("hiru")}>白昼</button>`
      + `<button data-slot="yoru" data-id="${esc(it.id)}"${sel("yoru")}>夜晚</button>`
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
    Promise.all([pickWallpaper("hiru", null), pickWallpaper("yoru", null)])
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
    return await musicPlay(list[idx].id, { play: true });
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
    const ok = await musicPlay(list[n].id, { play: true });
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

const MODEL = { list: null, loading: false };
const MODEL_KEY = "cocraft.model";

/* 模型官方图标（本地素材，由 tools/fetch_model_icons.py 下载；OpenCode 接口本身没有图标字段）。
   解析顺序：模型 id → family → family(去掉 -free) → providerID；都没有就用「青铜字母徽章」。
   muse-spark / big-pickle 没有公开品牌标 → 走字母徽章。 */
const MODEL_ICONS = {
  "deepseek": "deepseek", "deepseek-flash": "deepseek", "deepseek-thinking": "deepseek",
  "anthropic": "anthropic", "claude": "anthropic",
  "mimo": "xiaomi", "ling": "antgroup", "nemotron": "nvidia", "nemotron-free": "nvidia",
};
const MODEL_ICON_DIR = "/assets/models/";

/** 把会话里的轻量 model ref 与模型清单里的完整条目合并（拿 name / family） */
function modelFull(ref) {
  if (!ref) return null;
  const hit = (MODEL.list || []).find((m) => m.id === ref.id && m.providerID === ref.providerID);
  return hit ? Object.assign({}, hit, ref) : ref;
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
  const saved = defaultModel();
  if (saved && saved.id && saved.providerID) {
    const list = MODEL.list;
    if (!list || list.some((m) => m.id === saved.id && m.providerID === saved.providerID)) {
      return saved;
    }
  }
  let ref = null;
  try {
    const d = unwrap(await api("/api/model/default"));
    if (d && d.id) ref = { id: d.id, providerID: d.providerID };
  } catch { /* 后端不支持就算了 */ }
  if (!ref) {
    const list = await loadModels();
    const first = (list || []).find((m) => m && m.id && m.providerID);
    if (first) ref = { id: first.id, providerID: first.providerID };
  }
  if (ref) saveDefaultModel(ref);
  return ref;
}

/**
 * 确保某个会话已绑定**可用**的模型：没有就补默认；已绑但**已下架**也换成默认。
 * send() 之前调用，避免「没模型 / 模型失效 → 发出去空转、没有任何回答」。
 * 失败不抛异常（交给发送流程照常走，真失败会有 error-box）。
 */
async function ensureSessionModel(sid) {
  if (!sid) return null;
  const cur = (state.current && state.current.id === sid) ? state.current.model : null;
  if (cur && cur.id) {
    if (!MODEL.list || !MODEL.list.length) { try { await loadModels(); } catch { /* ignore */ } }
    if (modelAvailable(cur)) return cur;   // 已有且仍在清单里 → 不动
    // 模型已下架：落到下面重新解析默认并写回（否则发出去只会空转）
  }
  const ref = await resolveDefaultModel();
  if (!ref) return null;
  try {
    await api(`/api/session/${encodeURIComponent(sid)}/model`,
      { method: "POST", body: JSON.stringify({ model: ref }) });
    const m = { id: ref.id, providerID: ref.providerID, variant: ref.variant || "default" };
    if (state.current && state.current.id === sid) state.current.model = m;
    const s = state.sessions.find((x) => x.id === sid);
    if (s) s.model = m;
    updateModelBtn();
    return m;
  } catch { return null; }
}

/** 模型 ref 是否仍在当前清单里；清单还没加载时不做判断（避免误杀）。 */
function modelAvailable(ref) {
  if (!ref || !ref.id || !ref.providerID) return true;
  const list = MODEL.list;
  if (!list || !list.length) return true;
  return list.some((m) => m.id === ref.id && m.providerID === ref.providerID);
}

/**
 * 会话绑的模型已被下架（提供方删了/改名了）时：提示 + 自动切到默认模型。
 * ⚠ 不修的话，OpenCode 会 `ModelUnavailableError` → 只留一个 idle、**没有任何回复**，
 *   界面上完全看不出原因（只有服务端日志能查到）。返回是否真的切过。
 */
async function repairStaleModel(sess) {
  if (!sess || !sess.model || !sess.model.id) return false;
  if (!MODEL.list || !MODEL.list.length) { try { await loadModels(); } catch { /* ignore */ } }
  if (modelAvailable(sess.model)) return false;
  const oldText = modelText(sess.model);
  const ref = await resolveDefaultModel();
  if (!ref) {
    setBanner(`这个会话绑的模型 ${oldText} 已不可用；请点「模型」另选一个（当前没有可用模型）`);
    return false;
  }
  try {
    await api(`/api/session/${encodeURIComponent(sess.id)}/model`,
      { method: "POST", body: JSON.stringify({ model: ref }) });
  } catch {
    setBanner(`这个会话绑的模型 ${oldText} 已不可用，自动切换失败；请点「模型」另选一个`);
    return false;
  }
  const m = { id: ref.id, providerID: ref.providerID, variant: ref.variant || "default" };
  if (state.current && state.current.id === sess.id) state.current.model = m;
  const hit = state.sessions.find((x) => x.id === sess.id);
  if (hit) hit.model = m;
  updateModelBtn();
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
  b.innerHTML = modelIconHtml(cur);          // 用官方图标代替模型名（名字放 tooltip）
  b.title = cur ? `当前模型：${modelText(cur, true)}（点一下切换）` : "切换模型（对当前会话生效）";
}

async function loadModels() {
  if (MODEL.loading) return MODEL.list || [];
  MODEL.loading = true;
  try { MODEL.list = unwrap(await api("/api/model")) || []; }
  catch { MODEL.list = MODEL.list || []; }
  finally { MODEL.loading = false; }
  return MODEL.list;
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
  $("mdl-cur").textContent = state.current
    ? "当前会话：" + modelText(state.current.model, true)
    : (cur ? "未选会话 · 新会话默认：" + modelText(cur, true) : "未选会话 · 跟随引擎默认");
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
  if (!sid) {
    saveDefaultModel(ref);
    setBanner(`已设为新会话默认模型：${modelText(ref, true)}`);
    renderModels();
    updateModelBtn();
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

// 跟随策略：默认一路往下跟着最新内容；用户手动往上滚就暂停跟随，滚回底部再恢复
$("messages").addEventListener("wheel", (e) => {
  if (e.deltaY < 0) state.follow = false;
});
$("messages").addEventListener("scroll", () => {
  const b = $("messages");
  if (b.scrollHeight - b.scrollTop - b.clientHeight < 60) state.follow = true;
});

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
          + `<span class="tw">${open ? "▾" : "▸"}</span><span class="nm">${esc(it.name)}</span></div>`;
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
  const max = Math.max(300, window.innerWidth - 300);   // 给面板至少留 300px
  const nw = Math.max(300, Math.min(w, max));
  document.documentElement.style.setProperty("--code-w", nw + "px");
}

function codeInitResize() {
  const box = $("code"), grip = $("code-resize");
  if (!box || !grip) return;
  let dragging = false, startX = 0, startW = 0;
  grip.addEventListener("pointerdown", (e) => {
    dragging = true; startX = e.clientX; startW = box.getBoundingClientRect().width;
    box.classList.add("resizing");
    document.documentElement.classList.add("code-resizing");
    try { grip.setPointerCapture(e.pointerId); } catch { /* ignore */ }
    e.preventDefault();
  });
  grip.addEventListener("pointermove", (e) => {
    if (dragging) codeApplyWidth(startW + (startX - e.clientX));   // 往左拖 = 变宽
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

let UPD = { busy: false, latest: "" };

async function updCheck(force) {
  const note = $("upd-note"), cur = $("upd-cur"), apply = $("upd-apply"), chk = $("upd-check");
  if (chk) chk.disabled = true;
  try {
    const r = await api("/update/check" + (force ? "?force=1" : ""));
    const d = (r && r.data) || {};
    if (cur) { cur.textContent = d.current ? ("v" + d.current) : "开发环境"; cur.title = d.current || ""; }
    if (!d.supported) {
      if (note) note.textContent = d.reason || "开发环境 / 非安装版，不支持在线更新";
      if (apply) apply.hidden = true;
      return d;
    }
    if (!d.ok) {
      if (note) note.textContent = d.reason || "检查失败";
      if (apply) apply.hidden = true;
      return d;
    }
    if (d.hasUpdate) {
      UPD.latest = d.latest || "";
      if (note) note.textContent = "发现新版本 v" + d.latest + "（当前 v" + d.current + "）";
      if (apply) { apply.hidden = false; apply.textContent = "立即更新到 v" + d.latest; }
      if (force) setBanner("软件有新版本 v" + d.latest + "：点「立即更新」自动更新并重启");
    } else {
      if (note) note.textContent = "已是最新版本（v" + d.current + "）";
      if (apply) apply.hidden = true;
    }
    return d;
  } catch (err) {
    if (note) note.textContent = "检查失败：" + (err && err.message ? err.message : err);
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
  const apply = $("upd-apply");
  if (apply) { apply.disabled = true; apply.textContent = "正在下载…"; }
  setBanner("正在下载并静默更新，安装完成后面板会自动重启…");
  try {
    const r = await api("/update/apply", { method: "POST" });
    setBanner((r && r.message) || "已开始更新…");
  } catch (err) {
    UPD.busy = false;
    if (apply) { apply.disabled = false; apply.textContent = "立即更新"; }
    setBanner("更新失败：" + (err && err.message ? err.message : err));
  }
}

function initUpdate() {
  const chk = $("upd-check"), apply = $("upd-apply");
  if (chk) chk.addEventListener("click", () => updCheck(true));
  if (apply) apply.addEventListener("click", updApply);
  // 后台自动查一次（不打扰）；有新版就只在横幅提示，去设置里点更新
  setTimeout(() => {
    updCheck(false).then((d) => {
      if (d && d.hasUpdate && d.latest) {
        setBanner("软件有新版本 v" + d.latest + "：设置 →「软件更新」可一键更新");
      }
    }).catch(() => {});
  }, 5000);
}

(async function boot() {
  // 版本号：自动取本文件被加载时带的 ?v=（与服务端 /healthz 报告的版本比对，用来触发自动刷新）
  $("ui-ver").textContent = "ui " + (UI_VER || "?");

  migrateLegacyKeys();                   // 旧键（opencode-ui.*）→ 新键（cocraft.*）
  loadSettings();                        // 先应用设置（亮度/速度/落樱），再初始化各部件
  applySettings();

  initTheme();
  initToolsMenu();
  initTerm();
  initCode();
  initUpdate();
  // 更新提示：点横幅 / Ctrl+Shift+R 都能刷新（原生 WebView2 里快捷键不一定触发重载）
  const bnEl = $("banner");
  if (bnEl) bnEl.addEventListener("click", () => { if (updateReady) safeReload(); });
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === "R" || e.key === "r") && updateReady) {
      e.preventDefault();
      safeReload();
    }
  });
  initPetals();
  initSidebar();
  initBrandName();
  initAttachments();
  initCopy();
  initMusic();
  initLiveBg();
  initStaticBg();
  initCocraft();
  initWallpaperPicker();
  initMusicAudio();
  restoreMusicState();                   // 刷新后面板内歌曲信息 / 队列 / 进度恢复
  initSettings();
  // 自定义素材（头像 / 空会话主图与贴纸）：拉回来后再画一次首屏（接口没有就安静跳过）
  loadAppearance().then(() => refreshAppearanceArt());
  initModelPicker();
  initAsk();
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
