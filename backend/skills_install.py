# -*- coding: utf-8 -*-
"""技能安装（通用 GitHub / URL / catalog / marketplace）—— 把远程技能装进本机技能目录。

**引擎无关**：写的是跨工具共用的目录，Codex / OpenCode / Claude / Cursor 都能读。

来源（`source`）支持：
  1. `owner/repo` / `https://github.com/owner/repo`        —— 仓库里扫 `SKILL.md`
  2. `…/tree/<ref>/<path>` / `…/blob/<ref>/<file>.md`      —— 子目录 / 单文件
  3. 任意 `https://…/xxx.md`                                —— 单个技能文件
  4. **HTTP catalog**（OpenCode 原生）：`https://…/index.json`（或目录 URL），
     `{"skills":[{"name","version","files":[…]}]}` → 从 `<base>/<name>/<file>` 取。
  5. **Claude 插件市场**：仓库里有 `.claude-plugin/marketplace.json`（`plugins[].source`：
     相对路径 / `github` / `git-subdir`）→ 解析出每个插件里的技能。

⚠ 只做「下载文件 + 写盘」，**绝不执行**技能里的任何脚本。
⚠ 网络（国内实测）：`codeload` 整包 85s+；`raw.githubusercontent` ~13s；`jsDelivr` ~14s；
  **`ghfast.top` / `gh-proxy.com` 代理 raw ~1s** → 列表用 GitHub **tree API**，取文件走**代理链并行**。
"""
from __future__ import annotations

import base64
import concurrent.futures
import io
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "cocraft-panel/0.1.18 (+skills-install)"}
HTTP_TIMEOUT = 20.0
FILE_TIMEOUT = 15.0
SKILL_BYTES_CAP = 40 * 1024 * 1024
SKILL_FILES_CAP = 1200
MAX_WORKERS = 10
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MD_RE = re.compile(r"\.(md|markdown)$", re.I)
MANIFEST = ".cocraft.json"          # 装完后写在技能目录里的「来源记录」（更新用；扫描器忽略它）

_FILE_CACHE = {}
_TREE_CACHE = {}
_CACHE_TTL = 300.0
_RAW_PROXIES = ("https://ghfast.top/", "https://gh-proxy.com/")


# ---------------------------------------------------------------- 基础

def _home_dirs() -> list:
    cands = []
    for k in ("USERPROFILE", "HOME"):
        v = os.environ.get(k)
        if v:
            cands.append(v)
    hd, hp = os.environ.get("HOMEDRIVE"), os.environ.get("HOMEPATH")
    if hd and hp:
        cands.append(hd.rstrip("\\/") + hp)
    la = os.environ.get("LOCALAPPDATA") or ""
    tail = os.path.join("AppData", "Local")
    if la.lower().endswith(tail.lower()):
        cands.append(la[: -len(tail)].rstrip("\\/"))
    try:
        exp = os.path.expanduser("~")
        if exp and exp != "~":
            cands.append(exp)
    except Exception:  # noqa: BLE001
        pass
    seen, out = set(), []
    for p in cands:
        try:
            p = os.path.normpath(str(p))
        except Exception:  # noqa: BLE001
            continue
        if p and p.lower() not in seen and os.path.isdir(p):
            seen.add(p.lower())
            out.append(p)
    return out


def _http(url: str, cap: int = SKILL_BYTES_CAP, timeout: float = HTTP_TIMEOUT,
          headers: dict = None) -> bytes:
    req = urllib.request.Request(url, headers=(headers or UA))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        buf = io.BytesIO()
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            buf.write(chunk)
            if buf.tell() > cap:
                raise ValueError("内容超过上限（%d MB）" % (cap // 1024 // 1024))
        return buf.getvalue()


def _gh_headers() -> dict:
    h = dict(UA)
    h["Accept"] = "application/vnd.github+json"
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = "Bearer " + tok.strip()
    return h


def _gh_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=_gh_headers())
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8", "replace") or "{}")


def _json_url(url: str) -> dict:
    return json.loads(_http(url, cap=8 * 1024 * 1024).decode("utf-8", "replace") or "{}")


# ---------------------------------------------------------------- 来源解析

def parse_source(src: str) -> dict:
    s = str(src or "").strip().strip("`'\"")
    if not s:
        raise ValueError("请输入来源：owner/repo、GitHub 链接，或 catalog/marketplace 的 URL")
    if "://" not in s and re.match(r"^[\w.-]+/[\w.-]+$", s):
        owner, repo = s.split("/", 1)
        return {"kind": "github", "owner": owner, "repo": repo, "ref": "", "subpath": ""}
    u = urllib.parse.urlsplit(s)
    if u.scheme not in ("http", "https"):
        raise ValueError("来源要么是 owner/repo，要么以 http(s):// 开头")
    if (u.netloc or "").lower() in ("github.com", "www.github.com"):
        parts = [p for p in u.path.strip("/").split("/") if p]
        if len(parts) < 2:
            raise ValueError("GitHub 链接至少要 owner/repo")
        owner, repo = parts[0], re.sub(r"\.git$", "", parts[1])
        ref, subpath = "", ""
        if len(parts) >= 4 and parts[2] in ("tree", "blob"):
            ref = parts[3]
            subpath = "/".join(parts[4:])
        return {"kind": "github", "owner": owner, "repo": repo, "ref": ref, "subpath": subpath}
    return {"kind": "url", "url": s}


def _default_branch(owner: str, repo: str) -> str:
    try:
        return str(_gh_json("https://api.github.com/repos/%s/%s" % (owner, repo))
                   .get("default_branch") or "main")
    except Exception:  # noqa: BLE001
        return "main"


def _gh_tree(owner: str, repo: str, ref: str):
    key = "%s/%s@%s" % (owner, repo, ref)
    hit = _TREE_CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1], hit[2]
    url = ("https://api.github.com/repos/%s/%s/git/trees/%s?recursive=1"
           % (owner, repo, urllib.parse.quote(ref, safe="")))
    d, last = None, None
    for _ in range(3):                               # 大仓库偶发 IncompleteRead → 重试
        try:
            d = _gh_json(url)
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.6)
    if d is None:
        raise ValueError("读取仓库文件树失败（%s）" % type(last).__name__)
    blobs = [{"path": t.get("path"), "size": int(t.get("size") or 0)}
             for t in (d.get("tree") or []) if t.get("type") == "blob" and t.get("path")]
    trunc = bool(d.get("truncated"))
    _TREE_CACHE[key] = (time.time(), blobs, trunc)
    return blobs, trunc


def _raw_url(owner: str, repo: str, ref: str, path: str) -> str:
    return "https://raw.githubusercontent.com/%s/%s/%s/%s" % (
        owner, repo, urllib.parse.quote(ref, safe=""), urllib.parse.quote(path, safe="/"))


def _fetch_file(owner: str, repo: str, ref: str, path: str) -> bytes:
    key = (owner, repo, ref, path)
    hit = _FILE_CACHE.get(key)
    if hit and time.time() - hit[0] < _CACHE_TTL:
        return hit[1]
    raw = _raw_url(owner, repo, ref, path)
    errs = []
    for url in [p + raw for p in _RAW_PROXIES] + [raw]:
        try:
            b = _http(url, timeout=FILE_TIMEOUT)
            _FILE_CACHE[key] = (time.time(), b)
            return b
        except Exception as exc:  # noqa: BLE001
            errs.append("%s:%s" % (urllib.parse.urlsplit(url).netloc, type(exc).__name__))
    try:
        d = _gh_json("https://api.github.com/repos/%s/%s/contents/%s?ref=%s"
                     % (owner, repo, urllib.parse.quote(path, safe="/"),
                        urllib.parse.quote(ref, safe="")))
        b = base64.b64decode(d.get("content") or "")
        if b:
            _FILE_CACHE[key] = (time.time(), b)
            return b
    except Exception as exc:  # noqa: BLE001
        errs.append("api:%s" % type(exc).__name__)
    raise ValueError("抓取失败：%s（%s）" % (path, ", ".join(errs)))


def _maybe_file(owner: str, repo: str, ref: str, path: str):
    """尝试取一个文件；取不到回 None（不抛、不用 API 兜底，做"存在性探测"用）。"""
    raw = _raw_url(owner, repo, ref, path)
    for url in [p + raw for p in _RAW_PROXIES] + [raw]:
        try:
            return _http(url, timeout=FILE_TIMEOUT)
        except Exception:  # noqa: BLE001
            continue
    return None


# ---------------------------------------------------------------- frontmatter / id

def _front(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
    if not m:
        return {}
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.strip().startswith("#"):
            k, v = line.split(":", 1)
            k = k.strip()
            if k and k != "metadata":
                meta[k] = v.strip().strip('"').strip("'")
    return meta


def _id_for_path(path: str) -> str:
    p = str(path or "").rstrip("/")
    base = os.path.basename(p)
    if base.lower() in ("skill.md", "skill.markdown"):
        parent = os.path.basename(os.path.dirname(p))
        return parent or "SKILL"
    return os.path.splitext(base)[0] or "skill"


def _safe_join(root: str, *parts: str) -> str:
    base = os.path.abspath(root)
    p = os.path.abspath(os.path.join(base, *parts))
    if p != base and not p.startswith(base + os.sep):
        raise ValueError("路径越界：%s" % "/".join(parts))
    return p


# ================================================================ 技能项
# item = {"id","name","description","files","bytes","emit": fn(dest)->int}
# emit(dest) 负责把该技能所有文件写到 dest（已含支持文件）。

def _repo_items(owner: str, repo: str, ref: str, subpath: str = "") -> list:
    """GitHub 仓库（可选前缀）里的 SKILL.md → items。"""
    blobs, _ = _gh_tree(owner, repo, ref)
    sub = str(subpath or "").strip("/")
    md_paths = []
    for b in blobs:
        p = b["path"]
        if sub and not (p == sub or p.startswith(sub + "/")):
            continue
        if p.lower().endswith(("skill.md", "skill.markdown")):
            md_paths.append(p)
    if not md_paths:
        return []
    items, seen = [], set()
    for md in sorted(md_paths):
        sdir = md.rsplit("/", 1)[0] if "/" in md else ""
        sid = _id_for_path(md)
        if sid in seen:
            continue
        seen.add(sid)
        prefix = (sdir + "/") if sdir else ""
        n_files, n_bytes = 0, 0
        for b in blobs:
            p = b["path"]
            if prefix:
                if p.startswith(prefix):
                    n_files += 1
                    n_bytes += b["size"]
            elif "/" not in p:
                n_files += 1
                n_bytes += b["size"]
        items.append({"id": sid, "dir": sdir, "skill_md": md, "files": n_files, "bytes": n_bytes,
                      "name": sid, "description": "",
                      "_emit": _repo_emit(owner, repo, ref, sdir)})
    _fill_names(items, lambda it: _fetch_file(owner, repo, ref, it["skill_md"]))
    return items


def _repo_emit(owner, repo, ref, sdir):
    prefix = (sdir + "/") if sdir else ""

    def emit(dest, blobs=None):
        bl, _ = _gh_tree(owner, repo, ref)
        files = []
        for b in bl:
            p = b["path"]
            if prefix:
                if p.startswith(prefix):
                    files.append((p[len(prefix):], p))
            elif "/" not in p:
                files.append((p, p))
        if len(files) > SKILL_FILES_CAP:
            raise ValueError("文件数超过上限（%d）" % SKILL_FILES_CAP)
        _emit_parallel(dest, files, lambda remote: _fetch_file(owner, repo, ref, remote))
        return len(files)
    return emit


def _fill_names(items: list, fetch_md) -> None:
    def one(it):
        try:
            meta = _front(fetch_md(it).decode("utf-8", "replace"))
            it["name"] = meta.get("name") or it.get("name") or it["id"]
            it["description"] = meta.get("description") or ""
        except Exception:  # noqa: BLE001
            pass
    if items:
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            list(ex.map(one, items))


def _emit_parallel(dest: str, rel_remote_pairs, fetch):
    os.makedirs(dest, exist_ok=True)
    total = [0]
    lock = threading.Lock()

    def one(rr):
        rel, remote = rr
        data = fetch(remote)
        out = _safe_join(dest, *rel.split("/"))
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "wb") as fh:
            fh.write(data)
        with lock:
            total[0] += len(data)
            if total[0] > SKILL_BYTES_CAP:
                raise ValueError("体积超过上限（%d MB）" % (SKILL_BYTES_CAP // 1024 // 1024))

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        list(ex.map(one, rel_remote_pairs))


# ---------------------------------------------------------------- HTTP catalog（OpenCode 原生）

def _catalog_base(src: str, url: str) -> str:
    if url.lower().endswith("index.json"):
        return url[: -len("index.json")]
    if url.endswith("/"):
        return url
    return url + "/"


def _catalog_items(base: str, index: dict) -> list:
    entries = index.get("skills") if isinstance(index, dict) else None
    if not isinstance(entries, list):
        raise ValueError("catalog 的 index.json 里没有 skills 数组")
    items = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("name"):
            continue
        name = str(e["name"])
        files = [str(f) for f in (e.get("files") or []) if isinstance(f, str)]
        if not files:
            files = ["SKILL.md"]
        entry = next((f for f in files
                      if os.path.basename(f).lower() in ("skill.md", "skill.markdown")), "")
        if not entry:
            cand = name + ".md"
            if cand in files:
                entry = cand
        if not entry:
            continue

        def make_emit(name=name, files=files, entry=entry):
            def emit(dest):
                pairs = []
                for f in files:
                    url = base + urllib.parse.quote(name, safe="") + "/" + urllib.parse.quote(f, safe="/")
                    pairs.append((f, url))
                os.makedirs(dest, exist_ok=True)
                total = [0]
                lock = threading.Lock()

                def one(fu):
                    rel, u = fu
                    data = _http(u)
                    target = "SKILL.md" if rel == entry and not rel.lower().startswith("skill.") else rel
                    out = _safe_join(dest, *target.split("/"))
                    os.makedirs(os.path.dirname(out), exist_ok=True)
                    with open(out, "wb") as fh:
                        fh.write(data)
                    with lock:
                        total[0] += len(data)
                with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                    list(ex.map(one, pairs))
                return len(pairs)
            return emit
        items.append({"id": name, "name": name, "description": "", "files": len(files), "bytes": 0,
                      "dir": "", "skill_md": base + urllib.parse.quote(name, safe="") + "/" + urllib.parse.quote(entry, safe="/"),
                      "_emit": make_emit()})
    _fill_names(items, lambda it: _http(it["skill_md"]))
    return items


# ---------------------------------------------------------------- Claude marketplace

def _resolve_plugin_source(spec, base_owner, base_repo, base_ref):
    """回 `(owner, repo, ref, prefix)` 或 None（跳过）。"""
    if isinstance(spec, str):
        p = spec.strip()
        if p.startswith("./") or (p and not p.startswith("http") and "/" not in p.split(":")[0] and "\\" in p or p.startswith(".")):
            return (base_owner, base_repo, base_ref, p.lstrip("./").strip("/"))
        return None
    if not isinstance(spec, dict):
        return None
    kind = str(spec.get("source") or "")
    if kind == "github":
        r = str(spec.get("repo") or "")
        if "/" in r:
            o, rp = r.split("/", 1)
            return (o, rp, str(spec.get("ref") or "") or _default_branch(o, rp), str(spec.get("path") or "").strip("/"))
    if kind == "git-subdir":
        u = str(spec.get("url") or "").replace("https://github.com/", "").strip("/")
        if "/" in u:
            o, rp = u.split("/", 1)
            rp = re.sub(r"\.git$", "", rp)
            return (o, rp, str(spec.get("ref") or "") or _default_branch(o, rp), str(spec.get("path") or "").strip("/"))
    return None


def _marketplace_items(doc: dict, owner: str, repo: str, ref: str) -> list:
    plugins = doc.get("plugins") if isinstance(doc, dict) else None
    if not isinstance(plugins, list):
        return []
    items, used = [], set()
    for pl in plugins:
        if not isinstance(pl, dict) or not pl.get("name"):
            continue
        pname = str(pl["name"])
        resolved = _resolve_plugin_source(pl.get("source"), owner, repo, ref)
        if not resolved:
            continue
        po, pr, pref, pfx = resolved
        try:
            sub_items = _repo_items(po, pr, pref, pfx)
        except Exception:  # noqa: BLE001
            continue
        for it in sub_items:
            sid = it["id"]
            if sid in used:
                sid = "%s-%s" % (pname, it["id"])
            used.add(sid)
            it["id"] = sid
            if not it.get("description"):
                it["description"] = "插件 %s" % pname
            items.append(it)
    return items


# ---------------------------------------------------------------- 模式识别

def _detect(source: str) -> dict:
    ps = parse_source(source)
    if ps["kind"] == "url":
        url = ps["url"]
        if MD_RE.search(urllib.parse.urlsplit(url).path):
            return {"mode": "file", "url": url}
        # .json（或目录）→ catalog / marketplace
        try:
            doc = _json_url(url if url.lower().endswith(".json") else url.rstrip("/") + "/index.json")
        except Exception:  # noqa: BLE001
            doc = {}
        if isinstance(doc, dict) and isinstance(doc.get("plugins"), list):
            return {"mode": "marketplace-url", "url": url, "doc": doc}
        if isinstance(doc, dict) and isinstance(doc.get("skills"), list):
            base = _catalog_base(source, url)
            return {"mode": "catalog", "base": base, "doc": doc}
        raise ValueError("这个 URL 既不是技能文件，也不是 catalog/index.json 或 marketplace.json")
    owner, repo = ps["owner"], ps["repo"]
    ref = ps["ref"] or _default_branch(owner, repo)
    sub = ps.get("subpath") or ""
    only = ""
    if sub and MD_RE.search(sub):
        only = sub
    if not only:
        # ★ 先直接探 marketplace.json / index.json（不读整棵树 → 大仓库也稳）
        mp = _maybe_file(owner, repo, ref, ".claude-plugin/marketplace.json")
        if mp:
            try:
                doc = json.loads(mp.decode("utf-8", "replace") or "{}")
                if isinstance(doc, dict) and isinstance(doc.get("plugins"), list):
                    return {"mode": "marketplace", "owner": owner, "repo": repo,
                            "ref": ref, "doc": doc}
            except Exception:  # noqa: BLE001
                pass
        idx = _maybe_file(owner, repo, ref, "index.json")
        if idx:
            try:
                doc = json.loads(idx.decode("utf-8", "replace") or "{}")
                if isinstance(doc, dict) and isinstance(doc.get("skills"), list):
                    return {"mode": "catalog-gh", "doc": doc, "owner": owner, "repo": repo, "ref": ref}
            except Exception:  # noqa: BLE001
                pass
    return {"mode": "repo", "owner": owner, "repo": repo, "ref": ref, "subpath": sub, "only": only}


def _items_for(det: dict) -> list:
    mode = det["mode"]
    if mode == "repo":
        return _repo_items(det["owner"], det["repo"], det["ref"], det["subpath"])
    if mode == "marketplace":
        return _marketplace_items(det["doc"], det["owner"], det["repo"], det["ref"])
    if mode == "catalog":
        return _catalog_items(det["base"], det["doc"])
    if mode == "catalog-gh":
        return _catalog_items_gh(det["doc"], det["owner"], det["repo"], det["ref"])
    if mode == "marketplace-url":
        return _marketplace_items_from_doc(det["doc"], det["url"])
    return []


def _catalog_items_gh(index, owner, repo, ref):
    items = []
    for e in (index.get("skills") or []):
        if not isinstance(e, dict) or not e.get("name"):
            continue
        name = str(e["name"])
        files = [str(f) for f in (e.get("files") or []) if isinstance(f, str)] or ["SKILL.md"]
        entry = next((f for f in files if os.path.basename(f).lower() in ("skill.md", "skill.markdown")), "")
        if not entry and (name + ".md") in files:
            entry = name + ".md"
        if not entry:
            continue

        def make_emit(name=name, files=files, entry=entry):
            def emit(dest):
                pairs = [("%s/%s" % (name, f), "%s/%s" % (name, f)) for f in files]
                os.makedirs(dest, exist_ok=True)
                _emit_parallel(dest, pairs, lambda remote: _fetch_file(owner, repo, ref, remote))
                # 入口文件若是 <name>.md，另存一份 SKILL.md（各 agent 通用识别）
                if entry != "SKILL.md":
                    src = os.path.join(dest, *entry.split("/"))
                    if os.path.isfile(src):
                        with open(os.path.join(dest, "SKILL.md"), "w", encoding="utf-8", newline="") as fh:
                            fh.write(io.open(src, encoding="utf-8", errors="replace").read())
                return len(files)
            return emit
        items.append({"id": name, "name": name, "description": "", "files": len(files), "bytes": 0,
                      "dir": "", "skill_md": "%s/%s" % (name, entry), "_emit": make_emit()})
    _fill_names(items, lambda it: _fetch_file(owner, repo, ref, it["skill_md"]))
    return items


def _marketplace_items_from_doc(doc, url):
    # 仅支持相对路径插件（同目录树）。github / git-subdir 需要仓库，走 _marketplace_items。
    base = url[: url.rfind("/") + 1]
    items = []
    for pl in (doc.get("plugins") or []):
        if not isinstance(pl, dict) or not pl.get("name"):
            continue
        src = pl.get("source")
        rel = src if isinstance(src, str) and (src.startswith("./") or src.startswith(".")) else ""
        if not rel:
            continue
        prefix = base + rel.lstrip("./").strip("/") + "/"
        # 直接列 SKILL.md：需要目录列表，HTTP 没有 → 只能靠 index（略）
        items.append({"id": str(pl["name"]), "name": str(pl["name"]), "description": "",
                      "files": 0, "bytes": 0, "dir": "", "skill_md": prefix + "SKILL.md",
                      "_emit": _http_dir_emit(prefix)})
    return items


def _http_dir_emit(prefix):
    def emit(dest):
        data = _http(prefix + "SKILL.md")
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "SKILL.md"), "wb") as fh:
            fh.write(data)
        return 1
    return emit


# ---------------------------------------------------------------- 来源清单 / 卸载 / 更新

def _ancestors(d: str) -> list:
    d = str(d or "").strip()
    if not d:
        return []
    try:
        d = os.path.abspath(d)
    except Exception:  # noqa: BLE001
        return []
    chain, cur = [], d
    while True:
        chain.append(cur)
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            break
        cur = parent
    chain.reverse()
    return chain


def _skill_roots(directory: str = "") -> list:
    """允许读写/删除的技能根目录（全局 + 项目逐级）。"""
    roots = []
    for h in _home_dirs():
        for sub in ((".agents", "skills"), (".config", "opencode", "skills"),
                    (".claude", "skills"), (".codex", "skills")):
            roots.append(os.path.join(h, *sub))
    for d in _ancestors(directory):
        for sub in ((".agents", "skills"), (".opencode", "skills"),
                    (".claude", "skills"), (".codex", "skills")):
            roots.append(os.path.join(d, *sub))
    return [os.path.abspath(r) for r in roots]


def _write_manifest(dest: str, source: str, sid: str) -> None:
    try:
        with open(os.path.join(dest, MANIFEST), "w", encoding="utf-8") as fh:
            json.dump({"source": source, "id": sid, "installedAt": int(time.time())},
                      fh, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        pass


def _read_manifest(skill_dir: str) -> dict:
    try:
        with open(os.path.join(skill_dir, MANIFEST), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def uninstall(path: str, directory: str = "") -> dict:
    """卸载一个技能：删掉它的目录（只允许在已知技能根目录下、且目录名合法）。"""
    p = os.path.abspath(str(path or ""))
    if os.path.isfile(p):
        p = os.path.dirname(p)
    if not p or not os.path.isdir(p):
        raise ValueError("找不到这个技能目录：%s" % path)
    parent = os.path.normcase(os.path.dirname(p))
    if parent not in {os.path.normcase(r) for r in _skill_roots(directory)}:
        raise ValueError("只能卸载技能目录（`.agents/.opencode/.claude/.codex/skills` 下）")
    name = os.path.basename(p)
    if not ID_RE.match(name):
        raise ValueError("技能目录名不合法：%s" % name)
    shutil.rmtree(p)
    return {"ok": True, "removed": p, "id": name}


def update(path: str, directory: str = "") -> dict:
    """更新一个技能：按安装时记下的来源重新下载覆盖。"""
    p = os.path.abspath(str(path or ""))
    if os.path.isfile(p):
        p = os.path.dirname(p)
    if not os.path.isdir(p):
        raise ValueError("找不到这个技能目录：%s" % path)
    man = _read_manifest(p)
    src = str(man.get("source") or "")
    if not src:
        raise ValueError("这个技能没有「安装来源」记录（不是从面板装的）→ 请用「安装」重新指定来源")
    return install(src, ids=[man.get("id") or os.path.basename(p)],
                   root=os.path.dirname(p), force=True)


# ---------------------------------------------------------------- 目标目录

def target_root(target: str, directory: str = "") -> str:
    target = str(target or "global-agents")
    home = (_home_dirs() or [os.path.expanduser("~")])[0]
    if target == "global-agents":
        return os.path.join(home, ".agents", "skills")
    if target == "global-opencode":
        return os.path.join(home, ".config", "opencode", "skills")
    if target == "global-claude":
        return os.path.join(home, ".claude", "skills")
    if target == "global-codex":
        return os.path.join(home, ".codex", "skills")
    if target == "project-agents":
        if not directory:
            raise ValueError("装到项目需要先选一个会话（要知道项目目录）")
        return os.path.join(directory, ".agents", "skills")
    if target == "project-opencode":
        if not directory:
            raise ValueError("装到项目需要先选一个会话（要知道项目目录）")
        return os.path.join(directory, ".opencode", "skills")
    if target == "project-codex":
        if not directory:
            raise ValueError("装到项目需要先选一个会话（要知道项目目录）")
        return os.path.join(directory, ".codex", "skills")
    raise ValueError("未知安装位置：%s" % target)


# ---------------------------------------------------------------- 预览 / 安装

def _public(items):
    return [{"id": it["id"], "name": it.get("name") or it["id"],
             "description": it.get("description") or "", "dir": it.get("dir", ""),
             "files": it.get("files", 0), "bytes": it.get("bytes", 0)} for it in items]


def preview(source: str) -> dict:
    src = str(source or "").strip()
    det = _detect(src)
    items = _items_for(det)
    if det["mode"] == "repo" and not items:
        raise ValueError("这个来源里没找到 SKILL.md")
    label = det.get("url") or ("%s/%s@%s" % (det.get("owner", ""), det.get("repo", ""), det.get("ref", "")))
    return {"ok": True, "source": src, "label": label, "kind": det["mode"], "skills": _public(items)}


def install(source: str, ids=None, target: str = "global-agents",
            directory: str = "", force: bool = False, root: str = None) -> dict:
    root = root or target_root(target, directory)
    det = _detect(source)
    items = _items_for(det)
    if not items:
        raise ValueError("这个来源里没找到可安装的技能")
    want = [str(x) for x in (ids or []) if str(x)]
    by_id = {it["id"]: it for it in items}
    if want:
        missing = [i for i in want if i not in by_id]
        if missing:
            raise ValueError("来源里没有这些技能：%s" % ", ".join(missing))
        items = [by_id[i] for i in want]

    installed, skipped, errors = [], [], []
    for it in items:
        sid = it["id"]
        if not ID_RE.match(sid):
            errors.append({"id": sid, "error": "技能 id 不合法（只允许字母数字 . _ -）"})
            continue
        dest = os.path.join(root, sid)
        if os.path.exists(dest) and not force:
            skipped.append({"id": sid, "reason": "已存在（勾选「覆盖」可重装）"})
            continue
        try:
            n = it["_emit"](dest)
            _write_manifest(dest, source, sid)
            installed.append({"id": sid, "path": dest, "files": n})
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": sid, "error": "%s: %s" % (type(exc).__name__, exc)})
    return {"ok": True, "root": root, "installed": installed,
            "skipped": skipped, "errors": errors}
