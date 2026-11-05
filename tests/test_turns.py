from core.turns import conversation_turns, is_internal_message


def test_is_internal_message():
    assert is_internal_message({"role": "_checkpoint", "content": {"id": "x"}})
    assert not is_internal_message({"role": "user", "content": "hi"})
    assert not is_internal_message({"role": None})
    assert not is_internal_message("junk")


def test_conversation_turns_matches_astrbot_pair_count():
    # AstrBot 的 truncate_by_turns 按非 system 消息两两一轮
    messages = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "1"},
        {"role": "assistant", "content": "2"},
        {"role": "user", "content": "3"},
        {"role": "assistant", "content": "4"},
        {"role": "_checkpoint", "content": {"id": "x"}},
    ]
    assert conversation_turns(messages) == 2
    assert conversation_turns([]) == 0
    assert conversation_turns(None) == 0
    assert conversation_turns([{"role": "user", "content": "孤"}] * 3) == 1
