# -*- coding: utf-8 -*-
r"""无头回归：★可逆执行「隔离区」界面（`ui 0.1.76`，2026-10-16）。

背景：破坏性操作**放行前**先把要动的文件复制进 `runtime/quarantine/`（见 `backend/snapshots.py`），
本测试盯的是**前端那一半** —— 工具菜单里的入口 + 记录卡渲染 + 一键恢复 + 删除 + 打开目录。

用 `frontend/index.html` 生成探针页，真跑 `app.js`，把 `/quarantine/*` 换成内存假数据，断言：

    1. 工具菜单里有「隔离区」入口
    2. 打开 → 列出 N 条记录（项数 / 大小 / 时间 / 说明 / 命令原文）
    3. **没保住的项要明说**（`.q-warn`，不假装成功）
    4. 「恢复全部」→ `POST /quarantine/restore {id}` + 横幅回报
    5. **目标已存在 → 先问再用 `override:true` 覆盖**（不静默覆盖）
    6. 「删除记录」→ 二次确认后才 `POST /quarantine/drop`
    7. 「打开隔离区目录」→ `POST /open/dir {what:"explorer"}`
    8. Esc 关掉弹窗
    9. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\quarantine_ui_test.py
    python tools\quarantine_ui_test.py --shot resources\screenshots\quarantine.png
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
PROBE = os.path.join(WEB, "_probe_quar.html")
URL = "http://127.0.0.1:8787/_probe_quar.html"

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
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/fs/", "/ccagent"];
  var raw = window.fetch;
  var T = 1760000000000;
  window.__calls = [];

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var QITEMS = [
    { id: "chat-3/20261016-112700-001", ts: "20261016-112700-001", scope: "chat-3",
      taken_at: T / 1000, note: "破坏性操作放行前隔离：Run command",
      cmd: "Remove-Item -Recurse -Force 'D:\\proj\\dist'", root: "D:\\proj",
      count: 2, bytes: 40960, missed: 0 },
    { id: "chat-3/20261016-113000-002", ts: "20261016-113000-002", scope: "chat-3",
      taken_at: T / 1000 + 30, note: "破坏性操作放行前隔离：删除桌面所有文件",
      cmd: "del /s /q C:\\Users\\tester\\Desktop\\*.*", root: "D:\\proj",
      count: 1, bytes: 512, missed: 3 }
  ];

  var SESS = { id: "ses_probe", title: "隔离区探针",
               time: { created: T, updated: T },
               location: { directory: "D:\\\\proj" },
               model: { id: "probe-1", providerID: "acp" } };

  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ location: { directory: "D:\\\\proj" }, data: [] });
    if (path === "/api/model/default") return J({ data: null });

    if (path === "/quarantine/list") {
      return J({ ok: true, data: { items: QITEMS,
        stats: { quarantine: 41472, store: 0, snapshots: 0,
                 quarantine_dir: "D:\\\\opencode-ui\\\\runtime\\\\quarantine",
                 snapshot_dir: "D:\\\\opencode-ui\\\\runtime\\\\snapshots" } } });
    }
    if (path === "/quarantine/restore") {
      var b = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["restore", b.id, !!b.override]);
      if (b.id === QITEMS[1].id && !b.override) {
        return J({ ok: true, id: b.id, restored: 0, conflict: 1, missing: 0, failed: 0, actions: [] });
      }
      return J({ ok: true, id: b.id, restored: 2, conflict: 0, missing: 0, failed: 0, actions: [] });
    }
    if (path === "/quarantine/drop") {
      var d = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["drop", d.id]);
      return J({ ok: true, dropped: [d.id] });
    }
    if (path === "/open/dir") {
      var od = JSON.parse((o && o.body) || "{}");
      window.__calls.push(["open", od.what, od.dir]);
      return J({ ok: true });
    }

    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
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
function __callsOf(kind) { return (window.__calls || []).filter(function (c) { return c[0] === kind; }); }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 200; i++) { await __s(25); if (window.state) break; }
    await __s(200);

    var box = document.getElementById("quar");
    step("★工具菜单里有「隔离区」入口", !!document.getElementById("tm-quar"),
         document.getElementById("tm-quar") ? "" : "(没有 tm-quar)");
    step("★弹窗默认是收起的", !!box && box.hidden);

    await quarOpen();
    await __s(250);
    step("★打开后弹窗可见", !!box && !box.hidden);

    var rows = document.querySelectorAll("#quar-list .q-row");
    out.info.rows = rows.length;
    step("★列出 2 条隔离记录", rows.length === 2, "rows=" + rows.length);

    var t0 = __txt(rows[0]);
    out.info.row0 = t0;
    step("★记录写清「几项 + 大小 + 时间」",
         /2 项/.test(t0) && /(KB|B)/.test(t0) && /\d\d-\d\d \d\d:\d\d/.test(t0), t0.slice(0, 120));
    step("★记录带上说明与**命令原文**",
         /破坏性操作放行前隔离/.test(t0) && /Remove-Item -Recurse/.test(t0), t0.slice(0, 160));
    step("★**没保住的项要明说**（不假装成功）",
         document.querySelectorAll("#quar-list .q-warn").length === 1
         && /3 项没保住/.test(__txt(document.querySelector("#quar-list .q-warn"))),
         __txt(document.querySelector("#quar-list .q-warn")));
    step("★底部统计显示隔离区占用与目录",
         /隔离区占用/.test(__txt(document.getElementById("quar-foot")))
         && /quarantine/.test(__txt(document.getElementById("quar-foot"))),
         __txt(document.getElementById("quar-foot")));

    /* 恢复：目标已存在 → 先问，再用 override 覆盖 */
    document.querySelectorAll("[data-qrestore]")[0].click();
    await __s(300);
    var rs = __callsOf("restore");
    step("★「恢复全部」→ POST /quarantine/restore（带 id）",
         rs.length === 1 && rs[0][1].indexOf("20261016-112700") >= 0, JSON.stringify(rs));
    step("★恢复后有横幅回报", /已恢复 2 项/.test(__txt(document.getElementById("banner"))),
         __txt(document.getElementById("banner")));

    document.querySelectorAll("[data-qrestore]")[1].click();
    await __s(400);
    var rs2 = __callsOf("restore");
    out.info.restores = rs2;
    step("★目标已存在 → **先问一句**，再带 override:true 覆盖（不静默覆盖）",
         rs2.length === 3 && rs2[1][2] === false && rs2[2][2] === true, JSON.stringify(rs2));
    step("★跳过冲突时如实说明（保留现在的文件）",
         /目标已存在/.test(__txt(document.getElementById("banner")))
         || /已恢复/.test(__txt(document.getElementById("banner"))),
         __txt(document.getElementById("banner")));

    /* 删除记录：确认之后才发 */
    document.querySelectorAll("[data-qdrop]")[0].click();
    await __s(300);
    var dp = __callsOf("drop");
    step("★「删除记录」→ 确认后 POST /quarantine/drop", dp.length === 1, JSON.stringify(dp));
    document.querySelectorAll("[data-qdrop]")[1].click();
    await __s(250);
    out.info.approveAll = true;

    /* 打开隔离区目录 */
    document.getElementById("quar-open").click();
    await __s(250);
    var op = __callsOf("open");
    step("★「打开隔离区目录」→ POST /open/dir explorer（指向隔离区目录）",
         op.length === 1 && op[0][1] === "explorer" && /quarantine/.test(op[0][2]), JSON.stringify(op));

    /* Esc 关闭 */
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
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
  #quar, #quar * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.state) break; }
  await __sh(200);
  await quarOpen();
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
    dom = os.path.join(tmp, "probe_quar_dom.html")
    err = os.path.join(tmp, "probe_quar_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probequar"),
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
    with open(os.path.join(tmp, "probe_quar_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probequarshot"),
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
        shot = os.path.join(HERE, "resources", "screenshots", "quarantine.png")
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
    print("\n=== 可逆执行「隔离区」界面无头回归 ===")
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
