# -*- coding: utf-8 -*-
r"""构建依赖锁回归（`packaging/deps.lock.json` + `packaging/build.py`）。

覆盖：
  * 锁文件 schema（python / node / npm / codex / components）；
  * 版本补全（`opencode-ai` → `opencode-ai@1.19.0`；已带版本不重复加）；
  * 嵌入式 Python / node.exe 的 SHA256 与本机缓存一致（有文件时）；
  * 组件 venv 的版本从 `*.dist-info` 读得到、且与锁一致；
  * codex 依赖版本与锁一致（本机有 node_modules 时）；
  * `build.py --check-lock` 返回 0；
  * 版本漂移会被拦下。

不联网、不打包；CI 上缺素材的用例自动 SKIP。
"""
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD_PY = os.path.join(ROOT, "packaging", "build.py")
LOCK = os.path.join(ROOT, "packaging", "deps.lock.json")

N_PASS = 0
N_FAIL = 0
N_SKIP = 0


def check(name, cond, extra=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("PASS  %s" % name)
    else:
        N_FAIL += 1
        print("FAIL  %s%s" % (name, ("  | " + str(extra)) if extra else ""))


def skip(name, why):
    global N_SKIP
    N_SKIP += 1
    print("SKIP  %s（%s）" % (name, why))


def load_build():
    spec = importlib.util.spec_from_file_location("cocraft_build", BUILD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    check("deps.lock.json 存在", os.path.isfile(LOCK))
    with io.open(LOCK, encoding="utf-8") as fh:
        lock = json.load(fh)
    for key in ("version", "python", "node", "npm", "codex_deps", "components"):
        check("锁里有 %s" % key, key in lock, list(lock.keys()))
    check("python 有 sha256", bool((lock.get("python") or {}).get("sha256")))
    check("node 有 sha256_node_exe", bool((lock.get("node") or {}).get("sha256_node_exe")))
    check("node 有 npm_version", bool((lock.get("node") or {}).get("npm_version")))
    for pkg in ("@agentclientprotocol/codex-acp", "@openai/codex",
                "@zed-industries/claude-code-acp", "opencode-ai"):
        check("npm 锁定 %s" % pkg, bool((lock.get("npm") or {}).get(pkg)))
    for comp in ("terminal", "panel", "music"):
        check("组件锁定 %s" % comp, bool((lock.get("components") or {}).get(comp)))

    b = load_build()

    # ---------- 版本补全 ----------
    check("_split_pkg 带版本", b._split_pkg("@scope/name@1.2.3") == ("@scope/name", "1.2.3"))
    check("_split_pkg 无版本", b._split_pkg("@scope/name") == ("@scope/name", ""))
    check("_pinned_pkg 自动补版本",
          b._pinned_pkg("opencode-ai") == "opencode-ai@" + lock["npm"]["opencode-ai"])
    check("_pinned_pkg 已带版本不重复",
          b._pinned_pkg("opencode-ai@9.9.9") == "opencode-ai@9.9.9")
    check("_pinned_pkg scope 包",
          b._pinned_pkg("@zed-industries/claude-code-acp") ==
          "@zed-industries/claude-code-acp@" + lock["npm"]["@zed-industries/claude-code-acp"])

    # ---------- 路径发现 ----------
    check("NODE_VER 与锁一致", b.NODE_VER == str(lock["node"]["version"]),
          (b.NODE_VER, lock["node"]["version"]))
    check("PY_VER 与锁一致", b.PY_VER == str(lock["python"]["version"]),
          (b.PY_VER, lock["python"]["version"]))
    check("支持 COCRAFT_NODE_SRC", "COCRAFT_NODE_SRC" in io.open(BUILD_PY, encoding="utf-8").read())
    check("支持 COCRAFT_CODEX_NM", "COCRAFT_CODEX_NM" in io.open(BUILD_PY, encoding="utf-8").read())

    # ---------- SHA256（本机有文件时） ----------
    py_zip = os.path.join(ROOT, "dist", "cache",
                          "python-%s-embed-amd64.zip" % lock["python"]["version"])
    if os.path.isfile(py_zip):
        check("Python embeddable SHA256",
              b._sha256_file(py_zip).lower() == lock["python"]["sha256"].lower())
    else:
        skip("Python embeddable SHA256", "dist/cache 里还没有 zip")
    node_exe = os.path.join(b.NODE_SRC, "node.exe") if b.NODE_SRC else ""
    if node_exe and os.path.isfile(node_exe):
        check("node.exe SHA256",
              b._sha256_file(node_exe).lower() == lock["node"]["sha256_node_exe"].lower())
    else:
        skip("node.exe SHA256", "没找到 COCRAFT_NODE_SRC")

    # ---------- 打包必须带 npm（否则安装版面板更新不可用） ----------
    tmp = tempfile.mkdtemp(prefix="cocraft-node-")
    try:
        nd = os.path.join(tmp, "node")
        os.makedirs(nd)
        for f in ("node.exe", "LICENSE", "README.md"):
            with open(os.path.join(nd, f), "w", encoding="utf-8") as fh:
                fh.write("x")
        npm = os.path.join(nd, "node_modules", "npm")
        os.makedirs(os.path.join(npm, "bin"))
        with open(os.path.join(npm, "package.json"), "w", encoding="utf-8") as fh:
            json.dump({"name": "npm", "version": lock["node"].get("npm_version") or "0"}, fh)
        with open(os.path.join(npm, "bin", "npm-cli.js"), "w", encoding="utf-8") as fh:
            fh.write("// npm")
        old = (b.NODE_SRC, b.NODE_SHA256, b.NODE_VER, b.NPM_VER, b.PAYLOAD)
        b.NODE_SRC, b.NODE_SHA256, b.NODE_VER = nd, "", str(lock["node"]["version"])
        b.NPM_VER = str(lock["node"].get("npm_version") or "")
        b.PAYLOAD = os.path.join(tmp, "payload")
        try:
            b._stage_node()
            check("打包带 npm-cli.js",
                  os.path.isfile(os.path.join(b.PAYLOAD, "agents", "node",
                                              "node_modules", "npm", "bin", "npm-cli.js")))
        finally:
            b.NODE_SRC, b.NODE_SHA256, b.NODE_VER, b.NPM_VER, b.PAYLOAD = old
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 组件版本 ----------
    site = os.path.join(ROOT, "runtime", "venvs", "terminal", "Lib", "site-packages")
    if os.path.isdir(site):
        vers = b._site_versions(site)
        check("terminal 能读出版本", "pywinpty" in vers, vers)
        try:
            b._check_component_versions("terminal", site)
            check("terminal 版本与锁一致", True)
        except SystemExit as exc:
            check("terminal 版本与锁一致", False, exc)
        # 漂移必须被拦下
        old = b.LOCK["components"]["terminal"]
        b.LOCK["components"]["terminal"] = ["pywinpty==0.0.1"]
        try:
            b._check_component_versions("terminal", site)
            check("版本漂移会拦下", False)
        except SystemExit:
            check("版本漂移会拦下", True)
        finally:
            b.LOCK["components"]["terminal"] = old
    else:
        skip("terminal 版本", "runtime/venvs/terminal 不存在")

    # ---------- codex 依赖 ----------
    if b.CODEX_NM and os.path.isdir(b.CODEX_NM):
        try:
            b._check_codex_versions()
            check("codex 依赖版本与锁一致", True)
        except SystemExit as exc:
            check("codex 依赖版本与锁一致", False, exc)
    else:
        skip("codex 依赖版本", "没找到 COCRAFT_CODEX_NM")

    # ---------- --check-lock ----------
    check("check_lock() 返回 0", b.check_lock(False) == 0)
    check("build.py 有 --check-lock", "--check-lock" in io.open(BUILD_PY, encoding="utf-8").read())
    check("BUILD.md 存在", os.path.isfile(os.path.join(ROOT, "docs", "BUILD.md")))

    print("\n%d passed / %d failed / %d skipped" % (N_PASS, N_FAIL, N_SKIP))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
