"""保留下来的历史、回收站、索引、导入与搜索功能。"""

from __future__ import annotations

import json

from support import (
    import_core,
    pkg_history,
    pkg_memstore,
    pkg_registry,
    run,
    thing,
)


def store(tmp_path):
    return pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")


def test_forgotten_headers_and_read(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.forget("note.md"))
    headers = scope.forgotten_headers()
    assert [header.file for header in headers] == ["note.md"]
    assert headers[0].forgotten_at > 0
    assert "正文" in run(scope.read_forgotten("note.md"))
    assert run(scope.read("note.md")) is None


def test_restore_and_delete_forgotten(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.forget("note.md"))
    report = run(scope.restore("note.md"))
    assert report.ok
    assert "正文" in run(scope.read("note.md"))

    run(scope.forget("note.md"))
    report = run(scope.delete_forgotten("note.md"))
    assert report.ok
    assert run(scope.read_forgotten("note.md")) is None
    assert scope.trash_entries()[0].file == "note.md"


def test_finalize_rename_moves_history_and_audit(tmp_path):
    scope = store(tmp_path)
    run(scope.write("old.md", thing("old", body="旧内容")))
    run(scope.write("old.md", thing("old", body="第二版")))
    run(scope.write("new.md", thing("new", body="新内容")))
    report = run(scope.finalize_rename("old.md", "new.md", actor="webui"))
    assert report.ok
    assert not (scope.root / "old.md").exists()
    assert (scope.root / "new.md").is_file()
    versions = scope.history_versions("new.md")
    assert any("旧内容" in scope.read_history("new.md", v.version) for v in versions)
    audit = (scope.root / ".audit.jsonl").read_text("utf-8")
    assert "rename" in audit


def test_delete_file_completely_removes_history_and_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="第一版")))
    run(scope.write("note.md", thing("note", body="第二版")))
    run(scope.delete("note.md"))
    run(scope.write("note.md", thing("note", body="第三版")))
    report = run(scope.delete_file_completely("note.md"))
    assert report.ok
    assert run(scope.read("note.md")) is None
    assert scope.history_versions("note.md") == []
    assert scope.trash_entries() == []
    assert run(scope.read("MEMORY.md")) is not None


def test_rebuild_index_and_report(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", description="甲", body="内容")))
    run(scope.write("b.md", thing("b", description="乙", body="内容")))
    # 直接改磁盘上的索引（手工编辑入口已删，PLAN 3.12.3）：缺一行、钩子也旧了
    (scope.root / "MEMORY.md").write_text(
        "# 分组\n- [a](a.md) — 手工钩子\n", encoding="utf-8"
    )
    health = scope.index_report()
    assert "b.md" in health.missing
    count = run(scope.rebuild_index())
    assert count == 2
    health = scope.index_report()
    assert health.missing == [] and health.dangling == []
    assert "手工钩子" not in run(scope.read("MEMORY.md"))
    # 分组标题保留；索引只是缓存，重建不会往回收站里放副本
    assert "# 分组" in run(scope.read("MEMORY.md"))
    assert scope.trash_entries() == []


def test_rebuild_index_does_not_copy_index_to_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", description="甲", body="内容")))
    run(scope.rebuild_index())
    run(scope.rebuild_index())
    assert scope.trash_entries() == []
    assert scope.trash_count() == 0
    # 旧版本留下的 MEMORY.md 副本可以一键清掉
    trash = scope.root / ".trash"
    trash.mkdir(exist_ok=True)
    (trash / "20260101-000000-000__MEMORY.md").write_text("# 旧索引", encoding="utf-8")
    assert scope.trash_entries() == [] and scope.trash_count() == 0
    assert scope.remove_index_copies() == 1
    assert not (trash / "20260101-000000-000__MEMORY.md").exists()


def test_import_conflict_policies(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="现有内容")))
    current = run(scope.read("note.md"))

    newer = pkg_memstore.ImportEntry("note.md", thing("note", body="较旧"), mtime=1.0)
    results = run(scope.import_files([newer], pkg_memstore.CONFLICT_NEWER))
    assert results[0].outcome == pkg_memstore.IMPORT_KEPT

    newer = pkg_memstore.ImportEntry("note.md", thing("note", body="较新"), mtime=9e9)
    results = run(scope.import_files([newer], pkg_memstore.CONFLICT_NEWER))
    assert results[0].outcome == pkg_memstore.IMPORT_REPLACED
    assert "较新" in run(scope.read("note.md"))

    skipped = pkg_memstore.ImportEntry("note.md", thing("note", body="跳过"))
    results = run(scope.import_files([skipped], pkg_memstore.CONFLICT_SKIP))
    assert results[0].outcome == pkg_memstore.IMPORT_KEPT
    assert "较新" in run(scope.read("note.md"))

    forced = pkg_memstore.ImportEntry("note.md", thing("note", body="覆盖"))
    results = run(scope.import_files([forced], pkg_memstore.CONFLICT_OVERWRITE))
    assert results[0].outcome == pkg_memstore.IMPORT_REPLACED
    assert "覆盖" in run(scope.read("note.md"))
    assert current != run(scope.read("note.md"))


def test_snapshot_skips_non_utf8_files_instead_of_failing(tmp_path):
    scope = store(tmp_path)
    run(scope.write("good.md", thing("good", body="正常正文")))
    (scope.root / "bad.md").write_bytes(b"\xff\xfe not utf8")
    entries, index = scope.snapshot()
    assert {entry.name for entry in entries} == {"good.md"}
    assert "good.md" in index
    (scope.root / "MEMORY.md").write_bytes(b"\xff\xfe broken index")
    entries, index = scope.snapshot()
    assert {entry.name for entry in entries} == {"good.md"}
    assert index == ""


def test_search_modes_and_forgotten_hits(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", description="猫", body="喜欢猫和狗")))
    run(scope.write("b.md", thing("b", description="工作", body="写代码")))
    all_hits = run(scope.search("猫 狗", "all"))
    assert len(all_hits) == 1 and "a.md" in all_hits[0]
    any_hits = run(scope.search("猫 代码", "any"))
    assert {hit.split(":", 1)[0] for hit in any_hits} == {"a.md", "b.md"}

    run(scope.forget("a.md"))
    assert any("a.md" in hit for hit in run(scope.search_forgotten("猫", "all")))


def test_usage_statistics_and_long_files(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="内容")))
    scope.note_used(["note.md"], now=1000.0)
    assert scope.usage_of("note.md").uses == 1
    assert scope.usage_of("note.md").last_used == 1000.0
    assert run(scope.long_files()) == []


def test_cleanup_scope_archives(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.purge())
    archives = tmp_path / "memories" / pkg_history.SCOPE_TRASH_DIRNAME
    assert archives.is_dir()
    removed = pkg_history.cleanup_scope_archives(archives, 1, 1e12)
    assert removed >= 1
    assert not archives.exists() or not any(archives.iterdir())


# ---------------------------------------------------------------- 每条变更路径都可恢复


def test_write_over_forgotten_name_keeps_copy_in_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("p.md", thing("p", body="第一版")))
    run(scope.forget("p.md"))
    forgotten = run(scope.read_forgotten("p.md"))
    run(scope.write("p.md", thing("p", body="第二版")))
    assert scope.forgotten_files() == []
    entries = [
        item
        for item in scope.trash_entries()
        if item.file == "p.md"
        and item.action == pkg_history.ACTION_DISCARD_FORGOTTEN
    ]
    assert len(entries) == 1
    assert scope.read_trash(entries[0].entry) == forgotten


def test_import_overwrite_keeps_old_version(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="现有")))
    before = run(scope.read("a.md"))
    results = run(
        scope.import_files(
            [pkg_memstore.ImportEntry("a.md", thing("a", body="导入的"))],
            pkg_memstore.CONFLICT_OVERWRITE,
            actor="transfer",
        )
    )
    assert results[0].landed
    versions = scope.history_versions("a.md")
    assert versions[0].action == pkg_history.ACTION_IMPORT
    assert versions[0].actor == "transfer"
    assert scope.read_history("a.md", versions[0].version) == before


def test_restore_trash_over_active_saves_current_first(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="回收站版本")))
    archived = run(scope.read("a.md"))
    run(scope.delete("a.md"))
    run(scope.write("a.md", thing("a", body="在册版本")))
    current = run(scope.read("a.md"))
    entry = next(item for item in scope.trash_entries() if item.file == "a.md")
    assert run(scope.restore_trash(entry.entry)).ok
    assert run(scope.read("a.md")) == archived
    assert any(
        scope.read_history("a.md", version.version) == current
        for version in scope.history_versions("a.md")
    )


def test_transfer_move_keeps_source_in_scope_trash(tmp_path):
    transfer = import_core("transfer")
    memory_store = pkg_memstore.MemoryStore(tmp_path / "memories")
    registry = pkg_registry.SessionRegistry(tmp_path / "sessions.json")
    mover = transfer.MemoryTransfer(memory_store, registry)
    source = memory_store.notebook_scope("Bot-A")
    run(source.write("a.md", thing("a", body="要搬走")))
    before = run(source.read("a.md"))
    report = run(
        mover.transfer(
            transfer.ScopeRef("notebook", "Bot-A"),
            transfer.ScopeRef("notebook", "Bot-B"),
            mode=transfer.MODE_MOVE,
        )
    )
    assert report["moved"] == 1
    assert source.list_files() == []
    entries = [item for item in source.trash_entries() if item.file == "a.md"]
    assert len(entries) == 1
    assert source.read_trash(entries[0].entry) == before


def test_history_and_trash_never_show_up_as_memory(tmp_path):
    scope = store(tmp_path)
    now = 1_800_000_000.0
    run(scope.write("a.md", thing("a", body="绝密旧版本一号")))
    run(scope.write("a.md", thing("a", body="当前版本")))
    run(scope.write("b.md", thing("b", body="回收站内容二号")))
    run(scope.delete("b.md"))
    scope.note_used(["a.md"], now=now - 86400)
    assert run(scope.forget_stale(30 * 86400, now)) == []

    assert scope.list_files() == ["a.md"]
    header = scope.headers()[0]
    assert header.file == "a.md"
    assert "绝密旧版本一号" not in header.text
    assert "回收站内容二号" not in header.text
    index = run(scope.read("MEMORY.md"))
    assert "a.md" in index and "b.md" not in index
    assert "绝密旧版本一号" not in index and "回收站内容二号" not in index
    assert run(scope.search("绝密旧版本一号")) == []
    assert run(scope.search("回收站内容二号")) == []
    assert run(scope.search("当前版本")) == ["a.md: 当前版本"]
    report = scope.index_report()
    assert report.missing == [] and report.dangling == []
    assert set(scope.usage()) == {"a.md"}
    # 历史与回收站原样留着，只是不参与上面的任何扫描
    assert scope.history_versions("a.md")
    assert scope.trash_entries()


# ---------------------------------------------------------------- 保留期与审计


def test_history_cleanup_keeps_latest_ten_versions(tmp_path, monkeypatch):
    base = 1_800_000_000.0
    monkeypatch.setattr(pkg_history, "_now", lambda: base)
    scope = store(tmp_path)
    for index in range(13):
        run(scope.write("a.md", thing("a", body=f"第 {index} 版")))
    versions = scope.history_versions("a.md")
    assert len(versions) == 12

    scope.set_retention(30, 0)
    removed = scope.cleanup_history(now=base + 100 * 86400, force=True)
    assert removed["versions"] == 2
    kept = scope.history_versions("a.md")
    assert len(kept) == pkg_history.MIN_KEEP_VERSIONS
    assert kept[0].version == versions[0].version
    assert kept[-1].version == versions[9].version
    # 非 force 的调用被 6 小时间隔挡住
    assert scope.cleanup_history(now=base + 100 * 86400) == {}


def test_history_cleanup_zero_keeps_everything(tmp_path, monkeypatch):
    base = 1_800_000_000.0
    monkeypatch.setattr(pkg_history, "_now", lambda: base)
    scope = store(tmp_path)
    for index in range(13):
        run(scope.write("a.md", thing("a", body=f"第 {index} 版")))
    scope.set_retention(0, 0)
    assert scope.cleanup_history(now=base + 100 * 86400, force=True) == {
        "versions": 0,
        "trash": 0,
    }
    assert len(scope.history_versions("a.md")) == 12


def test_trash_cleanup_respects_keep_days(tmp_path, monkeypatch):
    base = 1_800_000_000.0
    monkeypatch.setattr(pkg_history, "_now", lambda: base)
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="甲")))
    run(scope.write("b.md", thing("b", body="乙")))
    run(scope.delete("a.md"))
    run(scope.delete("b.md"))
    scope.set_retention(0, 30)
    assert scope.cleanup_history(now=base + 100 * 86400, force=True)["trash"] == 2
    assert scope.trash_entries() == []

    run(scope.write("c.md", thing("c", body="丙")))
    run(scope.delete("c.md"))
    scope.set_retention(0, 0)
    assert scope.cleanup_history(now=base + 1000 * 86400, force=True)["trash"] == 0
    assert len(scope.trash_entries()) == 1


def test_audit_records_every_change(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="第一版"), source="10001", actor="Bot-A"))
    first = run(scope.read("a.md"))
    run(scope.write("a.md", thing("a", body="第二版"), source="10001", actor="Bot-A"))
    run(scope.edit("a.md", "第二版", "改过", source="10001", actor="Bot-A"))
    run(scope.delete("a.md", actor="webui"))
    rows = [
        json.loads(line)
        for line in (scope.root / pkg_history.AUDIT_FILENAME)
        .read_text("utf-8")
        .splitlines()
    ]
    assert [row["action"] for row in rows] == [
        pkg_history.ACTION_WRITE,
        pkg_history.ACTION_WRITE,
        pkg_history.ACTION_EDIT,
        pkg_history.ACTION_DELETE,
    ]
    assert rows[0]["before_sha1"] == "" and rows[0]["after_sha1"]
    assert rows[1]["before_sha1"] == pkg_history.content_sha1(first)
    assert rows[1]["actor"] == "Bot-A" and rows[1]["source"] == "10001"
    for row in rows:
        assert set(row) == {
            "ts",
            "action",
            "file",
            "actor",
            "source",
            "before_sha1",
            "after_sha1",
            "session",
            "group",
            "admin",
        }
        # 不经聊天工具写入时没有会话上下文：字段留空，不虚报
        assert row["session"] == "" and row["group"] == "" and row["admin"] is False


def test_import_refuses_non_utf8_existing_file_without_aborting_batch(tmp_path):
    scope = store(tmp_path)
    scope.root.mkdir(parents=True, exist_ok=True)
    bad = scope.root / "bad.md"
    bad.write_bytes(b"\xff\xfe\x00broken")
    results = run(
        scope.import_files(
            [
                pkg_memstore.ImportEntry("bad.md", thing("bad", body="坏的")),
                pkg_memstore.ImportEntry("ok.md", thing("ok", body="好的")),
            ],
            pkg_memstore.CONFLICT_OVERWRITE,
        )
    )
    assert results[0].outcome == pkg_memstore.IMPORT_INVALID
    assert "读取现有文件失败" in results[0].reason
    assert bad.read_bytes() == b"\xff\xfe\x00broken"
    assert results[1].landed
    assert "好的" in run(scope.read("ok.md"))


def test_import_overwrite_forgotten_copy_goes_to_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="睡着的旧版")))
    run(scope.forget("a.md"))
    forgotten = run(scope.read_forgotten("a.md"))
    results = run(
        scope.import_files(
            [
                pkg_memstore.ImportEntry(
                    "a.md", thing("a", body="导入的睡着版"), forgotten=True
                )
            ],
            pkg_memstore.CONFLICT_OVERWRITE,
            actor="transfer",
        )
    )
    assert results[0].landed
    entries = [item for item in scope.trash_entries() if item.file == "a.md"]
    assert len(entries) == 1 and entries[0].action == pkg_history.ACTION_IMPORT
    assert scope.read_trash(entries[0].entry) == forgotten
    assert "导入的睡着版" in run(scope.read_forgotten("a.md"))


def test_restore_audit_records_actor(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="甲")))
    run(scope.forget("a.md"))
    assert run(scope.restore("a.md", actor="Bot-A")).ok
    rows = [
        json.loads(line)
        for line in (scope.root / pkg_history.AUDIT_FILENAME)
        .read_text("utf-8")
        .splitlines()
    ]
    assert rows[-1]["action"] == pkg_history.ACTION_RESTORE
    assert rows[-1]["actor"] == "Bot-A"


def test_trash_count_matches_list_and_ignores_junk(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="甲")))
    run(scope.write("b.md", thing("b", body="乙")))
    run(scope.delete("a.md"))
    run(scope.delete("b.md"))
    trash = scope.root / pkg_history.TRASH_DIRNAME
    (trash / "notes.txt").write_text("不是回收站条目", "utf-8")
    (trash / "99999999-000000-000__bad.md").write_text("坏时间戳", "utf-8")
    assert scope.trash_count() == len(scope.trash_entries()) == 2


def _audit_rows(scope):
    return [
        json.loads(line)
        for line in (scope.root / pkg_history.AUDIT_FILENAME)
        .read_text("utf-8")
        .splitlines()
    ]


def test_restore_and_discard_audit_keep_session_context(tmp_path):
    scope = store(tmp_path)
    ctx = pkg_history.AuditContext(
        session="Bot-A:GroupMessage:987654321",
        group="星河秘密基地-甲子园",
        admin=False,
    )
    run(scope.write("a.md", thing("a", body="第一版"), actor="Bot-A"))
    run(scope.forget("a.md"))
    run(scope.restore("a.md", actor="Bot-A", audit=ctx))
    restore = [
        row for row in _audit_rows(scope) if row["action"] == pkg_history.ACTION_RESTORE
    ][-1]
    assert restore["session"] == ctx.session
    assert restore["group"] == ctx.group
    assert restore["admin"] is False

    run(scope.write("b.md", thing("b", body="旧")))
    run(scope.forget("b.md"))
    run(scope.write("b.md", thing("b", body="新"), actor="Bot-A", audit=ctx))
    discarded = [
        row
        for row in _audit_rows(scope)
        if row["action"] == pkg_history.ACTION_DISCARD_FORGOTTEN
    ][-1]
    assert discarded["session"] == ctx.session
    assert discarded["group"] == ctx.group


def test_purge_audit_row_keeps_context_keys(tmp_path):
    scope = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-B")
    run(scope.write("c.md", thing("c", body="正文")))
    run(scope.purge(actor="webui"))
    archives = tmp_path / "memories" / pkg_history.SCOPE_TRASH_DIRNAME
    archive = next(path for path in archives.iterdir() if path.is_dir())
    rows = [
        json.loads(line)
        for line in (archive / pkg_history.AUDIT_FILENAME).read_text("utf-8").splitlines()
    ]
    purge = next(row for row in rows if row["action"] == pkg_history.ACTION_PURGE_SCOPE)
    assert {"session", "group", "admin"} <= set(purge)
    assert purge["session"] == "" and purge["group"] == ""
    assert purge["admin"] is False


def test_rename_keeps_audit_session_context(tmp_path):
    scope = store(tmp_path)
    ctx = pkg_history.AuditContext(
        session="Bot-A:GroupMessage:987654321",
        group="星河秘密基地-甲子园",
        admin=True,
    )
    run(scope.write("a.md", thing("a", body="旧")))
    run(scope.write("a.md", thing("a", body="第二版"), actor="Bot-A", audit=ctx))
    run(scope.write("b.md", thing("b", body="新"), actor="Bot-A"))
    report = run(scope.finalize_rename("a.md", "b.md", actor="webui"))
    assert report.ok
    moved = [
        row
        for row in _audit_rows(scope)
        if row["file"] == "b.md" and row["action"] == pkg_history.ACTION_WRITE
    ]
    assert any(
        row["session"] == ctx.session
        and row["group"] == ctx.group
        and row["admin"] is True
        for row in moved
    )


def test_rename_keeps_trash_audit_session_context(tmp_path):
    scope = store(tmp_path)
    ctx = pkg_history.AuditContext(
        session="Bot-A:GroupMessage:987654321",
        group="星河秘密基地-甲子园",
        admin=False,
    )
    run(scope.write("a.md", thing("a", body="旧")))
    run(scope.delete("a.md", actor="Bot-A", reason="清理", audit=ctx))
    run(scope.write("a.md", thing("a", body="重建")))
    run(scope.write("b.md", thing("b", body="新")))
    report = run(scope.finalize_rename("a.md", "b.md", actor="webui"))
    assert report.ok
    moved = [
        row
        for row in _audit_rows(scope)
        if row["file"] == "b.md" and row["action"] == pkg_history.ACTION_DELETE
    ]
    assert moved and moved[-1]["session"] == ctx.session
    assert moved[-1]["group"] == ctx.group
