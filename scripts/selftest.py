"""确定性层自检 —— 不调模型，不需要 API key。

这一层是整个"可溯源"承诺的地基：SRT 解析、原话定位、字数口径、
自动修正。它必须是确定性的，所以它必须能被离线验证。

用法：python scripts/selftest.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config
from pipeline.docx_read import read_docx
from pipeline.srt import parse_srt
from pipeline.textutil import (
    chunk_by_sentence,
    count_draft_chars,
    find_offset,
    locate_quote,
    parse_ts,
    split_sentences,
)
from pipeline.validate import repair_quote_ref

results: list[tuple[bool, str, str]] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    results.append((bool(ok), name, detail))


def main() -> int:
    config.setup_stdout()
    t = parse_srt(config.SRT_PATH)

    # --- SRT 解析 -------------------------------------------------------
    check(len(t) == 97, "SRT 解析出 97 条字幕", f"实际 {len(t)}")
    check(t.total_chars == 12677, "正文 12,677 字", f"实际 {t.total_chars}")
    check(abs(t.duration_minutes - 49.1) < 0.1, "时长 49.1 分钟",
          f"实际 {t.duration_minutes:.1f}")
    check(t.speakers == ["主持人", "嘉宾A", "嘉宾B"], "识别出 3 位说话人",
          "、".join(t.speakers))

    # --- 转录异常检测（确定性） -----------------------------------------
    kinds = {}
    for issue in t.issues:
        kinds.setdefault(issue.kind, []).append(tuple(issue.cues))
    check(sorted(kinds.get("时间戳重叠", [])) == [(12, 13), (80, 81), (82, 83)],
          "时间戳重叠：12/13、80/81、82/83", str(kinds.get("时间戳重叠")))
    short = sorted(c[0] for c in kinds.get("超短片段", []))
    check(short == [12, 24, 80, 82, 84, 86],
          "超短片段：12、24、80、82、84、86", str(short))
    check((4, 11) in kinds.get("内容近似重复", []),
          "检出字幕 4 与 11 近似重复（片头预告 vs 正片）",
          str(kinds.get("内容近似重复")))

    # --- 原话定位：这是可溯源承诺的唯一验证点 ---------------------------
    c6, c11, c36, c57 = (t.get(i).text for i in (6, 11, 36, 57))
    quote_cases = [
        ("这个判断听起来很抓人，但我还没有看到明确证据", c6, True, "字幕6 限定语"),
        ("也有人说，未来三年里……但我还没有看到明确证据", c6, True, "省略号跨段引用"),
        ("但我还没有看到明确证据，也有人说", c6, False, "片段顺序颠倒应判失败"),
        ("这里我说的不是某个确定行业马上会发生什么", c36, True, "字幕36 限定语"),
        # 弯引号 vs 直角引号 vs 全角引号：标点差异不构成"改写原话"
        ("我认为‘企业家’这个词很大程度上就是‘能动性’的同义词",
         c57, True, "单弯引号 ‘’"),
        ("我认为“企业家”这个词很大程度上就是“能动性”的同义词",
         c57, True, "双弯引号 “”"),
        ("我认为「企业家」这个词很大程度上就是「能动性」的同义词", c57, True, "直角引号「」"),
        ("一半人说‘这简直像神一样，将会拯救世界’", c11, True, "字幕11 引号内嵌"),
        ("我编的一句原文里完全不存在的话", c57, False, "杜撰应被抓住"),
    ]
    for quote, hay, expected, label in quote_cases:
        check(locate_quote(quote, hay) == expected, f"原话定位：{label}")

    # --- 自动修正：模型标错字幕序号时，代码按原话反查覆盖 ---------------
    ref = {"quote": "最大的障碍不是技术", "cue": 99, "ts": "00:00:00", "speaker": "主持人"}
    problems = repair_quote_ref(ref, t, "selftest")
    check(not problems and ref["cue"] == 44 and ref["speaker"] == "嘉宾B",
          "模型标错字幕序号时按原话反查更正",
          f"cue={ref['cue']} speaker={ref['speaker']} ts={ref['ts']}")

    fake = {"quote": "这句话逐字稿里没有出现过", "cue": 1}
    check(bool(repair_quote_ref(fake, t, "selftest")),
          "杜撰的引用无法被修正，会被报为问题")

    # --- 字数口径 -------------------------------------------------------
    # 口径：剔除内联证据标记与 Markdown 语法符号，但**保留小标题文字**
    #（小标题是正文的一部分）
    # ⚠️ 两种标记写法都必须剔干净：漏掉一种，标记文本就会被算进字数。
    body = ("## 小标题\n" + "字" * 600
            + " [P01 字幕11 00:02:09 嘉宾B] [字幕38 13:32 嘉宾B]\n")
    check(count_draft_chars(body) == 603, "字数口径剔除两种写法的证据标记、保留标题文字",
          f"实际 {count_draft_chars(body)}，预期 600 正文 + 3 标题字")

    # --- 同源判定是可计算量，不问模型 -----------------------------------
    from pipeline.textutil import ngram_coverage
    transcript_text = "\n".join(c.text for c in t.cues)
    notes = [json.loads(line) for line in
             config.NOTES_PATH.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    cov = {n["note_id"]: ngram_coverage(n.get("正文", ""), transcript_text) for n in notes}
    same = [k for k, v in cov.items() if v >= config.SAME_SOURCE_COVERAGE]
    others = max(v for k, v in cov.items() if k not in same)
    check(same == ["note_02"] and others < 0.01,
          "同源笔记由字符覆盖率分出，无需模型判断",
          f"note_02={cov['note_02']:.4f}，其余最高 {others:.4f}")

    # --- 整句切分 -------------------------------------------------------
    sents = split_sentences("第一句。第二句！第三句？第四句")
    check(len(sents) == 4, "按标点切出整句", str(sents))
    chunks = chunk_by_sentence(c36, 120)
    check(all(c.rstrip()[-1] in "。！？；\n" or c is chunks[-1] for c in chunks),
          "分块落在整句边界上", str([len(c) for c in chunks]))

    check(parse_ts("13:32") == 812 and parse_ts("00:13:32") == 812,
          "时间戳解析兼容 mm:ss 与 hh:mm:ss")

    # --- 偏移量由代码算出 ------------------------------------------------
    offset = find_offset("这里我说的不是某个确定行业", c36)
    check(offset is not None and c36[offset[0]:offset[1]] == "这里我说的不是某个确定行业",
          "字符偏移量由代码定位，可反查校验", str(offset))

    # --- docx 兜底 ------------------------------------------------------
    for path in sorted(config.SAMPLES_DIR.glob("*.docx")):
        paragraphs, method = read_docx(path)
        check(len(paragraphs) > 0, f"{path.name} 可读取", method)

    # --- 输出 -----------------------------------------------------------
    width = max(len(name) for _, name, _ in results) + 2
    for ok, name, detail in results:
        print(f"{'✅' if ok else '❌'} {name.ljust(width)}{detail}")

    failed = sum(1 for ok, _, _ in results if not ok)
    print("-" * 60)
    if failed:
        print(f"{failed}/{len(results)} 项未通过")
        return 1
    print(f"全部通过（{len(results)} 项，未调用任何模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
