# -*- coding: utf-8 -*-
r"""无头回归：消息「往上翻页」读更早的历史。

做法与 inspect_ui.py 同一套：由 frontend/index.html 生成一个探针页，
拦掉会惊动守护进程 / 卡死无头渲染的请求（/heartbeat、/bye、SSE、/form、/permission、/qq/），
把 `/api/session*` 换成**内存里的假会话 + 250 条消息**（分页形状忠实模仿真实接口），
再用 Edge --headless 真跑一遍 app.js，断言：

  1. 首屏只取最新一页（80 条），且**没有**被误判成"没有更早的"；
  2. 点「↑ 加载更早的消息」→ 接上一页（160 条），且**视口锚住**（同一条消息停在原位，不跳）；
  3. 往上滚到顶 → **自动**再翻一页；
  4. 一直翻到底 → 250 条齐全，提示变成「没有更早的消息了」；
  5. 刷新（refreshMessages）**不丢**已翻出来的历史（olderKept）；
  6. ★两种游标形状都认：OpenCode `{previous,next}`（到底了也不为空）与 ACP 字符串（到底给 null）；
  7. 全程零 JS 报错。

用法（需要面板服务已在 8787 运行 —— 只是给探针页提供静态文件）：
    python tools\history_page_test.py
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
PROBE = os.path.join(WEB, "_probe_hist.html")
URL = "http://127.0.0.1:8787/_probe_hist.html"

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = """<script>
(function () {
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/api/event"];
  var raw = window.fetch;

  // ---- 假数据：250 条消息（旧 → 新），每 3 条来一个用户消息 ----
  var DATASET = (function () {
    var N = 250, ALL = [];
    for (var i = 1; i <= N; i++) {
      var base = 1700000000000 + i * 1000;
      if (i % 3 === 0) {
        ALL.push({ id: "m" + i, type: "user", text: "问题 " + i, time: { created: base } });
      } else {
        ALL.push({ id: "m" + i, type: "assistant", agent: "build",
                   model: { id: "probe-1", providerID: "acp" },
                   time: { created: base, completed: base + 400 },
                   content: [{ type: "text", text: "回答 " + i }] });
      }
    }
    return ALL;
  })();
  var SESS = { id: "ses_probe", title: "翻页探针",
               time: { created: 1700000000000, updated: 1700000250000 },
               location: { directory: "D:\\\\probe" },
               model: { id: "probe-1", providerID: "acp" } };
  var PAGE = 80;
  window.__msgReqs = [];
  window.__cursorStyle = "opencode";

  function jsonResp(obj, status) {
    return Promise.resolve(new Response(JSON.stringify(obj), {
      status: status || 200, headers: { "Content-Type": "application/json" },
    }));
  }

  // 忠实模仿真实接口的分页：cursor 指向"这一页之前的位置"。
  // ⚠ OpenCode 的 cursor 是对象 `{previous,next}`，而且**到底了 next 也不为空**；
  //   ACP 是字符串（上一页最旧一条的 id），到底时给 null。两种都要能翻。
  function msgPage(q) {
    var cur = q.get("cursor");
    var acp = window.__cursorStyle === "acp";
    var end = DATASET.length;
    if (cur) end = parseInt(String(cur).replace(/^c/, ""), 10) || 0;
    var start = Math.max(0, end - PAGE);
    var page = DATASET.slice(start, end);
    window.__msgReqs.push({ cursor: cur || null, start: start, got: page.length,
                            style: acp ? "acp" : "opencode" });
    var nextCursor = acp ? (start > 0 ? String(start) : null)
                         : { previous: "p" + end, next: "c" + start };
    return jsonResp({ data: page.slice().reverse(), cursor: nextCursor });
  }

  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    var qi = s.indexOf("?");
    var path = qi >= 0 ? s.slice(0, qi) : s;
    var q = new URLSearchParams(qi >= 0 ? s.slice(qi + 1) : "");
    if (path === "/api/session/ses_probe/message") return msgPage(q);
    if (s.indexOf("/api/session/ses_probe") === 0) {
      if (/\\/(model|agent|skill|command|permission|form)$/.test(path)) {
        return Promise.resolve(new Response("", { status: 204 }));
      }
      return jsonResp({ data: SESS });
    }
    if (path === "/api/session") return jsonResp({ data: [SESS], cursor: null });
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () {
    this.close = function () {};
    this.addEventListener = function () {};
  };
})();
</script>
"""

PROBE_JS = """<script>
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });

function __pSleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
/* 程序化滚动 + 手动派发 scroll 事件：真实浏览器会在下一帧自动派发，
   但无头 + 虚拟时间里没有合成帧，scroll 事件不一定来 —— 这里显式补上。 */
function __pScroll(v) {
  var b = document.getElementById("messages");
  b.scrollTop = v;
  b.dispatchEvent(new Event("scroll"));
  return b.scrollTop;
}
function __pTxt(el) { return el ? String(el.textContent || "").trim() : null; }
function __pMore() {
  var e = document.getElementById("msg-more");
  return (e && !e.hidden) ? __pTxt(e) : "";
}
/* 视口里第一条可见消息（用它的时间戳当身份，再记它在视口里的偏移） */
function __pFirstVisible() {
  var box = document.getElementById("messages");
  if (!box) return null;
  var br = box.getBoundingClientRect();
  var els = box.querySelectorAll("#msg-static .msg");
  for (var i = 0; i < els.length; i++) {
    var r = els[i].getBoundingClientRect();
    if (r.bottom > br.top + 1) {
      return { t: __pTxt(els[i].querySelector(".muted")), off: Math.round(r.top - br.top) };
    }
  }
  return null;
}

(async function () {
  var out = { steps: [], info: {} };
  var fail = function (n, extra) { out.steps.push([n, false, extra || ""]); };
  var pass = function (n, extra) { out.steps.push([n, true, extra || ""]); };
  var step = function (n, ok, extra) { out.steps.push([n, !!ok, extra || ""]); };
  try {
    var i;
    for (i = 0; i < 300; i++) {
      try { if (state && state.current && state.messages.length) break; } catch (e) {}
      await __pSleep(25);
    }
    var box = document.getElementById("messages");
    out.info.boot = { sess: state.current && state.current.id, count: state.messages.length,
                      done: state.msgDone, cursor: state.msgCursor, more: __pMore(), box: !!box };
    step("首屏：只取最新一页（80 条）且不误判'没有更早的'",
         state.messages.length === 80 && state.msgDone === false && !!state.msgCursor,
         "count=" + state.messages.length + " done=" + state.msgDone);
    step("首屏：顶部出现「↑ 加载更早的消息」",
         !!document.querySelector("#msg-more .mm-btn"), out.info.boot.more);

    /* ---- 2. 点按钮接上一页：视口要锚住 ---- */
    __pScroll(150);
    await __pSleep(60);
    var a0 = __pFirstVisible();
    var top0 = box.scrollTop, h0 = box.scrollHeight, n0 = state.messages.length;
    var btn = document.querySelector("#msg-more .mm-btn");
    if (btn) btn.click(); else fail("没找到「加载更早」按钮");
    await __pSleep(500);
    var a1 = __pFirstVisible();
    out.info.anchor = { before: a0, after: a1, top0: top0, top1: Math.round(box.scrollTop),
                        h0: h0, h1: box.scrollHeight, n0: n0, n1: state.messages.length };
    step("点按钮：接上更早一页（80 → 160）", state.messages.length === 160 && n0 === 80,
         n0 + " -> " + state.messages.length);
    step("★视口锚住：同一条消息停在原位（不跳）",
         !!a0 && !!a1 && a0.t === a1.t && Math.abs(a0.off - a1.off) <= 3,
         JSON.stringify(a0) + " -> " + JSON.stringify(a1));
    step("★滚动位置按'新增高度'平移（不是原地不动）",
         (box.scrollHeight - h0) > 100 && (box.scrollTop - top0) > 100,
         "dH=" + (box.scrollHeight - h0) + " dTop=" + Math.round(box.scrollTop - top0));

    /* ---- 3. 往上滚到顶 → 自动翻页 ---- */
    __pScroll(box.scrollHeight);                   // 先到底，让"上一次位置"追上
    await __pSleep(60);
    __pScroll(0);                                  // 往上滚到顶
    await __pSleep(500);
    out.info.auto = { count: state.messages.length, reqs: window.__msgReqs.length };
    step("★滚到顶部自动再翻一页", state.messages.length > 160,
         "count=" + state.messages.length);

    /* ---- 4. 一直翻到底 ---- */
    for (i = 0; i < 8 && !state.msgDone; i++) {
      var b2 = document.querySelector("#msg-more .mm-btn");
      if (!b2) break;
      b2.click();
      await __pSleep(320);
    }
    out.info.bottom = { count: state.messages.length, done: state.msgDone, more: __pMore(),
                        uniq: new Set(state.messages.map(function (m) { return m.id; })).size };
    step("★翻到底：250 条齐全、无重复", state.messages.length === 250 && out.info.bottom.uniq === 250,
         out.info.bottom.count + " / uniq=" + out.info.bottom.uniq);
    step("★到底后提示变成「没有更早的消息了」",
         state.msgDone === true && /没有更早/.test(__pMore()), out.info.bottom.more);
    step("★到底后不再出现按钮", !document.querySelector("#msg-more .mm-btn"));

    /* ---- 5. 刷新不丢历史 ---- */
    await refreshMessages();
    await __pSleep(200);
    out.info.refresh = { count: state.messages.length, older: state.msgOlder,
                         uniq: new Set(state.messages.map(function (m) { return m.id; })).size };
    step("★刷新（只重取最新一页）后仍保留已翻出的历史",
         state.messages.length === 250 && out.info.refresh.uniq === 250,
         JSON.stringify(out.info.refresh));

    /* ---- 6. 两种游标形状 ---- */
    out.info.cursorOf = {
      obj: msgCursorOf({ previous: "P", next: "N" }), str: msgCursorOf("m42"),
      nil: msgCursorOf(null), empty: msgCursorOf({}),
    };
    step("★两种游标形状都认（OpenCode {next} / ACP 字符串）",
         out.info.cursorOf.obj === "N" && out.info.cursorOf.str === "m42"
         && out.info.cursorOf.nil === null && out.info.cursorOf.empty === null,
         JSON.stringify(out.info.cursorOf));

    /* ---- 7. 换成 ACP 的游标形状（字符串 + 到底 null）再来一遍 ---- */
    window.__cursorStyle = "acp";
    resetMsgPaging();
    staticSig = null;
    await refreshMessages();
    await __pSleep(200);
    var c1 = state.messages.length;
    for (i = 0; i < 8 && !state.msgDone; i++) {
      var b3 = document.querySelector("#msg-more .mm-btn");
      if (!b3) break;
      b3.click();
      await __pSleep(300);
    }
    out.info.acp = { afterFirst: c1, final: state.messages.length, done: state.msgDone,
                     more: __pMore() };
    step("★ACP 形状（字符串游标 / 到底 null）也能翻到底",
         c1 === 80 && state.messages.length === 250 && state.msgDone === true,
         JSON.stringify(out.info.acp));

    /* ---- 8. 关掉跟随：往上读历史时不该被拽回底部 ---- */
    var box3 = document.getElementById("messages");
    beforeTop = __pScroll(Math.max(200, box3.scrollHeight - box3.clientHeight - 3000));  // 明显离开底部
    await __pSleep(100);
    out.info.follow = { top: Math.round(beforeTop), follow: state.follow,
                        bottomGap: Math.round(box3.scrollHeight - beforeTop - box3.clientHeight) };
    step("★离开底部就不再跟随（拖滚动条 / 键盘翻页也算，不只滚轮）", state.follow === false,
         "follow=" + state.follow + " 离底=" + out.info.follow.bottomGap);
    try { paintLiveSlot(); } catch (e) {}     // 这行内部就是 `if (state.follow) scrollTop = scrollHeight`
    await __pSleep(80);
    step("★不跟随时，新内容渲染不会把视口拽到底部",
         Math.abs(box3.scrollTop - beforeTop) < 5,
         Math.round(beforeTop) + " -> " + Math.round(box3.scrollTop));
    __pScroll(box3.scrollHeight);             // 回到底部 → 恢复跟随
    await __pSleep(100);
    step("★滚回底部自动恢复跟随", state.follow === true, "follow=" + state.follow);
  } catch (err) {
    out.crash = String(err && err.stack ? err.stack : err);
  }
  out.info.reqs = window.__msgReqs.slice(0, 4).concat(
    window.__msgReqs.length > 4 ? [{ "...共": window.__msgReqs.length }] : []);
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
    dom = os.path.join(tmp, "probe_hist_dom.html")
    err = os.path.join(tmp, "probe_hist_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-histprobe"),
             "--window-size=1400,920",
             "--virtual-time-budget=40000", "--dump-dom", URL],
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
    print("\n=== 消息「往上翻页」无头回归 ===")
    if d.get("crash"):
        print("  探针异常:", d["crash"])
    for name, ok, extra in d.get("steps", []):
        print("  %s %s%s" % ("OK  " if ok else "FAIL", name, ("  [" + str(extra) + "]") if extra else ""))
    print("\n  JS 运行错误:", d.get("jsErrors"))
    print("  关键数据:")
    for k, v in (d.get("info") or {}).items():
        print("    %-10s %s" % (k, json.dumps(v, ensure_ascii=False)[:300]))

    bad = [n for n, ok, _ in d.get("steps", []) if not ok]
    if d.get("crash") or d.get("jsErrors") or bad:
        print("\n  结论: [BAD] %s" % (bad or "有 JS 错误 / 探针异常"))
        return 2
    print("\n  结论: [OK] 往上翻页（按钮 / 自动 / 锚定 / 到底 / 刷新不丢 / 两种游标）全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
