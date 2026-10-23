"""Agent 步骤：在单个步骤内运行“模型 ⇄ 工具”循环，受轮次与时长上限约束。

模型只能：读输入、调用本步骤开放的工具、按 JSON Schema 输出结果。
模型不能：触发状态流转、调用未开放的工具、写文件。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable

import jsonschema

from ..tools.errors import BAD_ARGS, MISSING_INPUT, ToolDenied, ToolFailure
from .base import LLM

Emit = Callable[[str, str | None, dict[str, Any]], None]


class StepLimitExceeded(Exception):
    pass


class MissingInput(Exception):
    pass


def extract_json(text: str | None) -> Any:
    if not text:
        raise ValueError("模型没有输出内容")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


def run_agent_step(llm: LLM, gateway: Any, emit: Emit, *, step_id: str, purpose: str, system: str,
                   payload: dict[str, Any], tools: list[str], output_schema: dict[str, Any],
                   max_turns: int, max_seconds: float, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    schema_text = json.dumps(output_schema, ensure_ascii=False)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\n## 输出格式\n完成后只输出一个 JSON 对象（不要输出其他文字），"
                                      f"须符合以下 JSON Schema：\n{schema_text}"},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    tool_defs = gateway.model_tools(tools) if tools else []
    meta = {**(meta or {}), "step": step_id, "purpose": purpose}
    t0 = time.monotonic()
    for turn in range(1, max_turns + 1):
        if time.monotonic() - t0 > max_seconds:
            raise StepLimitExceeded(f"步骤用时超过上限 {max_seconds:.0f} 秒")
        last_turn = turn == max_turns and max_turns > 1
        if last_turn:  # 最后一轮：收回工具，要求基于已有信息直接给出结果，而不是继续检索
            messages.append({"role": "user", "content": "已到本步骤的最后一轮。不要再调用工具，请基于已获得的信息立即输出最终 JSON；"
                                                        "没有依据的原因写“原因待确认”。"})
        c0 = time.monotonic()
        resp = llm.chat(messages, [] if last_turn else tool_defs, {**meta, "turn": turn, "wrap_up": last_turn})
        ms = int((time.monotonic() - c0) * 1000)
        emit("model_called", step_id, {
            "model": llm.name, "purpose": purpose, "turn": turn, "ms": ms, "usage": resp.usage,
            "prompt_sha": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()[:12],
            "tool_calls": [{"name": c.name, "args": c.arguments} for c in resp.tool_calls],
            "content_preview": (resp.content or "")[:300], "wrap_up": last_turn,
        })
        if time.monotonic() - t0 > max_seconds:
            raise StepLimitExceeded(f"步骤用时超过上限 {max_seconds:.0f} 秒")
        if resp.tool_calls and not last_turn:
            messages.append({"role": "assistant", "content": resp.content or "", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
                for c in resp.tool_calls]})
            for c in resp.tool_calls:
                try:
                    result: Any = gateway.call(c.name, c.arguments, allowed=tools, step_id=step_id, caller="model")
                except ToolDenied as e:
                    result = {"error": f"无权限：{e}。只能使用：{tools}"}
                except ToolFailure as e:
                    if e.kind == BAD_ARGS:
                        result = {"error": e.message}
                    elif e.kind == MISSING_INPUT:
                        raise MissingInput(e.message) from e
                    else:
                        raise
                messages.append({"role": "tool", "tool_call_id": c.id,
                                 "content": json.dumps(result, ensure_ascii=False, default=str)})
            continue
        try:
            out = extract_json(resp.content)
            jsonschema.validate(out, output_schema)
            return out
        except (ValueError, jsonschema.ValidationError) as e:
            msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e)
            emit("model_output_invalid", step_id, {"turn": turn, "error": msg})
            messages.append({"role": "assistant", "content": resp.content or ""})
            messages.append({"role": "user", "content": f"输出不符合要求：{msg}。请只输出符合 Schema 的 JSON。"})
    raise StepLimitExceeded(f"模型调用超过轮次上限 {max_turns} 轮仍未完成")
