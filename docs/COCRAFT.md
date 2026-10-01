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
- 时间字段为 ISO 字符串（如 `2026-09-29T12:00:00`），前端显示时把 `T` 换成空格、截前 16 位。
- 出错时返回 `{detail}` 或 `{error}`（前端 `ccFetch` 两种都读）。

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
