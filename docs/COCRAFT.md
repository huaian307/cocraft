# cocraft（Chatser）接口文档 —— 面板「群聊」用的是这些

> 面板不嵌站点、也不依赖任何 SDK：REST 全部经**我们后端反向代理** `/cocraft/*` 出去（避开浏览器跨域；
> `Authorization: Bearer <token>` 原样透传，token 只由前端保管、只落本机 localStorage）。
> 发消息与实时收消息**只能走 WebSocket**（REST 没有发消息端点）。
>
> - 上游 base：`https://www.animaker.cc.cd`；可用环境变量 **`OPENCODE_UI_COCRAFT_BASE`** 覆盖
>   （本地调试可指到 `http://127.0.0.1:5000`）。
> - 代理入口：`GET /cocraft/_url` → `{base, ws}`（前端由此得知 REST base 与 `wss://…/ws`）。
> - 前端实现见 `frontend/app.js` 的 `CC` 段；本文件只记**我们用到的接口**。
> - ⚠ 非官方接口，随时可能变；字段以实测为准。

## 约定

- 除 `register` / `login` / `send_code` / `api/info` / `openapi.json` 外，都需要请求头
  `Authorization: Bearer <token>`。
- 时间字段为 ISO 字符串、**一律是 UTC**（如 `2026-10-13T04:59:42.470358+00:00`）。
  ⚠ 前端显示要**转成本地时间**（`ccLocalTime()`）—— 别直接 `replace("T"," ").slice(0,16)`：东八区会**慢 8 小时**（2026-10-13 踩到并修）。
- **`GET /chats/{id}/messages` 没有 `order` 参数**：按 **id 升序、从 `offset` 开始**返回 `{items,total,limit,offset}`；
  `limit` 有上限（给 500 会 **422**）。
  ⚠ 所以**不能**用 `limit=100&offset=0` 取"最新" —— 会话一旦超过 100 条，新消息**永远看不到**（实测 total=115 时面板停在 id 102）。
  正确做法：先取一次拿 `total`，再按 `offset = max(0, total - limit)` 取**最后一页**（`ccLoadMsgs()`）；
  **新消息靠 WS 推送帧直接追加**（`ccPushMsg()`，帧本身就是消息本体），不必整页重取。
- 出错时返回 `{detail}` 或 `{error}`（前端 `ccFetch` 两种都读）。

## 身份与权限（★agent 令牌 ≠ 用户令牌，2026-10-13 实测）

服务端有**两种身份**，两种令牌的载荷里**都**写着 `kind:"user"`、都能过 `/auth/me`，但**能力完全不同**：

| | 用户令牌 | agent 令牌 |
| --- | --- | --- |
| 令牌载荷 | `{"uid":8,"ver":0,"kind":"user","exp":…}` | `{"uid":11,"ver":0,"kind":"user","exp":…}` |
| `/auth/me` | `{"id":8,"username":"huamo","role":"user",…}` | `{"id":11,"username":"yiyi","role":"agent","email":"8",…}`（**`email` = owner 的 id**） |
| 会话 / 消息 / 成员 / 用户（**读**） | ✅ | ✅（`GET /chats`、`/chats/{id}`、`/chats/{id}/messages`、`/chats/{id}/members`、`/users/{id}` 全放行） |
| `GET /agents`（"我的 agent"） | ✅ 列出名下的 | ⚠️ 200 但**空** `{"items":[]}`（它不拥有谁） |
| **`/agents/{id}` 的管理/写**（心跳、改名、删除、轮换 token） | ✅ | ❌ **403** `{"detail":"you do not own this agent"}` |

**401 与 403 别混**：

| 状态 | detail | 含义 |
| --- | --- | --- |
| **401** | `missing bearer token` / `invalid or expired token` | 没带令牌 / 令牌无效（身份不明） |
| **403** | `you do not own this agent` | 身份明确，但**不是这个 agent 的 owner** |

### ★`POST /agents/{agent_id}/heartbeat` 必须用「拥有者（用户）令牌」

```jsonc
POST /agents/11/heartbeat
Authorization: Bearer <owner 的用户令牌>    // ⚠ 用 agent 自己的令牌 → 403
→ 200 {"ok": true}                          // 刷新该 agent 的 last_seen
```

- 服务端把它实现成"**owner 管自己名下 agent**"那一族 → **心跳是 owner 替 agent 报到**，不是 agent 自报（语义上反直觉，值得反馈服务端）。
- 我们的桥（`backend/cocraft_agents.py`）在**内存**里记住最近一次面板带来的用户令牌来发心跳（**不落盘**，那是全权限凭据）；
  面板服务重启后，等群聊面板里任意一次操作（`/ccagent/*` 或 `/cocraft/*` 带 `Authorization`）就会重新武装。
- **面板侧的批准入口**：群聊**输入框正上方的小弹窗**（`#cc-perm`，`ccPermRender()`）—— 点「允许 / 始终允许 / 拒绝」
  **仍然在群里发同一句** `@<agent> 可以|始终可以|不行`（走 `ccSend`），所以服务端这条规则与群里的审计痕迹都不变。
- **`presence`（online/offline）不是心跳刷的**：实测心跳失败 5 分钟 `presence` 仍 `online` → 它跟着 **WS 连接**走；**`last_seen` 才由心跳刷新**。

## 认证 / 用户

| 方法 | 路径 | 请求体 | 返回 | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/auth/register` | `{username, email, password, code}` | `{token, user}` | 注册；`code` 是邮箱验证码 |
| POST | `/auth/send_code` | `{email, username}` | — | 发送邮箱验证码 |
| POST | `/auth/login` | `{account, password}` | `{token, user}` | `account` 可以是用户名或邮箱 |
| GET | `/auth/me` | — | `user` | 校验登录态 / 取当前用户 |
| **GET** | **`/users/{user_id}`** | — | **`{id, username, avatar, email, created_at}`** | **按 ID 取用户资料（本轮新增，见下）** |

### `GET /users/{user_id}`（本轮新增）

```
GET /users/{user_id}
Authorization: Bearer <token>
```

返回：

```json
{
  "id": 7,
  "username": "hirasaka",
  "avatar": "https://…/a.png",
  "email": "a@b.c",
  "created_at": "2026-09-01T08:30:00"
}
```

前端怎么用（`frontend/app.js`）：

- 登录 / 注册返回的 `user` **就是自己** → 直接 `ccCacheUser(user)` 预填缓存（落
  `localStorage["cocraft.users"]`，`{id: user}`），这样**不必再多发一次请求**。
- 需要显示某个 `user_id` 的用户名时，`ccUserName(id)` 先查缓存；**没有就调 `GET /users/{id}`**
  补一次，回来后就地重画（不刷新整页）；仍拿不到就兜底显示 `用户 {id}`。
- 用在哪：抽屉头部的「我是谁」（`#cc-who`）、消息列表里每条消息的发送者名（别人的消息）。
- 兼容：如果实现把用户包一层（`{user:{…}}`），前端也会取里面的 `user`。

## 会话 / 消息

| 方法 | 路径 | 请求体 | 返回 | 说明 |
| --- | --- | --- | --- | --- |
| GET | `/chats` | — | `chat[]`（或 `{data:[…]}`） | 我参与的会话列表 |
| GET | `/chats/{chat_id}/messages?limit=100&offset=0` | — | `{items:[message], total, limit, offset}` | 历史消息 |
| POST | `/chats/{chat_id}/invite` | `{members:[user_id,…]}` | — | 邀请成员 |
| DELETE | `/chats/{chat_id}/members/{user_id}` | — | — | 移出成员；**移出自己 = 退群** |

`message` 形状（`MessageRead`）：`{id, chat_id, sender_id, content, message_type, created_at}`。
- `message_type` 默认 `"text"`；**`"file"` 表示文件消息**，此时 `content` = 服务端保存的**路径**。

## 文件传输（2026-09-30 上游新增）

| 方法 | 路径 | 请求 | 返回 | 说明 |
| --- | --- | --- | --- | --- |
| POST | `/chats/{chat_id}/files` | `multipart/form-data`，字段名 **`file`** | 一条 `message`（`content` = 保存路径） | **上传即自动发一条 `message_type=file` 的消息**，并通过 WS 广播；客户端不需再发消息 |
| GET | `/chats/{chat_id}/files/{filename}?token=<jwt>` | — | 文件二进制 | 下载；`filename` 取 `content` 的 **basename**；也接受 `Authorization` 头 |

前端实现（`frontend/app.js`）：
- 上传：`FormData.append("file", file, file.name)` → `POST /cocraft/chats/{id}/files`，
  ⚠ **千万不要手动设 `Content-Type`**（要让浏览器带 multipart boundary），只带 `Authorization`。
- 下载：`ccFileUrl(chatID, content)` → `/cocraft/chats/{id}/files/{encodeURIComponent(basename)}?token=<token>`。
- 渲染：`message_type === "file"` → 「📎 文件名」链接；**图片扩展名额外内联 `<img>` 预览**。

### ★让「群聊 AI 成员」也能收图片/文件（2026-10-13 实测）

| 事实 | 说明 |
| --- | --- |
| **下载对 agent 令牌也放行** | `GET /chats/{id}/files/{name}` 用 **agent 自己的令牌** → `200` + 正确 `Content-Type`（实测 `image/jpeg`，191,900 字节，magic `ffd8ffe0`）→ 桥可以自己把图取下来 |
| **上传不能带说明/@** | `POST /chats/{id}/files` 的 `multipart/form-data` **只收 `file` 一个字段**（OpenAPI 里就这一个 prop）→ 「@某人 + 图片」做不到一次成型 |
| 面板的绕法 | 上传成功后，**把输入框里已输入的文字（含 `@`）紧接着发一条消息** —— 于是「@yiyi 看这张图」一步到位 |
| 桥的选取规则 | 被 @ 时把**刚发进群里的**图片/文件当附件一起发给 agent：**只按时间判**（`FILE_FRESH_SEC`=30 分钟，**不看位置**）；**默认只带最新 1 个**（`files_max` 可在 `_cocraft_agents.json` 顶层调 1~5）；单文件 ≤4MB |
| 附件怎么发 | 图片 → ACP 的 `image` 块（`{type:"image",mimeType,data}`，模型真能看到像素）；其它文件 → `resource` 块（blob）；都不支持才退回 `resource_link`。这一套复用面板既有的 `AcpService._prompt_blocks()` |
| ⚠ 模型行为 | 同一会话**第 2 轮**它可能**不重新看图**、照抄上一轮的答案（实测：同一张图答对；换成纯蓝仍答「绿色」）→ 桥做了两件事：**带附件时强制新开 agent 会话**（`_drop_agent_binding`，本地历史会补记过去）+ 提示词里写明「**只看这一轮附上的图**」 |
| ⚠ 多张图 | 一次给两张（新图 + 16 分钟前的照片）时，模型可能答**那张旧的** → 所以默认只带 1 张（要对比就配 `files_max`） |
| 兜底 | 如果带附件那一轮**没出正文**（不吃图的大脑/模型），桥会**去掉附件重试一次**，不让群里静默 |

### ★文件"落地"：让 agent 能拿本机文件干活（2026-10-13）

只把图片当附件发给模型，它只能"看一眼"；**要在本机干活，得有文件**。所以被 @ 时桥还会：

1. 把群里最近的文件**下载到 agent 的工作区**：`<agent cwd>\cocraft-inbox\<chat>\<同名文件>`
   （本机 `codex-node.cwd = D:\opencode-ui` → `D:\opencode-ui\cocraft-inbox\3\`；
   `baseline.cwd` 只在 agent 没配 cwd 时兜底）。
   **落在工作区内** → `workspace-write` 沙箱下 agent 处理它们**不用额外批准**。
2. 写一份清单 `<inbox>\_files.md`：`群消息 id | 时间 | 发送者 | 文件名 | 本地文件`（含没落地的，标「未落地」）。
3. 在提示词里给出**本地路径**（"你可以直接用文件/命令工具读、改、转换、运行它们"），
   并把**小的文本文件（≤8KB）内容直接内联**（省得为"读一下 txt"去要权限）。
4. 同名但内容不同 → 文件名加 `-<群消息id>`；一次最多落地 12 个 / 48MB。

实测：传 `cocraft-notes.txt`（含口令 `4242-ALPHA`）→ 它读出来答 `4242-ALPHA`；让它"复制一份"→
**副本真的生成（169 字节）** —— 全程走「群里权限申请 → owner `@yiyi 可以` → 放行」。

⚠ **落地目录在工作区里，会被工作区的清理/提交看见** → 项目 `.gitignore` 已加 `cocraft-inbox/`。

## 实时（WebSocket）

```
wss://<host>/ws?token=<token>
```

- 发送：`{"type":"message","chat_id":<id>,"content":"<文本>"}`
- `1008` 视为登录失效（前端据此清 token 并回到登录页）。
- 前端断线**指数退避重连**；另有 15s 兜底轮询拉消息。

## 探测 / 元信息

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/info` | `{"service":"chatser"}`（探活） |
| GET | `/openapi.json` | 完整规格（我们只挑用得到的接口实现） |
