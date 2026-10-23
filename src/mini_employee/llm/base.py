from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class LLMUnavailable(Exception):
    """模型接口不可用（超时 / 断网 / 限流），已重试仍失败。"""


class LLM(Protocol):
    name: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict], meta: dict[str, Any]) -> LLMResponse: ...
