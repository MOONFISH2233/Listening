#!/usr/bin/env python3
"""把 spike-asr 的模型拷进 android 的 assets，供打包进 APK。

为什么需要这个脚本：sherpa-onnx 的 native 层只认文件路径，不读 assets 流，
所以 AsrEngine.kt 里做的是「首次启动把 assets 解压到 filesDir」。这意味着
模型必须先躺在 assets 里，APK 才带得动它。

模型总共约 189 MB（encoder 173.5 + decoder 12.5 + joiner 3.1），
**不进 git**——所以这个脚本要在每次全新 clone 之后跑一次。
.gitignore 里已经排除了 android/app/src/main/assets/models/。

用法：
    python tools/sync_models_to_android.py          # 拷贝
    python tools/sync_models_to_android.py --check  # 只检查，不拷贝（CI 用）

退出码：0 成功；1 缺文件或大小不对。
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC_DIR = REPO / "spike-asr" / "models"
DST_DIR = REPO / "android" / "app" / "src" / "main" / "assets" / "models"

# 这四个名字必须和 AsrEngine.kt 里的常量一字不差。
# 改这里就要同步改那边，否则运行时找不到文件（会抛 FileNotFoundException）。
FILES = [
    "encoder-epoch-99-avg-1.int8.onnx",
    "decoder-epoch-99-avg-1.int8.onnx",
    "joiner-epoch-99-avg-1.int8.onnx",
    "tokens.txt",
]

# 期望大小（字节）。用来判断拷贝是否完整——中断的拷贝会留下小文件，
# 那种文件在手机上加载会直接 segfault，比缺文件更难查。
# 期望大小（字节），2026-10-04 用 ls 实测的真实值。
# 用来判断拷贝是否完整——中断的拷贝会留下小文件，
# 那种文件在手机上加载会直接崩，比缺文件更难查。
EXPECTED_BYTES = {
    "encoder-epoch-99-avg-1.int8.onnx": 181_895_032,   # 173.5 MB
    "decoder-epoch-99-avg-1.int8.onnx": 13_091_040,    # 12.5 MB
    "joiner-epoch-99-avg-1.int8.onnx": 3_228_404,      # 3.1 MB
    "tokens.txt": 56_317,                              # 55 KB
}

# 大小允许的偏差。不同来源的导出可能有微小差异，但不该差太多。
TOLERANCE = 0.02


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def check_src() -> list[str]:
    """返回问题列表，空列表表示源文件都正常。"""
    problems: list[str] = []

    if not SRC_DIR.exists():
        problems.append(f"源目录不存在：{SRC_DIR}")
        return problems

    for name in FILES:
        f = SRC_DIR / name
        if not f.exists():
            problems.append(f"缺文件：{f}")
            continue
        size = f.stat().st_size
        expect = EXPECTED_BYTES.get(name)
        if expect is not None:
            lo, hi = expect * (1 - TOLERANCE), expect * (1 + TOLERANCE)
            if not (lo <= size <= hi):
                problems.append(
                    f"{name} 大小异常：{human(size)}，期望约 {human(expect)}"
                )

    return problems


def copy(dry_run: bool = False) -> int:
    problems = check_src()
    if problems:
        print("源模型有问题，先解决这些：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "\n模型不在 git 里，需要先拿到它们。"
            "见 spike-asr/README.md 里下载模型那一段。",
            file=sys.stderr,
        )
        return 1

    DST_DIR.mkdir(parents=True, exist_ok=True)

    total = 0
    for name in FILES:
        src = SRC_DIR / name
        dst = DST_DIR / name
        size = src.stat().st_size
        total += size

        if dst.exists() and dst.stat().st_size == size:
            print(f"  跳过 {name}（已是最新）")
            continue

        if dry_run:
            print(f"  将拷贝 {name}  {human(size)}")
            continue

        print(f"  拷贝 {name}  {human(size)} ...", end="", flush=True)
        # copy2 保留时间戳；这里其实无所谓，但用它能顺带把权限带过去。
        shutil.copy2(src, dst)

        # 拷完立刻验大小：中断的拷贝留下的半截文件最难查。
        if dst.stat().st_size != size:
            print(f"\n拷贝不完整：{dst} 只有 {human(dst.stat().st_size)}")
            return 1
        print(" 完成")

    print(f"\n目标目录：{DST_DIR}")
    print(f"总计：{human(total)}")
    if not dry_run:
        print("\n模型已在 assets 里，可以打包了：")
        print("  cd android && ./gradlew assembleDebug")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="只检查源文件和目标目录，不实际拷贝",
    )
    args = ap.parse_args()

    if args.check:
        problems = check_src()
        if problems:
            for p in problems:
                print(f"  - {p}")
            return 1
        missing_dst = [n for n in FILES if not (DST_DIR / n).exists()]
        if missing_dst:
            print(f"目标目录缺 {len(missing_dst)} 个文件，需要跑一次拷贝（不带 --check）")
            return 1
        print("源和目标都齐了")
        return 0

    return copy()


if __name__ == "__main__":
    sys.exit(main())
