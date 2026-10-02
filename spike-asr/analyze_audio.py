"""
音频结构分析：判断课堂录音里到底有多少时间是真的在讲课。

⚠️ 一次性探针代码。

看三件事：
1. 音量随时间的分布（老师是不是忽远忽近）
2. 有声段占比（多少时间是静音/噪声）
3. 动态范围（有声音时和没声音时差多少 —— 差得越多说明信噪比越好）
"""

import sys
import wave
from pathlib import Path

import numpy as np

WINDOW_S = 1.0  # 每秒算一个能量值


def main(path: Path) -> None:
    with wave.open(str(path), "rb") as f:
        sr = f.getframerate()
        n = f.getnframes()
        raw = f.readframes(n)
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    dur = len(x) / sr

    win = int(sr * WINDOW_S)
    nwin = len(x) // win
    frames = x[: nwin * win].reshape(nwin, win)
    rms = np.sqrt((frames**2).mean(axis=1))
    db = 20 * np.log10(np.maximum(rms, 1e-10))

    print(f"文件: {path.name}")
    print(f"时长: {dur:.0f}s ({dur/60:.1f} 分钟), 采样率 {sr}")

    # 用百分位数定阈值，不依赖绝对音量
    p10, p50, p90, p99 = np.percentile(db, [10, 50, 90, 99])
    print(f"\n每秒能量分布 (dB):")
    print(f"  p10  = {p10:6.1f}   (最安静的 10% 时间)")
    print(f"  p50  = {p50:6.1f}   (中位数)")
    print(f"  p90  = {p90:6.1f}   (较响的 10%)")
    print(f"  p99  = {p99:6.1f}   (峰值)")
    print(f"  动态范围 p90-p10 = {p90-p10:.1f} dB")

    # 「有声」判据：高于中位数 6dB 且高于绝对底线
    speech_thresh = max(p50 + 6, -45)
    active = db > speech_thresh
    pct = active.mean() * 100
    print(f"\n有声段判定 (阈值 {speech_thresh:.1f} dB):")
    print(f"  有声时长 {active.sum()}s / {nwin}s = {pct:.1f}%")
    print(f"  -> 131 分钟的录音里，真正有声音的只有约 {active.sum()/60:.0f} 分钟")

    # 每 10 分钟一个块，看看老师是不是有时离得近有时远
    block = 600
    nb = nwin // block
    if nb:
        print(f"\n每 10 分钟的中位音量 (看老师是不是忽远忽近):")
        for i in range(nb):
            seg = db[i * block : (i + 1) * block]
            seg_active = (seg > speech_thresh).mean() * 100
            bar = "#" * int((np.median(seg) + 60) / 2)
            print(f"  {i*10:3d}-{i*10+10:3d} 分: {np.median(seg):6.1f} dB  有声 {seg_active:5.1f}%  {bar}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
