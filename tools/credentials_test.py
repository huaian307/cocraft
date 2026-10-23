# -*- coding: utf-8 -*-
r"""离线回归：模型凭据（多 Key）—— backend/engines/acp/credentials.py

覆盖：
  * 旧 `baseline.env` 的密钥**迁移**进凭据表（非密钥变量留在 baseline）；
  * 同一家多个账号共存；默认选择；agent 级覆盖优先；
  * 列表只回掩码（绝不出明文）；删除默认后自动回退；
  * 改 env 时顺带迁移默认与 agent 覆盖；
  * 非法输入 / 未知 id；坏文件不崩；
  * `agents.effective_baseline()` 会带上凭据解析出的 env。

跑法：`python tools\credentials_test.py`（不联网、不碰真实 runtime/state）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

BACKEND = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from engines.acp import agents as ag          # noqa: E402
from engines.acp import credentials as cr     # noqa: E402

PASS = 0
FAIL = 0
BAD = []


def ok(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %s%s" % (label, (" " + extra) if extra else ""))
    else:
        FAIL += 1
        BAD.append(label)
        print("  [BAD]  %s%s" % (label, (" " + extra) if extra else ""))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="cocraft-cred-")
    real_cred = os.path.join(ag.ROOT_DIR, "runtime", "state", "_credentials.json")
    real_before = None
    try:
        with io.open(real_cred, "rb") as fh:
            real_before = fh.read()
    except OSError:
        real_before = None
    ag.STATE_DIR = tmp
    ag.AGENTS_FILE = os.path.join(tmp, "_acp_agents.json")
    ag.LEGACY_FILE = os.path.join(tmp, "_acp.json")
    ag.SECURITY_FILE = os.path.join(tmp, "_security.json")
    cr.STATE_DIR = tmp
    cr.CRED_FILE = os.path.join(tmp, "_credentials.json")

    reg = {
        "active": "a1",
        "baseline": {"cwd": "D:/ws", "mode": "read-only",
                      "env": {"DEEPSEEK_API_KEY": "sk-deepseek-AAA",
                              "ANTHROPIC_API_KEY": "sk-ant-BBB",
                              "OPENAI_BASE_URL": "https://example.invalid/v1"}},
        "agents": [
            {"id": "a1", "label": "codex", "command": ["cmd.exe"], "env": {}, "credentials": {}},
            {"id": "a2", "label": "claude", "command": ["cmd.exe"], "env": {}, "credentials": {}},
        ],
    }
    with io.open(ag.AGENTS_FILE, "w", encoding="utf-8") as fh:
        json.dump(reg, fh, ensure_ascii=False)

    try:
        print("== 1. 迁移：baseline.env 的密钥搬进凭据表 ==")
        pub = cr.list_public()
        items = pub.get("items") or []
        ok("迁出 2 条凭据（DeepSeek / Anthropic）",
           len(items) == 2 and {x["env"] for x in items} == {"DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"},
           "%r" % ([x["env"] for x in items],))
        ok("列表里没有明文（只有掩码）",
           all("sk-deepseek-AAA" not in json.dumps(x, ensure_ascii=False)
               and "sk-ant-BBB" not in json.dumps(x, ensure_ascii=False) for x in items),
           json.dumps(items, ensure_ascii=False)[:200])
        ok("掩码里带长度/首尾", all("…" in x["masked"] for x in items), repr(items[0]["masked"]))
        base_env = (ag.load_registry().get("baseline") or {}).get("env") or {}
        ok("baseline 里不再留密钥（否则删凭据会被旧值顶回来）",
           "DEEPSEEK_API_KEY" not in base_env and "ANTHROPIC_API_KEY" not in base_env,
           repr(base_env))
        ok("非密钥变量留在 baseline", base_env.get("OPENAI_BASE_URL") == "https://example.invalid/v1")
        ok("每个 env 都有默认", set((pub.get("defaults") or {}).keys()) ==
           {"DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"})

        print("\n== 2. 解析：默认 / agent 覆盖 ==")
        env = cr.env_for({})
        ok("默认解析出两个 Key", env.get("DEEPSEEK_API_KEY") == "sk-deepseek-AAA"
           and env.get("ANTHROPIC_API_KEY") == "sk-ant-BBB")
        ds = next(x for x in items if x["env"] == "DEEPSEEK_API_KEY")
        c1 = ds["id"]
        c2 = cr.add("DeepSeek · 公司", "DEEPSEEK_API_KEY", "sk-deepseek-CCC")["id"]
        ok("同一家可以有两个 Key（不再互相覆盖）", c2 != c1)
        ok("新加的不会自动抢默认", cr.env_for({}).get("DEEPSEEK_API_KEY") == "sk-deepseek-AAA")
        cr.set_default(c2)
        ok("设默认后默认解析用新的", cr.env_for({}).get("DEEPSEEK_API_KEY") == "sk-deepseek-CCC")
        env2 = cr.env_for({"credentials": {"DEEPSEEK_API_KEY": c1}})
        ok("agent 覆盖优先于默认", env2.get("DEEPSEEK_API_KEY") == "sk-deepseek-AAA")
        ok("覆盖只影响那一个 env", env2.get("ANTHROPIC_API_KEY") == "sk-ant-BBB")
        baseline = ag.effective_baseline({"id": "a1", "credentials": {"DEEPSEEK_API_KEY": c1}})
        ok("effective_baseline 会带上凭据 env",
           (baseline.get("env") or {}).get("DEEPSEEK_API_KEY") == "sk-deepseek-AAA"
           and (baseline.get("env") or {}).get("ANTHROPIC_API_KEY") == "sk-ant-BBB")

        print("\n== 3. 改 env：默认与 agent 覆盖一起迁移 ==")
        c3 = cr.add("DeepSeek · 备用", "DEEPSEEK_API_KEY", "sk-deepseek-DDD")["id"]
        cr.set_default(c3)                              # 先让它是默认，再改 env
        ag.update_agent("a2", {"credentials": {"DEEPSEEK_API_KEY": c3}})
        pub = cr.update(c3, {"env": "DEEPSEEK_KEY"})   # 改成另一个变量名
        ok("改 env 后默认指向跟着改",
           (pub.get("defaults") or {}).get("DEEPSEEK_KEY") == c3,
           repr(pub.get("defaults")))
        a2 = ag.find_agent(ag.load_registry(), "a2")
        ok("agent 覆盖的 env 键也一起改", (a2.get("credentials") or {}).get("DEEPSEEK_KEY") == c3,
           repr(a2.get("credentials")))

        print("\n== 4. 删除：默认自动回退 ==")
        cr.set_default(c2)
        after = cr.delete(c2)
        ok("删掉默认 → 同 env 的另一条接上",
           (after.get("defaults") or {}).get("DEEPSEEK_API_KEY") == c1,
           repr(after.get("defaults")))
        env3 = cr.env_for({})
        ok("删掉一条不影响另一条", env3.get("DEEPSEEK_API_KEY") == "sk-deepseek-AAA")
        cr.delete(c1)
        ok("删掉最后一条 → 该 env 不再注入", "DEEPSEEK_API_KEY" not in cr.env_for({}))
        cr.delete(c3)
        ok("删掉凭据时 agent 覆盖也清掉",
           "DEEPSEEK_KEY" not in (ag.find_agent(ag.load_registry(), "a2").get("credentials") or {}))

        print("\n== 5. 非法输入 / 坏文件 ==")
        for bad in ("", "1ABC", "A B", "_KEY", "A-B"):
            try:
                cr.add("x", bad, "sk-1")
                ok("非法 env 被拒：%r" % (bad or "(空)"), False)
            except ValueError:
                ok("非法 env 被拒：%r" % (bad or "(空)"), True)
        low = cr.add("x", "my_new_key", "sk-1")
        ok("小写 env 归一成大写（Windows 环境变量不区分大小写）",
           low.get("env") == "MY_NEW_KEY", low.get("env"))
        try:
            cr.add("x", "MY_KEY", "")
            ok("空 Key 被拒", False)
        except ValueError:
            ok("空 Key 被拒", True)
        try:
            cr.delete("nope")
            ok("未知 id 被拒", False)
        except KeyError:
            ok("未知 id 被拒", True)
        with io.open(cr.CRED_FILE, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        ok("坏文件不崩（当空表）", isinstance(cr.list_public(), dict))

        print("\n== 6. 不碰真实 runtime/state ==")
        real_after = None
        try:
            with io.open(real_cred, "rb") as fh:
                real_after = fh.read()
        except OSError:
            real_after = None
        ok("真实 _credentials.json 没有被测试改写", real_before == real_after,
           "before=%s after=%s" % (len(real_before or b""), len(real_after or b"")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%s  PASS=%d  FAIL=%d" % ("[FAIL]" if FAIL else "[PASS]", PASS, FAIL))
    for b in BAD:
        print("  - %s" % b)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
