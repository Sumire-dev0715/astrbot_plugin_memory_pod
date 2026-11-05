"""数据目录里不认识的旧目录直接忽略：不读、不写、不删，也不报错。

PLAN 3.8.2：旧版本的 `session/`、`people/`、`global/`、`archive/` 等目录
留在数据目录里也不影响插件与管理页。
"""

from __future__ import annotations

from test_sharing_tools import FakeContext, FakeEvent, main, run


def _make_old_dirs(tmp_path):
    """在数据目录与 memories/ 下造一批旧目录，每个都放一个标记文件。"""
    marker_dirs = [
        tmp_path / "archive",
        tmp_path / "session",
        tmp_path / "people",
        tmp_path / "global",
        tmp_path / "memories" / "archive",
        tmp_path / "memories" / "unknown-layer",
        tmp_path / "memories" / "session" / "not a valid key",
    ]
    for directory in marker_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "old.md").write_text("旧目录里的内容", "utf-8")
    return marker_dirs


def test_unknown_old_dirs_are_ignored_and_left_untouched(tmp_path, monkeypatch):
    marker_dirs = _make_old_dirs(tmp_path)
    monkeypatch.setattr(
        main.MemoryPodPlugin,
        "_resolve_data_dir",
        staticmethod(lambda: tmp_path),
    )
    monkeypatch.setattr(
        main.MemoryPodPlugin, "_ensure_pending_loop", lambda self: None
    )

    plugin = main.MemoryPodPlugin(FakeContext(), {"enable_memory": True})
    # 旧目录不会被当成作用域
    assert plugin._store.session_keys() == []
    assert plugin._store.people_keys() == []
    # 保留期清理只处理认识的作用域，旧目录原样保留
    plugin._store.cleanup_history(
        now=0, scopes=plugin._store.scopes_for_cleanup()
    )
    for directory in marker_dirs:
        assert (directory / "old.md").is_file(), directory

    # 工具与开头块照常工作，不因为旧目录报错
    event = FakeEvent("Bot-A")
    assert "没有找到" in run(plugin.memory_search(event, "旧目录", scope="session"))
    assert run(plugin._index_block(main.RuntimeSession(), event)) is None
    assert run(plugin._recall_message(event, event.unified_msg_origin, "旧目录")) is None

    # 管理页总览照常返回
    overview = run(plugin.dashboard.overview())
    assert overview["status"] == "ok"
    assert overview["data"]["sessions"] == []
    assert overview["data"]["people"]["platforms"] == []


def test_old_scope_dirs_hidden_files_do_not_break_tools(tmp_path, monkeypatch):
    """旧目录里的隐藏备份等文件不在 .md 列表里，工具与索引都看不到。"""
    hidden = tmp_path / "archive"
    hidden.mkdir()
    (hidden / ".history").mkdir()
    (hidden / ".history" / "x.md").write_text("旧历史", "utf-8")
    monkeypatch.setattr(
        main.MemoryPodPlugin,
        "_resolve_data_dir",
        staticmethod(lambda: tmp_path),
    )
    monkeypatch.setattr(
        main.MemoryPodPlugin, "_ensure_pending_loop", lambda self: None
    )
    plugin = main.MemoryPodPlugin(FakeContext(), {"enable_memory": True})
    event = FakeEvent("Bot-A")
    session = plugin._session_scope(event)
    assert session.list_files() == []
    assert "没有找到" in run(plugin.memory_search(event, "旧历史", scope="session"))
