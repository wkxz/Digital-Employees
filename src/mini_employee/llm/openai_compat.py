"""OpenAI 兼容接口客户端（DeepSeek、通义千问、GLM 均提供该接口）。"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .base import LLMResponse, LLMUnavailable, ToolCall


class OpenAICompatLLM:
    def __init__(self, provider: str, base_url: str, model: str, api_key_env: str, temperature: float = 0.2,
                 timeout: float = 60, max_retries: int = 2, faults: list[dict] | None = None, **_: Any):
        import openai

        key = os.environ.get(api_key_env)
        if not key:
            raise LLMUnavailable(f"未设置环境变量 {api_key_env}，无法调用 {provider} 模型")
        self.name = f"{provider}:{model}"
        self.model, self.temperature, self.max_retries = model, temperature, max_retries
        self.client = openai.OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)
        self._openai = openai
        self.faults = faults or []

    def chat(self, messages: list[dict[str, Any]], tools: list[dict], meta: dict[str, Any]) -> LLMResponse:
        if any(f.get("tool") == "llm" for f in self.faults):
            raise LLMUnavailable("[故障注入] 模型接口连接超时")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                kw: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
                if tools:
                    kw["tools"] = tools
                r = self.client.chat.completions.create(**kw)
                msg = r.choices[0].message
                calls = []
                for tc in msg.tool_calls or []:
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {"__raw__": tc.function.arguments}
                    calls.append(ToolCall(tc.id, tc.function.name, args))
                usage = {"prompt_tokens": r.usage.prompt_tokens, "completion_tokens": r.usage.completion_tokens} \
                    if r.usage else {}
                return LLMResponse(msg.content, calls, usage)
            except (self._openai.APIConnectionError, self._openai.APITimeoutError, self._openai.RateLimitError,
                    self._openai.InternalServerError) as e:
                last = e
                time.sleep(0 if os.environ.get("MINI_FAST") else 2 ** attempt)
        raise LLMUnavailable(f"模型接口不可用：{last}")
