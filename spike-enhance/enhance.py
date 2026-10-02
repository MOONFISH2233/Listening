"""信号修复实验：录坏了的课堂录音，算法能救回多少？

⚠️ 一次性验证代码。

spike-asr 的结论是「问题不在模型，在信号」——SNR 14.4 dB、2k-8k Hz
比人声核心低 26-28 dB。但那个结论没验证过：这些指标是可以通过算法改善的，
还是已经损失到无法挽回？

这个脚本把 analysis_report.md 里「建议做」的三件事真正做一遍：
    1. 高通滤波   去低频隆隆
    2. 谱减降噪   压稳态噪声
    3. 高频补偿   提 2k-8k Hz

每一步都可单独开关，便于定位到底哪一步有用。

用法：
    python enhance.py 输入.wav -o 输出.wav              # 全开
    python enhance.py 输入.wav -o 输出.wav --no-denoise # 只做高通+高频补偿
    python enhance.py 输入.wav --steps                  # 逐步输出各阶段指标

判据不是指标好看，是处理完喂给 ASR 能不能成句。指标只是代理。
"""

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

SR = 16000

# ---- 各步参数 ----
HP_CUTOFF = 100.0      # 高通截止频率 Hz
HP_ORDER = 4           # 巴特沃斯阶数
NR_OVERSUB = 1.5       # 谱减过减因子（越大越激进，也越容易出音乐噪声）
NR_FLOOR = 0.05        # 谱减后的谱底，防止减到零
NR_SMOOTH = 0.9        # 噪声谱估计的平滑系数
HF_GAIN_DB = 8.0       # 高频补偿增益
HF_START = 2000.0      # 补偿起始频率
HF_FULL = 4000.0       # 达到全增益的频率


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as f:
        assert f.getsampwidth() == 2, "只支持 16bit wav"
        n_ch = f.getnchannels()
        sr = f.getframerate()
        raw = f.readframes(f.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if n_ch > 1:
        x = x.reshape(-1, n_ch).mean(axis=1)
    return x, sr


def save_wav(path: Path, x: np.ndarray, sr: int) -> None:
    peak = np.max(np.abs(x))
    if peak > 0.999:
        x = x / peak * 0.999
    pcm = (x * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(sr)
        f.writeframes(pcm.tobytes())


def highpass(x: np.ndarray, sr: int, cutoff: float, order: int = HP_ORDER) -> np.ndarray:
    """高通滤波，频域实现（零相位）。

    不用 IIR 逐样本循环：7854 秒 = 1.25 亿个样本，Python 循环要跑几十秒，
    而这里要的只是「去掉 100 Hz 以下的隆隆声」，频域一刀切足够。
    零相位不会引入时延，对后续 ASR 对齐更安全。

    过渡带用余弦爬升（cutoff/2 -> cutoff），避免砖墙滤波的振铃污染语音。
    order 参数保留只为签名兼容，频域实现用不到。
    """
    n = len(x)
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)

    lo, hi = cutoff * 0.5, cutoff
    gain = np.ones_like(freqs)
    gain[freqs <= lo] = 0.0
    ramp = (freqs > lo) & (freqs < hi)
    t = (freqs[ramp] - lo) / (hi - lo)
    gain[ramp] = 0.5 - 0.5 * np.cos(np.pi * t)

    return np.fft.irfft(spec * gain, n=n).astype(np.float32)


def spectral_subtract(
    x: np.ndarray,
    sr: int,
    oversub: float = NR_OVERSUB,
    floor: float = NR_FLOOR,
    smooth: float = NR_SMOOTH,
    frame_ms: int = 32,
    hop_ms: int = 16,
) -> np.ndarray:
    """谱减法降噪。

    噪声谱用「最安静的前 10% 帧」估计 —— 课堂录音里静音段占比高，
    这个估计比全程滑动平均更接近真实噪声。
    """
    frame = int(sr * frame_ms / 1000)
    hop = int(sr * hop_ms / 1000)
    n = 1 + max(0, (len(x) - frame) // hop)
    win = np.hanning(frame)

    # 分帧
    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx] * win

    spec = np.fft.rfft(frames, axis=1)
    mag = np.abs(spec)
    phase = np.angle(spec)

    # 噪声谱估计：取每帧总能量，选最低的 10% 帧平均
    energy = (mag ** 2).sum(axis=1)
    k = max(1, int(n * 0.10))
    quiet_idx = np.argsort(energy)[:k]
    noise_mag = mag[quiet_idx].mean(axis=0, keepdims=True)
    # 平滑，避免噪声估计太尖锐
    noise_mag = smooth * noise_mag + (1 - smooth) * mag.mean(axis=0, keepdims=True)

    # 谱减
    clean_mag = mag - oversub * noise_mag
    clean_mag = np.maximum(clean_mag, floor * mag)

    clean_spec = clean_mag * np.exp(1j * phase)
    out_frames = np.fft.irfft(clean_spec, n=frame, axis=1) * win

    # 重叠相加
    y = np.zeros(len(x), dtype=np.float64)
    wsum = np.zeros(len(x), dtype=np.float64)
    for i in range(n):
        s = i * hop
        y[s : s + frame] += out_frames[i]
        wsum[s : s + frame] += win ** 2
    wsum = np.maximum(wsum, 1e-8)
    return (y / wsum).astype(np.float32)


def hf_compensate(
    x: np.ndarray,
    sr: int,
    gain_db: float = HF_GAIN_DB,
    start: float = HF_START,
    full: float = HF_FULL,
    frame_ms: int = 32,
    hop_ms: int = 16,
) -> np.ndarray:
    """高频补偿：2k-4k 线性爬升，4k 以上全增益。频域实现，零相位。"""
    frame = int(sr * frame_ms / 1000)
    hop = int(sr * hop_ms / 1000)
    n = 1 + max(0, (len(x) - frame) // hop)
    win = np.hanning(frame)

    idx = np.arange(frame)[None, :] + hop * np.arange(n)[:, None]
    frames = x[idx] * win
    spec = np.fft.rfft(frames, axis=1)
    freqs = np.fft.rfftfreq(frame, 1 / sr)

    # gain 这里是「补偿量的比例」：0 = 原样不动，1 = 提升满额 gain_db。
    # 必须初始化为 0。初始化为 1 会让 0-start 的整个频段都被当成满额补偿，
    # 结果是全频段抬升、只在 start 处留一个凹陷，频谱相对关系被搞乱
    # （实测：高频衰减从 -32.3 恶化到 -36.7，而本该改善）。
    gain = np.zeros_like(freqs)
    ramp = (freqs >= start) & (freqs < full)
    gain[ramp] = (freqs[ramp] - start) / (full - start)
    gain[freqs >= full] = 1.0
    gain = 10 ** (gain_db * gain / 20.0)

    out_frames = np.fft.irfft(spec * gain[None, :], n=frame, axis=1) * win

    y = np.zeros(len(x), dtype=np.float64)
    wsum = np.zeros(len(x), dtype=np.float64)
    for i in range(n):
        s = i * hop
        y[s : s + frame] += out_frames[i]
        wsum[s : s + frame] += win ** 2
    wsum = np.maximum(wsum, 1e-8)
    return (y / wsum).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description="信号修复实验")
    ap.add_argument("audio", type=Path)
    ap.add_argument("-o", "--output", type=Path, help="输出 wav")
    ap.add_argument("--no-highpass", action="store_true")
    ap.add_argument("--no-denoise", action="store_true")
    ap.add_argument("--no-hf", action="store_true")
    ap.add_argument("--steps", action="store_true",
                    help="每步存一个中间文件，便于逐步对比")
    ap.add_argument("--outdir", type=Path, default=Path("out"),
                    help="--steps 时的输出目录")
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"找不到文件: {args.audio}", file=sys.stderr)
        return 1

    print(f"读取 {args.audio.name} ...", end="", flush=True)
    x, sr = load_wav(args.audio)
    print(f" {len(x)/sr/60:.1f} 分钟 @ {sr} Hz")

    do_hp = not args.no_highpass
    do_nr = not args.no_denoise
    do_hf = not args.no_hf

    if args.steps:
        args.outdir.mkdir(parents=True, exist_ok=True)

    cur = x
    if do_hp:
        print(f"1. 高通 {HP_CUTOFF:.0f} Hz ...", end="", flush=True)
        cur = highpass(cur, sr, HP_CUTOFF, HP_ORDER)
        print(" 完成")
        if args.steps:
            save_wav(args.outdir / "step1_highpass.wav", cur, sr)

    if do_nr:
        print(f"2. 谱减降噪 (过减 {NR_OVERSUB}) ...", end="", flush=True)
        cur = spectral_subtract(cur, sr)
        print(" 完成")
        if args.steps:
            save_wav(args.outdir / "step2_denoise.wav", cur, sr)

    if do_hf:
        print(f"3. 高频补偿 +{HF_GAIN_DB} dB @ {HF_FULL:.0f} Hz ...", end="", flush=True)
        cur = hf_compensate(cur, sr)
        print(" 完成")
        if args.steps:
            save_wav(args.outdir / "step3_hf.wav", cur, sr)

    out = args.output or args.audio.with_name(args.audio.stem + "_enhanced.wav")
    save_wav(out, cur, sr)
    print(f"\n输出: {out}")
    print("下一步: py -3.11 ..\\tools\\record_check.py " + str(out))
    print("再下一步: 拿它跑 run_asr.py，看转出来的字成不成句")
    return 0


if __name__ == "__main__":
    sys.exit(main())
