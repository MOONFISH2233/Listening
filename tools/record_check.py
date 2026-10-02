#!/usr/bin/env python3
"""录音体检 —— 录完当场跑，不行就重录，不用等 ASR 跑完。

用法:
    python record_check.py 录音.m4a
    python record_check.py 录音1.m4a 录音2.m4a
    python record_check.py 录音.m4a --json

需要 ffmpeg 在 PATH 里（读 m4a/mp3 等格式；标准 16k 单声道 wav 不需要）。
"""

import argparse
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

# ---- 阈值（硬编码，不是配置项）----
# 当前只有一批素材、一个场景。等积累了多种录音环境的数据，
# 再决定阈值要不要按场景分化 —— 现在拆成配置文件是过早抽象。
THRESHOLDS = {
    "snr_db": 25.0,          # > 合格
    "hf_rolloff_db": -15.0,  # > 合格
    "dynamic_range_db": 25.0,# > 合格
    "clip_pct": 0.1,         # < 合格
}

# 与 spike-asr/analyze_spectrum.py 保持一致，方便对比历史数据
BANDS = {
    "voice": (400, 2000),    # 人声核心
    "hf_low": (2000, 4000),  # 辅音区
    "hf_high": (4000, 8000),
}

FRAME_MS = 20
HOP_MS = 10
EPS = 1e-12


def load_audio(path: Path) -> tuple[np.ndarray, int]:
    """返回 (单声道 float32 波形, 采样率)。"""
    if path.suffix.lower() == ".wav":
        try:
            return _load_wav(path)
        except wave.Error:
            pass  # 非标准 wav，回退 ffmpeg
    return _load_ffmpeg(path)


def _load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        n_ch = w.getnchannels()
        sampwidth = w.getsampwidth()
        sr = w.getframerate()
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)

    if sampwidth == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sampwidth == 1:
        data = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif sampwidth == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise wave.Error(f"unsupported sampwidth {sampwidth}")

    if n_ch > 1:
        data = data.reshape(-1, n_ch).mean(axis=1)
    return data, sr


def _load_ffmpeg(path: Path) -> tuple[np.ndarray, int]:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg not found in PATH (needed for non-wav formats)")
    sr = 16000
    cmd = [
        "ffmpeg", "-v", "error", "-i", str(path),
        "-ac", "1", "-ar", str(sr), "-f", "f32le", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace').strip()}")
    data = np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)
    return data, sr


def frame_rms(x: np.ndarray, sr: int) -> np.ndarray:
    frame = max(1, int(sr * FRAME_MS / 1000))
    hop = max(1, int(sr * HOP_MS / 1000))
    if len(x) < frame:
        return np.array([np.sqrt(np.mean(x ** 2) + EPS)])
    n = 1 + (len(x) - frame) // hop
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx]
    return np.sqrt(np.mean(frames ** 2, axis=1) + EPS)


def band_energy_db(x: np.ndarray, sr: int) -> dict[str, float]:
    """各频段平均能量（dB，相对人声核心）。

    逐秒分帧算功率谱再平均。不要对整段做一次 FFT —— 7854 秒整段 FFT 的窗长
    等于全长，频谱分辨率失去物理意义，高频能量被稀释（实测差了 14 dB）。
    分帧平均与 spike-asr/analyze_spectrum.py 一致，历史数据可直接对比。
    """
    win = sr
    n = len(x) // win
    if n == 0:
        win = len(x)
        n = 1
    if win < 16:
        return {k: -np.inf for k in BANDS}
    frames = x[: n * win].reshape(n, win)
    w = np.hanning(win)
    spec = np.zeros(win // 2 + 1)
    for fr in frames:
        spec += np.abs(np.fft.rfft(fr * w)) ** 2
    spec /= n
    freqs = np.fft.rfftfreq(win, 1.0 / sr)

    def energy(lo: float, hi: float) -> float:
        mask = (freqs >= lo) & (freqs < hi)
        return float((spec[mask] ** 2).sum())

    ref = max(energy(*BANDS["voice"]), EPS)
    return {
        name: float(10.0 * np.log10(max(energy(lo, hi), EPS) / ref))
        for name, (lo, hi) in BANDS.items()
    }


def analyze(path: Path) -> dict:
    x, sr = load_audio(path)
    if len(x) == 0:
        raise RuntimeError("empty audio")

    rms = frame_rms(x, sr)
    rms_db = 20.0 * np.log10(rms + EPS)

    # SNR: 95 分位 - 5 分位（不依赖 VAD）
    snr_db = float(np.percentile(rms_db, 95) - np.percentile(rms_db, 5))
    # 动态范围: 90 分位 - 10 分位
    dyn_db = float(np.percentile(rms_db, 90) - np.percentile(rms_db, 10))
    # 削波
    clip_pct = float(np.mean(np.abs(x) > 0.999) * 100.0)

    bands = band_energy_db(x, sr)
    hf_rolloff_db = float((bands["hf_low"] + bands["hf_high"]) / 2.0)

    return {
        "file": str(path),
        "duration_s": round(len(x) / sr, 2),
        "sample_rate": sr,
        "snr_db": round(snr_db, 1),
        "hf_rolloff_db": round(hf_rolloff_db, 1),
        "dynamic_range_db": round(dyn_db, 1),
        "clip_pct": round(clip_pct, 4),
        "pass": (
            snr_db > THRESHOLDS["snr_db"]
            and hf_rolloff_db > THRESHOLDS["hf_rolloff_db"]
            and dyn_db > THRESHOLDS["dynamic_range_db"]
            and clip_pct < THRESHOLDS["clip_pct"]
        ),
    }


def print_report(r: dict) -> None:
    def mark(key, ok):
        return "合格" if ok else "不合格"

    snr_ok = r["snr_db"] > THRESHOLDS["snr_db"]
    hf_ok = r["hf_rolloff_db"] > THRESHOLDS["hf_rolloff_db"]
    dyn_ok = r["dynamic_range_db"] > THRESHOLDS["dynamic_range_db"]
    clip_ok = r["clip_pct"] < THRESHOLDS["clip_pct"]

    print(f"\n{r['file']}  ({r['duration_s']}s @ {r['sample_rate']}Hz)")
    print(f"  信噪比 SNR      {r['snr_db']:>7.1f} dB   {mark('snr', snr_ok)}")
    print(f"  高频衰减        {r['hf_rolloff_db']:>7.1f} dB   {mark('hf', hf_ok)}")
    print(f"  动态范围        {r['dynamic_range_db']:>7.1f} dB   {mark('dyn', dyn_ok)}")
    print(f"  削波            {r['clip_pct']:>7.4f} %   {mark('clip', clip_ok)}")
    print(f"  => {'通过' if r['pass'] else '不通过，建议重录'}")


def main() -> int:
    ap = argparse.ArgumentParser(description="录音体检")
    ap.add_argument("files", nargs="+", help="录音文件")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    results = []
    failed = False
    for f in args.files:
        p = Path(f)
        if not p.exists():
            print(f"[跳过] 文件不存在: {f}", file=sys.stderr)
            failed = True
            continue
        try:
            r = analyze(p)
        except Exception as e:
            print(f"[错误] {f}: {e}", file=sys.stderr)
            failed = True
            continue
        results.append(r)
        if not args.json:
            print_report(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))

    if failed or any(not r["pass"] for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
