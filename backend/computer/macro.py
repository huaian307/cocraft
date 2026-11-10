"""cu macro —— 把一次跑通的动作序列存成宏，下次回放（不花模型、10 秒级）。

为什么：GUI 任务慢的根因是「步数 × 每步模型往返」（≥100 步 × 6–9s ≈ 10–15 分钟，见
`docs/任务加速-方案.md` §1）。录制回放把同一类任务从 100 步压到 1 步。

口径（方案 §4.2 / `docs/工作流沉淀-技能-方案.md`）：
  · **录制 = 读审计链**（`source=cu`）。审计里已有每步工具名 / 控件名 / 窗口 / 结果；
    正文只有长度 + sha256 —— 所以输入文本天然变成参数，密码/正文不会写进宏。
  · **不存 hwnd 数值**（每次都会变），存 `title_like` / `name` / `role` / `selector`，回放时重新解析。
  · **回放失败即停** + 明确说"宏可能过期"，绝不静默半执行。
  · **宏不能自己提权**：含危险动作（发送/支付/删除…）或回车类快捷键的宏，回放必须每次
    显式 `--allow-danger`；每一步仍然走动作闸门 / 黑名单 / 提权检查。
  · 从 `run.members[].activities` 提取**不可行**（只留最近 100 条、且只有工具标题、没有命令原文），
    所以统一读审计链（带 `ref.run` / `ref.member`，可以按某一轮筛）。
"""

import copy
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from . import gate  # noqa: E402

STATE_DIR = os.path.join(ROOT, "runtime", "state")
MACROS_FILE = os.path.join(STATE_DIR, "_cu_macros.json")

MAX_STEPS_DEFAULT = 40          # 一条宏最多多少步（防一条命令跑飞）
MAX_RECORDS_DEFAULT = 400       # 从审计里最多读多少条

_LOCK = threading.RLock()

# 工具名（审计 action.command 的第二个词）→ 宏里的 op
_TOOL_MAP = {
    "click": "click",
    "type": "type",
    "keys": "keys",
    "wait": "wait",
    "browser_open": "browser.open",
    "browser_go": "browser.go",
    "browser_click": "browser.click",
    "browser_type": "browser.type",
}

# 这些工具的步骤需要"找控件 / 找选择器"
_NEEDS_LOCATOR = {"click", "type", "wait"}

# 参数名：按控件名猜（认不出就 p1..pN）。长的放前面（"收件人地址" 先于 "收件人"）。
PARAM_ALIASES = [
    ("收件人地址", "to"), ("收件人邮箱", "to"), ("收件人", "to"), ("to", "to"),
    ("抄送", "cc"), ("cc", "cc"), ("密送", "bcc"), ("bcc", "bcc"),
    ("主题", "subject"), ("subject", "subject"), ("标题", "subject"),
    ("正文", "body"), ("内容", "body"), ("body", "body"),
    ("密码", "password"), ("password", "password"),
    ("账号", "account"), ("用户名", "account"), ("account", "account"),
    ("搜索框", "query"), ("搜索", "query"), ("query", "query"), ("search", "query"),
    ("附件", "attach"), ("attach", "attach"),
]

# 回车类快捷键 = "发出去"（邮件/消息/表单提交）。审计里看不出是不是发送，所以一律当危险。
DANGER_KEYS = {"ENTER", "RETURN", "CTRL+ENTER", "CTRL+RETURN", "ALT+S", "CTRL+RETURN"}

_TITLE_SPLIT = re.compile(r"\s+[-–—|]\s+")
_LOC_RE = re.compile(r"\bloc=(\{[^{}]*\})")
_ELEMENT_RE = re.compile(r"element='([^']*)'")
_ROLE_RE = re.compile(r"\brole=(\w+)")
_KEYS_RE = re.compile(r"\bkeys=([^\s|]+)")
_TEXT_LEN_RE = re.compile(r"\btext len=(\d+)")
_WIN_RE = re.compile(r"hwnd=(\S+)\s+process=(\S*)\s+class=(\S*)")
_PLACE_RE = re.compile(r"\{\{\s*([A-Za-z_][\w-]*)\s*\}\}")


class MacroError(ValueError):
    """宏相关的可读错误（回放失败 / 找不到 / 缺参数）。"""


# ---------------------------------------------------------------- 存储

def path():
    return os.environ.get("COCRAFT_CU_MACROS_FILE") or MACROS_FILE


def _load():
    try:
        with open(path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(data):
    p = path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def get(name):
    name = str(name or "").strip()
    if not name:
        raise MacroError("需要宏名")
    data = _load()
    m = data.get(name)
    if not isinstance(m, dict):
        raise MacroError("没有这个宏：%s（先用 cu macro list 看看）" % name)
    return m


def put(name, macro):
    name = str(name or "").strip()
    if not name:
        raise MacroError("需要宏名")
    with _LOCK:
        data = _load()
        data[name] = macro
        if not _save(data):
            raise MacroError("写宏文件失败：%s" % path())
    return macro


def remove(name):
    name = str(name or "").strip()
    with _LOCK:
        data = _load()
        if name not in data:
            return False
        data.pop(name, None)
        _save(data)
        return True


def list_rows():
    """列表（不含 steps 正文，省 token）。"""
    out = []
    for name, m in sorted(_load().items()):
        if not isinstance(m, dict):
            continue
        stats = m.get("stats") or {}
        out.append({
            "name": name,
            "steps": len(m.get("steps") or []),
            "params": list(m.get("params") or []),
            "danger": list(m.get("danger") or []),
            "source": (m.get("source") or {}).get("run") or (m.get("source") or {}).get("kind") or "",
            "created": m.get("created") or "",
            "plays": int(stats.get("plays") or 0),
            "lastOk": bool(stats.get("last_ok")),
            "lastError": str(stats.get("last_error") or "")[:120],
            "lastMs": int(stats.get("last_ms") or 0),
            # 「可能过期」= 上次回放失败过（界面改版 / 元素改名）
            "stale": bool(stats.get("plays") and not stats.get("last_ok")),
        })
    return out


def summary():
    rows = list_rows()
    return {
        "count": len(rows),
        "stale": sum(1 for r in rows if r.get("stale")),
        "names": [r["name"] for r in rows],
        "file": path(),
    }


# ---------------------------------------------------------------- 从审计链提取

def parse_since(text):
    """`30m` / `2h` / `90s` / `2026-10-27T09:00` / 时间戳 → 秒级时间戳（0 = 不限）。"""
    s = str(text or "").strip()
    if not s:
        return 0.0
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|d)$", s, re.I)
    if m:
        n = float(m.group(1))
        unit = (m.group(2) or "m").lower()
        mult = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}[unit]
        return max(0.0, time.time() - n * mult)
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(s, fmt))
        except Exception:
            continue
    try:
        return float(s)
    except Exception:
        raise MacroError("看不懂的时间：%r（用 30m / 2h / 2026-10-27T09:00 或时间戳）" % text)


def _tool_of(rec):
    cmd = str((rec.get("action") or {}).get("command") or "")
    toks = cmd.split()
    if len(toks) >= 2:
        return toks[1].lower()
    title = str((rec.get("action") or {}).get("title") or "")
    return title.split(" · ", 1)[0].strip().lower()


def _window_of(rec):
    title = str((rec.get("action") or {}).get("title") or "")
    if " · " in title:
        title = title.split(" · ", 1)[1]
    title = title.strip().rstrip("*").strip()
    note = str(rec.get("note") or "")
    proc = ""
    m = _WIN_RE.search(note)
    if m:
        proc = m.group(2) or ""
    return {"title_like": title, "title_words": title_words(title), "process": proc}


def title_words(title):
    """把 "写邮件 - QQ邮箱" 拆成候选：**最后一段（通常是应用名）先试**，其余按长度降序。

    Windows 的标题惯例是「文档 - 应用」；回退时先试应用名更可能命中同一个应用。
    """
    t = str(title or "").strip().rstrip("*").strip()
    if not t:
        return []
    parts = [p.strip() for p in _TITLE_SPLIT.split(t) if p and p.strip()]
    if len(parts) <= 1:
        return []
    out = []
    for p in parts:
        if 2 <= len(p) <= 40 and p != t and p not in out:
            out.append(p)
    if out:
        last = out[-1]
        out = [last] + sorted(out[:-1], key=len, reverse=True)
    return out


def _locator_of(rec):
    """优先读 `note` 里的 loc={...}（新记录）；老记录退回 element='X' / role=Y。"""
    note = str(rec.get("note") or "")
    out = {}
    m = _LOC_RE.search(note)
    if m:
        try:
            d = json.loads(m.group(1))
            if isinstance(d, dict):
                out = {str(k): str(v) for k, v in d.items() if v}
        except Exception:
            out = {}
    if not out:
        e = _ELEMENT_RE.search(note)
        if e:
            out["n"] = e.group(1)
        r = _ROLE_RE.search(note)
        if r:
            out["r"] = r.group(1)
    return out


def _param_for(element, params, hints):
    name = str(element or "")
    low = name.lower()
    for alias, key in PARAM_ALIASES:
        if alias.lower() in low:
            if key not in params:
                params.append(key)
            return key
    # 认不出：p1、p2…（同一个控件复用同一个参数）
    for key, hint in hints.items():
        if key.startswith("p") and str((hint or {}).get("element") or "") == name and name:
            return key
    n = 1
    while ("p%d" % n) in params:
        n += 1
    key = "p%d" % n
    params.append(key)
    return key


def _step_from(tool, rec, params, hints):
    op = _TOOL_MAP.get(tool)
    if not op:
        return None
    note = str(rec.get("note") or "")
    loc = _locator_of(rec)
    targets = [str(x) for x in ((rec.get("action") or {}).get("targets") or []) if x]
    step = {"op": op}

    if op in ("click", "type", "wait"):
        if loc.get("n"):
            step["name"] = loc["n"]
        if loc.get("a"):
            step["auto_id"] = loc["a"]
        if loc.get("r"):
            step["role"] = loc["r"]
        if loc.get("c"):
            step["class_like"] = loc["c"]
        if not any(k in step for k in ("name", "auto_id", "class_like")):
            return None                      # 定位信息不全 → 这一步没法回放，丢掉
        if op == "type":
            key = _param_for(step.get("name") or step.get("auto_id") or step.get("class_like"), params, hints)
            step["text"] = "{{%s}}" % key
            hint = hints.setdefault(key, {})
            if step.get("name") and not hint.get("element"):
                hint["element"] = step["name"]
            m = _TEXT_LEN_RE.search(note)
            if m and "len" not in hint:
                hint["len"] = int(m.group(1))
            # 回放时默认"先全选再输入"：同一张表单重填不该追加（可在宏里改成 false）
            step["replace"] = True
    elif op == "keys":
        m = _KEYS_RE.search(note)
        if not m:
            return None
        step["keys"] = m.group(1)
    elif op == "browser.click":
        if not targets:
            return None
        step["selector"] = targets[0]
    elif op == "browser.type":
        if not targets:
            return None
        step["selector"] = targets[0]
        key = _param_for(targets[0], params, hints)
        step["text"] = "{{%s}}" % key
        hints.setdefault(key, {})["selector"] = targets[0]
    elif op in ("browser.open", "browser.go"):
        if not targets:
            return None
        step["url"] = targets[0]
    return step


def danger_scan(steps):
    out = []
    for st in steps:
        hay = " ".join(str(st.get(k) or "") for k in ("name", "auto_id", "class_like"))
        word = gate.danger_word(hay)
        if word:
            out.append(word)
        keys = str(st.get("keys") or "").upper().replace(" ", "")
        if keys in DANGER_KEYS:
            out.append(keys)
    return sorted(set(out))


def _same_step(a, b):
    keys = ("op", "name", "auto_id", "class_like", "selector", "keys", "url")
    return all(str(a.get(k) or "") == str(b.get(k) or "") for k in keys)


def extract(since=0.0, run="", member="", limit=MAX_RECORDS_DEFAULT,
            max_steps=MAX_STEPS_DEFAULT, drop_dupes=False, window=""):
    """从审计链提取宏（按时间 / 按某一轮 run）。**只取成功的动作类步骤。**"""
    try:
        import audit as audit_mod
    except Exception as exc:  # noqa: BLE001
        raise MacroError("读不到审计链（%s）" % exc)

    recs = audit_mod.records(limit=max(1, int(limit or MAX_RECORDS_DEFAULT)),
                             since=since or 0.0,
                             source="cu", kind="cu",
                             run=run or None, member=member or None)
    recs = list(reversed(recs))                     # 新→旧 反过来变成时间正序
    params, hints, steps = [], {}, []
    first_win = None
    used = 0
    seen_actions = 0
    for rec in recs:
        if str(rec.get("result") or "") != "ok":
            continue
        if str(rec.get("decision") or "") not in ("", "allowed", "auto"):
            continue
        seen_actions += 1
        tool = _tool_of(rec)
        step = _step_from(tool, rec, params, hints)
        if not step:
            continue
        win = _window_of(rec)
        if win.get("title_like"):
            if first_win is None:
                first_win = win
            # 只有换窗口了才写（省体积，也让"同一窗口"一目了然）
            last_win = ""
            for prev in reversed(steps):
                if prev.get("win"):
                    last_win = prev["win"].get("title_like") or ""
                    break
            if win["title_like"] != last_win:
                step["win"] = {k: v for k, v in win.items() if v}
        if drop_dupes and steps and _same_step(steps[-1], step):
            continue
        steps.append(step)
        used += 1
        if max_steps and used >= int(max_steps):
            break

    if not steps:
        raise MacroError("这段时间里没有可回放的动作（只有只读操作 / 全部被拒）。"
                         "先按目标窗口跑一遍 click/type/keys，再来 save。")

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    tgt = first_win or {"title_like": str(window or "")}
    if window:
        tgt = {"title_like": str(window), "title_words": title_words(str(window)),
               "process": (first_win or {}).get("process") or ""}
    macro = {
        "v": 1,
        "created": now,
        "updated": now,
        "source": {"kind": "audit", "since": float(since or 0), "run": run or "",
                   "member": member or "", "records": len(recs), "actions": seen_actions},
        "target": {"kind": "desktop" if first_win else "any",
                   "match": {k: v for k, v in (tgt or {}).items() if v}},
        "params": list(params),
        "param_hints": hints,
        "steps": steps,
        "danger": danger_scan(steps),
        "stats": {"plays": 0, "last_ok": False, "last_played": "", "last_error": "", "last_ms": 0},
    }
    return macro


# ---------------------------------------------------------------- 回放

def render(macro, params):
    """把 `{{param}}` 替换成实际值；返回 (steps, missing)。"""
    params = {str(k): v for k, v in (params or {}).items()}
    steps = copy.deepcopy(macro.get("steps") or [])
    missing = []

    def sub(text):
        def one(m):
            key = m.group(1)
            if key not in params:
                missing.append(key)
                return m.group(0)
            return str(params[key])
        return _PLACE_RE.sub(one, text)

    for st in steps:
        for k, v in list(st.items()):
            if isinstance(v, str):
                st[k] = sub(v)
    return steps, sorted(set(missing))


def resolve_window(cu, match, prev_hwnd=None):
    """按 title_like → title_words → process → 上一步的 hwnd 依次找窗口。返回 hwnd 或 0。"""
    match = match or {}
    title = str(match.get("title_like") or "")
    if title:
        hit = cu.winapi.find_window(title=title)
        if hit:
            return int(hit["hwnd"])
    for word in (match.get("title_words") or []):
        hit = cu.winapi.find_window(title=str(word))
        if hit:
            return int(hit["hwnd"])
    proc = str(match.get("process") or "").lower()
    if proc:
        for w in cu.winapi.list_windows(limit=60):
            if proc in str(w.get("process") or "").lower():
                return int(w["hwnd"])
    if prev_hwnd and cu.winapi.is_window(prev_hwnd):
        return int(prev_hwnd)
    return 0


def expired_hint(macro, step_no=None):
    """回放失败时的提示：说清"可能过期"并给出重新探索的命令。"""
    tgt = ((macro.get("target") or {}).get("match") or {}).get("title_like") or ""
    where = ("第 %s 步" % step_no) if step_no else "某一步"
    bits = ["宏可能过期：%s失败（界面改版 / 元素改名 / 窗口没开）。" % where]
    if tgt:
        bits.append("先 `cu windows --title %s` 确认窗口在，" % tgt.split(" - ")[0])
    bits.append("再 `cu tree --hwnd N --prune` / `cu find --hwnd N --like 关键字` 重新拿到控件名，"
                "改宏（cu macro show <名字>）或让 agent 用探索模式重跑一遍，再 cu macro save --force。")
    return "".join(bits)


def touch_stats(name, ok, ms=0, error=""):
    """回放后更新统计（失败会标「可能过期」）。"""
    with _LOCK:
        data = _load()
        m = data.get(name)
        if not isinstance(m, dict):
            return
        stats = m.setdefault("stats", {})
        stats["plays"] = int(stats.get("plays") or 0) + 1
        stats["last_ok"] = bool(ok)
        stats["last_played"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        stats["last_error"] = str(error or "")[:200]
        stats["last_ms"] = int(ms or 0)
        m["updated"] = stats["last_played"]
        _save(data)
