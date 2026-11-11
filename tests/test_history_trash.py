"""自动历史、回收站与整作用域归档在三级记忆里照常工作。"""

from __future__ import annotations

import logging

from support import (
    FakeContext,
    FakeEvent,
    main,
    make_plugin,
    pkg_history,
    pkg_memstore,
    run,
    thing,
)


def store(tmp_path):
    return pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")


def trash_copy(root, name="MEMORY.md", stamp="20260101-000000-000"):
    """在作用域目录的回收站里放一条副本，模拟旧版本留下的文件。"""
    trash = root / ".trash"
    trash.mkdir(parents=True, exist_ok=True)
    path = trash / f"{stamp}__{name}"
    path.write_text("# 旧内容\n", encoding="utf-8")
    return path


def test_every_overwrite_keeps_the_previous_version(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="第一版")))
    run(scope.write("note.md", thing("note", body="第二版")))
    run(scope.edit("note.md", "", "追加", mode="append"))
    versions = scope.history_versions("note.md")
    assert len(versions) >= 2
    assert "第二版" in scope.read_history("note.md", versions[0].version)
    assert "第一版" in scope.read_history("note.md", versions[1].version)


def test_restore_history_version_keeps_current_as_history(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="第一版")))
    run(scope.write("note.md", thing("note", body="第二版")))
    first = scope.history_versions("note.md")[-1]
    report = run(scope.restore_history("note.md", first.version))
    assert report.ok
    assert "第一版" in run(scope.read("note.md"))
    assert "第二版" in scope.read_history("note.md", scope.history_versions("note.md")[0].version)


def test_delete_then_restore_from_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.delete("note.md"))
    entry = scope.trash_entries()[0]
    report = run(scope.restore_trash(entry.entry))
    assert report.ok
    assert "正文" in run(scope.read("note.md"))
    assert not scope.trash_entries()


def test_purge_scope_moves_directory_into_archive(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    removed = run(scope.purge(actor="webui"))
    assert removed == 1
    assert not scope.root.exists()
    archives = tmp_path / "memories" / pkg_history.SCOPE_TRASH_DIRNAME
    assert archives.is_dir()
    assert any(path.is_dir() for path in archives.iterdir())


def test_cleanup_removes_old_trash_and_keeps_recent(tmp_path):
    scope = store(tmp_path)
    scope.set_retention(1, 1)
    run(scope.write("old.md", thing("old", body="旧")))
    run(scope.delete("old.md"))
    old = scope.trash_entries()[0]
    now = old.ts + 10 * 86400
    scope.cleanup_history(now=now, force=True)
    assert not scope.trash_entries()


def test_trash_list_and_count_hide_index_copies(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.delete("note.md"))
    index_copy = trash_copy(scope.root)
    assert [item.file for item in scope.trash_entries()] == ["note.md"]
    assert scope.trash_count() == 1
    # 恢复索引的分支已删除：这条副本恢复不了，MEMORY.md 保持原样
    before = run(scope.read("MEMORY.md"))
    report = run(scope.restore_trash(index_copy.name))
    assert not report.ok
    assert run(scope.read("MEMORY.md")) == before
    assert index_copy.is_file()


def test_memory_store_removes_index_copies_from_every_layer(tmp_path):
    memories = tmp_path / "memories"
    store_obj = pkg_memstore.MemoryStore(memories)
    board = store_obj.board_scope()
    notebook = store_obj.notebook_scope("Bot-A")
    whisper = store_obj.whisper_scope("Bot-A:FriendMessage:1")
    copies = [
        trash_copy(board.root),
        trash_copy(notebook.root),
        trash_copy(whisper.root),
    ]
    # 普通回收站条目不是索引副本，不能删
    run(notebook.write("note.md", thing("note", body="正文")))
    run(notebook.delete("note.md"))
    keep = [item.entry for item in notebook.trash_entries()]
    assert len(keep) == 1

    assert store_obj.remove_index_copies() == len(copies)
    assert not any(path.exists() for path in copies)
    assert notebook.trash_entries()
    assert store_obj.remove_index_copies() == 0


def test_plugin_load_cleans_index_copies_and_logs(tmp_path, monkeypatch, caplog):
    data_dir = tmp_path / "data"
    copy = trash_copy(data_dir / "memories" / "board")
    monkeypatch.setattr(
        main.MemoryPodPlugin,
        "_resolve_data_dir",
        staticmethod(lambda: data_dir),
    )
    caplog.set_level(logging.INFO, logger="memory_pod_test")
    main.MemoryPodPlugin(FakeContext(), {})
    assert not copy.exists()
    assert any(
        "MEMORY.md" in record.getMessage() and "1 份" in record.getMessage()
        for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_model_tools_cannot_reach_audit_history_or_trash(tmp_path):
    """审计、历史与回收站不是记忆，模型的工具读不到（含群名的审计尤其重要）。"""
    plugin = make_plugin(tmp_path)
    event = FakeEvent(
        "Bot-A",
        "123",
        umo="Bot-A:GroupMessage:987654321",
        group_id="987654321",
        group_name="星河秘密基地-甲子园",
    )
    run(
        plugin.memory_write(
            event, "notebook", "note.md", "note", "摘要", "正文"
        )
    )
    run(plugin.memory_read(event, "notebook", "note.md"))
    run(
        plugin.memory_write(
            event, "notebook", "note.md", "note", "摘要", "第二版"
        )
    )
    run(
        plugin.memory_write(
            event, "notebook", "gone.md", "note", "摘要", "要删"
        )
    )
    run(plugin.memory_delete(event, "notebook", "gone.md", "清理"))
    store = plugin._store.notebook_scope("Bot-A")
    assert (store.root / pkg_history.AUDIT_FILENAME).is_file()
    assert store.history_versions("note.md")
    assert store.trash_entries()

    for name in (
        pkg_history.AUDIT_FILENAME,
        ".history/note.md/20260101-000000-000-write.md",
        ".trash/20260101-000000-000__gone.md",
        "forgotten/gone.md",
    ):
        text = run(plugin.memory_read(event, "notebook", name))
        assert "星河秘密基地" not in text
        assert "987654321" not in text
        assert "before_sha1" not in text and "after_sha1" not in text
        assert "第二版" not in text and "要删" not in text

    for query in ("audit", "origin", "before_sha1", ".history", ".trash"):
        text = run(plugin.memory_search(event, query))
        assert "before_sha1" not in text
        assert pkg_history.AUDIT_FILENAME not in text
        assert "星河秘密基地" not in text and "987654321" not in text
