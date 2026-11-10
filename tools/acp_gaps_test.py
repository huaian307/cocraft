#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""acp_gaps_test.py -- ACP 补齐项回归（压缩卡片 / 费用 / 内置技能 / 技能 resume / 模型元数据）。

只测**纯逻辑**（不拉起 agent、不联网、不写真实运行态）：
  1. `compaction_summary_chunk` → 不再进思考，而是独立一条 `compaction` 消息
  2. 共享库里的 `compaction` 行 → 面板形状（以前被 else: continue 丢掉）
  3. 费用：累计口径（opencode-acp）与逐轮增量口径都不算错
  4. 内置技能：从 OpenCode 服务取到的 `/builtin` 技能并入清单；磁盘同名覆盖它
  5. 技能 resume：先留「已注入技能」，正文走 pendingSkills，用户消息只留短指令
  6. 模型元数据：capabilities 用 agent 真宣告；价格表可选、没配就是 null
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from engines.acp import service as S            # noqa: E402
from engines.acp import skills as SK           # noqa: E402
from engines.acp.map_events import AssistantTurn  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond)))
    print(("[OK]  " if cond else "[BAD] ") + name + (("   " + str(detail)) if (detail and not cond) else ""))


def t_turn():
    # 1) 压缩摘要不进思考
    t = AssistantTurn("s1", "a1", agent="mock", model="m1")
    evs = t.apply({"sessionUpdate": "compaction_summary_chunk",
                   "content": {"type": "text", "text": "## Objective\n把上下文压一下"}})
    check("压缩摘要不再当「碎碎念」输出", evs == [] and t.reason_parts == [],
          (evs, t.reason_parts))
    check("压缩摘要存进本轮（可以单独成卡）", "".join(t.compaction_parts).startswith("## Objective"))
    cmp = t.compaction_message()
    check("compaction 消息形状（type/status/summary/time）",
          isinstance(cmp, dict) and cmp.get("type") == "compaction"
          and cmp.get("status") == "completed" and "## Objective" in (cmp.get("summary") or "")
          and (cmp.get("time") or {}).get("created"), cmp)

    # 2) 压缩发生在正文之前 → 标记 before_text（决定插在助手消息前面）
    t2 = AssistantTurn("s1", "a2", agent="mock", model="m1")
    t2.apply({"sessionUpdate": "compaction_summary_chunk",
              "content": {"type": "text", "text": "摘要"}})
    check("正文之前压缩 → compaction_before_text=True", t2.compaction_before_text is True)

    # 3) 先有正文、后压缩 → False
    t3 = AssistantTurn("s1", "a3", agent="mock", model="m1")
    t3.apply({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "答"}})
    t3.apply({"sessionUpdate": "compaction_summary_chunk",
              "content": {"type": "text", "text": "摘要"}})
    check("正文之后压缩 → compaction_before_text=False", t3.compaction_before_text is False)

    # 8) 没有摘要 → 不产生 compaction 消息
    t4 = AssistantTurn("s1", "a4")
    check("没有压缩 → 不产生 compaction 消息", t4.compaction_message() is None)

    # 8b) ★失败：错误要挂到消息上（前端才能画「余额不足 / 限流…」卡片，而不是"没有返回正文"）
    t4b = AssistantTurn("s1", "a4b")
    t4b.error = {"type": "quota", "message": "Insufficient Balance (status 402)", "status": 402}
    mm = t4b.message()
    check("失败消息带 error + finish=error",
          isinstance(mm.get("error"), dict) and mm.get("finish") == "error", mm.get("error"))

    # 5) 费用：累计口径（新值 >= 旧值 → 覆盖）
    t5 = AssistantTurn("s1", "a5")
    t5.apply({"sessionUpdate": "usage_update", "used": 10, "size": 100,
              "cost": {"amount": 0.5, "currency": "USD"}})
    t5.apply({"sessionUpdate": "usage_update", "used": 20, "size": 100,
              "cost": {"amount": 0.8, "currency": "USD"}})
    check("费用累计口径 → 取最新（0.8）", abs(float(t5.cost) - 0.8) < 1e-9, t5.cost)

    # 6) 费用：逐轮增量口径（新值更小 → 累加）
    t6 = AssistantTurn("s1", "a6")
    t6._note_cost(0.5, "USD")
    t6._note_cost(0.3, "USD")
    check("费用增量口径 → 累加（0.8）", abs(float(t6.cost) - 0.8) < 1e-9, t6.cost)

    # 7) 会话级费用同样两种口径
    s = {"cost": None}
    S._note_session_cost(s, 0.5)
    S._note_session_cost(s, 0.8)
    check("会话费用累计口径", abs(s["cost"] - 0.8) < 1e-9, s["cost"])
    s2 = {"cost": 0.5}
    S._note_session_cost(s2, 0.3)
    check("会话费用增量口径", abs(s2["cost"] - 0.8) < 1e-9, s2["cost"])


def t_history():
    """共享库里的 compaction 行 → 面板形状（以前整条丢掉）。"""
    tmp = tempfile.mkdtemp(prefix="cmp_")
    db = os.path.join(tmp, "oc.db")
    con = sqlite3.connect(db)
    con.execute("create table session_message (id text, session_id text, type text, seq int, "
                "time_created int, data text)")
    con.execute("insert into session_message values (?,?,?,?,?,?)",
                ("m1", "s1", "compaction", 1, 1700000000000,
                 json.dumps({"time": {"created": 1700000000000}, "status": "completed",
                             "reason": "auto", "summary": "## Objective\nx",
                             "tokens": {"input": 1, "output": 2}, "cost": 0})))
    con.execute("insert into session_message values (?,?,?,?,?,?)",
                ("m2", "s1", "user", 2, 1700000001000,
                 json.dumps({"time": {"created": 1700000001000}, "text": "hi"})))
    con.execute("insert into session_message values (?,?,?,?,?,?)",
                ("m3", "s1", "assistant", 3, 1700000002000,
                 json.dumps({"time": {"created": 1700000001000, "completed": 1700000002000},
                             "content": [], "finish": "error",
                             "error": {"type": "provider.quota",
                                       "message": "Insufficient Balance",
                                       "status": 402}})))
    con.commit()
    con.close()

    old = S._opencode_db
    S._opencode_db = lambda: db
    try:
        hist = S._opencode_history("s1")
    finally:
        S._opencode_db = old
    cps = [m for m in hist if m.get("type") == "compaction"]
    check("库里 compaction 行不再被丢掉", len(cps) == 1, hist)
    check("compaction 带 summary/status/reason",
          bool(cps) and cps[0].get("summary", "").startswith("## Objective")
          and cps[0].get("status") == "completed", cps)
    errs = [m for m in hist if m.get("type") == "assistant" and m.get("error")]
    check("库里失败消息的 error / finish 透传（前端才能画错误卡）",
          len(errs) == 1 and errs[0].get("finish") == "error"
          and (errs[0].get("error") or {}).get("status") == 402, errs)


def t_skills():
    """内置技能并入 + 磁盘同名覆盖。"""
    tmp = tempfile.mkdtemp(prefix="sk_")
    disk = os.path.join(tmp, ".opencode", "skills", "gap-skill")
    os.makedirs(disk)
    with open(os.path.join(disk, "SKILL.md"), "w", encoding="utf-8") as fh:
        fh.write("---\nname: 磁盘版\n---\n磁盘正文\n")

    def fake_builtin():
        return [
            {"id": "builtin-report", "name": "report", "description": "内置",
             "autoinvoke": True, "source": "builtin", "path": "/builtin/report",
             "content": "内置正文"},
            {"id": "gap-skill", "name": "内置版", "description": "会被磁盘覆盖",
             "autoinvoke": True, "source": "builtin", "path": "/builtin/gap-skill",
             "content": "内置正文"},
        ]

    old = SK.opencode_builtin_skills
    SK.opencode_builtin_skills = fake_builtin
    try:
        items = {it["id"]: it for it in SK.discover(tmp)}
    finally:
        SK.opencode_builtin_skills = old
    check("内置技能出现在清单里", "builtin-report" in items and items["builtin-report"]["source"] == "builtin")
    check("磁盘同名技能覆盖内置（source 不是 builtin）",
          "gap-skill" in items and items["gap-skill"]["source"] != "builtin",
          items.get("gap-skill"))


def t_skill_resume():
    """resume=True：留 skill 消息 + 正文走 pendingSkills + 用户消息只留短指令。"""
    svc = object.__new__(S.AcpService)
    import itertools
    svc.agent_id = "mock"
    svc._lock = threading.Lock()
    svc._id = itertools.count(1)
    svc._sessions = {"s1": {"id": "s1", "agent": "mock", "messages": [], "time": {}}}
    svc._order = ["s1"]
    svc._save = lambda: None
    sent = []
    svc.prompt = lambda sid, text, files=None: sent.append(text) or {"ok": True}

    res = svc.inject_skill("s1", {"id": "demo", "name": "演示技能", "content": "正文很长" * 50},
                           resume=True)
    sess = svc._sessions["s1"]
    kinds = [m.get("type") for m in sess["messages"]]
    pend = sess.get("pendingSkills") or []
    check("resume=True 仍先留一条 skill 消息", kinds[:1] == ["skill"], kinds)
    check("技能正文进 pendingSkills（随本轮前置注入）",
          len(pend) == 1 and "正文很长" in pend[0]["text"], pend)
    check("resume=True 的用户消息是短指令（不是整段正文）",
          len(sent) == 1 and len(sent[0]) < 80 and "已激活技能" in sent[0], sent)
    check("返回结果标记已 resume", res.get("resumed") is True, res)


def t_models():
    """模型元数据：capabilities 用 agent 真宣告；价格表没配 = null。"""
    svc = object.__new__(S.AcpService)
    svc.provider_id = "deepseek"
    svc._config = {"model": {"options": [
        {"value": "deepseek-flash", "name": "DeepSeek Flash"},
        {"value": "deepseek-chat", "name": "DeepSeek Chat"},
    ]}, "effort": {"options": [{"value": "high", "name": "High"}]}}
    svc._prompt_caps = lambda: {"image": True, "embeddedContext": False}

    tmp = tempfile.mkdtemp(prefix="price_")
    pf = os.path.join(tmp, "prices.json")
    with open(pf, "w", encoding="utf-8") as fh:
        json.dump({"deepseek/deepseek-flash": {"input": 0.28, "output": 0.42,
                                               "cache": {"read": 0.028, "write": 0}}}, fh)
    old = S._PRICE_FILE
    S._PRICE_FILE = pf
    try:
        rows = {m["id"]: m for m in svc.models()}
    finally:
        S._PRICE_FILE = old

    flash = rows.get("deepseek-flash") or {}
    chat = rows.get("deepseek-chat") or {}
    check("模型 capabilities 跟着 agent 宣告（image=True / embeddedContext=False）",
          (flash.get("capabilities") or {}).get("image") is True
          and (flash.get("capabilities") or {}).get("embeddedContext") is False,
          flash.get("capabilities"))
    check("价格表命中的模型带 cost[]",
          isinstance(flash.get("cost"), list) and abs(flash["cost"][0]["input"] - 0.28) < 1e-9,
          flash.get("cost"))
    check("价格表没配的模型 cost=null（不谎报免费）", chat.get("cost") is None, chat.get("cost"))
    check("variants 来自 effort", [v["id"] for v in (flash.get("variants") or [])] == ["high"],
          flash.get("variants"))


def t_price_off():
    """没有任何配置文件 → cost 一律 null。"""
    svc = object.__new__(S.AcpService)
    svc.provider_id = "acp"
    svc._config = {"model": {"options": [{"value": "x"}]}}
    svc._prompt_caps = lambda: {}
    old = S._PRICE_FILE
    S._PRICE_FILE = os.path.join(tempfile.mkdtemp(prefix="noprice_"), "none.json")
    try:
        rows = svc.models()
    finally:
        S._PRICE_FILE = old
    check("没价格表 → 全部 cost=null", rows and rows[0].get("cost") is None, rows)


def t_delete():
    """删除：**delete 优先**、失败才 close；接续列表的删除走 session/delete。

    以前顺序反了（close 成功就 return）→ agent 侧那会话永远删不掉，
    用户看到"删了还在（接续列表里又冒出来）"（2026-11-05 反馈）。
    """
    def mk(ok):
        calls = []

        class C:
            def request(self, method, params, timeout=0):
                calls.append(method)
                if method in ok:
                    return {}
                raise RuntimeError("unsupported")

        svc = object.__new__(S.AcpService)
        svc.agent_id = "mock"
        svc._client_or_none = lambda: C()
        svc._agent_sid = lambda sid: sid
        svc._log = lambda *a, **k: None
        return svc, calls

    svc, calls = mk({"session/delete"})
    check("删除优先走 session/delete",
          svc.close_agent_session("s1") == "delete" and calls == ["session/delete"], calls)

    svc, calls = mk({"session/close"})
    check("delete 不行才 close，并如实回报 close",
          svc.close_agent_session("s1") == "close"
          and calls == ["session/delete", "session/close"], calls)

    svc, calls = mk(set())
    check("两个都不行 → 回报 none",
          svc.close_agent_session("s1") == "" and len(calls) == 2, calls)

    svc, calls = mk({"session/delete"})
    check("delete_remote 走 session/delete", svc.delete_remote("r1") is True
          and calls == ["session/delete"], calls)

    svc, calls = mk(set())
    try:
        svc.delete_remote("r1")
        ok = False
    except RuntimeError as exc:
        ok = "不支持" in str(exc)
    check("delete_remote 失败给人话（不支持）", ok)


def t_remote_route():
    """POST /api/session/remote/delete 路由存在且回 200（前端「接续」里的 × 用它）。"""
    from engines.acp import engine as E
    eng = object.__new__(E.AcpEngine)
    svc = object.__new__(S.AcpService)
    svc.delete_remote = lambda rid: True
    eng.service = lambda: svc

    class H:
        path = "/api/session/remote/delete"

        def _read_json_body(self):
            return {"id": "x"}

        def _send_json(self, code, body):
            self.code, self.body = code, body

    h = H()
    eng.handle_api(h, "POST")
    check("POST /api/session/remote/delete → 200",
          getattr(h, "code", None) == 200, getattr(h, "body", None))


def main() -> int:
    print("=== ACP 补齐项回归（压缩 / 费用 / 内置技能 / 技能 resume / 模型元数据 / 删除）===")
    t_turn()
    t_history()
    t_skills()
    t_skill_resume()
    t_models()
    t_price_off()
    t_delete()
    t_remote_route()
    bad = [n for n, ok in RESULTS if not ok]
    print("\n  total %d, failed %d" % (len(RESULTS), len(bad)))
    for n in bad:
        print("  [BAD] " + n)
    print("  result: " + ("PASS" if not bad else "FAIL"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
