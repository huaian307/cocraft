# -*- coding: utf-8 -*-
r"""内部会话识别（群聊 AI 成员 / 本地协作）离线回归。

盘古：agent 侧 `session/list` 会把**面板自己**创建的会话（群聊桥 / 编排 Runner）
和用户项目会话混在一起，导致「接续」列表几十条全是「【你的角色：…】」。
`AcpService.list_remote()` 现在默认隐藏它们；本测试盯住识别与过滤：

  1. 桥的 `_cocraft_bridge_sessions.json` 里的 id / agentSessionId；
  2. 本地协作 `_orchestrator/runs/<run>/<member>.sessions.json` 里的 id；
  3. 标题特征（角色提示词 / 群聊成员声明 / 群聊AI· / 协作· / 仲裁）；
  4. 15 秒缓存与失效；
  5. `list_remote` 默认过滤 + `include_internal=True` 全给并带标记。

完全离线，只写临时目录，不碰真实 runtime/state。
用法：python tools\internal_sessions_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

FAILS: list = []
PASSES = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASSES
    if ok:
        PASSES += 1
        print("  OK   %s" % name)
    else:
        FAILS.append(name)
        safe = str(extra)[:240].encode("ascii", "backslashreplace").decode("ascii")
        print("  FAIL %s %s" % (name, safe))


def write_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)


def main() -> int:
    from backend.engines.acp import service as svc  # noqa: E402

    tmp = tempfile.mkdtemp(prefix="cocraft-internal-")
    old_bridge = svc._BRIDGE_SESSIONS_FILE
    old_runs = svc._ORCH_RUNS_DIR
    old_cache = dict(svc._INTERNAL_IDS_CACHE)
    try:
        bridge_file = os.path.join(tmp, "bridge.json")
        runs_dir = os.path.join(tmp, "runs")
        svc._BRIDGE_SESSIONS_FILE = bridge_file
        svc._ORCH_RUNS_DIR = runs_dir
        svc._INTERNAL_IDS_CACHE["ts"] = 0.0
        svc._INTERNAL_IDS_CACHE["ids"] = set()

        print("[1] 从桥 / 编排 Runner 的落地文件收集 id")
        write_json(bridge_file, {"sessions": [
            {"id": "ses_bridge_a", "agentSessionId": "ses_bridge_a", "title": "群聊AI·yiyi"},
            {"id": "local_b", "agentSessionId": "ses_bridge_b", "title": "群聊AI·yiyi"},
        ]})
        write_json(os.path.join(runs_dir, "run1", "impl.sessions.json"), {"sessions": [
            {"id": "ses_orch_a", "agentSessionId": "ses_orch_a"},
            {"id": "ses_orch_b", "agentSessionId": "ses_orch_b"},
        ]})
        ids = svc._internal_session_ids()
        check("bridge id 被收集", "ses_bridge_a" in ids, str(sorted(ids)))
        check("bridge agentSessionId 被收集", "ses_bridge_b" in ids, str(sorted(ids)))
        check("orchestrator id 被收集", "ses_orch_a" in ids and "ses_orch_b" in ids,
              str(sorted(ids)))
        check("总数正确（5：id 与 agentSessionId 都收）", len(ids) == 5, str(sorted(ids)))

        print("[2] 标题特征（老的 / 文件已被清掉的内部会话）")
        for title in ("【你的角色：实现者】只回复 AA",
                      "你是群聊成员「yiyi」",
                      "【最近的群聊记录】\n...",
                      "群聊AI·yiyi",
                      "协作·impl",
                      "【仲裁】请评审上面这次派活的方案",
                      "你是本次任务的**审查者**"):
            check("标题命中: %s" % title[:14],
                  svc._is_internal_session("ses_unknown", title) is True, title)
        check("普通项目会话不误判",
              svc._is_internal_session("ses_normal", "整理共享工作区") is False)
        check("项目会话里提到「方案」不误判",
              svc._is_internal_session("ses_normal2", "帮我看下【方案】") is False)

        print("[3] 15 秒缓存 + 失效")
        write_json(os.path.join(runs_dir, "run2", "rev.sessions.json"),
                   {"sessions": [{"id": "ses_orch_c"}]})
        check("缓存期内看不到新文件", "ses_orch_c" not in svc._internal_session_ids())
        svc._INTERNAL_IDS_CACHE["ts"] = 0.0
        check("缓存失效后能看到", "ses_orch_c" in svc._internal_session_ids())

        print("[4] list_remote 默认过滤 + include_internal 全给")
        class FakeClient:
            agent_capabilities = {"sessionCapabilities": {"list": {}}}

            def request(self, method, params, timeout=120):
                assert method == "session/list"
                return {"sessions": [
                    {"sessionId": "p1", "cwd": "D:/x", "title": "整理共享工作区"},
                    {"sessionId": "p2", "cwd": "D:/x", "title": "项目二"},
                    {"sessionId": "ses_bridge_a", "cwd": "D:/x", "title": "看起来正常但 id 是内部的"},
                    {"sessionId": "ses_unknown", "cwd": "D:/x",
                     "title": "【你的角色：实现者】只回复 OK"},
                ], "nextCursor": None}

        obj = object.__new__(svc.AcpService)
        obj._lock = threading.RLock()
        obj._sessions = {}
        obj._order = []
        obj._cwd = ""
        obj._log = lambda *_a, **_k: None
        obj._owns = lambda s: True
        obj._is_opencode = lambda: False
        obj.ensure_started = lambda: FakeClient()
        obj._client_or_none = lambda: FakeClient()

        proj = obj.list_remote()
        names = [x["id"] for x in (proj.get("sessions") or [])]
        check("默认只给 2 条项目会话", names == ["p1", "p2"], str(names))
        check("total/internalCount 正确", proj.get("total") == 4
              and proj.get("internalCount") == 2,
              str((proj.get("total"), proj.get("internalCount"))))

        allr = obj.list_remote(include_internal=True)
        items = allr.get("sessions") or []
        check("include_internal 给 4 条", len(items) == 4, str(len(items)))
        flags = {x["id"]: bool(x.get("internal")) for x in items}
        check("内部标记只落在内部会话上",
              flags == {"p1": False, "p2": False,
                        "ses_bridge_a": True, "ses_unknown": True}, str(flags))

        print()
        if FAILS:
            print("FAILED: %d / %d" % (len(FAILS), PASSES + len(FAILS)))
            for f in FAILS:
                print("  - %s" % f)
            return 1
        print("ALL PASS: %d checks" % PASSES)
        return 0
    finally:
        svc._BRIDGE_SESSIONS_FILE = old_bridge
        svc._ORCH_RUNS_DIR = old_runs
        svc._INTERNAL_IDS_CACHE.clear()
        svc._INTERNAL_IDS_CACHE.update(old_cache)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
