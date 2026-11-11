"""注入索引的紧凑渲染与超限丢行（3.6 第 4 条）。

MEMORY.md 磁盘格式不变；只有注入文本紧凑化、超限时按使用情况丢行。
"""

import asyncio

from core import index_view, journal, memstore
from core.memstore import INDEX_FILENAME, ScopeStore, format_index_line


def run(coro):
    return asyncio.run(coro)


def memory(
    name: str,
    description: str,
    updated: str = "2026-01-01 12:00",
    mem_type: str = "person",
) -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        f"  updated: {updated}\n"
        "---\n\n"
        "正文。\n"
    )


def lines(*names: str) -> list[str]:
    return [format_index_line(name, f"{name} 的描述") for name in names]


# ---------------------------------------------------------------- 紧凑渲染


def test_injection_is_compact_but_memory_md_format_is_unchanged(tmp_path):
    store = ScopeStore(tmp_path)
    run(store.write("a.md", memory("a", "甲的描述")))
    raw = (tmp_path / INDEX_FILENAME).read_text("utf-8")
    assert raw.strip() == "- [a](a.md) — 甲的描述"

    snap = run(store.load_index())
    assert snap.text == "- a.md — 甲的描述"
    assert snap.loaded_lines == snap.total_lines == 1
    # 写索引 / 管理页读到的仍是原格式
    assert (tmp_path / INDEX_FILENAME).read_text("utf-8") == raw


def test_compact_keeps_headings_and_non_entry_lines():
    raw = "# 会话记忆索引\n\n- [名称](a.md) — 描述\n- b.md — 已有紧凑行\n说明"
    view = index_view.render_for_injection(raw)
    assert view.text == "# 会话记忆索引\n\n- a.md — 描述\n- b.md — 已有紧凑行\n说明"
    assert not view.truncated


def test_entry_target_accepts_both_formats():
    assert index_view.entry_target("- [a](a.md) — 描述") == "a.md"
    assert index_view.entry_target("- a.md — 描述") == "a.md"
    assert index_view.entry_target("- 日记共 3 篇（2026-11-01）") == ""


# ---------------------------------------------------------------- 丢行顺序


def test_journal_block_is_dropped_before_any_normal_entry():
    normal = lines(*(f"m{i}.md" for i in range(4)))
    entries = [
        journal.JournalEntry(f"journal-2026-11-0{day}.md", f"{day} 的日志")
        for day in (1, 2, 3, 4, 5)
    ]
    raw = "\n".join(normal + journal.build_block(entries))
    view = index_view.render_for_injection(
        raw, journal_files={entry.name for entry in entries}, max_lines=6
    )
    compact = [index_view.compact_index_line(line) for line in normal]
    assert view.truncated
    assert "日记共" not in view.text
    assert all(line in view.text for line in compact)
    # 日志块汇总了 5 篇：整块未加载按 5 条计
    assert view.dropped_entries == 5
    assert "还有 5 条未加载，可用 memory_search 找到" in view.text


def test_oldest_entries_are_dropped_first_and_agreement_last():
    raw = "\n".join(
        lines("a.md", "b.md", "c.md", "fb.md", "fb2.md")
    )
    view = index_view.render_for_injection(
        raw,
        file_types={"fb.md": "agreement", "fb2.md": "agreement"},
        idle_times={"a.md": 100.0, "b.md": 300.0, "c.md": 200.0,
                    "fb.md": 1.0, "fb2.md": 50.0},
        max_lines=4,
    )
    # 丢掉最久没用的 a、c（b 更晚用过）；agreement 没被丢；
    # 上限里的 1 行留给"还有 N 条"说明。
    assert view.text.splitlines()[:3] == [
        "- b.md — b.md 的描述",
        "- fb.md — fb.md 的描述",
        "- fb2.md — fb2.md 的描述",
    ]
    assert len(view.text.splitlines()) <= 4
    assert "还有 2 条未加载" in view.text
    assert view.dropped_entries == 2

    # 名额更紧时 b 也丢，最后才轮到 agreement
    tighter = index_view.render_for_injection(
        raw,
        file_types={"fb.md": "agreement", "fb2.md": "agreement"},
        idle_times={"a.md": 100.0, "b.md": 300.0, "c.md": 200.0,
                    "fb.md": 1.0, "fb2.md": 50.0},
        max_lines=3,
    )
    assert tighter.text.splitlines()[:2] == [
        "- fb.md — fb.md 的描述",
        "- fb2.md — fb2.md 的描述",
    ]
    assert len(tighter.text.splitlines()) <= 3
    assert tighter.dropped_entries == 3


def test_remaining_lines_keep_original_order():
    raw = "\n".join(lines("z.md", "a.md", "m.md"))
    view = index_view.render_for_injection(raw, max_lines=10)
    assert view.text.splitlines() == [
        "- z.md — z.md 的描述",
        "- a.md — a.md 的描述",
        "- m.md — m.md 的描述",
    ]


def test_byte_limit_drops_journal_block_first():
    normal = lines(*(f"m{i}.md" for i in range(6)))
    block = journal.build_block(
        [journal.JournalEntry(f"journal-2026-11-0{day}.md", "日志") for day in (1, 2)]
    )
    # 只够装下正常行和"还有 N 条"说明，日志块一条都装不下
    max_bytes = sum(len(line.encode("utf-8")) + 1 for line in normal)
    max_bytes += len(index_view._drop_note(2).encode("utf-8")) + 1
    view = index_view.render_for_injection(
        "\n".join(normal + block), max_bytes=max_bytes
    )
    assert view.truncated
    assert "日记共" not in view.text
    compact = [index_view.compact_index_line(line) for line in normal]
    assert view.text.startswith("\n".join(compact))
    assert len(view.text.encode("utf-8")) <= max_bytes


def test_byte_limit_drops_oldest_normal_entries():
    normal = lines(*(f"m{i}.md" for i in range(6)))
    compact = [index_view.compact_index_line(line) for line in normal]
    # 只够装最后两条和"还有 N 条"说明；使用时间 m0 最早，按旧到新丢到只剩 m4、m5
    max_bytes = sum(len(line.encode("utf-8")) + 1 for line in compact[-2:])
    max_bytes += len(index_view._drop_note(4).encode("utf-8")) + 1
    view = index_view.render_for_injection(
        "\n".join(normal),
        idle_times={f"m{i}.md": float(i) for i in range(6)},
        max_bytes=max_bytes,
    )
    assert view.text.splitlines()[:2] == compact[-2:]
    assert view.dropped_entries == 4
    assert "还有 4 条未加载" in view.text
    assert len(view.text.encode("utf-8")) <= max_bytes


def test_mixed_journal_and_normal_counts():
    normal = lines(*(f"m{i}.md" for i in range(10)))
    entries = [
        journal.JournalEntry(f"journal-2026-11-0{day}.md", "日志")
        for day in (1, 2, 3)
    ]
    raw = "\n".join(normal + journal.build_block(entries))
    view = index_view.render_for_injection(
        raw,
        journal_files={entry.name for entry in entries},
        idle_times={f"m{i}.md": float(i) for i in range(10)},
        max_lines=10,
    )
    # 3 篇日志整块 + 最旧的 m0：共 4 条；上限里的 1 行留给说明
    assert "日记共" not in view.text
    assert "m0.md" not in view.text
    assert view.dropped_entries == 4
    assert "还有 4 条未加载" in view.text
    assert view.text.splitlines()[:9] == [
        index_view.compact_index_line(line) for line in normal[1:]
    ]
    assert len(view.text.splitlines()) <= 10


def test_journal_block_before_manual_line_is_dropped_whole():
    normal = lines(*(f"m{i}.md" for i in range(3)))
    entries = [
        journal.JournalEntry(f"journal-2026-11-0{day}.md", "日志")
        for day in (1, 2, 3, 4, 5)
    ]
    raw = "\n".join(normal + journal.build_block(entries) + ["# 手工补充"])
    view = index_view.render_for_injection(
        raw, journal_files={entry.name for entry in entries}, max_lines=5
    )
    # 汇总行后面有手工行，也按一个完整的日志块丢掉，不留半截；
    # 上限里的 1 行留给"还有 N 条"说明。
    assert "journal-2026" not in view.text and "日记共" not in view.text
    assert "# 手工补充" in view.text
    assert all(
        index_view.compact_index_line(line) in view.text for line in normal
    )
    assert view.dropped_entries == 5
    assert "还有 5 条未加载" in view.text
    assert len(view.text.splitlines()) <= 5


def test_non_entry_lines_over_limit_fall_back_to_bottom_cut(tmp_path):
    store = ScopeStore(tmp_path)
    total = memstore.INDEX_MAX_LINES + 50
    text = "\n".join(f"## 分组 {i}" for i in range(total))
    # 索引只能由插件按记忆文件维护（PLAN 3.12.3）：直接放一份超长的旧索引
    (tmp_path / INDEX_FILENAME).write_text(text + "\n", encoding="utf-8")

    snap = run(store.load_index())
    assert snap.truncated
    assert snap.total_lines == total
    assert snap.loaded_lines == memstore.INDEX_MAX_LINES
    assert snap.text.splitlines()[0] == "## 分组 0"
    assert snap.text.splitlines()[-1] == f"## 分组 {memstore.INDEX_MAX_LINES - 1}"
    # 没有条目被丢，不写"还有 N 条"（分组标题不是记忆）
    assert "还有" not in snap.text


def test_health_uses_compact_injection_size(tmp_path):
    store = ScopeStore(tmp_path)
    names = [f"file-{i:03d}-" + "x" * 50 + ".md" for i in range(100)]
    text = "\n".join(format_index_line(name, "描" * 43) for name in names)
    compact = sum(
        len(index_view.compact_index_line(line).encode("utf-8")) + 1
        for line in text.splitlines()
    )
    assert len(text.encode("utf-8")) > memstore.INDEX_MAX_BYTES
    assert compact < memstore.INDEX_MAX_BYTES * memstore.INDEX_WARN_RATIO

    (tmp_path / INDEX_FILENAME).write_text(text + "\n", encoding="utf-8")
    # 磁盘链接格式超过 25KB，但注入视图没超，健康提示不应误报超限
    snap = run(store.load_index())
    assert not snap.truncated
    detail = store.index_report()
    assert detail.size == compact
    assert detail.truncated is False
    assert store._index_health(*store._injection_metrics(text)) is None


def test_write_receipt_uses_compact_injection_size(tmp_path):
    store = ScopeStore(tmp_path)
    names = [f"file-{i:03d}-" + "x" * 50 + ".md" for i in range(100)]
    report = None
    for name in names:
        report = run(store.write(name, memory(name[:-3], "描" * 43)))
    assert report is not None and report.ok
    raw = (tmp_path / INDEX_FILENAME).read_text("utf-8")
    assert len(raw.encode("utf-8")) > memstore.INDEX_MAX_BYTES
    # _sync_index_line 也用注入视图的紧凑体积，不按磁盘链接格式误报
    assert "注入上限" not in report.message


def test_agreement_is_dropped_last_via_load_index(tmp_path, monkeypatch):
    long_desc = "描" * 100
    note = index_view._drop_note(1)
    one_line = (
        len(index_view.compact_index_line(format_index_line("fb.md", long_desc)).encode("utf-8"))
        + 1
    )
    monkeypatch.setattr(memstore, "INDEX_MAX_BYTES", one_line + len(note.encode("utf-8")) + 1)
    (tmp_path / "profile.md").write_text(
        memory("profile", long_desc, "2026-06-01 12:00", "person"), "utf-8"
    )
    (tmp_path / "fb.md").write_text(
        memory("fb", long_desc, "2026-01-01 12:00", "agreement"), "utf-8"
    )
    (tmp_path / INDEX_FILENAME).write_text(
        "\n".join(
            format_index_line(name, long_desc) for name in ("profile.md", "fb.md")
        ) + "\n",
        "utf-8",
    )
    store = ScopeStore(tmp_path)
    snap = run(store.load_index())
    # agreement 的 updated 更旧，但类型 priority 让它最后才被丢
    assert "fb.md" in snap.text and "profile.md" not in snap.text
    assert "还有 1 条未加载" in snap.text


# ---------------------------------------------------------------- 使用统计


def test_missing_usage_falls_back_to_updated(tmp_path, monkeypatch):
    long_desc = "描" * 100
    note = index_view._drop_note(1)
    budget = sum(
        len(index_view.compact_index_line(format_index_line(name, long_desc)).encode("utf-8")) + 1
        for name in ("mid.md", "new.md")
    )
    monkeypatch.setattr(memstore, "INDEX_MAX_BYTES", budget + len(note.encode("utf-8")) + 1)
    for name, updated in (
        ("old", "2026-01-01 12:00"),
        ("mid", "2026-03-01 12:00"),
        ("new", "2026-06-01 12:00"),
    ):
        (tmp_path / f"{name}.md").write_text(memory(name, long_desc), "utf-8")
    (tmp_path / INDEX_FILENAME).write_text(
        "\n".join(
            format_index_line(f"{name}.md", long_desc)
            for name in ("old", "mid", "new")
        ) + "\n",
        "utf-8",
    )
    store = ScopeStore(tmp_path)
    snap = run(store.load_index())
    # 没有任何使用统计：按 frontmatter 的 updated 从旧到新丢
    assert "old.md" not in snap.text
    assert "mid.md" in snap.text and "new.md" in snap.text
    assert "还有 1 条未加载" in snap.text


def test_load_index_reads_usage_each_time_it_builds_a_snapshot(tmp_path, monkeypatch):
    long_desc = "描" * 100
    note = index_view._drop_note(1)
    budget = sum(
        len(index_view.compact_index_line(format_index_line(f"{name}.md", long_desc)).encode("utf-8")) + 1
        for name in ("b", "c")
    )
    monkeypatch.setattr(memstore, "INDEX_MAX_BYTES", budget + len(note.encode("utf-8")) + 1)
    store = ScopeStore(tmp_path)
    for name in ("a", "b", "c"):
        run(store.write(f"{name}.md", memory(name, long_desc)))
    store.note_used(["a.md"], 100.0)
    first = run(store.load_index())
    assert "a.md" not in first.text

    # 下一次生成快照时读到新的统计：改成 b 最久没用
    store.note_used(["b.md"], 50.0)
    second = run(store.load_index())
    assert "b.md" not in second.text and "a.md" in second.text
    # 同一份统计下重复生成逐字一致
    assert run(store.load_index()).text == second.text


def test_usage_is_not_read_while_rendering():
    """渲染函数是纯函数：只按传入的统计决定丢谁，不碰磁盘。"""
    long_desc = "描" * 100
    raw = "\n".join(
        format_index_line(name, long_desc) for name in ("a.md", "b.md")
    )
    note = index_view._drop_note(1)
    budget = (
        len(index_view.compact_index_line(raw.splitlines()[0]).encode("utf-8")) + 1
    )
    budget += len(note.encode("utf-8")) + 1
    # 没有统计时两者同样"旧"，按索引顺序丢前面的 a
    without = index_view.render_for_injection(raw, max_bytes=budget)
    assert "a.md" not in without.text and "b.md" in without.text
    # 传入统计后改按最近使用：b 更久没用，丢 b；上限里的 1 行留给说明
    with_usage = index_view.render_for_injection(
        raw, idle_times={"a.md": 300.0, "b.md": 100.0}, max_bytes=budget
    )
    assert "b.md" not in with_usage.text and "a.md" in with_usage.text


def test_drop_note_counts_toward_both_limits():
    """超限说明自身也受 200 行 / 25KB 约束，最终注入不能因此超限。"""
    raw = "\n".join(lines(*(f"m{i}.md" for i in range(201))))
    view = index_view.render_for_injection(raw, max_lines=200)
    assert view.truncated
    assert len(view.text.splitlines()) <= 200
    # 200 行上限含说明行，所以保留 199 条、丢掉 2 条
    assert view.loaded_lines == 199
    assert "还有 2 条未加载" in view.text

    long = "\n".join(f"- m{i}.md — " + "测" * 20 for i in range(200))
    note = index_view._drop_note(1)
    max_bytes = (
        len(index_view.compact_index_line(long.splitlines()[0]).encode("utf-8"))
        + 1
        + len(note.encode("utf-8"))
        + 1
    )
    tight = index_view.render_for_injection(long, max_bytes=max_bytes)
    assert tight.truncated
    assert len(tight.text.encode("utf-8")) <= max_bytes
    assert "还有" in tight.text
