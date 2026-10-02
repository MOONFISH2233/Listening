"""
频谱分析：判断录音是怎么录坏的。

⚠️ 一次性探针代码。

假设验证：
- 低频能量过高找不到 -> 手机在兜里/包里，或放在震动的桌子上（隆隆声）
- 高频能量明显衰减   -> 麦克风被遮住（布料吸高频）
- 全频段都平        -> 就是离得远 + 房间噪声大
"""

import sys
import wave
from pathlib import Path

import numpy as np

BANDS = [
    ("低频隆隆 20-150Hz", 20, 150),
    ("人声基频 150-400Hz", 150, 400),
    ("人声核心 400-2kHz", 400, 2000),
    ("辅音/齿音 2k-4kHz", 2000, 4000),
    ("高频 4k-8kHz", 4000, 8000),
]


def band_energy_db(spec: np.ndarray, freqs: np.ndarray, lo: float, hi: float) -> float:
    m = (freqs >= lo) & (freqs < hi)
    e = (spec[m] ** 2).sum()
    return 10 * np.log10(max(e, 1e-20))


def main(path: Path) -> None:
    with wave.open(str(path), "rb") as f:
        sr = f.getframerate()
        raw = f.readframes(f.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # 1 秒一帧
    win = sr
    n = len(x) // win
    frames = x[: n * win].reshape(n, win)
    # 加窗 + 平均功率谱
    w = np.hanning(win)
    spec = np.zeros(win // 2 + 1)
    for fr in frames:
        spec += np.abs(np.fft.rfft(fr * w)) ** 2
    spec /= n
    freqs = np.fft.rfftfreq(win, 1 / sr)

    print(f"文件: {path.name}  ({len(x)/sr/60:.1f} 分钟)\n")
    print("平均功率谱分频段能量（相对中频人声核心）:")
    ref = band_energy_db(spec, freqs, 400, 2000)
    for name, lo, hi in BANDS:
        e = band_energy_db(spec, freqs, lo, hi)
        rel = e - ref
        bar = "#" * max(0, int((rel + 60) / 2))
        print(f"  {name:22s} {e:7.1f} dB   相对 {rel:+6.1f} dB  {bar}")

    # 逐秒能量分布：判断语音是否真的高出噪声
    win2 = int(sr * 0.5)
    n2 = len(x) // win2
    rms = np.sqrt((x[: n2 * win2].reshape(n2, win2) ** 2).mean(axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-10))
    quiet = np.percentile(db, 5)
    loud = np.percentile(db, 95)
    print(f"\n噪声底 (p5)      = {quiet:6.1f} dB")
    print(f"语音峰 (p95)     = {loud:6.1f} dB")
    print(f"信噪比 SNR 估计  = {loud - quiet:6.1f} dB")
    print()
    if loud - quiet < 15:
        print("  ⚠️ SNR < 15 dB —— 语音和噪声几乎一样响，ASR 基本无解")
    elif loud - quiet < 25:
        print("  ⚠️ SNR 15-25 dB —— 勉强，大模型能扛，小模型会崩")
    else:
        print("  ✅ SNR > 25 dB —— 够用")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
