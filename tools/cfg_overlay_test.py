# -*- coding: utf-8 -*-
r"""无头回归：打开「居中弹层」时**先把设置抽屉收起来**，关掉再还原（`ui 0.2.84`）。

用户实测（2026-10-27，发图）：
  * 设置抽屉开着 → 点「＋ 添加凭据」→ 弹窗**压在抽屉上**（两层字叠字）；
  * 设置抽屉开着 → 权限 ▾ → 「更多…」→ 信任面板同样叠着。

真因：只有 `agentlist` / 主题工坊 / 向导 / 壁纸页**各自写了一句 `cfgClose()`**，
凭据弹窗、信任面板（还有模型 / 模式 / 技能 / 接续 / 隔离区 / 撤销 / 通用工具 / 新建会话…）**都漏了**。

修法（两处，一次覆盖全部）：
  * `OVERLAY.push/pop`：居中弹层登记时调 `cfgYieldToOverlay(id)`，关闭时 `cfgRestoreFromOverlay()`；
  * `11-engine.js`：`CFG_YIELD_OVERLAYS` 白名单 + **计数**（A→B 连开两层时只有最后一层关掉才还原）；
    `trace`（左侧独立页）/ `copy-fb`（剪贴板兜底）/ `editq`（提问放回输入框）**故意不收**。

断言：
  1. 设置开着 → `openCredEdit` → 设备收起 + 弹窗可见；关掉 → 设置**还原**
  2. 设置开着 → `trustOpen(true)` → 收起；`trustClose()` → 还原
  3. 走 **Esc（`OVERLAY.closeTop`）** 关信任面板 → 设置也还原（不能重复扣计数 / 不能漏还原）
  4. 设置**没开**时打开弹层 → 关掉后设置**仍然关着**（别乱开）
  5. `trace` 是例外：设置开着时打开轨迹**不收设置**
  6. 零 JS 报错

用法（需要面板服务在 8787：只用它的静态文件）：
    python tools\cfg_overlay_test.py
    python tools\cfg_overlay_test.py --shot resources\screenshots\cfg_overlay.png
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
PROBE = os.path.join(WEB, "_probe_cfgov.html")
URL = "http://127.0.0.1:8787/_probe_cfgov.html"

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
              "/engine/", "/update/", "/appearance", "/live/", "/pty", "/fs/",
              "/audit/", "/themes", "/panel/", "/acp/"];
  var raw = window.fetch;
  var T = 1760000000000;

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }

  var MSGS = [
    { id: "m1", type: "user", text: "随便问一句", time: { created: T } },
  ];
  var SESS = { id: "ses_probe", title: "设置叠弹层探针",
               time: { created: T, updated: T + 1000 },
               location: { directory: "D:\\\\proj" },
               model: { id: "probe-1", providerID: "acp" } };

  window.fetch = function (u, o) {
    var s = String(u);
    // ⚠ 模型清单必须先挡（回 `{}` 会让 MODEL.list.find 抛，整条 openSession 链断掉）
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ location: { directory: "D:\\\\proj" }, data: [] });
    if (path === "/api/model/default") return J({ data: null });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
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
function __vis(id) { var b = document.getElementById(id); return !!b && !b.hidden; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 300; i++) {
      await __s(25);
      if ((state.messages || []).length >= 1) break;
    }
    await __s(150);

    /* 1. 「＋ 添加凭据」：设置开着 → 收起弹窗前面那层；关掉 → 设置还原 */
    cfgOpen();
    await __s(30);
    var before = cfgIsOpen();
    await openCredEdit("");
    await __s(120);
    out.info.cred1 = "cfg=" + cfgIsOpen() + " credVis=" + __vis("cred");
    step("★点「＋ 添加凭据」→ 设置**先收起**（弹窗不再压在抽屉上）",
         before === true && cfgIsOpen() === false && __vis("cred") === true, out.info.cred1);
    closeCredEdit();
    await __s(60);
    out.info.cred2 = "cfg=" + cfgIsOpen() + " credVis=" + __vis("cred");
    step("★关掉凭据弹窗 → 设置**还原**（不用再点一次齿轮）",
         cfgIsOpen() === true && __vis("cred") === false, out.info.cred2);

    /* 2. 信任面板：同样收起 / 还原 */
    trustOpen(true);
    await __s(200);
    out.info.trust1 = "cfg=" + cfgIsOpen() + " trustVis=" + __vis("trust");
    step("★打开信任面板 → 设置**先收起**",
         cfgIsOpen() === false && __vis("trust") === true, out.info.trust1);
    trustClose();
    await __s(60);
    out.info.trust2 = "cfg=" + cfgIsOpen() + " trustVis=" + __vis("trust");
    step("★关掉信任面板 → 设置**还原**", cfgIsOpen() === true && __vis("trust") === false,
         out.info.trust2);

    /* 3. 走 Esc（OVERLAY.closeTop）那条路：不能漏还原、也不能重复扣计数 */
    trustOpen(true);
    await __s(200);
    var closed = OVERLAY.closeTop();
    await __s(80);
    out.info.esc = "closed=" + closed + " cfg=" + cfgIsOpen() + " trustVis=" + __vis("trust")
                 + " yield=" + (typeof CFG_YIELD === "number" ? CFG_YIELD : "?");
    step("★Esc 关信任面板 → 设置也还原（计数不重复扣）",
         closed === true && cfgIsOpen() === true && __vis("trust") === false && CFG_YIELD === 0,
         out.info.esc);

    /* 4. 设置没开时：开/关弹层不能凭空把设置打开 */
    cfgClose();
    await __s(60);
    await openCredEdit("");
    await __s(80);
    closeCredEdit();
    await __s(60);
    out.info.plain = "cfg=" + cfgIsOpen() + " yield=" + CFG_YIELD;
    step("★设置本来就关着 → 关掉弹层后**仍然关着**（别乱开）",
         cfgIsOpen() === false && CFG_YIELD === 0, out.info.plain);

    /* 5. 例外：轨迹页是左侧独立页，打开它**不收**设置 */
    cfgOpen();
    await __s(30);
    traceOpen();
    await __s(80);
    out.info.trace = "cfg=" + cfgIsOpen() + " traceOpen=" + !!TRACE.open;
    step("★例外：打开「轨迹」页**不收起**设置（左侧独立页，本就不打架）",
         cfgIsOpen() === true && TRACE.open === true, out.info.trace);
    traceClose();
    await __s(60);

    /* 收尾 */
    cfgClose();
    await __s(60);
    out.info.end = "cfg=" + cfgIsOpen() + " yield=" + CFG_YIELD + " stack=" + OVERLAY.stack.length;
    step("★收尾：设置关掉、计数归零、弹层栈清空",
         cfgIsOpen() === false && CFG_YIELD === 0 && OVERLAY.stack.length === 0, out.info.end);
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
  cfgOpen();
  await __sh(120);
  await openCredEdit("");          // 截图：设置已收起，弹窗干净地居中
  await __sh(400);
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
    dom = os.path.join(tmp, "probe_cfgov_dom.html")
    err = os.path.join(tmp, "probe_cfgov_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probecfgov"),
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
    with open(os.path.join(tmp, "probe_cfgov_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probecfgovshot"),
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
        shot = os.path.join(HERE, "resources", "screenshots", "cfg_overlay.png")
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
    print("\n=== 打开弹层先收起设置 · 无头回归 ===")
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
