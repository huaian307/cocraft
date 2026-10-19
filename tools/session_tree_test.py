# -*- coding: utf-8 -*-
r"""无头回归：会话列表里的「项目文件树」（2026-10-16）。

和 `trace_time_test.py` 同一套：用 `frontend/index.html` 生成探针页，真跑 `app.js`，
把 `/api/session*` 换成内存假会话、把 `/fs/*` 换成假目录树，然后断言：

  1. 只有**有工作目录**的会话才有 🗂 按钮
  2. 点 🗂 → 请求 `/fs/list?root=<该会话目录>&path=<它自己>`，树出现在那条会话下面
  3. 目录行是 `▸`；点一下**才**懒加载子目录（新请求），变 `▾` 并列出子项
  4. 同一项目的**第二条会话**展开 → **不重复请求**（按 root 缓存命中）
  5. 点文件 → **编码模式**抽屉打开；`CODE.root=项目`、`CODE.sel=该文件`、编辑器有内容；树里那行高亮
  6. 再点 🗂 → 收起（树消失）
  7. 树里的**空白处**点击 → 不会顺手把会话打开（`state.current` 不变）
  8. 换个项目（另一条会话）→ 用的是它自己的 root
  9. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\session_tree_test.py
    python tools\session_tree_test.py --shot resources\screenshots\session_tree.png
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
PROBE = os.path.join(WEB, "_probe_tree.html")
URL = "http://127.0.0.1:8787/_probe_tree.html"

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
              "/api/model", "/engine/", "/update/", "/appearance", "/live/", "/pty"];
  var raw = window.fetch;
  var T = 1760000000000;
  var PROJ_A = "D:\\projA", PROJ_B = "D:\\projB";

  var SESSIONS = [
    { id: "ses_a", title: "会话 A（项目 A）", time: { created: T, updated: T + 3000 },
      location: { directory: PROJ_A } },
    { id: "ses_b", title: "会话 B（同一个项目 A）", time: { created: T - 1000, updated: T + 2000 },
      location: { directory: PROJ_A } },
    { id: "ses_c", title: "会话 C（项目 B）", time: { created: T - 2000, updated: T + 1000 },
      location: { directory: PROJ_B } },
    { id: "ses_d", title: "会话 D（没有工作目录）", time: { created: T - 3000, updated: T } },
  ];

  // 假目录树：key = "root|path"
  var TREE = {};
  TREE[PROJ_A + "|" + PROJ_A] = [
    { name: "src", path: PROJ_A + "\\src", dir: true },
    { name: "README.md", path: PROJ_A + "\\README.md", dir: false },
  ];
  TREE[PROJ_A + "|" + PROJ_A + "\\src"] = [
    { name: "app.js", path: PROJ_A + "\\src\\app.js", dir: false },
    { name: "sub", path: PROJ_A + "\\src\\sub", dir: true },
  ];
  TREE[PROJ_A + "|" + PROJ_A + "\\src\\sub"] = [
    { name: "deep.py", path: PROJ_A + "\\src\\sub\\deep.py", dir: false },
  ];
  TREE[PROJ_B + "|" + PROJ_B] = [{ name: "only.txt", path: PROJ_B + "\\only.txt", dir: false }];

  window.__fsReq = [];                                   // 记 /fs/list 请求
  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }
  function q(s) {
    var out = {}, parts = (s.split("?")[1] || "").split("&");
    parts.forEach(function (kv) {
      var a = kv.split("=");
      if (a[0]) out[decodeURIComponent(a[0])] = decodeURIComponent(a[1] || "");
    });
    return out;
  }

  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;

    if (path === "/fs/list") {                            // ★文件树的数据源
      var p = q(s);
      window.__fsReq.push({ root: p.root, path: p.path });
      var items = TREE[p.root + "|" + p.path] || [];
      return J({ ok: true, data: { root: p.root, path: p.path, items: items } });
    }
    if (path === "/fs/read") {
      var p2 = q(s);
      var txt = "// " + p2.path + "\nconsole.log('probe');\n";
      return J({ ok: true, data: { text: txt, size: txt.length, binary: false } });
    }
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) return J({ data: [], cursor: null });
    if (/^\/api\/session\/[^/]+$/.test(path)) {
      var hit = SESSIONS.filter(function (x) { return path === "/api/session/" + x.id; })[0];
      return J({ data: hit || SESSIONS[0] });
    }
    if (path === "/api/session") return J({ data: SESSIONS, cursor: null });
    return raw.apply(this, arguments);
  };

  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () {
    this.close = function () {}; this.addEventListener = function () {};
  };
  window.WebSocket = function () {
    this.send = function () {}; this.close = function () {}; this.readyState = 1;
  };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __sess(id) { return document.querySelector('#session-list .session[data-id="' + id + '"]'); }
function __treeBtn(id) { var s = __sess(id); return s && s.querySelector(".tree-btn"); }
function __tree(id) { var s = __sess(id); return s && s.querySelector(".session-tree"); }
function __row(id, attr, val) {
  var t = __tree(id);
  if (!t) return null;
  var rows = t.querySelectorAll("[data-" + attr + "]");
  for (var i = 0; i < rows.length; i++) if (rows[i].getAttribute("data-" + attr) === val) return rows[i];
  return null;
}
var PA = "D:\\projA", PB = "D:\\projB";

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    var i;
    for (i = 0; i < 300; i++) {
      await __s(25);                    // ⚠ 先 await：app.js 还没跑时 `state` 还不存在
      if ((state.sessions || []).length >= 4) break;
    }
    await __s(80);
    out.info.sessions = (state.sessions || []).map(function (s) { return s.id; });

    /* 1. 按钮只在有工作目录的会话上 */
    step("★只有有工作目录的会话才有 🗂", !!__treeBtn("ses_a") && !!__treeBtn("ses_c")
         && !__treeBtn("ses_d"),
         "a=" + !!__treeBtn("ses_a") + " c=" + !!__treeBtn("ses_c") + " d=" + !!__treeBtn("ses_d"));

    /* 2. 点 🗂 → 拉根目录，树出现 */
    var cur0 = state.current && state.current.id;
    __treeBtn("ses_a").click();
    await __s(120);
    out.info.req0 = window.__fsReq[0];
    step("★点 🗂 拉的是**这条会话自己的工作目录**",
         window.__fsReq.length === 1 && window.__fsReq[0].root === PA && window.__fsReq[0].path === PA,
         JSON.stringify(window.__fsReq));
    step("★树出来了：目录 ▸ + 文件都在",
         !!__tree("ses_a") && !!__row("ses_a", "sdir", PA + "\\src") && !!__row("ses_a", "sfile", PA + "\\README.md"),
         __tree("ses_a") ? __tree("ses_a").textContent.slice(0, 60) : "(无树)");
    step("★展开 🗂 不会顺手把这条会话打开", (state.current && state.current.id) === cur0,
         "before=" + cur0 + " after=" + (state.current && state.current.id));

    /* 3. 点目录 → 懒加载 */
    __row("ses_a", "sdir", PA + "\\src").click();
    await __s(120);
    step("★点目录才懒加载（第二次请求 path=子目录）",
         window.__fsReq.length === 2 && window.__fsReq[1].path === PA + "\\src",
         JSON.stringify(window.__fsReq.slice(1)));
    step("★子目录展开了：▾ + 子项出现",
         !!__row("ses_a", "sdir", PA + "\\src\\sub") && !!__row("ses_a", "sfile", PA + "\\src\\app.js"),
         __tree("ses_a").textContent.slice(0, 80));
    var sOpen = __row("ses_a", "sdir", PA + "\\src");
    var sSub = __row("ses_a", "sdir", PA + "\\src\\sub");
    step("★文件夹图标分「开/合」两种（展开的带 .fold.open，合上的不带）",
         !!sOpen && !!sOpen.querySelector("svg.fold.open")
         && !!sSub && !sSub.querySelector("svg.fold.open"),
         "src=" + !!(sOpen && sOpen.querySelector("svg.fold.open"))
         + " sub=" + !!(sSub && sSub.querySelector("svg.fold.open")));
    step("★用的是标准文件夹 SVG（不是 ▸/▾ 三角、也不是 emoji）",
         !!sOpen && /svg/.test(sOpen.querySelector(".tw.ico").innerHTML)
         && sOpen.querySelector(".tw.ico").textContent.trim() === "",
         sOpen ? sOpen.querySelector(".tw.ico").innerHTML.slice(0, 40) : "(没有)");

    /* 4. 同项目的第二条会话：缓存命中，不重复请求 */
    var before = window.__fsReq.length;
    __treeBtn("ses_b").click();
    await __s(150);
    out.info.cache = { before: before, after: window.__fsReq.length };
    step("★同项目第二条会话**不重复请求**（按 root 缓存）",
         window.__fsReq.length === before && !!__tree("ses_b") && !!__tree("ses_b").textContent.length,
         "before=" + before + " after=" + window.__fsReq.length);

    /* 5. 点文件 → 编码模式打开 */
    __row("ses_a", "sfile", PA + "\\README.md").click();
    await __s(200);
    out.info.code = { open: CODE.open, root: CODE.root, sel: CODE.sel,
                      htmlCls: document.documentElement.className };
    step("★点文件 → 编码模式抽屉打开（html.code-open）",
         !!CODE.open && document.documentElement.classList.contains("code-open"),
         JSON.stringify(out.info.code));
    step("★打开的是**那棵树的项目 + 那个文件**（不依赖当前会话目录）",
         CODE.root === PA && CODE.sel === PA + "\\README.md", CODE.root + " | " + CODE.sel);
    var ed = document.getElementById("code-editor");
    step("★编辑器里真有内容", !!ed && /probe/.test(ed.value), ed ? ed.value.slice(0, 40) : "(没有编辑器)");
    step("★左侧树里那个文件被高亮（.on）",
         !!__row("ses_a", "sfile", PA + "\\README.md")
         && __row("ses_a", "sfile", PA + "\\README.md").classList.contains("on"),
         "cls=" + (__row("ses_a", "sfile", PA + "\\README.md") || {}).className);

    /* 6. 再点 🗂 → 收起 */
    __treeBtn("ses_a").click();
    await __s(120);
    step("★再点 🗂 收起（树消失）", !__tree("ses_a"), "tree=" + !!__tree("ses_a"));

    /* 7. 树/空白处点击不打开会话 */
    var cur1 = state.current && state.current.id;
    var t = __tree("ses_b");
    if (t) t.click();
    await __s(80);
    step("★点树里的空白处不会打开会话", (state.current && state.current.id) === cur1,
         "cur=" + (state.current && state.current.id));

    /* 8. 另一个项目用另一个 root */
    __treeBtn("ses_c").click();
    await __s(150);
    var last = window.__fsReq[window.__fsReq.length - 1];
    step("★换一个项目 → 请求它自己的 root",
         !!last && last.root === PB && last.path === PB && !!__row("ses_c", "sfile", PB + "\\only.txt"),
         JSON.stringify(last));
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
  #session-list, #session-list *, #code, #code * { transition: none !important; animation: none !important; }
</style>
<script>
window.__probeErr = [];
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) {
    await __sh(25);
    if ((state.sessions || []).length >= 4) break;
  }
  await __sh(80);
  var b = document.querySelector('#session-list .session[data-id="ses_a"] .tree-btn');
  if (b) b.click();
  await __sh(150);
  var rows = document.querySelectorAll('#session-list .session[data-id="ses_a"] .session-tree [data-sdir]');
  if (rows.length) rows[0].click();
  await __sh(200);
  var f = document.querySelector('#session-list .session[data-id="ses_a"] .session-tree [data-sfile]');
  if (f) f.click();
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
    dom = os.path.join(tmp, "probe_tree_dom.html")
    err = os.path.join(tmp, "probe_tree_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probetree"),
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
    with open(os.path.join(tmp, "probe_tree_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probetreeshot"),
             # ⚠ 窗口要够宽：不然「编码模式抽屉」把主内容压到 <360px 会触发**抽屉重合雾化**，
             #   整个左侧(含文件树)被糊掉，截图就没法看（实测 1400 宽就会糊）。
             "--window-size=1760,1000", "--virtual-time-budget=30000",
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
        shot = os.path.join(HERE, "resources", "screenshots", "session_tree.png")
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
    print("\n=== 会话列表「项目文件树」无头回归 ===")
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
