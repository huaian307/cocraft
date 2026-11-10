# -*- coding: utf-8 -*-
"""把「系统输出（扬声器回环）」的频谱写成 _spectrum.json，供自建界面画随音乐跳动的音频条。

原理：WASAPI loopback 采集系统正在播放的声音 → 分块 FFT → 对数分频成 N 个频段 → 快起慢落平滑。

⚠ 必须跟着「当前默认输出设备」走，不能只在启动时定一次：
    蓝牙耳机一连上，系统默认输出就切走了 —— 老的回环流会永远采到一片静音，
    而它还在**不停写文件**，于是 server.py 的 spectrum_keeper()（只看文件新不新鲜）
    永远不会重拉它，表现就是「音频条一直贴在基线、怎么都不跳」。
   本脚本因此有两条自愈：
     ① 每 2 秒比对默认设备名，变了就重开回环；
     ② 连续静音 30 秒也重开一次（蓝牙耳机会自己进省电、把流放成死的）。

用法：
    python spectrum.py                 # 常驻，持续写 _spectrum.json
    python spectrum.py --test 4        # 只测 4 秒，把频段值打到 stdout（ASCII）
    python spectrum.py --out <file>    # 指定输出文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import soundcard as sc

import warnings

warnings.filterwarnings("ignore")          # WASAPI 回环偶发 "data discontinuity"，刷 stderr 没意义

BLOCK = 2048
BANDS = 48                             # 段数：越多越"密"、相邻段差异越大（起伏更明显）
FMIN, FMAX = 40.0, 16000.0
ATTACK, RELEASE = 0.60, 0.13          # 上升快、下降慢（看着像真的在跳）
DB_FLOOR = 50.0                       # -50 dB(相对满幅) 记作 0，0 dB 记作 1
REF = BLOCK / 4.0                     # 满幅正弦经 Hann 窗后在某频段的峰值参考
RATES = (48000, 44100, 16000)         # 蓝牙耳机可能不支持 48k，逐个降级试
CHECK_DEVICE_EVERY = 2.0
IDLE_REOPEN_AFTER = 30.0


def write(path: str, obj: dict) -> None:
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        pass


def open_loopback():
    """按**当前**默认输出设备开一路回环，返回 (设备名, mic)。

    ⚠ 必须用 **id** 精确挑回环，不能 `sc.get_microphone(名字)` ——
    `include_loopback=True` 只是把回环也放进候选，按名字匹配可能抓到**同名的麦克风端点**。
    蓝牙耳机就是这种情况：播放端与麦克风端点**同名**（都叫「耳机 (YSX6)」），
    名字匹配会返回麦克风 → 永远采到一片静音（实测峰值 0.00003，按 id 挑是 0.29）。
    """
    spk = sc.default_speaker()
    sid = str(spk.id)
    mics = list(sc.all_microphones(include_loopback=True))
    for m in mics:                                   # ① id 完全相同 + 是回环
        if getattr(m, "isloopback", False) and str(m.id) == sid:
            return str(spk.name), m
    for m in mics:                                   # ② 退一步：同名且是回环
        if getattr(m, "isloopback", False) and m.name == spk.name:
            return str(spk.name), m
    return str(spk.name), sc.get_microphone(str(spk.name), include_loopback=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    state_dir = os.path.join(root, "runtime", "state")
    ap.add_argument("--out", default=os.path.join(state_dir, "_spectrum.json"))
    ap.add_argument("--test", type=float, default=0.0, help="测试秒数，0=常驻")
    args = ap.parse_args()

    print("SPEAKERS:", ", ".join(s.name for s in sc.all_speakers()), file=sys.stderr)
    try:
        name, mic = open_loopback()
    except Exception as exc:  # noqa: BLE001
        print(f"LOOPBACK_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        write(args.out, {"error": f"{type(exc).__name__}: {exc}", "bars": []})
        return 1
    print(f"LOOPBACK_OK speaker={name!r}", file=sys.stderr)

    edges = np.geomspace(FMIN, FMAX, BANDS + 1)
    win = np.hanning(BLOCK)
    bars = np.zeros(BANDS)
    frames = 0
    peak = 0.0
    t0 = time.time()
    silent_since = time.time()
    last_write = 0.0
    rate = RATES[0]

    while True:
        try:
            with mic.recorder(samplerate=rate, channels=2, blocksize=BLOCK) as rec:
                last_check = time.time()
                while True:
                    data = rec.record(numframes=BLOCK)
                    mono = data.mean(axis=1) if getattr(data, "ndim", 1) > 1 else data
                    rms = float(np.sqrt((mono ** 2).mean()))
                    peak = max(peak, rms)
                    if rms > 1e-4:
                        silent_since = time.time()

                    # ★静音时不跑 FFT / 48 段（那是主要 CPU）：只让柱子缓慢落回，
                    #   每 0.5s 写一次零值；有声音再恢复完整计算。
                    idle = (not args.test) and rms <= 1e-4
                    if idle:
                        bars = bars * (1.0 - RELEASE)
                    else:
                        spec = np.abs(np.fft.rfft(mono * win))
                        freqs = np.fft.rfftfreq(len(mono), 1.0 / rate)
                        vals = np.empty(BANDS)
                        for i in range(BANDS):
                            m = (freqs >= edges[i]) & (freqs < edges[i + 1])
                            # 取频段内峰值：不受频段宽窄影响（用均值的话，段越窄读得越高，容易顶到 1.0）
                            vals[i] = spec[m].max() if m.any() else 0.0
                        db = 20.0 * np.log10(vals / REF + 1e-9)
                        norm = np.clip((db + DB_FLOOR) / DB_FLOOR, 0.0, 1.0)
                        up = norm > bars
                        bars = np.where(up, bars + (norm - bars) * ATTACK,
                                        bars + (norm - bars) * RELEASE)
                        frames += 1

                    if args.test:
                        if frames % 15 == 0:
                            print("RMS=%.5f PEAK=%.5f BARS=%s" % (
                                rms, peak, " ".join("%.2f" % b for b in bars)), flush=True)
                        if time.time() - t0 >= args.test:
                            return 0
                        continue

                    now = time.time()
                    if not idle or (now - last_write >= 0.5):
                        write(args.out, {
                            "ts": int(now * 1000),
                            "device": name,
                            "rate": rate,
                            "rms": round(rms, 5),
                            "idle": bool(idle),
                            "bars": [round(float(b), 3) for b in bars],
                        })
                        last_write = now

                    if now - last_check < CHECK_DEVICE_EVERY:
                        continue
                    last_check = now
                    reopen = False
                    try:
                        if str(sc.default_speaker().name) != name:
                            print("DEVICE_CHANGED -> reopen", file=sys.stderr)
                            reopen = True
                    except Exception:  # noqa: BLE001
                        pass
                    if not reopen and now - silent_since > IDLE_REOPEN_AFTER:
                        print("STREAM_IDLE_REOPEN", file=sys.stderr)
                        reopen = True
                    if reopen:
                        try:
                            name, mic = open_loopback()
                            print(f"REOPENED on {name!r}", file=sys.stderr)
                        except Exception as exc:  # noqa: BLE001
                            print(f"REOPEN_FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
                            time.sleep(1.0)
                        bars = np.zeros(BANDS)
                        silent_since = time.time()
                        break
        except Exception as exc:  # noqa: BLE001
            print(f"RECORD_ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
            # 采样率可能不被这个设备支持（蓝牙常见）→ 换下一个再试
            i = RATES.index(rate) if rate in RATES else 0
            rate = RATES[(i + 1) % len(RATES)]
            print(f"RETRY rate={rate}", file=sys.stderr)
            time.sleep(1.5)
            try:
                name, mic = open_loopback()
            except Exception:  # noqa: BLE001
                pass
            bars = np.zeros(BANDS)
            silent_since = time.time()


if __name__ == "__main__":
    raise SystemExit(main())
