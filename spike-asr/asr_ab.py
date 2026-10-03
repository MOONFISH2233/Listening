"""
对照实验：解码方式与热词，对本地 ASR 可读性的影响。

⚠️ 一次性验证代码，验证完即弃。和 run_asr.py 分开写，是为了不动那份历史证据。

背景：
    run_asr.py 在真实课堂录音上输出的是「跟知道离去中哥哥很远」这种不成句的
    音节堆砌。但同一段音频，通义听悟转出了能读懂大意的句子。
    差别可能在模型，也可能在解码配置——这个脚本把配置这一维固定住。

    原探针用的是 greedy_search（最快、最不准）。这里把它和 modified_beam_search
    对比，同时对比有无热词表。四组配置跑同一段音频。

用法：
    python asr_ab.py <音频> [--hotwords 热词.txt] [--beam-size 4]
"""

import argparse
import sys
import time
from pathlib import Path

import sherpa_onnx

sys.path.insert(0, str(Path(__file__).parent))
from run_asr import MODEL_DIR, SAMPLE_RATE, load_audio  # noqa: E402


def build(decoding_method: str, num_threads: int, hotwords: Path | None, beam_size: int):
    kwargs = dict(
        tokens=str(MODEL_DIR / "tokens.txt"),
        encoder=str(MODEL_DIR / "encoder-epoch-99-avg-1.int8.onnx"),
        decoder=str(MODEL_DIR / "decoder-epoch-99-avg-1.int8.onnx"),
        joiner=str(MODEL_DIR / "joiner-epoch-99-avg-1.int8.onnx"),
        num_threads=num_threads,
        sample_rate=SAMPLE_RATE,
        feature_dim=80,
        decoding_method=decoding_method,
        enable_endpoint_detection=True,
        rule1_min_trailing_silence=2.4,
        rule2_min_trailing_silence=1.2,
        rule3_min_utterance_length=300,
        provider="cpu",
    )
    if decoding_method == "modified_beam_search":
        kwargs["max_active_paths"] = beam_size
    if hotwords is not None:
        kwargs["hotwords_file"] = str(hotwords)
        # 1.5 是 run_asr.py 里的默认值。热词权重太高会诱发幻觉（把没说的词
        # 也硬塞进来），所以这里不调大。
        kwargs["hotwords_score"] = 1.5
    return sherpa_onnx.OnlineRecognizer.from_transducer(**kwargs)


def transcribe_all(rec, samples, chunk_ms: int = 100) -> tuple[str, float]:
    """喂完整段，返回 (拼接后的全文, 耗时)。"""
    stream = rec.create_stream()
    chunk = int(SAMPLE_RATE * chunk_ms / 1000)
    pieces: list[str] = []

    t0 = time.perf_counter()
    for i in range(0, len(samples), chunk):
        stream.accept_waveform(SAMPLE_RATE, samples[i : i + chunk])
        while rec.is_ready(stream):
            rec.decode_stream(stream)
        if rec.is_endpoint(stream):
            text = rec.get_result(stream).strip()
            if text:
                pieces.append(text)
            rec.reset(stream)
    tail = rec.get_result(stream).strip()
    if tail:
        pieces.append(tail)
    return "".join(pieces), time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description="ASR 解码配置对照实验")
    ap.add_argument("audio", type=Path)
    ap.add_argument("--hotwords", type=Path, help="热词表（每行一个词）")
    ap.add_argument("--beam-size", type=int, default=4)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    if not args.audio.exists():
        print(f"找不到文件: {args.audio}", file=sys.stderr)
        return 1

    samples = load_audio(args.audio)
    dur = len(samples) / SAMPLE_RATE

    configs = [
        ("greedy_search", None),
        ("modified_beam_search", None),
    ]
    if args.hotwords is not None and args.hotwords.exists():
        # sherpa-onnx 限制：热词只支持 modified_beam_search。
        # 试过 greedy + 热词，它直接抛 ValueError。
        configs.append(("modified_beam_search", args.hotwords))
    else:
        print(f"（未提供热词表，跳过带热词的一组）\n")

    print(f"音频: {args.audio.name}  ({dur:.1f} 秒)")
    print(f"模型: zipformer bilingual zh-en, int8, {args.threads} 线程")
    if args.hotwords is not None and args.hotwords.exists():
        n = len([l for l in args.hotwords.read_text(encoding="utf-8").splitlines() if l.strip()])
        print(f"热词: {args.hotwords.name}（{n} 条）")
    print()

    for method, hw in configs:
        label = f"{method}" + (f" + 热词" if hw else "")
        print("=" * 72)
        print(f"【{label}】")
        print("=" * 72)
        t0 = time.perf_counter()
        rec = build(method, args.threads, hw, args.beam_size)
        load_s = time.perf_counter() - t0
        text, proc_s = transcribe_all(rec, samples)
        print(text)
        print(f"\n  --- 加载 {load_s:.1f}s | 解码 {proc_s:.1f}s | "
              f"RTF {proc_s / dur:.3f} | 输出 {len(text)} 字 ---\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
