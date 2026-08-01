"""文本工具：原话定位、整句切分、字数统计。

`locate_quote` 是整个"可溯源"承诺的技术底座：
模型给出的每一句引用，都必须能在原文里被这个函数字面命中，命中不了就判失败。
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

# 引用里允许出现的省略号（模型用它省略中间部分），切分后逐段验证
ELLIPSIS_RE = re.compile(r"(?:\.{3,}|。{3,}|…+|・{3,})")

MIN_FRAGMENT = 6  # 短于此长度的片段不参与验证，避免"的"这类噪音假命中


@lru_cache(maxsize=4096)
def is_ignorable(ch: str) -> bool:
    """归一化时抹掉的字符：空白 + 所有标点 + 所有符号。

    用 Unicode 类别判断而不是手写字符集 —— 手写集必然漏。
    实测教训：模型这一次把原文的「“企业家”」写成「‘企业家’」，
    弯引号不在手写集里，于是一句真实存在的原话被判成了杜撰，整批打回。
    标点差异不构成"改写原话"，判断依据必须是完备的。
    """
    if ch.isspace():
        return True
    return unicodedata.category(ch)[0] in ("P", "S")


def normalize(text: str) -> str:
    return "".join(ch for ch in (text or "") if not is_ignorable(ch))


def locate_quote(quote: str, haystack: str) -> bool:
    """判断 quote 是否字面出自 haystack。

    允许模型用省略号省略中间部分；每个片段都必须独立命中，且顺序必须一致。
    """
    if not quote or not haystack:
        return False
    hay = normalize(haystack)
    fragments = [normalize(f) for f in ELLIPSIS_RE.split(quote)]
    fragments = [f for f in fragments if len(f) >= MIN_FRAGMENT]
    if not fragments:
        # 整句都很短（比如「是啊」），退化为整串匹配
        whole = normalize(quote)
        return bool(whole) and whole in hay

    cursor = 0
    for frag in fragments:
        pos = hay.find(frag, cursor)
        if pos < 0:
            return False
        cursor = pos + len(frag)
    return True


def find_offset(quote: str, haystack: str) -> tuple[int, int] | None:
    """在原文中定位一段引用，返回字符区间。

    段表的 offset 由代码算出而不是模型给出 —— 模型给的偏移量无法校验，
    代码算的可以（PRD §2.2 Stage 0a）。
    """
    if not quote:
        return None
    direct = haystack.find(quote.strip())
    if direct >= 0:
        return (direct, direct + len(quote.strip()))

    # 标点差异导致直接查找失败时，按归一化位置反查
    norm_hay = []
    mapping = []
    for i, ch in enumerate(haystack):
        if not is_ignorable(ch):
            norm_hay.append(ch)
            mapping.append(i)
    joined = "".join(norm_hay)
    target = normalize(quote)
    if not target:
        return None
    pos = joined.find(target)
    if pos < 0:
        return None
    return (mapping[pos], mapping[min(pos + len(target) - 1, len(mapping) - 1)] + 1)


SENT_END = "。！？；!?;\n"


def split_sentences(text: str) -> list[str]:
    """按标点切出整句。

    段表切段必须落在整句边界上：自由边界的标注一致性极低，
    句子级边界才可复现（PRD §2.2 Stage 0a）。
    """
    sentences: list[str] = []
    buf: list[str] = []
    for ch in text:
        buf.append(ch)
        if ch in SENT_END:
            sentence = "".join(buf).strip()
            if sentence:
                sentences.append(sentence)
            buf = []
    tail = "".join(buf).strip()
    if tail:
        sentences.append(tail)
    return sentences


def chunk_by_sentence(text: str, limit: int) -> list[str]:
    """把长文按整句边界切成不超过 limit 字的块，且块与块之间大小均衡。

    为什么要均衡而不是贪心填满：贪心切法会在末尾剩一个很小的尾巴块，
    而每次调用的成本主要在输出（思考链）上，与输入长度关系不大。
    实测 note_04 被切出一个 1,117 token 的尾巴块，照样烧了 8,335 token 输出 ——
    20 次调用里有 3 次是这样白花的。

    做法：先算出至少需要几块，再按"总长 / 块数"作为目标大小填，
    而不是一路填到 limit 才换块。
    """
    sentences = split_sentences(text)
    total = sum(len(s) for s in sentences)
    if total <= limit:
        return ["".join(sentences)] if sentences else []

    n_chunks = -(-total // limit)          # 向上取整
    target = -(-total // n_chunks)         # 均摊后的目标块长

    chunks: list[str] = []
    buf: list[str] = []
    size = 0
    for sentence in sentences:
        # 已达目标且后面还有内容要装 → 换块（但绝不超过硬上限 limit）
        if buf and (size >= target or size + len(sentence) > limit):
            chunks.append("".join(buf))
            buf, size = [], 0
        buf.append(sentence)
        size += len(sentence)
    if buf:
        chunks.append("".join(buf))
    return chunks


# 内联证据标记：`[P01 字幕11 00:02:09 嘉宾B]`，也兼容不带要点编号的老写法。
# 必须能剔干净 —— 漏一种写法，标记文本就会被算进正文字数（19 处标记约 380 字）。
MARKER_RE = re.compile(r"\[\s*(?:P\d+\s+)?字幕\s*\d+[^\]]*\]")
MD_SYNTAX_RE = re.compile(r"[#*`>\-\|\[\]]")


def count_draft_chars(markdown: str) -> int:
    """初稿字数口径：剔除内联证据标记与 Markdown 语法后的非空白字符数。

    口径必须写死在代码里，否则"500–800 字"这个约束无法被复现地检验。
    """
    text = MARKER_RE.sub("", markdown)
    text = MD_SYNTAX_RE.sub("", text)
    return len(re.sub(r"\s", "", text))


CJK_ONLY_RE = re.compile(r"[^一-鿿]")


def _char_ngrams(text: str, n: int) -> set[str]:
    s = CJK_ONLY_RE.sub("", text or "")
    if len(s) < n:
        return set()
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def ngram_coverage(candidate: str, reference: str, n: int = 8) -> float:
    """candidate 有多大比例能在 reference 里找到（字符 n-gram 覆盖率）。

    用途：判断一篇历史笔记是不是本次逐字稿的另一个整理版本（同源）。

    为什么不用 Jaccard：两侧长度差异大时 Jaccard 会被长度稀释 ——
    一篇 1 万字的整理稿对 1.3 万字的逐字稿，即使逐字对应，Jaccard 也只有 0.8 出头，
    而"整理稿有多少内容来自这份逐字稿"才是我们真正要问的问题，这是个**有向**的量。

    为什么是代码算而不是问模型：这是可计算事实（同 §validate 的设计原则）。
    【实测】note_02 覆盖率 0.9122，其余 7 篇 ≤ 0.0001 —— 差距三个数量级，
    任何阈值都分得开，不存在需要模型"理解内容"才能判断的模糊地带。
    """
    grams = _char_ngrams(candidate, n)
    if not grams:
        return 0.0
    return len(grams & _char_ngrams(reference, n)) / len(grams)


def parse_ts(value: str) -> float | None:
    """接受 hh:mm:ss / mm:ss 两种写法。"""
    if not value:
        return None
    parts = value.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None
