"""mini check-model：用三次小调用检查模型配置是否可用于本系统。

1. 连通性与密钥   2. 工具调用（Function Calling）   3. 读取工具结果后输出 JSON
同时测量响应时间，对照员工配置中的轮次 / 时长上限给出提示。
"""
from __future__ import annotations

import json
import time

from ..config import load_employee
from . import LLMUnavailable, build_llm, resolved_config
from .agent import extract_json

TOOL = {"type": "function", "function": {
    "name": "query_kpi", "description": "查询 KPI，返回格式化后的数值",
    "parameters": {"type": "object", "properties": {"prefix": {"type": "string", "description": "如 region.华东"}},
                   "required": ["prefix"]}}}


def _mask(k: str | None) -> str:
    return "（未填写）" if not k else f"{k[:4]}…{k[-4:]}（{len(k)} 位）"


def check_model() -> int:
    emp = load_employee("weekly_report")
    cfg = resolved_config(emp.raw.get("model", {}))
    print(f"模型提供方：{cfg['provider']}")
    if cfg["provider"] == "mock":
        print("当前使用离线 Mock 模型。接入真实模型：在 .env 中设置 MINI_MODEL=openai_compatible 并填写 "
              "MINI_BASE_URL / MINI_API_KEY / MINI_MODEL_NAME。")
        return 1
    import os
    print(f"Base URL：{cfg.get('base_url') or '（未填写）'}")
    print(f"模型名：{cfg.get('model') or '（未填写）'}")
    print(f"API Key：{_mask(cfg.get('api_key') or os.environ.get(cfg.get('api_key_env', '')))}")
    try:
        llm = build_llm(emp.raw.get("model", {}))
    except LLMUnavailable as e:
        print(f"❌ {e}")
        return 1

    times: list[float] = []

    def call(messages, tools):
        t0 = time.monotonic()
        r = llm.chat(messages, tools, {"purpose": "check"})
        times.append(time.monotonic() - t0)
        return r

    try:
        r = call([{"role": "user", "content": "只回复两个字：收到"}], [])
        print(f"✅ 1/3 连通性与密钥：模型回复“{(r.content or '').strip()[:20]}”，{times[-1]:.1f} 秒")

        msgs = [{"role": "system", "content": "你是经营分析助手。需要数字时必须调用 query_kpi 工具，不要自己编数字。"},
                {"role": "user", "content": "华东区本周销售额环比是多少？"}]
        r = call(msgs, [TOOL])
        if not r.tool_calls:
            print(f"❌ 2/3 工具调用：模型没有调用工具，直接回答了“{(r.content or '')[:60]}”。"
                  "该模型或接口可能不支持 Function Calling，请换用支持工具调用的模型。")
            return 1
        c = r.tool_calls[0]
        print(f"✅ 2/3 工具调用：{c.name}({json.dumps(c.arguments, ensure_ascii=False)})，{times[-1]:.1f} 秒")

        msgs += [{"role": "assistant", "content": r.content or "", "tool_calls": [
                     {"id": c.id, "type": "function",
                      "function": {"name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}}]},
                 {"role": "tool", "tool_call_id": c.id,
                  "content": json.dumps({"values": {"region.华东.sales_wow": "-20.0%"}}, ensure_ascii=False)},
                 {"role": "user", "content": '请只输出 JSON：{"region": "华东", "sales_wow": "<工具返回的值>"}'}]
        r = call(msgs, [TOOL])
        out = extract_json(r.content)
        ok = out.get("sales_wow") == "-20.0%"
        print(f"{'✅' if ok else '⚠️'} 3/3 读取工具结果并输出 JSON：{json.dumps(out, ensure_ascii=False)}，{times[-1]:.1f} 秒"
              + ("" if ok else "（数值与工具返回不一致，注意模型可能改写数字；本系统的占位符机制会兜底）"))
    except LLMUnavailable as e:
        print(f"❌ 调用失败：{e}")
        return 1
    except ValueError as e:
        print(f"❌ 3/3 模型没有按要求输出 JSON：{e}")
        return 1

    lim = emp.limits
    worst = max(times)
    print(f"\n响应时间：平均 {sum(times) / len(times):.1f} 秒，最长 {worst:.1f} 秒（短提示词；实际任务的提示词更长，通常会慢 2–4 倍）")
    est = worst * 3 * 4
    if est > lim["max_step_seconds"]:
        print(f"⚠️ 按 4 轮、每轮约 {worst * 3:.0f} 秒估算，单步可能超过上限 {lim['max_step_seconds']} 秒，"
              f"建议先跑一条任务观察事件日志中的 model_called 耗时，再调整 employee.yaml 的 limits。")
    print("模型可用。下一步：uv run mini cases  （或 make cases）")
    return 0
