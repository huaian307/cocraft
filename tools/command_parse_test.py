# -*- coding: utf-8 -*-
r"""删除目标解析回归（`backend/snapshots.guess_targets` + `backend/file_rank.triage`）。

为什么单独一套：我们的核心承诺是"**删除前先复制**"。如果 Windows 上常见的
PowerShell / CMD / Python / Node / .NET 删除写法解析不出目标，隔离区就是空的，
用户第一次试用就会失去信任。

覆盖：
  * `Remove-Item` / `del` / `rd` / `rmdir` / `rm -rf`（含 `cmd /c`、`powershell -Command` 套一层）；
  * `shutil.rmtree` / `os.remove` / `os.unlink` / `Path(...).unlink()`；
  * `fs.rmSync` / `fs.unlinkSync` / `require('fs').rmSync`；
  * `[System.IO.File]::Delete` / `[IO.Directory]::Delete`；
  * 变量 / 拼接 / 范围不定 → 必须进 `unresolved`（fail-closed）；
  * 复合命令里夹关机/杀进程 → 不自动放行。

不联网、只用临时目录。
"""
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import file_rank as fr          # noqa: E402
import snapshots as S          # noqa: E402

N_PASS = 0
N_FAIL = 0


def check(name, cond, extra=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("PASS  %s" % name)
    else:
        N_FAIL += 1
        print("FAIL  %s%s" % (name, ("  | " + str(extra)) if extra else ""))


def touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def names(targets):
    return sorted(os.path.basename(str(p).rstrip("\\/")) for p in (targets or []))


def main():
    ws = tempfile.mkdtemp(prefix="cocraft-parse-")
    try:
        for d in ("dist", "build", "node_modules", "sub", "src"):
            os.makedirs(os.path.join(ws, d), exist_ok=True)
        touch(os.path.join(ws, "dist", "bundle.js"))
        touch(os.path.join(ws, "build", "out.bin"))
        touch(os.path.join(ws, "node_modules", "pkg", "index.js"))
        touch(os.path.join(ws, "sub", "data.txt"))
        touch(os.path.join(ws, "src", "app.py"))
        touch(os.path.join(ws, "a.log"))
        touch(os.path.join(ws, "b.log"))
        touch(os.path.join(ws, "keep.py"))

        # ---------- 常见 Windows / 跨语言删除写法：目标必须解析出来 ----------
        cases = [
            (r"Remove-Item -Recurse -Force .\dist", ["dist"]),
            (r'Remove-Item -Path "dist" -Recurse -Force', ["dist"]),
            (r"Remove-Item -LiteralPath 'build' -Recurse", ["build"]),
            (r"del /s /q build", ["build"]),
            (r"del /f /q *.log", ["a.log", "b.log"]),
            (r"rd /s /q node_modules", ["node_modules"]),
            (r"rmdir /s /q node_modules", ["node_modules"]),
            (r"rm -rf ./dist", ["dist"]),
            (r'cmd /c "rmdir /s /q build"', ["build"]),
            (r'powershell -NoProfile -Command "Remove-Item -Recurse -Force dist"', ["dist"]),
            (r'''python -c "import shutil; shutil.rmtree('dist')"''', ["dist"]),
            (r'''python -c "import os; os.remove('a.log')"''', ["a.log"]),
            (r'''python -c "import os; os.unlink('a.log')"''', ["a.log"]),
            (r'''node -e "require('fs').rmSync('dist',{recursive:true})"''', ["dist"]),
            (r'''node -e "fs.unlinkSync('a.log')"''', ["a.log"]),
            (r"[System.IO.File]::Delete('a.log')", ["a.log"]),
            (r"[IO.Directory]::Delete('build', $true)", ["build"]),
            (r'''from pathlib import Path; Path('a.log').unlink()''', ["a.log"]),
        ]
        for cmd, want in cases:
            g = S.guess_targets(cmd, ws)
            got = names(g.get("targets"))
            check("解析: %s" % cmd[:58], got == sorted(want), "got=%r unresolved=%r" % (got, g.get("unresolved")))

        # ---------- 变量 / 拼接 / 不存在 → unresolved（不许猜） ----------
        for cmd in [r'''python -c "import shutil; shutil.rmtree(name)"''',
                    r"rm -rf $HOME/cocraft-nope",
                    r"shutil.rmtree(os.path.join(root, 'x'))"]:
            g = S.guess_targets(cmd, ws)
            check("不确定: %s" % cmd[:50], (not g.get("targets")) or bool(g.get("unresolved")),
                  "targets=%r unresolved=%r" % (g.get("targets"), g.get("unresolved")))

        # ---------- triage：安全 / 不安全 ----------
        safe = [
            r"Remove-Item -Recurse -Force .\dist",
            r"del /f /q *.log",
            r"rd /s /q node_modules",
            r'''python -c "import shutil; shutil.rmtree('dist')"''',
            r'''node -e "require('fs').rmSync('dist',{recursive:true})"''',
            r"[System.IO.File]::Delete('a.log')",
            r"[IO.Directory]::Delete('build', $true)",
            r'''from pathlib import Path; Path('a.log').unlink()''',
        ]
        for cmd in safe:
            tr = fr.triage(ws, cmd, max_star=1)
            check("自动放行: %s" % cmd[:56], bool(tr.get("ok")), tr.get("reason"))

        unsafe = [
            r'''python -c "import shutil; shutil.rmtree('keep.py')"''',
            r"Remove-Item -Recurse -Force .\src",
            r"rm -rf sub",
            r'''python -c "import shutil; shutil.rmtree(name)"''',
            r"rm -rf $HOME/cocraft-nope",
            r"rm -rf dist && shutdown /s",
            r"del /f /q *.log && taskkill /F /IM node.exe",
        ]
        for cmd in unsafe:
            tr = fr.triage(ws, cmd, max_star=1)
            check("拦下: %s" % cmd[:56], not tr.get("ok"), tr.get("reason"))

        # ---------- 显式确认：代码删除也进了 DELETE_RE ----------
        for cmd in [r'''shutil.rmtree('dist')''', r'''fs.rmSync('dist')''',
                    r"[IO.File]::Delete('a.log')", r'''Path('a.log').unlink()''']:
            check("DELETE_RE 命中: %s" % cmd[:44], bool(fr.DELETE_RE.search(cmd)))
    finally:
        shutil.rmtree(ws, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
