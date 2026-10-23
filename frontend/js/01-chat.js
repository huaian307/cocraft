/* 01-chat.js —— 全局状态 / 会话列表 / 消息渲染 / 流式 / 轨迹 / 连接 / 错误捕获（P1 从 app.js 拆出，纯搬运不改逻辑） */

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
        //   ⚠ 但**已经结束**的回复里，最后一段正文即使后面还跟着 tool 状态更新，也必须显示 ——
        //     Codex 常把「最终一段话」发完后再补一条 tool 完成状态；旧逻辑会把整条反馈吞掉
        //     （用户报："codex 完成工作后不会反馈"）。只有"后面还有别的正文"才算旁白。
        const later = contentArr.slice(i + 1);
        const hasToolAfter = later.some((q) => q && q.type === "tool");
        const hasTextAfter = later.some((q) => q && q.type === "text" && String(q.text || "").trim());
        const finishedMsg = !!(m.time && m.time.completed);
        if (hasToolAfter && !(finishedMsg && !hasTextAfter)) return "";
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

/** 写剪贴板：**先走同步 execCommand**（在点击手势里，WebView2 上最可靠），
 *  失败再退化到异步 Clipboard API。
 *  ⚠ 顺序不能反：先 `await navigator.clipboard.writeText` 再 execCommand 会**丢掉用户手势**，
 *    而 WebView2 里 clipboard API 常因没焦点/没权限 reject → 两步都失败（用户看到"复制失败"）。 */
async function copyToClipboard(text) {
  const s = String(text ?? "");
  if (!s) return false;
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
    if (ok) return true;
  } catch { /* 继续走 Clipboard API */ }
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(s);
      return true;
    }
  } catch { /* ignore */ }
  return false;
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
  if (!ok) {
    // 剪贴板两条路都失败（WebView2 没焦点 / 拒绝权限）→ 兜底：弹一个全选好的文本框
    showCopyFallback(s);
    flashCopied(btn, "请手动复制", false);
    return;
  }
  flashCopied(btn, "已复制", true);
}

/** 剪贴板不可用时的兜底：把内容放进一个已全选的 textarea（Ctrl+C 即可）。 */
function showCopyFallback(text) {
  const box = $("copy-fb"), ta = $("copy-fb-text");
  if (!box || !ta) return;
  ta.value = String(text || "");
  box.hidden = false;
  try { ta.focus(); ta.select(); } catch { /* ignore */ }
  OVERLAY.push("copy-fb", closeCopyFallback);
}

function closeCopyFallback() {
  const box = $("copy-fb");
  if (box) box.hidden = true;
  OVERLAY.pop("copy-fb");
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
  const closeFb = () => closeCopyFallback();
  const fb = $("copy-fb");
  if (fb) fb.addEventListener("click", (e) => { if (e.target === fb) closeFb(); });
  const b1 = $("copy-fb-close"), b2 = $("copy-fb-ok");
  if (b1) b1.addEventListener("click", closeFb);
  if (b2) b2.addEventListener("click", closeFb);
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
/** 单条消息的签名（id + 类型 + 内容长度 + 完成时间）—— 内容变则签名变。 */
function msgSig(m) {
  let len = (m.text || "").length;
  if (Array.isArray(m.content)) {
    for (const p of m.content) {
      len += (p && p.text ? p.text.length : 0);
      const o = p && p.state && (p.state.output || p.state.error);
      len += o ? String(o).length : 0;
    }
  }
  return `${m.id}:${m.type}:${len}:${(m.time && m.time.completed) || 0}`;
}

function signatureOf(items) {
  return items.map(msgSig).join("|");
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
let staticIds = [];       // ★P2：上次已渲染进 #msg-static 的消息 id 列表（按顺序，用于增量渲染判断）
let staticSigList = [];   // ★P2：上次已渲染的每条消息签名（内容变了就不能纯追加，要回退全量重建）
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
  OVERLAY.push("trace", traceClose);                 // ★P3：Esc 只关栈顶
}
function traceClose() {
  TRACE.open = false;
  const t = $("trace");
  if (t) { t.classList.remove("open"); t.setAttribute("aria-hidden", "true"); }
  const b = $("btn-trace"); if (b) b.classList.remove("on");
  syncDrawerOverlap();
  OVERLAY.pop("trace");
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
  if (state.messages.length && PERF.marks["msg:first"] == null) PERF.mark("msg:first");   // ★P5：首条消息
  const sSig = signatureOf(state.messages);
  if (sSig !== staticSig) {
    staticSig = sSig;
    // ★每一轮的「进展行」贴在该轮**最后一条消息之后**（一行，不列步骤）
    const cards = turnCards();
    const byAfter = new Map();
    cards.forEach((g, i) => { if (!g.live && g.afterId) byAfter.set(g.afterId, [g, i]); });

    // ★P2 增量渲染：历史 id 前缀完全不变 + 新增从「新一轮 user」开始 + 已有历史渲染内容
    //   → 只 append 新增消息，不重建历史 DOM（长会话收益最大）。
    //   ⚠ 其它情况（首次 / 删除 / 编辑 / 重排 / 同一轮分批追加）一律回退全量重建，
    //   保证「整轮时长 / 进展行」这类依赖整轮信息的显示不会错。
    const ids = state.messages.map((m) => m.id);
    const sigList = state.messages.map(msgSig);
    let prefix = 0;
    const minLen = Math.min(staticIds.length, ids.length);
    while (prefix < minLen
           && staticIds[prefix] === ids[prefix]
           && staticSigList[prefix] === sigList[prefix]) prefix++;
    const firstNew = state.messages[prefix];
    const pureAppend = prefix === staticIds.length
      && prefix < ids.length
      && staticHtml
      && firstNew && firstNew.type === "user";

    if (pureAppend) {
      const newHtml = state.messages.slice(prefix).map((m) => {
        const h = renderMessage(m) || "";
        const c = byAfter.get(m.id);
        return c ? h + turnLineHtml(c[0], c[1]) : h;
      }).join("");
      $("msg-static").insertAdjacentHTML("beforeend", newHtml);
      staticHtml += newHtml;
      staticIds = ids;
      staticSigList = sigList;
      heroShown = false;
    } else {
      staticHtml = state.messages.map((m) => {
        const h = renderMessage(m) || "";
        const c = byAfter.get(m.id);
        return c ? h + turnLineHtml(c[0], c[1]) : h;
      }).join("");
      staticIds = ids;
      staticSigList = sigList;
      heroShown = false;
      swapHtml($("msg-static"), staticHtml);
    }
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
  // ★工作台打开时：底部这一行是「任务输入条」（Enter / 送出 / ▶ 都走工作流，不发聊天）
  if (collabIsOpen()) {
    await collabQuickRun();
    return;
  }
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
  OVERLAY.push("newsess", closeNewSession);
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

function closeNewSession() { const b = $("newsess"); if (b) b.hidden = true; OVERLAY.pop("newsess"); }

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

/* ================= P0 · 全局错误捕获（接诊断包） =================
 * 任何未捕获异常 / 未处理的 Promise 拒绝 → 顶部横幅提示 + 写进 `window.__lastError`。
 * 供「诊断包」（docs/示例-诊断-工坊-方案.md §2）读取前端错误摘要；不白屏、不静默。 */
window.__lastError = null;
const __errSeen = new Set();          // 同样的错误不重复刷横幅（用 key 去重）

/** 记录一次错误：写 __lastError + 横幅提示（同源错误去重，避免刷屏）。 */
function reportError(err, ctx) {
  const msg = (err && err.message) ? err.message : String(err);
  const stack = (err && err.stack) ? err.stack : "";
  const src = (err && err.filename) || "app";
  window.__lastError = { msg, stack, src, ctx: ctx || "", ts: Date.now() };
  try { console.error("[cocraft]", ctx || "", err); } catch { /* ignore */ }

  // 去重 key：同 ctx + 同 msg 只弹一次（滚动/高频轮询别刷爆横幅）
  const key = (ctx || "") + "|" + msg;
  if (__errSeen.has(key)) return;
  __errSeen.add(key);
  if (__errSeen.size > 200) { const it = __errSeen.values().next(); if (it.done === false) __errSeen.delete(it.value); }

  if (typeof setBanner === "function") {
    setBanner("⚠ 出错了：" + msg + "（详见 设置 → 诊断）", { sticky: true });
  }
}

window.addEventListener("error", (ev) => {
  reportError(ev.error || ev.message, "window.error");
});
window.addEventListener("unhandledrejection", (ev) => {
  const r = ev.reason;
  reportError(r instanceof Error ? r : new Error(String(r)), "unhandledrejection");
});

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
  POLL.interval("heartbeat", 4000, beat, { heartbeat: true });

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
