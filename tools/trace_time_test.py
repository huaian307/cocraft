# -*- coding: utf-8 -*-
r"""无头回归：轨迹页（系统通知归轨迹 · 整轮时长 · 工具翻成人话 + 真输出 + 真 diff）。

做法同 inspect_ui.py / history_page_test.py：由 frontend/index.html 生成探针页，
拦掉会惊动守护进程的请求，把 `/api/session*` 换成**内存里的假会话**（形状照抄真实接口，
包括 `state.content` / `metadata.files[].patch` / `metadata.exit` 这些**真字段**），
再用 Edge --headless 真跑一遍 app.js，断言：

  1. `type:"system"` 的注入通知**不进主界面**；`synthetic` 这类会话内提示仍在；
  2. 「思考 时长」是**整轮**的（从上一条用户消息算到最后一条完成）；
  3. 轨迹里的工具**翻成中文摘要**（运行命令 / 搜索内容 / 修改文件 / 读取文件 / 写入文件）；
  4. 摘要带**元信息**：`exit 0` / `6 处命中` / `+3 −1` / `1654–1667 行`；
  5. **真输出接上了**（OpenCode 的输出在 `state.content`，以前一个字都不显示）；
  6. `edit` 渲染**真 diff**（`metadata.files[].patch` → 红绿逐行）；
  7. **默认折叠**，只有**出错**的条目自动展开；原始 JSON 收在「原始数据」里；
  8. 「全部展开 / 全部收起」按钮可用，折叠状态跨重渲染保留；
  9. 全程零 JS 报错。

用法（需要面板服务已在 8787 运行 —— 只是给探针页提供静态文件）：
    python tools\trace_time_test.py
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
PROBE = os.path.join(WEB, "_probe_trace.html")
URL = "http://127.0.0.1:8787/_probe_trace.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

PATCH = ("Index: frontend/app.js\n"
         "===================================================================\n"
         "--- frontend/app.js\n"
         "+++ frontend/app.js\n"
         "@@ -1080,3 +1080,4 @@\n"
         " }\n"
         "-  const empty = !state.messages.length;\n"
         "+  const empty = !staticHtml;\n"
         "+  // 新增一行注释\n")

STUB = """<script>
(function () {
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event"];
  var raw = window.fetch;
  var T = 1700000000000;
  var PATCH = __PATCH__;

  function tool(name, state) { return { type: "tool", name: name, state: state }; }
  function msg(o) { return o; }

  // 一轮（turn）= 一条用户消息 → 后面的一串助手消息（中间夹着系统通知）
  var MSGS = [
    { id: "m1", type: "user", text: "第一轮提问", time: { created: T } },
    { id: "m2", type: "system",
      text: "The instructions from D:\\\\opencode-ui\\\\AGENTS.md changed. Here's the diff: ```diff\\n@@ -1,2 +1,2 @@\\n-x\\n+y\\n```",
      time: { created: T + 10000 } },
    { id: "m3", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 12000, completed: T + 22000 },
      content: [{ type: "reasoning", text: "想一下 A" },
                tool("shell", { status: "completed", input: { command: "echo a" } })] },
    { id: "m4", type: "system", text: "Today's date is now: Wed Oct 15 2026", time: { created: T + 25000 } },
    { id: "m5", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 30000, completed: T + 95000 },
      content: [{ type: "reasoning", text: "想一下 B" },
                tool("read", { status: "completed", input: { path: "D:/a/b.md" } })] },
    { id: "m6", type: "system", text: "The Code Mode tool catalog has changed. This catalog supersedes ...",
      time: { created: T + 96000 } },
    { id: "m7", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 100000, completed: T + 300000 },
      content: [{ type: "reasoning", text: "最后想一下" }, { type: "text", text: "第一轮的最终回答" }] },
    { id: "m8", type: "idle", time: { created: T + 301000 }, outcome: "succeeded" },

    { id: "m9", type: "user", text: "第二轮提问", time: { created: T + 1000000 } },
    { id: "m10", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1001000, completed: T + 1010000 },
      content: [{ type: "reasoning", text: "第二轮想一下" },
                tool("shell", { status: "completed", input: { command: "ls" } })] },
    // 这一轮的最后一条**自己没有 reasoning**，但整轮思考过 → 也要能看到总时长
    { id: "m11", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1011000, completed: T + 1200000 },
      content: [{ type: "text", text: "第二轮的最终回答" }] },
    { id: "m12", type: "synthetic", text: "自动续写的一段", time: { created: T + 1201000 } },

    /* ---- 第三轮：把工具调用的各种形状都跑一遍（字段照抄真实接口）---- */
    { id: "m13", type: "user", text: "第三轮：跑一遍检查再改代码", time: { created: T + 1300000 } },
    { id: "m14", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1301000, completed: T + 1305000 },
      content: [{ type: "reasoning", text: "先跑体检" },
        tool("shell", { status: "completed",
          input: { command: "cd D:\\\\opencode-ui; python tools\\\\check_features.py" },
          metadata: { status: "completed", truncated: false, exit: 0 },
          content: [{ type: "text", text: "1111 项，缺失 0 项\\nresult: PASS" }] })] },
    { id: "m15", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1306000, completed: T + 1310000 },
      content: [tool("grep", { status: "completed",
        input: { pattern: "fmtTime", path: "D:/opencode-ui/frontend/app.js", limit: 10 },
        metadata: { matches: 6, truncated: false },
        content: [{ type: "text", text: "Found 6 matches\\n  Line 185: const fmtTime = ..." }] })] },
    { id: "m16", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1311000, completed: T + 1320000 },
      content: [tool("edit", { status: "completed",
        input: { path: "frontend/app.js", oldString: "old", newString: "new" },
        metadata: { truncated: false, files: [{ file: "frontend/app.js", status: "modified",
          additions: 3, deletions: 1, patch: PATCH }] },
        content: [{ type: "text", text: "Edited frontend/app.js (1 replacement)" }] })] },
    { id: "m17", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1321000, completed: T + 1322000 },
      content: [tool("read", { status: "completed",
        input: { path: "D:/opencode-ui/AGENTS.md", offset: 1654, limit: 14 },
        metadata: { truncated: false },
        content: [{ type: "text", text: "Read file AGENTS.md, lines 1654-1667\\n1654: line one" }] })] },
    { id: "m18", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1323000, completed: T + 1324000 },
      content: [tool("write", { status: "error",
        input: { path: "D:/tmp/new.py", content: "print(1)" },
        metadata: {},
        content: [{ type: "text", text: "Error: permission denied" }] })] },
    { id: "m19", type: "assistant", agent: "build", model: { id: "probe-1", providerID: "acp" },
      time: { created: T + 1325000, completed: T + 1330000 },
      content: [{ type: "text", text: "第三轮的最终回答" }] },
    { id: "m20", type: "idle", time: { created: T + 1331000 } }
  ];

  var SESS = { id: "ses_probe", title: "轨迹 / 时长探针",
               time: { created: T, updated: T + 1331000 },
               location: { directory: "D:\\\\probe" },
               model: { id: "probe-1", providerID: "acp" } };

  function J(o) {
    return Promise.resolve(new Response(JSON.stringify(o), {
      status: 200, headers: { "Content-Type": "application/json" },
    }));
  }
  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    var qi = s.indexOf("?"), path = qi >= 0 ? s.slice(0, qi) : s;
    if (path === "/api/session/ses_probe/message") {
      return J({ data: MSGS.slice().reverse(), cursor: null });      // 新→旧（照真实形状）
    }
    if (s.indexOf("/api/session/ses_probe") === 0) {
      if (/\\/(model|agent|skill|command|permission|form)$/.test(path)) {
        return Promise.resolve(new Response("", { status: 204 }));
      }
      return J({ data: SESS });
    }
    if (path === "/api/session") return J({ data: [SESS], cursor: null });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; this.addEventListener = function () {}; };
})();
</script>
"""

PROBE_JS = """<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
function __tSleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function __tTxt(el) { return el ? String(el.textContent || "").trim() : null; }

(async function () {
  var out = { steps: [], info: {} };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    for (var i = 0; i < 300; i++) {
      try { if (state && state.current && state.messages.length) break; } catch (e) {}
      await __tSleep(25);
    }
    var staticBox = document.getElementById("msg-static");
    var bubbles = [].slice.call(staticBox.querySelectorAll(".msg"));
    var notices = [].slice.call(staticBox.querySelectorAll(".notice"));
    var chips = bubbles.map(function (b) {
      var c = b.querySelector(".think-chip");
      return c ? __tTxt(c).replace(/\\s+/g, " ") : "";
    });

    out.info.main = {
      bubbles: bubbles.length, notices: notices.length, chips: chips,
      noticeText: notices.map(function (n) { return __tTxt(n).slice(0, 24); }),
      systemLeak: /instructions from|Today's date|tool catalog/.test(staticBox.textContent),
    };
    out.info.turn = { m7: TURNMAP.get("m7") || null, m3: TURNMAP.get("m3") || null,
                      m11: TURNMAP.get("m11") || null, m19: TURNMAP.get("m19") || null };

    /* ---- ① 系统通知不进主界面 / 会话内提示还在 ---- */
    step("★系统注入通知不占主界面", out.info.main.systemLeak === false);
    step("★会话内提示仍留着（synthetic → 1 条 notice）",
         out.info.main.notices === 1 && /自动续写/.test(out.info.main.noticeText.join("")),
         JSON.stringify(out.info.main.noticeText));
    step("主界面 6 条气泡（3 用户 + 3 最终回答）", out.info.main.bubbles === 6,
         String(out.info.main.bubbles));

    /* ---- ② 「思考 时长」= 整轮总时长 ---- */
    step("★第一轮 300s → 5m（不是单条的 200s）", /思考 5m$/.test(chips[1] || ""), chips[1]);
    step("★第二轮 200s → 3m20s，最后一条自己没有 reasoning 也显示",
         /思考 3m20s$/.test(chips[3] || ""), chips[3]);
    step("★第三轮 30s → 30s", /思考 30s$/.test(chips[5] || ""), chips[5]);
    step("★整轮起点 = 上一条用户消息的 created",
         out.info.turn.m7 && out.info.turn.m7.start === 1700000000000
         && out.info.turn.m3 && out.info.turn.m3.start === 1700000000000,
         JSON.stringify(out.info.turn.m7));
    step("★整轮标记：m7 / m11 / m19 是各自那一轮的最后一条，m3 不是",
         out.info.turn.m7.last === true && out.info.turn.m11.last === true
         && out.info.turn.m19.last === true && out.info.turn.m3.last === false
         && out.info.turn.m7.hasThink === true);

    out.info.fmt = [45, 95, 300, 3900, 60, 3600].map(function (s) { return fmtDur(s); });
    step("★时长人类可读：45s / 1m35s / 5m / 1h5m / 1m / 1h",
         out.info.fmt.join(",") === "45s,1m35s,5m,1h5m,1m,1h", out.info.fmt.join(","));

    /* ---- ③ 轨迹页打开 ---- */
    traceOpen();
    await __tSleep(80);
    var tb = document.getElementById("trace-body");
    var sums = function () {
      return [].slice.call(tb.querySelectorAll("details.tr-fold > summary.tr-sum")).map(function (s) {
        return __tTxt(s).replace(/\\s+/g, " ").trim();
      });
    };
    var all = sums();
    out.info.trace = {
      notes: tb.querySelectorAll(".tr-item.tr-note").length,
      labels: [].slice.call(tb.querySelectorAll(".tr-item.tr-note .tr-sum b"))
        .map(function (e) { return __tTxt(e); }),
      folds: all.length,
      sums: all.slice(-6),
      foldsOpen: tb.querySelectorAll("details.tr-fold[open]").length,
      raw: tb.querySelectorAll("details.tr-raw").length,
      toolFolds: tb.querySelectorAll(".tr-item.tr-tool details.tr-fold").length,
      diffs: { add: tb.querySelectorAll(".tr-diff .dl.add").length,
               del: tb.querySelectorAll(".tr-diff .dl.del").length },
      hasStdout: /1111 项，缺失 0 项/.test(tb.textContent),
      hasGrepHits: /Line 185/.test(tb.textContent),
      hasEnglishStatus: /(^|\\s)completed(\\s|$)|\\brunning\\b/.test(tb.textContent),
      allBtn: __tTxt(document.getElementById("trace-all")),
    };
    step("★轨迹页打开、系统通知 3 条都进来了", out.info.trace.notes === 3,
         JSON.stringify(out.info.trace.labels));
    step("★工具摘要是中文（运行命令 / 搜索内容 / 修改文件 / 读取文件 / 写入文件）",
         /运行命令/.test(all.join("|")) && /搜索内容/.test(all.join("|"))
         && /修改文件/.test(all.join("|")) && /读取文件/.test(all.join("|"))
         && /写入文件/.test(all.join("|")), JSON.stringify(out.info.trace.sums));
    step("★摘要带元信息：exit 0 / 6 处命中 / +3 −1 / 1654–1667 行",
         /exit 0/.test(all.join("|")) && /6 处命中/.test(all.join("|"))
         && /\\+3 −1/.test(all.join("|")) && /1654–1667 行/.test(all.join("|")),
         JSON.stringify(out.info.trace.sums));
    step("★摘要里只留文件名（不塞长路径）",
         /「fmtTime」 · app.js/.test(all.join("|")) && /app\\.js/.test(all.join("|")));
    step("★真输出接上了（shell stdout / grep 命中）",
         out.info.trace.hasStdout && out.info.trace.hasGrepHits);
    step("★edit 渲染真 diff（红绿逐行）",
         out.info.trace.diffs.add === 2 && out.info.trace.diffs.del === 1,
         JSON.stringify(out.info.trace.diffs));
    step("★默认折叠（只有出错那条自动展开）", out.info.trace.foldsOpen === 1,
         "open=" + out.info.trace.foldsOpen + " / 共 " + out.info.trace.folds + " 条");
    step("★状态也是中文（轨迹里没有 completed / running）",
         out.info.trace.hasEnglishStatus === false);
    step("★原始 JSON 收在「原始数据」里（每个工具一份）",
         out.info.trace.raw === out.info.trace.toolFolds && out.info.trace.raw > 0,
         out.info.trace.raw + " / 工具 " + out.info.trace.toolFolds);

    /* ---- ④ 全部展开 / 收起 ---- */
    document.getElementById("trace-all").click();
    await __tSleep(60);
    var opened = tb.querySelectorAll("details.tr-fold[open]").length;
    var label1 = __tTxt(document.getElementById("trace-all"));
    step("★「全部展开」把所有条目展开",
         opened === out.info.trace.folds && label1 === "全部收起",
         opened + "/" + out.info.trace.folds + " btn=" + label1);
    document.getElementById("trace-all").click();
    await __tSleep(60);
    step("★再点一次全部收起",
         tb.querySelectorAll("details.tr-fold[open]").length === 0
         && __tTxt(document.getElementById("trace-all")) === "全部展开",
         __tTxt(document.getElementById("trace-all")));

    /* ---- ⑤ 折叠状态跨重渲染保留 ---- */
    var first = tb.querySelector("details.tr-fold");
    first.open = true;
    first.dispatchEvent(new Event("toggle"));
    await __tSleep(30);
    renderTrace();
    await __tSleep(60);
    out.info.keepOpen = tb.querySelector("details.tr-fold[open]") ? true : false;
    step("★手动展开的状态在重渲染后保留", out.info.keepOpen === true);

    /* ---- ⑥ 摘要行几何：meta 必须贴右（别靠肉眼，量出来） ---- */
    var geo = [].slice.call(tb.querySelectorAll("details.tr-fold > summary.tr-sum"))
      .map(function (s) {
        var m = s.querySelector(".tr-meta");
        if (!m) return null;
        var rs = s.getBoundingClientRect(), rm = m.getBoundingClientRect();
        return Math.round(rs.right - rm.right);
      }).filter(function (x) { return x !== null; });
    out.info.geo = { gaps: geo.slice(0, 5), max: Math.max.apply(null, geo) };
    step("★摘要行的元信息贴右（每行离右缘 ≤ 6px，不是跟着文字跑）",
         geo.length > 0 && Math.max.apply(null, geo) <= 6, JSON.stringify(out.info.geo));

    /* ---- ⑥b 窄面板下**不许竖排**（「思考」被挤成「思/考」就是这个 bug） ---- */
    document.documentElement.style.setProperty("--trace-w", "320px");
    renderTrace();
    await __tSleep(100);
    var narrow = [].slice.call(tb.querySelectorAll("details.tr-fold > summary.tr-sum")).map(function (s) {
      var tag = s.querySelector(".tag");
      var zh = s.querySelector(".tr-zh");
      var meta = s.querySelector(".tr-meta");
      var rs = s.getBoundingClientRect();
      return {
        tagH: tag ? Math.round(tag.getBoundingClientRect().height) : 0,
        zhH: zh ? Math.round(zh.getBoundingClientRect().height) : 0,
        overflow: Math.round(s.scrollWidth - s.clientWidth),
        metaIn: meta ? Math.round(meta.getBoundingClientRect().right - rs.right) : 0,
        txt: s.textContent.replace(/\\s+/g, " ").trim().slice(0, 26),
      };
    });
    var worstTag = Math.max.apply(null, narrow.map(function (x) { return x.tagH; }));
    var worstZh = Math.max.apply(null, narrow.map(function (x) { return x.zhH; }));
    var worstOv = Math.max.apply(null, narrow.map(function (x) { return x.overflow; }));
    var worstMeta = Math.max.apply(null, narrow.map(function (x) { return x.metaIn; }));
    out.info.narrow = { tagH: worstTag, zhH: worstZh, overflow: worstOv, metaOut: worstMeta,
                        n: narrow.length, sample: (narrow[0] || {}).txt };
    step("★面板拉到 320px 时状态标签仍是单行（「思考」「完成」不竖排）",
         worstTag > 0 && worstTag <= 20 && worstZh > 0 && worstZh <= 20,
         "tag=" + worstTag + " zh=" + worstZh + "px");
    step("★窄面板下摘要行不溢出、右侧 meta 仍看得见",
         worstOv <= 1 && worstMeta <= 1, "overflow=" + worstOv + " metaOut=" + worstMeta);
    var headRows = [].slice.call(document.querySelectorAll("#trace .trace-head .tr-cap, #trace .trace-head button"))
      .map(function (e) { return { h: Math.round(e.getBoundingClientRect().height),
                                   t: e.textContent.trim() }; });
    out.info.headNarrow = headRows;
    step("★窄面板下标题栏也不竖排（轨迹 / 全部展开 都是单行）",
         headRows.length > 0 && Math.max.apply(null, headRows.map(function (x) { return x.h; })) <= 28,
         JSON.stringify(headRows));
    document.documentElement.style.removeProperty("--trace-w");
    renderTrace();
    await __tSleep(50);

    /* ---- ⑦ 右边缘可拖宽 ---- */
    var grip = document.getElementById("trace-resize");
    // ⚠ 无头 profile 会跨运行保留 localStorage → 先把上次留的宽度清掉，否则量到的"拖前"是上次的值
    try { localStorage.removeItem("cocraft.traceWidth"); } catch (e) {}
    document.documentElement.style.removeProperty("--trace-w");
    var before = document.getElementById("trace").getBoundingClientRect().width;
    var pd = function (type, x) {
      grip.dispatchEvent(new PointerEvent(type, { clientX: x, clientY: 300, pointerId: 1, bubbles: true }));
    };
    pd("pointerdown", 800);
    pd("pointermove", 1100);
    var after = document.getElementById("trace").getBoundingClientRect().width;
    pd("pointerup", 1100);
    var saved = null;
    try { saved = localStorage.getItem("cocraft.traceWidth"); } catch (e) {}
    out.info.resize = { before: Math.round(before), after: Math.round(after), saved: saved };
    step("★拖动右边缘能把轨迹拉宽", after > before + 100,
         out.info.resize.before + " → " + out.info.resize.after);
    step("★宽度落 localStorage（cocraft.traceWidth）", !!saved, String(saved));
    traceClose();
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
    stub = STUB.replace("__PATCH__", json.dumps(PATCH, ensure_ascii=False))
    out = src.replace('<script src="/app.js', stub + PROBE_JS + '<script src="/app.js')
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
    dom = os.path.join(tmp, "probe_trace_dom.html")
    err = os.path.join(tmp, "probe_trace_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-traceprobe"),
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
        print("没拿到探针结果（渲染可能失败）。DOM 前 600 字符：")
        print(html[:600])
        return 1

    d = json.loads(m.group(1))
    print("\n=== 轨迹页无头回归（系统通知 / 整轮时长 / 工具摘要）===")
    if d.get("crash"):
        print("  探针异常:", d["crash"])
    for name, ok, extra in d.get("steps", []):
        print("  %s %s%s" % ("OK  " if ok else "FAIL", name, ("  [" + str(extra) + "]") if extra else ""))
    print("\n  JS 运行错误:", d.get("jsErrors"))
    print("  关键数据:")
    for k, v in (d.get("info") or {}).items():
        print("    %-9s %s" % (k, json.dumps(v, ensure_ascii=False)[:340]))

    bad = [n for n, ok, _ in d.get("steps", []) if not ok]
    if d.get("crash") or d.get("jsErrors") or bad:
        print("\n  结论: [BAD] %s" % (bad or "有 JS 错误 / 探针异常"))
        return 2
    print("\n  结论: [OK] 轨迹页（系统通知 / 整轮时长 / 工具人话摘要 + 真输出 + 真 diff）全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
