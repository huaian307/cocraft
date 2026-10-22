# -*- coding: utf-8 -*-
r"""离线回归：★群聊审批审计（信任面板「谁批准了什么」的数据源）。

背景（2026-10-21 · 方向 3 信任面板收尾）：
  以前群聊审批只留两处痕迹 —— 群里那条消息（会滚走）+ 内存活动卡（重启就没）。
  「谁批准了哪条、什么时候、命令原文是什么」**查不回来**，等于审计缺失。

现在每次权限申请**结论落盘**到 `runtime/state/_cc_perm_log.json`（最多 500 条）：
  `{ts, chat, aid, agent, owner, kind, title, cmd, paths, destructive, verdict, by, reply}`
`verdict` = once / always / reject / auto；`by` = owner / level / timeout / no-ctx / send。

本测试**不碰真实文件**（全部指到临时目录），断言：
  1. 写一条 → `perm_log()` 能读回，字段（命令原文 / 路径 / 拥有者 / 回复原话）完整
  2. 新 → 旧排序；`limit` 生效
  3. 条数有上限（超出丢最旧的）
  4. 文件坏了 / 读不到 → 回空列表，**不抛异常**
  5. 写失败（路径不可写）→ **不抛异常**（审计失败绝不能挡住权限流程）
  6. 等级已放行（非破坏性）→ 走 `_perm_ask_group` 时记一条 `auto / level`
  7. 原子落盘（没有残留 `.tmp`）

跑法：python tools\perm_audit_test.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import time
import time as REAL_TIME

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(HERE, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cocraft_agents as ca  # noqa: E402

TMP = tempfile.mkdtemp(prefix="perm-audit-")
ca.STATE_DIR = TMP
ca.PERM_LOG_FILE = os.path.join(TMP, "_cc_perm_log.json")
ca.PERM_LOG_MAX = 5
# 不联网：find() 换成内存表（审计里要记成员显示名 / 群 id）
ca.find = lambda aid: {"id": aid, "name": "yiyi", "display": "yiyi", "chats": ["7"]}


class _Clock:
    """★测试专用时钟：Windows 的 time.time() 粒度约 15.6ms，连写多条会拿到**同一个 ts**，
    排序就变成"看运气"。这里每次取时间 +1 秒，让"新 → 旧"可以确定地断言。"""

    def __init__(self, t: float = 1700000000.0):
        self.t = t

    def time(self) -> float:
        self.t += 1.0
        return self.t

    def time_ns(self) -> int:
        return int(self.t * 1e9)


ca.time = _Clock()

PASS = 0
FAIL = 0
BAD = []


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   " + name)
    else:
        FAIL += 1
        BAD.append(name)
        print("  [BAD]  " + name + (("   [" + str(extra)[:220] + "]") if extra else ""))


def clean():
    try:
        if os.path.exists(ca.PERM_LOG_FILE):
            os.remove(ca.PERM_LOG_FILE)
    except OSError:
        pass


def main() -> int:
    print("\n== 1. 写一条 → 读回（字段完整） ==")
    clean()
    ca._perm_log_add("11", "7", {
        "owner": "8", "kind": "execute", "title": "Run command",
        "cmd": "Remove-Item -Recurse -Force build",
        "locations": ["D:/ws/build", "D:/ws/out"], "destructive": True,
        "verdict": "once", "by": "owner", "reply": "@yiyi 确认",
    })
    rows = ca.perm_log()
    ok("读到 1 条", len(rows) == 1, rows)
    r = rows[0] if rows else {}
    ok("★命令原文保留（审计要能追溯具体那一条）",
       r.get("cmd") == "Remove-Item -Recurse -Force build", r.get("cmd"))
    ok("★路径列表保留", r.get("paths") == ["D:/ws/build", "D:/ws/out"], r.get("paths"))
    ok("★谁批准（拥有者 id）+ 回复原话",
       r.get("owner") == "8" and r.get("reply") == "@yiyi 确认",
       (r.get("owner"), r.get("reply")))
    ok("结论 / 依据 / 破坏性标记",
       r.get("verdict") == "once" and r.get("by") == "owner" and r.get("destructive") is True,
       (r.get("verdict"), r.get("by"), r.get("destructive")))
    ok("成员显示名 + 群 id", r.get("agent") == "yiyi" and r.get("chat") == "7",
       (r.get("agent"), r.get("chat")))
    ok("原子落盘：没有残留 .tmp", not os.path.exists(ca.PERM_LOG_FILE + ".tmp"))

    print("\n== 2. 新 → 旧 + limit ==")
    for i in range(3):
        ca._perm_log_add("11", "7", {"kind": "edit", "title": "Edit file",
                                     "locations": ["D:/ws/a%d.js" % i],
                                     "verdict": "always", "by": "owner"})
    rows = ca.perm_log()
    ok("一共 4 条", len(rows) == 4, len(rows))
    ok("★最新在最前（按 ts 倒序）",
       rows[0].get("paths") == ["D:/ws/a2.js"] and str(rows[-1].get("cmd", "")).startswith("Remove-Item"),
       [x.get("paths") or x.get("cmd") for x in rows])
    ok("limit=2 只回 2 条", len(ca.perm_log(2)) == 2)

    print("\n== 3. 条数上限（丢最旧的） ==")
    clean()
    for i in range(8):
        ca._perm_log_add("11", "7", {"title": "t%d" % i, "verdict": "auto", "by": "level"})
    rows = ca.perm_log()
    ok("上限 = PERM_LOG_MAX(5)", len(rows) == 5, len(rows))
    # ⚠ Windows 的 time.time() 粒度约 15.6ms → 同一批写入的 ts 可能相同，
    #   所以只断言"最旧的三条被丢掉、剩下的是 t3..t7"，不依赖并列时的排序。
    ok("留下的是最新 5 条（t3..t7，t0~t2 被淘汰）",
       set(x.get("title") for x in rows) == {"t3", "t4", "t5", "t6", "t7"},
       [x.get("title") for x in rows])

    print("\n== 4. 文件坏了 → 空列表，不抛 ==")
    with io.open(ca.PERM_LOG_FILE, "w", encoding="utf-8") as fh:
        fh.write("{ not json")
    ok("坏文件 → []", ca.perm_log() == [])
    ca._perm_log_add("11", "7", {"title": "after-corrupt", "verdict": "auto", "by": "level"})
    ok("坏文件之后还能继续写", len(ca.perm_log()) == 1, ca.perm_log())

    print("\n== 4.5 坏 ts 不让接口挂 ==")
    with io.open(ca.PERM_LOG_FILE, "w", encoding="utf-8") as fh:
        json.dump([{"id": "x", "ts": "abc", "title": "bad"},
                   {"id": "y", "ts": 1, "title": "ok"}], fh)
    rows = ca.perm_log()
    ok("坏 ts 不抛、仍能返回两条", len(rows) == 2, rows)
    ok("按时间排：正常的那条在前（坏 ts 当 0）",
       rows[0].get("title") == "ok", [x.get("title") for x in rows])

    print("\n== 5. 写失败不能挡住权限流程 ==")
    old = ca.PERM_LOG_FILE
    ca.PERM_LOG_FILE = TMP          # 用一个目录当文件 → 必然写不进去
    raised = False
    try:
        ca._perm_log_add("11", "7", {"title": "boom", "verdict": "auto", "by": "level"})
    except Exception as exc:  # noqa: BLE001
        raised = True
        ok("不抛异常", False, repr(exc))
    if not raised:
        ok("★写失败静默（审计不是功能前置）", True)
    ca.PERM_LOG_FILE = old

    print("\n== 6. 等级已放行（非破坏性）→ 不打扰、也不写审计 ==")
    clean()
    v = ca._perm_ask_group("11", "execute", "Run command", ["D:/ws"], True, cmd="npm test")
    ok("返回 always（不打扰拥有者）", v == "always", v)
    # ★审计只记"真正需要人拍板"的事件；策略自动放行写进去会把真实审批冲掉，也会 IO 放大
    ok("★等级自动放行**不写审计**", ca.perm_log() == [], ca.perm_log())

    print("\n== 7. 真实文件没被碰 ==")
    real = os.path.join(BACKEND, "..", "runtime", "state", "_cc_perm_log.json")
    ok("测试全部写在临时目录（没污染 runtime/state）",
       os.path.abspath(ca.PERM_LOG_FILE) != os.path.abspath(real)
       and TMP in os.path.abspath(ca.PERM_LOG_FILE))

    # ---- 8. 真走 _perm_ask_group 的批准/拒绝/超时分支（假 WS，不联网、不真发群消息）----
    #    ⚠ 这一段是为了防止「加审计」改坏最危险的权限判定：
    #       破坏性操作必须**只认「确认」**（「可以」不算），且只认拥有者 + 必须 @ 到本 agent。
    print("\n== 8. 权限分支：拥有者批准/拒绝/超时 + 破坏性只认确认 ==")
    ca.time = REAL_TIME      # ★这一节要真实时间（假时钟每次 +1s 会让等待循环直接超时）

    class FakeWS:
        def __init__(self, frames):
            self.frames = list(frames)
            self.closed = False

        def recv(self, timeout=1.0):
            if self.frames:
                return self.frames.pop(0)
            time.sleep(0.03)          # 模拟"这段时间没人说话"
            return None

        class _Q:
            def put(self, fr):
                pass
        q = _Q()

    def frame(content, sender="8", mentions=None):
        return {"type": "message", "chat_id": "7", "sender_kind": "user",
                "sender_id": sender, "content": content, "id": "f-%d" % time.time_ns(),
                "mentions": mentions if mentions is not None else ["yiyi"]}

    state = {"quar": 0}
    ca._send_message = lambda *a, **k: True
    ca._act_add = lambda *a, **k: {}
    ca._act_finish = lambda *a, **k: None
    ca._seen_push = lambda *a, **k: None
    ca._quarantine_before_allow = lambda *a, **k: state.__setitem__("quar", state["quar"] + 1)
    ca.ask_timeout = lambda: 0.45

    def run(frames, destructive=False):
        clean()
        ca._perm_ctx_now = lambda aid, net=True: ("8", FakeWS(frames), "7")
        return ca._perm_ask_group("11", "execute", "Run command", ["D:/ws"], False,
                                  cmd="npm test", destructive=destructive)

    ok("拥有者「@yiyi 可以」→ once", run([frame("@yiyi 可以")]) == "once")
    r = (ca.perm_log() or [{}])[0]
    ok("★审计记下 by=owner + 回复原话",
       r.get("by") == "owner" and r.get("reply") == "@yiyi 可以" and r.get("verdict") == "once",
       (r.get("by"), r.get("verdict"), r.get("reply")))

    ok("「@yiyi 始终可以」→ always", run([frame("@yiyi 始终可以")]) == "always")
    ok("「@yiyi 不行」→ reject（by=owner）",
       run([frame("@yiyi 不行")]) == "reject"
       and (ca.perm_log() or [{}])[0].get("by") == "owner",
       (ca.perm_log() or [{}])[0].get("by"))

    state["quar"] = 0
    ok("★破坏性 + 「可以」→ **不放行**（超时拒绝）", run([frame("@yiyi 可以")], True) == "reject")
    ok("★破坏性 + 「可以」**没有**触发隔离（没放行就没动手）", state["quar"] == 0, state)
    ok("破坏性超时审计标 by=timeout",
       (ca.perm_log() or [{}])[0].get("by") == "timeout",
       (ca.perm_log() or [{}])[0].get("by"))
    state["quar"] = 0
    ok("★破坏性 + 「确认」→ once，且**放行前**先隔离",
       run([frame("@yiyi 确认")], True) == "once" and state["quar"] == 1, state)
    ok("破坏性审计带 destructive=True",
       (ca.perm_log() or [{}])[0].get("destructive") is True,
       (ca.perm_log() or [{}])[0].get("destructive"))

    ok("别人说「可以」不算（sender_id≠拥有者）→ 超时拒绝",
       run([frame("@yiyi 可以", sender="9")]) == "reject")
    ok("没 @ 到本 agent 的「可以」不算 → 超时拒绝",
       run([frame("可以", mentions=[])]) == "reject")

    # 拿不到 owner/ws/群 → 保守拒绝，且审计要能看出原因
    clean()
    ca.PERM_CTX_WAIT = 0.0
    ca._perm_ctx_now = lambda aid, net=True: (None, None, None)
    ok("★拿不到拥有者 / 群 → 保守拒绝",
       ca._perm_ask_group("11", "execute", "Run command", ["D:/ws"], False, cmd="x") == "reject")
    ok("审计记 by=no-ctx（能追到是「拿不到拥有者」）",
       (ca.perm_log() or [{}])[0].get("by") == "no-ctx",
       (ca.perm_log() or [{}])[0].get("by"))

    # 申请发不出去 → 拒绝，也要留痕
    clean()
    ca._perm_ctx_now = lambda aid, net=True: ("8", FakeWS([]), "7")
    ca._send_message = lambda *a, **k: False
    ok("★申请发不出去 → 拒绝",
       ca._perm_ask_group("11", "execute", "Run command", ["D:/ws"], False, cmd="x") == "reject")
    ok("审计记 by=send", (ca.perm_log() or [{}])[0].get("by") == "send",
       (ca.perm_log() or [{}])[0].get("by"))

    shutil.rmtree(TMP, ignore_errors=True)
    print("\n[%s]  PASS=%d  FAIL=%d" % ("PASS" if FAIL == 0 else "FAIL", PASS, FAIL))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - " + b)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
