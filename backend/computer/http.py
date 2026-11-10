"""`cu http` —— 直接发 HTTP 请求（GET 只读 / POST·PUT·PATCH·DELETE 动作）。

为什么要它：`cu browser eval` 只能在**已渲染的页面里** fetch；有时 agent 只想调一个
接口（而且希望带上浏览器里已登录的 cookie），没必要为一次请求开页面。

安全口径（和桌面/浏览器那套一致）：
  · **只允许 http / https** —— file: / ftp: / data: 一律拒（否则等于任意读本地文件）；
  · **GET / HEAD = 只读**（权限层自动放行）；**POST / PUT / PATCH / DELETE = 动作**（默认 deny）；
  · cookie 复用要显式：`--cookies auto`（默认，有专用浏览器就用）/ `browser` / `none`，
    取自**专用 profile 的 CDP**（`Storage.getCookies`），只挑域名 + 路径 + Secure 匹配的；
  · 响应体上限（默认 200 KB），超了截断并标 `truncated`；
  · **正文不进审计链**：只记 method / url / status / 字节数（和 `cu type` 的口径一致）。
"""

import json
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_SCHEMES = ("http", "https")
READ_METHODS = ("GET", "HEAD")
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
DEFAULT_UA = "cocraft-cu/1.0 (+local agent tool)"
DEFAULT_MAX_BYTES = 200 * 1024
DEFAULT_TIMEOUT = 30.0


class HttpError(RuntimeError):
    pass


# ---------------------------------------------------------------- 抓取（TLS / 代理）

def _open(req, timeout):
    """**本机**绕代理、**外网**走系统代理（见 AGENTS 踩坑 #45）。

    TLS 走 netsafe 合并后的 CA（安装版没有系统证书时也能连），拿不到就退回默认。
    """
    host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
    is_local = host in ("127.0.0.1", "localhost", "::1", "[::1]")
    try:
        import netsafe  # noqa: WPS433
        return netsafe.local_urlopen(req, timeout=timeout) if is_local else netsafe.urlopen(req, timeout=timeout)
    except Exception:
        pass
    if is_local:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        return opener.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


# ---------------------------------------------------------------- cookie 复用

def _domain_match(cookie_domain, host):
    d = str(cookie_domain or "").lstrip(".").lower()
    h = str(host or "").lower()
    if not d:
        return False
    return h == d or h.endswith("." + d)


def cookies_from_browser(url, port=None):
    """从**专用 profile 的 CDP** 里取匹配这个 URL 的 cookie（不启动浏览器）。"""
    try:
        from . import cdp
    except Exception:  # noqa: BLE001
        import cdp  # type: ignore
    if not cdp.is_up(port):
        return {}, "浏览器没在跑"
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    path = u.path or "/"
    secure = u.scheme == "https"
    b = None
    res = None
    try:
        b = cdp.Browser(port=port)
        b.connect()
        res = b.request("Storage.getCookies", timeout=10)
    except Exception as exc:  # noqa: BLE001
        return {}, "取 cookie 失败：%s" % exc
    finally:
        if b is not None:
            try:
                b.close()
            except Exception:
                pass
    out = {}
    for c in (res or {}).get("cookies") or []:
        try:
            if not _domain_match(c.get("domain"), host):
                continue
            cp = c.get("path") or "/"
            if not str(path).startswith(cp):
                continue
            if c.get("secure") and not secure:
                continue
            out[str(c.get("name"))] = str(c.get("value") or "")
        except Exception:
            continue
    return out, ""


def resolve_cookies(url, mode="auto", port=None):
    """返回 (cookie dict, 说明)。mode: auto / browser / none。"""
    m = str(mode or "auto").lower()
    if m == "none":
        return {}, "none"
    try:
        from . import cdp
    except Exception:  # noqa: BLE001
        import cdp  # type: ignore
    if m == "auto" and not cdp.is_up(port):
        return {}, "browser-not-running"
    cookies, err = cookies_from_browser(url, port=port)
    return cookies, (err or ("browser" if cookies else "browser-empty"))


# ---------------------------------------------------------------- 请求

def request(method="GET", url="", headers=None, body=None, json_obj=None,
            timeout=DEFAULT_TIMEOUT, cookies="auto", max_bytes=DEFAULT_MAX_BYTES,
            port=None):
    method = str(method or "GET").upper()
    url = str(url or "").strip()
    if method not in READ_METHODS + WRITE_METHODS:
        raise HttpError("不支持的 method：%s（只允许 %s）"
                        % (method, " / ".join(READ_METHODS + WRITE_METHODS)))
    u = urllib.parse.urlsplit(url)
    if u.scheme.lower() not in ALLOWED_SCHEMES:
        raise HttpError("只允许 http / https（收到 %r）—— 本地文件请用 fs/read 之类" % (u.scheme or "空"))
    if not u.hostname:
        raise HttpError("URL 里没有主机名：%r" % url)

    hdrs = {}
    for k, v in (headers or {}).items():
        if k and v is not None:
            hdrs[str(k)] = str(v)
    low = {k.lower() for k in hdrs}
    if "user-agent" not in low:
        hdrs["User-Agent"] = DEFAULT_UA
    if "accept" not in low:
        hdrs["Accept"] = "application/json, text/plain, */*"

    data = None
    if json_obj is not None:
        data = json.dumps(json_obj, ensure_ascii=False).encode("utf-8")
        if "content-type" not in low:
            hdrs["Content-Type"] = "application/json; charset=utf-8"
    elif body is not None:
        data = str(body).encode("utf-8")
        if "content-type" not in low:
            hdrs["Content-Type"] = "text/plain; charset=utf-8"

    ck, ck_note = ({}, "given")
    if "cookie" not in low:
        ck, ck_note = resolve_cookies(url, mode=cookies, port=port)
        if ck:
            hdrs["Cookie"] = "; ".join("%s=%s" % (k, v) for k, v in ck.items())

    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    t0 = time.time()
    status, rhdrs, raw = 0, {}, b""
    try:
        with _open(req, timeout) as r:
            status = int(getattr(r, "status", r.getcode()) or 0)
            rhdrs = {k.lower(): v for k, v in (r.headers or {}).items()}
            raw = r.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 也是"有响应"：把状态与正文拿回来，别把它当异常吞掉
        status = int(exc.code or 0)
        rhdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
        try:
            raw = exc.read(max_bytes + 1)
        except Exception:
            raw = b""
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        raise HttpError("请求失败：%s" % (getattr(exc, "reason", None) or exc))

    elapsed_ms = int((time.time() - t0) * 1000)
    truncated = len(raw) > max_bytes
    if truncated:
        raw = raw[:max_bytes]
    ctype = rhdrs.get("content-type", "")
    text = ""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        text = ""
    parsed = None
    if "json" in ctype.lower() or (text[:1] in "{[" and "json" not in ctype.lower()):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None

    keep = ("content-type", "content-length", "location", "set-cookie", "cache-control")
    return {
        "ok": 200 <= status < 400,
        "status": status,
        "url": url,
        "method": method,
        "elapsed_ms": elapsed_ms,
        "bytes": len(raw),
        "truncated": truncated,
        "content_type": ctype,
        "headers": {k: v for k, v in rhdrs.items() if k in keep},
        "json": parsed,
        "body": text if parsed is None else "",
        "cookies_used": sorted(ck.keys()),
        "cookies_source": ck_note,
    }
