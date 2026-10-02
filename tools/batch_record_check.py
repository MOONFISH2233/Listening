#!/usr/bin/env python3
"""批量录音体检 —— 对目录下所有录音跑 record_check，汇总成一张表。

用法:
    python batch_record_check.py 录音目录
    python batch_record_check.py 录音目录 --json
    python batch_record_check.py 录音目录 --ext .m4a .wav

退出码: 0 = 全部通过, 1 = 有不合格或错误。
"""

import argparse
import json
import sys
from pathlib import Path

# 同目录导入，避免装包
sys.path.insert(0, str(Path(__file__).resolve().parent))
from record_check import analyze, print_report  # noqa: E402

DEFAULT_EXTS = {".m4a", ".wav", ".mp3", ".flac", ".aac", ".ogg"}


def collect(root: Path, exts: set[str]) -> list[Path]:
    files = [p for p in sorted(root.rglob("*")) if p.suffix.lower() in exts and p.is_file()]
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description="批量录音体检")
    ap.add_argument("dir", help="录音目录（递归）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--ext", nargs="+", default=None,
                    help=f"扩展名过滤，默认 {' '.join(sorted(DEFAULT_EXTS))}")
    args = ap.parse_args()

    root = Path(args.dir)
    if not root.is_dir():
        print(f"[错误] 不是目录: {root}", file=sys.stderr)
        return 1

    exts = {e.lower() if e.startswith(".") else "." + e.lower()
            for e in (args.ext or DEFAULT_EXTS)}
    files = collect(root, exts)
    if not files:
        print(f"[空] {root} 下没找到录音（扩展名: {' '.join(sorted(exts))}）", file=sys.stderr)
        return 1

    results = []
    failed = False
    for p in files:
        try:
            r = analyze(p)
        except Exception as e:
            print(f"[错误] {p}: {e}", file=sys.stderr)
            failed = True
            continue
        results.append(r)
        if not args.json:
            print_report(r)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        n_pass = sum(1 for r in results if r["pass"])
        print(f"\n合计 {len(results)} 条，通过 {n_pass} 条，不合格 {len(results) - n_pass} 条")
        if results:
            worst = min(results, key=lambda r: r["snr_db"])
            print(f"信噪比最低: {worst['file']}  ({worst['snr_db']} dB)")

    if failed or any(not r["pass"] for r in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
