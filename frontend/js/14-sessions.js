/* 14-sessions.js —— 工作流成员 / 群聊 AI 成员的会话管理
 *
 * 目标：三类会话分开管理 ——
 *   · 单聊：左侧会话列表
 *   · 工作流成员：工作流画布 →「成员会话」
 *   · 群聊 AI 成员：群聊抽屉 →「AI 会话」
 *
 * 这里**只列会话元信息**（名字 / 成员 / 运行 / 条数 / id），不启动 agent。
 */

const SESSMGR = { scope: "", key: "", items: [], busy: false };

async function sessMgrOpen(scope, key) {
  const box = $("sessmgr");
  if (!box) return;
  SESSMGR.scope = String(scope || "");
  SESSMGR.key = String(key || "");
  box.hidden = false;
  OVERLAY.push("sessmgr", sessMgrClose);
  await sessMgrLoad();
}

function sessMgrClose() {
  const box = $("sessmgr");
  if (box) box.hidden = true;
  OVERLAY.pop("sessmgr");
}

async function sessMgrLoad() {
  const list = $("sessmgr-list"), note = $("sessmgr-note"), foot = $("sessmgr-foot");
  const title = $("sessmgr-title");
  const isWf = SESSMGR.scope === "workflow";
  if (title) title.textContent = isWf ? "工作流成员会话" : "群聊成员会话";
  if (note) {
    note.textContent = isWf
      ? "这里只管理工作流成员的会话（按项目 + 结点命名），单聊列表不会显示它们。"
      : "这里只管理群聊 AI 成员的会话（按群里创建时的名字命名），单聊列表不会显示它们。";
  }
  if (list) list.innerHTML = `<div class="muted" style="padding:10px">读取中…</div>`;
  try {
    const q = isWf
      ? `workflow=${encodeURIComponent(SESSMGR.key)}`
      : `chat=${encodeURIComponent(SESSMGR.key)}`;
    const path = isWf ? `/sessions/workflow?${q}` : `/sessions/group?${q}`;
    const r = unwrap(await api(path)) || {};
    SESSMGR.items = r.items || [];
    renderSessMgr();
    if (foot) {
      foot.textContent = `${SESSMGR.items.length} 个成员会话` + (r.workflowName ? ` · ${r.workflowName}` : "");
    }
  } catch (e) {
    if (list) list.innerHTML = `<div class="muted" style="padding:10px">读取失败：${esc((e && e.message) || e)}</div>`;
    if (foot) foot.textContent = "";
  }
}

function renderSessMgr() {
  const box = $("sessmgr-list");
  if (!box) return;
  if (!SESSMGR.items.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">${SESSMGR.scope === "workflow"
      ? "这个工作流还没有成员会话。跑一次后，每个成员的会话会按「项目 · 结点名」出现在这里。"
      : "这个群还没有 AI 成员会话。在群聊里建 AI 成员并 @ 它一次后，这里会出现。"}</div>`;
    return;
  }
  box.innerHTML = SESSMGR.items.map((x) => {
    const title = String(x.title || x.memberName || x.name || "会话");
    const who = SESSMGR.scope === "workflow"
      ? `${esc(x.memberName || "")} · ${esc(x.runTitle || "")}`
      : `${esc(x.name || "")}${x.engine ? " · " + esc(x.engine) : ""}`;
    const meta = SESSMGR.scope === "workflow"
      ? `${esc(x.status || "")} · ${Number(x.messageCount) || 0} 条` + (x.resumed ? " · 续用" : "")
      : `${Number(x.messageCount) || 0} 条`;
    const sid = String(x.sessionID || "");
    return `<div class="sm-row">
      <div class="sm-main"><b>${esc(title)}</b><span>${who} · ${meta}</span></div>
      <button type="button" data-sm-copy="${esc(sid)}" ${sid ? "" : "disabled"}>复制 ID</button>
    </div>`;
  }).join("");
}

function initSessMgr() {
  const close = $("sessmgr-close");
  if (close) close.addEventListener("click", sessMgrClose);
  const box = $("sessmgr");
  if (box) box.addEventListener("click", (e) => { if (e.target === box) sessMgrClose(); });
  const reload = $("sessmgr-reload");
  if (reload) reload.addEventListener("click", sessMgrLoad);
  const list = $("sessmgr-list");
  if (list) list.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-sm-copy]");
    if (!b || !b.dataset.smCopy) return;
    const ok = await copyToClipboard(b.dataset.smCopy);
    setBanner(ok ? "已复制会话 ID" : "复制失败", { ms: 4000 });
  });
}
