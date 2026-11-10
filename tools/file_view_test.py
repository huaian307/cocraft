# -*- coding: utf-8 -*-
r"""无头回归：「文件查看」—— 改名 + 预览（图片 / PDF / 音视频）+ **生成文件后自动打开**（`ui 0.2.86`）。

用户原话：「我们的面板现在没有预览功能，加一个每当涉及生成文件的时候就在右边用我们的编码模式
代开对应的文件，同时把编码模式的名字改为文件查看」。

所以三条需求，逐条断言：
  1. **改名**：抽屉标题 / 工具菜单都叫「文件查看」，不再出现「编码模式」；
  2. **预览**：图片 / PDF / 音视频**直接渲染**（走 `/fs/raw`），文本仍走编辑器，两者互斥；
  3. **自动打开**：`edit` / `write` 这类工具**成功之后**，把生成的文件在右侧抽屉自动打开；
     并且**别烦人** —— 当前文件有未保存改动时不切、工作目录外的文件不开、同一条消息只开一次、
     关掉开关就完全不自动开、**不抢焦点**。

为什么不用真文件系统：探针里 stub 掉 `/fs/list` `/fs/read`（和 `live_slot_test` 同一套做法），
`/fs/raw` 走 `<img src>` 这类真实请求（拿到 400 也不影响断言 DOM）。

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\file_view_test.py
    python tools\file_view_test.py --shot resources\screenshots\file_view.png
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys

try:                                   # 控制台是 GBK 时别让特殊符号把脚本自己打挂
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                      # noqa: BLE001
    pass

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_fv.html")
URL = "http://127.0.0.1:8787/_probe_fv.html"

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
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/audit/",
              "/snapshot/", "/quarantine/", "/ccagent/", "/orchestrate/"];
  var raw = window.fetch;
  var T = 1760000000000;
  var ROOT = "D:/ws";

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  // 假文件树：两个文本 + 一份 md + 一张图 + 一份 PDF
  var LIST = [
    { name: "out.py", path: ROOT + "/out.py", dir: false, size: 120, mtime: T },
    { name: "out.md", path: ROOT + "/out.md", dir: false, size: 120, mtime: T },
    { name: "readme.md", path: ROOT + "/readme.md", dir: false, size: 200, mtime: T },
    { name: "report.html", path: ROOT + "/report.html", dir: false, size: 400, mtime: T },
    { name: "pic.png", path: ROOT + "/pic.png", dir: false, size: 2048, mtime: T },
    { name: "report.pdf", path: ROOT + "/report.pdf", dir: false, size: 4096, mtime: T },
  ];
  var TEXT = {
    "out.py": "print('生成结果')\n",
    "out.md": "# 生成结果\n\n这是 agent 写出来的内容。\n",
    "readme.md": "## 标题二\n\n- 甲\n- 乙\n\n正文 **加粗** 与 `code`。\n",
    "report.html": "<!doctype html><html><body><h1>报告标题</h1><p>正文</p></body></html>\n",
  };

  var MSGS = [{ id: "m1", type: "user", text: "画一张图并导出 PDF", time: { created: T } }];
  window.MSGS = MSGS;                         // 探针里要往里面追加一条 assistant（测兜底路径）
  var SESS = { id: "ses_probe", title: "文件查看探针", time: { created: T, updated: T + 1000 },
               location: { directory: ROOT }, model: { id: "probe-1", providerID: "acp" } };

  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ location: { directory: ROOT }, data: [] });
    if (path === "/api/model/default") return J({ data: null });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    if (path === "/fs/list") {
      return J({ ok: true, data: { root: ROOT, path: ROOT, parent: "", items: LIST } });
    }
    if (path === "/fs/read") {
      var p = decodeURIComponent(s.split("path=")[1] || "");
      var key = p.split("/").pop();
      return J({ ok: true, data: { root: ROOT, path: p, size: 120, binary: false,
                                   text: TEXT[key] || "", mtime: T } });
    }
    if (path === "/api/session/ses_probe/message") {
      return J({ data: MSGS.slice().reverse(), cursor: null });
    }
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
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
function __pv() { return document.getElementById("code-preview"); }
function __ed() { return document.getElementById("code-editor"); }
function __wrap() { return document.querySelector(".code-edit-wrap"); }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 300; i++) {
      await __s(25);
      if ((state.messages || []).length >= 1) break;
    }
    await __s(150);

    /* 0. ★用户实测 bug：从没打开过「文件查看」时（CODE.root 为空）自动打开也要生效 */
    state.wantFiles = { at: Date.now(), on: true };     // 模拟"用户明确要求动文件"
    CODE.root = ""; CODE.open = false; CODE.autoSeen = {}; CODE.autoAt = 0;
    var firstOk = await codeAutoOpen(["D:/ws/out.md"], "tool:t0");
    await __s(300);
    out.info.first = "ok=" + firstOk + " sel=" + CODE.sel + " root=" + CODE.root;
    step("★没打开过抽屉也能自动开（根目录取当前会话目录，不再要求 CODE.root 已有值）",
         firstOk === true && CODE.sel === "out.md" && /ws$/i.test(CODE.root), out.info.first);
    codeClose();
    await __s(150);

    /* 1. 改名：抽屉 + 工具菜单都叫「文件查看」 */
    var head = __txt(document.querySelector("#code .code-head"));
    var menu = __txt(document.getElementById("tm-code"));
    out.info.head = head.slice(0, 90);
    step("★抽屉标题叫「文件查看」（不再出现「编码模式」）",
         /文件查看/.test(head) && !/编码模式/.test(head), head.slice(0, 60));
    step("★工具菜单那一项也叫「文件查看」", /文件查看/.test(menu) && !/编码模式/.test(menu), menu);

    /* 2. 文本 → 编辑器；图片 / PDF → 预览层；Markdown → 渲染（互斥） */
    var __realAuto = codeAutoOpen;
    codeAutoOpen = async function () { return false; };   // 这一段只测手动打开
    codeOpenAt("D:/ws", "out.py");
    await __s(400);
    out.info.text = "sel=" + CODE.sel + " previewHidden=" + __pv().hidden
      + " wrapHidden=" + __wrap().hidden + " val=" + (__ed().value || "").slice(0, 12);
    step("★普通文本文件走编辑器（预览层收起）",
         CODE.sel === "out.py" && __pv().hidden === true && __wrap().hidden === false
         && /生成结果/.test(__ed().value), out.info.text);

    /* 2b. ★Markdown → **渲染后的富文本**（以前 wrap=off 的原始文本，长行还被切掉） */
    await codeOpenFile("readme.md");
    await __s(400);
    var mdBox = __pv().querySelector(".cp-md");
    out.info.md = "hidden=" + __pv().hidden + " wrapHidden=" + __wrap().hidden
      + " h2=" + (mdBox ? mdBox.querySelectorAll("h2").length : -1)
      + " mdLen=" + (mdBox ? mdBox.textContent.length : 0);
    step("★Markdown 默认**渲染**（标题/列表出标签，不是原始 ##）",
         __pv().hidden === false && __wrap().hidden === true && !!mdBox
         && !!mdBox.querySelector("h2") && !/##\\s/.test(mdBox.textContent), out.info.md);
    var srcBtn = __pv().querySelector('[data-cp="srcedit"]');
    step("★Markdown 给「源码」入口（切回可编辑）", !!srcBtn, srcBtn ? srcBtn.textContent : "(没有)");
    if (srcBtn) {
      srcBtn.click();
      await __s(300);
      out.info.mdSrc = "wrapHidden=" + __wrap().hidden
        + " mdSrc=" + (__wrap().classList.contains("md-src"))
        + " editable=" + !__ed().disabled;
      step("★点「源码」→ 编辑器出现、可编辑、且**允许折行**（不再切掉长行）",
           __wrap().hidden === false && __wrap().classList.contains("md-src")
           && __ed().disabled === false, out.info.mdSrc);
    }

    /* 2c. ★HTML → 沙箱 iframe 渲染（用户实测：生成 .html 报告却看到原始标签） */
    await codeOpenFile("report.html");
    await __s(500);
    var frame = __pv().querySelector("iframe.cp-html");
    var sb = frame ? (frame.getAttribute("sandbox") || "") : "(没有)";
    out.info.html = "hidden=" + __pv().hidden + " frame=" + !!frame + " sandbox=[" + sb + "]"
      + " srcdoc=" + (frame ? String(frame.getAttribute("srcdoc") || "").slice(0, 30) : "");
    step("★HTML → 沙箱 iframe 渲染（不是原始标签）",
         __pv().hidden === false && !!frame && __wrap().hidden === true, out.info.html);
    step("★默认**不许脚本、也不许同源**（沙箱 = 空，碰不到本机接口）",
         !!frame && sb === "", out.info.html);
    step("★srcdoc 里带的是完整文档（h1 标题在）",
         !!frame && /<h1>/.test(String(frame.getAttribute("srcdoc") || "")), out.info.html);
    var hSrc = __pv().querySelector('[data-cp="srcedit"]');
    if (hSrc) {
      hSrc.click();
      await __s(300);
      step("★HTML 也能切「源码」（可编辑 + 折行）",
           __wrap().hidden === false && __wrap().classList.contains("md-src")
           && __ed().disabled === false && /<h1>/.test(__ed().value),
           "wrapHidden=" + __wrap().hidden + " val=" + (__ed().value || "").slice(0, 24));
    }

    await codeOpenFile("pic.png");    await __s(200);
    var img = __pv().querySelector("img");
    out.info.img = "hidden=" + __pv().hidden + " wrapHidden=" + __wrap().hidden
      + " src=" + (img ? img.getAttribute("src") : "(没有 img)");
    step("★图片 → 预览层 <img>，编辑器收起（/fs/raw）",
         __pv().hidden === false && !!img && /\/fs\/raw\?/.test(img.getAttribute("src"))
         && __wrap().hidden === true, out.info.img);

    await codeOpenFile("report.pdf");
    await __s(200);
    var emb = __pv().querySelector("embed");
    out.info.pdf = emb ? emb.getAttribute("type") : "(没有 embed)";
    step("★PDF → 预览层 <embed type=application/pdf>",
         !!emb && /pdf/.test(emb.getAttribute("type") || ""), out.info.pdf);

    /* 3. 自动打开：工具写文件 → 右侧代开（**只在用户明确要求动文件时**） */
    codeAutoOpen = __realAuto;                  // 恢复真实现
    codeOpenAt("D:/ws", null);
    await __s(300);
    codeClose();
    await __s(120);
    CODE.autoSeen = {}; CODE.autoAt = 0;
    var files = codeFilesOfTool("write", { filePath: "D:/ws/out.md" }, null);
    out.info.files = files.join(",");
    step("★从 write 工具里认出文件路径", files.length === 1 && /out\.md$/.test(files[0]), out.info.files);
    step("★绝对路径归一成工作目录内的相对路径", codeRelPath("D:\\ws\\out.md") === "out.md",
         codeRelPath("D:\\ws\\out.md"));
    step("★别的盘 / 工作目录外 → 不认（返回空）",
         codeRelPath("C:/other/x.md") === "" && codeRelPath("D:/ws2/x.md") === "",
         codeRelPath("C:/other/x.md") + " / " + codeRelPath("D:/ws2/x.md"));

    /* 3a. ★闸门：用户**没要求动文件** → 不自动打开，而且**不弹横幅**（安静） */
    state.wantFiles = { at: Date.now(), on: false };
    codeOpenAt("D:/ws", null);
    await __s(200);
    codeClose();
    await __s(120);
    CODE.autoSeen = {}; CODE.autoAt = 0;
    setBanner("");
    await __s(60);
    var gateOff = await codeAutoOpen(["D:/ws/out.md"], "tool:g0");
    await __s(200);
    out.info.gateOff = "ok=" + gateOff + " open=" + CODE.open
      + " banner=" + __txt(document.getElementById("banner")).slice(0, 30);
    step("★用户没让我动文件 → **不弹**、也**不弹横幅**（安静）",
         gateOff === false && CODE.open === false
         && !/已改动|没让我动文件/.test(__txt(document.getElementById("banner"))), out.info.gateOff);

    /* 3b. ★意图判定：像"改个界面"这种**不算**，带文件名的才算 */
    out.info.intent = [
      needsFileTouch("修改一下，当我打开文件树的时候才出现工作区标识") ? "1" : "0",
      needsFileTouch("把 README.md 里的安装步骤改一下") ? "1" : "0",
      needsFileTouch("生成一份 report.pdf") ? "1" : "0",
      needsFileTouch("这个文件帮我改一下") ? "1" : "0",
      needsFileTouch("今天天气怎么样") ? "1" : "0",
    ].join("");
    step("★意图判定：没点名文件的「改界面」不算；带文件名 / 「这个文件」才算",
         out.info.intent === "01110", out.info.intent);

    /* 3c. 用户明确要求 → 正常自动打开 */
    state.wantFiles = { at: Date.now(), on: true };
    CODE.autoSeen = {}; CODE.autoAt = 0;
    codeOpenAt("D:/ws", null);
    await __s(200);
    codeClose();
    await __s(120);
    var okOpen = await codeAutoOpen(["D:/ws/out.md"], "tool:t1");
    await __s(400);
    out.info.auto = "ok=" + okOpen + " sel=" + CODE.sel + " open=" + CODE.open
      + " focusStolen=" + (document.activeElement === __ed());
    step("★用户明确要动文件 → 自动在抽屉里打开那个文件",
         okOpen === true && CODE.sel === "out.md" && CODE.open === true, out.info.auto);
    step("★自动打开**不抢焦点**（用户可能正在输入框打字）",
         document.activeElement !== __ed(), "active=" + (document.activeElement || {}).id);

    /* 同一条消息 / 同一个文件不重复开 */
    var dup = await codeAutoOpen(["D:/ws/out.md"], "tool:t1");
    step("★同一个 key + 同一个文件不重复打开", dup === false, "dup=" + dup);

    /* 脏文件保护：正在改别的文件 → 不切 */
    CODE.dirty = true;
    var dirtyOk = await codeAutoOpen(["D:/ws/pic.png"], "tool:t2");
    await __s(200);
    out.info.dirty = "ok=" + dirtyOk + " sel=" + CODE.sel
      + " banner=" + __txt(document.getElementById("banner")).slice(0, 40);
    step("★当前文件有未保存改动 → **不切**，只给横幅说明",
         dirtyOk === false && CODE.sel === "out.md"
         && /未保存/.test(__txt(document.getElementById("banner"))), out.info.dirty);
    CODE.dirty = false;

    /* 开关关掉 → 完全不自动开 */
    setSetting("autoOpen", false);
    CODE.autoSeen = {}; CODE.autoAt = 0;
    var offOk = await codeAutoOpen(["D:/ws/pic.png"], "tool:t3");
    step("★「自动打开」关掉后不再自动开", offOk === false, "off=" + offOk);
    setSetting("autoOpen", true);

    /* 4. 权威消息兜底：refreshMessages 后也能认出来（ACP 靠这条） */
    MSGS.push({ id: "m2", type: "assistant", agent: "build",
      time: { created: 1760000001000, completed: 1760000002000 },
      content: [{ type: "tool", name: "write",
        state: { status: "completed", input: { filePath: "D:/ws/out.md" } } }] });
    CODE.autoSeen = {}; CODE.autoAt = 0;
    await refreshMessages();
    await __s(600);
    out.info.msg = "sel=" + CODE.sel + " seen=" + Object.keys(CODE.autoSeen).join(",");
    step("★已落库的消息里也能认出生成的文件（ACP 兜底）",
         !!Object.keys(CODE.autoSeen).some(function (k) { return /msg:m2\|out\.md/.test(k); }),
         out.info.msg);

    /* 5. 页面重载 / 切会话：第一次扫描只记账，**不弹抽屉** */
    codeClose();
    await __s(100);
    codePrimeAuto();                 // = 刚进会话
    await refreshMessages();
    await __s(500);
    out.info.prime = "open=" + CODE.open + " sel=" + CODE.sel
      + " seen=" + Object.keys(CODE.autoSeen).length;
    step("★刚进会话 / 刚重载：只记住旧文件，**不自动弹**",
         CODE.open === false && Object.keys(CODE.autoSeen).length > 0, out.info.prime);
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

SHOT_JS = r"""<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 300; i++) {
    await __sh(25);
    if ((state.messages || []).length >= 1) break;
  }
  await __sh(200);
  codeOpenAt("D:/ws", "__SHOTFILE__");
  await __sh(700);
})();
</script>
"""


def build_probe(shot: bool = False, shot_file: str = "readme.md") -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    extra = (SHOT_JS.replace("__SHOTFILE__", shot_file)) if shot else PROBE_JS
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
    dom = os.path.join(tmp, "probe_fv_dom.html")
    err = os.path.join(tmp, "probe_fv_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probefv"),
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
    with open(os.path.join(tmp, "probe_fv_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probefvshot"),
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
    if "--shot" in sys.argv or "--shot-html" in sys.argv:
        is_html = "--shot-html" in sys.argv
        shot = os.path.join(HERE, "resources", "screenshots",
                            "file_view_html.png" if is_html else "file_view.png")
        build_probe(shot=True, shot_file="report.html" if is_html else "readme.md")
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
    print("\n=== 「文件查看」：改名 / 预览 / 自动打开 · 无头回归 ===")
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
