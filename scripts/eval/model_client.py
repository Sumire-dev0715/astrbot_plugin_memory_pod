"""OpenAI 兼容的评测模型客户端（只用标准库 urllib）。

聊天与后台整理共用同一个客户端与模型。接口限流（429）与 5xx 退避重试；
MB_EVAL_BASE_URL 带不带 /v1 都能用。API key 只放在请求头里，任何异常、
日志与输出里都先把它抹掉。
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# 整次评测的接口调用上限（含重试）；超过后中止，按脚本问题退出码 2 处理。
MAX_API_CALLS = 3000
# 单次回复的工具循环上限（与 PLAN 3.7.7 一致）。
MAX_TOOL_STEPS = 8
# 请求超时与重试
REQUEST_TIMEOUT = 120
MAX_ATTEMPTS = 5
RETRY_BASE_DELAY = 1.0
RETRY_MAX_DELAY = 30.0


class EvalError(Exception):
    """评测基础设施问题：配置、网络或脚本自身，对应退出码 2。"""


class ConfigError(EvalError):
    """环境变量或命令行配置有误。"""


class ModelError(EvalError):
    """模型接口不可用（鉴权、地址、持续 5xx 等）。"""


class BudgetExceeded(EvalError):
    """整次评测的接口调用次数超过上限。"""


@dataclass
class ToolCall:
    name: str
    arguments: dict
    call_id: str = ""


@dataclass
class ModelReply:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0


class CallBudget:
    """整次评测共享的接口调用计数器（含重试）。"""

    def __init__(self, limit: int = MAX_API_CALLS) -> None:
        self.limit = limit
        self.count = 0
        self.exceeded = False
        self._lock = threading.Lock()

    def spend(self) -> None:
        with self._lock:
            if self.count >= self.limit:
                self.exceeded = True
                raise BudgetExceeded(
                    f"接口调用超过 {self.limit} 次，已中止本次评测"
                )
            self.count += 1


def normalize_chat_url(base_url: str) -> str:
    """把 MB_EVAL_BASE_URL 归一化成 chat/completions 地址。

    支持 https://host、https://host/v1、https://host/v1/ 三种写法；已经
    写到 /chat/completions 的也原样使用。
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ConfigError("MB_EVAL_BASE_URL 为空")
    if base.endswith("/chat/completions"):
        return base
    if re.search(r"/v\d+$", base):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


class OpenAICompatClient:
    """同步 urllib 请求放进线程，接口保持 async。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str,
        *,
        budget: CallBudget | None = None,
        timeout: int = REQUEST_TIMEOUT,
        max_attempts: int = MAX_ATTEMPTS,
        sleep=time.sleep,
    ) -> None:
        if not model:
            raise ConfigError("MB_EVAL_MODEL 为空")
        if not api_key:
            raise ConfigError("MB_EVAL_API_KEY 为空")
        self.endpoint = normalize_chat_url(base_url)
        self.model = model
        self._api_key = api_key
        self.budget = budget or CallBudget()
        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self._sleep = sleep
        # 指标：聊天回复与后台调用分开计数（含重试次数）
        self.chat_calls = 0
        self.background_calls = 0

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        *,
        purpose: str = "chat",
    ) -> ModelReply:
        return await asyncio.to_thread(
            self._complete_sync, messages, tools, purpose
        )

    # ------------------------------------------------------------ 同步实现

    def _complete_sync(
        self,
        messages: list[dict],
        tools: list[dict] | None,
        purpose: str,
    ) -> ModelReply:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            # 推理模型的思考 token 也计入 max_tokens：原先的 2400/1200 会在
            # 思考较长时截断正文，后台整理因此拿到残缺 JSON。留足余量。
            "max_tokens": 8192,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            self.budget.spend()
            if purpose == "chat":
                self.chat_calls += 1
            else:
                self.background_calls += 1
            retry_after = 0.0
            try:
                data = self._post(body)
                return self._parse_reply(data)
            except urllib.error.HTTPError as exc:
                detail = self._read_http_error(exc)
                last_error = detail
                if not (exc.code == 429 or exc.code >= 500):
                    raise ModelError(detail) from None
                retry_after = self._retry_after(exc)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f"接口连接失败：{self._redact(str(exc))}"
            except ValueError as exc:
                # 响应不是合法 JSON：可能是一次性的坏响应，按可重试处理
                last_error = f"接口响应无法解析：{self._redact(str(exc))}"
            if attempt < self.max_attempts:
                delay = min(
                    RETRY_BASE_DELAY * (2 ** (attempt - 1)), RETRY_MAX_DELAY
                )
                if retry_after > 0:
                    delay = min(max(delay, retry_after), RETRY_MAX_DELAY)
                self._sleep(delay)
        raise ModelError(f"接口重试 {self.max_attempts} 次仍失败：{last_error}")

    def _post(self, body: bytes) -> dict:
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("响应不是 JSON 对象")
        return data

    def _parse_reply(self, data: dict) -> ModelReply:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("响应缺少 choices")
        choice = choices[0] if isinstance(choices[0], dict) else {}
        if choice.get("finish_reason") == "length":
            # 思考 token 用尽额度，正文或工具参数被截断：按可重试处理，
            # 不能把残缺输出当成模型回复交给评测
            raise ValueError("响应被 max_tokens 截断（finish_reason=length）")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ValueError("响应缺少 message")
        content = message.get("content")
        content = content if isinstance(content, str) else ""
        calls: list[ToolCall] = []
        for index, raw in enumerate(message.get("tool_calls") or []):
            if not isinstance(raw, dict):
                continue
            function = raw.get("function") or {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(
                ToolCall(
                    name=name,
                    arguments=arguments,
                    call_id=str(raw.get("id") or f"call_{index + 1}"),
                )
            )
        usage = data.get("usage") or {}
        try:
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
        except (TypeError, ValueError):
            prompt_tokens = 0
        return ModelReply(content=content, tool_calls=calls, prompt_tokens=prompt_tokens)

    def _read_http_error(self, exc: urllib.error.HTTPError) -> str:
        try:
            raw = exc.read().decode("utf-8", "replace")
        except OSError:
            raw = ""
        tail = f"：{raw[:300]}" if raw else ""
        if exc.code in (401, 403):
            # 不回显响应正文，避免任何鉴权材料出现在输出里
            return f"接口鉴权失败（HTTP {exc.code}），请检查 MB_EVAL_API_KEY"
        return f"接口返回 HTTP {exc.code}{self._redact(tail)}"

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float:
        raw = exc.headers.get("Retry-After") if exc.headers else None
        try:
            return max(0.0, float(raw or 0))
        except (TypeError, ValueError):
            return 0.0

    def _redact(self, text: str) -> str:
        """任何要进入异常/输出的文本都先抹掉 key。"""
        if self._api_key and self._api_key in text:
            return text.replace(self._api_key, "***")
        return text
