# -*- coding: utf-8 -*-
r"""主题工坊 · 无头回归：真渲染面板页，验证「应用用户主题 / 明暗切换 / 图标槽 / 恢复内置 / 列表」。

用一个探针页（结构 = index.html，拦掉 /heartbeat /bye /qq /form /permission 与 SSE），
在页面里调用真实函数，把结论写进 `#__probe__`，再用 Edge 无头 --dump-dom 取回。

用法（需要面板服务在运行，且含 /themes 路由；并已安装 cocraft-tech / cocraft-dark）：
    python tools\theme_ui_test.py [http://127.0.0.1:8787/_probe_themes.html]
也可用 env `COCRAFT_UI_URL` 指定。
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
PROBE = os.path.join(WEB, "_probe_themes.html")
URL = os.environ.get("COCRAFT_UI_URL") or (sys.argv[1] if len(sys.argv) > 1
                                           else "http://127.0.0.1:8787/_probe_themes.html")

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]

STUB = """<script>
(function () {
  var DEAD = ["/heartbeat", "/bye", "/form", "/permission", "/qq/"];
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
})();
window.__probeErr = [];
window.addEventListener("error", function (e) { window.__probeErr.push(String(e.message)); });
window.addEventListener("unhandledrejection", function (e) { window.__probeErr.push("rej: " + String(e.reason)); });
</script>
"""

TEST = """<script>
(function () {
  function done(out) {
    var pre = document.createElement("pre");
    pre.id = "__probe__";
    pre.textContent = JSON.stringify(out);
    document.body.appendChild(pre);
  }
  function paper() { return getComputedStyle(document.documentElement).getPropertyValue("--paper").trim(); }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  async function run() {
    var out = { errs: window.__probeErr.slice() };
    try {
      // ⚠ 2026-10-16 修正：**夜主题已在 `ui 0.1.50` 删除**（只剩「昼」`data-theme="hiru"`），
      //   所以这里不再有「点 夜/昼 按钮」的玩法 —— 断言改成"那个按钮确实不存在"，
      //   并且**不硬编码颜色值**（拿应用前的 `--paper` 当基准比大小），免得主题包一改就误报。
      //   ⚠ 还必须**先回内置再取基准**：探针页的 localStorage 会跨次复用，上次跑完留下的用户主题
      //     会让"基准"本身就是某个主题的颜色 → 后面的比较全错。
      applyTheme("hiru");
      await sleep(250);
      out.paperBefore = paper();
      out.baseBefore = document.documentElement.dataset.theme || "";
      out.hasNightBtn = !!document.getElementById("btn-theme");

      // 用户主题（双模式包）：套上后 data-theme-user 生效、变量真的变了
      await applyUserTheme("cocraft-tech");
      out.techUser = themeUser();
      out.techBase = document.documentElement.dataset.theme || "";
      out.techPaper = paper();
      out.techModes = _userThemeModes.slice();

      // 单模式主题：变量照样生效（不再有"按钮禁用"这回事 —— 按钮已经没了）
      await applyUserTheme("cocraft-dark");
      out.singleModes = _userThemeModes.length;
      out.singleUser = themeUser();
      out.singlePaper = paper();

      // 工坊列表
      await loadThemes();
      openThemes();
      out.builtinCount = TH.builtin.length;
      out.installedCount = TH.installed.length;
      out.listCards = document.querySelectorAll("#th-list .wp-it").length;

      // 恢复路径 A：点内置卡片「套用」
      var cardBtn = document.querySelector('#th-list [data-th-apply="hiru"]');
      out.foundBuiltinCard = !!cardBtn;
      if (cardBtn) { cardBtn.click(); await sleep(500); }
      out.cardUser = themeUser();
      out.cardPaper = paper();

      // 恢复路径 B：再套用户主题，点「恢复内置」
      await applyUserTheme("cocraft-tech");
      out.beforeResetUser = themeUser();
      var rstBtn = document.getElementById("th-reset");
      out.foundReset = !!rstBtn;
      if (rstBtn) { rstBtn.click(); await sleep(400); }
      out.afterResetUser = themeUser();
      out.afterResetPaper = paper();

      // 图标槽
      setThemeIcons({ tools: "/assets/cocraft.ico" });
      out.iconOn = document.querySelector("#btn-tools").hasAttribute("data-ico-on");
      setThemeIcons({});
      out.iconOff = !document.querySelector("#btn-tools").hasAttribute("data-ico-on");

      // 最终：直接回内置
      applyTheme("hiru");
      out.afterRevertUser = document.documentElement.dataset.themeUser || "";
      out.afterRevertPaper = paper();
      out.afterRevertBase = document.documentElement.dataset.theme || "";
      out.styleCleared = (function () { var s = document.getElementById("cocraft-theme"); return !s || !s.textContent; })();
      out.activeCards = document.querySelectorAll("#th-list .wp-it.th-on").length;
    } catch (e) {
      out.errs.push("run: " + String((e && (e.stack || e.message)) || e));
    }
    out.jsErrors = window.__probeErr.slice();
    done(out);
  }
  (function wait(n) {
    if (window.__appLoaded || n <= 0) { setTimeout(run, 350); return; }
    setTimeout(function () { wait(n - 1); }, 200);
  })(70);
})();
</script>
"""


def build_probe() -> None:
    src = io.open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    out = src.replace('<script src="/app.js', STUB + TEST + '<script src="/app.js')
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
    dom = os.path.join(tmp, "probe_theme_dom.html")
    err = os.path.join(tmp, "probe_theme_err.txt")
    for f in (dom, err):
        if os.path.exists(f):
            os.remove(f)
    with open(dom, "wb") as fh_dom, open(err, "wb") as fh_err:
        proc = subprocess.Popen(
            [edge, "--headless=new", "--disable-gpu", "--no-first-run",
             "--no-default-browser-check",
             "--user-data-dir=" + os.path.join(tmp, "edge-theme-test"),
             "--virtual-time-budget=25000", "--dump-dom", URL],
            stdout=fh_dom, stderr=fh_err)
        try:
            proc.wait(timeout=80)
        except subprocess.TimeoutExpired:
            proc.kill()
            print("  （Edge 超时，已强杀）")
    return io.open(dom, encoding="utf-8", errors="replace").read()


def main() -> int:
    edge = find_edge()
    build_probe()
    print("探针页已生成，正在无头渲染…")
    try:
        html = render(edge)
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)

    m = re.search(r'<pre id="__probe__">(.*?)</pre>', html, re.S)
    if not m:
        print("没拿到探针结果。DOM 前 400 字符：")
        print(html[:400])
        return 1
    d = json.loads(m.group(1))

    checks = [
        ("★夜主题已删：页面上没有「夜/昼」按钮（`#btn-theme`）", d.get("hasNightBtn") is False),
        ("底盘是「昼」`data-theme=hiru`", d.get("baseBefore") == "hiru" and d.get("techBase") == "hiru"),
        ("双模式：用户主题生效", d.get("techUser") == "cocraft-tech"),
        ("★双模式：用户主题**真的换了变量**（`--paper` 与内置不同）",
         bool(d.get("techPaper")) and d.get("techPaper") != d.get("paperBefore")),
        ("双模式：主题包自带两套明暗（light+dark）", len(d.get("techModes") or []) == 2),
        ("单模式：只一种明暗", d.get("singleModes") == 1),
        ("单模式：用户主题仍生效", d.get("singleUser") == "cocraft-dark"),
        ("单模式：变量仍生效", bool(d.get("singlePaper"))
         and d.get("singlePaper") != d.get("paperBefore")),
        ("★找得到内置卡片", d.get("foundBuiltinCard") is True),
        ("★点内置卡片能恢复（主题清掉）", d.get("cardUser") == ""),
        ("★点内置卡片能恢复（变量回内置值）", d.get("cardPaper") == d.get("paperBefore")),
        ("★找得到「恢复内置」按钮", d.get("foundReset") is True),
        ("★「恢复内置」清掉主题", d.get("afterResetUser") == ""),
        ("★「恢复内置」变量回内置值（不再有「回内置夜」这回事）",
         d.get("afterResetPaper") == d.get("paperBefore")),
        ("图标槽：加上", d.get("iconOn") is True),
        ("图标槽：清掉", d.get("iconOff") is True),
        ("恢复内置：清 data-theme-user", d.get("afterRevertUser") == ""),
        ("恢复内置：变量回内置值", d.get("afterRevertPaper") == d.get("paperBefore")),
        ("恢复内置：底盘仍是 hiru（夜主题已删）", d.get("afterRevertBase") == "hiru"),
        ("恢复内置：样式清空", d.get("styleCleared") is True),
        ("★内置主题 2 个（圣烛教堂默认 + 和纸白昼）", d.get("builtinCount") == 2),
        ("已安装 ≥ 3 个", (d.get("installedCount") or 0) >= 3),
        ("列表渲染出卡片", (d.get("listCards") or 0) >= 4),
        ("当前主题唯一高亮", (d.get("activeCards") or 0) == 1),
        ("无 JS 未捕获错误", not d.get("jsErrors")),
    ]
    ok = 0
    for name, cond in checks:
        print(("  OK   " if cond else "  FAIL ") + name)
        ok += 1 if cond else 0
    print("\n结论: PASS %d/%d" % (ok, len(checks)))
    if d.get("errs"):
        print("内部错误:", d["errs"])
    return 0 if ok == len(checks) else 2


if __name__ == "__main__":
    raise SystemExit(main())
