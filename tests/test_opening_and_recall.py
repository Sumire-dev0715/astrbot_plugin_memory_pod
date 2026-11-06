"""人物档案强制召回与开头块：私聊 / 群聊两种情况下都不能串悄悄话。"""

from __future__ import annotations

from support import FakeEvent, main, make_plugin, pkg_injection, person_file, run, thing


def seed(plugin):
    board = plugin._store.board_scope()
    run(
        board.write(
            "notice.md",
            thing(
                "notice",
                description="新员工",
                body="11 月 11 日新员工 teto 入职",
                signature="管理员",
            ),
        )
    )
    notebook = plugin._store.notebook_scope("Bot-A")
    run(notebook.write("user-123.md", person_file("123", "身份：工程师，喜欢猫")))
    run(
        notebook.write(
            "user-456.md",
            thing("user-456", description="阿杰", body="身份：设计师", mem_type="user"),
        )
    )
    return board, notebook


def test_private_opening_pins_notebook_and_whisper_profile(tmp_path):
    plugin = make_plugin(tmp_path)
    seed(plugin)
    event = FakeEvent("Bot-A", sender_id="123")
    whisper = plugin._store.whisper_scope(memstore_key(event))
    run(whisper.write("user-123.md", person_file("123", "私密：最近在考虑换工作")))
    run(whisper.write("secret.md", thing("secret", body="这段对话的悄悄话")))

    rt = main.RuntimeSession()
    opening = run(plugin._index_block(rt, event, record_usage=True))
    assert opening
    # 留言板全文
    assert "11 月 11 日新员工 teto 入职" in opening
    assert "署名：管理员" in opening
    # 小本本与悄悄话索引
    assert "# 小本本索引" in opening
    assert "# 这段对话的悄悄话索引" in opening
    # 私聊：对方的小本本档案与悄悄话档案都在开头块
    assert "### notebook/user-123.md" in opening
    assert "### whisper/user-123.md" in opening
    assert "身份：工程师，喜欢猫" in opening
    assert "私密：最近在考虑换工作" in opening


def test_group_opening_never_contains_anyones_whisper(tmp_path):
    plugin = make_plugin(tmp_path)
    seed(plugin)
    private = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:FriendMessage:123")
    private_store = plugin._store.whisper_scope(memstore_key(private))
    run(private_store.write("user-123.md", person_file("123", "私密：健康情况")))
    run(private_store.write("secret.md", thing("secret", body="只属于私聊的秘密")))

    group = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:900")
    group_store = plugin._store.whisper_scope(memstore_key(group))
    run(group_store.write("group-secret.md", thing("group-secret", body="本群内部安排")))

    rt = main.RuntimeSession()
    opening = run(plugin._index_block(rt, group))
    assert opening
    assert "11 月 11 日新员工 teto 入职" in opening
    # 群聊里没有私聊档案与私聊悄悄话
    assert "私密：健康情况" not in opening
    assert "只属于私聊的秘密" not in opening
    assert "whisper/user-123.md" not in opening
    # 只出现这个群自己的悄悄话索引
    assert "group-secret.md" in opening


def test_group_recall_never_pulls_private_whisper(tmp_path):
    plugin = make_plugin(tmp_path)
    seed(plugin)
    private = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:FriendMessage:123")
    private_store = plugin._store.whisper_scope(memstore_key(private))
    run(private_store.write("secret.md", thing("secret", body="只属于私聊的秘密")))

    group = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:900")
    group_store = plugin._store.whisper_scope(memstore_key(group))
    run(group_store.write("group.md", thing("group", body="群里的事")))
    rt = main.RuntimeSession()
    run(plugin._index_block(rt, group))
    block = run(plugin._recall_message(group, group.unified_msg_origin, "秘密", rt))
    assert block is None or "只属于私聊的秘密" not in block


def test_recall_forces_sender_notebook_profile_in_group(tmp_path):
    plugin = make_plugin(tmp_path)
    seed(plugin)
    # 群聊不钉档案，发送者的小本本档案按身份必选召回
    event = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:900")
    rt = main.RuntimeSession()
    run(plugin._index_block(rt, event))
    block = run(plugin._recall_message(event, event.unified_msg_origin, "今天做什么", rt))
    assert block is not None
    assert "notebook/user-123.md" in block
    assert "身份：工程师，喜欢猫" in block


def test_private_whisper_profile_is_not_repeated_in_recall(tmp_path):
    plugin = make_plugin(tmp_path)
    seed(plugin)
    event = FakeEvent("Bot-A", sender_id="123")
    whisper = plugin._store.whisper_scope(memstore_key(event))
    run(whisper.write("user-123.md", person_file("123", "私密档案内容")))
    rt = main.RuntimeSession()
    opening = run(plugin._index_block(rt, event))
    assert "私密档案内容" in opening
    block = run(plugin._recall_message(event, event.unified_msg_origin, "私密档案", rt))
    assert block is None or "私密档案内容" not in block


def test_tool_search_and_read_cannot_reach_other_conversations_whisper(tmp_path):
    plugin = make_plugin(tmp_path)
    alice = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:FriendMessage:123")
    bob = FakeEvent("Bot-A", sender_id="456", umo="Bot-A:FriendMessage:456")
    alice_store = plugin._store.whisper_scope(memstore_key(alice))
    run(alice_store.write("secret.md", thing("secret", body="只属于爱丽丝的秘密")))

    # 另一个人在自己的私聊里搜不到
    result = run(plugin.memory_search(bob, "爱丽丝的秘密", "whisper", "all"))
    assert "secret.md" not in result and "只属于爱丽丝的秘密" not in result

    # 群聊里也搜不到、读不到
    group = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:900")
    result = run(plugin.memory_search(group, "爱丽丝的秘密", "all", "any"))
    assert "secret.md" not in result
    result = run(plugin.memory_read(group, "whisper", "secret.md"))
    assert "只属于爱丽丝的秘密" not in result

    # 本人私聊里能读
    result = run(plugin.memory_read(alice, "whisper", "secret.md"))
    assert "只属于爱丽丝的秘密" in result


def memstore_key(event):
    from support import pkg_memstore

    return pkg_memstore.safe_key(event.unified_msg_origin)


def test_request_injects_guidance_opening_and_recall_as_temp_parts(tmp_path):
    import types

    plugin = make_plugin(tmp_path)
    seed(plugin)
    event = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:900")
    notebook = plugin._store.notebook_scope("Bot-A")
    run(notebook.write("weekend.md", thing("weekend", description="开黑", body="每周六晚上八点")))

    req = types.SimpleNamespace(
        prompt="周六开黑吗",
        contexts=[{"role": "user", "content": "早"}, {"role": "assistant", "content": "早"}],
        system_prompt="你是测试机器人。",
        conversation=types.SimpleNamespace(cid="conv-1"),
        func_tool=None,
        image_urls=[],
        extra_user_content_parts=[],
    )
    run(plugin._process_request(event, req))
    assert "# 长期记忆（鲸群记忆）" in req.system_prompt
    texts = [part["text"] for part in req.extra_user_content_parts]
    opening = next(text for text in texts if text.startswith("<memory_pod_index>"))
    assert "11 月 11 日新员工 teto 入职" in opening
    assert "notebook/user-123.md" not in opening  # 群聊不钉档案
    assert "weekend.md" in opening  # 小本本索引
    assert any(part.get("_no_save") is True for part in req.extra_user_content_parts)
    assert any(text.startswith("<memory_pod_recall>") for text in texts)

    # 记忆不变时第二次请求的开头块逐字不变
    first = opening
    req2 = types.SimpleNamespace(
        prompt="周六开黑吗",
        contexts=req.contexts,
        system_prompt="你是测试机器人。",
        conversation=types.SimpleNamespace(cid="conv-1"),
        func_tool=None,
        image_urls=[],
        extra_user_content_parts=[],
    )
    run(plugin._process_request(event, req2))
    second = next(
        part["text"]
        for part in req2.extra_user_content_parts
        if part["text"].startswith("<memory_pod_index>")
    )
    assert second == first


def test_board_notice_from_one_bot_visible_to_another_with_signature(tmp_path):
    plugin = make_plugin(tmp_path)
    run(
        plugin._store.board_scope().write(
            "notice.md",
            thing(
                "notice",
                description="新员工",
                body="11 月 11 日新员工 teto 入职",
                signature="管理员",
            ),
        )
    )
    for platform in ("Bot-A", "Bot-B"):
        event = FakeEvent(platform, "1", f"{platform}:GroupMessage:1")
        opening = run(plugin._index_block(main.RuntimeSession(), event))
        assert "11 月 11 日新员工 teto 入职" in opening
        assert "署名：管理员" in opening


def test_opening_with_origin_note_stays_byte_stable(tmp_path):
    """开头块的来源标注在快照生成时算好，记忆不变时逐轮逐字不变。"""
    plugin = make_plugin(tmp_path)
    group = FakeEvent(
        "Bot-A",
        "123",
        umo="Bot-A:GroupMessage:900",
        name="小明",
        group_id="900",
        group_name="合成群名-甲子园",
    )
    run(
        plugin.memory_write(
            group, "notebook", "user-123.md", "user", "小明档案", "身份：工程师"
        )
    )
    private = FakeEvent("Bot-A", "123", umo="Bot-A:FriendMessage:123", name="小明")
    rt = main.RuntimeSession()
    first = run(plugin._index_block(rt, private))
    second = run(plugin._index_block(rt, private))
    assert first == second
    assert "（本人说的）" in first
    assert "合成群名-甲子园" not in first
