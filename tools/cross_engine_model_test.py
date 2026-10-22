# -*- coding: utf-8 -*-
r"""无头回归：★「切引擎把 ACP 的模型写进 OpenCode」—— 2026-10-20 的那个严重事故。

事故现场（真数据）：
  ACP 下建的会话里模型是 `{id:"deepseek/deepseek-flash", providerID:"acp"}`
  （ACP 注册表没配 provider → 后端兜底成中性 "acp"）。切到 opencode 引擎后：
    ① `MODEL.list` 还是 ACP 那份 → `repairStaleModel()` 把合法的 opencode 引用判成"失效"；
    ② `normalizeModelRef()` 只比 id、不比 providerID → 把 `providerID:"acp"` 原样吐出来；
    ③ `writeSessionModel()` 照单全收 POST → OpenCode `ModelUnavailableError`
       → 那一轮 17ms 就 idle，提问**没有任何回复**。

这一套断言（真跑 app.js，stub 掉 fetch）：
  1. 切引擎后 `MODEL.engine` 必须变成新引擎，且**写模型之前**清单已经换好；
  2. 全程**不得**出现 `providerID:"acp"` 的 `POST /api/session/{id}/model`；
  3. 修复路径要么写当前引擎里合法的模型，要么**不写**；
  4. 清单不属于当前引擎时 `normalizeModelRef()` 返回 null（不猜）；
  5. 清单不属于当前引擎时 `modelAvailable()` 不误杀；
  6. `resolveDefaultModel()` 不会直接吐回上一个引擎存下的引用；
  7. 顶栏不会把"别的引擎的模型"当权威展示（`modelFull().engineForeign`）；
  8. 直接调 `writeSessionModel()` 写非法引用 → 拒绝，且不发 POST。

跑法：python tools\cross_engine_model_test.py
"""

import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_xmodel.html")
URL = "http://127.0.0.1:8787/_probe_xmodel.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = r"""<script>
(function () {
  try { localStorage.setItem("cocraft.setup.v1", "1"); } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/panel/notify", "/api/event", "/update/",
              "/appearance", "/live/", "/qq/", "/pty", "/fs/", "/ccagent/", "/orchestrate/"];
  var raw = window.fetch;
  function J(o, st) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: st || 200, headers: { "Content-Type": "application/json" } }));
  }

  window.__ENGINE = "acp";
  window.__LIST = {
    acp: [{ id: "deepseek/deepseek-flash", modelID: "deepseek/deepseek-flash",
            providerID: "acp", name: "DeepSeek Flash (ACP)", variants: [], cost: null,
            capabilities: { tools: true } }],
    opencode: [{ id: "deepseek-v4.1-flash-free", modelID: "deepseek-v4.1-flash-free",
                 providerID: "opencode", name: "DeepSeek V4.1 Flash (free)",
                 limit: { context: 1000000, output: 393216 }, variants: [], cost: null,
                 capabilities: { tools: true } }]
  };
  window.__ACP_REF = { id: "deepseek/deepseek-flash", providerID: "acp" };
  window.__OC_REF = { id: "deepseek-v4.1-flash-free", providerID: "opencode" };
  window.__SESSION_MODEL = Object.assign({}, window.__ACP_REF);
  window.__WRITES = [];
  window.__slowModel = 0;              // 模拟 ACP 的 /api/model 很慢（resume 最近会话）

  window.fetch = function (u, o) {
    var s = String(u), qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    var method = String((o && o.method) || "GET").toUpperCase();
    if (path === "/engine" && method === "POST") {
      try { window.__ENGINE = JSON.parse((o && o.body) || "{}").engine || window.__ENGINE; } catch (e) {}
      return J({ ok: true, engine: window.__ENGINE });
    }
    if (path === "/engine/status") {
      return J({ ok: true, active: window.__ENGINE, available: ["opencode", "acp"],
                 engines: [{ id: "opencode", label: "OpenCode", available: true },
                           { id: "acp", label: "ACP", available: true }] });
    }
    if (path === "/api/model") {
      var delay = Number(window.__slowModel || 0);
      if (delay > 0) {
        return new Promise(function (res) {
          setTimeout(function () {
            res(new Response(JSON.stringify({ data: window.__LIST[window.__ENGINE] || [] }), {
              status: 200, headers: { "Content-Type": "application/json" } }));
          }, delay);
        });
      }
      return J({ data: window.__LIST[window.__ENGINE] || [] });
    }
    if (path === "/api/model/default") return J({ data: window.__OC_REF });
    if (/^\/api\/session\/[^/]+\/model$/.test(path) && method === "POST") {
      var ref = null;
      try { ref = (JSON.parse((o && o.body) || "{}") || {}).model; } catch (e) {}
      window.__WRITES.push({ ref: ref, engine: window.__ENGINE,
                             listEngine: (window.__MODEL || {}).engine || "" });
      window.__SESSION_MODEL = ref || window.__SESSION_MODEL;
      return Promise.resolve(new Response(null, { status: 204 }));
    }
    if (/^\/api\/session\/[^/]+\/message$/.test(path)) return J({ data: [], cursor: null });
    if (/^\/api\/session\/[^/]+$/.test(path)) {
      return J({ data: { id: "s1", title: "t", location: { directory: "D:/ws" },
                         model: window.__SESSION_MODEL } });
    }
    if (path === "/api/session") {
      return J({ data: [{ id: "s1", title: "t", location: { directory: "D:/ws" },
                          model: window.__SESSION_MODEL }], cursor: null });
    }
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

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 240; i++) { await __s(25); if (window.__appLoaded && window.CC && window.state) break; }
    await __s(300);
    window.__MODEL = MODEL;            // 让 stub 记下"写入时清单属于哪个引擎"

    /* ---------- 0. 先站在 ACP 上：清单属于 acp，会话模型是 ACP 的中性引用 ---------- */
    window.__ENGINE = "acp";
    ENGINE.active = "acp";
    resetModels("acp");
    await loadModels();
    state.current = { id: "s1", title: "t", model: Object.assign({}, window.__ACP_REF) };
    window.__SESSION_MODEL = Object.assign({}, window.__ACP_REF);
    window.__WRITES.length = 0;
    step("★ACP 清单加载后带引擎标记", MODEL.engine === "acp", "engine=" + MODEL.engine);
    step("ACP 下这条中性引用是「可用」的", modelAvailable(window.__ACP_REF) === true);

    /* ---------- 1. 切引擎：清单必须**在写模型之前**换成 opencode 的 ---------- */
    await setEngine("opencode");
    // ★ 模型清单现在是**后台重拉**（ACP 拿清单要 resume/冷启动，秒级）—— 这里等它落地再断言
    for (var wi = 0; wi < 80; wi++) { if (MODEL.engine === "opencode") break; await __s(50); }
    await __s(600);                       // repairStaleModel 是异步的（openSession 不 await 它）

    out.info.writes = window.__WRITES;
    step("★切引擎后 ENGINE.active = opencode", ENGINE.active === "opencode", ENGINE.active);
    step("★切引擎后模型清单跟着换（MODEL.engine = opencode）",
         MODEL.engine === "opencode", "MODEL.engine=" + MODEL.engine);

    var bad = window.__WRITES.filter(function (w) {
      return !w.ref || String(w.ref.providerID) === "acp";
    });
    step("★★绝不把 providerID=acp 的模型写进 opencode 会话", bad.length === 0,
         JSON.stringify(bad));
    var wrongList = window.__WRITES.filter(function (w) { return w.listEngine !== "opencode"; });
    step("★每次写之前清单都已经是当前引擎的", wrongList.length === 0,
         JSON.stringify(wrongList));
    var legal = window.__WRITES.filter(function (w) {
      return w.ref && w.ref.providerID === "opencode" && w.ref.id === "deepseek-v4.1-flash-free";
    });
    step("★修复路径写的是当前引擎里真实存在的模型", legal.length >= 1,
         JSON.stringify(window.__WRITES));
    step("★会话模型已经被改回合法引用",
         state.current && state.current.model && state.current.model.providerID === "opencode",
         JSON.stringify(state.current && state.current.model));

    /* ---------- 2. 直接写非法引用 → 拒绝，且不发 POST ---------- */
    var before = window.__WRITES.length;
    var r = await writeSessionModel("s1", Object.assign({}, window.__ACP_REF));
    step("★writeSessionModel 拒绝非法引用（返回 null）", r === null, String(r));
    step("★拒绝时**没有**发 POST", window.__WRITES.length === before,
         "writes=" + window.__WRITES.length);

    /* ---------- 3. 清单不属于当前引擎 → 不猜 ---------- */
    var keep = MODEL.engine;
    MODEL.engine = "acp";                 // 模拟"旧引擎的清单还留着"
    state.current.model = Object.assign({}, window.__ACP_REF);
    step("★清单属于别的引擎 → normalizeModelRef 返回 null（不猜）",
         normalizeModelRef(window.__ACP_REF) === null, String(normalizeModelRef(window.__ACP_REF)));
    step("★清单属于别的引擎 → modelAvailable 不误杀（返回 true）",
         modelAvailable(window.__ACP_REF) === true);
    MODEL.engine = keep;

    /* ---------- 4. 默认模型不吃"上一个引擎留下的引用" ---------- */
    try { localStorage.setItem("cocraft.model", JSON.stringify(window.__ACP_REF)); } catch (e) {}
    resetModels("opencode");
    await loadModels();
    var dref = await resolveDefaultModel();
    step("★本地存的 ACP 引用不会被当成默认模型", !!dref && dref.providerID === "opencode",
         JSON.stringify(dref));

    /* ---------- 5. 顶栏别把别的引擎的模型当权威展示 ---------- */
    var full = modelFull(Object.assign({}, window.__ACP_REF));
    step("★当前引擎清单里没有的引用 → 标记 engineForeign",
         !!(full && full.engineForeign), JSON.stringify(full));

    /* ---------- 6. 撤销"切到 acp"再切回来（反向也要安全） ---------- */
    window.__WRITES.length = 0;
    window.__SESSION_MODEL = Object.assign({}, window.__OC_REF);
    state.current.model = Object.assign({}, window.__OC_REF);
    await setEngine("acp");
    for (var wj = 0; wj < 80; wj++) { if (MODEL.engine === "acp") break; await __s(50); }
    await __s(700);
    var bad2 = window.__WRITES.filter(function (w) {
      return w.ref && String(w.ref.providerID) === "opencode";
    });
    step("★反向切（opencode → acp）同样不会把 opencode 引用写进 ACP",
         bad2.length === 0, JSON.stringify(bad2));

    /* ---------- 7. 切换不许被模型清单拖住（ACP 拿清单是秒级） ---------- */
    window.__slowModel = 900;            // 模拟 ensure_model_config → resume 最近会话
    window.__WRITES.length = 0;
    var t0 = Date.now();
    await setEngine("opencode");
    var dur = Date.now() - t0;
    step("★模型清单很慢时，切引擎也不等它（立即返回、清单仍为空）",
         dur < 600 && !(MODEL.list && MODEL.list.length),
         "dur=" + dur + "ms, list=" + (MODEL.list ? MODEL.list.length : null));

    /* ---------- 8. 切 ACP agent 同样不等模型清单（用户实测的卡顿点） ---------- */
    window.__slowModel = 900;
    ENGINE.active = "acp";
    resetModels("acp");
    var t1 = Date.now();
    await resetAfterAgentSwitch("切换到假 agent");
    var dur2 = Date.now() - t1;
    step("★切 ACP agent 也不等模型清单（立即返回）",
         dur2 < 600 && !(MODEL.list && MODEL.list.length),
         "dur=" + dur2 + "ms, list=" + (MODEL.list ? MODEL.list.length : null));
    window.__slowModel = 0;
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


def render(edge: str) -> str:
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_xmodel_dom.html")
    err = os.path.join(tmp, "probe_xmodel_err.txt")
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
             "--user-data-dir=" + os.path.join(tmp, "edge-probexmodel"),
             "--window-size=1400,920",
             "--virtual-time-budget=30000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    edge = find_edge()
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

    print("\n=== 跨引擎模型引用 · 无头回归 ===")
    fails = 0
    for name, okv, extra in d.get("steps", []):
        print(("  OK   " if okv else "  BAD  ") + name
              + (("   [" + extra[:220] + "]") if extra else ""))
        if not okv:
            fails += 1
    if d.get("crash"):
        print("\n  [BAD] 探针崩了：\n" + d["crash"])
        fails += 1
    errs = d.get("jsErrors") or []
    if errs:
        print("\n  JS 报错:", errs)
        fails += 1
    print("\n  total %d, failed %d" % (len(d.get("steps", [])), fails))
    print("  result:", "PASS" if fails == 0 else "FAIL")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
