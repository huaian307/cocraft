/* 13-restore.js —— 忏悔室：群聊 / 工作流 / 单聊 → 项目 → 文件版本时间线
 *
 * 数据来自 `/restore/overview` 和 `/restore/timeline`：
 *   - 忏悔室：破坏性操作放行前的副本（删除前）
 *   - 轮快照：每一轮开始时的文件版本（只有开了「轮快照带内容」才能回滚内容）
 *
 * 目标：**指定文件 → 指定版本**回滚；回滚前当前文件会先备份进忏悔室。 */

const RESTORE = { data: null, cat: "", proj: "", files: [], busy: false };

function restoreWhen(ts) {
  const t = Number(ts || 0) * 1000;
  if (!t) return "—";
  const d = new Date(t);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

async function restoreOpen() {
  const box = $("restore");
  if (!box) return;
  box.hidden = false;
  OVERLAY.push("restore", restoreClose);
  await restoreLoadOverview();
}

function restoreClose() {
  const box = $("restore");
  if (box) box.hidden = true;
  OVERLAY.pop("restore");
}

async function restoreLoadOverview() {
  const cats = $("restore-cats"), files = $("restore-files"), head = $("restore-head");
  if (cats) cats.innerHTML = `<div class="muted" style="padding:4px">读取中…</div>`;
  try {
    const r = unwrap(await api("/restore/overview")) || {};
    RESTORE.data = r;
    const list = r.categories || [];
    let c = list.find((x) => (x.projects || []).length) || list[0];
    RESTORE.cat = c ? c.id : "chat";
    RESTORE.proj = (c && c.projects && c.projects[0]) ? c.projects[0].key : "";
    renderRestoreCats();
    renderRestoreProjects();
    if (RESTORE.proj) await restoreLoadTimeline();
    else renderRestoreFiles();
  } catch (e) {
    if (cats) cats.innerHTML = `<div class="muted" style="padding:4px">读取失败：${esc((e && e.message) || e)}</div>`;
    if (head) head.textContent = "读取失败";
    if (files) files.innerHTML = "";
  }
}

function renderRestoreCats() {
  const box = $("restore-cats");
  if (!box) return;
  const list = (RESTORE.data && RESTORE.data.categories) || [];
  box.innerHTML = list.map((c) => `<button type="button" class="restore-cat${c.id === RESTORE.cat ? " on" : ""}" data-rcat="${esc(c.id)}">`
    + `<b>${esc(c.label)}</b> <span class="muted">${c.files || 0} 文件 / ${c.versions || 0} 版本</span></button>`).join("");
}

function renderRestoreProjects() {
  const box = $("restore-projects");
  if (!box) return;
  const c = ((RESTORE.data && RESTORE.data.categories) || []).find((x) => x.id === RESTORE.cat);
  const ps = (c && c.projects) || [];
  if (!ps.length) {
    box.innerHTML = `<div class="muted" style="padding:4px">这个分类还没有记录</div>`;
    return;
  }
  box.innerHTML = ps.map((p) => `<button type="button" class="restore-proj${p.key === RESTORE.proj ? " on" : ""}" data-rproj="${esc(p.key)}">`
    + `${esc(p.name)} <span class="muted">${p.files || 0} 文件 / ${p.versions || 0} 版本</span>`
    + `<span class="rp-root">${esc(p.root || "")}</span></button>`).join("");
}

async function restoreLoadTimeline() {
  const files = $("restore-files"), head = $("restore-head"), foot = $("restore-foot");
  if (files) files.innerHTML = `<div class="muted" style="padding:8px">读取中…</div>`;
  if (head) head.textContent = "读取中…";
  try {
    const q = `category=${encodeURIComponent(RESTORE.cat)}&project=${encodeURIComponent(RESTORE.proj)}`;
    const r = unwrap(await api(`/restore/timeline?${q}`)) || {};
    RESTORE.files = r.files || [];
    renderRestoreFiles();
    if (head) {
      const c = ((RESTORE.data && RESTORE.data.categories) || []).find((x) => x.id === RESTORE.cat);
      const p = ((c && c.projects) || []).find((x) => x.key === RESTORE.proj);
      head.textContent = `${r.label || ""} · ${p ? p.name : ""} · ${r.total || 0} 个文件`;
    }
    if (foot) {
      const n = RESTORE.files.reduce((a, f) => a + (f.count || 0), 0);
      foot.textContent = `共 ${RESTORE.files.length} 个文件 / ${n} 个版本（回滚前会先备份当前文件）`;
    }
  } catch (e) {
    if (files) files.innerHTML = `<div class="muted" style="padding:8px">读取失败：${esc((e && e.message) || e)}</div>`;
  }
}

function renderRestoreFiles() {
  const box = $("restore-files");
  if (!box) return;
  const rows = RESTORE.files || [];
  if (!rows.length) {
    box.innerHTML = `<div class="muted" style="padding:10px">这个项目还没有可恢复的版本。<br>`
      + `删除 / 修改文件在审批放行前会自动留旧版；如果 agent 完全绕过审批，可打开设置里的「每轮工作区快照」并勾选「轮快照带内容」兜底。</div>`;
    return;
  }
  box.innerHTML = rows.map((f) => {
    const vs = (f.versions || []).map((v) => {
      const q = v.source === "quarantine";
      const isMod = q && v.kind === "modify";
      const when = restoreWhen(v.ts);
      const desc = esc((v.detail || "") + (v.note ? " · " + v.note : ""));
      const size = v.size ? fmtBytes(v.size) : "";
      const can = !!v.restorable;
      const attrs = q
        ? `data-rsource="quarantine" data-rid="${esc(v.id || "")}" data-rpath="${esc(f.path || "")}"`
        : `data-rsource="snapshot" data-rscope="${esc(v.scope || "")}" data-rts="${esc(v.ts_tag || "")}" data-rrel="${esc(f.rel || "")}"`;
      const btn = can
        ? `<button type="button" data-rfile="1" ${attrs}>回滚到此版本</button>`
        : `<button type="button" disabled title="这条只记了文件名 / 内容已被回收">仅记录</button>`;
      return `<div class="restore-v">`
        + `<time>${esc(when)}</time>`
        + `<span class="restore-badge ${isMod ? "m" : (q ? "q" : "s")}">${isMod ? "修改前" : (q ? "删除前" : "快照")}</span>`
        + `<span class="desc" title="${desc}">${desc}</span>`
        + `<span class="r-size">${esc(size)}</span>`
        + btn
        + `</div>`;
    }).join("");
    return `<div class="restore-file">`
      + `<div class="restore-file-hd"><b>${esc(f.rel || f.path)}</b>`
      + `<span class="rf-count">${f.count || 0} 个版本</span></div>`
      + `<div class="restore-vs">${vs}</div>`
      + `</div>`;
  }).join("");
}

async function restoreRun(btn) {
  if (!btn || RESTORE.busy) return;
  const src = btn.dataset.rsource || "";
  const rel = src === "snapshot" ? btn.dataset.rrel : btn.dataset.rpath;
  if (!confirm(`把「${rel}」回滚到这个版本？\n\n当前文件会先备份到忏悔室（可再恢复）。`)) return;
  RESTORE.busy = true;
  btn.disabled = true;
  try {
    const body = src === "quarantine"
      ? { source: "quarantine", id: btn.dataset.rid, path: btn.dataset.rpath }
      : { source: "snapshot", scope: btn.dataset.rscope, ts: btn.dataset.rts, rel: btn.dataset.rrel };
    const r = unwrap(await api("/restore/file", { method: "POST", body: JSON.stringify(body) })) || {};
    setBanner(`已回滚：${r.path || rel}${r.backup ? "（旧版已进忏悔室）" : ""}`, { ms: 9000 });
    await restoreLoadTimeline();
  } catch (e) {
    setBanner("回滚失败：" + ((e && e.message) || e), { sticky: true });
  } finally {
    RESTORE.busy = false;
    btn.disabled = false;
  }
}

async function restoreGitClean() {
  if (!confirm("按 git 提交清理这个忏悔室？\\n\\n只会清「提交前、且旧内容已经能在 git 里找到」的旧版；"
    + "未提交 / 未跟踪的旧版会保留。")) return;
  try {
    const r = unwrap(await api("/confessional/git-clean", { method: "POST", body: "{}" })) || {};
    const items = r.items || [];
    const dropped = items.reduce((a, x) => a + ((x.dropped || []).length), 0) || r.dropped || 0;
    const snapshots = items.reduce((a, x) => a + ((x.snapshots_dropped || []).length), 0) || r.snapshots_dropped || 0;
    const held = items.reduce((a, x) => a + ((x.held || []).length), 0) || r.held || 0;
    setBanner(`按 git 提交清理：旧版 ${dropped} 条 / 旧快照 ${snapshots} 条；保留 ${held} 条（git 里找不到旧内容）`,
      { ms: 14000 });
    await restoreLoadOverview();
  } catch (e) {
    setBanner("按 git 清理失败：" + ((e && e.message) || e), { sticky: true });
  }
}

function initRestore() {
  const close = $("restore-close");
  if (close) close.addEventListener("click", restoreClose);
  const box = $("restore");
  if (box) box.addEventListener("click", (e) => { if (e.target === box) restoreClose(); });
  const reload = $("restore-reload");
  if (reload) reload.addEventListener("click", restoreLoadOverview);
  const all = $("restore-all");
  if (all) all.addEventListener("click", () => { restoreClose(); quarOpen(); });
  const git = $("restore-git");
  if (git) git.addEventListener("click", restoreGitClean);
  const cats = $("restore-cats");
  if (cats) cats.addEventListener("click", (e) => {
    const b = e.target.closest("[data-rcat]");
    if (!b) return;
    const c = ((RESTORE.data && RESTORE.data.categories) || []).find((x) => x.id === b.dataset.rcat);
    RESTORE.cat = b.dataset.rcat;
    RESTORE.proj = (c && c.projects && c.projects[0]) ? c.projects[0].key : "";
    renderRestoreCats();
    renderRestoreProjects();
    if (RESTORE.proj) restoreLoadTimeline(); else renderRestoreFiles();
  });
  const projs = $("restore-projects");
  if (projs) projs.addEventListener("click", (e) => {
    const b = e.target.closest("[data-rproj]");
    if (!b) return;
    RESTORE.proj = b.dataset.rproj;
    renderRestoreProjects();
    restoreLoadTimeline();
  });
  const files = $("restore-files");
  if (files) files.addEventListener("click", (e) => {
    const b = e.target.closest("[data-rfile]");
    if (b) restoreRun(b);
  });
}
