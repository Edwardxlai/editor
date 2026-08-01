"""输出快照：改 prompt 之前存一份，改完再跑一次，只报「哪里变了」。

    python scripts/snapshot.py --save    # 把当前 outputs/_intermediate 存为基准
    python scripts/snapshot.py           # 与基准比对，打印结构性差异

**为什么需要它。** 现有两道检查都盯不住 LLM 那一层：

- `selftest.py` 验的是确定性层（SRT 解析、原话定位、字数口径），
  改 prompt 它一项都不会变红。
- `check.py` 验的是「这一版合不合规」（引用是否字面命中、角度是否 3 个、
  note_02 是否被拦住），它不问「要点是不是从 16 条掉到了 7 条」。

也就是说：把抽取 prompt 的「8–16 条」改成「6–10 条」，两道检查全绿，
而 P16 可能已经消失、outline 少了一节、初稿塌了一段 —— 没有任何东西会提醒你。

**只比结构性计数，不比措辞。** `temperature=0.3`，什么都不改原样重跑，
措辞也会飘。措辞变了是正常的，编号集合少了四个才是信号。

**它不判好坏。** 只打印「16 → 7」这种事实，要不要接受由人决定 ——
和整个工具的分工原则一致：程序报事实，判断留给人。

受众是改 prompt 的人，不是编辑，所以基准文件放 `store/` 不放 `outputs/`。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config                       # noqa: E402
from pipeline.textutil import count_draft_chars   # noqa: E402

SNAPSHOT_PATH = config.STORE_DIR / "output_snapshot.json"


def _load(name: str) -> dict:
    path = config.INTERMEDIATE_DIR / f"{name}.json"
    if not path.exists():
        raise SystemExit(f"缺少中间产物 {path}。先跑 python run.py（或 --cache）。")
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint() -> dict:
    """把六个交付物压成一组可比的数 —— 每一项都是「变了就该看一眼」的量。"""
    ev = _load("evidence")
    angles = _load("angles")
    outline = _load("outline")
    reuse = _load("reuse")
    draft = _load("draft")

    points = ev.get("points", [])
    # 引用总数 = 每条要点的 claim 一条 + support 若干。它掉得比要点数快，
    # 说明模型开始只给判断不给原话 —— 这正是本工具最不该出现的退化。
    quotes = sum(1 + len(p.get("support") or []) for p in points)

    outline_pids, sections = set(), outline.get("sections", [])
    for sec in sections:
        for sub in sec.get("subs") or []:
            for kp in sub.get("points") or []:
                outline_pids.add(kp.get("from", ""))

    return {
        "evidence.要点数": len(points),
        "evidence.要点编号": sorted(p["point_id"] for p in points),
        "evidence.类型分布": dict(Counter(p.get("type", "?") for p in points)),
        "evidence.核实状态分布": dict(Counter(p.get("verification", "?") for p in points)),
        "evidence.带限定语的要点": sorted(p["point_id"] for p in points if p.get("qualifier")),
        "evidence.引用总数": quotes,
        "evidence.被剔除条数": len(ev.get("_dropped") or []),
        "checks.说话人存疑": len(ev.get("speaker_doubts") or []),
        "checks.口播段落": len(ev.get("promo_segments") or []),
        "checks.缺失信息": len(ev.get("missing_info") or []),
        "checks.术语不统一": len(ev.get("terminology") or []),
        "checks.脱敏遗漏": len(ev.get("desensitization_gaps") or []),
        "angles.角度数": len(angles.get("angles") or []),
        "angles.引用的要点": sorted({p for a in angles.get("angles") or []
                                     for p in (a.get("based_on") or [])}),
        "angles.砍掉的候选": len(angles.get("rejected") or []),
        "outline.章节数": len(sections),
        "outline.进结构的要点": sorted(outline_pids),
        "outline.未进结构的要点": sorted(set(p["point_id"] for p in points) - outline_pids),
        "reuse.建议条数": len(reuse.get("suggestions") or []),
        "reuse.涉及笔记": sorted(s.get("note_id", "?") for s in reuse.get("suggestions") or []),
        "reuse.结论分布": dict(Counter(s.get("verdict", "?") for s in reuse.get("suggestions") or [])),
        "reuse.同源排除": sorted(x.get("note_id", "?") for x in reuse.get("excluded_same_source") or []),
        "draft.正文字数": count_draft_chars(draft.get("body_markdown", "")),
        "draft.内联标记数": len(re.findall(r"\[P\d+\s", draft.get("body_markdown", ""))),
        "draft.引用的要点": sorted(draft.get("used_points") or []),
    }


def _diff_line(key: str, old, new) -> str | None:
    if old == new:
        return None
    if isinstance(new, list):
        gone = [x for x in old if x not in new]
        added = [x for x in new if x not in old]
        parts = [f"-{x}" for x in gone] + [f"+{x}" for x in added]
        return f"  {key:<26} {len(old)} → {len(new)}　{' '.join(parts)}"
    if isinstance(new, dict):
        keys = sorted(set(old) | set(new))
        bits = [f"{k} {old.get(k, 0)}→{new.get(k, 0)}" for k in keys if old.get(k) != new.get(k)]
        return f"  {key:<26} {'　'.join(bits)}"
    return f"  {key:<26} {old} → {new}"


def compare(base: dict, now: dict) -> list[str]:
    keys = list(dict.fromkeys(list(base) + list(now)))
    lines = []
    for k in keys:
        if k not in base:
            lines.append(f"  {k:<26} （新增指标）{now[k]}")
        elif k not in now:
            lines.append(f"  {k:<26} （指标消失）")
        elif (line := _diff_line(k, base[k], now[k])):
            lines.append(line)
    return lines


def main() -> int:
    config.setup_stdout()
    ap = argparse.ArgumentParser(description="输出快照比对（只报变化，不判好坏）")
    ap.add_argument("--save", action="store_true", help="把当前输出存为基准")
    args = ap.parse_args()

    now = fingerprint()

    if args.save:
        SNAPSHOT_PATH.write_text(
            json.dumps(now, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"已存基准：{SNAPSHOT_PATH}")
        print(f"  要点 {now['evidence.要点数']} 条 / 引用 {now['evidence.引用总数']} 处 / "
              f"初稿 {now['draft.正文字数']} 字")
        return 0

    if not SNAPSHOT_PATH.exists():
        print(f"还没有基准。先跑：python scripts/snapshot.py --save")
        return 1

    base = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    lines = compare(base, now)

    print("=" * 60)
    if not lines:
        print("与基准无结构性差异（措辞可能仍有变化，本脚本不比措辞）")
        print("=" * 60)
        return 0

    print(f"与基准有 {len(lines)} 处结构性差异：")
    print("-" * 60)
    print("\n".join(lines))
    print("-" * 60)
    print("这些只是事实，不是判断。要接受就重新 --save，不接受就回退改动。")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
