"""写记忆提醒（PLAN 3.8.5）：每 N 轮、截断/压缩前各提醒一次，且不进历史。"""

from __future__ import annotations

import types

from support import FakeEvent, make_plugin, pkg_reminder, run


def make_request(prompt="你好", contexts=None, cid="conv-1"):
    return types.SimpleNamespace(
        prompt=prompt,
        contexts=contexts
        if contexts is not None
        else [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早"},
        ],
        system_prompt="你是测试机器人。",
        conversation=types.SimpleNamespace(cid=cid),
        func_tool=None,
        image_urls=[],
        extra_user_content_parts=[],
    )


def reminders(req):
    return [
        part["text"]
        for part in req.extra_user_content_parts
        if pkg_reminder.REMINDER_BLOCK_OPEN in part.get("text", "")
    ]


def turn_contexts(count=3, tail="字" * 400):
    """count 轮、每轮正文够长的历史；用来触发上下文危险水位。"""
    messages = []
    for index in range(count):
        messages.append({"role": "user", "content": f"{index}{tail}"})
        messages.append({"role": "assistant", "content": f"回答{index}"})
    return messages


def no_provider(plugin):
    """窗口取不到、也没有轮数上限：只留每 N 轮的提醒。"""
    plugin._astrbot_window = lambda provider, umo: (0, "")
    plugin._astrbot_turn_limit = lambda umo=None: -1


def test_periodic_reminder_every_n_turns(tmp_path):
    plugin = make_plugin(tmp_path, memory_reminder_turns=2)
    no_provider(plugin)
    event = FakeEvent("Bot-A", "123")
    seen = []
    texts = []
    reqs = []
    for _ in range(4):
        req = make_request()
        reqs.append(req)
        run(plugin._process_request(event, req))
        blocks = reminders(req)
        seen.append(bool(blocks))
        texts.append(blocks[0] if blocks else "")
    assert seen == [False, True, False, True]
    text = texts[1]
    assert "已经聊了 2 轮" in text
    assert "memory_write" in text and "memory_edit" in text and "memory_delete" in text
    # 提醒是 _no_save 附加内容，不进会话历史
    part = next(
        item
        for item in reqs[1].extra_user_content_parts
        if pkg_reminder.REMINDER_BLOCK_OPEN in item["text"]
    )
    assert part["_no_save"] is True
    assert pkg_reminder.REMINDER_BLOCK_OPEN not in reqs[1].prompt
    assert pkg_reminder.REMINDER_BLOCK_OPEN not in str(reqs[1].contexts)


def test_periodic_reminder_can_be_disabled(tmp_path):
    plugin = make_plugin(tmp_path, memory_reminder_turns=0)
    no_provider(plugin)
    event = FakeEvent("Bot-A", "123")
    for _ in range(5):
        req = make_request()
        run(plugin._process_request(event, req))
        assert reminders(req) == []


def test_context_reminder_once_per_truncation(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin._astrbot_window = lambda provider, umo: (100, "")
    plugin._astrbot_turn_limit = lambda umo=None: -1
    event = FakeEvent("Bot-A", "123")
    contexts = turn_contexts()

    first = make_request(contexts=contexts)
    run(plugin._process_request(event, first))
    text = reminders(first)[0]
    assert "上下文快满了" in text

    # 仍在危险水位：不重复提醒
    second = make_request(contexts=contexts)
    run(plugin._process_request(event, second))
    assert reminders(second) == []

    # 模拟 AstrBot 截断/压缩改动了历史开头：下一次逼近时再提醒一次
    truncated = turn_contexts()
    truncated[0] = {"role": "user", "content": "新的开头" + "字" * 400}
    third = make_request(contexts=truncated)
    run(plugin._process_request(event, third))
    assert reminders(third)
    assert "上下文快满了" in reminders(third)[0]


def test_turn_limit_triggers_reminder_before_truncation(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin._astrbot_window = lambda provider, umo: (0, "")
    plugin._astrbot_turn_limit = lambda umo=None: 5
    event = FakeEvent("Bot-A", "123")
    contexts = [
        {"role": "user", "content": f"问题{i}"} for i in range(3)
    ] + [
        {"role": "assistant", "content": f"回答{i}"} for i in range(3)
    ]
    # 6 条非 system 消息 = 3 轮 = 上限 5 减 2
    first = make_request(contexts=contexts)
    run(plugin._process_request(event, first))
    assert "轮数上限减 2" in reminders(first)[0]

    second = make_request(contexts=contexts)
    run(plugin._process_request(event, second))
    assert reminders(second) == []


def test_both_triggers_share_one_reminder_block(tmp_path):
    plugin = make_plugin(tmp_path, memory_reminder_turns=1)
    plugin._astrbot_window = lambda provider, umo: (100, "")
    plugin._astrbot_turn_limit = lambda umo=None: -1
    event = FakeEvent("Bot-A", "123")
    req = make_request(contexts=turn_contexts())
    run(plugin._process_request(event, req))
    blocks = reminders(req)
    assert len(blocks) == 1
    assert "已经聊了 1 轮" in blocks[0]
    assert "上下文快满了" in blocks[0]


def test_no_reminder_when_memory_disabled(tmp_path):
    plugin = make_plugin(tmp_path, enable_memory=False, memory_reminder_turns=1)
    plugin._astrbot_window = lambda provider, umo: (100, "")
    plugin._astrbot_turn_limit = lambda umo=None: -1
    event = FakeEvent("Bot-A", "123")
    req = make_request(contexts=turn_contexts())
    run(plugin._process_request(event, req))
    assert reminders(req) == []


def test_plan_reminder_boundaries():
    # 恰好 0.70 不算"超过"；71% 才算
    plan = pkg_reminder.plan_reminder(
        turn_count=1, every=10, estimate=700, window=1000, turns=0, turn_limit=-1
    )
    assert plan.context_reasons == ()
    plan = pkg_reminder.plan_reminder(
        turn_count=1, every=10, estimate=701, window=1000, turns=0, turn_limit=-1
    )
    assert plan.context_reasons
    # 轮数上限减 2 的边界
    plan = pkg_reminder.plan_reminder(
        turn_count=1, every=10, estimate=0, window=0, turns=2, turn_limit=5
    )
    assert plan.context_reasons == ()
    plan = pkg_reminder.plan_reminder(
        turn_count=1, every=10, estimate=0, window=0, turns=3, turn_limit=5
    )
    assert plan.context_reasons
    assert pkg_reminder.build_block(pkg_reminder.ReminderPlan(), 10) == ""
