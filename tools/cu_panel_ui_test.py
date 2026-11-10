# -*- coding: utf-8 -*-
r"""无头回归：★「通用工具（cu）」界面（`ui 0.2.75`）。

背景：`cu` 是给所有 agent 的"眼睛和手"（桌面窗口 + 浏览器 CDP，见 `backend/computer/`）。
agent 侧靠"命令进 PATH + 每轮 prompt 工具卡"就能用；**这个界面是给人看的**：
有哪些工具、每个是只读还是动作、怎么用、现在权限/租约/宿主是什么状态。

用 `frontend/index.html` 生成探针页，真跑 `app.js`，把 `/cu/status` 换成内存假数据，断言：

    1. 扳手菜单里有「通用工具（cu）」入口；弹窗默认收起
    2. 打开 → 4 个状态条（动作权限 / 桌面租约 / UIA 宿主 / 浏览器）
    3. 工具按分组列出（组数 / 条数 / 每组标题）
    4. 每条写清「名字 + 只读/动作 + 命令 + 说明」
    5. `eval` 那种危险工具要**标红警告**
    6. 点示例 → 复制 + 横幅回报；「复制全部示例」也能用
    7. 「刷新」会重新拉 `/cu/status`
    8. Esc 关掉弹窗
    9. 全程零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\cu_panel_ui_test.py
    python tools\cu_panel_ui_test.py --shot resources\screenshots\cu_panel.png
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
PROBE = os.path.join(WEB, "_probe_cu.html")
URL = "http://127.0.0.1:8787/_probe_cu.html"

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
  try { document.execCommand = function () { return true; }; } catch (e) {}

  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event",
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/fs/",
              "/ccagent", "/ws"];
  var raw = window.fetch;
  var T = 1760000000000;
  window.__calls = [];
  window.__cuHits = 0;

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var CAT = {
    caps: [
      { id: "observe", label: "观察", hint: "看清楚有什么、长什么样", count: 2 },
      { id: "act", label: "操作（模拟人）", hint: "像人一样动手：点击 / 输入", count: 3 },
      { id: "execute", label: "执行与取数", hint: "跑 JS / 直接调接口", count: 3 }
    ],
    surfaces: { desktop: "桌面", web: "网页", any: "通用" },
    tools: [
      { id: "windows", cap: "observe", surface: "desktop", surfaceLabel: "桌面",
        cmd: "cu windows", title: "列窗口", perm: "read", permLabel: "只读",
        desc: "列出可见窗口：hwnd、进程、标题、矩形。", example: "cu windows --title QQ", warn: "" },
      { id: "browser.text", cap: "observe", surface: "web", surfaceLabel: "网页",
        cmd: "cu browser text", title: "读页面文本", perm: "read", permLabel: "只读",
        desc: "读某个选择器的文本。", example: "cu browser text --selector h1", warn: "" },
      { id: "click", cap: "act", surface: "desktop", surfaceLabel: "桌面",
        cmd: "cu click", title: "点击控件", perm: "action", permLabel: "动作",
        desc: "点一个控件（UIA 模式优先，坐标兜底）。",
        example: "cu click --hwnd 12 --name 写信", warn: "" },
      { id: "clipboard-set", cap: "act", surface: "any", surfaceLabel: "通用",
        cmd: "cu clipboard-set", title: "写剪贴板", perm: "action", permLabel: "动作",
        desc: "把文本写进剪贴板。", example: "cu clipboard-set --text hi", warn: "" },
      { id: "browser.type", cap: "act", surface: "web", surfaceLabel: "网页",
        cmd: "cu browser type", title: "填写输入框", perm: "action", permLabel: "动作",
        desc: "往网页输入框打字。", example: "cu browser type --selector #q --text 周报", warn: "" },
      { id: "browser.eval", cap: "execute", surface: "web", surfaceLabel: "网页",
        cmd: "cu browser eval", title: "执行 JS", perm: "action", permLabel: "动作",
        desc: "在当前页跑一段 JS。", example: "cu browser eval --js \"document.title\"",
        warn: "等价于在这页里做任意操作（含调接口），所以按「动作」管：默认 deny" },
      { id: "http.get", cap: "execute", surface: "any", surfaceLabel: "通用",
        cmd: "cu http get", title: "调接口（读）", perm: "read", permLabel: "只读",
        desc: "直接发 GET：回 status / 头 / JSON（可带浏览器 cookie）。",
        example: "cu http get --url https://example.com/api/me", warn: "" },
      { id: "http.post", cap: "execute", surface: "any", surfaceLabel: "通用",
        cmd: "cu http post", title: "调接口（写）", perm: "action", permLabel: "动作",
        desc: "POST / PUT / PATCH / DELETE。",
        example: "cu http post --url https://example.com/api/todo --json {}",
        warn: "写请求会改远端状态；URL 与状态进审计，正文不进链" }
    ],
    perms: { read: "只读", action: "动作" },
    counts: { total: 8, read: 3, action: 5, byCap: { observe: 2, act: 3, execute: 3 } }
  };

  var SESS = { id: "ses_probe", title: "工具探针", time: { created: T, updated: T },
               location: { directory: "D:\\\\proj" },
               model: { id: "probe-1", providerID: "acp" } };

  window.fetch = function (u, o) {
    var s = String(u);
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;

    if (path === "/cu/status") {
      window.__cuHits++;
      return J({ ok: true, catalog: CAT,
                 gate: { ok: true, actions: (window.__cuActions || "deny"), allow_dangerous: false,
                         blacklist: 24, dangerous_words: 26,
                         config_file: "D:\\\\opencode-ui\\\\runtime\\\\state\\\\_cu.json",
                         config_exists: false, env: {} },
                 lease: { held: false },
                 uia: { running: false, mode: "lazy" },
                 browser: { running: false, port: 9333, headless: false, exe: "msedge.exe", started: "" } });
    }

    if (path === "/cu/config") {
      var body = {};
      try { body = JSON.parse((o && o.body) || "{}"); } catch (e) {}
      window.__cuCfg = body;
      window.__cuCfgPosts = (window.__cuCfgPosts || 0) + 1;
      if (body.actions === "allow" || body.actions === "deny") window.__cuActions = body.actions;
      return J({ ok: true, actions: (window.__cuActions || "deny"),
                 gate: { ok: true, actions: (window.__cuActions || "deny"),
                         allow_dangerous: false, blacklist: 24, dangerous_words: 26,
                         config_file: "D:\\\\opencode-ui\\\\runtime\\\\state\\\\_cu.json",
                         config_exists: true, env: {} },
                 file: "D:\\\\opencode-ui\\\\runtime\\\\state\\\\_cu.json" });
    }

    if (path === "/api/model") return J({ location: { directory: "D:\\\\proj" }, data: [] });
    if (path === "/api/model/default") return J({ data: null });

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
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
  window.WebSocket = function () { this.send = function () {}; this.close = function () {}; this.readyState = 1; };
  window.confirm = function () { return true; };
})();
</script>
"""

PROBE_JS = r"""<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __s(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 200; i++) { await __s(25); if (window.state) break; }
    await __s(250);

    var box = document.getElementById("cu");
    step("★扳手菜单里有「通用工具（cu）」入口", !!document.getElementById("tm-cu"),
         document.getElementById("tm-cu") ? "" : "(没有 tm-cu)");
    step("★弹窗默认收起", !!box && box.hidden);

    await cuOpen();
    await __s(300);
    step("★打开后弹窗可见", !!box && !box.hidden);

    var chips = document.querySelectorAll("#cu-chips .cu-chip");
    out.info.chips = __txt(document.getElementById("cu-chips"));
    step("★4 个状态条（权限 / 租约 / UIA 宿主 / 浏览器）", chips.length === 4, "chips=" + chips.length);
    step("★权限显示「需信任」、租约「空闲」、宿主「按需启动」、浏览器「未启动」",
         /动作权限/.test(out.info.chips) && /需信任/.test(out.info.chips)
         && /空闲/.test(out.info.chips) && /按需启动/.test(out.info.chips)
         && /未启动/.test(out.info.chips), out.info.chips);

    /* ★「允许桌面操作」开关（本轮新增） */
    var actBtn = document.getElementById("cu-actions");
    out.info.actBtn = __txt(actBtn);
    step("★有「允许桌面操作」开关，默认是「需信任」",
         !!actBtn && /需信任/.test(out.info.actBtn) && actBtn.getAttribute("aria-pressed") === "false",
         out.info.actBtn);
    if (actBtn) actBtn.click();
    await __s(350);
    step("★点开关 → POST /cu/config {actions:allow}", !!window.__cuCfg && window.__cuCfg.actions === "allow",
         JSON.stringify(window.__cuCfg || {}));
    step("★开关立刻变成「已允许」", actBtn && /已允许/.test(__txt(actBtn))
         && actBtn.getAttribute("aria-pressed") === "true", __txt(actBtn));
    out.info.chips2 = __txt(document.getElementById("cu-chips"));
    step("★状态条「动作权限」跟着变（允许）",
         /动作权限/.test(out.info.chips2) && /允许/.test(out.info.chips2), out.info.chips2);
    step("★开关不破坏工具列表（还是 8 条）",
         document.querySelectorAll("#cu-list .cu-item").length === 8,
         document.querySelectorAll("#cu-list .cu-item").length);
    if (actBtn) actBtn.click();
    await __s(350);
    step("★再点一下 → POST deny 并回到「需信任」",
         !!window.__cuCfg && window.__cuCfg.actions === "deny" && /需信任/.test(__txt(actBtn)),
         JSON.stringify(window.__cuCfg || {}) + " / " + __txt(actBtn));

    var items = document.querySelectorAll("#cu-list .cu-item");
    var groups = document.querySelectorAll("#cu-list .cu-group");
    out.info.items = items.length;
    step("★按「能力」分组：8 个工具 / 3 个能力组", items.length === 8 && groups.length === 3,
         "items=" + items.length + " groups=" + groups.length);
    step("★能力组标题写清名字 + 条数 + 提示",
         /观察/.test(__txt(groups[0])) && /2/.test(__txt(groups[0])) && /看清楚/.test(__txt(groups[0])),
         __txt(groups[0]));
    step("★按人的动作顺序排（观察 → 操作 → 执行）",
         /观察/.test(__txt(groups[0])) && /操作/.test(__txt(groups[1]))
         && /执行/.test(__txt(groups[2])),
         [__txt(groups[0]), __txt(groups[1]), __txt(groups[2])].join(" / "));

    var t0 = __txt(items[0]);
    out.info.row0 = t0;
    step("★每条写清「名字 + 平台 + 只读/动作 + 命令 + 说明」",
         /列窗口/.test(t0) && /桌面/.test(t0) && /只读/.test(t0)
         && /cu windows/.test(t0) && /hwnd/.test(t0), t0.slice(0, 160));

    var surf = document.querySelectorAll("#cu-list .cu-surface");
    out.info.surfaces = [].slice.call(surf).map(function (x) { return __txt(x); }).join(",");
    step("★平台只是标签（桌面 / 网页 / 通用 都在）",
         surf.length === 8 && /桌面/.test(out.info.surfaces)
         && /网页/.test(out.info.surfaces) && /通用/.test(out.info.surfaces), out.info.surfaces);

    out.info.reads = document.querySelectorAll("#cu-list .cu-perm.read").length;
    out.info.acts = document.querySelectorAll("#cu-list .cu-perm.action").length;
    step("★只读 / 动作 分别标对（3 / 5）",
         out.info.reads === 3 && out.info.acts === 5,
         "read=" + out.info.reads + " action=" + out.info.acts);

    var warns = document.querySelectorAll("#cu-list .cu-warn");
    out.info.warns = [].slice.call(warns).map(function (x) { return __txt(x); }).join(" | ");
    step("★危险工具标红警告（eval 任意操作 / 写请求改远端）",
         warns.length === 2 && /任意操作/.test(out.info.warns) && /改远端状态/.test(out.info.warns),
         out.info.warns);

    step("★底部统计写清总数", /共 8 个工具/.test(__txt(document.getElementById("cu-foot"))),
         __txt(document.getElementById("cu-foot")));

    /* 点示例 → 复制 + 横幅 */
    items[0].querySelector(".cu-ex").click();
    await __s(250);
    var bn = __txt(document.getElementById("banner"));
    step("★点示例 → 复制并在横幅回报命令", /已复制/.test(bn) && /cu windows/.test(bn), bn);

    /* 复制全部 */
    document.getElementById("cu-copy-all").click();
    await __s(250);
    var bn2 = __txt(document.getElementById("banner"));
    step("★「复制全部示例」也能用", /已复制/.test(bn2) && /全部示例/.test(bn2), bn2);

    /* 刷新重新拉 /cu/status */
    var before = window.__cuHits;
    document.getElementById("cu-reload").click();
    await __s(350);
    step("★「刷新」会重新拉 /cu/status", window.__cuHits > before,
         "before=" + before + " after=" + window.__cuHits);

    /* Esc 关闭（走 OVERLAY 栈顶） */
    window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
    await __s(200);
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
  #cu, #cu * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.state) break; }
  await __sh(200);
  await cuOpen();
  await __sh(350);
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
    dom = os.path.join(tmp, "probe_cu_dom.html")
    err = os.path.join(tmp, "probe_cu_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probecu"),
             "--window-size=1400,980",
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
    with open(os.path.join(tmp, "probe_cu_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probecushot"),
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
        shot = os.path.join(HERE, "resources", "screenshots", "cu_panel.png")
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
    print("\n=== 通用工具（cu）界面无头回归 ===")
    bad = 0
    for name, ok, extra in d.get("steps", []):
        print("  %s %s%s" % ("OK  " if ok else "MISS", name,
                             ("   [" + extra + "]") if extra and not ok else ""))
        if not ok:
            bad += 1
    if d.get("crash"):
        print("\n!! 探针自身抛异常：\n" + d["crash"])
        bad += 1
    errs = d.get("jsErrors") or []
    if errs:
        print("\n!! JS 报错：")
        for e in errs[:6]:
            print("   - " + str(e)[:300])
        bad += 1

    total = len(d.get("steps") or [])
    print("\n  total %d, failed %d" % (total, bad))
    print("  result:", "PASS" if bad == 0 else "FAIL")
    if d.get("info"):
        print("  info:", json.dumps(d["info"], ensure_ascii=False)[:400])
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
