"""写记忆提醒（PLAN 3.8.5）。

两种触发，互相独立：

- 每 ``memory_reminder_turns`` 轮提醒一次；填 0 关闭。
- 上下文快被 AstrBot 截断或压缩之前提醒一次：估算超过窗口的 70%
  （AstrBot 在 82% 处理，见 3.8.1），或者 AstrBot 设了轮数上限、而且
  已经达到上限减 2 轮。每次截断或压缩之前只提醒一次，是否已经提醒过
  由调用方按会话记录。

提醒和召回块放在同样的位置：本轮用户消息的 ``_no_save`` 附加内容
（PLAN 3.8.1），不存进会话历史，也不影响前缀缓存。这里只做纯判断与
文本拼接，不读写磁盘、不依赖 AstrBot 事件。
"""

from __future__ import annotations

from dataclasses import dataclass

REMINDER_BLOCK_OPEN = "<memory_pod_reminder>"
REMINDER_BLOCK_CLOSE = "</memory_pod_reminder>"
# 估算的上下文超过窗口的这个比例就提前提醒；AstrBot 在 0.82 处理
CONTEXT_DANGER_RATIO = 0.70


@dataclass(frozen=True)
class ReminderPlan:
    """一次请求的提醒结果：两种触发原因分开记，便于调用方决定是否置位。"""

    periodic: bool = False
    # 上下文触发的具体原因（空表示没到危险水位）
    context_reasons: tuple[str, ...] = ()

    @property
    def wanted(self) -> bool:
        return self.periodic or bool(self.context_reasons)


def plan_reminder(
    *,
    turn_count: int,
    every: int,
    estimate: int,
    window: int,
    turns: int,
    turn_limit: int,
) -> ReminderPlan:
    """判断这次请求要不要提醒；只返回原因，不处理"已经提醒过"的抑制。"""
    periodic = every > 0 and turn_count > 0 and turn_count % every == 0
    reasons: list[str] = []
    if window > 0 and estimate > window * CONTEXT_DANGER_RATIO:
        reasons.append("上下文快满了，AstrBot 即将截断或压缩")
    if turn_limit > 0 and turns >= max(0, turn_limit - 2):
        reasons.append(f"已经达到 AstrBot 的轮数上限减 2（{max(0, turn_limit - 2)} 轮）")
    return ReminderPlan(periodic=periodic, context_reasons=tuple(reasons))


def build_block(plan: ReminderPlan, every: int) -> str:
    """把提醒拼成 _no_save 数据块；plan 没有任何原因时返回空串。"""
    reasons: list[str] = []
    if plan.periodic:
        reasons.append(f"已经聊了 {every} 轮")
    reasons.extend(plan.context_reasons)
    if not reasons:
        return ""
    return (
        f"{REMINDER_BLOCK_OPEN}\n"
        f"写记忆提醒：{'；'.join(reasons)}。先回顾最近的对话，把值得长期记住的"
        "写进记忆（memory_write 新建或整篇重写、memory_edit 局部修改、"
        "memory_delete 删除），放哪一层按记忆规范判断，然后照常回复。\n"
        f"{REMINDER_BLOCK_CLOSE}"
    )
