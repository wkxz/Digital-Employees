"""模型层：统一接口 + OpenAI 兼容客户端（DeepSeek / 通义千问 / GLM）+ 离线 Mock 模型。"""
from __future__ import annotations

import os
from typing import Any

from .base import LLM, LLMResponse, LLMUnavailable, ToolCall  # noqa: F401

PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "api_key_env": "DEEPSEEK_API_KEY"},
    "qwen": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
             "api_key_env": "DASHSCOPE_API_KEY"},
    "glm": {"base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-plus", "api_key_env": "ZHIPU_API_KEY"},
}


def build_llm(model_cfg: dict[str, Any], faults: list[dict] | None = None) -> LLM:
    """MINI_MODEL 环境变量优先于员工配置；值为 mock / deepseek / qwen / glm / openai_compatible。"""
    provider = os.environ.get("MINI_MODEL") or model_cfg.get("provider", "mock")
    if provider == "mock":
        from .mock import MockLLM
        return MockLLM(faults=faults)
    from .openai_compat import OpenAICompatLLM
    cfg = {**PRESETS.get(provider, {}), **{k: v for k, v in model_cfg.items() if k != "provider"}}
    if os.environ.get("MINI_MODEL_NAME"):
        cfg["model"] = os.environ["MINI_MODEL_NAME"]
    return OpenAICompatLLM(provider=provider, faults=faults, **cfg)
