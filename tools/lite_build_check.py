# -*- coding: utf-8 -*-
r"""★构建产物自检：把 `dist\payload\app\frontend`（精简包里的前端）无头跑一遍。

为什么需要它：`packaging\strip_music.py` 会**删 DOM / 换 JS 桩**，
这类改动很容易变成"面板一开机就 `xxx is not defined`"（踩过两次）。
静态检查（jsbalance / check_ids）抓不到，只有真跑一遍才知道。

它做什么：
  1. 起一个**临时静态服务器**（默认 8811，不碰 8787 的正在跑的面板）；
  2. 由 index.html 生成探针页：拦住 API / SSE / 心跳（否则长轮询会把无头渲染卡死）；
  3. 无头 Edge `--dump-dom` 取回探针结论；
  4. 断言：`__appLoaded`、零 JS 错误、面板骨架在、音乐相关 DOM 都不在。

用法：
    python tools\lite_build_check.py                       :: 查 dist\payload\app\frontend
    python tools\lite_build_check.py --app <前端目录>       :: 查别的目录
    python tools\lite_build_check.py --shot 路径.png        :: 顺便出图
"""
from __future__ import annotations

import functools
import http.server
import io
import json
import os
import re
import socketserver
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_APP = os.path.join(HERE, "dist", "payload", "app", "frontend")
TMP = os.environ.get("TEMP", ".")
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 这些 DOM 在精简包里**应该不存在**
MUST_BE_GONE = ["player", "vis-bar", "mus-home", "spec-left", "spec-right",
                "lyrics-overlay", "lyric-ball", "tm-music",
                "cfg-ck-netease", "cfg-ck-qq", "cfg-spec-sides", "cfg-zh"]
# 这些必须还在（精简不能把面板骨架删了）
MUST_EXIST = ["composer", "sidebar", "topbar", "messages", "mus-audio"]

STUB = """<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/music/", "/live/", "/pty/",
              "/panel/", "/audit", "/restore", "/confessional", "/cu/", "/fs/",
              "/orchestrate", "/planner", "/ccagent", "/cocraft", "/agent", "/engine",
              "/skills", "/wallpaper", "/snapshot", "/quarantine", "/update", "/appearance"];
  var raw = window.fetch;
  window.fetch = function (u) {
    var s = String(u);
    if (s.indexOf("/healthz") >= 0) {
      return Promise.resolve(new Response(JSON.stringify({ ok: true, ui: "__VER__", version: "__VER__" }),
        { status: 200, headers: { "Content-Type": "application/json" } }));
    }
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    var body = {};
    if (s.indexOf("/api/session") >= 0) body = { data: [], cursor: null };
    else if (s.indexOf("/api/model") >= 0) body = { data: [], location: {} };
    else if (s.indexOf("/themes") >= 0) body = { ok: true, builtin: [], installed: [], active: "chapel" };
    else body = { ok: true, data: [], items: [], runs: [], pending: [] };
    return Promise.resolve(new Response(JSON.stringify(body),
      { status: 200, headers: { "Content-Type": "application/json" } }));
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () { this.readyState = 1; this.send = function () {}; this.close = function () {}; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) {
  window.__probeErr.push(String(e.message) + " @" + String(e.filename || "").split("/").pop() + ":" + e.lineno);
});
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
setTimeout(function () {
  var has = function (id) { return !!document.getElementById(id); };
  var out = {
    appLoaded: !!window.__appLoaded,
    lite: !!window.__COCRAFT_NO_MUSIC,
    jsErrors: window.__probeErr,
    banner: ((document.getElementById("banner") || {}).textContent || "").trim().slice(0, 200),
    theme: document.documentElement.dataset.theme || "",
    scroll: { w: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth,
              h: document.documentElement.scrollHeight, ch: document.documentElement.clientHeight },
    wide: [],
    exists: {}, gone: {}
  };
  try {
    var lim = out.scroll.cw + 1;
    [].slice.call(document.querySelectorAll("body *")).forEach(function (el) {
      var r = el.getBoundingClientRect();
      if (r.width > lim) out.wide.push((el.id || el.className || el.tagName) + "=" + Math.round(r.width));
    });
    out.wide = out.wide.slice(0, 8);
  } catch (e) {}
  %s.forEach(function (id) { out.exists[id] = has(id); });
  %s.forEach(function (id) { out.gone[id] = has(id); });
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}, 2600);
</script>
""" % (json.dumps(MUST_EXIST), json.dumps(MUST_BE_GONE))


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):        # 别把 404 刷满控制台
        pass


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("[BAD] 没找到 msedge.exe")


def pick_port(preferred: int = 8811) -> int:
    import socket
    for port in range(preferred, preferred + 40):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("[BAD] 找不到可用端口")


def serve(directory: str, port: int) -> socketserver.TCPServer:
    handler = functools.partial(_Quiet, directory=directory)
    httpd = socketserver.ThreadingTCPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main(argv) -> int:
    app = DEFAULT_APP
    shot = ""
    if "--app" in argv:
        app = os.path.abspath(argv[argv.index("--app") + 1])
    if "--shot" in argv:
        shot = os.path.abspath(argv[argv.index("--shot") + 1])
    if not os.path.isdir(app):
        print("[BAD] 找不到前端目录：%s" % app)
        return 1

    html = io.open(os.path.join(app, "index.html"), encoding="utf-8").read()
    m = re.search(r"/app\.js\?v=([\w.\-]+)", html)
    ver = m.group(1) if m else "0.0.0"
    probe_name = "_probe_lite_check.html"
    probe_path = os.path.join(app, probe_name)
    injected = html.replace('<script src="/app.js', STUB.replace("__VER__", ver) + PROBE_JS + '<script src="/app.js')
    if injected == html:
        print("[BAD] index.html 里找不到 app.js 的 <script> 标签")
        return 1
    io.open(probe_path, "w", encoding="utf-8", newline="").write(injected)

    port = pick_port()
    httpd = serve(app, port)
    url = "http://127.0.0.1:%d/%s" % (port, probe_name)
    dom = os.path.join(TMP, "lite_check_dom.html")
    errf = os.path.join(TMP, "lite_check_err.txt")
    print("[lite] %s（ui %s）→ %s" % (app, ver, url))
    try:
        with open(dom, "wb") as fh_dom, open(errf, "wb") as fh_err:
            args = [find_edge(), "--headless=new", "--disable-gpu", "--no-first-run",
                    "--no-default-browser-check", "--window-size=1400,920",
                    "--user-data-dir=" + os.path.join(TMP, "edge-lite-check-%d" % os.getpid()),
                    "--virtual-time-budget=15000"]
            if shot:
                args.append("--screenshot=" + shot)
            args += ["--dump-dom", url]
            proc = subprocess.Popen(args, stdout=fh_dom, stderr=fh_err)
            try:
                proc.wait(timeout=90)
            except subprocess.TimeoutExpired:
                proc.kill()
                print("  （Edge 超时，已强杀）")
    finally:
        httpd.shutdown()
        try:
            os.remove(probe_path)
        except OSError:
            pass

    text = io.open(dom, encoding="utf-8", errors="replace").read()
    mm = re.search(r'<pre id="__probe__">(.*?)</pre>', text, re.S)
    if not mm:
        print("[BAD] 没拿到探针结果（页面可能没起来）。DOM 前 300 字符：")
        print(text[:300])
        return 1

    d = json.loads(mm.group(1))
    ok = True
    print("\n=== 构建产物自检 ===")
    print("  __appLoaded :", d["appLoaded"])
    print("  lite 开关   :", d["lite"])
    print("  主题        :", d["theme"] or "(空)")
    print("  JS 错误     :", d["jsErrors"] or "[]")
    print("  横幅        :", d["banner"] or "(空)")
    sc = d.get("scroll") or {}
    if sc:
        over = sc.get("w", 0) > sc.get("cw", 0) + 1
        print("  视口        : %sx%s  内容 %sx%s  %s"
              % (sc.get("cw"), sc.get("ch"), sc.get("w"), sc.get("h"),
                 "[!] 横向溢出" if over else "OK"))
        if over and d.get("wide"):
            print("     最宽元素 :", d["wide"])
    if not d["appLoaded"]:
        ok = False
        print("  [BAD] __appLoaded 不是 true —— 启动半路挂了")
    if d["jsErrors"]:
        ok = False
        print("  [BAD] 有 JS 报错")
    print("  必须存在：")
    for k in MUST_EXIST:
        v = d["exists"].get(k)
        print("     [%s] %s" % ("OK " if v else "BAD", k))
        if not v:
            ok = False
    print("  必须消失（音乐 / 音频条）：")
    for k in MUST_BE_GONE:
        v = d["gone"].get(k)
        print("     [%s] %s" % ("OK " if not v else "BAD", k))
        if v:
            ok = False
    if shot:
        print("  截图        :", shot, os.path.isfile(shot))
    print("\n  result: %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
