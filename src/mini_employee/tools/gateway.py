"""工具网关：员工的所有工具调用都经过这里。

- 白名单：只把本步骤开放的工具 Schema 发给模型；白名单外的调用被拒绝并留痕（tool_denied）
- 入参校验：按 MCP 工具的 JSON Schema 校验；task_dir 由网关注入，路径限定在任务目录内
- 重试：可重试错误按指数退避重试，耗尽后抛出 ToolFailure，由状态机转“暂停转人工”
- 故障注入：现场演示用，来源为任务配置、环境变量 MINI_FAULTS、runs/faults.json（可在界面开关）
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

import jsonschema
from fastmcp import Client, FastMCP

from ..config import runs_dir
from .errors import BAD_ARGS, TRANSIENT, ToolDenied, ToolFailure, decode

logging.getLogger("fastmcp").setLevel(logging.CRITICAL)
logging.getLogger("mcp").setLevel(logging.CRITICAL)

Emit = Callable[[str, str | None, dict[str, Any]], None]


def load_faults(task_faults: list[dict] | None = None) -> list[dict]:
    """故障格式：{"tool": "read_minutes", "mode": "timeout|error|missing", "times": 2}，times 省略表示一直失败。"""
    faults = list(task_faults or [])
    env = os.environ.get("MINI_FAULTS", "")
    for spec in filter(None, (s.strip() for s in env.split(","))):
        parts = spec.split(":")
        faults.append({"tool": parts[0], "mode": parts[1] if len(parts) > 1 else "timeout",
                       "times": int(parts[2]) if len(parts) > 2 else None})
    f = runs_dir() / "faults.json"
    if f.exists():
        try:
            faults += json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return faults


class ToolGateway:
    def __init__(self, server: FastMCP, task_dir: Path, emit: Emit, *, max_retries: int = 3,
                 backoff: list[float] | None = None, timeout: float = 30.0, task_faults: list[dict] | None = None):
        self.server, self.task_dir, self.emit = server, task_dir, emit
        self.max_retries, self.timeout = max_retries, timeout
        self.backoff = backoff or [1, 2, 4]
        if os.environ.get("MINI_FAST"):
            self.backoff = [0]
        self.task_faults = task_faults
        self._fault_hits: dict[int, int] = {}
        self._schemas: dict[str, dict] | None = None
        self._descriptions: dict[str, str] = {}

    # ------------------------------------------------------------ schemas
    def schemas(self) -> dict[str, dict]:
        if self._schemas is None:
            async def _list():
                async with Client(self.server) as c:
                    return await c.list_tools()
            tools = asyncio.run(_list())
            self._schemas = {t.name: t.input_schema for t in tools}
            self._descriptions = {t.name: (t.description or "") for t in tools}
        return self._schemas

    def model_tools(self, allowed: list[str]) -> list[dict]:
        """OpenAI function-calling 格式的工具定义，去掉网关注入的参数。"""
        from .server import INJECTED
        out = []
        for name in allowed:
            s = json.loads(json.dumps(self.schemas()[name]))
            for k in INJECTED:
                s.get("properties", {}).pop(k, None)
                if k in s.get("required", []):
                    s["required"].remove(k)
            out.append({"type": "function", "function": {"name": name, "description": self._descriptions[name],
                                                           "parameters": s}})
        return out

    # ------------------------------------------------------------ call
    def _inject_fault(self, tool: str) -> str | None:
        for i, f in enumerate(load_faults(self.task_faults)):
            if f.get("tool") not in (tool, "*"):
                continue
            key = hash((i, json.dumps(f, sort_keys=True)))
            n = self._fault_hits.get(key, 0)
            if f.get("times") is not None and n >= int(f["times"]):
                continue
            self._fault_hits[key] = n + 1
            mode = f.get("mode", "timeout")
            if mode == "missing":
                return f"MISSING_INPUT: [故障注入] {tool} 依赖的材料不可用"
            return f"TRANSIENT: [故障注入] {tool} {'连接超时' if mode == 'timeout' else '服务返回 503'}"
        return None

    async def _call_once(self, tool: str, args: dict) -> tuple[bool, Any]:
        async with Client(self.server) as c:
            r = await asyncio.wait_for(c.call_tool(tool, args, raise_on_error=False), self.timeout)
            if r.is_error:
                return False, " ".join(getattr(x, "text", "") for x in r.content)
            return True, r.structured_content if r.structured_content is not None else r.data

    def call(self, tool: str, args: dict[str, Any], *, allowed: list[str], step_id: str | None,
             caller: str = "program") -> dict[str, Any]:
        if tool not in allowed:
            self.emit("tool_denied", step_id, {"tool": tool, "args": args, "caller": caller, "allowed": allowed})
            raise ToolDenied(tool, step_id)
        if tool not in self.schemas():
            self.emit("tool_denied", step_id, {"tool": tool, "args": args, "caller": caller, "reason": "工具不存在"})
            raise ToolDenied(tool, step_id)
        full = {**args, "task_dir": str(self.task_dir)}
        try:
            jsonschema.validate(full, self.schemas()[tool])
        except jsonschema.ValidationError as e:
            msg = f"参数不符合 Schema：{e.message}"
            self.emit("tool_failed", step_id, {"tool": tool, "args": args, "kind": BAD_ARGS, "error": msg, "attempt": 1})
            raise ToolFailure(tool, BAD_ARGS, msg) from e

        attempts = self.max_retries + 1
        last = ""
        for attempt in range(1, attempts + 1):
            t0 = time.monotonic()
            fault = self._inject_fault(tool)
            if fault:
                ok, res = False, fault
            else:
                try:
                    ok, res = asyncio.run(self._call_once(tool, full))
                except (TimeoutError, asyncio.TimeoutError):
                    ok, res = False, f"TRANSIENT: 调用超时（>{self.timeout}s）"
                except (ConnectionError, OSError) as e:
                    ok, res = False, f"TRANSIENT: 连接失败：{e}"
            ms = int((time.monotonic() - t0) * 1000)
            if ok:
                self.emit("tool_called", step_id, {"tool": tool, "args": args, "attempt": attempt, "ms": ms,
                                                    "caller": caller, "result": _brief(res)})
                return res
            kind, msg = decode(str(res))
            last = msg
            self.emit("tool_failed", step_id, {"tool": tool, "args": args, "attempt": attempt, "ms": ms,
                                                "kind": kind, "error": msg, "caller": caller})
            if kind != TRANSIENT:
                raise ToolFailure(tool, kind, msg, attempt)
            if attempt < attempts:
                time.sleep(self.backoff[min(attempt - 1, len(self.backoff) - 1)])
        raise ToolFailure(tool, TRANSIENT, f"{last}（已重试 {self.max_retries} 次）", attempts)


def _brief(res: Any, limit: int = 600) -> Any:
    s = json.dumps(res, ensure_ascii=False, default=str)
    return res if len(s) <= limit else s[:limit] + "…"
