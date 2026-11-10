/* 00-util.js —— 基础工具 + 统一轮询管理器 POLL（P1 从 app.js 拆出，纯搬运不改逻辑）
 * 加载顺序：**最先**（其它文件都依赖这里的 $ / api / esc / renderText / fmtTime / sleep / POLL）。
 * UI_VER 用 document.currentScript 读本文件 ?v=（每个拆分文件都带同一个版本号）。
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
    // ★告诉后端"这次是**面板里点的**"（本机接口无鉴权，审计要靠这个区分人 / 脚本）
    headers: { "X-Cocraft-UI": "1", "Content-Type": "application/json", ...(options.headers || {}) },
  });
  const text = await res.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = text; }
  if (!res.ok) {
    const detail = body && typeof body === "object"
      ? (body.error || body.message || JSON.stringify(body))
      : String(body || res.statusText);
    // ★ 上游 500 经常**没有 body**（OpenCode 的校验失败就是空响应），只显示
    //   「500 Internal Server Error」等于没说。这里补一句最可能的原因（附件的坑见 AGENTS 踩坑 #53）。
    const blank5xx = !body && res.status >= 500;
    throw new Error(`${res.status} ${detail}${blank5xx ? "（服务端内部错误；若刚发过附件，多半是附件太大或格式不支持）" : ""}`);
  }
  return body;
}

const unwrap = (b) => (b && typeof b === "object" && "data" in b ? b.data : b);

/** 面板 / 后端版本不一致时（新前端 + 旧后端进程）给一句人话提示。
 *
 *  典型：前端已经带「导出简报」按钮，但 8787 还是旧进程 → 后端只认 json/csv/md → 400。
 *  不加这句，用户只会看到 `400 format 只支持 json / csv / md`，以为是功能坏了。 */
function backendStaleHint(msg) {
  const s = String(msg || "");
  if (/只支持|not found|404|405|请求失败/i.test(s)) {
    return s + "（面板和后端版本可能不一致：重启一次面板服务 8787 再试）";
  }
  return s;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- ★报错分类（前端镜像，`ui 0.2.87`） ----------------
 * 后端 `backend/errkind.py` 认得出「余额不足 / 限流 / 网络…」，但它只对 ACP 与工作流生效；
 * 老后端 / opencode 引擎把错误原样丢过来时，前端也得会说人话（至少别只显示一串英文）。
 * ⚠ 两张表要**保持同口径**（`tools/errkind_test.py` 会两边都跑）。 */
const ERR_KINDS = [
  ["balance", /insufficient\s+balance|余额不足|credit\s+balance\s+is\s+too\s+low|payment\s+required|status\s*402|recharge|欠费/i,
   "余额不足", "去厂商后台充值，或换一个有额度的 Key（设置 → 模型凭据）；换模型/重试都没用"],
  ["quota", /quota|额度|exceeded\s+your\s+current|billing\s+hard\s+limit|usage\s+limit|免费额度/i,
   "额度用尽", "额度/并发用完了：等重置、换 Key，或换厂商（设置 → 模型凭据）"],
  ["nokey", /missing\s+environment\s+variable|no\s+api\s+key|api\s*key\s+(is\s+)?(not\s+set|missing)|未配置\s*key|环境变量.*(缺失|没有)/i,
   "没配 Key", "这个 agent 拿不到 Key：去「设置 → 模型凭据」补一条，或设对应环境变量"],
  ["auth", /invalid\s*api[_\s-]?key|incorrect\s+api\s+key|unauthorized|authentication\s+required|status\s*401|\b401\b.*(unauthor|auth)|凭据无效|key\s+无效|token\s+(is\s+)?(invalid|expired)/i,
   "凭据无效", "Key 不认（401）：检查是否过期/写错，或该厂商要换 base_url"],
  ["model", /model\s*(unavailable|not\s+found|does\s+not\s+exist|not\s+supported|is\s+deleted)|modelunavailableerror|unknown\s+model|no\s+such\s+model|模型(不可用|不存在|已下架|不支持)/i,
   "模型不可用", "这个会话绑的模型被下架/无权访问：点顶栏「模型」换一个"],
  ["rate", /rate\s*limit|too\s+many\s+requests|status\s*429|\b429\b|请求过于频繁|限流/i,
   "被限流", "请求太密（429）：等几秒再试"],
  ["context", /context\s*(length|window)|maximum\s+context|too\s+many\s+tokens|token\s+limit|prompt\s+is\s+too\s+long|超出(上下文|长度)|上下文(超|过)/i,
   "上下文超限", "这一轮太长：开新会话或让它先总结"],
  ["cancelled", /\b(cancelled|canceled|interrupted|aborted)\b|已取消|已中断/i,
   "已取消", "这一轮被中断/取消，不是故障"],
  ["timeout", /\btimed?\s*out\b|timeout|超时/i,
   "超时", "等太久了：缩小范围或拆成几步"],
  ["network", /connection\s*(reset|refused|closed)|connectionrefused|connectionreset|econnreset|econnrefused|unable\s+to\s+connect|fetch\s+failed|socket\s+hang\s+up|getaddrinfo|network\s+error|bad\s+gateway|service\s+unavailable|status\s*50[234]|endpoint\s+is\s+unavailable|upstream\s+request\s+failed|unavailable/i,
   "连不上上游", "网络/网关问题（不是 Key）：检查代理或公司网络，稍后重试"],
  ["agent", /acperror|acp\s*(错误|error)|-3260[0-9]|internal\s+error|process\s+exited|agent\s+crashed|traceback/i,
   "agent 内部出错", "agent 自己崩了：看 runtime/logs/acp.log 尾部；反复出现就换一个大脑"],
];

/** 把一段报错文本归类成 `{kind, label, hint, account}`（认不出 → unknown）。 */
function errKindOf(text) {
  const s = String(text || "");
  for (const [kind, re, label, hint] of ERR_KINDS) {
    if (re.test(s)) {
      return { kind, label, hint, account: kind === "balance" || kind === "quota"
        || kind === "auth" || kind === "nokey" };
    }
  }
  return { kind: "unknown", label: "未知错误", account: false,
           hint: "拿不准：看 runtime/logs/acp.log 尾部，或用「审计与活动」导出诊断" };
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
  // ★LaTeX（`ui 0.2.87`）：`\(...\)` / `$...$` 以前**原样显示**（用户发图：`\(a_1, a_2, \dots, a_n\)`）
  //   我们不引 KaTeX（要字体 + 体积），而是**翻成人能读的文本**：`\le`→≤、`\dots`→…、`\frac{a}{b}`→(a)/(b)。
  //   先替换再处理行内代码，避免公式里的 `_` `*` 被当成斜体。
  const maths = [];
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, (m, tex) => {
    maths.push(`<span class="math">${mathToText(tex)}</span>`); return "\u0001" + (maths.length - 1) + "\u0001";
  });
  s = s.replace(/(^|[^$\\])\$([^$\n]{1,400}?)\$/g, (m, pre, tex) => {
    maths.push(`<span class="math">${mathToText(tex)}</span>`); return pre + "\u0001" + (maths.length - 1) + "\u0001";
  });
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, t, u) =>
    /^(https?:|mailto:|\/|#)/i.test(u) ? `<a href="${u}" target="_blank" rel="noreferrer">${t}</a>` : t);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
  return s.replace(/\u0001(\d+)\u0001/g, (m, i) => maths[+i]);
}

/* ---------------- ★LaTeX → 可读文本（`ui 0.2.87`） ----------------
 * 渲染器不解析 LaTeX（引 KaTeX 要 ~1.2 MB + 一堆字体，且离线安装包里很重）。
 * 折中：把常见记号翻成 Unicode / ASCII，公式「读得懂」而不是一堆反斜杠。
 * ⚠ 只做**确定性的替换**，不猜语义；复杂公式（矩阵、分段函数）会保留近似写法。 */
const TEX_SYMS = {
  dots: "…", ldots: "…", cdots: "⋯", vdots: "⋮", ddots: "⋱",
  cdot: "·", times: "×", div: "÷", pm: "±", mp: "∓", ast: "∗", star: "⋆",
  le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠", approx: "≈",
  equiv: "≡", sim: "∼", propto: "∝", ll: "≪", gg: "≫",
  in: "∈", notin: "∉", ni: "∋", subset: "⊂", subseteq: "⊆", supset: "⊃",
  cup: "∪", cap: "∩", emptyset: "∅", forall: "∀", exists: "∃", nexists: "∄",
  to: "→", rightarrow: "→", leftarrow: "←", leftrightarrow: "↔",
  Rightarrow: "⇒", Leftarrow: "⇐", Leftrightarrow: "⇔", mapsto: "↦",
  infty: "∞", partial: "∂", nabla: "∇", angle: "∠", perp: "⊥", parallel: "∥",
  sum: "Σ", prod: "Π", int: "∫", oint: "∮", sqrt: "√",
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
  zeta: "ζ", eta: "η", theta: "θ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ",
  nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", upsilon: "υ",
  phi: "φ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
  Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉", langle: "⟨", rangle: "⟩",
  quad: " ", qquad: "  ", "%": "%", "{": "{", "}": "}", "&": " ", "\\": " ",
};

/** 取一个 `{...}` 分组（支持嵌套）；没有花括号就取下一个 token。 */
function texGroup(s, i) {
  if (s[i] !== "{") {
    const m = /^\\?[A-Za-z0-9]+|^./.exec(s.slice(i) || "");
    return { text: m ? m[0] : "", end: i + (m ? m[0].length : 0) };
  }
  let depth = 0;
  for (let j = i; j < s.length; j++) {
    if (s[j] === "{") depth++;
    else if (s[j] === "}") { depth--; if (!depth) return { text: s.slice(i + 1, j), end: j + 1 }; }
  }
  return { text: s.slice(i + 1), end: s.length };
}

/**
 * LaTeX 片段 → 可读文本。
 * `\frac{a}{b}` → `(a)/(b)`；`\sqrt{x}` → `√(x)`；`a_{n}` → `a_n`；`x^{2}` → `x^2`；
 * `\le` → `≤`…；未知命令去掉反斜杠（`\foo` → `foo`，别让反斜杠糊一屏）。
 * 返回的是**已经 HTML 转义过的**安全片段（调用方直接拼进 innerHTML）。
 */
function mathToText(src) {
  let s = String(src || "");
  s = s.replace(/\\(?:text|mathrm|mathbf|mathit|mathbb|operatorname|left|right|displaystyle)\s*/g, "");
  const skip = (i) => { while (i < s.length && /\s/.test(s[i])) i++; return i; };
  // \frac{a}{b} → (a)/(b)（循环到没有为止，天然支持嵌套）
  for (let guard = 0; guard < 80; guard++) {
    const i = s.indexOf("\\frac");
    if (i < 0) break;
    const a = texGroup(s, skip(i + 5));
    const b = texGroup(s, skip(a.end));
    s = s.slice(0, i) + `(${a.text})/(${b.text})` + s.slice(b.end);
  }
  // \sqrt{x} → √(x)
  for (let guard = 0; guard < 80; guard++) {
    const i = s.indexOf("\\sqrt");
    if (i < 0) break;
    const a = texGroup(s, skip(i + 5));
    s = s.slice(0, i) + `√(${a.text})` + s.slice(a.end);
  }
  s = s.replace(/\\[A-Za-z]+/g, (m) => {
    const name = m.slice(1);
    return Object.prototype.hasOwnProperty.call(TEX_SYMS, name) ? TEX_SYMS[name] : name;
  });
  // `\{ \}` 是**真的花括号**（集合写法），先占位，免得被下一步当分组删掉
  s = s.replace(/\\([{}])/g, (m, ch) => (ch === "{" ? "\u0002" : "\u0003"));
  s = s.replace(/\\([%$&#_])/g, "$1");               // \% \$ \& \# \_
  s = s.replace(/[{}]/g, "");                        // 剩下的分组括号（a_{n} → a_n）
  s = s.replace(/\u0002/g, "{").replace(/\u0003/g, "}");
  s = s.replace(/[ \t]+/g, " ");
  return s.trim();
}

/** 块级公式：`\[ ... \]` / `$$ ... $$` → 单独一块（居中、等宽） */
function mdMathBlock(tex) {
  return `<div class="md-math">${mathToText(tex)}</div>`;
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
    // ★块级公式（`ui 0.2.87`）：`\[ ... \]`（可跨行）与 `$$ ... $$` 单独成块
    const mOpen = line.match(/^\s*(\\\[|\$\$)\s*$/);
    if (mOpen) {
      const close = mOpen[1] === "\\[" ? "\\]" : "$$";
      flushP();
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].includes(close)) { buf.push(lines[i]); i++; }
      if (i < lines.length) {
        const tail = lines[i].split(close)[0];
        if (tail.trim()) buf.push(tail);
        i++;
      }
      out += mdMathBlock(buf.join(" "));
      continue;
    }
    const mSame = line.match(/^\s*(\\\[|\$\$)([^\n]*?)(\\\]|\$\$)\s*$/);
    if (mSame) { flushP(); out += mdMathBlock(mSame[2]); i++; continue; }
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

/* ================= P0 · 统一轮询管理器 + 可见性暂停 =================
 * 页面隐藏（切后台 / 最小化 / 锁屏）时，除 `heartbeat` 外的所有轮询回调一律跳过，
 * 省下 CPU 与网络；重新可见时**立即补跑一次**，保证状态不过期（见方案 §3 阶段 0）。
 * ⚠ 心跳（beat，守护进程靠它判断面板是否还开着）与 WS 保活 ping 必须 `heartbeat:true`。
 * 回调抛错会交给全局错误捕获（reportError），不会让定时器静默死掉。 */
const POLL = {
  _timers: new Map(),
  hidden: false,

  /** 注册一个轮询（替代裸 `setInterval`）。同名会先清掉旧的。 */
  interval(name, ms, fn, opts) {
    opts = opts || {};
    this.clear(name);
    const rec = { name, ms, fn, heartbeat: !!opts.heartbeat, id: null };
    const tick = () => {
      if (this.hidden && !rec.heartbeat) return;
      try { fn(); } catch (e) {
        if (typeof reportError === "function") reportError(e, "poll:" + name);
        else console.error("poll:" + name, e);
      }
    };
    rec.id = setInterval(tick, ms);
    this._timers.set(name, rec);
    return rec;
  },

  clear(name) {
    const old = this._timers.get(name);
    if (old) { if (old.id) clearInterval(old.id); this._timers.delete(name); }
  },

  clearAll() {
    for (const name of [...this._timers.keys()]) this.clear(name);
  },

  /** 可见性变化：可见时把非 heartbeat 的轮询立即补一次。 */
  _sync() {
    this.hidden = document.hidden;
    if (this.hidden) return;
    for (const rec of this._timers.values()) {
      if (rec.heartbeat) continue;
      try { rec.fn(); } catch (e) {
        if (typeof reportError === "function") reportError(e, "poll:resume:" + rec.name);
        else console.error("poll:resume:" + rec.name, e);
      }
    }
  },
};

document.addEventListener("visibilitychange", () => POLL._sync());

/* ================= P3 · 弹层栈（OVERLAY）=================
 * 统一管理弹层的打开顺序：Esc 只关**最上面**那一层；关闭后焦点回到触发元素。
 * 用**捕获阶段**监听 Esc（抢在其它 Esc 监听之前），只关栈顶并 stopPropagation —— 避免一次关多层。
 * 各弹层只需在自己的 open/close 里 push/pop（行为不变）；未迁移的弹层走原有 Esc 监听。
 * ⚠ 多级 Esc（工作台「权限抽屉 → 检查器 → 退工作台」等）仍保留各自的逻辑：
 *   OVERLAY 只在栈里有已注册弹层时接管 Esc。
 */
const OVERLAY = {
  stack: [],   // [{ id, close, opener, yielded }]

  /** 弹层打开时注册。同一 id 重复 open 会移到栈顶（不重复）。 */
  push(id, close, opener) {
    const i = this.stack.findIndex((x) => x.id === id);
    if (i >= 0) this.stack.splice(i, 1);
    let op = opener;
    if (op == null) { try { op = document.activeElement; } catch { op = null; } }
    // ★居中对话框打开时**先把设置抽屉收起来**（关掉再还原）——
    //   否则两层叠在一起、字压字（「＋ 添加凭据」「权限与批准」都踩过）。
    //   规则与例外见 `cfgYieldToOverlay()`（11-engine.js）。
    let yielded = false;
    try {
      if (typeof cfgYieldToOverlay === "function") yielded = cfgYieldToOverlay(String(id));
    } catch { /* 设置模块还没加载完 → 不管 */ }
    this.stack.push({ id: String(id), close, opener: op, yielded });
  },

  /** 弹层关闭时注销。返回被移除的记录（没有则 null）。 */
  pop(id) {
    const i = this.stack.findIndex((x) => x.id === String(id));
    if (i < 0) return null;
    const rec = this.stack.splice(i, 1)[0];
    if (rec && rec.yielded) {
      try { cfgRestoreFromOverlay(); } catch { /* ignore */ }
    }
    return rec;
  },

  top() { return this.stack.length ? this.stack[this.stack.length - 1] : null; },

  isOpen(id) { return this.stack.some((x) => x.id === String(id)); },

  /** 关掉最上面那一层（并归还焦点）。返回是否真的关了。 */
  closeTop() {
    const t = this.top();
    if (!t) return false;
    try { t.close(); } catch (e) {
      if (typeof reportError === "function") reportError(e, "overlay:" + t.id);
      else console.error("overlay:" + t.id, e);
    }
    // close 里通常会自己 pop；这里再兜底清一次（幂等）
    this.pop(t.id);
    if (t.opener && typeof t.opener.focus === "function") {
      try { t.opener.focus(); } catch { /* ignore */ }
    }
    return true;
  },
};

// ★统一 Esc：捕获阶段先处理 —— 栈里有弹层就关栈顶，并拦住其它 Esc 监听（避免一次关多层）。
window.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (OVERLAY.closeTop()) { e.stopPropagation(); e.preventDefault(); }
}, true);

/* ================= P5 · 性能埋点（只在 localStorage.debug=1 时输出）=================
 * 关键路径打点：boot 开始/结束、首条消息渲染、工作流首次渲染…（见 app.js 的 boot）。
 * 数据同时留在 `window.__perf` 里，供「诊断包」读取（docs/示例-诊断-工坊-方案.md §2）。
 * ⚠ 只在 `localStorage.debug = "1"` 时往 console 输出；平时零噪音、零开销（只记 performance.mark）。 */
const PERF = {
  marks: {},
  enabled: false,
  init() { try { this.enabled = localStorage.getItem("debug") === "1"; } catch { /* 隐私模式 */ } },
  /** 打一个时间点。 */
  mark(name) {
    try { this.marks[name] = performance.now(); performance.mark(name); } catch { /* ignore */ }
    if (this.enabled) { try { console.log("[perf] mark", name, Math.round(performance.now())); } catch { /* ignore */ } }
  },
  /** 从 startMark 到现在过了多少 ms（没打过点返回 null）。 */
  since(startMark) {
    const t = this.marks[startMark];
    return t == null ? null : performance.now() - t;
  },
  /** 量一段耗时；返回 ms（没打过起点返回 null）。 */
  measure(name, startMark) {
    const d = this.since(startMark);
    if (d == null) return null;
    try { performance.measure(name, startMark); } catch { /* ignore */ }
    if (this.enabled) { try { console.log("[perf] " + name + " " + Math.round(d) + "ms"); } catch { /* ignore */ } }
    return d;
  },
};
PERF.init();
window.__perf = PERF;   // 供诊断包读取

/** 前端诊断快照（错误 + 性能标记 + 环境）—— 「诊断包」可直接读 window.__diagnostics()。 */
window.__diagnostics = () => ({
  lastError: window.__lastError || null,
  perf: Object.assign({}, PERF.marks),
  ui: (typeof UI_VER === "string" ? UI_VER : ""),
  at: Date.now(),
  ua: (navigator && navigator.userAgent) || "",
});
