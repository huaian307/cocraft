# -*- coding: utf-8 -*-
"""CSS 未使用规则扫描（只报告，不删除）。

思路（保守）：把 HTML + 所有 JS 里出现的**任何标识符**（[A-Za-z][\\w-]*）当成"可能用到"，
只会报告那些**完全没在 HTML/JS 里出现过**的 CSS class —— 这类是"高置信度死代码"。
误报主要来自"纯巧合同名"（比如 CSS 里有个 .add、JS 里恰好有个变量叫 add），所以只当报告看。

用法: python tools/css_scan.py [--show]   （--show 打印逐个未使用 class）
"""

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
WEB = os.path.join(ROOT, "frontend")


def all_text() -> str:
    parts = [open(os.path.join(WEB, "index.html"), encoding="utf-8").read()]
    jsdir = os.path.join(WEB, "js")
    if os.path.isdir(jsdir):
        for fn in sorted(os.listdir(jsdir)):
            if fn.endswith(".js"):
                parts.append(open(os.path.join(jsdir, fn), encoding="utf-8").read())
    parts.append(open(os.path.join(WEB, "app.js"), encoding="utf-8").read())
    return "\n".join(parts)


def main() -> int:
    parts = []
    cssdir = os.path.join(WEB, "style")
    if os.path.isdir(cssdir):
        for fn in sorted(os.listdir(cssdir)):
            if fn.endswith(".css"):
                parts.append(open(os.path.join(cssdir, fn), encoding="utf-8").read())
    legacy = os.path.join(WEB, "style.css")
    if os.path.isfile(legacy):
        parts.append(open(legacy, encoding="utf-8").read())
    css = "\n".join(parts)
    css_nc = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)

    # CSS 里定义的 class（去掉数字/单位误匹配：要求前面不是数字）
    css_classes = set(re.findall(r"\.([A-Za-z_][\w-]*)", css_nc))

    text = all_text()
    # HTML/JS 里出现过的所有标识符（宽松）
    words = set(re.findall(r"[A-Za-z][\w-]*", text))

    unused = sorted(c for c in css_classes if c not in words)
    used = sorted(css_classes - set(unused))

    print(f"CSS class 总数: {len(css_classes)}")
    print(f"完全未出现（高置信度死代码）: {len(unused)}")
    print(f"有出现（保留）: {len(used)}")

    # 顺便统计选择器 / 规则数
    rules = len(re.findall(r"\{", css_nc))
    print(f"规则块数（约）: {rules}")

    if "--show" in sys.argv:
        for c in unused:
            print("  ." + c)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
