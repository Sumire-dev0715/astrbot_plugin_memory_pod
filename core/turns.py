"""消息工具的公共判定。"""

from __future__ import annotations


def is_internal_message(message) -> bool:
    """平台内部记录（role 以下划线开头，如 AstrBot 的 _checkpoint）。

    这类记录跟着历史一起传进来，但不是对话内容，发给模型前平台会剔除：
    估算与展示都要跳过。
    """
    role = message.get("role") if isinstance(message, dict) else None
    return isinstance(role, str) and role.startswith("_")


def conversation_turns(messages) -> int:
    """按 AstrBot 的口径数对话轮数：非 system 消息两两一轮。

    AstrBot 的上下文截断就是这么数的（core/agent/context/truncator.py 的
    truncate_by_turns：``len(non_system_messages) // 2``）。插件不再管理
    上下文，只用同一口径判断"是否快到 AstrBot 的轮数上限"（PLAN 3.8.5）。
    """
    count = 0
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "system" or is_internal_message(message):
            continue
        count += 1
    return count // 2
