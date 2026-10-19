# -*- coding: utf-8 -*-
r"""无头回归：播放条「显示状态属于用户意图」——刷新/自动续播都不许把它顶开。

背景（用户 2026-10-15 报）：点顶部横幅刷新面板时，列表下方的**音乐条会自己弹出来**。
根因是 boot → restoreMusicState() → musicPlay() 里无条件 `MUSIC.shown = true` 并落盘
（播放条本来是「选『音乐』才出现」）。修法：musicPlay 加 `keepShown` 选项 + 换 localStorage 键
（`cocraft.player.shown2`，旧键被 bug 污染过，不继承）。

做法与 history_page_test.py 同一套：由 frontend/index.html 生成探针页，插一段 STUB
（预置「上次在放的曲目」+ 拦掉长挂请求）再跑真 app.js，用两个场景各断言：

  A（上次**没**开着播放条，只是有歌在放）：
   1. 刷新恢复曲目后 → 播放条**不**自动出现（#player 无 .on）；
   2. 刷新不会把显示状态写成 "1"；
   3. 自动续播（musicNext(true)，歌放完自动下一首）也**不**顶开播放条；
   4. musicPlay 带 keepShown:true 时**不**顶开；
   5. 用户主动播放（没有 keepShown）**会**亮出播放条（「开始放歌时亮出」仍保留）；
   6. 全程零 JS 报错。

  B（用户上次**特意开着**播放条）：
   6. 刷新后播放条仍在（尊重用户选择）。

用法（需要面板服务已在 8787 运行 —— 只提供静态文件）：
    python tools\music_bar_test.py
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
PROBE = os.path.join(WEB, "_probe_bar.html")
BASE = "http://127.0.0.1:8787/_probe_bar.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


# 上次在放的曲目（3 首够测「自动续播 / 手动切歌」）
MUSIC_SAVED = {
    "v": 1, "provider": "netease",
    "list": [
        {"id": "1", "name": "load-a", "artists": "a", "album": ""},
        {"id": "2", "name": "load-b", "artists": "b", "album": ""},
        {"id": "3", "name": "load-c", "artists": "c", "album": ""},
    ],
    "playing": "1", "loop": "all", "shuffle": False, "queueName": "",
    "panel": {"id": "1", "name": "load-a", "artists": "a", "album": ""},
    "pos": 0, "wasPlaying": False,
}

# STUB：进页最先跑 —— 预置 localStorage + 假装已登录 + 拦掉长挂请求
STUB = r"""<script>
(function () {
  var scene = (location.search.indexOf("scene=B") >= 0) ? "B" : "A";
  try {
    localStorage.setItem("cocraft.token", "PROBE-TKN");
    localStorage.setItem("cocraft.setup.v1", "1");
    localStorage.setItem("cocraft.music", '__MUSIC__');
    localStorage.removeItem("cocraft.cc.unread");
    localStorage.removeItem("cocraft.users");
    // A：用户上次没开着播放条（键缺失）；B：用户特意开着
    if (scene === "B") localStorage.setItem("cocraft.player.shown2", "1");
    else localStorage.removeItem("cocraft.player.shown2");
  } catch (e) {}

  // 拦掉会惊动守护进程 / 卡死无头渲染 / 拖长虚拟时间的请求（音乐服务也拦掉，本测试不真播）
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event", "/music/", "/cocraft/"];
  var raw = window.fetch;
  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    return raw.apply(this, arguments);
  };

  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () {
    this.close = function () {}; this.addEventListener = function () {};
    this.onerror = null; this.onmessage = null;
  };
})();
</script>
"""

PROBE_A = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 400; i++) {
      if (window.__appLoaded && MUSIC && MUSIC.panel && String(MUS.playing) === "1") break;
      await __s(25);
    }
    await __s(250);
    var bar = document.getElementById("player");
    var ls = function () { try { return localStorage.getItem("cocraft.player.shown2"); } catch (e) { return null; } };
    out.info.boot = { on: bar.classList.contains("on"), shown: MUSIC.shown, ls: ls(),
                      listLen: (MUS.list || []).length, playing: MUS.playing,
                      musLs: (function () { try { return (localStorage.getItem("cocraft.music") || "").length; } catch (e) { return -1; } })() };

    step("★刷新恢复曲目后不自动打开播放条",
         !bar.classList.contains("on") && MUSIC.shown === false, JSON.stringify(out.info.boot));
    step("★刷新没有把显示状态写成 1（键仍为空）", ls() !== "1", "ls=" + ls());

    // ③ 自动续播（真实路径：歌放完自动下一首）→ 不顶开
    //     ⚠ 这里不断言「切到了哪首」：本测试把 /music/url 拦成失败，musicNext 会逐个试完
    //     三首后绕回第一首，playing 会回到 "1"。真正要锁的是「播放条别被顶开」。
    await musicNext(true);
    await __s(150);
    out.info.auto = { on: bar.classList.contains("on"), shown: MUSIC.shown, ls: ls() };
    step("★自动续播（musicNext(true)）不顶开播放条",
         !bar.classList.contains("on") && MUSIC.shown === false && ls() !== "1", JSON.stringify(out.info.auto));

    // ④ 自动续播内部用的就是 musicPlay(keepShown:true)：直接调一次，也不顶开
    await musicPlay("2", { play: true, keepShown: true });
    await __s(150);
    out.info.keep = { on: bar.classList.contains("on"), shown: MUSIC.shown, ls: ls() };
    step("★musicPlay(keepShown:true) 不顶开播放条",
         !bar.classList.contains("on") && MUSIC.shown === false && ls() !== "1", JSON.stringify(out.info.keep));

    // ⑤ 用户主动播放（默认，没有 keepShown）→ 亮出（「开始放歌时亮出」仍在）
    await musicPlay("3");
    await __s(150);
    out.info.manual = { on: bar.classList.contains("on"), shown: MUSIC.shown, ls: ls() };
    step("★用户主动播放会亮出播放条",
         bar.classList.contains("on") && ls() === "1", JSON.stringify(out.info.manual));
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

PROBE_B = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 400; i++) {
      if (window.__appLoaded && MUSIC && MUSIC.panel && String(MUS.playing) === "1") break;
      await __s(25);
    }
    await __s(250);
    var bar = document.getElementById("player");
    out.info.boot = { on: bar.classList.contains("on"), shown: MUSIC.shown,
                      ls: (function () { try { return localStorage.getItem("cocraft.player.shown2"); } catch (e) { return null; } })() };
    step("★用户上次开着 → 刷新后播放条仍在（尊重用户选择）",
         bar.classList.contains("on") && MUSIC.shown === true, JSON.stringify(out.info.boot));
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


def build_probe(probe_js: str) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    stub = STUB.replace("__MUSIC__", json.dumps(MUSIC_SAVED, ensure_ascii=False).replace("'", "\\'"))
    out = src.replace('<script src="/app.js', stub + probe_js + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str, url: str, udd: str) -> str:
    tmp = os.environ.get("TEMP", ".")
    # ⚠ 文件名**按场景区分**（以前 A/B 共用 `probe_bar_dom.html`：上一个 Edge 还攥着句柄时
    #   `os.remove` 会抛 WinError 32 → 整个测试误判失败，2026-10-16 踩到）。删不掉也不当失败。
    dom = os.path.join(tmp, "probe_bar_dom_%s.html" % udd)
    err = os.path.join(tmp, "probe_bar_err_%s.txt" % udd)
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
             "--user-data-dir=" + os.path.join(tmp, udd),
             "--window-size=1400,920",
             "--virtual-time-budget=40000", "--dump-dom", url],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def run_scene(edge: str, name: str, probe_js: str, udd: str) -> list:
    print("  场景 %s …" % name)
    build_probe(probe_js)
    try:
        html = render(edge, BASE + "?scene=" + name, udd)
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)
    m = re.search(r'<pre id="__probe__">(.*?)</pre>', html, re.S)
    if not m:
        print("  没拿到探针结果（渲染可能失败）。DOM 前 600 字符：")
        print(html[:600])
        return [("场景 " + name + " 没跑起来", False, "")]
    d = json.loads(m.group(1))
    if d.get("crash"):
        print("  探针异常:", d["crash"])
    return d


def main() -> int:
    edge = find_edge()
    print("探针页生成中，正在无头渲染（真跑 app.js，两个场景）…")
    results = []
    a = run_scene(edge, "A", PROBE_A, "edge-bartest-a")
    b = run_scene(edge, "B", PROBE_B, "edge-bartest-b")

    print("\n=== 播放条「显示状态 = 用户意图」无头回归 ===")
    for label, d in (("A 上次没开着", a), ("B 上次开着", b)):
        print("  [%s]" % label)
        for nm, ok, extra in d.get("steps", []):
            print("    %s %s%s" % ("OK  " if ok else "FAIL", nm, ("  [" + str(extra) + "]") if extra else ""))
        print("    JS 运行错误:", d.get("jsErrors"))
        results.append(d)

    bad = []
    for d in results:
        bad += [n for n, ok, _ in d.get("steps", []) if not ok]
    if any(d.get("crash") for d in results) or any(d.get("jsErrors") for d in results) or bad:
        print("\n  结论: [BAD] %s" % (bad or "有 JS 错误 / 探针异常"))
        return 2
    print("\n  结论: [OK] 刷新恢复曲目 / 自动续播都不顶开播放条，用户主动操作才改变；尊重用户上一次的开关")
    return 0


if __name__ == "__main__":
    sys.exit(main())
