"""把桌面动作写进审计链（backend/audit.py，source="cu"）。

口径（和 docs/审计-方案.md 一致）：
  · 记工具名 / 窗口标题·进程 / 控件名·角色 / 坐标 / 结果；
  · **输入文本不记正文**：只记长度 + sha256 前 12 位 + 前 40 字（可审计但不泄漏）；
  · 截图只记路径，不把图片内容写进链；
  · 任何异常都吞掉（fail-open）—— 审计坏了不能挡 agent 干活。
"""

import hashlib
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))


def _digest_text(text):
    if not text:
        return ""
    raw = str(text).encode("utf-8", "replace")
    # 只记长度 + 哈希（正文一个字都不进链）——要复核就让对方把候选文本哈希一遍比对
    return "text len=%d sha256=%s" % (len(text), hashlib.sha256(raw).hexdigest()[:16])


def actor():
    """从环境变量还原"谁在动鼠标"（orchestrator / 群聊桥会注入）。"""
    return {
        "kind": os.environ.get("COCRAFT_CU_ACTOR_KIND") or "agent",
        "id": os.environ.get("COCRAFT_CU_ACTOR") or "local-agent",
        "name": os.environ.get("COCRAFT_CU_ACTOR_NAME") or os.environ.get("COCRAFT_CU_ACTOR") or "",
        "member": os.environ.get("COCRAFT_CU_MEMBER") or "",
        "engine": os.environ.get("COCRAFT_CU_ENGINE") or "",
    }


def record(tool, ok, window=None, action_kind="read", decision="", result="",
           targets=None, text=None, note="", extra=None, via=None):
    """写一条 kind=cu 的审计。失败静默。"""
    try:
        import audit as audit_mod
    except Exception:
        return None
    win = window or {}
    act = {
        "kind": "cu",
        "title": "%s · %s" % (tool, win.get("title") or ""),
        "command": (extra or {}).get("command", "") or ("cu " + str(tool)),
        "targets": list(targets or []) or [x for x in (
            win.get("process"), win.get("title"), win.get("class")) if x],
        "destructive": bool(action_kind == "action" and not ok and decision == "denied"),
    }
    bits = []
    if win.get("hwnd"):
        bits.append("hwnd=%s process=%s class=%s" % (win.get("hwnd"), win.get("process"),
                                                     win.get("class")))
    if note:
        bits.append(note)
    if text is not None:
        bits.append(_digest_text(text))
    if extra:
        for k, v in extra.items():
            if k == "command" or v in (None, ""):
                continue
            bits.append("%s=%s" % (k, v))
    ref = {}
    run = os.environ.get("COCRAFT_CU_RUN")
    if run:
        ref["run"] = run
        ref["member"] = os.environ.get("COCRAFT_CU_MEMBER") or ""
    try:
        return audit_mod.append(
            source="cu",
            actor=actor(),
            action=act,
            decision=decision or ("allowed" if ok else "failed"),
            result="ok" if ok else "error",
            ref=ref or None,
            note=" | ".join(bits)[:480],
            via=via or ("ui" if os.environ.get("X-Cocraft-UI") else "system"),
        )
    except Exception:
        return None
