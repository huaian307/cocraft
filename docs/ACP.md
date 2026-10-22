# ACP 引擎（对话引擎抽象）

> 这份文档讲「把面板从『只能连 OpenCode』变成『可切换的对话引擎』」这套东西。
> 快速上手：想直接看效果 → `python tools/acp_demo.py`；想接真 agent → 看第 6 节。
> 最后更新：2026-09-26（前端 `ui 260926zi`；补 plan/usage/工具进度、权限「始终允许」记忆、
> `session/list` 导入、provider 推断与中性化，见第 4 / 8 节）

## 1. 这是什么 / 为什么

原来的面板（`frontend/` + `backend/server.py`）是**OpenCode 专用**的：
所有 `/api/*` 都是把请求反代给 OpenCode 后台服务，前端消费的是 OpenCode 的
会话/消息/SSE 形状。

我们想让它能驱动**别的 agent**（Codex / Claude Code / DeepSeek Harness…），
而这些 agent 没有一个说 OpenCode 的 API。于是加了一层**引擎抽象**：

- `server.py` 里所有「跟哪个 agent 说话」的语义，改成先问**当前引擎**，由引擎实现；
- 新增 `acp` 引擎，用 **ACP（Agent Client Protocol）** 驱动外部 agent；
- **默认引擎仍是 `opencode`，行为与改造前完全一致**；前端几乎不用改。

```
面板前端（几乎不动，仍消费 OpenCode 形状的 /api/*）
      │
      ▼
backend/server.py  ──  engines.active_engine() 分派
      │
      ├── engines/opencode.py   → 反代到本机 OpenCode（原逻辑，原样搬移）
      └── engines/acp/          → ACP 桥：JSONL/JSON-RPC over stdio 驱动子进程 agent
```

## 2. 文件地图

| 文件 | 作用 |
| --- | --- |
| `backend/engines/__init__.py` | 引擎注册表 + `active_engine_id()` / `set_active_engine_id()` / `get_engine()`（**实例单例缓存**） |
| `backend/engines/base.py` | `Engine` 接口 + 共用 `HOP_BY_HOP` |
| `backend/engines/opencode.py` | `OpenCodeEngine`：`read_service` / `/api/*` 流式反代 / `/healthz`（从 server.py 搬来） |
| `backend/engines/acp/process.py` | `AcpProcess`：stdio 子进程 + **JSONL 分帧**（读/写，Windows 无窗口） |
| `backend/engines/acp/client.py` | `AcpClient`：JSON-RPC 2.0 请求/响应配对 + **双向请求**（agent→client 丢工作线程） |
| `backend/engines/acp/map_events.py` | ACP `session/update` → 前端事件的纯映射；`AssistantTurn`；elicitation schema→form 字段；权限 optionId 选择 |
| `backend/engines/acp/service.py` | `AcpService`：会话/消息/prompt/interrupt/事件订阅/权限与表单的等待；**用量记账**、**「始终允许」记忆**、**agent 侧会话导入** |
| `backend/engines/acp/engine.py` | `AcpEngine(Engine)`：把 service 接到 `/api/*`（含 `/api/event` SSE） |
| `backend/engines/acp/agents.py` | ★agent 注册表 + **provider 推断**（env / 命令 / Codex `config.toml`）+ 自动检测 |
| `backend/acp_mock_agent.py` | **开发用假 agent**（说 ACP v1），不需要任何外部运行时；已覆盖 plan / usage / 工具进度 / 权限（含 allow_always）/ `session/list` |
| `tools/acp_demo.py` | 一键联调：独立端口起面板（不动线上 8787 / 不改全局引擎） |
| `tools/acp_engine_ui_test.py` | 无头交互测试：真在浏览器里切换引擎并验证 SSE 重连 |
| `tools/acp_events_test.py` | ★无头回归：真拉起假 agent，逐条断言事件映射 / 用量 / 记忆 / 导入（48 项） |
| `tools/acp_api_test.py` | ★无头回归：起真 `server.py`（临时端口 + 假 agent，进程内注入）打 `/api/*`，专抓路由互相遮挡（25 项） |

## 3. 接口契约（`base.Engine`）

```python
class Engine:
    id: str
    label: str
    def available(self) -> bool          # 依赖是否就绪（OpenCode：service.json 在不在）
    def read_service(self)               # -> (url, auth, version)；不需要上游就抛 NotImplementedError
    def status(self) -> dict             # /engine/status 的自述
    def healthz(self)                    # -> (http_code, body)
    def handle_api(self, handler, method)  # 实现 /api/* 的语义（直接写响应）
```

新增引擎：写 `backend/engines/<id>.py` 实现 `Engine`，在 `registry()` 登记即可，
`server.py` 不用再改。

### 状态文件

| 文件 | 内容 | 说明 |
| --- | --- | --- |
| `runtime/state/_engine.json` | `{"engine": "opencode"}` | 当前引擎；文件缺失/非法/未注册 → 回落 `opencode` |
| `runtime/state/_acp_agents.json` | `{"active":"<id>","agents":[{id,label,command,cwd,env,note}]}` | **agent 注册表（2026-09-25）**；缺失时从 `_acp.json` bootstrap |
| `runtime/state/_acp.json` | `{"command": ["...exe","..."], "cwd": "..."}` | 旧的单 agent 配置；仍兼容（bootstrap 的来源），不再是主配置 |
| `runtime/state/_acp_sessions.json` | 落盘的 ACP 会话+消息 | 重启不丢；续聊时 `session/resume` 接回 |
| 环境变量 `OPENCODE_UI_ENGINE` | 引擎 id | **临时覆盖**当前引擎（联调隔离用；优先于状态文件） |

### HTTP（引擎层新增）

| 接口 | 说明 |
| --- | --- |
| `GET /engine/status` | `{ok, active, available:[id...], detail}` |
| `GET/POST /engine` | POST `{engine}` 切换（校验已注册）；返回 `{ok, active}` |
| `GET /engine/acp/agents` | ACP **agentlist**：`{ok, active, detail, agents:[{id,label,available,note,command,cwd,hasEnv}]}` |
| `POST /engine/acp/agents` | `{id}` 切换当前 agent；`{id, command|label|cwd|env|note}` 修改；改完 `shutdown()` 旧子进程 |

前端在**设置弹窗**里有「对话引擎」下拉（`#cfg-engine`）：打开设置时拉
`/engine/status` 填充；切换后清空当前会话、**重连 SSE**、重载会话列表。
老后端没有这些接口时优雅降级为「不可用」。
设置里引擎选 `acp` 时，下面会多出**「ACP 代理」**一行 → 「agentlist…」打开 `#agents` 弹窗选 agent
（自带 `codex-node` / `codex-client` / `dsh`；金点=命令可找到，红点=找不到）。

### ACP 专属的 `/api` 路由（2026-09-26 补）

这些只对 `acp` 引擎存在（前端还没入口，先把接口与数据铺好）：

| 接口 | 说明 |
| --- | --- |
| `GET /api/session/remote?cwd=&cursor=&internal=` | 列出 **agent 侧**已有会话（`session/list`），每条带 `imported` / `localID`；默认**隐藏内部会话**（群聊 AI 成员 / 本地协作），`internal=1` 给全量并带 `internal` 标记 + `internalCount`；不支持时回 `{supported:false, error}` |
| `POST /api/session/import` `{id, title?}` | 把一条 agent 会话「领」进面板（幂等）；只建指向它的本地记录，**不复制历史**（ACP 没有读别人消息的接口） |
| `GET /api/permission/always?session=` | 列出「始终允许」的记忆规则 |
| `DELETE /api/permission/always` `{session?,kind?,id?}` | 清记忆；都不给 = 全清（回 `{ok, removed}`） |
| `GET /api/session/{id}/permission` | 除 `data` 外**多了 `always`**（该会话的规则） |

> ⚠ `/api/session/remote` 与 `/api/session/import` **必须在通用 `/api/session/{id}` 正则之前判**，
> 否则 `remote` / `import` 会被当成会话 id（`tools/acp_api_test.py` 专抓这个）。

## 4. ACP 要点与映射

ACP = **JSON-RPC 2.0 + 换行分帧（JSONL）over stdio**；JSON-RPC 头字段规范，
ACP 自己的键 `camelCase`、判别值 `snake_case`，路径必须绝对、行号 1-based。
客户端启动 agent 子进程；agent 的 stderr 只当日志。

用到的方向：
- 我们 → agent：`initialize` · `session/new` · `session/prompt`(可 `session/cancel`)
- agent → 我们（通知）：`session/update`（文本/思考/工具/…）
- agent → 我们（请求，需回复）：`session/request_permission` · `elicitation/create`

### 事件映射（`session/update` → 前端 SSE）

| ACP `sessionUpdate` | 前端事件 / 落库 |
| --- | --- |
| `agent_message_chunk` | `session.text.delta` |
| `agent_thought_chunk` | `session.reasoning.delta` |
| `compaction_summary_chunk` | 同上（上下文压缩的摘要，别整段丢掉） |
| `tool_call` | `session.tool.called`；**自带 `status:completed/failed` 时立刻补发 success/error**（Codex 大量工具是一次性发完的） |
| `tool_call_update`（completed / failed） | `session.tool.success` / `session.tool.error` |
| `tool_call_update`（进行中，`content` 或 Codex 的 `_meta.terminal_output_delta` / `_meta.mcp_output_delta` / `_meta.terminal_output`） | `session.tool.progress {delta, output}` + 累加进工具输出 |
| `plan` / `plan_update`（items·markdown·file）/ `plan_removed` | `session.plan.update {planId, plan}`；**不进 content**（前端对未知 part 只会兜底显示原始 JSON），挂在消息 `plan` 与会话 `plan` 上 |
| `usage_update` `{used,size}` | `session.usage.update` + 会话 `context`（**上下文窗口占用**，不是账单 token） |
| `session_info_update` `{title,updatedAt}` | 标题还是默认值 / 前端自动命名的那句时 → 改标题并发 `session.renamed`；用户手动改过的**绝不覆盖** |
| `current_mode_update` | 同步 `_modes.currentModeId`（让 `/api/agent` 显示真值）+ `session.mode.update` |
| `config_option_update` | `_remember_config()`（模型/思考强度的**权威**值）+ `session.model.update` |
| `notice` | `session.notice` + 日志（Codex 的重试/配额提示） |
| `available_commands_update` | 记在 `AcpService._commands`（斜杠命令清单，前端暂无入口） |
| `user_message_chunk` | 回放用（暂不落库） |
| 其它（含 `subagent_*`） | 忽略 |

⚠ **`session/prompt` 的响应也要用**：`{stopReason, usage:{totalTokens,inputTokens,outputTokens,
thoughtTokens?,cachedReadTokens?,cachedWriteTokens?}}` —— 这是**账单口径**的 token，
累加进会话 `tokens`（会话详情里那行「Token」从此是真数字）；`stopReason` 记成会话「结果」。
⚠ **费用（cost）仍然是 `null`**，面板显示「费用未知」：ACP 不给官方单价，**不谎报免费**。

一轮结束由 `AcpService._run_turn` 发 `session.text.ended` / `session.step.ended` /
`session.execution.succeeded`，前端据此**重取一次权威消息**。

### 权限「始终允许」的记忆（2026-09-26）

前端回 `once/always/reject`（`pick_option_id()` 按 `kind` 选回 `optionId`）。选 **always** 时
`AcpService` 记一条规则到 `runtime/state/_acp_always.json`：

```jsonc
{"id":"ar_codex-node_execute_*", "agent":"codex-node", "session":"<本地会话 id>",
 "kind":"execute", "path":"", "hits":1, "ts":1790399291612, "action":"Run command"}
```

- 之后同类请求（**同 agent + 同会话 + 同 kind**，规则里 `path` 非空时还要路径落在范围内）
  **直接放行、不弹窗**，并发 `permission.auto` 事件（前端可以显示"已按记忆放行"）。
- 匹配只用**同向**的 `allow_always`（没有就 `allow_once`）；找不到任何 allow 选项仍回 `cancelled`。
- 规则**默认只在本会话内有效**（`session` 存本地 id），换会话要重新允许 —— 宁可多问一次。
- 清除：`DELETE /api/permission/always`（`{session}` / `{kind}` / `{id}` 过滤，全空 = 全清）。
- 实测（真实 codex-node）：第一轮弹窗点「始终允许」→ 第二轮 `permission.auto`、零弹窗。

### 消息映射（`/api/session/{id}/message`）

助手消息拼成 `{id,type:"assistant",time:{created,completed},content:[...]}`，
`content` 里依次是 `{type:"reasoning"|"text"|"tool"}`。用户消息是顶层 `text`。
返回**新→旧**（前端自己按 `time.created` 升序重排）。

### 权限 / elicitation → 复用现有弹窗

- **权限**：`session/request_permission` 的 `toolCall.title` → `action`，
  `toolCall.locations[].path` → `resources`。前端回 `once/always/reject`，
  经 `pick_option_id()` 按 `kind` 选回 `optionId`，响应必须是
  **`{"outcome":{"outcome":"selected","optionId":...}}`**（或 `cancelled`）；
  ⚠ 找不到同向选项时只能回 `cancelled`，**绝不能把「拒绝」退化成「允许」**。
- **elicitation**：`requestedSchema`（扁平 JSON Schema）→ 前端 form 的
  `fields[]`（`enum`→选项按钮，`boolean/number/integer/string`→对应控件）；
  回复 `{"action":"accept","content":{...}}`。只声明并支持 `form` 模式
  （URL 模式回 `decline`）。

### 安全默认（`clientCapabilities`）

只声明 `{"elicitation":{"form":{}}}` + `{"_meta":{"terminal_output_delta":true}}`；
`fs/*`、`terminal/*` **一律不开**（ACP 规定「没声明 = 不支持」）。要开由调用方以后显式传入。

`_meta.terminal_output_delta` 的作用：Codex 据此**逐段**发命令输出
（`_meta.terminal_output_delta.data`），而不是只给终态一大坨 —— 工具进度条才有内容。

### agent 侧已有会话（`session/list` → 导入）

Codex 宣告 `agentCapabilities.sessionCapabilities = {resume:{}, list:{}, close:{}, delete:{},
fork:{}, …}`，所以能列出 **Codex 客户端里聊过的那些会话**（"读歌单式"导入）：

1. `supports_session_list()` —— ⚠ **只能判"键在不在"**：宣告值常是**空对象** `{}`，
   `bool({})` 是 `False`，会把支持的 agent 误判成不支持。
2. `list_remote(cwd=None, cursor="", include_internal=False)` → `{supported, sessions:[{id,title,cwd,updatedAt,internal,imported,localID}], cursor, total, internalCount}`
   - **默认 `include_internal=False`**：隐藏**面板内部**的会话（群聊 AI 成员 / 本地协作 Runner），
     否则「接续」列表会被几十条 `【你的角色：…】` 淹没。
   - 内部会话识别三路：桥的 `_cocraft_bridge_sessions.json`、`_orchestrator/runs/<run>/*.sessions.json`、
     标题特征（`【你的角色：` / `你是群聊成员` / `【最近的群聊记录】` / `群聊AI·` / `协作·` / `【仲裁】`）。
   - 前端「接续」弹窗分「项目会话 / 内部会话」两个标签；接续内部会话会二次确认。
3. `import_remote(id, title?)` → 建一条指向该 agent 会话的本地记录（`agentSessionId=id`），
   并 `session/resume` 接回一次以收回模型/审批模式；**历史不复制**（ACP 没有读消息的接口），
   所以导入后会话里是空的，但 agent 那边上下文还在，接着聊即可。
   - 它自己核对 cwd 时用 `list_remote(include_internal=True)`，不会因为默认过滤而找不到内部会话。
4. 再导一次同一个 id 是**幂等**的（回同一条本地会话）。

## 5. 一键联调

```bat
python tools\acp_demo.py            :: 默认 http://127.0.0.1:8799
```

它会：把 `_acp.json` 指向 `backend/acp_mock_agent.py`（退出恢复）→ 设
`OPENCODE_UI_ENGINE=acp`（**只影响本进程**）→ 起独立端口的面板服务。
打开那个地址、新建会话、发一句话，就能看到：文本 / 思考 / 工具 / **权限弹窗** /
**表单弹窗**。**不碰线上 8787、不改全局 `_engine.json`。**

## 6. 接真实 agent

1. 在 `runtime/state/_acp.json` 写启动命令，例如：
   ```json
   { "command": ["<适配器可执行文件>"], "cwd": "D:\\你的项目" }
   ```
2. 重启后端（或在设置里把引擎切成 `acp`）。
3. 新建会话。

各家适配器（2026 现状，**需自行安装**；安装包会带 Codex / Claude 的适配器）：
- Codex：`@agentclientprotocol/codex-acp`（npm；`codex-node` 档案）
- Claude Code：**`@zed-industries/claude-code-acp`（npm；`claude-node` / `claude-client` 两个档案）**
- DeepSeek Harness：`dsh-acp` / `openma-ai/deepseek-harness-acp`

### Claude Code（2026-09-26 接入，**与 Codex 同构**）

| 档案 | 命令 | 依赖（缺了会显示在 `missing`） |
| --- | --- | --- |
| `claude-node` | `[<node>, <node_modules>/@zed-industries/claude-code-acp/dist/index.js]` | node + **Claude Code CLI** + 适配器 |
| `claude-client` | `["claude-code-acp"]` | `claude-code-acp`（或 Claude Code CLI） |

- **我们不下载 / 不随包 Claude Code 本体**：适配器只是「ACP ↔ Claude Code」的翻译层，
  它调用的是**目标机器上已登录的 Claude Code**（登录态在 `~/.claude`）；
  也可以在 `runtime/state/_acp_agents.json` 的 `baseline.env` 里配 `ANTHROPIC_API_KEY`
  （面板：设置 →「模型 API Key」，变量名填 `ANTHROPIC_API_KEY`）。
- **自动检测位置**：PATH、`%LOCALAPPDATA%\Programs`、`%APPDATA%\npm`、`~/.claude`、
  `_search_dirs()` 下的本地 npm 安装（`node_modules/@zed-industries/claude-code-acp`）。
  ⚠ **故意不探** VS Code 扩展缓存（`%APPDATA%\Code\agent-host\sdk-cache\...`）—— 那是别的软件的私有缓存，
  路径带版本号、不稳（用户 2026-09-26 明确要求）。
- `autofill()` 会把 npx 形态升级成本地 npm 适配器（和 codex 一样）；`scan_candidates()` 也会列出两类候选。
- **网络现实**：Anthropic 直连在国内通常不通。"直接接 Claude Code 客户端"靠的是**它自己的登录/通道**；
  拿到另一台机器上测之前，先确认那台机器的 Claude Code 自己能正常对话，这样出问题好分锅。

⚠ **本机没有 Node/npm**：`claude-code-acp` / `dsh` 是 Node/TS 生态；Codex 的适配器
看是否有免 Node 的原生产物。ACP 客户端侧（我们）是**纯标准库 Python**，不需要 Node。

## 7. 测试

仓库里的回归脚本：

```bat
python tools\acp_events_test.py       :: ★无头：真拉起假 agent，断言事件/用量/记忆/导入（48 项）
python tools\acp_api_test.py          :: ★无头：起真 server.py 打 /api/*，抓路由遮挡（25 项）
python tools\acp_engine_ui_test.py    :: 无头交互：切换引擎 + 验证 SSE 重连
python tools\check_features.py        :: 功能体检（含 29 条 ACP 后端断言，共 267 项）
python tools\acp_demo.py              :: 一键联调面板（假 agent，独立端口）
```

`acp_events_test.py` / `acp_api_test.py` 都把落盘文件指到临时目录
（`service.STATE_DIR` 与 `_acp_always.json`），**不碰真实的 `runtime/state/`**；
输出纯 ASCII（Windows 控制台是 GBK）。

另有开发期的回归脚本（`%TEMP%\opencode\`，可重建）：S1 opencode 回归、S2 ACP
客户端、S2b 大消息完整性（3×2MB）、S3 端到端 HTTP+SSE、修复定向测试。

## 8. 「子命令式」ACP：OpenCode 也能被 ACP 驱动（2026-09-26）

**背景**：用户问"OpenCode 的客户端不在 agentlist 里吗？为什么 ACP 找不到它？" —— 查证结果：
**在，而且它自带 ACP**，只是我们的识别规则没想到它这种形态。

| 形态 | 例子 | 老规则认吗 |
| --- | --- | --- |
| 独立可执行文件，名字含 acp | `codex-acp.exe` | ✅ |
| node 包，bin 名含 acp | `@agentclientprotocol/codex-acp` | ✅ |
| **CLI 的 `acp` 子命令** | **`opencode-cli.exe acp`** | ❌ 文件名没 acp、也不是 node 包 |

实测（`opencode-cli.exe acp` 是一个完整的 ACP v1 server）：

```jsonc
protocolVersion : 1
agentInfo       : {"name": "OpenCode", "version": "2.0.13"}
agentCapabilities: {loadSession: true,
                    sessionCapabilities: {list:{}, resume:{}, close:{}, delete:{}, fork:{}},
                    promptCapabilities: {embeddedContext: true, image: true}}
// session/new 返回 78 个模型、id 是 `provider/model`（deepseek/deepseek-v4-pro、opencode/…）
// configOptions: model / effort(low…max,default) / mode(**build**=默认执行工具, **plan**=只读)
// 实测 session/prompt 回 "pong"、session/delete 正常
```

**做法**（`agents.py`）：

- 新增规格表 `_SUBCOMMAND_ACP`（目前只有 OpenCode）+ `find_subcommand_acp()`：
  在 `_search_dirs()`/PATH 里找这些 CLI，返回 `[<exe 真实路径>, "acp"]`。
- `scan_candidates()` 多一条 ④；`infer_from_dir()` 也认它（"选文件夹"指到
  `D:\agentlist\opencode` / `…\opencode\app` / `C:\…\@opencodedesktop` 都行）。
- ⚠ **去重按 `os.path.realpath`**：本机 `C:\…\@opencodedesktop` 是指向 `D:\agentlist\opencode\app`
  的 junction，不归一化就会"同一个 exe 扫出两条"（实测）。
- 扫出来的 id 是 `opencode-acp`，`available` 只看命令存在；**provider 留空**（它的模型跨多家，
  填任何单一 provider 都是错的 → 前端按模型 id 的 `provider/` 前缀找图标）。

**顺带修的两个真问题**：

1. **基线 `mode` 不能瞎发**：`baseline.mode = read-only` 是给 Codex 的，而 OpenCode 只有 `build`/`plan`
   → 以前每次建会话都会收到 `-32602: mode not found: read-only`。现在 `_apply_baseline()`
   **只在目标值确实在该 agent 的选项里时才发**，否则记一行日志跳过。
2. **每个 agent 可以有自己的 `mode`**：注册表 agent 新增 `mode` 字段（**覆盖共享基线**）——
   于是 Codex 用 `read-only`、OpenCode 用 `plan`，互不打架。`public()` 会回这个字段。
   本机 `opencode-acp.mode = "plan"`（= 它的只读模式），实测建会话日志是「基线：mode=plan」。
3. **改模型要以 agent 回执为准**：`session/set_config_option` 的响应里带权威 `configOptions`，
   现在直接 `_remember_config()` 回收（之前盲目写本地值 → agent 不认时会"本地显示已切换、实际没切"）。

## 9. provider 与中性化（2026-09-26）

**为什么**：ACP 不告诉客户端"这些模型是哪家的"（`configOptions` 里只有 `value/name`）。以前我们把
provider 写死成 `deepseek`，换个 agent 就贴错牌子；改成中性又变成"所有人都叫 `acp`、没有品牌图标"。
现在的规矩是**能认出来就填、认不出就留空，留空照样能跑**：

| 环节 | 行为 |
| --- | --- |
| 注册表 `agents[].provider` | 用户/推断填的 provider（如 `deepseek` / `openai` / 空） |
| `agents.guess_provider(a)` | ① 已配的 provider → ② agent **自己的** env / 命令里的主机名或 API key 名 → ③ `CODEX_HOME/config.toml` 的 `base_url` / `env_key`；认不出回 `""` |
| `add_agent` / `autofill` / `scan_candidates` | 都会**只补空着的** provider（已配的绝不覆盖）；`autofill` 的结果里单列 `filledProviders`（只为记录，不触发子进程重启） |
| `GET /engine/acp/agents` 的 `public()` | 回 `provider` + `providerGuess`（"猜出来的建议"，**不写盘**） |
| `POST {action:"guess-provider"}` | 只猜不写（前端编辑弹窗的「自动」按钮） |
| `AcpService(provider_id=...)` | `providerID` 就用它；**空 → `"acp"`**，`cost: null` → 前端显示「费用未知」 |

前端（`ui 260926zi`）：agent 卡片显示 provider 徽章（没配时显示 `≈deepseek` 这种推断提示）、
每张卡片「改」→ `#agent-edit` 弹窗（名称 / 命令 / 目录 / **provider + 一键「自动」** / 说明）。

⚠ **只扫 agent 自己的 env，不扫共享基线的 env** —— 否则基线里那个 `DEEPSEEK_API_KEY` 会把所有 agent 都带成 deepseek。
⚠ **空值的 env 键不算依据**：`OPENAI_API_KEY: ""` 这种占位曾把任意 agent 猜成 `openai`。

**默认 agent 也是中性的**：`agents.bootstrap()` 不再 `active = agents[0]`（那往往就是旧 `_acp.json` 收来的
`dsh`），而是**自动补全命令后，在"真的可用"的 agent 里挑第一个**；一个都不可用就留空。
内置的 `codex-node` 默认项也**不再塞 `OPENAI_API_KEY: ""`**、不再写"需登录 OpenAI"。

**引擎可用性**：`GET /engine/status` 额外返回 `engines:[{id,label,available,error}]`（`engines.describe()`）。
⚠ `available`（旧的 id 列表）**只是"注册过"**，不代表能用 —— 首次设置向导与设置弹窗用 `engines[]`
把打不通的引擎标「未就绪」并 `disabled`，当前引擎不可用时向导会自动落到第一个可用的。

## 10. 已知限制

- **模型清单/切换已接**（2026-09-24；2026-09-26 修「重启/切 agent 后旧会话拿不到清单」）：
  从 `session/new` **以及 `session/resume` / `session/load`** 返回的 `configOptions`（`category == "model"`）取清单，
  `/api/model` 返回、`/api/model/default` 返回当前值、切换走 `session/set_config_option`；
  **思考强度（`effort`）做成 variants**（off/low/high/max）。
  - ⚠ 模型清单来自**会话**：一条会话都没有时 `/api/model` 仍然是空的（先新建一个会话）。
  - `/api/model` 发现配置还没记住时，会拿「当前 agent 最近的一条会话」`session/resume` 一次来补全
    （失败就顺次试最近几条）—— 所以 **服务重启 / 切 agent 之后，旧会话也能拿到模型清单**，不用新建会话。
  - 兼容只回 `models`（`SessionModelState`）、不回 `configOptions` 的适配器：清单仍会显示，
    切换改用 `session/set_model`。
  - 模型 `provider` 取自 agent 注册表（`_acp_agents.json` 的 `provider`）；没配就用中性的 `acp`，
    费用显示「费用未知」（ACP 不给官方价格，不要谎报免费）。前端首次启动会弹「首次设置」向导：
    选引擎 →（ACP）选 agent → 拉模型清单并记默认模型。
  - **用量/费用（2026-09-26 接）**：`session/prompt` 响应的 `usage` 累加进会话 `tokens`
    （会话详情那行「Token」是真数字）；`usage_update` 是**上下文窗口**占用（`used/size`），
    记在会话 `context` 并发 `session.usage.update`。
    ⚠ **费用仍是 `null`**：Codex 只给 `usage_update.used/size`，不给单价 → 显示「费用未知」，不谎报。
- **事件补齐（2026-09-26）**：`plan` / `usage_update` / 工具进行中输出 / `session_info_update` /
  `current_mode_update` / `config_option_update` / `notice` / `compaction_summary_chunk` 都已映射，
  详见第 4 节；顺带修掉两个真 bug：Codex 一次性发完的工具不再永远转圈、
  `rawOutput` 是对象时不再显示 `[object Object]`。
- **权限「始终允许」记忆（2026-09-26）**：见第 4 节。规则落 `runtime/state/_acp_always.json`，
  默认**只在本会话内**生效。
- **导入 agent 侧已有会话（2026-09-26）**：`GET /api/session/remote` + `POST /api/session/import`。
  实测真实 codex-node 列出 8 条 Codex 客户端会话、导入后 `session/resume` 成功、模型清单正确。
  ⚠ **前端还没有入口**（属于待办里的「前端一轮」）；导入的会话**本地历史是空的**（ACP 不给读消息）。
- 会话**已持久化**：会话+消息落盘在 `runtime/state/_acp_sessions.json`，**重启后仍能列出**；
  第一次续聊时会用 `session/resume`（失败退 `session/load`）把会话在 agent 侧接回来。
  ⚠ 本修复**之前**丢掉的会话无法找回（当时是纯内存）。
- **多 agent 进程保活（P1 · 2026-09-26）**：`AcpEngine` 按 agent id 缓存 `AcpService`（一个常驻子进程），
  `select_agent` **不再关旧进程** → 切回旧 agent 不重启；`select_agent` 后异步 `prewarm()`，
  `server.py` 启动时若当前引擎是 ACP 也会预热。配置变更（command/env/cwd/provider）只关受影响的 agent。
- **resume 失败降级（P1 · 2026-09-26）**：`session/resume` / `session/load` 都失败时自动
  `session/new` 新建 agent 会话，本地会话用 `session["agentSessionId"]` 映射；prompt / set_model / cancel
  都用它，**本地消息历史仍是权威**。`/api/model` 的补全走 `allow_new=False`，只拉清单、不凭空建会话。
  多个 agent 同时保活时 `_acp_sessions.json` 只写自己 agent 的会话、别人的原样保留。
- **与 OpenCode 对齐的通用接口（2026-09-26）**：
  - `GET /api/agent`：把 ACP 的 **审批模式**（`modes.availableModes`，没有就取 configOptions 里的
    `mode`/`approval`）按 OpenCode 的 `{location,data:[{id,name,description,mode,hidden,current}]}` 形状返回。
  - `POST /api/session/{id}/agent`：模式切换；有 `SessionModeState` 时走标准 `session/set_mode`，
    否则退回 `session/set_config_option`。
  - `DELETE /api/session/{id}`：删本地会话前 best-effort 调 `session/close` / `session/delete`。
  - `GET /api/session/{id}/message`：支持 `cursor` 翻页，返回 `{data, cursor}`（与 OpenCode 形状一致）。
  - `GET /api/model`、`GET /api/session`：补 `location` / `cursor` 字段。
  - ⚠ 基线 mode 此前对 Codex 用 `session/set_config_option` 不生效（旧会话一直停在 `agent/auto_review`），
    现在改成优先 `session/set_mode`，`baseline.mode=read-only` 才真正落实。
- 附件只发 `text` + `resource_link`；`data:` URI 对真实 agent 多半无效，图片未发。
- 数学公式：面板的 Markdown 渲染器**不解析 LaTeX**；ACP agent 应遵守
  `D:\agentlist\shared\AGENTS.md` 的输出格式规则（行内代码 / 代码块 / ASCII 符号），
  不要用 `$...$` 或 `∑ ∏ ∫ √` 这类 Unicode 数学符号，否则会原样显示或显示成方框。
- `fs/*`、`terminal/*` 未实现。
- 切到**未配置**的引擎会看到 `500 未配置 ACP agent`（`/engine/status` 只报当前引擎可用性）。
- 同一会话**串行发问**：上一轮没结束再发会回 `409`（我们的 turn 映射不支持并发）。

## 11. 本轮踩过的坑（都已修 + 进体检）

1. **`AcpClient` 默认 60s 超时**会掐断真实 agent 的长回答 → 一轮用 `TURN_TIMEOUT`(24h)。
2. **`bufsize=0` 的 stdin 会短写** → 必须用 `memoryview` 循环写满，否则大消息破坏 JSONL。
3. **拒绝被做成允许**：`pick_option_id` 的盲兜底 `options[0]` 会跨方向乱选 → 只接受同向 kind。
4. **切引擎后没重连 SSE**：`/api/event` 在连接时就绑定了当时的引擎 → 切换后必须 `connectEvents()`。
5. **SSE 漏 `end_headers()`**：状态行/响应头从没刷出去，客户端把正文当状态行（用裸 socket 才定位到）。
6. **引擎实例每请求新建** → 会话状态丢失；改成单例缓存。
7. **dsh 在 Windows 上的沙箱 / bash**（2026-09-24 首记，**2026-10-12 更正**）：
   - 首记说"默认 `workspace-write` 沙箱在 Windows 没有可用后端（`no sandbox backend is usable on this host`）
     → 必须 `env.DSH_PERMISSION_MODE = "danger-full-access"`"。⚠ **今天实测推翻**：Windows 后端包
     `@deepseek-ai/dsh-sandbox-windows-acl`（靠 `koffi` 3.3.1 FFI 调 Win32 受限令牌）**在本机可用** ——
     直接跑它的 runner（read-only 包 `cmd /c exit 0`）**exit 0**；把 dsh 用 `workspace-write` 起一轮也**正常**。
     那条报错源码仍在（`dsh-sandbox/lib/index.js` 的 `SANDBOX_UNAVAILABLE`，属"绝不静默降级"的设计），当前**不触发**。
   - **现在的配置 = `workspace-write`**（`_acp_agents.json` → `dsh.env.DSH_PERMISSION_MODE`）。实测行为：
     **工作区（+ 系统 TEMP）内免问直接做；区外会发 `session/request_permission`（"提高等级"）**，拒了就写不进去
     ⇒ **面板权限等级 / 群聊拥有者审批管得住 dsh**（2026-10-12 用真实注册表复测：区外写 → 1 次申请 → 拒绝 → 文件未创建）。
   - ⚠ **2026-10-19 复核发现过一次回归**：当前 `_acp_agents.json` 里又变成了 `danger-full-access`
     （根因：**旧的 `_acp.json` 里一直留着 full-access** —— 注册表一旦被重建/覆盖，bootstrap 会从它把 dsh 收回来）。
     已同时改回：`_acp_agents.json` **和** `_acp.json` 都写 `workspace-write`（各自留了
     `.bak-before-dsh-sandbox` 备份）；并当场用真实 dsh 复测：
     **工作区内 → `DSH-OK` 正常；区外写 → 弹出「工具调用」申请 → 拒绝 → dsh 自报"被文件沙箱拒绝"，文件未创建**。
     ⇒ 现在配置与文档一致；**以后如果 dsh 又"不问就干"，先查这两个 json 的 `DSH_PERMISSION_MODE`**。
   - **★代码级兜底（`0.2.3` 起，不再只靠手动改配置）**：
     ① `agents.load_registry()` / `update_agent()` / `add_agent()` **读/写时就地纠正**危险值并落盘；
     ② `engine.config()` / 本地协作 Runner / 群聊桥在**拼子进程 env 前再兜一次**；
     ③ `agents.public()` 回 `dangerous / dangerousNote`，agentlist **红字**提示。
     显式放行（默认关）二选一：`runtime/state/_security.json` → `{"allowDangerousAgents": true}`，
     或 env `COCRAFT_ALLOW_DANGEROUS_AGENTS=1`；放行后仍会标红警告。
     每次纠正写一行 `runtime/logs/security.log`。回归 `tools/agent_safety_test.py`（22 项）。
     ⚠ 同一个变量**也**决定审批策略（`cordis.default.yml`：`danger-full-access` → `policy:'never'`）→
     写 full-access 就等于"永不问"，等级与群审批对它**全部失效**（桥会给这种大脑加 `warn` 红字）。
     ⚠ 代价：可写集合 = 工作区 + `os.tmpdir()`（**临时目录永远可写**）；Windows ACL 是 **partial enforcement**
     （受限令牌保留 Everyone、NTFS 硬链接可绕）；fs 工具是**策略层围栏**（README 自己写明"不是安全边界"），
     bash 才是内核级（受限令牌）。沙箱不可用时它**拒绝执行**、**不会静默降级**（真遇到再切回 full-access）。
   - **★上下文估算（`0.2.4`）**：dsh 只在 `session/prompt` 结果里给 `usage`，不发 `usage_update`。
     面板现在用本轮 usage 估一个 `context {used, size:0, estimated:true}`（优先 `totalTokens`，
     否则 input+output），并推 `session.usage.update` → 输入框显示 **「上下文 ~N」**。
     `size` 保持 0（**不编窗口上限**，所以不显示「剩 X%」）；真实 `usage_update` 到达时绝不覆盖。
   - **斜杠命令**：dsh-acp **没有实现 `available_commands_update`，也没有命令解析** →
     技能弹窗里的「斜杠命令」永远为空（现在是如实说明，不再说"跑过一轮就有"）。
     要真正有命令只能改 dsh-acp（fork/PR）或加面板自己的快捷命令（那不是 dsh 的命令）。
   - `spawn bash` 需要 **Git 的 bash 在 PATH**：本机 Git 装在 `D:\Git`，而 PATH 只有 `D:\Git\cmd`。
     **已改成自动探测**：`process.py` 的 `_git_bin_dirs()`（按 PATH 里的 git.exe 反推 + 常见安装位置）
     + `_inject_git_path()` 在起子进程时自动把 `D:\Git\bin`、`usr\bin`、`mingw64\bin` 等补进 PATH —— **不用手配**。
     （首记把"满屏 `error bash`、dsh 内部 turn 崩"归给权限模式；那更像 **Git bash 不在 PATH** 造成的 —— 两者现在都已修。）
   - ⚠ `env` 是**叠加**到进程环境的（见 process.py）。
8. **`bool({})` 是 `False`**（2026-09-26）：Codex 宣告能力用**空对象**（`list: {}`），
   `bool(caps.get("list"))` 会把"支持 session/list"误判成"不支持"→ 只能判 `"list" in caps`。
9. **`0 == False`**（同上）：拼工具输出尾注时写 `if v2 not in (None, "", False)`，
   结果 `exit_code=0` 被当成空值丢掉。判空要用 `v2 is None or v2 is False or v2 == ""`。
10. **工具输出对象直接丢给前端** → `String({...})` 变 `[object Object]`，面板里工具输出全白读
    （Codex 的 `rawOutput` 是 `{formatted_output, exit_code}`）→ 加 `normalize_output()` 统一转字符串。
11. **`tool_call` 自带 `status:completed` 被无视**（同上）：Codex 大量工具（读文件、web 搜索、
    上下文压缩）是一次性 `tool_call` 且已完成；以前一律当 running → 面板上工具永远转圈、
    也没有 `session.tool.success` 事件。
12. **子进程 stdout 编码**（同上）：ACP 规定 JSONL 是 UTF-8（读侧就是 utf-8 解码），但 **Python 写的
    agent** 在 stdout 是管道时用系统 ANSI 代码页（本机 GBK）→ 中文全变 `U+FFFD`。
    修法：`process.py` 给子进程加 `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1`（Node 系不受影响）。
13. **两个测试工具改 `_acp.json` 早已失效**（同上）：`tools/acp_demo.py` / `tools/acp_engine_ui_test.py`
    一直靠改 `runtime/state/_acp.json` 指向假 agent，可引擎**早就改成读注册表** `_acp_agents.json` →
    假 agent 根本没被用上、这两个工具**真的在连你本机的 Codex**。现在都改成**进程内猴补丁 `agents.load_registry`**
    （不写任何配置文件）。
14. **`provider` 推断的两个陷阱**（同上）：① 别扫**共享基线**的 env（一个 `DEEPSEEK_API_KEY` 会把所有 agent 带偏）；
    ② 别把**空值**的 env 键当依据（`OPENAI_API_KEY: ""` 曾把任意 agent 猜成 `openai`）。
15. **`/api/session/remote` 被通用路由吃掉**（同上）：`^/api/session/([^/]+)$` 会把 `remote`
    当会话 id → 这类"字面量子路径"必须排在通用正则**之前**判（`tools/acp_api_test.py` 专抓）。
