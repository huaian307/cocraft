# 发布流程（单机版）

> 面向维护者。版本号唯一来源：`frontend/index.html` 里的 `?v=`（打包时写入 `VERSION`）。
> 发布前必须满足 `CHANGELOG.md` 顶部的门槛。

## 0. 版本与分支

1. 定版本（SemVer）：修 bug = PATCH，功能 = MINOR（`0.x` 期间 MINOR 也可含行为变化）。
2. 改 `frontend/index.html` **两处** `?v=`（`style.css` / `app.js`）。
3. 更新 `CHANGELOG.md`（新增 / 修复 / 破坏性变更 / 已知限制 / 升级注意）。
4. 更新 `docs/RELEASE-<版本>.md`（面向用户）。
5. 更新 `README.md` 里与版本相关的表述（若功能/组件有变化）。
6. 跑完整检查（见 §1）→ 提交 → 打 tag（`v<版本>`）。

## 1. 发布前检查（全绿才继续）

```bat
python -m py_compile backend\server.py backend\watch.py backend\cocraft_agents.py backend\snapshots.py backend\agent_install.py backend\themes.py backend\panel_window.py backend\tray_host.py backend\fsview.py backend\pty.py backend\pty_host.py backend\updater.py
python tools\check_features.py
python tools\jsbalance.py frontend\app.js
python tools\check_ids.py
python tools\run_suite.py
```

要求：`check_features` **0 缺失**；`run_suite` **0 failed**。
CI 会跳过三套依赖本机资源的测试（`theme_ui_test` / `notify_desktop_test` / `agent_install_test`，原因见脚本），
**发版前必须在本地跑一次全量（不加 `--ci`）**。

另跑一次编码体检（BOM / U+FFFD / 中文抽样），以及 `python tools\inspect_ui.py`。

## 2. 打包

```bat
python packaging\build.py
```

- 产物：`dist\cocraft-setup-<版本>-windows-x64.exe`
- 确认：`VERSION` = 版本；资源管理器「详细信息」里 **FileVersion / ProductVersion** 正确
- 确认没有把 `runtime\state`、`dist`、截图、参考图带进 payload
- 记录：文件大小、SHA256

## 3. 代码签名（推荐；没有证书则按 §3.1 处理）

1. 准备 OV/EV 代码签名证书，导出 PFX（或使用云签名）。
2. 用 `signtool` 签 **payload 里的 exe 与最终安装包**（顺序：先签内部 exe，再签安装包）。
3. 验证：`signtool verify /pa /v <file>`；资源管理器显示签名者；SmartScreen 不再拦。

### 3.1 无签名时的兜底

- 在 Release 页写清：Windows 可能提示"未知发布者"；给出 **SHA256** 与校验命令；
- 文档写明绕过步骤（"更多信息 → 仍要运行"）；
- 不把未签名包描述成"已验证安全"。

## 4. 虚拟机验证矩阵（必须在干净机做）

| 场景 | 检查 |
| --- | --- |
| Win10 21H2 干净机 | 安装（per-user，不弹 UAC）→ 首启 → 填 Key（或跳过）→ 建会话 → 一轮回复 |
| Win11 干净机 | 同上；检查 WebView2 窗口；没有 WebView2 时是否退回 Edge `--app` |
| 旧版覆盖升级（`v0.1.62` → 新版） | 会话 / 设置 / 主题 / Key 不丢；更新后能正常对话 |
| 可选组件不装 | 终端 / 音乐位置显示"未安装（可选）"，其余可用 |
| 卸载 | 先停进程，`runtime\` 保留策略符合预期；无残留端口占用 |
| 关窗 / 托盘 / 唤起 | 关窗联动、托盘图标、快捷方式唤起（含窗口已被收进托盘） |
| 权限 | 切换等级立刻生效；破坏性操作必须确认；信任面板可撤销「始终允许」 |
| 可逆执行 | 隔离区恢复、撤销这一轮、冲突先问 |
| 群聊 | 登录、发消息、AI 成员回复、权限申请小弹窗 |
| 更新 | 从上一版检查更新 → 下载 → 静默装 → 自动重启 |

> 任何一项失败：**不发版**，修完重打包。

## 5. 发布

1. 生成 `SHA256`：
   ```powershell
   Get-FileHash dist\cocraft-setup-<版本>-windows-x64.exe -Algorithm SHA256
   ```
2. 命名资产：`cocraft-setup-<版本>-windows-x64.exe`
3. 写 Release notes（用 `docs/RELEASE-<版本>.md`），包含：
   - 新增 / 修复 / 已知限制
   - 升级方式与数据保留说明
   - SHA256
   - 是否需要先手工安装一次（跨大版本时）
4. 上传 → 设为 Latest → **删除上一个 Latest（保留历史 tag 按需）**。
5. 同步桌面安装包副本（如果本机使用）。

## 6. 发布后验证

- `GET /repos/huaian307/cocraft/releases/latest` 指向新版本；资产名正确。
- 在一台机器上**从旧版点"检查更新"**走完整链路。
- 安装后 `/healthz` 的 `ui` 版本 = 新版本；`VERSION` 文件正确。
- 跑一次「自检」（开始菜单）与核心 smoke（会话 / 权限 / 隔离区 / 撤销 / 信任面板）。

## 7. 回滚

- 保留上一版安装包与本版 `SHA256`。
- 升级失败：保留 `%TEMP%\cocraft-state-backup\`（`state` / `themes`），
  重装上一版后按需恢复。
- 更新器失败日志：`%TEMP%\cocraft-update\install.log`；应用日志：`<app>\runtime\logs\`。
- 若新版有数据格式变更：**先在 CHANGELOG 标注不可逆**，并提供导出备份入口后再发。

## 8. 发布检查单（勾选）

- [ ] 版本号两处一致，`VERSION` 正确
- [ ] CHANGELOG / Release notes / README 与版本一致
- [ ] `check_features` 0 缺失
- [ ] `run_suite` 0 failed
- [ ] 编码体检通过
- [ ] 干净机安装 + 首启 + 一轮对话
- [ ] 旧版覆盖升级 + 数据保留
- [ ] 可选组件缺失时不崩
- [ ] 卸载干净（保留 runtime 策略正确）
- [ ] 权限 / 隔离区 / 撤销 / 信任面板 smoke
- [ ] SHA256 已记录
- [ ] 签名（或未签名声明）完成
- [ ] 更新链路验证
- [ ] 回滚包已保留
