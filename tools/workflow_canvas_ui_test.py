# -*- coding: utf-8 -*-
r"""无头回归：★本地协作「工作流画布」（0.2.11）。

真跑 `frontend/app.js`，把 `/orchestrate/*` 换成内存假数据，断言：

  1. 打开工作台 → **默认选中工作流**（不是直接跳运行详情）；左栏按项目分组
  2. 画布：任务节点 + 成员节点 + 连线（reviewer 有虚线）
  3. 双击/单击成员 → 检查器能改角色 / 名字 / 大脑（改完自动保存）
  4. 拖动节点 → 坐标变化并**保存**（防抖后发 workflows/save）
  5. 画布「＋ 成员」→ 成员 +1、节点 +1
  6. 快速任务条 ▶ / Enter → POST /orchestrate/start 带 `workflowId`，并切到运行详情
  7. 运行详情「← 工作流」→ 回到画布
  8. 左栏工作流 ▶ → 用默认任务一键跑
  9. 新建工作流 → workflows/save，列表 +1 并选中
  10. 全程零 JS 报错

跑法：python tools\workflow_canvas_ui_test.py
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_wfcanvas.html")
URL = "http://127.0.0.1:8787/_probe_wfcanvas.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/engine/", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/music/",
              "/ccagent/", "/snapshot/", "/quarantine/", "/themes", "/pick/"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  function now() { return Date.now(); }
  window.__calls = [];
  function call(kind, body) { window.__calls.push([kind, body || {}]); }

  window.CATALOG = {
    agents: [{ id: "mock-a", label: "Mock 实现", available: true },
             { id: "mock-b", label: "Mock 审查", available: true }],
    roles: { implementer: { label: "实现者" }, reviewer: { label: "审查者" },
             tester: { label: "测试者" }, researcher: { label: "调研者" } },
    presets: [], askTimeout: 180, runTimeout: 600
  };
  window.WFS = [{
    id: "wf_1", name: "登录重构", cwd: "D:/ws", taskTemplate: "把登录拆开",
    members: [{ id: "m1", role: "implementer", name: "impl", engine: "mock-a" },
              { id: "m2", role: "reviewer", name: "rev", engine: "mock-b" }],
    layout: { zoom: 1, nodes: { task: { x: 40, y: 120 },
                                m1: { x: 360, y: 30 }, m2: { x: 760, y: 30 } } },
    runCount: 1, lastRunId: "run_1", created: now() - 60000, updated: now() - 1000
  }];
  window.RUNS = [{
    id: "run_1", title: "登录重构", task: "把登录拆开", cwd: "D:/ws",
    workflowId: "wf_1", workflowName: "登录重构", status: "done",
    created: now() - 40000, updated: now() - 1000,
    members: 2, done: 2, failed: 0, running: 0
  }];
  function M(id, name, role, roleLabel, engine, status, output) {
    return { id: id, name: name, role: role, roleLabel: roleLabel, engine: engine,
             status: status, output: output || "完成", error: "", created: now() - 30000,
             started: now() - 29000, ended: now() - 1000,
             tokens: { input: 100, output: 20 }, context: { used: 100, size: 100000 },
             activities: [], kind: "member", sessionID: "sess_" + id, cancelled: false };
  }
  window.RUN = {
    id: "run_1", title: "登录重构", task: "把登录拆开", cwd: "D:/ws",
    workflowId: "wf_1", workflowName: "登录重构", status: "done",
    created: now() - 40000, updated: now() - 1000,
    members: [M("m1", "impl", "implementer", "实现者", "mock-a", "done"),
              M("m2", "rev", "reviewer", "审查者", "mock-b", "done")],
    permissions: [], arbitration: null, error: ""
  };
  window.RUN_NEW = JSON.parse(JSON.stringify(window.RUN));
  window.RUN_NEW.id = "run_new";
  window.RUN_NEW.status = "running";
  window.RUN_NEW.members[0].status = "running";
  window.RUN_NEW.members[1].status = "wait";

  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var qs = qi >= 0 ? s.slice(qi + 1) : "";
    var method = String((o && o.method) || "GET").toUpperCase();
    var body = {};
    try { body = JSON.parse((o && o.body) || "{}"); } catch (e) {}

    if (path === "/orchestrate/catalog") return J(window.CATALOG);
    if (path === "/orchestrate/runs") return J({ ok: true, runs: window.RUNS });
    if (path === "/orchestrate/workflows" && method === "GET") {
      return J({ ok: true, workflows: window.WFS });
    }
    if (path === "/orchestrate/workflows/save" && method === "POST") {
      call("save", body);
      var wf = null;
      for (var i = 0; i < window.WFS.length; i++) {
        if (window.WFS[i].id === body.id) { wf = window.WFS[i]; break; }
      }
      if (!wf) {
        wf = { id: "wf_new_" + window.WFS.length, runCount: 0, lastRunId: "",
               created: now(), updated: now() };
        window.WFS.unshift(wf);
      }
      Object.assign(wf, {
        name: body.name || wf.name || "未命名工作流", cwd: body.cwd || "",
        taskTemplate: body.taskTemplate || "", members: body.members || [],
        layout: body.layout || { zoom: 1, nodes: {} }, updated: now()
      });
      return J({ ok: true, workflow: wf });
    }
    if (path === "/orchestrate/workflows/duplicate" && method === "POST") {
      call("duplicate", body);
      var src = window.WFS[0];
      var cp = JSON.parse(JSON.stringify(src));
      cp.id = "wf_dup"; cp.name = (src.name || "") + " 副本";
      window.WFS.unshift(cp);
      return J({ ok: true, workflow: cp });
    }
    if (path === "/orchestrate/workflows/delete" && method === "POST") {
      call("wfdel", body);
      window.WFS = window.WFS.filter(function (w) { return w.id !== body.id; });
      return J({ ok: true });
    }
    if (path === "/orchestrate/start" && method === "POST") {
      call("start", body);
      return J({ ok: true, run: window.RUN_NEW });
    }
    if (path === "/orchestrate/run") {
      var mm = /(?:^|&)id=([^&]+)/.exec(qs);
      var id = mm ? decodeURIComponent(mm[1]) : "";
      return J({ ok: true, run: id === "run_new" ? window.RUN_NEW : window.RUN });
    }
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) return J({ data: [], cursor: null });
    if (/^\/api\/session\/[^/]+$/.test(path)) {
      return J({ data: { id: "s1", title: "t", location: { directory: "D:/ws" },
                         model: { id: "m", providerID: "p" } } });
    }
    if (path === "/api/session") return J({ data: [], cursor: null });
    if (path === "/api/model") return J({ data: [] });
    if (path === "/engine/status") return J({ active: "acp", available: [], engines: [] });
    for (var d = 0; d < DEAD.length; d++) {
      if (s.indexOf(DEAD[d]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path.indexOf("/api/") === 0) return J({ data: [], items: [], total: 0, ok: true });
    return raw.apply(this, arguments);
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
function __t(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __vis(el) { return !!el && !el.hidden && el.getBoundingClientRect().width > 0; }
function __node(id) { return document.querySelector('#collab-cv-nodes [data-node="' + id + '"]'); }
(async function () {
  var out = { steps: [], info: {} };
  function step(n, ok, extra) { out.steps.push([n, !!ok, extra == null ? "" : String(extra).slice(0, 300)]); }
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (typeof collabOpen === "function") break; }
    await __s(400);

    /* 1. 打开：默认工作流视图 */
    document.getElementById("btn-collab").click();
    await __s(1000);
    step("★打开工作台默认选中工作流（不是直接跳运行）",
         __vis(document.getElementById("collab-wf")) && !__vis(document.getElementById("collab-run")),
         "wf=" + __vis(document.getElementById("collab-wf")) + " run=" + __vis(document.getElementById("collab-run")));
    var wfRows = document.querySelectorAll("#collab-wf-list [data-wf]");
    step("★左栏工作流按项目分组", wfRows.length === 1
         && /ws/.test(__t(document.getElementById("collab-wf-list"))), __t(document.getElementById("collab-wf-list")));

    /* 2. 画布节点 / 连线 */
    var nodes = document.querySelectorAll("#collab-cv-nodes [data-node]");
    var edges = document.querySelectorAll("#collab-cv-edges path");
    out.info.nodes = nodes.length; out.info.edges = edges.length;
    step("★画布：任务 + 2 个成员节点", nodes.length === 3, "n=" + nodes.length);
    step("★连线：任务→成员 + 审查者虚线", edges.length >= 3
         && document.querySelectorAll("#collab-cv-edges path.dash").length >= 1,
         "edges=" + edges.length + " dash=" + document.querySelectorAll("#collab-cv-edges path.dash").length);

    /* 3. 检查器 */
    var m1 = __node("m1");
    m1.click();
    await __s(250);
    var insp = document.getElementById("collab-inspector");
    step("★点成员 → 检查器（角色 / 名字 / 大脑）",
         __vis(insp) && !!insp.querySelector("[data-insp-role]")
         && !!insp.querySelector("[data-insp-name]") && !!insp.querySelector("[data-insp-engine]"),
         __t(insp));
    var roleSel = insp.querySelector("[data-insp-role]");
    roleSel.value = "tester";
    roleSel.dispatchEvent(new Event("change", { bubbles: true }));
    await __s(200);
    step("★改角色 → 工作流成员跟着变", COLLAB.workflows[0].members[0].role === "tester",
         COLLAB.workflows[0].members[0].role);
    roleSel = document.getElementById("collab-inspector").querySelector("[data-insp-role]");
    roleSel.value = "implementer";
    roleSel.dispatchEvent(new Event("change", { bubbles: true }));
    await __s(200);

    /* 4. 拖动节点 → 保存布局 */
    window.__calls.length = 0;
    m1 = __node("m1");                 // ★上面的检查器改动会重绘画布，旧引用已脱离 DOM
    var r = m1.getBoundingClientRect();
    var sx = r.left + 30, sy = r.top + 20;
    m1.dispatchEvent(new PointerEvent("pointerdown", { bubbles: true, clientX: sx, clientY: sy, button: 0 }));
    window.dispatchEvent(new PointerEvent("pointermove", { bubbles: true, clientX: sx + 70, clientY: sy + 45 }));
    window.dispatchEvent(new PointerEvent("pointerup", { bubbles: true, clientX: sx + 70, clientY: sy + 45 }));
    await __s(1000);                    // 保存防抖 600ms
    var saves = (window.__calls || []).filter(function (c) { return c[0] === "save"; });
    var lastSave = saves[saves.length - 1] || [];
    var pos = lastSave[1] && lastSave[1].layout && lastSave[1].layout.nodes
      && lastSave[1].layout.nodes.m1;
    out.info.savePos = pos;
    step("★拖节点后坐标变了并自动保存", !!pos && pos.x > 380, JSON.stringify(pos));

    /* 5. 加成员 */
    var before = (COLLAB.workflows[0].members || []).length;
    document.getElementById("collab-wf-add").click();
    await __s(300);
    step("★画布「＋ 成员」→ 成员 +1、节点 +1",
         (COLLAB.workflows[0].members || []).length === before + 1
         && document.querySelectorAll("#collab-cv-nodes [data-node]").length === before + 2,
         "members=" + (COLLAB.workflows[0].members || []).length);

    /* 6. 快速任务 → 运行 */
    window.__calls.length = 0;
    document.getElementById("collab-quick-task").value = "补充一条任务";
    document.getElementById("collab-quick-run").click();
    await __s(900);
    var start = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    out.info.start = start[1];
    step("★快速任务 ▶ → POST /start 带 workflowId + 正文",
         start[1] && start[1].workflowId === "wf_1" && start[1].task === "补充一条任务",
         JSON.stringify(start[1]));
    step("★开跑后切到运行详情", __vis(document.getElementById("collab-run")),
         "run=" + __vis(document.getElementById("collab-run")));

    /* 7. 返回画布 */
    var back = document.querySelector("[data-collab-back-wf]");
    step("★运行详情有「← 工作流」", !!back);
    if (back) back.click();
    await __s(500);
    step("★点「← 工作流」回到画布", __vis(document.getElementById("collab-wf"))
         && !__vis(document.getElementById("collab-run")));

    /* 8. 左栏 ▶ 一键跑（用默认任务） */
    window.__calls.length = 0;
    var play = document.querySelector('#collab-wf-list [data-wf-run="wf_1"]');
    if (play) play.click();
    await __s(800);
    var start2 = (window.__calls || []).filter(function (c) { return c[0] === "start"; })[0] || [];
    step("★左栏工作流 ▶ 用默认任务开跑",
         start2[1] && start2[1].workflowId === "wf_1" && start2[1].task === "把登录拆开",
         JSON.stringify(start2[1]));
    await __s(300);
    var back2 = document.querySelector("[data-collab-back-wf]");
    if (back2) back2.click();
    await __s(400);

    /* 9. 新建工作流 */
    window.__calls.length = 0;
    document.getElementById("collab-new-wf").click();
    await __s(700);
    var saves2 = (window.__calls || []).filter(function (c) { return c[0] === "save"; });
    step("★新建工作流 → workflows/save，列表 +1 并选中",
         saves2.length >= 1 && (COLLAB.workflows || []).length === 2
         && String(COLLAB.wfId) === String(window.WFS[0].id),
         "n=" + (COLLAB.workflows || []).length + " id=" + COLLAB.wfId);
  } catch (err) {
    out.crash = String(err && err.stack ? err.stack : err);
  }
  out.jsErrors = window.__probeErr;
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""


SHOT_JS = r"""<style>
  #collab, #collab * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 240; i++) { await __sh(25); if (typeof collabOpen === "function") break; }
  await __sh(400);
  document.getElementById("btn-collab").click();
  await __sh(1400);
})();
</script>
"""

SHOT_INSP_JS = SHOT_JS.replace(
    '  await __sh(1400);\n})();',
    '  await __sh(1400);\n'
    '  var n = document.querySelector(\'#collab-cv-nodes [data-node="m1"]\');\n'
    '  if (n) n.click();\n'
    '  await __sh(500);\n})();')


def build_probe(mode: str = "") -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    inject = PROBE_JS if not mode else (SHOT_INSP_JS if mode == "insp" else SHOT_JS)
    out = src.replace('<script src="/app.js', STUB + inject + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_wfcanvas_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probewfcanvasshot"),
             "--window-size=1500,980", "--virtual-time-budget=30000",
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
    dom = os.path.join(tmp, "probe_wfcanvas_dom.html")
    err = os.path.join(tmp, "probe_wfcanvas_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probewfcanvas"),
             "--window-size=1500,960",
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
    if "--shot" in sys.argv or "--shot-insp" in sys.argv:
        insp = "--shot-insp" in sys.argv
        flag = "--shot-insp" if insp else "--shot"
        i = sys.argv.index(flag)
        default_name = "workflow_canvas_inspector.png" if insp else "workflow_canvas.png"
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", default_name)
        build_probe("insp" if insp else "shot")
        try:
            take_shot(edge, os.path.abspath(shot))
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        return 0
    build_probe()
    print("探针页已生成，正在无头渲染（真跑 app.js）…")
    try:
        html = render(edge)
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)
        print("探针页已清理")

    m = re.search(r'<pre id="__probe__">(.*?)</pre>', html, re.S)
    if not m:
        print("[BAD] 没拿到探针结果（渲染失败）。DOM 前 300 字符：")
        print(html[:300])
        return 1
    d = json.loads(m.group(1))

    print("\n=== 工作流画布 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name
              + (("   [" + extra[:240] + "]") if extra else ""))
        if not okv:
            fails += 1
    if d.get("crash"):
        print("\n  [BAD] 探针崩了：\n" + d["crash"])
        fails += 1
    errs = d.get("jsErrors") or []
    if errs:
        print("\n  JS 报错:", errs)
        fails += 1
    print("\n  total %d, failed %d" % (len(d.get("steps", [])), fails))
    print("  result:", "PASS" if fails == 0 else "FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
