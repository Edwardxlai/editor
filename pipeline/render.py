"""Stage 6：JSON → 6 个 Markdown（纯代码，无 AI）。

六个文件是编辑动线上的一条链，每份只做自己那一环，不预演下一环：

    angles 选 → outline 搭 → evidence 查 → draft 写 → reuse 补 → checks 核

所以这里有一条硬规矩：**方法论不进交付物。**
"为什么不喂原文""排序留给编辑"这类话是写给评审看的，属于 PRD；
约束模型的规则属于 prompt。编辑打开 outputs/ 只该看到他当天要用的东西。
"""

from __future__ import annotations

from datetime import datetime

from .srt import Transcript
from .textutil import count_draft_chars

BANNER = (
    "> 本文件由「AI 增强内容复盘工具」自动生成，**全部内容均为待编辑确认的草稿**，"
    "无一可直接发布。\n>\n"
    "> 生成时间：{ts}｜素材：{srt}（{cues} 条字幕 / {chars} 字 / {mins:.1f} 分钟）\n"
)


def _banner(transcript: Transcript, srt_name: str) -> str:
    return BANNER.format(
        ts=datetime.now().strftime("%Y-%m-%d %H:%M"),
        srt=srt_name,
        cues=len(transcript),
        chars=transcript.total_chars,
        mins=transcript.duration_minutes,
    )


def _ref(ref: dict) -> str:
    return f"{ref.get('speaker', '?')} 字幕{ref.get('cue', '?')} {ref.get('ts', '?')}"


def _cell(text: str) -> str:
    return (text or "").replace("|", "丨").replace("\n", " ")


def _points_by_id(evidence: dict) -> dict[str, dict]:
    return {p["point_id"]: p for p in evidence.get("points", [])}


# ---------------------------------------------------------------------------
# 1. angles —— 选
# ---------------------------------------------------------------------------

def render_angles(angles: dict, evidence: dict, transcript: Transcript, srt_name: str) -> str:
    """编辑在这一步只做一件事：选一个。所以只给选它的理由和选它的代价。

    原话由代码按 `based_on` 的编号从要点表回填 —— 模型抄一遍只会引入错字，
    程序去主表取才是准的（同 validate 的设计原则）。
    """
    by_id = _points_by_id(evidence)
    lines = ["# 内容标题 / 切入角度备选", "", _banner(transcript, srt_name), ""]

    for i, angle in enumerate(angles.get("angles", []), 1):
        lines += [f"## 角度 {i}：{angle.get('title', '')}", ""]
        lines.append(f"**主张**：{angle.get('claim', '—')}")
        lines.append("")
        lines.append("**依据**：")
        for pid in angle.get("based_on") or []:
            point = by_id.get(pid)
            if not point:
                lines.append(f"- `{pid}`（要点表中查无此条）")
                continue
            claim = point.get("claim") or {}
            lines.append(f"- 「{claim.get('quote', '')}」（{_ref(claim)}）")
            qualifier = point.get("qualifier")
            if qualifier:
                lines.append(
                    f"  - ⚠️ 带限定语，引用时必须一并带上："
                    f"「{qualifier.get('quote', '')}」"
                )
        lines.append("")
        lines.append(f"**软肋**：{angle.get('weakness', '—')}")
        lines.append("")

    rejected = angles.get("rejected") or []
    if rejected:
        lines += ["---", "", "## 想过但砍掉的", "", "| 候选 | 为什么砍 |", "|---|---|"]
        for item in rejected:
            lines.append(
                f"| {_cell(item.get('candidate', ''))} | {_cell(item.get('reason', ''))} |"
            )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. outline —— 搭
# ---------------------------------------------------------------------------

def render_outline(
    outline: dict, evidence: dict, transcript: Transcript, srt_name: str
) -> str:
    """文章结构与关键要点 = 一棵标题树，照已发布样稿的形状。

    不摆原话：原话在 evidence.md，编辑顺着 P 编号翻过去查。
    在这儿抄一遍，这份文件就变成了第二张证据表。
    """
    lines = ["# 文章结构与关键要点", "", _banner(transcript, srt_name), ""]
    lines += [f"> `P` 编号对应 `evidence.md`，原话去那里查。", "", "---", ""]

    used: set[str] = set()
    for section in outline.get("sections", []):
        lines += [f"## {section.get('no', '')}、{section.get('heading', '')}", ""]
        for sub in section.get("subs") or []:
            lines.append(f"**{sub.get('no', '')}.{sub.get('heading', '')}**")
            lines.append("")
            for kp in sub.get("points") or []:
                pid = kp.get("from", "")
                used.add(pid)
                lines.append(f"- {kp.get('text', '')} `{pid}`")
                if kp.get("qualifier_note"):
                    lines.append(f"  - ⚠️ 限定语：{kp['qualifier_note']}")
            lines.append("")

    # 没进这篇结构的要点只用一行交代，不做成表格：
    # 它是给编辑的一个指针（"还有这些，你可能想捡回来"），不是需要逐条读的清单。
    by_id = _points_by_id(evidence)
    unused = sorted(set(by_id) - used)
    if unused:
        tail = (
            f"未进入本结构的要点：{'、'.join(f'`{p}`' for p in unused)}"
            f" —— 见 `evidence.md`。"
        )
        # 但"漏掉的是哪一类"值得当场说清：编辑看到一串编号的第一反应是
        # "我是不是丢了东西"，而无出处的要点本来就不该在核实前进正文。
        # 不说，他得回 evidence.md 逐条查一遍才能得出同一个结论。
        no_src = [p for p in unused if by_id[p].get("verification") == "无出处"]
        if no_src:
            which = (
                "**全部都是**" if len(no_src) == len(unused)
                else f"其中 {'、'.join(f'`{p}`' for p in no_src)} 是"
            )
            tail += (
                f"\n\n{which}「无出处」的说法，已在 `editorial_checks.md` 的待核清单里；"
                f"**核实前不建议写进正文**。"
            )
        lines += ["---", "", tail, ""]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. evidence —— 查
# ---------------------------------------------------------------------------

FACT_TYPES = {"事实"}


def render_evidence(evidence: dict, transcript: Transcript, srt_name: str) -> str:
    """关键观点对应的原始素材证据。

    `support` 字段对两类要点问的是两个不同的问题，渲染必须跟着分叉：

    - `事实` 类：support 是「他拿什么证据支持这个数字」。空 = 红灯，必须核实。
    - 其余类：  support 是「他为什么这么认为」。空是正常的 —— 一个定义式判断
      本来就不需要外部背书。早先版本对两者一律打「纯断言」警告，是范畴错误：
      它让"100 美元没出处"和"企业家就是能动性的同义词"长得一模一样，
      而区分这两件事恰恰是这个工具最该做的事。
    """
    lines = ["# 关键观点与原始素材证据", "", _banner(transcript, srt_name), ""]
    lines += [
        "- **观点原话**：他主张了什么。",
        "- **限定语**：他自己给这个主张打的折扣。⚠️ 引用该主张时必须一并带上。",
        "- **支撑**：`事实` 类问的是「有什么证据」，其余类问的是「他为什么这么认为」。",
        "- 说话人、字幕序号、时间戳由程序从逐字稿反查得出，不由模型填写。",
        "",
        "---",
        "",
    ]

    for point in evidence.get("points", []):
        pid = point["point_id"]
        ptype = point.get("type", "?")
        is_fact = ptype in FACT_TYPES
        lines += [f"### {pid} {point.get('title', '')}", ""]
        lines.append(
            f"- 类型：{ptype}｜核实状态：{point.get('verification', '?')}"
        )

        claim = point.get("claim") or {}
        lines.append(f"- **观点原话**：「{claim.get('quote', '')}」（{_ref(claim)}）")

        qualifier = point.get("qualifier")
        if qualifier:
            lines.append(
                f"- **限定语**：「{qualifier.get('quote', '')}」（{_ref(qualifier)}）"
            )
            lines.append("  - ⚠️ 引用本条主张时必须同时带上该限定语。")

        if point.get("why"):
            lines.append(f"- 为什么值得写：{point['why']}")

        support = point.get("support") or []
        header = "有什么证据" if is_fact else "他为什么这么认为"
        lines += ["", f"| # | {header} | 说话人 | 字幕 | 时间戳 |", "|---|---|---|---|---|"]
        if support:
            for i, item in enumerate(support, 1):
                lines.append(
                    f"| {i} | {_cell(item.get('quote'))} | {item.get('speaker', '?')} | "
                    f"{item.get('cue', '?')} | {item.get('ts', '?')} |"
                )
        else:
            lines.append("| — | **无** | — | — | — |")
            lines.append("")
            if is_fact:
                lines.append(
                    "> 🔴 **这是一条事实性说法，但全篇没有任何东西托着它。**"
                    "写进正文前必须找到出处，见 `editorial_checks.md`。"
                )
            else:
                lines.append(
                    "> 说话人直接下了这个判断，没有展开理由。"
                    "不是缺陷，但编辑扩写时需要自己补论证。"
                )
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 4. reuse —— 补
# ---------------------------------------------------------------------------

def render_reuse(reuse: dict, transcript: Transcript, srt_name: str) -> str:
    """只摆能用的：哪一篇 / 用哪一段 / 放在哪一节。

    不相关的笔记不列 —— 编辑不会因为知道 note_05 讲的是组织管理而做出不同决定。
    唯一保留的"不能用"是同源：它主题完全对口，编辑自己翻库一定会翻到并且会想用，
    不拦住就会出循环佐证。所以它在顶部一行报掉，而不是当成一条建议。
    """
    lines = ["# 历史内容复用建议", "", _banner(transcript, srt_name), ""]

    excluded = reuse.get("excluded_same_source") or []
    for item in excluded:
        lines += [
            f"> ⛔ **`{item['note_id']}` 与本次逐字稿同源**"
            f"（字符 8-gram 覆盖率 {item['coverage']:.4f}），"
            f"是同一场访谈的另一个整理版本，已排除出候选池。",
            "> 引用它等于拿素材佐证素材，还会制造「历史上已有观点支持」的错觉。",
            "",
        ]
    if reuse.get("candidate_count") is not None:
        lines += [f"本次候选 {reuse['candidate_count']} 篇。", ""]
    lines += ["---", ""]

    icon = {"可用": "✅", "有限复用": "⚠️"}
    suggestions = reuse.get("suggestions") or []
    for item in suggestions:
        verdict = item.get("verdict", "?")
        lines += [
            f"## {icon.get(verdict, '•')} {item.get('note_id', '?')} —— {verdict}",
            "",
        ]
        if item.get("place_in_article"):
            lines.append(f"**放在**：{item['place_in_article']}")
            lines.append("")

        for seg in item.get("matched_segments") or []:
            lines.append(f"**用这段**（`{seg.get('seg_id', '?')}`）：")
            lines.append("")
            lines.append(f"> {seg.get('claim', '')}")
            lines.append("")
            lines.append(
                f"对应访谈 {seg.get('matched_point', '—')}：{seg.get('why', '')}"
            )
            lines.append("")

        if item.get("how_to_use"):
            lines.append(f"- **怎么用**：{item['how_to_use']}")
        if item.get("how_not_to_use"):
            lines.append(f"- **不能这么用**：{item['how_not_to_use']}")
        if item.get("limit"):
            lines.append(f"- **限制**：{item['limit']}")
        if item.get("quote_scope_verbatim"):
            lines.append(f"- **可引用范围**（原文）：{item['quote_scope_verbatim']}")
        lines.append("")

    if not suggestions:
        lines += [
            "（无可复用的历史内容）",
            "",
            reuse.get("no_match_reason") or reuse.get("search_summary") or "",
            "",
        ]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 5. draft —— 写
# ---------------------------------------------------------------------------

def render_draft(
    draft: dict, transcript: Transcript, srt_name: str, title: str
) -> str:
    """可供编辑继续修改的内容初稿 v0.5。

    不再输出字数越界警告：题目原文写 500–800，但已向出题方确认「合理即可」，
    区间因此只是 prompt 里的语感参照，不是需要向编辑报备偏离量的门禁。

    也不输出「模型自检」和「留给编辑扩写的要点」—— 前者是写给评审看的，
    后者与 outline 里未进入结构的那行重复。
    """
    body = draft.get("body_markdown", "")
    lines = [f"# {title}", "", _banner(transcript, srt_name), ""]
    lines += [
        f"> v0.5 初稿，正文 {count_draft_chars(body)} 字。编辑在此基础上往上加。",
        "> 方括号内是内联证据标记 `[要点编号 字幕序号 时间戳 说话人]`，"
        "顺着编号可回 `evidence.md` 查原话。",
        "",
        "---",
        "",
        body,
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 6. checks —— 核
# ---------------------------------------------------------------------------

def _desensitization_rows(evidence: dict) -> list[str]:
    """脱敏口径不一致：材料方声明已脱敏，但同一份逐字稿里两种口径并存。

    工具不替业务方决定往哪边统一，也绝不做反向识别（README 使用边界明令禁止）。
    """
    gaps = evidence.get("desensitization_gaps")
    if gaps is None:
        return ["> ⚠️ 本次抽取结果缺该字段，需重跑 Stage 2（不带 --cache）。", ""]
    if not gaps:
        return ["（模型未检出 —— 此项漏报无法自动发现，建议人工抽查）", ""]

    lines = ["| 类别 | 名称 | 出现字幕 | 说明 |", "|---|---|---|---|"]
    for g in gaps:
        cues = "、".join(map(str, g.get("cues") or [])) or "—"
        lines.append(
            f"| {g.get('kind', '—')} | {_cell(str(g.get('name', '')))} | {cues} | "
            f"{_cell(g.get('note'))} |"
        )
    lines.append("")
    return lines


def render_checks(
    evidence: dict,
    reuse: dict,
    soft_warnings: list[str],
    draft_warnings: list[str],
    transcript: Transcript,
    srt_name: str,
) -> str:
    """需要编辑人工确认的事实、风险与缺失信息。

    分节严格按题目原话的三类。每一条都必须带**动作** —— 只报警不给解法，
    等于把判断又推回给编辑，而这个文件存在的意义正是替他把判断的位置标出来。

    不收录时间戳重叠、超短片段这类转录层噪音：编辑读这份文件是为了防止错写漏写，
    "字幕 24 时长 1.0 秒"不会让他做出任何不同的决定。
    """
    points = evidence.get("points", [])
    lines = ["# 需要编辑人工确认的事实、风险与缺失信息", "", _banner(transcript, srt_name), ""]
    lines += [
        "工具不做事实核实 —— 没有联网，没有权威源，AI 核实等于编造置信度。",
        "它只做一件事：**把需要人判断的地方找出来、标好位置、给出该做什么**。",
        "",
        "---",
        "",
    ]

    # ---- 一、事实 ----
    lines += ["## 一、待核事实", ""]
    rows = []
    for point in points:
        verification = point.get("verification")
        if verification not in {"无出处", "需核实"}:
            continue
        claim = point.get("claim") or {}
        no_support = not (point.get("support") or [])
        if verification == "无出处" and no_support:
            action = "全篇无出处。要用只能写成「嘉宾称」，不能作为论断依据，不得进标题结尾。"
        elif verification == "无出处":
            action = "说话人给了理由但没给出处。核到原始来源再决定是否作为事实引用。"
        else:
            action = "说话人给了可查线索，请找到原始出处后再引用。"
        rows.append((point["point_id"], verification, claim, action))

    if rows:
        lines += ["| 要点 | 状态 | 说法原话 | 位置 | 编辑要做什么 |",
                  "|---|---|---|---|---|"]
        for pid, status, claim, action in rows:
            lines.append(
                f"| {pid} | **{status}** | {_cell(claim.get('quote'))} | "
                f"{_ref(claim)} | {action} |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    # ---- 二、风险 ----
    lines += ["## 二、风险", "", "不是事实对错的问题，是「这么写会不会出事」的问题。", ""]

    doubts = evidence.get("speaker_doubts") or []
    lines += ["### 2.1 说话人归属存疑", "",
              "引错说话人是发布事故。原则：能消歧就消歧，消不了就标记，**不硬猜**。", ""]
    if doubts:
        lines += ["| 字幕 | 逐字稿标注 | 疑似实为 | 依据 | 编辑要做什么 |",
                  "|---|---|---|---|---|"]
        for d in doubts:
            lines.append(
                f"| {d.get('cue', '?')} | {d.get('labeled', '?')} | "
                f"{d.get('suspected', '?')} | {_cell(d.get('reason'))} | "
                f"引用这条前回听原始录音确认；无法确认则不要引用 |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    promos = evidence.get("promo_segments") or []
    lines += ["### 2.2 具有推广口播特征的段落", "",
              "⚠️ 判据是**表达形式**（单向连续陈述、不含问句、带行动号召、与访谈主线无关），"
              "**不能据此断言任何商业关系或利益往来**。", "",
              "**编辑要做什么**：不作为观点素材进入正文。", ""]
    if promos:
        for p in promos:
            lines.append(
                f"- 字幕 {'、'.join(map(str, p.get('cues') or []))}："
                f"「{p.get('quote', '')}」"
            )
    else:
        lines.append("（无）")
    lines.append("")

    lines += ["### 2.3 脱敏口径不一致", "",
              "材料包声明可识别信息已脱敏，但下列名称在逐字稿中**原样出现**，"
              "而同类的另一些却已被泛化。", "",
              "**编辑要做什么**：口径由编辑或法务定。工具不替业务方决定往哪边统一，"
              "也不做反向识别（不猜被泛化掉的是谁、不补网址）。", ""]
    lines += _desensitization_rows(evidence)

    excluded = reuse.get("excluded_same_source") or []
    if excluded:
        lines += ["### 2.4 同源历史素材", ""]
        for item in excluded:
            lines.append(
                f"- `{item['note_id']}` 覆盖率 {item['coverage']:.4f}，"
                f"是本次访谈的另一个整理版本，已排除。"
            )
        lines += ["",
                  "**编辑要做什么**：不要把它当作独立的历史观点引用 —— "
                  "那构成循环佐证，会让读者以为存在外部支持。", ""]

    # ---- 三、缺失信息 ----
    lines += ["## 三、缺失信息", "",
              "材料包不提供原始视频或分镜稿。以下位置因缺画面或指代不明而无法确定含义。", ""]
    missing = evidence.get("missing_info") or []
    if missing:
        lines += ["| 字幕 | 缺什么 | 编辑要做什么 |", "|---|---|---|"]
        for m in missing:
            lines.append(
                f"| {m.get('cue', '?')} | {_cell(m.get('detail'))} | "
                f"回原始录音/视频确认；确认不了就不要写进正文 |"
            )
    else:
        lines.append("（无）")
    lines.append("")

    terms = evidence.get("terminology") or []
    if terms:
        lines += ["### 术语不统一", "",
                  "**编辑要做什么**：选定一种写法后全文统一。工具不自行统一。", "",
                  "| 并列写法 | 出现字幕 | 说明 |", "|---|---|---|"]
        for t in terms:
            lines.append(
                f"| {' / '.join(t.get('variants') or [])} | "
                f"{'、'.join(map(str, t.get('cues') or []))} | {_cell(t.get('note'))} |"
            )
        lines.append("")

    hook = evidence.get("opening_hook") or {}
    if hook.get("mapping"):
        lines += ["### 片头预告与正片的对应", "",
                  "播客片头把后面的片段剪到开头做钩子，剪掉了原本的语境。", "",
                  "**编辑要做什么**：引用一律指向正片位置，不引片头。", "",
                  "| 片头字幕 | 正片字幕 | 依据 |", "|---|---|---|"]
        for m in hook["mapping"]:
            lines.append(
                f"| {m.get('hook_cue', '?')} | {m.get('main_cue', '?')} | "
                f"{_cell(m.get('reason'))} |"
            )
        lines.append("")

    # ---- 附：工具自身的剔除与更正 ----
    # 保留但降级为附录：编辑需要知道"工具丢了什么"（不做静默删除），
    # 但这不是他发布前要处理的事，不该占据正文位置。
    dropped = evidence.get("_dropped") or []
    all_warnings = (soft_warnings or []) + (draft_warnings or [])
    if dropped or all_warnings:
        lines += ["---", "", "## 附：工具的剔除与自动更正记录", "",
                  "程序按逐字稿覆盖或剔除了模型输出的地方。"
                  "记在这里是因为**工具不做静默删除**。", ""]
        for item in dropped:
            lines.append(
                f"- 剔除{item.get('scope', '')} {item.get('point_id', '')}："
                f"「{_cell(item.get('quote'))[:40]}…」—— {item.get('reason', '')}"
            )
        for w in all_warnings:
            lines.append(f"- {w}")
        lines.append("")

    return "\n".join(lines)
