"""厂商无关的 LLM 客户端。

两条原则（PRD §3.5）：
1. **宁可报错，不可伪造**。任何一步失败都显式抛出，不用兜底文本掩盖。
2. **不落盘原文**。日志只记 token 数与耗时，不记请求体（假设 A2 的默认取值）。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from .config import MVP_DEFAULTS, LLMConfig


def _httpx():
    """延迟导入 httpx —— 只有真的要发请求时才需要它。

    这不是洁癖，是在兑现一句承诺：README 把 `run.py --dry-run` 写成
    "不需要 API key" 的审查入口，而 run.py 顶部就 import 了本模块。
    在顶层 import httpx 会让一台还没跑过 `pip install` 的机器连 --dry-run
    都起不来 —— 报的还是 ImportError，跟 prompt 审查毫无关系。
    延迟之后，"不装任何依赖也能跑 selftest / check / --dry-run" 成立。
    """
    try:
        import httpx
    except ImportError as exc:
        raise LLMError(
            "需要 httpx 才能调用模型：pip install -r requirements.txt\n"
            "（不调模型的路径无需依赖：scripts/selftest.py、check.py、run.py --dry-run）"
        ) from exc
    return httpx


class LLMError(RuntimeError):
    pass


class TruncatedError(LLMError):
    """输出被 max_tokens 截断。

    对推理模型（DeepSeek-V4 / o 系列等）这是最常见的失败方式：
    思考链吃光整个输出预算，正文返回空字符串。
    必须与"模型没按格式输出"区分开 —— 前者是配置问题，后者是 prompt 问题。
    """


@dataclass
class Usage:
    stage: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    elapsed: float

    def line(self) -> str:
        extra = f"（其中思考 {self.reasoning_tokens}）" if self.reasoning_tokens else ""
        return (
            f"[{self.stage}] in={self.prompt_tokens} out={self.completion_tokens}{extra} "
            f"{self.elapsed:.1f}s"
        )


class LLMClient:
    def __init__(self, cfg: LLMConfig | None = None) -> None:
        self.cfg = cfg or LLMConfig()
        self.usages: list[Usage] = []

    # -- 传输层 -------------------------------------------------------------

    def _post_openai(self, system: str, user: str, json_mode: bool) -> tuple[str, dict]:
        body = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
        }
        if json_mode and self.cfg.json_mode:
            body["response_format"] = {"type": "json_object"}

        resp = _httpx().post(
            f"{self.cfg.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.cfg.api_key}"},
            json=body,
            timeout=self.cfg.timeout,
        )
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        choice = data["choices"][0]
        usage = dict(data.get("usage", {}))
        usage["finish_reason"] = choice.get("finish_reason", "")
        usage["reasoning_tokens"] = (
            usage.get("completion_tokens_details", {}) or {}
        ).get("reasoning_tokens", 0)
        return choice["message"].get("content") or "", usage

    def _post_anthropic(self, system: str, user: str, json_mode: bool) -> tuple[str, dict]:
        body = {
            "model": self.cfg.model,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
        }
        resp = _httpx().post(
            f"{self.cfg.base_url}/v1/messages",
            headers={
                "x-api-key": self.cfg.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=self.cfg.timeout,
        )
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        usage = data.get("usage", {})
        return text, {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "reasoning_tokens": 0,
            "finish_reason": data.get("stop_reason", ""),
        }

    def complete(self, stage: str, system: str, user: str, json_mode: bool = True) -> str:
        self.cfg.require()
        started = time.time()
        if self.cfg.provider == "openai":
            text, usage = self._post_openai(system, user, json_mode)
        else:
            text, usage = self._post_anthropic(system, user, json_mode)
        self.usages.append(
            Usage(
                stage=stage,
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                reasoning_tokens=usage.get("reasoning_tokens", 0) or 0,
                elapsed=time.time() - started,
            )
        )

        if usage.get("finish_reason") in {"length", "max_tokens"} and not text.strip():
            raise TruncatedError(
                f"[{stage}] 输出被 max_tokens={self.cfg.max_tokens} 截断，正文为空"
                f"（思考消耗 {usage.get('reasoning_tokens', 0)} tokens）。"
                f"这是配置问题不是 prompt 问题：请调高 .env 里的 MAX_TOKENS，"
                f"或换用非推理模型。"
            )
        return text

    # -- JSON 层 ------------------------------------------------------------

    def complete_json(
        self,
        stage: str,
        system: str,
        user: str,
        validate=None,
        return_last_on_failure: bool = False,
        retry_hint: str = "",
    ) -> dict:
        """要 JSON，解析失败或校验不过就重试。

        `return_last_on_failure`：重试用尽后返回最后一次的结果，由调用方决定
        是整批放弃还是只剔除不合格的部分。**仅在调用方有能力剔除时才可打开** ——
        它不是"放宽校验"，是把处置权交给能做更细决策的那一层。
        """
        attempts = max(1, getattr(self.cfg, "max_retries", MVP_DEFAULTS["llm_retry"] + 1))
        last_error = ""
        last_data: dict | None = None
        prompt = user

        for attempt in range(attempts):
            # 截断是配置问题，重试只会重复烧钱 —— 直接抛出
            raw = self.complete(f"{stage}#{attempt + 1}", system, prompt, json_mode=True)
            try:
                data = extract_json(raw)
            except LLMError as exc:
                last_error = str(exc)
            else:
                if validate is None:
                    return data
                problems = validate(data)
                if not problems:
                    return data
                last_data = data
                last_error = "；".join(problems[:5])

            prompt = (
                f"{user}\n\n---\n"
                f"上一次输出不合格，原因：\n{last_error}\n\n"
                f"{retry_hint}\n"
                f"请重新输出**完整的、严格合法的 JSON**，不要任何解释文字、不要代码块围栏。"
            )

        if return_last_on_failure and last_data is not None:
            return last_data
        raise LLMError(f"[{stage}] 连续 {attempts} 次输出不合格：{last_error}")

    def report(self) -> str:
        if not self.usages:
            return "（无 LLM 调用）"
        total_in = sum(u.prompt_tokens for u in self.usages)
        total_out = sum(u.completion_tokens for u in self.usages)
        total_t = sum(u.elapsed for u in self.usages)
        lines = [u.line() for u in self.usages]
        lines.append(f"[合计] in={total_in} out={total_out} {total_t:.1f}s")
        return "\n".join(lines)


FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """从模型输出里挖出 JSON。容忍代码块围栏和前后废话，不容忍结构损坏。"""
    candidate = text.strip()

    fenced = FENCE_RE.search(candidate)
    if fenced:
        candidate = fenced.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    start = candidate.find("{")
    end = candidate.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"JSON 解析失败：{exc}") from exc

    raise LLMError("输出中找不到 JSON 对象")
