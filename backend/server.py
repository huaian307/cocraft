#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""cocraft —— 零依赖本地代理 + 静态服务器

它做三件事：
  1. 托管 frontend/ 下的静态前端；
  2. 把 /api/* 反向代理到本机 OpenCode 后台服务，自动附加 HTTP Basic 鉴权
     （浏览器无法直接带鉴权跨源访问，这一步把鉴权和同源问题一次性解决）；
  3. 以流式方式透传响应，保证 /api/event (SSE) 的实时性，不缓冲。

依赖：仅 Python 标准库。运行：
    python backend/server.py                 # 默认 http://127.0.0.1:8787
    python backend/server.py --port 9000
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import glob
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs, quote

# ⚠ 必须**在**本地 import 之前把自己所在目录塞进 sys.path：
#   嵌入式 Python（安装包自带的那种）有 `._pth` → 进入"隔离模式"，
#   **不会**像普通 python 那样把脚本目录加进 sys.path，于是 `import cleanup` 直接
#   ModuleNotFoundError（实测踩到）。这一行让两种装法都一样能跑。
HERE = os.path.dirname(os.path.abspath(__file__))
# ⚠ 必须**放在 sys.path 最前**：安装版用嵌入式 Python 的 `._pth` 隔离模式，它会把 `..\backend`
#   追加在**标准库 zip 之后**；若只写"不在列表里才插入"，`import pty` 会命中**标准库的 Unix pty**
#   → 它去 import `termios`（Windows 没有）→ 面板服务起不来（安装版实测：装了打不开、17887 不监听）。
if HERE in sys.path:
    sys.path.remove(HERE)
sys.path.insert(0, HERE)

import cleanup
import engines
import external
import fsview
import netsafe
import procutils
import pty as pty_term
import updater
from engines.base import HOP_BY_HOP
from engines.acp import agents as acp_agents

# 抑制「应用程序无法正常启动 (0xc0000142)」这类硬错误模态框：本进程 + 所有子进程
# （音乐/频谱/QQ SMTC/外部打开/更新器/pick 对话框…… 都会继承）。
try:
    procutils.set_error_mode()
except Exception:  # noqa: BLE001
    pass

# 安装版的嵌入式 Python **没有 CA 根证书** → 所有 HTTPS 请求报 CERTIFICATE_VERIFY_FAILED
# （群聊 `/cocraft/*` 代理、软件更新都中招；开发机的系统 Python 读 Windows 证书存储，所以一直没暴露）。
# 这里把随包 CA（backend/cacert.pem）+ 系统证书装成默认 HTTPS context，urlopen 全局生效。
try:
    netsafe.install()
except Exception:  # noqa: BLE001
    pass

ROOT = os.path.dirname(HERE)
FRONTEND_DIR = os.path.join(ROOT, "frontend")
TOOLS_DIR = os.path.join(ROOT, "tools")
RESOURCES_DIR = os.path.join(ROOT, "resources")
RUNTIME_DIR = os.path.join(ROOT, "runtime")
STATE_DIR = os.path.join(RUNTIME_DIR, "state")
LOGS_DIR = os.path.join(RUNTIME_DIR, "logs")
VENVS_DIR = os.path.join(RUNTIME_DIR, "venvs")
REFS_DIR = os.path.join(RESOURCES_DIR, "references", "primary")
EXTRA_ROOTS = {
    "/refs/": REFS_DIR,
    "/refsq/": os.path.join(RESOURCES_DIR, "references", "q"),
}

for _directory in (RUNTIME_DIR, STATE_DIR, LOGS_DIR, VENVS_DIR):
    os.makedirs(_directory, exist_ok=True)

# ★ 权限授予等级（引擎无关）：全局一个挡位；命中则该类权限**自动放行**（不弹窗、不需要理由）。
#   前端据此自动应答；ACP 引擎也会读同一个文件（彼此不冲突）。
PERM_LEVELS = [
    ("ask",  "全部询问（默认）"),
    ("read", "只读自动"),
    ("edit", "读 + 改（限工作区内）"),
    ("exec", "读 + 改 + 执行（限工作区内）"),
    ("all",  "全部放行（含工作区外）"),
]
PERM_LEVEL_IDS = [k for k, _ in PERM_LEVELS]
PERM_LEVEL_FILE = os.path.join(STATE_DIR, "_perm_level.json")


def read_perm_level() -> str:
    try:
        with open(PERM_LEVEL_FILE, "r", encoding="utf-8") as fh:
            v = str((json.load(fh) or {}).get("level") or "").strip().lower()
    except Exception:  # noqa: BLE001
        return "ask"
    return v if v in PERM_LEVEL_IDS else "ask"


def write_perm_level(level: str) -> str:
    v = str(level or "").strip().lower()
    if v not in PERM_LEVEL_IDS:
        v = "ask"
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = PERM_LEVEL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"level": v}, fh, ensure_ascii=False)
        os.replace(tmp, PERM_LEVEL_FILE)
    except OSError:
        pass
    return v

UPSTREAM = {"url": None, "auth": None, "version": None}

# 面板页心跳：页面每 5 秒 POST /heartbeat 一次。窗口一关，心跳就停，
# 守护进程据此判断"面板是否还开着"（比查进程可靠得多）。
# /bye 是页面在关闭瞬间发出的"再见"，让判定从"等超时"变成"立刻"。
LAST_BEAT = {"t": 0.0, "count": 0, "bye": 0.0}
BEAT_TIMEOUT = 8.0          # 心跳断档多久算"页面没了"（正常 4 秒一次；窗口被强杀时靠它兜底）


# ============================ QQ音乐联动 ============================
# 读/控都走 Windows 媒体会话（SMTC）——QQ音乐会发布一个会话。
# 曲目信息由 tools/smtc-daemon.ps1（常驻，每 500ms 打一行 JSON）提供；
# 控制动作由 tools/smtc-control.ps1（一次性调用）执行。
# 歌词走 QQ音乐网页接口（搜索 → 歌词），只用于本地显示。

MUSIC_FILE = os.path.join(STATE_DIR, "_music.json")
SPECTRUM_FILE = os.path.join(STATE_DIR, "_spectrum.json")


SITE_PACKAGES_DIR = os.path.join(RUNTIME_DIR, "site-packages")


def _component_python(name: str):
    """可选组件（音乐服务 / 音频频谱）该怎么启动 → `(解释器路径|None, 额外 PYTHONPATH)`。

    支持两种装法：
      ① **老式 venv**：`runtime/venvs/<name>/Scripts/pythonw.exe`（本机现状）
      ② **安装包形态**：面板自带的解释器 + `runtime/site-packages/<name>`。
         ⚠ venv **不能随包**：`pyvenv.cfg` 与 Scripts 里的转发壳写着构建机的绝对路径，
         换台机器就废。所以安装包只放"纯 site-packages"，用 `sys.executable` + PYTHONPATH 跑。
    返回 `(None, "")` 表示**这个组件没装** —— 调用方一律安静跳过（前端会提示"可选组件未安装"）。
    """
    base = os.path.join(VENVS_DIR, name, "Scripts")
    pw = os.path.join(base, "pythonw.exe")
    if os.path.isfile(pw):
        return pw, ""          # ① venv 的 pythonw：GUI 子系统，从根上不闪黑框
    pe = os.path.join(base, "python.exe")
    if os.path.isfile(pe):
        return pe, ""
    extra = os.path.join(SITE_PACKAGES_DIR, name)
    if os.path.isdir(extra):
        return sys.executable, extra   # ② 自带解释器 + 可选 site-packages
    return None, ""


def _component_env(extra_site: str) -> dict:
    """给可选组件的子进程准备 env（把可选 site-packages 挂进 PYTHONPATH）。

    ⚠ 嵌入式 Python 有 `._pth` 时会**忽略 PYTHONPATH** —— 安装包那边靠 `init_state.fix_pth()`
      把组件目录写进 `._pth` 才真正生效；这里设上是为了"用普通 python 跑"的场景也没问题。
    """
    env = dict(os.environ)
    if extra_site:
        cur = env.get("PYTHONPATH") or ""
        env["PYTHONPATH"] = extra_site + (os.pathsep + cur if cur else "")
    return env


AUDIO_PY, AUDIO_SITE = _component_python("audio")
_spectrum_proc = None
_spectrum_lock = threading.Lock()
MUSIC = {"state": {}, "updated": 0.0}
_music_proc = None
_music_lock = threading.Lock()
_qq_cached = {"v": None}          # 本机是否装了 QQ音乐客户端（决定要不要起 SMTC 采集进程）

# 网易云音乐服务（非官方接口，仅供个人自用）。**可选组件**：
# 装它是 runtime/venvs/music（开发机）或 runtime/site-packages/music（安装包）；
# 没装就完全跳过，面板里的音乐 UI 会显示"未安装音乐服务（可选组件）"。
MUSIC_SVC_PY, MUSIC_SVC_SITE = _component_python("music")
MUSIC_SVC_PORT = 8790
_musicsvc_proc = None
_musicsvc_lock = threading.Lock()

# cocraft（Chatser）群聊后端：前端走 /cocraft/* 代理（避开跨域）；WS 由浏览器直连 wss://。
COCRAFT_BASE = "https://www.animaker.cc.cd"

QQ_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")
QQ_EXE = r"C:\Program Files (x86)\Tencent\QQMusic\QQMusic.exe"
LYRICS_CACHE: dict = {}
LYRICS_TTL = 6 * 3600

# 歌词翻译（外文 → 中文）：优先用 QQ 返回的官方 trans（实测基本为空），
# 否则走有道 demo 接口（无需 key；实测质量最好："夢ならばどれほどよかったでしょう"
# → "如果只是一场梦该有多好"）。结果落盘缓存 7 天，避免每次切歌都重翻。
# ⚠ 不要用 fanyi.youdao.com 老端点（返回 HTML）、Google gtx（被墙）、Bing ttranslatev3（要 token）。
TRANSLATE_URL = "https://aidemo.youdao.com/trans"
LYRICS_ZH_FILE = os.path.join(STATE_DIR, "_lyrics_zh.json")
LYRICS_ZH_TTL = 7 * 24 * 3600

# 面板设置：是否允许守护进程在面板打开时隐藏任务栏（false = 一直不隐藏）
TASKBAR_PREF_FILE = os.path.join(STATE_DIR, "_taskbar_enabled.json")
_ZH_CACHE = None

NO_WINDOW = 0x08000000

# ---------- 系统音量（Core Audio：IAudioEndpointVolume）----------
# ⚠ 不要用 winmm 的 waveOutGetVolume / waveOutSetVolume：Win10/11 上多数设备直接
#   返回 0xFFFFFFFF（不受支持），表现为"音量条能拖、音量纹丝不动"（本项目实测：
#   waveOutGetVolume -> 0xFFFFFFFF，而真实音量是 15，滑块却一直显示 100）。
#   这里用纯 ctypes 调 Core Audio，拿到的才是真实系统音量，也才能真的改。

class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    def __init__(self, s: str):
        super().__init__()
        if _ole32.CLSIDFromString(s, ctypes.byref(self)) != 0:
            raise OSError("bad guid " + s)


try:
    _ole32 = ctypes.windll.ole32
    _ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    _ole32.CoInitializeEx.restype = ctypes.c_long
    _ole32.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong,
                                        ctypes.c_void_p, ctypes.c_void_p]
    _ole32.CoCreateInstance.restype = ctypes.c_long
    _ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p]
    _ole32.CLSIDFromString.restype = ctypes.c_long
    _CLSID_MMDEVICE = _GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}")      # MMDeviceEnumerator
    _IID_IMMDEVICE_ENUM = _GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _IID_ENDPOINT_VOLUME = _GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
    _E_RENDER, _E_MULTIMEDIA = 0, 1
except Exception:  # noqa: BLE001
    _ole32 = None


def _com_call(this, index: int, argtypes, *args):
    """按 vtable 下标调 COM 方法（this = 接口指针）。"""
    vtbl = ctypes.cast(this, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])(this, *args)


def _open_endpoint_volume():
    """默认输出设备的 IAudioEndpointVolume 指针（用完记得 Release，即 _com_call(p,2,[])）。"""
    _ole32.CoInitializeEx(None, 0)          # 每个请求线程都要初始化（幂等，可重复调）
    enum = ctypes.c_void_p()
    hr = _ole32.CoCreateInstance(ctypes.byref(_CLSID_MMDEVICE), None, 1,
                                 ctypes.byref(_IID_IMMDEVICE_ENUM), ctypes.byref(enum))
    if hr != 0:
        raise OSError("CoCreateInstance 0x%08X" % (hr & 0xFFFFFFFF))
    dev = ctypes.c_void_p()
    hr = _com_call(enum, 4, [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)],
                   _E_RENDER, _E_MULTIMEDIA, ctypes.byref(dev))
    if hr != 0:
        _com_call(enum, 2, [])
        raise OSError("GetDefaultAudioEndpoint 0x%08X" % (hr & 0xFFFFFFFF))
    vol = ctypes.c_void_p()
    hr = _com_call(dev, 3, [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                            ctypes.POINTER(ctypes.c_void_p)],
                   ctypes.byref(_IID_ENDPOINT_VOLUME), 23, None, ctypes.byref(vol))
    _com_call(enum, 2, [])
    _com_call(dev, 2, [])
    if hr != 0:
        raise OSError("Activate 0x%08X" % (hr & 0xFFFFFFFF))
    return vol


def volume_get() -> int:
    """系统音量（0-100）；失败返回 -1。"""
    if _ole32 is None:
        return -1
    try:
        vol = _open_endpoint_volume()
        try:
            v = ctypes.c_float()
            hr = _com_call(vol, 9, [ctypes.POINTER(ctypes.c_float)], ctypes.byref(v))
            return round(v.value * 100) if hr == 0 else -1
        finally:
            _com_call(vol, 2, [])
    except Exception:  # noqa: BLE001
        return -1


def volume_set(pct) -> int:
    """把系统音量设成 0-100，返回设完后的实际值（-1 = 失败）。"""
    if _ole32 is None:
        return -1
    try:
        vol = _open_endpoint_volume()
        try:
            f = ctypes.c_float(max(0.0, min(100.0, float(pct))) / 100.0)
            hr = _com_call(vol, 7, [ctypes.c_float, ctypes.c_void_p], f, None)
            if hr != 0:
                return -1
        finally:
            _com_call(vol, 2, [])
        time.sleep(0.02)
        return volume_get()
    except Exception:  # noqa: BLE001
        return -1


def _qq_get_json(url: str, referer: str = "https://y.qq.com/") -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": QQ_UA, "Referer": referer,
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        text = resp.read().decode("utf-8", "replace")
    if not text.lstrip().startswith("{"):          # 有些接口会套一层 jsonp
        text = text[text.index("(") + 1: text.rindex(")")]
    return json.loads(text)


def _lrc_parse(raw: str) -> list:
    """把 LRC 解析成 [{t: 秒, s: 文本}]，按时间升序。

    ⚠ 要处理 `[offset:±毫秒]`：官方 LRC（包括 QQ 的部分文件）会用它整体平移时间轴，
    忽略它会让所有歌词提前/延后。offset 可能出现在任意位置，所以先扫一遍。
    """
    raw = raw or ""
    m_off = re.search(r"\[offset:([+-]?\d+)\]", raw)
    offset = int(m_off.group(1)) / 1000.0 if m_off else 0.0
    out = []
    for line in raw.splitlines():
        m = re.match(r"^((?:\[\d+:\d+(?:[.:]\d+)?\])+)(.*)$", line.strip())
        if not m:
            continue
        text = m.group(2).strip()
        if not text:
            continue
        for mm, ss in re.findall(r"\[(\d+):(\d+(?:[.:]\d+)?)\]", m.group(1)):
            t = int(mm) * 60 + float(ss.replace(":", ".")) + offset
            out.append({"t": round(max(0.0, t), 2), "s": text})
    out.sort(key=lambda x: x["t"])
    return out


def _zh_all() -> dict:
    """翻译缓存（落盘，重启不丢）：{ mid: {"_t": 时间戳, "zh": [每行中文]} }"""
    global _ZH_CACHE
    if _ZH_CACHE is None:
        try:
            with open(LYRICS_ZH_FILE, "r", encoding="utf-8") as fh:
                _ZH_CACHE = json.load(fh)
        except Exception:  # noqa: BLE001
            _ZH_CACHE = {}
    return _ZH_CACHE


def _zh_get(mid: str):
    hit = _zh_all().get(mid)
    if hit and time.time() - hit.get("_t", 0) < LYRICS_ZH_TTL:
        return hit.get("zh")
    return None


def _zh_put(mid: str, zh: list) -> None:
    data = _zh_all()
    data[mid] = {"_t": time.time(), "zh": zh}
    if len(data) > 200:                                # 只留最近 200 首
        for k in sorted(data, key=lambda x: data[x].get("_t", 0))[:len(data) - 200]:
            data.pop(k, None)
    try:
        with open(LYRICS_ZH_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


_RE_KANA = re.compile(r"[\u3040-\u30ff]")
_RE_HANGUL = re.compile(r"[\uac00-\ud7af]")
_RE_CJK = re.compile(r"[\u4e00-\u9fff]")
_RE_LATIN = re.compile(r"[A-Za-z]")


def _needs_translation(lines: list) -> bool:
    """外文才翻：有假名/韩文 → 要；以汉字为主 → 不要；拉丁字母多 → 要。"""
    text = "".join((l.get("s") or "") for l in lines)
    if not text.strip():
        return False
    kana = len(_RE_KANA.findall(text))
    hangul = len(_RE_HANGUL.findall(text))
    cjk = len(_RE_CJK.findall(text))
    latin = len(_RE_LATIN.findall(text))
    if kana or hangul:
        return True
    if cjk >= 2 and cjk * 4 >= latin:
        return False
    return latin > 0


def _youdao(text: str) -> str:
    """有道 demo 翻译：from=auto 自动识别语种，to=zh-CHS。"""
    url = f"{TRANSLATE_URL}?from=auto&to=zh-CHS&q={quote(text)}"
    req = urllib.request.Request(url, headers={"User-Agent": QQ_UA,
                                              "Referer": "https://ai.youdao.com/"})
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    tr = d.get("translation") or []
    return (tr[0] if tr else "").strip()


def _translate_lines(texts: list, chunk: int = 8) -> list:
    """整段翻（保留换行）；行数对不上就逐行重试。"""
    out: list = []
    for i in range(0, len(texts), chunk):
        part = texts[i:i + chunk]
        got = []
        try:
            got = _youdao("\n".join(part)).split("\n")
        except Exception:  # noqa: BLE001
            got = []
        if len(got) != len(part):
            got = []
            for t in part:
                try:
                    got.append(_youdao(t))
                except Exception:  # noqa: BLE001
                    got.append("")
        out.extend(got)
    return out


def qq_lyrics(title: str, artist: str, translate: bool = True) -> dict:
    """按 曲名+艺人 搜到 mid，再取歌词（可带中文翻译）。带缓存。"""
    key = f"{title}|{artist}".strip().lower()
    hit = LYRICS_CACHE.get(key)
    if hit and time.time() - hit.get("_t", 0) < LYRICS_TTL:
        # 中文歌词（needsTrans=False）直接命中；外文歌词要"翻好了"才命中，
        # 否则（上次翻译失败）这次重试一次。
        if not translate or hit.get("translated") or not hit.get("needsTrans"):
            return hit

    out = {"title": title, "artist": artist, "matchedTitle": "", "matchedArtist": "",
           "mid": "", "lines": [], "found": False, "translated": False}
    try:
        js = _qq_get_json("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?"
                          f"w={quote((title + ' ' + artist).strip())}&format=json&n=5&p=1&new_json=1")
        songs = (js.get("data") or {}).get("song", {}).get("list") or []
        if songs:
            s0 = songs[0]
            mid = s0.get("mid") or s0.get("songmid") or ""
            out["found"] = True
            out["mid"] = mid
            out["matchedTitle"] = s0.get("title") or s0.get("songname") or title
            out["matchedArtist"] = "/".join(x.get("name", "") for x in (s0.get("singer") or []))
            if mid:
                lj = _qq_get_json("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?"
                                  f"songmid={mid}&format=json&nobase64=1&g_tk=5381",
                                  referer="https://y.qq.com/portal/player.html")
                out["lines"] = _lrc_parse(lj.get("lyric", ""))
                if translate:
                    _attach_translation(out, mid, lj.get("trans") or "")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"

    out["_t"] = time.time()
    LYRICS_CACHE[key] = out
    return out


def _attach_translation(out: dict, mid: str, official: str) -> None:
    """给 out["lines"] 逐行补 "zh"：官方 trans 优先，其次机器翻译（带缓存）。"""
    lines = out["lines"]
    if not lines:
        out["needsTrans"] = False
        return
    out["needsTrans"] = _needs_translation(lines)
    zh: list = []
    if official.strip():                               # 官方翻译（实测基本为空）
        off = _lrc_parse(official)
        if len(off) == len(lines):                     # 行数对齐才用
            zh = [x.get("s", "") for x in off]
    if not zh:
        cached = _zh_get(mid)
        if cached and len(cached) == len(lines):
            zh = cached
        elif out["needsTrans"]:
            try:
                zh = _translate_lines([l.get("s", "") for l in lines])
                _zh_put(mid, zh)
            except Exception as exc:  # noqa: BLE001
                out["transError"] = f"{type(exc).__name__}: {exc}"
                zh = []
    for i, l in enumerate(lines):
        if i < len(zh) and (zh[i] or "").strip():
            l["zh"] = zh[i].strip()
    out["translated"] = any(l.get("zh") for l in lines)


def read_taskbar_pref() -> bool:
    """面板打开时是否隐藏任务栏；默认开。"""
    try:
        with open(TASKBAR_PREF_FILE, "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("enabled", True))
    except Exception:  # noqa: BLE001
        return True


def write_taskbar_pref(enabled: bool) -> bool:
    """落盘任务栏开关，供守护进程在 /alive 里读取。"""
    enabled = bool(enabled)
    try:
        tmp = TASKBAR_PREF_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"enabled": enabled}, fh, ensure_ascii=False)
        os.replace(tmp, TASKBAR_PREF_FILE)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- 面板窗口形态（原生 WebView2 / 浏览器 Edge --app）----
PANEL_WINDOW_FILE = os.path.join(STATE_DIR, "_panel_window.json")


def panel_native_available() -> bool:
    """原生窗口（pywebview / WebView2）这个可选组件装没装。"""
    if not os.path.isfile(os.path.join(HERE, "panel_window.py")):
        return False
    if os.path.isfile(os.path.join(VENVS_DIR, "panel", "Scripts", "pythonw.exe")):
        return True
    if os.path.isdir(os.path.join(SITE_PACKAGES_DIR, "panel")):
        return True
    return False


def read_panel_window_pref() -> str:
    """窗口模式：`native`（原生）/ `browser`（Edge --app）/ `auto`（默认，装了原生就用原生）。"""
    try:
        with open(PANEL_WINDOW_FILE, "r", encoding="utf-8") as fh:
            v = str(json.load(fh).get("mode") or "").strip().lower()
            return v if v in ("native", "browser") else "auto"
    except Exception:  # noqa: BLE001
        return "auto"


def write_panel_window_pref(mode: str) -> bool:
    mode = str(mode or "auto").strip().lower()
    if mode not in ("native", "browser", "auto"):
        mode = "auto"
    try:
        tmp = PANEL_WINDOW_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"mode": mode}, fh, ensure_ascii=False)
        os.replace(tmp, PANEL_WINDOW_FILE)
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_music_file() -> dict:
    try:
        with open(MUSIC_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def qq_search(kw: str, limit: int = 10) -> dict:
    """在 QQ音乐 曲库里搜歌。返回标题/艺人/专辑/mid/时长。"""
    out = {"ok": True, "q": kw, "items": []}
    try:
        js = _qq_get_json("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?"
                          f"w={quote(kw)}&format=json&n={limit}&p=1&new_json=1")
        for s in ((js.get("data") or {}).get("song", {}).get("list") or [])[:limit]:
            album = s.get("album")
            out["items"].append({
                "title": s.get("title") or s.get("songname") or "",
                "artist": "/".join(x.get("name", "") for x in (s.get("singer") or [])),
                "album": album.get("name", "") if isinstance(album, dict) else "",
                "mid": s.get("mid") or s.get("songmid") or "",
                "interval": s.get("interval"),
            })
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def _qq_pids() -> set:
    # 首选纯 ctypes（不起子进程 → 不会在关机/异常时弹 tasklist 的 0xc0000142 框）
    try:
        return set(procutils.pids_named("QQMusic.exe"))
    except Exception:  # noqa: BLE001
        pass
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq QQMusic.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=15, creationflags=NO_WINDOW).stdout or ""
    except Exception:  # noqa: BLE001
        return set()
    pids = set()
    for raw in out.splitlines():
        parts = [p.strip('"') for p in raw.strip().split('","')]
        if len(parts) >= 2 and parts[0].lower() == "qqmusic.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


_qq_state_cache = {"t": 0.0, "running": False}


def qq_running(max_age: float = 4.0) -> bool:
    """QQ音乐客户端是否在跑（带缓存，避免每次轮询都起一次 tasklist）。"""
    now = time.time()
    if now - _qq_state_cache["t"] > max_age:
        _qq_state_cache["running"] = bool(_qq_pids())
        _qq_state_cache["t"] = now
    return _qq_state_cache["running"]


def close_qqmusic() -> int:
    """优先优雅关闭（给主窗口发 WM_CLOSE），没有可见窗口时才强杀。返回处理的窗口数。"""
    pids = _qq_pids()
    if not pids:
        return 0
    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p),
                              ctypes.c_void_p]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    u.IsWindowVisible.argtypes = [ctypes.c_void_p]
    u.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _lp):
        pid = ctypes.c_uint()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
        return True

    u.EnumWindows(_cb, None)
    for hwnd in found:
        u.PostMessageW(hwnd, 0x0010, None, None)          # WM_CLOSE
    # QQ音乐 收到 WM_CLOSE 往往只是缩到托盘（甚至弹确认框），并不会真的退出，
    # 所以等一会儿再检查；还活着就强杀，否则"关闭"按钮形同虚设。
    for _ in range(6):
        time.sleep(0.5)
        if not _qq_pids():
            return len(found)
    subprocess.run(["taskkill", "/IM", "QQMusic.exe", "/F"],
                   capture_output=True, creationflags=NO_WINDOW)
    return len(found)


def start_qqmusic() -> tuple:
    if not os.path.isfile(QQ_EXE):
        return False, f"找不到 {QQ_EXE}"
    try:
        subprocess.Popen([QQ_EXE], cwd=os.path.dirname(QQ_EXE), creationflags=NO_WINDOW)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def _qqmusic_installed() -> bool:
    """本机装没装 QQ音乐客户端（装了才需要 SMTC 采集进程；**没装就别起 powershell**——
    VM 里 powershell 坏了会弹 0xc0000142 模态框，还会拖慢启动）。结果缓存。"""
    if _qq_cached["v"] is not None:
        return _qq_cached["v"]
    cands = [
        QQ_EXE,
        r"C:\Program Files\Tencent\QQMusic\QQMusic.exe",
        r"C:\Program Files (x86)\Tencent\QQMusic\QQMusic.exe",
    ]
    try:
        import shutil as _sh
        if _sh.which("QQMusic.exe"):
            _qq_cached["v"] = True
            return True
    except Exception:  # noqa: BLE001
        pass
    _qq_cached["v"] = any(os.path.isfile(p) for p in cands)
    return _qq_cached["v"]


def start_music_daemon(force: bool = False) -> None:
    """拉起常驻的 SMTC 采集进程（幂等）。它把状态写进 _music.json。

    两重去重：
      ① 状态文件还新鲜（< 10s）说明已经有采集进程在写 → 不再拉新的；
         （服务重启不会带走已脱离的采集进程，这一步避免每次重启都堆一个）
      ② 采集脚本内部用命名互斥量做单实例 → 万一并发，第二个会立刻退出。
    ⚠ 没装 QQ音乐客户端时**直接跳过**（这条链要起 powershell）。
    """
    global _music_proc
    if not force and not _qqmusic_installed():
        return
    if not force:
        try:
            if (time.time() - os.path.getmtime(MUSIC_FILE)) < 10:
                return
        except OSError:
            pass
    with _music_lock:
        if _music_proc is not None and _music_proc.poll() is None:
            return
        script = os.path.join(TOOLS_DIR, "smtc-daemon.ps1")
        if not os.path.isfile(script):
            return
        try:
            _music_proc = subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-OutFile", MUSIC_FILE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=NO_WINDOW,
            )
        except Exception:  # noqa: BLE001
            _music_proc = None


def ui_version() -> str:
    """读 frontend/index.html 里 **app.js** 的 `?v=`，作为"服务端现在的前端版本号"。
    前端拿它和自己加载到的 ?v= 比对，不一致就自动刷新（避免面板一直跑旧代码）。

    ⚠ 一定要**锚定 app.js**：文件里第一个 `?v=` 是 `style.css` 的（两处版本号各管各的缓存），
      用 `re.search(r"\\?v=")` 会取到 CSS 版本 —— app.js 改了却报"没变"，面板就不会刷新。
    """
    try:
        with open(os.path.join(FRONTEND_DIR, "index.html"), "r", encoding="utf-8") as fh:
            m = re.search(r"/app\.js\?v=([\w.\-]+)", fh.read())
        return m.group(1) if m else ""
    except Exception:  # noqa: BLE001
        return ""


# ============================ 动态壁纸（Wallpaper Engine 原片） ============================
# 收起侧栏时播放创意工坊里的 mp4 原片（展开立刻暂停 → 不再解码）。
# 用 glob 匹配，避免把带书名号/破折号的文件名硬编码进来；优先「不在 files/ 子目录里」的那个。

WE_APPID = "431960"
# 每个主题一串候选，按顺序取第一个找到的。三种写法：
#   "title:关键字" —— 扫各壁纸的 project.json 标题，包含关键字就用它（改名/换文件名都不怕）
#   "id:文件夹名"  —— 直接指定创意工坊 id
#   其余当作相对 431960 的 glob（例如 "*/sakura*.mp4"）
WE_WALLPAPERS = {
    # 白昼：［绯莎］绘梨衣 sakura —— 夜：月读命绘梨衣
    "hiru": ["title:绯莎", "3786830950", "*/sakura*.mp4"],
    "yoru": ["title:月读", "3568633530"],
}
_we_root_cache = {"path": None, "t": 0.0}
MEDIA_EXT = (".mp4", ".webm", ".mkv")
WALLPAPER_PICK_FILE = os.path.join(STATE_DIR, "_wallpapers.json")   # 用户在面板里选的壁纸（主题 → 创意工坊 id）
WALLPAPER_ROOT_FILE = os.path.join(STATE_DIR, "_wallpaper_root.json")  # 用户自定义的创意工坊目录（空 = 自动）


def _steam_roots() -> list:
    """Steam 根目录候选：注册表 + 各库的 libraryfolders.vdf。"""
    roots = []
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            p = winreg.QueryValueEx(k, "SteamPath")[0]
        if p:
            roots.append(os.path.normpath(p))
    except Exception:  # noqa: BLE001
        pass
    for r in list(roots):                      # 别的库（D:\SteamLibrary 之类）
        vdf = os.path.join(r, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as fh:
                for m in re.finditer(r'"path"\s+"([^"]+)"', fh.read()):
                    roots.append(os.path.normpath(m.group(1).replace("\\\\", "\\")))
        except Exception:  # noqa: BLE001
            pass
    return roots


def read_wallpaper_root_override() -> str:
    """用户在设置里指定的动态壁纸目录（去掉首尾空白；没有/无效则空串）。"""
    try:
        with open(WALLPAPER_ROOT_FILE, "r", encoding="utf-8") as fh:
            return str(json.load(fh).get("root") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def write_wallpaper_root_override(path: str) -> tuple:
    """保存自定义动态壁纸目录；空串 = 恢复自动查找。返回 (ok, message)。"""
    path = str(path or "").strip()
    if not path:
        try:
            if os.path.exists(WALLPAPER_ROOT_FILE):
                os.remove(WALLPAPER_ROOT_FILE)
        except OSError as exc:
            return False, f"删除记录失败：{exc}"
        return True, "已恢复自动查找"
    path = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))
    if not os.path.isdir(path):
        return False, f"目录不存在：{path}"
    try:
        tmp = WALLPAPER_ROOT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"root": path}, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, WALLPAPER_ROOT_FILE)
    except Exception as exc:  # noqa: BLE001
        return False, f"保存失败：{exc}"
    return True, path


# ============================ 自定义外观（头像 / 空会话素材 / 文案）============================
# 能换的东西：左上角头像、对话里助手头像、空会话的主图与两张贴纸。
# 图片的做法：只记**本机文件路径**（runtime/state/_appearance.json），再由 `/appearance/img`
#   代理出去 —— 面板页是 http://127.0.0.1，直接引用 file:// 会被浏览器拦（混合内容）。
# 文案（空会话那几行）放前端 localStorage，跟「左上角名字」一致。
# ⚠ `key` 是固定枚举：界面上的 data-img 值必须在这里，写别的会被 400 顶回来。
APPEARANCE_FILE = os.path.join(STATE_DIR, "_appearance.json")
APPEARANCE_KEYS = ("brand", "avatar", "hero", "sticker1", "sticker2", "lyricball", "playerbg")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif")
IMAGE_MAX_BYTES = 32 * 1024 * 1024


def read_appearance() -> dict:
    """当前自定义素材：`{key: 绝对路径}`（没设的键不出现）。"""
    try:
        with open(APPEARANCE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
    except Exception:  # noqa: BLE001
        return {}
    out = {}
    if isinstance(d, dict):
        for k in APPEARANCE_KEYS:
            v = str(d.get(k) or "").strip()
            if v:
                out[k] = v
    return out


def appearance_version() -> int:
    """给前端破缓存的版本号（文件 mtime；没有就是 0）。"""
    try:
        return int(os.path.getmtime(APPEARANCE_FILE))
    except OSError:
        return 0


def write_appearance_image(key: str, path: str) -> tuple:
    """设置/清除一张自定义图（`path` 为空 = 恢复默认）。返回 `(ok, message)`。"""
    key = str(key or "").strip()
    if key not in APPEARANCE_KEYS:
        return False, "未知的图片位置：%s（可选：%s）" % (key or "(空)", "/".join(APPEARANCE_KEYS))
    cur = read_appearance()
    path = str(path or "").strip()
    if not path:
        cur.pop(key, None)
        msg = "已恢复默认"
    else:
        p = os.path.normpath(os.path.expandvars(os.path.expanduser(path)))
        if not os.path.isfile(p):
            return False, "文件不存在：%s" % p
        if os.path.splitext(p)[1].lower() not in IMAGE_EXTS:
            return False, "不是支持的图片格式（png / jpg / webp / gif / bmp / avif）"
        try:
            if os.path.getsize(p) > IMAGE_MAX_BYTES:
                return False, "图片太大（上限 32 MB）"
        except OSError as exc:
            return False, "读不到文件：%s" % exc
        cur[key] = p
        msg = p
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = APPEARANCE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(cur, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, APPEARANCE_FILE)
    except Exception as exc:  # noqa: BLE001
        return False, "保存失败：%s" % exc
    return True, msg


def auto_we_root():
    """自动找创意工坊 431960 目录；找不到返回 None。"""
    for r in _steam_roots():
        cand = os.path.join(r, "steamapps", "workshop", "content", WE_APPID)
        if os.path.isdir(cand):
            return os.path.normpath(cand)
    return None


def we_root(max_age: float = 60.0):
    """创意工坊 431960（壁纸）目录；优先用户设置，其次自动查找。带缓存。"""
    now = time.time()
    if _we_root_cache["path"] is not None and now - _we_root_cache["t"] < max_age:
        return _we_root_cache["path"] or None
    override = read_wallpaper_root_override()
    found = override if override and os.path.isdir(override) else auto_we_root()
    _we_root_cache["path"] = found or ""
    _we_root_cache["t"] = now
    return found


def _we_titles(root: str) -> dict:
    """{文件夹名: project.json 里的标题}"""
    out = {}
    for d in os.listdir(root):
        p = os.path.join(root, d)
        if not os.path.isdir(p):
            continue
        try:
            with open(os.path.join(p, "project.json"), "r", encoding="utf-8-sig") as fh:
                out[d] = str(json.load(fh).get("title") or "")
        except Exception:  # noqa: BLE001
            out[d] = ""
    return out


def _pick_media(folder: str):
    """在一个壁纸目录里挑主视频：优先 project.json 的 file，其次目录里最大的视频文件。"""
    try:
        with open(os.path.join(folder, "project.json"), "r", encoding="utf-8-sig") as fh:
            f = str(json.load(fh).get("file") or "")
        if f and f.lower().endswith(MEDIA_EXT):
            p = os.path.join(folder, f)
            if os.path.isfile(p):
                return p
    except Exception:  # noqa: BLE001
        pass
    best = None
    try:
        for name in os.listdir(folder):
            p = os.path.join(folder, name)
            if os.path.isfile(p) and name.lower().endswith(MEDIA_EXT):
                if best is None or os.path.getsize(p) > os.path.getsize(best):
                    best = p
    except OSError:
        pass
    return best


def live_wallpaper_files() -> dict:
    """按主题解析出可用的原片：{主题: (相对 431960 的路径, 壁纸标题)}。
    优先用**用户在面板里选过的**，没选过才落到 WE_WALLPAPERS 里的默认候选。"""
    root = we_root()
    out = {}
    if not root:
        return out
    titles = _we_titles(root)
    pick = _read_pick()
    for theme, specs in WE_WALLPAPERS.items():
        wid = str(pick.get(theme) or "")
        if wid and wid in titles:                    # ① 用户选的
            folder = os.path.join(root, wid)
            if we_project_kind(folder) != "video":   # 只支持 video 类壁纸
                continue
            p = _pick_media(folder)
            if p:
                out[theme] = (os.path.relpath(p, root).replace("\\", "/"), titles.get(wid, ""))
                continue
        for spec in specs:                           # ② 默认候选
            pick_path, label = None, ""
            if spec.startswith("title:"):
                key = spec[len("title:"):]
                for d, t in titles.items():
                    if key and key in t:
                        pick_path = _pick_media(os.path.join(root, d))
                        label = t
                        if pick_path:
                            break
            elif spec.startswith("id:"):
                d = spec[len("id:"):]
                if d in titles:
                    pick_path = _pick_media(os.path.join(root, d))
                    label = titles.get(d, "")
            else:
                hits = [p for p in glob.glob(os.path.join(root, spec)) if os.path.isfile(p)]
                if hits:
                    hits.sort(key=lambda p: -os.path.getsize(p))
                    pick_path = hits[0]
            if pick_path:
                out[theme] = (os.path.relpath(pick_path, root).replace("\\", "/"), label)
                break
    return out


def _read_pick() -> dict:
    try:
        with open(WALLPAPER_PICK_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_pick(theme: str, wid) -> dict:
    d = _read_pick()
    if wid:
        d[theme] = str(wid)
    else:
        d.pop(theme, None)
    try:
        with open(WALLPAPER_PICK_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except Exception:  # noqa: BLE001
        pass
    return d


def we_project_kind(folder: str) -> str:
    """video / scene / application / unsupported（application 一律避开）"""
    try:
        names = os.listdir(folder)
    except OSError:
        return "unsupported"
    low = [n.lower() for n in names]
    if any(n.endswith((".exe", ".dll", ".bat", ".cmd", ".ps1", ".vbs")) for n in low):
        return "application"
    typ = ""
    try:
        with open(os.path.join(folder, "project.json"), "r", encoding="utf-8-sig") as fh:
            typ = str(json.load(fh).get("type") or "").lower()
    except Exception:  # noqa: BLE001
        pass
    if typ in ("application", "program"):
        return "application"
    if typ == "scene" or any(n.endswith(".pkg") for n in low):
        return "scene"
    if typ == "video" or any(n.endswith(MEDIA_EXT) for n in low):
        return "video"
    return "unsupported"


def list_video_wallpapers() -> list:
    """创意工坊里的 **video** 壁纸（可在浏览器里直接播）。
    scene / application / 含 exe·dll 的项目一律不列、不解析、不执行。"""
    root = we_root()
    if not root:
        return []
    out = []
    for d, t in _we_titles(root).items():
        folder = os.path.join(root, d)
        if we_project_kind(folder) != "video":
            continue
        p = _pick_media(folder)
        if not p:
            continue
        preview = ""
        for name in ("preview.gif", "preview.jpg", "preview.png"):
            if os.path.isfile(os.path.join(folder, name)):
                preview = "/we/" + quote(d) + "/" + quote(name)
                break
        rel = os.path.relpath(p, root).replace("\\", "/")
        try:
            size = os.path.getsize(p)
        except OSError:
            size = 0
        out.append({
            "id": d, "title": t, "kind": "video",
            "file": os.path.basename(rel), "size_mb": round(size / 1048576.0, 1),
            "url": "/we/" + "/".join(quote(seg) for seg in rel.split("/")),
            "preview": preview,
        })
    out.sort(key=lambda x: -x["size_mb"])
    return out


def _read_spectrum() -> dict:
    try:
        with open(SPECTRUM_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def spectrum_age():
    try:
        return max(0.0, time.time() - os.path.getmtime(SPECTRUM_FILE))
    except OSError:
        return None


def start_spectrum(force: bool = False) -> None:
    """拉起频谱采集进程（可选组件；**没装就安静跳过**）。

    装法有两种（见 `_component_python`）：`runtime/venvs/audio`（开发机）或
    `runtime/site-packages/audio`（安装包）。没装时前端自动退回 CSS 合成动画。
    """
    global _spectrum_proc
    if not AUDIO_PY:
        return
    if not force:
        age = spectrum_age()
        if age is not None and age < 5:
            return
    with _spectrum_lock:
        if _spectrum_proc is not None and _spectrum_proc.poll() is None:
            return
        script = os.path.join(TOOLS_DIR, "spectrum.py")
        if not os.path.isfile(script):
            return
        try:
            _spectrum_proc = subprocess.Popen(
                [AUDIO_PY, script, "--out", SPECTRUM_FILE],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                env=_component_env(AUDIO_SITE),
                creationflags=NO_WINDOW)
        except Exception:  # noqa: BLE001
            _spectrum_proc = None


def spectrum_keeper() -> None:
    """频谱文件超过 8 秒没更新就重新拉起采集进程。"""
    while True:
        time.sleep(5)
        try:
            age = spectrum_age()
            if age is None or age > 8:
                start_spectrum()
        except Exception:  # noqa: BLE001
            pass


def stop_spectrum() -> None:
    global _spectrum_proc
    with _spectrum_lock:
        if _spectrum_proc is not None and _spectrum_proc.poll() is None:
            try:
                _spectrum_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _spectrum_proc = None


def music_svc_alive() -> bool:
    """本地网易云音乐服务是否在监听。"""
    try:
        with socket.create_connection(("127.0.0.1", MUSIC_SVC_PORT), 0.3):
            return True
    except OSError:
        return False


def music_component() -> dict:
    """音乐服务这个**可选组件**的状态（给 /music/status 与前端提示用）。"""
    return {"installed": bool(MUSIC_SVC_PY),
            "running": music_svc_alive() if MUSIC_SVC_PY else False,
            "kind": ("venv" if MUSIC_SVC_PY and os.path.join(VENVS_DIR, "music") in str(MUSIC_SVC_PY)
                     else ("site-packages" if MUSIC_SVC_SITE else "")),
            "reason": "" if MUSIC_SVC_PY else "未安装音乐服务（安装包里的可选组件）"}


def start_music_service() -> None:
    """拉起本地音乐服务（可选组件；**没装就安静跳过**）。"""
    global _musicsvc_proc
    if not MUSIC_SVC_PY:
        return
    with _musicsvc_lock:
        if _musicsvc_proc is not None and _musicsvc_proc.poll() is None:
            return
        script = os.path.join(TOOLS_DIR, "music_service.py")
        if not os.path.isfile(script):
            return
        try:
            _musicsvc_proc = subprocess.Popen(
                [MUSIC_SVC_PY, script, "--port", str(MUSIC_SVC_PORT)],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, env=_component_env(MUSIC_SVC_SITE),
                creationflags=NO_WINDOW)
        except Exception:  # noqa: BLE001
            _musicsvc_proc = None


def stop_music_service() -> None:
    """停掉面板内搜歌服务（连 venv 转发壳的子进程一起）。"""
    global _musicsvc_proc
    with _musicsvc_lock:
        proc = _musicsvc_proc
        _musicsvc_proc = None
    if proc is not None and proc.poll() is None:
        cleanup.kill_pid(proc.pid)


def music_service_keeper() -> None:
    """音乐服务没在监听就重新拉起。"""
    while True:
        time.sleep(5)
        try:
            if not music_svc_alive():
                start_music_service()
        except Exception:  # noqa: BLE001
            pass


def music_keeper() -> None:
    """只看状态文件新不新鲜：超过 10 秒没更新就重新拉起采集进程。
    采集进程自带命名互斥量做单实例，所以这里多拉几次也不会堆进程。"""
    while True:
        time.sleep(5)
        try:
            try:
                stale = (time.time() - os.path.getmtime(MUSIC_FILE)) > 10
            except OSError:
                stale = True
            if stale:
                start_music_daemon()
        except Exception:  # noqa: BLE001
            pass


def stop_music_daemon() -> None:
    global _music_proc
    with _music_lock:
        if _music_proc is not None and _music_proc.poll() is None:
            try:
                _music_proc.kill()
            except Exception:  # noqa: BLE001
                pass
        _music_proc = None



class Handler(BaseHTTPRequestHandler):
    server_version = "cocraft"
    protocol_version = "HTTP/1.1"

    # ---------- 基础工具 ----------

    def log_message(self, fmt, *args):
        # 静默常规请求，避免控制台被 SSE 刷屏
        if os.environ.get("OPENCODE_UI_VERBOSE"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- 路由 ----------

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_PATCH(self):
        self._route("PATCH")

    def do_DELETE(self):
        self._route("DELETE")

    def _route(self, method: str):
        path = urlsplit(self.path).path
        try:
            if path == "/healthz":
                self._healthz()
            elif path == "/heartbeat":
                self._heartbeat()
            elif path == "/bye":
                self._bye()
            elif path == "/alive":
                self._alive()
            elif path == "/panel/taskbar":
                self._panel_taskbar(method)
            elif path == "/panel/window":
                self._panel_window(method)
            elif path == "/engine/status":
                self._engine_status()
            elif path == "/engine":
                self._engine_switch(method)
            elif path == "/engine/acp/baseline":
                # ACP 共享基线：模型 API Key 就写在这里（只存本机 runtime/state）
                eng = engines.get_engine("acp")
                if method == "GET":
                    self._send_json(200, {"ok": True, "data": eng.baseline_view()})
                    return
                if method != "POST":
                    self._send_json(405, {"error": "GET or POST only"})
                    return
                b = self._read_json_body() or {}
                env = b.get("env") if isinstance(b.get("env"), dict) else {}
                patch = dict(env)
                if b.get("name") is not None:                # 也允许 {name, value} 单键写法
                    patch[str(b.get("name") or "")] = str(b.get("value") or "")
                if not patch:
                    self._send_json(400, {"ok": False, "error": "没有要写的环境变量（env 或 name/value）"})
                    return
                eng.set_baseline_env(patch)
                self._send_json(200, {"ok": True, "data": eng.baseline_view()})
            elif path == "/engine/acp/log":
                # 最近几行 ACP / agent 日志（随包 codex 起不来 / 网络不通 / 缺 Key 时看这个）
                eng = engines.get_engine("acp")
                q = parse_qs(urlsplit(self.path).query)
                try:
                    n = int((q.get("tail") or ["200"])[0])
                except Exception:  # noqa: BLE001
                    n = 200
                self._send_json(200, {"ok": True, "tail": eng.log_tail(n),
                                      "path": acp_agents.ROOT_DIR})
            elif path == "/engine/acp/agents":
                self._acp_agents(method)
            elif path == "/qq/state":
                self._qq_state()
            elif path == "/qq/control":
                self._qq_control(method)
            elif path == "/qq/volume":
                self._qq_volume(method)
            elif path == "/qq/lyrics":
                self._qq_lyrics()
            elif path == "/qq/search":
                self._qq_search()
            elif path == "/qq/spectrum":
                self._qq_spectrum()
            elif path == "/qq/app":
                self._qq_app(method)
            elif path == "/live/root/pick":
                self._live_root_pick(method)
            elif path == "/pick/image":
                self._pick_image(method)
            elif path == "/pick/folder":
                self._pick_folder(method)
            elif path == "/open/dir":
                # 在外部程序里打开目录：终端 / VS Code / 资源管理器（与引擎无关）
                self._open_dir(method)
            elif path == "/pty/component":
                self._pty_component()
            elif path == "/pty" or path.startswith("/pty/"):
                # 面板内终端（ConPTY，跑在可选组件里；与引擎无关）
                self._pty_route(method, path)
            elif path == "/fs/list":
                self._fs_list()
            elif path == "/fs/read":
                self._fs_read()
            elif path == "/fs/write":
                self._fs_write(method)
            elif path == "/update/check":
                self._update_check()
            elif path == "/update/apply":
                self._update_apply(method)
            elif path == "/appearance":
                self._appearance(method)
            elif path == "/appearance/img":
                self._appearance_img()
            elif path == "/live/root":
                self._live_root(method)
            elif path == "/live/wallpapers":
                self._live_wallpapers()
            elif path == "/live/list":
                self._live_list()
            elif path == "/live/pick":
                self._live_pick(method)
            elif path.startswith("/we/"):
                self._we(path)
            elif path == "/music/component":
                # 音乐服务是**可选组件**：这条不受 /music/* 代理影响，永远 200，
                # 前端据此显示"未安装"提示而不是让人以为坏了。
                self._send_json(200, {"ok": True, "audio": bool(AUDIO_PY),
                                      "music": music_component()})
            elif path.startswith("/music/"):
                self._music_proxy(method, path)
            elif path == "/cocraft/_url":
                # 前端拿后端/WS 地址（WS 由浏览器直连，不经我们的 HTTP 代理）
                _base = (os.environ.get("OPENCODE_UI_COCRAFT_BASE") or COCRAFT_BASE).rstrip("/")
                _ws = _base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
                self._send_json(200, {"base": _base, "ws": _ws})
            elif path.startswith("/cocraft/"):
                self._cocraft_proxy(method, path)
            elif path == "/api/permission/level":
                self._perm_level_api(method)
            elif path.startswith("/api/"):
                self._proxy(method)
            else:
                self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001 - 兜底，保证服务不崩
            try:
                self._send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                self.close_connection = True

    # ---------- 健康检查 ----------

    def _healthz(self):
        code, body = engines.active_engine().healthz()
        body["ui"] = ui_version()
        self._send_json(code, body)

    # ---------- 面板心跳 ----------

    def _heartbeat(self):
        LAST_BEAT["t"] = time.time()
        LAST_BEAT["count"] += 1
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _bye(self):
        """页面关闭瞬间的信号：比心跳超时判定快得多。"""
        LAST_BEAT["bye"] = time.time()
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _alive(self):
        now = time.time()
        t = LAST_BEAT["t"]
        age = (now - t) if t else None
        fresh = age is not None and age < BEAT_TIMEOUT
        closed = LAST_BEAT["bye"] > t          # 收到过比最后一次心跳更晚的"再见"
        self._send_json(200, {
            "alive": bool(fresh and not closed),
            "age_seconds": round(age, 1) if age is not None else None,
            "closed": bool(closed),
            "beats": LAST_BEAT["count"],
            "taskbar": read_taskbar_pref(),   # 守护进程据此决定要不要隐藏任务栏
            "panelWindow": read_panel_window_pref(),   # native / browser / auto
            "panelNative": panel_native_available(),
        })

    def _panel_taskbar(self, method: str):
        """面板设置：任务栏是否在面板打开时自动隐藏。"""
        if method == "GET":
            self._send_json(200, {"enabled": read_taskbar_pref()})
            return
        if method != "POST":
            self._send_json(405, {"error": "GET or POST only"})
            return
        body = self._read_json_body() or {}
        enabled = bool(body.get("enabled", True))
        ok = write_taskbar_pref(enabled)
        self._send_json(200 if ok else 500, {"ok": ok, "enabled": enabled})

    def _panel_window(self, method: str):
        """面板窗口形态：native（WebView2 原生窗口）/ browser（Edge --app）/ auto。"""
        if method == "GET":
            self._send_json(200, {"mode": read_panel_window_pref(),
                                  "nativeAvailable": panel_native_available()})
            return
        if method != "POST":
            self._send_json(405, {"error": "GET or POST only"})
            return
        body = self._read_json_body() or {}
        ok = write_panel_window_pref(body.get("mode") or "auto")
        self._send_json(200 if ok else 500, {
            "ok": ok, "mode": read_panel_window_pref(),
            "nativeAvailable": panel_native_available(),
            "note": "下次开窗生效；已开着的窗口关掉重开才会换",
        })

    # ---------- 引擎（跟哪个 agent 对话）----------

    def _engine_status(self):
        eng = engines.active_engine()
        self._send_json(200, {
            "ok": True,
            "active": eng.id,
            "available": engines.list_engines(),
            # 每个引擎的自述（label + 到底能不能用）—— 向导/设置据此标灰不可用的引擎
            "engines": engines.describe(),
            "detail": eng.status(),
        })

    def _engine_switch(self, method: str):
        if method == "GET":
            self._engine_status()
            return
        if method != "POST":
            self._send_json(405, {"error": "GET or POST only"})
            return
        eid = str((self._read_json_body() or {}).get("engine") or "")
        if not engines.is_registered(eid):
            self._send_json(400, {"ok": False, "error": "未知引擎", "engine": eid,
                                  "available": engines.list_engines()})
            return
        ok = engines.set_active_engine_id(eid)
        if ok and eid == "acp":
            try:                                     # 切到 ACP 就预热进程，别等第一次提问
                threading.Thread(target=engines.get_engine("acp").prewarm, daemon=True).start()
            except Exception:  # noqa: BLE001
                pass
        if ok and eid == "opencode":
            # ★ 切到 opencode：它没在跑就把**桌面端**拉起来 —— 否则面板反代不到上游，
            #   界面会一直报 `502 无法连接 OpenCode 服务`（用户实测）。
            try:
                import engines.opencode as _oc
                if _oc.launch_desktop():
                    print("[i] 已拉起 OpenCode 桌面端（引擎切到 opencode）")
            except Exception:  # noqa: BLE001
                pass
        self._send_json(200 if ok else 500, {"ok": ok, "active": engines.active_engine_id()})

    def _acp_agents(self, method: str):
        """ACP agentlist：列出 / 切换 / 修改 agent（配置在 runtime/state/_acp_agents.json）。"""
        try:
            eng = engines.get_engine("acp")
        except Exception as exc:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            return
        if method == "GET":
            self._send_json(200, {"ok": True, "active": eng.active_agent_id(),
                                  "detail": eng.status(), "agents": eng.list_agents()})
            return
        if method != "POST":
            self._send_json(405, {"error": "GET or POST only"})
            return
        b = self._read_json_body() or {}
        act = str(b.get("action") or "")
        if act == "detect":
            eng.autofill_agents()
            self._send_json(200, {"ok": True, "active": eng.active_agent_id(),
                                  "detail": eng.status(), "agents": eng.list_agents()})
            return
        if act == "candidates":
            self._send_json(200, {"ok": True, "candidates": acp_agents.scan_candidates()})
            return
        if act == "scan":
            # 「扫描本机」：本机发现报告（含已登记、可用性、客户端缺适配器提示）
            self._send_json(200, {"ok": True, **acp_agents.deep_scan()})
            return
        if act == "guess-provider":
            # 编辑弹窗的「自动」按钮：只**猜**不写（用户点了保存才落盘）
            aid = str(b.get("id") or "")
            a = acp_agents.find_agent(acp_agents.load_registry(), aid) if aid else {}
            if not a:
                a = {"command": b.get("command") or [], "env": b.get("env") or {},
                     "label": b.get("label") or "", "note": b.get("note") or ""}
            self._send_json(200, {"ok": True, "provider": acp_agents.guess_provider(a)})
            return
        if act == "add-folder":
            d = str(b.get("dir") or "")
            cmd, note = acp_agents.infer_from_dir(d)
            if not cmd:
                self._send_json(400, {"ok": False,
                                      "error": "这个文件夹里没找到 ACP agent（既没有 node 适配器，也没有 *acp*.exe）"})
                return
            label = b.get("label") or os.path.basename(d.rstrip("\\/")) or "agent"
            # provider 由 add_agent 自动猜（从命令/目录推断）；用户可在 agentlist 里改
            a = eng.add_agent(label, cmd, "", {}, note)
            if not a:
                self._send_json(400, {"ok": False, "error": "添加失败"})
                return
            if b.get("activate", False):
                eng.select_agent(a["id"])
            self._send_json(200, {"ok": True, "added": a.get("id"), "note": note,
                                  "provider": a.get("provider") or "",
                                  "active": eng.active_agent_id(),
                                  "detail": eng.status(), "agents": eng.list_agents()})
            return
        if act == "add":
            cmd = b.get("command")
            if isinstance(cmd, str):
                cmd = [x for x in re.split(r"\s+", cmd.strip()) if x]
            a = eng.add_agent(b.get("label") or "", cmd,
                              b.get("cwd") or "", b.get("env") or {}, b.get("note") or "",
                              b.get("provider") or "")
            if not a:
                self._send_json(400, {"ok": False, "error": "需要 label 与 command"})
                return
            if b.get("activate", True):
                eng.select_agent(a["id"])
            self._send_json(200, {"ok": True, "added": a.get("id"), "active": eng.active_agent_id(),
                                  "detail": eng.status(), "agents": eng.list_agents()})
            return
        if act == "delete":
            if not eng.remove_agent(str(b.get("id") or "")):
                self._send_json(400, {"ok": False,
                                      "error": "不能删除（当前使用中 / 不存在 / 至少要留一个）"})
                return
            self._send_json(200, {"ok": True, "active": eng.active_agent_id(),
                                  "detail": eng.status(), "agents": eng.list_agents()})
            return
        aid = str(b.get("id") or "")
        if not aid:
            self._send_json(400, {"ok": False, "error": "缺少 id"})
            return
        patch = {k: b[k] for k in ("label", "command", "cwd", "env", "note", "provider", "mode") if k in b}
        if patch and not eng.patch_agent(aid, patch):
            self._send_json(404, {"ok": False, "error": "agent 不存在", "id": aid})
            return
        if b.get("activate", True):
            eng.select_agent(aid)
        self._send_json(200, {"ok": True, "active": eng.active_agent_id(),
                              "detail": eng.status(), "agents": eng.list_agents()})

    # ---------- QQ音乐 ----------

    def _read_json_body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    def _qq_state(self):
        state = _read_music_file()
        age = None
        try:
            age = max(0.0, time.time() - os.path.getmtime(MUSIC_FILE))
        except OSError:
            age = None
        self._send_json(200, {
            "ok": True,
            "age": round(age, 1) if age is not None else None,
            "stale": age is None or age > 3.0,
            "daemon": bool(_music_proc is not None and _music_proc.poll() is None),
            "qqRunning": qq_running(),
            "spectrum": (lambda a: bool(a is not None and a < 3.0))(spectrum_age()),
            "volume": volume_get(),
            "music": state,
        })

    def _qq_control(self, method: str):
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        action = str((self._read_json_body() or {}).get("action", ""))
        if action not in ("playpause", "play", "pause", "next", "prev", "stop"):
            self._send_json(400, {"error": "bad action", "action": action})
            return
        script = os.path.join(TOOLS_DIR, "smtc-control.ps1")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-Action", action],
                capture_output=True, text=True, timeout=25, creationflags=NO_WINDOW,
            ).stdout.strip()
            ok = '"ok":true' in out.replace(" ", "")
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            return
        self._send_json(200, {"ok": ok, "action": action, "raw": out[:200]})

    def _qq_volume(self, method: str):
        if method == "POST":
            body = self._read_json_body() or {}
            v = volume_set(body.get("value", 100))
        else:
            v = volume_get()
        self._send_json(200, {"ok": v >= 0, "volume": v})

    def _qq_lyrics(self):
        q = parse_qs(urlsplit(self.path).query)
        title = (q.get("title", [""])[0] or "").strip()
        artist = (q.get("artist", [""])[0] or "").strip()
        if not title:
            self._send_json(400, {"error": "title required"})
            return
        data = qq_lyrics(title, artist, translate=(q.get("tr", ["1"])[0] != "0"))
        data.pop("_t", None)
        self._send_json(200, data)

    def _qq_search(self):
        q = parse_qs(urlsplit(self.path).query)
        kw = (q.get("q", [""])[0] or "").strip()
        if not kw:
            self._send_json(400, {"error": "q required"})
            return
        self._send_json(200, qq_search(kw))

    def _qq_spectrum(self):
        data = _read_spectrum()
        age = spectrum_age()
        bars = data.get("bars") or []
        self._send_json(200, {
            "ok": bool(bars) and age is not None and age < 3.0,
            "age": round(age, 2) if age is not None else None,
            "device": data.get("device"),          # 采集器当前盯着的输出设备（蓝牙切走时一眼能看出来）
            "bars": bars,
            "error": data.get("error"),
        })

    def _qq_app(self, method: str):
        """启动 / 关闭 QQ音乐 客户端。"""
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        action = str((self._read_json_body() or {}).get("action", ""))
        if action == "start":
            ok, err = start_qqmusic()
            self._send_json(200 if ok else 500, {"ok": ok, "action": "start", "error": err})
        elif action == "stop":
            self._send_json(200, {"ok": True, "action": "stop", "windows": close_qqmusic()})
        elif action == "status":
            self._send_json(200, {"ok": True, "running": bool(_qq_pids())})
        else:
            self._send_json(400, {"ok": False, "error": "bad action"})

    # ---------- 动态壁纸（创意工坊 mp4）----------

    def _pick_path(self, script_name: str, tag: str, what: str):
        """弹一个 Windows 原生选择框，把结果路径回给前端。

        `what` 只用在报错文案里（「选择文件夹超时」/「选择图片超时」）。
        取消 → `{ok:false, canceled:true}`；脚本缺失 → 500。
        ⚠ 读回来的内容要剥掉可能的 BOM（PS5.1 的 `Set-Content -Encoding UTF8` 会写 BOM，
          路径前面多一个 `\\ufeff` 就打不开了）。
        """
        script = os.path.join(TOOLS_DIR, script_name)
        if not os.path.isfile(script):
            self._send_json(500, {"ok": False, "error": "缺少 tools/%s" % script_name})
            return
        out = os.path.join(tempfile.gettempdir(),
                           "cocraft-%s-%d-%d.txt" % (tag, os.getpid(), int(time.time() * 1000)))
        proc = None
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                 "-File", script, "-OutFile", out],
                capture_output=True, text=True, timeout=300, creationflags=NO_WINDOW,
            )
            path = ""
            try:
                with open(out, "r", encoding="utf-8", errors="replace") as fh:
                    path = fh.read().strip().lstrip("\ufeff")
            except OSError:
                path = ""
            if not path:
                self._send_json(200, {"ok": False, "canceled": True, "path": ""})
                return
            self._send_json(200, {"ok": True, "canceled": False, "path": path})
        except subprocess.TimeoutExpired:
            self._send_json(504, {"ok": False, "error": "%s超时" % what})
        except Exception as exc:  # noqa: BLE001
            detail = (proc.stderr or "")[:300] if proc is not None else ""
            self._send_json(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}", "detail": detail})
        finally:
            try:
                if os.path.exists(out):
                    os.remove(out)
            except OSError:
                pass

    def _live_root_pick(self, method: str):
        """弹一个 Windows 原生文件夹选择框；取消返回 {ok:false, canceled:true}。"""
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        self._pick_path("pick_folder.ps1", "pick", "选择文件夹")

    def _pick_image(self, method: str):
        """弹一个原生「选图片」框（自定义头像 / 空会话素材用）。"""
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        self._pick_path("pick_image.ps1", "pickimg", "选择图片")

    def _pick_folder(self, method: str):
        """弹一个原生「选文件夹」框（新会话选工作目录用）。"""
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        self._pick_path("pick_folder.ps1", "pickdir", "选择文件夹")

    def _open_dir(self, method: str):
        """在外部程序里打开目录：{what:"terminal"|"editor"|"explorer", dir}。"""
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        b = self._read_json_body() or {}
        what = str(b.get("what") or "terminal").strip().lower()
        directory = str(b.get("dir") or "")
        res = external.open_in(what, directory)
        self._send_json(200 if res.get("ok") else 400, res)

    # ---------- 面板内终端（ConPTY） ----------

    def _pty_component(self):
        """永远 200：告诉前端终端组件装没装（没装就禁用入口并说明）。"""
        self._send_json(200, {"ok": True, "component": pty_term.component_info()})

    def _pty_route(self, method: str, path: str):
        if not pty_term.terminal_available():
            self._send_json(503, {"ok": False, "installed": False,
                                  "error": pty_term.component_info()["error"]})
            return
        parts = [p for p in path[len("/pty"):].split("/") if p]
        if not parts:                                   # /pty
            if method == "GET":
                self._send_json(200, {"ok": True, "data": pty_term.list_all()})
                return
            if method == "POST":
                b = self._read_json_body() or {}
                try:
                    s = pty_term.create(str(b.get("cwd") or ""),
                                        int(b.get("cols") or 100), int(b.get("rows") or 30))
                except Exception as exc:  # noqa: BLE001
                    self._send_json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
                    return
                self._send_json(200, {"ok": True, "data": s.info()})
                return
            self._send_json(405, {"error": "GET or POST only"})
            return

        sid = parts[0]
        if len(parts) == 1:                             # /pty/{id}
            if method == "DELETE":
                ok = pty_term.remove(sid)
                self._send_json(200 if ok else 404, {"ok": ok})
                return
            if method == "GET":
                s = pty_term.get(sid)
                if not s:
                    self._send_json(404, {"ok": False, "error": "终端不存在（可能已关闭）"})
                    return
                self._send_json(200, {"ok": True, "data": s.info()})
                return
            self._send_json(405, {"error": "GET or DELETE only"})
            return

        action = parts[1]
        s = pty_term.get(sid)
        if not s:
            self._send_json(404, {"ok": False, "error": "终端不存在（可能已关闭）"})
            return
        if action == "stream" and method == "GET":
            self._pty_stream(s)
            return
        if action == "input" and method == "POST":
            b = self._read_json_body() or {}
            ok = s.write(str(b.get("data") or ""))
            self._send_json(200 if ok else 409, {"ok": ok})
            return
        if action == "resize" and method == "POST":
            b = self._read_json_body() or {}
            ok = s.resize(int(b.get("cols") or s.cols), int(b.get("rows") or s.rows))
            self._send_json(200 if ok else 409, {"ok": ok})
            return
        self._send_json(404, {"ok": False, "error": "未知操作"})

    def _pty_stream(self, s):
        """把终端输出以 SSE 流给前端（我们不支持 WebSocket）。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        def emit(obj: dict):
            self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        emit({"t": "ready", "info": s.info()})
        try:
            while True:
                chunk = s.read(timeout=15)
                if chunk is None:
                    emit({"t": "exit", "code": s.exit_code})
                    break
                if chunk == "":
                    self.wfile.write(b": ping\n\n")     # 保活，别让中间层掐断
                    self.wfile.flush()
                    continue
                emit({"t": "out", "d": chunk})
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- 编码模式：列目录 / 读文件 / 写文件（引擎无关） ----------

    def _fs_q(self):
        q = parse_qs(urlsplit(self.path).query)
        return (q.get("root") or [""])[0], (q.get("path") or [""])[0]

    def _fs_list(self):
        root, path = self._fs_q()
        try:
            self._send_json(200, {"ok": True, "data": fsview.list_dir(root, path)})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _fs_read(self):
        root, path = self._fs_q()
        try:
            self._send_json(200, {"ok": True, "data": fsview.read_file(root, path)})
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})

    def _fs_write(self, method: str):
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        b = self._read_json_body() or {}
        text = b.get("text")
        try:
            data = fsview.write_file(str(b.get("root") or ""), str(b.get("path") or ""),
                                     "" if text is None else str(text))
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        self._send_json(200, {"ok": True, "data": data})

    # ---------- 软件更新（查 GitHub Releases 最新版 / 一键静默更新） ----------

    def _update_check(self):
        q = parse_qs(urlsplit(self.path).query)
        force = (q.get("force") or ["0"])[0] in ("1", "true", "yes")
        self._send_json(200, {"ok": True, "data": updater.check(force=force)})

    def _update_apply(self, method: str):
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        res = updater.apply_update()
        self._send_json(200 if res.get("ok") else 400, res)

    def _appearance(self, method: str):
        """自定义外观（头像 / 空会话素材）：GET 读当前，POST 设置或清除。"""
        if method == "GET":
            self._send_json(200, {"ok": True, "data": read_appearance(),
                                  "keys": list(APPEARANCE_KEYS), "version": appearance_version()})
            return
        if method != "POST":
            self._send_json(405, {"error": "GET or POST only"})
            return
        b = self._read_json_body() or {}
        ok, msg = write_appearance_image(b.get("key"), b.get("path"))
        self._send_json(200 if ok else 400,
                        {"ok": ok, "message": msg, "error": "" if ok else msg,
                         "data": read_appearance(), "version": appearance_version()})

    def _appearance_img(self):
        """把某个位置的自定义图流出去（没设 / 文件没了 → 404，前端退回自带素材）。"""
        q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        key = str((q.get("k") or [""])[0])
        target = read_appearance().get(key)
        if not target or not os.path.isfile(target):
            self._send_json(404, {"error": "no custom image", "key": key})
            return
        self._send_file(target)

    def _live_root(self, method: str):
        """自定义动态壁纸读取目录：空 = 自动查找本机 Wallpaper Engine 创意工坊 431960。"""
        if method == "GET":
            self._send_json(200, {
                "ok": True,
                "override": read_wallpaper_root_override(),
                "auto": auto_we_root() or "",
                "effective": we_root() or "",
            })
            return
        if method != "POST":
            self._send_json(405, {"error": "GET or POST only"})
            return
        body = self._read_json_body() or {}
        ok, msg = write_wallpaper_root_override(str(body.get("root") or ""))
        _we_root_cache["path"] = None                     # 立刻让下一次请求重新解析
        _we_root_cache["t"] = 0.0
        self._send_json(200 if ok else 400, {
            "ok": ok,
            "override": read_wallpaper_root_override(),
            "auto": auto_we_root() or "",
            "effective": we_root() or "",
            "message": msg,
        })

    def _live_wallpapers(self):
        """给前端：当前各主题生效的原片（只支持 video 类壁纸）。"""
        root = we_root()
        files = live_wallpaper_files() if root else {}
        out = {"ok": bool(files), "root": root, "hiru": None, "yoru": None, "detail": {}}
        for theme, (rel, label) in files.items():
            url = "/we/" + "/".join(quote(seg) for seg in rel.split("/"))
            out[theme] = url
            try:
                size = os.path.getsize(os.path.join(root, rel))
            except OSError:
                size = 0
            out["detail"][theme] = {"title": label, "file": os.path.basename(rel),
                                    "size_mb": round(size / 1048576.0, 1), "url": url,
                                    "kind": "video"}
        self._send_json(200, out)

    def _live_list(self):
        """所有能用浏览器播的壁纸（含用户当前选择），给面板里的选择器用。"""
        root = we_root()
        items = list_video_wallpapers() if root else []
        files = live_wallpaper_files() if root else {}
        self._send_json(200, {
            "ok": bool(items), "root": root, "items": items, "pick": _read_pick(),
            "detail": {t: {"title": v[1]} for t, v in files.items()},
        })

    def _live_pick(self, method: str):
        """用户在面板里选壁纸：{theme: "hiru"|"yoru", id: "<创意工坊 id>"|null}（null = 恢复默认）"""
        if method != "POST":
            self._send_json(405, {"error": "POST only"})
            return
        body = self._read_json_body() or {}
        theme = str(body.get("theme") or "")
        if theme not in ("hiru", "yoru"):
            self._send_json(400, {"error": "theme 必须是 hiru / yoru"})
            return
        root = we_root()
        wid = body.get("id")
        wid = str(wid) if wid else None
        if wid and (not root or wid not in _we_titles(root)):
            self._send_json(400, {"error": "找不到这个壁纸 id", "id": wid})
            return
        _write_pick(theme, wid)
        files = live_wallpaper_files() if root else {}
        self._send_json(200, {"ok": True, "pick": _read_pick(), "theme": theme, "id": wid,
                              "detail": {t: {"title": v[1]} for t, v in files.items()}})

    def _we(self, path: str):
        """/we/<相对 431960 的路径> —— 流式发创意工坊里的文件（给 <video> 用）。"""
        root = we_root()
        if not root:
            self._send_json(404, {"error": "没找到 Wallpaper Engine 创意工坊目录"})
            return
        rel = urllib.parse.unquote(path[len("/we/"):])
        target = os.path.normpath(os.path.join(root, rel))
        if not target.startswith(root) or not os.path.isfile(target):
            self._send_json(404, {"error": "not found", "path": path})
            return
        self._send_file(target)

    # ---------- 静态文件 ----------

    def _ctype_for(self, target: str) -> str:
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        return ctype

    def _send_file(self, target: str, base: int = 0, total: int = None, ctype: str = None):
        """发文件，**支持 Range**。
        没有 Range 的话 <video> 得把整份下完才能播（我们的壁纸原片是 300~400MB）——
        所以这里必须实现 206 / Content-Range。"""
        try:
            file_size = os.path.getsize(target)
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        size = int(total) if total is not None else max(0, file_size - base)
        start, end, status = 0, size - 1, 200
        rng = (self.headers.get("Range") or "").strip()
        m = re.match(r"bytes=(\d*)-(\d*)$", rng)
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:                                  # bytes=-N：最后 N 字节
                start = max(0, size - int(m.group(2)))
                end = size - 1
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype or self._ctype_for(target))
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(target, "rb") as fh:
            fh.seek(base + start)
            left = length
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        root, rel = FRONTEND_DIR, path.lstrip("/")
        for prefix, extra in EXTRA_ROOTS.items():
            if path.startswith(prefix):
                root, rel = extra, path[len(prefix):]
                break
        target = os.path.normpath(os.path.join(root, rel))
        if not target.startswith(root) or not os.path.isfile(target):
            self._send_json(404, {"error": "not found", "path": path})
            return
        self._send_file(target)

    # ---------- 反向代理 ----------

    def _perm_level_api(self, method: str):
        if method == "GET":
            return self._send_json(200, {"ok": True, "level": read_perm_level(),
                                         "levels": [{"id": k, "label": lb} for k, lb in PERM_LEVELS]})
        if method == "POST":
            body = self._read_json_body() or {}
            return self._send_json(200, {"ok": True, "level": write_perm_level(body.get("level"))})
        return self._send_json(405, {"error": "GET/POST only"})

    def _proxy(self, method: str):
        """交给「当前引擎」实现；默认引擎（opencode）就是原来的反代行为。"""
        engines.active_engine().handle_api(self, method)

    def _music_proxy(self, method: str, path: str):
        """把 /music/* 代理到本地音乐服务（127.0.0.1:MUSIC_SVC_PORT）。

        ⚠ 音乐服务是**可选组件**：没装的话这里必须给一句人话，别让用户以为面板坏了。
        """
        if not MUSIC_SVC_PY:
            self._send_json(503, {"ok": False, "installed": False,
                                  "error": music_component()["reason"],
                                  "hint": "重跑安装包勾选「音乐服务」组件，或自建 "
                                          "runtime/venvs/music / runtime/site-packages/music"})
            return
        if not music_svc_alive():
            start_music_service()
            for _ in range(10):                     # 刚装完第一次点：给它最多 3 秒起来
                time.sleep(0.3)
                if music_svc_alive():
                    break
        query = urlsplit(self.path).query
        target = "http://127.0.0.1:%d%s" % (MUSIC_SVC_PORT, path[len("/music"):])
        if query:
            target += "?" + query
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {}
        for key, value in self.headers.items():
            if key.lower() in ("host", "content-length", "accept-encoding", "connection"):
                continue
            headers[key] = value
        req = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            upstream = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            upstream = exc
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"ok": False, "error": "音乐服务未就绪: %s: %s"
                                  % (type(exc).__name__, exc)})
            return
        with upstream:
            self.send_response(upstream.status)
            declared = upstream.headers.get("Content-Length")
            for key, value in upstream.headers.items():
                low = key.lower()
                if low in HOP_BY_HOP or low in ("content-length", "transfer-encoding"):
                    continue
                self.send_header(key, value)
            if declared:
                self.send_header("Content-Length", declared)
            else:
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            while True:
                chunk = upstream.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _cocraft_proxy(self, method: str, path: str):
        """把 `/cocraft/*` 代理到 cocraft（Chatser）后端 —— 顺带避开浏览器跨域。

        例：前端 `GET /cocraft/api/info`  →  `https://www.animaker.cc.cd/api/info`
        `Authorization`（Bearer token）原样透传（token 由前端保管）；支持 GET/POST/DELETE/PATCH。
        地址可被环境变量 `OPENCODE_UI_COCRAFT_BASE` 覆盖（比如本地跑 `http://127.0.0.1:5000`）。
        """
        base = (os.environ.get("OPENCODE_UI_COCRAFT_BASE") or COCRAFT_BASE).rstrip("/")
        target = base + path[len("/cocraft"):]
        query = urlsplit(self.path).query
        if query:
            target += "?" + query
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {"Accept": "application/json", "User-Agent": "cocraft"}
        auth = self.headers.get("Authorization")
        if auth:
            headers["Authorization"] = auth
        ctype = self.headers.get("Content-Type")
        if ctype:
            headers["Content-Type"] = ctype
        req = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            upstream = urllib.request.urlopen(req, timeout=60)
        except urllib.error.HTTPError as exc:
            upstream = exc
        except Exception as exc:  # noqa: BLE001
            self._send_json(502, {"ok": False, "error": "cocraft 不可达：%s: %s"
                                  % (type(exc).__name__, exc)})
            return
        with upstream:
            data = upstream.read()
            self.send_response(upstream.status)
            self.send_header("Content-Type", upstream.headers.get(
                "Content-Type", "application/json; charset=utf-8"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        super().server_bind()


def main() -> int:
    parser = argparse.ArgumentParser(description="OpenCode 自建界面的本地代理")
    parser.add_argument("--host", default="127.0.0.1")
    try:
        from panel_port import read_ports as _read_ports
        _defaults = _read_ports()
    except Exception:  # noqa: BLE001
        _defaults = {"port": 8787, "music": 8790}
    parser.add_argument("--port", type=int, default=int(_defaults.get("port", 8787)))
    parser.add_argument("--music-port", type=int, default=int(_defaults.get("music", 8790)),
                        help="本地音乐服务端口（两个副本同时跑时必须错开）")
    args = parser.parse_args()
    global MUSIC_SVC_PORT
    MUSIC_SVC_PORT = int(args.music_port)

    eng = engines.active_engine()
    url = None
    version = None
    try:
        url, _, version = eng.read_service()
    except FileNotFoundError:
        # ⚠ 首次启动 / 还没选引擎时不要直接退出：页面照样要在 8787 上打开，
        # 由前端「首次设置」向导引导选引擎 / agent。缺上游只是代理暂时不可用。
        print(f"[!] 引擎 {eng.id} 未就绪：找不到 {getattr(eng, 'service_state', '')}", file=sys.stderr)
        if eng.id == "opencode":
            # ★ 引擎 = opencode 但桌面端没在跑 → 主动拉起来（否则面板一直 502）。
            try:
                import engines.opencode as _oc
                if _oc.launch_desktop():
                    print("[i] OpenCode 没在跑 → 已拉起桌面端（等它就绪后页面会自动重连）",
                          file=sys.stderr)
            except Exception:  # noqa: BLE001
                pass
            print("    （若仍不行：在 http://127.0.0.1:8787 的首次设置里换成 ACP，或手动启动 OpenCode）",
                  file=sys.stderr)
    except NotImplementedError:
        pass                       # 该引擎不需要上游地址（例如 ACP 自己拉起 agent 子进程）
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 引擎 {eng.id} 未就绪：{exc}", file=sys.stderr)

    # 自愈：把「子命令式 ACP」（典型：`opencode-cli.exe acp`）补进注册表。
    # ⚠ 安装那一刻的 init_state 可能正好探测不到（环境变量不全 / 文件数上限 / 链接未就绪），
    #   漏了的话用户就会觉得"OpenCode 明明在本机、agentlist 里却没有它"——所以每次启动都补一次。
    try:
        added = acp_agents.ensure_opencode_agent()
        if added:
            print(f"[i] 已自动补上 ACP agent：{added}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 自动补 ACP agent 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
    # 自愈：把「随包 OpenCode（node 版）」补进注册表（与上面的本机客户端是两条，互不覆盖）。
    try:
        added = acp_agents.ensure_bundled_opencode_agent()
        if added:
            print(f"[i] 已自动补上 ACP agent：{added}")
    except Exception as exc:  # noqa: BLE001
        print(f"[!] 自动补随包 OpenCode 失败：{type(exc).__name__}: {exc}", file=sys.stderr)

    httpd = None
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as exc:
        # 端口已被占用通常意味着"已经在跑了"，对自启动来说属于正常情况
        print(f"[i] 端口 {args.host}:{args.port} 已在监听，视为已在运行（{exc}）")
        return 0

    # 启动前先清掉上次遗留的辅助进程（脱离父进程 + SO_REUSEADDR 会越积越多）。
    # ⚠ 它内部要起 powershell —— 在"powershell 起不来"的机器上会**弹 0xc0000142 模态框并卡住**，
    #   所以放**后台线程**：面板服务该起就起，别被这一步拖死（VM 白屏事故就是这么来的）。
    def _cleanup_stale_bg():
        try:
            _n = cleanup.kill_stale(exclude=[os.getpid()])
            if _n:
                print(f"[i] 已清理 {_n} 个遗留辅助进程")
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=_cleanup_stale_bg, daemon=True).start()

    start_music_daemon()                      # 拉起 SMTC 采集进程（QQ音乐联动）
    threading.Thread(target=music_keeper, daemon=True).start()
    start_spectrum()                          # 拉起频谱采集进程（真·音频条）
    threading.Thread(target=spectrum_keeper, daemon=True).start()
    start_music_service()                     # 拉起网易云音乐服务（面板内搜歌/放歌）
    threading.Thread(target=music_service_keeper, daemon=True).start()
    threading.Thread(target=pty_term.warmup, daemon=True).start()   # 预热面板内终端（可选组件，别让首次点开卡探测）

    # ACP 引擎：启动就预热当前 agent 的子进程（第一次提问不用等 node 冷启动）
    if eng.id == "acp":
        try:
            threading.Thread(target=eng.prewarm, daemon=True).start()
        except Exception:  # noqa: BLE001
            pass

    print("cocraft 已启动")
    print(f"  界面    http://{args.host}:{args.port}")
    print(f"  引擎    {eng.id}")
    if url:
        print(f"  上游    {url}  ({eng.label} {version})")
    print("  停止    Ctrl+C")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        stop_music_daemon()
        stop_spectrum()
        stop_music_service()
        try:                                     # 收起所有 ACP agent 子进程
            engines.get_engine("acp").shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:                                     # 关掉面板内终端（含其宿主子进程）
            pty_term.close_all()
        except Exception:  # noqa: BLE001
            pass
        try:
            cleanup.kill_stale(exclude=[os.getpid()])
        except Exception:  # noqa: BLE001
            pass
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
