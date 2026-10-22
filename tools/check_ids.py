# -*- coding: utf-8 -*-
"""检查 app.js 里 $("id") 引用的元素是否存在。

存在性有两个来源：
  1) index.html 里静态写的 id="xxx"
  2) app.js 自己用模板串动态生成的 id="xxx"（例如弹窗里的 #ask-msg）
只查第 1 种会误报。

用法: python tools/check_ids.py
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "frontend")


def ids_declared(text: str) -> set:
    return set(re.findall(r'id="([^"]+)"', text))


html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
# P1 拆文件后，$("id") 引用分散到 js/*.js + app.js（boot），都要扫到。
_js_parts = []
_jsdir = os.path.join(WEB, "js")
if os.path.isdir(_jsdir):
    for _fn in sorted(os.listdir(_jsdir)):
        if _fn.endswith(".js"):
            _js_parts.append(open(os.path.join(_jsdir, _fn), encoding="utf-8").read())
_js_parts.append(open(os.path.join(WEB, "app.js"), encoding="utf-8").read())
js = "\n".join(_js_parts)
# P4 拆 CSS 后，样式在 style/*.css（拼接顺序与 index.html 的 <link> 一致）；旧单文件仍兼容。
_css_parts = []
_cssdir = os.path.join(WEB, "style")
if os.path.isdir(_cssdir):
    for _fn in sorted(os.listdir(_cssdir)):
        if _fn.endswith(".css"):
            _css_parts.append(open(os.path.join(_cssdir, _fn), encoding="utf-8").read())
if not _css_parts:
    _css_parts.append(open(os.path.join(WEB, "style.css"), encoding="utf-8").read())
css = "\n".join(_css_parts)

used = set(re.findall(r'\$\("([^"]+)"\)', js))
have = ids_declared(html) | ids_declared(js)

missing = sorted(used - have)
print(f"index.html 静态 id : {len(ids_declared(html))}")
print(f"app.js  动态 id    : {len(ids_declared(js))}")
print(f"JS 引用            : {len(used)}")
print("缺失:", missing if missing else "无")

# 顺手看一下：CSS 里有没有引用已经不存在的 id（死样式）
css_ids = set(re.findall(r"#([A-Za-z][\w-]*)\s*[{,:]", css))
dead = sorted(i for i in css_ids if i not in have and i not in
              {"petals", "lyrics-overlay"})
print("CSS 里可能已失效的 id:", dead if dead else "无")

sys.exit(1 if missing else 0)
