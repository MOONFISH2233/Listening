"""
探针脚本：端侧流式中文 ASR 在真实课堂录音上够不够准、够不够快。

⚠️ 这是一次性验证代码，不是产品的一部分。验证完即弃。

要回答的问题：sherpa-onnx 的 int8 流式 zipformer（中英双语）在教室远场录音上，
准确率能不能支撑「点名时给答案」这个场景？

用法：
    python run_asr.py <音频文件> [<音频文件2> ...]
    python run_asr.py <音频文件> --realtime     # 模拟手机上的实时滚动
    python run_asr.py <音频文件> --chunk-ms 200 # 调分块大小

注意：脚本只输出文字和耗时。准确率必须由人耳核对——机器判断不了自己转得对不对。
"""

import argparse
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

MODEL_DIR = Path(__file__).parent / "models"
SAMPLE_RATE = 16000


def to_wav_16k_mono(src: Path, dst: Path) -> None:
    """手机录的课堂音频多半是 m4a/44.1kHz 立体声，先用 ffmpeg 归一到 16k 单声道。"""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as f:
        assert f.getframerate() == SAMPLE_RATE, f"采样率应为 {SAMPLE_RATE}，实际 {f.getframerate()}"
        assert f.getnchannels() == 1, f"应为单声道，实际 {f.getnchannels()}"
        assert f.getsampwidth() == 2, f"应为 16bit，实际 {f.getsampwidth() * 8}bit"
        raw = f.readframes(f.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def load_audio(path: Path) -> np.ndarray:
    """任何格式 -> 16kHz 单声道 float32。已是目标格式就直读，省一次转换。"""
    if path.suffix.lower() == ".wav":
        try:
            return read_wav(path)
        except AssertionError:
            pass  # 采样率/声道数不对，落到 ffmpeg 重转
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "conv.wav"
        to_wav_16k_mono(path, tmp)
        return read_wav(tmp)


def build_recognizer(num_threads: int, hotwords: Path | None = None) -> sherpa_onnx.OnlineRecognizer:
    kwargs = dict(
        tokens=str(MODEL_DIR / "tokens.txt"),
        encoder=str(MODEL_DIR / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(MODEL_DIR / "decoder-epoch-99-avg-1.int8.onnx"),
        joiner=str(MODEL_DIR / "joiner-epoch-99-avg-1.int8.onnx"),
        num_threads=num_threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method="greedy_search",
        # 断句规则：静音多久算一句说完。rule1/2 是两套阈值，
        # rule3 兜底防止一直没人说话时永不切分。
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=1.2,
        rule3_min_utterance_length=300,
        provider="cpu",
    )
    if hotwords is not None:
        kwargs["hotwords_file"] = str(hotwords)
        kwargs["hotwords_score"] = 1.5
    return sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)


def transcribe(rec, samples: np.ndarray, chunk_ms: int, realtime: bool):
    """按 chunk_ms 切块喂进去，模拟手机上的流式采集。"""
    stream = rec.create_stream()
    chunk_size = int(SAMPLE_RATE * chunk_ms / 1000)
    segments: list[tuple[float, float, str]] = []
    seg_start = 0.0
    last_shown = ""

    for i in range(0, len(samples), chunk_size):
        block = samples[i : i + chunk_size]
        stream.accept_waveform(SAMPLE_RATE, block)
        while rec.is_ready(stream):
            rec.decode_stream(stream)

        now = (i + len(block)) / SAMPLE_RATE
        partial = rec.get_result(stream)

        if realtime and partial != last_shown:
            last_shown = partial
            print(f"\r  [{now:7.1f}s] {partial[:70]:<70}", end="", flush=True)

        if rec.is_endpoint(stream):
            final = partial.strip()
            if final:
                segments.append((seg_start, now, final))
                if realtime:
                    print(f"\r  [{seg_start:7.1f}s -> {now:7.1f}s] {final}")
            rec.reset(stream)
            last_shown = ""
            seg_start = now

    # 收尾：最后一段可能没有触发 endpoint（音频到头时静音不够长）
    tail = rec.get_result(stream).strip()
    if tail:
        segments.append((seg_start, len(samples) / SAMPLE_RATE, tail))
        if realtime:
            print(f"\r  [{seg_start:7.1f}s -> {len(samples)/SAMPLE_RATE:7.1f}s] {tail}")

    return segments


def main() -> int:
    ap = argparse.ArgumentParser(description="端侧流式 ASR 探针")
    ap.add_argument("audio", nargs="+", type=Path, help="音频文件（wav/m4a/mp3 都行）")
    ap.add_argument("--chunk-ms", type=int, default=100, help="分块时长，默认 100ms")
    ap.add_argument("--threads", type=int, default=4, help="解码线程数")
    ap.add_argument("--realtime", action="store_true", help="模拟实时滚动显示")
    ap.add_argument("--hotwords", type=Path, help="热词表（每行一个词）")
    args = ap.parse_args()

    for p in args.audio:
        if not p.exists():
            print(f"找不到文件: {p}", file=sys.stderr)
            return 1

    print("加载模型...", end="", flush=True)
    t0 = time.perf_counter()
    rec = build_recognizer(args.threads, args.hotwords)
    print(f" {time.perf_counter() - t0:.1f}s")
    print(f"模型: zipformer bilingual zh-en, int8, {args.threads} 线程\n")

    total_audio = 0.0
    total_proc = 0.0

    for path in args.audio:
        samples = load_audio(path)
        dur = len(samples) / SAMPLE_RATE
        total_audio += dur

        print("=" * 72)
        print(f"{path.name}  ({dur:.1f} 秒)")
        print("=" * 72)

        t0 = time.perf_counter()
        segments = transcribe(rec, samples, args.chunk_ms, args.realtime)
        elapsed = time.perf_counter() - t0
        total_proc += elapsed

        if not args.realtime:
            for start, end, text in segments:
                print(f"  [{start:7.1f}s -> {end:7.1f}s] {text}")

        rtf = elapsed / dur if dur else 0
        print(f"\n  --- {len(segments)} 段 | 音频 {dur:.1f}s | 耗时 {elapsed:.1f}s | "
              f"RTF {rtf:.3f} ({'实时富余' if rtf < 1 else '⚠️ 跟不上'}) ---\n")

    if len(args.audio) > 1:
        print("=" * 72)
        rtf = total_proc / total_audio if total_audio else 0
        print(f"合计: 音频 {total_audio:.1f}s | 耗时 {total_proc:.1f}s | RTF {rtf:.3f}")
        print(f"（RTF < 1 表示处理比录音快，手机上能实时跟上）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
