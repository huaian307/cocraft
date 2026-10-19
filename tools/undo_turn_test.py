# -*- coding: utf-8 -*-
r"""无头回归：★可逆执行「撤销这一轮」（`ui 0.1.77`，2026-10-16）。

背景：`backend/snapshots.py` 的底座与 `/snapshot/*` 路由在 `0.1.76` 就绪；这一步补**两处接线**：
  ① 面板**每发一条消息、在 prompt 之前**给会话工作目录落一次快照（`send()` → `snapTurnStart()`）；
  ② 工具菜单里一个「撤销这一轮」入口（读到这一轮改了哪些文件 → 点开看行级 diff → 一键退回）。

用 `frontend/index.html` 生成探针页，真跑 `app.js`，把 `/snapshot/*` 换成内存假数据，断言：

    1. ★「每轮快照」**默认关着**（只留破坏性隔离区）→ 发消息**不落快照**、但 prompt 照发
    2. 打开开关后：★**先落快照再发 prompt**（顺序不能反），带 scope=会话 id / label=turn / content=true
    3. ★**快照失败绝不能挡住发消息**（take 回 500，prompt 照样发出、页面不报"发送失败"）
    4. 工具菜单里有「撤销这一轮」入口
    5. 打开 → 列出「改 / 增 / 删 / 改名」四类，各带路径
    6. 点一行 → 拉 `/snapshot/file` → 画出**行级 diff**（红绿行）
    7. 「撤销这一轮」→ 先确认 → `POST /snapshot/undo`（带 scope + ts）→ 横幅回报
    8. **还没有快照**时 → 明说「还没有快照」且按钮禁用（不假装能撤）
    9. **会话没有工作目录**时 → 说明原因（不空转）
   10. Esc 关掉弹窗
   11. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\undo_turn_test.py
    python tools\undo_turn_test.py --shot resources\screenshots\undo_turn.png
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
PROBE = os.path.join(WEB, "_probe_undo.html")
URL = "http://127.0.0.1:8787/_probe_undo.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try {
    localStorage.setItem("cocraft.setup.v1", "1");
    localStorage.setItem("opencode-ui.setup.v1", "1");
  } catch (e) {}

  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event",
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/fs/", "/ccagent",
              "/quarantine"];
  var raw = window.fetch;
  var T = 1760000000000;
  window.__calls = [];
  window.__takeFail = false;
  window.__snapMode = "full";          // full | empty

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var SNAP_TS = "20261016-120000-001";
  var DIFF = {
    added: [{ path: "notes/new.md", size: 12 }],
    deleted: [{ path: "notes/gone.txt", size: 20 }],
    changed: [{ path: "src/app.js", size: 120, size_before: 90, binary: false }],
    renamed: [{ from: "old-name.txt", to: "new-name.txt" }],
    stats: { added: 1, deleted: 1, changed: 1, renamed: 1,
             base_at: T / 1000, cur_at: T / 1000 + 120 },
  };
  var PATCH_LINES = [
    "--- a/src/app.js", "+++ b/src/app.js", "@@ -1,2 +1,3 @@",
    " const a = 1;", "+const b = 2;", "-const c = 3;",
  ];

  var SESS = { id: "ses_probe", title: "撤销探针",
               time: { created: T, updated: T },
               location: { directory: "D:\\\\proj" },
               model: { id: "probe-1", providerID: "acp" } };

  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ location: { directory: "D:\\\\proj" }, data: [] });
    if (path === "/api/model/default") return J({ data: null });

    if (path === "/snapshot/take") {
      var b = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["take", b.scope, b.label, !!b.content]);
      if (window.__takeFail) {
        return Promise.resolve(new Response("{\"ok\":false}", { status: 500 }));
      }
      return J({ ok: true, data: { ts: SNAP_TS, scope: b.scope, root: b.root,
                                   count: 12, rehash: 0, stored: 1, label: b.label } });
    }
    if (path === "/snapshot/list") {
      var items = window.__snapMode === "empty" ? []
        : [{ ts: SNAP_TS, taken_at: T / 1000, root: "D:\\\\proj",
             label: "turn", count: 12, with_content: true }];
      return J({ ok: true, data: { items: items } });
    }
    if (path === "/snapshot/diff") return J({ ok: true, data: DIFF });
    if (path === "/snapshot/file") {
      var fq = new URLSearchParams(s.slice(qi + 1));
      window.__calls.push(["file", fq.get("path")]);
      return J({ ok: true, data: { ok: true, lines: PATCH_LINES, additions: 1, deletions: 1 } });
    }
    if (path === "/snapshot/undo") {
      var ub = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["undo", ub.scope, ub.ts]);
      return J({ ok: true, restored: 2, removed: 1, skipped: 0, actions: [] });
    }

    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    // ⚠ prompt 必须排在通用 /api/session/ses_probe 之前（否则被"会话详情"那条吃掉）
    if (/\/api\/session\/[^/]+\/prompt$/.test(path)) {
      window.__calls.push(["prompt"]);
      return J({ ok: true });
    }
    if (path === "/api/session/ses_probe/message") return J({ data: [], cursor: null });
    if (s.indexOf("/api/session/ses_probe") === 0) {
      if (/\/(model|agent|skill|command|permission|form)$/.test(path)) {
        return Promise.resolve(new Response("", { status: 204 }));
      }
      return J({ data: SESS });
    }
    if (path === "/api/session") return J({ data: [SESS], cursor: null });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () {
    this.close = function () {}; this.addEventListener = function () {};
  };
  window.WebSocket = function () {
    this.send = function () {}; this.close = function () {}; this.readyState = 1;
  };
  window.__confirmAnswer = true;      // 探针里别弹真模态（会卡死 --dump-dom）
  window.confirm = function () { return window.__confirmAnswer; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __kinds(kind) { return (window.__calls || []).filter(function (c) { return c[0] === kind; }); }
function __idx(kind) {
  var a = window.__calls || [];
  for (var i = 0; i < a.length; i++) { if (a[i][0] === kind) return i; }
  return -1;
}
window.__SESS = { id: "ses_probe", title: "撤销探针", time: { created: 1760000000000 },
                  location: { directory: "D:\\proj" }, model: { id: "probe-1", providerID: "acp" } };

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 200; i++) { await __s(25); if (window.state) break; }
    await __s(250);

    /* ---------- 1. ★默认关：只留破坏性隔离区，不落快照 ---------- */
    state.current = window.__SESS;
    SET.turnSnap = false;                    // 显式确认默认值
    out.info.defaultTurnSnap = SET.turnSnap;
    window.__calls.length = 0;
    document.getElementById("input").value = "先发一条（开关关着）";
    await send();
    await __s(250);
    step("★「每轮快照」**默认关着** → 发消息**不落快照**，但 prompt 照发（只留破坏性隔离区）",
         __kinds("take").length === 0 && __kinds("prompt").length === 1,
         JSON.stringify(window.__calls));

    /* ---------- 2. 打开开关：先落快照再发 prompt ---------- */
    SET.turnSnap = true;
    window.__calls.length = 0;
    document.getElementById("input").value = "把 app.js 改一下";
    await send();
    await __s(250);
    var tk = __kinds("take"), pr = __kinds("prompt");
    out.info.calls = window.__calls;
    step("★打开开关后：**先落快照再发 prompt**（顺序不能反）",
         tk.length === 1 && pr.length === 1 && __idx("take") < __idx("prompt"),
         JSON.stringify(window.__calls));
    step("★快照带 scope=会话 id / label=turn / content=true",
         tk.length === 1 && tk[0][1] === "ses_probe" && tk[0][2] === "turn" && tk[0][3] === true,
         JSON.stringify(tk));

    /* ---------- 2. 快照失败也不挡发消息 ---------- */
    window.__takeFail = true;
    window.__calls.length = 0;
    document.getElementById("input").value = "再改一处";
    await send();
    await __s(250);
    window.__takeFail = false;
    out.info.failCalls = window.__calls.slice();
    step("★**快照失败绝不挡住发消息**（take 500 → prompt 照样发出）",
         __kinds("prompt").length === 1 && __kinds("take").length === 1,
         JSON.stringify(window.__calls));
    step("★也没有给用户蹦「发送失败」",
         !/发送失败/.test(__txt(document.getElementById("messages"))), "");

    /* ---------- 3~5. 弹窗 ---------- */
    step("★工具菜单里有「撤销这一轮」入口", !!document.getElementById("tm-undo"));
    await undoOpen();
    await __s(300);
    var box = document.getElementById("undo");
    step("★弹窗打开", !!box && !box.hidden);

    var rows = document.querySelectorAll("#undo-list .u-row");
    out.info.rows = rows.length;
    step("★列出「改 / 增 / 删 / 改名」四类，各带路径", rows.length === 4, "rows=" + rows.length);
    step("★四种类型都有中文小标（改/增/删/名）",
         document.querySelectorAll("#undo-list .u-kind").length === 4
         && /src\/app\.js/.test(__txt(document.querySelector("#undo-list"))),
         __txt(document.querySelector("#undo-list")).slice(0, 160));
    step("★底部写清「改 N · 增 N · 删 N · 改名 N」+ 基准时间",
         /改 1/.test(__txt(document.getElementById("undo-foot")))
         && /增 1/.test(__txt(document.getElementById("undo-foot")))
         && /\d\d-\d\d \d\d:\d\d/.test(__txt(document.getElementById("undo-foot"))),
         __txt(document.getElementById("undo-foot")));

    document.querySelector('[data-upath="src/app.js"]').click();
    await __s(300);
    var dbox = document.querySelector("#undo-list .u-diff");
    out.info.diffHtml = dbox ? dbox.innerHTML.slice(0, 120) : "(没有)";
    step("★点一行 → 拉 /snapshot/file 并画出**行级 diff**（红绿行）",
         !!dbox && !dbox.hidden && !!dbox.querySelector(".tr-diff")
         && __kinds("file").length === 1 && __kinds("file")[0][1] === "src/app.js",
         out.info.diffHtml);

    /* ---------- 6. 撤销 ---------- */
    window.__calls.length = 0;
    document.getElementById("undo-run").click();
    await __s(400);
    var un = __kinds("undo");
    out.info.undo = un;
    step("★「撤销这一轮」→ POST /snapshot/undo（带 scope + ts）",
         un.length === 1 && un[0][1] === "ses_probe" && un[0][2] === "20261016-120000-001",
         JSON.stringify(un));
    step("★撤销后有横幅回报（恢复了几个 / 删掉几个新增）",
         /已撤销这一轮/.test(__txt(document.getElementById("banner"))),
         __txt(document.getElementById("banner")));

    /* ---------- 7. 还没有快照 ---------- */
    window.__snapMode = "empty";
    await undoLoad();
    await __s(200);
    step("★**还没有快照**时明说「还没有快照」且按钮禁用（不假装能撤）",
         /还没有快照/.test(__txt(document.getElementById("undo-list")))
         && document.getElementById("undo-run").disabled === true,
         __txt(document.getElementById("undo-list")) + " | disabled="
         + document.getElementById("undo-run").disabled);
    window.__snapMode = "full";
    SET.turnSnap = false;
    window.__snapMode = "empty";
    await undoLoad();
    await __s(200);
    step("★开关关着时，提示直接指向设置里那个开关（别让人干等）",
         /现在是关着的/.test(__txt(document.getElementById("undo-list"))),
         __txt(document.getElementById("undo-list")));
    SET.turnSnap = true;

    /* ---------- 8. 会话没有工作目录 ---------- */
    var keep = state.current;
    state.current = { id: "ses_probe", title: "x", location: {} };
    await undoLoad();
    await __s(200);
    step("★会话没有工作目录时说明原因（不空转）",
         /没有工作目录/.test(__txt(document.getElementById("undo-list"))),
         __txt(document.getElementById("undo-list")));
    state.current = keep;

    /* ---------- 9. Esc ---------- */
    await undoLoad();
    await __s(120);
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(150);
    step("★Esc 关掉弹窗", !!box && box.hidden);
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
  #undo, #undo * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.state) break; }
  await __sh(200);
  state.current = { id: "ses_probe", title: "撤销探针", location: { directory: "D:\\proj" },
                    model: { id: "probe-1", providerID: "acp" } };
  await undoOpen();
  await __sh(300);
  var r = document.querySelector('[data-upath="src/app.js"]');
  if (r) r.click();
  await __sh(300);
})();
</script>
"""


def build_probe(shot: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    extra = SHOT_JS if shot else PROBE_JS
    out = src.replace('<script src="/app.js', STUB + extra + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str) -> str:
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_undo_dom.html")
    err = os.path.join(tmp, "probe_undo_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeundo"),
             "--window-size=1400,920",
             "--virtual-time-budget=30000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def take_shot(edge: str, path: str) -> None:
    tmp = os.environ.get("TEMP", ".")
    with open(os.path.join(tmp, "probe_undo_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probeundoshot"),
             "--window-size=1400,1000", "--virtual-time-budget=30000",
             "--screenshot=" + path, URL],
            stdout=subprocess.DEVNULL, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("  截图:", path, "存在=", os.path.isfile(path))


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv:
        shot = os.path.join(HERE, "resources", "screenshots", "undo_turn.png")
        build_probe(shot=True)
        try:
            take_shot(edge, shot)
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
        print("没拿到探针结果（渲染可能失败）。DOM 前 600 字符：")
        print(html[:600])
        return 1

    d = json.loads(m.group(1))
    print("\n=== 可逆执行「撤销这一轮」无头回归 ===")
    bad = 0
    for name, ok, extra in d.get("steps", []):
        print("  %s %s%s" % ("OK  " if ok else "MISS", name, ("   [" + extra + "]") if extra and not ok else ""))
        if not ok:
            bad += 1
    if d.get("crash"):
        print("\n!! 探针自身抛异常：\n" + d["crash"])
        bad += 1
    errs = d.get("jsErrors") or []
    print("\n  %s 全程零 JS 报错%s" % ("OK  " if not errs else "MISS", ("  " + str(errs[:3])) if errs else ""))
    if errs:
        bad += 1
    if os.environ.get("PROBE_INFO"):
        print("\n  info: " + json.dumps(d.get("info"), ensure_ascii=False))
    total = len(d.get("steps", [])) + 1
    print("\n  total %d, failed %d" % (total, bad))
    print("  result: %s" % ("PASS" if bad == 0 else "FAIL"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
