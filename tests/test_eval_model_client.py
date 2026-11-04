"""OpenAI 兼容客户端的本地 HTTP 测试：不访问任何真实接口。"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.eval.model_client import (
    BudgetExceeded,
    CallBudget,
    ModelError,
    OpenAICompatClient,
    normalize_chat_url,
)


class _Server:
    """可编程的本地 OpenAI 兼容服务，记录请求路径与请求头。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.paths: list[str] = []
        self.auth_headers: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server 命名
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.paths.append(self.path)
                outer.auth_headers.append(self.headers.get("Authorization") or "")
                status, payload = (
                    outer.responses.pop(0)
                    if outer.responses
                    else (500, {"error": "no response"})
                )
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args) -> None:  # noqa: A002
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def _reply(tool_call: bool = False) -> dict:
    message: dict = {"role": "assistant", "content": "你好"}
    if tool_call:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "memory_note",
                        "arguments": json.dumps({"content": "记住"}, ensure_ascii=False),
                    },
                }
            ],
        }
    return {
        "choices": [{"message": message}],
        "usage": {"prompt_tokens": 12},
    }


@pytest.mark.parametrize(
    "base",
    [
        "https://api.example.com",
        "https://api.example.com/v1",
        "https://api.example.com/v1/",
    ],
)
def test_normalize_chat_url(base):
    assert normalize_chat_url(base) == "https://api.example.com/v1/chat/completions"


def test_client_retries_5xx_and_parses_tool_calls():
    server = _Server([(500, {"error": "boom"}), (200, _reply(tool_call=True))])
    try:
        client = OpenAICompatClient(
            server.url,
            "eval-model",
            "sk-local-secret",
            sleep=lambda _seconds: None,
            max_attempts=3,
        )
        reply = asyncio.run(
            client.complete([{"role": "user", "content": "hi"}], None)
        )
    finally:
        server.close()
    assert server.paths == ["/v1/chat/completions", "/v1/chat/completions"]
    assert server.auth_headers[0] == "Bearer sk-local-secret"
    assert reply.content == ""
    assert reply.tool_calls[0].name == "memory_note"
    assert reply.tool_calls[0].arguments == {"content": "记住"}
    assert reply.prompt_tokens == 12


def test_client_retries_truncated_response():
    """finish_reason=length 的残缺回复不能当成功结果。"""
    truncated = _reply()
    truncated["choices"][0]["finish_reason"] = "length"
    server = _Server([(200, truncated), (200, _reply())])
    try:
        client = OpenAICompatClient(
            server.url,
            "eval-model",
            "sk-local-secret",
            sleep=lambda _seconds: None,
            max_attempts=2,
        )
        reply = asyncio.run(
            client.complete([{"role": "user", "content": "hi"}], None)
        )
    finally:
        server.close()
    assert len(server.paths) == 2
    assert reply.content == "你好"


def test_client_accepts_base_with_v1():
    server = _Server([(200, _reply())])
    try:
        client = OpenAICompatClient(
            server.url + "/v1",
            "eval-model",
            "sk-local-secret",
            sleep=lambda _seconds: None,
        )
        asyncio.run(client.complete([{"role": "user", "content": "hi"}], None))
    finally:
        server.close()
    assert server.paths == ["/v1/chat/completions"]


def test_client_redacts_key_from_http_error():
    key = "sk-local-secret"
    server = _Server([(400, {"error": f"bad key {key}"})])
    try:
        client = OpenAICompatClient(
            server.url,
            "eval-model",
            key,
            sleep=lambda _seconds: None,
            max_attempts=1,
        )
        with pytest.raises(ModelError) as caught:
            asyncio.run(client.complete([{"role": "user", "content": "hi"}], None))
    finally:
        server.close()
    message = str(caught.value)
    assert key not in message
    assert "HTTP 400" in message


def test_budget_stops_extra_calls():
    budget = CallBudget(1)
    server = _Server([(200, _reply()), (200, _reply())])
    try:
        client = OpenAICompatClient(
            server.url,
            "eval-model",
            "sk-local-secret",
            budget=budget,
            sleep=lambda _seconds: None,
        )
        asyncio.run(client.complete([{"role": "user", "content": "one"}], None))
        with pytest.raises(BudgetExceeded):
            asyncio.run(client.complete([{"role": "user", "content": "two"}], None))
    finally:
        server.close()
    assert budget.count == 1
