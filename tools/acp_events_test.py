# -*- coding: utf-8 -*-
r"""ACP bridge headless regression: really launches backend/acp_mock_agent.py as the
agent, then asserts behaviour item by item.

Covers (added in the 2026-09-26 round):
  1. clientCapabilities declares `_meta.terminal_output_delta`
  2. plan / plan_update -> session.plan.update event + `plan` on the message
  3. usage_update (context window) -> session.usage.update + session.context
  4. tool_call(in_progress) + `_meta.terminal_output_delta` -> session.tool.progress,
     terminal completed -> session.tool.success; dict rawOutput must not become
     "[object Object]"
  5. usage in the session/prompt result -> session tokens really accumulate;
     stopReason -> outcome
  6. session_info_update auto-renames (and never overwrites a user-set title)
  7. permission "always allow" memory: popup once, then auto-allow the same
     session + same kind without a popup
  8. session/list + import_remote (idempotent)

Usage: python tools\acp_events_test.py
Output is pure ASCII on purpose (Windows console is GBK).
"""
from __future__ import annotations

import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

MOCK = os.path.join(HERE, "backend", "acp_mock_agent.py")
FAILS: list = []
PASSES = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASSES
    if ok:
        PASSES += 1
        print("  OK   %s" % name)
    else:
        FAILS.append(name)
        # Windows console is GBK -> keep the detail pure ASCII
        safe = str(extra)[:200].encode("ascii", "backslashreplace").decode("ascii")
        print("  FAIL %s  %s" % (name, safe))


def drain(q, until_types=None, timeout=20.0):
    """Collect events until one of `until_types` shows up (or timeout)."""
    got = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ev = q.get(timeout=max(0.1, deadline - time.time()))
        except Exception:  # noqa: BLE001
            break
        got.append(ev)
        if until_types and (ev.get("type") in until_types):
            break
    return got


def wait_perm(svc, sid, timeout=10.0):
    """Wait for a pending permission request (it arrives on a worker thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        ps = svc.list_permissions(sid)
        if ps:
            return ps
        time.sleep(0.1)
    return []


def wait_form(svc, sid, timeout=10.0):
    """Wait for a pending elicitation (form) request."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        fs = svc.list_forms(sid)
        if fs:
            return fs
        time.sleep(0.1)
    return []


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="acp_test_")
    from backend.engines.acp import service as svc_mod  # noqa: E402
    from backend.engines.acp.service import CLIENT_CAPABILITIES, AcpService  # noqa: E402

    # point both state files at a temp dir - never touch the real runtime/state
    svc_mod.STATE_DIR = tmp
    svc_mod._ALWAYS_FILE = os.path.join(tmp, "_acp_always.json")
    svc_mod.audit.set_file(os.path.join(tmp, "_audit.jsonl"))   # ★别写真实审计链

    got_all: list = []
    svc = AcpService([sys.executable, MOCK], cwd=tmp, env=dict(os.environ),
                     log=lambda s: None, agent_id="mock", provider_id="mock")
    q = svc.subscribe()
    orig_emit = svc._emit

    def spy(ev):
        got_all.append(ev)
        orig_emit(ev)
    svc._emit = spy

    print("[1] start + handshake")
    client = svc.ensure_started()
    caps = client.agent_capabilities or {}
    check("agent advertises sessionCapabilities.list",
          "list" in ((caps.get("sessionCapabilities") or {}) or {}))
    check("service.supports_session_list() accepts empty-dict capability",
          svc.supports_session_list() is True)
    check("CLIENT_CAPABILITIES declares _meta.terminal_output_delta",
          (CLIENT_CAPABILITIES.get("_meta") or {}).get("terminal_output_delta") is True)

    print("[2] create session + model list")
    s = svc.create_session(title=u"\u65b0\u4f1a\u8bdd")   # "new session"
    sid = s["id"]
    check("session created", bool(sid))
    check("model list has 2", len(svc.models()) == 2, str(svc.models()))
    check("default model = mock-fast", (svc.model_default() or {}).get("id") == "mock-fast")

    print("[3] turn 1: plan / usage / tool.progress / auto-rename")
    got_all.clear()
    svc.prompt(sid, "run the tests")
    got = drain(q, until_types=("permission.updated",), timeout=10)
    perms = wait_perm(svc, sid)
    check("permission popup raised", len(perms) == 1, str(perms)[:200])
    if perms:
        check("permission carries a reason", bool(perms[0].get("message")))
        check("permission carries model-thought background",
              u"\u80cc\u666f\uff08" in (perms[0].get("detail") or ""))   # "background ("
        svc.reply_permission(sid, perms[0]["id"], "once")
    got += drain(q, until_types=("form.updated",), timeout=15)
    forms = wait_form(svc, sid)
    check("form popup raised", len(forms) == 1, str(forms)[:200])
    if forms:
        svc.reply_form(sid, forms[0]["id"], {"strategy": "balanced"})
    got += drain(q, until_types=("session.execution.succeeded",), timeout=25)

    kinds = [e.get("type") for e in got]
    check("session.plan.update emitted", "session.plan.update" in kinds, str(kinds))
    check("session.usage.update emitted", "session.usage.update" in kinds, str(kinds))
    check("session.tool.called emitted", "session.tool.called" in kinds, str(kinds))
    check("session.tool.progress emitted", "session.tool.progress" in kinds, str(kinds))
    check("session.tool.success emitted", "session.tool.success" in kinds, str(kinds))
    check("session.text.ended emitted", "session.text.ended" in kinds, str(kinds))
    plan_ev = [e for e in got if e.get("type") == "session.plan.update"]
    check("plan markdown captured",
          bool((plan_ev[-1]["data"].get("plan") or {}).get("markdown")) if plan_ev else False,
          str(plan_ev[-1]["data"]) if plan_ev else "")
    prog = [e for e in got if e.get("type") == "session.tool.progress"]
    check("tool progress carries the delta text",
          bool(prog and "12 passed" in (prog[0]["data"].get("delta") or "")),
          str(prog[0]["data"]) if prog else "")

    msgs, _ = svc.messages(sid)
    tool_parts = [p for m in msgs for p in (m.get("content") or []) if p.get("type") == "tool"]
    check("tool status completed",
          bool(tool_parts) and tool_parts[0]["state"]["status"] == "completed",
          str(tool_parts[:1])[:200])
    out = (tool_parts[0]["state"].get("output") if tool_parts else "") or ""
    check("dict rawOutput became readable text",
          "12 passed" in out and "[object Object]" not in out, out[:160])
    check("exit code appended to output", "exit_code=0" in out, out[:240])
    check("message carries plan (not inside content)",
          any(m.get("plan") for m in msgs), str([m.get("type") for m in msgs]))

    sess = svc.get_session(sid)
    check("agent-provided title applied", sess["title"] == u"\u5047 agent \u7684\u6b63\u540d",
          sess["title"])
    tok = sess["tokens"]
    check("tokens accumulated from prompt usage",
          tok.get("input", 0) > 0 and tok.get("output", 0) > 0, str(tok))
    check("cached read tokens recorded", (tok.get("cache") or {}).get("read", 0) > 0, str(tok))
    check("context window usage recorded",
          (sess.get("context") or {}).get("used") == 12100, str(sess.get("context")))
    check("outcome = end_turn", sess.get("outcome") == "completed", str(sess.get("outcome")))
    check("cost still None (never faked)", sess.get("cost") is None, str(sess.get("cost")))

    print("[4] user-set title is never overwritten")
    svc.rename_session(sid, "my own name")
    check("manual title protected",
          svc._auto_title_ok({"title": "my own name",
                              "messages": [{"type": "user", "text": "run the tests"}]}, "x") is False)
    check("default title replaceable",
          svc._auto_title_ok({"title": u"\u65b0\u4f1a\u8bdd", "messages": []}, "x") is True)
    check("auto-named-from-question title replaceable",
          svc._auto_title_ok({"title": "run the tests",
                              "messages": [{"type": "user", "text": "run the tests"}]}, "x") is True)

    print("[5] permission always-allow memory")
    got_all.clear()
    svc.prompt(sid, "second turn")
    perms = wait_perm(svc, sid)
    check("second turn still pops up (nothing remembered yet)", len(perms) == 1, str(perms)[:200])
    if perms:
        svc.reply_permission(sid, perms[0]["id"], "always")
    f2 = wait_form(svc, sid, timeout=8)
    if f2:
        svc.reply_form(sid, f2[0]["id"], {"strategy": "balanced"})
    drain(q, until_types=("session.execution.succeeded",), timeout=25)
    rules = svc.always_rules(sid)
    check("one rule remembered", len(rules) == 1, str(rules)[:200])
    check("rule is execute scoped to this session",
          bool(rules) and rules[0].get("kind") == "execute" and rules[0].get("session") == sid,
          str(rules)[:200])

    got_all.clear()
    before = len(svc.list_permissions(sid))
    svc.prompt(sid, "third turn")
    time.sleep(1.5)
    after = svc.list_permissions(sid)
    check("same session+kind auto-allowed without popup", len(after) == before, str(after)[:200])
    auto = [e for e in got_all if e.get("type") == "permission.auto"]
    check("permission.auto event emitted", len(auto) >= 1, str(auto[-1:])[:200])
    check("rule hit counter bumped",
          svc.always_rules(sid) and svc.always_rules(sid)[0].get("hits", 0) >= 1,
          str(svc.always_rules(sid))[:200])
    f3 = wait_form(svc, sid, timeout=8)
    if f3:
        svc.reply_form(sid, f3[0]["id"], {"strategy": "balanced"})
    drain(q, until_types=("session.execution.succeeded",), timeout=25)

    print("[6] remote sessions: list + import")
    remote = svc.list_remote()
    check("session/list supported", remote.get("supported") is True, str(remote)[:200])
    check("project sessions only by default (2)", len(remote.get("sessions") or []) == 2,
          str(remote)[:300])
    check("internal count reported (1)", int(remote.get("internalCount") or 0) == 1,
          str((remote.get("total"), remote.get("internalCount"))))
    allr = svc.list_remote(include_internal=True)
    check("include_internal returns 3 with internal flag",
          len(allr.get("sessions") or []) == 3
          and all(("internal" in x) for x in (allr.get("sessions") or []))
          and sum(1 for x in (allr.get("sessions") or []) if x.get("internal")) == 1,
          str(allr)[:300])
    first = (remote.get("sessions") or [{}])[0]
    check("not-imported flagged", first.get("imported") is False, str(first)[:200])
    imported = svc.import_remote(first["id"])
    check("import ok", bool(imported.get("id")) and imported.get("imported") is True,
          str(imported)[:200])
    check("title taken from agent",
          imported.get("title") == u"\u6574\u7406\u5171\u4eab\u5de5\u4f5c\u533a",  # tidy shared workspace
          str(imported.get("title")))
    again = svc.import_remote(first["id"])
    check("re-import is idempotent", again.get("id") == imported.get("id"),
          "%s vs %s" % (again.get("id"), imported.get("id")))
    hit = [x for x in svc.list_remote()["sessions"] if x["id"] == first["id"]][0]
    check("list marks it imported", hit.get("imported") is True and hit.get("localID") == imported["id"],
          str(hit)[:200])
    svc.prompt(imported["id"], "continue please")
    time.sleep(1.0)
    check("imported session bound to the agent session",
          svc._agent_sid(imported["id"]) == first["id"], svc._agent_sid(imported["id"]))
    svc.interrupt(imported["id"])
    drain(q, until_types=("session.execution.succeeded", "session.error"), timeout=20)

    print("[7] running turn: 中途补充要求 → 排队，本轮结束自动投递")
    qenv = dict(os.environ)
    qenv["ACP_MOCK_SIMPLE"] = "1"
    qenv["ACP_MOCK_DELAY"] = "0.6"
    qsvc = AcpService([sys.executable, MOCK], cwd=tmp, env=qenv,
                      log=lambda s: None, agent_id="mockq", provider_id="mock")
    qev = []
    _qemit = qsvc._emit

    def _qspy(ev):
        qev.append(ev)
        _qemit(ev)
    qsvc._emit = _qspy
    qid = qsvc.create_session(title=u"queue")["id"]
    r1 = qsvc.prompt(qid, u"第一轮：先做基础工作")
    r2 = qsvc.prompt(qid, u"补充要求：再加一条")
    check("第一轮正常开跑", r1.get("ok") is True and not r1.get("queued"), str(r1))
    check("★跑着的时候发第二条 → 排队（不再 409）",
          r2.get("queued") is True and r2.get("position") == 1, str(r2))
    check("排队时发 session.inbox.enqueued",
          any(e.get("type") == "session.inbox.enqueued" for e in qev))
    _deadline = time.time() + 20
    while time.time() < _deadline:
        if [e.get("type") for e in qev].count("session.execution.succeeded") >= 2:
            break
        time.sleep(0.1)
    check("★第一轮结束后自动投递（session.inbox.delivered）",
          any(e.get("type") == "session.inbox.delivered" for e in qev))
    qmsgs, _ = qsvc.messages(qid, 50)       # 新 → 旧
    qusers = [m for m in qmsgs if m.get("type") == "user"]
    qadd = [m for m in qusers if u"补充要求" in (m.get("text") or "")]
    check("两条用户消息都在历史里", len(qusers) >= 2, len(qusers))
    check("★排队那条已被标成送达（queued 清掉、delivered=True）",
          bool(qadd) and not qadd[0].get("queued") and qadd[0].get("delivered") is True,
          str(qadd[:1])[:200])
    check("两轮都有助手回复", sum(1 for m in qmsgs if m.get("type") == "assistant") >= 2)
    check("★时间戳单调：排队那条晚于第一轮（不会排到前面）",
          len(qusers) >= 2 and ((qusers[0].get("time") or {}).get("created") or 0)
          > ((qusers[1].get("time") or {}).get("created") or 0))

    print("[7b] 队列上限")
    old_max = svc_mod.MAX_INBOX
    svc_mod.MAX_INBOX = 1
    q2env = dict(qenv)
    q2env["ACP_MOCK_DELAY"] = "0.8"
    q2 = AcpService([sys.executable, MOCK], cwd=tmp, env=q2env,
                    log=lambda s: None, agent_id="mockq2", provider_id="mock")
    q2id = q2.create_session(title=u"queue-limit")["id"]
    q2.prompt(q2id, u"开跑")
    q2.prompt(q2id, u"第一条补充")
    raised = False
    try:
        q2.prompt(q2id, u"第二条补充（应被拒）")
    except Exception as exc:  # noqa: BLE001
        raised = "排队已满" in str(exc)
    check("★排队超过上限 → 明确拒绝（不无限堆）", raised)
    time.sleep(1.6)
    q2.close()
    svc_mod.MAX_INBOX = old_max

    print("[8] 重启后：没发出去的排队消息标「未送达」（不自动跑）")
    qs = qsvc._sessions[qid]
    qs["messages"].append({"id": "u_undelivered_test", "type": "user",
                           "text": u"重启前没发出去", "queued": True,
                           "time": {"created": int(time.time() * 1000)}})
    qsvc._save()
    q3 = AcpService([sys.executable, MOCK], cwd=tmp, env=qenv,
                    log=lambda s: None, agent_id="mockq", provider_id="mock")
    s3 = q3._sessions.get(qid) or {}
    m3 = [m for m in (s3.get("messages") or []) if m.get("id") == "u_undelivered_test"]
    check("★载入后标成 undelivered、queued 清掉",
          bool(m3) and m3[0].get("undelivered") is True and not m3[0].get("queued"),
          str(m3[:1])[:200])
    check("重启不自动补跑（inbox 为空）", not (s3.get("inbox") or []),
          str(s3.get("inbox")))
    q3.close()
    qsvc.close()

    print("[9] cleanup")
    n = svc.forget_always()
    check("all memory cleared", n >= 1 and svc.always_rules() == [], "removed=%s" % n)
    svc.close()

    print("\ntotal %d, failed %d" % (PASSES + len(FAILS), len(FAILS)))
    if FAILS:
        print("failed list:")
        for f in FAILS:
            print("   -", f)
        return 1
    print("result: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
