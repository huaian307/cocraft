; cocraft 安装脚本（Inno Setup 6）
;
; 用法（由 packaging/build.py 调用）：
;   ISCC.exe /DAppVersion=0.1.0 /DPayloadDir=<dist\payload> ^
;            /DOutputExe=<dist\cocraft-setup-0.1.0.exe> /DComponents=music,audio cocraft.iss
;
; 设计要点：
;   * **per-user 安装**（PrivilegesRequired=lowest）→ 不弹 UAC，装到 %LOCALAPPDATA%\Programs
;   * **AppId 保持不变**（曾用名 opencode-ui）→ 老用户仍能**原地升级**，不会并排另装一份
;   * 可选组件用 [Components]：音乐服务 / 音频频谱 —— **默认都不勾**（要用户自己选）
;   * 随包 agent（node + codex-acp）默认勾上（否则装完没有可用 agent）
;   * 装完调 init_state.py 写 runtime/state（agent 注册表 + 默认引擎），**不写任何密钥**
;   * 卸载时先按端口杀掉面板/守护进程，再删自己的目录；**不碰**用户的 OpenCode 与其它程序

#define AppName "cocraft"
#define AppPublisher "cocraft"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; 文件版本（资源管理器"详细信息"里的「文件版本」）必须是 **4 段数字**（a.b.c.d），
; 由 build.py 传进来；不设的话 Inno 默认写成 0.0.0.0。
#ifndef VersionInfoVersion
  #define VersionInfoVersion "0.0.0.0"
#endif
#ifndef PayloadDir
  #define PayloadDir "..\dist\payload"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId={{8E31A0F1-6C2B-4F4A-9E7D-3B2C1A5D9E01}
AppName={#AppName}
AppVersion={#AppVersion}
VersionInfoVersion={#VersionInfoVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription={#AppName} Setup
AppPublisher={#AppPublisher}
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputBaseFilename={#AppName}-setup-{#AppVersion}
OutputDir={#OutputDir}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
; ⚠ 覆盖安装时旧版面板（pythonw 连着 python\*.dll）若还在跑，替换文件会 "DeleteFile failed; code 5"。
;   我们自己在 [Code] 的 CurStepChanged(ssInstall) 里按端口/路径把旧进程停掉，所以关掉 Inno 自带的
;   "检测占用文件"（它认不出我们的进程，反而会弹 Try again/Skip/Cancel）。
CloseApplications=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName} 面板
MinVersion=10.0
DisableWelcomePage=no

[Languages]
Name: "chinese"; MessagesFile: "compiler:Default.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

; —— 各目录的 payload 只有 build.py 备了才存在，这里按需声明 ——
; ⚠ 2026-10-12 起：agent 与 terminal / panel 全部是**必选组件**；
;   ★ 2026-10-14 起 **music 也随包、也是必选**（用户要求：音乐别让装的人自己勾）；audio（真频谱）仍不随包。
#define HaveTerminal DirExists(AddBackslash(PayloadDir) + "components\terminal\site-packages")
#define HavePanelDir DirExists(AddBackslash(PayloadDir) + "components\panel\site-packages")
#define HaveMusic DirExists(AddBackslash(PayloadDir) + "components\music\site-packages")
#define HaveAgent FileExists(AddBackslash(PayloadDir) + "agents\node\node.exe")
#define HaveCodex DirExists(AddBackslash(PayloadDir) + "agents\codex\node_modules")
#define HaveClaude DirExists(AddBackslash(PayloadDir) + "agents\claude\node_modules")
#define HaveOpencode DirExists(AddBackslash(PayloadDir) + "agents\opencode\node_modules\opencode-ai")

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: checkedonce

[Components]
; ⚠ 2026-10-17 起：**agent 改成可选**（用户只装自己用的；装错了随时重跑本安装包补装 ——
;   覆盖安装**保留 `runtime\`**，设置/会话/密钥都不丢）。
;   核心 / 终端 / 原生窗口 / 音乐 仍然**必选**（他们不是"可选功能"，是面板的骨架）。
;   ⚠ **Node 运行时不再单独占勾选项**：Codex / Claude 的适配器是 JS，没 node 跑不起来 →
;     它的文件由 `Components: agent_codex or agent_claude` **带着装**；随包 OpenCode 是原生 exe，不需要 node。
Name: "core";    Description: "核心（面板 + 后端 + 嵌入式 Python）"; Types: full compact custom; Flags: fixed
#if HaveAgent
; ⚠ Node 运行时**不再单独占一个勾选项**：它的文件由 `Components: agent_codex or agent_claude` 带着装
;   （两个适配器都是 JS，没 node 跑不起来）；随包 OpenCode 是原生 exe，不需要 node。
#endif
#if HaveCodex
Name: "agent_codex";  Description: "Codex 适配器 + 自带 codex CLI（约 440 MB）"; Types: full compact custom
#endif
#if HaveClaude
Name: "agent_claude"; Description: "Claude Code 适配器（自带 SDK 的 Claude Code CLI）"; Types: full compact custom
#endif
#if HaveOpencode
Name: "agent_opencode"; Description: "随包 OpenCode（原生 exe，不依赖 node）"; Types: full compact custom
#endif
#if HaveTerminal
Name: "terminal"; Description: "面板内终端（ConPTY / pywinpty）"; Types: full compact custom; Flags: fixed
#endif
#if HavePanelDir
Name: "panel";   Description: "原生软件窗口（WebView2 / pywebview + 托盘）"; Types: full compact custom; Flags: fixed
#endif
#if HaveMusic
Name: "music";   Description: "音乐（网易云 / QQ 搜歌放歌 + 歌词）"; Types: full compact custom; Flags: fixed
#endif

[Files]
; —— 核心（必装）——
Source: "{#PayloadDir}\python\*";        DestDir: "{app}\python";   Flags: ignoreversion recursesubdirs createallsubdirs; Components: core
Source: "{#PayloadDir}\app\*";           DestDir: "{app}";          Flags: ignoreversion recursesubdirs createallsubdirs; Components: core
Source: "{#PayloadDir}\init_state.*";   DestDir: "{app}";          Flags: ignoreversion; Components: core
Source: "{#PayloadDir}\VERSION";         DestDir: "{app}";          Flags: ignoreversion; Components: core
; —— 随包 agent ——
#ifdef HaveAgent
Source: "{#PayloadDir}\agents\node\*";    DestDir: "{app}\agents\node";   Flags: ignoreversion recursesubdirs createallsubdirs; Components: agent_codex or agent_claude
#endif
#ifdef HaveCodex
Source: "{#PayloadDir}\agents\codex\*";   DestDir: "{app}\agents\codex";  Flags: ignoreversion recursesubdirs createallsubdirs; Components: agent_codex
#endif
#ifdef HaveClaude
Source: "{#PayloadDir}\agents\claude\*";  DestDir: "{app}\agents\claude"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: agent_claude
#endif
#ifdef HaveOpencode
Source: "{#PayloadDir}\agents\opencode\*"; DestDir: "{app}\agents\opencode"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: agent_opencode
#endif
; —— 组件（纯 site-packages；由 _component_python() 用自带解释器 + PYTHONPATH 跑）——
; ⚠ audio（真频谱）仍不随包（没装就退回 CSS 合成音频条）；**music 自 2026-10-14 起随包且必选**。
#if HaveTerminal
Source: "{#PayloadDir}\components\terminal\site-packages\*"; DestDir: "{app}\runtime\site-packages\terminal"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: terminal
#endif
#if HavePanelDir
Source: "{#PayloadDir}\components\panel\site-packages\*"; DestDir: "{app}\runtime\site-packages\panel"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: panel
#endif
#if HaveMusic
Source: "{#PayloadDir}\components\music\site-packages\*"; DestDir: "{app}\runtime\site-packages\music"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: music
#endif

[InstallDelete]
; ★ 覆盖安装前**先清掉旧的「代码/页面」目录**，再整份铺新包 —— 避免"残留旧文件"类问题。
;   真事故：0.1.9 的安装目录里是 .py **源码**；0.1.30 的包改成只发 .pyc（档1 去源码）。
;   覆盖安装时 Inno 默认**不删**旧文件 → 旧的 base.py/fsview.py/updater.py 还在 →
;   Python 导入**优先 .py**（源码 > .pyc）→ 旧 .py 盖住新 .pyc → ImportError → 面板起不来。
;   这些目录在新包里都会整份重发（[Files] 的 app\*），所以"先删后铺"永远干净。
Type: filesandordirs; Name: "{app}\backend"
Type: filesandordirs; Name: "{app}\launchers"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\scripts"
Type: filesandordirs; Name: "{app}\frontend"

[Icons]
Name: "{group}\cocraft 面板";     Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launchers\launch_opencode.py"""; WorkingDir: "{app}"; Comment: "打开 cocraft 面板"
Name: "{group}\cocraft 自检";     Filename: "{app}\python\python.exe";  Parameters: """{app}\tools\selftest.py"" --app-dir ""{app}"""; WorkingDir: "{app}"; Comment: "检查安装是否完整"
Name: "{group}\卸载 cocraft";     Filename: "{uninstallexe}"
Name: "{autodesktop}\cocraft";    Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launchers\launch_opencode.py"""; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; 初始化运行时状态（写 agent 注册表 + 默认引擎；不写密钥）
Filename: "{app}\python\python.exe"; Parameters: """{app}\init_state.py"" --app-dir ""{app}"""; WorkingDir: "{app}"; Flags: runhidden waituntilterminated; StatusMsg: "初始化配置…"
; 装完直接打开面板
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\launchers\launch_opencode.py"""; WorkingDir: "{app}"; Description: "立即打开 cocraft 面板"; Flags: postinstall nowait skipifsilent

[UninstallRun]
; 先按端口停掉面板服务与守护进程（谁占端口谁就是我们的），再清理残留辅助进程
Filename: "{sys}\WindowsPowerShell\v1.0\powershell.exe"; Parameters: "-NoProfile -Command ""foreach ($p in 8788,8787,8790,17888,17887,17990) {{ $q = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess; if ($q) {{ Stop-Process -Id $q -Force -ErrorAction SilentlyContinue }} }}"""; Flags: runhidden; RunOnceId: "killports"
Filename: "{app}\python\python.exe"; Parameters: """{app}\backend\cleanup.py"""; Flags: runhidden; RunOnceId: "cleanup"; Check: FileExists(ExpandConstant('{app}\backend\cleanup.py'))
Filename: "{app}\python\python.exe"; Parameters: """{app}\backend\taskbar.py"" restore"; Flags: runhidden; RunOnceId: "taskbar"

[UninstallDelete]
; ★ 2026-10-13 改：**卸载保留用户数据**（用户反馈「更新把我的主题和壁纸设置覆盖了」）。
;   以前这里整删 `{app}\runtime` → "卸载旧版再装新版"式升级会把：
;     · 壁纸/主题/外观/权限等级（`runtime\state`）
;     · 用户自己装的主题（`runtime\themes`）
;     · WebView2 / Edge 的 profile（**localStorage 里的主题与界面设置**）
;   全部清掉。现在只清**纯缓存/日志/字节码**；用户数据留着（真要清空，手动删目录即可）。
Type: filesandordirs; Name: "{app}\runtime\logs"
Type: filesandordirs; Name: "{app}\runtime\cache"
; 音乐服务用 diskcache 落的缓存（实测卸载后会留 cache\cache.db*）
Type: filesandordirs; Name: "{app}\cache"
; Python 运行时产生的字节码目录（启动器也设了 PYTHONDONTWRITEBYTECODE，这里是双保险）
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{app}\backend\__pycache__"
Type: filesandordirs; Name: "{app}\backend\engines\__pycache__"
Type: filesandordirs; Name: "{app}\backend\engines\acp\__pycache__"
Type: filesandordirs; Name: "{app}\python\__pycache__"
Type: filesandordirs; Name: "{app}\tools\__pycache__"

[Code]
// 覆盖安装前：把旧版的面板/守护/音乐服务停掉（否则 python\libcrypto-3.dll 等被占用，
// [Files] 替换会 "DeleteFile failed; code 5" 拒绝访问）。
// 判据与卸载一致：**谁占端口谁就是我们的进程** + 可执行文件在 {app} 下的都结束。
procedure KillOurProcesses();
var
  Code: Integer;
  Cmd: String;
begin
  Cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
         'foreach ($p in 8788,8787,8790,17888,17887,17990) {' +
         '$q = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty OwningProcess;' +
         'if ($q) { Stop-Process -Id $q -Force -ErrorAction SilentlyContinue } };' +
         'Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -like ''' +
         ExpandConstant('{app}') + '\*'' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"';
  Exec('powershell.exe', Cmd, '', SW_HIDE, ewWaitUntilTerminated, Code);
  Sleep(1200);
end;

function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    KillOurProcesses();
end;

// ⚠ 不再需要「勾适配器就自动勾 node」那段了（`ui 0.1.82`）：
//   node 的 [Files] 直接写成 `Components: agent_codex or agent_claude`，跟着适配器走；
//   旧的那个自动勾选守卫要显式选一个已经不存在的组件，会在运行时报错，所以整段移除。
