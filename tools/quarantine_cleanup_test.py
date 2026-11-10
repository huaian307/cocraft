# -*- coding: utf-8 -*-
r"""隔离区保留期 / 自动清理（`backend/snapshots.py`，`ui 0.2.95`）离线回归。

覆盖：
  * 策略默认值与越界归一；
  * `plan` 的 keep / soon / due / hold 四个桶（用固定 `now` 卡边界）；
  * **只清安全项**：★ 可再生成 / 原件已恢复 / 只是清单墓碑 → due；
    git 跟踪文件已缺失 / 无法判定 → hold（永不自动清）；
  * 手动隔离记录永不自动清；
  * `dry_run` 只算不动；`cleanup=False` 时到期也不清；
  * 墓碑记录（> 64MB 只留清单）能正常列 / 恢复时明确说"无法恢复"。

不联网、只用临时目录。
"""
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import snapshots as sn  # noqa: E402

N_PASS = 0
N_FAIL = 0


def check(name, cond, extra=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("PASS  %s" % name)
    else:
        N_FAIL += 1
        print("FAIL  %s%s" % (name, ("  | " + str(extra)) if extra else ""))


def touch(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def age_record(qid, taken_at):
    p = os.path.join(sn.QUARANTINE_DIR, qid.replace("/", os.sep), "_manifest.json")
    with open(p, "r", encoding="utf-8") as fh:
        js = json.load(fh)
    js["taken_at"] = float(taken_at)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(js, fh, ensure_ascii=False)


def main():
    base = tempfile.mkdtemp(prefix="cocraft-quar-")
    try:
        root = os.path.join(base, "ws")
        os.makedirs(root)
        sn.QUARANTINE_DIR = os.path.join(base, "quarantine")
        sn.QUAR_CFG_FILE = os.path.join(base, "_quarantine.json")
        os.makedirs(sn.QUARANTINE_DIR, exist_ok=True)

        # ---------- 默认值 ----------
        cfg = sn.quarantine_config()
        check("默认开启", cfg.get("enabled") is True, cfg)
        check("默认只放 ★1", int(cfg.get("maxStar")) == 1, cfg)
        check("默认保留 7 天", int(cfg.get("days")) == 7, cfg)
        check("默认提前 24h 提醒", int(cfg.get("remindHours")) == 24, cfg)
        check("默认到期自动清", cfg.get("cleanup") is True, cfg)

        # ---------- 越界归一 ----------
        c2 = sn.set_quarantine_config(days=999, maxStar=9, remindHours=9999)
        check("days 上限 90", int(c2.get("days")) == 90, c2)
        check("maxStar 上限 2", int(c2.get("maxStar")) == 2, c2)
        check("remindHours 上限 168", int(c2.get("remindHours")) == 168, c2)
        c3 = sn.set_quarantine_config(days=0, maxStar=0, remindHours=-5)
        check("days 下限 1", int(c3.get("days")) == 1, c3)
        check("maxStar 下限 1", int(c3.get("maxStar")) == 1, c3)
        check("remindHours 下限 0", int(c3.get("remindHours")) == 0, c3)
        sn.set_quarantine_config(days=7, maxStar=1, remindHours=24, enabled=True, cleanup=True)

        # ---------- 造记录 ----------
        now = 2_000_000_000.0

        # A: ★1 且原件已不在 → 安全（放行时就认了"可再生成"）
        fa = touch(os.path.join(root, "a.log"))
        ra = sn.quarantine_paths(root, [fa], scope="p", auto=True,
                                 scores=[{"path": fa, "stars": 1, "score": 10, "reasons": ["log"]}])
        os.remove(fa)

        # B: ★4 且原件不在、不是 git 仓库 → 无法确认 → hold
        fb = touch(os.path.join(root, "app.py"))
        rb = sn.quarantine_paths(root, [fb], scope="p", auto=True,
                                 scores=[{"path": fb, "stars": 4, "score": 75, "reasons": ["src"]}])
        os.remove(fb)

        # C: 原件还在（比如只是重命名 / 没删成）→ 备份多余 → 安全
        fc = touch(os.path.join(root, "c.log"))
        rc = sn.quarantine_paths(root, [fc], scope="p", auto=True,
                                 scores=[{"path": fc, "stars": 1, "score": 10, "reasons": ["log"]}])

        # D: 手动隔离 → 永不清
        fd = touch(os.path.join(root, "manual.txt"))
        rd = sn.quarantine_paths(root, [fd], scope="p", auto=False)

        # E: 墓碑（把阈值调成 1KB，让 2KB 文件只留清单）
        old_limit = sn.AUTO_COPY_MAX_BYTES
        sn.AUTO_COPY_MAX_BYTES = 1024
        fe = touch(os.path.join(root, "big.bin"), "x" * 4096)
        re_ = sn.quarantine_paths(root, [fe], scope="p", auto=True,
                                  scores=[{"path": fe, "stars": 1, "score": 8, "reasons": ["tmp"]}])
        sn.AUTO_COPY_MAX_BYTES = old_limit
        os.remove(fe)
        check("墓碑：tombstone=True", bool(re_.get("tombstone")), re_.get("tombstone"))
        check("墓碑：没有复制内容", (re_.get("bytes") or 0) == 0, re_.get("bytes"))

        # G: 墓碑但还在保留期（用来验证 list / restore 的墓碑路径）
        sn.AUTO_COPY_MAX_BYTES = 1024
        fg = touch(os.path.join(root, "big2.bin"), "y" * 4096)
        rg = sn.quarantine_paths(root, [fg], scope="p", auto=True,
                                 scores=[{"path": fg, "stars": 1, "score": 8, "reasons": ["tmp"]}])
        sn.AUTO_COPY_MAX_BYTES = old_limit
        os.remove(fg)

        # F: 未到期（5 天）→ keep
        ff = touch(os.path.join(root, "f.log"))
        rf = sn.quarantine_paths(root, [ff], scope="p", auto=True,
                                 scores=[{"path": ff, "stars": 1, "score": 10, "reasons": ["log"]}])
        os.remove(ff)

        # H: 进入提醒窗口（6.5 天，剩 12h）→ soon
        fh = touch(os.path.join(root, "h.log"))
        rh = sn.quarantine_paths(root, [fh], scope="p", auto=True,
                                 scores=[{"path": fh, "stars": 1, "score": 10, "reasons": ["log"]}])
        os.remove(fh)

        age_record(ra["id"], now - 8 * 86400)      # 到期
        age_record(rb["id"], now - 8 * 86400)      # 到期但 hold
        age_record(rc["id"], now - 8 * 86400)      # 到期（安全）
        age_record(rd["id"], now - 30 * 86400)     # 手动，再老也不清
        age_record(re_["id"], now - 8 * 86400)     # 墓碑，到期
        age_record(rg["id"], now - 1 * 86400)      # 墓碑，未到期
        age_record(rf["id"], now - 5 * 86400)      # 未到期
        age_record(rh["id"], now - int(6.5 * 86400))  # 提醒窗口

        plan = sn.quarantine_plan(now=now)
        ids_due = {e["id"] for e in plan["due"]}
        ids_hold = {e["id"] for e in plan["hold"]}
        ids_keep = {e["id"] for e in plan["keep"]}
        ids_soon = {e["id"] for e in plan["soon"]}
        check("plan: A 到期可清", ra["id"] in ids_due, plan["counts"])
        check("plan: B 到期 hold", rb["id"] in ids_hold, plan["counts"])
        check("plan: C 到期可清（原件还在）", rc["id"] in ids_due, plan["counts"])
        check("plan: D 手动 → keep", rd["id"] in ids_keep, plan["counts"])
        check("plan: E 墓碑 → 可清", re_["id"] in ids_due, plan["counts"])
        check("plan: G 墓碑未到期 → keep", rg["id"] in ids_keep, plan["counts"])
        check("plan: F 未到期 → keep", rf["id"] in ids_keep, plan["counts"])
        check("plan: H 进入提醒窗口 → soon", rh["id"] in ids_soon, plan["counts"])
        check("plan: hold 说明带路径", rb["id"] in ids_hold
              and any("app.py" in str(x) for x in (plan["hold"][0].get("hold_reasons") or [])),
              plan["hold"][0].get("hold_reasons"))

        # ---------- dry_run ----------
        dry = sn.cleanup_quarantine(now=now, dry_run=True)
        check("dry_run: 列出 3 条", len(dry.get("ids") or []) == 3, dry.get("ids"))
        check("dry_run: 一条没删", not dry.get("dropped"), dry.get("dropped"))
        check("dry_run: 目录还在", os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, ra["id"].replace("/", os.sep))))

        # ---------- 只清指定 id（后台守护的宽限逻辑用） ----------
        one = sn.cleanup_quarantine(now=now, dry_run=True, ids=[ra["id"]])
        check("ids 过滤: 只列指定那条", one.get("ids") == [ra["id"]], one.get("ids"))
        check("ids 过滤: 其它 due 不在里面", rc["id"] not in (one.get("ids") or []), one.get("ids"))

        # ---------- cleanup=False ----------
        sn.set_quarantine_config(cleanup=False)
        off = sn.cleanup_quarantine(now=now)
        check("cleanup=False: 不清", off.get("enabled") is False and not off.get("dropped"), off)
        sn.set_quarantine_config(cleanup=True)

        # ---------- 真清理 ----------
        res = sn.cleanup_quarantine(now=now)
        check("cleanup: 清 3 条", len(res.get("dropped") or []) == 3, res.get("dropped"))
        check("cleanup: 释放字节 > 0", (res.get("freed") or 0) > 0, res.get("freed"))
        check("cleanup: A 已删", not os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, ra["id"].replace("/", os.sep))))
        check("cleanup: B 仍保留", os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, rb["id"].replace("/", os.sep))))
        check("cleanup: D 仍保留", os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, rd["id"].replace("/", os.sep))))
        check("cleanup: F 仍保留", os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, rf["id"].replace("/", os.sep))))
        check("cleanup: G 仍保留", os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, rg["id"].replace("/", os.sep))))
        check("cleanup: H 仍保留", os.path.isdir(os.path.join(
            sn.QUARANTINE_DIR, rh["id"].replace("/", os.sep))))

        # ---------- 列表字段 / 墓碑恢复 ----------
        items = sn.list_quarantine(limit=100)
        by_id = {x["id"]: x for x in items}
        check("list: auto 标记", bool(by_id.get(rg["id"], {}).get("auto")), by_id.get(rg["id"]))
        check("list: tombstone 标记", bool(by_id.get(rg["id"], {}).get("tombstone")))
        check("list: max_star", int(by_id.get(rb["id"], {}).get("max_star") or 0) == 4,
              by_id.get(rb["id"]))
        check("list: expires_at 有值", (by_id.get(rf["id"], {}).get("expires_at") or 0) > 0)
        rr = sn.restore_quarantine(rg["id"])
        check("restore 墓碑: 不报错", rr.get("ok") is True, rr)
        check("restore 墓碑: 明确记 tombstone", int(rr.get("tombstone") or 0) == 1, rr)

        # ---------- 手动记录恢复仍正常 ----------
        if os.path.exists(fd):
            os.remove(fd)
        rr2 = sn.restore_quarantine(rd["id"])
        check("restore 手动: 放回原处", int(rr2.get("restored") or 0) == 1, rr2)
        check("restore 手动: 文件真的回来了", os.path.exists(fd))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    print("\n%d passed / %d failed" % (N_PASS, N_FAIL))
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
