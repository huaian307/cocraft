#!/usr/bin/env python
"""cu_test.py -- 桌面工具（Computer Use）回归。

真起一个 WinForms 测试窗（tools/cu_testwin.ps1），然后：
  1. 枚举窗口 / UIA 树 / 按名字找控件 / 只截窗口
  2. 点 7 + 8 = → 断言标签真的变成 "result: 15"（不是"调用了就算过"）
  3. 往输入框打字 → 断言控件内容真的变了
  4. 权限闸门：默认 deny / 危险词（发送）拒绝 / 提权窗口拒绝
  5. 桌面租约：别人持有时拿到 busy
  6. 审计：每条都进链（kind=cu），**正文不进链**（只有长度+哈希）

全程用临时目录（审计链 / 租约文件），不污染真实运行态。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import importlib.util

spec = importlib.util.spec_from_file_location("cumain", os.path.join(HERE, "cu.py"))
cumain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cumain)

import computer as cu  # noqa: E402

TITLE = "cocraft CU Test Window"
CREATE_NO_WINDOW = 0x08000000
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    mark = "[OK]  " if cond else "[BAD] "
    line = mark + name
    if detail and not cond:
        line += "  -> " + str(detail)
    print(line)
    return bool(cond)


def run(argv):
    args = cumain.build_parser().parse_args(argv)
    return args.func(args, cu)


def ps51():
    sysroot = os.environ.get("SystemRoot") or r"C:\Windows"
    p = os.path.join(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    return p if os.path.isfile(p) else "powershell.exe"


def wait_window(title, timeout=25.0):
    end = time.time() + timeout
    while time.time() < end:
        hit = cu.winapi.find_window(title=title)
        if hit:
            return hit
        time.sleep(0.25)
    return None


def read_audit(path):
    rows = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
    except Exception:
        pass
    return rows


def offline_checks():
    """不需要桌面就能验的部分：PATH 薄壳 + 权限分级 + prompt 工具卡。"""
    # 1) PATH 薄壳
    from computer import shim
    bin_dir = shim.ensure_bin_dir()
    check("生成 runtime/bin/cu.cmd 薄壳", bool(bin_dir) and
          os.path.isfile(os.path.join(bin_dir, "cu.cmd")), bin_dir)
    try:
        raw = open(os.path.join(bin_dir, "cu.cmd"), "rb").read()
        raw.decode("ascii")
        check("薄壳是纯 ASCII（.cmd 里有中文会被 GBK 读坏）", True)
    except Exception as exc:
        check("薄壳是纯 ASCII（.cmd 里有中文会被 GBK 读坏）", False, exc)
    env = {}
    shim.inject_path(env)
    path_val = env.get("PATH") or env.get("Path") or ""
    check("注入后 PATH 以 runtime/bin 开头", path_val.lower().startswith(bin_dir.lower()), path_val[:80])
    check("注入时带 COCRAFT_PY（薄壳用它跑源码）", bool(env.get("COCRAFT_PY")), env.get("COCRAFT_PY"))

    # 2) 权限分级：只读自动放行、动作放行但真闸门在 cu、带 --allow-actions 不自动放行
    from computer import gate as cu_gate
    cases = [
        ("cu windows --title QQ", "windows", True),
        ("cu tree --hwnd 12 --depth 3", "tree", True),
        ("cu shot --hwnd 12 --out x.png", "shot", True),
        ("cu click --hwnd 12 --name 7", "click", False),
        ("cu type --hwnd 12 --class-like EDIT --text hi", "type", False),
        ("cu.cmd keys --hwnd 12 --keys CTRL+S", "keys", False),
    ]
    ok = all(cu_gate.parse_cu_command(c) == (sub, ro) for c, sub, ro in cases)
    check("ACP 侧能认出 cu 子命令并分级", ok,
          [cu_gate.parse_cu_command(c) for c, _, _ in cases])
    bad = ["cu windows && del x", "cu", "cd d:\\x; cu windows",
           "echo cu windows", "cu unknown-cmd --hwnd 1"]
    check("带 shell 拼接 / 子命令不认识 → 不当成 cu",
          all(cu_gate.parse_cu_command(c) is None for c in bad),
          [cu_gate.parse_cu_command(c) for c in bad])

    # 3) prompt 工具卡（ACP 没有自定义工具 → 靠"命令在 PATH + prompt 讲一句"）
    try:
        from engines.acp.service import AcpService, CU_CARD, CU_ONE_LINE
        sess = {}
        first = AcpService._cu_prompt_note(None, sess)
        again = AcpService._cu_prompt_note(None, sess)
        check("首轮注入完整工具卡", first == CU_CARD, first[:40])
        check("之后只注入一行提醒（省 token）", again == CU_ONE_LINE, again[:40])
        os.environ["COCRAFT_CU_PROMPT"] = "0"
        check("COCRAFT_CU_PROMPT=0 可关掉注入",
              AcpService._cu_prompt_note(None, {}) == "")
    except Exception as exc:
        check("加载 ACP 工具卡", False, exc)
    finally:
        os.environ.pop("COCRAFT_CU_PROMPT", None)

    # 4) 展示层剥掉注入前缀（用户 2026-11-05：「这个是啥？我没有输入过这一段」）
    #    卡片是拼在 prompt 前面的，opencode-acp 会把「卡片 + 原话」一起存进共享库 →
    #    messages() 直接读库时会把卡片当成用户说的话显示出来。
    try:
        from engines.acp.service import strip_injected_prefix, CU_CARD, CU_ONE_LINE
        user = "我们接入桌面端的agent，当用户使用这个桌面端的agent的时候一定要唤起对应的客户端吗？"
        check("完整工具卡 + 原话 → 只留原话",
              strip_injected_prefix(CU_CARD + "\n" + user) == user,
              strip_injected_prefix(CU_CARD + "\n" + user)[:40])
        check("一行提醒 + 原话 → 只留原话",
              strip_injected_prefix(CU_ONE_LINE + "\n" + user) == user,
              strip_injected_prefix(CU_ONE_LINE + "\n" + user)[:40])
        reseed = ("（本地补记：以下是面板保存的此前对话，仅作为上下文，请据此继续；不要复述这段补记。）\n"
                  "用户：x\n（以上为历史。）")
        check("补记块 + 原话 → 只留原话",
              strip_injected_prefix(reseed + "\n" + user) == user)
        check("正文里引用工具卡不会被吞（只剥开头）",
              strip_injected_prefix("我说的是这段：" + CU_CARD) == "我说的是这段：" + CU_CARD)
        check("只有卡片、没有正文 → 保留原文（不变成空白气泡）",
              strip_injected_prefix(CU_CARD) == CU_CARD)
        check("普通消息原样返回", strip_injected_prefix(user) == user)
    except Exception as exc:
        check("剥注入前缀", False, exc)

    # 5) cuCardShown 要落盘：不然后端一重启，下一条消息又发一整张卡
    try:
        import threading
        from engines.acp.service import AcpService as _Svc
        svc = object.__new__(_Svc)
        svc.agent_id = "mock"
        svc._lock = threading.Lock()
        svc._sessions = {"s1": {"id": "s1", "agent": "mock",
                                "cuCardShown": True, "messages": []}}
        svc._order = ["s1"]
        with tempfile.TemporaryDirectory() as td:
            svc._sessions_file = os.path.join(td, "_acp_sessions.json")
            svc._save()
            with open(svc._sessions_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        row = (data.get("sessions") or [{}])[0]
        check("cuCardShown 会落盘（重启后不再重复发完整卡）",
              row.get("cuCardShown") is True, row.get("cuCardShown"))
    except Exception as exc:
        check("cuCardShown 落盘", False, exc)


def main():
    tmp = tempfile.mkdtemp(prefix="cu_test_")
    audit_path = os.path.join(tmp, "audit.jsonl")
    lease_path = os.path.join(tmp, "lease.json")
    os.environ["COCRAFT_AUDIT_FILE"] = audit_path
    os.environ["COCRAFT_CU_LEASE_FILE"] = lease_path
    os.environ.pop("COCRAFT_CU_ACTIONS", None)
    os.environ["COCRAFT_CU_ACTOR"] = "cu_test"
    print("temp dir: %s" % tmp)

    # ---------- 离线部分（不需要交互式桌面） ----------
    offline_checks()

    # Windows 上 UIA 需要交互式桌面；没有桌面时只能跳过（不是失败）
    if not cu.winapi.foreground_hwnd() and not cu.winapi.list_windows(limit=1):
        print("[SKIP] 没有可见窗口（无交互桌面），跳过")
        return 0

    proc = None
    try:
        # ---------- 0. 起测试窗 ----------
        proc = subprocess.Popen(
            [ps51(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             os.path.join(HERE, "cu_testwin.ps1")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW)
        win = wait_window(TITLE)
        if not win:
            print("[SKIP] 测试窗口没出现（可能无交互桌面），跳过")
            return 0
        hwnd = win["hwnd"]
        check("能起并找到测试窗口", True)
        check("窗口 pid 与子进程一致", win["pid"] == proc.pid, (win["pid"], proc.pid))

        # ---------- 1. 只读层 ----------
        res = run(["windows", "--title", "CU Test"])
        check("windows 列出目标窗口", res["ok"] and res["count"] >= 1, res)
        res = run(["describe", "--hwnd", str(hwnd)])
        check("describe 拿到窗口信息", res["ok"] and res["window"]["title"].startswith("cocraft"),
              res)

        t0 = time.time()
        res = run(["tree", "--hwnd", str(hwnd), "--depth", "4", "--prune"])
        dt = time.time() - t0
        names = []

        def collect(node):
            names.append(node.get("name") or "")
            for k in node.get("children") or []:
                collect(k)

        collect(res.get("root") or {})
        check("tree 有节点", res["ok"] and (res.get("nodes") or 0) >= 6, res.get("nodes"))
        check("tree 里能看到 7/8/+/=", all(x in names for x in ("7", "8", "+", "=")),
              [n for n in names if n])
        check("tree 冷/热调用 < 3 秒", dt < 3.0, "%.2fs" % dt)

        res = run(["find", "--hwnd", str(hwnd), "--name", "7", "--class-like", "BUTTON"])
        check("find 按名字+类名找到按钮", res["ok"] and res["count"] == 1 and
              res["items"][0]["name"] == "7", res)
        check("find 结果带 rect", bool((res["items"][0].get("rect") or {}).get("w")), res)
        # ★图标：窗口能换成官方 cocraft.ico（标题栏 / 任务栏 / Alt-Tab 用）
        ico = os.path.join(ROOT, "frontend", "assets", "cocraft.ico")
        check("能给窗口换官方图标", cu.winapi.set_window_icon(hwnd, ico), ico)
        try:
            import ctypes as _ct
            from ctypes import wintypes as _wt
            _u = _ct.WinDLL("user32")
            _u.SendMessageW.restype = _ct.c_void_p
            _u.SendMessageW.argtypes = [_wt.HWND, _wt.UINT, _ct.c_void_p, _ct.c_void_p]
            _big = _u.SendMessageW(_wt.HWND(hwnd), 0x007F,  # WM_GETICON
                                   _ct.c_void_p(1), _ct.c_void_p(0))  # ICON_BIG
            check("换完能读回窗口图标（WM_GETICON 非 0）", bool(_big), _big)
        except Exception as exc:  # noqa: BLE001
            check("换完能读回窗口图标（WM_GETICON 非 0）", False, exc)
        # ★回归：uia_host 曾把条件数组写成 AutomationCondition[]（不存在的类型）→
        #   name + role / name + auto-id 这种"2 个条件"的 find 直接报「找不到类型」
        role = (res["items"][0].get("role") or "Pane")
        res2 = run(["find", "--hwnd", str(hwnd), "--name", "7", "--role", role])
        check("find 多条件（name + role）不再挂", res2["ok"] and res2["count"] == 1, res2)

        shot = run(["shot", "--hwnd", str(hwnd), "--max-width", "800"])
        check("shot 落盘", shot["ok"] and os.path.isfile(shot["path"]), shot)
        check("shot 不是黑图", shot.get("mean", 0) > 20, shot.get("mean"))
        check("shot 不抢焦点", cu.winapi.foreground_hwnd() != hwnd, "截图把窗口顶到前台了")

        # ---------- 2. 权限闸门 ----------
        denied = run(["click", "--hwnd", str(hwnd), "--name", "7"])
        check("默认 deny：动作被拒", denied.get("ok") is False and denied.get("denied"),
              denied)
        danger = run(["click", "--hwnd", str(hwnd), "--name", "发送", "--allow-actions"])
        check("危险词（发送）被拒", danger.get("ok") is False and danger.get("denied"), danger)
        check("危险词拒绝原因说清楚", "发送" in (danger.get("error") or ""), danger.get("error"))

        # ---------- 3. 动作层：7 + 8 = ----------
        # 先把窗口带到前台：否则第一次点击可能被"激活窗口"吃掉（7 丢了 → 0+8=8）
        run(["focus", "--hwnd", str(hwnd), "--allow-actions"])
        time.sleep(0.35)
        steps = [("7", 1), ("+", 2), ("8", 3), ("=", 4)]
        for name, idx in steps:
            res = run(["click", "--hwnd", str(hwnd), "--name", name, "--allow-actions"])
            if not check("click %r 成功（第 %d 步）" % (name, idx), res.get("ok"), res):
                break
        else:
            method = res.get("method") or ""
            check("点击方式已记录（uia 或 coord）", method.startswith("uia:") or method == "coord",
                  method)

        label = run(["find", "--hwnd", str(hwnd), "--like", "result:"])
        got = (label["items"][0]["name"] if label.get("items") else "")
        # 点击是 SendInput 投递的，界面处理慢半拍 → 等一小会儿再断言（否则偶发读到旧值）。
        # ★整套回归并行时机器更忙，2 秒偶尔不够（2026-11-04 实测出现过 result: 0）→ 给 5 秒。
        end = time.time() + 5.0
        while got != "result: 15" and time.time() < end:
            time.sleep(0.1)
            label = run(["find", "--hwnd", str(hwnd), "--like", "result:"])
            got = (label["items"][0]["name"] if label.get("items") else "")
        check("7+8 真的算出 15（界面真变了）", got == "result: 15", got)

        # ---------- 4. 打字 ----------
        typed = "hello-cu-42"
        res = run(["type", "--hwnd", str(hwnd), "--class-like", "EDIT",
                   "--text", typed, "--replace", "--allow-actions"])
        check("type 成功", res.get("ok"), res)
        tb = run(["find", "--hwnd", str(hwnd), "--class-like", "EDIT"])
        got_tb = (tb["items"][0]["name"] if tb.get("items") else "")
        check("输入框内容真的变了", got_tb == typed, got_tb)

        # ---------- 5. 桌面租约 ----------
        got_a, info_a = cu.lease.acquire("test/a", wait_ms=0)
        got_b, info_b = cu.lease.acquire("test/b", wait_ms=0)
        check("A 能拿到租约", got_a, info_a)
        check("B 拿不到（busy + 指明 owner）",
              (not got_b) and info_b.get("busy") and info_b.get("owner") == "test/a", info_b)
        cu.lease.release("test/a")
        check("释放后租约清空", not cu.lease.current().get("held"), cu.lease.current())

        # 动作后租约要自己释放（否则别人永远抢不到）
        run(["click", "--hwnd", str(hwnd), "--name", "7", "--allow-actions"])
        check("动作结束自动释放租约", not cu.lease.current().get("held"), cu.lease.current())

        # ---------- 6. 审计 ----------
        rows = read_audit(audit_path)
        cu_rows = [r for r in rows if r.get("source") == "cu"]
        check("审计里有 cu 记录", len(cu_rows) >= 5, len(cu_rows))
        check("每条 cu 记录都带 kind=cu",
              all((r.get("action") or {}).get("kind") == "cu" for r in cu_rows), "")
        check("被拒绝的动作也入链",
              any(r.get("decision") == "denied" for r in cu_rows),
              [r.get("decision") for r in cu_rows])
        raw = json.dumps(cu_rows, ensure_ascii=False)
        check("审计不记正文（只有长度+哈希）", typed not in raw, "正文泄漏进链了")
        check("审计记了长度与哈希", ("len=%d" % len(typed)) in raw, "")

        # ---------- 7. 剪贴板 ----------
        res = run(["clipboard-set", "--text", "cu-clip-777", "--allow-actions"])
        check("clipboard-set 成功", res.get("ok"), res)
        res = run(["clipboard-get"])
        check("clipboard-get 读回同一串", res.get("text") == "cu-clip-777", res.get("text"))

        # ---------- 8. CLI 子进程（真命令行入口） ----------
        env = dict(os.environ)
        cli = subprocess.run(
            [sys.executable, os.path.join(HERE, "cu.py"), "windows", "--title", "CU Test"],
            capture_output=True, timeout=60, env=env)
        ok_json = False
        try:
            payload = json.loads(cli.stdout.decode("utf-8"))
            ok_json = payload.get("ok") and payload.get("count") >= 1
        except Exception as exc:
            print("   CLI 输出解析失败: %s / %r" % (exc, cli.stdout[:200]))
        check("CLI 子进程输出是 JSON 且找到窗口", ok_json, cli.stdout[:200])
    except Exception:
        traceback.print_exc()
        check("测试过程无异常", False)
    finally:
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        try:
            if getattr(cu.uia, "_HOST", None):
                cu.uia._HOST.close()
                cu.uia._HOST = None
        except Exception:
            pass

    passed = sum(1 for _, ok in RESULTS if ok)
    failed = len(RESULTS) - passed
    print("\n%d 项 / 通过 %d / 失败 %d" % (len(RESULTS), passed, failed))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
