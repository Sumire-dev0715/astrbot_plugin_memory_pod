"""core.writer：PLAN 3.8.3 四条强制规则的统一校验与落盘。"""

from __future__ import annotations

import json

from support import pkg_memstore, pkg_writer, run, thing


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

    # write / delete 不能用 journal 占位名，避免造出名叫 journal.md 的普通文件
    for raw in (
        {"op": "write", "scope": "notebook", "file": "journal",
         "type": "reference", "description": "x", "content": "y"},
        {"op": "delete", "scope": "notebook", "file": "journal"},
    ):
        ops, skipped = validate([raw], sender="123", admin=False)
        assert not ops and "只能通过 memory_edit 的 append 写入" in skipped[0]


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
    # 回执所需的落盘内容与说明随结果返回，调用方不必再读一遍磁盘
    assert "追加" in result.written["notebook/note.md"]
    assert result.messages and "索引行已同步" in result.messages[0]

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


def test_person_file_name_is_case_insensitive():
    raw = [
        {
            "op": "write",
            "scope": "notebook",
            "file": "USER-456.md",
            "type": "user",
            "description": "阿杰",
            "content": "工程师",
        }
    ]
    ops, skipped = validate(raw, sender="123", admin=False)
    assert not ops and "只有本人或管理员" in skipped[0]
    ops, skipped = validate(raw, sender="456", admin=False)
    assert skipped == [] and ops[0].file == "USER-456.md"


def test_edit_description_updates_frontmatter_and_index(tmp_path):
    store = tmp_path_store(tmp_path)
    run(store.write("note.md", thing("note", description="旧摘要", body="正文")))
    ops, skipped = validate(
        [{"op": "edit", "scope": "notebook", "file": "note.md",
          "mode": "append", "new": "追加", "description": "新摘要"}],
        sender="123",
        admin=False,
        notebook=["note.md"],
    )
    assert skipped == []
    result = run(
        pkg_writer.apply_operations(
            {"notebook": store}, ops, actor="Bot-A", source="123",
            now=1_800_000_000.0,
        )
    )
    assert result.ok
    content = run(store.read("note.md"))
    assert pkg_memstore.parse_memory(content).description == "新摘要"
    assert "新摘要" in (store.root / "MEMORY.md").read_text("utf-8")


def test_delete_reason_is_recorded_in_audit(tmp_path):
    store = tmp_path_store(tmp_path)
    run(store.write("note.md", thing("note", body="正文")))
    ops, _ = validate(
        [{"op": "delete", "scope": "notebook", "file": "note.md", "reason": "过期了"}],
        sender="123",
        admin=False,
        notebook=["note.md"],
    )
    result = run(
        pkg_writer.apply_operations(
            {"notebook": store}, ops, actor="Bot-A", source="123",
            now=1_800_000_000.0,
        )
    )
    assert result.ok
    audit = (store.root / ".audit.jsonl").read_text("utf-8").strip().splitlines()
    assert json.loads(audit[-1])["reason"] == "过期了"


def test_delegated_signature_format():
    assert (
        pkg_writer.delegated_signature("小明", "鲸群")
        == "小明（由 鲸群 代发）"
    )
    assert pkg_writer.delegated_signature("", "").endswith("（由 机器人 代发）")
