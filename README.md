# cocraft

> 曾用名 **opencode-ui**（仓库已改名为 `cocraft`，旧链接 GitHub 会自动重定向）。

一个**本地优先的多智能体 AI 协作台**：既能把 OpenCode 当对话引擎，也能用 ACP 驱动 codex / claude / opencode 等外部 agent；
面板独立运行、外观完全自控，并配套守护进程实现「启动面板 / 关窗联动」。

- 前端是**原生 HTML/CSS/JS**（无 Node、无 npm、无构建步骤）
- 后端是 **Python 3.12 标准库**（零第三方依赖即可跑起来）
- 项目目录可放在任意位置，下文用 `%PROJECT%` 表示（例如 `D:\opencode-ui`）

> **0.2.0 起**：新增**可逆执行**（隔离区 / 撤销这一轮）、**多智能体主动编排**（角色 / 派活 / 对比 / 仲裁 / 建组）、
> **信任面板**（权限等级 / 待批 / 始终允许撤销）、**上下文余量**。详见 [`CHANGELOG.md`](CHANGELOG.md)
> 与 [`docs/RELEASE-0.2.0.md`](docs/RELEASE-0.2.0.md)。
> 从零复现见 [`docs/REPRODUCE.md`](docs/REPRODUCE.md)；对话引擎 / ACP 桥见 [`docs/ACP.md`](docs/ACP.md)；
> 发布流程见 [`docs/RELEASE.md`](docs/RELEASE.md)。

---

## 安装包（推荐给别人的机器）

> **平台：Windows 10 / 11 · 64 位（x64）** —— 安装包自带嵌入式 Python 3.12 x64 与随包 agent，
> **不需要**目标机预装 Python/Node；面板窗口用系统自带的 **Edge**（`--app` 独立窗口）。
> 目前**只发布 Windows x64 一份**，没有 macOS / Linux 版本。

**现成的安装包**：见 [Releases](https://github.com/huaian307/cocraft/releases)（资产名形如
`cocraft-setup-<版本>-windows-x64.exe`）。或自己编一份：

```bat
python packaging\build.py            :: 产出 dist\cocraft-setup-<版本>.exe
```

- **核心必装**：面板 + 后端 + 嵌入式 Python + 面板终端 + 音乐服务（`core / terminal / panel / music` 不可取消）。
- **随包 agent（可选，默认勾选）**：Codex 适配器 + Claude Code 适配器（**不含 Claude 本体**）+ OpenCode（原生 exe）；
  node 跟着两个 JS 适配器自动安装。全不勾也能装，之后可在面板里「扫描本机 Agent」或「直接 npm 安装」。
- **不带任何密钥**：引擎默认跟随本机可用项；装了 OpenCode 的机器上「设置 → 对话引擎」也能切到 `opencode`。
- **可一键更新**：装的是带更新功能的版本后，**设置 →「软件更新」→ 检查更新 / 立即更新**即可升到最新
  GitHub Release（静默安装，装完面板自动重启）。⚠ 更早的版本没有这个入口，需要**手工装一次**新版。
- **模型 API Key 由你自己给**（三条路任选）：
  ① 首次设置向导里的「模型 API Key」直接粘；② 设置 → **模型 API Key**（值留空 = 清除，界面只显示掩码）；
  ③ 设系统环境变量，例如 `DEEPSEEK_API_KEY`。没填时 agent 会明确回
  `Missing environment variable: DEEPSEEK_API_KEY.`
- 换 provider：改 `<安装目录>\agents\codex\codex-home\config.toml` 的
  `model` / `base_url` / `env_key` 三行即可。
- 装到 `%LOCALAPPDATA%\Programs\cocraft`（**per-user，不弹 UAC**），开始菜单有「打开面板 / 自检 / 卸载」；
  「设置 → 应用 → 已安装的应用」里也能看到并卸载。

## 1. 它是什么 / 为什么

官方 OpenCode 桌面版的界面编译在 `app.asar` 里，没有自定义入口；改 asar 会被升级覆盖、还会破坏签名。
所以这里**另做一个独立前端**：浏览器（或 Edge 的 `--app` 独立窗口）连本地面板服务，
面板服务再把 `/api/*` 反向代理到 OpenCode 自己的后端，并顺带补齐官方客户端里那些"由客户端负责"的交互
（**form 选择弹窗、权限申请弹窗**——不做的话 agent 会被静默卡住）。

**设计目标**

1. 外观完全自控（自带两套昼夜主题，前端零依赖、零构建）
2. 与 OpenCode **成对启停**（开 OpenCode 就拉起面板；关面板就关 OpenCode）
3. 把"需要用户决策"的环节都做成面板内的弹窗（form / 权限 / 模型 / 歌单 / 壁纸 / 设置）
4. 面板**不只是 OpenCode 的前端**：对话引擎可切换（`opencode` / `acp`），`acp` 可驱动外部 agent

---

## 2. 主要功能

| 分类 | 功能 |
| --- | --- |
| **会话** | 列表 / 搜索 / 新建 / 删除（含二次确认）/ **用第一句提问自动命名**；侧栏每项可展开「详细信息」；**消息往上翻页**（视口锚定、刷新不丢） |
| **消息** | 流式增量渲染（文本 / 思考 / 工具调用）、**打字机效果**、Markdown（GFM 表格 / 列表 / 标题 / 引用 / 链接 / 代码块）、生成中可**中断** |
| **可逆执行** | **破坏性命令先隔离再放行**；**撤销这一轮**（改/增/删/改名 + 行级 diff + 一键退回，撤销本身也可逆）；工具菜单「隔离区」可恢复 / 删除 |
| **多智能体** | 角色（实现 / 审查 / 测试 / 调研，只注入本人）、**派活**（一条 `@a @b` 并行）、**并排对比**（2 人逐行差异）、**结果仲裁**（推荐 X 可点跳列）、**一键建组**（可自定义预设、建完自动派第一活） |
| **信任** | 输入框「权限 ▾ → 信任面板…」：权限 **5 档**集中切换（放宽要确认、一键收紧）、**待批准**（会话 + 群聊）、**最近「始终允许」**逐条撤销 |
| **上下文** | 输入框左下角「上下文 剩 X%」：OpenCode 用模型上限 + 上一条 tokens；ACP 用 agent 上报；拿不到不编数字 |
| **附件** | 粘贴 / 拖拽 / 选择文件；图片自动压缩后以 `data:` URL 发送；**点击可看大图** |
| **弹窗交互** | **form 选择**（含"自己说"自由输入、忽略）、**权限申请**（说明这次要什么、为什么；ACP 还会带模型的想法节选） |
| **轨迹** | 工具**翻成人话**（中文摘要 + 真输出 + 真 diff）；默认折叠、出错自动展开、可拖宽；主界面每轮一行「进展」 |
| **模型 / 引擎** | 顶栏切模型（provider 分组 + 搜索 + 思考强度）、**对话引擎** `opencode` / `acp`、**agentlist** 扫描 / 新增 / 删除 / 面板内 npm 安装 |
| **音乐** | QQ音乐(SMTC) 联动 + **面板内直接搜歌放歌**（网易云 / QQ，非官方接口）；播放条常驻可折叠、可拖动进度、自动连播 / 随机、刷新后续播；音乐主页；真频谱 |
| **主题 / 视觉** | 昼夜两套主题、动态壁纸（Wallpaper Engine 视频原片，两层交叉淡入）、液态玻璃、毛玻璃与亮度可调、落樱 |
| **系统联动** | 任务栏自动隐藏（面板开着且未最小化时）、关面板连带关 OpenCode、面板最小化自动恢复任务栏、托盘气泡 / 闪任务栏 |
| **可调项** | 名字 / 小标签、亮度、模糊、界面缩放、动态壁纸速度与目录、任务栏、两侧音频条、落樱、歌词翻译、引擎与 agent……设置存 `localStorage` / `runtime/state` |

---

## 3. 架构与数据流

```text
面板窗口（WebView2 原生窗口；没有组件时退回 Edge --app；独立 profile）
        │  http://127.0.0.1:8787
        ▼
backend/server.py  ──  静态前端 + 反向代理 + 本地服务
  ├─ frontend/           静态文件
  ├─ /api/*  ─────────►  当前「对话引擎」
  │                        ├─ engine=opencode：Basic 鉴权 + 流式反代到 OpenCode 后端（含 SSE）
  │                        └─ engine=acp：ACP 客户端（JSON-RPC over stdio）驱动外部 agent 子进程
  ├─ /engine/*           引擎状态与切换、ACP agentlist
  ├─ /qq/*               QQ音乐状态、控制、音量（Core Audio）、歌词、搜索结果、真频谱
  ├─ /music/*            代理本地音乐服务（网易云 / QQ，跑在隔离 venv）
  ├─ /live/*             Wallpaper Engine 视频原片（支持 Range）
  └─ /heartbeat /alive /bye /healthz

backend/watch.py（守护进程，8788 单实例锁）
  ├─ 保活 8787 面板服务
  ├─ OpenCode 启动 → 等后端就绪 → 打开独立面板窗口 → 最小化 OpenCode 自己的窗口
  ├─ 面板关闭 → 反悔 1 秒 → 关闭 OpenCode（可用 runtime/no-kill 关闭此行为）
  └─ 面板开且未最小化 → 任务栏自动隐藏；其余情况恢复
```

### 对话引擎抽象

`backend/engines/` 把"跟哪个 agent 说话"抽象成可替换实现，`server.py` 的 `/api/*`、`/healthz`
统一先问「当前引擎」：

| 引擎 | 说明 |
| --- | --- |
| `opencode`（默认） | 原样反代到本机 OpenCode 后端，行为与改造前一致 |
| `acp` | 实现 **Agent Client Protocol**（JSON-RPC 2.0 + JSONL over stdio）的**纯标准库 Python 客户端**，驱动外部 agent 子进程；把 ACP 的会话/消息/事件映射成前端熟悉的形状 |

- 当前引擎：`runtime/state/_engine.json`；环境变量 `OPENCODE_UI_ENGINE` 可临时覆盖（联调隔离）
- **agent 注册表**：`runtime/state/_acp_agents.json`
  - 顶层 `baseline = {cwd, env, mode}` —— **所有 agent 共用**（避免每个 agent 各配一份）：
    默认会话工作目录、公共环境变量、新建会话后自动套用的审批模式
  - `agents[] = {id, label, command, cwd, env, note, builtin?}`
  - 旧的单 agent 配置 `_acp.json` 仍兼容（首次会自动 bootstrap 成注册表）
- 面板里可**新增 / 切换 / 删除** agent：「新增」= 先扫描本机候选（`PATH` 上的 `*-acp`、
  同盘 `agentlist\*`、全局 npm），没有就**打开文件夹选择框**，由后端自动识别
  （`node_modules/.../package.json` 里含 `acp` 的 bin，或 `*acp*.exe`）
- 会话**按 agent 隔离**（ACP 的 `sessionId` 只有创建它的 agent 认识），并落盘 `_acp_sessions.json`

---

## 4. 目录结构

```text
%PROJECT%\
├── backend\
│   ├── server.py            静态服务 + /api 反代 + /qq /music /live + 面板心跳
│   ├── watch.py             守护进程：服务保活、窗口与 OpenCode 联动、任务栏联动
│   ├── taskbar.py           任务栏自动隐藏 / 恢复（SHAppBarMessage）
│   ├── engines\             ★对话引擎抽象
│   │   ├── __init__.py      注册表（单例缓存）
│   │   ├── base.py          Engine 接口
│   │   ├── opencode.py      反代到 OpenCode（原逻辑）
│   │   └── acp\             ACP 桥：process / client / map_events / service / engine / agents(注册表+检测)
│   └── acp_mock_agent.py    开发用假 agent（说 ACP v1，无需任何外部运行时）
├── frontend\                原生前端（无构建）
│   ├── index.html  style.css  app.js
│   └── assets\              主题素材 + models\（本地模型品牌标 SVG）
├── launchers\
│   ├── launch_opencode.py   桌面「OpenCode（带面板）」快捷方式使用
│   └── launch.vbs           备用入口（纯 ASCII）
├── scripts\                 start / stop / 安装卸载自启 / OpenCode 链接自愈
├── tools\                   验证与工具：jsbalance / check_ids / check_features / inspect_ui /
│                            grep / fetch_model_icons / spectrum / smtc-* / music_service /
│                            pick_folder.ps1 / acp_demo / acp_engine_ui_test / md_render_test
├── docs\                    REPRODUCE.md（复现）· ACP.md（引擎与 ACP 桥）
├── resources\               references\ · screenshots\
├── runtime\                 本机运行数据（详见第 6 节）
├── README.md
└── .gitignore
```

---

## 5. 运行 / 停止

### 推荐：桌面快捷方式「OpenCode（带面板）」

```text
pythonw.exe "%PROJECT%\launchers\launch_opencode.py"
```

先启动 OpenCode（已在跑就跳过），再拉起 `backend\watch.py`，由它打开独立面板窗口。
**不改动**你原有的 OpenCode 图标。

### 手动

```bat
%PROJECT%\scripts\start.bat    :: 确保 8787 在跑 + 打开面板窗口
%PROJECT%\scripts\stop.bat     :: 关窗口 + 停服务 + 停守护（OpenCode 毫发无损）
```

浏览器也可直接访问 <http://127.0.0.1:8787>，但推荐脚本启动的独立应用窗口（独立 profile，不干扰日常浏览器）。

开机自启默认**不装**；需要时：

```powershell
powershell -ExecutionPolicy Bypass -File "%PROJECT%\scripts\install-startup.ps1"
```

---

## 6. 关键数据位置

所有运行数据都在 `runtime/` 下：

| 数据 | 路径 |
| --- | --- |
| 当前对话引擎 | `runtime/state/_engine.json` |
| ACP agent 注册表（**含 API key**） | `runtime/state/_acp_agents.json` |
| ACP 会话 + 消息（持久化） | `runtime/state/_acp_sessions.json` |
| 权限等级 | `runtime/state/_perm_level.json` |
| 权限「始终允许」记忆 | `runtime/state/_acp_always.json` |
| 可逆执行：隔离区 | `runtime/quarantine/<scope>/<ts>/` |
| 可逆执行：工作区快照 | `runtime/snapshots/`（内容寻址仓 + 清单） |
| 群聊 AI 成员 | `runtime/state/_cocraft_agents.json`（含 agent token，仅本机） |
| QQ/SMTC 状态 | `runtime/state/_music.json` |
| 真频谱 | `runtime/state/_spectrum.json` |
| 歌词翻译缓存 | `runtime/state/_lyrics_zh.json` |
| 音乐平台 Cookie | `runtime/state/_music_cookie.json` |
| 壁纸选择 / 自定义目录 | `runtime/state/_wallpapers.json` · `_wallpaper_root.json` |
| 守护日志 | `runtime/logs/watch.log` |
| 面板浏览器 profile | `runtime/browser-profile/` |
| 隔离环境 | `runtime/venvs/audio` · `runtime/venvs/music` |
| 临时关闭联动 | `runtime/no-kill` |

> ⚠ `runtime/no-kill` **只在守护进程启动时读取一次**：要让保险生效，必须先创建文件，**再重启守护进程**。

---

## 7. 环境约束

- Windows 10/11
- Python 3.12（`server.py` / `watch.py` / `taskbar.py` **仅标准库**；`python` / `pythonw` 在 PATH 上）
- **不需要 Node / npm**（前端零依赖、零构建）
- OpenCode 官方桌面版**至少启动过一次**，以生成
  `%USERPROFILE%\.local\state\opencode\service.json`（其中 `url` 的端口每次重启都会变，必须实时读取）
- 可选：真频谱（`runtime/venvs/audio`：soundcard + numpy）、面板内搜歌（`runtime/venvs/music`）、
  Wallpaper Engine（动态壁纸）、QQ音乐（联动）、**PowerShell 7**（`pwsh`，让 agent 读写不再乱码）
- 真频谱与音乐服务都用 `runtime/venvs/` 下的**隔离环境**，不污染系统 Python

---

## 8. 验证 / 自检

```bat
cd /d %PROJECT%
python tools\jsbalance.py frontend\app.js     :: JS 括号配平 + 函数重名
python tools\check_ids.py                     :: DOM id 引用是否都存在
python tools\check_features.py                :: ★功能体检：逐条断言（app.js + index.html + style.css + 后端）
python tools\run_suite.py                     :: ★23 套无头回归（需要 8787 静态服务；CI 用 --ci）
python tools\inspect_ui.py --shot resources\screenshots\panel.png   :: ★无头渲染看真实界面（可出图）
python tools\grep.py -F "关键字" frontend      :: ★快搜（默认跳过 runtime 等大目录）
python -m py_compile backend\server.py backend\watch.py backend\taskbar.py
```

HTTP 检查：

```bat
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8787/healthz').read())"
python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8787/alive').read())"
```

`inspect_ui.py` 用的是**安全探针页**，不会向真实页面发送 `/bye`，因此不会误触发"关面板连带关 OpenCode"。

---

## 9. 已知边界

- 面板内的音乐接口（网易云 / QQ）都是**非官方接口**，可能随平台调整失效；会员/付费曲目受账号权限限制。
- **只在页面里播 QQ音乐音频做不到**（DRM + vkey）；让 QQ音乐客户端播指定歌曲也做不到（`qqmusic://` 协议未注册）。
- 壁纸只支持 Wallpaper Engine 的 **video** 类型；`scene` / `application` 已移除（私有格式、链路脆）。
- **可逆执行是"尽力而为"的安全网，不是事务系统**：只保得住能识别出的目标；codex 不报路径、猜不出时会**如实说"这次没能预先隔离"**。
- **破坏性操作永远需要确认**（等级再宽也一样）；`all` 档连工作区外也放行。
- ACP agent 能力不一致：部分 agent 不上报上下文用量 / 权限路径；界面会标注「—」或「不支持」。
- form / 权限弹窗目前只轮询**当前会话**。
- 关面板会关闭 OpenCode（这是设计），做相关实验前请按 `docs/REPRODUCE.md` §8 放 `runtime/no-kill` **并重启守护进程**。
- 本仓库**不含**本机运行数据、开发笔记与同人图素材（见 `.gitignore` / `docs/REPRODUCE.md`）。
- 当前只有 **Windows 10/11 x64** 安装包；未做 macOS / Linux。

---

## 10. 声明

- 本项目是**非官方**的第三方前端，与 OpenCode / OpenAI / 腾讯 / 网易均无关联。
- 音乐、歌词等接口仅用于**本机个人自用**；请遵守各平台的服务条款。
- 界面所用同人图素材**仅供本地自用、不随仓库发布**；缺图时页面仍可运行（只是没有头像与背景）。
