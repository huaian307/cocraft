# -*- coding: utf-8 -*-
"""把面板里 ACP 的一条本地会话，与它在 agent 侧（opencode 共享库）的同一条历史**合并成一条**。

背景：面板给 ACP 侧用了本地 id（如 `acp_r1`），而 agent 侧是 `ses_…` —— 于是同一个会话在
「opencode 引擎」与「acp 引擎」里看着像两条。本脚本把它**归一成 agent 侧的 id**，并把两边消息
按时间合并去重，之后两个引擎看到的就是**同一条会话**。

用法（改文件前先 dry-run）：
    python tools\\merge_acp_session.py --list
    python tools\\merge_acp_session.py --local-id acp_r1 --dry-run
    python tools\\merge_acp_session.py --local-id acp_r1

选项：
    --rename-only   只把 id 归一，不并入库历史
    --keep-local-all 不与库历史去重，直接用本地那份（等价上面）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATE = os.path.join(ROOT, "runtime", "state")
SESSIONS_FILE = os.path.join(STATE, "_acp_sessions.json")


def _load():
    with open(SESSIONS_FILE, "r", encoding="utf-8") as fh:
        d = json.load(fh)
    if isinstance(d, dict):
        return d, d.get("sessions") or []
    return {"sessions": d}, d


def _save(doc: dict) -> None:
    os.makedirs(STATE, exist_ok=True)
    try:
        if os.path.isfile(SESSIONS_FILE):
            shutil.copy2(SESSIONS_FILE, SESSIONS_FILE + ".bak")
    except Exception:  # noqa: BLE001
        pass
    tmp = SESSIONS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, SESSIONS_FILE)


def _text_of(m: dict) -> str:
    """消息的"可比文本"：用户取 text，助手取 content 里的 text 部分。"""
    if not isinstance(m, dict):
        return ""
    if m.get("type") == "assistant":
        return "".join(str(p.get("text") or "") for p in (m.get("content") or [])
                       if isinstance(p, dict) and p.get("type") == "text")
    return str(m.get("text") or "")


def _key(m: dict) -> tuple:
    t = m.get("time") or {}
    created = t.get("created") or 0
    try:
        bucket = int(created) // 2000            # 2 秒一桶，容忍轻微时间差
    except Exception:  # noqa: BLE001
        bucket = 0
    return (str(m.get("type") or ""), _text_of(m).strip(), bucket)


def _merged(local: list, remote: list) -> list:
    """本地为准，补上"远程里本地没有的"，最后按时间升序。"""
    out = list(local)
    seen = {_key(m) for m in local}
    for m in remote:
        k = _key(m)
        if k in seen:
            continue
        seen.add(k)
        out.append(m)
    out.sort(key=lambda m: (m.get("time") or {}).get("created") or 0)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local-id", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--list", action="store_true", help="只列出会话")
    ap.add_argument("--mode", choices=("rename", "remote", "union"), default="rename",
                    help="rename=只归一 id、消息用本地那份（默认，推荐）；"
                         "remote=用 opencode 库历史替换本地；union=两边并集(类型+文本去重)")
    ap.add_argument("--rename-only", action="store_true", help="（保留兼容）等价 --mode rename")
    args = ap.parse_args()
    if args.rename_only:
        args.mode = "rename"

    if not os.path.isfile(SESSIONS_FILE):
        print("[BAD] 找不到 %s" % SESSIONS_FILE)
        return 1
    doc, sessions = _load()

    if args.list:
        for s in sessions:
            print("  id=%-40s agent=%-12s agentSessionId=%-38s msgs=%d  title=%s"
                  % (s.get("id"), s.get("agent"), s.get("agentSessionId"),
                     len(s.get("messages") or []), (s.get("title") or "")[:24]))
        return 0

    if not args.local_id:
        print("[BAD] 需要 --local-id（或用 --list 看有哪些）")
        return 1

    idx = next((i for i, s in enumerate(sessions) if str(s.get("id")) == args.local_id), -1)
    if idx < 0:
        print("[BAD] 没找到本地会话 id=%s" % args.local_id)
        return 1
    s = sessions[idx]
    agent_sid = str(s.get("agentSessionId") or "").strip()
    if not agent_sid:
        print("[BAD] 这条会话没有 agentSessionId（不是从 agent 侧来的，归不了 id）")
        return 1
    if agent_sid == args.local_id:
        print("[i] id 已经是 agent 侧 id，无需合并")
        return 0
    if any(str(x.get("id")) == agent_sid for j, x in enumerate(sessions) if j != idx):
        print("[BAD] 列表里已经有一条 id=%s 了（先处理它）" % agent_sid)
        return 1

    local_msgs = list(s.get("messages") or [])
    remote = []
    if args.mode != "rename":
        try:
            sys.path.insert(0, os.path.join(ROOT, "backend"))
            from engines.acp.service import _opencode_history     # noqa: PLC0415
            remote = _opencode_history(agent_sid) or []
        except Exception as exc:  # noqa: BLE001
            print("[!] 读 opencode 历史失败（%s: %s），退回 rename" % (type(exc).__name__, exc))
            remote = []
    if args.mode == "remote" and remote:
        merged = list(remote)            # ⚠ 库那边是 opencode 的"分步"记录
    elif args.mode == "union" and remote:
        merged = _merged(local_msgs, remote)
    else:
        merged = list(local_msgs)        # 默认：以面板本地那份为准

    print("模式     : %s" % args.mode)
    print("本地记录 : id=%s  msgs=%d" % (args.local_id, len(local_msgs)))
    if remote:
        print("opencode : id=%s  读到 %d 条" % (agent_sid, len(remote)))
    print("合并结果 : %d 条" % len(merged))
    uniq = len({_key(m) for m in merged})
    print("唯一消息 : %d 条  %s" % (uniq, "OK" if uniq == len(merged) else "⚠ 仍有重复键"))
    t0 = (merged[0].get("time") or {}).get("created") if merged else 0
    t1 = (merged[-1].get("time") or {}).get("created") if merged else 0
    print("时间范围 : %s .. %s" % (t0, t1))

    if args.dry_run:
        print("\n[dry-run] 未写入。确认无误后去掉 --dry-run 再跑。")
        return 0

    s["messages"] = merged
    s["id"] = agent_sid
    s["agentSessionId"] = agent_sid
    _save(doc)
    print("\n[OK] 已归一：%s -> %s（备份 %s.bak）" % (args.local_id, agent_sid, os.path.basename(SESSIONS_FILE)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
