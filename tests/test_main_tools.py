"""聊天侧工具：三层作用域解析、检索与已遗忘恢复规则。"""

from __future__ import annotations

import json
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


def test_tidy_uses_selected_model_by_provider_id(tmp_path):
    """整理改用管理员选定的提供商 ID 调用，不再走默认对话模型（PLAN 3.12.1）。"""
    plugin = make_plugin(tmp_path)
    plugin._tidy.save_model("provider-x", "model-x")
    plugin.tidy_model_options = lambda: [
        {"providerId": "provider-x", "model": "model-x"}
    ]
    calls = []

    async def llm_generate(**kwargs):
        calls.append(kwargs)
        return types.SimpleNamespace(completion_text="[]")

    plugin.context.llm_generate = llm_generate
    text, log = run(plugin.call_tidy_model("整理"))
    assert text == "[]"
    assert calls == [{"chat_provider_id": "provider-x", "prompt": "整理"}]
    assert log and "成功" in log[0] and "provider-x" in log[0]


def test_tidy_model_falls_back_to_get_provider_by_id(tmp_path):
    """旧版 AstrBot 没有 llm_generate 时，按 ID 找到提供商再调用 text_chat。"""
    plugin = make_plugin(tmp_path)
    plugin._tidy.save_model("provider-x", "model-x")
    plugin.tidy_model_options = lambda: [
        {"providerId": "provider-x", "model": "model-x"}
    ]
    calls = []

    class Provider:
        async def text_chat(self, prompt):
            calls.append(prompt)
            return types.SimpleNamespace(completion_text="[]")

    plugin.context.get_provider_by_id = lambda provider_id: Provider()
    text, _log = run(plugin.call_tidy_model("整理"))
    assert text == "[]"
    assert calls == ["整理"]


def test_tidy_without_selected_model_does_not_call_any_model(tmp_path):
    plugin = make_plugin(tmp_path)
    called = []

    async def llm_generate(**kwargs):
        called.append(kwargs)
        return types.SimpleNamespace(completion_text="[]")

    plugin.context.llm_generate = llm_generate
    text, log = run(plugin.call_tidy_model("整理"))
    assert text is None
    assert called == []
    assert log == ["没有选择整理模型"]


def test_tidy_model_options_list_loaded_chat_providers(tmp_path):
    plugin = make_plugin(tmp_path)

    class Inst:
        def __init__(self, provider_id, model):
            self.provider_config = {
                "id": provider_id,
                "model": model,
                "api_key": "sk-TOP-SECRET-KEY",
                "api_base": "https://secret.example/v1",
            }

        def meta(self):
            return types.SimpleNamespace(id=self.provider_config["id"], model=self.provider_config["model"])

    plugin.context.provider_manager = types.SimpleNamespace(
        provider_insts=[Inst("provider-b", "model-b"), Inst("provider-a", "model-a")]
    )
    options = plugin.tidy_model_options()
    assert options == [
        {"providerId": "provider-a", "model": "model-a"},
        {"providerId": "provider-b", "model": "model-b"},
    ]
    # 下拉框只要提供商 ID 和模型名，配置里的密钥字段不能跟着出去
    dumped = json.dumps(options, ensure_ascii=False)
    assert "sk-TOP-SECRET-KEY" not in dumped
    assert "api_key" not in dumped and "api_base" not in dumped


def test_tidy_model_options_fall_back_to_enabled_config(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.context.provider_manager = types.SimpleNamespace(
        provider_insts=[],
        providers_config=[
            {"id": "a", "enable": True, "provider_type": "chat_completion", "model": "m1", "type": "openai", "api_key": "sk-TOP-SECRET-KEY", "api_base": "https://secret.example/v1"},
            {"id": "b", "enable": False, "provider_type": "chat_completion", "model": "m2", "type": "openai", "api_key": "sk-OFF-SECRET"},
            {"id": "c", "enable": True, "provider_type": "speech_to_text", "model": "m3", "type": "openai", "api_key": "sk-STT-SECRET"},
            {"id": "d", "enable": True, "provider_type": "chat_completion", "type": "agent_runner", "api_key": "sk-AGENT-SECRET"},
        ],
    )
    options = plugin.tidy_model_options()
    assert options == [{"providerId": "a", "model": "m1"}]
    dumped = json.dumps(options, ensure_ascii=False)
    assert "SECRET" not in dumped
    assert "api_key" not in dumped and "api_base" not in dumped


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
