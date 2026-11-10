"""Agent 通用桌面工具（Computer Use）—— cocraft 的"眼睛和手"。

对外入口：
  CLI   tools/cu.py          所有 agent 都能用（谁都有 shell）
  MCP   tools/cu_mcp.py      结构化工具（可选，先 CLI 跑通）
  注入  backend/engines/acp  PATH + 每轮 prompt 工具卡 + 权限旁路

P0（只读）：winapi / capture / uia
P1（动作）：input / lease / gate / audit
"""

from . import audit, capture, cdp, gate, http, input, lease, macro, uia, winapi  # noqa: F401

__all__ = ["winapi", "uia", "capture", "input", "lease", "gate", "audit", "cdp", "http", "macro"]
