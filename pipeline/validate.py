"""校验与自动修正。

设计原则：**能用代码算出来的事实，不问模型。**

模型只负责一件它不可替代的事 —— 从原文里挑出哪句话值得引。
说话人是谁、时间戳是多少、这句话在第几条字幕，全部由代码从逐字稿反查得出。
这样做的结果是：溯源信息 100% 正确不是靠模型自觉，而是结构上不可能错；
模型唯一可能出的错（引了原文里不存在的话）会被 `locate_quote` 当场抓住。
"""

from __future__ import annotations

import re

from .config import (
    ANGLE_COUNT,
    DRAFT_SANITY_MIN,
    REUSE_MIN_COUNT,
)
from .srt import OPENING_HOOK_CUES, Transcript
from .textutil import count_draft_chars, locate_quote, parse_ts

VALID_TYPES = {"观点", "案例", "因果链", "事实"}
VALID_VERIFICATION = {"无需核实", "需核实", "无出处"}


# ---------------------------------------------------------------------------
# 证据定位与自动修正
# ---------------------------------------------------------------------------

def locate_in_transcript(quote: str, transcript: Transcript) -> list[int]:
    """返回所有包含该引用的字幕序号。"""
    return [cue.index for cue in transcript.cues if locate_quote(quote, cue.text)]


def repair_quote_ref(ref: dict, transcript: Transcript, label: str) -> list[str]:
    """把一条引用的 cue / ts / speaker 用逐字稿的事实覆盖掉。

    返回问题列表（只有"原话根本不存在"才算问题）。
    """
    problems: list[str] = []
    quote = (ref.get("quote") or "").strip()
    if not quote:
        problems.append(f"{label}：quote 为空")
        return problems

    hits = locate_in_transcript(quote, transcript)
    if not hits:
        problems.append(
            f"{label}：这段引用在逐字稿中找不到（可能是改写或杜撰）——「{quote[:40]}…」"
        )
        return problems

    claimed = ref.get("cue")
    chosen = claimed if claimed in hits else hits[0]
    cue = transcript.get(chosen)
    assert cue is not None

    if claimed != chosen:
        ref["_repaired"] = f"模型标注字幕 {claimed}，实际出自字幕 {chosen}"
    if len(hits) > 1:
        ref["_ambiguous"] = f"该原话在字幕 {hits} 中均出现"

    ref["cue"] = cue.index
    ref["ts"] = cue.start_ts
    ref["speaker"] = cue.speaker
    return problems


def repair_evidence(data: dict, transcript: Transcript) -> list[str]:
    """就地修正 evidence，返回无法修正的问题。"""
    problems: list[str] = []
    points = data.get("points")
    if not isinstance(points, list) or not points:
        return ["points 为空或不是数组"]

    seen_ids: set[str] = set()
    for i, point in enumerate(points):
        pid = point.get("point_id") or f"P{i + 1:02d}"
        point["point_id"] = pid
        if pid in seen_ids:
            problems.append(f"{pid}：point_id 重复")
        seen_ids.add(pid)

        if not (point.get("title") or "").strip():
            problems.append(f"{pid}：title 为空")

        if point.get("type") not in VALID_TYPES:
            problems.append(f"{pid}：type={point.get('type')!r} 不在 {VALID_TYPES}")

        if point.get("verification") not in VALID_VERIFICATION:
            problems.append(
                f"{pid}：verification={point.get('verification')!r} 不在 {VALID_VERIFICATION}"
            )

        claim = point.get("claim")
        if not isinstance(claim, dict):
            problems.append(f"{pid}：缺 claim")
        else:
            problems += repair_quote_ref(claim, transcript, f"{pid}.claim")

        qualifier = point.get("qualifier")
        if isinstance(qualifier, dict) and qualifier.get("quote"):
            problems += repair_quote_ref(qualifier, transcript, f"{pid}.qualifier")
        else:
            point["qualifier"] = None

        support = point.get("support")
        if support is None:
            support = []
        if not isinstance(support, list):
            problems.append(f"{pid}：support 不是数组")
            support = []
        for j, item in enumerate(support):
            problems += repair_quote_ref(item, transcript, f"{pid}.support[{j}]")
        point["support"] = support

        # support 空意味着什么，取决于这条是事实还是观点 —— 两类问的是两个问题：
        #   事实类：support 是「有什么证据」，空 = 这个数字全篇没东西托着（红灯）
        #   其余类：support 是「他为什么这么认为」，空 = 他没展开（正常）
        # 早先版本对两者一律打「纯断言」标记，是范畴错误：它让"100 美元没出处"
        # 和"企业家就是能动性的同义词"长得一模一样，而区分这两件事恰恰是本工具
        # 最该做的。渲染层按 type 分叉，这里只保留事实类的标记。
        if not isinstance(point.get("flags"), list):
            point["flags"] = []
        if not support and point.get("type") == "事实":
            if "无出处事实" not in point["flags"]:
                point["flags"].append("无出处事实")

    return problems


def evidence_hard_problems(data: dict, transcript: Transcript) -> list[str]:
    """给 LLM 重试用的校验器：只报致命问题。"""
    import copy

    return repair_evidence(copy.deepcopy(data), transcript)


def drop_unverifiable(data: dict, transcript: Transcript) -> list[dict]:
    """剔除溯源不了的内容，返回被剔除项的记录。

    重试用尽后的处置：**不放行任何未经验证的引用，但也不因为一条坏引用
    丢掉整批好要点。** 被剔除的每一项都会写进 editorial_checks.md ——
    编辑必须能看见"工具丢了什么、为什么丢"，否则这就成了静默删除。

    - `claim` 溯源失败 → 整条要点剔除（观点原话是这条要点存在的理由）
    - `support` / `qualifier` 溯源失败 → 只剔除该条引用，要点保留
    """
    dropped: list[dict] = []
    kept_points: list[dict] = []

    for point in data.get("points", []):
        pid = point.get("point_id", "?")

        claim = point.get("claim") or {}
        if not locate_in_transcript(claim.get("quote", ""), transcript):
            dropped.append({
                "scope": "整条要点",
                "point_id": pid,
                "title": point.get("title", ""),
                "quote": claim.get("quote", ""),
                "reason": "观点原话在逐字稿中定位不到（模型可能改写或省略了插入语）",
            })
            continue

        qualifier = point.get("qualifier")
        if isinstance(qualifier, dict) and qualifier.get("quote"):
            if not locate_in_transcript(qualifier["quote"], transcript):
                dropped.append({
                    "scope": "限定语",
                    "point_id": pid,
                    "title": point.get("title", ""),
                    "quote": qualifier["quote"],
                    "reason": "限定语原话定位不到，已移除 —— ⚠️ 该要点可能仍带限定，请人工回看原文",
                })
                point["qualifier"] = None

        kept_support = []
        for item in point.get("support") or []:
            if locate_in_transcript(item.get("quote", ""), transcript):
                kept_support.append(item)
            else:
                dropped.append({
                    "scope": "印证",
                    "point_id": pid,
                    "title": point.get("title", ""),
                    "quote": item.get("quote", ""),
                    "reason": "印证原话定位不到，已移除",
                })
        point["support"] = kept_support
        kept_points.append(point)

    data["points"] = kept_points
    return dropped


def evidence_soft_warnings(data: dict) -> list[str]:
    """不阻断流程，但要写进 editorial_checks 让编辑知道的事。"""
    warnings: list[str] = []
    for point in data.get("points", []):
        pid = point["point_id"]
        for ref, label in _iter_refs(point):
            if ref.get("_repaired"):
                warnings.append(f"{pid}.{label}：{ref['_repaired']}（已按逐字稿自动更正）")
            if ref.get("_ambiguous"):
                warnings.append(f"{pid}.{label}：{ref['_ambiguous']}")
            if ref.get("cue") in OPENING_HOOK_CUES:
                warnings.append(
                    f"{pid}.{label}：证据落在片头预告（字幕 {ref['cue']}），"
                    f"应改指向正片对应位置"
                )
    return warnings


def _iter_refs(point: dict):
    if isinstance(point.get("claim"), dict):
        yield point["claim"], "claim"
    if isinstance(point.get("qualifier"), dict):
        yield point["qualifier"], "qualifier"
    for i, item in enumerate(point.get("support", [])):
        yield item, f"support[{i}]"


# ---------------------------------------------------------------------------
# 各阶段的输出校验
# ---------------------------------------------------------------------------

def validate_angles(data: dict, valid_pids: set[str]) -> list[str]:
    problems: list[str] = []
    angles = data.get("angles")
    if not isinstance(angles, list):
        return ["angles 不是数组"]
    if len(angles) != ANGLE_COUNT:
        problems.append(f"angles 必须恰好 {ANGLE_COUNT} 个，收到 {len(angles)} 个")

    for i, angle in enumerate(angles):
        tag = f"angles[{i}]"
        if not (angle.get("title") or "").strip():
            problems.append(f"{tag}：title 为空")
        if not (angle.get("claim") or "").strip():
            problems.append(f"{tag}：claim 为空——没说清这个角度主张什么")
        if not (angle.get("weakness") or "").strip():
            problems.append(f"{tag}：weakness 为空——没说清选它的代价")
        based = angle.get("based_on") or []
        if not based:
            problems.append(f"{tag}：based_on 为空——说不出 P 编号的角度等于凭空编的")
        unknown = [p for p in based if p not in valid_pids]
        if unknown:
            problems.append(f"{tag}：based_on 含要点表里不存在的编号 {unknown}")
    return problems


def validate_outline(data: dict, valid_pids: set[str]) -> list[str]:
    """结构是一棵两级标题树，每级都必须是判断句、每条要点都必须挂得住编号。"""
    problems: list[str] = []
    sections = data.get("sections")
    if not isinstance(sections, list) or not sections:
        return ["sections 为空"]

    for i, section in enumerate(sections):
        tag = f"sections[{i}]"
        heading = (section.get("heading") or "").strip()
        if not heading:
            problems.append(f"{tag}：一级 heading 为空")
        subs = section.get("subs") or []
        if not subs:
            problems.append(f"{tag}「{heading}」：没有二级标题")
        for j, sub in enumerate(subs):
            sub_tag = f"{tag}.subs[{j}]"
            if not (sub.get("heading") or "").strip():
                problems.append(f"{sub_tag}：二级 heading 为空")
            points = sub.get("points") or []
            if not points:
                problems.append(
                    f"{sub_tag}「{sub.get('heading', '')}」：没挂任何要点——"
                    f"挂不上要点的小节是你自己想出来的过渡内容，删掉"
                )
            unknown = [
                p.get("from") for p in points if p.get("from") not in valid_pids
            ]
            if unknown:
                problems.append(f"{sub_tag}：引用了不存在的编号 {unknown}")
    return problems


def validate_reuse(data: dict, valid_pids: set[str]) -> list[str]:
    """同源已由代码在发 prompt 前排除，所以这里只该出现能用的两种结论。

    `matched_point` 必须落在要点表的 P 编号上：要点表已经是"这场访谈里真正值得
    写的东西"，一段历史内容如果对不上任何一条要点，编辑不会为了用它专门加一节。
    把这道门槛写成硬校验，是因为它是本阶段最容易放水的地方 ——
    不设限时模型会为 7 篇里的 6 篇都找出一个理由。
    """
    problems: list[str] = []
    suggestions = data.get("suggestions")
    if not isinstance(suggestions, list):
        return ["suggestions 不是数组"]
    if len(suggestions) < REUSE_MIN_COUNT and not (data.get("no_match_reason") or "").strip():
        problems.append(
            "既没有给出复用建议，也没有说明为什么都不该复用——题目要求二者至少有其一"
        )
    for i, item in enumerate(suggestions):
        if item.get("verdict") not in {"可用", "有限复用"}:
            problems.append(
                f"suggestions[{i}]：verdict={item.get('verdict')!r} 非法"
                f"（只报能用的；不相关的不要写进输出）"
            )
        if not item.get("note_id"):
            problems.append(f"suggestions[{i}]：缺 note_id")
        if not (item.get("place_in_article") or "").strip():
            problems.append(f"suggestions[{i}]：缺 place_in_article——没说建议放在哪一节")
        matched = item.get("matched_segments") or []
        if not matched:
            problems.append(f"suggestions[{i}]：没给出匹配到的段")
        for j, seg in enumerate(matched):
            point = (seg.get("matched_point") or "").strip()
            if point not in valid_pids:
                problems.append(
                    f"suggestions[{i}].matched_segments[{j}]："
                    f"matched_point={point!r} 不是要点表里的 P 编号——"
                    f"对不上任何一条要点就是不相关，整篇不要写进输出"
                )
    return problems


# 模型只写 `[P08 字幕33]`，时间戳和说话人由代码补 —— 不问模型可算之事。
# 兼容它多写了后两项的情况（一律以逐字稿为准覆盖）。
MARKER_PATTERN = re.compile(r"\[\s*(P\d+)\s+字幕\s*(\d+)[^\]]*\]")


def repair_draft_markers(body: str, transcript: Transcript) -> tuple[str, list[str]]:
    """把正文里的内联证据标记补全成逐字稿里的事实值。"""
    warnings: list[str] = []

    def _fix(match: re.Match) -> str:
        pid, idx = match.group(1), int(match.group(2))
        cue = transcript.get(idx)
        if cue is None:
            warnings.append(f"证据标记 {pid} 指向不存在的字幕 {idx}")
            return match.group(0)
        if idx in OPENING_HOOK_CUES:
            warnings.append(f"证据标记 {pid} 指向片头预告（字幕 {idx}），应改指正片位置")
        return f"[{pid} 字幕{cue.index} {cue.start_ts} {cue.speaker}]"

    return MARKER_PATTERN.sub(_fix, body), warnings


def validate_draft(
    data: dict, transcript: Transcript, cited_cues: set[int], valid_pids: set[str]
) -> list[str]:
    problems: list[str] = []
    body = data.get("body_markdown") or ""
    if not body.strip():
        return ["body_markdown 为空"]

    # 字数不再是门禁：题目原文写 500–800，但已向出题方确认「合理即可」。
    # 只保留一个"是不是根本没写完"的地板 —— 那是故障，不是风格问题。
    count = count_draft_chars(body)
    if count < DRAFT_SANITY_MIN:
        problems.append(f"正文只有 {count} 字（下限 {DRAFT_SANITY_MIN}）——不像写完了")

    markers = MARKER_PATTERN.findall(body)
    if not markers:
        problems.append("正文没有任何内联证据标记")

    for pid, idx_str in markers:
        idx = int(idx_str)
        if pid not in valid_pids:
            problems.append(f"证据标记引用了要点表里不存在的编号 {pid}")
        if transcript.get(idx) is None:
            problems.append(f"证据标记指向不存在的字幕 {idx}")
        elif cited_cues and idx not in cited_cues:
            problems.append(
                f"证据标记指向字幕 {idx}，但要点表里没有任何条目引用这条字幕——"
                f"标记必须落在已抽取的证据上"
            )
    return problems
