"""主流程：逐字稿 + 历史素材 → 6 份编辑工作包。

    python run.py                # 全流程
    python run.py --dry-run      # 只导出第一个未缓存阶段的 prompt（不需要 API key）
    python run.py --cache        # 复用已有中间产物，只跑缺的阶段
    python run.py --angle 2      # 用第 2 个角度写初稿（默认第 1 个）

阶段划分见 PRD §2.1。每个阶段的中间产物都落在 outputs/_intermediate/，
目的有三：评审可以只看 evidence 主表判断抽取质量；改 prompt 后可单步重跑；
自动校验可以离线在中间产物上跑。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from functools import partial
from pathlib import Path

from pipeline import config, render, validate
from pipeline.llm import LLMClient, LLMError
from pipeline.prompts import load_prompt
from pipeline.srt import OPENING_HOOK_CUES, Transcript, parse_srt
from pipeline.textutil import count_draft_chars, ngram_coverage


STAGE_ORDER = ["extract", "reuse", "angles", "outline", "draft"]


class DryRun(Exception):
    """--dry-run 时用来在第一个需要调模型的阶段停下。"""


class StopAfter(Exception):
    """--until 到达指定阶段后停下。"""


class Stage:
    def __init__(self, run: "Runner", name: str) -> None:
        self.run = run
        self.name = name
        self.path = config.INTERMEDIATE_DIR / f"{name}.json"

    def cached(self) -> dict | None:
        if self.run.use_cache and self.path.exists():
            print(f"  ↩ 复用缓存 {self.path.name}")
            return json.loads(self.path.read_text(encoding="utf-8"))
        return None

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.dry_run = args.dry_run
        self.use_cache = args.cache
        self.angle_index = args.angle - 1
        self.until = args.until
        self.client: LLMClient | None = None
        self.transcript: Transcript | None = None

    def checkpoint(self, stage: str) -> None:
        """跑完一个阶段后调用：到了 --until 指定的位置就停。"""
        if self.until == stage:
            raise StopAfter(stage)

    # -- 输入 ---------------------------------------------------------------

    def load_inputs(self) -> None:
        print("Stage 1 解析逐字稿（纯代码）…")
        self.transcript = parse_srt(config.SRT_PATH)
        t = self.transcript
        print(f"  {len(t)} 条字幕 / {t.total_chars} 字 / {t.duration_minutes:.1f} 分钟"
              f" / 说话人 {'、'.join(t.speakers)}")
        # 转录层异常（时间戳重叠、超短片段）只作为控制台诊断，**不进交付物**：
        # 编辑读 editorial_checks 是为了防止错写漏写，"字幕 24 时长 1.0 秒"
        # 不会让他做出任何不同的决定，列进去只会把真正要处理的几件事淹掉。
        print(f"  转录层异常 {len(t.issues)} 处（仅诊断，不进交付物）")

        self.brief = config.BRIEF_PATH.read_text(encoding="utf-8-sig")
        self.segments = self._read_jsonl(config.SEGMENTS_PATH)
        self.notes_meta = self._read_jsonl(config.NOTES_META_PATH)
        if not self.segments:
            print("  ⚠️ 关键段表为空：先跑 `python ingest.py` 才能做历史复用判断")

    @staticmethod
    def _read_jsonl(path: Path) -> list[dict]:
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip()
        ]

    def llm(self) -> LLMClient:
        if self.client is None:
            self.client = LLMClient()
        return self.client

    def _dump_prompt(self, name: str, system: str, user: str) -> None:
        path = config.INTERMEDIATE_DIR / "prompts" / f"{name}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"[SYSTEM]\n{system}\n\n[USER]\n{user}", encoding="utf-8")
        print(f"  [dry-run] prompt → {path}（{len(system) + len(user)} 字）")

    def call(self, name: str, system: str, user: str, validator=None,
             return_last_on_failure: bool = False, retry_hint: str = "") -> dict:
        if self.dry_run:
            self._dump_prompt(name, system, user)
            raise DryRun(name)
        return self.llm().complete_json(
            name, system, user,
            validate=validator,
            return_last_on_failure=return_last_on_failure,
            retry_hint=retry_hint,
        )

    # -- 各阶段 -------------------------------------------------------------

    def stage_extract(self) -> dict:
        print("\nStage 2 观点抽取（LLM×1，全文一次进，不分段）…")
        stage = Stage(self, "evidence")
        cached = stage.cached()
        if cached:
            return cached

        low, high = config.TBD_PLACEHOLDERS["evidence_point_range"]
        hook = sorted(OPENING_HOOK_CUES)
        system, user = load_prompt(
            "extract",
            brief=self.brief,
            transcript=self.transcript.as_prompt_block(),
            point_range=f"{low}–{high}",
            hook_range=f"{hook[0]}–{hook[-1]}",
        )
        data = self.call(
            "extract", system, user,
            validator=partial(validate.evidence_hard_problems, transcript=self.transcript),
            # 重试用尽后不整批丢弃，改为只剔除溯源不了的条目并记账（见下）
            return_last_on_failure=True,
            retry_hint=(
                "常见原因：你在引用中间省略了插入语（如括号内的补充说明）却没有用 …… 标出。"
                "请回到原文逐字复制，需要省略中间部分时必须写 ……"
            ),
        )

        # 严格重试仍不合格时的处置：不放行任何未经验证的引用，
        # 但也不因为一条坏引用丢掉整批好要点。被剔除项全部记账。
        dropped = validate.drop_unverifiable(data, self.transcript)
        problems = validate.repair_evidence(data, self.transcript)
        if problems:
            raise LLMError("evidence 剔除后仍未通过校验：" + "；".join(problems[:8]))

        points = data.get("points", [])
        floor = config.TBD_PLACEHOLDERS["evidence_point_range"][0]
        if len(points) < floor:
            raise LLMError(
                f"剔除后只剩 {len(points)} 条要点（下限 {floor}），"
                f"抽取质量不足以继续。被剔除 {len(dropped)} 条。"
            )

        data["_dropped"] = dropped
        print(f"  抽出 {len(points)} 条要点"
              + (f"，剔除 {len(dropped)} 条溯源失败的引用" if dropped else ""))
        for item in dropped:
            print(f"    ⚠️ 剔除{item['scope']} {item['point_id']}：{item['quote'][:34]}…")
        stage.save(data)
        return data

    def stage_reuse(self, evidence: dict) -> dict:
        print("\nStage 3 历史复用判断（LLM×1，段表 × 逐字稿）…")
        stage = Stage(self, "reuse")
        cached = stage.cached()
        if cached:
            return cached

        if not self.segments:
            # 刻意不写缓存：这是"入库还没跑"的临时状态，缓存下来会在
            # 补跑 ingest 之后被 --cache 静默复用，等于永久性地丢掉复用判断
            print("  ⚠️ 段表为空，跳过匹配（不写缓存）")
            return {
                "suggestions": [],
                "excluded_same_source": [],
                "search_summary": "关键段表为空，未执行匹配。请先运行 `python ingest.py`。",
            }

        excluded = self._exclude_same_source()
        segments = [s for s in self.segments if s["note_id"] not in {e["note_id"] for e in excluded}]
        for item in excluded:
            print(f"  ⛔ {item['note_id']} 覆盖率 {item['coverage']:.4f}，"
                  f"与本次逐字稿同源，已排除出候选池")

        seg_block = "\n".join(
            f"- `{s['seg_id']}`（{s['note_id']}）[{s.get('topic_tag', '')}] "
            f"{s['claim']}\n  原文：{s['quote']}"
            for s in segments
        )
        kept_ids = {s["note_id"] for s in segments}
        meta_block = "\n".join(
            "- " + "｜".join(f"{k}：{v}" for k, v in m.items())
            for m in self.notes_meta
            if m.get("note_id") in kept_ids
        )
        system, user = load_prompt(
            "reuse",
            brief=self.brief,
            notes_meta=meta_block,
            segments=seg_block,
            evidence=evidence_digest(evidence),
            transcript=self.transcript.as_prompt_block(),
            note_count=str(len(kept_ids)),
        )
        pids = {p["point_id"] for p in evidence.get("points", [])}
        data = self.call(
            "reuse", system, user,
            validator=partial(validate.validate_reuse, valid_pids=pids),
        )
        data["excluded_same_source"] = excluded
        data["candidate_count"] = len(kept_ids)
        print(f"  候选 {len(kept_ids)} 篇 → {len(data.get('suggestions', []))} 条建议")
        stage.save(data)
        return data

    def _exclude_same_source(self) -> list[dict]:
        """把与本次逐字稿同源的笔记挡在 prompt 之外。

        这件事**在发 prompt 之前用代码做完**，不进模型：
        「这篇笔记有多少内容能在这份逐字稿里字面找到」是可计算量，
        而让模型读两份长文本自己"感觉像不像同一场"既贵又不稳定。
        【实测】note_02 覆盖率 0.9122，其余 7 篇 ≤ 0.0001。

        不删段表 —— 同源是"相对本次输入"的属性，换一场访谈它就恢复可用。
        """
        transcript_text = "\n".join(cue.text for cue in self.transcript.cues)
        bodies: dict[str, list[str]] = {}
        for seg in self.segments:
            bodies.setdefault(seg["note_id"], []).append(seg.get("quote", ""))

        excluded = []
        for note_id, quotes in bodies.items():
            coverage = ngram_coverage("".join(quotes), transcript_text)
            if coverage >= config.SAME_SOURCE_COVERAGE:
                meta = next(
                    (m for m in self.notes_meta if m.get("note_id") == note_id), {}
                )
                excluded.append({
                    "note_id": note_id,
                    "title": meta.get("标题", ""),
                    "coverage": round(coverage, 4),
                })
        return excluded

    def stage_angles(self, evidence: dict) -> dict:
        print("\nStage 4 角度生成（LLM×1，只喂要点表，不喂原文）…")
        stage = Stage(self, "angles")
        cached = stage.cached()
        if cached:
            return cached

        pids = {p["point_id"] for p in evidence["points"]}
        system, user = load_prompt(
            "angles",
            brief=self.brief,
            evidence=evidence_digest(evidence),
            angle_count=str(config.ANGLE_COUNT),
        )
        data = self.call(
            "angles", system, user,
            validator=partial(validate.validate_angles, valid_pids=pids),
        )
        for i, angle in enumerate(data.get("angles", []), 1):
            print(f"  {i}. {angle.get('title', '')}")
        stage.save(data)
        return data

    def stage_outline(self, evidence: dict, angle: dict) -> dict:
        print("\nStage 5a 搭结构（LLM×1）…")
        stage = Stage(self, "outline")
        cached = stage.cached()
        if cached:
            return cached

        pids = {p["point_id"] for p in evidence["points"]}
        system, user = load_prompt(
            "outline",
            brief=self.brief,
            angle=json.dumps(angle, ensure_ascii=False, indent=2),
            evidence=evidence_digest(evidence),
        )
        data = self.call(
            "outline", system, user,
            validator=partial(validate.validate_outline, valid_pids=pids),
        )
        sections = data.get("sections", [])
        subs = sum(len(s.get("subs") or []) for s in sections)
        print(f"  {len(sections)} 节 / {subs} 个二级标题")
        stage.save(data)
        return data

    def stage_draft(self, evidence: dict, angle: dict, outline: dict) -> tuple[dict, list[str]]:
        print("\nStage 5b 写 v0.5 初稿（LLM×1）…")
        stage = Stage(self, "draft")
        cached = stage.cached()
        if cached:
            body, warnings = validate.repair_draft_markers(
                cached.get("body_markdown", ""), self.transcript
            )
            cached["body_markdown"] = body
            return cached, warnings

        pids = {p["point_id"] for p in evidence["points"]}
        cited = {
            ref.get("cue")
            for point in evidence["points"]
            for ref, _ in validate._iter_refs(point)
            if ref.get("cue")
        }
        system, user = load_prompt(
            "draft",
            angle=json.dumps(angle, ensure_ascii=False, indent=2),
            outline=json.dumps(outline, ensure_ascii=False, indent=2),
            evidence=evidence_digest(evidence),
            min_chars=str(config.DRAFT_MIN_CHARS),
            max_chars=str(config.DRAFT_MAX_CHARS),
        )
        data = self.call(
            "draft", system, user,
            validator=partial(
                validate.validate_draft,
                transcript=self.transcript,
                cited_cues=cited,
                valid_pids=pids,
            ),
        )
        body, warnings = validate.repair_draft_markers(
            data.get("body_markdown", ""), self.transcript
        )
        data["body_markdown"] = body
        print(f"  {count_draft_chars(body)} 字")
        stage.save(data)
        return data, warnings

    # -- 渲染 ---------------------------------------------------------------

    def render_all(
        self, evidence, reuse, angles, outline, draft, draft_warnings, title
    ) -> None:
        print("\nStage 6 渲染 6 个交付文件（纯代码）…")
        config.OUT_DIR.mkdir(parents=True, exist_ok=True)
        srt_name = config.SRT_PATH.name
        t = self.transcript
        soft = validate.evidence_soft_warnings(evidence)

        files = {
            "evidence.md": render.render_evidence(evidence, t, srt_name),
            "angles.md": render.render_angles(angles, evidence, t, srt_name),
            "outline.md": render.render_outline(outline, evidence, t, srt_name),
            "reuse_suggestions.md": render.render_reuse(reuse, t, srt_name),
            "draft_v0_5.md": render.render_draft(draft, t, srt_name, title),
            "editorial_checks.md": render.render_checks(
                evidence, reuse, soft, draft_warnings, t, srt_name
            ),
        }
        for name, content in files.items():
            path = config.OUT_DIR / name
            path.write_text(content, encoding="utf-8")
            print(f"  ✓ {path.relative_to(config.ROOT)}（{len(content)} 字）")


def evidence_digest(evidence: dict) -> str:
    """喂给下游阶段的要点表文本形态。

    下游只看这个，不看逐字稿原文 —— 取舍已经在抽取阶段做完了。
    """
    lines = []
    for point in evidence.get("points", []):
        claim = point.get("claim") or {}
        lines.append(
            f"### {point['point_id']} {point.get('title', '')}\n"
            f"- 类型：{point.get('type')}｜核实状态：{point.get('verification')}"
            f"｜标记：{'、'.join(point.get('flags') or []) or '无'}\n"
            f"- 观点原话：「{claim.get('quote', '')}」"
            f"（{claim.get('speaker')} 字幕{claim.get('cue')} {claim.get('ts')}）"
        )
        qualifier = point.get("qualifier")
        if qualifier:
            lines.append(
                f"- ⚠️ 限定语：「{qualifier.get('quote', '')}」"
                f"（{qualifier.get('speaker')} 字幕{qualifier.get('cue')}）"
                f" —— 引用本条主张时必须一并带上"
            )
        # support 的含义随 type 变：事实类问「有什么证据」，其余类问「他为什么这么认为」。
        # 下游看到的标签必须跟着变，否则它会把一条没展开理由的观点当成没出处的事实。
        is_fact = point.get("type") == "事实"
        label = "证据" if is_fact else "理由"
        support = point.get("support") or []
        if support:
            for i, item in enumerate(support, 1):
                lines.append(
                    f"  - {label}{i}：「{item.get('quote', '')}」"
                    f"（{item.get('speaker')} 字幕{item.get('cue')} {item.get('ts')}）"
                )
        elif is_fact:
            lines.append("  - 证据：**无** —— 🔴 无出处的事实，不得进结论位")
        else:
            lines.append("  - 理由：**无** —— 他直接下了判断，没展开")
        if point.get("why"):
            lines.append(f"- 为什么值得写：{point['why']}")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    config.setup_stdout()
    parser = argparse.ArgumentParser(description="AI 增强内容复盘工具")
    parser.add_argument("--dry-run", action="store_true",
                        help="只导出第一个未缓存阶段的 prompt，不调模型")
    parser.add_argument("--cache", action="store_true",
                        help="复用 outputs/_intermediate 下已有的中间产物")
    parser.add_argument("--angle", type=int, default=1,
                        help="用第几个角度写初稿（默认 1）")
    parser.add_argument("--until", choices=STAGE_ORDER, default=None,
                        help="跑到指定阶段为止（阶段：%(choices)s）")
    args = parser.parse_args()

    started = time.time()
    runner = Runner(args)

    try:
        runner.load_inputs()
        evidence = runner.stage_extract()
        runner.checkpoint("extract")
        reuse = runner.stage_reuse(evidence)
        runner.checkpoint("reuse")
        angles = runner.stage_angles(evidence)
        runner.checkpoint("angles")

        candidates = angles.get("angles", [])
        idx = min(max(runner.angle_index, 0), len(candidates) - 1)
        angle = candidates[idx]
        # 标题从这里往下一路透传，不让 outline / draft 阶段的模型重写。
        # 实测过它会漂：angles 给「时间已缩到一个周末」，draft 交回「只需一个周末」。
        # 编辑选的是哪个角度，交付物就得是哪个角度，这是可以由代码保证的事。
        title = angle.get("title", "")
        print(f"\n选用角度 {idx + 1}：{title}")

        outline = runner.stage_outline(evidence, angle)
        runner.checkpoint("outline")
        draft, draft_warnings = runner.stage_draft(evidence, angle, outline)
        runner.render_all(
            evidence, reuse, angles, outline, draft, draft_warnings, title
        )

    except DryRun as stop:
        print(f"\n[dry-run] 已在阶段「{stop}」停止。"
              f"\n加 --cache 可跳过已有中间产物，逐阶段导出 prompt。")
        return 0
    except StopAfter as stop:
        print(f"\n已按 --until 在阶段「{stop}」后停止，中间产物已缓存。"
              f"\n继续：`python run.py --cache`")
        if runner.client:
            print(runner.client.report())
        return 0
    except Exception as exc:  # noqa: BLE001 —— 宁可报错，不可伪造
        print(f"\n❌ 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("按设计不产出兜底内容 —— 半成品比报错更危险。", file=sys.stderr)
        return 1

    print(f"\n完成，耗时 {time.time() - started:.1f}s")
    if runner.client:
        print(runner.client.report())
    print("\n下一步：`python check.py` 跑验收清单")
    return 0


if __name__ == "__main__":
    sys.exit(main())
