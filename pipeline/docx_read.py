"""已发布样稿读取（Stage 0b）。

实测：sample_01 / sample_02 用 python-docx 正常；
sample_03 报 KeyError: 'NULL'，必须走 zipfile 直读 word/document.xml 兜底。
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _read_with_docx(path: Path) -> list[str]:
    import docx  # 延迟导入：只有解析样稿时才需要

    document = docx.Document(str(path))
    return [p.text.strip() for p in document.paragraphs]


def _read_with_zipfile(path: Path) -> list[str]:
    """python-docx 失败时的兜底：直接解 XML。

    只取 w:p 下的 w:t 文本，按段落聚合 —— 丢样式，但正文一个字不丢。
    """
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)

    paragraphs: list[str] = []
    for para in root.iter(f"{W_NS}p"):
        texts = [node.text or "" for node in para.iter(f"{W_NS}t")]
        paragraphs.append("".join(texts).strip())
    return paragraphs


def read_docx(path: Path) -> tuple[list[str], str]:
    """返回 (非空段落列表, 使用的读取方式)。"""
    try:
        paragraphs = _read_with_docx(path)
        method = "python-docx"
    except Exception as exc:  # noqa: BLE001 —— 兜底的意义就是接住任何异常
        paragraphs = _read_with_zipfile(path)
        method = f"zipfile 兜底（python-docx 报错：{type(exc).__name__}: {exc}）"
    return [p for p in paragraphs if p], method


HEADING_RE = re.compile(r"^\s*([一二三四五六七八九十]+)\s*[、.]")
SUBHEADING_RE = re.compile(r"^\s*(\d+)\s*[、.]")


def profile(paragraphs: list[str]) -> dict:
    """对一篇样稿做结构画像。用于人工归纳 style card，不产出结论。"""
    body = "".join(paragraphs)
    return {
        "非空段落": len(paragraphs),
        "字数": len(re.sub(r"\s", "", body)),
        "标题行": paragraphs[0] if paragraphs else "",
        "一级章节": [p for p in paragraphs if HEADING_RE.match(p)][:20],
        "二级章节": [p for p in paragraphs if SUBHEADING_RE.match(p)][:20],
        "疑似元信息行": [
            p for p in paragraphs[:8]
            if any(k in p for k in ("内容来源", "责编", "深度好文", "分钟阅读"))
        ],
        "疑似尾注": [
            p for p in paragraphs[-6:]
            if any(k in p for k in ("参考资料", "独立观点", "不代表"))
        ],
    }
