"""可选的 LangSmith 追踪：在 .env 中设置 LANGSMITH_TRACING=true 与 LANGSMITH_API_KEY 后开启（默认关闭）。

开启后：LangGraph 编排图、步骤图、模型步骤子图的每个节点自动上报为一棵调用树（LangGraph 内置支持），
模型调用经 langsmith.wrappers.wrap_openai 包装，记录提示词、输出、token 与耗时；每次运行带任务编号作为元数据，
在 LangSmith 中按 task_id 筛选即可看到一个任务的完整执行过程。
注意：开启后提示词与模型输出（含销售数据摘要、纪要内容）会发送到 LangSmith，敏感数据请用自建实例
（LANGSMITH_ENDPOINT）或设置 LANGSMITH_HIDE_INPUTS / LANGSMITH_HIDE_OUTPUTS 隐藏内容。
"""
from __future__ import annotations

import os
from typing import Any


def enabled() -> bool:
    flag = (os.environ.get("LANGSMITH_TRACING") or os.environ.get("LANGCHAIN_TRACING_V2") or "").lower()
    return flag in ("true", "1", "yes") and bool(os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"))


def wrap_client(client: Any) -> Any:
    """开启追踪时包装 OpenAI 客户端，使模型调用作为子节点出现在调用树中；未开启或未安装 langsmith 时原样返回。"""
    if not enabled():
        return client
    try:
        from langsmith.wrappers import wrap_openai
    except ImportError:
        return client
    return wrap_openai(client)


def run_config(tid: str, employee: str | None = None) -> dict[str, Any]:
    """图运行配置中的追踪信息（未开启追踪时也无副作用）。"""
    meta = {"task_id": tid, **({"employee": employee} if employee else {})}
    return {"run_name": f"task {tid}", "metadata": meta, "tags": ["mini-employee", *([employee] if employee else [])]}
