# -*- coding: utf-8 -*-
r"""安装后自检：检查 cocraft 这套安装到底缺什么（纯标准库，装完可直接跑）。

用法：
    <安装目录>\python\python.exe <安装目录>\tools\selftest.py --app-dir <安装目录>
    # 不带参数也能跑：默认按本文件所在位置推断安装目录

检查项：
  1. 核心文件（backend / frontend / tools / launchers）
  2. 解释器（嵌入式 python / pythonw）
  3. 可选组件（音乐服务 / 音频频谱）装没装 —— 没装只提示，不算失败
  4. ACP agent：随包 agent 文件在不在；adapter 能不能被 node 跑起来（只做 --version，不烧 token）
  5. 运行时状态（agent 注册表 / 引擎）与端口是否被占用
  6. 浏览器（Edge / Chrome）是否能找到

输出纯 ASCII（Windows 控制台是 GBK）。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys

# 证书兜底：安装版的嵌入式 Python 缺 CA 根证书，否则下面实探 api.deepseek.com 会
# CERTIFICATE_VERIFY_FAILED（看起来像"网络不通"，其实是证书问题）。
_SELF_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(_SELF_DIR), "backend"))
try:
    import netsafe
    netsafe.install()
except Exception:  # noqa: BLE001
    netsafe = None

FAILS: list = []
WARNS: list = []
PASSES = 0


def check(name: str, ok: bool, extra: str = "", warn_only: bool = False) -> None:
    global PASSES
    tag = "OK  "
    if ok:
        PASSES += 1
    elif warn_only:
        tag = "WARN"
        WARNS.append(name)
    else:
        tag = "FAIL"
        FAILS.append(name)
    safe = str(extra)[:160].encode("ascii", "backslashreplace").decode("ascii")
    print("  %s %s %s" % (tag, name, safe))


def port_busy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.3):
            return True
    except OSError:
        return False


def _check_model_key(app: str, base_env: dict) -> None:
    """有 DEEPSEEK_API_KEY 就实探一下 api.deepseek.com。

    这一步把三种情况一眼分开：Key 有效 / Key 无效(401) / 连不上网(超时)。装了却"答不上来"时先看这里。
    """
    import urllib.error
    import urllib.request
    key = next((str(base_env[k]) for k in base_env if str(k).upper() == "DEEPSEEK_API_KEY"), "")
    if not key:
        return
    req = urllib.request.Request("https://api.deepseek.com/v1/models",
                                 headers={"Authorization": "Bearer " + key})
    _open = netsafe.urlopen if netsafe else urllib.request.urlopen
    try:
        with _open(req, timeout=15) as r:
            body = r.read(8000).decode("utf-8", "replace")
        has_flash = "deepseek-flash" in body
        check("DeepSeek Key 有效 + 网络可达", True,
              "已看到 deepseek-flash" if has_flash else "Key 可用，但列表里没有 deepseek-flash（模型可能对不上）")
    except urllib.error.HTTPError as e:  # noqa: BLE001
        check("DeepSeek Key 有效性", False,
              "HTTP %s —— 多半 Key 无效 / 不是 DeepSeek 官方 Key（中转 Key 常见）" % e.code)
    except Exception as e:  # noqa: BLE001
        check("连 api.deepseek.com", False, "连不上：%s（网络 / 代理 / 防火墙）" % e, warn_only=True)


def _check_codex_runtime(app: str, base_env: dict) -> None:
    """真的用随包 node + codex CLI 跑一句，验证"能不能连上模型"。

    比只看文件/网络更彻底：它把"没 Key / Key 无效 / 网络不通 / codex 起不来"一次性问出来。
    """
    node = os.path.join(app, "agents", "node", "node.exe")
    cjs = os.path.join(app, "agents", "codex", "node_modules", "@openai", "codex", "bin", "codex.js")
    home = os.path.join(app, "agents", "codex", "codex-home")
    if not (os.path.isfile(node) and os.path.isfile(cjs)):
        return
    key = next((str(base_env[k]) for k in base_env if str(k).upper() == "DEEPSEEK_API_KEY"), "")
    if not key:
        check("随包 codex 实跑（有 Key 才测）", True, "没填 Key → 跳过", warn_only=True)
        return
    env = dict(os.environ)
    env["CODEX_HOME"] = home
    env["DEEPSEEK_API_KEY"] = key
    cwd = os.path.join(app, "runtime", "workspace")
    if not os.path.isdir(cwd):
        cwd = app
    try:
        r = subprocess.run([node, cjs, "exec", "reply with exactly: PONG"],
                           capture_output=True, text=True, timeout=90, env=env, cwd=cwd)
        out = ((r.stdout or "") + (r.stderr or "")).replace("\r", "")
        if "PONG" in out:
            check("★随包 codex 能连上模型并回答", True, "回了 PONG")
        elif "Missing environment variable" in out:
            check("★随包 codex 能跑", False, "缺 DEEPSEEK_API_KEY（检查 baseline.env）")
        else:
            line = next((x for x in out.splitlines() if x.strip()), "")
            check("★随包 codex 能跑", False, line[:180], warn_only=True)
    except subprocess.TimeoutExpired:
        check("★随包 codex 能跑", False,
              "90s 超时 —— 多半连不上 api.deepseek.com（网络 / 代理 / 防火墙）", warn_only=True)
    except Exception as exc:  # noqa: BLE001
        check("★随包 codex 能跑", False, repr(exc), warn_only=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    args = ap.parse_args()
    app = os.path.abspath(args.app_dir)
    print("cocraft 自检\n  安装目录: %s" % app.encode("ascii", "backslashreplace").decode("ascii"))

    print("\n[1] 核心文件")
    for rel in ("backend/server.py", "backend/watch.py", "frontend/index.html", "frontend/app.js",
                "tools/music_service.py", "tools/spectrum.py", "tools/pick_image.ps1",
                "launchers/launch_opencode.py"):
        check("有 %s" % rel, os.path.isfile(os.path.join(app, rel)))

    print("\n[2] 解释器")
    py = os.path.join(app, "python", "python.exe")
    pyw = os.path.join(app, "python", "pythonw.exe")
    check("嵌入式 python.exe", os.path.isfile(py))
    check("嵌入式 pythonw.exe（不弹黑框）", os.path.isfile(pyw), warn_only=True)
    if os.path.isfile(py):
        try:
            v = subprocess.run([py, "-c", "import sys;print(sys.version.split()[0])"],
                               capture_output=True, text=True, timeout=30)
            check("解释器能跑", v.returncode == 0 and bool(v.stdout.strip()), (v.stdout or v.stderr).strip()[:40])
        except Exception as exc:  # noqa: BLE001
            check("解释器能跑", False, repr(exc))

    print("\n[3] 可选组件（不装不算失败，面板会自己降级/提示）")
    music = os.path.join(app, "runtime", "site-packages", "music")
    audio = os.path.join(app, "runtime", "site-packages", "audio")
    check("音乐服务 site-packages（可选）", os.path.isdir(music), warn_only=True,
          extra="" if os.path.isdir(music) else "未安装 → 面板显示「音乐服务未安装」")
    check("音频频谱 site-packages（可选）", os.path.isdir(audio), warn_only=True,
          extra="" if os.path.isdir(audio) else "未安装 → 退回 CSS 合成动画")

    print("\n[4] 随包 ACP agent")
    node = os.path.join(app, "agents", "node", "node.exe")
    acp = os.path.join(app, "agents", "codex", "node_modules", "@agentclientprotocol",
                       "codex-acp", "dist", "index.js")
    claude = os.path.join(app, "agents", "claude", "node_modules", "@zed-industries",
                          "claude-code-acp", "dist", "index.js")
    check("node.exe", os.path.isfile(node), warn_only=True)
    check("codex-acp 入口", os.path.isfile(acp), warn_only=True)
    check("claude-code-acp 入口", os.path.isfile(claude), warn_only=True,
          extra="" if os.path.isfile(claude) else "未随包 → 面板里 claude 档会显示缺适配器")
    if os.path.isfile(claude):
        # 有登录态或 API Key 才能真的用起来；这里只提示，不算失败
        home = os.path.expanduser("~")
        auth = (os.path.isdir(os.path.join(home, ".claude"))
                or os.path.isfile(os.path.join(home, ".claude.json"))
                or bool(os.environ.get("ANTHROPIC_API_KEY")))
        check("Claude 登录态（~/.claude 或 ANTHROPIC_API_KEY）", auth, warn_only=True,
              extra="" if auth else "没有 → 首次设置里填 ANTHROPIC_API_KEY，或先在本机登录 Claude Code")
    if os.path.isfile(node) and os.path.isfile(acp):
        try:
            r = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=60)
            check("node 能跑", r.returncode == 0, (r.stdout or r.stderr).strip()[:40])
        except Exception as exc:  # noqa: BLE001
            check("node 能跑", False, repr(exc))
        # ⚠ 它是 ACP server（stdio），启动后会等输入 —— 用 --help 会"挂住"。
        #   所以只判断"能不能拉起来"：超时 = 已经跑起来了（正常）；立刻非零退出才算可疑。
        try:
            r2 = subprocess.run([node, acp], capture_output=True, text=True, timeout=10)
            check("codex-acp 能被拉起（立刻退出）", r2.returncode == 0,
                  ((r2.stdout or "") + (r2.stderr or "")).strip().splitlines()[:1])
        except subprocess.TimeoutExpired:
            check("codex-acp 能被拉起（当作 ACP server 在等输入 = 正常）", True)
        except Exception as exc:  # noqa: BLE001
            check("codex-acp 能被拉起", False, repr(exc), warn_only=True)

    print("\n[5] 运行时状态")
    state = os.path.join(app, "runtime", "state")
    reg = os.path.join(state, "_acp_agents.json")
    check("agent 注册表存在", os.path.isfile(reg), warn_only=True)
    if os.path.isfile(reg):
        try:
            with open(reg, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            ids = [a.get("id") for a in (data.get("agents") or [])]
            base_env = ((data.get("baseline") or {}).get("env") or {})
            check("注册表里有 agent", bool(ids), str(ids))
            # ⚠ 这条以前断言 baseline.env **不能**有 Key（那是"安装包不带密钥"的检查）。
            #   但用户按提示填了 Key 之后，这条会误报失败 —— 现在改成"看有没有 Key"，并顺便验它能不能用。
            keys = sorted(k for k in base_env if k.upper().endswith("API_KEY") and str(base_env[k]).strip())
            check("baseline.env 里有模型 Key（首次设置里填的）", bool(keys),
                  ("已设置：" + ", ".join(keys)) if keys else
                  "没有 → 面板里「首次设置 / 设置 → 模型 API Key」填一个，否则 agent 会报 "
                  "Missing environment variable", warn_only=True)
            _check_model_key(app, base_env)
            _check_codex_runtime(app, base_env)
        except Exception as exc:  # noqa: BLE001
            check("注册表可解析", False, repr(exc))
    # 随包 codex 的干净配置（env_key 指向环境变量）
    cfg = os.path.join(app, "agents", "codex", "codex-home", "config.toml")
    if os.path.isfile(cfg):
        try:
            txt = open(cfg, "r", encoding="utf-8", errors="replace").read()
            has_env_key = ('env_key' in txt and 'DEEPSEEK_API_KEY' in txt)
            check("codex-home/config.toml 指向 DEEPSEEK_API_KEY", has_env_key,
                  "" if has_env_key else "配置里找不到 env_key = DEEPSEEK_API_KEY")
        except Exception as exc:  # noqa: BLE001
            check("codex-home/config.toml 可读", False, repr(exc), warn_only=True)
    else:
        check("codex-home/config.toml（随包 codex 配置）", False,
              "没有 → 首次启动会从 codex-home-template 生成；若装了 codex 却缺它，agent 会退回默认(OpenAI)",
              warn_only=True)
    check("端口 8787 空着（或面板已在跑）", True, warn_only=True,
          extra="注意：8787/8788 已被占用说明面板已在运行" if port_busy(8787) else "空闲")

    print("\n[6] 浏览器（面板窗口用它以 --app 打开）")
    edges = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    check("找到 Edge/Chrome", any(os.path.isfile(p) for p in edges),
          "" if any(os.path.isfile(p) for p in edges) else "会退回默认浏览器打开")

    print("\n合计 %d 项通过，%d 项警告，%d 项失败" % (PASSES, len(WARNS), len(FAILS)))
    if WARNS:
        print("警告（不影响启动）:")
        for w in WARNS:
            print("   -", w)
    if FAILS:
        print("失败（需要修）:")
        for f in FAILS:
            print("   -", f)
        return 1
    print("result: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
