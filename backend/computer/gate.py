"""权限闸门 —— 动作类工具（click / type / keys / clipboard_set）放不放行。

默认 **deny**（fail-closed）：
  · 工作流里 `trust` 成员的 orchestrator 会注入 COCRAFT_CU_ACTIONS=allow；
  · 单聊 agent 默认没有这个环境变量 → 一律拒绝，并返回人话原因；
  · 想手动试用：`set COCRAFT_CU_ACTIONS=allow` 或 `cu click --allow-actions`。

即使 allow，下面这些仍然拒绝：
  · 提权窗口（非提权进程 SendInput 根本送不进去，硬试只会静默失败）；
  · 黑名单应用（密码管理器 / 银行 / 安全软件，可在 runtime/state/_cu.json 里改）；
  · "危险动作"关键词（发送 / 支付 / 删除 / 卸载 …）——V1 让 agent 换方式，别赌。
"""

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATE_DIR = os.path.join(ROOT, "runtime", "state")
CONFIG_FILE = os.path.join(STATE_DIR, "_cu.json")

DEFAULT_BLACKLIST = [
    "1password.exe", "keepass.exe", "keepassxc.exe", "bitwarden.exe",
    "lastpass.exe", "dashlane.exe", "roboform.exe", "enpass.exe",
    "authy.exe", "authenticator.exe",
    "bank", "网上银行", "招商银行", "工商银行", "支付宝", "alipay",
    "360se.exe", "huorong.exe", "hipsmain.exe", "avp.exe", "msmpeng.exe",
    "consent.exe", "logonui.exe", "useraccountcontrol",
]

# 命中即拒绝（V1）。宁可让 agent 换 API，也不要它"顺手点一下发送"。
DANGEROUS_WORDS = [
    "发送", "提交", "支付", "付款", "转账", "汇款", "购买", "下单", "删除", "卸载",
    "格式化", "注销", "退出登录", "发送并", "确认支付", "立即支付", "确认下单",
    "send", "submit", "pay now", "purchase", "delete", "uninstall", "format",
    "transfer", "confirm payment",
]


def _config():
    data = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    cfg = {
        "actions": os.environ.get("COCRAFT_CU_ACTIONS", "").strip().lower() or
                   str(data.get("actions") or "deny").strip().lower(),
        "blacklist": list(data.get("blacklist") or []) or list(DEFAULT_BLACKLIST),
        "dangerous_words": list(data.get("dangerous_words") or []) or list(DANGEROUS_WORDS),
        "allow_dangerous": bool(data.get("allow_dangerous")),
    }
    return cfg


def mode():
    return _config()["actions"]


def danger_word(text):
    """文本里命中危险词就返回那个词。"""
    if not text:
        return ""
    low = str(text).lower()
    for w in _config()["dangerous_words"]:
        if w.lower() in low:
            return w
    return ""


def blacklist_hit(process="", title=""):
    cfg = _config()
    hay = ("%s %s" % (process or "", title or "")).lower()
    for w in cfg["blacklist"]:
        if w and w.lower() in hay:
            return w
    return ""


def check_read(tool, window=None):
    """只读工具永远放行（但要入审计）。"""
    return True, ""


def check_action(tool, window=None, target=None, text=None, allow_actions=False):
    """返回 (allowed, reason)。window 是 winapi.describe() 的 dict。"""
    cfg = _config()
    if not allow_actions and cfg["actions"] != "allow":
        return False, ("桌面动作未获授权（当前 %s）。工作流里把该成员权限改成「信任」，"
                       "或单聊时设置里开「允许桌面操作」/ 临时设 COCRAFT_CU_ACTIONS=allow。"
                       % cfg["actions"])

    win = window or {}
    if win.get("elevated"):
        return False, "目标窗口属于提权进程，非提权进程无法可靠操作它（请手动操作）"

    hit = blacklist_hit(win.get("process"), win.get("title"))
    if hit:
        return False, "目标应用在桌面自动化黑名单里（%s），请手动操作" % hit

    if not cfg["allow_dangerous"]:
        word = danger_word(target) or danger_word(text)
        if word:
            return False, ("这次动作用到了「%s」，属于不可撤销或影响外部世界的操作，"
                           "V1 一律要人来做（或改用对应 API/CLI）" % word)
    return True, ""


def status():
    cfg = _config()
    return {
        "ok": True,
        "actions": cfg["actions"],
        "allow_dangerous": cfg["allow_dangerous"],
        "blacklist": len(cfg["blacklist"]),
        "dangerous_words": len(cfg["dangerous_words"]),
        "config_file": CONFIG_FILE,
        "config_exists": os.path.isfile(CONFIG_FILE),
        "env": {k: v for k, v in os.environ.items() if k.startswith("COCRAFT_CU")},
    }


# ---------------------------------------------------------------- ACP 侧识别
# ACP 协议没有"客户端自定义工具"，所以 agent 是通过 shell 调 `cu ...`。
# 权限层要认出这种命令：只读的别弹窗（否则一个 GUI 任务弹几十次），
# 动作类的也放行 —— 真正的闸门在 cu 进程里（env / 黑名单 / 危险词 / 审计）。

READ_ONLY_CMDS = {"windows", "describe", "tree", "find", "shot", "wait",
                  "status", "ping", "clipboard-get", "help"}
ACTION_CMDS = {"click", "type", "keys", "focus", "clipboard-set", "lease"}

# `cu browser <verb>`: 只读的能直接看；能改变页面/开进程的一律按动作管。
# ⚠ `eval` 等价于"在这页里跑任意 JS"（能提交表单、能读 cookie）→ 按动作管。
BROWSER_READ = {"status", "list", "text", "wait", "shot", "html", "tabs"}
BROWSER_ACTION = {"start", "stop", "open", "go", "click", "type", "eval",
                  "close", "new-tab", "reload", "back", "forward"}

# `cu macro <verb>`: play 会动鼠标键盘（动作）；其余只读写本地宏 / 读审计（只读）
MACRO_READ = {"list", "show", "save", "rm", "export"}

_CU_NAMES = {"cu", "cu.cmd", "cu.bat", "cu.py", "cu.ps1", "cu.exe"}
# 这些字符说明命令不止"跑一次 cu"（可能夹了别的命令）→ 一律不当成 cu 放行
_SHELL_META = (";", "&&", "||", "|", ">", "<", "`", "$(", "\n", "\r")
# cu 前面只允许出现"解释器/壳"这类令牌（python xxx\cu.py、cmd /c cu …）；
# 出现 echo / type / start 之类就不是在跑 cu —— 这条挡住 "echo cu windows" 冒充。
_PREFIX_OK = {"python", "python3", "pythonw", "py",
              "python.exe", "python3.exe", "pythonw.exe", "py.exe",
              "cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe",
              "/c", "/k", "-c"}


def _prefix_ok(toks, idx):
    for t in toks[:idx]:
        base = t.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        if base in _PREFIX_OK:
            continue
        return False
    return True


def parse_cu_command(cmd):
    """识别"单纯调用 cu"的命令。

    返回 (子命令, 是否只读)；不是 cu / 带 shell 拼接 / 前缀不是解释器 /
    子命令不认识 → None。
    """
    if not cmd:
        return None
    s = str(cmd).strip()
    if not s or any(m in s for m in _SHELL_META):
        return None
    toks = s.replace('"', " ").replace("'", " ").split()
    idx = None
    for i, t in enumerate(toks):
        base = t.replace("/", "\\").rsplit("\\", 1)[-1].lower()
        if base in _CU_NAMES:
            idx = i
            break
    if idx is None or idx + 1 >= len(toks) or not _prefix_ok(toks, idx):
        return None
    sub = toks[idx + 1].lower()
    if sub.startswith("-"):
        return None
    if sub == "browser":
        if idx + 2 >= len(toks):
            return None
        verb = toks[idx + 2].lower()
        if verb.startswith("-"):
            return None
        if verb in BROWSER_READ:
            return "browser." + verb, True
        if verb in BROWSER_ACTION:
            return "browser." + verb, False
        return None
    if sub == "http":
        # cu http get = 只读；cu http post（含 --method PUT/PATCH/DELETE）= 动作
        if idx + 2 >= len(toks):
            return None
        verb = toks[idx + 2].lower()
        if verb == "get":
            return "http.get", True
        if verb in ("post", "put", "patch", "delete", "send"):
            return "http.post", False
        return None
    if sub == "macro":
        # 回放 = 动作（会动鼠标键盘）；其余只是读写本地宏文件 / 读审计，不弹窗
        if idx + 2 >= len(toks):
            return None
        verb = toks[idx + 2].lower()
        if verb.startswith("-"):
            return None
        if verb == "play":
            return "macro.play", False
        if verb in MACRO_READ:
            return "macro." + verb, True
        return None
    if sub in READ_ONLY_CMDS:
        return sub, True
    if sub in ACTION_CMDS:
        return sub, False
    return None
