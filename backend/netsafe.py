# -*- coding: utf-8 -*-
r"""HTTPS 证书兜底（安装版关键修复）。

**问题**：安装版用**嵌入式 Python**，它没有可靠的 CA 根证书；而开发机用的是系统
Python，`ssl.create_default_context()` 会去读 **Windows 证书存储**（实测本机 247 张），
所以这个问题一直没暴露。结果安装版里**所有 HTTPS 请求**都报：

    URLError: <urlopen error [SSL: CERTIFICATE_VERIFY_FAILED]
    certificate verify failed: unable to get local issuer certificate (_ssl.c:1010)>

受影响的至少有两处（都是 0.1.3 → 0.1.8 升级时用户实测到的）：
  * 群聊登录 —— 后端 `/cocraft/*` 反向代理（`server.py::_cocraft_proxy`）；
  * 软件更新 —— `updater.py` 查 / 下载 GitHub Releases。

**修法**：随包一份 Mozilla CA bundle（`backend/cacert.pem`，由 certifi 提供），
构造 SSLContext 时 **系统证书 + 随包证书一起加载**（合并，不替换 —— 企业自签证书仍有效）；
`install()` 再把默认 HTTPS context 换成它，于是**所有** `urllib.request.urlopen()` 自动生效，
顺带设 `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE` 照顾第三方库。

纯标准库，无额外依赖。幂等，可重复调用。
"""

from __future__ import annotations

import os
import ssl
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 随包 CA 的候选位置：源码布局（backend/cacert.pem）与安装后布局都认。
# 打包时 `backend/` 整个目录会进 payload（build.py 的 CORE），所以这个文件一定在。
_CA_CANDIDATES = (
    os.path.join(HERE, "cacert.pem"),
    os.path.join(ROOT, "cacert.pem"),
    os.path.join(ROOT, "runtime", "cacert.pem"),
)

_ctx: "ssl.SSLContext | None" = None
_installed = False


def ca_file() -> str:
    """返回第一个存在的随包 CA 文件；都没有就回空串（退回只用系统证书）。"""
    for path in _CA_CANDIDATES:
        if os.path.isfile(path):
            return path
    try:                                    # 环境里恰好装了 certifi 也能用
        import certifi                      # noqa: PLC0415
        return certifi.where()
    except Exception:                       # noqa: BLE001
        return ""


def ssl_context() -> ssl.SSLContext:
    """系统证书 + 随包证书**合并**的 SSLContext（进程内缓存）。"""
    global _ctx
    if _ctx is not None:
        return _ctx
    ctx = ssl.create_default_context()
    try:
        ctx.load_default_certs()            # Windows 读证书存储；其它平台读系统路径
    except Exception:                       # noqa: BLE001
        pass
    cafile = ca_file()
    if cafile:
        try:
            ctx.load_verify_locations(cafile=cafile)
        except Exception:                   # noqa: BLE001
            pass
    _ctx = ctx
    return ctx


def install() -> bool:
    """让证书兜底**全局生效**（幂等）。返回是否找到了随包 CA。"""
    global _installed
    cafile = ca_file()
    if cafile:
        for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            os.environ.setdefault(var, cafile)
    # `urllib.request` → `http.client.HTTPSConnection`（context=None 时）会调它，
    # 于是所有 urlopen 自动用上我们的 context。
    ssl._create_default_https_context = ssl_context   # noqa: SLF001
    _installed = True
    # 诊断：把"用了哪个 CA 文件 / 一共多少张证书"记一行 —— 装到别的机器出 SSL 错时一眼可查。
    try:
        n = len(ssl_context().get_ca_certs())
        os.makedirs(os.path.join(ROOT, "runtime", "logs"), exist_ok=True)
        with open(os.path.join(ROOT, "runtime", "logs", "netsafe.log"), "a",
                  encoding="utf-8") as fh:
            fh.write("%s  ca=%s  certs=%d  py=%s\n"
                     % (time.strftime("%Y-%m-%d %H:%M:%S"), cafile or "(none)", n,
                        sys.version.split()[0]))
    except Exception:  # noqa: BLE001
        pass
    return bool(cafile)


def urlopen(req, timeout: float = 30, context: "ssl.SSLContext | None" = None):
    """带证书兜底的 urlopen —— 即使没调 `install()` 也能单独用。"""
    return urllib.request.urlopen(req, timeout=timeout,
                                  context=context or ssl_context())


# ---------------- ★连「本机服务」专用：显式绕过系统代理 ----------------
# 为什么必须单独一个（2026-10-15 实测踩到，排查了很久）：
#   ① CPython 的 `urllib.request.urlopen()` **首次调用就把 opener 缓存住**（`_opener`），
#      而 `build_opener()` 在构建时读 `getproxies()` —— 那一刻系统代理是**开**的，就带上了 `ProxyHandler`。
#   ② 之后用户把代理**关**掉（注册表 `ProxyEnable=0`）—— 而 CPython 在 Windows 上
#      `proxy_bypass_registry()` 在 `ProxyEnable=0` 时返回 **0 = "不绕过"** →
#      缓存里那个代理**照样生效**（per-request 不会重新判断）。
#   ③ 于是面板把**发给本机上游**（`http://127.0.0.1:49374`，OpenCode）的请求塞给了已关闭的
#      代理 `127.0.0.1:7890` → `WinError 10061 目标计算机积极拒绝` → **界面一直"未连接"，
#      而上游其实是好的**（`netstat` 铁证：`127.0.0.1:xxxxx → 127.0.0.1:7890 SYN_SENT`）。
# ⇒ **凡是要连本机端口（面板 8787 / OpenCode 上游 / 音乐服务 8790 …），一律用这个 opener。**
#   要连外网（GitHub / cocraft / 音乐站 / 技能下载）的仍用上面的 `urlopen()` —— 那种场景代理**可能是有用的**。
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def local_urlopen(req, timeout: float = 30):
    """连**本机服务**专用：**不走系统代理**（见上面那段的血泪史）。"""
    return LOCAL_OPENER.open(req, timeout=timeout)
