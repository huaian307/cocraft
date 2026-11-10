/* 15-planner.js —— 策划会话（Planner，P0）
 *
 * 最小闭环：创建策划会话 → 查看/编辑 plan.json → 校验确认 → 启动执行 → 查看状态/报告。
 * 数据走 /planner/*（本机 HTTP）；执行复用 orchestrator run。
 */
(function () {
  "use strict";

  const P = {
    sessions: [],
    sid: "",
    token: "", // 仅当前页面会话内存；创建时返回
    data: null, // {session, plan, validation, run}
    busy: false,
    timer: 0,
    tab: "plan",   // ★计划 / 对话（主控聊天面板内）
    chat: null,    // {sid, chatId, messages, loading, sending, error}
  };

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function fmtTs(ts) {
    if (!ts) return "";
    try { return new Date(Number(ts) * 1000).toLocaleString("zh-CN", { hour12: false }); } catch { return ""; }
  }
  function badge(status) {
    const m = { draft: "草稿", confirmed: "已确认", planning: "规划中", running: "运行中", done: "完成", failed: "失败", canceled: "已取消", idle: "空闲", paused: "暂停" };
    const cls = ({ running: "run", done: "ok", failed: "bad", canceled: "mut", confirmed: "ok" })[status] || "mut";
    return `<span class="pl-badge pl-badge-${cls}">${esc(m[status] || status || "")}</span>`;
  }

  async function jget(path) { return api(path); }
  async function jpost(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
  }

  function root() { return $("planner"); }
  function open(sid) {
    const box = root(); if (!box) return;
    const wasOpen = !box.hidden;
    box.hidden = false;
    if (!wasOpen) { try { OVERLAY.push("planner", close); } catch {} }
    const want = String(sid || "");
    loadSessions().then(() => {
      if (want) return select(want);
      if (!P.sid && P.sessions[0]) return select(P.sessions[0].id);
      if (P.sid) return refresh();
      render();
    });
  }
  function close() {
    const box = root(); if (box) box.hidden = true;
    try { OVERLAY.pop("planner"); } catch {}
    stopPoll();
  }
  function toggle() { const box = root(); if (box && !box.hidden) close(); else open(); }

  async function loadSessions() {
    try {
      const r = await jget("/planner/sessions?limit=60");
      P.sessions = (r && (r.sessions || r.data && r.data.sessions)) || [];
    } catch (e) { P.sessions = []; }
    renderList();
  }

  async function select(sid) {
    P.sid = String(sid || "");
    P.chat = null;
    await refresh();
    startPollIfRunning();
  }

  async function refresh() {
    if (!P.sid) { P.data = null; render(); return; }
    P.busy = true; render();
    try {
      const st = await jget("/planner/status?id=" + encodeURIComponent(P.sid));
      const pr = await jget("/planner/plan?id=" + encodeURIComponent(P.sid)).catch(() => null);
      P.data = { session: st && st.session, plan: (pr && pr.plan) || (st && st.plan), validation: (pr && pr.validation) || (st && st.validation), run: st && st.run };
      if (st && st.session) {
        // update token not returned; keep local
        const idx = P.sessions.findIndex((s) => s.id === P.sid);
        if (idx >= 0) P.sessions[idx] = { ...P.sessions[idx], ...st.session };
      }
    } catch (e) {
      P.data = { error: String(e && e.message || e) };
    }
    P.busy = false;
    renderList();
    render();
    startPollIfRunning();
  }

  function stopPoll() { if (P.timer) { clearInterval(P.timer); P.timer = 0; } }
  function startPollIfRunning() {
    stopPoll();
    const st = P.data && P.data.session && P.data.session.status;
    if (st === "running") P.timer = setInterval(refresh, 2500);
  }

  async function createSession() {
    const title = prompt("策划会话名称（可空）：", "") || "";
    const cwd = prompt("项目目录（cwd，必填）：", (typeof state !== "undefined" && state.cwd) ? state.cwd : "") || "";
    if (!cwd.trim()) { alert("必须填写项目目录"); return; }
    const goal = prompt("目标（一句话，可空）：", "") || "";
    try {
      const r = await jpost("/planner/session", { title, cwd, goal });
      if (!r || r.ok === false) throw new Error((r && r.error) || "创建失败");
      if (r.token) P.token = r.token;
      const sid = r.session && r.session.id;
      await loadSessions();
      if (sid) await select(sid);
      if (r.token) alert("策划会话已创建。\n\nCLI 可用环境变量：\nCOCRAFT_PLANNER_SESSION=" + sid + "\nCOCRAFT_PLANNER_TOKEN=" + r.token);
    } catch (e) { alert("创建失败：" + (e && e.message || e)); }
  }

  async function savePlan(confirm) {
    if (!P.sid) return;
    const ta = $("planner-plan-json");
    if (!ta) return;
    let plan;
    try { plan = JSON.parse(ta.value); } catch (e) { alert("plan JSON 格式错误：" + e.message); return; }
    try {
      // P1 人审：保存/确认前先做变更对比；有实质变更时展示摘要让用户确认
      if (confirm || true) {
        try {
          const d = await jpost("/planner/plan/diff", { id: P.sid, plan });
          if (d && d.hasChanges && d.changeCount > 0) {
            const lines = (d.changes || []).slice(0, 12).map((c) => "· " + (c.label || c.field) + (c.stageId ? " [" + c.stageId + "]" : "") + (c.name ? " " + c.name : "") + (c.from !== undefined ? ("：" + JSON.stringify(c.from) + " → " + JSON.stringify(c.to)) : ""));
            const v = d.validation || {};
            const warn = (v.warnings || []).length ? ("\n\n⚠ 校验提醒：\n- " + v.warnings.join("\n- ")) : "";
            const err = (v.errors || []).length ? ("\n\n❌ 校验错误：\n- " + v.errors.join("\n- ")) : "";
            const ok = window.confirm("计划变更审查（" + (d.summary || "") + "）\n\n" + (lines.join("\n") || "内容有变化") + warn + err + "\n\n确认" + (confirm ? "保存并作为可执行计划" : "保存草稿") + "？");
            if (!ok) return;
          } else if (confirm && d && d.validation && d.validation.ok === false) {
            alert("计划校验未通过：\n- " + ((d.validation.errors || []).join("\n- ") || "未知错误"));
            return;
          }
        } catch (_) { /* diff 失败不阻塞，但 confirm 路径仍由后端校验 */ }
      }
      const r = await jpost("/planner/plan", { id: P.sid, plan, confirm: !!confirm });
      if (!r || r.ok === false) {
        const errs = (r && r.validation && r.validation.errors) || (r && r.errors) || [];
        alert("保存失败：" + ((r && r.error) || "") + (errs.length ? "\n- " + errs.join("\n- ") : ""));
      }
      await refresh();
    } catch (e) { alert("保存失败：" + (e && e.message || e)); }
  }

  async function showDiff() {
    if (!P.sid) return;
    const ta = $("planner-plan-json");
    if (!ta) return;
    let plan;
    try { plan = JSON.parse(ta.value); } catch (e) { alert("plan JSON 格式错误：" + e.message); return; }
    try {
      const d = await jpost("/planner/plan/diff", { id: P.sid, plan });
      const lines = (d.changes || []).map((c) => "· " + (c.label || c.field) + (c.stageId ? " [" + c.stageId + "]" : "") + (c.name ? " " + c.name : "") + (c.from !== undefined ? ("：" + JSON.stringify(c.from).slice(0,80) + " → " + JSON.stringify(c.to).slice(0,80)) : ""));
      const v = d.validation || {};
      const extra = [];
      if ((v.errors || []).length) extra.push("❌ 错误：\n- " + v.errors.join("\n- "));
      if ((v.warnings || []).length) extra.push("⚠ 提醒：\n- " + v.warnings.join("\n- "));
      alert("计划变更审查\n\n" + (d.summary || "") + "\n" + (lines.length ? "\n" + lines.join("\n") : "\n（与当前已保存计划无结构化差异）") + (extra.length ? "\n\n" + extra.join("\n\n") : ""));
    } catch (e) { alert("审查失败：" + (e && e.message || e)); }
  }

  async function startRun() {
    if (!P.sid) return;
    if (!confirm("按当前计划启动执行？（会调用工作流 Runner）")) return;
    try {
      const r = await jpost("/planner/run/start", { id: P.sid });
      if (!r || r.ok === false) throw new Error((r && r.error) || "启动失败");
      await refresh();
    } catch (e) { alert("启动失败：" + (e && e.message || e)); }
  }

  async function cancelRun() {
    if (!P.sid) return;
    try {
      const r = await jpost("/planner/run/cancel", { id: P.sid });
      if (!r || r.ok === false) throw new Error((r && r.error) || "取消失败");
      await refresh();
    } catch (e) { alert("取消失败：" + (e && e.message || e)); }
  }

  async function showReport() {
    if (!P.sid) return;
    try {
      const r = await jget("/planner/report?id=" + encodeURIComponent(P.sid));
      const rep = r && (r.report || r);
      const md = (rep && rep.markdown) || (r && r.markdown) || JSON.stringify(rep, null, 2);
      const w = window.open("", "_blank");
      if (w) { w.document.write("<pre style='white-space:pre-wrap;font:14px/1.5 ui-sans-serif;padding:20px'>" + esc(md) + "</pre>"); w.document.close(); }
      else alert(md);
    } catch (e) { alert("报告生成失败：" + (e && e.message || e)); }
  }

  async function writeHandoff() {
    if (!P.sid) return;
    const note = prompt("交接备注（可空，写给接棒者）：", "") || "";
    try {
      const r = await jpost("/planner/handoff", { id: P.sid, reason: "manual", note });
      if (!r || r.ok === false) throw new Error((r && r.error) || "写交接失败");
      alert("已生成 HANDOFF.md\n" + (r.path || ""));
      await refresh();
    } catch (e) { alert("写交接失败：" + (e && e.message || e)); }
  }

  async function showHandoff() {
    if (!P.sid) return;
    try {
      const r = await jget("/planner/handoff?id=" + encodeURIComponent(P.sid));
      const md = (r && r.markdown) || "（还没有 HANDOFF.md，点『写交接』生成）";
      const w = window.open("", "_blank");
      if (w) { w.document.write("<pre style='white-space:pre-wrap;font:14px/1.5 ui-sans-serif;padding:20px'>" + esc(md) + "</pre>"); w.document.close(); }
      else alert(md);
    } catch (e) { alert("读取交接失败：" + (e && e.message || e)); }
  }

  async function switchAgent() {
    if (!P.sid) return;
    const to = prompt("切换到哪个策划 agent id？（会先自动写 HANDOFF.md，再切换并做能力复核）", "") || "";
    if (!to.trim()) return;
    const note = prompt("切换备注（可空）：", "") || "";
    try {
      const r = await jpost("/planner/switch-agent", { id: P.sid, agent: to.trim(), note });
      if (!r || r.ok === false) throw new Error((r && r.error) || "切换失败");
      const rev = r.review || {};
      const findings = (rev.findings || []).map((f) => "· " + (f.msg || JSON.stringify(f))).join("\n");
      const warns = (rev.warnings || []).map((w) => "· " + w).join("\n");
      alert("已切换策划 agent\n" + (r.fromAgent || "") + " → " + (r.toAgent || "") +
            "\n\nHANDOFF.md 已生成。\n" +
            (findings ? "\n能力复核发现：\n" + findings : "\n能力复核：未发现阻塞项。") +
            (warns ? "\n\n提醒：\n" + warns : "") +
            "\n\n接棒时请先注入：python tools/planner.py handoff show --id " + P.sid + " --context");
      await loadSessions(); await refresh();
    } catch (e) { alert("切换失败：" + (e && e.message || e)); }
  }

  async function showResumeContext() {
    if (!P.sid) return;
    try {
      const r = await jget("/planner/resume?id=" + encodeURIComponent(P.sid));
      const ctx = r && r.context;
      const text = (ctx && ctx.text) || (r && r.markdown) || JSON.stringify(r, null, 2);
      const w = window.open("", "_blank");
      if (w) { w.document.write("<pre style='white-space:pre-wrap;font:14px/1.5 ui-sans-serif;padding:20px'>" + esc(text) + "</pre>"); w.document.close(); }
      else alert(String(text).slice(0, 4000));
    } catch (e) { alert("获取接棒上下文失败：" + (e && e.message || e)); }
  }

  async function sendFromComposer() {
    const input = $("input");
    const text = String((input && input.value) || "").trim();
    if (!text) return;
    const sid = String((state.current && (state.current._plannerSid
      || String(state.current.id || "").replace(/^planner:/, ""))) || "");
    if (!sid) return;
    let sess = {};
    try {
      const st = await jget("/planner/status?id=" + encodeURIComponent(sid));
      sess = (st && st.session) || {};
    } catch (e) {
      setBanner("读取策划会话失败：" + (e && e.message || e), { ms: 9000 });
      return;
    }
    let chatId = String(sess.chatSessionId || "");
    let createdChat = false;
    if (!chatId) {
      try {
        const cwd = sess.cwd || "";
        const title = "策划 · " + (sess.title || "主控");
        let created = null;
        try {
          if (typeof createSessionWith === "function") created = await createSessionWith(cwd, title);
        } catch { /* fall through */ }
        if (!created) {
          const r = await jpost("/api/session", cwd ? { cwd, title } : { title });
          created = (r && (r.data || r.session || r)) || null;
        }
        chatId = String((created && (created.id || created.sessionID || created.sessionId)) || "");
        if (!chatId) throw new Error("创建聊天会话失败（没返回 id）");
        await jpost("/planner/bind", { id: sid, chatSessionId: chatId });
        createdChat = true;
      } catch (e) {
        setBanner("打开主控聊天失败：" + (e && e.message || e), { ms: 9000 });
        return;
      }
    }
    // 先清空输入区（否则 openSession 之后还带着这句）
    input.value = "";
    input.style.height = "auto";
    const atts = (typeof state !== "undefined" && state.attachments) ? state.attachments.slice() : [];
    if (typeof state !== "undefined") state.attachments = [];
    try { renderAttachRow(); } catch { /* ignore */ }
    try {
      if (createdChat && typeof loadSessions === "function") await loadSessions();
      await openSession(chatId);
      // 新聊天：把「接棒上下文 + 用户需求」合成第一条（否则新会话不知道 plan）
      let first = text;
      if (createdChat) {
        try {
          const rs = await jget("/planner/resume?id=" + encodeURIComponent(sid));
          const ctx = (rs && rs.context && rs.context.text) || "";
          if (ctx) {
            first = "【策划会话接棒】以下是交接/上下文（请先阅读；不要重设计 plan.json，除非校验失败或用户明确要求）。\n\n"
              + ctx + "\n\n【用户需求】\n" + text;
          }
        } catch { /* 没上下文也照发 */ }
      }
      const ci = $("input");
      if (ci) { ci.value = first; ci.style.height = "auto"; }
      if (typeof state !== "undefined") state.attachments = atts;
      try { renderAttachRow(); } catch { /* ignore */ }
      // 主控视图可能是在协作页里打开的：发送前把协作页收掉，否则 send() 会走「工作流任务条」
      try {
        if (typeof collabIsOpen === "function" && collabIsOpen()
            && typeof collabOpen === "function") await collabOpen(false);
      } catch { /* ignore */ }
      await send();
      setBanner(createdChat
        ? "已创建主控聊天会话（计划面板仍可从左侧「策划」会话打开）"
        : "已切到主控聊天会话", { ms: 8000 });
    } catch (e) {
      setBanner("发送到主控聊天失败：" + (e && e.message || e), { ms: 9000 });
      if (input && !input.value) input.value = text;
    }
  }

  async function openBoundChat() {
    if (!P.sid) return;
    // ★已经绑定过聊天会话 → 直接打开它（以前每次点都新建一条，越点越多）
    try {
      const st = await jget("/planner/status?id=" + encodeURIComponent(P.sid));
      const existing = String((st && st.session && st.session.chatSessionId) || "");
      if (existing) {
        close();
        try { await loadSessions(); } catch { /* ignore */ }
        await openSession(existing);
        setBanner("已打开主控聊天会话", { ms: 6000 });
        return;
      }
    } catch { /* 没绑定就按下面新建 */ }
    if (!confirm("创建一个新的聊天会话，绑定到当前策划会话，并自动注入接棒上下文？")) return;
    try {
      const rs = await jget("/planner/resume?id=" + encodeURIComponent(P.sid));
      const sess = (P.data && P.data.session) || (rs && rs.session) || {};
      const cwd = sess.cwd || "";
      const contextText = (rs && rs.context && rs.context.text) || "";
      // 1) create chat session via existing engine API
      let created = null;
      try {
        if (typeof createSessionWith === "function" && cwd) {
          created = await createSessionWith(cwd, "策划 · " + (sess.title || "策划会话"));
        }
      } catch {}
      if (!created) {
        const r = await jpost("/api/session", cwd ? { cwd, title: "策划 · " + (sess.title || "策划会话") } : { title: "策划 · " + (sess.title || "策划会话") });
        created = (r && (r.data || r.session || r)) || null;
      }
      const chatId = created && (created.id || created.sessionID || created.sessionId);
      if (!chatId) throw new Error("创建聊天会话失败（未返回 id）");
      // 2) bind
      await jpost("/planner/bind", { id: P.sid, chatSessionId: chatId });
      // 3) inject context as first prompt (best-effort)
      if (contextText) {
        const intro = "【策划会话接棒】以下是交接/上下文（请先阅读；不要重设计 plan.json，除非校验失败或用户明确要求）。\n\n" + contextText;
        try { await jpost("/api/session/" + encodeURIComponent(chatId) + "/prompt", { text: intro }); } catch (e) { console.warn("planner inject prompt", e); }
      }
      // 4) navigate
      close();
      try { if (typeof loadSessions === "function") await loadSessions(); } catch {}
      try { if (typeof openSession === "function") await openSession(chatId); } catch {}
    } catch (e) { alert("打开绑定聊天会话失败：" + (e && e.message || e)); }
  }

  /** 保证有绑定的主控聊天（没有就建一条并 /planner/bind），返回 chatId。不跳页。 */
  async function ensureBoundChat() {
    const sess = (P.data && P.data.session) || {};
    let chatId = String(sess.chatSessionId || "");
    if (chatId) return chatId;
    const cwd = sess.cwd || "";
    const title = "策划 · " + (sess.title || "策划会话");
    let created = null;
    try {
      if (typeof createSessionWith === "function") created = await createSessionWith(cwd, title);
    } catch { /* fall through */ }
    if (!created) {
      const r = await jpost("/api/session", cwd ? { cwd, title } : { title });
      created = (r && (r.data || r.session || r)) || null;
    }
    chatId = String((created && (created.id || created.sessionID || created.sessionId)) || "");
    if (!chatId) throw new Error("创建聊天会话失败（未返回 id）");
    await jpost("/planner/bind", { id: P.sid, chatSessionId: chatId });
    if (P.data && P.data.session) P.data.session.chatSessionId = chatId;
    return chatId;
  }

  function chatTextOf(m) {
    if (!m) return "";
    if (m.type === "user") return String(m.text || "");
    if (m.type !== "assistant") return "";
    const arr = Array.isArray(m.content) ? m.content : [];
    return arr.filter((p) => p && p.type === "text").map((p) => String(p.text || "")).join("\n").trim();
  }

  async function loadChat(sess) {
    sess = sess || (P.data && P.data.session) || {};
    const sid = P.sid;
    const chatId = String(sess.chatSessionId || "");
    P.chat = { sid: sid, chatId: chatId, messages: [], loading: !!chatId, sending: false, error: "" };
    render();
    if (!chatId) return;
    try {
      const r = await jget("/api/session/" + encodeURIComponent(chatId) + "/message?limit=40");
      const list = (r && (r.data || r.messages)) || [];
      // /message 是「新→旧」；聊天气泡按旧→新显示
      P.chat.messages = (Array.isArray(list) ? list.slice().reverse() : []);
    } catch (e) {
      P.chat.error = "读取聊天失败：" + (e && e.message || e);
    }
    P.chat.loading = false;
    if (P.sid === sid && P.tab === "chat") render();
  }

  async function sendChat() {
    const sess = (P.data && P.data.session) || {};
    const input = document.getElementById("pl-chat-input");
    const text = String((input && input.value) || "").trim();
    if (!text) return;
    const sid = P.sid;
    let chatId = String(sess.chatSessionId || "");
    let created = false;
    try {
      if (!chatId) { chatId = await ensureBoundChat(); created = true; }
    } catch (e) {
      setBanner("创建主控聊天失败：" + (e && e.message || e), { ms: 9000 });
      return;
    }
    let first = text;
    if (created) {
      try {
        const rs = await jget("/planner/resume?id=" + encodeURIComponent(sid));
        const ctx = (rs && rs.context && rs.context.text) || "";
        if (ctx) {
          first = "【策划会话接棒】以下是交接/上下文（请先阅读；不要重设计 plan.json，除非校验失败或用户明确要求）。\n\n"
            + ctx + "\n\n【用户需求】\n" + text;
        }
      } catch { /* 没上下文也照发 */ }
    }
    if (input) input.value = "";
    P.chat = P.chat || { sid: sid, chatId: chatId, messages: [], loading: false, sending: false, error: "" };
    P.chat.sending = true; P.chat.error = "";
    render();
    try {
      await jpost("/api/session/" + encodeURIComponent(chatId) + "/prompt", { text: first });
      await loadChat(P.data && P.data.session);
    } catch (e) {
      P.chat.sending = false;
      P.chat.error = "发送失败：" + (e && e.message || e);
      render();
    }
  }

  function openChatFull() {
    const chatId = String((P.data && P.data.session && P.data.session.chatSessionId) || "");
    if (!chatId) { alert("还没有绑定聊天会话；先在「对话」里发一句，会自动创建。"); return; }
    close();
    try { if (typeof loadSessions === "function") loadSessions(); } catch { /* ignore */ }
    try { if (typeof openSession === "function") openSession(chatId); } catch { /* ignore */ }
  }

  async function retryRun() {
    if (!P.sid) return;
    const mode = prompt("输入模式：retry=重试（保留计划，计数+1） / replan=重规划（计数+1，回草稿需重审）", "retry") || "";
    if (!mode.trim()) return;
    const note = prompt("备注（可空）：", "") || "";
    try {
      const r = await jpost("/planner/run/retry", { id: P.sid, mode: mode.trim(), note });
      if (r && r.ok === false) throw new Error(r.error || "失败");
      alert("已处理：" + (r.mode || mode) + (r.runId ? "\nrunId=" + r.runId : "")
        + (r.plan ? ("\nretryCount=" + (((r.plan.meta || {}).retryCount) || 0)) : "")
        + (r.message ? ("\n" + r.message) : ""));
      await refresh();
    } catch (e) { alert("重试/重规划失败：" + (e && e.message || e)); }
  }

  async function isolationMenu() {
    if (!P.sid) return;
    const sub = prompt("操作：list / apply:<memberId> / applyAll / cleanup", "list") || "";
    if (!sub.trim()) return;
    const s = sub.trim();
    try {
      if (s === "list") {
        const r = await jget("/planner/isolation/list?id=" + encodeURIComponent(P.sid));
        alert(JSON.stringify(r, null, 2).slice(0, 5000));
        return;
      }
      if (s === "cleanup") {
        if (!confirm("清理隔离副本？")) return;
        alert(JSON.stringify(await jpost("/planner/isolation/cleanup", { id: P.sid }), null, 2).slice(0, 3000));
        await refresh();
        return;
      }
      if (s === "applyAll") {
        if (!confirm("应用所有成员改动到项目目录？")) return;
        alert(JSON.stringify(await jpost("/planner/isolation/apply", { id: P.sid, all: true }), null, 2).slice(0, 5000));
        await refresh();
        return;
      }
      if (s.indexOf("apply:") === 0) {
        const mid = s.slice(6).trim();
        if (!mid) { alert("用法：apply:<memberId>"); return; }
        if (!confirm("应用成员 " + mid + " 的改动？")) return;
        alert(JSON.stringify(await jpost("/planner/isolation/apply", { id: P.sid, memberId: mid }), null, 2).slice(0, 5000));
        await refresh();
        return;
      }
      alert("不认识的子命令：" + s);
    } catch (e) { alert("隔离操作失败：" + (e && e.message || e)); }
  }

  async function delSession() {
    if (!P.sid) return;
    if (!confirm("删除这个策划会话？（计划文件保留在磁盘目录中）")) return;
    try { await fetch("/planner/session?id=" + encodeURIComponent(P.sid), { method: "DELETE" }); } catch {}
    P.sid = ""; P.data = null;
    await loadSessions(); render();
  }

  function renderList() {
    const el = $("planner-list"); if (!el) return;
    if (!P.sessions.length) { el.innerHTML = '<div class="muted" style="padding:10px">还没有策划会话</div>'; return; }
    el.innerHTML = P.sessions.map((s) => {
      const on = s.id === P.sid ? " on" : "";
      return `<button type="button" class="pl-item${on}" data-sid="${esc(s.id)}">
        <div class="pl-item-title">${esc(s.title || "策划会话")}</div>
        <div class="pl-item-meta">${badge(s.status || "idle")} <span class="muted">${esc(s.cwd || "")}</span></div>
      </button>`;
    }).join("");
    el.querySelectorAll("[data-sid]").forEach((b) => b.addEventListener("click", () => select(b.getAttribute("data-sid"))));
  }

  function renderValidation(v) {
    if (!v) return "";
    const errs = v.errors || [];
    const warns = v.warnings || [];
    if (!errs.length && !warns.length) return `<div class="pl-v pl-v-ok">校验通过${v.canRun ? "，可以执行" : ""}</div>`;
    return `<div class="pl-v ${errs.length ? "pl-v-bad" : "pl-v-warn"}">
      ${errs.length ? `<div><b>错误</b><ul>${errs.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
      ${warns.length ? `<div><b>提醒</b><ul>${warns.map((x) => `<li>${esc(x)}</li>`).join("")}</ul></div>` : ""}
    </div>`;
  }

  function renderRun(run) {
    if (!run) return `<div class="muted">还没有运行。确认计划后点击「开始执行」。</div>`;
    const members = (run.members || []).map((m) => {
      const st = m.status || "";
      const err = m.error || m.failReason || "";
      return `<tr><td>${esc(m.name || m.id || "")}</td><td>${esc(m.engine || "")}</td><td>${badge(st)}</td><td class="muted">${esc(String(err).slice(0, 120))}</td></tr>`;
    }).join("");
    return `<div class="pl-card">
      <div class="pl-card-h"><b>运行</b> ${badge(run.status)} <span class="muted">#${esc(run.id || "")}</span></div>
      ${run.failReason ? `<div class="pl-v pl-v-bad">${esc(run.failReason)}</div>` : ""}
      <table class="pl-table"><thead><tr><th>成员</th><th>引擎</th><th>状态</th><th>说明</th></tr></thead><tbody>${members || '<tr><td colspan="4" class="muted">无</td></tr>'}</tbody></table>
    </div>`;
  }

  function chatHtml(sess) {
    const chatId = String((sess && sess.chatSessionId) || "");
    const c = P.chat || { messages: [], loading: false, sending: false, error: "" };
    const msgs = (c.messages || []).map((m) => {
      const txt = chatTextOf(m);
      if (!txt) return "";
      const who = m.type === "user" ? "我" : "主控";
      return `<div class="pl-chat-m ${m.type}"><b>${who}</b><div>${esc(txt).replace(/\n/g, "<br>")}</div></div>`;
    }).join("");
    return `
      <div class="pl-head">
        <div><b>${esc(sess.title || "策划会话")}</b> ${badge(sess.status || "idle")}
          <span class="pl-badge pl-badge-mut">主控聊天</span></div>
        <div class="muted">${chatId ? ("绑定聊天：" + esc(chatId)) : "还没有绑定聊天（发送时自动创建）"}</div>
      </div>
      <div class="pl-chat">
        <div class="pl-chat-hd">
          <span class="muted">${c.loading ? "加载中…" : "最近 40 条"}</span>
          <span class="collab-sp"></span>
          <button type="button" id="pl-chat-reload">刷新</button>
          <button type="button" id="pl-chat-full" ${chatId ? "" : "disabled"}>打开完整聊天</button>
        </div>
        <div class="pl-chat-list">${c.loading ? '<div class="muted">加载中…</div>' : (msgs || '<div class="muted">还没有消息。下面输入你的需求。</div>')}</div>
        ${c.error ? `<div class="pl-v pl-v-bad">${esc(c.error)}</div>` : ""}
        <div class="pl-chat-send">
          <textarea id="pl-chat-input" placeholder="${chatId ? "告诉主控你的需求…（Enter 发送，Shift+Enter 换行）" : "发送时会自动创建并绑定主控聊天"}"></textarea>
          <button type="button" id="pl-chat-sendbtn" class="primary" ${c.sending ? "disabled" : ""}>${c.sending ? "发送中…" : "发送"}</button>
        </div>
      </div>`;
  }

  function wirePlannerTabs() {
    const tp = document.getElementById("pl-tab-plan"), tc = document.getElementById("pl-tab-chat");
    if (tp) tp.onclick = () => { P.tab = "plan"; render(); };
    if (tc) tc.onclick = () => {
      P.tab = "chat";
      if (!P.chat || P.chat.sid !== P.sid) { loadChat(); return; }
      render();
    };
  }

  function wirePlannerChat(sess) {
    const re = document.getElementById("pl-chat-reload"); if (re) re.onclick = () => loadChat(sess);
    const full = document.getElementById("pl-chat-full"); if (full && !full.disabled) full.onclick = openChatFull;
    const btn = document.getElementById("pl-chat-sendbtn"); if (btn) btn.onclick = () => sendChat();
    const ta = document.getElementById("pl-chat-input");
    if (ta) {
      ta.onkeydown = (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendChat(); }
      };
      setTimeout(() => { try { ta.focus(); } catch { /* ignore */ } }, 0);
    }
  }

  function render() {
    const body = $("planner-body"); if (!body) return;
    if (!P.sid) { body.innerHTML = '<div class="muted" style="padding:18px">左侧选择一个策划会话，或点击「新建」。</div>'; return; }
    if (P.busy && !P.data) { body.innerHTML = '<div class="muted" style="padding:18px">加载中…</div>'; return; }
    const d = P.data || {};
    if (d.error) { body.innerHTML = `<div class="pl-v pl-v-bad">${esc(d.error)}</div>`; return; }
    const sess = d.session || {};
    const plan = d.plan || {};
    const v = d.validation || {};
    const run = d.run;
    const tabs = `<div class="pl-tabs">
      <button type="button" id="pl-tab-plan" class="${P.tab === "chat" ? "" : "on"}">计划</button>
      <button type="button" id="pl-tab-chat" class="${P.tab === "chat" ? "" : "on"}">对话</button>
    </div>`;
    if (P.tab === "chat") {
      if (!P.chat || P.chat.sid !== P.sid) { loadChat(sess); return; }
      body.innerHTML = tabs + chatHtml(sess);
      wirePlannerTabs();
      wirePlannerChat(sess);
      return;
    }
    if (P.chat && P.chat.sid !== P.sid) P.chat = null;
    body.innerHTML = tabs + `
      <div class="pl-head">
        <div><b>${esc(sess.title || "策划会话")}</b> ${badge(sess.status || "idle")} ${badge(plan.status || "draft")}${sess.plannerAgent ? ` <span class="pl-badge pl-badge-mut">agent:${esc(sess.plannerAgent)}</span>` : ""}</div>
        <div class="muted">${esc(sess.cwd || "")} · v${esc(plan.version || 1)} · ${esc(fmtTs(sess.updatedAt))}${sess.handoffAt ? " · 已交接 " + esc(fmtTs(sess.handoffAt)) : ""}</div>
      </div>
      <div class="pl-actions">
        <button type="button" id="pl-save">保存草稿</button>
        <button type="button" id="pl-confirm" class="primary">校验并确认</button>
        ${sess.status === "running"
          ? `<button type="button" id="pl-cancel">取消运行</button>`
          : `<button type="button" id="pl-run">开始执行</button>`}
        <button type="button" id="pl-open-chat" class="primary">主控聊天</button>
        <button type="button" id="pl-refresh">刷新</button>
      </div>
      <details class="pl-more" id="pl-more">
        <summary>更多操作（报告 / 交接 / 重试 / 隔离 / 删除）▾</summary>
        <div class="pl-more-menu">
          ${sess.status === "running"
            ? `<button type="button" id="pl-run" disabled>开始执行（运行中）</button>`
            : `<button type="button" id="pl-cancel" disabled>取消运行（未运行）</button>`}
          <button type="button" id="pl-diff">审查变更</button>
          <button type="button" id="pl-report">完成报告</button>
          <button type="button" id="pl-retry">重试/重规划</button>
          <button type="button" id="pl-isolation">隔离/应用改动</button>
          <button type="button" id="pl-handoff">写交接</button>
          <button type="button" id="pl-handoff-show">看交接</button>
          <button type="button" id="pl-resume">接棒上下文</button>
          <button type="button" id="pl-switch">换策划agent</button>
          <button type="button" id="pl-del" class="danger">删除会话</button>
        </div>
      </details>
      ${renderValidation(v)}
      <div class="pl-cols">
        <details class="pl-card pl-json-adv">
          <summary>高级：直接编辑 Plan JSON（一般不需要）</summary>
          <textarea id="planner-plan-json" class="pl-json" spellcheck="false">${esc(JSON.stringify(plan, null, 2))}</textarea>
        </details>
        <div>
          ${renderRun(run)}
          <details class="pl-card">
            <summary>? 说明</summary>
            <div class="muted" style="font-size:13px;line-height:1.5">
              策划会话是工作流的控制器：计划定义阶段/成员/权限，运行走本地 Runner。<br>
              破坏性操作仍需你确认；策划 CLI 不能批准权限。<br>
              CLI：<code>python tools/planner.py status --id ${esc(P.sid)}</code>
            </div>
          </details>
        </div>
      </div>`;
    const byId = (id) => $(id);
    if (byId("pl-diff")) byId("pl-diff").onclick = showDiff;
    if (byId("pl-save")) byId("pl-save").onclick = () => savePlan(false);
    if (byId("pl-confirm")) byId("pl-confirm").onclick = () => savePlan(true);
    if (byId("pl-run")) byId("pl-run").onclick = startRun;
    if (byId("pl-cancel")) byId("pl-cancel").onclick = cancelRun;
    if (byId("pl-report")) byId("pl-report").onclick = showReport;
    if (byId("pl-retry")) byId("pl-retry").onclick = retryRun;
    if (byId("pl-isolation")) byId("pl-isolation").onclick = isolationMenu;
    if (byId("pl-handoff")) byId("pl-handoff").onclick = writeHandoff;
    if (byId("pl-handoff-show")) byId("pl-handoff-show").onclick = showHandoff;
    if (byId("pl-switch")) byId("pl-switch").onclick = switchAgent;
    if (byId("pl-resume")) byId("pl-resume").onclick = showResumeContext;
    if (byId("pl-open-chat")) byId("pl-open-chat").onclick = () => {
      P.tab = "chat";
      if (!P.chat || P.chat.sid !== P.sid) loadChat(); else render();
    };
    if (byId("pl-refresh")) byId("pl-refresh").onclick = refresh;
    if (byId("pl-del")) byId("pl-del").onclick = delSession;
    wirePlannerTabs();
  }


  function card(title, body, extra) {
    return `<div class="pl-rich-card"><div class="pl-rich-hd"><b>${esc(title)}</b>${extra||""}</div><div class="pl-rich-bd">${body}</div></div>`;
  }

  async function renderPlannerRichView(sid) {
    sid = String(sid || "");
    if (!sid) return;
    // ⚠ 只写 #msg-static，**不能写 #messages**：后者里面还有 #msg-more / #msg-static / #live-slot，
    //   整块替换会把它们删掉 → 切回普通会话 / 切引擎时 swapHtml($("msg-static")) 直接 null 崩。
    const host = $("msg-static") || $("messages");
    if (!host) return;
    host.innerHTML = `<div class="muted" style="padding:18px">加载策划会话…</div>`;
    let st, rep, appr;
    try { st = await jget("/planner/status?id=" + encodeURIComponent(sid)); } catch (e) { host.innerHTML = `<div class="error-box">加载失败：${esc((e&&e.message)||e)}</div>`; return; }
    try { rep = await jget("/planner/report?id=" + encodeURIComponent(sid)); } catch { rep = null; }
    try { appr = await jget("/planner/approvals?id=" + encodeURIComponent(sid)); } catch { appr = null; }
    const sess = st.session || {}; const plan = st.plan || {}; const vr = st.validation || {}; const run = st.run || {};
    const approvals = (appr && appr.approvals) || [];
    const report = (rep && rep.report) || {};
    const stages = (plan.stages || []).map((s,i)=>`<li><b>${esc(s.name||s.id||("阶段"+(i+1)))}</b> ${badge(s.status||"")} <span class="muted">${esc(s.objective||"")}</span><div class="muted" style="font-size:12px">when=${esc(s.when||"always")} · onFail=${esc(s.failPolicy||s.onFail||"stop")} · retries=${esc(String(s.retries||0))} · 成员 ${(s.members||[]).length}</div></li>`).join("");
    const errs = (vr.errors||[]).map(e=>`<li>${esc(e)}</li>`).join("");
    const warns = (vr.warnings||[]).map(e=>`<li>${esc(e)}</li>`).join("");
    const runMembers = ((run && run.members)||[]).map(m=>`<div class="pl-rich-mem"><b>${esc(m.name||m.id||"")}</b> ${badge(m.status||"")} <span class="muted">${esc(m.role||"")}</span>${m.error?`<div class="error-box" style="margin-top:4px">${esc(String(m.error).slice(0,300))}</div>`:""}</div>`).join("");
    const next = (report.nextActions||[]).map(a=>`<li>${esc(a)}</li>`).join("");
    const actions = `
      <div class="pl-rich-actions">
        <button type="button" class="primary" onclick="window._plannerRichAction('openPanel','${esc(sid)}')">打开策划面板</button>
        <button type="button" onclick="window._plannerRichAction('bindChat','${esc(sid)}')" title="打开已绑定的主控聊天；没有就创建并注入接棒上下文">主控聊天</button>
        <button type="button" onclick="window._plannerRichAction('retry','${esc(sid)}')">重试/重规划</button>
        <button type="button" onclick="window._plannerRichAction('isolation','${esc(sid)}')">隔离/应用改动</button>
        <button type="button" onclick="window._plannerRichAction('refresh','${esc(sid)}')">刷新</button>
      </div>`;
    host.innerHTML = `
      <div class="pl-rich">
        ${card("策划会话", `<div><b>${esc(sess.title||"")}</b> ${badge(sess.status||"")} ${badge(plan.status||"")}</div><div class="muted" style="margin-top:6px">id=${esc(sid)} · cwd=${esc(sess.cwd||"")} · plan v${esc(String(plan.version||"?"))} ${plan.id?("· "+esc(plan.id)):""}${sess.chatSessionId?(" · 已绑定聊天="+esc(sess.chatSessionId)):""}</div><div style="margin-top:8px">${esc(plan.goal||"")}</div>`)}
        ${actions}
        ${card("计划阶段（串行/条件/重试）", stages?`<ol class="pl-rich-list">${stages}</ol>`:"<div class='muted'>暂无阶段</div>", `<span class="muted">校验 ${vr.ok?"OK":"未通过"}${vr.canRun?" · 可执行":""}</span>`)}
        ${(errs||warns)?card("校验/提醒", `${errs?`<div><b>错误</b><ul class="pl-rich-list">${errs}</ul></div>`:""}${warns?`<div><b>提醒</b><ul class="pl-rich-list">${warns}</ul></div>`:""}`):""}
        ${card("执行状态", run && run.id ? `<div>run=${esc(run.id)} ${badge(run.status||"")} ${run.workflowId?`<span class="muted">workflow=${esc(run.workflowId)}</span>`:""}</div><div class="pl-rich-members">${runMembers||"<span class='muted'>无成员信息</span>"}</div>` : "<div class='muted'>尚未执行或无运行记录</div>")}
        ${card("会话内批权限联动", approvals.length ? `<div><b>待审批 ${approvals.length} 项（run 关联）</b></div><ul class="pl-rich-list">${approvals.slice(0,8).map(a=>`<li>${esc(a.title||a.action||a.kind||a.id||"审批")} <span class="muted">${esc(a.status||"")}</span></li>`).join("")}</ul><div class="muted" style="margin-top:6px">在协作/画布或托盘中批准/拒绝；本视图展示 run 关联快照。</div>` : `<div class="muted">当前无 run 关联待审批。</div>`)}
        ${card("报告与下一步", `${report.markdown?`<pre class="pl-rich-pre">${esc(String(report.markdown).slice(0,6000))}</pre>`:(next?`<ul class="pl-rich-list">${next}</ul>`:"<div class='muted'>暂无报告</div>")}`)}
      </div>`;
  }

  window._plannerRichAction = async function (action, sid, arg) {
    sid = String(sid||""); arg = String(arg||"");
    try {
      if (action === "refresh") return renderPlannerRichView(sid);
      if (action === "openPanel") { P.sid = sid; P.tab = "plan"; P.chat = null; open(); return; }
      if (action === "bindChat") { P.sid = sid; P.tab = "chat"; P.chat = null; open(); return; }
      if (action === "retry") {
        const mode = prompt("输入模式：retry=重试（保留计划，计数+1） / replan=重规划（计数+1，回草稿需重审）", "retry") || "";
        if (!mode) return;
        const note = prompt("备注（可空）：", "") || "";
        const r = await jpost("/planner/run/retry", { id: sid, mode: mode.trim(), note });
        if (r && r.ok === false) throw new Error(r.error || "失败");
        alert("已处理：" + (r.mode||mode) + (r.runId ? "\nrunId=" + r.runId : "") + (r.plan?("\nretryCount="+(((r.plan.meta||{}).retryCount)||0)):"") + (r.message?("\n"+r.message):""));
        return renderPlannerRichView(sid);
      }
      if (action === "isolation") {
        const st = await jget("/planner/status?id=" + encodeURIComponent(sid));
        const run = st.run || {};
        if (!run.id) { alert("没有运行记录，无法应用隔离区改动。"); return; }
        const sub = prompt("操作：list / apply:<memberId> / applyAll / cleanup", "list") || "";
        if (!sub) return;
        if (sub === "list") { const r = await jget("/planner/isolation/list?id="+encodeURIComponent(sid)); alert(JSON.stringify(r,null,2).slice(0,5000)); return; }
        if (sub === "cleanup") { if(!confirm("清理隔离副本？"))return; alert(JSON.stringify(await jpost("/planner/isolation/cleanup",{id:sid}),null,2).slice(0,3000)); return renderPlannerRichView(sid); }
        if (sub === "applyAll") { if(!confirm("应用所有成员改动到项目目录？"))return; alert(JSON.stringify(await jpost("/planner/isolation/apply",{id:sid,all:true}),null,2).slice(0,5000)); return renderPlannerRichView(sid); }
        if (sub.indexOf("apply:")===0) { const mid=sub.slice(6).trim(); if(!confirm("应用成员 "+mid+"？"))return; alert(JSON.stringify(await jpost("/planner/isolation/apply",{id:sid,memberId:mid}),null,2).slice(0,5000)); return renderPlannerRichView(sid); }
      }
    } catch (e) { alert((e && e.message) || e); }
  };

  window.renderPlannerRichView = renderPlannerRichView;

  function initPlanner() {
    const c = $("planner-close"); if (c) c.addEventListener("click", close);
    const n = $("planner-new"); if (n) n.addEventListener("click", createSession);
    const r = $("planner-reload"); if (r) r.addEventListener("click", () => { loadSessions(); refresh(); });
    // tools menu hook (if present)
    const tm = $("tm-planner"); if (tm) tm.addEventListener("click", () => { try { closeToolsMenu && closeToolsMenu(); } catch {} open(); });
  }

  window.initPlanner = initPlanner;
  window.plannerOpen = open;
  // ★工作流头/左栏「主控会话」用：在**当前画面**上开策划面板（.wp 覆盖层），不关协作页、不跳会话。
  //   tab="chat" 时直接落到「对话」页（主控聊天）。
  window.plannerOpenSession = (sid, tab) => {
    P.tab = tab ? String(tab) : "plan";
    P.chat = null;
    return open(sid);
  };
  window.plannerSendFromComposer = sendFromComposer; // ★主控会话的输入框：发到绑定聊天会话
  window.plannerClose = close;
  window.plannerToggle = toggle;
})();
