# -*- coding: utf-8 -*-
r"""会话分开管理回归（工作流成员 / 群聊成员 / 单聊过滤 + 命名）。

覆盖：
  * 工作流成员会话名 = `<项目名> · <结点名>`；旧标题会被迁移；
  * 群聊成员会话名 = 群里创建时的名字（去掉 `群聊AI·`）；
  * `session_scope.managed_ids()` 能从桥 / 工作流 run 目录收集内部会话；
  * 单聊列表过滤依赖这个集合（前端/接口约定）。
"""
import io
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import orchestrator as orch          # noqa: E402
import cocraft_agents as ca          # noqa: E402
import session_scope as ss           # noqa: E402

N_PASS = 0
N_FAIL = 0


def check(name, cond, extra=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("PASS  %s" % name)
    else:
        N_FAIL += 1
        print("FAIL  %s%s" % (name, ("  | " + str(extra)) if extra else ""))


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)


def main():
    tmp = tempfile.mkdtemp(prefix="cocraft-sessmgr-")
    old = (orch.ROOT, orch.RUNS_DIR, orch.WORKFLOWS_FILE,
           ca.BRIDGE_SESSIONS_FILE, ca.list_agents, ss.BRIDGE_SESSIONS, ss.RUNS_DIR,
           dict(ss._CACHE), dict(ca._bridges))
    try:
        proj = os.path.join(tmp, "my-proj")
        os.makedirs(proj)
        runs = os.path.join(tmp, "runs")
        os.makedirs(runs)
        orch.ROOT = tmp
        orch.RUNS_DIR = runs
        orch.WORKFLOWS_FILE = os.path.join(tmp, "workflows.json")
        ca.BRIDGE_SESSIONS_FILE = os.path.join(tmp, "bridge.json")
        ss.BRIDGE_SESSIONS = ca.BRIDGE_SESSIONS_FILE
        ss.RUNS_DIR = runs
        ss._CACHE.update({"ts": 0.0, "ids": set()})
        ca._bridges.clear()

        wf = orch.save_workflow({"name": "测试流", "cwd": proj,
                                 "members": [{"id": "m1", "name": "impl", "engine": "mock"}],
                                 "stages": [{"id": "s1", "name": "实现"}]})
        wid = wf["id"]
        rid = "run_test"
        write_json(os.path.join(runs, rid + ".json"), {
            "id": rid, "workflowId": wid, "workflowName": "测试流", "cwd": proj,
            "task": "测试", "title": "测试轮", "status": "done", "created": 1,
            "members": [{"id": "m1", "name": "impl", "engine": "mock", "kind": "member",
                         "status": "done", "sessionID": "ses_wf_1"}],
        })
        write_json(os.path.join(runs, rid, "m1.sessions.json"), {
            "sessions": [{"id": "ses_wf_1", "agent": "mock", "agentSessionId": "ses_wf_1",
                          "title": "协作·impl", "messages": [{"x": 1}, {"x": 2}]}],
        })
        # 新命名：项目 basename · 结点名；旧标题迁移
        data = orch.list_member_sessions(wid)
        row = (data.get("items") or [{}])[0]
        check("工作流会话名 = 项目 · 结点名",
              row.get("title") == "my-proj · impl", row)
        check("工作流会话有消息条数", int(row.get("messageCount") or 0) == 2, row)
        with io.open(os.path.join(runs, rid, "m1.sessions.json"), encoding="utf-8") as fh:
            saved = json.load(fh)
        check("旧标题已迁移到会话文件",
              (saved.get("sessions") or [{}])[0].get("title") == "my-proj · impl", saved)

        # 群聊：标题改成群里创建时的名字
        ca.list_agents = lambda: [{"id": "11", "name": "yiyi", "engine": "mock",
                                    "roleLabel": "实现者", "chats": [3]}]
        ca._bridges["11"] = {"sid": "ses_cc_1"}
        write_json(ca.BRIDGE_SESSIONS_FILE, {"sessions": [
            {"id": "ses_cc_1", "agent": "mock", "agentSessionId": "ses_cc_1",
             "title": "群聊AI·yiyi", "messages": [{"x": 1}]},
        ]})
        g = ca.list_member_sessions(3)
        grow = (g.get("items") or [{}])[0]
        check("群聊会话名 = 群里创建时的名字", grow.get("title") == "yiyi", grow)
        with io.open(ca.BRIDGE_SESSIONS_FILE, encoding="utf-8") as fh:
            gsaved = json.load(fh)
        check("群聊旧标题已迁移",
              (gsaved.get("sessions") or [{}])[0].get("title") == "yiyi", gsaved)

        # 单聊过滤集合
        ids = ss.managed_ids()
        check("managed_ids 收工作流会话", "ses_wf_1" in ids, ids)
        check("managed_ids 收群聊会话", "ses_cc_1" in ids, ids)
    finally:
        (orch.ROOT, orch.RUNS_DIR, orch.WORKFLOWS_FILE,
         ca.BRIDGE_SESSIONS_FILE, ca.list_agents, ss.BRIDGE_SESSIONS, ss.RUNS_DIR,
         ss_cache, ca_bridges) = old
        ss._CACHE.update(ss_cache)
        ca._bridges.clear()
        ca._bridges.update(ca_bridges)
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
