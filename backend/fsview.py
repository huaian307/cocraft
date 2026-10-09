# -*- coding: utf-8 -*-
r"""编码模式的后端：列目录 / 读文件 / 写文件（**引擎无关**，纯标准库）。

为什么不用上游 OpenCode 的 `/api/fs/*`：那套只在引擎 = opencode 时存在；本项目要能脱离 OpenCode
单跑（ACP 模式），所以自己实现。

安全：
  * 所有操作都必须落在调用方给的 `root` 目录内（`realpath` 前缀校验），`..\` / 符号链接都逃不出去；
  * 读 / 写都有大小上限；
  * 列目录时跳过一批“又大又没用”的目录（.git / node_modules / runtime …），保持文件树好用。
"""

from __future__ import annotations

import os

MAX_READ = 2 * 1024 * 1024          # 2 MB
MAX_WRITE = 4 * 1024 * 1024         # 4 MB

# 文件树里默认不展开的“重”目录（VS Code 默认也隐藏这些）
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "runtime", "dist", "build", ".idea", ".vscode", "browser-profile", ".mypy_cache",
    ".pytest_cache", "target", "bin", "obj",
}


def _real(p: str) -> str:
    return os.path.realpath(p)


def resolve(root: str, path: str):
    """把 (root, path) 归一化；越界抛 ValueError。返回 (root_real, target_real)。"""
    if not root or not os.path.isdir(root):
        raise ValueError("根目录不存在：%s" % (root or "(空)"))
    r = _real(root)
    target = path or r
    if not os.path.isabs(target):
        target = os.path.join(r, target)
    t = _real(target)
    if t != r and not t.startswith(r + os.sep):
        raise ValueError("越界：%s 不在 %s 内" % (path, root))
    return r, t


_BAD_NAME_CHARS = set('\\/:*?"<>|')


def make_project(base: str, name: str) -> dict:
    """在 `base` 下创建一个**真实、长期存在**的项目目录。

    新会话的「新建项目」用它（不是写运行态/缓存，而是在磁盘上真建一个可复用的目录）。
    安全：`base` 必须是已存在的目录，`name` 必须是**单个**安全路径段（不能有分隔符 / `..` /
    Windows 非法字符），拼出来的目标父目录必须仍是 `base`（`realpath` 前缀校验防越界）。
    已存在且是目录 → 幂等返回（`existed=True`）；同名是文件 → 报错。
    """
    if not base or not os.path.isdir(base):
        raise ValueError("项目位置不存在：%s" % (base or "(空)"))
    b = _real(base)
    nm = str(name or "").strip()
    if not nm:
        raise ValueError("项目名不能为空")
    if nm in (".", "..") or any(c in _BAD_NAME_CHARS for c in nm) \
            or any(ord(c) < 32 for c in nm):
        raise ValueError('项目名不能包含 \\ / : * ? " < > | 等字符')
    nm = nm.rstrip(" .")                       # Windows 不允许以空格 / 点结尾
    if not nm:
        raise ValueError("项目名不合法")
    if len(nm) > 80:
        raise ValueError("项目名太长（>80）")
    target = os.path.join(b, nm)
    if _real(os.path.dirname(target)) != b:    # 双保险：拼出来必须还在 base 下
        raise ValueError("越界")
    if os.path.exists(target):
        if not os.path.isdir(target):
            raise ValueError("同名文件已存在：%s" % target)
        return {"ok": True, "path": target, "existed": True}
    try:
        os.makedirs(target)
    except OSError as exc:
        raise ValueError("创建目录失败：%s" % exc)
    return {"ok": True, "path": target, "existed": False}


def list_dir(root: str, path: str) -> dict:
    r, t = resolve(root, path)
    if not os.path.isdir(t):
        raise ValueError("不是目录：%s" % path)
    try:
        names = os.listdir(t)
    except OSError as exc:
        raise ValueError("读目录失败：%s" % exc)
    items = []
    for name in names:
        full = os.path.join(t, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        isdir = os.path.isdir(full)
        if isdir and name in SKIP_DIRS:
            continue
        items.append({
            "name": name,
            "path": full,
            "dir": isdir,
            "size": 0 if isdir else st.st_size,
            "mtime": int(st.st_mtime * 1000),
        })
    items.sort(key=lambda x: (not x["dir"], x["name"].lower()))
    return {"root": r, "path": t,
            "parent": (os.path.dirname(t) if t != r else ""),
            "items": items}


def read_file(root: str, path: str) -> dict:
    r, t = resolve(root, path)
    if not os.path.isfile(t):
        raise ValueError("不是文件：%s" % path)
    size = os.path.getsize(t)
    if size > MAX_READ:
        raise ValueError("文件太大（%.1f MB > %.0f MB）" % (size / 1048576.0, MAX_READ / 1048576.0))
    with open(t, "rb") as fh:
        raw = fh.read()
    binary = b"\x00" in raw[:8192]
    text = "" if binary else raw.decode("utf-8", "replace")
    return {"root": r, "path": t, "size": size, "binary": binary, "text": text,
            "mtime": int(os.path.getmtime(t) * 1000)}


def write_file(root: str, path: str, text: str) -> dict:
    r, t = resolve(root, path)
    if os.path.isdir(t):
        raise ValueError("目标是目录：%s" % path)
    data = (text if text is not None else "").encode("utf-8")
    if len(data) > MAX_WRITE:
        raise ValueError("内容太大（>%.0f MB）" % (MAX_WRITE / 1048576.0))
    with open(t, "wb") as fh:
        fh.write(data)
    return {"root": r, "path": t, "size": len(data), "mtime": int(os.path.getmtime(t) * 1000)}
