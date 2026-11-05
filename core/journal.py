"""journal 日志：类型判定与索引折叠。

日志是每日流水账，不是稳定事实。它照常以 Markdown 文件保存、照常参与
遗忘，但在索引里不占单独一行：非日志行保持原顺序，日志折叠成索引末尾
的一个块——最近两篇各占一行，再加一行汇总。注入通道超出 200 行 / 25KB
时日志块整块最先让位（见 core/index_view.py），不显示半截日志目录。

判定规则（任一成立即为日志）：
- frontmatter 的 type 是 journal；
- 文件名以 journal-<YYYY-MM-DD> 开头。

日志不参与关键词召回、身份必选，也不占单独的索引行；需要时按日期用
memory_read 读取，或用 memory_search 搜索。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

JOURNAL_TYPE = "journal"
# 索引里保留最近几篇日志各占一行；其余只进汇总
RECENT_ENTRIES = 2

JOURNAL_DATE_RE = re.compile(r"^journal-(\d{4}-\d{2}-\d{2})")
_HEADING_DATE_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$", re.M)
_TARGET_RE = re.compile(r"\]\(([^()\s]+\.md)\)")
_SUMMARY_PREFIX = "- 日志共 "
_SUMMARY_MARK = "不自动召回"


@dataclass(frozen=True)
class JournalEntry:
    """一篇日志在索引折叠时需要的头信息。"""

    name: str
    description: str = ""
    # 文件名里的日期（YYYY-MM-DD）；没有日期规则时由 updated 推算
    date: str = ""
    # 没有日期时按最近修改排序
    updated: float = 0.0
    # 这篇日志覆盖的日期范围（最早的 ## 日期小标题 ~ 最近一次更新）
    start_date: str = ""
    end_date: str = ""


def is_journal_name(name: str) -> bool:
    """文件名是否符合日志规则（不看 frontmatter）。"""
    return bool(JOURNAL_DATE_RE.match(name or ""))


def is_journal(name: str, mem_type: str = "") -> bool:
    """是否为日志：frontmatter 类型或文件名任一命中即可。"""
    return (
        (mem_type or "").strip().lower() == JOURNAL_TYPE
        or is_journal_name(name)
    )


def journal_date(name: str) -> str:
    """从日志文件名取出日期；不是日期命名的日志返回空串。"""
    match = JOURNAL_DATE_RE.match(name or "")
    return match.group(1) if match else ""


def heading_dates(text: str) -> list[str]:
    """正文里的 `## YYYY-MM-DD` 小标题日期（去重保序）。"""
    dates: list[str] = []
    for date in _HEADING_DATE_RE.findall(text or ""):
        if date not in dates:
            dates.append(date)
    return dates


def last_heading_date(text: str) -> str:
    """正文里最后一个 `## YYYY-MM-DD` 小标题的日期；没有返回空串。"""
    dates = heading_dates(text)
    return dates[-1] if dates else ""


def _updated_date(updated: float) -> str:
    if updated <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(updated))


def entry_span(name: str, text: str, updated: float) -> tuple[str, str]:
    """一篇日志的日期范围：最早的日期小标题（没有就用文件名日期）到最近更新。

    写日记时每天会加 `## YYYY-MM-DD`，一篇日志可能跨好几天；
    索引里的日期范围要能看出它从哪天记到哪天。没有小标题时退回文件名
    日期与最近修改时间。
    """
    dates = heading_dates(text)
    file_date = journal_date(name)
    start = min(dates) if dates else file_date
    if dates:
        # 有日期小标题：这篇日志跨了好几天，结束日期取最近一次更新
        end = _updated_date(updated) or max(dates)
    else:
        # 没有日期小标题：文件名日期就是它记的那一天
        end = file_date or _updated_date(updated)
    if not start:
        start = end
    if not end:
        end = start
    if start and end and end < start:
        end = start
    return start, end


def _entry_span(entry: JournalEntry) -> tuple[str, str]:
    start = entry.start_date or entry.date or journal_date(entry.name)
    end = entry.end_date or entry.date or _updated_date(entry.updated)
    if not start:
        start = end
    if not end:
        end = start
    if start and end and end < start:
        end = start
    return start, end


def _span_label(start: str, end: str) -> str:
    if not start:
        return ""
    if not end or end == start:
        return f"（{start}）"
    return f"（{start} ~ {end}）"


def _index_line(entry: JournalEntry) -> str:
    # 标准索引行的清洗与格式化在 memstore；这里延迟导入避免循环依赖。
    from .memstore import format_index_line

    line = format_index_line(entry.name, entry.description)
    start, end = _entry_span(entry)
    return line + _span_label(start, end)


def _entry_date(entry: JournalEntry) -> str:
    """日志的最近日期；用于"最近两篇"排序的次键。"""
    _, end = _entry_span(entry)
    return end


def _summary_line(entries: list[JournalEntry]) -> str:
    spans = [_entry_span(entry) for entry in entries]
    starts = sorted(start for start, _ in spans if start)
    ends = sorted(end for _, end in spans if end)
    if not starts or not ends:
        span = ""
    elif starts[0] == ends[-1]:
        span = f"（{starts[0]}）"
    else:
        span = f"（{starts[0]} ~ {ends[-1]}）"
    return (
        f"{_SUMMARY_PREFIX}{len(entries)} 篇{span}，不自动召回；"
        "按日期用 memory_read 读日志文件，或用 memory_search 搜"
    )


def build_block(entries: list[JournalEntry]) -> list[str]:
    """索引末尾的日志块：最近两篇各一行，再加一行汇总；没有日志返回空列表。"""
    if not entries:
        return []
    ordered = sorted(
        entries,
        # 最近 = 最近一次写入；文件名日期只做次排序键与汇总区间，
        # 这样 type: journal 但文件名不带日期的日志也能排进最近两篇。
        key=lambda entry: (entry.updated, _entry_date(entry), entry.name),
        reverse=True,
    )
    lines = [_index_line(entry) for entry in ordered[:RECENT_ENTRIES]]
    lines.append(_summary_line(entries))
    return lines


def is_summary_line(line: str) -> bool:
    return line.startswith(_SUMMARY_PREFIX) and _SUMMARY_MARK in line


_SUMMARY_COUNT_RE = re.compile(r"^- 日志共 (\d+) 篇")


def summary_count(line: str) -> int:
    """日志块汇总行里的日志篇数；不是汇总行返回 0。"""
    match = _SUMMARY_COUNT_RE.match(line or "")
    return int(match.group(1)) if match else 0


def _is_entry_line(line: str, journal_files: set[str] | None = None) -> bool:
    match = _TARGET_RE.search(line)
    target = match.group(1) if match else ""
    return is_journal_name(target) or bool(journal_files and target in journal_files)


def split_index(
    lines: list[str], journal_files: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """把索引行拆成（非日志行, 日志行）；两边都保持原顺序。

    journal_files 是当前在册日志的文件名集合，用来识别以 type: journal
    标注、但文件名不带日期的日志；文件名符合日期规则的也一并认出来。
    """
    names = journal_files or set()
    kept: list[str] = []
    removed: list[str] = []
    for line in lines:
        match = _TARGET_RE.search(line)
        target = match.group(1) if match else ""
        if is_summary_line(line) or target in names or is_journal_name(target):
            removed.append(line)
        else:
            kept.append(line)
    return kept, removed


def fold_index(lines: list[str], entries: list[JournalEntry]) -> list[str]:
    """把索引里的日志行收拢到末尾：非日志行原样保序，日志块重新生成。

    每次索引同步都调用它，把散落在索引里的日志行收拢到末尾，重复调用
    结果不变。
    """
    kept, _ = split_index(lines, {entry.name for entry in entries})
    return kept + build_block(entries)


def block_span(
    lines: list[str], journal_files: set[str] | None = None
) -> tuple[int, int] | None:
    """索引里日志块的范围 (起始行, 汇总行)；找不到完整日志块时返回 None。

    从最后一条汇总行往前认块：末尾的空行，以及汇总行之后手工加的行
    （WebUI 编辑器允许，注入侧 index_view._journal_span 同样认可），都
    不影响识别。供截断与索引体检使用：日志块要么完整显示，要么整块让位。
    journal_files 是当前在册日志的文件名集合，用来认出以 type: journal
    标注、文件名不带日期的日志；不传时只按日期文件名识别。
    """
    end = None
    for index in range(len(lines) - 1, -1, -1):
        if is_summary_line(lines[index]):
            end = index
            break
    if end is None:
        return None
    start = end
    while start > 0 and _is_entry_line(lines[start - 1], journal_files):
        start -= 1
    return start, end
