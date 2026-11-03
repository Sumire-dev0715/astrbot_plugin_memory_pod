"""注入索引的紧凑渲染与超限丢行（3.6 第 4 条）。

MEMORY.md 磁盘格式不变；只有注入文本紧凑化、超限时按使用情况丢行。
"""

import asyncio

from core import index_view, journal, memstore
from core.memstore import INDEX_FILENAME, ScopeStore, format_index_line


def run(coro):
    return asyncio.run(coro)


def memory(name: str, description: str, updated: str = "2026-01-01 12:00") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        "  type: user\n"
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
    assert index_view.entry_target("- 日志共 3 篇（2026-11-01）") == ""


# ---------------------------------------------------------------- 丢行顺序


def test_journal_block_is_dropped_before_any_normal_entry():
    normal = lines(*(f"m{i}.md" for i in range(4)))
    entries = [
        journal.JournalEntry(f"daily-2026-11-0{day}.md", f"{day} 的日志")
        for day in (1, 2, 3, 4, 5)
    ]
    raw = "\n".join(normal + journal.build_block(entries))
    view = index_view.render_for_injection(
        raw, journal_files={entry.name for entry in entries}, max_lines=6
    )
    compact = [index_view.compact_index_line(line) for line in normal]
    assert view.truncated
    assert "日志共" not in view.text
    assert all(line in view.text for line in compact)
    # 日志块汇总了 5 篇：整块未加载按 5 条计
    assert view.dropped_entries == 5
    assert "还有 5 条未加载，可用 memory_search 找到" in view.text


def test_oldest_entries_are_dropped_first_and_feedback_last():
    raw = "\n".join(
        lines("a.md", "b.md", "c.md", "fb.md", "fb2.md")
    )
    view = index_view.render_for_injection(
        raw,
        file_types={"fb.md": "feedback", "fb2.md": "feedback"},
        idle_times={"a.md": 100.0, "b.md": 300.0, "c.md": 200.0,
                    "fb.md": 1.0, "fb2.md": 50.0},
        max_lines=3,
    )
    # 丢掉最久没用的 a、c（b 更晚用过）；feedback 没被丢
    assert view.text.splitlines()[:3] == [
        "- b.md — b.md 的描述",
        "- fb.md — fb.md 的描述",
        "- fb2.md — fb2.md 的描述",
    ]
    assert "还有 2 条未加载" in view.text
    assert view.dropped_entries == 2

    # 名额更紧时 b 也丢，最后才轮到 feedback
    tighter = index_view.render_for_injection(
        raw,
        file_types={"fb.md": "feedback", "fb2.md": "feedback"},
        idle_times={"a.md": 100.0, "b.md": 300.0, "c.md": 200.0,
                    "fb.md": 1.0, "fb2.md": 50.0},
        max_lines=2,
    )
    assert tighter.text.splitlines()[:2] == [
        "- fb.md — fb.md 的描述",
        "- fb2.md — fb2.md 的描述",
    ]
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
        [journal.JournalEntry(f"daily-2026-11-0{day}.md", "日志") for day in (1, 2)]
    )
    # 只够装下正常行，日志块一条都装不下
    max_bytes = sum(len(line.encode("utf-8")) + 1 for line in normal)
    view = index_view.render_for_injection(
        "\n".join(normal + block), max_bytes=max_bytes
    )
    assert view.truncated
    assert "日志共" not in view.text
    compact = [index_view.compact_index_line(line) for line in normal]
    assert view.text.startswith("\n".join(compact))


# ---------------------------------------------------------------- 使用统计


def test_missing_usage_falls_back_to_updated(tmp_path, monkeypatch):
    monkeypatch.setattr(memstore, "INDEX_MAX_LINES", 2)
    for name, updated in (
        ("old", "2026-01-01 12:00"),
        ("mid", "2026-03-01 12:00"),
        ("new", "2026-06-01 12:00"),
    ):
        (tmp_path / f"{name}.md").write_text(memory(name, name), "utf-8")
    (tmp_path / INDEX_FILENAME).write_text(
        "\n".join(lines("old.md", "mid.md", "new.md")) + "\n", "utf-8"
    )
    store = ScopeStore(tmp_path)
    snap = run(store.load_index())
    # 没有任何使用统计：按 frontmatter 的 updated 从旧到新丢
    assert "old.md" not in snap.text
    assert "mid.md" in snap.text and "new.md" in snap.text
    assert "还有 1 条未加载" in snap.text


def test_load_index_reads_usage_each_time_it_builds_a_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(memstore, "INDEX_MAX_LINES", 2)
    store = ScopeStore(tmp_path)
    for name in ("a", "b", "c"):
        run(store.write(f"{name}.md", memory(name, name)))
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
    raw = "\n".join(lines("a.md", "b.md"))
    # 没有统计时两者同样"旧"，按索引顺序丢前面的 a
    without = index_view.render_for_injection(raw, max_lines=1)
    assert "a.md" not in without.text and "b.md" in without.text
    # 传入统计后改按最近使用：b 更久没用，丢 b
    with_usage = index_view.render_for_injection(
        raw, idle_times={"a.md": 300.0, "b.md": 100.0}, max_lines=1
    )
    assert "b.md" not in with_usage.text and "a.md" in with_usage.text
