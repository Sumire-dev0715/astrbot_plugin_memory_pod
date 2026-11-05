"""Token 估算。

口径对齐 AstrBot 平台的字符启发式（中文 ×0.6、其他 ×0.3、图片 765、
音频 500），供上下文面板展示插件看到的请求构成。插件不再据此压缩或
截断上下文，估算只用于展示与后续的写记忆提醒。
"""

from __future__ import annotations

import json
import math
from typing import Any

from .turns import is_internal_message

CHINESE_WEIGHT = 0.6
OTHER_WEIGHT = 0.3
IMAGE_TOKENS = 765
AUDIO_TOKENS = 500
# 每条消息的固定开销（role 字段、消息边界标记等），取小值保持口径接近平台。
MESSAGE_OVERHEAD = 4

def _is_chinese(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "㐀" <= ch <= "䶿"


def estimate_text(text: str) -> int:
    """按字符启发式估算一段纯文本的 token 数。"""
    if not text:
        return 0
    chinese = sum(1 for ch in text if _is_chinese(ch))
    other = len(text) - chinese
    return math.ceil(chinese * CHINESE_WEIGHT + other * OTHER_WEIGHT)


def _estimate_segment(segment: Any) -> int:
    """估算多模态 content 列表中的一个消息段。"""
    if isinstance(segment, str):
        return estimate_text(segment)
    if not isinstance(segment, dict):
        return estimate_text(str(segment))
    seg_type = segment.get("type", "")
    if seg_type == "text":
        return estimate_text(str(segment.get("text", "")))
    if seg_type in ("image_url", "image"):
        return IMAGE_TOKENS
    if seg_type in ("input_audio", "audio", "record"):
        return AUDIO_TOKENS
    # 未知消息段：按其 JSON 文本长度估算，避免漏算成 0。
    try:
        return estimate_text(json.dumps(segment, ensure_ascii=False))
    except (TypeError, ValueError):
        return estimate_text(str(segment))


def estimate_content(content: Any) -> int:
    """估算一条消息的 content 字段（str / 多模态 list / None）。"""
    if content is None:
        return 0
    if isinstance(content, str):
        return estimate_text(content)
    if isinstance(content, list):
        return sum(_estimate_segment(seg) for seg in content)
    return estimate_text(str(content))


def estimate_message(message: dict) -> int:
    """估算一条 OpenAI 风格消息字典的 token 数。"""
    total = MESSAGE_OVERHEAD + estimate_content(message.get("content"))
    for tool_call in message.get("tool_calls") or []:
        if not isinstance(tool_call, dict):
            total += estimate_text(str(tool_call))
            continue
        function = tool_call.get("function") or {}
        total += estimate_text(str(function.get("name", "")))
        total += estimate_text(str(function.get("arguments", "")))
    return total


def estimate_messages(messages: list[dict] | None) -> int:
    if not messages:
        return 0
    # 平台内部记录（_checkpoint 等）发给模型前会被剔除，不计入
    return sum(
        estimate_message(m)
        for m in messages
        if isinstance(m, dict) and not is_internal_message(m)
    )


def _usage_field(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


# 各家 provider 上报缓存命中的字段名不同，逐个探测。
_CACHE_HIT_KEYS = (
    "prompt_cache_hit_tokens",  # DeepSeek
    "cache_read_input_tokens",  # Anthropic 系
    "cached_content_token_count",  # Gemini 系
)


def extract_cache_hit(usage: Any) -> int | None:
    """从 usage（dict 或对象）提取提示词缓存命中的 token 数，未上报返回 None。"""
    if usage is None:
        return None
    candidates = [_usage_field(usage, key) for key in _CACHE_HIT_KEYS]
    details = _usage_field(usage, "prompt_tokens_details")  # OpenAI 系
    if details is not None:
        candidates.append(_usage_field(details, "cached_tokens"))
    for value in candidates:
        if value is None:
            continue
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return None


class TokenEstimator:
    """无状态的 token 估算器；保留类是为了调用方按同一入口取估算。"""

    def text(self, text: str) -> int:
        return estimate_text(text)

    def messages(self, messages: list[dict] | None) -> int:
        return estimate_messages(messages)

    def scale(self, count: int) -> int:
        """固定 token 常数（工具配额、图片等）也走同一入口。"""
        return max(0, int(count))
