"""OpenAI 兼容接口客户端（DeepSeek、通义千问、GLM 均提供该接口）。"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from .base import LLMResponse, LLMUnavailable, ToolCall


class OpenAICompatLLM:
    def __init__(self, provider: str, base_url: str, model: str, api_key_env: str = "MINI_API_KEY",
                 api_key: str | None = None, temperature: float = 0.2, timeout: float = 60, max_retries: int = 2,
                 faults: list[dict] | None = None, **_: Any):
        import openai

        key = api_key or os.environ.get(api_key_env)
        if not key:
            raise LLMUnavailable(f"未配置 API Key：请在 .env 中填写 MINI_API_KEY（或 {api_key_env}）")
        self.name = f"{provider}:{model}"
        self.model, self.temperature, self.max_retries = model, temperature, max_retries
        self.client = openai.OpenAI(api_key=key, base_url=base_url, timeout=timeout, max_retries=0)
        self._openai = openai
        self.faults = faults or []
        self.streaming = True

    def chat(self, messages: list[dict[str, Any]], tools: list[dict], meta: dict[str, Any]) -> LLMResponse:
        if any(f.get("tool") == "llm" for f in self.faults):
            raise LLMUnavailable("[故障注入] 模型接口连接超时")
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                kw: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": self.temperature}
                if tools:
                    kw["tools"] = tools
                if meta.get("on_delta") and self.streaming:  # 流式输出：边生成边回调，网页实时显示模型正在写的内容
                    try:
                        return self._stream(kw, meta["on_delta"])
                    except self._openai.BadRequestError:  # 个别服务不支持流式参数：改回普通调用，不影响结果
                        self.streaming = False
                r = self.client.chat.completions.create(**kw)
                msg = r.choices[0].message
                calls = [(tc.id, tc.function.name, tc.function.arguments) for tc in msg.tool_calls or []]
                usage = {"prompt_tokens": r.usage.prompt_tokens, "completion_tokens": r.usage.completion_tokens} \
                    if r.usage else {}
                return LLMResponse(msg.content, _calls(calls), usage)
            except (self._openai.APIConnectionError, self._openai.APITimeoutError, self._openai.RateLimitError,
                    self._openai.InternalServerError) as e:
                last = e
                time.sleep(0 if os.environ.get("MINI_FAST") else 2 ** attempt)
            except self._openai.APIStatusError as e:  # 401 密钥错误、400 参数不被支持等：重试无用，直接暂停并说明
                raise LLMUnavailable(f"模型接口返回 {e.status_code}：{_short(e)}") from e
        raise LLMUnavailable(f"模型接口不可用（已重试 {self.max_retries} 次）：{_short(last)}")

    def _stream(self, kw: dict[str, Any], on_delta: Any) -> LLMResponse:
        text, calls, usage = "", {}, {}
        for chunk in self.client.chat.completions.create(**kw, stream=True, stream_options={"include_usage": True}):
            if chunk.usage:
                usage = {"prompt_tokens": chunk.usage.prompt_tokens, "completion_tokens": chunk.usage.completion_tokens}
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            if d.content:
                text += d.content
                on_delta(text)
            for tc in d.tool_calls or []:  # 工具调用按 index 分片到达，拼接成完整调用
                c = calls.setdefault(tc.index, {"id": None, "name": "", "args": ""})
                c["id"] = tc.id or c["id"]
                if tc.function:
                    c["name"] += tc.function.name or ""
                    c["args"] += tc.function.arguments or ""
        return LLMResponse(text or None, _calls([(c["id"], c["name"], c["args"]) for _, c in sorted(calls.items())]),
                           usage)


def _calls(raw: list[tuple[Any, str, str | None]]) -> list[ToolCall]:
    out = []
    for cid, name, arguments in raw:
        try:
            args = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            args = {"__raw__": arguments}
        out.append(ToolCall(cid, name, args))
    return out


def _short(e: Exception | None, n: int = 200) -> str:
    s = str(e or "")
    return s if len(s) <= n else s[:n] + "…"
