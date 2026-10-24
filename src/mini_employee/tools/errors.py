"""工具错误分类。MCP 只传文本，因此用前缀编码错误类型，网关据此决定重试 / 暂停 / 回给模型。"""
from __future__ import annotations

TRANSIENT = "TRANSIENT"        # 可重试：超时、连接断开、服务 5xx
MISSING_INPUT = "MISSING_INPUT"  # 不可重试：缺材料 → 待补材料
BAD_ARGS = "BAD_ARGS"          # 不可重试：参数错 → 回给模型自行纠正
INVALID_INPUT = "INVALID_INPUT"  # 不可重试：输入不合规 → 拒绝
INTERNAL = "INTERNAL"          # 不可重试：工具内部的程序错误（未分类异常）→ 暂停转人工，重试无用


def encode(kind: str, message: str) -> str:
    return f"{kind}: {message}"


def decode(text: str) -> tuple[str, str]:
    for kind in (TRANSIENT, MISSING_INPUT, BAD_ARGS, INVALID_INPUT, INTERNAL):
        if text.startswith(kind + ":"):
            return kind, text[len(kind) + 1:].strip()
    # 未分类异常是工具代码本身的错误（如 KeyError），同样的输入重试结果不变，直接暂停；
    # 超时、连接断开等真正的临时故障由网关识别为 TRANSIENT 并重试
    return INTERNAL, f"工具内部错误：{text}"



class ToolFailure(Exception):
    def __init__(self, tool: str, kind: str, message: str, attempts: int = 1):
        super().__init__(f"{tool}: {message}")
        self.tool, self.kind, self.message, self.attempts = tool, kind, message, attempts


class ToolDenied(Exception):
    def __init__(self, tool: str, step_id: str | None):
        super().__init__(f"工具 {tool} 未对步骤 {step_id} 开放")
        self.tool, self.step_id = tool, step_id
