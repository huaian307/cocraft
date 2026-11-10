# -*- coding: utf-8 -*-
r"""无头测试：面板的 Markdown 渲染（renderText / renderBlocks / inlineMd）。

用一个固定的 Markdown 样例，在无头 Edge 里跑真实前端代码，把渲染结果抓回来断言：
表格 / 列表 / 标题 / 引用 / 分割线 / 链接 / 加粗 / 斜体 / 删除线 / 代码块都要出标签，
而 `javascript:` 链接必须被拦掉（只留文字）。

前置：面板服务在 8787 运行（打开 frontend/ 即可）。
用法：python tools\md_render_test.py
"""

from __future__ import annotations

import io
import json
import html as htmlmod
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(HERE, "frontend")
PROBE = os.path.join(WEB, "_probe_md.html")
URL = "http://127.0.0.1:8787/_probe_md.html"

sys.path.insert(0, os.path.join(HERE, "tools"))
from inspect_ui import find_edge  # noqa: E402  复用 Edge 定位

STUB = """<script>
(function () {
  try {
    localStorage.setItem("cocraft.setup.v1", "1");     // 别让首次向导挡住截图
    localStorage.setItem("opencode-ui.setup.v1", "1");
  } catch (e) {}
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/", "/live/"];
  var raw = window.fetch;
  window.fetch = function (u, o) {
    var s = String(u);
    for (var i = 0; i < DEAD.length; i++) {
      if (s.indexOf(DEAD[i]) >= 0) return Promise.resolve(new Response("{}", { status: 200 }));
    }
    return raw.apply(this, arguments);
  };
  try { navigator.sendBeacon = function () { return true; }; } catch (e) {}
  window.EventSource = function () { this.close = function () {}; };
  window.__probeErr = [];
  window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
})();
</script>
"""

SAMPLE = (
    "# 标题一\n\n段落 **加粗** *斜体* `代码` ~~删~~ "
    "[好](https://example.com) [坏](javascript:alert(1))\n\n"
    "- 甲\n- 乙\n\n1. 一\n2. 二\n\n> 引用一行\n\n"
    "| 名称 | 值 |\n| --- | --- |\n| a | 1 |\n| b | 2 |\n\n---\n\n"
    "```py\nprint(\"hi\")\n```\n"
    # ★LaTeX（`ui 0.2.87`）：`\(...\)` / `$...$` 翻成可读文本；`<` `>` 不许被吞
    "给定数组 \\(a_1, a_2, \\dots, a_n\\)，选整数 \\(k\\)（\\(1 \\le k \\le n\\)）。\n\n"
    "行内：$x^2 + y^2 = z^2$，比较 $a < b$ 与 $a > b$。\n\n"
    "\\[\n\\frac{n(n-1)}{2} \\cdot \\sqrt{x+1}\n\\]\n\n"
)

SCRIPT = """<script>
setTimeout(function () {
  var out = {};
  try { out.html = renderText(__SAMPLE__); }
  catch (e) { out.error = String(e); }
  out.jsErrors = window.__probeErr;
  var pre = document.createElement("pre");
  pre.id = "__probe__";
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
}, 2500);
</script>
""".replace("__SAMPLE__", json.dumps(SAMPLE, ensure_ascii=False))


SHOT_JS = """<style>
  #msg-static { padding: 18px 22px !important; }
</style>
<script>
setTimeout(function () {
  var host = document.getElementById("msg-static");
  if (!host) { host = document.body; }
  host.innerHTML = "";
  var box = document.createElement("div");
  box.className = "msg assistant";
  box.innerHTML = '<div class="who"><strong>助手</strong></div>'
    + '<div class="bubble">' + renderText(__SAMPLE__) + '</div>';
  host.appendChild(box);
  var box2 = document.createElement("div");
  box2.className = "msg assistant";
  box2.innerHTML = '<div class="who"><strong>助手</strong></div><div class="bubble">'
    + renderText("出错示例：\\n\\n> ⚠ 余额不足（账户/凭据问题）：AcpError ... Insufficient Balance "
                 + "(request_id: cefc94cc) · 去厂商后台充值，或换一个有额度的 Key（设置 → 模型凭据）；"
                 + "换模型/重试都没用")
    + '</div>';
  host.appendChild(box2);
  try { document.getElementById("messages").scrollTop = 0; window.scrollTo(0, 0); } catch (e) {}
}, 2200);
</script>
""".replace("__SAMPLE__", json.dumps(SAMPLE, ensure_ascii=False))


def build_probe(shot: bool = False):
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    extra = (STUB + SHOT_JS) if shot else (STUB + SCRIPT)
    out = src.replace('<script src="/app.js', extra + '<script src="/app.js')
    if out == src:
        raise SystemExit("在 index.html 里找不到 app.js 的 <script> 标签")
    io.open(PROBE, "w", encoding="utf-8", newline="").write(out)


def render(edge):
    tmp = os.environ.get("TEMP", ".")
    dom = os.path.join(tmp, "probe_md_dom.html")
    if os.path.exists(dom):
        os.remove(dom)
    with open(dom, "wb") as fh:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-mdtest"),
             "--virtual-time-budget=12000", "--dump-dom", URL],
            stdout=fh, stderr=subprocess.DEVNULL)
        try:
            proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  (Edge 超时，已强杀)")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    edge = find_edge()
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        shot = sys.argv[i + 1] if i + 1 < len(sys.argv) else os.path.join(
            HERE, "resources", "screenshots", "md_math.png")
        shot = os.path.abspath(shot)
        build_probe(shot=True)
        try:
            tmp = os.environ.get("TEMP", ".")
            with open(os.path.join(tmp, "probe_md_shot_err.txt"), "wb") as fh:
                proc = subprocess.Popen(
                    [edge, "--headless=new", "--disable-gpu", "--no-first-run",
                     "--no-default-browser-check",
                     "--user-data-dir=" + os.path.join(tmp, "edge-mdshot"),
                     "--window-size=1200,900", "--virtual-time-budget=12000",
                     "--screenshot=" + shot, URL],
                    stdout=subprocess.DEVNULL, stderr=fh)
                try:
                    proc.wait(timeout=60)
                except subprocess.TimeoutExpired:
                    proc.kill()
            print("  截图:", shot, "存在=", os.path.isfile(shot))
        finally:
            if os.path.exists(PROBE):
                os.remove(PROBE)
        return 0

    build_probe()
    try:
        html = render(edge)
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)

    m = re.search(r'<pre id="__probe__">(.*?)</pre>', html, re.S)
    if not m:
        print("[BAD] 没拿到探针结果。DOM 前 300 字符：")
        print(html[:300])
        return 1
    d = json.loads(m.group(1))
    got = htmlmod.unescape(d.get("html") or "")
    print("\n=== Markdown 渲染实测 ===")
    print("  渲染片段:", got[:220].encode("ascii", "replace").decode())

    must = ["<h1>", "<strong>加粗</strong>", "<em>斜体</em>", "<code>代码</code>",
            "<del>删</del>", '<a href="https://example.com"', "<ul>", "<ol>",
            "<blockquote>", "<table>", "<th>名称</th>", "<td>1</td>", "<hr>", "<pre>",
            # ★LaTeX → 可读文本（0.2.87）
            '<span class="math">', '<div class="md-math">',
            "≤", "…", "(n(n-1))/(2)", "√(x+1)", "·"]
    missing = [x for x in must if x not in got]
    # 反斜杠命令不许原样漏出来；`<`/`>` 必须仍然可见（以前担心被当标签吞掉）
    bad = [x for x in ["javascript:", "| --- |", "\\le", "\\dots", "\\frac", "\\sqrt"]
           if x in got]
    if "a &lt; b" not in got or "a &gt; b" not in got:
        bad.append("比较符号 < / > 被吞了")
    err = d.get("error") or (d.get("jsErrors") or [])

    print("  缺失标签:", missing)
    print("  不该出现:", bad)
    print("  脚本异常:", d.get("error"), " JS错误:", d.get("jsErrors"))
    ok = (not missing) and (not bad) and not err
    print("\n  结论:", "[OK] Markdown 渲染正确（含 XSS 拦截）" if ok else "[BAD] 见上")
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
