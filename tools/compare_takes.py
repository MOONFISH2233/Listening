#!/usr/bin/env python3
"""采集对照实验：同一节课用不同摆法录几段，哪种最好？

COLLECTION.md 里的建议（手机别放兜里、坐前三排、关空调）全是推断，
没有实测数据。这个脚本用来把推断变成结论：

    同一节课，用不同摆法录几段，文件名标明摆法，然后：

        python compare_takes.py 录音目录

它会对每段跑体检，输出横向对比表，并明确指出哪个指标最差、
哪种摆法整体最好。

文件名约定：摆法写在文件名里，用 -- 或 _ 分隔都行，脚本只按名字分组，
不解析语义。建议这样命名：

    take1_兜里.m4a
    take2_桌上朝讲台.m4a
    take3_前三排桌上.m4a

用法:
    python compare_takes.py 录音目录
    python compare_takes.py 录音目录 --json
    python compare_takes.py 录音目录 --baseline take1_兜里

带上 --baseline 会额外输出「其它摆法相对基准改善了多少 dB」——
这才是对照实验真正要的答案：哪条建议真的有用，有用多少。
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from record_check import THRESHOLDS, analyze  # noqa: E402

DEFAULT_EXTS = {".m4a", ".wav", ".mp3", ".flac", ".aac", ".ogg"}

# 指标 -> (中文名, 合格线, 越大越好?)
METRICS = {
    "snr_db": ("信噪比 SNR", THRESHOLDS["snr_db"], True),
    "hf_rolloff_db": ("高频衰减", THRESHOLDS["hf_rolloff_db"], True),
    "dynamic_range_db": ("动态范围", THRESHOLDS["dynamic_range_db"], True),
    "clip_pct": ("削波", THRESHOLDS["clip_pct"], False),
}


def collect(root: Path, exts: set[str]) -> list[Path]:
    return [p for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() in exts]


def mark(value: float, limit: float, higher_better: bool) -> str:
    ok = value > limit if higher_better else value < limit
    return "合格" if ok else "不合格"


def print_table(results: list[dict]) -> None:
    """横向对比表。列宽按最长文件名自适应。"""
    name_w = max(len(Path(r["file"]).stem) for r in results)
    name_w = max(name_w, 12)

    header = f"{'摆法':<{name_w}}  {'SNR':>8}  {'高频':>8}  {'动态':>8}  {'削波':>8}  判定"
    print()
    print(header)
    print("-" * len(header))

    for r in results:
        stem = Path(r["file"]).stem
        verdict = "通过" if r["pass"] else "不通过"
        print(f"{stem:<{name_w}}  {r['snr_db']:>8.1f}  {r['hf_rolloff_db']:>8.1f}  "
              f"{r['dynamic_range_db']:>8.1f}  {r['clip_pct']:>8.4f}  {verdict}")

    print()
    print(f"合格线：SNR > {THRESHOLDS['snr_db']:.0f} dB | "
          f"高频 > {THRESHOLDS['hf_rolloff_db']:.0f} dB | "
          f"动态 > {THRESHOLDS['dynamic_range_db']:.0f} dB | "
          f"削波 < {THRESHOLDS['clip_pct']}%")


def print_analysis(results: list[dict]) -> None:
    """指出哪个指标最拖后腿、哪种摆法最好。"""
    if len(results) < 2:
        return

    print()
    print("=" * 60)
    print("分析")
    print("=" * 60)

    # 每种摆法差在哪
    for r in results:
        stem = Path(r["file"]).stem
        bad = []
        for key, (label, limit, higher) in METRICS.items():
            v = r[key]
            ok = v > limit if higher else v < limit
            if not ok:
                bad.append(label)
        if bad:
            print(f"  {stem}：差 {', '.join(bad)}")
        else:
            print(f"  {stem}：四项全合格")

    # 每个指标谁最好
    print()
    print("各指标最好的摆法：")
    for key, (label, _, higher) in METRICS.items():
        best = max(results, key=lambda r: r[key]) if higher \
            else min(results, key=lambda r: r[key])
        print(f"  {label:<12} {Path(best['file']).stem}  ({best[key]:.2f})")

    # 整体最好：通过项数最多，其次 SNR 最高
    def score(r):
        n_pass = sum(
            1 for k, (_, limit, higher) in METRICS.items()
            if (r[k] > limit if higher else r[k] < limit)
        )
        return (n_pass, r["snr_db"])

    overall = max(results, key=score)
    n_pass, _ = score(overall)
    print()
    print(f"整体最好：{Path(overall['file']).stem}（{n_pass}/4 项合格）")
    if n_pass < 4:
        print("没有一种摆法全合格 —— 换更靠前的位置，或换录音设备。")


def print_baseline(results: list[dict], baseline_name: str) -> None:
    """相对基准的改善量。这才是对照实验要的答案。"""
    base = None
    for r in results:
        if Path(r["file"]).stem == baseline_name or r["file"] == baseline_name:
            base = r
            break
    if base is None:
        print(f"\n[警告] 找不到基准 {baseline_name}，跳过对比。", file=sys.stderr)
        print(f"       可用的：{', '.join(Path(r['file']).stem for r in results)}",
              file=sys.stderr)
        return

    print()
    print("=" * 60)
    print(f"相对基准「{baseline_name}」的改善")
    print("=" * 60)
    print(f"{'摆法':<24}  {'SNR':>10}  {'高频':>10}  {'动态':>10}")
    print("-" * 60)
    for r in results:
        stem = Path(r["file"]).stem
        if r is base:
            print(f"{stem:<24}  {'(基准)':>10}  {'':>10}  {'':>10}")
            continue
        d_snr = r["snr_db"] - base["snr_db"]
        d_hf = r["hf_rolloff_db"] - base["hf_rolloff_db"]
        d_dyn = r["dynamic_range_db"] - base["dynamic_range_db"]
        print(f"{stem:<24}  {d_snr:>+10.1f}  {d_hf:>+10.1f}  {d_dyn:>+10.1f}")
    print()
    print("正数 = 比基准好。看哪一列的正数最大，就知道哪条建议最值钱。")


def main() -> int:
    ap = argparse.ArgumentParser(description="采集对照实验：哪种摆法录得最好")
    ap.add_argument("dir", help="录音目录（递归）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--baseline", help="基准摆法的文件名（不含扩展名）")
    ap.add_argument("--ext", nargs="+", default=None, help="扩展名过滤")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"[错误] 不是目录: {root}", file=sys.stderr)
        return 1

    exts = {e.lower() if e.startswith(".") else "." + e.lower()
            for e in (args.ext or DEFAULT_EXTS)}
    files = collect(root, exts)
    if not files:
        print(f"[空] {root} 下没找到录音", file=sys.stderr)
        return 1
    if len(files) < 2:
        print(f"[警告] 只找到 1 段录音，对照实验需要至少 2 段。", file=sys.stderr)

    results = []
    for p in files:
        try:
            results.append(analyze(p))
        except Exception as e:
            print(f"[错误] {p}: {e}", file=sys.stderr)

    if not results:
        return 1

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        print_table(results)
        print_analysis(results)
        if args.baseline:
            print_baseline(results, args.baseline)

    n_pass = sum(1 for r in results if r["pass"])
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
