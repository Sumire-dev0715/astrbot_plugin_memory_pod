"""消息工具的公共判定。"""

from __future__ import annotations


def is_internal_message(message) -> bool:
    """平台内部记录（role 以下划线开头，如 AstrBot 的 _checkpoint）。

    这类记录跟着历史一起传进来，但不是对话内容，发给模型前平台会剔除：
    估算与展示都要跳过。
    """
    role = message.get("role") if isinstance(message, dict) else None
    return isinstance(role, str) and role.startswith("_")
