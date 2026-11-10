"""桌面租约 —— 同一时刻只允许一个 agent 控制鼠标键盘。

只读工具（windows / tree / find / shot / wait / clipboard_get）不需要租约；
动作工具（click / type / keys / clipboard_set）必须先拿到租约。

为什么需要：多 agent 并行跑 GUI 任务时，两个 agent 同时 SetForegroundWindow +
点坐标，A 的点击会落到 B 激活的窗口上（静默错点，比报错危险得多）。

租约文件：runtime/state/_cu_lease.json
  {"owner":"run_x/m1","token":"...","expires":1792980000,"since":...}
"""

import json
import os
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
STATE_DIR = os.path.join(ROOT, "runtime", "state")
LEASE_FILE = os.path.join(STATE_DIR, "_cu_lease.json")
LEASE_TTL = 30.0        # 秒，每次动作续租
LEASE_MAX_HOLD = 300.0  # 单个 owner 连续持有上限，防止忘释放饿死别人


def _path():
    p = os.environ.get("COCRAFT_CU_LEASE_FILE")
    return p or LEASE_FILE


def _read():
    try:
        with open(_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write(data):
    p = _path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


def current(owner=None):
    """当前租约状态；owner 给了就顺便判断"是不是我的"。"""
    data = _read()
    if not data:
        return {"held": False}
    expires = float(data.get("expires") or 0)
    if expires and expires < time.time():
        return {"held": False, "expired": True, "owner": data.get("owner")}
    out = {"held": True, "owner": data.get("owner"), "token": data.get("token"),
           "expires_in": round(expires - time.time(), 1)}
    if owner:
        out["mine"] = data.get("owner") == owner
    return out


def acquire(owner, wait_ms=0, poll_ms=120):
    """拿租约（owner 建议写成 "<run>/<member>"）。返回 (ok, info)。"""
    owner = str(owner or "").strip()
    if not owner:
        return False, {"held": False, "error": "需要 owner（例如 run_xxx/m1）"}
    deadline = time.time() + max(0, wait_ms) / 1000.0
    while True:
        data = _read()
        now = time.time()
        expires = float(data.get("expires") or 0)
        if not data or not expires or expires < now or data.get("owner") == owner:
            since = float(data.get("since") or 0) if data.get("owner") == owner else now
            if data.get("owner") == owner and since and (now - since) > LEASE_MAX_HOLD:
                since = now  # 超长持有：重置计时（但仍允许续租，避免误伤长任务）
            token = data.get("token") if data.get("owner") == owner else uuid.uuid4().hex[:12]
            ok = _write({"owner": owner, "token": token, "since": since,
                         "expires": now + LEASE_TTL,
                         "updated": now})
            if ok:
                return True, {"held": True, "owner": owner, "token": token,
                              "expires_in": LEASE_TTL, "mine": True}
        if time.time() >= deadline:
            return False, {"held": True, "busy": True,
                           "owner": data.get("owner"),
                           "expires_in": round(expires - now, 1),
                           "error": "桌面被 %s 占用（它正在操作鼠标键盘）" % data.get("owner")}
        time.sleep(max(0.05, poll_ms / 1000.0))


def renew(owner):
    data = _read()
    if not data or data.get("owner") != owner:
        return False
    data["expires"] = time.time() + LEASE_TTL
    data["updated"] = time.time()
    return _write(data)


def release(owner):
    data = _read()
    if not data or data.get("owner") != owner:
        return False
    try:
        os.remove(_path())
        return True
    except Exception:
        return False
