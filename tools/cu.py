#!/usr/bin/env python
"""cu -- agent 通用桌面工具 CLI（统一 JSON 输出）。

用法（P0 只读）：
  python tools/cu.py windows [--title QQ] [--limit 20]
  python tools/cu.py describe --hwnd 1234
  python tools/cu.py tree --hwnd 1234 --depth 4 --prune
  python tools/cu.py find --hwnd 1234 --name 写信 --role Button
  python tools/cu.py shot --hwnd 1234 --out runtime/cache/cu/mail.png
  python tools/cu.py wait --hwnd 1234 --name 发送 --timeout-ms 8000
  python tools/cu.py ping

约定：stdout 只有一行 JSON；失败时 {"ok":false,"error":"..."} + 退出码 1。
⚠ 任何子命令都不要用截图猜坐标 —— 先 tree / find。
"""

import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))


def _force_utf8():
    """统一 UTF-8 输出：agent 拿管道读，中文不能变 GBK/问号。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def emit(obj, pretty=False):
    if pretty:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    else:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return 0 if obj.get("ok") else 1


def fail(msg):
    return {"ok": False, "error": str(msg)}


def resolve_hwnd(args, winapi):
    """--hwnd 优先；否则 --title 子串匹配最靠前的可见窗口。"""
    if getattr(args, "hwnd", None):
        return int(args.hwnd)
    title = getattr(args, "title", None)
    if not title:
        raise ValueError("需要 --hwnd 或 --title")
    hit = winapi.find_window(title=title)
    if not hit:
        raise ValueError("找不到标题包含 %r 的窗口（先用 cu windows 看看）" % title)
    return int(hit["hwnd"])


def prune_tree(node, keep_unnamed_with_children=True):
    """去掉"没名字、没模式、也没孩子"的噪声节点，省 token。"""
    if not isinstance(node, dict):
        return None
    kids = node.get("children") or []
    if isinstance(kids, dict):
        kids = [kids]
    new_kids = []
    for k in kids:
        pk = prune_tree(k, keep_unnamed_with_children)
        if pk is not None:
            new_kids.append(pk)
    if new_kids:
        node["children"] = new_kids
        return node
    node.pop("children", None)
    name = (node.get("name") or "").strip()
    patterns = node.get("patterns") or []
    if not name and not patterns:
        return None
    return node


def cmd_windows(args, cu):
    wins = cu.winapi.list_windows(
        title=args.title,
        visible_only=not args.include_hidden,
        include_untitled=args.include_untitled,
        include_tool_windows=args.include_tool_windows,
        limit=args.limit,
    )
    return {"ok": True, "count": len(wins),
            "foreground": cu.winapi.foreground_hwnd(), "windows": wins}


def cmd_describe(args, cu):
    hwnd = resolve_hwnd(args, cu.winapi)
    info = cu.winapi.describe(hwnd)
    uia_info = None
    try:
        uia_info = cu.uia.get_host().describe(hwnd)
    except Exception as exc:
        uia_info = {"error": str(exc)}
    return {"ok": True, "window": info, "uia": uia_info}


def cmd_tree(args, cu):
    hwnd = resolve_hwnd(args, cu.winapi)
    resp = cu.uia.get_host().tree(hwnd, depth=args.depth, max_nodes=args.max_nodes)
    root = resp.get("root")
    if args.prune and root:
        root = prune_tree(root) or root
    return {"ok": True, "hwnd": hwnd, "window_title": cu.winapi.window_text(hwnd),
            "nodes": resp.get("nodes"), "truncated": resp.get("truncated"),
            "elapsed_ms": resp.get("elapsed_ms"), "root": root}


def cmd_find(args, cu):
    hwnd = resolve_hwnd(args, cu.winapi)
    items = cu.uia.get_host().find(
        hwnd, name=args.name, role=args.role, automation_id=args.auto_id,
        name_like=args.like, class_like=args.class_like, limit=args.limit)
    return {"ok": True, "hwnd": hwnd, "count": len(items), "items": items}


def cmd_shot(args, cu):
    hwnd = resolve_hwnd(args, cu.winapi)
    out = args.out
    if out and not os.path.isabs(out):
        out = os.path.join(os.getcwd(), out)
    res = cu.capture.capture_window(hwnd, out_path=out, max_width=args.max_width)
    return res


def cmd_wait(args, cu):
    hwnd = resolve_hwnd(args, cu.winapi)
    res = cu.uia.get_host().wait(
        hwnd, timeout_ms=args.timeout_ms, name=args.name, role=args.role,
        name_like=args.like, automation_id=args.auto_id)
    res["ok"] = True
    res["hwnd"] = hwnd
    return res


def cmd_ping(args, cu):
    res = cu.uia.get_host().ping()
    return {"ok": True, "host_pid": res.get("pid"), "version": res.get("version"),
            "powershell": __import__("computer.uia", fromlist=["powershell_exe"]).powershell_exe()}


# ---------------------------------------------------------------- 动作层（P1）

def _owner(args):
    return (getattr(args, "owner", None) or os.environ.get("COCRAFT_CU_OWNER")
            or ("cli/%d" % os.getpid()))


def _window_ctx(args, cu):
    hwnd = resolve_hwnd(args, cu.winapi)
    return hwnd, cu.winapi.describe(hwnd)


def _deny(cu, tool, win, reason, args):
    cu.audit.record(tool, False, window=win, action_kind="action",
                    decision="denied", note=reason)
    return {"ok": False, "denied": True, "error": reason}


def _locate(cu, hwnd, args, limit=1):
    """先找控件：一次 find 同时拿到 id（走 UIA）和 rect（坐标兜底用）。"""
    if getattr(args, "element", None):
        return {"id": args.element, "name": "", "role": "", "rect": None}
    items = cu.uia.get_host().find(
        hwnd, name=getattr(args, "name", None), role=getattr(args, "role", None),
        automation_id=getattr(args, "auto_id", None), name_like=getattr(args, "like", None),
        class_like=getattr(args, "class_like", None), limit=limit)
    return items[0] if items else None


def _center(rect):
    return (int(rect["x"] + rect["w"] / 2.0), int(rect["y"] + rect["h"] / 2.0))


def _wait_foreground(cu, hwnd, timeout=1.2):
    """等目标窗口**真的**到了前台再动手。

    ⚠ 坑（2026-10-27）：`set_foreground()` 返回 True 只代表"系统接受了这次切换"，
      激活/绘制是异步的；紧接着投递的第一次点击可能被"激活窗口"吃掉
      （现象：7 + 8 = 算成 0 + 8 = 8，而 4 次点击都返回 ok）。机器越忙越容易中。
    """
    end = time.time() + max(0.0, timeout)
    while True:
        try:
            if cu.winapi.foreground_hwnd() == int(hwnd):
                return True
        except Exception:  # noqa: BLE001
            pass
        if time.time() >= end:
            return False
        time.sleep(0.02)


def _loc_json(item, **extra):
    """把"这一步对谁做"写成紧凑 JSON 进审计 note —— macro 提取时按它重建定位。

    ⚠ 只放**定位信息**（名字/角色/AutomationId/类名/选择器），绝不放输入正文。
    """
    d = {}
    item = item or {}
    if item.get("name"):
        d["n"] = str(item["name"])[:60]
    if item.get("role"):
        d["r"] = str(item["role"])[:30]
    if item.get("automation_id"):
        d["a"] = str(item["automation_id"])[:60]
    if item.get("class"):
        d["c"] = str(item["class"])[:60]
    for k, v in extra.items():
        if v:
            d[k] = str(v)[:120]
    return json.dumps(d, ensure_ascii=False, separators=(",", ":")) if d else "{}"


def _take_lease(cu, args):
    if getattr(args, "no_lease", False):
        return True, {"held": False, "skipped": True}
    return cu.lease.acquire(_owner(args), wait_ms=getattr(args, "wait_ms", 0))


def cmd_click(args, cu):
    hwnd, win = _window_ctx(args, cu)
    target = args.name or args.like or args.auto_id or args.element or ""
    allowed, reason = cu.gate.check_action("click", window=win, target=target,
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "click", win, reason, args)

    got, linfo = _take_lease(cu, args)
    if not got:
        return {"ok": False, "busy": True, "error": linfo.get("error"), "lease": linfo}
    try:
        item = _locate(cu, hwnd, args)
        if not item:
            return {"ok": False, "error": "没找到匹配的控件（先 cu tree / cu find 看看名字）"}
        method = None
        uia_err = None
        try:
            res = cu.uia.get_host().click(element_id=item["id"])
            method = "uia:%s" % res.get("method")
        except Exception as exc:
            uia_err = str(exc)
            rect = item.get("rect")
            if not rect:
                return {"ok": False, "error": "UIA 点不了（%s），而且没有坐标可用（用 --name 定位）" % uia_err}
            if not cu.winapi.set_foreground(hwnd):
                return {"ok": False,
                        "error": "无法把目标窗口带到前台（Windows 前台锁），已放弃点击以免点到别的窗口",
                        "uia_error": uia_err}
            _wait_foreground(cu, hwnd)
            time.sleep(0.12)
            cx, cy = _center(rect)
            if not cu.input.click_at(cx, cy, button=args.button, clicks=args.clicks):
                return {"ok": False, "error": "坐标点击失败（SendInput 被拒，多半是提权窗口）"}
            method = "coord"

        if args.restore_focus:
            try:
                prev = getattr(args, "_prev_fg", None)
                if prev and cu.winapi.is_window(prev):
                    cu.winapi.set_foreground(prev)
            except Exception:
                pass

        cu.audit.record("click", True, window=win, action_kind="action",
                        decision="allowed", targets=[target or item.get("name") or ""],
                        note="method=%s element=%r role=%s loc=%s"
                             % (method, item.get("name"), item.get("role"),
                                _loc_json(item)))
        return {"ok": True, "method": method, "uia_error": uia_err,
                "element": {"name": item.get("name"), "role": item.get("role"),
                            "automation_id": item.get("automation_id"), "rect": item.get("rect")},
                "lease": {"owner": linfo.get("owner")}}
    finally:
        if not args.keep_lease:
            cu.lease.release(_owner(args))


def cmd_type(args, cu):
    hwnd, win = _window_ctx(args, cu)
    # 注意：只拿**控件名**判危险词；正文里出现"删除"不该拦（那是在写信）。
    allowed, reason = cu.gate.check_action("type", window=win,
                                           target=args.name or args.like or "",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "type", win, reason, args)

    got, linfo = _take_lease(cu, args)
    if not got:
        return {"ok": False, "busy": True, "error": linfo.get("error"), "lease": linfo}
    try:
        item = _locate(cu, hwnd, args)
        # ⚠ 给了定位（名字/类名/auto-id）却没找到 → **不许**往当前焦点盲打
        #   （以前会 fall through 到 SendInput，把字打进别的控件里，静默错输入）。
        if item is None and (args.name or args.like or args.auto_id
                             or args.class_like or args.element):
            return {"ok": False, "error": "没找到匹配的控件（先 cu tree / cu find 看看名字）"}
        method = None
        uia_err = None
        if item and item.get("id"):
            try:
                res = cu.uia.get_host().set_value(args.text, element_id=item["id"])
                method = "uia:%s" % res.get("method")
            except Exception as exc:
                uia_err = str(exc)
        if method is None:
            if not cu.winapi.set_foreground(hwnd):
                return {"ok": False,
                        "error": "无法把目标窗口带到前台（前台锁），已放弃输入以免打到别的窗口",
                        "uia_error": uia_err}
            _wait_foreground(cu, hwnd)
            time.sleep(0.12)
            # 先把焦点放到目标控件上（坐标来自 UIA 报的矩形，不是猜的）
            if item and item.get("rect"):
                cx, cy = _center(item["rect"])
                cu.input.click_at(cx, cy)
                time.sleep(0.05)
            elif item and item.get("id"):
                try:
                    cu.uia.get_host().focus(element_id=item["id"])
                    time.sleep(0.05)
                except Exception:
                    pass
            if args.replace:
                cu.input.send_keys("CTRL+A")
                time.sleep(0.03)
            if not cu.input.type_text(args.text):
                return {"ok": False, "error": "SendInput 输入失败（多半是提权窗口）"}
            method = "sendinput"

        cu.audit.record("type", True, window=win, action_kind="action",
                        decision="allowed", text=args.text,
                        note="method=%s element=%r loc=%s"
                             % (method, (item or {}).get("name"), _loc_json(item)))
        return {"ok": True, "method": method, "chars": len(args.text), "uia_error": uia_err,
                "element": {"name": (item or {}).get("name"), "role": (item or {}).get("role")}}
    finally:
        if not args.keep_lease:
            cu.lease.release(_owner(args))


def cmd_keys(args, cu):
    hwnd, win = _window_ctx(args, cu)
    allowed, reason = cu.gate.check_action("keys", window=win, target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "keys", win, reason, args)
    got, linfo = _take_lease(cu, args)
    if not got:
        return {"ok": False, "busy": True, "error": linfo.get("error"), "lease": linfo}
    try:
        cu.winapi.set_foreground(hwnd)
        _wait_foreground(cu, hwnd)
        time.sleep(0.12)
        ok = cu.input.send_keys(args.keys)
        cu.audit.record("keys", ok, window=win, action_kind="action",
                        decision="allowed" if ok else "failed", note="keys=%s" % args.keys)
        if not ok:
            return {"ok": False, "error": "SendInput 送键失败"}
        return {"ok": True, "keys": args.keys}
    finally:
        if not args.keep_lease:
            cu.lease.release(_owner(args))


def cmd_focus(args, cu):
    hwnd, win = _window_ctx(args, cu)
    allowed, reason = cu.gate.check_action("focus", window=win, target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "focus", win, reason, args)
    ok = cu.winapi.set_foreground(hwnd)
    cu.audit.record("focus", ok, window=win, action_kind="action")
    return {"ok": ok, "hwnd": hwnd, "foreground": cu.winapi.foreground_hwnd(),
            "error": None if ok else "Windows 前台锁拒绝了切换（可先点一下任务栏）"}


def cmd_clip_get(args, cu):
    text = cu.input.clipboard_get()
    cu.audit.record("clipboard_get", True, action_kind="read", text=text)
    return {"ok": True, "chars": len(text), "text": text[:4000],
            "truncated": len(text) > 4000}


def cmd_clip_set(args, cu):
    allowed, reason = cu.gate.check_action("clipboard_set", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "clipboard_set", {}, reason, args)
    got, linfo = _take_lease(cu, args)
    if not got:
        return {"ok": False, "busy": True, "error": linfo.get("error"), "lease": linfo}
    try:
        ok = cu.input.clipboard_set(args.text)
        cu.audit.record("clipboard_set", ok, action_kind="action", text=args.text)
        return {"ok": ok, "chars": len(args.text)}
    finally:
        if not args.keep_lease:
            cu.lease.release(_owner(args))


def cmd_status(args, cu):
    out = {"ok": True, "gate": cu.gate.status(), "lease": cu.lease.current(_owner(args)),
           "cache_dir": cu.capture.CACHE_DIR}
    try:
        ping = cu.uia.get_host().ping()
        out["uia"] = {"ok": True, "host_pid": ping.get("pid")}
    except Exception as exc:
        out["uia"] = {"ok": False, "error": str(exc)}
    if os.path.isdir(cu.capture.CACHE_DIR):
        try:
            pngs = [n for n in os.listdir(cu.capture.CACHE_DIR) if n.lower().endswith(".png")]
        except OSError:
            pngs = []
        out["shots"] = len(pngs)
    return out


def cmd_lease(args, cu):
    owner = _owner(args)
    if args.release:
        return {"ok": True, "released": cu.lease.release(owner), "owner": owner}
    got, info = cu.lease.acquire(owner, wait_ms=args.wait_ms)
    if not got:
        return {"ok": False, "busy": True, "error": info.get("error"), "lease": info}
    if not args.hold:
        cu.lease.release(owner)
        info["released"] = True
    return {"ok": True, "lease": info}


# ---------------------------------------------------------------- 浏览器（CDP）

def _cdp(cu):
    return cu.cdp


def _browser_start(cu, headless=True, url="about:blank", port=None):
    """确保有一个可用浏览器（没起就起一个；返回 (cdp, 是否我们起的)）。"""
    cdp = _cdp(cu)
    if cdp.is_up(port):
        return cdp, False
    cdp.start(port=port, headless=headless, url=url)
    return cdp, True


def _browser_page(cu, args, autostart=True, url=None, headless=True):
    cdp = _cdp(cu)
    started = False
    if not cdp.is_up():
        if not autostart:
            raise RuntimeError("浏览器没启动：先跑 cu browser start")
        cdp.start(headless=headless, url=url or "about:blank")
        started = True
    b = cdp.Browser()
    b.connect()
    page = b.page(getattr(args, "tab", None), create_url=url or "about:blank")
    return b, page, started


def _burl(page):
    try:
        return page.url()
    except Exception:
        return ""


def cmd_browser_status(args, cu):
    cdp = _cdp(cu)
    out = cdp.status()
    out["exe"] = out.get("exe") or ""
    if out.get("running"):
        b = cdp.Browser()
        try:
            out["tabs"] = b.list_targets()
        except Exception as exc:
            out["tabs_error"] = str(exc)
    return out


def cmd_browser_start(args, cu):
    cdp = _cdp(cu)
    allowed, reason = cu.gate.check_action("browser_start", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_start", {}, reason, args)
    res = cdp.start(port=args.port, headless=args.headless, url=args.url or "about:blank")
    cu.audit.record("browser_start", True, action_kind="action",
                    note="port=%s headless=%s" % (res.get("port"), res.get("headless")))
    return res


def cmd_browser_stop(args, cu):
    cdp = _cdp(cu)
    allowed, reason = cu.gate.check_action("browser_stop", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_stop", {}, reason, args)
    res = cdp.stop(port=args.port)
    cu.audit.record("browser_stop", True, action_kind="action")
    return res


def cmd_browser_list(args, cu):
    cdp = _cdp(cu)
    up = cdp.is_up()
    tabs = cdp.Browser().list_targets() if up else []
    return {"ok": True, "running": up, "count": len(tabs), "tabs": tabs}


def cmd_browser_open(args, cu):
    allowed, reason = cu.gate.check_action("browser_open", target=args.url or "",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_open", {}, reason, args)
    cdp, started = _browser_start(cu, headless=args.headless, url="about:blank",
                                  port=args.port)
    b = cdp.Browser()
    b.connect()
    tid = b.new_tab(args.url or "about:blank")
    page = b.attach(tid)
    if args.url and args.url != "about:blank":
        page.goto(args.url, timeout_ms=args.timeout_ms)
    cu.audit.record("browser_open", True, action_kind="action",
                    targets=[args.url or ""], note="new tab %s" % tid[:8])
    return {"ok": True, "tab": tid, "url": _burl(page), "title": page.title(),
            "started_browser": started}


def cmd_browser_go(args, cu):
    allowed, reason = cu.gate.check_action("browser_go", target=args.url or "",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_go", {}, reason, args)
    b, page, started = _browser_page(cu, args, url=None, headless=args.headless)
    res = page.goto(args.url, timeout_ms=args.timeout_ms)
    cu.audit.record("browser_go", True, action_kind="action", targets=[args.url])
    res.update({"ok": True, "tab": page.target_id, "started_browser": started})
    return res


def cmd_browser_text(args, cu):
    b, page, started = _browser_page(cu, args)
    if not args.selector:
        raise ValueError("需要 --selector（想看整页用 --selector body）")
    val = page.text(args.selector, limit=args.limit)
    cu.audit.record("browser_text", True, action_kind="read",
                    targets=[args.selector], note=_burl(page))
    return {"ok": True, "selector": args.selector, "chars": len(val), "text": val,
            "url": _burl(page), "title": page.title(), "started_browser": started}


def cmd_browser_eval(args, cu):
    """⚠ eval 等价于"在这页里跑任意 JS"，按**动作**管（默认 deny）。"""
    allowed, reason = cu.gate.check_action("browser_eval", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_eval", {}, reason, args)
    b, page, started = _browser_page(cu, args)
    val = page.eval(args.js)
    cu.audit.record("browser_eval", True, action_kind="action",
                    text=args.js, note=_burl(page))
    return {"ok": True, "value": val, "url": _burl(page), "started_browser": started}


def cmd_browser_click(args, cu):
    # 第一道：只验"准不准动手"（selector 本身不当文本判危险词，"#send" 会误伤）
    allowed, reason = cu.gate.check_action("browser_click", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_click", {}, reason, args)
    b, page, started = _browser_page(cu, args)
    # 拿到元素的可读文本，先过危险词（"发送/支付/删除"点下去就回不来了）
    info = page.locate(args.selector)
    label = " ".join(str(x) for x in (info.get("text"), info.get("id"),
                                      info.get("name"), info.get("aria")) if x)
    ok2, reason2 = cu.gate.check_action("browser_click", window=None, target=label,
                                        allow_actions=args.allow_actions)
    if not ok2:
        return _deny(cu, "browser_click", {}, reason2, args)
    res = page.click(args.selector)
    cu.audit.record("browser_click", True, action_kind="action",
                    targets=[args.selector, label[:80]],
                    note="%s %s loc=%s" % (res.get("method"), _burl(page),
                                           _loc_json(None, s=args.selector)))
    return {"ok": True, "selector": args.selector, "method": res.get("method"),
            "element": {k: res["element"].get(k) for k in ("tag", "id", "name", "text")},
            "url": _burl(page), "title": page.title(), "started_browser": started}


def cmd_browser_type(args, cu):
    allowed, reason = cu.gate.check_action("browser_type", target=args.selector or "",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_type", {}, reason, args)
    b, page, started = _browser_page(cu, args)
    res = page.type_text(args.selector, args.text, clear=args.clear)
    # ⚠ note 里**不能**放读回来的 value（那就是正文）；正文只走 text= 的哈希
    cu.audit.record("browser_type", True, action_kind="action", text=args.text,
                    targets=[args.selector, _burl(page)],
                    note="method=%s chars=%d loc=%s"
                         % (res.get("method"), len(args.text), _loc_json(None, s=args.selector)))
    return {"ok": True, "selector": args.selector, "method": res.get("method"),
            "value": res.get("value"), "url": _burl(page), "started_browser": started}


def cmd_browser_wait(args, cu):
    b, page, started = _browser_page(cu, args)
    res = page.wait(args.selector, timeout_ms=args.timeout_ms)
    res.update({"ok": True, "selector": args.selector, "url": _burl(page),
                "started_browser": started})
    return res


def cmd_browser_shot(args, cu):
    b, page, started = _browser_page(cu, args)
    out = args.out
    res = page.shot(path=out, full=args.full)
    res["ok"] = True
    res["started_browser"] = started
    return res


def cmd_browser_close(args, cu):
    allowed, reason = cu.gate.check_action("browser_close", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "browser_close", {}, reason, args)
    cdp = _cdp(cu)
    if not cdp.is_up():
        return {"ok": True, "closed": False, "reason": "浏览器没在跑"}
    b = cdp.Browser()
    b.connect()
    t = b.pick_target(getattr(args, "tab", None))
    if not t:
        return {"ok": False, "error": "找不到这个标签（先 cu browser list）"}
    b.close_tab(t["id"])
    cu.audit.record("browser_close", True, action_kind="action", targets=[t.get("url") or ""])
    return {"ok": True, "closed": True, "tab": t["id"], "title": t.get("title")}


def cmd_http_get(args, cu):
    res = cu.http.request(method="GET", url=args.url, headers=_headers(args),
                          timeout=args.timeout, cookies=args.cookies,
                          max_bytes=args.max_bytes, port=args.port)
    cu.audit.record("http_get", bool(res.get("ok")), action_kind="read",
                    targets=[args.url],
                    note="status=%s bytes=%s ms=%s cookies=%s"
                         % (res.get("status"), res.get("bytes"), res.get("elapsed_ms"),
                            ",".join(res.get("cookies_used") or []) or "-"))
    res["tool"] = "http.get"
    return res


def cmd_http_post(args, cu):
    allowed, reason = cu.gate.check_action("http_post", target="",
                                           allow_actions=args.allow_actions)
    if not allowed:
        return _deny(cu, "http_post", {}, reason, args)
    # body 可以是纯文本，也可以是 JSON 对象（--json '{"k":1}'）
    payload = None
    if getattr(args, "json", None):
        try:
            payload = json.loads(args.json)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("--json 不是合法 JSON：%s" % exc)
    body = None if payload is not None else (args.body if args.body is not None else "")
    res = cu.http.request(method=args.method or "POST", url=args.url, headers=_headers(args),
                          body=body, json_obj=payload, timeout=args.timeout,
                          cookies=args.cookies, max_bytes=args.max_bytes, port=args.port)
    # ⚠ 正文不进审计：只记 method / url / status / 字节数
    cu.audit.record("http_post", bool(res.get("ok")), action_kind="action",
                    targets=[args.url],
                    note="method=%s status=%s bytes=%s cookies=%s"
                         % (res.get("method"), res.get("status"), res.get("bytes"),
                            ",".join(res.get("cookies_used") or []) or "-"))
    res["tool"] = "http.post"
    return res


def _headers(args):
    out = {}
    for raw in (getattr(args, "header", None) or []):
        s = str(raw)
        if ":" not in s:
            raise ValueError('--header 要写成 "K: V"：%r' % s)
        k, v = s.split(":", 1)
        out[k.strip()] = v.strip()
    return out


# ---------------------------------------------------------------- 宏（复用）

def _macro_params(args):
    out = {}
    raw = getattr(args, "params", None)
    if raw:
        try:
            data = json.loads(raw)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("--params 不是合法 JSON：%s" % exc)
        if not isinstance(data, dict):
            raise ValueError("--params 要是一个 JSON 对象")
        out.update({str(k): v for k, v in data.items()})
    for kv in (getattr(args, "set_", None) or []):
        if "=" not in str(kv):
            raise ValueError('--set 要写成 名=值：%r' % kv)
        k, v = str(kv).split("=", 1)
        out[k.strip()] = v
    return out


def _locator_args(step):
    out = []
    if step.get("name"):
        out += ["--name", str(step["name"])]
    if step.get("auto_id"):
        out += ["--auto-id", str(step["auto_id"])]
    if step.get("role"):
        out += ["--role", str(step["role"])]
    if step.get("class_like"):
        out += ["--class-like", str(step["class_like"])]
    return out


def _step_argv(step, hwnd, owner, wait_ms):
    """把一步宏翻成等价的一条 cu 命令（**复用**同一套闸门/租约/审计，不另写一遍）。"""
    op = step.get("op") or ""
    if op in ("click", "type", "keys", "wait"):
        if not hwnd:
            raise ValueError("这一步需要窗口，但没找到")
        base = ["--hwnd", str(int(hwnd))] + _locator_args(step)
        if op == "click":
            return ["click"] + base + ["--allow-actions", "--keep-lease",
                                       "--owner", owner, "--wait-ms", str(int(wait_ms))]
        if op == "type":
            argv = ["type"] + base + ["--text", str(step.get("text") or "")]
            if step.get("replace", True):
                argv.append("--replace")
            return argv + ["--allow-actions", "--keep-lease",
                           "--owner", owner, "--wait-ms", str(int(wait_ms))]
        if op == "keys":
            return ["keys", "--hwnd", str(int(hwnd)), "--keys", str(step.get("keys") or "")] + \
                   ["--allow-actions", "--keep-lease", "--owner", owner,
                    "--wait-ms", str(int(wait_ms))]
        return ["wait", "--hwnd", str(int(hwnd))] + base + \
               ["--timeout-ms", str(int(step.get("timeout_ms") or 8000))]
    if op == "browser.click":
        return ["browser", "click", "--selector", str(step.get("selector") or ""),
                "--allow-actions", "--owner", owner]
    if op == "browser.type":
        argv = ["browser", "type", "--selector", str(step.get("selector") or ""),
                "--text", str(step.get("text") or "")]
        if step.get("replace", True):
            argv.append("--clear")
        return argv + ["--allow-actions", "--owner", owner]
    if op == "browser.go":
        return ["browser", "go", "--url", str(step.get("url") or ""),
                "--allow-actions", "--owner", owner]
    if op == "browser.open":
        return ["browser", "open", "--url", str(step.get("url") or ""),
                "--allow-actions", "--owner", owner]
    raise ValueError("不认识的步骤：%r" % op)


def _run_step(cu, step, hwnd, owner, wait_ms):
    """把一步翻成等价的 cu 命令直接跑（同一套闸门/租约/审计，不另写一遍）。"""
    argv = _step_argv(step, hwnd, owner, wait_ms)
    ns = build_parser().parse_args(argv)
    return ns.func(ns, cu) or {}


def _probe_step(cu, step, hwnd):
    """干跑：只找控件 / 看选择器在不在，**不点不输入**。"""
    op = step.get("op") or ""
    if op in ("click", "type", "wait"):
        items = cu.uia.get_host().find(
            hwnd, name=step.get("name"), role=step.get("role"),
            automation_id=step.get("auto_id"), class_like=step.get("class_like"), limit=1)
        return {"ok": bool(items), "found": bool(items),
                "element": (items[0].get("name") if items else ""),
                "error": "" if items else "控件没找到（宏可能过期）"}
    if op == "keys":
        return {"ok": True, "found": True, "note": "快捷键没法预检（不会真的按）"}
    if op.startswith("browser."):
        if not cu.cdp.is_up():
            return {"ok": False, "found": False, "error": "浏览器没启动（先 cu browser start）"}
        sel = step.get("selector")
        if not sel:
            return {"ok": True, "found": True}
        b = cu.cdp.Browser()
        b.connect()
        try:
            b.page(None).locate(sel)
            return {"ok": True, "found": True, "selector": sel}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "found": False, "error": str(exc)[:160]}
    return {"ok": True, "found": True}


def cmd_macro_list(args, cu):
    return {"ok": True, "macro": cu.macro.summary(), "items": cu.macro.list_rows()}


def cmd_macro_show(args, cu):
    return {"ok": True, "name": args.name, "macro": cu.macro.get(args.name)}


def cmd_macro_save(args, cu):
    if not args.from_audit and not args.from_run:
        raise ValueError("需要 --from-audit（按时间窗）或 --from-run <run_id>（按某一轮）")
    try:
        cu.macro.get(args.name)
    except cu.macro.MacroError:
        pass
    else:
        if not args.force:
            raise ValueError("宏已存在：%s（要覆盖加 --force）" % args.name)
    since = cu.macro.parse_since(args.since) if args.since else 0.0
    m = cu.macro.extract(since=since, run=args.from_run or args.run or "",
                         member=args.member or "", limit=args.limit,
                         max_steps=args.max_steps, drop_dupes=args.drop_dupes,
                         window=args.window or "")
    cu.macro.put(args.name, m)
    return {"ok": True, "name": args.name, "steps": len(m["steps"]),
            "params": m["params"], "param_hints": m["param_hints"],
            "danger": m["danger"], "target": m["target"],
            "hint": ("含不可撤销动作（%s）：回放要 --allow-danger" % "、".join(m["danger"]))
                    if m["danger"] else "回放：cu macro play %s --set 参数=值" % args.name}


def cmd_macro_rm(args, cu):
    return {"ok": True, "removed": cu.macro.remove(args.name), "name": args.name}


def cmd_macro_export(args, cu):
    m = cu.macro.get(args.name)
    out = {"ok": True, "name": args.name, "macro": m}
    if args.out:
        p = args.out if os.path.isabs(args.out) else os.path.join(os.getcwd(), args.out)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(m, f, ensure_ascii=False, indent=2)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("写文件失败：%s" % exc)
        out["path"] = p
    return out


def cmd_macro_play(args, cu):
    m = cu.macro.get(args.name)
    params = _macro_params(args)
    steps, missing = cu.macro.render(m, params)
    if missing:
        raise ValueError("缺参数：%s（用 --set 名=值 传；cu macro show %s 看提示）"
                         % (", ".join(missing), args.name))
    # 危险动作按**当前步骤**重算一遍：宏文件是本机可编辑的，光信存下来的 danger 会被绕过
    danger = sorted(set(list(m.get("danger") or [])) | set(cu.macro.danger_scan(steps)))
    if danger and not args.allow_danger:
        return {"ok": False, "denied": True, "danger": danger,
                "error": ("这条宏含不可撤销动作（%s）：每次回放都要显式 --allow-danger"
                          "（宏不能自己提权）" % "、".join(danger))}

    target = m.get("target") or {}
    owner = args.owner or ("macro/%s" % args.name)
    dry = bool(args.dry_run)
    t0 = time.time()
    done = []
    failed = None
    err = ""
    win = None
    prev_hwnd = 0

    if not dry:
        try:
            h0 = cu.macro.resolve_window(cu, target.get("match") or {}, None)
            if h0:
                win = cu.winapi.describe(h0)
        except Exception:  # noqa: BLE001
            win = None
        allowed, reason = cu.gate.check_action("macro_play", window=win,
                                               target=args.name, allow_actions=args.allow_actions)
        if not allowed:
            cu.audit.record("macro_play", False, window=win, action_kind="action",
                            decision="denied", targets=[args.name], note=reason)
            return {"ok": False, "denied": True, "error": reason}

    try:
        for i, st in enumerate(steps, 1):
            op = st.get("op") or ""
            need_win = op in ("click", "type", "keys", "wait")
            row = {"n": i, "op": op}
            try:
                hwnd = 0
                if need_win:
                    match = st.get("win") or target.get("match") or {}
                    hwnd = cu.macro.resolve_window(cu, match, prev_hwnd=prev_hwnd)
                    if not hwnd:
                        raise ValueError("找不到窗口：%s（先 cu windows 看看）"
                                         % (match.get("title_like") or match.get("title_words") or "?"))
                    prev_hwnd = hwnd
                if dry:
                    res = _probe_step(cu, st, hwnd)
                else:
                    res = _run_step(cu, st, hwnd, owner, args.wait_ms)
                    # ① 控件还没出来（刚点开的新窗口/慢半拍）→ 等 200ms 重试一次
                    _err = str((res or {}).get("error") or "")
                    if (not res.get("ok")) and op in ("click", "type") and _err and (
                            "没找到匹配的控件" in _err or "控件没找到" in _err):
                        time.sleep(0.2)
                        res = _run_step(cu, st, hwnd, owner, args.wait_ms)
                    # ② 输入框 / 动态文字的控件名常常会变（Edit 的 name 就是它当前内容）→
                    #    带完整定位找不到时，只用「类名 / 角色」再试一次（仍在同一个窗口里）
                    if (not res.get("ok")) and op in ("click", "type") \
                            and st.get("class_like") and st.get("name"):
                        row["retried"] = "class"
                        res = _run_step(cu, {k: v for k, v in st.items()
                                             if k not in ("name", "auto_id")},
                                        hwnd, owner, args.wait_ms)
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            row["ok"] = bool((res or {}).get("ok"))
            row["method"] = str((res or {}).get("method") or "")
            row["error"] = str((res or {}).get("error") or "")[:160]
            done.append(row)
            if not row["ok"]:
                failed = i
                err = row["error"] or "失败"
                break
    finally:
        if not args.keep_lease:
            try:
                cu.lease.release(owner)
            except Exception:  # noqa: BLE001
                pass

    ms = int((time.time() - t0) * 1000)
    ok_all = failed is None
    if not dry:
        cu.audit.record("macro_play", ok_all, window=win, action_kind="action",
                        decision="allowed" if ok_all else "failed",
                        targets=[args.name],
                        text=json.dumps(params, ensure_ascii=False, sort_keys=True),
                        note="macro=%s steps=%d/%d ms=%d%s"
                             % (args.name, len(done), len(steps), ms,
                                (" failed=%d" % failed) if failed else ""))
        try:
            cu.macro.touch_stats(args.name, ok_all, ms, err)
        except Exception:  # noqa: BLE001
            pass

    out = {"ok": ok_all, "macro": args.name, "dry_run": dry, "ms": ms,
           "steps": len(steps), "done": done}
    if failed:
        out.update({"failed_step": failed, "error": err,
                    "hint": cu.macro.expired_hint(m, failed)})
    return out


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--pretty", action="store_true", help="缩进输出（人看）")
    common.add_argument("--owner", help="桌面租约的持有者（默认 cli/<pid>，工作流会是 run/member）")
    common.add_argument("--allow-actions", action="store_true",
                        help="显式允许动作类工具（等价 COCRAFT_CU_ACTIONS=allow）")

    p = argparse.ArgumentParser(prog="cu", description="agent 通用桌面工具",
                                parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_target(sp):
        sp.add_argument("--hwnd", type=int, help="窗口句柄")
        sp.add_argument("--title", help="窗口标题子串（自动挑最靠前的）")

    def add_element(sp):
        sp.add_argument("--element", help="UIA 元素 id（cu tree/find 里拿到的 eN）")
        sp.add_argument("--name", help="控件名称（精确）")
        sp.add_argument("--role", help="控件角色（Button/Edit/Pane/MenuItem…）")
        sp.add_argument("--auto-id", dest="auto_id", help="AutomationId")
        sp.add_argument("--like", help="控件名称包含（模糊）")
        sp.add_argument("--class-like", dest="class_like", help="类名包含（如 BUTTON / EDIT）")

    def add_lease(sp):
        sp.add_argument("--no-lease", action="store_true", help="不取桌面租约（仅调试用）")
        sp.add_argument("--wait-ms", type=int, default=0, help="等租约的毫秒数")
        sp.add_argument("--keep-lease", action="store_true", help="动作后不释放租约（连续操作）")

    def add_sp(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common])

    sp = add_sp("windows", "列出顶层窗口")
    sp.add_argument("--title", help="标题子串过滤")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--include-hidden", action="store_true")
    sp.add_argument("--include-untitled", action="store_true")
    sp.add_argument("--include-tool-windows", action="store_true")
    sp.set_defaults(func=cmd_windows)

    sp = add_sp("describe", "单个窗口的详细信息")
    add_target(sp)
    sp.set_defaults(func=cmd_describe)

    sp = add_sp("tree", "导出窗口的 UIA 树")
    add_target(sp)
    sp.add_argument("--depth", type=int, default=4)
    sp.add_argument("--max-nodes", type=int, default=300)
    sp.add_argument("--prune", action="store_true", help="去掉无名噪声节点（省 token）")
    sp.set_defaults(func=cmd_tree)

    sp = add_sp("find", "按名称/角色/AutomationId 找控件")
    add_target(sp)
    sp.add_argument("--name")
    sp.add_argument("--role")
    sp.add_argument("--auto-id", dest="auto_id")
    sp.add_argument("--like", help="名称包含（模糊）")
    sp.add_argument("--class-like", dest="class_like", help="类名包含（如 BUTTON）")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_find)

    sp = add_sp("shot", "只截目标窗口（不抢焦点）")
    add_target(sp)
    sp.add_argument("--out", help="保存路径（默认 runtime/cache/cu/）")
    sp.add_argument("--max-width", type=int, default=1600)
    sp.set_defaults(func=cmd_shot)

    sp = add_sp("wait", "等控件出现")
    add_target(sp)
    add_element(sp)
    sp.add_argument("--timeout-ms", type=int, default=8000)
    sp.set_defaults(func=cmd_wait)

    sp = add_sp("click", "点击控件（UIA 优先，坐标兜底；一次调用完成 find+click）")
    add_target(sp)
    add_element(sp)
    add_lease(sp)
    sp.add_argument("--button", default="left", choices=["left", "right", "middle"])
    sp.add_argument("--clicks", type=int, default=1)
    sp.add_argument("--restore-focus", action="store_true", help="动作后把前台还给原窗口")
    sp.set_defaults(func=cmd_click)

    sp = add_sp("type", "输入文本（ValuePattern 优先，SendInput 兜底；支持中文）")
    add_target(sp)
    add_element(sp)
    add_lease(sp)
    sp.add_argument("--text", required=True)
    sp.add_argument("--replace", action="store_true", help="先全选再输入（替换原内容）")
    sp.set_defaults(func=cmd_type)

    sp = add_sp("keys", "发送快捷键（CTRL+S / ALT+F4…）")
    add_target(sp)
    add_lease(sp)
    sp.add_argument("--keys", required=True, help='如 "CTRL+S" 或 "CTRL+ALT+Y"')
    sp.set_defaults(func=cmd_keys)

    sp = add_sp("focus", "把窗口带到前台")
    add_target(sp)
    sp.set_defaults(func=cmd_focus)

    sp = add_sp("clipboard-get", "读剪贴板")
    sp.set_defaults(func=cmd_clip_get)

    sp = add_sp("clipboard-set", "写剪贴板")
    add_lease(sp)
    sp.add_argument("--text", required=True)
    sp.set_defaults(func=cmd_clip_set)

    sp = add_sp("lease", "查看/获取/释放桌面租约")
    sp.add_argument("--release", action="store_true")
    sp.add_argument("--hold", action="store_true", help="拿到后不释放（配合 --keep-lease）")
    sp.add_argument("--wait-ms", type=int, default=0)
    sp.set_defaults(func=cmd_lease)

    sp = add_sp("status", "桌面工具总览（权限/租约/UIA 宿主/截图数）")
    sp.set_defaults(func=cmd_status)

    sp = add_sp("ping", "UIA 宿主是否可用")
    sp.set_defaults(func=cmd_ping)

    # ---- browser（CDP）：网页任务不走桌面像素 ----
    sp = add_sp("browser", "浏览器 CDP 工具（网页任务不走桌面像素）")
    bsub = sp.add_subparsers(dest="bcmd", required=True)

    def add_b(name, help_text):
        return bsub.add_parser(name, help=help_text, parents=[common])

    def add_tab(bp):
        bp.add_argument("--tab", help="标签 id（可前缀）或序号，默认第一个")

    bp = add_b("status", "浏览器是否在跑 + 标签列表")
    bp.set_defaults(func=cmd_browser_status)

    bp = add_b("start", "启动专用 profile 的 Edge/Chrome（不碰日常浏览器）")
    bp.add_argument("--headless", action="store_true", help="无头（登录过的 profile 可复用）")
    bp.add_argument("--port", type=int)
    bp.add_argument("--url")
    bp.set_defaults(func=cmd_browser_start)

    bp = add_b("stop", "关掉浏览器（先 Browser.close，再按 pid 兜底）")
    bp.add_argument("--port", type=int)
    bp.set_defaults(func=cmd_browser_stop)

    bp = add_b("list", "列标签页（JSON）")
    bp.set_defaults(func=cmd_browser_list)

    bp = add_b("open", "开一个新标签并跳转")
    bp.add_argument("--url", required=True)
    bp.add_argument("--headless", action="store_true")
    bp.add_argument("--port", type=int)
    bp.add_argument("--timeout-ms", type=int, default=25000)
    bp.set_defaults(func=cmd_browser_open)

    bp = add_b("go", "在当前/指定标签跳转")
    bp.add_argument("--url", required=True)
    bp.add_argument("--headless", action="store_true")
    bp.add_argument("--timeout-ms", type=int, default=25000)
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_go)

    bp = add_b("text", "读元素文本（--selector body = 整页）")
    bp.add_argument("--selector", default="body")
    bp.add_argument("--limit", type=int, default=8000)
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_text)

    bp = add_b("eval", "在页面里执行 JS（等价于任意操作，按动作管）")
    bp.add_argument("--js", required=True)
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_eval)

    bp = add_b("click", "点元素（真实鼠标事件，DOM click 兜底）")
    bp.add_argument("--selector", required=True)
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_click)

    bp = add_b("type", "往输入框打字（insertText，中文没问题）")
    bp.add_argument("--selector", required=True)
    bp.add_argument("--text", required=True)
    bp.add_argument("--clear", action="store_true", help="先全选再输入")
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_type)

    bp = add_b("wait", "等元素出现")
    bp.add_argument("--selector", required=True)
    bp.add_argument("--timeout-ms", type=int, default=8000)
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_wait)

    bp = add_b("shot", "页面截图（--full 整页）")
    bp.add_argument("--out")
    bp.add_argument("--full", action="store_true")
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_shot)

    bp = add_b("close", "关掉标签")
    add_tab(bp)
    bp.set_defaults(func=cmd_browser_close)

    # ---- http（直接调接口；GET 只读 / 写请求按动作管）----
    def add_http_common(hp):
        hp.add_argument("--url", required=True)
        hp.add_argument("--header", action="append", metavar='"K: V"',
                        help="额外请求头，可重复")
        hp.add_argument("--cookies", default="auto", choices=["auto", "browser", "none"],
                        help="auto=有专用浏览器就带它的 cookie（默认）/ browser / none")
        hp.add_argument("--timeout", type=float, default=30.0)
        hp.add_argument("--max-bytes", dest="max_bytes", type=int, default=204800)
        hp.add_argument("--port", type=int, help="CDP 端口（取 cookie 用，默认读状态文件）")

    hp = add_sp("http", "直接发 HTTP 请求（可复用浏览器 cookie）")
    hsub = hp.add_subparsers(dest="hcmd", required=True)

    hg = hsub.add_parser("get", help="GET（只读）", parents=[common])
    add_http_common(hg)
    hg.set_defaults(func=cmd_http_get)

    hp2 = hsub.add_parser("post", help="POST / PUT / PATCH / DELETE（动作）", parents=[common])
    add_http_common(hp2)
    hp2.add_argument("--method", default="POST", choices=["POST", "PUT", "PATCH", "DELETE"])
    hp2.add_argument("--body", help="纯文本请求体")
    hp2.add_argument("--json", help="JSON 请求体（会带 Content-Type: application/json）")
    hp2.set_defaults(func=cmd_http_post)

    # ---- macro（复用）：把跑通的一串动作存成宏，下次回放不花模型 ----
    mp = add_sp("macro", "宏：从审计链提取动作序列，之后回放（不花模型）")
    msub = mp.add_subparsers(dest="mcmd", required=True)

    def add_m(name, help_text):
        return msub.add_parser(name, help=help_text, parents=[common])

    m = add_m("list", "列出本机宏（步数 / 参数 / 危险动作 / 回放统计）")
    m.set_defaults(func=cmd_macro_list)

    m = add_m("show", "看某条宏的完整步骤与参数提示")
    m.add_argument("name")
    m.set_defaults(func=cmd_macro_show)

    m = add_m("save", "从审计链提取宏（--from-run 某一轮 / --from-audit 时间窗）")
    m.add_argument("name")
    m.add_argument("--from-audit", dest="from_audit", action="store_true",
                   help="按时间窗提取（配合 --since）")
    m.add_argument("--from-run", dest="from_run", help="只取这一轮 run 的动作（审计 ref.run）")
    m.add_argument("--since", help="时间窗：30m / 2h / 2026-10-27T09:00 / 时间戳")
    m.add_argument("--run", help="同 --from-run（显式过滤）")
    m.add_argument("--member", help="只看某个成员的动作（审计 ref.member）")
    m.add_argument("--window", help="强制目标窗口标题（默认取第一步所在窗口）")
    m.add_argument("--limit", type=int, default=400, help="最多读多少条审计")
    m.add_argument("--max-steps", dest="max_steps", type=int, default=40,
                   help="最多提取多少步（默认 40；0=不限）")
    m.add_argument("--drop-dupes", dest="drop_dupes", action="store_true",
                   help="丢掉连续重复的同一步（重试造成的）")
    m.add_argument("--force", action="store_true", help="覆盖同名宏")
    m.set_defaults(func=cmd_macro_save)

    m = add_m("play", "回放宏（参数用 --set 名=值 传；失败即停）")
    m.add_argument("name")
    m.add_argument("--set", dest="set_", action="append", metavar="名=值")
    m.add_argument("--params", help="JSON 对象形式的参数")
    m.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="只预检（找窗口 / 控件 / 选择器），不点不输入")
    m.add_argument("--allow-danger", dest="allow_danger", action="store_true",
                   help="允许回放含不可撤销动作（发送/支付/删除/回车）的宏 —— 每次都要显式给")
    m.add_argument("--wait-ms", dest="wait_ms", type=int, default=0, help="抢桌面租约等多久")
    m.add_argument("--keep-lease", dest="keep_lease", action="store_true",
                   help="回放完不释放租约（接着手动操作）")
    m.set_defaults(func=cmd_macro_play)

    m = add_m("rm", "删掉一条宏")
    m.add_argument("name")
    m.set_defaults(func=cmd_macro_rm)

    m = add_m("export", "导出宏 JSON（放进工作流 / 技能当配方）")
    m.add_argument("name")
    m.add_argument("--out", help="保存路径（不给就直接打到 stdout）")
    m.set_defaults(func=cmd_macro_export)
    return p


def main(argv=None):
    _force_utf8()
    args = build_parser().parse_args(argv)
    try:
        import computer as cu
    except Exception as exc:
        return emit(fail("加载 backend/computer 失败：%s" % exc), args.pretty)
    try:
        res = args.func(args, cu)
    except Exception as exc:
        return emit(fail("%s: %s" % (type(exc).__name__, exc)), args.pretty)
    return emit(res, args.pretty)


if __name__ == "__main__":
    sys.exit(main())
