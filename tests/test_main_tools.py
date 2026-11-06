"""聊天侧工具：三层作用域解析、检索与已遗忘恢复规则。"""

from __future__ import annotations

import os
import time
import types

from support import (
    FakeEvent,
    main,
    make_plugin,
    pkg_memstore,
    person_file,
    run,
    thing,
)


def test_five_memory_tools_registered():
    names = {
        getattr(value, "__astrbot_llm_tool__", None)
        for value in vars(main.MemoryPodPlugin).values()
    }
    assert {name for name in names if name} == {
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_delete",
    }


def test_memory_edit_old_text_description_marks_section_required():
    """section 模式必须给小节标题，参数说明不能写成可空。"""
    from scripts import prompt_sizes

    schema = prompt_sizes.tool_schema(main.MemoryPodPlugin.memory_edit)
    desc = schema["function"]["parameters"]["properties"]["old_text"]["description"]
    assert "section 的小节标题（必填）" in desc
    assert "section 的小节标题（可空）" not in desc


def test_board_readable_by_every_conversation(tmp_path):
    plugin = make_plugin(tmp_path)
    run(
        plugin._store.board_scope().write(
            "notice.md", thing("notice", body="所有会话都能看到", signature="管理员")
        )
    )
    for event in (
        FakeEvent("Bot-A", "1", "Bot-A:FriendMessage:1"),
        FakeEvent("Bot-B", "2", "Bot-B:GroupMessage:9"),
    ):
        result = run(plugin.memory_read(event, "board", "notice.md"))
        assert "所有会话都能看到" in result


def test_notebook_is_per_bot(tmp_path):
    plugin = make_plugin(tmp_path)
    run(plugin._store.notebook_scope("Bot-A").write("note.md", thing("note", body="A 的小本本")))
    result = run(plugin.memory_read(FakeEvent("Bot-A", "1"), "notebook", "note.md"))
    assert "A 的小本本" in result
    result = run(plugin.memory_read(FakeEvent("Bot-B", "1"), "notebook", "note.md"))
    assert "A 的小本本" not in result and "不存在" in result


def test_search_all_covers_board_notebook_and_current_whisper(tmp_path):
    plugin = make_plugin(tmp_path)
    event = FakeEvent("Bot-A", "1")
    run(plugin._store.board_scope().write("b.md", thing("b", body="公告内容甲")))
    run(plugin._store.notebook_scope("Bot-A").write("n.md", thing("n", body="小本本内容乙")))
    run(
        plugin._store.whisper_scope(pkg_memstore.safe_key(event.unified_msg_origin)).write(
            "w.md", thing("w", body="悄悄话内容丙")
        )
    )
    result = run(plugin.memory_search(event, "内容", "all", "all"))
    assert "b.md" in result and "n.md" in result and "w.md" in result
    result = run(plugin.memory_search(event, "内容", "board", "all"))
    assert "b.md" in result and "n.md" not in result


def test_forgotten_person_profile_not_restored_by_other_user(tmp_path):
    plugin = make_plugin(tmp_path, enable_forgetting=True, forget_after_days=1)
    notebook = plugin._store.notebook_scope("Bot-A")
    notebook.root.mkdir(parents=True, exist_ok=True)
    path = notebook.root / "user-456.md"
    path.write_text(person_file("456", "别人的档案"), "utf-8")
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    run(notebook.forget_stale(86400.0))

    stranger = FakeEvent("Bot-A", "123")
    result = run(plugin.memory_read(stranger, "notebook", "user-456.md"))
    assert "别人的档案" in result
    assert notebook.has_forgotten("user-456.md")  # 没有替别人恢复

    owner = FakeEvent("Bot-A", "456")
    result = run(plugin.memory_read(owner, "notebook", "user-456.md"))
    assert "别人的档案" in result
    assert not notebook.has_forgotten("user-456.md")


def test_forgotten_topic_profile_not_restored_by_other_user(tmp_path):
    """主题档案和主档案一样：别人读过不替人恢复，本人读才恢复。"""
    plugin = make_plugin(tmp_path, enable_forgetting=True, forget_after_days=1)
    notebook = plugin._store.notebook_scope("Bot-A")
    notebook.root.mkdir(parents=True, exist_ok=True)
    path = notebook.root / "user-456-games.md"
    path.write_text(
        thing(
            "user-456-games",
            description="游戏",
            body="喜欢英雄联盟",
            mem_type="person",
        ),
        "utf-8",
    )
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    run(notebook.forget_stale(86400.0))

    stranger = FakeEvent("Bot-A", "123")
    result = run(plugin.memory_read(stranger, "notebook", "user-456-games.md"))
    assert "喜欢英雄联盟" in result
    assert notebook.has_forgotten("user-456-games.md")

    owner = FakeEvent("Bot-A", "456")
    result = run(plugin.memory_read(owner, "notebook", "user-456-games.md"))
    assert "喜欢英雄联盟" in result
    assert not notebook.has_forgotten("user-456-games.md")


def test_forgotten_board_notice_only_restored_by_admin(tmp_path):
    plugin = make_plugin(tmp_path, enable_forgetting=True)
    board = plugin._store.board_scope()
    board.root.mkdir(parents=True, exist_ok=True)
    path = board.root / "notice.md"
    path.write_text(thing("notice", body="旧告示"), "utf-8")
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))
    run(board.forget_stale(86400.0))

    result = run(plugin.memory_read(FakeEvent("Bot-A", "123"), "board", "notice.md"))
    assert "旧告示" in result
    assert board.has_forgotten("notice.md")

    result = run(
        plugin.memory_read(FakeEvent("Bot-A", "123", admin=True), "board", "notice.md")
    )
    assert "旧告示" in result
    assert not board.has_forgotten("notice.md")


def test_history_cleanup_only_runs_when_memory_enabled(tmp_path):
    event = FakeEvent("Bot-A", "123")

    def request():
        return types.SimpleNamespace(
            prompt="你好",
            contexts=[{"role": "user", "content": "早"}],
            system_prompt="你是测试机器人。",
            conversation=types.SimpleNamespace(cid="conv-1"),
            func_tool=None,
            image_urls=[],
            extra_user_content_parts=[],
        )

    disabled = make_plugin(tmp_path, enable_memory=False)
    calls = []
    disabled._schedule_history_cleanup = lambda: calls.append("cleanup")
    run(disabled._process_request(event, request()))
    assert calls == []

    enabled = make_plugin(tmp_path, enable_memory=True)
    enabled._schedule_history_cleanup = lambda: calls.append("cleanup")
    run(enabled._process_request(event, request()))
    assert calls == ["cleanup"]


def test_manual_consolidation_uses_default_chat_provider(tmp_path):
    plugin = make_plugin(tmp_path)
    calls = []

    class Provider:
        async def text_chat(self, prompt):
            calls.append(prompt)
            return types.SimpleNamespace(completion_text="[]")

    async def get_using_provider(umo=None):
        calls.append(umo)
        return Provider()

    plugin.context.get_using_provider = get_using_provider
    text, log = run(plugin.call_consolidator("整理"))
    assert text == "[]"
    assert calls == [None, "整理"]
    assert log and "成功" in log[0]


def test_board_notices_count_as_used_so_forgetting_does_not_drop_them(tmp_path):
    plugin = make_plugin(tmp_path, enable_forgetting=True)
    board = plugin._store.board_scope()
    old = time.time() - 10 * 86400
    run(
        board.write(
            "notice.md",
            thing("notice", body="旧告示", signature="管理员"),
            now=old,
        )
    )
    event = FakeEvent("Bot-A", "123")
    opening = run(plugin._index_block(main.RuntimeSession(), event, record_usage=True))
    assert "旧告示" in opening
    assert board.usage_of("notice.md").uses >= 1
    forgotten = run(board.forget_stale(86400.0))
    assert forgotten == []
    refreshed = run(plugin._index_block(main.RuntimeSession(), event))
    assert "旧告示" in refreshed
