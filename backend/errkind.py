# -*- coding: utf-8 -*-
r"""把「各家 agent 的报错」翻成一类一类的人话（`ui 0.2.87`）。

为什么单独一个模块：
  * 同一个意思，各家写法完全不同 ——
    DeepSeek `Insufficient Balance` / OpenAI `exceeded your current quota` /
    Anthropic `credit balance is too low` / 网关 `402 Payment Required`；
    Codex `unexpected status 401 Unauthorized`、`Missing environment variable: X`；
    ACP 适配器 `-32603 Internal error`；dsh `Endpoint is unavailable`…
  * 以前只有工作流里有一份 `_account_error_kind()`（只认 4 种），**单聊 / 群聊各写各的**，
    结果「余额不足」在面板里根本认不出来（用户实测报过）。

约定：
  * **纯函数、无依赖、无副作用** —— 好测（`tools/errkind_test.py` 20+ 用例）。
  * 只在**错误路径**上调用（`session.error` / 成员 error / 「错误被当回答」），
    所以规则可以写宽一点；但**正常回答里提到**这些词不该误伤（调用方用 `looks_error()` 先筛）。
  * 返回 **结构化** 结果，前端 / 审计 / 日志共用同一套 `kind`：

        {"kind": "balance", "label": "余额不足", "hint": "…",
         "account": True,   # 账户/凭据问题：换模型、重试都没用
         "retry_model": False}
"""
from __future__ import annotations

import re

# kind → (中文名, 人话建议, 是否账户问题, 换模型重试有用吗)
KINDS = {
    "balance": ("余额不足",
                "去厂商后台充值，或换一个有额度的 Key（设置 → 模型凭据）；换模型/重试都没用",
                True, False),
    "quota": ("额度用尽",
              "这个账号的额度/并发用完了：等额度重置、换 Key，或换厂商（设置 → 模型凭据）",
              True, False),
    "auth": ("凭据无效",
             "Key 不认（401）：检查设置 → 模型凭据里的 Key 是否过期/写错，或该厂商是否要换 base_url",
             True, False),
    "nokey": ("没配 Key",
              "这个 agent 拿不到 Key：在「设置 → 模型凭据」补一条，或设对应的环境变量；"
              "安装版还要确认 agent 自己的 env（如 CODEX_HOME）没丢",
              True, False),
    "model": ("模型不可用",
              "这个会话绑的模型被下架/无权访问；点顶栏「模型」换一个（工作流里会自动换一次）",
              False, True),
    "rate": ("被限流",
             "请求太密（429）：等几秒再试；批量任务建议降低并发",
             False, True),
    "context": ("上下文超限",
                "这一轮太长：开新会话、让它先总结，或减少一次贴进来的文件/日志",
                False, False),
    "network": ("连不上上游",
                "网络/网关问题（不是 Key 的问题）：检查代理或公司网络，稍后重试",
                False, True),
    "timeout": ("超时",
                "等太久了（可能任务太大）：缩小范围再来，或把任务拆成几步",
                False, True),
    "cancelled": ("已取消",
                  "这一轮被中断/取消，不是故障",
                  False, False),
    "agent": ("agent 内部出错",
              "agent 自己崩了（不是你的 Key）：看 runtime/logs/acp.log 尾部；"
              "反复出现就换一个大脑（agentlist）",
              False, True),
    "unknown": ("未知错误",
                "拿不准是什么问题：看 runtime/logs/acp.log 尾部；"
                "可以在「审计与活动」导出诊断后贴给别人看",
                False, True),
}

# 有序规则（先匹配先赢）。**顺序有讲究**：账户类要在 network 前面（402 别被当成网络）。
_RULES = [
    ("balance", re.compile(
        r"insufficient\s+balance|余额不足|余额为\s*0|credit\s+balance\s+is\s+too\s+low|"
        r"payment\s+required|status\s*402|\b402\b.*(balance|credit)|recharge|欠费", re.I)),
    ("quota", re.compile(
        r"quota|额度|exceeded\s+your\s+current|billing\s+hard\s+limit|usage\s+limit|"
        r"monthly\s+limit|免费额度", re.I)),
    ("nokey", re.compile(
        r"missing\s+environment\s+variable|no\s+api\s+key|api\s*key\s+(is\s+)?(not\s+set|missing)|"
        r"未配置\s*key|环境变量.*(缺失|没有)|请先设置.*key", re.I)),
    ("auth", re.compile(
        r"invalid\s*api[_\s-]?key|incorrect\s+api\s+key|unauthorized|authentication\s+required|"
        r"status\s*401|\b401\b.*(unauthor|auth)|凭据无效|key\s+无效|token\s+(is\s+)?(invalid|expired)", re.I)),
    ("model", re.compile(
        r"model\s*(unavailable|not\s+found|does\s+not\s+exist|not\s+supported|is\s+deleted)|"
        r"modelunavailableerror|unknown\s+model|no\s+such\s+model|模型(不可用|不存在|已下架|不支持)", re.I)),
    ("rate", re.compile(
        r"rate\s*limit|too\s+many\s+requests|status\s*429|\b429\b|请求过于频繁|限流", re.I)),
    ("context", re.compile(
        r"context\s*(length|window)|maximum\s+context|too\s+many\s+tokens|token\s+limit|"
        r"prompt\s+is\s+too\s+long|超出(上下文|长度)|上下文(超|过)", re.I)),
    ("cancelled", re.compile(
        r"\b(cancelled|canceled|interrupted|aborted)\b|已取消|已中断|用户取消", re.I)),
    ("timeout", re.compile(
        r"\btimed?\s*out\b|timeout|超时|deadline\s+exceeded", re.I)),
    ("network", re.compile(
        r"connection\s*(reset|refused|closed)|connectionrefused|connectionreset|"
        r"econnreset|econnrefused|etimedout|enotfound|unable\s+to\s+connect|"
        r"fetch\s+failed|socket\s+hang\s+up|"
        r"getaddrinfo|network\s+error|ssl|tls|"
        r"bad\s+gateway|service\s+unavailable|status\s*50[234]|\b50[234]\b|"
        r"endpoint\s+is\s+unavailable|upstream\s+request\s+failed|unavailable", re.I)),
    ("agent", re.compile(
        r"acperror|acp\s*(错误|error)|-3260[0-9]|-3261[0-9]|internal\s+error|"
        r"process\s+(exited|died)|agent\s+(crashed|exited)|panic:|traceback", re.I)),
]

# 「这段文字其实是错误」的前缀（调用方用来避免把正常回答当故障）
_ERRISH = re.compile(
    r"^\s*(acperror|acp\s*错误|acp\s*error|apierror|api\s*error|error:|"
    r"unexpected\s+status|request\s+failed|traceback|panic:|"
    r"insufficient\s+balance|余额不足)", re.I)


def looks_error(text: str) -> bool:
    """这段文字像不像"错误被当回答吐出来"。

    ⚠ 判据要**窄**（只认前缀 + 短文本里的 request_id/status 4xx），
      否则正常回答里解释一句「Insufficient Balance 建议充值」也会被当成故障。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if _ERRISH.search(t):
        return True
    return len(t) <= 400 and bool(re.search(
        r"(request_id|status\s*4\d\d|status\s*5\d\d)", t, re.I))


def classify(text: str) -> dict:
    """把一段报错文本归类。永远返回一个 dict（认不出就是 `unknown`）。"""
    raw = str(text or "")
    low = raw.strip()
    for kind, pat in _RULES:
        if pat.search(low):
            label, hint, account, retry_model = KINDS[kind]
            return {"kind": kind, "label": label, "hint": hint,
                    "account": bool(account), "retry_model": bool(retry_model)}
    label, hint, account, retry_model = KINDS["unknown"]
    return {"kind": "unknown", "label": label, "hint": hint,
            "account": bool(account), "retry_model": bool(retry_model)}


def kind_of(text: str) -> str:
    """只要 kind（`balance` / `auth` / … / `unknown`）。"""
    return classify(text)["kind"]


def is_account(text: str) -> bool:
    """账户 / 凭据类问题 → 换模型、重试都没用，必须人处理。"""
    return bool(classify(text)["account"])


def one_line(text: str, limit: int = 300) -> str:
    """把原始报错压成一行人话（列表/横幅用）：`余额不足（402）：Insufficient Balance …`"""
    k = classify(text)
    body = " ".join(str(text or "").split())
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return ("%s：%s" % (k["label"], body)) if body else k["label"]


def payload(text: str, base: dict = None) -> dict:
    """给 `session.error` / 成员 error 用的附加字段（原样并进 data）。"""
    k = classify(text)
    out = dict(base or {})
    out.update({"kind": k["kind"], "label": k["label"], "hint": k["hint"],
                "account": k["account"], "retryModel": k["retry_model"]})
    return out


def _self_test() -> None:  # pragma: no cover - 手动跑
    for s in ("Insufficient Balance (request_id: x)", "402 Payment Required",
              "unexpected status 401 Unauthorized", "Missing environment variable: X",
              "ModelUnavailableError: deepseek/x", "Rate limit reached",
              "context length exceeded", "Endpoint is unavailable",
              "ACP 错误 -32603: Internal error", "随便一句正常的话"):
        k = classify(s)
        print("%-46s → %-9s %s" % (s[:46], k["kind"], k["label"]))


if __name__ == "__main__":  # pragma: no cover
    _self_test()
