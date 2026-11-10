/* 42-cu.js —— 通用工具（桌面 cu / 浏览器 CDP）：列表 + 说明 + 一键复制
 *
 * 只读：打开列表不会启动 UIA 宿主 / 浏览器，也不会占用桌面租约。
 * 工具目录来自后端 `/cu/status`（唯一一份说明在 backend/computer/catalog.py）。
 * ★ 入口：顶栏「工具」（扳手）→「通用工具（cu）…」→ 这个弹层。
 */

const CU_UI = {
  data: null,     // /cu/status 的返回
  err: "",
  open: false,
};

function cuIsOpen() {
  const b = $("cu");
  return !!(b && !b.hidden);
}

async function cuOpen() {
  const box = $("cu");
  if (!box) return;
  box.hidden = false;
  CU_UI.open = true;
  OVERLAY.push("cu", cuClose);          // ★P3：Esc 只关栈顶
  await cuLoad();
}

function cuClose() {
  const b = $("cu");
  if (b) b.hidden = true;
  CU_UI.open = false;
  OVERLAY.pop("cu");
}

async function cuLoad() {
  const list = $("cu-list");
  if (list && !CU_UI.data) {
    list.innerHTML = '<div class="muted" style="padding:10px">读取中…</div>';
  }
  try {
    const r = unwrap(await api("/cu/status")) || {};
    CU_UI.data = r;
    CU_UI.err = "";
  } catch (e) {
    CU_UI.err = (e && e.message) ? e.message : String(e);
  }
  renderCu();
}

function _cuChip(label, value, state) {
  return '<span class="cu-chip cu-' + (state || "mute") + '">' +
    '<i>' + esc(label) + '</i><b>' + esc(String(value)) + '</b></span>';
}

function _cuChips(d) {
  const g = d.gate || {};
  const l = d.lease || {};
  const u = d.uia || {};
  const b = d.browser || {};
  const allow = String(g.actions || "deny") === "allow";
  const out = [];
  out.push(_cuChip("动作权限", allow ? "允许" : "需信任", allow ? "ok" : "warn"));
  out.push(_cuChip("桌面租约", l.held ? ("占用：" + (l.owner || "?")) : "空闲", l.held ? "warn" : "ok"));
  out.push(_cuChip("UIA 宿主", u.running ? ("运行中 · pid " + u.pid) : "按需启动", u.running ? "ok" : "mute"));
  out.push(_cuChip("浏览器", b.running ? ("运行中 · :" + b.port + (b.headless ? " · 无头" : "")) : "未启动",
    b.running ? "ok" : "mute"));
  return out.join("");
}

/** 「允许桌面操作」开关：反映 gate.actions（allow / deny）。 */
function _cuActionsState(d) {
  const allow = String((((d || {}).gate) || {}).actions || "deny") === "allow";
  const btn = $("cu-actions");
  if (!btn) return;
  btn.setAttribute("aria-pressed", allow ? "true" : "false");
  btn.classList.toggle("on", allow);
  const st = btn.querySelector(".cu-st");
  if (st) st.textContent = allow ? "已允许" : "需信任";
}

/** 点开关 → 写本机 _cu.json 的 actions（**不动**黑名单/危险词）。 */
async function cuToggleActions() {
  const cur = String((((CU_UI.data || {}).gate) || {}).actions || "deny");
  const want = cur === "allow" ? "deny" : "allow";
  try {
    const r = await api("/cu/config", { method: "POST", body: JSON.stringify({ actions: want }) });
    const j = unwrap(r) || {};
    if (!j.ok) throw new Error(j.error || "写入失败");
    CU_UI.data = CU_UI.data || {};
    CU_UI.data.gate = Object.assign({}, CU_UI.data.gate || {}, j.gate || {},
      { actions: j.actions || want });
    renderCu();
    if (want === "allow") {
      cuSay("已允许桌面操作（本机）：单聊里的动作类工具可用 —— 每条动作命令仍会弹一次确认；工作流按成员权限走，不受影响");
    } else {
      cuSay("已收回桌面操作权限：动作类工具会拒绝（工作流按成员权限走，不受影响）");
    }
  } catch (e) {
    cuSay("改动作权限失败：" + backendStaleHint((e && e.message) || String(e)));
  }
}

function _cuItem(t) {
  const perm = t.perm === "read" ? "read" : "action";
  const permLabel = t.permLabel || (perm === "read" ? "只读" : "动作");
  const surf = t.surfaceLabel
    ? '<span class="cu-surface s-' + esc(t.surface || "any") + '">' + esc(t.surfaceLabel) + '</span>'
    : "";
  const warn = t.warn ? '<div class="cu-warn">⚠ ' + esc(t.warn) + '</div>' : "";
  return '' +
    '<div class="cu-item" data-perm="' + perm + '" data-cap="' + esc(t.cap || "") +
      '" data-id="' + esc(t.id) + '">' +
      '<div class="cu-top">' +
        '<b>' + esc(t.title || t.id) + '</b>' +
        surf +
        '<span class="cu-perm ' + perm + '">' + esc(permLabel) + '</span>' +
        '<code class="cu-cmd" title="点一下复制">' + esc(t.cmd || "") + '</code>' +
      '</div>' +
      '<div class="cu-desc">' + esc(t.desc || "") + '</div>' +
      warn +
      '<div class="cu-ex" role="button" tabindex="0" title="点一下复制这条命令">' +
        '<code>' + esc(t.example || t.cmd || "") + '</code>' +
        '<span class="cu-cp">复制</span>' +
      '</div>' +
    '</div>';
}

function renderCu() {
  const list = $("cu-list");
  const chips = $("cu-chips");
  const foot = $("cu-foot");
  if (!list) return;
  if (CU_UI.err) {
    if (chips) chips.innerHTML = "";
    list.innerHTML = '<div class="error-box">读取 /cu/status 失败：' + esc(CU_UI.err) + '</div>';
    if (foot) foot.textContent = "—";
    return;
  }
  const d = CU_UI.data || {};
  const cat = d.catalog || {};
  const tools = cat.tools || [];
  const caps = cat.caps || [];
  if (chips) chips.innerHTML = _cuChips(d);
  _cuActionsState(d);

  let html = "";
  caps.forEach((c) => {
    const items = tools.filter((t) => t.cap === c.id);
    if (!items.length) return;
    html += '<div class="cu-group" data-cap="' + esc(c.id) + '"><b>' + esc(c.label) + '</b>' +
      '<span class="cu-count">' + items.length + '</span>' +
      '<span class="cu-hint">' + esc(c.hint || "") + '</span></div>';
    items.forEach((t) => { html += _cuItem(t); });
  });
  if (!html) html = '<div class="muted" style="padding:10px">没有工具（catalog 为空）</div>';
  list.innerHTML = html;

  const c2 = cat.counts || {};
  if (foot) {
    foot.textContent = "共 " + (c2.total || tools.length) + " 个工具 · 只读 " +
      (c2.read || 0) + " / 动作 " + (c2.action || 0) +
      "；按能力分组，命令都在 PATH 里，点示例即可复制";
  }
  syncCuMenuState();
}

/** 扳手菜单里那一项：面板打开时标一下。 */
function syncCuMenuState() {
  const b = $("tm-cu");
  if (b) b.classList.toggle("on", cuIsOpen());
}

function cuSay(msg) {
  try { setBanner(msg); } catch (e) { /* ignore */ }
}

async function cuCopy(text, label) {
  const t = String(text || "").trim();
  if (!t) return;
  const ok = await copyToClipboard(t);
  if (ok) cuSay("已复制：" + (label ? label + " · " : "") + t);
  else cuSay("复制失败（可手动选中命令再复制）：" + t);
}

function initCuPanel() {
  const close = $("cu-close");
  if (close) close.addEventListener("click", cuClose);
  const reload = $("cu-reload");
  if (reload) reload.addEventListener("click", () => { CU_UI.data = null; cuLoad(); });
  const acts = $("cu-actions");
  if (acts) acts.addEventListener("click", cuToggleActions);

  const list = $("cu-list");
  if (list) {
    list.addEventListener("click", (e) => {
      const row = e.target.closest(".cu-ex");
      if (!row) return;
      const code = row.querySelector("code");
      cuCopy(code ? code.textContent : "");
    });
    list.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const row = e.target.closest(".cu-ex");
      if (!row) return;
      e.preventDefault();
      const code = row.querySelector("code");
      cuCopy(code ? code.textContent : "");
    });
  }

  const all = $("cu-copy-all");
  if (all) all.addEventListener("click", () => {
    const tools = ((CU_UI.data || {}).catalog || {}).tools || [];
    if (!tools.length) return cuSay("还没有读到工具列表");
    const text = tools.map((t) => "# " + (t.title || t.id) + " —— " + (t.desc || "") + "\n" + (t.example || t.cmd || "")).join("\n\n");
    cuCopy(text, "全部示例");
  });
}
