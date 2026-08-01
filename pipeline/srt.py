"""Stage 1：逐字稿解析（纯代码，无 AI）。

这一步的正确性是整条链路溯源的地基，不能有概率性 —— 所以不用模型。
顺带做三项确定性的转录异常检测，结果透传给 editorial_checks.md。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TS_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)
SPEAKER_RE = re.compile(r"^([^：:]{1,12})[：:]\s*(.*)$", re.S)

# 片头钩子区间（PRD §2.2⑤）。播客把后面的精彩片段剪到开头做预告，
# 这些位置不作为证据。区间由人工勘察确定，不做算法检测。
OPENING_HOOK_CUES = set(range(1, 8))  # 字幕 1–7，00:00:00–00:01:07


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def fmt_ts(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


@dataclass
class Cue:
    """一条字幕。注意：一个字幕序号内可能含多句话（本篇 36 号含十余句）。"""

    index: int
    start: float
    end: float
    speaker: str
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def start_ts(self) -> str:
        return fmt_ts(self.start)

    @property
    def end_ts(self) -> str:
        return fmt_ts(self.end)

    def contains_ts(self, seconds: float, tolerance: float = 1.0) -> bool:
        return (self.start - tolerance) <= seconds <= (self.end + tolerance)

    def to_dict(self) -> dict:
        return {
            "cue": self.index,
            "start": self.start_ts,
            "end": self.end_ts,
            "speaker": self.speaker,
            "text": self.text,
        }


@dataclass
class TranscriptIssue:
    kind: str
    cues: list[int]
    detail: str


@dataclass
class Transcript:
    cues: list[Cue]
    issues: list[TranscriptIssue] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.cues)

    def get(self, index: int) -> Cue | None:
        for cue in self.cues:
            if cue.index == index:
                return cue
        return None

    @property
    def speakers(self) -> list[str]:
        seen: list[str] = []
        for cue in self.cues:
            if cue.speaker not in seen:
                seen.append(cue.speaker)
        return seen

    @property
    def total_chars(self) -> int:
        return sum(len(c.text) for c in self.cues)

    @property
    def duration_minutes(self) -> float:
        return (self.cues[-1].end - self.cues[0].start) / 60 if self.cues else 0.0

    def as_prompt_block(self) -> str:
        """喂给模型的全文形态。带序号与时间戳，模型才能给出可校验的证据位置。"""
        lines = []
        for cue in self.cues:
            lines.append(
                f"[{cue.index}] {cue.start_ts}-{cue.end_ts} {cue.speaker}：{cue.text}"
            )
        return "\n".join(lines)


def parse_srt(path: Path) -> Transcript:
    raw = path.read_text(encoding="utf-8-sig")
    blocks = [b for b in re.split(r"\r?\n\s*\r?\n", raw.strip()) if b.strip()]

    cues: list[Cue] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if len(lines) < 2:
            continue
        idx_line, ts_line, *body = lines
        m = TS_RE.search(ts_line)
        if not m:
            # 有些文件把序号和时间戳写在同一行，容错一次
            m = TS_RE.search(idx_line)
            if not m:
                continue
            body = lines[1:]
            index = len(cues) + 1
        else:
            index = int(re.sub(r"\D", "", idx_line) or len(cues) + 1)

        start = _to_seconds(*m.group(1, 2, 3, 4))
        end = _to_seconds(*m.group(5, 6, 7, 8))
        text = "\n".join(body).strip()

        speaker = "未知"
        sm = SPEAKER_RE.match(text)
        if sm:
            speaker, text = sm.group(1).strip(), sm.group(2).strip()

        cues.append(Cue(index=index, start=start, end=end, speaker=speaker, text=text))

    transcript = Transcript(cues=cues)
    transcript.issues = detect_issues(cues)
    return transcript


# ---------------------------------------------------------------------------
# 转录异常检测（确定性，不依赖模型）
# ---------------------------------------------------------------------------

def _shingles(text: str, n: int = 8) -> set[str]:
    clean = re.sub(r"[\s，。、！？；：“”‘’（）《》…—\-,.!?;:\"'()]", "", text)
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def jaccard(a: str, b: str, n: int = 8) -> float:
    sa, sb = _shingles(a, n), _shingles(b, n)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


ULTRA_SHORT_SEC = 1.0
# 实测：97 条字幕两两比对，>0.10 的只有 (4,11) 一对，正是片头预告与正片的重复段。
# 阈值仅用于"给编辑提示重复"，不参与任何排除动作 —— 片头排除靠抽取 prompt。
DUPLICATE_THRESHOLD = 0.10


def detect_issues(cues: list[Cue]) -> list[TranscriptIssue]:
    issues: list[TranscriptIssue] = []

    for prev, cur in zip(cues, cues[1:]):
        if cur.start < prev.end:
            issues.append(
                TranscriptIssue(
                    kind="时间戳重叠",
                    cues=[prev.index, cur.index],
                    detail=(
                        f"字幕 {prev.index} 结束于 {prev.end_ts}，"
                        f"字幕 {cur.index} 却起于 {cur.start_ts}"
                    ),
                )
            )

    for cue in cues:
        if cue.duration <= ULTRA_SHORT_SEC:
            issues.append(
                TranscriptIssue(
                    kind="超短片段",
                    cues=[cue.index],
                    detail=f"时长 {cue.duration:.1f}s，疑为断句而非完整表达",
                )
            )

    for i, a in enumerate(cues):
        for b in cues[i + 1 :]:
            score = jaccard(a.text, b.text)
            if score >= DUPLICATE_THRESHOLD:
                issues.append(
                    TranscriptIssue(
                        kind="内容近似重复",
                        cues=[a.index, b.index],
                        detail=(
                            f"字符 8-gram Jaccard={score:.3f}；"
                            f"若其一位于片头（字幕 {sorted(OPENING_HOOK_CUES)[0]}–"
                            f"{sorted(OPENING_HOOK_CUES)[-1]}），应以正片位置为证据"
                        ),
                    )
                )

    return issues
