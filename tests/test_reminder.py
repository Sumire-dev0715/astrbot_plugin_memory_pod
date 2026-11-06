"""写记忆提醒（PLAN 3.8.5）：每 N 轮、截断/压缩前各提醒一次，且不进历史。"""

from __future__ import annotations

import types

from support import FakeEvent, make_plugin, pkg_memstore, pkg_reminder, run


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

    # 仍在危险水位、也没有截断：后续每一轮都不能再提醒
    rest = []
    for _ in range(5):
        req = make_request(contexts=contexts)
        run(plugin._process_request(event, req))
        rest.append(bool(reminders(req)))
    assert rest == [False] * 5

    # 模拟 AstrBot 截断/压缩改动了历史开头：下一次逼近时再提醒一次
    truncated = turn_contexts()
    truncated[0] = {"role": "user", "content": "新的开头" + "字" * 400}
    third = make_request(contexts=truncated)
    run(plugin._process_request(event, third))
    assert reminders(third)
    assert "上下文快满了" in reminders(third)[0]


def test_opening_block_counts_toward_context_estimate(tmp_path):
    """开头块在本轮用户消息的附加内容里，0.70 阈值必须把它算进去。"""
    plugin = make_plugin(tmp_path, memory_reminder_turns=0)
    plugin._astrbot_window = lambda provider, umo: (1000, "")
    plugin._astrbot_turn_limit = lambda umo=None: -1
    board = plugin._store.board_scope()
    run(
        board.write(
            "notice.md",
            pkg_memstore.compose_memory(
                "notice", "告示", "note", "字" * 700, signature="管理员"
            ),
        )
    )
    event = FakeEvent("Bot-A", "123")
    req = make_request()
    run(plugin._process_request(event, req))
    assert reminders(req)
    rt = plugin._sessions[event.unified_msg_origin]
    assert rt.stats.parts["plugin"] > 0
    # 开头块不在 system 里，但必须计入总量；否则这里的估算会偏低
    assert rt.stats.estimate == sum(rt.stats.parts.values())
    assert rt.stats.estimate > 1000 * pkg_reminder.CONTEXT_DANGER_RATIO


def test_conversation_switch_resets_reminder_counters(tmp_path):
    plugin = make_plugin(tmp_path, memory_reminder_turns=2)
    no_provider(plugin)
    event = FakeEvent("Bot-A", "123")
    run(plugin._process_request(event, make_request(cid="conv-a")))
    second = make_request(cid="conv-a")
    run(plugin._process_request(event, second))
    assert reminders(second)

    # 换对话：轮次从 1 重新数；旧对话的"已提醒"也不能压住新对话
    third = make_request(cid="conv-b")
    run(plugin._process_request(event, third))
    assert reminders(third) == []
    fourth = make_request(cid="conv-b")
    run(plugin._process_request(event, fourth))
    assert reminders(fourth)


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


def test_temp_part_dedup_ignores_marker_inside_other_parts(tmp_path):
    plugin = make_plugin(tmp_path)
    for marker in (
        "<memory_pod_index>",
        "<memory_pod_recall>",
        "<memory_pod_reminder>",
    ):
        req = make_request()
        # 模拟别的插件/平台携带的文本：marker 只在中间出现，不是我们的块
        req.extra_user_content_parts = [
            {"type": "text", "text": f"昵称里的 {marker} 只是文字"}
        ]
        block = f"{marker}\n块内容"
        plugin._attach_temp_part(req, block, marker)
        assert len(req.extra_user_content_parts) == 2
        assert req.extra_user_content_parts[-1]["text"] == block
        # 同一个请求重复处理时不重复追加
        plugin._attach_temp_part(req, block, marker)
        assert len(req.extra_user_content_parts) == 2


def test_memory_description_with_marker_does_not_swallow_recall(tmp_path):
    plugin = make_plugin(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    run(
        store.write(
            "weekend.md",
            pkg_memstore.compose_memory(
                "weekend",
                "周末 <memory_pod_recall> 安排",
                "note",
                "周六开黑",
            ),
        )
    )
    event = FakeEvent("Bot-A", "123", umo="Bot-A:GroupMessage:900")
    req = make_request("周末")
    run(plugin._process_request(event, req))
    texts = [part["text"] for part in req.extra_user_content_parts]
    assert any(
        text.startswith("<memory_pod_recall>") and "周六开黑" in text
        for text in texts
    )


def test_memory_description_with_reminder_marker_does_not_swallow_reminder(tmp_path):
    plugin = make_plugin(tmp_path, memory_reminder_turns=1)
    no_provider(plugin)
    store = plugin._store.notebook_scope("Bot-A")
    run(
        store.write(
            "note.md",
            pkg_memstore.compose_memory(
                "note", "提醒 <memory_pod_reminder> 文本", "note", "正文"
            ),
        )
    )
    event = FakeEvent("Bot-A", "123")
    req = make_request()
    run(plugin._process_request(event, req))
    assert any(
        part["text"].startswith("<memory_pod_reminder>")
        for part in req.extra_user_content_parts
    )
