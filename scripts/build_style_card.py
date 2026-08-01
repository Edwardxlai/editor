"""Stage 0b：样稿解析 → 结构画像。

跑一次，产出 store/style_samples_raw.md（机器提取的结构证据）。
store/style_card.md 由人根据这份证据定稿并入库 —— 3 篇样稿只能支撑
"暂定 style card"，不足以证明这是固定规范（PRD §2.2 Stage 0b、假设 C3），
所以这一步不允许模型自动生成结论。

用法：python scripts/build_style_card.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import config
from pipeline.docx_read import profile, read_docx


def main() -> int:
    config.setup_stdout()
    samples = sorted(config.SAMPLES_DIR.glob("*.docx"))
    if not samples:
        print(f"未找到样稿：{config.SAMPLES_DIR}")
        return 1

    config.STORE_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [
        "# 已发布样稿结构画像（机器提取，不含结论）",
        "",
        "> 由 `scripts/build_style_card.py` 生成。",
        "> 结论性的风格约定见 `store/style_card.md`，由人定稿。",
        "",
    ]

    for path in samples:
        paragraphs, method = read_docx(path)
        info = profile(paragraphs)
        print(f"{path.name}: {method}，{info['非空段落']} 段，{info['字数']} 字")

        lines += [
            f"## {path.name}",
            "",
            f"- 读取方式：{method}",
            f"- 非空段落：{info['非空段落']}",
            f"- 字数：{info['字数']}",
            f"- 首行：{info['标题行']}",
        ]
        if info["疑似元信息行"]:
            lines.append("- 元信息行：")
            lines += [f"  - {p}" for p in info["疑似元信息行"]]
        if info["一级章节"]:
            lines.append("- 一级章节标题：")
            lines += [f"  - {p}" for p in info["一级章节"]]
        if info["二级章节"]:
            lines.append("- 二级章节标题（前 20）：")
            lines += [f"  - {p}" for p in info["二级章节"]]
        if info["疑似尾注"]:
            lines.append("- 尾注：")
            lines += [f"  - {p}" for p in info["疑似尾注"]]
        lines.append("")

    config.SAMPLES_RAW_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已写出：{config.SAMPLES_RAW_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
