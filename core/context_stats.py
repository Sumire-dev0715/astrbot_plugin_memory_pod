"""上下文统计：最近一次请求的估算构成与提供商上报的用量。

AstrBot 自己负责截断与压缩上下文（PLAN 3.8.1），插件只记录自己看到的
请求构成，供 `/mb_status` 与管理页的只读「上下文」面板展示：

- 每轮的估算分项（系统提示词、插件开头块、工具声明、历史、本轮输入、
  召回块）与总额；
- 提供商最近一次上报的输入 token 数与缓存命中数；
- 从当前对话历史里读出 AstrBot 最近一次压缩留下的摘要。

这里只做数据存取与纯解析，不读盘、不依赖 AstrBot 事件。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .turns import is_internal_message

# AstrBot 4.29.4 摘要压缩后写回历史的摘要消息前缀
# （core/agent/context/ 下的摘要模块，约 296 行）
ASTRBOT_SUMMARY_PREFIX = "Our previous history conversation summary: "

# 估算分项的键（顺序即 /mb_status 与管理页的展示顺序）
PART_KEYS = ("system", "plugin", "tools", "history", "prompt", "recall")


def message_fingerprint(message: dict) -> str:
    """一条消息的稳定指纹，用于判断历史开头是否被 AstrBot 改动。"""
    try:
        payload = json.dumps(
            {
                "role": message.get("role"),
                "content": message.get("content"),
                "tool_calls": message.get("tool_calls"),
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except (TypeError, ValueError):
        payload = str(message)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


def history_head(contexts: list[dict]) -> str:
    """历史开头（开头连续 system 消息之后的第一条）的指纹。

    截断会从开头丢掉旧轮次，压缩会把开头换成 AstrBot 的摘要消息，
    两种情况都会改变这个指纹。历史为空时返回空串。
    """
    for message in contexts or []:
        if (
            not isinstance(message, dict)
            or message.get("role") == "system"
            or is_internal_message(message)
        ):
            continue
        return message_fingerprint(message)
    return ""


def latest_astrbot_summary(messages: list[dict]) -> str:
    """当前对话历史里最新一条 AstrBot 压缩摘要的正文；没有返回空串。"""
    latest = ""
    for message in messages or []:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith(ASTRBOT_SUMMARY_PREFIX):
            latest = content[len(ASTRBOT_SUMMARY_PREFIX) :].strip()
    return latest


@dataclass
class ContextStats:
    """一个会话最近一次请求的统计，可 JSON 持久化。"""

    parts: dict = field(default_factory=dict)
    estimate: int = 0
    # 提供商上报的真实输入 token 数；0 表示尚未上报
    actual: int = 0
    # 缓存命中的 token 数；-1 表示提供商未上报
    cache_hit: int = -1
    # 最近一次请求的时间
    request_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "parts": dict(self.parts),
            "estimate": self.estimate,
            "actual": self.actual,
            "cache_hit": self.cache_hit,
            "request_at": self.request_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ContextStats":
        state = cls()
        if not isinstance(raw, dict):
            return state
        parts = raw.get("parts")
        if isinstance(parts, dict):
            values: dict[str, int] = {}
            for key in PART_KEYS:
                try:
                    values[key] = max(0, int(parts.get(key, 0)))
                except (TypeError, ValueError):
                    values[key] = 0
            if any(values.values()):
                state.parts = values
        try:
            state.estimate = max(0, int(raw.get("estimate", 0)))
            state.actual = max(0, int(raw.get("actual", 0)))
            state.cache_hit = max(-1, int(raw.get("cache_hit", -1)))
            state.request_at = float(raw.get("request_at", 0.0))
        except (TypeError, ValueError):
            return cls(parts=state.parts)
        return state
