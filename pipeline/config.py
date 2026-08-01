"""运行配置：路径、.env 读取、以及所有 MVP 默认值的集中声明。

设计约定（对应 PRD §3.8）：
凡是没有业务校准依据的数值，一律放在本文件的 TBD / MVP 区块里，
不允许散落在业务代码中被当成常识使用。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 跨环境交付的第一道闸：先报版本，再谈别的。
# 3.9 是硬下限（本项目语法层已验证兼容到 3.8，但不承诺低于 3.9 的运行时行为）。
# 需要显式挡一道的原因：macOS 自带的 Xcode Command Line Tools 给的是
# Python 3.9.6，`python3` 默认指向它。没有这道闸时，报错会以某个
# 深处的 TypeError 形式出现，看不出根因是版本。
MIN_PYTHON = (3, 9)
TESTED_PYTHON = (3, 10)

if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        f"需要 Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} 或更高，"
        f"当前是 {sys.version.split()[0]}（{sys.executable}）。\n"
        f"macOS 如果 `python3` 指向系统自带版本，装一个新版：brew install python@3.12"
    )

ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = ROOT / "data"
STORE_DIR = ROOT / "store"
PROMPT_DIR = ROOT / "prompts"
OUT_DIR = ROOT / "outputs"
INTERMEDIATE_DIR = OUT_DIR / "_intermediate"

SRT_PATH = DATA_DIR / "interview_transcript.srt"
BRIEF_PATH = DATA_DIR / "content_brief.md"
NOTES_PATH = DATA_DIR / "historical_notes.jsonl"
SAMPLES_DIR = DATA_DIR / "published_samples"

SEGMENTS_PATH = STORE_DIR / "segments.jsonl"
NOTES_META_PATH = STORE_DIR / "notes_meta.jsonl"
STYLE_CARD_PATH = STORE_DIR / "style_card.md"
SAMPLES_RAW_PATH = STORE_DIR / "style_samples_raw.md"

OUTPUT_FILES = [
    "angles.md",
    "outline.md",
    "evidence.md",
    "reuse_suggestions.md",
    "draft_v0_5.md",
    "editorial_checks.md",
]


# --------------------------------------------------------------------------
# 题目写死的约束（不可改，改了就是不合题）
# --------------------------------------------------------------------------
ANGLE_COUNT = 3                  # 产出 3：3 个内容标题/切入角度备选
REUSE_MIN_COUNT = 1              # 产出 3：至少 1 条历史内容复用建议
DRAFT_MIN_CHARS = 500            # 产出 3 原文写的区间，现仅作为 prompt 里的语感参照
DRAFT_MAX_CHARS = 800

# 字数不是门禁。题目原文写「500–800 字」，但**已向出题方当面确认「合理即可」**，
# 这是出题方本人对原文的放宽，优先级高于 PDF 字面值。
#
# 因此代码不再做区间判定、不再输出偏离量警告、更不因字数重写。
# 只保留一个"是不是根本没写完"的地板：低于它说明模型中途截断或返回了残稿，
# 那是故障不是风格问题。上限不设 —— 编辑要的是能往上加的初稿，长一点不构成缺陷。
DRAFT_SANITY_MIN = 300

# 同源判定阈值（MVP 默认值）。
# 【实测】note_02 段表 quotes 对本次逐字稿覆盖率 0.8913，其余 7 篇 ≤ 0.0001。
#（拿 note_02 全文比是 0.9685；比的对象不同，值不同，见 PRD §2.6 的三数对照表。
#  run.py 走的是段表 quotes 这一条，所以这里记 0.8913。）
# 真实分布是三个数量级的鸿沟，0.30 落在鸿沟正中间，不是精调出来的值。
SAME_SOURCE_COVERAGE = 0.30


# --------------------------------------------------------------------------
# MVP 默认值 —— 无业务校准依据，交付物中必须如此标注（PRD §3.8）
# --------------------------------------------------------------------------
MVP_DEFAULTS = {
    "run_timeout_min": 10,       # 单次运行时长上限，拍的（待确认 C2）
    "llm_retry": 1,              # 单步失败重试次数
}

# --------------------------------------------------------------------------
# TBD 占位值 —— PRD §4 尚未定案，这里给的是"为了能跑"的临时值，不是结论。
# 每处使用点都会把该标记写进输出文件，避免被误读成业务真理。
# --------------------------------------------------------------------------
TBD_PLACEHOLDERS = {
    # TBD-B：evidence 要点数量。题目未规定，给一个宽区间让模型自己收敛。
    "evidence_point_range": (8, 16),
    # TBD-E：单篇关键段抽取上限。
    #   比例上限（10%）已证伪：10% × 37 万字 = 3.7 万字段表，超容量估算 4 倍。
    #   字数上限已取消：它是段数的从属量，两个上限在重复约束同一件事。
    #   段数上限保留 —— 【实测】不设上限时 note_01 抽出 34 段（占正文 15.1%），
    #   且出现重复 topic_tag 与非商业判断的碎片，即"缩写版全文"。
    #   取 10：现表每篇 11–12 段中站得住的约 8–9 条，12 是在放水。数值仍无业务依据。
    "segments_per_note": 10,
}

# 这两个占位值**不渲染进 outputs/**，声明集中在 PRD §3.8 与 README §九。
#
# 曾有一个 TBD_NOTICE 常量准备随输出一并声明，已删除：它的受众是评审与业务方，
# 不是编辑 —— 编辑当天不会因为"这个数字是拍的"而改任何一个字。
# 同一条判据也把时间戳重叠移出了 editorial_checks.md（见 render.py 模块 docstring）。


# --------------------------------------------------------------------------
# 入库分块（PRD §2.2 Stage 0a）
#
# 原则：**一篇笔记一次调用，能不切就不切。**
# 切块会让模型只能"在这 1/4 篇里挑 3 段"，而不是"读完整篇再挑 12 段" ——
# 它看不到全貌就做不了取舍，这和逐字稿不分段是同一个道理。
# 只有当单篇长到超出下面的 token 上限时才切，且必须按整句边界切。
#
# 上限取 250k token。目标模型（deepseek-v4-flash）上下文 1M，这里只用四分之一：
# 真正卡住的从来不是输入窗口，而是**输出预算**（推理模型的思考链，见 MAX_TOKENS）
# 和长上下文末端的注意力衰减。留这么大余量是为了以后来更长的笔记时不用改代码。
#
# 对当前 8 篇而言这个值是"永不触发" —— 最长的 note_05 才 5.5 万 token。
# 换句话说：本项目实际运行路径上没有任何分块，分块只是给未来留的降级出口。
# --------------------------------------------------------------------------
INGEST_MAX_TOKENS = 250_000

# 中文 token 比。实测：note_05 整篇 82,978 字 = 48,253 token → 1.72 字/token
#（短文本因 prompt 固定开销摊薄，比值会更高，最高见到 3.6）。
# 这里取保守值 1.5 —— 保守方向是**高估 token 数**，宁可早切也不要超窗。
CHARS_PER_TOKEN = 1.5

INGEST_CHUNK_CHARS = int(INGEST_MAX_TOKENS * CHARS_PER_TOKEN)   # 150,000 字


def load_dotenv(path: Path | None = None) -> None:
    """极简 .env 读取。不引第三方库 —— 依赖清单只允许两个（PRD §3.3）。

    用 utf-8-sig 而不是 utf-8：Windows 记事本、PowerShell `Out-File`、
    部分编辑器保存时会写入 UTF-8 BOM。按 utf-8 读会让首行的键变成
    `﻿OPENAI_API_KEY` —— key 明明填了，程序却报"缺少 API key"，
    而且肉眼看不出区别。这是跨环境交付里最难自查的一类故障。
    """
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _first_env(*names: str, default: str) -> str:
    """按顺序取第一个有值的环境变量。

    兼容两套命名：仓库里既有的 TEMPERATURE / MAX_RETRIES / REQUEST_TIMEOUT，
    以及带 LLM_ 前缀的写法。
    """
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return default


class ConfigError(RuntimeError):
    pass


class LLMConfig:
    """厂商无关的模型配置。两组环境变量任选其一。"""

    def __init__(self) -> None:
        load_dotenv()
        self.provider = os.environ.get("PROVIDER", "openai").strip().lower()

        if self.provider == "openai":
            self.base_url = os.environ.get(
                "OPENAI_BASE_URL", "https://api.openai.com/v1"
            ).rstrip("/")
            self.api_key = os.environ.get("OPENAI_API_KEY", "")
            self.model = os.environ.get("OPENAI_MODEL", "")
            self.json_mode = os.environ.get("OPENAI_JSON_MODE", "1") == "1"
        elif self.provider == "anthropic":
            self.base_url = os.environ.get(
                "ANTHROPIC_BASE_URL", "https://api.anthropic.com"
            ).rstrip("/")
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            self.model = os.environ.get("ANTHROPIC_MODEL", "")
            self.json_mode = False
        else:
            raise ConfigError(f"PROVIDER 只支持 openai / anthropic，收到：{self.provider}")

        self.max_tokens = int(_first_env("MAX_TOKENS", "LLM_MAX_TOKENS", default="8000"))
        self.temperature = float(_first_env("TEMPERATURE", "LLM_TEMPERATURE", default="0.3"))
        self.timeout = int(
            _first_env("REQUEST_TIMEOUT", "LLM_TIMEOUT", default="300")
        )
        self.max_retries = int(_first_env("MAX_RETRIES", default=str(MVP_DEFAULTS["llm_retry"] + 1)))

    def require(self) -> None:
        if not self.api_key:
            raise ConfigError(
                f"缺少 API key：请在 .env 中设置 "
                f"{'OPENAI_API_KEY' if self.provider == 'openai' else 'ANTHROPIC_API_KEY'}。"
                f"（可先用 --dry-run 检查 prompt，不需要 key）"
            )
        if not self.model:
            raise ConfigError("缺少模型名：请在 .env 中设置 OPENAI_MODEL / ANTHROPIC_MODEL。")


def setup_stdout() -> None:
    """Windows 控制台默认 GBK，中文输出会炸。统一切 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            # line_buffering：重定向到文件时也要逐行刷新，
            # 否则单次调用动辄两三分钟的流程看起来像卡死了
            stream.reconfigure(encoding="utf-8", line_buffering=True)  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
