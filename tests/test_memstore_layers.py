"""三层记忆的目录、写入、历史/回收站与日记落篇规则。"""

from __future__ import annotations

import time

from support import pkg_journal, pkg_memstore, run, thing

NOW = 1_800_000_000.0  # 2027-01-15 20:00（本地时区）附近


def test_store_has_three_layers_and_separate_directories(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories")
    board = store.board_scope()
    notebook_a = store.notebook_scope("Bot-A")
    notebook_b = store.notebook_scope("Bot-B")
    whisper_1 = store.whisper_scope("Bot-A:FriendMessage:1")
    whisper_2 = store.whisper_scope("Bot-A:FriendMessage:2")

    assert board.root.name == "board"
    assert notebook_a.root.parent.name == "notebook"
    assert whisper_1.root.parent.name == "whisper"
    assert len({board.root, notebook_a.root, notebook_b.root, whisper_1.root, whisper_2.root}) == 5

    run(board.write("notice.md", thing("notice", body="告示")))
    run(notebook_a.write("note.md", thing("note", body="小本本")))
    run(whisper_1.write("secret.md", thing("secret", body="悄悄话")))
    assert not (notebook_b.root / "note.md").exists()
    assert not (whisper_2.root / "secret.md").exists()


def test_write_edit_delete_keep_history_and_trash(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    report = run(store.write("note.md", thing("note", body="第一版")))
    assert report.ok and report.file == "note.md"

    report = run(store.edit("note.md", "", "追加内容", mode="append"))
    assert report.ok
    assert "追加内容" in run(store.read("note.md"))
    versions = store.history_versions("note.md")
    assert versions and "第一版" in store.read_history("note.md", versions[0].version)

    report = run(store.delete("note.md"))
    assert report.ok
    assert run(store.read("note.md")) is None
    entries = store.trash_entries()
    assert entries and entries[0].file == "note.md"
    assert "第一版" in store.read_trash(entries[0].entry)


def test_single_file_size_limit(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").whisper_scope("s")
    huge = "x" * (pkg_memstore.MAX_FILE_BYTES + 1)
    report = run(store.write("huge.md", thing("huge", body=huge)))
    assert not report.ok
    assert run(store.read("huge.md")) is None


# ---------------------------------------------------------------- 日记落篇


def test_journal_new_file_then_continue_same_day(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    date = time.strftime("%Y-%m-%d", time.localtime(NOW))
    first = run(store.write_journal("今天认识了一个人", now=NOW))
    assert first.ok and first.new
    assert first.file == f"journal-{date}.md"
    content = run(store.read(first.file))
    assert content.count(f"## {date}") == 1

    later = NOW + 60
    second = run(store.write_journal("下午又聊了一次", now=later))
    assert second.ok and not second.new
    assert second.file == first.file
    content = run(store.read(first.file))
    assert content.count(f"## {date}") == 1
    assert "今天认识了一个人" in content and "下午又聊了一次" in content


def test_journal_append_that_would_cross_3000_starts_new_file(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    date = time.strftime("%Y-%m-%d", time.localtime(NOW))
    first = run(store.write_journal("a" * 2000, now=NOW))
    assert first.ok
    # 再写 2000 字会让正文超过 3000，另开一篇，而不是先撑大再新开
    second = run(store.write_journal("b" * 2000, now=NOW + 1))
    assert second.ok and second.new
    assert second.file == f"journal-{date}-2.md"
    third = run(store.write_journal("新的流水", now=NOW + 10))
    assert third.ok and not third.new
    assert third.file == second.file


def test_journal_older_than_seven_days_starts_new_file(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    first = run(store.write_journal("七天前", now=NOW))
    date = time.strftime("%Y-%m-%d", time.localtime(NOW + 8 * 86400))
    second = run(store.write_journal("八天后", now=NOW + 8 * 86400))
    assert second.ok and second.new
    assert second.file == f"journal-{date}.md"
    assert second.file != first.file


def test_journal_same_day_duplicate_gets_serial(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    date = time.strftime("%Y-%m-%d", time.localtime(NOW))
    # 直接占住当天的无序号文件，模拟它已经很旧/超长而不能再续写
    run(store.write(f"journal-{date}.md", thing(f"journal-{date}", body="x" * 4000, mem_type="journal")))
    report = run(store.write_journal("新的一篇", now=NOW))
    assert report.ok and report.new
    assert report.file == f"journal-{date}-2.md"


def test_journal_is_folded_in_index(tmp_path):
    date = time.strftime("%Y-%m-%d", time.localtime(NOW))
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(store.write("note.md", thing("note", body="普通记忆")))
    run(store.write_journal("第一篇", now=NOW))
    snapshot = run(store.load_index())
    assert "note.md" in snapshot.text
    assert "篇" in snapshot.text
    journal_lines = [
        line for line in snapshot.text.splitlines() if "journal-" in line
    ]
    assert len(journal_lines) <= 2
    assert "不自动召回" in snapshot.text
    assert pkg_journal.is_journal_name(f"journal-{date}.md")


def test_journal_append_is_not_silently_truncated(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    report = run(store.write_journal("甲" * 3000, now=NOW))
    assert report.ok
    body = pkg_memstore.parse_memory(run(store.read(report.file))).body
    assert len(body) >= 3000
    over = run(
        store.write_journal(
            "乙" * (pkg_memstore.MAX_JOURNAL_APPEND_CHARS + 1), now=NOW + 1
        )
    )
    assert not over.ok
    assert "上限" in over.message


def test_person_file_name_is_case_insensitive():
    assert pkg_memstore.is_person_file("USER-123.md")
    assert pkg_memstore.is_person_file("user-123.MD")
    assert pkg_memstore.is_person_file("User-123.md")


def test_person_file_name_accepts_lowercase_topics_only():
    assert pkg_memstore.is_person_file("user-123-games.md")
    assert pkg_memstore.is_person_file("user-123-games-2026.md")
    assert not pkg_memstore.is_person_file("user-123-Games.md")
    assert not pkg_memstore.is_person_file("user-123-.md")
    assert not pkg_memstore.is_person_file("user-123--games.md")
    assert pkg_memstore.is_primary_person_file("user-123.md")
    assert not pkg_memstore.is_primary_person_file("user-123-games.md")
    assert pkg_memstore.person_id("user-123-games.md") == "123"
    assert pkg_memstore.person_id("user-123.md") == "123"
    assert pkg_memstore.person_id("someone.md") == ""
    # 未知类型按笔记处理；人物档案由文件名决定
    assert pkg_memstore.effective_type("note.md", "legacy") == "note"
    assert pkg_memstore.effective_type("user-123-games.md", "legacy") == "person"
    assert pkg_memstore.effective_type("user-123.md", "journal") == "person"
