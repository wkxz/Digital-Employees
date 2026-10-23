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


def current_provider(model_cfg: dict[str, Any] | None = None) -> str:
    return os.environ.get("MINI_MODEL") or (model_cfg or {}).get("provider", "mock")


def resolved_config(model_cfg: dict[str, Any]) -> dict[str, Any]:
    """合并顺序：预设 < employee.yaml 的 model 段 < 环境变量 / .env（MINI_BASE_URL、MINI_MODEL_NAME、MINI_API_KEY）。"""
    provider = current_provider(model_cfg)
    cfg = {**PRESETS.get(provider, {}), **{k: v for k, v in model_cfg.items() if k != "provider"}}
    if os.environ.get("MINI_BASE_URL"):
        cfg["base_url"] = os.environ["MINI_BASE_URL"]
    if os.environ.get("MINI_MODEL_NAME"):
        cfg["model"] = os.environ["MINI_MODEL_NAME"]
    if os.environ.get("MINI_API_KEY"):
        cfg["api_key"] = os.environ["MINI_API_KEY"]
    return {"provider": provider, **cfg}


def build_llm(model_cfg: dict[str, Any], faults: list[dict] | None = None) -> LLM:
    """MINI_MODEL 环境变量优先于员工配置；值为 mock / openai_compatible / deepseek / qwen / glm。"""
    cfg = resolved_config(model_cfg)
    if cfg["provider"] == "mock":
        from .mock import MockLLM
        return MockLLM(faults=faults)
    from .openai_compat import OpenAICompatLLM
    missing = [k for k in ("base_url", "model") if not cfg.get(k)]
    if missing:
        raise LLMUnavailable(f"模型配置缺少 {missing}：请在 .env 中填写 MINI_BASE_URL / MINI_MODEL_NAME")
    return OpenAICompatLLM(faults=faults, **cfg)
