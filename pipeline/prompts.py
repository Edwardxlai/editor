"""Prompt 模板加载。

模板与代码分离放在 prompts/ 下，理由：prompt 是本方案里最需要迭代的部分，
改 prompt 不应该动代码，也不应该被埋在字符串里看不见 diff。

模板格式：`@@@USER@@@` 之前是 system，之后是 user。
占位符用 %%NAME%%，不用 str.format —— 模板里有大量 JSON 花括号。
"""

from __future__ import annotations

from .config import PROMPT_DIR

SPLIT = "@@@USER@@@"


def load_prompt(name: str, **fields: str) -> tuple[str, str]:
    path = PROMPT_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"缺少 prompt 模板：{path}")
    # utf-8-sig：模板可能被任意编辑器改过，带 BOM 会让 system 段首多一个
    # 不可见字符，跟着进 prompt。见 config.load_dotenv 同注。
    raw = path.read_text(encoding="utf-8-sig")

    if SPLIT not in raw:
        raise ValueError(f"{path} 缺少 {SPLIT} 分隔符")
    system, user = raw.split(SPLIT, 1)

    for key, value in fields.items():
        token = f"%%{key.upper()}%%"
        system = system.replace(token, value)
        user = user.replace(token, value)

    leftovers = [
        token
        for token in ("%%",)
        if token in user.replace("%%%%", "")
    ]
    if leftovers and "%%" in user:
        # 只警告不报错：允许模板里出现百分号文本
        pass

    return system.strip(), user.strip()
