#!/usr/bin/env python
"""cu_macro_test.py -- `cu macro`（录制回放）回归。

两段：
  A. 离线：合成审计记录 → 提取宏（只取成功动作 / 跳过只读与被拒 / 文本变参数 /
     param_hints / 危险快捷键 / 不含 hwnd）→ 参数替换 → 存/列/导出/删 → 权限分级。
  B. 真窗口（tools/cu_testwin.ps1）：点 7 + 8 = → 存宏 → 干跑预检 → 回放（结果还是 15）；
     再往输入框打字 → 存宏 → 回放并换参数（内容真的变了）；
     再故意改坏定位 → 回放失败在第 1 步 + 「宏可能过期」+ 列表标 stale。

全程用临时目录（审计链 / 宏文件 / 租约），不污染真实运行态。
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
    print(("[OK]  " if cond else "[BAD] ") + name + (("  -> " + str(detail)[:300]) if (detail and not cond) else ""))
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


def wait_name(hwnd, like, want="", timeout=2.5):
    """等控件文字变成期望值（点击是 SendInput 投递，界面处理慢半拍 —— 不能立刻读）。"""
    end = time.time() + timeout
    got = ""
    while True:
        res = run(["find", "--hwnd", str(hwnd), "--like", like])
        items = res.get("items") or []
        got = items[0].get("name") if items else ""
        if (want and got == want) or time.time() >= end:
            return got
        time.sleep(0.1)


WIN = {"hwnd": 42, "process": "QQMail.exe", "class": "WinForms10.Window.8.app",
       "title": "写邮件 - QQ邮箱"}


def fake_mail_trace():
    """合成一段"写邮件"的审计（等价于真跑过一次的样子）。"""
    cu.audit.record("tree", True, window=WIN, action_kind="read", decision="allowed",
                    note="深度 4 节点 9")                       # 只读 → 不该进宏
    cu.audit.record("click", False, window=WIN, action_kind="action", decision="denied",
                    targets=["发送"], note="危险词 发送")        # 被拒 → 不该进宏
    cu.audit.record("click", True, window=WIN, action_kind="action", decision="allowed",
                    targets=["写信"], note="method=uia:invoke element='写信' role=Button loc="
                                           + json.dumps({"n": "写信", "r": "Button"}, ensure_ascii=False))
    cu.audit.record("type", True, window=WIN, action_kind="action", decision="allowed", text="a@b.com",
                    note="method=uia:value element='收件人' loc="
                         + json.dumps({"n": "收件人", "r": "Edit", "c": "Edit"}, ensure_ascii=False))
    cu.audit.record("type", True, window=WIN, action_kind="action", decision="allowed", text="周报",
                    note="method=sendinput element='主题' loc="
                         + json.dumps({"n": "主题", "r": "Edit"}, ensure_ascii=False))
    cu.audit.record("keys", True, window=WIN, action_kind="action", decision="allowed",
                    note="keys=CTRL+ENTER")


def offline_checks(since_before_fake):
    # ---- 权限分级：play = 动作；其余只读；不认识的子命令不当成 cu ----
    cases = [
        ("cu macro play send-mail --set to=a", ("macro.play", False)),
        ("cu macro list", ("macro.list", True)),
        ("cu macro show x", ("macro.show", True)),
        ("cu macro save x --from-audit --since 2h", ("macro.save", True)),
        ("cu macro rm x", ("macro.rm", True)),
        ("cu macro export x --out a.json", ("macro.export", True)),
    ]
    got = [cu.gate.parse_cu_command(c) for c, _ in cases]
    check("权限分级：macro play=动作，其余只读", got == [w for _, w in cases], got)
    bad = ["cu macro", "cu macro bogus", "cu macro play x && del y"]
    check("奇怪/带拼接的 macro 命令 → 不当成 cu",
          all(cu.gate.parse_cu_command(c) is None for c in bad),
          [cu.gate.parse_cu_command(c) for c in bad])

    # ---- 从审计提取 ----
    fake_mail_trace()
    m = cu.macro.extract(since=since_before_fake)
    check("提取出 4 步且顺序正确", [s["op"] for s in m["steps"]] == ["click", "type", "type", "keys"],
          [s["op"] for s in m["steps"]])
    check("只读工具（tree）不进宏", all(s.get("op") != "tree" for s in m["steps"]))
    check("被拒绝的动作不进宏", not any(s.get("name") == "发送" for s in m["steps"]))
    check("输入文本变成参数（控件名 → 参数名）", m["params"] == ["to", "subject"], m["params"])
    check("参数提示记着原控件名", (m["param_hints"].get("to") or {}).get("element") == "收件人",
          m["param_hints"])
    check("文本步骤用 {{参数}} 占位", m["steps"][1]["text"] == "{{to}}", m["steps"][1])
    check("回车类快捷键算危险动作", "CTRL+ENTER" in m["danger"], m["danger"])
    raw = json.dumps(m, ensure_ascii=False)
    check("宏里不存 hwnd 数值（会变）", "hwnd" not in raw, raw[:200])
    check("宏里不存输入正文（只留占位）", "a@b.com" not in raw and "周报" not in raw)
    check("目标窗口按标题匹配（不是句柄）",
          (m.get("target") or {}).get("match", {}).get("title_like") == "写邮件 - QQ邮箱", m.get("target"))

    # ---- 参数替换 ----
    steps, missing = cu.macro.render(m, {"to": "x@y.com", "subject": "周报-2"})
    check("缺参数时不会静默替换", (not missing) and steps[1]["text"] == "x@y.com"
          and steps[2]["text"] == "周报-2", (steps[1], steps[2]))
    _, miss2 = cu.macro.render(m, {"to": "only"})
    check("缺参数能列出来", miss2 == ["subject"], miss2)

    # ---- 存 / 列 / 导出 / 删 ----
    cu.macro.put("send-mail", m)
    row = [r for r in cu.macro.list_rows() if r["name"] == "send-mail"]
    check("list 有这条宏（步数/参数/危险）",
          bool(row) and row[0]["steps"] == 4 and row[0]["params"] == ["to", "subject"]
          and row[0]["danger"] == ["CTRL+ENTER"], row)
    check("/cu/status 用的 summary 带数量", cu.macro.summary().get("count") == 1,
          cu.macro.summary())
    exp = run(["macro", "export", "send-mail", "--out", os.path.join(tempfile.gettempdir(),
                                                                    "cu_macro_export.json")])
    check("export 写文件成功", exp.get("ok") and os.path.isfile(exp.get("path") or ""), exp)
    check("同名再 save 不给 --force 会报错",
          _raises(lambda: run(["macro", "save", "send-mail", "--from-audit"])), "")

    # ---- 回放前的三道闸 ----
    denied = run(["macro", "play", "send-mail", "--set", "to=a", "--set", "subject=b"])
    check("含危险动作的宏：不给 --allow-danger 直接拒",
          denied.get("ok") is False and denied.get("denied") and "CTRL+ENTER" in denied.get("error", ""),
          denied)
    try:
        run(["macro", "play", "send-mail"])
        ok = False
        err = ""
    except Exception as exc:  # noqa: BLE001
        ok = True
        err = str(exc)
    check("缺参数时明确说缺哪个", ok and "to" in err and "subject" in err, err)

    noplay = run(["macro", "play", "send-mail", "--set", "to=a", "--set", "subject=b",
                  "--dry-run"])
    check("干跑缺 --allow-danger 也拒（不会碰桌面）", noplay.get("ok") is False, noplay)
    check("删掉宏", run(["macro", "rm", "send-mail"]).get("removed") is True, "")
    check("删完 list 为空", cu.macro.summary().get("count") == 0, cu.macro.summary())
    return m


def _raises(fn):
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


def live_checks(proc_holder, audit_path):
    # 换一个干净的审计文件：离线那段是合成记录，不能混进真回放（否则 --since 会一起捞到）
    import audit as audit_mod
    audit_mod.set_file(audit_path)
    # 清掉上次可能残留的同名测试窗（同标题会让"点"和"读"落到不同窗口上）
    for w in cu.winapi.list_windows(title=TITLE, limit=20):
        try:
            subprocess.run(["taskkill", "/PID", str(w["pid"]), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        except Exception:
            pass
    proc = subprocess.Popen(
        [ps51(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
         os.path.join(HERE, "cu_testwin.ps1")],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW)
    proc_holder.append(proc)
    win = wait_window(TITLE)
    if not win:
        print("[SKIP] 测试窗口没出现（可能无交互桌面），跳过真窗口部分")
        return
    hwnd = win["hwnd"]
    check("起并找到测试窗口", True)
    # 先把窗口带到前台再点：否则第一次点击可能被"激活窗口"吃掉（7 就丢了，算成 0+8=8）
    run(["focus", "--hwnd", str(hwnd), "--allow-actions"])
    time.sleep(0.35)

    t_live = time.time()
    time.sleep(0.2)
    steps = [("7"), ("+"), ("8"), ("=")]
    ok_all = True
    for name in steps:
        res = run(["click", "--hwnd", str(hwnd), "--name", name, "--allow-actions"])
        ok_all = ok_all and bool(res.get("ok"))
    check("真跑 4 步点击（7 + 8 =）", ok_all)
    got = wait_name(hwnd, "result:", "result: 15")
    check("跑完结果真的是 15", got == "result: 15", got)

    # ---- 从真审计提取 + 回放 ----
    saved = run(["macro", "save", "calc-15", "--from-audit",
                 "--since", str(t_live - 1), "--force"])
    check("从真审计提取宏（4 步）", saved.get("ok") and saved.get("steps") == 4, saved)
    check("宏的目标窗口是测试窗",
          TITLE in str((saved.get("target") or {}).get("match", {}).get("title_like") or ""),
          saved.get("target"))
    check("这条宏不含危险动作", not saved.get("danger"), saved.get("danger"))

    dry = run(["macro", "play", "calc-15", "--dry-run"])
    check("干跑：4 个控件都找得到", dry.get("ok") and len(dry.get("done") or []) == 4, dry)

    played = run(["macro", "play", "calc-15", "--allow-actions"])
    check("真回放 4 步全过", played.get("ok") and played.get("steps") == 4, played)
    check("回放耗时 < 5 秒（不花模型）", int(played.get("ms") or 0) < 5000, played.get("ms"))
    # 回放是"照做"：宏不会自己重置窗口状态，所以 15 被继续按 7 + 8 = → 95
    got = wait_name(hwnd, "result:", "result: 95")
    check("回放真的点到了（结果从 15 变 95，宏不重置状态）", got == "result: 95", got)
    row = [r for r in cu.macro.list_rows() if r["name"] == "calc-15"]
    check("回放统计写进宏（plays / lastOk）",
          bool(row) and row[0]["plays"] >= 1 and row[0]["lastOk"] is True, row)

    # ---- 打字 → 存宏 → 换参数回放 ----
    t_type = time.time()
    typed1 = "macro-typing-1"
    res = run(["type", "--hwnd", str(hwnd), "--class-like", "EDIT", "--text", typed1,
               "--replace", "--allow-actions"])
    check("先手打一次（作为录制样本）", res.get("ok"), res)
    s2 = run(["macro", "save", "fill-box", "--from-audit", "--since", str(t_type), "--force"])
    check("提取出带参数的宏", s2.get("ok") and len(s2.get("params") or []) >= 1, s2)
    typed2 = "macro-typing-2"
    p2 = run(["macro", "play", "fill-box", "--allow-actions",
              "--set", "%s=%s" % (s2["params"][0], typed2)])
    check("换参数回放成功", p2.get("ok"), p2)
    # 输入也是投递式的：等它变成新值再断言
    tb = run(["find", "--hwnd", str(hwnd), "--class-like", "EDIT"])
    got_tb = tb["items"][0]["name"] if tb.get("items") else ""
    end = time.time() + 2.0
    while got_tb != typed2 and time.time() < end:
        time.sleep(0.1)
        tb = run(["find", "--hwnd", str(hwnd), "--class-like", "EDIT"])
        got_tb = tb["items"][0]["name"] if tb.get("items") else ""
    check("输入框内容真的变成新参数", got_tb == typed2, got_tb)

    # ---- 故意改坏定位 → 失败即停 + 过期提示 ----
    m = cu.macro.get("fill-box")
    m["steps"][0]["name"] = "不存在的控件-zzz"
    m["steps"][0].pop("class_like", None)
    cu.macro.put("fill-box", m)
    bad = run(["macro", "play", "fill-box", "--allow-actions",
               "--set", "%s=whatever" % s2["params"][0]])
    check("定位失效 → 失败在第 1 步", bad.get("ok") is False and bad.get("failed_step") == 1, bad)
    check("失败提示说清「宏可能过期」并给重新探索的办法",
          "宏可能过期" in (bad.get("hint") or "") and "cu tree" in (bad.get("hint") or ""),
          bad.get("hint"))
    row = [r for r in cu.macro.list_rows() if r["name"] == "fill-box"]
    check("失败后列表标「可能过期」", bool(row) and row[0]["stale"] is True, row)

    # ---- 危险动作：显式 --allow-danger 也仍受逐步闸门约束 ----
    m2 = {"v": 1, "created": "", "target": {"kind": "desktop",
                                            "match": {"title_like": TITLE}},
          "params": [], "steps": [{"op": "click", "name": "发送"}], "danger": ["发送"],
          "stats": {}}
    cu.macro.put("danger-click", m2)
    deny = run(["macro", "play", "danger-click", "--allow-danger", "--allow-actions"])
    check("--allow-danger 也不能突破「危险词」闸门",
          deny.get("ok") is False and "发送" in (deny.get("error") or ""), deny)
    cu.macro.remove("danger-click")

    # 有人把宏文件里的 danger 抹掉 → 仍然按当前步骤重算（不能靠改 JSON 免确认）
    m3 = cu.macro.get("calc-15")
    m3["danger"] = []
    m3["steps"] = [{"op": "keys", "keys": "CTRL+ENTER"}]
    cu.macro.put("tampered", m3)
    t3 = run(["macro", "play", "tampered", "--allow-actions"])
    check("危险动作按步骤重算（改 JSON 抹掉 danger 也拦得住）",
          t3.get("ok") is False and t3.get("denied"), t3)
    cu.macro.remove("tampered")

    # ---- 审计：宏回放入链，参数值只哈希 ----
    rows = [r for r in read_audit(audit_path) if r.get("source") == "cu"]
    macro_rows = [r for r in rows if r.get("action", {}).get("command") == "cu macro_play"]
    check("宏回放入审计（tool=macro_play）", len(macro_rows) >= 2, len(macro_rows))
    raw = json.dumps(rows, ensure_ascii=False)
    check("审计不记宏参数正文（只有哈希/长度）", typed2 not in raw, "参数正文进链了")
    check("审计记了参数长度", "text len=" in raw, "")
    check("宏失败也入链（decision=failed）",
          any(r.get("decision") == "failed" for r in macro_rows), [r.get("decision") for r in macro_rows])


def main():
    tmp = tempfile.mkdtemp(prefix="cu_macro_test_")
    os.environ["COCRAFT_AUDIT_FILE"] = os.path.join(tmp, "audit.jsonl")
    os.environ["COCRAFT_CU_MACROS_FILE"] = os.path.join(tmp, "macros.json")
    os.environ["COCRAFT_CU_LEASE_FILE"] = os.path.join(tmp, "lease.json")
    os.environ.pop("COCRAFT_CU_ACTIONS", None)
    os.environ["COCRAFT_CU_ACTOR"] = "cu_macro_test"
    print("temp dir: %s" % tmp)

    procs = []
    try:
        t0 = time.time()
        offline_checks(t0 - 1)
        if not cu.winapi.foreground_hwnd() and not cu.winapi.list_windows(limit=1):
            print("[SKIP] 没有可见窗口（无交互桌面），跳过真窗口部分")
        else:
            live_checks(procs, os.path.join(tmp, "audit_live.jsonl"))
    except Exception:
        traceback.print_exc()
        check("测试过程无异常", False)
    finally:
        for proc in procs:
            try:
                if proc.poll() is None:
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
