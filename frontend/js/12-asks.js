/* 12-asks.js —— 权限 / 表单 / 信任面板（P1 从 app.js 拆出，纯搬运不改逻辑） */

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
  if (open) { trustRefresh(); OVERLAY.push("trust", trustClose); }
  else OVERLAY.pop("trust");
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
              // ⚠ 必须 JSON.stringify：api() 不会替你序列化，传对象会变成 [object Object]
              //    → 后端读不到 decision → 按 reject → agent 报 "aborted by user"（0.1s 取消）
              await api(`/api/session/${sid}/permission/${encodeURIComponent(pm.id)}/reply`,
                        { method: "POST", body: JSON.stringify({ decision: "once" }) });
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
