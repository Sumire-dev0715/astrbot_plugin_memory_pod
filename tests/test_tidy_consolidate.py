"""管理页手动整理：复盘、应用、记录与撤销（不再有定期自动整理）。"""

from __future__ import annotations

import json

from support import import_core, pkg_tidy, run, thing

consolidate = import_core("consolidate")


def replies(*items):
    queue = list(items)

    async def call_model(prompt):
        return queue.pop(0) if queue else "", []

    return call_model


def test_tidy_has_no_periodic_mode_api():
    assert not hasattr(pkg_tidy, "MODE_AUTO")
    assert not hasattr(pkg_tidy, "MODE_CONFIRM")
    assert not hasattr(pkg_tidy, "due_reason")
    assert not hasattr(pkg_tidy, "normalize_mode")


def test_propose_apply_and_undo(tmp_path):
    from support import pkg_memstore

    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(store.write("a.md", thing("a", description="甲", body="旧内容")))
    run(store.write("b.md", thing("b", description="乙", body="待合并")))
    plan = [
        {
            "op": "write",
            "file": "a.md",
            "type": "reference",
            "description": "合并后",
            "content": "旧内容\n待合并",
            "reason": "合并 b",
        },
        {"op": "delete", "file": "b.md", "reason": "已合并进 a"},
    ]
    proposal = run(
        pkg_tidy.propose(
            store, "小本本", replies("<operations>" + json.dumps(plan) + "</operations>")
        )
    )
    assert len(proposal.operations) == 2
    results = run(pkg_tidy.apply(store, proposal.operations))
    assert all(item["ok"] for item in results)
    assert run(store.read("a.md")).count("待合并") == 1
    assert run(store.read("b.md")) is None
    assert store.trash_entries()

    record = pkg_tidy.make_run(pkg_tidy.TRIGGER_MANUAL, results, 1000.0)
    undone = run(pkg_tidy.undo(store, record))
    assert all(item["ok"] for item in undone)
    assert "待合并" in run(store.read("b.md"))
    assert "待合并" not in run(store.read("a.md"))


def test_propose_never_lets_model_touch_journal(tmp_path):
    from support import pkg_memstore

    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    journal = run(store.write_journal("今天的流水", now=1000.0))
    run(store.write("a.md", thing("a", body="内容")))
    plan = [
        {"op": "delete", "file": journal.file, "reason": "删日志"},
        {"op": "delete", "file": "a.md", "reason": "删普通文件"},
    ]
    proposal = run(
        pkg_tidy.propose(
            store, "小本本", replies("<operations>" + json.dumps(plan) + "</operations>")
        )
    )
    assert [op.file for op in proposal.operations] == ["a.md"]


def test_consolidate_parse_rejects_unknown_types_and_journal():
    ops = consolidate.parse_operations(
        json.dumps(
            [
                {"op": "write", "file": "ok.md", "type": "reference",
                 "description": "好", "content": "正文"},
                {"op": "write", "file": "bad.md", "type": "unknown",
                 "description": "坏", "content": "正文"},
                {"op": "write", "file": "MEMORY.md", "type": "reference",
                 "description": "坏", "content": "正文"},
            ]
        ),
        {"ok.md"},
    )
    assert [op.file for op in ops] == ["ok.md"]


def test_consolidate_prompt_mentions_three_layers_but_one_scope():
    prompt = consolidate.CONSOLIDATE_PROMPT
    assert "这一处" in prompt
    assert "留言板" in prompt and "小本本" in prompt and "悄悄话" in prompt
    assert "不要在这里跨层搬动内容" in prompt


def test_board_rewrite_keeps_signature_and_new_notice_gets_one(tmp_path):
    from support import pkg_memstore

    store = pkg_memstore.MemoryStore(tmp_path / "memories").board_scope()
    run(store.write("a.md", thing("a", description="甲", body="内容甲", signature="甲管理员")))
    run(store.write("b.md", thing("b", description="乙", body="内容乙", signature="乙管理员")))
    plan = [
        {
            "op": "write",
            "file": "a.md",
            "type": "reference",
            "description": "合并",
            "content": "内容甲\n内容乙",
            "reason": "合并 b",
            # 模型返回的署名不可信：整理必须沿用原文件的署名
            "signature": "伪造的署名",
        },
        {"op": "delete", "file": "b.md", "reason": "已合并"},
    ]
    proposal = run(
        pkg_tidy.propose(
            store, "留言板", replies("<operations>" + json.dumps(plan) + "</operations>")
        )
    )
    assert proposal.operations[0].signature == "甲管理员"
    assert proposal.operations[0].to_dict()["signature"] == "甲管理员"
    run(pkg_tidy.apply(store, proposal.operations))
    assert pkg_memstore.parse_memory(run(store.read("a.md"))).signature == "甲管理员"

    new_plan = [
        {
            "op": "write",
            "file": "merged.md",
            "type": "reference",
            "description": "合并告示",
            "content": "合并后的正文",
            "reason": "合并",
        },
        {"op": "delete", "file": "a.md", "reason": "已合并"},
    ]
    proposal = run(
        pkg_tidy.propose(
            store,
            "留言板",
            replies("<operations>" + json.dumps(new_plan) + "</operations>"),
        )
    )
    assert proposal.operations[0].signature
    run(pkg_tidy.apply(store, proposal.operations))
    assert pkg_memstore.parse_memory(run(store.read("merged.md"))).signature


def test_board_rewrite_keeps_expiry_and_new_notice_has_none(tmp_path):
    from support import pkg_memstore

    store = pkg_memstore.MemoryStore(tmp_path / "memories").board_scope()
    run(
        store.write(
            "a.md",
            thing(
                "a",
                description="甲",
                body="内容甲",
                signature="管理员",
                expires="2026-11-11",
            ),
        )
    )
    plan = [
        {
            "op": "write",
            "file": "a.md",
            "type": "reference",
            "description": "改写",
            "content": "内容甲改",
            "reason": "改正文",
        }
    ]
    proposal = run(
        pkg_tidy.propose(
            store, "留言板", replies("<operations>" + json.dumps(plan) + "</operations>")
        )
    )
    assert proposal.operations[0].expires == "2026-11-11"
    assert proposal.operations[0].to_dict()["expires"] == "2026-11-11"
    run(pkg_tidy.apply(store, proposal.operations))
    assert pkg_memstore.parse_memory(run(store.read("a.md"))).expires == "2026-11-11"

    new_plan = [
        {
            "op": "write",
            "file": "merged.md",
            "type": "reference",
            "description": "合并告示",
            "content": "合并后的正文",
            "reason": "合并",
        }
    ]
    proposal = run(
        pkg_tidy.propose(
            store, "留言板", replies("<operations>" + json.dumps(new_plan) + "</operations>")
        )
    )
    assert proposal.operations[0].expires == ""
    run(pkg_tidy.apply(store, proposal.operations))
    assert pkg_memstore.parse_memory(run(store.read("merged.md"))).expires == ""


def test_consolidate_only_accepts_expires_when_explicitly_allowed():
    plan = json.dumps(
        [
            {
                "op": "write",
                "file": "note.md",
                "type": "reference",
                "description": "笔记",
                "content": "正文",
                "expires": "2999-12-31",
            }
        ]
    )
    ops = consolidate.parse_operations(plan, {"note.md"})
    assert ops[0].expires == ""
    ops = consolidate.parse_operations(plan, {"note.md"}, allow_expires=True)
    assert ops[0].expires == "2999-12-31"


def test_notebook_consolidation_ignores_model_expires(tmp_path):
    from support import pkg_memstore

    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(store.write("note.md", thing("note", description="笔记", body="旧正文")))
    plan = [
        {
            "op": "write",
            "file": "note.md",
            "type": "reference",
            "description": "笔记",
            "content": "新正文",
            "expires": "2999-12-31",
            "reason": "改正文",
        }
    ]
    proposal = run(
        pkg_tidy.propose(
            store, "小本本", replies("<operations>" + json.dumps(plan) + "</operations>")
        )
    )
    assert proposal.operations[0].expires == ""
    run(pkg_tidy.apply(store, proposal.operations))
    assert "expires:" not in run(store.read("note.md"))


def test_apply_keeps_origin_when_merging_into_new_file(tmp_path):
    """整理新建的合并文件要继承被删源文件的 origin（PLAN 3.9.1 第 4 条）。"""
    from support import pkg_memstore, pkg_origin

    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    record = pkg_origin.Origin(
        "group",
        "123",
        "小明",
        1_700_000_000.0,
        pkg_origin.session_hash("Bot-A:GroupMessage:900"),
    )
    run(
        store.write(
            "source.md",
            thing("source", description="源文件", body="源正文"),
            source="123",
            origin_record=record,
        )
    )
    ops = [
        consolidate.Operation(
            op="write",
            file="merged.md",
            type="reference",
            description="合并后的文件",
            content="源正文",
        ),
        consolidate.Operation(
            op="delete",
            file="source.md",
            reason="已合并",
            base_mtime=store.mtime("source.md") or 0.0,
        ),
    ]
    results = run(pkg_tidy.apply(store, ops))
    assert all(item["ok"] for item in results), results
    doc = pkg_memstore.parse_memory(run(store.read("merged.md")))
    records = pkg_origin.parse(doc.origin)
    assert len(records) == 1
    assert records[0].speaker == "123"
    assert records[0].session == record.session


def test_apply_merges_source_origin_into_existing_target(tmp_path):
    from support import pkg_memstore, pkg_origin

    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(store.write("a.md", thing("a", description="甲", body="旧内容")))
    record = pkg_origin.Origin(
        "private",
        "456",
        "小红",
        1_700_000_000.0,
        pkg_origin.session_hash("Bot-A:FriendMessage:456"),
    )
    run(
        store.write(
            "b.md",
            thing("b", description="乙", body="待合并"),
            source="456",
            origin_record=record,
        )
    )
    ops = [
        consolidate.Operation(
            op="write",
            file="a.md",
            type="reference",
            description="合并后",
            content="旧内容\n待合并",
            base_mtime=store.mtime("a.md") or 0.0,
        ),
        consolidate.Operation(
            op="delete",
            file="b.md",
            reason="已合并进 a",
            base_mtime=store.mtime("b.md") or 0.0,
        ),
    ]
    results = run(pkg_tidy.apply(store, ops))
    assert all(item["ok"] for item in results), results
    doc = pkg_memstore.parse_memory(run(store.read("a.md")))
    assert [item.speaker for item in pkg_origin.parse(doc.origin)] == ["456"]


def test_tidy_scope_label_does_not_leak_group_name(tmp_path):
    key = "Bot-A_GroupMessage_987654321-abcdef12"
    label = pkg_tidy.scope_label("whisper", key)
    assert "987654321" not in label
    assert "GroupMessage" not in label
    assert "这段对话的悄悄话" in label
