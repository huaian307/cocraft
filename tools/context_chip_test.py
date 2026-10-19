# -*- coding: utf-8 -*-
r"""无头回归：★上下文长度显示（「还剩多少」）—— 引擎无关。

覆盖：
  * OpenCode：模型 `limit.context` + 上一条 assistant 的 `tokens`（input + cache.read/write）
  * ACP：agent 上报的 `context {used,size}` / 事件 `session.usage.update`
  * 只有 used、没有窗口上限 → 显示已用量并说明「上限未知」
  * 什么都没有 → 「上下文 —」+ 说明（不编数字）
  * 快满（>=80% 金 / >=95% 红）、点一下出详细、没会话时隐藏

跑法（需要 8787 在跑，只用它发静态文件）：
    python tools\context_chip_test.py
    python tools\context_chip_test.py --shot resources\screenshots\context_chip.png
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
PROBE = os.path.join(WEB, "_probe_ctx.html")
URL = "http://127.0.0.1:8787/_probe_ctx.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/engine/", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }
  window.MODELS = [{ id: "deepseek-flash", modelID: "deepseek-flash", providerID: "deepseek",
                     name: "DeepSeek Flash", limit: { context: 1000000, output: 393216 },
                     variants: [], cost: null, capabilities: { tools: true } }];
  window.ASSIST = { id: "m1", type: "assistant",
                    model: { id: "deepseek-flash", providerID: "deepseek" },
                    time: { created: Date.now() - 1000, completed: Date.now() },
                    content: [{ type: "text", text: "ok" }],
                    tokens: { input: 210, output: 360, reasoning: 113,
                              cache: { read: 499584, write: 0 } } };
  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/model") return J({ data: window.MODELS, location: { directory: "D:/ws" } });
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) return J({ data: [window.ASSIST], cursor: null });
    if (/^\/api\/session\/[^/]+$/.test(path)) return J({ data: { id: "s1", title: "t",
      model: { id: "deepseek-flash", providerID: "deepseek" } } });
    if (path === "/api/session") return J({ data: [], cursor: null });
    if (path === "/api/skill" || path === "/api/command") return J({ data: [] });
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
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
function __txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 200; i++) { await __s(25); if (window.CC && window.state && window.MODEL) break; }
    await __s(200);
    var chip = document.getElementById("ctx-chip");

    /* ---------- 0. 没会话：隐藏 ---------- */
    state.current = null; state.messages = [];
    ctxSync();
    step("★没选会话 → 上下文条隐藏", !!chip && chip.hidden);

    /* ---------- 1. OpenCode：模型上限 + 上一条回答的 tokens ---------- */
    state.current = { id: "s1", title: "t", model: { id: "deepseek-flash", providerID: "deepseek" } };
    state.messages = [Object.assign({}, window.ASSIST)];
    await loadModels();
    ctxSync();
    out.info.t1 = __txt(chip);
    step("★OpenCode：算出「剩 50%」", /上下文 剩 50%/.test(__txt(chip)), __txt(chip));
    step("★tooltip 写清已用 / 上限 / 来源",
         /已用 50 万 \/ 100 万/.test(chip.title)
         && /来源：模型窗口上限/.test(chip.title), chip.title.replace(/\n/g, " | "));

    /* ---------- 2. 快满：金 / 红 ---------- */
    window.ASSIST.tokens = { input: 1000, cache: { read: 849000, write: 0 } };
    state.messages = [Object.assign({}, window.ASSIST)];
    ctxSync();
    step("★用到 85% → 金色 warn", /剩 15%/.test(__txt(chip)) && chip.classList.contains("warn"),
         __txt(chip) + " cls=" + chip.className);
    window.ASSIST.tokens = { input: 1000, cache: { read: 969000, write: 0 } };
    state.messages = [Object.assign({}, window.ASSIST)];
    ctxSync();
    step("★用到 97% → 红色 danger", /剩 3%/.test(__txt(chip)) && chip.classList.contains("danger"),
         __txt(chip) + " cls=" + chip.className);

    /* ---------- 3. ACP：agent 上报的 context ---------- */
    state.current = { id: "s1", title: "t", model: { id: "deepseek-flash" },
                      context: { used: 500000, size: 1000000 } };
    state.messages = [];
    ctxSync();
    step("★ACP：用 session.context（剩 50%）", /剩 50%/.test(__txt(chip)), __txt(chip));
    step("★tooltip 标「agent 上报」", /来源：agent 上报的 usage_update/.test(chip.title), chip.title);

    /* ---------- 4. 事件：session.usage.update 实时更新 ---------- */
    state.current.context = null;
    handleEvent({ type: "session.usage.update",
                  data: { sessionID: "s1", used: 960000, size: 1000000 } });
    await __s(80);
    step("★usage_update 事件 → 立刻变「剩 4%」+ 红",
         /剩 4%/.test(__txt(chip)) && chip.classList.contains("danger")
         && state.current.context && state.current.context.used === 960000,
         __txt(chip) + " ctx=" + JSON.stringify(state.current.context));

    /* ---------- 5. 只有已用、没有窗口上限 ---------- */
    state.current = { id: "s1", title: "t", model: { id: "custom-model" } };
    state.messages = [{ type: "assistant", tokens: { input: 12000, cache: { read: 0, write: 0 } } }];
    ctxSync();
    step("★只有 used → 显示已用量（1.2 万），不编百分比",
         /上下文 1\.2 万/.test(__txt(chip)) && !/剩/.test(__txt(chip)), __txt(chip));
    step("★并说明「窗口上限未知」", /窗口上限未知/.test(chip.title), chip.title);

    /* ---------- 6. 什么都没有 → — + 说明（点一下出横幅） ---------- */
    state.current = { id: "s1", title: "t", model: { id: "dsh" } };
    state.messages = [];
    ctxSync();
    step("★没有数据 → 「上下文 —」", /上下文 —/.test(__txt(chip)), __txt(chip));
    step("★tooltip 说明为什么（agent 没上报）",
         /这个 agent 还没上报/.test(chip.title), chip.title);
    chip.click();
    await __s(150);
    step("★点「上下文 —」→ 横幅说明（不假装有数据）",
         /还没上报上下文用量/.test(__txt(document.getElementById("banner"))),
         __txt(document.getElementById("banner")));

    /* ---------- 7. 回正常 → 点一下出详细 ---------- */
    state.current = { id: "s1", title: "t", model: { id: "deepseek-flash", providerID: "deepseek" } };
    state.messages = [Object.assign({}, window.ASSIST, { tokens: { input: 210, cache: { read: 499584, write: 0 } } })];
    ctxSync();
    chip.click();
    await __s(150);
    step("★点一下 → 横幅给出「已用 … / … tokens · 剩余 …%」",
         /已用 50 万 \/ 100 万 tokens/.test(__txt(document.getElementById("banner")))
         && /剩余 50%/.test(__txt(document.getElementById("banner"))),
         __txt(document.getElementById("banner")));
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
  #ctx-chip, #ctx-chip * { transition: none !important; animation: none !important; }
</style>
<script>
function __sh(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
(async function () {
  for (var i = 0; i < 200; i++) { await __sh(25); if (window.CC && window.state) break; }
  await __sh(250);
  state.current = { id: "s1", title: "t", model: { id: "deepseek-flash", providerID: "deepseek" } };
  state.messages = [Object.assign({}, window.ASSIST, { tokens: { input: 210, cache: { read: 499584, write: 0 } } })];
  await loadModels();
  ctxSync();
  await __sh(350);
})();
</script>
"""


def build_probe(shot: bool = False) -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + (SHOT_JS if shot else PROBE_JS) + '<script src="/app.js')
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
    dom = os.path.join(tmp, "probe_ctx_dom.html")
    err = os.path.join(tmp, "probe_ctx_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probectx"),
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
    with open(os.path.join(tmp, "probe_ctx_shot_err.txt"), "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-probectxshot"),
             "--window-size=1400,980", "--virtual-time-budget=30000",
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
        i = sys.argv.index("--shot")
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", "context_chip.png")
        build_probe(shot=True)
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

    print("\n=== 上下文长度 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name + (("   [" + extra[:180] + "]") if extra else ""))
        if not okv:
            fails += 1
    if d.get("crash"):
        print("  脚本异常:", d["crash"])
        fails += 1
    print("\n  JS 报错:", d.get("jsErrors"))
    if d.get("jsErrors"):
        fails += 1
    print("\n  total %d, failed %d" % (len(d.get("steps", [])), fails))
    print("  result:", "PASS" if fails == 0 else "FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
