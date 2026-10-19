# -*- coding: utf-8 -*-
"""主题工坊（Theme Workshop）—— 从 GitHub 安装社区主题（CSS + 素材，**绝不执行脚本**）。

设计见 `docs/THEMES.md`。要点：
  · 主题 = 一个目录（`theme.json` + 可选 `theme.css` / 素材）；**引擎无关**、纯前端样式。
  · 安装到 `runtime/themes/<id>/`；来源记录 `.cocraft.json`（用于更新 / 卸载）。
  · 网络复用 `skills_install` 的 GitHub 层（tree API + 代理并行，国内快）。
  · 安全：id 白名单、realpath 前缀校验、体积/文件数上限、CSS 里的远程 `url()` **一律拦截**。
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import time
import urllib.parse

import skills_install as sk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES_DIR = os.path.join(ROOT, "runtime", "themes")
STATE_DIR = os.path.join(ROOT, "runtime", "state")
THEME_STATE_FILE = os.path.join(STATE_DIR, "_theme.json")

# 官方主题仓库（可用 env 覆盖，便于本地联调）
DEFAULT_CATALOG = os.environ.get("COCRAFT_THEME_CATALOG") or "huaian307/cocraft-themes"

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")     # 主题 id
RESERVED = {"hiru", "yoru", "cocraft"}               # 内置占用
MANIFEST = ".cocraft.json"

MAX_BYTES = 20 * 1024 * 1024      # 单主题总大小
MAX_FILES = 300                   # 单主题文件数
MAX_CSS_BYTES = 1024 * 1024       # 单个 CSS 上限
MAX_IMG_BYTES = 6 * 1024 * 1024   # 远程预览图上限

# 内置主题（真正实现在 frontend/style.css，这里只做清单）
# ⚠ 夜主题已在 `ui 0.1.50` 删除（CSS 全没了）→ **清单里只留「昼」**。
#   以前这里还挂着一个 `yoru`，于是「主题工坊」会列出一张点不出效果的死卡片（2026-10-16 修）。
BUILTIN = [
    {"id": "hiru", "name": "昼（内置）", "author": "cocraft", "version": "内置",
     "description": "内置白昼主题", "mode": "light", "builtin": True},
]

_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.I)


# ---------------------------------------------------------------- 基础

def ensure_dir() -> None:
    os.makedirs(THEMES_DIR, exist_ok=True)
    os.makedirs(STATE_DIR, exist_ok=True)


def valid_id(tid: str) -> bool:
    tid = str(tid or "")
    return bool(ID_RE.match(tid)) and tid not in RESERVED


def _norm_rel(p: str):
    """把 CSS 里的相对路径规范成主题目录内的相对路径；逃逸返回 None。"""
    p = str(p or "").replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    out = []
    for seg in p.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            if not out:
                return None
            out.pop()
        else:
            out.append(seg)
    return "/".join(out) or None


def _asset_url(tid: str, rel: str) -> str:
    return "/themes/asset?id=%s&path=%s" % (
        urllib.parse.quote(str(tid), safe=""), urllib.parse.quote(rel, safe=""))


def _rewrite_css(css: str, tid: str) -> str:
    """主题 CSS 里的 url() 重写：相对路径 → 本地素材；远程 http(s) → 拦截。"""
    def rep(m):
        inner = (m.group(2) or "").strip()
        if not inner:
            return m.group(0)
        low = inner.lower()
        if low.startswith(("data:", "#", "/")):
            return m.group(0)                       # data / 服务器绝对路径 / 锚点：保留
        if low.startswith(("http://", "https://", "//")):
            return 'url("data:,")'                  # 远程一律拦截（防外联/泄露）
        rel = _norm_rel(inner)
        if not rel:
            return 'url("data:,")'
        return 'url("%s")' % _asset_url(tid, rel)
    return _URL_RE.sub(rep, css)


def theme_dir(tid: str) -> str:
    return os.path.join(THEMES_DIR, tid)


def asset_path(tid: str, rel: str) -> str:
    """解析主题素材的绝对路径（严查越界）。找不到 → ValueError。"""
    if not valid_id(tid):
        raise ValueError("非法的主题 id：%s" % tid)
    base = theme_dir(tid)
    if not os.path.isdir(base):
        raise ValueError("主题未安装：%s" % tid)
    r = _norm_rel(rel)
    if not r:
        raise ValueError("非法的素材路径：%s" % rel)
    base_abs = os.path.abspath(base)
    p = os.path.abspath(os.path.join(base_abs, *r.split("/")))
    if p != base_abs and not p.startswith(base_abs + os.sep):
        raise ValueError("路径越界：%s" % rel)
    if not os.path.isfile(p):
        raise ValueError("素材不存在：%s" % rel)
    return p


# ---------------------------------------------------------------- 本地主题读取

def _finalize_meta(meta: dict) -> dict:
    """校验 + 归一化一份 theme.json（本地/远程共用）。非法抛 ValueError。"""
    if not isinstance(meta, dict):
        raise ValueError("theme.json 不是对象")
    tid = str(meta.get("id") or "").strip()
    if not valid_id(tid):
        raise ValueError("id 不合法（^[a-z0-9][a-z0-9-]{1,63}$，且不得为内置名）：%r" % tid)
    if not str(meta.get("name") or "").strip():
        raise ValueError("缺 name")
    meta["id"] = tid
    meta["name"] = str(meta.get("name")).strip()
    meta["author"] = str(meta.get("author") or "匿名").strip()
    meta["version"] = str(meta.get("version") or "1.0.0").strip()
    meta["description"] = str(meta.get("description") or "").strip()
    meta["mode"] = "dark" if str(meta.get("mode") or "").strip().lower() == "dark" else "light"
    # 明/暗变体：主题可同时提供 `light` / `dark` 两个块（各自可有 vars/css/icons/background）；
    # 没提供就只支持默认那一种（顶栏「夜/昼」按钮会被禁用）。见 docs/THEMES.md。
    blocks = [m for m in ("light", "dark") if isinstance(meta.get(m), dict)]
    meta["modes"] = blocks or [meta["mode"]]
    if meta["mode"] not in meta["modes"]:
        meta["mode"] = meta["modes"][0]
    return meta


def _load_meta(folder: str) -> dict:
    """读 + 校验一个主题目录的 theme.json。非法抛 ValueError。"""
    mp = os.path.join(folder, "theme.json")
    with open(mp, "r", encoding="utf-8-sig") as fh:
        meta = json.load(fh)
    return _finalize_meta(meta)


def _effective(meta: dict, mode: str) -> dict:
    """把「共享顶层」与「该 mode 的块」合并出有效负载。"""
    out = {
        "vars": dict(meta.get("vars") or {}),
        "css": str(meta.get("css") or ""),
        "icons": dict(meta.get("icons") or {}),
        "background": dict(meta.get("background") or {}),
    }
    blk = meta.get(mode)
    if isinstance(blk, dict):
        if isinstance(blk.get("vars"), dict):
            out["vars"].update(blk["vars"])
        if blk.get("css"):
            out["css"] = (out["css"] + "\n" + str(blk["css"])) if out["css"] else str(blk["css"])
        if isinstance(blk.get("icons"), dict):
            out["icons"].update(blk["icons"])
        if isinstance(blk.get("background"), dict):
            out["background"].update(blk["background"])
    return out


def _pub(meta: dict, builtin: bool = False) -> dict:
    return {
        "id": meta.get("id"), "name": meta.get("name"), "author": meta.get("author"),
        "version": meta.get("version"), "description": meta.get("description"),
        "mode": meta.get("mode"), "modes": list(meta.get("modes") or [meta.get("mode")]),
        "builtin": bool(builtin), "preview": meta.get("preview") or "",
    }


def _is_builtin(tid: str) -> bool:
    return any(b.get("id") == tid for b in BUILTIN)


def read_active() -> str:
    """当前生效的主题 id。

    ★**兜底**：内置主题被删（例如 `yoru` 在 `ui 0.1.50` 随夜主题一起没了）/ 主题被卸掉了 →
    一律回落到「hiru」。否则老状态会让启动套一个**不存在的主题**（界面上看不出哪里坏了）。
    """
    try:
        with open(THEME_STATE_FILE, "r", encoding="utf-8") as fh:
            tid = str(json.load(fh).get("id") or "hiru")
    except Exception:  # noqa: BLE001
        return "hiru"
    if _is_builtin(tid):
        return tid
    try:
        if valid_id(tid) and os.path.isdir(theme_dir(tid)):
            return tid
    except Exception:  # noqa: BLE001
        pass
    return "hiru"


def write_active(tid: str) -> None:
    ensure_dir()
    try:
        with open(THEME_STATE_FILE, "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"id": tid, "ts": int(time.time())}, fh, ensure_ascii=False)
    except OSError:
        pass


def _read_manifest(folder: str) -> dict:
    try:
        with open(os.path.join(folder, MANIFEST), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_manifest(folder: str, source: str, tid: str, path: str) -> None:
    try:
        with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8", newline="\n") as fh:
            json.dump({"source": source, "id": tid, "path": path,
                       "installedAt": int(time.time())}, fh, ensure_ascii=False)
    except OSError:
        pass


def list_installed() -> list:
    ensure_dir()
    out = []
    for name in sorted(os.listdir(THEMES_DIR)):
        folder = os.path.join(THEMES_DIR, name)
        if not os.path.isdir(folder):
            continue
        try:
            meta = _load_meta(folder)
        except Exception:  # noqa: BLE001
            continue
        item = _pub(meta)
        item["source"] = _read_manifest(folder).get("source") or ""
        item["hasPreview"] = bool(meta.get("preview")) and os.path.isfile(
            os.path.join(folder, *_norm_rel(meta.get("preview") or "x").split("/"))) if meta.get("preview") else False
        out.append(item)
    return out


def get_theme(tid: str, mode: str = "") -> dict:
    """完整主题负载（给前端套用）：vars/css/icons/background（按 mode 合并）。内置只回身份信息。"""
    tid = str(tid or "").strip()
    if tid == "yoru":
        tid = "hiru"                     # ⚠ 夜主题已删：旧的引用一律当「昼」，别把启动搞挂
    if tid == "hiru":
        return {"ok": True, "id": "hiru", "mode": "light",
                "modes": ["light"], "builtin": True}
    if not valid_id(tid):
        raise ValueError("非法的主题 id：%s" % tid)
    folder = theme_dir(tid)
    if not os.path.isdir(folder):
        raise ValueError("主题未安装：%s" % tid)
    meta = _load_meta(folder)
    modes = list(meta["modes"])
    mode = str(mode or "").strip().lower()
    if mode not in modes:
        mode = meta["mode"]
    eff = _effective(meta, mode)

    css = ""
    cf = eff.get("css")
    if cf:
        r = _norm_rel(cf)
        if r:
            cp = os.path.join(folder, *r.split("/"))
            if os.path.isfile(cp):
                try:
                    if os.path.getsize(cp) <= MAX_CSS_BYTES:
                        with open(cp, "r", encoding="utf-8", errors="replace") as fh:
                            css = _rewrite_css(fh.read(), tid)
                except OSError:
                    css = ""

    icons = {}
    for slot, rel in (eff.get("icons") or {}).items():
        r = _norm_rel(rel)
        if r and os.path.isfile(os.path.join(folder, *r.split("/"))):
            icons[str(slot)] = _asset_url(tid, r)

    background = {}
    for key in ("light", "dark"):
        rel = (eff.get("background") or {}).get(key)
        r = _norm_rel(rel)
        if r and os.path.isfile(os.path.join(folder, *r.split("/"))):
            background[key] = _asset_url(tid, r)

    preview = ""
    if meta.get("preview"):
        r = _norm_rel(meta["preview"])
        if r and os.path.isfile(os.path.join(folder, *r.split("/"))):
            preview = _asset_url(tid, r)

    return {
        "ok": True, "id": tid, "mode": mode, "modes": modes, "builtin": False,
        "vars": {str(k): str(v) for k, v in (eff.get("vars") or {}).items()
                 if str(k).startswith("--")},
        "css": css, "icons": icons, "background": background, "preview": preview,
    }


def uninstall(tid: str) -> dict:
    if not valid_id(tid):
        raise ValueError("不能卸载这个主题：%s" % tid)
    folder = theme_dir(tid)
    if not os.path.isdir(folder):
        raise ValueError("主题未安装：%s" % tid)
    shutil.rmtree(folder)
    if read_active() == tid:
        write_active("hiru")
    return {"ok": True, "removed": folder, "id": tid}


def update(tid: str) -> dict:
    if not valid_id(tid):
        raise ValueError("不能更新这个主题：%s" % tid)
    folder = theme_dir(tid)
    man = _read_manifest(folder)
    src = str(man.get("source") or "")
    if not src:
        raise ValueError("这个主题没有「安装来源」记录（不是从面板装的）→ 请用「安装」重新指定来源")
    return install(src, ids=[tid], force=True)


# ---------------------------------------------------------------- 远程（GitHub）

def _theme_json_paths(blobs: list, sub: str) -> list:
    sub = str(sub or "").strip("/")
    out = []
    for b in blobs:
        p = b["path"]
        if sub and not (p == sub or p.startswith(sub + "/")):
            continue
        base = p.rsplit("/", 1)[-1].lower()
        if base == "theme.json":
            out.append(p)
    return out


def _gh_emit(owner: str, repo: str, ref: str, tdir: str):
    prefix = (tdir + "/") if tdir else ""

    def emit(dest: str) -> int:
        blobs, _ = sk._gh_tree(owner, repo, ref)
        files = []
        for b in blobs:
            p = b["path"]
            if p.startswith(".git/"):
                continue
            if prefix:
                if p.startswith(prefix):
                    files.append((p[len(prefix):], p))
            elif "/" not in p:
                files.append((p, p))
        if len(files) > MAX_FILES:
            raise ValueError("文件数超过上限（%d）" % MAX_FILES)
        total = sum(int(next((b["size"] for b in blobs if b["path"] == r), 0)) for _, r in files)
        if total > MAX_BYTES:
            raise ValueError("体积超过上限（%d MB）" % (MAX_BYTES // 1024 // 1024))
        os.makedirs(dest, exist_ok=True)
        sk._emit_parallel(dest, files, lambda remote: sk._fetch_file(owner, repo, ref, remote))
        return len(files)
    return emit


def _src_for_preview(owner: str, repo: str, ref: str) -> str:
    return "%s/%s" % (owner, repo)


def _items_gh(source: str, owner: str, repo: str, ref: str, subpath: str) -> list:
    blobs, _ = sk._gh_tree(owner, repo, ref)
    items = []
    for tj in sorted(_theme_json_paths(blobs, subpath)):
        tdir = tj.rsplit("/", 1)[0] if "/" in tj else ""
        try:
            meta = json.loads(sk._fetch_file(owner, repo, ref, tj).decode("utf-8-sig") or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        if not meta.get("id") and tdir:
            meta["id"] = os.path.basename(tdir)
        try:
            meta = _finalize_meta(meta)
        except Exception:  # noqa: BLE001
            continue
        tid = meta["id"]
        prefix = (tdir + "/") if tdir else ""
        n = nb = 0
        for b in blobs:
            p = b["path"]
            if prefix:
                if p.startswith(prefix):
                    n += 1
                    nb += int(b["size"] or 0)
            elif "/" not in p:
                n += 1
                nb += int(b["size"] or 0)
        pv = str(meta.get("preview") or "")
        preview_url = ""
        if pv:
            r = _norm_rel(pv)
            if r:
                relpath = (tdir + "/" + r) if tdir else r
                preview_url = "/themes/img?source=%s&path=%s" % (
                    urllib.parse.quote(source, safe=""), urllib.parse.quote(relpath, safe=""))
        it = _pub(meta)
        it.update({"dir": tdir, "files": n, "bytes": nb, "previewUrl": preview_url,
                   "_emit": _gh_emit(owner, repo, ref, tdir)})
        items.append(it)
    return items


def _items_url_catalog(url: str, doc: dict) -> list:
    """HTTP catalog：{"themes":[{"id","path",…}]}，从 <base>/<path>/theme.json 取。"""
    if not isinstance(doc, dict) or not isinstance(doc.get("themes"), list):
        raise ValueError("catalog 里没有 themes 数组")
    base = url[: url.rfind("/") + 1]
    items = []
    for e in doc["themes"]:
        if not isinstance(e, dict):
            continue
        path = str(e.get("path") or e.get("id") or "").strip("/")
        if not path:
            continue
        tj = base + urllib.parse.quote(path, safe="/") + "/theme.json"
        try:
            meta = json.loads(sk._http(tj, cap=MAX_CSS_BYTES).decode("utf-8-sig") or "{}")
        except Exception:  # noqa: BLE001
            continue
        if not isinstance(meta, dict):
            continue
        if not meta.get("id"):
            meta["id"] = str(e.get("id") or path.rsplit("/", 1)[-1]).strip()
        try:
            meta = _finalize_meta(meta)
        except Exception:  # noqa: BLE001
            continue
        tid = meta["id"]

        def make_emit(tj=tj, meta=meta, tid=tid):
            def emit(dest: str) -> int:
                # HTTP 无目录列表 → 只装清单目录下已被 theme.json 声明的文件 + 常见素材
                want = ["theme.json"]
                if meta.get("css"):
                    want.append(str(meta["css"]))
                want += [str(v) for v in (meta.get("icons") or {}).values()]
                want += [str(v) for v in (meta.get("background") or {}).values()]
                if meta.get("preview"):
                    want.append(str(meta["preview"]))
                d = tj[: tj.rfind("/") + 1]
                files = []
                seen = set()
                for rel in want:
                    r = _norm_rel(rel)
                    if not r or r in seen:
                        continue
                    seen.add(r)
                    files.append(r)
                os.makedirs(dest, exist_ok=True)
                sk._emit_parallel(dest, [(r, urllib.parse.quote(r, safe="/")) for r in files],
                                  lambda remote, d=d: sk._http(d + remote))
                return len(files)
            return emit
        it = _pub(meta)
        it.update({"dir": "", "files": 0, "bytes": 0, "previewUrl": "", "_emit": make_emit()})
        items.append(it)
    return items


def _items_for(source: str) -> list:
    ps = sk.parse_source(source)
    if ps["kind"] == "url":
        url = ps["url"]
        if url.lower().endswith(".json"):
            doc = sk._json_url(url)
            return _items_url_catalog(url, doc)
        raise ValueError("URL 来源要是 catalog 的 .json（例如 https://…/themes.json）")
    owner, repo = ps["owner"], ps["repo"]
    ref = ps["ref"] or sk._default_branch(owner, repo)
    return _items_gh(source, owner, repo, ref, ps.get("subpath") or "")


def preview(source: str) -> dict:
    src = str(source or "").strip()
    if not src:
        raise ValueError("请输入来源：owner/repo、GitHub 链接，或 catalog 的 .json")
    items = _items_for(src)
    if not items:
        raise ValueError("这个来源里没找到主题（theme.json）")
    out = []
    for it in items:
        out.append({k: it.get(k) for k in
                    ("id", "name", "author", "version", "description", "mode", "modes",
                     "dir", "files", "bytes", "previewUrl")})
    return {"ok": True, "source": src, "themes": out}


def install(source: str, ids=None, force: bool = False) -> dict:
    ensure_dir()
    det = _items_for(source)
    if not det:
        raise ValueError("这个来源里没找到可安装的主题")
    want = [str(x) for x in (ids or []) if str(x)]
    by_id = {it["id"]: it for it in det}
    if want:
        missing = [i for i in want if i not in by_id]
        if missing:
            raise ValueError("来源里没有这些主题：%s" % ", ".join(missing))
        det = [by_id[i] for i in want]

    installed, skipped, errors = [], [], []
    for it in det:
        tid = it["id"]
        if not valid_id(tid):
            errors.append({"id": tid, "error": "主题 id 不合法或为内置名"})
            continue
        dest = theme_dir(tid)
        if os.path.exists(dest) and not force:
            skipped.append({"id": tid, "reason": "已存在（勾选「覆盖」可重装）"})
            continue
        try:
            if force and os.path.isdir(dest):
                shutil.rmtree(dest)
            n = it["_emit"](dest)
            meta = _load_meta(dest)                      # 校验装出来的主题
            _write_manifest(dest, source, tid, it.get("dir") or "")
            installed.append({"id": tid, "path": dest, "files": n, "meta": _pub(meta)})
        except Exception as exc:  # noqa: BLE001
            if os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
            errors.append({"id": tid, "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"ok": True, "installed": installed, "skipped": skipped, "errors": errors}


def fetch_remote_image(source: str, path: str):
    """代理远程预览图/素材（限图，≤6MB）。返回 (bytes, ctype)。"""
    ps = sk.parse_source(source)
    rel = _norm_rel(path)
    if not rel:
        raise ValueError("非法的 path")
    low = rel.lower()
    if not low.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".avif")):
        raise ValueError("只代理图片")
    if ps["kind"] == "url":
        data = sk._http(ps["url"].rstrip("/") + "/" + rel, cap=MAX_IMG_BYTES)
    else:
        owner, repo = ps["owner"], ps["repo"]
        ref = ps["ref"] or sk._default_branch(owner, repo)
        if ps.get("subpath"):
            rel = ps["subpath"].strip("/") + "/" + rel
        data = sk._fetch_file(owner, repo, ref, rel)
        if len(data) > MAX_IMG_BYTES:
            raise ValueError("图片太大")
    ctype = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml",
        ".avif": "image/avif",
    }.get(os.path.splitext(rel)[1].lower(), "application/octet-stream")
    return data, ctype
