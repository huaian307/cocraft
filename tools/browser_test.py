#!/usr/bin/env python
"""browser_test.py -- 浏览器 CDP 工具回归（真起无头 Edge + 本地测试页）。

验的不是"调用了没报错"，而是**页面上真的发生了什么**：
  点按钮 → `#count` 从 0 变 1；打字 → `input.value` 精确等于输入的中文；
  `eval` 拿到 2；截图落盘且不是空文件；`wait` 找到/超时都对。

同时验护栏：默认 deny、`eval` 按动作管、危险词（发送）拒绝、审计不记正文。
全程临时目录（state / profile / 审计链），不碰真实运行态和用户浏览器。
"""

import http.server
import io
import json
import os
import socketserver
import sys
import tempfile
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import importlib.util

spec = importlib.util.spec_from_file_location("cumain", os.path.join(HERE, "cu.py"))
cumain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cumain)

import computer as cu  # noqa: E402
from computer import cdp  # noqa: E402
from computer import gate as cu_gate  # noqa: E402

RESULTS = []
TEST_PORT = 9345   # 故意避开默认 9333（别撞用户自己开的浏览器）

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>cocraft CDP Test</title></head><body>
<h1 id="t">hello cdp</h1>
<button id="btn" onclick="
  var c=document.getElementById('count');
  c.textContent = String(parseInt(c.textContent||'0',10)+1);">add one</button>
<div id="count">0</div>
<input id="name" placeholder="name">
<div id="send">发送</div>
<div id="later" style="display:none">later</div>
</body></html>"""


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("[OK]  " if cond else "[BAD] ") + name +
          (("  -> " + str(detail)) if (detail and not cond) else ""))
    return bool(cond)


def run(argv):
    """按 CLI 的真实行为跑：异常也转成 {"ok":false,"error":...}（测错误路径）。"""
    try:
        args = cumain.build_parser().parse_args(argv)
        return args.func(args, cu)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def read_audit(path):
    rows = []
    try:
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def main():
    tmp = tempfile.mkdtemp(prefix="cdp_test_")
    audit_path = os.path.join(tmp, "audit.jsonl")
    os.environ["COCRAFT_AUDIT_FILE"] = audit_path
    os.environ.pop("COCRAFT_CU_ACTIONS", None)
    os.environ["COCRAFT_CU_ACTOR"] = "cdp_test"
    # 隔离：状态文件 / profile / 端口都放临时目录，不碰真实运行态与用户浏览器
    cdp.STATE_FILE = os.path.join(tmp, "_cdp.json")
    cdp.PROFILE_DIR = os.path.join(tmp, "profile")
    cdp.DEFAULT_PORT = TEST_PORT
    print("temp dir: %s" % tmp)

    # ---------- 0. 静态：命令分级 ----------
    cases = [
        ("cu browser text --selector #t", ("browser.text", True)),
        ("cu browser list", ("browser.list", True)),
        ("cu browser shot --full", ("browser.shot", True)),
        ("cu browser click --selector #b", ("browser.click", False)),
        ("cu browser type --selector #q --text hi", ("browser.type", False)),
        ("cu browser eval --js 1+1", ("browser.eval", False)),   # eval 按动作管
        ("cu browser start --headless", ("browser.start", False)),
    ]
    ok = all(cu_gate.parse_cu_command(c) == want for c, want in cases)
    check("ACP 认得 browser 动词并分级（eval 算动作）", ok,
          [(c, cu_gate.parse_cu_command(c)) for c, _ in cases])
    check("browser 未知动词不当成 cu",
          cu_gate.parse_cu_command("cu browser nope") is None)
    check("找到 Edge/Chrome", bool(cdp.find_exe()), cdp.find_exe())

    # ---------- 1. 本地测试页 ----------
    with io.open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as f:
        f.write(PAGE)

    class QuietServer(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

        def handle_error(self, request, client_address):
            pass          # 浏览器被 stop 时会掐断连接，正常，别刷屏

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            kw["directory"] = tmp
            super().__init__(*a, **kw)

        def log_message(self, *a):
            pass

    httpd = QuietServer(("127.0.0.1", 0), Handler)
    web_port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/index.html" % web_port

    started = False
    try:
        # ---------- 2. 启浏览器 ----------
        t0 = time.time()
        st = run(["browser", "start", "--headless", "--allow-actions"])
        dt = time.time() - t0
        started = bool(st.get("ok"))
        check("无头 Edge 起来了", started and st.get("port") == TEST_PORT, st)
        check("冷启动 < 30s", dt < 30, "%.1fs" % dt)
        check("status 说在跑", run(["browser", "status"]).get("running") is True)

        # ---------- 3. 只读 ----------
        op = run(["browser", "open", "--url", url, "--headless", "--allow-actions"])
        check("open 新标签并跳转", op.get("ok") and op.get("url") == url, op)
        check("标签列表里有它", run(["browser", "list"]).get("count", 0) >= 1)
        tx = run(["browser", "text", "--selector", "#t"])
        check("text 读到真实文本", tx.get("text") == "hello cdp", tx.get("text"))
        ev = run(["browser", "eval", "--js", "1+1", "--allow-actions"])
        check("eval 1+1 = 2", ev.get("value") == 2, ev)
        miss = run(["browser", "text", "--selector", "#nope"])
        check("选择器不存在 → 明确报错", miss.get("ok") is False and "找不到" in (miss.get("error") or ""),
              miss)
        w1 = run(["browser", "wait", "--selector", "#count", "--timeout-ms", "2000"])
        check("wait 找到元素", w1.get("found") is True, w1)
        w2 = run(["browser", "wait", "--selector", "#nope", "--timeout-ms", "300"])
        check("wait 超时如实返回 found=false", w2.get("found") is False, w2)

        # ---------- 4. 动作 ----------
        denied = run(["browser", "click", "--selector", "#btn"])
        check("默认 deny：点击被拒", denied.get("ok") is False and denied.get("denied"), denied)
        denied_ev = run(["browser", "eval", "--js", "1"])
        check("默认 deny：eval 也被拒", denied_ev.get("ok") is False and denied_ev.get("denied"),
              denied_ev)

        c1 = run(["browser", "click", "--selector", "#btn", "--allow-actions"])
        check("click 走真实鼠标事件", c1.get("ok") and c1.get("method") == "mouse", c1)
        cnt = run(["browser", "text", "--selector", "#count"])
        check("点完 DOM 真的变了（0 → 1）", cnt.get("text") == "1", cnt.get("text"))

        typed = "张三 hello-42"
        tp = run(["browser", "type", "--selector", "#name", "--text", typed, "--allow-actions"])
        check("type 成功（insertText）", tp.get("ok"), tp)
        val = run(["browser", "eval", "--js", "document.querySelector('#name').value",
                   "--allow-actions"])
        check("输入框内容精确等于中文输入", val.get("value") == typed, val.get("value"))

        tp2 = run(["browser", "type", "--selector", "#name", "--text", "replaced",
                   "--clear", "--allow-actions"])
        val2 = run(["browser", "eval", "--js", "document.querySelector('#name').value",
                    "--allow-actions"])
        check("--clear 是替换不是追加", val2.get("value") == "replaced", val2.get("value"))

        danger = run(["browser", "click", "--selector", "#send", "--allow-actions"])
        check("危险词（发送）被拒", danger.get("ok") is False and danger.get("denied"), danger)
        check("拒绝原因说清楚", "发送" in (danger.get("error") or ""), danger.get("error"))

        shot = run(["browser", "shot", "--full"])
        check("截图落盘", shot.get("ok") and os.path.isfile(shot.get("path") or ""), shot)
        check("截图不是空文件", (shot.get("bytes") or 0) > 1000, shot.get("bytes"))

        # ---------- 5. 审计 ----------
        rows = [r for r in read_audit(audit_path) if r.get("source") == "cu"]
        tools = {(r.get("action") or {}).get("title") for r in rows}
        check("审计有 browser_* 记录", any(str(t).startswith("browser_") for t in tools),
              sorted(x for x in tools if x))
        check("每个 cu 记录的 kind=cu",
              all((r.get("action") or {}).get("kind") == "cu" for r in rows), "")
        check("被拒绝的动作入链", any(r.get("decision") == "denied" for r in rows),
              [r.get("decision") for r in rows][:8])
        raw = json.dumps(rows, ensure_ascii=False)
        check("审计不记输入正文", typed not in raw, "正文泄漏进链了")

        # ---------- 6. 收尾 ----------
        stp = run(["browser", "stop", "--allow-actions"])
        check("stop 关掉了浏览器", stp.get("ok") and not cdp.is_up(TEST_PORT), stp)
        check("stop 后 status 说没跑", run(["browser", "status"]).get("running") is False)
    except Exception:
        traceback.print_exc()
        check("测试过程无异常", False)
    finally:
        try:
            if started:
                cdp.stop()
        except Exception:
            pass
        try:
            cu.uia._HOST and cu.uia._HOST.close()
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n%d 项 / 通过 %d / 失败 %d" % (len(RESULTS), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
