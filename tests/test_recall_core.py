"""召回：关键词打分、身份必选、分档预算与渲染。"""

from __future__ import annotations

from support import pkg_injection, pkg_memstore, pkg_origin, pkg_recall, run, thing


def header(file, description="", body="", mem_type="note", updated=1.0):
    return pkg_memstore.MemoryHeader(
        file=file,
        name=file,
        description=description,
        type=mem_type,
        mtime=updated,
        size=len(body),
        updated=updated,
        text=body.lower(),
    )


def test_tokenize_and_score():
    tokens = pkg_recall.tokenize("周六开黑吗，Hello World")
    assert "周六" in tokens and "hello" in tokens
    high = header("weekend.md", "周六开黑", "晚上八点集合")
    low = header("other.md", "工作", "写代码")
    assert pkg_recall.score(tokens, high) > pkg_recall.score(tokens, low)


def test_identity_files_from_sender_mentions_and_text():
    names = pkg_recall.identity_files("12345", ["45678"], "提到 78901 和 12345")
    assert names == ["user-12345.md", "user-45678.md", "user-78901.md"]


def test_select_by_keywords_forces_notebook_profiles_and_skips_journal():
    candidates = [
        pkg_recall.Candidate("notebook", header("user-123.md", "我", "身份")),
        pkg_recall.Candidate("notebook", header("weekend.md", "周六开黑", "八点")),
        pkg_recall.Candidate("notebook", header("journal-2026-11-01.md", "流水", "周六")),
        pkg_recall.Candidate("whisper", header("secret.md", "周六", "秘密开黑")),
    ]
    chosen = pkg_recall.select_by_keywords(
        candidates, "周六开黑", ["user-123.md"], limit=3
    )
    refs = [item.ref for item in chosen]
    assert refs[0] == "notebook/user-123.md"  # 身份必选在前
    assert "notebook/journal-2026-11-01.md" not in refs  # 日志不参与
    assert "notebook/weekend.md" in refs


def test_plan_sections_keeps_first_and_drops_over_budget():
    items = [
        pkg_recall.Recalled(f"notebook/{name}.md", char * 5000, 1.0)
        for name, char in (("a", "甲"), ("b", "乙"), ("c", "丙"), ("d", "丁"))
    ]
    sections, dropped = pkg_recall.plan_sections(items)
    assert len(sections) == 2
    assert sections[0].truncated
    assert [item.ref for item in dropped] == ["notebook/c.md", "notebook/d.md"]


def test_render_recall_block_with_pointer_line():
    item = pkg_recall.Recalled(
        "notebook/a.md", thing("a", body="正文"), 1.0, description="摘要"
    )
    sections, dropped = pkg_recall.plan_sections([item])
    block = pkg_recall.render_recall_block(
        sections, "这是数据", now=0.0, pointers=dropped
    )
    assert block.startswith(pkg_recall.RECALL_BLOCK_OPEN)
    assert "notebook/a.md" in block and "正文" in block
    assert pkg_recall.render_pointer_line(
        [pkg_recall.Recalled("notebook/b.md", "", 1.0, description="钩子")]
    ).startswith("可能也相关")


def test_freshness_note_only_after_one_day():
    assert pkg_recall.freshness_note(1000.0, now=1000.0 + 3600) == ""
    assert "3 天前" in pkg_recall.freshness_note(1000.0, now=1000.0 + 3 * 86400)


def test_assemble_recall_reads_files_and_marks_journal(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(store.write("weekend.md", thing("weekend", description="开黑", body="八点集合")))
    run(store.write("journal-2026-11-01.md", thing("journal", body="流水", mem_type="journal")))
    candidates = [
        pkg_recall.Candidate("notebook", h)
        for h in store.headers()
    ]
    plan = run(
        pkg_recall.assemble_recall(
            {"notebook": store},
            candidates,
            query="开黑",
            forced_files=[],
            limit=5,
            data_notice="数据",
            full_files=2,
        )
    )
    assert plan.block is not None
    assert "weekend.md" in plan.block
    assert "journal-2026-11-01.md" not in plan.block
    assert plan.used["notebook"] == ["weekend.md"]


def test_indexed_refs_hide_description_in_pointer_line():
    line = pkg_recall.render_pointer_line(
        [pkg_recall.Recalled("notebook/a.md", "", 1.0, description="摘要")],
        indexed_refs={"notebook/a.md"},
    )
    assert "摘要" not in line


def test_prepare_view_trims_and_keeps_origin_note():
    now = 1_700_000_000.0
    content = pkg_memstore.compose_memory("user-123", "我", "person", "身份", source="456")
    content = pkg_memstore.stamp_memory(
        content,
        now,
        origin_record=pkg_origin.Origin(
            "group",
            "456",
            "小明",
            now - 3 * 86400,
            pkg_origin.session_hash("Bot-A:GroupMessage:900"),
        ),
    )
    view = pkg_injection.prepare_view(
        "notebook/user-123.md",
        content,
        1.0,
        current_session=pkg_origin.session_hash("Bot-A:FriendMessage:123"),
        now=now,
    )
    assert "身份" in view.body
    assert "小明在群聊里说的，3 天前" in view.origin_note


def test_forced_profile_does_not_consume_full_text_quota(tmp_path):
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(store.write("user-123.md", thing("user-123", description="我", body="身份", mem_type="person")))
    run(store.write("a.md", thing("a", description="开黑", body="周六开黑")))
    run(store.write("b.md", thing("b", description="开黑备用", body="开黑时间")))
    candidates = [pkg_recall.Candidate("notebook", header) for header in store.headers()]
    plan = run(
        pkg_recall.assemble_recall(
            {"notebook": store},
            candidates,
            query="开黑",
            forced_files=["user-123.md"],
            limit=5,
            data_notice="数据",
            full_files=1,
        )
    )
    # 身份必选带全文；recall_full_files=1 的名额仍然留给一个关键词文件
    assert plan.full_files[0] == "notebook/user-123.md"
    assert len(plan.full_files) == 2
    assert any(ref in plan.full_files for ref in ("notebook/a.md", "notebook/b.md"))


def test_recall_score_ignores_origin_display_name(tmp_path):
    """frontmatter 里的来源显示名不是召回关键词（PLAN 3.9.1 第 4 条）。"""
    store = pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")
    run(
        store.write(
            "coffee.md",
            thing("coffee", description="咖啡偏好", body="喜欢喝美式"),
            origin_record=pkg_origin.Origin(
                "group",
                "456",
                "深海小鲸",
                1_700_000_000.0,
                pkg_origin.session_hash("Bot-A:GroupMessage:900"),
            ),
        )
    )
    stored = store.headers()[0]
    assert "origin" not in stored.text and "深海小鲸" not in stored.text
    query = pkg_recall.tokenize("深海小鲸最近在忙什么")
    assert pkg_recall.score(query, stored) == 0
    candidates = [pkg_recall.Candidate("notebook", stored)]
    chosen = pkg_recall.select_by_keywords(candidates, "深海小鲸最近在忙什么", [])
    assert chosen == []


def test_pointer_line_marks_origin_from_other_conversation():
    """指针行也不让别的对话里的条目看起来像本地记录。"""
    now = 1_700_000_000.0
    content = pkg_memstore.compose_memory("a", "摘要", "note", "正文")
    content = pkg_memstore.stamp_memory(
        content,
        now,
        origin_record=pkg_origin.Origin(
            "group",
            "456",
            "小明",
            now - 3 * 86400,
            pkg_origin.session_hash("Bot-A:GroupMessage:900"),
        ),
    )
    doc = pkg_memstore.parse_memory(content)
    item = pkg_recall.Recalled(
        "notebook/a.md", content, 1.0, description="摘要", origin_text=doc.origin
    )
    line = pkg_recall.render_pointer_line(
        [item],
        current_session=pkg_origin.session_hash("Bot-A:FriendMessage:123"),
        now=now,
    )
    assert "小明在群聊里说的，3 天前" in line
    assert "900" not in line
