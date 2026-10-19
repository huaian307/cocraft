# -*- coding: utf-8 -*-
r"""把 23 套无头回归**一次跑完**（本地 / CI 都用这一个）。

用法（在项目根目录）：
    python tools\run_suite.py                 :: 全部（本地开发机）
    python tools\run_suite.py --ci            :: CI 模式（跳过需要本机组件的两套）
    python tools\run_suite.py --only dispatch_test,team_test
    python tools\run_suite.py --start trust_panel_test     :: 从某一套开始
    python tools\run_suite.py --timeout 900

前置：
  * UI 类测试（大部分）只从 **8787** 取静态文件（index.html / app.js / style.css），
    所以本机要有面板服务；CI 里用 `python -m http.server 8787 --directory frontend` 即可。
  * `theme_ui_test` 需要本机已安装主题包；`notify_desktop_test` 需要托盘组件（pystray）；
    `agent_install_test` 需要随包 node/npm。`--ci` 会跳过这三套并说明原因。

输出：每套的完整日志落到 `<项目>/runtime/logs/suite/<name>.log`（runtime/ 已 gitignore）。
退出码：0 = 全过；1 = 有失败 / 缺前置。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
LOG_DIR = os.path.join(HERE, "runtime", "logs", "suite")

# 顺序 = 从快到慢（ACP 两套最后，启动 agent 要 20s+）
TESTS = [
    "theme_ui_test",
    "music_bar_test",
    "history_page_test",
    "trace_time_test",
    "compact_render_test",
    "cocraft_unread_test",
    "cocraft_activity_test",
    "session_tree_test",
    "session_progress_test",
    "quarantine_test",
    "quarantine_ui_test",
    "undo_turn_test",
    "dispatch_test",
    "team_test",
    "trust_panel_test",
    "context_chip_test",
    "acp_session_sync_test",
    "agent_install_test",
    "acp_events_test",
    "acp_api_test",
    "watch_mode_test",
    "notify_desktop_test",
    "acp_engine_ui_test",
]

# CI 里没有本机组件 → 跳过（不是"失败"，会在汇总里标 SKIP）
CI_SKIP = {
    "theme_ui_test": "需要本机已安装 cocraft-tech / cocraft-dark 主题包",
    "notify_desktop_test": "需要 pystray（本地 runtime/venvs/panel）",
    "agent_install_test": "需要随包 node/npm（本地 agents/）",
}


def parse_args():
    ap = argparse.ArgumentParser(description="run cocraft headless regression suite")
    ap.add_argument("--ci", action="store_true", help="CI 模式：跳过需要本机组件的测试")
    ap.add_argument("--only", default="", help="只跑这些（逗号分隔）")
    ap.add_argument("--start", default="", help="从这一套开始（含）")
    ap.add_argument("--timeout", type=int, default=900, help="单套超时秒数（默认 900）")
    return ap.parse_args()


def main() -> int:
    args = parse_args()
    names = list(TESTS)
    if args.only:
        want = [x.strip() for x in args.only.split(",") if x.strip()]
        unknown = [x for x in want if x not in TESTS]
        if unknown:
            print("[BAD] 不认识的测试：%s" % ", ".join(unknown))
            return 1
        names = want
    if args.start:
        if args.start not in names:
            print("[BAD] --start 不在本次列表里：%s" % args.start)
            return 1
        names = names[names.index(args.start):]

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    os.makedirs(LOG_DIR, exist_ok=True)

    results = []
    started_all = time.time()
    for name in names:
        if args.ci and name in CI_SKIP:
            print("[SKIP] %s（%s）" % (name, CI_SKIP[name]))
            results.append((name, "skip", 0.0, ""))
            continue
        path = os.path.join(HERE, "tools", name + ".py")
        if not os.path.isfile(path):
            print("[MISS] %s（找不到 tools/%s.py）" % (name, name))
            results.append((name, "miss", 0.0, "文件不存在"))
            continue
        t0 = time.time()
        try:
            p = subprocess.run([PY, path], cwd=HERE, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=args.timeout)
            rc = p.returncode
            raw = p.stdout or b""
        except subprocess.TimeoutExpired as exc:
            rc = -999
            raw = (exc.output or b"") + ("\n[TIMEOUT %ss]" % args.timeout).encode()
        dur = time.time() - t0
        text = raw.decode("utf-8", "replace")
        with open(os.path.join(LOG_DIR, name + ".log"), "w", encoding="utf-8") as fh:
            fh.write(text)
        tail = [ln.rstrip() for ln in text.splitlines() if ln.strip()][-3:]
        print("[%s] %s  %.1fs" % ("OK  " if rc == 0 else "FAIL", name, dur))
        for ln in tail:
            print("      " + ln[:200])
        sys.stdout.flush()
        results.append((name, "ok" if rc == 0 else "fail", dur, ""))

    total = time.time() - started_all
    print("\n" + "=" * 66)
    print("SUMMARY  (%.1fs)" % total)
    failed = 0
    for name, st, dur, note in results:
        if st == "fail":
            failed += 1
        print("  %-5s %-22s %7.1fs %s" % (st.upper(), name, dur, note))
    n_ok = sum(1 for _, st, _, _ in results if st == "ok")
    n_skip = sum(1 for _, st, _, _ in results if st == "skip")
    print("\n  %d ok / %d failed / %d skipped / %d total" %
          (n_ok, failed, n_skip, len(results)))
    if failed:
        print("  日志：%s" % LOG_DIR)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
