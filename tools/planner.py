#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""planner -- 策划会话 CLI（统一 JSON 输出，给策划 agent 用）。

用法：
  python tools/planner.py session create --title "..." --cwd D:\\proj [--goal "..."]
  python tools/planner.py session get --id <sid>
  python tools/planner.py session list [--cwd D:\\proj]
  python tools/planner.py plan get --id <sid>
  python tools/planner.py plan set --id <sid> --file plan.json [--confirm]
  python tools/planner.py plan set --id <sid> --json '{...}' [--confirm]
  python tools/planner.py run start --id <sid> [--note "..."]
  python tools/planner.py status --id <sid>
  python tools/planner.py report --id <sid>
  python tools/planner.py cancel --id <sid>

环境变量：
  COCRAFT_PLANNER_TOKEN   策划会话 token（session create 返回；后续调用必需）
  COCRAFT_PLANNER_SESSION 当前策划会话 id（可代替 --id）
  COCRAFT_PANEL_URL       面板地址（默认 http://127.0.0.1:8787）

约定：stdout 只有一行 JSON；失败时 {"ok":false,"error":"..."} + 退出码 1。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def _force_utf8():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def emit(obj, pretty=False):
    if pretty:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def die(msg, code=1, **extra):
    out = {"ok": False, "error": str(msg)}
    out.update(extra)
    emit(out)
    raise SystemExit(code)


def panel_url():
    return str(os.environ.get("COCRAFT_PANEL_URL") or "http://127.0.0.1:8787").rstrip("/")


def token():
    return str(os.environ.get("COCRAFT_PLANNER_TOKEN") or "").strip()


def session_id(args):
    sid = getattr(args, "id", None) or os.environ.get("COCRAFT_PLANNER_SESSION") or ""
    return str(sid).strip()


def _headers(sid=None, tok=None):
    h = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    t = tok if tok is not None else token()
    if t:
        h["X-Planner-Token"] = t
    s = sid or ""
    if s:
        h["X-Planner-Session"] = s
    return h


def request(method, path, body=None, sid=None, tok=None, timeout=60):
    url = panel_url() + path
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method.upper(), headers=_headers(sid=sid, tok=tok))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ct = (resp.headers.get("Content-Type") or "").lower()
            if "json" in ct or raw[:1] in (b"{", b"["):
                try:
                    return json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    return {"ok": False, "error": "响应不是合法 JSON", "raw": raw.decode("utf-8", "replace")[:500]}
            return {"ok": True, "text": raw.decode("utf-8", "replace")}
    except urllib.error.HTTPError as e:
        raw = e.read() or b""
        try:
            obj = json.loads(raw.decode("utf-8", "replace") or "{}")
        except Exception:
            obj = {"error": raw.decode("utf-8", "replace")[:500] or str(e)}
        if "ok" not in obj:
            obj["ok"] = False
        obj.setdefault("status", e.code)
        return obj
    except urllib.error.URLError as e:
        return {"ok": False, "error": "无法连接面板 %s：%s（请确认 cocraft 面板已启动）" % (panel_url(), e.reason)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def need_sid(args):
    sid = session_id(args)
    if not sid:
        die("缺少会话 id：传 --id 或设置 COCRAFT_PLANNER_SESSION")
    return sid


# ---- commands ----

def cmd_session_create(args):
    body = {"title": args.title or "", "cwd": args.cwd or os.getcwd(), "goal": args.goal or "",
            "plannerAgent": args.agent or ""}
    res = request("POST", "/planner/session", body=body, tok=token() or None)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_session_get(args):
    sid = need_sid(args)
    res = request("GET", "/planner/session?id=" + urllib.parse.quote(sid), sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") or res.get("session") else 1)


def cmd_session_list(args):
    q = "/planner/sessions"
    params = {}
    if args.cwd:
        params["cwd"] = args.cwd
    if args.limit:
        params["limit"] = str(args.limit)
    if params:
        q += "?" + urllib.parse.urlencode(params)
    res = request("GET", q)
    emit(res)
    raise SystemExit(0 if res.get("ok") or isinstance(res.get("sessions"), list) else 1)


def cmd_plan_get(args):
    sid = need_sid(args)
    res = request("GET", "/planner/plan?id=" + urllib.parse.quote(sid), sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") or res.get("plan") else 1)


def _load_plan_input(args):
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception as e:
            die("读取 plan 文件失败：%s" % e)
    if args.json:
        try:
            return json.loads(args.json)
        except Exception as e:
            die("解析 --json 失败：%s" % e)
    # read stdin if piped
    if not sys.stdin.isatty():
        try:
            raw = sys.stdin.read()
            if raw.strip():
                return json.loads(raw)
        except Exception as e:
            die("解析 stdin JSON 失败：%s" % e)
    die("请提供 plan：--file plan.json 或 --json '...' 或 stdin")


def cmd_plan_set(args):
    sid = need_sid(args)
    plan = _load_plan_input(args)
    body = {"id": sid, "plan": plan, "confirm": bool(args.confirm)}
    if args.expect_version is not None:
        body["expectVersion"] = args.expect_version
    res = request("POST", "/planner/plan", body=body, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_run_start(args):
    sid = need_sid(args)
    body = {"id": sid, "note": args.note or ""}
    res = request("POST", "/planner/run/start", body=body, sid=sid, timeout=max(60, int(args.timeout or 60)))
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_status(args):
    sid = need_sid(args)
    res = request("GET", "/planner/status?id=" + urllib.parse.quote(sid), sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") or res.get("session") else 1)


def cmd_report(args):
    sid = need_sid(args)
    res = request("GET", "/planner/report?id=" + urllib.parse.quote(sid), sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") or res.get("markdown") or res.get("report") else 1)


def cmd_cancel(args):
    sid = need_sid(args)
    res = request("POST", "/planner/run/cancel", body={"id": sid}, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


# ---- P0.5 handoff ----

def cmd_handoff_show(args):
    sid = need_sid(args)
    path = "/planner/handoff?id=" + urllib.parse.quote(sid)
    if args.context:
        path += "&context=1"
    res = request("GET", path, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") or res.get("markdown") or res.get("text") else 1)


def cmd_handoff_write(args):
    sid = need_sid(args)
    res = request("POST", "/planner/handoff", body={"id": sid, "reason": args.reason or "manual", "note": args.note or "", "toAgent": args.to or ""}, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_agent_switch(args):
    sid = need_sid(args)
    if not args.to:
        die("缺少 --to <agentId>")
    res = request("POST", "/planner/switch-agent", body={"id": sid, "agent": args.to, "note": args.note or ""}, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_handoff_auto(args):
    sid = need_sid(args)
    res = request("POST", "/planner/handoff/auto", body={"id": sid, "used": args.used, "size": args.size, "threshold": args.threshold, "note": args.note or ""}, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_recover(args):
    sid = session_id(args)  # optional
    body = {"id": sid} if sid else {}
    tok = token() if sid else None
    res = request("POST", "/planner/recover", body=body, sid=sid or None, tok=tok)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_bind(args):
    sid = need_sid(args)
    if not args.chat:
        die("缺少 --chat <chatSessionId>")
    res = request("POST", "/planner/bind", body={"id": sid, "chatSessionId": args.chat, "engine": args.engine or ""}, sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def cmd_resume(args):
    sid = need_sid(args)
    res = request("GET", "/planner/resume?id=" + urllib.parse.quote(sid), sid=sid)
    emit(res)
    raise SystemExit(0 if res.get("ok") else 1)


def build_parser():
    ap = argparse.ArgumentParser(prog="planner", description="cocraft 策划会话 CLI（JSON 输出）")
    sp = ap.add_subparsers(dest="cmd", required=True)

    # session
    ssp = sp.add_parser("session", help="策划会话")
    ssp_sp = ssp.add_subparsers(dest="session_cmd", required=True)
    c = ssp_sp.add_parser("create", help="创建策划会话")
    c.add_argument("--title", default="")
    c.add_argument("--cwd", default=os.getcwd())
    c.add_argument("--goal", default="")
    c.add_argument("--agent", default="", help="策划 agent id（记录用）")
    c.set_defaults(func=cmd_session_create)
    c = ssp_sp.add_parser("get", help="获取会话")
    c.add_argument("--id", default="")
    c.set_defaults(func=cmd_session_get)
    c = ssp_sp.add_parser("list", help="列出会话")
    c.add_argument("--cwd", default="")
    c.add_argument("--limit", type=int, default=30)
    c.set_defaults(func=cmd_session_list)

    # plan
    pp = sp.add_parser("plan", help="计划")
    pp_sp = pp.add_subparsers(dest="plan_cmd", required=True)
    c = pp_sp.add_parser("get", help="读取计划与校验结果")
    c.add_argument("--id", default="")
    c.set_defaults(func=cmd_plan_get)
    c = pp_sp.add_parser("set", help="写入计划（--file/--json/stdin）")
    c.add_argument("--id", default="")
    c.add_argument("--file", default="")
    c.add_argument("--json", default="")
    c.add_argument("--confirm", action="store_true", help="校验通过则标记 confirmed")
    c.add_argument("--expect-version", type=int, default=None)
    c.set_defaults(func=cmd_plan_set)

    # run/status/report/cancel
    c = sp.add_parser("run", help="运行")
    run_sp = c.add_subparsers(dest="run_cmd", required=True)
    r = run_sp.add_parser("start", help="按计划启动执行")
    r.add_argument("--id", default="")
    r.add_argument("--note", default="")
    r.add_argument("--timeout", type=int, default=60)
    r.set_defaults(func=cmd_run_start)

    c = sp.add_parser("status", help="会话/计划/运行状态")
    c.add_argument("--id", default="")
    c.set_defaults(func=cmd_status)

    c = sp.add_parser("report", help="生成完成报告")
    c.add_argument("--id", default="")
    c.set_defaults(func=cmd_report)

    c = sp.add_parser("cancel", help="取消当前运行")
    c.add_argument("--id", default="")
    c.set_defaults(func=cmd_cancel)

    # handoff (P0.5)
    h = sp.add_parser("handoff", help="接力包 HANDOFF.md")
    hsp = h.add_subparsers(dest="handoff_cmd", required=True)
    hc = hsp.add_parser("show", help="读取 HANDOFF.md / 注入上下文")
    hc.add_argument("--id", default="")
    hc.add_argument("--context", action="store_true", help="输出注入用上下文（HANDOFF + plan 摘要）")
    hc.set_defaults(func=cmd_handoff_show)
    hc = hsp.add_parser("write", help="生成并写入 HANDOFF.md")
    hc.add_argument("--id", default="")
    hc.add_argument("--reason", default="manual")
    hc.add_argument("--note", default="")
    hc.add_argument("--to", default="", help="接棒 agent（可选）")
    hc.set_defaults(func=cmd_handoff_write)
    hc = hsp.add_parser("auto", help="上下文阈值触发自动交接（幂等）")
    hc.add_argument("--id", default="")
    hc.add_argument("--used", type=float, required=True)
    hc.add_argument("--size", type=float, required=True)
    hc.add_argument("--threshold", type=float, default=0.8)
    hc.add_argument("--note", default="")
    hc.set_defaults(func=cmd_handoff_auto)

    c = sp.add_parser("recover", help="崩溃/重启恢复：reconcile 策划会话的运行状态")
    c.add_argument("--id", default="", help="可选，只恢复指定会话")
    c.set_defaults(func=cmd_recover)

    c = sp.add_parser("bind", help="绑定引擎聊天会话（用于上下文自动交接钩子）")
    c.add_argument("--id", default="")
    c.add_argument("--chat", required=True, help="引擎聊天会话 id")
    c.add_argument("--engine", default="")
    c.set_defaults(func=cmd_bind)

    c = sp.add_parser("resume", help="接棒恢复：reconcile + 返回注入上下文（HANDOFF+plan 摘要）")
    c.add_argument("--id", default="")
    c.set_defaults(func=cmd_resume)

    ag = sp.add_parser("agent", help="策划 agent 管理")
    agsp = ag.add_subparsers(dest="agent_cmd", required=True)
    ac = agsp.add_parser("switch", help="换策划 agent（先写交接，再切换，返回能力复核+注入上下文）")
    ac.add_argument("--id", default="")
    ac.add_argument("--to", required=True, help="新策划 agent id")
    ac.add_argument("--note", default="")
    ac.set_defaults(func=cmd_agent_switch)

    return ap


def main(argv=None):
    _force_utf8()
    args = build_parser().parse_args(argv)
    try:
        args.func(args)
    except SystemExit:
        raise
    except Exception as e:
        die(str(e))


if __name__ == "__main__":
    main()
