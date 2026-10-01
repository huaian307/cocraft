#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""Fast project-local grep for cocraft.

Why: the generic recursive search walks runtime/browser-profile (thousands of
Edge cache files) and runtime/venvs, so it times out. This tool skips those by
default and only reads text files, so it is fast and UTF-8 safe.

Usage (run from the project root):
    python tools\grep.py PATTERN [PATH]
        [--include "*.py,*.js"]   only these globs (comma separated)
        [--exclude-dir "a,b"]     extra dir names to skip
        [--no-skip]               do not skip the default big dirs
        [-i]                      case-insensitive
        [-F]                      literal match (default is regex)
        [-l]                      files only
        [--max N]                 stop after N matches (default 300)
        [--hidden]                include dotfiles
        [--out FILE]              also write results to FILE (UTF-8)

Exit code: 0 if any match, 1 if none, 2 on error.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Directories that hold huge/generated data or dependencies; skipped by default.
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "venvs",
    "browser-profile", "dist", "build", ".next", ".cache", ".mypy_cache",
    ".pytest_cache", ".ruff_cache",
}
MAX_FILE_BYTES = 3 * 1024 * 1024     # skip files larger than 3 MB
MAX_LINE_CHARS = 400


def is_binary(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(1024)
    except OSError:
        return True


def walk(base: str, skip: set[str], include_hidden: bool):
    for cur, dirs, files in os.walk(base):
        if not include_hidden:
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            files = [f for f in files if not f.startswith(".")]
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            yield os.path.join(cur, name)


def rel(path: str) -> str:
    try:
        return os.path.relpath(path, ROOT)
    except ValueError:
        return path


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("pattern")
    ap.add_argument("path", nargs="?", default=ROOT)
    ap.add_argument("--include", default="")
    ap.add_argument("--exclude-dir", default="")
    ap.add_argument("--no-skip", action="store_true")
    ap.add_argument("-i", "--ignore-case", action="store_true")
    ap.add_argument("-F", "--literal", action="store_true")
    ap.add_argument("-l", "--files-only", action="store_true")
    ap.add_argument("--max", type=int, default=300)
    ap.add_argument("--hidden", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    flags = re.IGNORECASE if args.ignore_case else 0
    if args.literal:
        pat = re.compile(re.escape(args.pattern), flags)
    else:
        try:
            pat = re.compile(args.pattern, flags)
        except re.error as exc:
            print("BAD regex: %s" % exc)
            return 2

    includes = [g.strip() for g in args.include.split(",") if g.strip()]
    skip = set() if args.no_skip else set(SKIP_DIRS)
    skip |= {d.strip() for d in args.exclude_dir.split(",") if d.strip()}

    base = args.path if os.path.isabs(args.path) else os.path.join(ROOT, args.path)
    if os.path.isfile(base):
        candidates = [base]
    else:
        candidates = walk(base, skip, args.hidden)

    t0 = time.time()
    hits = []
    matched_files = set()
    files_scanned = 0
    truncated = False

    for path in candidates:
        if includes and not any(fnmatch.fnmatch(os.path.basename(path), g) for g in includes):
            continue
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        if is_binary(path):
            continue
        files_scanned += 1
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for n, line in enumerate(fh, 1):
                    if pat.search(line):
                        matched_files.add(path)
                        if not args.files_only:
                            s = line.rstrip("\n")
                            if len(s) > MAX_LINE_CHARS:
                                s = s[:MAX_LINE_CHARS] + " ..."
                            hits.append("%s:%d: %s" % (rel(path), n, s))
                        if len(hits) >= args.max or (args.files_only and len(matched_files) >= args.max):
                            truncated = True
                            break
        except OSError:
            continue
        if truncated:
            break

    if args.files_only:
        body = [rel(p) for p in sorted(matched_files)]
    else:
        body = hits
    text = "\n".join(body)

    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(text + ("\n" if text else ""))
        except OSError as exc:
            print("cannot write --out: %s" % exc)

    if text:
        print(text)
    summary = "[grep] files scanned=%d  matches=%d  %.2fs%s" % (
        files_scanned, len(body), time.time() - t0, "  (truncated)" if truncated else "")
    print(summary)
    return 0 if body else 1


if __name__ == "__main__":
    raise SystemExit(main())
