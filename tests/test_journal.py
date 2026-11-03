"""journal 日志：判定规则、索引折叠、召回与查重排除。"""

import asyncio

from core import journal, memstore, prompts, recall
from core.memstore import (
    INDEX_FILENAME,
    MemoryHeader,
    ScopeStore,
    _truncate_index,
    format_index_line,
)
from core.recall import Candidate


def run(coro):
    return asyncio.run(coro)


def memory_md(name: str, description: str, mem_type: str = "project") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        "---\n\n"
        "正文。\n"
    )


def header(file: str, description: str = "", mem_type: str = "user") -> MemoryHeader:
    text = (
        f"---\nname: {file[:-3]}\ndescription: {description}\n---\n\n{description}"
    ).lower()
    return MemoryHeader(
        file=file,
        name=file[:-3],
        description=description,
        type=mem_type,
        mtime=1_800_000_000.0,
        size=len(text),
        text=text,
    )


def cand(
    file: str, description: str = "", mem_type: str = "user", scope: str = "session"
) -> Candidate:
    return Candidate(scope, header(file, description, mem_type))


# ---------------------------------------------------------------- 判定规则


def test_is_journal_by_type_and_filename():
    # frontmatter 的 type 是 journal：文件名随意
    assert journal.is_journal("notes.md", "journal")
    assert journal.is_journal("notes.md", "JOURNAL")
    # 文件名带日期：兼容上游老文件写的 type: project
    assert journal.is_journal("daily-2026-11-03.md", "project")
    assert journal.is_journal("journal-2026-10-15.md")
    assert journal.is_journal_name("daily-2026-11-03.md")
    # 只有日期在这个位置才算，普通名字里出现 daily 不算
    assert not journal.is_journal("project-daily-log.md", "project")
    assert not journal.is_journal("daily-summary.md", "project")
    assert not journal.is_journal("daily-2026-11-3.md", "project")


def test_journal_date_from_filename():
    assert journal.journal_date("daily-2026-11-03.md") == "2026-11-03"
    assert journal.journal_date("journal-2026-10-15.md") == "2026-10-15"
    assert journal.journal_date("notes.md") == ""


# ---------------------------------------------------------------- 索引折叠


def test_fold_moves_journals_to_end_and_keeps_non_journal_order():
    lines = [
        "# 分组标题",
        "- [daily-2026-10-15](daily-2026-10-15.md) — 旧日志",
        "- [alpha](alpha.md) — 甲",
        "- [journal-2026-11-02](journal-2026-11-02.md) — 次新日志",
        "- [beta](beta.md) — 乙",
        "",
        "- [daily-2026-11-03](daily-2026-11-03.md) — 新日志",
    ]
    entries = [
        journal.JournalEntry("daily-2026-11-03.md", "新日志"),
        journal.JournalEntry("journal-2026-11-02.md", "次新日志"),
        journal.JournalEntry("daily-2026-10-15.md", "旧日志"),
    ]
    folded = journal.fold_index(lines, entries)
    assert folded == [
        "# 分组标题",
        "- [alpha](alpha.md) — 甲",
        "- [beta](beta.md) — 乙",
        "",
        "- [daily-2026-11-03](daily-2026-11-03.md) — 新日志",
        "- [journal-2026-11-02](journal-2026-11-02.md) — 次新日志",
        "- 日志共 3 篇（2026-10-15 ~ 2026-11-03），不自动召回；"
        "按日期用 memory_read 读 daily-<日期>.md，或用 memory_search 搜",
    ]
    # 重复折叠结果不变（每次索引同步都会跑一遍）
    assert journal.fold_index(folded, entries) == folded


def test_fold_removes_stale_block_when_no_journals_left():
    lines = [
        "- [alpha](alpha.md) — 甲",
        "- [daily-2026-11-03](daily-2026-11-03.md) — 新日志",
        "- 日志共 1 篇（2026-11-03），不自动召回；按日期用 memory_read 读 daily-<日期>.md，或用 memory_search 搜",
    ]
    assert journal.fold_index(lines, []) == ["- [alpha](alpha.md) — 甲"]


def test_store_sync_folds_legacy_index_without_losing_non_journal(tmp_path):
    store = ScopeStore(tmp_path)
    (tmp_path / "alpha.md").write_text(memory_md("alpha", "甲"), "utf-8")
    (tmp_path / "beta.md").write_text(memory_md("beta", "乙"), "utf-8")
    (tmp_path / "daily-2026-10-15.md").write_text(
        memory_md("daily-2026-10-15", "旧日志"), "utf-8"
    )
    (tmp_path / "daily-2026-11-02.md").write_text(
        memory_md("daily-2026-11-02", "次新日志", "journal"), "utf-8"
    )
    (tmp_path / "daily-2026-11-03.md").write_text(
        memory_md("daily-2026-11-03", "新日志"), "utf-8"
    )
    old_index = (
        "# 分组标题\n"
        "- [daily-2026-10-15](daily-2026-10-15.md) — 旧日志\n"
        "- [alpha](alpha.md) — 旧钩子\n"
        "- [beta](beta.md) — 乙\n"
        "\n"
        "- [daily-2026-11-03](daily-2026-11-03.md) — 新日志\n"
        "- [daily-2026-11-02](daily-2026-11-02.md) — 次新日志\n"
    )
    (tmp_path / INDEX_FILENAME).write_text(old_index, "utf-8")

    # 升级后第一次同步：写一个普通记忆触发索引同步
    report = run(store.write("alpha.md", memory_md("alpha", "甲改")))
    assert report.ok
    lines = (tmp_path / INDEX_FILENAME).read_text("utf-8").splitlines()
    assert lines[:4] == [
        "# 分组标题",
        "- [alpha](alpha.md) — 甲改",
        "- [beta](beta.md) — 乙",
        "",
    ]
    assert lines[4:] == [
        "- [daily-2026-11-03](daily-2026-11-03.md) — 新日志",
        "- [daily-2026-11-02](daily-2026-11-02.md) — 次新日志",
        "- 日志共 3 篇（2026-10-15 ~ 2026-11-03），不自动召回；"
        "按日期用 memory_read 读 daily-<日期>.md，或用 memory_search 搜",
    ]
    # 日志没有独占行，也不算索引缺行
    assert store.index_report().missing == []

    # 再同步一次（写 beta）仍然保持同样顺序，折叠幂等
    run(store.write("beta.md", memory_md("beta", "乙改")))
    again = (tmp_path / INDEX_FILENAME).read_text("utf-8").splitlines()
    assert again == [
        "# 分组标题",
        "- [alpha](alpha.md) — 甲改",
        "- [beta](beta.md) — 乙改",
        "",
        *lines[4:],
    ]


def test_journal_write_keeps_only_recent_two_lines(tmp_path):
    store = ScopeStore(tmp_path)
    for day in ("01", "02", "03", "04"):
        name = f"daily-2026-11-{day}"
        run(store.write(f"{name}.md", memory_md(name, f"{day} 的日志")))
    lines = (tmp_path / INDEX_FILENAME).read_text("utf-8").splitlines()
    assert len(lines) == 3
    assert lines[0] == "- [daily-2026-11-04](daily-2026-11-04.md) — 04 的日志"
    assert lines[1] == "- [daily-2026-11-03](daily-2026-11-03.md) — 03 的日志"
    assert lines[2].startswith("- 日志共 4 篇（2026-11-01 ~ 2026-11-04），不自动召回")
    assert "daily-2026-11-02" not in "\n".join(lines)


def test_rebuild_index_folds_journals(tmp_path):
    store = ScopeStore(tmp_path)
    (tmp_path / "alpha.md").write_text(memory_md("alpha", "甲"), "utf-8")
    (tmp_path / "daily-2026-11-03.md").write_text(
        memory_md("daily-2026-11-03", "日志"), "utf-8"
    )
    (tmp_path / INDEX_FILENAME).write_text(
        "- [daily-2026-11-03](daily-2026-11-03.md) — 日志\n- [alpha](alpha.md) — 甲\n",
        "utf-8",
    )
    assert run(store.rebuild_index()) == 2
    lines = (tmp_path / INDEX_FILENAME).read_text("utf-8").splitlines()
    assert lines[0] == "- [alpha](alpha.md) — 甲"
    assert lines[1] == "- [daily-2026-11-03](daily-2026-11-03.md) — 日志"
    assert lines[2].startswith("- 日志共 1 篇（2026-11-03）")


# ---------------------------------------------------------------- 召回


def test_select_by_keywords_excludes_journals_even_when_forced():
    cands = [
        cand("daily-2026-11-03.md", "今天聊了开黑", "journal"),
        cand("notes.md", "开黑流水账", "journal"),
        cand("project-game.md", "每周六晚上的开黑活动"),
    ]
    chosen = recall.select_by_keywords(
        cands, "开黑", ["daily-2026-11-03.md", "notes.md"], 5
    )
    assert [item.header.file for item in chosen] == ["project-game.md"]


def test_selector_prompt_and_response_exclude_journals():
    cands = [
        cand("daily-2026-11-03.md", "今天聊了开黑", "journal"),
        cand("project-game.md", "每周六晚上的开黑活动"),
    ]
    prompt = recall.build_selector_prompt(cands, "开黑", 5)
    assert "daily-2026-11-03" not in prompt
    assert "project-game.md" in prompt
    chosen = recall.parse_selector_response(
        '["session/daily-2026-11-03.md", "session/project-game.md"]', cands
    )
    assert [item.header.file for item in chosen] == ["project-game.md"]


# ---------------------------------------------------------------- 查重


def test_similar_memories_skips_journals_and_new_journal_has_no_hint():
    headers = [
        header("daily-2026-11-03.md", "每周六晚上的开黑活动", "journal"),
        header("project-game.md", "每周六晚上的开黑活动"),
    ]
    text = "每周六晚上八点开黑，群里集合"
    # 日志不作为查重候选
    assert [
        item.file for item in recall.similar_memories(text, headers, "other.md")
    ] == ["project-game.md"]
    # 新建日志本身不提示查重：文件名规则与 type 规则都算
    assert recall.similar_memories(text, headers, "daily-2026-11-04.md") == []
    assert (
        recall.similar_memories(text, headers, "notes.md", mem_type="journal")
        == []
    )


# ---------------------------------------------------------------- 截断


def test_truncate_index_drops_whole_journal_block_first(monkeypatch):
    monkeypatch.setattr(memstore, "INDEX_MAX_LINES", 200)
    normal = [format_index_line(f"m{i}.md", f"钩子{i}") for i in range(198)]
    entries = [
        journal.JournalEntry(f"daily-2026-11-0{day}.md", f"{day} 的日志")
        for day in (1, 2, 3)
    ]
    block = journal.build_block(entries)
    raw = "\n".join(normal + block)
    snap = _truncate_index(raw)
    assert snap.truncated
    # 日志块要么完整显示，要么整块让位：不显示半截
    assert snap.loaded_lines == 198
    assert all(line in snap.text for line in normal)
    assert "daily-2026" not in snap.text
    assert "日志共" not in snap.text
    assert "还有 3 行" in snap.text


def test_truncate_index_drops_type_only_journal_block(monkeypatch):
    monkeypatch.setattr(memstore, "INDEX_MAX_LINES", 200)
    normal = [format_index_line(f"m{i}.md", f"钩子{i}") for i in range(199)]
    block = journal.build_block([journal.JournalEntry("notes.md", "日志")])
    raw = "\n".join(normal + block)
    # notes.md 不按文件名算日志，靠当前在册日志文件名集合认出来
    snap = _truncate_index(raw, {"notes.md"})
    assert snap.loaded_lines == 199
    assert "notes.md" not in snap.text
    assert "日志共" not in snap.text


def test_truncate_index_keeps_journal_block_when_it_fits():
    normal = [format_index_line(f"m{i}.md", f"钩子{i}") for i in range(3)]
    block = journal.build_block(
        [journal.JournalEntry("daily-2026-11-03.md", "日志")]
    )
    raw = "\n".join(normal + block)
    snap = _truncate_index(raw)
    assert not snap.truncated
    assert snap.text == raw


# ---------------------------------------------------------------- 记忆规范


def test_guidance_describes_journal_type():
    text = prompts.memory_guidance(True)
    assert "journal" in text
    assert "daily-<日期>.md" in text
    assert "不参与自动召回" in text
    assert "不参与写入查重" in text
