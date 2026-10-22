# 任意 DAG · 详细施工方案（工作流执行模型升级）

> 状态：**方案（2026-10-22，`ui 0.2.20` 基线）** · 尚未动代码
> 范围：**工作流执行模型**从「线性阶段」升级为「任意依赖 DAG」。
> 相关：`docs/工作流画布-方案.md`（P1~P3）、`docs/工作流阶段-方案.md`（P4 线性阶段）、`docs/工作台-能力补齐-方案.md`（另一个方案）
> 前置：线性阶段（`0.2.15`）已交付；**DAG 是它的超集**。

---

## 0. 为什么需要 DAG（线性阶段的局限）

### 0.1 现状：线性阶段

```
任务 → [阶段1: A,B] → [阶段2: C] → [阶段3: D]
       （阶段内并行；阶段之间整段同步）
```

能覆盖最常用的「实现 → 审查 → 测试」，但表达不了：

1. **分支不等长**：`A→B→D`，而 `A→C→E→D`（C 比 B 多走一步）。硬塞进线性阶段会"等齐"最慢的分支。
2. **条件分支**：A 输出"用方案一" → 走 B；否则 → 走 C。
3. **成员级依赖**：B 只等 A，不必等同一阶段的 C。
4. **多下游汇合**：A→C 且 B→C（C 等 A、B 都完成）。

### 0.2 DAG 能表达

任意**有向无环图**：节点 = 成员（或任务），边 = 依赖；入度为 0 的节点并行启动，依赖满足就继续。

---

## 1. 目标与非目标

### 目标
- **节点级依赖**（`dependsOn` / `edges`），支持分支、汇合、不等长分支。
- **并行调度**（拓扑排序 + 就绪即跑）。
- **条件分支**（边 `when`：success / failed / contains:xxx）。
- **画布拖线**（手动连线 + 环检测 + 删除边）。
- **兼容旧工作流**（`stages` 自动归一成 DAG）。

### 非目标（本轮不做）
- ❌ 无限嵌套子工作流（先做"节点 = 成员"，不做"节点 = 另一个工作流"）。
- ❌ 富条件表达式（先做 `success / failed / contains` 三种）。
- ❌ 阶段级超时（沿用每成员 `RUN_TIMEOUT`）。
- ❌ 与技能/模板联动（工作流工坊的事）。

---

## 2. 数据模型

工作流（`runtime/state/_collab_workflows.json`）：

```jsonc
{
  "id": "wf_...",
  "name": "实现→审查→测试",
  "cwd": "D:/work/foo",
  "taskTemplate": "把登录拆成独立模块",
  "nodes": [
    { "id": "n1", "type": "member", "engine": "codex-node", "role": "implementer",
      "name": "实现", "permLevel": "", "retries": 0, "onFail": "stop" },
    { "id": "n2", "type": "member", "engine": "opencode-acp", "role": "reviewer",
      "name": "审查", "dependsOn": ["n1"] },
    { "id": "n3", "type": "member", "engine": "claude-node", "role": "tester",
      "name": "测试", "dependsOn": ["n1"] },
    { "id": "n4", "type": "member", "engine": "opencode-acp", "role": "reviewer",
      "name": "汇总", "dependsOn": ["n2", "n3"] }
  ],
  "edges": [
    { "from": "n1", "to": "n2", "when": "success" },
    { "from": "n1", "to": "n3", "when": "success" },
    { "from": "n2", "to": "n4", "when": "success" },
    { "from": "n3", "to": "n4", "when": "success" }
  ]
}
```

- **`dependsOn` 与 `edges` 二选一**（推荐 `edges`，因为能带 `when`；`dependsOn` 作为简写，保存时生成 edges）。
- 边条件 `when`：`always`（默认）/ `success` / `failed` / `contains:<子串>`。
- 节点 `onFail`：`stop`（整个 run 停）/ `continue`（只跳过下游）。`retries`：0~2。

---

## 3. 兼容旧工作流（`stages` → DAG）

旧数据（`stages[]` + `members[].stage`）加载时自动归一：

1. 每个成员 → 一个 node。
2. 阶段 `i` 的节点 `dependsOn` 阶段 `i-1` 的**所有**节点（链式，保持"阶段串行、阶段内并行"的旧语义）。
3. `stages[].when / onFail / retries` → 映射到对应节点的 `when / onFail / retries`。
4. 保存时写 `nodes/edges`（同时保留 `stages` 只读兼容，或直接迁移）。

> **线性阶段 = DAG 的特例**（链式依赖），所以升级不会破坏旧工作流。

---

## 4. 执行语义（`backend/orchestrator.py`）

### 4.1 调度

```
1. 构建依赖图：node → 入度 / 下游列表。
2. 队列 = 入度为 0 的节点（并行上限 MAX_PARALLEL，默认 8）。
3. 就绪节点启动（每个成员独立 AcpService，已有）。
4. 节点完成 → 对每条出边：
   - 判断 when（看上游结果 / 输出）；
   - 满足 → 下游入度 -1；入度变 0 → 入队。
5. 所有节点终态 → run 收口（done / partial / failed）。
```

### 4.2 产出传递

- 节点完成后，收集其正文（`_stage_digest` 已有，每人 4000 / 总 12000 字封顶）。
- 下游节点的 prompt = `【前置产出：n1 实现 / n2 审查】…` + 本次任务 + 角色。
- **关键**：一个节点有多个前置时，合并所有前置产出。

### 4.3 条件分支

- 边 `when=success`：上游 `status=done` 才激活。
- 边 `when=failed`：上游 `status=failed` 才激活。
- 边 `when=contains:x`：上游输出里包含 `x` 才激活（用于"结论：推荐方案一"这类）。
- 被条件跳过（未激活）的下游节点 → `skipped`（**不计失败**，run 仍可为 done）。

### 4.4 失败 / 重试 / 取消

- `onFail=stop`：上游失败 → run 停（后面的 `skipped`）。
- `onFail=continue`：只跳过该节点的下游，其它分支继续。
- `retries`：只重跑失败节点（每次新会话），与线性阶段一致。
- 取消：不再启动新节点；跑着的 `interrupt`；未开始标 `skipped`。

---

## 5. 画布

### 5.1 交互
- 节点拖动（已有）。
- **拖线连节点**：从节点右缘拖到另一节点 → 建边；如果形成环 → 拒绝 + 提示。
- 删除边：点边 + `Delete`。
- 自动布局：按拓扑层级从左到右（分层），或应用户"排布"。
- 检查器：节点（角色/名字/大脑/权限/重试/失败策略）+ 边（条件 `when`）。

### 5.2 渲染
- SVG 边层（贝塞尔曲线 + 箭头 + 条件小标）。
- 运行态：节点颜色（等待/运行/完成/失败/跳过），边高亮（激活/未激活）。

### 5.3 环检测
- 每次拖线后做一次 DFS / Kahn 拓扑；
- 有环 → 撤销这条边 + 横幅「不能形成环」。
- 保存前再校验一次（防止程序写入坏数据）。

---

## 6. API

| 变更 | 说明 |
| --- | --- |
| `POST /orchestrate/workflows/save` | 支持 `nodes/edges`（仍然兼容 `stages`） |
| `POST /orchestrate/start` | 从工作流启动（`workflowId`），成员快照带 `nodes/edges` |
| `GET /orchestrate/run` | 回 `nodes/edges` 状态（节点状态 / 边激活） |
| `POST /orchestrate/workflows/validate` | **新**：校验 DAG（环检测），给 UI 拖线时用 |

---

## 7. 回归计划

| 测试 | 新增断言 |
| --- | --- |
| `orchestrator_test` | 拓扑调度（并行顺序）/ 多前置汇合 / 条件分支 success·failed·contains / 跳过不计失败 / 环检测拒绝 / 旧 stages 归一 / 重试 |
| `workflow_canvas_ui_test` | 拖线建边 / 边渲染 / 环提示 / 删除边 / 自动布局按拓扑层 / 检查器改边条件 |
| `check_features` | 新增约 10~15 条（nodes/edges / 拓扑 / 条件 / 环检测） |

---

## 8. 风险与工作量

| 风险 | 说明 | 对策 |
| --- | --- | --- |
| 环检测漏 | 拖线形成环 | 每次改边都校验 + 保存前再校验 |
| 拓扑调度复杂 | 比线性阶段多一层 | 用 Kahn 算法 + 明确状态机（wait/ready/running/done/failed/skipped） |
| 产出合并爆炸 | 多前置产出太长 | 每人 4000 / 总 12000 字封顶（沿用 `_stage_digest`） |
| 画布拖线体验 | 拖线、删除、环提示 | 先做"点节点 A → 点节点 B 建边"（比拖线简单可靠），拖线作为增强 |
| 兼容旧数据 | 旧 `stages` 坏数据 | 归一函数 + 单测（含空 stages / 未知 stage id） |

**工作量**：后端 **3~4 天**（数据模型 + 拓扑调度 + 条件 + 环检测 + 兼容 + 测试）+ 前端 **3~4 天**（画布拖线/边/环提示 + 检查器）≈ **1~1.5 周**。

---

## 9. 与线性阶段的关系

- **线性阶段是 DAG 的特例**（链式依赖），所以可以**渐进升级**：
  - 第一步：后台用 DAG 执行，`stages` 自动归一（UI 不变）；
  - 第二步：画布加"拖线建边"；
  - 第三步：检查器加"边条件"。
- 也可以"一步到位"：直接切 DAG + 画布拖线（风险集中在画布交互）。

> 建议：**先做"后台 DAG + 旧 stages 归一"（风险最低），再做画布拖线**，分两次交付。

---

> 一句话：**DAG 是执行模型的超集；先把"链式依赖"跑通（兼容旧 stages），再放开画布拖线，最后加条件分支。**
