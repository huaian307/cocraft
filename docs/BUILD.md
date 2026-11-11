# 构建 cocraft 安装包（可复现）

> 目标：**换一台 Windows 机器，也能打出同一个包**。
> 唯一依赖清单：`packaging/deps.lock.json`。改版本只改它 + 本文，然后跑 `--check-lock`。

---

## 0. 先分清两层

| 层 | 谁需要 | 依赖从哪来 |
| --- | --- | --- |
| **最终用户** | 下载安装包的人 | **安装包自带**：嵌入式 Python 3.12 + node + codex/claude/opencode + 组件 `site-packages`。不需要装 Python/Node |
| **构建者** | 想自己出安装包的人 | 本机 Python 3.12、Inno Setup 6、Node 22、codex 的 `node_modules`、三个组件 venv、npm 网络 |

> 用户侧唯一的"外部依赖"是 **WebView2/Edge**（Win10/11 自带；缺了退回 Edge `--app`）和模型 API 网络。

---

## 1. 前置

| 项 | 版本 | 说明 |
| --- | --- | --- |
| Windows | 10 / 11 x64 | 只支持 x64 |
| Python | **3.12.x x64** | 构建脚本用；运行时用包里的嵌入式 Python |
| Inno Setup | **6.x** | 只有出 `.exe` 需要；`--no-iss` 不需要 |
| Node | **22.23.3**（自带 **npm 10.9.9**） | 随包 agent 与**面板内安装/更新**都用它；版本锁在 `deps.lock.json`。⚠ 打包会把 `node_modules/npm` 一起带上，否则安装版更新按钮不可用 |
| 磁盘 | ≥ 3 GB | `dist/payload` + `dist/cache` + 安装包 |
| 网络 | 可访问 python.org / npm registry | 首次下载嵌入式 Python 和 npm 适配器 |

安装 Inno Setup：

```bat
winget install JRSoftware.InnoSetup
```

---

## 2. 设置构建素材（两个环境变量）

```bat
:: Node 解压目录（里面要有 node.exe）
set COCRAFT_NODE_SRC=D:\agentlist\deepseekharness\node\node-v22.23.3-win-x64

:: Codex 的 node_modules（里面有 @agentclientprotocol/codex-acp、@openai/codex）
set COCRAFT_CODEX_NM=D:\agentlist\codex\node_modules
```

> 不设也行：脚本会依次找 `D:\agentlist\...`、`<repo>\agents\node\...`、`<repo>\agents\codex\node_modules`。
> 换机器时**建议显式设**，报错会直接告诉你缺什么。

三个组件 venv 用脚本重建（按锁文件装**固定版本**）：

```bat
powershell -ExecutionPolicy Bypass -File scripts\bootstrap-dev.ps1
```

脚本会在 `runtime\venvs\{terminal,panel,music}` 里装：

- terminal：`pywinpty==3.0.5`
- panel：`pywebview==6.2.1` + `pythonnet==3.1.0` + `pystray==0.19.5` 等
- music：`NeteaseCloudMusic==0.1.10` + `py-mini-racer==0.6.0` + `setuptools==80.10.2`

---

## 3. 构建

```bat
:: 1) 先校验锁 + 本机素材（CI 也跑这条）
python packaging\build.py --check-lock
python packaging\build.py --check-lock --strict    :: 缺素材就失败

:: 2) 只准备 payload（不出安装包，最快）
python packaging\build.py --no-iss

:: 3) 完整出安装包（需要 Inno Setup 6）
python packaging\build.py

:: 4) 只带部分组件 / agent
python packaging\build.py --agents none
python packaging\build.py --components terminal,panel
```

产物：

- `dist\payload\`：安装包内容
- `dist\cocraft-setup-<ver>.exe`：安装包
- `dist\cache\`：Python embeddable / npm 缓存（可删，下次重下）

---

## 4. `--check-lock` 会检查什么

| 检查 | 失败时 |
| --- | --- |
| `deps.lock.json` 能否读出 | 直接 `[BAD]` |
| 嵌入式 Python 版本 / URL / SHA256 | 有缓存就校验，不匹配直接停 |
| Node 版本 / `node.exe` SHA256 | 找不到或哈希不对直接停 |
| npm 适配器版本 | 打印锁里的版本；没锁到会警告"会装最新版" |
| codex 依赖版本 | 与 `codex_deps` 不一致 → 停（可 `--allow-drift`） |
| 组件 venv 版本 | 与 `components` 不一致 → 停（可 `--allow-drift`） |
| Inno Setup | 只警告（`--no-iss` 不需要） |

> `--strict` 只影响"构建素材缺失"是否算失败；版本不一致**默认就失败**。
> 临时绕过：`--allow-drift` 或 `set COCRAFT_SKIP_HASH=1`（不推荐，别用在发布包上）。

---

## 5. 更新依赖的流程

以升级 `opencode-ai` 为例：

1. 改 `packaging/deps.lock.json` 里的 `npm.opencode-ai`；
2. 删掉对应缓存：`dist\cache\npm-opencode_ai@*`（或整个 `dist\cache`）；
3. 跑 `python packaging\build.py --check-lock`；
4. 跑 `python packaging\build.py --no-iss`，确认 `dist\payload\agents\opencode` 存在且能启动；
5. 跑 `tools\selftest.py --app-dir dist\payload\app` 或完整安装后自检；
6. 更新本文件 §1 的版本表。

升级 Python / Node：

- Python：改 `python.version` / `url` / `sha256`，删 `dist\cache\python-*.zip`；
- Node：改 `node.version` / `sha256_node_exe`，设新的 `COCRAFT_NODE_SRC`；
- 组件：重建 venv 后 `pip freeze`，把结果写回 `components`。

---

## 6. 可复现性边界（诚实说明）

**已锁**：

- 嵌入式 Python 版本 + SHA256
- Node 版本 + `node.exe` SHA256
- 四个 npm 适配器版本（`codex-acp` / `@openai/codex` / `claude-code-acp` / `opencode-ai`）
- Codex 依赖树版本
- 三个组件 venv 的 `pip freeze` 版本

**还没锁**：

- Inno Setup 的具体小版本（只要求 6.x）；
- npm 的**传递依赖**（锁的是顶层包版本；npm 的 `lockfileVersion` 未入库）；
- `dist/cache` 的缓存内容（可删重下，但下载源是公网）。

> 要做到"字节级可复现"，下一步是把 `package-lock.json` 也纳入 `packaging/`，并在 CI 里做一次完整构建 + 产物 SHA256。
> 当前目标是"**版本可复现 + 构建可重复**"，不是"字节级可复现"。

---

## 7. CI

`.github/workflows/ci.yml` 里有一步：

```yaml
- name: Validate build dependency lock
  run: python packaging/build.py --check-lock
```

它只校验锁文件格式与版本表（GitHub 托管 runner 上没有本机的 node/codex/venv）。
**完整打包**需要自托管 Windows runner（带 §2 的素材），或用夜间任务。

---

## 8. 常见错误

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `找不到 node.exe（需要 node v22.23.3）` | 没设 `COCRAFT_NODE_SRC` | 按 §2 设置 |
| `找不到 codex 的 node_modules` | 没设 `COCRAFT_CODEX_NM` | 按 §2 设置 |
| `SHA256 与 deps.lock.json 不一致` | 缓存/二进制被换过 | 删缓存重下，或确认后更新锁 |
| `组件 xxx 的版本与 deps.lock.json 不一致` | venv 是旧版本 | 跑 `bootstrap-dev.ps1` 重建 |
| `codex 依赖版本不一致` | 本机 codex 升级了 | 更新 `codex_deps` 或 `--allow-drift` 临时跳过 |
| `找不到 ISCC.exe` | 没装 Inno Setup 6 | `winget install JRSoftware.InnoSetup` |
| 安装包能出但用户装完打不开 | payload 缺组件 | 跑 `--no-iss` 后检查 `dist\payload\python\pythonw.exe`、`agents\node\node.exe` |
| 用户装完报 `The setup files are corrupted` | 上一次 ISCC 被**超时打断**，exe 是半截（体积远小于 payload） | 删掉重编，**别给 ISCC 设短超时**（lzma2/max 压 800 MB 要几分钟）；核对体积应在 190~210 MB |

---

## 9. 「不带音乐」的精简包（lite）

需要出"没有音乐 / 没有音频条 / 没有 QQ音乐与网易云"的包时（`ui 0.2.122` 起）：

```bat
:: ① 只备 terminal / panel 两个组件（music 不备 → components\music 根本不会生成）
python packaging\build.py --components terminal,panel --agents codex,claude,opencode --no-iss --no-protect

:: ② 把音乐相关的文件 / DOM / 入口从 payload 里摘掉（幂等，可重复跑）
python packaging\strip_music.py

:: ③ 再编译安装包（⚠ 不要设短超时：lzma2/max 压 ~830 MB 要几分钟）
ISCC.exe /Qp /DAppVersion=<ver> /DVersionInfoVersion=<x.y.z.0> ^
  /DPayloadDir=D:\opencode-ui\dist\payload /DOutputDir=D:\opencode-ui\dist ^
  /Fcocraft-setup-<ver>-noaudio-noqq packaging\cocraft.iss
```

`strip_music.py` 做的事（**只删文件是不够的**，这两条都踩过）：

- 删文件：`tools/spectrum.py`、`tools/smtc-*.ps1`、`tools/probe_smtc.ps1`、
  `tools/music_service.py`、`tools/music_bar_test.py`、`frontend/style/03-music.css`、`components/music/**`；
- 删 DOM：`#player` 播放条、`#vis-bar`、`#spec-left/right`、`#mus-home`、`#lyrics-overlay`、
  `#lyric-ball`、工具菜单「音乐」、设置里的音乐登录 / 音频条 / 歌词翻译 / 音乐条背景；
- **JS 换成空实现桩**：`40-music.js` 整份替换为同名符号的空实现（否则 `02-shell` / `11-engine` /
  `41-visual` / `app.js` 会 `syncLyricsVisibility is not defined`）；
  另外 `41-visual` 的 `initMusicAudio` / `restoreMusicState` 加守卫，`11-engine` 的设置绑定换成 `__liteOn()`。

> ⚠ 坑：`const MUS` **不能**写进桩 —— 它本来定义在 `41-visual.js`，顶层 `const` 是跨 script 共享的，
> 重复声明会让 `41-visual.js` 整份 `SyntaxError`（表现：`initWallpaperPicker is not defined`）。
> 自查手法：把 index.html 里所有 script 按顺序拼成一个文件，用随包 node 跑 `node --check`。
>
> 验收：`dist\payload\app\frontend` 起个静态服务 → 无头 Edge 打开 → 断言 `__appLoaded === true`、
> `window.__errs` 为空、`#player` / `#vis-bar` / `#mus-home` / `#tm-music` 都不存在。
