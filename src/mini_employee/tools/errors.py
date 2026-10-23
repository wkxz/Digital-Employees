"""工具错误分类。MCP 只传文本，因此用前缀编码错误类型，网关据此决定重试 / 暂停 / 回给模型。"""
from __future__ import annotations

TRANSIENT = "TRANSIENT"        # 可重试：超时、连接断开、服务 5xx
MISSING_INPUT = "MISSING_INPUT"  # 不可重试：缺材料 → 待补材料
BAD_ARGS = "BAD_ARGS"          # 不可重试：参数错 → 回给模型自行纠正
INVALID_INPUT = "INVALID_INPUT"  # 不可重试：输入不合规 → 拒绝


def encode(kind: str, message: str) -> str:
    return f"{kind}: {message}"


def decode(text: str) -> tuple[str, str]:
    for kind in (TRANSIENT, MISSING_INPUT, BAD_ARGS, INVALID_INPUT):
        if text.startswith(kind + ":"):
            return kind, text[len(kind) + 1:].strip()
    return TRANSIENT, text  # 未分类异常按可重试处理，重试耗尽后暂停转人工


class ToolFailure(Exception):
    def __init__(self, tool: str, kind: str, message: str, attempts: int = 1):
        super().__init__(f"{tool}: {message}")
        self.tool, self.kind, self.message, self.attempts = tool, kind, message, attempts


class ToolDenied(Exception):
    def __init__(self, tool: str, step_id: str | None):
        super().__init__(f"工具 {tool} 未对步骤 {step_id} 开放")
        self.tool, self.step_id = tool, step_id
