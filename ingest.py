"""Stage 0a：历史笔记入库 → 关键段表（离线，一次性）。

历史笔记是静态的，关键段只在入库时抽一次，之后永久复用。
这是全流程唯一需要切块的环节，且必须按整句边界切。

用法：
    python ingest.py                 # 全量入库
    python ingest.py --dry-run       # 只导出 prompt，不调模型（不需要 API key）
    python ingest.py --only note_03  # 只处理某几篇
"""

from __future__ import annotations

import argparse
import json
import sys

from pipeline import config
from pipeline.llm import LLMClient, LLMError
from pipeline.prompts import load_prompt
from pipeline.textutil import chunk_by_sentence, find_offset, ngram_coverage


def read_jsonl(path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]


def merge_by(existing: list[dict], incoming: list[dict], key: str) -> list[dict]:
    """按 key 合并：incoming 覆盖同 key 的旧记录，其余保留。

    `--only` 是增量入库，不能把没处理的笔记从表里抹掉。
    """
    touched = {item[key] for item in incoming}
    return [item for item in existing if item[key] not in touched] + incoming


def load_notes() -> list[dict]:
    notes = []
    with config.NOTES_PATH.open(encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if line:
                notes.append(json.loads(line))
    return notes


def reject_duplicates(notes: list[dict]) -> tuple[list[dict], list[tuple[str, str, float]]]:
    """入库门禁：同一篇内容的两个整理版本不能同时进库。

    为什么门禁在这里而不在复用阶段：段表是**跨访谈复用**的资产，库里躺着两份
    同一场访谈的整理稿，任何一次复用判断都会重复命中同一批观点，
    并且制造"有两篇历史笔记都这么说"的错觉。这与"这篇是否与本次输入同源"
    是两个不同的问题 —— 后者不能删库（换一场访谈它就又能用了），
    只能在发 prompt 前过滤，见 run.py stage_reuse。

    保留先入库的那篇，后来的记账并拒收。
    """
    kept: list[dict] = []
    rejected: list[tuple[str, str, float]] = []
    for note in notes:
        body = note.get("正文", "")
        hit = next(
            (
                (k["note_id"], c)
                for k in kept
                if (c := ngram_coverage(body, k.get("正文", "")))
                >= config.SAME_SOURCE_COVERAGE
            ),
            None,
        )
        if hit:
            rejected.append((note["note_id"], hit[0], hit[1]))
        else:
            kept.append(note)
    return kept, rejected


def validate_segments(data: dict) -> list[str]:
    segments = data.get("segments")
    if not isinstance(segments, list):
        return ["segments 不是数组"]
    problems = []
    for i, seg in enumerate(segments):
        if not (seg.get("claim") or "").strip():
            problems.append(f"segments[{i}]：claim 为空")
        if len((seg.get("quote") or "").strip()) < 20:
            problems.append(f"segments[{i}]：quote 过短或为空")
    return problems


def ingest_note(note: dict, client: LLMClient | None, dry_run: bool) -> list[dict]:
    note_id = note["note_id"]
    body = note.get("正文", "")
    caps = config.TBD_PLACEHOLDERS
    chunks = chunk_by_sentence(body, config.INGEST_CHUNK_CHARS)

    if len(chunks) == 1:
        # 常态：整篇一次进。模型看到全貌，"挑哪 10 段"是在整篇范围内做的取舍。
        per_chunk_segs = caps["segments_per_note"]
    else:
        # 兜底：单篇超出 token 上限才会走到这里。此时上限按块摊薄，
        # 但要清楚这是**降级路径** —— 模型只能在局部做取舍，段表质量会下降。
        print(f"  ⚠️ {note_id} 超出单次上限，降级为 {len(chunks)} 块分别抽取，"
              f"模型看不到全篇，取舍质量会下降")
        per_chunk_segs = max(2, caps["segments_per_note"] // len(chunks))

    segments: list[dict] = []
    for i, chunk in enumerate(chunks, 1):
        system, user = load_prompt(
            "segment",
            note_id=note_id,
            title=note.get("标题", ""),
            date=note.get("日期", ""),
            topic=note.get("主题", ""),
            body=chunk,
            chunk_no=str(i),
            chunk_total=str(len(chunks)),
            seg_cap=str(per_chunk_segs),
        )

        if dry_run:
            path = config.INTERMEDIATE_DIR / "prompts" / f"segment_{note_id}_{i}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"[SYSTEM]\n{system}\n\n[USER]\n{user}", encoding="utf-8")
            continue

        assert client is not None
        data = client.complete_json(
            f"segment/{note_id}#{i}", system, user, validate=validate_segments
        )

        for seg in data.get("segments", []):
            quote = (seg.get("quote") or "").strip()
            offset = find_offset(quote, body)
            if offset is None:
                # quote 在原文里定位不到 = 模型改写了原文，丢弃并记账
                print(f"  ⚠️ {note_id}: 段落原文定位失败，已丢弃 —— 「{quote[:30]}…」")
                continue
            segments.append(
                {
                    "seg_id": f"{note_id}#{len(segments) + 1:02d}",
                    "note_id": note_id,
                    "offset": list(offset),
                    "claim": seg.get("claim", "").strip(),
                    "quote": quote,
                    "topic_tag": seg.get("topic_tag", ""),
                }
            )

    return segments[: caps["segments_per_note"]]


def main() -> int:
    config.setup_stdout()
    parser = argparse.ArgumentParser(description="历史笔记入库 → 关键段表")
    parser.add_argument("--dry-run", action="store_true", help="只导出 prompt，不调模型")
    parser.add_argument("--only", nargs="*", help="只处理指定 note_id")
    args = parser.parse_args()

    notes = load_notes()
    if args.only:
        notes = [n for n in notes if n["note_id"] in set(args.only)]
    if not notes:
        print("没有要处理的笔记")
        return 1

    notes, dup_rejected = reject_duplicates(notes)
    for note_id, same_as, cov in dup_rejected:
        print(f"⛔ {note_id} 与 {same_as} 覆盖率 {cov:.4f}，判为同一内容的另一整理版本，不入库")
    if not notes:
        print("去重后没有要处理的笔记")
        return 1

    config.STORE_DIR.mkdir(parents=True, exist_ok=True)

    # 篇级元数据独立存放：元数据会变（业务方可能收紧某篇的可引用范围），
    # 冗余到每个段上就得全表更新。
    metas = merge_by(
        read_jsonl(config.NOTES_META_PATH),
        [
            {**{k: v for k, v in note.items() if k != "正文"},
             "正文字数": len(note.get("正文", ""))}
            for note in notes
        ],
        "note_id",
    )
    with config.NOTES_META_PATH.open("w", encoding="utf-8") as fh:
        for meta in metas:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
    print(f"篇级元数据 → {config.NOTES_META_PATH}（表内共 {len(metas)} 篇）")

    client = None
    if not args.dry_run:
        try:
            client = LLMClient()
        except Exception as exc:  # noqa: BLE001
            print(f"❌ {exc}")
            return 2

    all_segments: list[dict] = []
    for note in notes:
        body_len = len(note.get("正文", ""))
        print(f"\n处理 {note['note_id']}（{body_len} 字）…")
        try:
            segments = ingest_note(note, client, args.dry_run)
        except LLMError as exc:
            print(f"❌ {note['note_id']} 入库失败：{exc}")
            return 3
        all_segments += segments
        if not args.dry_run:
            total = sum(len(s["quote"]) for s in segments)
            print(f"  → {len(segments)} 段，累计 {total} 字")

    if args.dry_run:
        print(f"\n[dry-run] prompt 已导出到 {config.INTERMEDIATE_DIR / 'prompts'}")
        return 0

    merged = merge_by(read_jsonl(config.SEGMENTS_PATH), all_segments, "seg_id")
    # 同一篇重跑时，旧段可能比新段多，按 note_id 整篇替换才干净
    touched_notes = {n["note_id"] for n in notes}
    merged = [
        s for s in merged
        if s["note_id"] not in touched_notes
    ] + all_segments

    with config.SEGMENTS_PATH.open("w", encoding="utf-8") as fh:
        for seg in merged:
            fh.write(json.dumps(seg, ensure_ascii=False) + "\n")

    total_chars = sum(len(s["quote"]) for s in merged)
    print(f"\n关键段表 → {config.SEGMENTS_PATH}")
    print(f"本次 {len(all_segments)} 段；表内共 {len(merged)} 段 / {total_chars} 字"
          f"（覆盖 {len({s['note_id'] for s in merged})} 篇）")
    print("\n" + client.report() if client else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
