# 主题工坊（Theme Workshop）设计方案

> 状态：**方案（待评审）** · 2026-10-08
> 目标：在 GitHub 上建一个社区主题仓库，用户把自己设计的**主题**（按钮图标 + 界面样式）投稿上去；
> 面板内置「主题工坊」浏览 / 安装 / 套用 / 卸载。顺带把内置主题中性化、**完全剥离绘梨衣**。
> 相关代码：`frontend/style.css`（变量/主题）、`frontend/app.js`（主题引擎）、`backend/skills_install.py`（网络层可复用）。

---

## 1. 背景与目标

**现状**

- 主题是**硬编码的两套**：`:root`（昼 `hiru`）+ `[data-theme="yoru"]`（夜），全靠 CSS 变量（`--paper / --hi / --gold / --ink …`）。
- 按钮图标是**内联 SVG**（扳手 / 齿轮 / 小房子）或**文字符号**（`☰ ↻ ■ 模式 模型 夜`），主题无法替换。
- 切换逻辑写死在 `applyTheme()`，全项目约 **20 处**按 `data-theme === "yoru"` 分支；`style.css` 约 **22 处** `[data-theme=...]` 选择器。
- 背景层 `.sb[data-bg="hiru"|"yoru"]` 也按主题名选。

**目标**

1. 定义**主题包格式** + **安装/套用机制**，主题能改配色、字体、圆角、任意 CSS、背景图、logo、**按钮图标**、明暗模式。
2. 面板内「**主题工坊**」：浏览（内置 / 已安装 / 在线）、安装、套用、卸载、更新。
3. 来源两条路：**官方仓库 `huaian307/cocraft-themes`**（PR 投稿 + catalog）**＋ 任意 GitHub 仓库/链接**（复用技能安装那套网络层）。
4. 主题身份与「明/暗」解耦（`data-theme` + `data-mode`），为「剥离绘梨衣」铺路。

**非目标**

- 不执行主题里的任何脚本（**只有 CSS + 素材**）。
- 不做账号体系 / 评分 / 评论（可作后续）。
- 不引入前端构建（沿用原生 JS）。

---

## 2. 主题包格式

一个目录 = 一个主题：

```
themes/<id>/
  theme.json          # 清单（必需）
  preview.png         # 缩略图（可选，建议 16:9 或 1:1，≤ 2 MB）
  theme.css           # 变量 + 额外样式（可选）
  assets/             # 背景 / logo / 按钮图标（可选）
```

### `theme.json`

```jsonc
{
  "id": "aurora-dark",                // 必填，^[a-z0-9][a-z0-9-]{1,63}$，不得为 hiru/yoru/cocraft
  "name": "极光夜",                    // 显示名（必填）
  "author": "github-user",            // 作者（必填）
  "version": "1.0.0",                 // 语义化版本（必填）
  "description": "深色科技风，青紫渐变",  // 一句话（可选）
  "mode": "dark",                     // light | dark（必填，供面板判断明暗）
  "preview": "preview.png",           // 相对本主题目录（可选）
  "vars": {                           // CSS 变量覆盖（可选，见 §3.1）
    "--paper": "#0d1117",
    "--paper-2": "#161b22",
    "--ink": "#e6edf3",
    "--hi": "#22d3ee",
    "--gold": "#a371f7"
  },
  "css": "theme.css",                 // 额外样式表（相对路径，可选）
  "icons": {                          // 按钮图标槽（可选，见 §4.3）
    "tools": "assets/wrench.svg",
    "gear": "assets/gear.svg",
    "model": "assets/model.svg"
  },
  "background": {                     // 静态背景（可选，见 §4.4）
    "light": "assets/day.jpg",
    "dark": "assets/night.jpg"
  },
  "homepage": "https://github.com/...", // 可选
  "license": "MIT"                    // 可选
}
```

**校验规则**（安装时）

| 字段 | 规则 |
| --- | --- |
| `id` | 正则 `^[a-z0-9][a-z0-9-]{1,63}$`，且不占用保留名 `hiru` / `yoru` / `cocraft` |
| `name` / `author` / `version` | 非空；显示时做长度截断 |
| `mode` | ∈ `light` / `dark`（缺省按 `dark`） |
| `vars` 的键 | 必须以 `--` 开头，只允许字母数字下划线连字符 |
| `css` / 图标 / 背景路径 | 相对路径，**必须是本主题目录内的文件**（防越界） |
| 素材文件 | 见 §5 体积/文件数上限 |

> 简单主题可以只给 `vars`（无 css / 无素材），即「只换配色」。

### 明/暗两套（可选）

主题可选地同时提供 `light` 与 `dark` 两个块，各自可带 `vars` / `css` / `icons` / `background`；
顶层的 `vars`/`css`/`icons`/`background` 是**两种模式共享**的部分。`mode` 仍是**默认**模式。

```jsonc
{
  "id": "cocraft-tech", "name": "cocraft 科技", "author": "cocraft",
  "version": "1.0.0", "mode": "dark", "css": "theme.css",
  "light": { "vars": { "--paper": "#f6f8fb", "--hi": "#2563eb" } },
  "dark":  { "vars": { "--paper": "#0d1117", "--hi": "#22d3ee" } }
}
```

- **支持的明/暗** = 出现了哪些块（`light` / `dark`）；一个块都没有就 = `[mode]`（单模式）。
- 顶栏「**夜/昼**」按钮：
  - 双模式主题 → **在该主题内切换**（`GET /themes/get?id=…&mode=light|dark`），主题不丢；
  - 单模式主题 → **按钮禁用**并提示「只有一种明暗」，套用/切换都不会把主题清掉。
- 内置主题 `hiru`/`yoru` 也是「`light`+`dark`」，所以内置下按钮照常切昼夜。

---

## 3. 主题能力与安全边界

### 3.1 能改什么

- **CSS 变量**（`vars`）：覆盖 `--paper / --paper-2 / --paper-3 / --ink / --ink-dim / --ink-faint / --hi / --hi-deep / --hi-wash / --gold / --gold-soft / --sakura / --line / --line-soft / --ok / --run / --err / --radius / --serif / --sans / --mono`。
- **任意 CSS**（`theme.css`）：直接注入，可覆盖任何面板样式（含按钮、图标、布局）。
- **素材**：背景图、logo、**按钮图标**（通过 §4.3 图标槽）。
- **明暗模式**（`mode`）：供面板选择对应背景 / 做明暗相关处理。

### 3.2 安全边界（**重要**）

- **绝不执行**主题里的脚本（主题包里只有文本 + 图片；发现 `.js/.html` 一律忽略/拒绝）。
- **CSS 注入是"应用内自我修改"**：主题作者可以改坏界面 —— 这是设计取舍（用户要求"任意 CSS"）。
  ⇒ 必须提供 **「恢复内置主题」** 一键兜底（清掉注入的 `<style>` + 还原 `data-theme/mode`）。
- **网络**：`theme.css` 里的 `url(…)` **只允许解析到本主题的本地素材**（安装时把相对路径重写为
  `/themes/asset?id=<id>&path=…`）；**远程 http(s) URL 一律拦截**（防止主题悄悄外联 / 泄露）。
- **路径**：所有素材读取走 `realpath` 前缀校验，禁止 `..` 逃逸。
- **体积**：单主题 ≤ **20 MB**、文件数 ≤ **300**、单个 CSS ≤ **1 MB**（可调）。

---

## 4. 应用机制（前端主题引擎）

### 4.1 身份与「底盘」（**已实现的更稳做法**）

为避免改动 22 处 `[data-theme=...]` 选择器与约 20 处 JS 分支，**没有引入 `data-mode`**，而是：

| 属性 | 取值 | 含义 |
| --- | --- | --- |
| `html[data-theme]` | `hiru` / `yoru` | **底盘（明/暗）**：决定内置样式与背景槽。**永远只有这两个值** |
| `html[data-theme-user]` | `<用户主题 id>` | 用户主题身份（变量 / 图标 / 背景覆盖） |

用户主题把自己的 `mode` 映射到 `hiru`/`yoru` 底盘（`dark→yoru`、`light→hiru`），于是**所有既有
`[data-theme=...]` 规则继续生效**；用户变量注入在 `html[data-theme-user="<id>"]{…}`（特异性 0,1,1）
下，压过内置 `:root` / `[data-theme="yoru"]`（0,1,0）。切/清用户主题只需增删 `data-theme-user`
与那段注入样式 —— **零散落改动**。

> 模型一句话：**主题 = 底盘 + 叠加**。内置主题 = 纯底盘；用户主题 = 底盘 + 覆盖。

### 4.2 套用一个主题

1. `GET /themes/get?id=<id>` → `{ ok, id, mode, vars, css, icons, background }`（`css` 已做 url 重写）。
2. `document.documentElement.dataset.theme = (mode === "dark" ? "yoru" : "hiru")`；
   `.dataset.themeUser = id`。
3. 注入/替换 `<style id="cocraft-theme">`：

   ```css
   html[data-theme-user="<id>"] { /* vars（特异性 0,1,1，压过内置） */ }
   /* theme.css（已重写 url） */
   /* 背景覆盖：#static-bg .sb[data-bg=…] { background-image: … } */
   ```
4. 图标槽：给对应按钮加 `data-ico-on` + `--ico-val: url(...)`（见 §4.3）；没给图标的槽回退默认。
5. 背景：默认由 `_themeBgCss()` 生成覆盖 `.sb[data-bg=hiru|yoru]` 与 `#live-bg` 的规则（见 §4.4）。
6. 持久化：`localStorage["cocraft.theme"]`（底盘）+ `localStorage["cocraft.themeUser"]`（用户主题 id）。

内置主题：`applyTheme("hiru")` **清掉** `data-theme-user`、清空 `#cocraft-theme`、清掉所有 `data-ico-on`
→ 完全回到内置外观。

### 4.3 图标槽机制（让主题能换按钮图标）

把顶栏等处的**内联 SVG / 文字符号**改为**CSS mask 图标**（已实现）：

```css
/* 主题给了图标 → 给按钮加 data-ico-on + --ico-val；遮住默认内容、改画主题图标 */
[data-ico-on] { position: relative; font-size: 0 !important; }
[data-ico-on] > * { visibility: hidden; }
[data-ico-on]::after {
  content: ""; position: absolute; left: 50%; top: 50%; width: 18px; height: 18px;
  transform: translate(-50%, -50%); background: currentColor;
  -webkit-mask: var(--ico-val) center / contain no-repeat;
  mask: var(--ico-val) center / contain no-repeat;
}
```

**核心图标槽**（第一批）：

| 槽名 | 对应按钮 | 现状 |
| --- | --- | --- |
| `sidebar` | `#btn-sidebar` | `☰` |
| `tools` | `#btn-tools` | 扳手 SVG |
| `agent` | `#btn-agent` | 文字「模式」 |
| `model` | `#btn-model` | 文字「模型」 |
| `theme` | `#btn-theme` | 文字「夜 / 昼」 |
| `refresh` | `#btn-refresh` | `↻` |
| `stop` | `#btn-stop` | `■` |
| `home` | `#p-home` | 房子 SVG |

> 其它（播放/上一首/音量/歌词…）后续再纳入；不纳入的也可以被主题用任意 CSS 换掉。

主题给的图标走 `--ico-<slot>: url("/themes/asset?id=<id>&path=assets/x.svg")`；**没给就用默认**。

### 4.4 背景

- 内置静态背景槽：`.sb[data-bg="hiru"]`（light）/ `.sb[data-bg="yoru"]`（dark）。
- 主题可在 `theme.css` 里覆盖：

  ```css
  .sb[data-bg="light"] { background-image: url("assets/day.jpg"); }
  .sb[data-bg="dark"]  { background-image: url("assets/night.jpg"); }
  ```

  或直接在 `theme.json.background` 里声明，引擎据此生成同样的 `--theme-bg-*`。
- 动态壁纸（Wallpaper Engine）优先级不变：**有动态壁纸时**视频层覆盖静态层。

---

## 5. 安装目录与记录

- 安装位置：`runtime/themes/<id>/`（每个主题一个目录，内容与 §2 一致）。
- 来源记录：`runtime/themes/<id>/.cocraft.json`（复用技能安装的约定）：

  ```json
  { "source": "huaian307/cocraft-themes", "path": "themes/aurora-dark",
    "id": "aurora-dark", "version": "1.0.0", "ts": 1760000000 }
  ```

  用于「更新」（按来源重下）与「卸载」（记录允许的清删范围）。
- **内置主题不在此目录**（在 `frontend/`），列表里标 `builtin:true`，**不可卸载**。
- 卸载安全网：只允许删 `runtime/themes/` 下、且目录名是合法主题 id 的目录。

---

## 6. 后端接口

新增 `backend/themes.py`（**引擎无关**，复用 `skills_install.py` 的 `_gh_tree / _fetch_file / _maybe_file / _gh_json / parse_source`）。
路由挂到 `backend/server.py`（放在 `/themes*` 前缀，注意与其它路径不互相遮挡）。

| 方法 | 路径 | 请求 | 返回 |
| --- | --- | --- | --- |
| GET | `/themes` | `?source=`（可选，给就顺带查远程） | `{ok, active, installed:[…], builtin:[…]}` |
| POST | `/themes/preview` | `{source}` | `{ok, source, items:[{id,name,author,version,description,mode,previewUrl,files,bytes}]}` |
| POST | `/themes/install` | `{source, ids:[…], force?}` | `{ok, installed:[…], errors:[{id,error}]}` |
| POST | `/themes/uninstall` | `{id}` | `{ok}` |
| POST | `/themes/update` | `{id}` | `{ok, version}` |
| GET | `/themes/get` | `?id=` | `{ok, id, mode, vars, css, icons, background, builtin}` |
| GET | `/themes/asset` | `?id=&path=` | 图片/SVG/CSS 字节（`Content-Type` 按扩展名，带路径校验） |

**`GET /themes` 的 `installed[]` 每项**：`{id,name,author,version,description,mode,active,builtin,source,previewUrl}`。

### 来源解析（与技能安装一致）

| 来源写法 | 解释 |
| --- | --- |
| `owner/repo` | 扫仓库里的 `theme.json`（每找到一个 = 一个主题） |
| `https://github.com/owner/repo/tree/<ref>/<path>` | 指定分支/子目录 |
| `https://…/theme.json` 或直链 | 单主题 |
| `https://…/catalog.json` / `…/themes.json` | **HTTP catalog**：`{"themes":[{"id","path","…"}]}`，文件从 `<base>/<path>/…` 取 |

官方仓库既有 `catalog.json`（快），也支持"扫 `theme.json`"兜底。

---

## 7. 官方仓库 `huaian307/cocraft-themes`

```
cocraft-themes/
├── README.md                 # 投稿说明 + 格式 + 本地调试
├── catalog.json              # 主题索引（面板优先读它）
├── schema/theme.schema.json  # JSON Schema（编辑器/CI 校验）
├── themes/
│   ├── aurora-dark/          # 示例主题
│   │   ├── theme.json
│   │   ├── preview.png
│   │   ├── theme.css
│   │   └── assets/…
│   └── …
└── .github/
    ├── pull_request_template.md
    └── workflows/validate.yml  # CI：校验 theme.json + 路径 + 体积
```

`catalog.json`：

```json
{
  "schema": 1,
  "themes": [
    { "id": "aurora-dark", "name": "极光夜", "author": "someone",
      "version": "1.0.0", "mode": "dark", "path": "themes/aurora-dark",
      "description": "深色科技风", "preview": "themes/aurora-dark/preview.png" }
  ]
}
```

**投稿流程（README 里写明）**

1. Fork → 在 `themes/<你的 id>/` 放主题（`theme.json` + 素材）。
2. 把条目加进 `catalog.json`。
3. 提 PR；CI 校验通过后合并；面板下一次刷新即可看到并安装。

---

## 8. 与「剥离绘梨衣」的关系

主题工坊是**机制**，"剥离绘梨衣"是**内容**。顺序建议：

1. **先搭工坊**（本方案）——保留 `hiru` / `yoru` 内置主题，确保功能可用。
2. **再做默认中性化**：
   - 新增内置主题 `cocraft`（中性科技风，白/深两套）作为**新默认**；
   - 现有绘梨衣相关的**文案/名称/校徽/图标**逐步中性化（单独任务）；
   - 绘梨衣的视觉可作为一个**可安装主题**放进官方仓库（保留粉丝可用，但不再是内置默认）。
3. **图标/背景**替换为中性素材（可复用之前生成的方案，或社区投稿）。

> 提示：`data-theme` 解耦后，"换个默认主题"只是换个 id，不再需要动代码分支。

---

## 9. 分阶段实施计划

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P1** | 后端 `themes.py`：列表 / 预览 / 安装 / 卸载 / 更新 / 取用 / 素材服务；复用网络层 | 真从 GitHub 装一个主题到 `runtime/themes/`，能列能读 |
| **P2** | 前端主题引擎：`data-mode` 解耦、注入 vars+css、图标槽、背景；「恢复内置」 | 套用/切回无残留；昼夜逻辑不被用户主题破坏 |
| **P3** | 「主题工坊」弹窗：内置 / 已安装 / 在线 三栏，卡片预览、装/用/删/更 | 无头探针 + 出图核对 |
| **P4** | 官方仓库 `cocraft-themes` + README + 示例主题 + CI 校验 | 面板能从官方 catalog 列出并安装示例主题 |
| **P5** | 内置主题中性化 / 剥离绘梨衣（内容） | 默认界面无绘梨衣；绘梨衣可装回 |
| **P6** | 测试收口：`check_features` 断言、`acp_api_test` 路由、主题探针 | 全绿 |

---

## 10. 验收标准（摘）

- 从**官方仓库**和**任意 GitHub 链接**两种来源都能预览 + 安装主题。
- 主题能改**配色 + 任意 CSS + 背景图 + logo + 按钮图标 + 明暗**。
- 「套用」即时生效、刷新后保持；「恢复内置」一键干净还原。
- 主题**不执行脚本**；远程 `url()` 被拦截；路径越界被拒。
- 内置 `hiru`/`yoru` 行为与改造前**一致**（回归）。

---

## 11. 未决问题（**已拍板 · 按推荐**）

| # | 决策 |
| --- | --- |
| 1 | 远程 `url()` **一律拦截**（主题 CSS 只能用本地素材） |
| 2 | 图标槽**先做顶栏 8 个**：`sidebar / tools / agent / model / theme / refresh / stop / home` |
| 3 | 新中性内置主题命名 **`cocraft`**；内置 `hiru/yoru` 暂不改 id |
| 4 | **用 `gh` 创建** `huaian307/cocraft-themes`（已建，含 catalog + 2 个示例主题） |
| 5 | 同 id **默认拒绝**，UI 给「覆盖」（`force`） |
| 6 | 缩略图**作者提供**，缺省用背景 / 纯色兜底 |

---

## 12. 实施进度

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| P1 | 后端 `backend/themes.py` + 7 个路由（列表/预览/安装/卸载/更新/取用/素材）+ 远程预览图代理 | **完成**（真从官方仓库装成功） |
| P2 | 前端主题引擎（底盘 + `data-theme-user` 叠加）+ 图标槽 + 背景覆盖 | **完成**（无头回归 15/15） |
| P3 | 「主题工坊」弹窗（已安装/内置 + 在线工坊 + 从 GitHub 安装） | **完成** |
| P4 | 官方仓库 `huaian307/cocraft-themes`（catalog + schema + 2 示例主题 + README） | **完成**（Contents API 上传） |
| P5 | 内置主题中性化 / 剥离绘梨衣（内容） | 待做 |
| P6 | 测试：`check_features` 断言 + `tools/theme_ui_test.py` 无头回归 | 进行中 |
