"""通用工具目录 —— **按「能力（作用）」分组**，平台只是每条上的标签。

分组口径（按"人做一件事"的顺序排，不是按平台）：

  observe   观察        看清楚有什么、长什么样（列窗口 / 控件树 / 找控件 / 读页面文本 / 列标签）
  act       操作        像人一样动手（点击 / 输入 / 快捷键 / 写剪贴板）
  navigate  导航        去哪个窗口或页面（切前台 / 开标签 / 跳转）
  wait      等待        等条件成立再动手（别 sleep 盲等）
  capture   取证        留下证据（只截目标窗口 / 整页截图 / 读剪贴板）
  execute   执行与取数  在页面里跑 JS（能读 DOM、能 fetch 接口、能带 cookie）
  control   资源与自检  启停浏览器 / 桌面租约 / 状态与宿主自检

每条工具同时带 `surface`（desktop / web / any），面板上作为小标签显示，
所以"网页里输入文本"和"桌面里输入文本"同属 act，但一眼能看出跑在哪。

字段：
  id / cap / surface / cmd / title / desc / perm(read·action) / example / warn
说明放在后端是故意的：面板列表、文档、以后的 MCP `tools/list` 都读这一份，
否则"界面上写的"和"`cu --help` 里写的"迟早对不上。
"""

CAPS = [
    ("observe", "观察", "看清楚有什么、长什么样：列窗口 / 控件树 / 找控件 / 读页面文本 / 列标签"),
    ("act", "操作（模拟人）", "像人一样动手：点击 / 输入文本 / 快捷键 / 写剪贴板"),
    ("navigate", "导航", "去哪个窗口或页面：切到前台 / 开新标签 / 在当前标签跳转"),
    ("wait", "等待", "等条件成立再动手 —— 别用 sleep 盲等，也别截图轮询"),
    ("capture", "取证", "留下证据：只截目标窗口 / 整页截图 / 读剪贴板"),
    ("execute", "执行与取数", "在页面里跑 JS 或直接调接口：读 DOM / fetch（带 cookie）/ 提交表单 / HTTP GET·POST"),
    ("reuse", "复用（宏）", "把跑通的一串动作存成宏，下次回放：不花模型、10 秒级；失败即停并提示过期"),
    ("control", "资源与自检", "启停专用浏览器 / 桌面租约 / 状态与 UIA 宿主自检"),
]

SURFACES = [("desktop", "桌面"), ("web", "网页"), ("any", "通用")]

TOOLS = [
    # ---------------- 观察 ----------------
    dict(id="windows", cap="observe", surface="desktop", cmd="cu windows", title="列窗口", perm="read",
         desc="列出可见窗口：hwnd、进程、标题、矩形、是否前台/最小化/提权。找窗口的第一步。",
         example="cu windows --title QQ --limit 20"),
    dict(id="tree", cap="observe", surface="desktop", cmd="cu tree", title="导出控件树", perm="read",
         desc="把某个窗口的 UIA 控件树导出来（名字 / 角色 / 矩形 / 可用模式），--prune 去掉噪声。",
         example="cu tree --hwnd 1234 --depth 4 --prune"),
    dict(id="find", cap="observe", surface="desktop", cmd="cu find", title="找控件", perm="read",
         desc="按名称 / 角色 / AutomationId / 类名找控件，返回可点的元素和它的矩形。",
         example='cu find --hwnd 1234 --name 发送 --role Button'),
    dict(id="browser.text", cap="observe", surface="web", cmd="cu browser text", title="读页面文本",
         perm="read",
         desc="读某个选择器的文本（--selector body = 整页）。网页里最先该用的是它。",
         example='cu browser text --selector "h1"'),
    dict(id="browser.list", cap="observe", surface="web", cmd="cu browser list", title="列标签页",
         perm="read",
         desc="列出所有 page 标签（id / 标题 / URL），确认自己在哪个页面。",
         example="cu browser list"),
    # ---------------- 操作（模拟人） ----------------
    dict(id="click", cap="act", surface="desktop", cmd="cu click", title="点击控件", perm="action",
         desc="点一个控件：UIA 模式优先，没有模式就用 UIA 报的矩形做坐标点击。一次调用完成 find+click。",
         example="cu click --hwnd 1234 --name 写信"),
    dict(id="type", cap="act", surface="desktop", cmd="cu type", title="输入文本", perm="action",
         desc="往控件里输入文本（ValuePattern 优先，SendInput 兜底，中文直送）。--replace 先全选。",
         example='cu type --hwnd 1234 --class-like EDIT --text "你好" --replace'),
    dict(id="keys", cap="act", surface="desktop", cmd="cu keys", title="快捷键", perm="action",
         desc="发送组合键（CTRL+S / ALT+F4 …），会先把目标窗口带到前台。",
         example="cu keys --hwnd 1234 --keys CTRL+S"),
    dict(id="clipboard-set", cap="act", surface="any", cmd="cu clipboard-set", title="写剪贴板",
         perm="action",
         desc="把文本写进剪贴板（配合 cu keys CTRL+V 粘贴长文本）。",
         example='cu clipboard-set --text "要粘贴的内容"'),
    dict(id="browser.click", cap="act", surface="web", cmd="cu browser click", title="点击网页元素",
         perm="action",
         desc="按 CSS 选择器点击（真实鼠标事件，DOM click 兜底）。按钮文字含危险词会被拒。",
         example='cu browser click --selector "#submit"'),
    dict(id="browser.type", cap="act", surface="web", cmd="cu browser type", title="填写输入框",
         perm="action",
         desc="往网页输入框打字（Input.insertText，中文没问题）；--clear 先全选再输入。",
         example='cu browser type --selector "#q" --text "周报" --clear'),
    # ---------------- 导航 ----------------
    dict(id="focus", cap="navigate", surface="desktop", cmd="cu focus", title="窗口带到前台",
         perm="action",
         desc="把窗口切到前台（会轻敲一下 Alt 绕过 Windows 前台锁）。",
         example="cu focus --hwnd 1234"),
    dict(id="browser.open", cap="navigate", surface="web", cmd="cu browser open", title="打开网页",
         perm="action",
         desc="在专用 profile 的 Edge/Chrome 里开新标签并跳转（不碰你日常浏览器）。",
         example='cu browser open --url https://example.com --headless'),
    dict(id="browser.go", cap="navigate", surface="web", cmd="cu browser go", title="当前标签跳转",
         perm="action",
         desc="在已有标签里跳转到新地址（不新开标签）。",
         example='cu browser go --url "https://example.com/a"'),
    # ---------------- 等待 ----------------
    dict(id="wait", cap="wait", surface="desktop", cmd="cu wait", title="等控件出现", perm="read",
         desc="等到某个控件出现再动手，避免 sleep 盲等；超时会如实返回 found=false。",
         example="cu wait --hwnd 1234 --name 完成 --timeout-ms 8000"),
    dict(id="browser.wait", cap="wait", surface="web", cmd="cu browser wait", title="等元素出现",
         perm="read",
         desc="等某个选择器出现（轮询 DOM），超时返回 found=false。",
         example='cu browser wait --selector "#done" --timeout-ms 8000'),
    # ---------------- 取证 ----------------
    dict(id="shot", cap="capture", surface="desktop", cmd="cu shot", title="截窗口", perm="read",
         desc="只截目标窗口（PrintWindow），不抢前台、不截全屏；结果落在 runtime/cache/cu/。",
         example="cu shot --hwnd 1234 --max-width 1600"),
    dict(id="browser.shot", cap="capture", surface="web", cmd="cu browser shot", title="网页截图",
         perm="read",
         desc="截当前页面（--full 整页），落在 runtime/cache/cu/。",
         example="cu browser shot --full"),
    dict(id="clipboard-get", cap="capture", surface="any", cmd="cu clipboard-get", title="读剪贴板",
         perm="read",
         desc="读当前剪贴板文本（最多回 4000 字）。",
         example="cu clipboard-get"),
    # ---------------- 执行与取数 ----------------
    dict(id="browser.eval", cap="execute", surface="web", cmd="cu browser eval", title="执行 JS",
         perm="action",
         desc="在当前页跑一段 JS：读 DOM、取接口数据（fetch 带 cookie）、提交表单都行。",
         example='cu browser eval --js "document.title"',
         warn="等价于在这页里做任意操作（含调接口），所以按「动作」管：默认 deny"),
    dict(id="http.get", cap="execute", surface="any", cmd="cu http get", title="调接口（读）",
         perm="read",
         desc="直接发 GET/HEAD：回 status / 头 / JSON，可带浏览器里已登录的 cookie；正文上限 200 KB。",
         example='cu http get --url "https://example.com/api/me"'),
    dict(id="http.post", cap="execute", surface="any", cmd="cu http post", title="调接口（写）",
         perm="action",
         desc="POST / PUT / PATCH / DELETE：body 用 --body 文本或 --json 对象；按动作管。",
         example='cu http post --url "https://example.com/api/todo" --json "{\\"t\\":\\"x\\"}"',
         warn="写请求会改远端状态；URL 与状态进审计，正文不进链"),
    # ---------------- 复用（宏）：跑通一次，之后回放 ----------------
    dict(id="macro.save", cap="reuse", surface="any", cmd="cu macro save", title="从轨迹存宏",
         perm="read",
         desc="把审计链里一次跑通的动作序列提取成宏（控件名 + 窗口 + 结果），输入文本自动变成参数；不存 hwnd。",
         example="cu macro save send-mail --from-run run_20261026-215334_e807c6"),
    dict(id="macro.play", cap="reuse", surface="any", cmd="cu macro play", title="回放宏",
         perm="action",
         desc="按宏里的步骤重放（参数用 --set 传），不花模型；任何一步失败立即停并提示宏可能过期。",
         example='cu macro play send-mail --set to=a@qq.com --set subject=周报 --set body=…',
         warn="含「发送/支付/删除」或回车类快捷键的宏，必须每次显式 --allow-danger；宏不能自己提权"),
    dict(id="macro.list", cap="reuse", surface="any", cmd="cu macro list", title="宏列表",
         perm="read",
         desc="列出本机宏：步数 / 参数 / 是否含危险动作 / 回放过几次 / 上次是否失败（失败标可能过期）。",
         example="cu macro list"),
    dict(id="macro.show", cap="reuse", surface="any", cmd="cu macro show", title="看宏内容",
         perm="read",
         desc="看某条宏的完整步骤与参数提示（确认它到底会点什么、输什么）。",
         example="cu macro show send-mail"),
    # ---------------- 资源与自检 ----------------
    dict(id="browser.start", cap="control", surface="web", cmd="cu browser start", title="启动浏览器",
         perm="action",
         desc="拉起专用 profile 的 Edge/Chrome；--headless 无头（登录过的 profile 可复用）。",
         example="cu browser start --headless"),
    dict(id="browser.stop", cap="control", surface="web", cmd="cu browser stop", title="关闭浏览器",
         perm="action",
         desc="关掉这个专用浏览器（先请它自己退，再按 pid 兜底）。",
         example="cu browser stop"),
    dict(id="lease", cap="control", surface="any", cmd="cu lease", title="桌面租约", perm="action",
         desc="查看 / 占用 / 释放鼠标键盘的独占权（多 agent 并行时避免互相抢焦点）。",
         example="cu lease --hold --owner run_x/m1"),
    dict(id="status", cap="control", surface="any", cmd="cu status", title="工具状态", perm="read",
         desc="一次看全：动作权限、桌面租约、UIA 宿主、截图数量、配置文件名。",
         example="cu status"),
    dict(id="ping", cap="control", surface="desktop", cmd="cu ping", title="UIA 宿主自检", perm="read",
         desc="探一下 UIA 常驻宿主（会按需启动，首次约 0.5 秒）。",
         example="cu ping"),
]

PERM_LABEL = {"read": "只读", "action": "动作"}
SURFACE_LABEL = dict(SURFACES)


def public():
    """给面板 / MCP 用的 JSON 结构（**按能力分组**，平台是标签）。"""
    cap_ids = {c[0] for c in CAPS}
    tools = []
    for raw in TOOLS:
        t = dict(raw)
        t.setdefault("warn", "")
        t["permLabel"] = PERM_LABEL.get(t["perm"], t["perm"])
        t["surfaceLabel"] = SURFACE_LABEL.get(t.get("surface"), t.get("surface") or "")
        if t.get("cap") not in cap_ids:
            t["cap"] = "control"
        tools.append(t)

    by_cap = {}
    for c in CAPS:
        by_cap[c[0]] = sum(1 for t in tools if t["cap"] == c[0])

    return {
        "caps": [{"id": c, "label": l, "hint": h, "count": by_cap.get(c, 0)} for c, l, h in CAPS],
        "surfaces": dict(SURFACES),
        "tools": tools,
        "perms": dict(PERM_LABEL),
        "counts": {
            "total": len(tools),
            "read": sum(1 for t in tools if t["perm"] == "read"),
            "action": sum(1 for t in tools if t["perm"] == "action"),
            "byCap": by_cap,
        },
    }
