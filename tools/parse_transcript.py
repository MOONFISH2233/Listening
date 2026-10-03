#!/usr/bin/env python3
"""转写稿解析器 —— 把手机/通义的 txt 按统一口径拆成「摘要」和「逐字稿」。

为什么需要它：
    之前比较各段录音的转写质量时，我给每份文件手写了不同的正则去过滤
    「摘要行」，算出一组字/分钟。结果这组数字跟 SNR 毫无次序关系——
    因为每份文件的过滤条件不一样，比的是过滤规则，不是录音质量。

    这个脚本把口径固定下来：同一套规则处理所有文件。

两类内容怎么区分：
    这两类 txt 都混着两种东西，但混法不一样：

    - AI 纪要：形如「00:02:02 时间常数辨识方法」的章节标题，
      以及「【智能概览】」「议题一」这类小节标记
    - 逐字稿：老师的口语原文，有「对不对」「是吧」，句子长且断得乱

    通义的稿子通常是「开头一段纪要 + 后面整篇逐字稿」，
    手机的稿子则可能只有纪要（如 20260918.txt）。

用法：
    python parse_transcript.py <txt> [<txt> ...]
    python parse_transcript.py <txt> --json
    python parse_transcript.py <txt> --show-verbatim   # 打印逐字稿开头
"""

import argparse
import json
import re
import sys
from pathlib import Path

# ---- 判定规则（对所有文件一视同仁）----

# 章节标题：「00:02:02 时间常数辨识方法」
RE_CHAPTER = re.compile(r"^\d{2}:\d{2}:\d{2}\s+\S")

# 小节标记：「【智能概览】」
RE_SECTION = re.compile(r"^【.+】$")

# 纪要内部的条目：「议题一」「· 推导…」「✓ …」「⚠ …」
RE_ITEM = re.compile(r"^(议题[一二三四五六七八九十]+|解读|主题|参会人|时长|责任人|截止|[·✓⚠•‣▪])")

# 说话人标记：「说话人2 02:27:04」
RE_SPEAKER = re.compile(r"^说话人\d+\s+\d{2}:\d{2}:\d{2}")

# 带方括号的要点行：「[00:01:27] 课程设计目标…」
RE_BRACKET = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]")

# markdown 标题：「##工程热力学」
RE_MDHEAD = re.compile(r"^#{1,6}\s*\S")

# ---- 逐字稿的判据 ----
# 不看行首标记，看「像不像人说的话」。
# 口语的特征：句子长、有连接词、有语气词。
SPOKEN_MARKERS = ("对不对", "是不是", "是吧", "对吧", "我们", "这个", "就是",
                  "那么", "所以说", "大家", "你看", "明白", "知道", "有没有")

MIN_SPOKEN_LEN = 30   # 短于这个长度不足以判断是口语


def classify_line(line: str) -> str:
    """返回 'meta'（纪要/标记）或 'verbatim'（逐字稿）。"""
    s = line.strip()
    if not s:
        return "meta"
    if RE_CHAPTER.match(s) or RE_SECTION.match(s) or RE_ITEM.match(s):
        return "meta"
    if RE_SPEAKER.match(s) or RE_BRACKET.match(s) or RE_MDHEAD.match(s):
        return "meta"

    # 长的自然语句 -> 逐字稿
    if len(s) >= MIN_SPOKEN_LEN:
        return "verbatim"

    # 短的：靠口语特征判断
    if any(m in s for m in SPOKEN_MARKERS) and len(s) >= 12:
        return "verbatim"

    return "meta"


def parse(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    verbatim: list[str] = []
    meta: list[str] = []

    for ln in lines:
        (verbatim if classify_line(ln) == "verbatim" else meta).append(ln.strip())

    vtext = "".join(verbatim)
    return {
        "file": path.name,
        "lines_total": len(lines),
        "chars_total": sum(len(l) for l in lines),
        "verbatim_lines": len([l for l in verbatim if l]),
        "verbatim_chars": len(vtext),
        "meta_lines": len([l for l in meta if l]),
        # 逐字稿占比：这才是能跨文件比的量
        "verbatim_ratio": round(len(vtext) / max(sum(len(l) for l in lines), 1), 3),
        "verbatim_head": vtext[:120],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="转写稿解析器：拆出摘要与逐字稿")
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--show-verbatim", action="store_true", help="打印逐字稿开头")
    args = ap.parse_args()

    results = []
    for p in args.files:
        if not p.exists():
            print(f"找不到文件: {p}", file=sys.stderr)
            return 1
        results.append(parse(p))

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    print(f"{'文件':<26} {'总字符':>8} {'逐字稿':>8} {'占比':>7} {'逐字稿行':>9}")
    print("-" * 66)
    for r in results:
        print(f"{r['file']:<26} {r['chars_total']:>8} {r['verbatim_chars']:>8} "
              f"{r['verbatim_ratio']:>7.1%} {r['verbatim_lines']:>9}")

    if args.show_verbatim:
        print()
        for r in results:
            print(f"##### {r['file']}")
            print(f"  {r['verbatim_head']}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
