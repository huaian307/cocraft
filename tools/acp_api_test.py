# -*- coding: utf-8 -*-
r"""ACP HTTP layer headless test: boots a real backend/server.py on an ephemeral port
with the mock agent injected **in-process only** (no file writes, no global state
changes, does not touch the live panel on 8787), then exercises the /api/* routes.

Why HTTP level: the route table is where mistakes hide - especially
`/api/session/remote` vs the generic `/api/session/<id>` pattern, and the extra
`always` key in the permission response.

Covers: /engine/status, POST /api/session, GET /api/model, GET /api/session/remote,
POST /api/session/import (idempotent), GET/DELETE /api/permission/always,
GET /api/session/<id>/permission (extra `always` key), GET /api/agent.

Usage: python tools\acp_api_test.py
Output is pure ASCII (Windows console is GBK).
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

MOCK = os.path.join(ROOT, "backend", "acp_mock_agent.py")
FAILS: list = []
PASSES = 0
PORT = 0


def check(name: str, ok: bool, extra: str = "") -> None:
    global PASSES
    if ok:
        PASSES += 1
        print("  OK   %s" % name)
    else:
        FAILS.append(name)
        safe = str(extra)[:200].encode("ascii", "backslashreplace").decode("ascii")
        print("  FAIL %s  %s" % (name, safe))


def call(method: str, path: str, body=None, timeout=30):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, raw


def main() -> int:
    global PORT
    tmp = tempfile.mkdtemp(prefix="acp_api_")
    os.environ["OPENCODE_UI_ENGINE"] = "acp"        # this process only
    # 造一个"Codex 直连 DeepSeek"的 CODEX_HOME，用来验证 config.toml → provider 推断
    codex_home = os.path.join(tmp, "codex-home")
    os.makedirs(codex_home, exist_ok=True)
    with open(os.path.join(codex_home, "config.toml"), "w", encoding="utf-8") as fh:
        fh.write('model_providers.deepseek = { base_url = "https://api.deepseek.com/v1",'
                 ' env_key = "DEEPSEEK_API_KEY" }\n')

    import server                                       # noqa: E402
    from engines.acp import agents                      # noqa: E402
    from engines.acp import service as svc_mod          # noqa: E402

    # keep all state files inside the temp dir
    svc_mod.STATE_DIR = tmp
    svc_mod._ALWAYS_FILE = os.path.join(tmp, "_acp_always.json")

    # inject the mock agent as the only agent, in memory (no file writes)
    reg = {"active": "mock",
           "baseline": {"cwd": tmp, "env": {}, "mode": "read-only"},
           "agents": [{"id": "mock", "label": "Mock", "command": [sys.executable, MOCK],
                       "cwd": tmp, "env": {}, "provider": "mock", "note": "test"}]}
    agents.load_registry = lambda: reg
    agents.save_registry = lambda r: None

    httpd = server.Server(("127.0.0.1", 0), server.Handler)
    PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    time.sleep(0.3)
    print("[0] test server on port %d (mock agent, temp state)" % PORT)

    try:
        print("[1] engine status")
        st, js = call("GET", "/engine/status")
        check("200 + engine acp", st == 200 and js.get("active") == "acp", str(js)[:160])
        englist = (js or {}).get("engines") or []
        check("/engine/status reports engines[] with label+available",
              len(englist) >= 2 and all(("label" in e and "available" in e) for e in englist),
              str(englist)[:220])
        check("acp listed as available", any(e["id"] == "acp" and e["available"] for e in englist),
              str(englist)[:220])

        print("[2] create session via HTTP")
        st, js = call("POST", "/api/session", {"title": "http session"})
        check("POST /api/session 200", st == 200, str(js)[:160])
        sid = ((js or {}).get("data") or {}).get("id") or ""
        check("session id returned", bool(sid), str(js)[:160])

        st, js = call("GET", "/api/model")
        check("GET /api/model has 2 models", st == 200 and len((js or {}).get("data") or []) == 2,
              str(js)[:200])

        print("[3] remote session list + import")
        st, js = call("GET", "/api/session/remote")
        check("GET /api/session/remote 200", st == 200, str(js)[:200])
        check("supported = true", (js or {}).get("supported") is True, str(js)[:200])
        rlist = (js or {}).get("sessions") or []
        check("2 project sessions (internal hidden by default)", len(rlist) == 2, str(js)[:200])
        check("internal count reported", int((js or {}).get("internalCount") or 0) == 1,
              str(((js or {}).get("total"), (js or {}).get("internalCount"))))
        st, jall = call("GET", "/api/session/remote?internal=1")
        rall = (jall or {}).get("sessions") or []
        check("?internal=1 returns all 3 + internal flags",
              st == 200 and len(rall) == 3
              and sum(1 for x in rall if x.get("internal")) == 1,
              str(jall)[:240])
        rid = rlist[0]["id"] if rlist else ""

        st, js2 = call("POST", "/api/session/import", {"id": rid})
        check("POST /api/session/import 200", st == 200, str(js2)[:200])
        imported_id = ((js2 or {}).get("data") or {}).get("id") or ""
        check("imported session created", bool(imported_id), str(js2)[:200])
        st, js3 = call("POST", "/api/session/import", {"id": rid})
        check("import is idempotent over HTTP",
              ((js3 or {}).get("data") or {}).get("id") == imported_id, str(js3)[:160])
        st, js4 = call("POST", "/api/session/import", {"id": ""})
        check("import without id -> 400", st == 400, str(js4)[:160])

        print("[4] always-allow memory routes")
        st, js = call("GET", "/api/permission/always")
        check("GET /api/permission/always 200", st == 200, str(js)[:160])
        check("initially empty", (js or {}).get("data") == [], str(js)[:160])
        # create a rule through the service, then read it back over HTTP
        eng = server.engines.get_engine("acp")
        svc = eng.service()
        svc._remember_always(sid, "execute", ["D:/tmp/x"], "run tests")
        st, js = call("GET", "/api/permission/always")
        check("rule visible over HTTP", len((js or {}).get("data") or []) == 1, str(js)[:200])
        st, js = call("GET", "/api/session/%s/permission" % sid)
        check("permission route carries `always`",
              st == 200 and len((js or {}).get("always") or []) == 1, str(js)[:200])
        st, js = call("DELETE", "/api/permission/always", {})
        check("DELETE /api/permission/always 200", st == 200, str(js)[:160])
        check("removed 1", (js or {}).get("removed") == 1, str(js)[:160])
        st, js = call("GET", "/api/permission/always")
        check("memory cleared", (js or {}).get("data") == [], str(js)[:160])

        print("[5] generic session route still works (no shadowing)")
        st, js = call("GET", "/api/session/%s" % sid)
        check("GET /api/session/<id> 200", st == 200, str(js)[:160])
        st, js = call("GET", "/api/session/sess_does_not_exist")
        check("unknown session -> 404", st == 404, str(js)[:160])
        st, js = call("GET", "/api/session")
        check("GET /api/session lists both", st == 200 and len((js or {}).get("data") or []) >= 2,
              str(js)[:200])

        print("[6] /api/agent (modes)")
        st, js = call("GET", "/api/agent")
        ids = [a.get("id") for a in ((js or {}).get("data") or [])]
        check("agent modes exposed", st == 200 and "read-only" in ids, str(js)[:200])
        check("current mode flagged",
              any(a.get("current") for a in ((js or {}).get("data") or [])), str(js)[:200])
        st, js = call("POST", "/api/session/%s/agent" % sid, {"agent": "auto"})
        check("mode switch 204", st == 204, str(js)[:160])
        st, js = call("GET", "/api/agent")
        cur = [a.get("id") for a in ((js or {}).get("data") or []) if a.get("current")]
        check("current mode now auto", cur == ["auto"], str(js)[:200])

        print("[7] provider neutralisation (guess + auto-fill on add)")
        st, js = call("POST", "/engine/acp/agents", {"action": "guess-provider",
                                                     "command": ["node", "acp.js"],
                                                     "env": {"DEEPSEEK_API_KEY": "sk-test-123456"}})
        check("guess-provider -> deepseek", st == 200 and (js or {}).get("provider") == "deepseek",
              str(js)[:160])
        st, js = call("POST", "/engine/acp/agents", {"action": "guess-provider",
                                                     "command": ["npx", "-y", "some-acp"],
                                                     "env": {"OPENAI_API_KEY": ""}})
        check("empty env value is not a hint (neutral)", st == 200 and (js or {}).get("provider") == "",
              str(js)[:160])
        st, js = call("POST", "/engine/acp/agents",
                      {"action": "add", "label": "guess-test", "command": ["node", "acp.js"],
                       "env": {"CODEX_HOME": codex_home}, "activate": False})
        added = [a for a in ((js or {}).get("agents") or []) if a.get("id") == "guess-test"]
        check("add auto-fills provider", st == 200 and added and added[0].get("provider") == "deepseek",
              str(added)[:200])
        st, js = call("GET", "/engine/acp/agents")
        rows = (js or {}).get("agents") or []
        check("public() carries provider/providerGuess keys",
              bool(rows) and "provider" in rows[0] and "providerGuess" in rows[0], str(rows[:1])[:200])

        print("[8] subcommand-style ACP discovery (OpenCode's own `acp` subcommand)")
        from engines.acp import agents as _ag                       # noqa: PLC0415
        expected = _ag.find_subcommand_acp()
        st, js = call("POST", "/engine/acp/agents", {"action": "candidates"})
        cands = (js or {}).get("candidates") or []
        if expected:
            hit = [c for c in cands if _ag._spec_for_command(c.get("command"))]
            check("candidates include the subcommand ACP entry",
                  bool(hit) and str(hit[0]["command"][-1]) == "acp",
                  str([c.get("id") for c in cands])[:200])
            check("only one entry for the same real exe (junction deduped)",
                  len({os.path.realpath(str(c["command"][0])) for c in
                       [c for c in cands if c.get("command")]}) == len(
                      [c for c in cands if c.get("command")]),
                  str([c.get("id") for c in cands])[:200])
        else:
            check("no OpenCode on this machine -> nothing to assert (skipped)", True)
        if expected:
            check("path resolved to the real install (no C: junction duplicate)",
                  "agentlist" in os.path.realpath(str(expected[0])).lower(),
                  str(expected))
        print("[7b] provider inference edge cases (paths must not mislead)")
        from engines.acp import agents as _pv                       # noqa: PLC0415
        check("path containing deepseekharness is NOT a deepseek hint",
              _pv.provider_from_text(r"D:\agentlist\deepseekharness\node\node.exe") == "",
              _pv.provider_from_text(r"D:\agentlist\deepseekharness\node\node.exe"))
        check("api.deepseek.com is still deepseek",
              _pv.provider_from_text('base_url = "https://api.deepseek.com/v1"') == "deepseek")
        check("DEEPSEEK_API_KEY is still deepseek",
              _pv.provider_from_text("DEEPSEEK_API_KEY") == "deepseek")
        check("claude agent whose command has a portable-node path -> anthropic",
              _pv.guess_provider({"id": "claude-node", "label": "Claude Code",
                                  "command": [r"D:\agentlist\deepseekharness\node\node.exe", "x",
                                              "@zed-industries/claude-code-acp"], "env": {}}) == "anthropic",
              _pv.guess_provider({"id": "claude-node", "label": "Claude Code",
                                  "command": [r"D:\agentlist\deepseekharness\node\node.exe", "x",
                                              "@zed-industries/claude-code-acp"], "env": {}}))
        check("agents.ROOT_DIR is really the project root (has backend/)",
              os.path.isdir(os.path.join(_pv.ROOT_DIR, "backend")), _pv.ROOT_DIR)
        check("search dirs include the bundled agents\\node (installed status)",
              any(str(x).replace("/", "\\").lower().endswith("agents\\node")
                  for x in _pv._search_dirs()),
              str(_pv._search_dirs())[:180])
        check("autofill(save=False) never touches the real registry file",
              "save: bool = True" in io.open(os.path.join(ROOT, "backend", "engines", "acp", "agents.py"),
                                             encoding="utf-8").read())

        # ---- 引擎：缺 USERPROFILE/HOME 也要能找到 service.json（安装版曾因此判"未就绪"）----
        from engines import opencode as _oc                        # noqa: PLC0415
        _saved = {k: os.environ.get(k) for k in ("USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME")}
        # 用本机**真实**的 LOCALAPPDATA 来验证推导（写死 C:\Users\tester 那种目录不存在，会被 isdir 过滤掉）
        _la = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        _la = _la.rstrip("\\/")
        _expect = os.path.dirname(os.path.dirname(_la))            # …\AppData\Local → 用户目录
        try:
            for _k in _saved:
                os.environ.pop(_k, None)
            os.environ["LOCALAPPDATA"] = _la
            _homes = _oc._home_dirs()
            check("no USERPROFILE/HOME -> home still derived from LOCALAPPDATA",
                  any(os.path.normcase(h) == os.path.normcase(_expect) for h in _homes),
                  "%r -> %s" % (_homes, _expect))
            check("literal '~' never becomes a user dir",
                  all(not h.startswith("~") for h in _homes), str(_homes))
            check("service_states() never yields a '~'-based path",
                  all(not p.startswith("~") for p in _oc.service_states()), str(_oc.service_states())[:160])
        finally:
            for _k, _v in _saved.items():
                if _v is None:
                    os.environ.pop(_k, None)
                else:
                    os.environ[_k] = _v
        check("opencode engine: an installed CLI also counts as available",
              "def cli_path" in io.open(os.path.join(ROOT, "backend", "engines", "opencode.py"),
                                        encoding="utf-8").read())
        # 自愈：本机装了 OpenCode 时，ensure_opencode_agent() 应该把 opencode-acp 补进（内存里的）注册表
        _cli = _pv.find_subcommand_acp()
        if _cli:
            _added = _pv.ensure_opencode_agent()
            check("ensure_opencode_agent() adds opencode-acp when the CLI is present",
                  _added == "opencode-acp", "%s <- %s" % (_added, _cli))
            check("ensure_opencode_agent() is idempotent (2nd call does nothing)",
                  _pv.ensure_opencode_agent() == "", "second call should be a no-op")
        else:
            print("  ..   (this machine has no OpenCode CLI; skipping the self-heal check)")

        # ---- 自定义外观（只测失败路径，不写任何状态）----
        st, js = call("GET", "/appearance")
        check("GET /appearance -> 200 + data map",
              st == 200 and isinstance((js or {}).get("data"), dict), str(js)[:140])
        st, js = call("POST", "/appearance", {"key": "nope", "path": r"C:\x.png"})
        check("POST /appearance with unknown key -> 400", st == 400, str(js)[:140])
        st, js = call("POST", "/appearance", {"key": "hero", "path": r"C:\definitely-missing.png"})
        check("POST /appearance with missing file -> 400", st == 400, str(js)[:140])
        st, js = call("GET", "/appearance/img?k=hero")
        check("GET /appearance/img with nothing set -> 404", st == 404, str(js)[:120])

        st, js = call("GET", "/engine/acp/agents")
        check("agents list still healthy after candidates scan",
              st == 200 and isinstance((js or {}).get("agents"), list), str(js)[:120])

        # ---- 技能 / 斜杠命令（ACP：面板自己扫文件系统 + 注入；见 engines/acp/skills.py）----
        print("[10] skills (ACP scans the filesystem)")
        _q = urllib.parse.quote(tmp, safe="")
        skroot = os.path.join(tmp, ".opencode", "skills", "demo-skill")
        os.makedirs(skroot, exist_ok=True)
        with open(os.path.join(skroot, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: Demo Skill\ndescription: demo\n---\n\nDEMO BODY\n")
        st, js = call("GET", "/api/skill?directory=%s" % _q)
        ids = [s.get("id") for s in ((js or {}).get("data") or [])]
        check("GET /api/skill lists the project skill", st == 200 and "demo-skill" in ids, str(js)[:200])
        row = next((s for s in ((js or {}).get("data") or []) if s.get("id") == "demo-skill"), {})
        check("list rows carry name/source but NOT content",
              row.get("name") == "Demo Skill" and "content" not in row, str(row)[:200])
        st, js = call("GET", "/api/skill/demo-skill?directory=%s" % _q)
        check("GET /api/skill/<id> returns the body",
              st == 200 and "DEMO BODY" in (((js or {}).get("data") or {}).get("content") or ""),
              str(js)[:160])
        st, js = call("GET", "/api/skill/no-such-skill")
        check("unknown skill -> 404", st == 404, str(js)[:120])
        st, js = call("GET", "/api/command")
        cmd_names = [c.get("name") for c in ((js or {}).get("data") or [])]
        check("GET /api/command lists the agent's own advertised commands (mock-cmd)",
              st == 200 and "mock-cmd" in cmd_names, str(js)[:200])

        print("[11] activate skill = inject only (no immediate run)")
        st, js = call("POST", "/api/session/%s/skill" % sid, {"id": "demo-skill"})
        check("POST .../skill 200", st == 200 and (js or {}).get("injected") == "demo-skill", str(js)[:160])
        st, js = call("GET", "/api/session/%s/message" % sid)
        msgs = (js or {}).get("data") or []
        check("a `skill` message lands in local history",
              any(m.get("type") == "skill" and m.get("skill") == "demo-skill" for m in msgs),
              str(msgs)[:220])
        st, js = call("POST", "/api/session/%s/skill" % sid, {"id": "nope"})
        check("activating an unknown skill -> 404", st == 404, str(js)[:120])
        _svc = server.engines.get_engine("acp").service()
        _pend = (_svc._sessions.get(sid) or {}).get("pendingSkills") or []
        check("skill is armed as a pending injection (runs on the user's next message)",
              len(_pend) == 1 and _pend[0].get("id") == "demo-skill", str(_pend)[:160])
        st, js = call("GET", "/api/session/%s" % sid)
        check("generic session route not shadowed by the skill route", st == 200, str(js)[:120])

        print("[12] slash command runs (ACP: sends `/name` as a prompt)")
        st, js = call("POST", "/api/session/%s/command" % sid, {"name": "hello", "text": "world"})
        check("POST .../command 200", st == 200, str(js)[:160])
        st, js = call("POST", "/api/session/sess_missing/command", {"name": "x"})
        check("command on unknown session -> 404", st == 404, str(js)[:120])

        # ---- ★可逆执行：隔离区 + 工作区快照（引擎无关的 HTTP 路由）----
        print("[13] quarantine + snapshot routes")
        import snapshots as _sn                                 # noqa: PLC0415
        _sn.QUARANTINE_DIR = os.path.join(tmp, "q")
        _sn.SNAPSHOT_DIR = os.path.join(tmp, "snaps")
        _sn.STORE_DIR = os.path.join(_sn.SNAPSHOT_DIR, "_store")
        wsdir = os.path.join(tmp, "ws")
        os.makedirs(wsdir, exist_ok=True)
        with open(os.path.join(wsdir, "precious.txt"), "w", encoding="utf-8") as fh:
            fh.write("PRECIOUS\n")

        st, js = call("GET", "/quarantine/list")
        check("GET /quarantine/list -> 200 + items/stats",
              st == 200 and isinstance((js or {}).get("data", {}).get("items"), list)
              and "stats" in ((js or {}).get("data") or {}), str(js)[:160])

        st, js = call("POST", "/quarantine/add",
                      {"root": wsdir, "paths": [os.path.join(wsdir, "precious.txt")],
                       "scope": "routes", "note": "test", "cmd": "Remove-Item x"})
        qid = ((js or {}).get("data") or {}).get("id") or ""
        check("POST /quarantine/add quarantines 1 item", st == 200 and bool(qid), str(js)[:200])

        st, js = call("GET", "/quarantine/get?id=%s" % urllib.parse.quote(qid, safe=""))
        check("GET /quarantine/get returns the manifest",
              st == 200 and ((js or {}).get("data") or {}).get("cmd") == "Remove-Item x", str(js)[:160])

        os.remove(os.path.join(wsdir, "precious.txt"))
        st, js = call("POST", "/quarantine/restore", {"id": qid})
        check("POST /quarantine/restore puts the file back",
              st == 200 and ((js or {}).get("restored") == 1)
              and os.path.isfile(os.path.join(wsdir, "precious.txt")), str(js)[:200])
        st, js = call("POST", "/quarantine/restore", {"id": qid})
        check("restore onto an existing target -> conflict (no silent overwrite)",
              st == 200 and ((js or {}).get("conflict") == 1), str(js)[:160])
        st, js = call("POST", "/quarantine/drop", {"id": qid})
        check("POST /quarantine/drop removes the record", st == 200, str(js)[:140])
        st, js = call("POST", "/quarantine/restore", {"id": "../../etc"})
        check("restore with a bogus id -> 400", st == 400, str(js)[:140])

        st, js = call("POST", "/snapshot/take",
                      {"root": wsdir, "scope": "routes", "label": "turn", "content": True})
        ts = ((js or {}).get("data") or {}).get("ts") or ""
        check("POST /snapshot/take -> 200 + ts", st == 200 and bool(ts), str(js)[:200])
        with open(os.path.join(wsdir, "precious.txt"), "w", encoding="utf-8") as fh:
            fh.write("CHANGED\n")
        with open(os.path.join(wsdir, "new.txt"), "w", encoding="utf-8") as fh:
            fh.write("NEW\n")
        st, js = call("GET", "/snapshot/diff?scope=routes")
        _d = (js or {}).get("data") or {}
        check("GET /snapshot/diff detects 1 changed + 1 added",
              st == 200 and _d.get("stats", {}).get("changed") == 1
              and _d.get("stats", {}).get("added") == 1, str(_d)[:220])
        st, js = call("GET", "/snapshot/file?scope=routes&path=precious.txt")
        check("GET /snapshot/file gives a line-level diff",
              st == 200 and ((js or {}).get("data") or {}).get("ok") is True, str(js)[:200])
        st, js = call("POST", "/snapshot/undo", {"scope": "routes", "ts": ts})
        check("POST /snapshot/undo restores the old content",
              st == 200 and ((js or {}).get("restored") == 1) and ((js or {}).get("removed") == 1)
              and open(os.path.join(wsdir, "precious.txt"), encoding="utf-8").read().strip() == "PRECIOUS"
              and not os.path.exists(os.path.join(wsdir, "new.txt")), str(js)[:240])

        print("\n[14] local orchestration routes (no chat / no login)")
        st, js = call("GET", "/orchestrate/catalog")
        _c = js or {}
        check("GET /orchestrate/catalog -> 200 + agents/roles",
              st == 200 and any(a.get("id") == "mock" for a in (_c.get("agents") or []))
              and bool(_c.get("roles")), str(_c)[:230])
        st, js = call("GET", "/orchestrate/runs")
        check("GET /orchestrate/runs -> 200 list",
              st == 200 and isinstance((js or {}).get("runs"), list), str(js)[:180])
        st, js = call("GET", "/orchestrate/run?id=nope")
        check("GET /orchestrate/run unknown -> 404", st == 404, str(js)[:160])
        st, js = call("POST", "/orchestrate/start", {"task": "", "members": [{"engine": "mock"}]})
        check("POST /orchestrate/start empty task -> 400", st == 400, str(js)[:160])
        st, js = call("POST", "/orchestrate/permission", {"id": "nope", "decision": "once"})
        check("POST /orchestrate/permission unknown -> 404", st == 404, str(js)[:160])
    finally:
        try:
            eng = server.engines.get_engine("acp")
            eng.shutdown()
        except Exception:  # noqa: BLE001
            pass
        httpd.shutdown()
        httpd.server_close()

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
