"""core.writer：PLAN 3.8.3 四条强制规则的统一校验与落盘。"""

from __future__ import annotations

from support import pkg_writer, run, thing


def files(notebook=None, whisper=None, board=None):
    return {
        "board": set(board or []),
        "notebook": set(notebook or []),
        "whisper": set(whisper or []),
    }


def validate(raw, *, sender="123", admin=False, **kwargs):
    return pkg_writer.validate_operations(
        raw,
        files_by_scope=files(**kwargs),
        sender=sender,
        admin=admin,
    )


def test_board_writes_require_admin():
    raw = [
        {
            "op": "write",
            "scope": "board",
            "file": "notice.md",
            "type": "reference",
            "description": "告示",
            "content": "11 月 11 日新员工 teto 入职",
            "signature": "管理员",
        }
    ]
    ops, skipped = validate(raw, sender="123", admin=False)
    assert ops == []
    assert "留言板只有管理员能写" in skipped[0]

    ops, skipped = validate(raw, sender="123", admin=True)
    assert skipped == []
    assert ops[0].scope == "board" and ops[0].signature == "管理员"

    unsigned = [dict(raw[0], signature="")]
    ops, skipped = validate(unsigned, sender="123", admin=True)
    assert not ops and "必须带署名" in skipped[0]

    ops, _ = validate(raw, board=["old.md"], sender="123", admin=False)
    deleted = [
        {"op": "delete", "scope": "board", "file": "old.md", "reason": "过期"}
    ]
    ops, skipped = validate(deleted, sender="123", admin=False)
    assert not ops and "留言板只有管理员能写" in skipped[0]


def test_person_file_only_self_or_admin_can_change():
    raw = [
        {
            "op": "write",
            "scope": "notebook",
            "file": "user-456.md",
            "type": "user",
            "description": "阿杰",
            "content": "工程师",
        }
    ]
    ops, skipped = validate(raw, sender="123", admin=False)
    assert not ops
    assert "只有本人或管理员" in skipped[0]

    ops, skipped = validate(raw, sender="456", admin=False)
    assert skipped == [] and ops[0].file == "user-456.md"

    ops, skipped = validate(raw, sender="123", admin=True)
    assert skipped == [] and ops[0].file == "user-456.md"

    # 悄悄话里的档案同样受限
    whisper = [
        {
            "op": "edit",
            "scope": "whisper",
            "file": "user-456.md",
            "mode": "append",
            "new": "补充",
        }
    ]
    ops, skipped = validate(whisper, sender="123", whisper=["user-456.md"])
    assert not ops and "只有本人或管理员" in skipped[0]


def test_non_person_files_are_not_restricted_by_owner():
    raw = [
        {
            "op": "write",
            "scope": "notebook",
            "file": "project.md",
            "type": "project",
            "description": "项目",
            "content": "进度",
        }
    ]
    ops, skipped = validate(raw, sender="123", admin=False)
    assert skipped == [] and ops[0].file == "project.md"


def test_validation_rejects_bad_names_types_and_lengths():
    bad = [
        {"op": "write", "scope": "notebook", "file": "中文.md",
         "type": "reference", "description": "x", "content": "y"},
        {"op": "write", "scope": "notebook", "file": "MEMORY.md",
         "type": "reference", "description": "x", "content": "y"},
        {"op": "write", "scope": "notebook", "file": "a.md",
         "type": "journal", "description": "x", "content": "y"},
        {"op": "write", "scope": "notebook", "file": "b.md",
         "type": "reference", "description": "", "content": "y"},
        {"op": "write", "scope": "notebook", "file": "c.md",
         "type": "reference", "description": "x",
         "content": "z" * (pkg_writer.MAX_CONTENT_CHARS + 1)},
        {"op": "write", "scope": "unknown", "file": "d.md",
         "type": "reference", "description": "x", "content": "y"},
        {"op": "edit", "scope": "notebook", "file": "missing.md",
         "mode": "append", "new": "x"},
        {"op": "delete", "scope": "notebook", "file": "missing.md"},
    ]
    ops, skipped = validate(bad, sender="123", admin=True)
    assert ops == []
    assert len(skipped) == len(bad)


def test_journal_target_only_append():
    ops, skipped = validate(
        [{"op": "edit", "scope": "notebook", "file": "journal",
          "mode": "append", "content": "今天很好"}],
        sender="123",
        admin=False,
    )
    assert skipped == [] and ops[0].file == "journal" and ops[0].mode == "append"

    ops, skipped = validate(
        [{"op": "edit", "scope": "notebook", "file": "journal",
          "mode": "replace", "old": "x", "new": "y"}],
        sender="123",
        admin=False,
    )
    assert not ops and "只支持 append" in skipped[0]

    ops, skipped = validate(
        [{"op": "edit", "scope": "board", "file": "journal",
          "mode": "append", "content": "告示"}],
        sender="123",
        admin=True,
    )
    assert not ops and "留言板不记日记" in skipped[0]


def test_apply_writes_keep_history_and_delete_goes_to_trash(tmp_path):
    store = tmp_path_store(tmp_path)
    run(store.write("note.md", thing("note", body="第一版")))
    ops, skipped = validate(
        [{"op": "edit", "scope": "notebook", "file": "note.md",
          "mode": "append", "new": "追加"}],
        sender="123",
        admin=False,
        notebook=["note.md"],
    )
    result = run(
        pkg_writer.apply_operations(
            {"notebook": store},
            ops,
            actor="Bot-A",
            source="123",
            now=1_800_000_000.0,
        )
    )
    assert skipped == [] and result.ok
    assert "追加" in run(store.read("note.md"))
    assert store.history_versions("note.md")

    ops, _ = validate(
        [{"op": "delete", "scope": "notebook", "file": "note.md", "reason": "过期"}],
        sender="123",
        admin=False,
        notebook=["note.md"],
    )
    result = run(
        pkg_writer.apply_operations(
            {"notebook": store}, ops, actor="Bot-A", source="123",
            now=1_800_000_001.0,
        )
    )
    assert result.ok
    assert run(store.read("note.md")) is None
    assert store.trash_entries()


def tmp_path_store(tmp_path):
    from support import pkg_memstore

    return pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
