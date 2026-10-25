# -*- coding: utf-8 -*-
r"""无头交互测试：真的在浏览器里切换「对话引擎」，并验证切换后重连了 SSE。

背景：S4 修过一个 bug —— 切引擎后没重新 connectEvents()，于是 REST 走新引擎、
事件流却还挂在旧引擎上。静态检查抓不到这种"时序/副作用" bug，所以这里真跑一遍：

  1. 起一个独立的、带新后端的服务（固定端口，不影响线上 8787）；
  2. 由 frontend/index.html 生成探针页：拦掉会长挂的请求（心跳/SSE/qq/form/permission），
     保留真实的 /engine/status、/engine、/api/session；
  3. 用计数器替换 window.EventSource —— 每 new 一次就 +1；
  4. 探针里开设置 → 读下拉 → setEngine("acp") → setEngine("opencode")，把结果塞进 <pre>；
  5. Edge 无头 --dump-dom 抓回来解析；
  6. 断言：下拉含两个引擎、切换后 active 正确、**每次切换 EventSource 计数都 +1**、无 JS 报错。

用法：python tools\acp_engine_ui_test.py
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(HERE, "backend")
WEB = os.path.join(HERE, "frontend")
STATE_DIR = os.path.join(HERE, "runtime", "state")
PROBE = os.path.join(WEB, "_probe_engine.html")
MOCK = os.path.join(BACKEND, "acp_mock_agent.py")
ACF_FILE = os.path.join(STATE_DIR, "_acp.json")
ENGINE_FILE = os.path.join(STATE_DIR, "_engine.json")

PORT = 8801
URL = "http://127.0.0.1:%d/_probe_engine.html" % PORT

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

# 拦掉会长挂 / 会惊动守护进程的请求；EventSource 换成计数器（用来数"重连了几次"）
STUB = """<script>
(function () {
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/live/",
              "/update/", "/wallpaper/", "/orchestrate/", "/audit/"];
  var raw = window.fetch;
  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.__esCount = 0;
  window.EventSource = function () { window.__esCount++; this.close = function () {}; };
  window.__probeErr = [];
  window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
  window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
})();
</script>
"""

TEST = """<script>
setTimeout(async function () {
  var out = {};
  var note = function () { var n = document.getElementById("cfg-engine-note"); return n ? n.textContent : null; };
  var wait = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var until = async function (fn, ms) {
    var t0 = Date.now();
    while (Date.now() - t0 < ms) { if (fn()) return true; await wait(150); }
    return !!fn();
  };
  try {
    var cfg = document.getElementById("btn-cfg");
    if (cfg) cfg.click();
    await wait(1200);
    var sel = document.getElementById("cfg-engine");
    out.options = [].slice.call(sel.options).map(function (o) { return o.value; });
    out.value = sel.value;
    out.disabled = sel.disabled;
    out.note0 = note();
    // 引擎自述（可用性）也一起验：新后端 /engine/status 会带 engines[]
    out.engines = (ENGINE.engines || []).map(function (e) {
      return { id: e.id, label: e.label, available: e.available };
    });
    // 纯函数：不可用的引擎要标「未就绪」并 disabled（中性化 UX，不用真造一个坏引擎）
    out.optHtml = engineOptionsHtml(["good", "bad"]) + engineOptionsHtml(["bad"]);
    ENGINE.engines = [{ id: "good", label: "可用引擎", available: true },
                      { id: "bad", label: "坏引擎", available: false }];
    out.optHtml2 = engineOptionsHtml(["good", "bad"]);
    ENGINE.engines = out.engines;
    // 先保证从 opencode 起步（CI 上可能已经是 acp → setEngine("acp") 什么都没做）
    if (ENGINE.active !== "opencode") {
      var b0 = window.__esCount;
      await setEngine("opencode");
      await until(function () { return ENGINE.active === "opencode" && window.__esCount > b0; }, 8000);
    }
    var before = window.__esCount;
    await setEngine("acp");
    // CI 上慢（POST /engine + 重连 SSE 可能 >1.2s）→ 轮询等它真的生效，别用固定 sleep 赌
    await until(function () { return ENGINE.active === "acp" && window.__esCount > before; }, 8000);
    out.acpActive = ENGINE.active;
    out.acpEsDelta = window.__esCount - before;
    out.acpNote = note();
    var before2 = window.__esCount;
    await setEngine("opencode");
    await until(function () { return ENGINE.active === "opencode" && window.__esCount > before2; }, 8000);
    out.openActive = ENGINE.active;
    out.openEsDelta = window.__esCount - before2;
    out.title = (document.getElementById("session-title") || {}).textContent;
    out.msgCount = (state.messages || []).length;

    // ---- agentlist 卡片 + 编辑弹窗（含 provider 中性化）----
    await setEngine("acp");
    await wait(600);
    openAgents();
    await wait(900);
    out.agentsRows = document.querySelectorAll("#agents-list .agents-row[data-aid]").length;
    out.agentsProviders = (ACP.list || []).map(function (a) {
      return { id: a.id, provider: a.provider || "", guess: a.providerGuess || "" };
    });
    var first = (ACP.list || [])[0];
    editAgentCommand(first.id);
    await wait(200);
    out.editOpen = !document.getElementById("agent-edit").hidden;
    out.editLabel = document.getElementById("ae-label").value;
    out.editCommand = document.getElementById("ae-command").value;
    out.editProvider = document.getElementById("ae-provider").value;
    // 引号包路径的命令解析（纯函数）
    out.parsed = parseCmd('"C:\\\\Program Files\\\\x.exe" --flag  a');
    // 点「自动」→ 后端猜 provider（不写盘）
    document.getElementById("ae-guess").click();
    await wait(900);
    out.guessInput = document.getElementById("ae-provider").value;
    out.guessMsg = document.getElementById("ae-msg").textContent;
    // 填一个 provider 并保存 → 卡片上应出现该徽章
    document.getElementById("ae-provider").value = "deepseek";
    await saveAgentEdit();
    await wait(400);
    var saved = (ACP.list || []).filter(function (a) { return a.id === first.id; })[0] || {};
    out.savedProvider = saved.provider || "";
    out.editClosed = document.getElementById("agent-edit").hidden;
    out.badgeInHtml = document.getElementById("agents-list").innerHTML.indexOf('data-edit') >= 0;

    // ---- 从 agent 导入会话（session/list → import）----
    var sessionsBefore = (state.sessions || []).length;
    var impBtn = document.getElementById("btn-import");
    out.impBtnVisible = !!impBtn && !impBtn.hidden;
    if (impBtn) impBtn.click();
    await wait(1200);
    out.impOpen = !document.getElementById("import").hidden;
    out.impCur = ((document.getElementById("imp-cur") || {}).textContent || "").slice(0, 80);
    out.impRows = document.querySelectorAll("#imp-list [data-imp]").length;
    out.impTabs = !!document.getElementById("imp-tabs")
      && !document.getElementById("imp-tabs").hidden;
    out.impProjectCount = ((document.getElementById("imp-n-project") || {}).textContent || "").trim();
    out.impInternalCount = ((document.getElementById("imp-n-internal") || {}).textContent || "").trim();
    var itab = document.querySelector('#imp-tabs [data-imp-tab="internal"]');
    if (itab) itab.click();
    await wait(250);
    out.impInternalRows = document.querySelectorAll("#imp-list [data-imp]").length;
    var ptab = document.querySelector('#imp-tabs [data-imp-tab="project"]');
    if (ptab) ptab.click();
    await wait(250);
    out.impRowsAfterTab = document.querySelectorAll("#imp-list [data-imp]").length;
    // ⚠ 点**还没导入**的那条（已导入的点下去只是打开它，会话数不会 +1）
    var fresh = [].slice.call(document.querySelectorAll("#imp-list [data-imp]"))
      .filter(function (r) { return r.querySelector(".dot.ok") && r.textContent.indexOf("已导入") < 0; })[0];
    out.impFreshFound = !!fresh;
    if (fresh) fresh.click();
    await wait(1500);
    out.impClosed = document.getElementById("import").hidden;
    out.impDelta = (state.sessions || []).length - sessionsBefore;
    out.impCurSession = (state.current || {}).title || "";

    // ---- 智能体 / 模式切换（/api/agent）----
    document.getElementById("btn-agent").click();
    await wait(1000);
    out.modeOpen = !document.getElementById("mode").hidden;
    out.modeRows = document.querySelectorAll("#mode-list [data-mode]").length;
    out.modeCur = ((document.getElementById("mode-cur") || {}).textContent || "").slice(0, 90);
    out.modeMarked = document.querySelectorAll("#mode-list [data-mode].on").length;
    var other = [].slice.call(document.querySelectorAll("#mode-list [data-mode]"))
      .filter(function (r) { return !r.classList.contains("on"); })[0];
    var beforeBtn = (document.getElementById("btn-agent") || {}).textContent || "";
    if (other) other.click();
    await wait(1500);
    out.modeClosed = document.getElementById("mode").hidden;
    out.modeBtnBefore = beforeBtn;
    out.modeBtnAfter = (document.getElementById("btn-agent") || {}).textContent || "";
    // 顶栏按钮不能被当成 34px 方图标截断（踩坑 #24）——量出来，别靠肉眼看截图
    var bBtn = document.getElementById("btn-agent");
    out.modeBtnW = bBtn ? Math.round(bBtn.getBoundingClientRect().width) : 0;
    out.modeBtnClipped = bBtn ? (bBtn.scrollWidth > bBtn.clientWidth + 1) : true;

    // ---- 模型凭据（0.2.22 起改成「厂商 → 账号」多凭据列表；老的「单 Key 输入框」已下线）----
    // 单 Key 的保存 / 掩码流程由 tools/credentials_ui_test.py 专门盯，这里只锁结构不重复跑网络。
    var cfgBtn = document.getElementById("btn-cfg");
    if (cfgBtn) cfgBtn.click();
    await wait(900);
    out.keyRowThere = !!document.getElementById("cfg-key-row");
    out.keyLabel = (((document.querySelector("#cfg-key-row > label") || {}).textContent) || "").trim().slice(0, 40);
    out.credAdd = !!document.getElementById("cred-add");
    out.credList = !!document.getElementById("cred-list");
    out.legacyKeyGone = !document.getElementById("cfg-key-value")
        && !document.getElementById("cfg-key-save")
        && !document.getElementById("cfg-key-var");

    // ---- 空会话文案 + 自定义素材（头像/首图/贴纸）：改一下要立刻反映到首屏 ----
    var l1 = document.getElementById("cfg-hero-l1");
    l1.value = "测试文案一号";
    l1.dispatchEvent(new Event("input", { bubbles: true }));
    await wait(400);
    out.heroL1 = SET.heroL1;
    var ms = document.getElementById("msg-static");
    if (ms) ms.innerHTML = heroHtml();
    out.heroHasText = (ms ? ms.innerHTML : "").indexOf("测试文案一号") >= 0;
    // 真跑一遍写接口（APPEARANCE_FILE 已被指到临时目录）
    // ⚠ 用 avatar 而不是 hero：0.2.55 起「空会话主图」那一行已删（首屏只留内联圣徽），
    //    hero 没有 data-img-name 元素可回显了。
    // ⚠ 用**仓库里就有**的图（`hiru-bg.jpg` 是项目自有资产、随仓库走）；
    //   以前用 sticker-duck.jpg —— 那是本机同人图、CI 里根本不存在，所以 CI 必挂。
    await setAppearanceImage("avatar", "__ASSET__");
    out.appearUrl = appearanceUrl("avatar", "/assets/icon-256.png");
    out.appearName = (document.querySelector('[data-img-name="avatar"]') || {}).textContent || "";
    try { out.appearImg = (await fetch(appearanceUrl("avatar", "/assets/icon-256.png"))).status; }
    catch (e) { out.appearImg = -1; }
    await setAppearanceImage("avatar", "");
    out.appearCleared = appearanceUrl("avatar", "/assets/icon-256.png");
  } catch (e) { out.error = String(e); }
  out.jsErrors = window.__probeErr;
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}, 3500);
</script>
"""


def build_probe():
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    # 素材用**仓库自带**的图（CI 里没有同人图）；路径按本仓库算，别写死 D:
    asset = os.path.join(WEB, "assets", "hiru-bg.jpg").replace("\\", "/")
    js = TEST.replace("__ASSET__", asset)
    out = src.replace('<script src="/app.js', STUB + js + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    raise SystemExit("没找到 msedge.exe")


def render(edge=None):
    """★ 走 CDP 取结果（不再用 `--dump-dom` + 虚拟时间：CI 上会卡住/拿不到）。

    轮询页面里的 `#__probe__` 直到出现（最多 150s），然后取它的文本（就是那段 JSON）。
    """
    sys.path.insert(0, os.path.join(HERE, "tools"))
    from _cdp import Browser                     # noqa: E402
    with Browser() as b:
        b.navigate(URL)
        if not b.wait_for_js('!!document.getElementById("__probe__")', timeout=150):
            try:
                txt = b.eval_js('(document.body ? document.body.innerText : "").slice(0, 300)')
            except Exception:                    # noqa: BLE001
                txt = ""
            print("  (CDP 等到 150s 仍没有 #__probe__；页面文字前 200：%r)" % str(txt)[:200])
            return ""
        return b.eval_js('document.getElementById("__probe__").textContent') or ""


def main() -> int:
    edge = find_edge()

    os.environ.pop("OPENCODE_UI_ENGINE", None)          # 确保按 _engine.json 走
    sys.path.insert(0, BACKEND)
    import server  # noqa: E402
    from engines.acp import agents  # noqa: E402
    from engines.acp import service as svc_mod  # noqa: E402
    import tempfile  # noqa: E402

    # ⚠⚠ 必须把 ACP 的**落盘目录**也指到临时目录：以前只猴补丁了注册表，
    #   结果这个测试里导入的"假 agent 会话"被写进了**真实的** runtime/state/_acp_sessions.json
    #   （实测踩到：真文件里混进一条 agent=mock / "整理共享工作区" / 0 消息的垃圾）。
    tmpdir = tempfile.mkdtemp(prefix="acp_ui_test_")
    svc_mod.STATE_DIR = tmpdir
    svc_mod._ALWAYS_FILE = os.path.join(tmpdir, "_acp_always.json")
    svc_mod.audit.set_file(os.path.join(tmpdir, "_audit.jsonl"))   # ★别写真实审计链

    # ⚠⚠ 自定义外观（POST /appearance）也会写状态文件 —— 同样必须指到临时目录，
    #   否则会污染真实的 runtime/state/_appearance.json（踩坑 #36 同一类坑）。
    real_appear = os.path.join(STATE_DIR, "_appearance.json")
    try:
        real_appear_before = io.open(real_appear, "rb").read()
    except OSError:
        real_appear_before = None
    server.APPEARANCE_FILE = os.path.join(tmpdir, "_appearance.json")

    # ⚠ 以前这里改 `_acp.json` 指向假 agent，可引擎早已改成读注册表
    #   `_acp_agents.json` → 假 agent 根本没被用上（实际连的是你真实的 Codex）。
    #   现在改成**进程内猴补丁注册表**：不写任何配置文件，测的就是假 agent。
    reg = {"active": "mock",
           "baseline": {"cwd": HERE, "env": {}, "mode": "read-only"},
           "agents": [{"id": "mock", "label": "ACP 假 agent（联调用）",
                       "command": [sys.executable, MOCK], "cwd": HERE, "env": {},
                       "provider": "", "note": "由 tools/acp_engine_ui_test.py 内存注入"},
                      {"id": "mock-unavailable", "label": "缺依赖的假 agent",
                       "command": ["definitely-not-a-real-acp-binary"], "cwd": HERE, "env": {},
                       "provider": "", "note": "用来验证「缺：…」提示"}]}
    agents.load_registry = lambda: reg
    agents.save_registry = lambda r: None

    os.makedirs(STATE_DIR, exist_ok=True)
    old_engine = None
    try:
        old_engine = io.open(ENGINE_FILE, encoding="utf-8").read()
    except OSError:
        pass
    io.open(ENGINE_FILE, "w", encoding="utf-8").write('{"engine": "opencode"}')

    build_probe()
    httpd = server.Server(("127.0.0.1", PORT), server.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    # ★ CI 上这台机器没装 OpenCode，`_engine.json` 可能已被别处写成 acp；
    #   这里强制从 opencode 起步（否则 setEngine("acp") 是空操作 → 测不到 SSE 重连）
    try:
        server.engines.set_active_engine_id("opencode")
    except Exception:  # noqa: BLE001
        pass
    print("测试服务已起，正在无头渲染并切换引擎…")
    try:
        html = render(edge)
    finally:
        httpd.shutdown()
        if os.path.exists(PROBE):
            os.remove(PROBE)
        try:
            eng = server.engines.get_engine("acp")
            if hasattr(eng, "shutdown"):
                eng.shutdown()
        except Exception:  # noqa: BLE001
            pass
        # 还原引擎状态
        if old_engine is not None:
            io.open(ENGINE_FILE, "w", encoding="utf-8").write(old_engine)
        else:
            io.open(ENGINE_FILE, "w", encoding="utf-8").write('{"engine": "opencode"}')

    raw = html if isinstance(html, str) else ""
    if not raw.strip().startswith("{"):
        m = re.search(r'<pre id="__probe__">(.*?)</pre>', raw, re.S)
        raw = m.group(1) if m else ""
    if not raw.strip():
        print("[BAD] 没拿到探针结果（CDP 超时）。")
        return 1

    d = json.loads(raw)
    print("\n=== 引擎切换（无头实测）===")
    print("  下拉选项    :", d.get("options"))
    print("  初始值/禁用 :", d.get("value"), "/", d.get("disabled"))
    print("  初始说明    :", d.get("note0"))
    print("  引擎自述    :", d.get("engines"))
    print("  未就绪渲染  :", d.get("optHtml2"))
    print("  切到 acp    : active=%s  SSE重连=%s  说明=%s"
          % (d.get("acpActive"), d.get("acpEsDelta"), d.get("acpNote")))
    print("  切回 opencode: active=%s  SSE重连=%s" % (d.get("openActive"), d.get("openEsDelta")))
    print("  切换后标题/消息数:", d.get("title"), "/", d.get("msgCount"))
    print("\n=== agentlist / 编辑弹窗（provider 中性化）===")
    print("  卡片数      :", d.get("agentsRows"))
    print("  provider状态:", d.get("agentsProviders"))
    print("  弹窗打开    :", d.get("editOpen"), "| 预填 label/命令:", d.get("editLabel"), "/",
          d.get("editCommand"))
    print("  预填 provider:", repr(d.get("editProvider")))
    print("  命令解析    :", d.get("parsed"))
    print("  「自动」后   :", repr(d.get("guessInput")), "|", d.get("guessMsg"))
    print("  保存后 provider:", repr(d.get("savedProvider")), "| 弹窗已关:", d.get("editClosed"))
    print("  卡片有「改」按钮:", d.get("badgeInHtml"))
    print("  --- 智能体 / 模式切换 ---")
    print("  弹窗打开/行数/已标记:", d.get("modeOpen"), "/", d.get("modeRows"), "/", d.get("modeMarked"))
    print("  说明:", d.get("modeCur"))
    print("  切一下后：弹窗关闭:", d.get("modeClosed"), "| 按钮", d.get("modeBtnBefore"), "->", d.get("modeBtnAfter"))
    print("  按钮宽度:", d.get("modeBtnW"), "px（>34 才算放开了方图标宽度）| 被截断:", d.get("modeBtnClipped"))
    print("  --- 从 agent 导入会话 ---")
    print("  按钮可见/弹窗打开:", d.get("impBtnVisible"), "/", d.get("impOpen"), "| 列表:", d.get("impCur"))
    print("  项目/内部标签:", d.get("impProjectCount"), "/", d.get("impInternalCount"),
          "| 行:", d.get("impRows"), "| 内部行:", d.get("impInternalRows"), "| 切回项目:", d.get("impRowsAfterTab"))
    print("  行数:", d.get("impRows"), "| 点一下后弹窗关闭:", d.get("impClosed"),
          "| 面板会话 +", d.get("impDelta"), "| 当前:", d.get("impCurSession"))
    print("  --- 模型凭据（多 Key）---")
    print("  凭据行/标签:", d.get("keyRowThere"), "/", d.get("keyLabel"))
    print("  添加按钮/凭据列表:", d.get("credAdd"), "/", d.get("credList"))
    print("  老的单 Key 输入框已下线:", d.get("legacyKeyGone"), "（值/保存/只读变量名标签都不该在）")
    print("  脚本异常    :", d.get("error"), " JS错误:", d.get("jsErrors"))

    # 只读断言：整个测试**没有**把假 agent 的会话写进真实状态（隔离生效）
    real = os.path.join(HERE, "runtime", "state", "_acp_sessions.json")
    leaked = 0
    if os.path.isfile(real):
        try:
            data = json.load(open(real, encoding="utf-8"))
            leaked = len([s for s in (data.get("sessions") or [])
                          if str(s.get("agent") or "") == "mock"])
        except Exception:  # noqa: BLE001
            leaked = 0
    print("  真实状态有没有被写进假会话:", leaked, "条（应为 0）")
    try:
        real_appear_after = io.open(real_appear, "rb").read()
    except OSError:
        real_appear_after = None
    appear_untouched = real_appear_before == real_appear_after
    print("  真实 _appearance.json 有没有被动过:", "没有" if appear_untouched else "★被动过")

    opts = d.get("options") or []
    engs = d.get("engines") or []
    # 逐条命名 → 失败时能直接说出是哪一条（以前是一个大 and，只看到 BAD）
    checks = [
        ("引擎下拉含 acp/opencode", "acp" in opts and "opencode" in opts),
        ("引擎下拉不禁用", d.get("disabled") is False),
        ("切到 acp 生效 + SSE 重连", d.get("acpActive") == "acp" and (d.get("acpEsDelta") or 0) >= 1),
        ("切回 opencode 生效 + SSE 重连",
         d.get("openActive") == "opencode" and (d.get("openEsDelta") or 0) >= 1),
        # 引擎自述带 label/available（向导/设置据此标灰不可用引擎）
        ("引擎自述带 label/available",
         len(engs) >= 2 and all(("label" in e and "available" in e) for e in engs)),
        # 不可用引擎：标「未就绪」+ disabled
        ("不可用引擎标「未就绪」", "（未就绪）" in str(d.get("optHtml2") or "")),
        ("不可用引擎 disabled", 'value="bad" disabled' in str(d.get("optHtml2") or "")),
        # 编辑弹窗：能打开、预填、命令解析正确、能保存 provider
        ("agentlist 编辑弹窗能打开", d.get("editOpen") is True),
        ("命令解析", d.get("parsed") == ["C:\\Program Files\\x.exe", "--flag", "a"]),
        ("能保存 provider", d.get("savedProvider") == "deepseek"),
        ("保存后弹窗关闭", d.get("editClosed") is True),
        ("卡片有「改」按钮", d.get("badgeInHtml") is True),
        # 模式切换：弹窗能开、列出可见模式、有 current 标记、点一下能切、按钮文案跟着变
        ("模式弹窗能开", d.get("modeOpen") is True),
        ("模式列表 >= 2 行", (d.get("modeRows") or 0) >= 2),
        ("模式有 current 标记", (d.get("modeMarked") or 0) >= 1),
        ("切换后模式弹窗关闭", d.get("modeClosed") is True),
        ("模式按钮文案跟着变", (d.get("modeBtnAfter") or "") != ""),
        ("模式按钮宽度 > 34px（踩坑 #24）", (d.get("modeBtnW") or 0) > 34),
        ("模式按钮文字没被截断", d.get("modeBtnClipped") is False),
        # 从 agent 导入：按钮可见（acp 模式）、弹窗能开、列出 agent 侧会话、点一下导入并打开
        ("接续按钮可见", d.get("impBtnVisible") is True),
        ("接续弹窗能开", d.get("impOpen") is True),
        ("接续列表 >= 2 行", (d.get("impRows") or 0) >= 2),
        ("项目/内部两个标签", d.get("impTabs") is True),
        ("项目标签计数 2", (d.get("impProjectCount") or "") == "2"),
        ("内部标签计数 1", (d.get("impInternalCount") or "") == "1"),
        ("内部行 1 条", (d.get("impInternalRows") or 0) == 1),
        ("切回项目后 >= 2 行", (d.get("impRowsAfterTab") or 0) >= 2),
        ("接续后弹窗关闭", d.get("impClosed") is True),
        ("接续后面板会话 +1", (d.get("impDelta") or 0) >= 1),
        # 模型凭据：行在、是多凭据 UI（＋添加凭据 + 列表）、老的单 Key 输入框已彻底下线
        ("凭据行存在", d.get("keyRowThere") is True),
        ("有「＋ 添加凭据」", d.get("credAdd") is True),
        ("有凭据列表", d.get("credList") is True),
        ("老的单 Key 输入框已下线", d.get("legacyKeyGone") is True),
        ("凭据行标签写「凭据」", "凭据" in str(d.get("keyLabel") or "")),
        # 空会话文案可改（改 SET + 立刻反映到 heroHtml）+ 自定义素材真能存取
        ("空会话文案改了", d.get("heroL1") == "测试文案一号"),
        ("首屏跟着变", d.get("heroHasText") is True),
        ("素材 URL 走 /appearance/img", str(d.get("appearUrl") or "").startswith("/appearance/img?k=avatar")),
        ("素材文件名回显", d.get("appearName") == "hiru-bg.jpg"),
        ("素材代理取图 200", d.get("appearImg") == 200),
        ("素材能恢复默认", d.get("appearCleared") == "/assets/icon-256.png"),
        ("真实 _appearance.json 没被动过", appear_untouched),
        ("真实状态没混进假 agent 会话", leaked == 0),
        ("脚本没抛异常", not d.get("error")),
        ("页面没有 JS 错误", not d.get("jsErrors")),
    ]
    bads = [name for name, val in checks if not val]
    ok = not bads
    if bads:
        print("  未通过:", " / ".join(bads))
    print("\n  结论:", "[OK] 引擎切换 / SSE 重连 / agentlist 编辑 都正常" if ok else "[BAD] 有问题，见上面")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
