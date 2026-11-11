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


ROLE_ENV = {"reviewer": "MINI_REVIEW_"}  # 复核员可单独指定模型：MINI_REVIEW_MODEL / _BASE_URL / _MODEL_NAME / _API_KEY


def current_provider(model_cfg: dict[str, Any] | None = None, role: str | None = None) -> str:
    cfg = model_cfg or {}
    pre = ROLE_ENV.get(role or "")
    if pre and (os.environ.get(pre + "MODEL") or (cfg.get(role) or {}).get("provider")):
        return os.environ.get(pre + "MODEL") or cfg[role]["provider"]
    return os.environ.get("MINI_MODEL") or cfg.get("provider", "mock")


def resolved_config(model_cfg: dict[str, Any], role: str | None = None) -> dict[str, Any]:
    """合并顺序：预设 < employee.yaml 的 model 段 < 环境变量 / .env（MINI_BASE_URL、MINI_MODEL_NAME、MINI_API_KEY）。
    role（如 reviewer 复核员）：再叠加 model 段下同名子段与 MINI_REVIEW_* 环境变量；都没配置时与主模型相同。"""
    provider = current_provider(model_cfg, role)
    own = (model_cfg.get(role) or {}) if role else {}
    switched = bool(role) and provider != current_provider(model_cfg)  # 复核员换了服务商：不沿用主模型的地址与密钥
    base = {k: v for k, v in model_cfg.items() if k != "provider" and k not in ROLE_ENV}
    if switched:
        base = {k: v for k, v in base.items() if k in ("temperature", "timeout", "max_retries")}
    cfg = {**PRESETS.get(provider, {}), **base, **{k: v for k, v in own.items() if k != "provider"}}
    prefixes = ([] if switched else ["MINI_"]) + ([ROLE_ENV[role]] if role in ROLE_ENV else [])
    for pre in prefixes:
        if os.environ.get(pre + "BASE_URL"):
            cfg["base_url"] = os.environ[pre + "BASE_URL"]
        if os.environ.get(pre + "MODEL_NAME"):
            cfg["model"] = os.environ[pre + "MODEL_NAME"]
        if os.environ.get(pre + "API_KEY"):
            cfg["api_key"] = os.environ[pre + "API_KEY"]
    return {"provider": provider, **cfg}


def build_llm(model_cfg: dict[str, Any], faults: list[dict] | None = None, role: str | None = None) -> LLM:
    """MINI_MODEL 环境变量优先于员工配置；值为 mock / openai_compatible / deepseek / qwen / glm。
    role=reviewer 时按复核员配置（见 resolved_config），可与撰写模型不同。"""
    cfg = resolved_config(model_cfg, role)
    if cfg["provider"] == "mock":
        from .mock import MockLLM
        return MockLLM(faults=faults)
    from .openai_compat import OpenAICompatLLM
    missing = [k for k in ("base_url", "model") if not cfg.get(k)]
    if missing:
        raise LLMUnavailable(f"模型配置缺少 {missing}：请在 .env 中填写 MINI_BASE_URL / MINI_MODEL_NAME")
    return OpenAICompatLLM(faults=faults, **cfg)
