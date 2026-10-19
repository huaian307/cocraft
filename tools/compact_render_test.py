# -*- coding: utf-8 -*-
r"""无头回归：上下文压缩（`compaction`）等 OpenCode 消息类型的渲染。

背景（用户报过）：OpenCode 客户端里正常显示的「会话已压缩」小条，在面板里变成了
**「未知结构 · 原始数据」兜底卡** —— 因为 `renderMessage` 没处理 `compaction`。
同族的 `agent-switched` / `model-switched` / `shell` 也一起补上，都是**中文小条**。

做法同 trace_time_test.py：由 frontend/index.html 生成探针页，拦掉会惊动守护进程的请求，
`/api/session*` 换成**内存里的假会话**（字段形状照抄上游 `openapi.json`），
再用 Edge --headless 真跑一遍 app.js，断言：

  1. 主界面**没有**「未知结构」兜底卡（这是本轮的起因）；
  2. `compaction` 三种状态：`running` →「正在压缩会话…」/ `completed` →「会话已压缩」/
     `failed` →「会话压缩失败」+ 失败原因（`[type] message · response.body`）；
  3. 「自动 / 手动」+ 用量（`输入 12,345 / 输出 1,234`）都在小条上；
  4. 「压缩出的上下文摘要」**默认折叠**、展开是 **Markdown 渲染**（不是原始 JSON），
     且**跨重渲染保留**展开状态；
  5. `agent-switched` →「已切换模式：build」、`model-switched` →「已切换模型：provider/id · variant」；
  6. `shell` → 命令 + 中文状态 + `exit 0`，输出可折叠；
  7. 压缩条**不进轨迹页**（摘要在主界面已可展开，避免重复占位）；
  8. 全程零 JS 报错。

用法（需要面板服务已在 8787 运行 —— 只是给探针页提供静态文件）：
    python tools\compact_render_test.py
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
PROBE = os.path.join(WEB, "_probe_compact.html")
URL = "http://127.0.0.1:8787/_probe_compact.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = """<script>
(function () {
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event",
              "/music/", "/spectrum", "/panel/", "/update/", "/appearance", "/ccagent"];
  var raw = window.fetch;
  var T = 1700000000000;

  var SUMMARY = "# 上下文摘要\\n\\n这一段是**压缩出来的摘要**。\\n\\n- 第一件事\\n- 第二件事\\n\\n" +
                "```py\\nprint(1)\\n```\\n";

  var MSGS = [
    { id: "c1", type: "user", text: "跑一轮看看", time: { created: T } },
    // ① 正在压缩（字段：status=running / reason / summary / recent）
    { id: "c2", type: "compaction", status: "running", reason: "auto",
      summary: "", recent: "", time: { created: T + 1000 } },
    // ② 压缩完成（+ model / tokens / cost）
    { id: "c3", type: "compaction", status: "completed", reason: "auto",
      model: { id: "deepseek-v4-flash", providerID: "deepseek" },
      tokens: { input: 12345, output: 1234 },
      cost: 0.02, summary: SUMMARY, recent: "",
      time: { created: T + 20000 } },
    // ③ 压缩失败（+ error: Session.StructuredError）
    { id: "c4", type: "compaction", status: "failed", reason: "manual",
      error: { type: "provider", message: "upstream 500", response: { body: "boom" } },
      summary: "", recent: "", time: { created: T + 30000 } },
    // ④ 同族的三类：切换模式 / 切换模型 / 在客户端跑过的 shell 命令
    { id: "c5", type: "agent-switched", agent: "build", previous: "plan",
      time: { created: T + 31000 } },
    { id: "c6", type: "model-switched",
      model: { id: "deepseek-v4-flash", providerID: "acp", variant: "high" },
      previous: { id: "deepseek-flash", providerID: "deepseek", variant: "default" },
      time: { created: T + 32000 } },
    { id: "c7", type: "shell", shellID: "sh_1", command: "python -c \\"print(1)\\"",
      status: "exited", exit: 0,
      output: { output: "1\\n", cursor: 2, size: 2, truncated: false },
      time: { created: T + 33000, completed: T + 34000 } },
    { id: "c8", type: "assistant", agent: "build", model: { id: "deepseek-v4-flash", providerID: "deepseek" },
      time: { created: T + 35000, completed: T + 36000 },
      content: [{ type: "text", text: "好了" }] },
    { id: "c9", type: "idle", time: { created: T + 36100 }, outcome: "succeeded" }
  ];

  var SESS = { id: "ses_probe", title: "压缩 / 小条探针",
               time: { created: T, updated: T + 36100 },
               location: { directory: "D:\\\\probe" },
               model: { id: "deepseek-v4-flash", providerID: "deepseek" } };

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
      return J({ data: MSGS.slice().reverse(), cursor: null });   // 新→旧（照真实形状）
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
    var box = document.getElementById("msg-static");
    var txt = box.textContent || "";

    /* ---- ① 起因：不再掉「未知结构」兜底卡 ---- */
    out.info.leak = { unknown: /未知结构/.test(txt), raw: /原始数据/.test(txt),
                      notices: box.querySelectorAll(".notice").length };
    step("★主界面没有「未知结构」兜底卡（本轮的起因）", out.info.leak.unknown === false,
         JSON.stringify(out.info.leak));

    /* ---- ② compaction 三种状态 ---- */
    step("★压缩中 → 「正在压缩会话…」", /正在压缩会话/.test(txt));
    step("★压缩完成 → 「会话已压缩」", /会话已压缩/.test(txt));
    step("★压缩失败 → 「会话压缩失败」", /会话压缩失败/.test(txt));
    step("★失败原因是人话（[type] message · response.body），不是 JSON",
         /\\[provider\\] upstream 500/.test(txt) && /boom/.test(txt),
         (box.querySelector(".cp-err") || {}).textContent);

    /* ---- ③ 自动/手动 + 用量 ---- */
    out.info.bits = [].slice.call(box.querySelectorAll(".notice.compact .cp-sub"))
      .map(function (e) { return __tTxt(e); });
    var bitsAll = out.info.bits.join(" | ");
    step("★标了「自动 / 手动」", /自动/.test(bitsAll) && /手动/.test(bitsAll), bitsAll);
    step("★用量（输入 12,345 / 输出 1,234）",
         /输入 12,345/.test(bitsAll) && /输出 1,234/.test(bitsAll), bitsAll);
    step("★小条带时间戳", /\\d{1,2}-\\d{1,2}\\s?\\d{1,2}:\\d{2}|\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}/.test(bitsAll)
         || /\\d{2}:\\d{2}/.test(bitsAll), bitsAll);

    /* ---- ④ 摘要：默认折叠 / Markdown 渲染 / 展开状态跨重渲染保留 ---- */
    var sum = box.querySelector("details.cp-sum:not(.cp-out)");
    out.info.sum = {
      found: !!sum,
      open: sum ? sum.open : null,
      summaryText: sum ? __tTxt(sum.querySelector("summary")) : null,
      hasStrong: sum ? !!sum.querySelector(".body strong") : false,
      hasCode: sum ? !!sum.querySelector(".body pre, .body code") : false,
      hasList: sum ? !!sum.querySelector(".body li") : false,
      rawMd: sum ? /\\*\\*压缩出来的摘要\\*\\*/.test(sum.querySelector(".body").textContent) : false,
    };
    step("★压缩摘要默认折叠（点开才看）", !!sum && sum.open === false, JSON.stringify(out.info.sum));
    step("★摘要标题写明「压缩出的上下文摘要」+ 字数",
         /压缩出的上下文摘要/.test(out.info.sum.summaryText || ""),
         out.info.sum.summaryText);
    sum.open = true;
    sum.dispatchEvent(new Event("toggle"));
    await __tSleep(30);
    out.info.sum2 = { hasStrong: !!sum.querySelector(".body strong"),
                      hasCode: !!sum.querySelector(".body pre, .body code"),
                      hasList: !!sum.querySelector(".body li"),
                      rawMd: /\\*\\*压缩出来的摘要\\*\\*/.test(sum.querySelector(".body").textContent) };
    step("★摘要是 Markdown 渲染（粗体 / 代码块 / 列表都在）",
         out.info.sum2.hasStrong && out.info.sum2.hasCode && out.info.sum2.hasList,
         JSON.stringify(out.info.sum2));
    step("★没有把 Markdown 源码原样倒出来（不是原始 `**粗体**`）", out.info.sum2.rawMd === false);
    renderMessages();
    await __tSleep(80);
    var sum2 = box.querySelector("details.cp-sum:not(.cp-out)");
    out.info.sumKeep = sum2 ? sum2.open : null;
    step("★展开状态跨重渲染保留", out.info.sumKeep === true, String(out.info.sumKeep));

    /* ---- ⑤ 切换模式 / 切换模型 ---- */
    step("★agent-switched → 「已切换模式：build」", /已切换模式：build/.test(txt));
    step("★model-switched → 「已切换模型：deepseek-flash → deepseek-v4-flash · high」（用真实 previous，且不显示中性占位 acp/）",
         /已切换模型：deepseek-flash → deepseek-v4-flash · high/.test(txt)
         && !/acp\\/deepseek-v4-flash/.test(txt));

    /* ---- ⑥ shell 消息 ---- */
    var outFold = box.querySelector("details.cp-out");
    out.info.sh = {
      cmd: /python -c/.test(txt), done: /已完成/.test(txt), code: /exit 0/.test(txt),
      foldFound: !!outFold, foldOpen: outFold ? outFold.open : null,
      bodyText: outFold ? __tTxt(outFold.querySelector(".body")) : null,
    };
    step("★shell：命令 + 中文状态 + exit 0",
         out.info.sh.cmd && out.info.sh.done && out.info.sh.code, JSON.stringify(out.info.sh));
    step("★shell 输出默认折叠、内容是 stdout", !!outFold && outFold.open === false
         && /^1$/.test((out.info.sh.bodyText || "").trim()),
         JSON.stringify([out.info.sh.foldOpen, out.info.sh.bodyText]));

    /* ---- ⑦ 压缩条不进轨迹（摘要在主界面已可展开） ---- */
    var items = traceItems();
    out.info.trace = { n: items.length,
                       compact: items.filter(function (it) {
                         return /会话已压缩|正在压缩会话|会话压缩失败/.test(String(it.text || ""));
                       }).length };
    step("★压缩条不进轨迹页（不重复占位）", out.info.trace.compact === 0,
         JSON.stringify(out.info.trace));

    step("★4 条中文小条都在主界面（压缩中 / 已压缩 / 失败 / 切换）",
         box.querySelectorAll(".notice").length >= 4, String(box.querySelectorAll(".notice").length));
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
    dom = os.path.join(tmp, "probe_compact_dom.html")
    err = os.path.join(tmp, "probe_compact_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-compactprobe"),
             "--window-size=1400,920",
             "--virtual-time-budget=30000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  (Edge 超时，已强杀)")
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
    print("\n=== 上下文压缩 / 小条渲染无头回归 ===")
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
    print("\n  结论: [OK] compaction / agent-switched / model-switched / shell 全部渲染正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
