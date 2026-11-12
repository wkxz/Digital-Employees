"""Agent 步骤：在单个步骤内运行“模型 ⇄ 工具”循环（LangGraph 子图），受轮次与时长上限约束。

模型只能：读输入、调用本步骤开放的工具、按 JSON Schema 输出结果。
模型不能：触发状态流转、调用未开放的工具、写文件。

子图：START → model ─┬─ 有工具调用 → tools → model
                      └─ 给出结果   → parse ─┬─ 符合 Schema → END
                                              └─ 不符合     → model（附上错误要求重写）
路由全部由程序判定；超轮次、超时抛 StepLimitExceeded，由编排图转人工。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Callable, TypedDict

import jsonschema
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

from ..tools.errors import BAD_ARGS, MISSING_INPUT, ToolDenied, ToolFailure
from .base import LLM, LLMResponse

Emit = Callable[[str, str | None, dict[str, Any]], None]
# 实时进度推送的模型输出上限（字符）：网页要从开头解析 JSON 按字段显示，需要完整输出；超长时只保留末尾
LIVE_TEXT_MAX = 30000


class StepLimitExceeded(Exception):
    pass


class MissingInput(Exception):
    pass


class AgentState(TypedDict, total=False):
    messages: list[dict[str, Any]]
    turn: int
    last_turn: bool
    response: LLMResponse | None
    result: dict[str, Any] | None


def extract_json(text: str | None) -> Any:
    if not text:
        raise ValueError("模型没有输出内容")
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    raw = m.group(1) if m else text[text.find("{"): text.rfind("}") + 1]
    return json.loads(raw)


WRAP_UP = ("已到本步骤的最后一轮。不要再调用工具，请基于已获得的信息立即输出最终 JSON；"
           "没有依据的原因按岗位说明标注待核实。")


def build_agent_graph(llm: LLM, gateway: Any, emit: Emit, *, step_id: str, purpose: str, tools: list[str],
                      output_schema: dict[str, Any], max_turns: int, max_seconds: float,
                      meta: dict[str, Any] | None = None):
    """构建单个 agent 步骤的子图。模型、网关、上限通过闭包注入，图状态只保存对话与轮次。"""
    tool_defs = gateway.model_tools(tools) if tools else []
    meta = {**(meta or {}), "step": step_id, "purpose": purpose}
    t0 = time.monotonic()

    def check_time() -> None:
        if time.monotonic() - t0 > max_seconds:
            raise StepLimitExceeded(f"步骤用时超过上限 {max_seconds:.0f} 秒")

    def model(state: AgentState) -> AgentState:
        check_time()
        turn = state.get("turn", 0) + 1
        if turn > max_turns:
            raise StepLimitExceeded(f"模型调用超过轮次上限 {max_turns} 轮仍未完成")
        messages = list(state["messages"])
        last_turn = turn == max_turns and max_turns > 1
        if last_turn:  # 最后一轮：收回工具，要求基于已有信息直接给出结果，而不是继续检索
            messages.append({"role": "user", "content": WRAP_UP})
        c0 = time.monotonic()
        write = get_stream_writer()  # 流式输出：模型边生成边推送（外层以 stream 运行时网页可实时看到）

        def on_delta(text: str) -> None:
            write({"type": "model", "step": step_id, "turn": turn, "text": text[-LIVE_TEXT_MAX:], "done": False})

        resp = llm.chat(messages, [] if last_turn else tool_defs,
                        {**meta, "turn": turn, "wrap_up": last_turn, "on_delta": on_delta})
        write({"type": "model", "step": step_id, "turn": turn, "text": (resp.content or "")[-LIVE_TEXT_MAX:], "done": True,
               "tool_calls": [c.name for c in resp.tool_calls]})
        emit("model_called", step_id, {
            "model": llm.name, "purpose": purpose, "turn": turn, "ms": int((time.monotonic() - c0) * 1000),
            "usage": resp.usage,
            "prompt_sha": hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).hexdigest()[:12],
            "tool_calls": [{"name": c.name, "args": c.arguments} for c in resp.tool_calls],
            "content_preview": (resp.content or "")[:300], "wrap_up": last_turn,
        })
        check_time()
        return {"messages": messages, "turn": turn, "last_turn": last_turn, "response": resp}

    def after_model(state: AgentState) -> str:
        resp = state["response"]
        return "tools" if resp.tool_calls and not state["last_turn"] else "parse"

    def call_tools(state: AgentState) -> AgentState:
        resp = state["response"]
        messages = list(state["messages"])
        messages.append({"role": "assistant", "content": resp.content or "", "tool_calls": [
            {"id": c.id, "type": "function",
             "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}
            for c in resp.tool_calls]})
        for c in resp.tool_calls:  # 一律经工具网关：白名单、参数校验、重试、幂等、留痕
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
        return {"messages": messages, "response": None}

    def parse(state: AgentState) -> AgentState:
        resp = state["response"]
        try:
            out = extract_json(resp.content)
            jsonschema.validate(out, output_schema)
            return {"result": out, "response": None}
        except (ValueError, jsonschema.ValidationError) as e:
            msg = e.message if isinstance(e, jsonschema.ValidationError) else str(e)
            emit("model_output_invalid", step_id, {"turn": state["turn"], "error": msg})
            if state["turn"] >= max_turns:
                raise StepLimitExceeded(f"模型调用超过轮次上限 {max_turns} 轮仍未完成") from None
            messages = list(state["messages"]) + [
                {"role": "assistant", "content": resp.content or ""},
                {"role": "user", "content": f"输出不符合要求：{msg}。请只输出符合 Schema 的 JSON。"}]
            return {"messages": messages, "response": None, "result": None}

    def after_parse(state: AgentState) -> str:
        return END if state.get("result") is not None else "model"

    g = StateGraph(AgentState)
    g.add_node("model", model)
    g.add_node("tools", call_tools)
    g.add_node("parse", parse)
    g.add_edge(START, "model")
    g.add_conditional_edges("model", after_model, ["tools", "parse"])
    g.add_edge("tools", "model")
    g.add_conditional_edges("parse", after_parse, ["model", END])
    return g.compile(name=f"agent:{step_id}", checkpointer=False)  # 对话不写检查点（结果已落盘）


def run_agent_step(llm: LLM, gateway: Any, emit: Emit, *, step_id: str, purpose: str, system: str,
                   payload: dict[str, Any], tools: list[str], output_schema: dict[str, Any],
                   max_turns: int, max_seconds: float, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    schema_text = json.dumps(output_schema, ensure_ascii=False)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"{system}\n\n## 输出格式\n完成后只输出一个 JSON 对象（不要输出其他文字），"
                                      f"须符合以下 JSON Schema：\n{schema_text}"},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
    ]
    graph = build_agent_graph(llm, gateway, emit, step_id=step_id, purpose=purpose, tools=tools,
                              output_schema=output_schema, max_turns=max_turns, max_seconds=max_seconds, meta=meta)
    # 每轮最多经过 model → tools/parse 两个节点，recursion_limit 只作兜底，真正的上限是 max_turns
    final = graph.invoke({"messages": messages, "turn": 0, "result": None},
                         {"recursion_limit": max_turns * 3 + 5})
    return final["result"]
