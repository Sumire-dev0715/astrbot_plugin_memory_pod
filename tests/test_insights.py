"""3.4 编辑与检索体验：section 替换、搜索排序/片段、链接与长文件判断。

全部使用临时目录里的合成数据，不读取真实记忆。
"""

import asyncio
import time
from pathlib import Path

from core import history, insights, journal, memstore
from core.memstore import INDEX_FILENAME, ScopeStore


def run(coro):
    return asyncio.run(coro)


def memory_md(
    name: str,
    description: str = "描述",
    mem_type: str = "user",
    body: str = "正文。",
    updated: str = "",
) -> str:
    stamp = f"  updated: {updated}\n" if updated else ""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        f"{stamp}"
        "---\n\n"
        f"{body}\n"
    )


def write_file(root: Path, filename: str, text: str) -> None:
    (root / filename).write_text(text, "utf-8")


# ---------------------------------------------------------------- section 替换


def test_replace_section_keeps_heading_and_stops_at_same_level():
    content = (
        "# 资料\n"
        "引言\n"
        "\n"
        "## 作息与习惯\n"
        "旧作息一\n"
        "旧作息二\n"
        "\n"
        "### 例外\n"
        "晚睡\n"
        "\n"
        "## 爱好\n"
        "阅读\n"
    )
    updated, error = insights.replace_section(
        content, "## 作息与习惯", "早睡早起\n\n周末补觉"
    )
    assert error == ""
    assert updated == (
        "# 资料\n"
        "引言\n"
        "\n"
        "## 作息与习惯\n"
        "\n"
        "早睡早起\n"
        "\n"
        "周末补觉\n"
        "\n"
        "## 爱好\n"
        "阅读\n"
    )
    # 更深一层的子标题属于本节正文，已随旧正文一起替换掉
    assert "### 例外" not in updated and "晚睡" not in updated


def test_replace_section_can_clear_body():
    content = "## 甲\n旧正文\n\n## 乙\n乙正文\n"
    updated, error = insights.replace_section(content, "## 甲", "")
    assert error == ""
    assert updated == "## 甲\n\n## 乙\n乙正文\n"


def test_replace_section_missing_or_duplicate_lists_all_headings():
    content = "## 甲\n一\n\n### 甲子\n二\n\n## 乙\n三\n"
    missing, error = insights.replace_section(content, "## 丙", "新")
    assert missing == ""
    assert "没有找到" in error
    assert "## 甲" in error and "### 甲子" in error and "## 乙" in error

    duplicate = "## 甲\n一\n\n## 甲\n二\n"
    missing, error = insights.replace_section(duplicate, "## 甲", "新")
    assert missing == ""
    assert "出现了 2 处" in error
    assert "## 甲" in error


def test_replace_section_rejects_non_heading_old_text():
    missing, error = insights.replace_section("## 甲\n正文\n", "正文", "新")
    assert missing == ""
    assert "现有标题" in error and "## 甲" in error


def test_scope_edit_section_goes_through_history(tmp_path):
    store = ScopeStore(tmp_path)
    original = memory_md(
        "user-1",
        "测试人物",
        "user",
        "## 基本信息\n\n职业：工程师\n\n## 作息与习惯\n\n旧作息，晚睡\n",
    )
    run(store.write("user-1.md", original, actor="Bot-A"))
    report = run(
        store.edit(
            "user-1.md",
            "## 作息与习惯",
            "早睡早起",
            mode="section",
            actor="Bot-A",
        )
    )
    assert report.ok, report.message
    updated = run(store.read("user-1.md"))
    assert "## 作息与习惯" in updated
    assert "早睡早起" in updated and "旧作息" not in updated
    assert "职业：工程师" in updated  # 其他小节不受影响

    versions = store.history_versions("user-1.md")
    assert versions and versions[0].action == history.ACTION_EDIT
    assert versions[0].actor == "Bot-A"
    old = store.read_history("user-1.md", versions[0].version)
    assert "旧作息，晚睡" in old and "早睡早起" not in old


# ---------------------------------------------------------------- 搜索模式与排序


def test_search_any_and_all_modes(tmp_path):
    store = ScopeStore(tmp_path)
    write_file(tmp_path, "alpha.md", memory_md("alpha", body="alpha 事实"))
    write_file(tmp_path, "beta.md", memory_md("beta", body="beta 事实"))

    assert run(store.search("alpha beta")) == []
    hits = run(store.search("alpha beta", mode="any"))
    assert {hit.partition(": ")[0] for hit in hits} == {"alpha.md", "beta.md"}
    # 默认 all 等价于原来的 AND 语义
    assert run(store.search("alpha 事实"))[0].startswith("alpha.md")


def test_search_ranking_non_journal_then_matched_then_updated(tmp_path):
    write_file(
        tmp_path, "both.md", memory_md("both", body="alpha beta", updated="2026-01-01")
    )
    write_file(
        tmp_path, "alpha.md", memory_md("alpha", body="alpha", updated="2026-01-03")
    )
    write_file(
        tmp_path, "beta.md", memory_md("beta", body="beta", updated="2026-01-02")
    )
    write_file(
        tmp_path,
        "daily-2026-01-01.md",
        memory_md(
            "daily-2026-01-01",
            "日志",
            "project",
            body="alpha beta",
            updated="2026-01-04",
        ),
    )
    store = ScopeStore(tmp_path)
    hits = run(store.search_hits("alpha beta", mode="any"))
    assert [hit.file for hit in hits] == [
        "both.md",  # 命中两个词，非日志，排最前
        "alpha.md",  # 命中一个词，updated 比 beta 新
        "beta.md",
        "daily-2026-01-01.md",  # 日志永远排在非日志后面
    ]
    assert [hit.matched for hit in hits] == [2, 1, 1, 2]
    assert [hit.journal for hit in hits] == [False, False, False, True]


def test_search_is_not_crowded_out_by_daily_files(tmp_path):
    """回归：旧实现按文件名取前 8 条，daily-* 会把真正的记忆挤掉。"""
    write_file(
        tmp_path, "important.md", memory_md("important", body="项目 事实")
    )
    for day in range(1, 10):
        write_file(
            tmp_path,
            f"daily-2026-01-{day:02d}.md",
            memory_md(f"daily-2026-01-{day:02d}", "日志", "project", body="项目 日志"),
        )
    store = ScopeStore(tmp_path)

    hits = run(store.search("项目"))
    assert len(hits) == memstore.MAX_SEARCH_RESULTS == 8
    assert hits[0].startswith("important.md")
    assert any(hit.startswith("important.md") for hit in hits)

    # 结构化结果里也是非日志在前
    detailed = run(store.search_hits("项目"))
    assert detailed[0].file == "important.md"
    assert len(detailed) == 10


def test_search_hit_has_type_updated_and_two_snippets(tmp_path):
    long_line = "alpha " + "长" * 300
    write_file(
        tmp_path,
        "a.md",
        memory_md(
            "a",
            body=f"{long_line}\nalpha 第二行\nalpha 第三行\n",
            updated="2026-02-03",
        ),
    )
    store = ScopeStore(tmp_path)
    hits = run(store.search_hits("alpha"))
    assert len(hits) == 1
    hit = hits[0]
    assert hit.file == "a.md"
    assert hit.mem_type == "user"
    assert hit.updated == time.mktime((2026, 2, 3, 0, 0, 0, 0, 0, -1))
    assert len(hit.snippets) == 2
    assert hit.snippets[0].endswith("…")
    assert all(len(line) <= insights.SNIPPET_CHARS for line in hit.snippets)
    assert hit.snippets[1] == "alpha 第二行"


def test_search_forgotten_keeps_old_format_and_supports_any(tmp_path):
    store = ScopeStore(tmp_path)
    write_file(tmp_path, "a.md", memory_md("a", body="alpha"))
    write_file(tmp_path, "b.md", memory_md("b", body="beta"))

    async def scenario():
        await store.forget("a.md")
        await store.forget("b.md")
        assert await store.search_forgotten("alpha beta") == []
        hits = await store.search_forgotten("alpha beta", mode="any")
        assert {hit.partition(": ")[0] for hit in hits} == {"a.md", "b.md"}

    run(scenario())


# ---------------------------------------------------------------- 悬空链接


def test_dangling_links_normalize_and_ignore_existing():
    assert insights.dangling_links(["b", " c.md ", "b.md"], {"b.md", "c.md"}) == []
    missing = insights.dangling_links(["missing", "x.md", "missing.md"], set())
    assert missing == ["missing.md", "x.md"]
    hint = insights.dangling_link_hint(missing)
    assert "[[missing]]" in hint and "[[x]]" in hint
    assert "不会自动创建" in hint


# ---------------------------------------------------------------- 长文件阈值


def test_long_file_size_threshold_is_strict_and_journal_ignores_dates():
    assert insights.long_file_reasons("a.md", "user", "短正文", 8 * 1024) == []
    reasons = insights.long_file_reasons("a.md", "user", "短正文", 8 * 1024 + 1)
    assert reasons == ["约 8.0KB，超过 8KB"]

    body = "2026-01-01 " * 20
    reasons = insights.long_file_reasons("a.md", "user", body, 100)
    assert reasons == ["正文里有 20 个日期"]

    # 日志只看体积，日期再多也不提示
    assert insights.long_file_reasons("daily-2026-11-03.md", "project", body, 100) == []
    journal_reasons = insights.long_file_reasons(
        "daily-2026-11-03.md", "project", body, 8 * 1024 + 1
    )
    assert len(journal_reasons) == 1 and "超过 8KB" in journal_reasons[0]


def test_date_threshold_is_fifteen():
    fifteen = "2026-01-01 " * 15
    sixteen = "2026-01-01 " * 16
    assert insights.count_dates(fifteen) == 15
    assert insights.long_file_reasons("a.md", "user", fifteen, 100) == []
    reasons = insights.long_file_reasons("a.md", "user", sixteen, 100)
    assert any("16 个日期" in reason for reason in reasons)
    # 非日期形态的编号不算
    assert insights.count_dates("编号 20260101 和 2026-1-1") == 0


def test_long_files_lists_only_over_threshold(tmp_path):
    store = ScopeStore(tmp_path)
    write_file(tmp_path, "short.md", memory_md("short", body="短正文"))
    write_file(
        tmp_path,
        "long.md",
        memory_md("long", body="x" * (8 * 1024)),
    )
    write_file(
        tmp_path,
        "daily-2026-11-03.md",
        memory_md(
            "daily-2026-11-03",
            "日志",
            "project",
            body="2026-01-01 " * 20,
        ),
    )
    write_file(
        tmp_path,
        "dense.md",
        memory_md("dense", body="2026-01-01 " * 16),
    )
    long_files = run(store.long_files())
    assert [item.file for item in long_files] == ["dense.md", "long.md"]
    # 日志的日期不触发；它的体积也没超过阈值
    assert all(item.file != "daily-2026-11-03.md" for item in long_files)
    assert INDEX_FILENAME not in [item.file for item in long_files]


def test_long_file_hint_mentions_split_or_tidy():
    hint = insights.long_file_hint("a.md", ["约 9.0KB，超过 8KB"])
    assert "a.md" in hint and "拆分或整理" in hint
    assert insights.long_file_hint("a.md", []) == ""


def test_is_journal_names_still_used_for_ordering():
    assert journal.is_journal("daily-2026-11-03.md", "project")
