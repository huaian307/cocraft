#!/usr/bin/env python
"""http_test.py -- `cu http` 回归（真起本地 HTTP 服务，不联网）。

验的是"请求真的发出去了、响应真的读回来了"：
  GET JSON → status/JSON 解析对；GET 文本 → body 对；超限 → truncated 且不超上限；
  POST --json → 服务端收到的 body/Content-Type 对；4xx 也当"有响应"返回；
  file:// 一律拒；写请求默认被权限闸门拒；审计只记 method/url/status（正文不进链）；
  **cookie 复用**：真起无头 Edge → 塞一个 cookie → `--cookies browser` 时服务端能收到它。

全程临时目录（审计链 / CDP 状态 / profile），不碰真实运行态与用户浏览器。
"""

import http.server
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
TEST_PORT = 9347

JSON_BODY = {"ok": True, "n": 1, "名字": "张三"}
BIG_TEXT = "x" * (300 * 1024)


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("[OK]  " if cond else "[BAD] ") + name +
          (("  -> " + str(detail)) if (detail and not cond) else ""))
    return bool(cond)


def run(argv):
    try:
        args = cumain.build_parser().parse_args(argv)
        return args.func(args, cu)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def read_audit(path):
    rows = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def main():
    tmp = tempfile.mkdtemp(prefix="http_test_")
    audit_path = os.path.join(tmp, "audit.jsonl")
    os.environ["COCRAFT_AUDIT_FILE"] = audit_path
    os.environ.pop("COCRAFT_CU_ACTIONS", None)
    os.environ["COCRAFT_CU_ACTOR"] = "http_test"
    cdp.STATE_FILE = os.path.join(tmp, "_cdp.json")
    cdp.PROFILE_DIR = os.path.join(tmp, "profile")
    cdp.DEFAULT_PORT = TEST_PORT
    print("temp dir: %s" % tmp)

    seen = {}          # 服务端看到的最后一次请求

    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            seen["method"] = "GET"
            seen["path"] = self.path
            seen["cookie"] = self.headers.get("Cookie") or ""
            seen["ua"] = self.headers.get("User-Agent") or ""
            if self.path.startswith("/json"):
                self._send(200, json.dumps(JSON_BODY, ensure_ascii=False))
            elif self.path.startswith("/text"):
                self._send(200, "hello http", "text/plain; charset=utf-8")
            elif self.path.startswith("/big"):
                self._send(200, BIG_TEXT, "text/plain; charset=utf-8")
            elif self.path.startswith("/404"):
                self._send(404, json.dumps({"error": "not found"}), "application/json")
            else:
                self._send(200, json.dumps({"path": self.path}))

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            seen["method"] = "POST"
            seen["path"] = self.path
            seen["cookie"] = self.headers.get("Cookie") or ""
            seen["ctype"] = self.headers.get("Content-Type") or ""
            try:
                seen["body"] = json.loads(raw.decode("utf-8"))
            except Exception:
                seen["body"] = raw.decode("utf-8", "replace")
            self._send(200, json.dumps({"ok": True, "echo": seen.get("body")},
                                       ensure_ascii=False))

        def do_PUT(self):
            self.do_POST()

        def do_PATCH(self):
            self.do_POST()

        def do_DELETE(self):
            self.do_POST()

        def log_message(self, *a):
            pass

    class Quiet(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass

    httpd = Quiet(("127.0.0.1", 0), H)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % port
    print("serving", base)

    started = False
    try:
        # ---------- 1. 静态：命令分级 ----------
        check("cu http get → 只读", cu_gate.parse_cu_command("cu http get --url http://x") == ("http.get", True))
        check("cu http post → 动作", cu_gate.parse_cu_command("cu http post --url http://x") == ("http.post", False))
        check("cu http 未知动词不当成 cu", cu_gate.parse_cu_command("cu http nope") is None)

        # ---------- 2. GET ----------
        r = run(["http", "get", "--url", base + "/json"])
        check("GET JSON → 200", r.get("status") == 200 and r.get("ok"), r)
        check("JSON 自动解析（含中文）",
              isinstance(r.get("json"), dict) and r["json"].get("名字") == "张三", r.get("json"))
        check("带 UA 与字节数", bool(r.get("bytes")) and "cocraft-cu" in (seen.get("ua") or ""),
              seen.get("ua"))

        r = run(["http", "get", "--url", base + "/text"])
        check("GET 文本 → body", "hello http" in (r.get("body") or ""), r.get("body"))

        r = run(["http", "get", "--url", base + "/big", "--max-bytes", "40000"])
        check("超过上限 → truncated 且字节不超限",
              r.get("truncated") is True and r.get("bytes") <= 40000, r.get("bytes"))

        r = run(["http", "get", "--url", base + "/404"])
        check("4xx 也当「有响应」返回（不抛异常）",
              r.get("status") == 404 and r.get("ok") is False, r.get("status"))

        r = run(["http", "get", "--url", "file:///C:/Windows/win.ini"])
        check("file:// 一律拒", r.get("ok") is False and "http / https" in (r.get("error") or ""),
              r.get("error"))

        # ---------- 3. 写请求（权限 + body） ----------
        denied = run(["http", "post", "--url", base + "/echo", "--json", '{"a":1}'])
        check("写请求默认被拒", denied.get("ok") is False and denied.get("denied"), denied)

        r = run(["http", "post", "--url", base + "/echo", "--json", '{"a":1,"b":"中"}',
                 "--allow-actions"])
        check("POST --json 成功", r.get("ok") and r.get("status") == 200, r)
        check("服务端收到 JSON body 与 Content-Type",
              seen.get("body") == {"a": 1, "b": "中"} and "application/json" in (seen.get("ctype") or ""),
              seen)
        check("响应里的 echo 对得上",
              isinstance(r.get("json"), dict) and r["json"].get("echo") == {"a": 1, "b": "中"},
              r.get("json"))

        r = run(["http", "post", "--url", base + "/echo", "--method", "PUT", "--body", "plain",
                 "--allow-actions"])
        check("--method PUT + 文本 body（Content-Type: text/plain）",
              r.get("ok") and "text/plain" in (seen.get("ctype") or "")
              and seen.get("body") == "plain", seen)

        r = run(["http", "post", "--url", base + "/echo", "--json", "{bad json", "--allow-actions"])
        check("非法 --json → 明确报错", r.get("ok") is False and "不是合法 JSON" in (r.get("error") or ""),
              r.get("error"))

        # ---------- 4. 显式头 ----------
        r = run(["http", "get", "--url", base + "/json", "--header", "X-Probe: 42"])
        check("--header 能加（被服务端接受、请求成功）", r.get("ok"), r)

        # ---------- 5. cookie 复用（起无头 Edge） ----------
        try:
            cdp.start(headless=True, url="about:blank")
            started = cdp.is_up(TEST_PORT)
            b = cdp.Browser()
            b.connect()
            page = b.page()
            b.request("Network.setCookie", {"name": "probe", "value": "1",
                                            "domain": "127.0.0.1", "path": "/"},
                      session=page.sid)
            b.close()
            check("无头 Edge 起来了（用于 cookie 复用）", started, cdp.status())
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            check("无头 Edge 起来了（用于 cookie 复用）", False, exc)

        r = run(["http", "get", "--url", base + "/json", "--cookies", "browser"])
        check("--cookies browser → 服务端收到 Cookie: probe=1",
              "probe=1" in (seen.get("cookie") or ""), seen.get("cookie"))
        check("结果里写明用了哪些 cookie", (r.get("cookies_used") or []) == ["probe"], r.get("cookies_used"))

        r = run(["http", "get", "--url", base + "/json", "--cookies", "none"])
        check("--cookies none → 不带 cookie", "probe=1" not in (seen.get("cookie") or ""),
              seen.get("cookie"))

        # ---------- 6. 审计 ----------
        rows = [x for x in read_audit(audit_path) if x.get("source") == "cu"]
        titles = {(x.get("action") or {}).get("title") for x in rows}
        check("审计里有 http_* 记录",
              any(str(t).startswith("http_") for t in titles), sorted(str(t) for t in titles))
        check("写请求那条被记为动作",
              any(str((x.get("action") or {}).get("title")).startswith("http_post") for x in rows), "")
        raw = json.dumps(rows, ensure_ascii=False)
        check("审计不记请求正文", '"b":"中"' not in raw and '"a":1' not in raw, "正文泄漏进链了")
        check("审计记了 status", "status=200" in raw, "")
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
            httpd.shutdown()
        except Exception:
            pass

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n%d 项 / 通过 %d / 失败 %d" % (len(RESULTS), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
