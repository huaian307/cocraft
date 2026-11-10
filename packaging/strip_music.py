# -*- coding: utf-8 -*-
r"""把「音乐 / 音频条 / QQ音乐(SMTC) / 网易云服务」从**已经构建好的 payload** 里去干净。

设计原则（踩过的坑写在前面）：
  * **只删文件是不够的**：`40-music.js` 里定义着 `syncLyricsVisibility / renderPlayer /
    initMusic` 等 44 个符号，`02-shell.js`、`11-engine.js`、`41-visual.js`、`app.js` 都在调用它们。
    直接删文件 → 面板一启动就 `xxx is not defined`（用户实测报过这个错）。
    所以 **JS 模块保留、加"lite 开关"提前 return**，只把「看得见的入口」和「网络/后台链路」去掉。
  * **只删 UI 也是不够的**：`renderPlayer()` 里 `bar.classList.toggle(...)` 没判空，
    `initMusic()` 里 `$("p-prev").addEventListener(...)` 也一样 → 删了 DOM 必须同时加守卫。
  * 设置里那几行（音乐登录 / 音频条 / 音乐条背景）删掉后，`initSettings()` 里的
    `$("cfg-xxx").addEventListener(...)` 会拿到 null → 统一换成 `__liteOn(id, ev, fn)`。

删除范围：
  文件      tools/spectrum.py · tools/smtc-daemon.ps1 · tools/smtc-control.ps1 ·
            tools/probe_smtc.ps1 · tools/music_service.py · tools/music_bar_test.py ·
            frontend/style/03-music.css · components/music/**
  DOM       #spec-left/right · #mus-home · #player · #vis-bar · #lyrics-overlay ·
            #lyric-ball · 工具菜单「音乐」· 设置里的音乐登录/音频条/歌词翻译/音乐条背景
  保留      #mus-audio（隐藏 <audio>，`initMusicAudio()` 会取它；加了守卫但留着更稳）

用法：
    python packaging\strip_music.py                 # 处理 dist\payload\app
    python packaging\strip_music.py <app目录>
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DEFAULT_APP = os.path.join(ROOT, "dist", "payload", "app")

LITE_FLAG = "window.__COCRAFT_NO_MUSIC"

# 40-music.js 的**空实现桩**：符号名与原模块一致（否则 02-shell / 11-engine / 41-visual /
# app.js 里的调用点会 `xxx is not defined`），但不含任何 QQ音乐 / SMTC / 歌词 / 频段逻辑。
MUSIC_STUB = """/* 40-music.js —— lite 包的空实现桩（由 packaging/strip_music.py 生成）
 *
 * 这个包里**没有**音乐 / 音频条：
 *   - 没有 QQ音乐 SMTC 采集（tools/smtc-*.ps1）、没有真频谱（tools/spectrum.py）、
 *     没有网易云/QQ 搜歌服务（components/music、tools/music_service.py）；
 *   - 播放条 / 音乐主页 / 歌词浮层 / 两侧频段的 DOM 与 CSS 也已移除。
 *
 * 保留同名符号只为"别的模块还在调用它们"，全部是空实现；`%s` 为 true。
 */
%s = true;

const MUSIC = {
  state: {}, panel: null, panelError: "", lyrics: [], shown: false, folded: true,
  lyrWanted: false, lyrKey: "", on: false,
};
/* ⚠ `MUS` 不要在这里声明：它本来就定义在 41-visual.js（顶层 const 是**跨 script 共享**的，
   重复声明会让 41-visual.js 整份 SyntaxError → initWallpaperPicker 之类全部 undefined）。 */
const LB = { idle: true, edge: "right", timer: null };

const VIS_TOP = 88;
const VIS_GAMMA = 1.7;
const VIS_GATE = 0.08;
const VIS_WEIGHT = 48;
const LYR_PREF_KEY = "cocraft.lyrics";
const LB_KEY = "cocraft.lyricball";

let _lyricWasPlaying = false;
let spectrumBusy = false;
let spectrumWasPlaying = false;

function musicPlaying() { return false; }
function currentTrack() { return null; }
function musicPos() { return 0; }
function lyricsWanted() { return false; }
function lyricsIndexAt() { return -1; }
function visHeight() { return 0; }
function pickEven(a) { return a; }

function loadLyricsPref() { return false; }
function saveLyricsPref() {}
function setPlayerFold() {}
function setPlayerShown() {}
function renderPlayer() {}
function updateSeek() {}
function renderLyrics() {}
function updatePlayerLyric() {}
function highlightLyrics() {}
function ensureLyrics() {}
function syncLyricsVisibility() {}
function loadLyricBall() {}
function saveLyricBall() {}
function applyLyricBall() {}
function updateLyricBallState() {}
function lbCancelIdle() {}
function lbScheduleIdle() {}
function initLyricBall() {}
function updateLyricBtn() {}
function pollMusic() {}
function buildVis() {}
function paintBars() {}
function paintSides() {}
function drawSpectrum() {}
function initMusic() {}

/* 工具菜单高亮（原来和音乐一起在 40-music.js 里）：只留「在线协作」那一半 */
function syncHubBtn() {
  try {
    const ch = $("tm-chat");
    if (ch) ch.classList.toggle("on", (typeof cocraftIsOpen === "function") && cocraftIsOpen());
  } catch (e) { /* ignore */ }
}
""" % (LITE_FLAG, LITE_FLAG)


def _is_stub(path: str) -> bool:
    try:
        with open(path, encoding="utf-8") as fh:
            return "lite 包的空实现桩" in fh.read(2000)
    except OSError:
        return False


def log(msg: str) -> None:
    print(msg, flush=True)


# ----------------------------------------------------------------- 文件
FILES = (
    r"tools\spectrum.py",
    r"tools\smtc-daemon.ps1",
    r"tools\smtc-control.ps1",
    r"tools\probe_smtc.ps1",
    r"tools\music_service.py",
    r"tools\music_bar_test.py",
    r"frontend\style\03-music.css",
)


def drop_files(app: str) -> None:
    for rel in FILES:
        p = os.path.join(app, rel)
        for cand in (p, p + "c"):
            if os.path.isfile(cand):
                os.remove(cand)
                log("  - 文件 %s" % cand[len(app) + 1:])
    comp = os.path.join(os.path.dirname(app), "components", "music")
    if os.path.isdir(comp):
        import shutil
        shutil.rmtree(comp, ignore_errors=True)
        log("  - 组件 components/music")


# ----------------------------------------------------------------- HTML
def _remove_div(html: str, needle: str) -> tuple[str, bool]:
    """删掉**包含 needle 的那个 <div> 块**（按标签配对，不是找第一个 </div>）。"""
    idx = html.find(needle)
    if idx < 0:
        return html, False
    start = html.rfind("<div", 0, idx)
    if start < 0:
        return html, False
    depth = 0
    pos = start
    pat = re.compile(r"<div\b|</div>")
    while True:
        m = pat.search(html, pos)
        if not m:
            return html, False
        if m.group(0) == "</div>":
            depth -= 1
            if depth == 0:
                end = m.end()
                # 吃掉行尾空白 + 一个换行（避免留空行）
                nl = html.find("\n", end)
                if nl != -1 and not html[end:nl].strip():
                    end = nl + 1
                return html[:start] + html[end:], True
        else:
            depth += 1
        pos = m.end()


HTML_TASKS = (
    ("#spec-left/right", lambda h: (
        h.replace('      <div id="spec-left" class="spec-side" aria-hidden="true"></div>\n', "", 1)
         .replace('      <div id="spec-right" class="spec-side" aria-hidden="true"></div>\n', "", 1))),
    ("#mus-home", lambda h: _remove_section(h, 'id="mus-home"')),
    ("#player", lambda h: _remove_div(h, 'id="player" class="player"')),
    ("#vis-bar", lambda h: h.replace(
        '      <div id="vis-bar" class="vis-bar" aria-hidden="true"></div>\n', "", 1)),
    ("#lyrics-overlay", lambda h: _remove_div(h, 'id="lyrics-overlay"')),
    # ⚠ 气泡球是 <button>（不是 div），用 _remove_div 会误删/漏删
    ("#lyric-ball", lambda h: re.sub(
        r'\n[ \t]*<button[^>]*id="lyric-ball"[\s\S]*?</button>', "", h, count=1)),
    ("设置·歌词翻译", lambda h: _remove_div(h, 'id="cfg-zh"')),
    ("设置·音乐登录 网易云", lambda h: _remove_div(h, 'id="cfg-ck-netease"')),
    ("设置·音乐登录 QQ", lambda h: _remove_div(h, 'id="cfg-ck-qq"')),
    ("设置·对话两侧音频条", lambda h: _remove_div(h, 'id="cfg-spec-sides"')),
    ("设置·音频条高度", lambda h: _remove_div(h, 'id="cfg-spec-h"')),
    ("设置·音频条起伏", lambda h: _remove_div(h, 'id="cfg-spec-gain"')),
    ("设置·音乐条背景", lambda h: _remove_div(h, 'data-img="playerbg"')),
)


def _remove_section(html: str, needle: str) -> tuple[str, bool]:
    """删掉包含 needle 的 <section> 块（按标签配对）。"""
    idx = html.find(needle)
    if idx < 0:
        return html, False
    start = html.rfind("<section", 0, idx)
    if start < 0:
        return html, False
    depth = 0
    pos = start
    pat = re.compile(r"<section\b|</section>")
    while True:
        m = pat.search(html, pos)
        if not m:
            return html, False
        if m.group(0) == "</section>":
            depth -= 1
            if depth == 0:
                end = m.end()
                nl = html.find("\n", end)
                if nl != -1 and not html[end:nl].strip():
                    end = nl + 1
                return html[:start] + html[end:], True
        else:
            depth += 1
        pos = m.end()


def patch_html(app: str) -> None:
    p = os.path.join(app, "frontend", "index.html")
    with open(p, encoding="utf-8") as fh:
        html = fh.read()
    before = len(html)

    # 1) 去掉 03-music.css
    html2 = re.sub(r'\s*<link[^>]*03-music\.css[^>]*>\n', "\n", html)
    if html2 != html:
        log("  - <link 03-music.css>")
        html = html2

    # 2) 工具菜单里的「音乐」
    html2 = re.sub(r'\n[ \t]*<button[^>]*id="tm-music"[^>]*>[^<]*</button>', "", html)
    if html2 != html:
        log("  - 工具菜单「音乐」")
        html = html2
    # 顶部工具按钮的 tooltip 里也写着「音乐」
    html2 = html.replace('title="工具：在线协作 / 音乐 / 面板终端 / 文件查看"',
                         'title="工具：在线协作 / 面板终端 / 文件查看"')
    if html2 != html:
        log("  - #btn-tools 的 tooltip")
        html = html2

    # 3) 各块
    for label, fn in HTML_TASKS:
        res = fn(html)
        if isinstance(res, tuple):
            html, ok = res
            if ok:
                log("  - DOM %s" % label)
        else:
            if res != html:
                log("  - DOM %s" % label)
                html = res

    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    log("  * index.html %.1f KB → %.1f KB" % (before / 1024, len(html) / 1024))


# ----------------------------------------------------------------- JS
def _prepend(path: str, text: str) -> bool:
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if text.strip() in src:
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text + "\n" + src)
    return True


def _guard(path: str, signature: str, guard: str) -> bool:
    """在函数体第一行插入守卫。"""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    if guard.strip() in src:
        return False
    idx = src.find(signature)
    if idx < 0:
        log("  [!] 找不到 %s" % signature)
        return False
    brace = src.find("{", idx)
    if brace < 0:
        log("  [!] %s 没有函数体" % signature)
        return False
    out = src[:brace + 1] + "\n" + guard + src[brace + 1:]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(out)
    return True


def patch_js(app: str) -> None:
    js = os.path.join(app, "frontend", "js")
    music = os.path.join(js, "40-music.js")
    visual = os.path.join(js, "41-visual.js")
    engine = os.path.join(js, "11-engine.js")

    # 40-music.js：整份换成**空实现桩** —— QQ音乐 / SMTC / 歌词 / 频段的代码不再随包，
    # 但 02-shell / 11-engine / 41-visual / app.js 里那些调用点仍然拿得到同名符号（不会再 undefined）。
    if not _is_stub(music):
        with open(music, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(MUSIC_STUB)
        log("  - 40-music.js 换成空实现桩（QQ音乐 / 歌词 / 频段代码不再随包）")

    # 41-visual.js：音频元素监听 + 恢复播放状态
    for sig in ("function initMusicAudio(", "function restoreMusicState("):
        name = sig.split("(")[0].replace("function ", "")
        guard = "  if (%s) return;  // lite 包：%s 已停用" % (LITE_FLAG, name)
        if _guard(visual, sig, guard):
            log("  - 41-visual.js 守卫 %s" % name)

    # 11-engine.js：设置项的绑定换成空安全的 __liteOn
    with open(engine, encoding="utf-8") as fh:
        src = fh.read()
    if "__liteOn" not in src:
        helper = (
            "\n/* lite 包（packaging/strip_music.py）：设置项可能整块被删掉，绑定要判空。 */\n"
            "function __liteOn(id, ev, fn) { const el = $(id); if (el) el.addEventListener(ev, fn); }\n"
        )
        anchor = "function initSettings("
        i = src.find(anchor)
        if i >= 0:
            src = src[:i] + helper.lstrip("\n") + src[i:]
            log("  - 11-engine.js 插入 __liteOn 助手")
    n = 0
    for cid in ("cfg-zh", "cfg-ck-netease", "cfg-ck-qq",
                "cfg-spec-sides", "cfg-spec-h", "cfg-spec-gain"):
        old = '$("%s").addEventListener(' % cid
        new = '__liteOn("%s", ' % cid
        if old in src:
            src = src.replace(old, new)
            n += 1
    if n:
        log("  - 11-engine.js 设置绑定改为空安全 ×%d" % n)
    with open(engine, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(src)


def main(argv) -> int:
    app = os.path.abspath(argv[1]) if len(argv) > 1 else DEFAULT_APP
    if not os.path.isdir(app):
        log("[BAD] 找不到 app 目录：%s" % app)
        return 1
    log("[strip] %s" % app)
    drop_files(app)
    patch_html(app)
    patch_js(app)
    log("[strip] 完成（音乐 / 音频条 / QQ音乐 / 网易云服务 已移除）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
