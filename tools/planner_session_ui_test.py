# -*- coding: utf-8 -*-
r"""无头回归：★主控（策划）会话的「会话体验」。

覆盖用户实测的三件事：
  1. 点开主控会话后，输入框**可用**（不是灰的），提示语说明会发到绑定聊天会话；
  2. 输入一句需求 → 自动创建/复用绑定聊天会话，并把这句话发过去（切到聊天会话）；
  3. 从主控会话能切回普通会话，输入框恢复；切引擎不会又被拉回主控（planner:… 不参与接续）。

跑法（需要 8787 在跑，只用它发静态文件）：
    python tools\planner_session_ui_test.py
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_planner_ui.html")
URL = "http://127.0.0.1:8787/_probe_planner_ui.html"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/appearance", "/live/",
              "/qq/", "/pty", "/fs/", "/music/", "/ccagent/", "/snapshot/", "/quarantine/",
              "/themes", "/pick/", "/update/", "/skills/", "/projectroot"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  function now() { return Date.now(); }
  window.__calls = [];
  window.__prompt = null;
  window.__boundChat = "";
  function call(kind, body) { window.__calls.push([kind, body || {}]); }

  window.S1 = { id: "s1", title: "普通会话", agentSessionId: "s1",
                location: { directory: "D:/ws" }, model: { id: "deepseek-flash", providerID: "deepseek" },
                time: { created: now() - 9000, updated: now() - 1000 } };
  window.MSG1 = [{ id: "m1", type: "assistant", model: { id: "deepseek-flash", providerID: "deepseek" },
                   time: { created: now() - 8000, completed: now() - 7000 },
                   content: [{ type: "text", text: "普通回答" }] }];
  window.PLANNERS = [{ id: "pls_1", title: "主控会话", cwd: "D:/ws", status: "draft",
                       updatedAt: Math.floor(now() / 1000) }];
  window.PLAN = { id: "plan_1", version: 1, status: "draft", goal: "把登录拆开",
                  stages: [{ id: "s1", name: "实现", objective: "改代码", when: "always",
                             failPolicy: "stop", retries: 0,
                             members: [{ id: "m1", role: "implementer", name: "impl" }] }] };
  window.CHAT = { id: "chat_1", title: "策划 · 主控会话", agentSessionId: "chat_1",
                  location: { directory: "D:/ws" }, model: { id: "deepseek-flash", providerID: "deepseek" },
                  time: { created: now() - 500, updated: now() - 500 } };

  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = String((o && o.method) || "GET").toUpperCase();
    var body = {};
    try { body = JSON.parse((o && o.body) || "{}"); } catch (e) {}

    if (path === "/healthz") return J({ ok: true, engine: "acp", ui: "" });
    if (path === "/api/session" && method === "GET") return J({ data: [window.S1], cursor: null });
    if (path === "/api/session" && method === "POST") {
      call("createChat", body);
      return J({ data: window.CHAT });
    }
    if (path === "/api/session/s1" && method === "GET") return J({ data: window.S1 });
    if (path === "/api/session/chat_1" && method === "GET") return J({ data: window.CHAT });
    if (path === "/api/session/s1/message") return J({ data: window.MSG1, cursor: null });
    if (path === "/api/session/chat_1/message") return J({ data: [], cursor: null });
    if (path === "/api/session/chat_1/prompt" && method === "POST") {
      window.__prompt = body; call("promptChat", body);
      return J({ ok: true });
    }
    if (path === "/api/session/s1/prompt" && method === "POST") {
      window.__prompt = body; call("promptS1", body);
      return J({ ok: true });
    }
    if (path === "/planner/sessions" && method === "GET") {
      return J({ ok: true, sessions: window.PLANNERS });
    }
    if (path === "/planner/session" && method === "POST") {
      call("createPlanner", body);
      var np = { id: "pls_1", title: body.title || "主控", cwd: body.cwd || "", status: "draft" };
      window.PLANNERS = [np];
      return J({ ok: true, session: np });
    }
    if (path === "/planner/session" && method === "GET") {
      return J({ ok: true, session: { id: "pls_1", title: "主控会话", cwd: "D:/ws", status: "draft" } });
    }
    if (path === "/planner/status" && method === "GET") {
      return J({ ok: true,
                 session: { id: "pls_1", title: "主控会话", cwd: "D:/ws", status: "draft",
                            chatSessionId: window.__boundChat || "" },
                 plan: window.PLAN, validation: { ok: true, errors: [], warnings: [] }, run: {} });
    }
    if (path === "/planner/plan" && method === "GET") {
      return J({ ok: true, plan: window.PLAN, validation: { ok: true, errors: [], warnings: [] } });
    }
    if (path === "/planner/plan/diff" && method === "POST") {
      call("planDiff", body);
      return J({ ok: true, hasChanges: false, changes: [], summary: "",
                 validation: { ok: true, errors: [], warnings: [] } });
    }
    if (path === "/planner/plan" && method === "POST") {
      call("savePlan", body);
      return J({ ok: true, plan: window.PLAN, validation: { ok: true, errors: [], warnings: [] } });
    }
    if (path === "/planner/run/start" && method === "POST") {
      call("startRun", body);
      window.RUN_STARTED = { id: "run_1", status: "running", members: [], workflowId: "wf_1" };
      return J({ ok: true, run: window.RUN_STARTED, plan: window.PLAN, workflowId: "wf_1" });
    }
    if (path === "/planner/run/cancel" && method === "POST") {
      call("cancelRun", body);
      return J({ ok: true });
    }
    if (path === "/planner/run/retry" && method === "POST") {
      call("retry", body);
      return J({ ok: true, mode: body.mode || "retry", runId: "run_1", plan: window.PLAN, message: "已重新开始" });
    }
    if (path === "/planner/isolation/list" && method === "GET") {
      call("isoList", {});
      return J({ ok: true, members: [{ id: "m1", name: "impl", hasChanges: true }] });
    }
    if (path === "/planner/isolation/cleanup" && method === "POST") {
      call("isoCleanup", body);
      return J({ ok: true });
    }
    if (path === "/planner/isolation/apply" && method === "POST") {
      call("isoApply", body);
      return J({ ok: true });
    }
    if (path === "/planner/handoff" && method === "POST") {
      call("handoff", body);
      return J({ ok: true, path: "HANDOFF.md" });
    }
    if (path === "/planner/handoff" && method === "GET") {
      call("handoffShow", {});
      return J({ ok: true, markdown: "# HANDOFF" });
    }
    if (path === "/planner/switch-agent" && method === "POST") {
      call("switch", body);
      return J({ ok: true, fromAgent: "a", toAgent: body.agent || "b",
                 review: { findings: [], warnings: [] } });
    }
    if (path === "/planner/session" && method === "DELETE") {
      call("plannerDel", {});
      return J({ ok: true });
    }
    if (path === "/planner/report" && method === "GET") {
      call("report", {});
      return J({ ok: true, report: { markdown: "# 报告" } });
    }
    if (path === "/planner/approvals" && method === "GET") return J({ ok: true, approvals: [] });
    if (path === "/planner/resume" && method === "GET") {
      call("resume", {});
      return J({ ok: true, context: { text: "CTX-PLAN" } });
    }
    if (path === "/planner/bind" && method === "POST") {
      call("bind", body); window.__boundChat = body.chatSessionId || "chat_1"; return J({ ok: true });
    }
    if (path === "/engine" && method === "POST") { call("engine", body); return J({ ok: true }); }
    if (path === "/engine/status") return J({ active: "acp", available: [], engines: [] });
    if (path === "/api/model") return J({ data: [] });
    if (path === "/api/agent") return J({ data: [] });
    if (path === "/api/skill" || path === "/api/command") return J({ data: [] });
    for (var d = 0; d < DEAD.length; d++) {
      if (s.indexOf(DEAD[d]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
    // ★探针里绝不落到真实网络：任何没列出来的请求都给一个空 JSON（否则虚拟时间会卡在长请求上）
    return J({ ok: true, data: [], items: [], total: 0, sessions: [], workflows: [], runs: [] });
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () { this.readyState = 1; this.send = function () {}; this.close = function () {}; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __q(sel) { return document.querySelector(sel); }
function __txt(el) { return el ? String(el.textContent || "").replace(/\s+/g, " ").trim() : ""; }
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 240)]); }
  function mark(s) { window.__probeStep = s; }
  try {
    mark("boot-wait");
    for (var i = 0; i < 240; i++) { await __s(25); if (typeof state !== "undefined" && typeof openSession === "function") break; }
    await __s(500);
    mark("boot-done");

    var plRow = __q('.session[data-id="planner:pls_1"]');
    var s1Row = __q('.session[data-id="s1"]');
    step("★聊天列表不再混入主控会话（只在工作流面板）", !plRow && !!s1Row,
         "pl=" + !!plRow + " s1=" + !!s1Row);

    /* 1. 主控会话：输入框可用（从工作流面板进来 = openSession("planner:…")） */
    mark("click-planner");
    await openSession("planner:pls_1");
    await __s(700);
    mark("planner-opened");
    var inp = document.getElementById("input");
    step("★主控会话：输入框不是灰的", !!(inp && !inp.disabled), inp ? ("disabled=" + inp.disabled) : "no input");
    step("★提示语说明会发到绑定聊天", !!inp && /主控/.test(inp.placeholder || ""), inp ? inp.placeholder : "");
    step("★主控视图渲染（计划/报告卡）", !!__q("#messages .pl-rich"), __txt(__q("#messages")).slice(0, 80));

    /* 2. 输入一句 → 自动绑定聊天并发过去 */
    mark("send");
    inp.value = "给我一个计划";
    document.getElementById("btn-send").click();
    await __s(1200);
    mark("sent");
    step("★发送后切到绑定聊天会话", !!state.current && state.current.id === "chat_1",
         state.current ? state.current.id : "none");
    step("★这句话真的发到了聊天会话", !!(window.__prompt && /给我一个计划/.test(String(window.__prompt.text || ""))),
         JSON.stringify(window.__prompt || {}).slice(0, 160));
    step("★新聊天带「接棒上下文」", !!(window.__prompt && /CTX-PLAN/.test(String(window.__prompt.text || ""))),
         JSON.stringify(window.__prompt || {}).slice(0, 160));
    step("★切到聊天后输入框可用", !inp.disabled, "disabled=" + inp.disabled);

    /* 3. 从主控切回普通会话 */
    mark("click-s1");
    s1Row = __q('.session[data-id="s1"]');
    if (s1Row) s1Row.click();
    await __s(900);
    mark("s1-opened");
    step("★从主控能切回普通会话", !!state.current && state.current.id === "s1",
         state.current ? state.current.id : "none");
    step("★普通会话输入框可用、提示语恢复",
         !inp.disabled && /写在这里/.test(inp.placeholder || ""), inp.placeholder || "");

    /* 4. 切引擎：不把主控会话当成可接续的 agent 会话 */
    mark("open-planner-again");
    await openSession("planner:pls_1");
    await __s(400);
    mark("engine-start");
    await setEngine("opencode");
    mark("engine-done");
    await __s(900);
    step("★切引擎后不会又被拉回主控会话",
         !!state.current && state.current.id !== "planner:pls_1" && state.current._kind !== "planner",
         state.current ? state.current.id : "none");
    step("★切引擎后打开的是一个普通会话", !!state.current && state.current.id === "s1",
         state.current ? state.current.id : "none");

    /* 5. ★主控面板（工作流同屏弹层）：计划/对话页签 + 按钮都要能用 */
    window.alert = function () { return true; };
    window.confirm = function () { return true; };
    window.prompt = function (q) {
      if (/模式/.test(String(q || ""))) return "retry";
      if (/操作/.test(String(q || ""))) return "list";
      return "opencode-acp";
    };
    window.open = function () { return null; };
    await plannerOpenSession("pls_1", "plan");
    await __s(700);
    var plBox = document.getElementById("planner");
    step("★主控面板在弹层里打开（不跳会话）", !!plBox && !plBox.hidden,
         "hidden=" + (plBox ? plBox.hidden : "null"));
    step("★面板有 计划 / 对话 两个页签",
         !!(document.getElementById("pl-tab-plan") && document.getElementById("pl-tab-chat")));
    var ids = ["pl-diff", "pl-save", "pl-confirm", "pl-run", "pl-cancel", "pl-report",
               "pl-retry", "pl-isolation",
               "pl-handoff", "pl-handoff-show", "pl-switch", "pl-resume",
               "pl-open-chat", "pl-refresh", "pl-del"];
    step("★面板 15 个按钮都在", ids.every(function (id) { return !!document.getElementById(id); }),
         ids.filter(function (id) { return !document.getElementById(id); }).join(","));
    var plMore = document.getElementById("pl-more");
    var primaryBtns = document.querySelectorAll(".pl-actions button");
    step("★常用操作只露 5 个（其余收进「更多操作」）",
         primaryBtns.length === 5 && !!plMore && !plMore.open,
         "primary=" + primaryBtns.length + " more=" + (plMore ? plMore.open : "null"));
    if (plMore) plMore.querySelector("summary").click();
    await __s(120);
    step("★展开「更多操作」", !!plMore && plMore.open);
    step("★没在运行时「取消运行」是禁用（不乱发请求）", !!document.getElementById("pl-cancel")
         && document.getElementById("pl-cancel").disabled);

    window.__calls.length = 0;
    document.getElementById("pl-diff").click(); await __s(120);
    document.getElementById("pl-save").click(); await __s(220);
    document.getElementById("pl-confirm").click(); await __s(260);
    document.getElementById("pl-run").click(); await __s(260);
    document.getElementById("pl-report").click(); await __s(120);
    document.getElementById("pl-retry").click(); await __s(220);
    document.getElementById("pl-isolation").click(); await __s(180);
    document.getElementById("pl-handoff").click(); await __s(220);
    document.getElementById("pl-handoff-show").click(); await __s(120);
    document.getElementById("pl-switch").click(); await __s(260);
    document.getElementById("pl-resume").click(); await __s(120);
    document.getElementById("pl-refresh").click(); await __s(220);
    var cn = (window.__calls || []).map(function (c) { return c[0]; });
    function has(k) { return cn.indexOf(k) >= 0; }
    step("★按钮都发出了对应请求（diff/save/confirm/run/report/retry/iso/handoff/switch/resume）",
         has("planDiff") && has("savePlan") && has("startRun") && has("report")
         && has("retry") && has("isoList")
         && has("handoff") && has("handoffShow") && has("switch") && has("resume"),
         cn.join(","));

    /* 5b. 对话页签：面板内直接输入、发到绑定聊天 */
    document.getElementById("pl-tab-chat").click();
    await __s(600);
    var ci = document.getElementById("pl-chat-input");
    step("★对话页签有输入框", !!ci, ci ? "ok" : "missing");
    if (ci) {
      ci.value = "面板里发一句";
      document.getElementById("pl-chat-sendbtn").click();
      await __s(900);
      step("★对话页发送到绑定聊天", !!(window.__prompt && /面板里发一句/.test(String(window.__prompt.text || ""))),
           JSON.stringify(window.__prompt || {}).slice(0, 120));
    }

    /* 5c. 删除会话（最后做；确认删掉） */
    document.getElementById("pl-tab-plan").click();
    await __s(150);
    window.__calls.length = 0;
    document.getElementById("pl-del").click();
    await __s(350);
    step("★删除会话发出 DELETE", (window.__calls || []).some(function (c) { return c[0] === "plannerDel"; }),
         (window.__calls || []).map(function (c) { return c[0]; }).join(","));
  } catch (err) {
    out.crash = String(err && err.stack ? err.stack : err);
  }
  mark("done");
  out.jsErrors = window.__probeErr;
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""


SHOT_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 240; i++) { await __s(25); if (typeof state !== "undefined" && typeof plannerOpenSession === "function") break; }
  await __s(400);
  await plannerOpenSession("pls_1", "plan");
  await __s(900);
})();
</script>
"""


def build_probe(shot: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    inject = SHOT_JS if shot else PROBE_JS
    out = src.replace('<script src="/app.js', STUB + inject + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_planner_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeplannershot"),
             "--window-size=1400,980", "--virtual-time-budget=30000",
             "--screenshot=" + path, URL],
            stdout=subprocess.DEVNULL, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  截图:", path, "存在=", os.path.isfile(path))


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str) -> str:
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_planner_ui_dom.html")
    err = os.path.join(tmp, "probe_planner_ui_err.txt")
    for f in (dom, err):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeplannerui"),
             "--window-size=1400,920",
             "--virtual-time-budget=30000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", "planner_panel.png")
        build_probe(shot=True)
        try:
            take_shot(edge, os.path.abspath(shot))
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        return 0
    build_probe()
    try:
        dom = render(edge)
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)
    m = re.search(r'<pre id="__probe__">(.*?)</pre>', dom, re.S)
    if not m:
        print("[BAD] 探针没有返回结果（页面可能没跑起来）")
        return 1
    data = json.loads(m.group(1))
    print("\n=== 主控会话 · 无头回归 ===")
    if data.get("crash"):
        print("  CRASH:", data["crash"])
    failed = 0
    for name, ok, extra in data.get("steps", []):
        print(("  OK   " if ok else "  BAD  ") + name + (("  [" + extra + "]") if extra else ""))
        if not ok:
            failed += 1
    errs = data.get("jsErrors") or []
    print("\n  JS 报错:", errs)
    print("\n  total %d, failed %d" % (len(data.get("steps", [])), failed))
    print("  result:", "PASS" if (failed == 0 and not data.get("crash") and not errs) else "FAIL")
    return 0 if (failed == 0 and not data.get("crash") and not errs) else 1


if __name__ == "__main__":
    sys.exit(main())
