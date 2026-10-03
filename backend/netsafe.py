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
