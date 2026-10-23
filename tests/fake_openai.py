"""本地假 OpenAI 兼容服务：/v1/chat/completions 按 OpenAI 协议收发，内部用 Mock 模型的规则生成回复。

用途：没有 API Key 时也能测试真实模型的代码路径（openai SDK、HTTP、tool_calls 解析、多轮消息拼接）。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mini_employee.llm.mock import MockLLM


def _purpose(messages: list[dict]) -> str:
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "## 质检修正" in system:
        return "fix"
    if "## 异常分析" in system:
        return "analysis"
    if "## 撰写周报" in system:
        return "draft"
    return "check"


class _Handler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *a):  # 静默
        pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != "Bearer test-key":
            return self._send(401, {"error": {"message": "invalid api key", "type": "auth"}})
        msgs = body["messages"]
        purpose = _purpose(msgs)
        _Handler.calls.append({"purpose": purpose, "tools": [t["function"]["name"] for t in body.get("tools", [])]})
        if purpose == "check":
            content, calls = self._check(msgs, body.get("tools"))
        else:
            # 协议约定：Mock 读取第二条消息为 JSON 载荷；工具结果按 OpenAI 格式回传
            turn = sum(1 for m in msgs if m["role"] == "assistant") + 1
            r = MockLLM().chat(msgs, body.get("tools", []), {"purpose": purpose, "turn": turn})
            content, calls = r.content, [{"id": c.id, "type": "function", "function": {
                "name": c.name, "arguments": json.dumps(c.arguments, ensure_ascii=False)}} for c in r.tool_calls]
        msg = {"role": "assistant", "content": content}
        if calls:
            msg["tool_calls"] = calls
        self._send(200, {"id": "x", "object": "chat.completion", "created": int(time.time()), "model": body["model"],
                         "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if calls else "stop"}],
                         "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})

    @staticmethod
    def _check(msgs, tools):
        last = msgs[-1]
        if last["role"] == "user" and "收到" in last["content"]:
            return "收到", []
        if tools and not any(m["role"] == "tool" for m in msgs):
            return None, [{"id": "c1", "type": "function", "function": {"name": "query_kpi",
                                                                         "arguments": '{"prefix": "region.华东"}'}}]
        return '{"region": "华东", "sales_wow": "-20.0%"}', []

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def start() -> tuple[str, ThreadingHTTPServer]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}/v1", srv
