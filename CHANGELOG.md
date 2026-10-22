# Changelog

> 版本号按 SemVer；`0.x` 期间次要版本可能包含行为变化。
> 每个版本都要求：`check_features` 0 缺失 + `tools/run_suite.py` 0 失败 + 干净机/覆盖升级验证。

## 0.2.13 — 2026-10-22

**定位**：权限不再"发一封邮件审核 40 次" —— 每个节点可以有自己的权限等级，破坏性操作仍然拦住。

### 节点级权限（per-member `permLevel`）
- 成员多一个 `permLevel`：`""`（跟随面板）/ `ask` / `read` / `edit` / `exec` / **`trust`**。
- **`trust` = 非破坏性操作自动放行**（读 / 写 / 执行 / 联网）；**破坏性操作永远要人确认**，且放行前先进隔离区。
- 保存位置：工作流 `members[].permLevel`（下次一键跑继承）→ 启动时快照进 run；
  运行中可用 **`POST /orchestrate/member {run, member, permLevel}`** 改（只影响下一条申请）。
- UI：画布检查器加「权限」下拉；权限抽屉里逐成员下拉（立即生效）。

### 权限面板 / 运行详情
- 待批权限从"38% 高的横条"改成 **右侧 620px 宽抽屉**（近实底 + 模糊，命令原文完整可读）；
  主界面只留窄条「N 条待你批准 · 去处理…」，任务动作里也有「权限 · N」入口。
- 运行详情**默认只显示选中的成员**（成员选择条）；点「⊞ 并排」才铺开全部；「推荐 X」跳列会自动切过去。

### 回归
- `tools/orchestrator_test.py` 57 → **64 项**（permLevel 保存/归一 / 运行中改 / trust 非破坏性放行 / trust 破坏性仍排队）。
- `tools/collab_ui_test.py` 50 → **58 项**（默认只看选中成员 / 并排开关 / 窄条 / 抽屉宽度 ≥380 / 成员权限下拉 POST）。
- `check_features` 1698 → **1711 项 / 缺失 0**；出图 `resources/screenshots/collab.png`、`collab_perm.png`。
- ⚠ 后端改了（orchestrator + server）→ 已重启面板服务；前端 bump `0.2.13`（`Ctrl+Shift+R`）。

## 0.2.12 — 2026-10-22

**定位**：入口收口 + 工作台里的 codex 不再被它自己的审批模型拦死。

### 入口收口
- 删掉一次性「＋ 新建任务」按钮 / 弹窗 / 函数 / 死样式；**工作流是唯一入口**：
  左栏「工作流 ＋」新建，画布快速任务条做一次性开跑（不另存流程）。
- 运行历史与运行详情保留（那是某一次执行的记录，不是旧版残留）。

### 修「工作台里的 agent 没联网 / 不会干活」
- 根因：注册表 `baseline.mode` 丢了 → Codex 用默认 `agent`（Approve for me）→ 每条命令交内建
  `codex-auto-review` 审，而 DeepSeek 不认这个模型 → 所有 exec 被拒。
- 新增 `agents.effective_baseline()`：**agent 自己的 `mode` 覆盖基线**（工作台 / 群聊桥也走它）；
  注册表给 `codex-node` / `codex-client` 写 `mode: "read-only"`。
- 实测：`Get-ChildItem` 弹一次待批后执行成功；`curl` 联网成功。

### 回归
- `tools/orchestrator_test.py` 55 → **57 项**；`check_features` 1699 → **1698 项 / 缺失 0**。

## 0.2.11 — 2026-10-22

**定位**：本地协作从「一次性任务」升级成**可复用的工作流 + 画布**（不用每次新建，也看得见一套流程）。

### 新增：工作流成为一等公民
- 新文件 `runtime/state/_collab_workflows.json`：`{name, cwd, taskTemplate, members[], layout{zoom,nodes}, lastRunId, runCount}`。
- 后端：`GET /orchestrate/workflows`、`POST /orchestrate/workflows/save|delete|duplicate`。
- `/orchestrate/start` 可带 `workflowId`：**成员 / 目录 / 标题取工作流**，启动时把成员**快照**进 run（之后改工作流不影响历史）。
- `/orchestrate/runs` 与 `/run` 回 `workflowId / workflowName`；工作流记 `lastRunId / runCount`。
- 工作流列表按项目分组；**删除工作流不删运行记录**（历史保留，只是不再归属）。

### 新增：画布（编辑器 + 启动器，不是 DAG 引擎）
- 任务节点 + 成员节点 + **reviewer 虚线**；连线由角色推导（fan-out 执行模型没变）。
- 拖动节点排版（布局落盘、600ms 防抖）、滚轮缩放 0.6~1.6、空白拖拽平移、自动适应窗口。
- 检查器：成员改 **角色 / 名字 / 大脑**；任务节点改 **工作流名 / 目录 / 默认任务**；改完自动保存。
- 加成员 / × 删成员（至少留一个、最多 8 个）；双击空白也能加成员。

### 新增：不用每次走新建流程
- 画布顶部**快速任务条**：输入 + **Enter/▶** 直接开跑（留空 = 用默认任务）。
- 左栏工作流 **▶** 用默认任务一键跑；运行详情 **「← 工作流」** 回到画布。
- 打开工作台**默认落在工作流画布**；没有工作流时才退回运行历史。
- 运行历史可按工作流筛（「全部」恢复），每条 run 带工作流小标。

### 回归
- `tools/orchestrator_test.py` 40 → **55 项**：工作流 CRUD / 同 id 更新 / 布局落盘 / 从工作流启动用其成员与 cwd / run 带 workflowId / lastRunId+runCount / 复制 / 删除不删 run / 非法 id / 不存在的工作流。
- 新增 `tools/workflow_canvas_ui_test.py`（**14 项**，无头）：默认选工作流 / 项目分组 / 节点+连线 / 检查器改角色 / 拖动后保存 / ＋成员 / 快速任务带 workflowId / ← 工作流 / ▶ 一键跑 / 新建。
- `tools/collab_ui_test.py` 50 → **51 项**（stub 里补 `workflows: []`，无工作流时保持旧行为）。
- `check_features` 1677 → **1699 项 / 缺失 0**；`run_suite` 32 → **33 套**（全绿）。
- 出图 `resources/screenshots/workflow_canvas.png`。
- ⚠ 后端改动（orchestrator + server）→ 已重启面板服务；前端 bump `0.2.11`（需 `Ctrl+Shift+R`）。

## 0.2.10 — 2026-10-22

**定位**：ACP 引擎**一轮跑着的时候也能补充新条件**（排队，不再干等整轮）。

### 修复：ACP 忙时直接 409
- 根因：`AcpService.prompt()` 在 `s["turn"]` 非空时 `raise BusyError` → 接口 409「上一轮还在生成」。
  OpenCode 引擎本来就有 inbox 队列（`inbox.enqueued/delivered`），ACP 没有。
- 现在：忙时把消息**排队**（立刻落库 `queued:true`），本轮结束**自动投递**；返回 `{queued:true, position}`。

### 细节
- 队列上限 `MAX_INBOX = 20`；超了明确拒绝（不无限堆）。
- 投递复用同一条用户消息（不重复插），推 `session.inbox.enqueued` / `session.inbox.delivered`。
- 时间戳单调递增：同一毫秒连发也不乱序；中途补充**永远排在这一轮回答之后**。
- 队列只在内存：重启后没发出去的标 `undelivered:true`，**不自动补跑**（界面红字「未送达」）。
- `opencode-acp` 历史读共享库 → `_pending_local_msgs()` 把本地排队/未送达消息合并进 `/message`，否则界面看不到。
- 前端：「已排队」小圆片 / 「未送达」红字；被排队时横幅「已加入队列（第 N 条）」，**不是发送失败**。

### 行为说明
- 排队 = **这一轮结束后立刻发**，不是打断当前这一轮（ACP 协议不允许并发 prompt）。
- 真要立刻改变方向：点「■ 中断」；队列里的下一条会在中断后继续发送。

### 回归
- `tools/acp_events_test.py` 48 → **59 项**：排队返回 position / enqueued / delivered / 复用消息 / 时间戳单调 / 队列上限 / 重启未送达。
- 新增 `tools/inbox_queue_test.py`（**6 项**，无头）：已排队 / 未送达 / POST 带正文 / 横幅提示 / 无 error-box / 输入框清空。
- `check_features` 1668 → **1677 项 / 缺失 0**；`run_suite` 31 → **32 套**（全绿）。
- ⚠ 后端改动（service/engine）→ 已重启面板服务；前端 bump `0.2.10`。

## 0.2.9 — 2026-10-21

**定位**：本地协作工作台**看得懂「哪一组 agent 工作流属于哪个项目」**。

### 修复：运行历史没有项目信息
- 根因：左栏用的 `orchestrator.list_runs()` **不返回 `cwd`**（只有单条详情 `get_run()` 带）→ 多组工作流只能看任务名，分不清项目。
- 现在 `list_runs()` 带回 `cwd`。

### 新增
- 左栏每条历史带**项目徽章**：工作目录末段（`D:\work\foo` → `foo`，带文件夹图标）；没有目录写「未指定项目」。
- 左栏顶部**按项目筛选**：`全部 N` + 每个项目 `名字 N`；只有一个项目时自动不显示。
- 左栏计数：`3 条 · 3 个项目 · 1 运行中`（筛选时再加「筛出 N」）。
- 任务头明确写 `项目 <名字> <完整路径>` + **「打开」按钮** → 直接在编码模式打开该项目目录。
- `codeOpenAt(root, "")`：空路径 = 只打开项目根目录（列文件树）。

### 回归
- `tools/orchestrator_test.py` 39 → **40 项**（历史列表必须带 `cwd`）。
- `tools/collab_ui_test.py` 44 → **50 项**：3 条历史各有项目徽章（含「未指定项目」）/ 4 个筛选 chip / 点 ws2 只剩 1 条 / 点全部恢复 3 条 / 任务头项目 + 完整目录 / 「打开」按钮真实可见。
- `check_features` 1661 → **1668 项 / 缺失 0**；`jsbalance` / `check_ids` 全绿；出图 `resources/screenshots/collab_projects.png`。
- ⚠ 后端改动（`list_runs`）→ 已重启面板服务；前端 bump `0.2.9`。

## 0.2.8 — 2026-10-21

**定位**：方向 3「信任面板」收尾 —— 把「谁批准了什么 / 什么还能撤 / AI 刚才干了什么 / 仲裁推荐了谁」全摆进同一个面板。

### 新增：群聊审批时间线（落盘审计）
- 后端每次权限申请**结论落盘** `runtime/state/_cc_perm_log.json`（上限 500 条，重启不丢）：
  `{ts, chat, aid, agent, owner, kind, title, cmd, paths, destructive, verdict, by, reply}`。
- `verdict` = `once / always / reject`；`by` = `owner / timeout / no-ctx / send`。
- **只记真正需要人拍板的事件**：等级自动放行的操作**不写审计**（写进去会把真正的审批记录冲掉，而且每个读/命令都重写整份文件）。
- **保留命令原文 / 路径 / 拥有者 id / 回复原话** —— 「谁批准了什么」能追溯到具体那一条，不是只给统计。
- 写审计失败**静默**（审计不是权限流程的前置条件）。
- 新接口 `GET /ccagent/permissions?limit=80`（只读，新 → 旧）。

### 新增：信任面板里的其余三块
- **可撤销**：读 `/quarantine/list` + `/snapshot/list?scope=<会话>`；「打开」**复用**现有 `#quar` / `#undo` 弹窗（不另造一套）。
- **agent 活动**：读 `/ccagent/activity`，中文状态（完成 / 进行中 / 等待批准 / 已拒绝 / 超时）+ 谁 + 干了什么 + 时间；
  明说**内存缓冲、面板重启后从零开始**。
- **派活 / 仲裁结论**：读 `localStorage["cocraft.cc.decisions"]`（推荐谁 + 任务 + 审查者 + 时间）。

### 修复：切 ACP agent / 切引擎不再被模型清单拖住
- 0.2.7 为修跨引擎模型事故，在切换后 `await` 了模型清单重拉；而 ACP 的 `/api/model` 要先
  `ensure_model_config()`（**resume 最近会话，最多试 5 条 + 冷启动**，实测秒级）→ 切 agent 明显卡顿。
- 现在：**同步作废清单**（`resetModels` → `MODEL.list = null`）后**后台重拉，不 await**；
  清单为空期间 `normalizeModelRef()` 返回 `null`（不猜）、`writeSessionModel()` 不写非法引用 → 安全不打折。
- 回归加了一条硬断言：把 `/api/model` 拖到 **900ms**，切引擎仍在 **<600ms** 返回（实测 0ms）。

### 回归
- 新增 `tools/perm_audit_test.py`（**35 项**，完全离线）：字段完整 / 新→旧 / 上限 / 坏文件回空不抛 / 写失败不挡流程 / 等级自动放行**不写**审计 /
  **真走批准·拒绝·超时·破坏性分支**（破坏性「可以」不算、「确认」才放行且先隔离；别人/不 @ 不算）+ no-ctx / send 失败也留痕。
- `tools/trust_panel_test.py` 21 → **28 项**：四块内容 + 结论配色 + 「打开」复用隔离区入口 + **接口返回错形状也不崩**。
- `tools/cross_engine_model_test.py` 15 → **17 项**（新增「切引擎 / 切 ACP agent 都不等模型清单」）。
- **顺手修：agent 列表本身不再每 15 秒重扫**。`detect_all_cached()` 原来是 **15s** 缓存 —— 一旦过期，
  `GET /engine/acp/agents` 就同步扫盘（本机实测 **7~9s**，打开 agentlist / 切 agent 都像卡死）。
  现在缓存 **5 分钟**（从扫描完成时开始计时）+ **服务启动后台预热** + 并发只扫一次；
  只有「扫描本机 / 新增 agent」这种显式发现才 `invalidate_detect_cache()` 重新扫。
- `check_features` 1641 → **1661 项 / 缺失 0**；`run_suite` 30 → **31 套**；`acp_api_test`（82）/ `acp_events_test`（48）全绿。
- 出图 `resources/screenshots/trust_audit.png`。
- ⚠ 后端改动（新路由 + 审计）→ 已重启面板服务；前端 bump `0.2.8`。

### 已知边界
- **agent 活动只在内存**（桥的 `deque(maxlen=200)`）：面板/后端重启后清空。要做长期审计需要落盘（下一步可选）。
- **per-agent 权限上限**仍未做：群聊成员与面板共用同一份 `_perm_level.json`。

## 0.2.7 — 2026-10-20

**定位**：修用户报的严重问题 —— **切引擎后把 ACP 的模型引用写进 OpenCode 会话**，导致提问发出去**没有任何回复**（那一轮 17ms 就 `idle`）。

### 修复：模型清单必须绑定引擎
- 事故链（三段叠加，缺一不可）：
  1. `setEngine` 换引擎时**不清 `MODEL.list`** → 还是 ACP 那份；
  2. `openSession` 末尾的 `repairStaleModel` 拿**旧引擎清单**判「会话模型失效」→ 把合法引用误判成失效；
  3. `normalizeModelRef` 分支③**只比 `id`、不比 `providerID`** → 把 ACP 的中性 `providerID:"acp"` 原样吐出来 → `writeSessionModel` 照单全收 POST。
- 现在：`MODEL.engine` 标记清单属于谁；`setEngine` **先作废旧清单并重拉**（赶在 `openSession` 之前）；
  `loadModels` 请求期间引擎变了就**丢弃结果**、失败也不再沿用旧清单；
  `normalizeModelRef` / `modelAvailable` / `ctxLimitOf` 一律**只认当前引擎**的清单，跨引擎**不猜**；
  `writeSessionModel` 加**最后一道闸**（引擎不认的引用 → 不写 + 提示「请点「模型」另选一个」）。
- 换 ACP **agent** 也作废模型清单（每个 agent 能用的模型不同，同一类问题）。
- 顶栏 / 模型弹窗会提示「当前引擎不认这个模型」，不再把它当权威展示。

### 上下文显示
- 切引擎后**立刻清掉旧引擎的上下文数字**，不再拿旧模型上限算百分比。
- 两个引擎统一口径：窗口上限只在**当前引擎**的模型清单里找；agent 只给 `used` 时，能补到真实上限就显示「剩 X%」，补不到仍是「~N」（**不编数字**）。
- **ACP 侧终于有数了**：以前从 OpenCode 共享库读历史（`_opencode_history`）**只搬了 content/model，把 `tokens` 丢了**
  → ACP 里看同一条会话，上下文条显示「—」、会话详情 Token 全 0。现在 `tokens` / `context` 一并带过来，
  两个引擎看同一条会话的用量口径一致（窗口上限仍以各自引擎的清单为准）。

### 回归
- 新增 `tools/cross_engine_model_test.py`（**15 项**，无头真跑 app.js）：切引擎后清单跟着换、写之前清单已是新引擎、
  **绝不出现 `providerID:"acp"` 的 POST**、非法引用被拒且不发请求、跨引擎清单不猜、本地存的旧引用不会被当默认模型。
- `check_features` 1632 → **1641 项 / 缺失 0**；`run_suite` 29 → **30 套**；`jsbalance` / `check_ids` / `acp_api_test`（82）/ `acp_events_test`（48）全绿。

## 0.2.6 — 2026-10-20

**定位**：把「群聊 AI 成员 / 本地协作」的**内部会话**从「接续」列表里分出去，项目会话不再被几十条角色提示词淹没。

### 改进：接续弹窗分「项目会话 / 内部会话」
- **默认只看项目会话**；内部会话收进第二个标签（带条数），不再和项目混排。
- 内部会话识别三路（尽量不误伤、也不漏）：
  1. 桥的独立会话文件 `_cocraft_bridge_sessions.json`（id + agentSessionId）；
  2. 本地协作每个成员的 `_orchestrator/runs/<run>/<member>.sessions.json`；
  3. 标题特征兜底：`【你的角色：` / `你是群聊成员` / `【最近的群聊记录】` / `群聊AI·` / `协作·` / `【仲裁】` / `你是本次任务的`。
- 后端 `list_remote(include_internal=False)` 默认过滤；`GET /api/session/remote?internal=1` 给全量并带 `internal` 标记与 `internalCount`。
- 前端接续内部会话前会二次确认（避免把它误当项目会话接进左栏）；`import_remote` 自己查 agent 侧时用 `include_internal=True`，不会因为过滤而找不到 cwd。
- 顺手把角色提示词里的「群里可能有别的角色…」改成「同一任务里可能有别的角色…」——本地协作不是群聊，措辞不再误导。

### 回归
- 新增 `tools/internal_sessions_test.py`（**19 项**，完全离线）：桥 / Runner 落盘 id、标题特征、15 秒缓存与失效、
  `list_remote` 默认 2 条项目 + `include_internal` 4 条带标记。
- `tools/acp_events_test.py` 46 → **48 项**（默认 2 条项目 + `internalCount=1` + `include_internal` 3 条带标记）。
- `tools/acp_api_test.py` 80 → **82 项**（`?internal=1` 路由与过滤）。
- `tools/acp_engine_ui_test.py` 增加标签断言：项目 2 / 内部 1 / 点内部标签只显示 1 行 / 切回项目恢复 2 行。
- `run_suite` 28 → **29 套**；`check_features` 1616 → **1632 项 / 缺失 0**。

### 已知边界
- 更老的内部会话如果**标题不含特征、落盘文件也已被删**，仍可能出现在项目标签里（没法从 agent 侧可靠反推来源）；
  新会话有三路识别，基本不会漏。

## 0.2.5 — 2026-10-20

**定位**：本地协作工作台补上**运行历史管理**，并把「能不能同时跑多个任务」说清楚。

### 新增：删除运行记录
- 左栏每条历史右侧新增 **×**（悬停显现）；删除已结束任务时二次确认。
- 后端 `POST /orchestrate/delete {id}`：删 JSON + 该任务的成员会话文件（`runs/<id>/`）。
- **跑着的 / 还在收尾的任务拒绝删除**（先「取消全部」，等进程退出）；
  非法 id（`..\` 之类）直接拒绝，不越界删文件。
- 删的是当前选中的任务时，自动切到剩下最新一条；删空了回到空态。

### 改进：多任务并行可见
- **后端本来就支持同时跑多个任务**（每个成员独立 `AcpService` + 会话，互不阻塞）；
  以前只是 UI 只显示一条、其他任务的状态不刷新。
- 左栏每 **4 秒**轻刷一次历史 → 不选中的任务也能看到「完成 / 失败 / **运行中**」变化。
- 左栏标题显示 **「N 条 · M 运行中」**；新建任务时会提示「现在还有 N 个任务在跑，可以再开一个」。
- 顺手修了：全部成员都取消时，`run.status` 会被 `_update_run` 写成 `failed`（应为 `cancelled`）。

### 回归
- `tools/orchestrator_test.py` 30 → **39 项**：并发两任务同时 running + 各自取消、
  删除已结束、拒绝跑着的、非法 id、取消收尾后可删。
- `tools/collab_ui_test.py` 39 → **44 项**：历史 3 条 / 「N 运行中」/ 跑着的不能删 /
  开始新任务后「2 运行中」/ 删除 + 行消失。
- `run_suite` 仍 **28 套**；`check_features` 1604 → **1616 项 / 缺失 0**。

## 0.2.4 — 2026-10-20

**定位**：把 dsh 的「上下文 —」补成**可读的估算值**；同时纠正「没有斜杠命令」的误导文案。

### 改进：dsh 这类 agent 的上下文显示
- **问题**：dsh 只在 `session/prompt` 结果里给 `usage`，**不发 `usage_update`** → 面板一直显示「上下文 —」。
- **修法**：`_record_usage()` 用本轮 usage 估一个 `context {used, size:0, estimated:true}`
  （优先 `totalTokens`，没有就 `inputTokens + outputTokens`），并推一条 `session.usage.update` 事件。
  前端显示 **「上下文 ~N」**（`~` 表示估算），tooltip 写明「本轮 tokens 估算；窗口上限未知」。
- **不编窗口上限**：`size` 保持 0 → 不显示「剩 X%」；有真实 `usage_update` 的 agent 不受影响（绝不覆盖）。
- 顺手修了一个真 bug：`s["tokens"]` 是空字典时 `setdefault(...) or {}` 会**新建一个字典丢掉**，
  导致空 tokens 的会话 token 累加不进去；现在直接拿原字典改。

### 文案
- 「技能 ▾ → 斜杠命令」为空时，不再说「ACP 一般要跑过一轮才拿得到」——
  改成「这个 agent 没有上报斜杠命令（ACP 里不是每个 adapter 都实现；dsh 就没有）」，
  并指路顶栏「模式 / 模型」。

### 回归
- 新增 `tools/context_estimate_test.py`（13 项，离线）：totalTokens/input+output/全 0、
  写入 estimate、真实 context 不覆盖、estimate 下一轮更新、tokens 累加。
- `tools/context_chip_test.py` 14 → **16 项**（dsh 估算显示 + tooltip）。
- `run_suite` 27 → **28 套**；`check_features` 1596 → **1604 项 / 缺失 0**。

### 仍然做不到
- **dsh 的斜杠命令列表**：dsh-acp 没有实现 `available_commands_update`，也没有命令解析；
  面板没有数据可列。要真正有命令只能改 dsh-acp（fork/PR）或加"面板自己的快捷命令"（不是 dsh 的命令）。

## 0.2.3 — 2026-10-20

**定位**：把 dsh 的「全权限回退」从"手动改配置"升级成**代码级兜底** —— 以后不会再悄悄回到 `danger-full-access`。

### 修复：dsh 可能静默退回「永不问」模式
- **现象**：`_acp_agents.json` 里 dsh 又变成 `DSH_PERMISSION_MODE=danger-full-access`。
  这种模式下 dsh **根本不发权限请求** → 面板权限等级 / 群聊拥有者审批全部失效。
- **根因**：旧的 `_acp.json` 里一直留着 full-access；注册表一旦重建/覆盖，bootstrap 会把 dsh 连同它收回来。
- **三层兜底**：
  1. **读/写注册表时**（`load_registry` / `update_agent` / `add_agent`）就地改回 `workspace-write` 并落盘；
  2. **拼子进程 env 前**（`engine.config` / 本地协作 Runner / 群聊桥）再兜一次；
  3. `public()` 返回 `dangerous` / `dangerousNote`，agentlist 直接**红字**提示。
- **显式放行**（默认关，必须用户主动开）：
  `runtime/state/_security.json` → `{"allowDangerousAgents": true}`，或 env `COCRAFT_ALLOW_DANGEROUS_AGENTS=1`；
  放行后仍会在 agentlist / 群聊里标红警告。
- **自愈日志**：`runtime/logs/security.log` 记一行「谁从什么改成了什么」。
- **实测**：临时把真实注册表写回 `danger-full-access` → 调一次接口 → 自动改回 `workspace-write`；
  真实 dsh 工作区内回 `SAFE`；区外写 → 权限申请 → 拒绝 → dsh 自报沙箱拒绝、文件未创建。

### 回归
- 新增 `tools/agent_safety_test.py`（22 项，完全离线，临时注册表）：
  load 自愈 / update 拒写 / 拼 env 兜底 / 其它 agent 不动 / public 标记 /
  显式放行 / 旧 `_acp.json` bootstrap / 默认不放行。
- `run_suite` 26 → **27 套**；`check_features` 1581 → **1596 项 / 缺失 0**。

## 0.2.2 — 2026-10-19

**定位**：会话界面的**回位与导航** —— 长对话里不再"翻上去就回不来"。

### 新增：消息导航
- 消息区右下角新增 **「回到底部」**：离开底部 >80px 时出现，点一下恢复跟随最新输出（未读数不会再有"看不见"）。
- **「问题」→ 问题导航**：列出自上次加载以来的每次提问（序号 / 摘要 / 时间），
  点一行**跳到那一轮**并让该轮闪一下；当前阅读的那一问高亮。
- **上一问 / 下一问**：弹层里的 ↑ / ↓，或直接 **`Alt+↑` / `Alt+↓`**（输入框里不抢键）。
- **「回到本问」**：长回答读一半时，一键回到**当前视口所在那一轮**的提问。
- 刚发出去、还在 pending 的提问也会出现在列表里（列表取自 DOM，重建/翻页都跟着变）。

### 细节
- 滚动同步用 **`setTimeout` 40ms 节流**（不用 rAF：无头/虚拟时间下 rAF 不推进，会卡在"已排程但没执行"）。
- 平滑滚动尊重 `prefers-reduced-motion` / 自动化环境（直接落位）。
- 导航不是功能前置：计算失败只安静跳过，绝不挡住消息。

### 回归
- 新增 `tools/msg_nav_test.py`（17 项，无头真跑 app.js，6 轮提问 + 长回答）：
  底部隐藏 / 上滚出现 / 回到底部 / 6 行列表 / 当前高亮 / 点行跳转 + 闪 / 回到本问 /
  Alt+↑↓ / Esc / 点外面 / 零 JS 报错。
- `tools/run_suite.py` 25 → **26 套**；`check_features` 1562 → **1581 项 / 缺失 0**。

## 0.2.1 — 2026-10-19

**定位**：把「本地协作」从只有 Runner/API 推到**可用的工作台 UI** —— 多智能体不再必须先登录群聊。

### 新增：本地协作工作台（顶栏「协作」）
- **全页工作台**（像音乐主页那样替换消息区）：左栏运行历史，右栏任务详情。
- **新建任务**：任务文本 + 工作目录（可选 / 原生选文件夹）+ **成员行（角色 / 名字 / 引擎）**
  + 内置预设（开发+审查+测试 / 对拍 / 调研+审查）一键填；1~8 个成员。
- **成员列并排**：状态 chip（等待 / 运行中 / 完成 / 失败 / 超时 / 已取消）、Markdown 正文、
  tokens / 上下文、最近工具与权限活动；每个成员可单独取消。
- **待批权限逐条卡片**：成员 / 动作 / **命令原文** / 涉及路径 / 倒计时；
  **破坏性只给「确认执行」**（不显示「始终允许」），按钮 = 允许一次 / 始终允许 / 拒绝；
  处理完进入**审批历史**（谁批准了哪条、什么时候、命令原文）。
- **对比 / 逐行差异 / 仲裁**：成员列本身就是并排对比；正好 2 人完成时可逐行差异（LCS + 红绿）；
  「请它仲裁」再跑一个 reviewer，抠到「推荐 X」做成按钮，**点一下跳到那一列并高亮**。
- **取消 / 重跑 / 导出审计**：取消全部或单个成员；完成后用同样成员发新任务；
  一键导出 JSON（时间线 + 审批记录，不含成员正文）。
- **接线信任面板**：信任面板新增「本地协作」区（待批 + 最近任务），可直接跳到工作台处理。

### 后端
- `list_runs` 改为**内存 + 磁盘合并**（打开一条历史不会把其它历史挤掉），并返回 `done/failed/running` 计数。
- `catalog` 返回 `askTimeout / runTimeout`（前端据此显示待批倒计时，不再猜 180s）。
- 剥掉 Codex 自定义 provider 的 `Model metadata … not found` 噪音（工作台 / 仲裁 / 对比都拿干净正文）。

### 回归
- 新增 `tools/collab_ui_test.py`（39 项，无头真跑 app.js，stub `/orchestrate/*`）。
- `tools/orchestrator_test.py` 27 → **33 项**（catalog 超时、历史合并、列表计数）。
- `tools/run_suite.py` 增加这一套；`check_features` **1526 → 1562 项 / 缺失 0**。

### 已知限制
- 同一任务的成员**共享工作目录**（不做 worktree 隔离）→ 「每人改了什么」无法可靠归属。
- 表单（ACP elicitation）仍自动拒绝；权限是完整逐条排队的。
- 工作台只管**本机**；多人 / 多设备仍在群聊（顶栏「工具 → 在线协作」）。

## 0.2.0 — 2026-10-19

**定位**：把 `0.1.62` 之后积压的能力收口成"可公开下载的单机版"。

### 新增：可逆执行（安全网）
- **破坏性操作先隔离再放行**：命中破坏性命令时，拥有者确认后、执行前把目标复制进
  `runtime/quarantine/<scope>/<ts>/`（不信系统回收站）。
- **撤销这一轮**：每轮（默认关，设置里可开）落工作区快照 → 工具菜单列出「改 / 增 / 删 / 改名」
  + 行级 diff → 一键退回；删除"新增"前会先自动隔离，撤销本身也可逆。
- **隔离区界面**：列出记录、一键恢复（冲突先问）、删除、打开目录。
- **快照回收**：`KEEP_SNAPSHOTS` 裁剪 + 内容仓 GC + 2 GB 硬上限，避免长期运行爆盘。

### 新增：主动编排（多智能体）
- **角色预设**：实现者 / 审查者 / 测试者 / 调研者，**只注入本人提示词**。
- **派活台**：多选成员 → 一条 `@a @b 任务`（服务端按 mentions 各推 → 天然并行）+ 汇总卡。
- **多方案对比**：每人一列并排（Markdown）；正好 2 人时逐行差异（LCS，太长不算）。
- **结果仲裁**：请审查者按固定格式收口「结论：推荐 X」；「推荐 X」**可点跳列**，并记进结论历史。
- **一键建组**：3 个预设（开发+审查+测试 / 对拍 / 调研+审查）→ 建 agent → 进群 → 开应答；
  预设可**自定义存/删**；建完可**自动派第一活**。
- **对比带上文件改动**：派活前落只记路径+哈希的轮快照（不占磁盘）→ 每列显示「文件改动 · N 个」。

### 新增：信任面板（方向 3 第一块）
- 「权限 ▾ → 信任面板…」→ 权限 **5 档**集中展示（每档人话说明）+ 一键切换；放宽到 exec/all 二次确认，
  另有「立即收紧」。
- **待批准**：当前会话 + 群聊待批；「去处理 / 去群聊处理」。
- **最近「始终允许」**：逐条列出 `kind / 路径 / 会话 / 次数 / 时间`，可逐条撤销或清空全部；
  OpenCode 引擎不支持时明确说明。

### 新增：上下文余量
- 输入框左下角「上下文 剩 X%」：
  - OpenCode：模型 `limit.context` + 上一条回答的 `input + cache.read + cache.write`；
  - ACP：agent 上报的 `usage_update`（事件实时更新）；
  - 拿不到不编：只显示已用量或「—」并说明原因；≥80% 金 / ≥95% 红。

### 新增：安装与 agent
- agent 组件改为**可选**（node 跟着适配器装）；覆盖安装保留 `runtime\`，可随时补装。
- **面板内安装 agent**：直接 npm 安装三个常见适配器 → 自动认入口 → 注册；卸载不删空。
- 群聊「☰」菜单重排（380px + 竖向滚动；成员行两层；新建成员两行），不再裁控件。

### 修复
- **权限等级切换没写进去**：`setPermLevel` 的 POST body 未 `JSON.stringify`，实际发 `[object Object]`，
  后端回落成 `ask`；已修复并加断言。
- **OpenCode 删掉的会话仍留在 ACP**：载入/列会话时与共享 SQLite 对账清理；
  读不到库时不同步、本地草稿不碰。
- **`_is_opencode()` 误判**：不再把路径里的 `opencode-ui` 当 OpenCode。
- **`cfg-key-note` / `setup-key-note` 被 tooltip 逻辑删掉**：保存 Key 后看不到掩码提示。
- 上下文压缩 / shell / 模式切换 / 模型切换消息不再掉「未知结构」卡。
- 消息往上翻页、轨迹人话化、群聊未读/提醒等一批既有问题。

### 已知限制
- 仅 **Windows 10/11 x64**、单机、单用户。
- **可逆执行是尽力而为**：只保得住能识别出的目标；codex 不报路径、猜不出时如实记 `skipped`。
- 破坏性操作永远需要确认；`all` 档连工作区外也放行。
- 群聊 / 音乐使用非官方接口，随时可能变化；仅供个人使用。
- ACP agent 能力不一致：部分 agent 不上报上下文 / 权限路径。
- 安装包未随仓库发布；从 Releases 下载。

### 升级注意
- 从 `v0.1.62` 起可直接覆盖安装；`runtime\`（设置 / 会话 / Key / 主题）保留。
- 本次起信任面板、上下文条、可逆执行、主动编排才真正进入安装版。
