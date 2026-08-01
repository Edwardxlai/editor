"""验收清单（PRD §3.7）：跑什么、生成什么、什么算通过。

第 1–8 项是通用逻辑，换一份素材依然成立。
第 9–12 项是**针对本材料包的回归测试** —— 它们检的不是"程序有没有跑完"，
而是"这个工具在这份数据上有没有真正看见问题"。

用法：python check.py
退出码 0 = 全部通过。
"""

from __future__ import annotations

import json
import re
import sys

from pipeline import config
from pipeline.srt import OPENING_HOOK_CUES, parse_srt
from pipeline.textutil import count_draft_chars, locate_quote, parse_ts
from pipeline.validate import MARKER_PATTERN, _iter_refs

PASS, FAIL, SKIP = "✅", "❌", "⏭️"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, ok: bool | None, name: str, detail: str = "") -> None:
        icon = SKIP if ok is None else (PASS if ok else FAIL)
        self.rows.append((icon, name, detail))

    @property
    def failed(self) -> int:
        return sum(1 for icon, _, _ in self.rows if icon == FAIL)

    def render(self) -> str:
        width = max(len(name) for _, name, _ in self.rows) + 2
        lines = []
        for icon, name, detail in self.rows:
            lines.append(f"{icon} {name.ljust(width)}{detail}")
        return "\n".join(lines)


def load_json(name: str) -> dict | None:
    path = config.INTERMEDIATE_DIR / f"{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    config.setup_stdout()
    report = Report()
    transcript = parse_srt(config.SRT_PATH)
    n_cues = len(transcript)

    # --- 1. 六个文件 ------------------------------------------------------
    missing = [
        name for name in config.OUTPUT_FILES
        if not (config.OUT_DIR / name).exists()
        or (config.OUT_DIR / name).stat().st_size == 0
    ]
    report.add(not missing, "1. 六个交付文件存在且非空",
               f"缺失：{missing}" if missing else f"{len(config.OUTPUT_FILES)}/6")

    evidence = load_json("evidence")
    reuse = load_json("reuse")
    angles = load_json("angles")
    draft = load_json("draft")

    if evidence is None:
        report.add(False, "2–4、12. 证据类检查", "缺少中间产物 evidence.json，先跑 run.py")
    else:
        points = evidence.get("points", [])

        # --- 2. 引用字面命中 ---------------------------------------------
        total = miss = 0
        misses: list[str] = []
        for point in points:
            for ref, label in _iter_refs(point):
                total += 1
                cue = transcript.get(ref.get("cue"))
                if cue is None or not locate_quote(ref.get("quote", ""), cue.text):
                    miss += 1
                    misses.append(f"{point['point_id']}.{label}")
        report.add(
            miss == 0 and total > 0,
            "2. 所有引用在逐字稿中字面命中",
            f"{total - miss}/{total}" + (f"，未命中：{misses[:4]}" if misses else ""),
        )

        # --- 3. 位置合法 --------------------------------------------------
        bad_pos: list[str] = []
        for point in points:
            for ref, label in _iter_refs(point):
                cue = transcript.get(ref.get("cue"))
                seconds = parse_ts(ref.get("ts", ""))
                if cue is None or not (1 <= ref.get("cue", 0) <= n_cues):
                    bad_pos.append(f"{point['point_id']}.{label} cue")
                elif seconds is None or not cue.contains_ts(seconds, tolerance=1.0):
                    bad_pos.append(f"{point['point_id']}.{label} ts")
        report.add(not bad_pos, f"3. 字幕序号 ∈ [1,{n_cues}] 且时间戳落在区间内",
                   f"异常：{bad_pos[:4]}" if bad_pos else "全部合法")

        # --- 4. 不引片头 --------------------------------------------------
        in_hook = [
            f"{p['point_id']}.{label}"
            for p in points
            for ref, label in _iter_refs(p)
            if ref.get("cue") in OPENING_HOOK_CUES
        ]
        report.add(not in_hook,
                   f"4. 无证据指向片头（字幕 {min(OPENING_HOOK_CUES)}–{max(OPENING_HOOK_CUES)}）",
                   f"违规：{in_hook[:4]}" if in_hook else "0 例外")

        # --- 12. 空支撑显式化，且事实类要打红灯 -----------------------------
        #
        # 空栏必须显式写「无」，这是给编辑的信号。但空栏对两类要点意味着不同的事：
        # 事实类空 = 这个数字没出处（红灯）；观点类空 = 他没展开理由（正常）。
        # 把两者渲染成同一个样子，等于把这个工具最该做的区分抹掉。
        text = (config.OUT_DIR / "evidence.md").read_text(encoding="utf-8") \
            if (config.OUT_DIR / "evidence.md").exists() else ""
        bare = [p for p in points if not (p.get("support") or [])]
        bare_facts = [p for p in bare if p.get("type") == "事实"]
        explicit = text.count("| — | **无** | — | — | — |")
        red = text.count("🔴 **这是一条事实性说法")
        report.add(
            explicit == len(bare) and red == len(bare_facts),
            "12. 空支撑显式写「无」，事实类另打红灯",
            f"空 {len(bare)} 条/标注 {explicit} 处；"
            f"其中事实类 {len(bare_facts)} 条/红灯 {red} 处",
        )

    # --- 5. 角度恰好 3 个 -------------------------------------------------
    if angles is None:
        report.add(False, f"5. 角度恰好 {config.ANGLE_COUNT} 个", "缺少 angles.json")
    else:
        count = len(angles.get("angles", []))
        report.add(count == config.ANGLE_COUNT,
                   f"5. 角度恰好 {config.ANGLE_COUNT} 个", f"实际 {count} 个")

    # --- 6. 复用建议 ≥1 ---------------------------------------------------
    if reuse is None:
        report.add(False, "6. 复用建议 ≥1 条或说明为何不复用", "缺少 reuse.json")
    else:
        n = len(reuse.get("suggestions", []))
        has_reason = bool((reuse.get("no_match_reason") or "").strip())
        report.add(n >= config.REUSE_MIN_COUNT or has_reason,
                   "6. 复用建议 ≥1 条或说明为何不复用",
                   f"{n} 条建议" + ("，附无匹配说明" if has_reason else ""))

    # --- 7/8. 初稿 --------------------------------------------------------
    draft_path = config.OUT_DIR / "draft_v0_5.md"
    if draft is None or not draft_path.exists():
        report.add(False, "7. 初稿字数在区间内", "缺少 draft.json")
        report.add(False, "8. 初稿证据标记与逐字稿一致", "缺少 draft.json")
    else:
        body = draft.get("body_markdown", "")
        chars = count_draft_chars(body)
        # 字数已由出题方确认「合理即可」，不再是门禁。
        # 只剩一个"是不是根本没写完"的地板 —— 那是故障，不是风格问题。
        report.add(chars >= config.DRAFT_SANITY_MIN,
                   f"7. 初稿写完了（地板 {config.DRAFT_SANITY_MIN} 字）",
                   f"{chars} 字")

        # 标记现在是 [P08 字幕33 时间戳 说话人]：编号和字幕由模型给，
        # 后两项由代码补，所以这里只需要验前两项指得对不对。
        bad_marks: list[str] = []
        valid_pids = {p["point_id"] for p in (evidence or {}).get("points", [])}
        marks = MARKER_PATTERN.findall(body)
        for pid, idx_str in marks:
            if valid_pids and pid not in valid_pids:
                bad_marks.append(f"{pid} 不在要点表")
            if transcript.get(int(idx_str)) is None:
                bad_marks.append(f"字幕{idx_str} 不存在")
        report.add(bool(marks) and not bad_marks,
                   "8. 初稿证据标记指向真实要点与字幕",
                   f"{len(marks)} 处标记" + (f"，异常：{bad_marks[:3]}" if bad_marks else ""))

    # --- 9–11. 本材料包的回归测试 -----------------------------------------
    checks_path = config.OUT_DIR / "editorial_checks.md"
    reuse_path = config.OUT_DIR / "reuse_suggestions.md"
    checks_text = checks_path.read_text(encoding="utf-8") if checks_path.exists() else ""
    reuse_text = reuse_path.read_text(encoding="utf-8") if reuse_path.exists() else ""

    if reuse is None:
        report.add(None, "9. note_02 被判为同源并排除出候选池", "缺少 reuse.json")
    else:
        excluded = {e["note_id"]: e for e in reuse.get("excluded_same_source") or []}
        hit = excluded.get("note_02")
        # 同源现在由代码算，所以这里能断言的比"模型说了同源"强得多：
        # 覆盖率必须真的过阈值，且它不能出现在给编辑的建议里。
        in_suggestions = any(
            s.get("note_id") == "note_02" for s in reuse.get("suggestions") or []
        )
        ok = bool(hit) and hit["coverage"] >= config.SAME_SOURCE_COVERAGE and not in_suggestions
        report.add(
            ok, "9. note_02 被判为同源并排除出候选池",
            f"覆盖率 {hit['coverage']:.4f} ≥ {config.SAME_SOURCE_COVERAGE}"
            if hit else "未出现在 excluded_same_source",
        )

    # --- 10. 三个带数字的说法必须被真的抓到 --------------------------------
    #
    # ⚠️ 这一项以前是假绿灯：它判的是 `"字幕44" in checks_text`，
    # 而字幕 44 会因为另一条要点（"最大的障碍是机构"）而出现在文件里，
    # 于是「用 AI 学习能少 60% 时间」这个数字整条漏抽，检查照样是绿的。
    #
    # 现在改成断言**说法本身**：这个数字要么进了某条要点的原话/支撑，
    # 要么就是真的漏了。检字幕号只能证明"那条字幕被提过"，
    # 证明不了"那个需要核实的说法被抓住了"。
    FACT_TARGETS = (
        ("五分之一", "终生收入下降超过五分之一"),
        ("100美元", "同等教育成本约 100 美元"),
        ("60%", "用 AI 学习少 60% 时间"),
    )
    if evidence is None:
        for _, label in FACT_TARGETS:
            report.add(None, f"10. 「{label}」被抽取并进入待核清单", "缺少 evidence.json")
    else:
        for needle, label in FACT_TARGETS:
            owner = None
            for point in evidence.get("points", []):
                blob = json.dumps(
                    [point.get("claim"), point.get("support")], ensure_ascii=False
                ).replace(" ", "")
                if needle in blob:
                    owner = point
                    break
            if owner is None:
                report.add(False, f"10. 「{label}」被抽取并进入待核清单",
                           "❗未被任何要点抽到 —— 这是漏抽，不是格式问题")
                continue
            listed = owner["point_id"] in checks_text
            report.add(
                listed, f"10. 「{label}」被抽取并进入待核清单",
                f"{owner['point_id']}（{owner.get('verification')}）"
                + ("" if listed else " —— 抽到了但没进 editorial_checks"),
            )

    doubt = bool(re.search(r"\|\s*85\s*\|", checks_text))
    report.add(doubt, "11. 字幕 85 说话人存疑进入风险项",
               "已列出" if doubt else "未出现在 editorial_checks.md")

    print("验收清单（PRD §3.7）\n" + "=" * 60)
    print(report.render())
    print("=" * 60)
    if report.failed:
        print(f"{report.failed} 项未通过")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
