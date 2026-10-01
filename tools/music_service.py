# -*- coding: utf-8 -*-
"""本地音乐服务（网易云 + QQ音乐 · 均为非官方接口）。

跑在隔离环境 `runtime/venvs/music` 里，由 `backend/server.py` 保活；只监听 127.0.0.1。
前端 → server.py 代理(/music/*) → 本服务。统一带 `p=` 选平台（默认 netease）：

    GET  /status                         -> {ok, providers:{netease:{loggedIn,cookieTail}, qq:{...}}}
    GET  /search?p=&q=&limit=&offset=    -> {ok, songs:[{id,name,artists,album,cover,duration}]}
    GET  /playlists?p=                   -> {ok, playlists:[{id,name,count,cover,creator}]}
    GET  /playlist?p=&id=                -> {ok, songs:[{id,name,artists,album,cover,duration,mediaMid}]}
    GET  /recommend?p=                   -> {ok, playlists:[{id,name,count,cover,creator}]}（推荐歌单）
    GET  /cover?p=&u=                    -> 歌单封面（服务端临时缓存，只放行音乐站图片）
    POST /cover/clear                    -> 清空歌单封面缓存（选完歌单后由前端调用）
    GET  /url?p=&id=&level=              -> {ok, url, ...}
    GET  /stream?p=&id=&level=           -> 音频流（支持 Range）
    POST /cookie  {provider, cookie}     -> 保存（cookie 为空则清除）

- netease：PyPI `NeteaseCloudMusic`（V8 算 eapi 签名，HTTP 由 Python 发）。
- qq：算法移植自 Mineradio（`server.js`），**纯标准库**（hashlib/base64/urllib），不要 Node。

⚠ 仅供个人自用；接口非官方，随时可能失效。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE_DIR = os.path.join(ROOT, "runtime", "state")
os.makedirs(STATE_DIR, exist_ok=True)
COOKIE_FILE = os.path.join(STATE_DIR, "_music_cookie.json")
COVER_DIR = os.path.join(ROOT, "runtime", "cache", "music_covers")
PORT = 8790
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# SDK 会在「当前工作目录」建一个 diskcache 用的 `cache/`；把它引到隔离环境里，别脏了项目根
_VENV_DIR = os.path.join(ROOT, "runtime", "venvs", "music")
if os.path.isdir(_VENV_DIR):
    os.chdir(_VENV_DIR)

# ---------------- 网易云：旧 V8 的 encodeURIComponent 按 UTF-16 编码，crypto-js 的
# UTF-8 编码因此出错 → 中文搜索乱码。注入正确的 UTF-8 版覆盖它。 ----------------
SHIM = r'''
globalThis.encodeURIComponent = (function () {
  function h(x) { var s = x.toString(16).toUpperCase(); return s.length < 2 ? '0' + s : s; }
  return function (str) {
    str = String(str); var out = '';
    for (var i = 0; i < str.length; i++) {
      var c = str.charCodeAt(i);
      if (c < 0x80) { out += '%' + h(c); }
      else if (c < 0x800) { out += '%' + h(0xC0 | (c >> 6)) + '%' + h(0x80 | (c & 0x3F)); }
      else if (c >= 0xD800 && c <= 0xDBFF) {
        var c2 = str.charCodeAt(++i), cp = 0x10000 + ((c - 0xD800) << 10) + (c2 - 0xDC00);
        out += '%' + h(0xF0 | (cp >> 18)) + '%' + h(0x80 | ((cp >> 12) & 0x3F))
             + '%' + h(0x80 | ((cp >> 6) & 0x3F)) + '%' + h(0x80 | (cp & 0x3F));
      } else {
        out += '%' + h(0xE0 | (c >> 12)) + '%' + h(0x80 | ((c >> 6) & 0x3F)) + '%' + h(0x80 | (c & 0x3F));
      }
    }
    return out;
  };
})();
'''

_api = None
_lock = threading.Lock()


def get_api():
    global _api
    with _lock:
        if _api is None:
            from NeteaseCloudMusic import NeteaseCloudMusicApi
            a = NeteaseCloudMusicApi()
            a.ctx.eval(SHIM)
            _api = a
        return _api


# ---------------- Cookie（按平台分开存）----------------

def read_cookies() -> dict:
    out = {"netease": "", "qq": ""}
    try:
        with open(COOKIE_FILE, "r", encoding="utf-8") as fh:
            d = json.load(fh) or {}
        if "netease" in d or "qq" in d:
            out["netease"] = str(d.get("netease") or "")
            out["qq"] = str(d.get("qq") or "")
        else:                                  # 兼容旧的 {cookie: "..."}
            out["netease"] = str(d.get("cookie") or "")
    except Exception:  # noqa: BLE001
        pass
    return out


def write_cookie(provider: str, value: str) -> None:
    d = read_cookies()
    d[provider if provider in ("netease", "qq") else "netease"] = value or ""
    with open(COOKIE_FILE, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False, indent=2)


# ---------------- 网易云 ----------------

def ne_search(q: str, limit: int, offset: int) -> list:
    api = get_api()
    r = api.request("/cloudsearch", {"keywords": q, "limit": limit, "offset": offset,
                                     "type": 1, "cookie": read_cookies()["netease"]})
    songs = (((r or {}).get("data") or {}).get("result") or {}).get("songs") or []
    out = []
    for s in songs:
        al = s.get("al") or {}
        out.append({"id": s.get("id"), "name": s.get("name"),
                    "artists": " / ".join(a.get("name", "") for a in (s.get("ar") or [])),
                    "album": al.get("name", ""), "cover": al.get("picUrl", ""),
                    "duration": int((s.get("dt") or 0) / 1000)})
    return out


def ne_url(sid: str, level: str) -> dict:
    api = get_api()
    r = api.request("/song/url/v1", {"id": sid, "level": level, "cookie": read_cookies()["netease"]})
    arr = (((r or {}).get("data") or {}).get("data") or [])
    if not arr:
        return {"ok": False, "error": "no data"}
    d = arr[0]
    return {"ok": bool(d.get("url")), "url": d.get("url"), "br": d.get("br"),
            "size": d.get("size"), "fee": d.get("fee"), "type": d.get("type"),
            "error": None if d.get("url") else "该曲目无可用音源（可能需会员）"}


def ne_stream_headers() -> dict:
    return {"User-Agent": UA, "Referer": "https://music.163.com/"}


def _ne_data(r) -> dict:
    """NeteaseCloudMusic 的响应统一是 {code, data}；取内层 data。"""
    return (r or {}).get("data") or {}


def _ne_http_json(url: str, cookie: str) -> dict:
    """直连 music.163.com 老接口（不走 SDK 的 eapi/weapi 签名）。"""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Referer": "https://music.163.com/", "Cookie": cookie or ""})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _ne_track(it: dict) -> dict:
    al = it.get("al") or it.get("album") or {}
    ar = it.get("ar") or it.get("artists") or []
    return {"id": it.get("id"), "name": it.get("name"),
            "artists": " / ".join(a.get("name", "") for a in ar),
            "album": al.get("name", ""), "cover": al.get("picUrl", ""),
            "duration": int((it.get("dt") or it.get("duration") or 0) / 1000)}


def ne_playlists() -> dict:
    """登录用户自己的歌单（网易云）。需要 Cookie 含 MUSIC_U。

    ⚠ SDK 的 eapi `/user/account` 对浏览器 Cookie 可能返回 `account:null`
    （实测本机：老接口能认，eapi 不认）。所以优先走老接口 `/api/nuser/account/get` +
    `/api/user/playlist`，失败再回退 SDK。
    """
    ck = read_cookies()["netease"]
    if ck:
        try:
            acc = _ne_http_json("https://music.163.com/api/nuser/account/get", ck)
            uid = (acc.get("account") or {}).get("id") or (acc.get("profile") or {}).get("userId")
            if uid:
                pl = _ne_http_json("https://music.163.com/api/user/playlist/?uid=%s&limit=100&offset=0" % uid, ck)
                out = []
                for p in (pl.get("playlist") or []):
                    out.append({"id": p.get("id"), "name": p.get("name") or "",
                                "count": p.get("trackCount") or 0, "cover": p.get("coverImgUrl") or "",
                                "creator": (p.get("creator") or {}).get("nickname") or ""})
                return {"ok": True, "uid": uid, "playlists": out}
        except Exception:  # noqa: BLE001
            pass

    api = get_api()
    acc = _ne_data(api.request("/user/account", {"cookie": ck}))
    uid = (acc.get("account") or {}).get("id") or (acc.get("profile") or {}).get("userId")
    if not uid:
        return {"ok": False, "error": "网易云未登录：Cookie 需含 MUSIC_U（到设置里重新粘贴【完整】Cookie）"}
    pl = _ne_data(api.request("/user/playlist", {"uid": uid, "limit": 100, "offset": 0, "cookie": ck}))
    out = []
    for p in (pl.get("playlist") or []):
        out.append({"id": p.get("id"), "name": p.get("name") or "",
                    "count": p.get("trackCount") or 0, "cover": p.get("coverImgUrl") or "",
                    "creator": (p.get("creator") or {}).get("nickname") or ""})
    return {"ok": True, "uid": uid, "playlists": out}


def ne_playlist_songs(pid, limit: int = 300) -> list:
    """歌单歌曲。优先老接口 `/api/v6/playlist/detail`（实测带 Cookie 能拿全），失败回退 SDK。"""
    ck = read_cookies()["netease"]
    if ck:
        try:
            d = _ne_http_json("https://music.163.com/api/v6/playlist/detail?id=%s&n=%s"
                              % (pid, max(1, int(limit))), ck)
            tracks = ((d.get("playlist") or {}).get("tracks") or [])
            if tracks:
                return [_ne_track(t) for t in tracks]
        except Exception:  # noqa: BLE001
            pass

    api = get_api()
    d = _ne_data(api.request("/playlist/track/all", {"id": pid, "limit": limit, "offset": 0, "cookie": ck}))
    songs = d.get("songs") or d.get("tracks") or []
    return [_ne_track(s) for s in songs]


# ---------------- QQ音乐（算法来自 Mineradio server.js，纯标准库）----------------

QQ_UA_AND = "QQMusic 14090508(android 12)"
QQ_H = {"Referer": "https://y.qq.com/", "User-Agent": UA}
QQ_COMM = {
    "ct": "11", "cv": "14090508", "v": "14090508", "tmeAppID": "qqmusic",
    "phonetype": "EBG-AN10", "os_ver": "12", "OpenUDID": "0", "QIMEI36": "0",
    "udid": "0", "chid": "0", "aid": "0", "oaid": "0", "taid": "0", "tid": "0",
    "wid": "0", "uid": "0", "sid": "0", "modeSwitch": "6", "teenMode": "0",
    "ui_mode": "2", "nettype": "1020",
}


def qq_sign(text: str) -> str:
    """Mineradio 的 qqSearchSign：sha1 + 查表 + 异或 + base64。"""
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()
    at = lambda i: h[i] if i < len(h) else ""    # JS: hash[40] === undefined -> join 当空串
    p1 = "".join(at(i) for i in (23, 14, 6, 36, 16, 40, 7, 19))
    p2 = "".join(at(i) for i in (16, 1, 32, 12, 19, 27, 8, 5))
    scramble = [89, 39, 179, 150, 218, 82, 58, 252, 177, 52, 186, 123, 120, 64,
                242, 133, 143, 161, 121, 179]
    b = bytes(v ^ int(h[i * 2:i * 2 + 2], 16) for i, v in enumerate(scramble))
    mid = base64.b64encode(b).decode().translate(str.maketrans("", "", "/+="))
    return ("zzc" + p1 + mid + p2).lower()


def _http_json(url: str, body: bytes | None, headers: dict) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body else "GET")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _parse_cookie(cookie: str) -> dict:
    d = {}
    for part in (cookie or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def qq_uin_key() -> tuple:
    c = _parse_cookie(read_cookies()["qq"])
    uin = (c.get("uin") or c.get("qqmusic_uin") or c.get("wxuin") or "").lstrip("0")
    key = c.get("qm_keyst") or c.get("qqmusic_key") or c.get("music_key") or c.get("wxskey") or ""
    return (uin or "0"), key


def _qq_track(it: dict) -> dict:
    """把 QQ 的 track_info / songlist item 归一成我们的歌曲结构（搜索与歌单共用）。"""
    ti = it.get("track_info") or it.get("songInfo") or it.get("songinfo") or it.get("song") or it
    al = ti.get("album") or {}
    mid = ti.get("mid") or (ti.get("file") or {}).get("media_mid") or ""
    cover = ""
    if al.get("mid"):
        cover = "https://y.gtimg.cn/music/photo_new/T002R300x300M000%s.jpg" % al["mid"]
    return {"id": mid, "name": ti.get("name") or "",
            "artists": " / ".join((s.get("name") or "") for s in (ti.get("singer") or [])),
            "album": al.get("name") or "", "cover": cover,
            "duration": int(ti.get("interval") or 0),
            "mediaMid": (ti.get("file") or {}).get("media_mid") or ""}


def qq_search(q: str, limit: int, offset: int) -> list:
    payload = {"comm": QQ_COMM, "req": {
        "module": "music.search.SearchCgiService", "method": "DoSearchForQQMusicMobile",
        "param": {"search_type": 0, "searchid": str(int(time.time() * 1000)) + str(random.randint(10, 99)),
                  "query": q, "page_num": offset // max(1, limit) + 1, "num_per_page": limit,
                  "highlight": 0, "nqc_flag": 0, "multi_zhida": 0, "cat": 2, "grp": 1,
                  "sin": offset, "sem": 0}}}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": QQ_UA_AND, "Content-Type": "application/json",
               "Content-Length": str(len(body))}
    ck = read_cookies()["qq"]
    if ck:
        headers["Cookie"] = ck
    js = _http_json("https://u.y.qq.com/cgi-bin/musics.fcg?sign=" + qq_sign(body.decode("utf-8")),
                    body, headers)
    data = (js.get("req") or {}).get("data") or {}
    body_data = data.get("body") or data
    items = (body_data.get("item_song")
             or (body_data.get("song") or {}).get("list") or body_data.get("list") or [])
    return [_qq_track(it) for it in items]


QQ_QUALITY = [("M800", ".mp3", "320k"), ("F000", ".flac", "无损"),
              ("M500", ".mp3", "128k"), ("C400", ".m4a", "AAC")]

# ---- QQ 登录自动续期 ----
# 实测：musickey（qm_keyst/qqmusic_key）**约 3 天过期**（refresh 回执 keyExpiresIn=259200），
# 过期后付费歌全 104003（免费歌不受影响）；但 Cookie 里的 psrf_* 令牌（access/refresh token）
# 有效期长得多（expiresAt 约 60 天），可以用 music.login.LoginServer/Login(loginMode=2) 换新密钥。
# ⚠ 关键坑：param.musicid 必须是 **int**，传字符串会回 code=10006（参数错误）。
QQ_REFRESH_AFTER = 2 * 86400        # 密钥用了 2 天就先换（留 1 天余量）
QQ_REFRESH_COOLDOWN = 600           # 失败/强刷的冷却：10 分钟内不重复刷
_qq_refresh_lock = threading.Lock()
_qq_last_refresh = 0.0


def _qq_cookie_dict() -> dict:
    return _parse_cookie(read_cookies()["qq"])


def _qq_apply_refresh(c: dict, d: dict) -> None:
    """把刷新回执写回 Cookie 串（新密钥 + 滚动后的 psrf_* 令牌），落盘。"""
    mk = d.get("musickey") or d.get("musicKey") or ""
    if not mk:
        return
    c["qm_keyst"] = mk
    c["qqmusic_key"] = mk
    for src, dst in (("access_token", "psrf_qqaccess_token"),
                     ("refresh_token", "psrf_qqrefresh_token"),
                     ("openid", "psrf_qqopenid"), ("unionid", "psrf_qqunionid")):
        if d.get(src):
            c[dst] = str(d[src])
    if d.get("expired_at"):
        c["psrf_access_token_expiresAt"] = str(d["expired_at"])
    if d.get("musickeyCreateTime"):
        c["psrf_musickey_createtime"] = str(d["musickeyCreateTime"])
    if d.get("refresh_key"):
        c["refresh_key"] = str(d["refresh_key"])
    write_cookie("qq", "; ".join("%s=%s" % (k, v) for k, v in c.items() if v != ""))


def qq_refresh_login() -> dict:
    """用 psrf_* 令牌换一份新 musickey。返回 {ok, refreshed, ...}（不含密钥明文）。"""
    c = _qq_cookie_dict()
    uin = (c.get("uin") or c.get("qqmusic_uin") or "").lstrip("0")
    if not uin:
        return {"ok": False, "error": "QQ音乐未登录"}
    access = c.get("psrf_qqaccess_token") or ""
    refresh = c.get("psrf_qqrefresh_token") or ""
    openid = c.get("psrf_qqopenid") or ""
    if not (access and refresh and openid):
        return {"ok": False, "error": "缺少 psrf_* 令牌，无法自动续期（需重新登录一次）"}
    param = {
        "openid": openid,
        "access_token": access,
        "refresh_token": refresh,
        "expired_in": int(c.get("psrf_access_token_expiresAt") or 0),
        "musicid": int(uin),                       # ⚠ 必须 int，字符串 → 10006
        "musickey": c.get("qm_keyst") or c.get("qqmusic_key") or "",
        "refresh_key": c.get("refresh_key") or "",
        "loginMode": 2,
    }
    payload = {"comm": {"uin": uin, "format": "json", "ct": 24, "cv": 0, "tmeLoginType": 2},
               "req": {"module": "music.login.LoginServer", "method": "Login", "param": param}}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": UA, "Content-Type": "application/json",
               "Content-Length": str(len(body)), **QQ_H, "Cookie": read_cookies()["qq"]}
    js = _http_json("https://u.y.qq.com/cgi-bin/musicu.fcg", body, headers)
    req = js.get("req") or {}
    data = req.get("data") or {}
    if req.get("code") != 0 or not (data.get("musickey") or data.get("musicKey")):
        return {"ok": False, "code": req.get("code"), "error": "续期失败（code=%s）" % req.get("code")}
    _qq_apply_refresh(c, data)
    return {"ok": True, "refreshed": True, "code": 0,
            "keyExpiresIn": data.get("keyExpiresIn"),
            "musickeyCreateTime": data.get("musickeyCreateTime")}


def qq_musickey_age() -> int:
    """当前密钥用了多少秒；读不到 createtime 回 -1。"""
    ts = _qq_cookie_dict().get("psrf_musickey_createtime") or ""
    try:
        t = int(ts)
    except (TypeError, ValueError):
        return -1
    return max(0, int(time.time()) - t) if t > 0 else -1


def qq_ensure_fresh(force: bool = False) -> dict:
    """密钥快到期（或 force）就续期一次；带锁 + 冷却，失败不影响播放。"""
    global _qq_last_refresh
    now = time.time()
    age = qq_musickey_age()
    if not force and not (age >= 0 and age >= QQ_REFRESH_AFTER):
        return {"ok": True, "skipped": True, "age": age}
    with _qq_refresh_lock:
        if time.time() - _qq_last_refresh < QQ_REFRESH_COOLDOWN:
            return {"ok": True, "skipped": True, "reason": "cooldown", "age": age}
        _qq_last_refresh = time.time()
    try:
        return qq_refresh_login()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


def qq_url(mid: str, media_mid: str = "") -> dict:
    """取播放地址：先确保密钥新鲜；若因 104003 失败则强刷一次再试。"""
    qq_ensure_fresh()
    r = _qq_url_once(mid, media_mid)
    if not r.get("ok") and "104003" in str(r.get("error") or ""):
        if qq_ensure_fresh(force=True).get("refreshed"):
            r = _qq_url_once(mid, media_mid)
    return r


def _qq_url_once(mid: str, media_mid: str = "") -> dict:
    mid = str(mid or "").strip()
    if not mid:
        return {"ok": False, "error": "missing mid"}
    uin, key = qq_uin_key()
    ck = read_cookies()["qq"]
    guid = str(10000000 + random.randint(0, 89999999))
    ids = [x for x in dict.fromkeys([str(media_mid or "").strip(), mid]) if x]
    # ⚠ 高音质的文件名常要用 media_mid（≠ songmid）；两种都列，逐个探测谁真能下
    fns = [p + i + e for p, e, _ in QQ_QUALITY for i in ids]
    comm = {"uin": uin, "format": "json", "ct": 19 if key else 24, "cv": 0}
    if key:
        comm["authst"] = key
    payload = {"comm": comm, "req_0": {
        "module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
        "param": {"guid": guid, "songmid": [mid] * len(fns), "songtype": [0] * len(fns),
                  "uin": uin, "loginflag": 1, "platform": "20", "filename": fns}}}
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": UA, "Content-Type": "application/json",
               "Content-Length": str(len(body)), **QQ_H}
    if ck:
        headers["Cookie"] = ck
    try:
        js = _http_json("https://u.y.qq.com/cgi-bin/musicu.fcg", body, headers)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    d = (js.get("req_0") or {}).get("data") or {}
    infos = d.get("midurlinfo") or []
    sip = (d.get("sip") or ["http://aqqmusic.tc.qq.com/"])[0]
    by_fn = {i.get("filename"): i for i in infos}
    tried = 0
    for prefix, ext, label in QQ_QUALITY:                 # 音质优先；每个再用 media_mid / songmid
        for i in ids:
            fn = prefix + i + ext
            info = by_fn.get(fn)
            if not (info and info.get("purl")):
                continue
            tried += 1
            if tried > 6:
                break
            url = sip + info["purl"]
            if _probe_audio(url):                          # 只有真能取到音频的才算
                return {"ok": True, "url": url, "filename": fn, "quality": label}
    last = infos[0] if infos else None
    if not ck:
        reason = "未登录 QQ 音乐（点「登录」贴【完整】Cookie：uin=…; qm_keyst=…）"
    elif last is not None:
        code = last.get("result") or last.get("code")
        if str(code) == "104003":
            reason = ("无播放授权（104003）。两种可能：①这份 QQ Cookie 的音乐密钥已过期"
                      "（qm_keyst 大约 3 天就过期）——重新登录 y.qq.com 后重贴【完整】Cookie 即可；"
                      "②该曲目需购买数字专辑/单曲（会员不覆盖），只能单独购买。")
        else:
            reason = "有 vkey 但音频文件取不到（可能该音质无文件），换一首试试"
    else:
        reason = "该曲目无可用音源"
    return {"ok": False, "error": reason}


def qq_stream_headers() -> dict:
    return {"User-Agent": UA, "Referer": "https://y.qq.com/"}


def _probe_audio(url: str) -> bool:
    """Range 探测：真能拿到音频头才算可用（vkey 会对不存在的文件也返回 purl）。"""
    try:
        req = urllib.request.Request(url, headers=qq_stream_headers())
        req.add_header("Range", "bytes=0-1023")
        with urllib.request.urlopen(req, timeout=8) as r:
            st = r.status
            head = r.read(4)
        if st not in (200, 206):
            return False
        return (head[:3] == b"ID3" or head[:4] == b"fLaC" or head[4:8] == b"ftyp"
                or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0))
    except Exception:  # noqa: BLE001
        return False


def _qq_fcg(module: str, method: str, param: dict) -> dict:
    """调 QQ 移动端 musics.fcg（带 sign）；返回 req.data。"""
    uin, key = qq_uin_key()
    payload = {"comm": dict(QQ_COMM, uin=uin, ct=19 if key else 24, cv=0),
               "req": {"module": module, "method": method, "param": param}}
    if key:
        payload["comm"]["authst"] = key
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    headers = {"User-Agent": QQ_UA_AND, "Content-Type": "application/json",
               "Content-Length": str(len(body))}
    ck = read_cookies()["qq"]
    if ck:
        headers["Cookie"] = ck
    js = _http_json("https://u.y.qq.com/cgi-bin/musics.fcg?sign=" + qq_sign(body.decode("utf-8")), body, headers)
    return ((js.get("req") or {}).get("data")) or {}


def qq_playlists() -> dict:
    """登录用户自己的歌单（QQ音乐）。"""
    uin, _ = qq_uin_key()
    if not uin or uin == "0":
        return {"ok": False, "error": "QQ音乐未登录：贴【完整】Cookie（uin=…; qm_keyst=…）"}
    data = _qq_fcg("music.musicasset.PlaylistBaseRead", "GetPlaylistByUin", {"uin": uin})
    out = []
    for p in (data.get("v_playlist") or []):
        out.append({"id": p.get("tid"), "dirId": p.get("dirId"), "name": p.get("dirName") or "",
                    "count": p.get("songNum") or 0, "cover": p.get("picUrl") or "",
                    "creator": p.get("nick") or ""})
    return {"ok": True, "uin": uin, "playlists": out}


def qq_playlist_songs(pid, limit: int = 300) -> list:
    data = _qq_fcg("music.srfDissInfo.DissInfo", "CgiGetDiss",
                   {"disstid": int(pid), "num": limit, "page": 0})
    return [_qq_track(it) for it in (data.get("songlist") or [])]


# QQ 推荐歌单：实测该接口会**忽略 sin 偏移**，sortId 大多也不生效 —— 只有换
# categoryId、或 sortId=2 才会换一批（2026-09-26 逐项探测确认）。
# 用一个游标轮流取不同的「分类 × 排序」，保证连续两次调用（点「换一批」）必定不同。
QQ_RECO_PAIRS = [(c, s)
                 for c in ("10000000", "222", "6", "8", "11", "13", "14", "15", "16", "17", "18")
                 for s in ("5", "2")]
_qq_reco_i = 0


def qq_recommend(limit: int = 12) -> list:
    """QQ 推荐歌单（公开 fcg，无需登录）。每次调用轮换一批，供前端「换一批」。"""
    global _qq_reco_i
    cat, sort = QQ_RECO_PAIRS[_qq_reco_i % len(QQ_RECO_PAIRS)]
    _qq_reco_i += 1
    q = {"picmid": "1", "rnd": str(int(time.time() * 1000)), "g_tk": "5381",
         "loginUin": "0", "hostUin": "0", "format": "json",
         "inCharset": "utf8", "outCharset": "utf-8", "notice": "0",
         "platform": "yqq.json", "needNewCode": "0",
         "categoryId": cat, "sortId": sort, "sin": "0", "ein": str(max(1, limit))}
    url = ("https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg?"
           + urllib.parse.urlencode(q))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://y.qq.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        js = json.loads(r.read().decode("utf-8", "replace"))
    lst = ((js.get("data") or {}).get("list")) or []
    out = [{"id": it.get("dissid"), "name": it.get("dissname") or "",
            "count": it.get("listennum") or 0,
            "cover": it.get("imgurl") or it.get("disscover") or "",
            "creator": ""} for it in lst]
    if not out:                      # 个别分类偶尔空 → 退回归一化「全部」
        return qq_recommend_all(limit)
    return out


def qq_recommend_all(limit: int = 12) -> list:
    """兜底：categoryId=10000000（全部）最热。"""
    q = {"picmid": "1", "rnd": str(int(time.time() * 1000)), "g_tk": "5381",
         "loginUin": "0", "hostUin": "0", "format": "json",
         "inCharset": "utf8", "outCharset": "utf-8", "notice": "0",
         "platform": "yqq.json", "needNewCode": "0",
         "categoryId": "10000000", "sortId": "5", "sin": "0", "ein": str(max(1, limit))}
    url = ("https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg?"
           + urllib.parse.urlencode(q))
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://y.qq.com/"})
    with urllib.request.urlopen(req, timeout=20) as r:
        js = json.loads(r.read().decode("utf-8", "replace"))
    lst = ((js.get("data") or {}).get("list")) or []
    return [{"id": it.get("dissid"), "name": it.get("dissname") or "",
             "count": it.get("listennum") or 0,
             "cover": it.get("imgurl") or it.get("disscover") or "",
             "creator": ""} for it in lst]


def ne_recommend(limit: int = 12) -> list:
    """网易云推荐歌单（/personalized，公开）。"""
    api = get_api()
    ck = read_cookies()["netease"]
    d = _ne_data(api.request("/personalized", {"limit": limit, "cookie": ck}))
    arr = d.get("result") or []
    return [{"id": it.get("id"), "name": it.get("name") or "",
             "count": it.get("playCount") or it.get("playcount") or 0,
             "cover": it.get("picUrl") or "", "creator": ""} for it in arr]


# ---------------- 歌单封面：服务端临时缓存 ----------------
# 封面 URL 由上游接口给出（QQ 多为 y.gtimg.cn / p.qpic.cn，网易云多为 *.music.126.net），
# 直接丢给 <img> 可能被防盗链挡住，所以统一走这里代理；只放行已知音乐站域名。
# 前端每次看推荐/我的歌单都把封面抓进这个目录，选完歌单再调 /cover/clear 清掉。

COVER_CTYPE = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp"}
COVER_HOSTS = ("music.126.net", "126.net", "gtimg.cn", "qpic.cn", "qq.com")
COVER_MAX_BYTES = 4 * 1024 * 1024


def _cover_norm(url: str) -> str:
    """把协议相对地址（//host/...）补成 https。"""
    url = str(url or "").strip()
    return "https:" + url if url.startswith("//") else url


def _cover_ok(url: str) -> bool:
    """只放行音乐站图片，避免这个代理被拿去请求任意地址。"""
    try:
        u = urllib.parse.urlsplit(_cover_norm(url))
    except Exception:  # noqa: BLE001
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in COVER_HOSTS)


def _cover_key(url: str) -> str:
    return hashlib.sha1(_cover_norm(url).encode("utf-8")).hexdigest()


def _cover_lookup(key: str):
    for ext, ctype in COVER_CTYPE.items():
        path = os.path.join(COVER_DIR, key + ext)
        if os.path.isfile(path):
            return path, ctype
    return None, None


def _cover_ext(url: str, ctype: str) -> str:
    ctype = (ctype or "").split(";")[0].strip().lower()
    for ext, ct in COVER_CTYPE.items():
        if ctype == ct:
            return ".jpg" if ext == ".jpeg" else ext
    path = (urllib.parse.urlsplit(_cover_norm(url)).path or "").lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


def _cover_fetch(url: str, provider: str):
    referer = "https://y.qq.com/" if provider == "qq" else "https://music.163.com/"
    req = urllib.request.Request(_cover_norm(url), headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read(COVER_MAX_BYTES + 1)
        ctype = resp.headers.get("Content-Type") or ""
    if len(data) > COVER_MAX_BYTES:
        raise ValueError("cover too large")
    if not data:
        raise ValueError("empty cover")
    ext = _cover_ext(url, ctype)
    os.makedirs(COVER_DIR, exist_ok=True)
    path = os.path.join(COVER_DIR, _cover_key(url) + ext)
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return path, COVER_CTYPE.get(ext, "image/jpeg")


def clear_covers() -> int:
    """删掉封面缓存目录里的文件（选完歌单后调用）。删不掉的（被占用）跳过。"""
    n = 0
    try:
        names = os.listdir(COVER_DIR)
    except FileNotFoundError:
        return 0
    except OSError:
        return 0
    for name in names:
        try:
            os.remove(os.path.join(COVER_DIR, name))
            n += 1
        except OSError:
            pass
    return n


# ---------------- 分发 ----------------

def do_search(provider: str, q: str, limit: int, offset: int) -> list:
    return qq_search(q, limit, offset) if provider == "qq" else ne_search(q, limit, offset)


def do_playlists(provider: str) -> dict:
    return qq_playlists() if provider == "qq" else ne_playlists()


def do_playlist(provider: str, pid: str) -> dict:
    try:
        songs = qq_playlist_songs(pid) if provider == "qq" else ne_playlist_songs(pid)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "songs": songs}


def do_recommend(provider: str) -> dict:
    try:
        items = qq_recommend() if provider == "qq" else ne_recommend()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    return {"ok": True, "playlists": items}


def do_url(provider: str, sid: str, level: str, media_mid: str = "") -> dict:
    return qq_url(sid, media_mid) if provider == "qq" else ne_url(sid, level)


def stream_headers(provider: str) -> dict:
    return qq_stream_headers() if provider == "qq" else ne_stream_headers()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = (q.get("p") or ["netease"])[0]
        if p not in ("netease", "qq"):
            p = "netease"
        try:
            if u.path == "/status":
                cks = read_cookies()
                qk_age = qq_musickey_age()
                self._json(200, {"ok": True, "providers": {
                    "netease": {"loggedIn": bool(cks["netease"]), "cookieTail": cks["netease"][-12:]},
                    "qq": {"loggedIn": bool(cks["qq"]), "cookieTail": cks["qq"][-12:],
                           "keyAgeSeconds": qk_age,
                           "keyExpiresIn": 259200,
                           "canAutoRefresh": bool(
                               _parse_cookie(cks["qq"]).get("psrf_qqaccess_token")
                               and _parse_cookie(cks["qq"]).get("psrf_qqrefresh_token"))}}})
            elif u.path == "/refresh":
                # 手动强制续期（QQ）：用 psrf_* 令牌换新 musickey
                self._json(200, qq_refresh_login() if p == "qq" else {"ok": False, "error": "only qq"})
            elif u.path == "/search":
                kw = (q.get("q") or [""])[0].strip()
                if not kw:
                    self._json(400, {"ok": False, "error": "empty q"})
                    return
                self._json(200, {"ok": True, "songs": do_search(
                    p, kw, int((q.get("limit") or ["20"])[0]), int((q.get("offset") or ["0"])[0]))})
            elif u.path == "/playlists":
                self._json(200, do_playlists(p))
            elif u.path == "/playlist":
                self._json(200, do_playlist(p, (q.get("id") or [""])[0]))
            elif u.path == "/recommend":
                self._json(200, do_recommend(p))
            elif u.path == "/cover":
                self._cover(p, (q.get("u") or [""])[0])
            elif u.path == "/url":
                self._json(200, do_url(p, (q.get("id") or [""])[0],
                                       (q.get("level") or ["standard"])[0],
                                       (q.get("mid2") or [""])[0]))
            elif u.path == "/stream":
                self._stream(p, (q.get("id") or [""])[0],
                             (q.get("level") or ["standard"])[0], (q.get("mid2") or [""])[0])
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            import traceback
            self._json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc),
                             "trace": traceback.format_exc()[-500:]})

    def do_POST(self):
        u = urllib.parse.urlsplit(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            if u.path == "/cookie":
                provider = str(body.get("provider") or "netease")
                ck = str(body.get("cookie") or "")
                write_cookie(provider, ck)
                c = _parse_cookie(ck)
                missing = []
                if provider == "qq":
                    if not (c.get("uin") or c.get("qqmusic_uin") or c.get("wxuin")):
                        missing.append("uin")
                    if not (c.get("qm_keyst") or c.get("qqmusic_key") or c.get("music_key") or c.get("wxskey")):
                        missing.append("qm_keyst")
                elif "MUSIC_U" not in ck:
                    missing.append("MUSIC_U")
                self._json(200, {"ok": True, "provider": provider, "keys": sorted(c.keys()),
                                 "missing": missing,
                                 "loggedIn": bool(read_cookies().get(provider))})
            elif u.path == "/cover/clear":
                self._json(200, {"ok": True, "cleared": clear_covers()})
            else:
                self._json(404, {"ok": False, "error": "not found"})
        except Exception as exc:  # noqa: BLE001
            self._json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})

    def _cover(self, provider: str, url: str):
        """代理并缓存歌单封面；Cache-Control: no-store 让浏览器每次都问服务端。"""
        url = _cover_norm(url)
        if not _cover_ok(url):
            self._json(400, {"ok": False, "error": "invalid cover url"})
            return
        key = _cover_key(url)
        path, ctype = _cover_lookup(key)
        if not path:
            try:
                path, ctype = _cover_fetch(url, provider)
            except Exception as exc:  # noqa: BLE001
                self._json(502, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
                return
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            self._json(500, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, provider: str, sid: str, level: str, media_mid: str = ""):
        info = do_url(provider, sid, level, media_mid)
        url = info.get("url")
        if not url:
            self._json(404, {"ok": False, "error": info.get("error") or "no url"})
            return
        headers = stream_headers(provider)
        rng = self.headers.get("Range")
        if rng:
            headers["Range"] = rng
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except Exception as exc:  # noqa: BLE001
            self._json(502, {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.headers.get("Content-Type", "audio/mpeg"))
        for h in ("Content-Length", "Content-Range", "Accept-Ranges"):
            if resp.headers.get(h):
                self.send_header(h, resp.headers[h])
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


def main() -> int:
    port = PORT
    if "--port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--port") + 1])
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print("music_service on http://127.0.0.1:%d" % port)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
