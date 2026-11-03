"""journal 日志：类型判定与索引折叠。

日志是每日流水账，不是稳定事实。它照常以 Markdown 文件保存、照常参与
遗忘，但在索引里不占单独一行：非日志行保持原顺序，日志折叠成索引末尾
的一个块——最近两篇各占一行，再加一行汇总。放在末尾是因为注入通道超出
200 行 / 25KB 时从底部截断，日志块应该最先让位；截断落在块中间时整块
一起略去，不显示半截日志目录。

判定规则（任一成立即为日志）：
- frontmatter 的 type 是 journal；
- 文件名以 daily-<YYYY-MM-DD> 或 journal-<YYYY-MM-DD> 开头。

文件名规则是给上游老数据留的：v1.4.1 的 daily-*.md 写的是 type: project，
不改用户的文件，直接按文件名认成日志。

日志不参与关键词召回、摘要模型挑选、身份必选和写入查重；需要时按日期用
memory_read 读取，或用 memory_search 搜索。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

JOURNAL_TYPE = "journal"
# 索引里保留最近几篇日志各占一行；其余只进汇总
RECENT_ENTRIES = 2

JOURNAL_DATE_RE = re.compile(r"^(?:daily|journal)-(\d{4}-\d{2}-\d{2})")
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


def _index_line(entry: JournalEntry) -> str:
    # 标准索引行的清洗与格式化在 memstore；这里延迟导入避免循环依赖。
    from .memstore import format_index_line

    return format_index_line(entry.name, entry.description)


def _entry_date(entry: JournalEntry) -> str:
    """日志日期：优先文件名日期，没有日期规则时按最近修改推算。

    用于汇总日期区间，也做"最近两篇"排序的次键；以 type: journal 标注、
    文件名不带日期的日志因此也能进入汇总区间。
    """
    date = entry.date or journal_date(entry.name)
    if date:
        return date
    if entry.updated > 0:
        return time.strftime("%Y-%m-%d", time.localtime(entry.updated))
    return ""


def _summary_line(entries: list[JournalEntry]) -> str:
    dates = sorted(date for date in map(_entry_date, entries) if date)
    if not dates:
        span = ""
    elif dates[0] == dates[-1]:
        span = f"（{dates[0]}）"
    else:
        span = f"（{dates[0]} ~ {dates[-1]}）"
    return (
        f"{_SUMMARY_PREFIX}{len(entries)} 篇{span}，不自动召回；"
        "按日期用 memory_read 读 daily-<日期>.md，或用 memory_search 搜"
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


def _is_entry_line(line: str, journal_files: set[str] | None = None) -> bool:
    match = _TARGET_RE.search(line)
    target = match.group(1) if match else ""
    return is_journal_name(target) or bool(journal_files and target in journal_files)


def split_index(
    lines: list[str], journal_files: set[str] | None = None
) -> tuple[list[str], list[str]]:
    """把索引行拆成（非日志行, 日志行）；两边都保持原顺序。

    journal_files 是当前在册日志的文件名集合，用来识别以 type: journal
    标注、但文件名不带日期的日志；文件名符合日期规则的旧行也一并认出来。
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

    每次索引同步都调用它，因此升级后第一次同步就会把老格式（日志行散落
    在索引里）改写成折叠格式，重复调用结果不变。
    """
    kept, _ = split_index(lines, {entry.name for entry in entries})
    return kept + build_block(entries)


def block_span(
    lines: list[str], journal_files: set[str] | None = None
) -> tuple[int, int] | None:
    """索引末尾日志块的范围 (起始行, 汇总行)；末尾不是完整日志块时返回 None。

    末尾的空行（手工编辑或外部写入留下）不影响识别。供截断与索引体检
    使用：日志块要么完整显示，要么整块让位。journal_files 是当前在册
    日志的文件名集合，用来认出以 type: journal 标注、文件名不带日期的
    日志；不传时只按日期文件名识别。
    """
    end = len(lines) - 1
    while end >= 0 and not lines[end].strip():
        end -= 1
    if end < 0 or not is_summary_line(lines[end]):
        return None
    start = end
    while start > 0 and _is_entry_line(lines[start - 1], journal_files):
        start -= 1
    return start, end
