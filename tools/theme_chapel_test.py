# -*- coding: utf-8 -*-
r"""★无头回归：官方主题「圣烛教堂」(chapel)

覆盖：
  1. 默认主题 = chapel（**新装 / 没存过**）
  2. 变量真的生效（--paper / --ink / --hi 是暗色那套）
  3. 按钮：主按钮走烛金渐变（白字压金对比不够 → 必须是深色字）、危险按钮保持红
  4. 静态背景槽跟着主题走（chapel ↔ hiru）
  5. 弹窗校徽换成「尖拱窗 + 烛焰」(.mk-chapel)，世界树在 chapel 下隐藏
  6. 设置里的两张内置主题卡：高亮当前 + 点一下切换（含 localStorage 持久化）
  7. 老用户横幅：以前存的是 hiru → 出一次「新增主题」横幅，点它就能换

用法：python tools\theme_chapel_test.py [--shot 路径.png]
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
TMP = os.environ.get("TEMP", ".")
PROBE = os.path.join(WEB, "_probe_theme_chapel.html")
URL = "http://127.0.0.1:8787/_probe_theme_chapel.html"
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

STUB = """<script>
(function () {
  // 老用户现场：以前存过 hiru、还没看过新主题提示 → 用来测「新增主题」横幅
  try {
    localStorage.setItem("cocraft.theme", "hiru");
    localStorage.removeItem("cocraft.themeSeen");
    localStorage.setItem("cocraft.setup.v1", "1");   // 别让首次向导挡住截图
  } catch (e) {}
  var raw = window.fetch;
  var DEAD = ["/heartbeat", "/bye", "/qq/", "/api/event", "/ccagent/", "/cocraft/"];
  window.fetch = function (u) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    var body = {};
    if (s.indexOf("/api/session") >= 0) body = { data: [], cursor: null };
    else if (s.indexOf("/api/model") >= 0) body = { data: [], location: {} };
    else if (s.indexOf("/api/agent") >= 0) body = { data: [], items: [] };
    else if (s.indexOf("/engine/status") >= 0) body = { engines: [], active: "opencode" };
    else if (s.indexOf("/themes") >= 0) body = { ok: true, builtin: [], installed: [], active: "chapel" };
    else body = { ok: true, data: [], items: [], runs: [], pending: [] };
    return Promise.resolve(new Response(JSON.stringify(body), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
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
function CSSV(name) {
  try { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); } catch (e) { return ""; }
}
function btnVar(el, name) {
  try { return getComputedStyle(el).getPropertyValue(name).trim(); } catch (e) { return ""; }
}
function b2x() { return document.getElementById("btn-stop"); }
function matchedColorRules(el) {
  var out = [];
  try {
    for (var i = 0; i < document.styleSheets.length; i++) {
      var sh = document.styleSheets[i], rules;
      try { rules = sh.cssRules; } catch (e) { continue; }
      for (var j = 0; j < rules.length; j++) {
        var r = rules[j];
        if (!r.selectorText || !/color/.test(r.cssText)) continue;
        var hit = false;
        try { hit = el.matches(r.selectorText); } catch (e) { hit = false; }
        if (hit) out.push((sh.href || "inline").split("/").pop() + " :: " + r.cssText.slice(0, 120));
      }
    }
  } catch (e) {}
  return out;
}
(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 200; i++) { await __s(25); if (window.CC && window.state && window.applyTheme) break; }
    await __s(300);
    // ★无头 + 虚拟时间里 **CSS transition 不会推进**（button 上有 `transition: all .13s`）
    //   → 改动后的计算值会停在"起点"（比如白字 / 旧圆角）。先禁用过渡再测，否则测的是动画中间态。
    try {
      var nt = document.createElement("style");
      nt.textContent = "*{transition:none!important;animation:none!important}";
      document.head.appendChild(nt);
    } catch (e) {}
    await __s(120);
    var root = document.documentElement;

    /* ---------- 1. 老用户现场：侧栏「新主题」提示出现（存的是 hiru） ---------- */
    var hint = document.getElementById("theme-hint");
    var hintTxt = hint ? hint.textContent.replace(/\s+/g, " ").trim() : "";
    out.info.oldUser = { theme: root.dataset.theme, hint: hintTxt, hidden: hint ? hint.hidden : null };
    step("★老用户（存着 hiru）→ 侧栏出现「新主题」提示",
         !!hint && !hint.hidden && /圣烛教堂/.test(hintTxt), hintTxt);
    step("★老用户不会被强行换主题（仍是 hiru）", root.dataset.theme === "hiru", root.dataset.theme);
    var go = hint && hint.querySelector("[data-theme-hint-go]");
    if (go) go.click();
    await __s(400);
    step("★点提示里的「换成圣烛教堂」→ 立刻切换", root.dataset.theme === "chapel", root.dataset.theme);
    step("★切完提示自己收起（不再打扰）", !!hint && hint.hidden, hint ? hint.hidden : null);

    /* ---------- 2. 默认主题 = chapel（新装 / 没存过） ---------- */
    try { localStorage.removeItem("cocraft.theme"); localStorage.removeItem("cocraft.themeSeen"); } catch (e) {}
    initTheme();
    await __s(400);
    out.info.fresh = { theme: root.dataset.theme, stored: localStorage.getItem("cocraft.theme") };
    step("★新装默认 = chapel（且写进 localStorage）",
         root.dataset.theme === "chapel" && localStorage.getItem("cocraft.theme") === "chapel",
         JSON.stringify(out.info.fresh));

    /* ---------- 3. 变量：暗色那套真的生效 ---------- */
    var v = { paper: CSSV("--paper"), ink: CSSV("--ink"), hi: CSSV("--hi"),
              gold: CSSV("--gold"), line: CSSV("--line"), tint: CSSV("--g-tint") };
    out.info.vars = v;
    step("★底盘变量是 chapel 的暖黑 / 米白（玻璃下 --paper 是 tint 半透明，纸面模式是 #17120f）",
         (/rgba\(30,\s*24,\s*19/.test(v.paper) || /17120f/i.test(v.paper))
         && /f6efe3/i.test(v.ink), JSON.stringify(v));
    step("★主强调是烛金 #e0a24a（不是原来的绯红 #c8102e）",
         /e0a24a/i.test(v.hi) && !/c8102e/i.test(v.hi), v.hi);
    step("★玻璃 tint 是暖棕黑 30,24,19", /30,\s*24,\s*19/.test(v.tint), v.tint);

    /* ---------- 4. 按钮：主按钮金渐变 + 深色字；危险按钮保持红 ---------- */
    var bn = document.getElementById("btn-new");
    var bnS = getComputedStyle(bn);
    out.info.primary = { img: bnS.backgroundImage, color: bnS.color,
                         theme: root.dataset.theme,
                         matches: bn.matches('[data-theme="chapel"] button.primary'),
                         radius: CSSV("--radius"),
                         bnHi: btnVar(bn, "--hi"), bnErr: btnVar(b2x(), "--err"),
                         inline: bn.getAttribute("style") || "",
                         cls: bn.className,
                         hover: bn.matches(":hover"),
                         disabled: bn.disabled,
                         cloneBody: (function () {
                           var c = bn.cloneNode(true); document.body.appendChild(c);
                           var v = getComputedStyle(c).color; c.remove(); return v;
                         })(),
                         cloneSide: (function () {
                           var c = bn.cloneNode(true); bn.parentElement.appendChild(c);
                           var v = getComputedStyle(c).color; c.remove(); return v;
                         })(),
                         styles: Array.prototype.map.call(document.querySelectorAll("style"),
                                                          function (s) { return (s.id || "?") + ":" + s.textContent.length; }),
                         count: document.querySelectorAll("#btn-new").length,
                         outer: bn.outerHTML.slice(0, 160),
                         parent: (bn.parentElement && (bn.parentElement.id || bn.parentElement.className)) || "",
                         rect: (function () { var r = bn.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; })(),
                         rules: matchedColorRules(bn) };
    step("★主按钮用烛金渐变（不是纯绯红实底）",
         /linear-gradient/.test(bnS.backgroundImage) && /224,\s*162,\s*74|224,162,74/.test(bnS.backgroundImage),
         bnS.backgroundImage.slice(0, 90));
    step("★主按钮字是深色（白字压金对比不够）",
         /rgb\(27,\s*20,\s*16\)|rgb\(23,\s*18,\s*15\)/.test(bnS.color), JSON.stringify(out.info.primary));
    var st = document.getElementById("btn-stop");
    var stS = getComputedStyle(st);
    out.info.danger = { color: stS.color, border: stS.borderColor };
    step("★危险按钮仍是红（安全语义不被主题弱化）",
         /255,\s*143,\s*136|224,\s*100,\s*92/.test(stS.color), stS.color);
    var ico = getComputedStyle(document.getElementById("btn-tools"));
    step("★图标按钮：金边 + 略尖的上圆角",
         /232,\s*200,\s*150|216,\s*189,\s*131/.test(ico.borderColor)
         && /8px 8px 6px/.test(ico.borderRadius),
         ico.borderColor + " / " + ico.borderRadius);

    /* ---------- 4b. 消息气泡：必须是「暗玻璃」，不能叠成白色蒙版 ---------- */
    //  气泡背景 = --paper = rgba(tint,.38)，叠到壁纸亮部（白袍 / 烛光）上会糊成浅灰白纱。
    //  chapel 下单独加厚到 .7+，否则白字压上去读不清（用户报过）。
    var fake = document.createElement("div");
    fake.className = "msg assistant";
    fake.innerHTML = '<div class="bubble"><p>测试</p></div>';
    (document.getElementById("messages") || document.body).appendChild(fake);
    await __s(80);
    var bub = fake.querySelector(".bubble");
    var bubS = getComputedStyle(bub);
    var mBub = /rgba?\(([^)]+)\)/.exec(bubS.backgroundColor) || [];
    var parts = (mBub[1] || "").split(",").map(function (x) { return parseFloat(x); });
    var alpha = parts.length === 4 ? parts[3] : 1;
    var luma = (0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]) || 999;
    out.info.bubble = { bg: bubS.backgroundColor, alpha: alpha, luma: luma };
    step("★消息气泡是**暗玻璃**（不是浅白蒙版：底色暗 + 不透明度 ≥ .6）",
         luma < 60 && alpha >= 0.6, JSON.stringify(out.info.bubble));
    fake.remove();

    /* ---------- 5. 静态背景槽 + 校徽 ---------- */
    var sbs = Array.prototype.slice.call(document.querySelectorAll("#static-bg .sb"));
    var onBg = sbs.filter(function (el) { return el.classList.contains("on"); })
                  .map(function (el) { return el.dataset.bg; });
    out.info.bg = onBg;
    step("★静态背景切到 chapel 槽（hiru 槽让位）",
         onBg.length === 1 && onBg[0] === "chapel", JSON.stringify(onBg));

    /* ---------- 5b. 全屏白纱（#live-bg .lb-wash）在 chapel 下必须换成暗纱 ---------- */
    //  那张 `rgba(250,247,242,.34~.58)` 的白纱 z-index 3 盖在**整屏**视频/壁纸上；
    //  以前只藏 hiru → chapel 下整块壁纸被糊成灰白（用户报「全部的白色蒙版都不要」）。
    var wash = document.querySelector("#live-bg .lb-wash");
    out.info.wash = {
      bg: wash ? getComputedStyle(wash).backgroundImage.slice(0, 120) : "no-node",
      hasPaper: wash ? /250,\s*247,\s*242/.test(getComputedStyle(wash).backgroundImage) : null,
    };
    step("★全屏蒙层不是纸白纱（chapel 下 .lb-wash 换暖黑暗纱）",
         !!wash && !/250,\s*247,\s*242/.test(getComputedStyle(wash).backgroundImage),
         JSON.stringify(out.info.wash));
    var mkT = document.querySelector(".mk-tree");
    var mkC = document.querySelector(".mk-chapel");
    out.info.mk = { tree: mkT && getComputedStyle(mkT).display, chapel: mkC && getComputedStyle(mkC).display };
    step("★弹窗校徽换成尖拱窗（世界树隐藏 / mk-chapel 显示）",
         mkT && mkC && getComputedStyle(mkT).display === "none"
         && getComputedStyle(mkC).display !== "none", JSON.stringify(out.info.mk));

    /* ---------- 6. 设置卡片：高亮 + 切换 + 持久化 ---------- */
    var box = document.getElementById("cfg-theme-builtin");
    var btnC = box && box.querySelector('[data-theme-pick="chapel"]');
    var btnH = box && box.querySelector('[data-theme-pick="hiru"]');
    step("★设置里有「圣烛教堂 / 和纸白昼」两张内置主题卡",
         !!btnC && !!btnH, box ? box.textContent.replace(/\s+/g, " ").trim() : "没有容器");
    step("★当前主题高亮在「圣烛教堂」上",
         btnC && btnC.classList.contains("on") && !(btnH && btnH.classList.contains("on")));
    btnH.click();
    await __s(350);
    var afterH = { theme: root.dataset.theme, paper: CSSV("--paper"),
                   stored: localStorage.getItem("cocraft.theme"),
                   onBg: Array.prototype.slice.call(document.querySelectorAll("#static-bg .sb.on"))
                         .map(function (el) { return el.dataset.bg; }),
                   primary: getComputedStyle(bn).backgroundImage };
    out.info.hiru = afterH;
    step("★点「和纸白昼」→ 真的切回浅色（变量 / 背景槽 / 持久化）",
         afterH.theme === "hiru"
         && (/faf7f2/i.test(afterH.paper) || /rgba\(128,\s*128,\s*128/.test(afterH.paper))
         && afterH.stored === "hiru" && afterH.onBg[0] === "hiru", JSON.stringify(afterH));
    step("★切回浅色后主按钮回到绯红实底（不是金渐变）",
         afterH.primary === "none" || !/224,\s*162,\s*74/.test(afterH.primary), afterH.primary);
    btnC.click();
    await __s(350);
    step("★再点「圣烛教堂」→ 回到 chapel", root.dataset.theme === "chapel", root.dataset.theme);

    /* ---------- 7. 不崩：JS 无报错 ---------- */
    step("探针无 JS 报错", window.__probeErr.length === 0, JSON.stringify(window.__probeErr));
  } catch (err) {
    out.crash = String(err && err.stack ? err.stack : err);
  }
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""


def build_probe() -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + PROBE_JS + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def find_edge() -> str:
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge: str, shot: str = "") -> str:
    dom = os.path.join(TMP, "probe_theme_chapel_dom.html")
    err = os.path.join(TMP, "probe_theme_chapel_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    # ★每次都用一个**全新的 profile**：旧 profile 里存着 cocraft.theme=hiru，
    #   会让"新装默认"测不出来（inspect_ui 用的是固定 profile，踩过）
    profile = os.path.join(TMP, "edge-theme-chapel-%d" % os.getpid())
    if os.path.isdir(profile):
        import shutil
        shutil.rmtree(profile, ignore_errors=True)
    args = [edge, "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--user-data-dir=" + profile,
            "--window-size=1400,920", "--virtual-time-budget=25000"]
    if shot:
        args.append("--screenshot=" + shot)
    args += ["--dump-dom", URL]
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(args, stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    shot = ""
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        if i + 1 < len(sys.argv):
            shot = os.path.abspath(sys.argv[i + 1])
    build_probe()
    edge = find_edge()
    try:
        dom = render(edge, shot)
    finally:
        # 探针页写在 frontend/ 里（要走 http，绝对路径的 /js/* 才解析得对）；
        # 跑完必须删掉，否则会被 build.py 整目录复制进安装包。
        try:
            if os.path.isfile(PROBE):
                os.remove(PROBE)
        except OSError:
            pass
    if "__probe__" not in dom:
        print("[BAD] 探针没有产出结果（页面可能没起来；先确认 8787 在跑）")
        return 1
    raw = dom.split('id="__probe__">', 1)[1].split("</pre>", 1)[0]
    import html as _html
    data = json.loads(_html.unescape(raw))
    ok = 0
    bad = []
    for name, passed, extra in data.get("steps", []):
        print("  %s %s%s" % ("OK  " if passed else "FAIL", name,
                             ("  [" + str(extra)[:160] + "]") if (extra and not passed) else ""))
        if passed:
            ok += 1
        else:
            bad.append(name)
    if data.get("crash"):
        print("  [CRASH] %s" % data["crash"][:600])
    if "--debug" in sys.argv:
        print(json.dumps(data.get("info"), ensure_ascii=False, indent=2)[:4000])
    if data.get("jsErrors"):
        print("  JS 报错: %s" % data["jsErrors"])
    print("\n  total %d, failed %d" % (ok + len(bad), len(bad)))
    if shot:
        print("  截图: %s 存在=%s" % (shot, os.path.isfile(shot)))
    print("  result: %s" % ("PASS" if not bad and not data.get("crash") else "FAIL"))
    return 0 if (not bad and not data.get("crash")) else 1


if __name__ == "__main__":
    sys.exit(main())
