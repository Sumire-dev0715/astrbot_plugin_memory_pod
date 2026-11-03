"""注入用的索引渲染：紧凑行、超长时按使用情况丢行。

MEMORY.md 在磁盘上的格式不变，仍是 ``- [名称](文件.md) — 描述``；只有
注入给模型的文本改写成 ``- 文件.md — 描述``：名称就是文件名的词干，
重复一遍只占字数。分组标题、手工加的行原样保留。

注入的索引超过 200 行或 25KB 时不再从底部截断。底部截断会把真正有用的
旧条目一起切掉，却留下排在前面的日志块。丢弃顺序：

1. 日志块整块让位（日志是流水账，需要时按日期 memory_read）；
2. 非日志条目按最近一次用到的时间从旧到新丢；使用统计里没有的按
   ``updated`` 算；
3. feedback 类最后丢——行为指导长期没被提起不代表失效。

剩下的行保持索引里的原顺序，末尾注明还有多少条未加载、可用
memory_search 检索。如果可丢条目全部丢完仍然超限（索引里只有分组标题、
手工说明这类非条目行），再从底部截去剩余行，保证注入不超过上限；这些
不是记忆条目，不计入"还有 N 条"。

这里只做纯渲染，不读盘。使用统计由调用方（ScopeStore.load_index）在
生成快照时读取一次，快照缓存期间不重复读，因此记忆不变时注入的索引
逐字不变，不会跟着每轮的召回记录漂移。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import journal

# 标准索引行：- [名称](文件.md) — 描述；名称里可能有空格或方括号
_INDEX_LINE_RE = re.compile(r"^\s*-\s*\[[^\]]*\]\(([^()\s]+\.md)\)(.*)$")
# 注入时已渲染成紧凑行的条目：- 文件.md — 描述
_PLAIN_LINE_RE = re.compile(r"^\s*-\s+([^\s—]+\.md)(?:\s+—.*)?\s*$")
_FEEDBACK_TYPE = "feedback"


@dataclass(frozen=True)
class IndexView:
    """注入用的索引文本与计数；truncated 只表示有条目被丢掉。"""

    text: str
    total_lines: int
    loaded_lines: int
    # 被丢掉的条目数：非日志按行计，日志块按块内日志篇数计
    dropped_entries: int = 0

    @property
    def truncated(self) -> bool:
        return self.loaded_lines < self.total_lines


def entry_target(line: str) -> str:
    """索引行指向的文件名；不是条目行返回空串。

    兼容磁盘上的 ``- [名称](文件.md) — 描述`` 与注入用的紧凑行
    ``- 文件.md — 描述``。
    """
    match = _INDEX_LINE_RE.match(line or "")
    if match is not None:
        return match.group(1)
    plain = _PLAIN_LINE_RE.match(line or "")
    return plain.group(1) if plain is not None else ""


def compact_index_line(line: str) -> str:
    """把 ``- [名称](文件.md) — 描述`` 渲染成 ``- 文件.md — 描述``。

    不是标准索引行的内容（分组标题、空行、手工写的说明）原样返回。
    """
    match = _INDEX_LINE_RE.match(line or "")
    if match is None:
        return line
    target, rest = match.group(1), match.group(2).rstrip()
    if rest and not rest[0].isspace():
        rest = " " + rest
    return f"- {target}{rest}"


def _is_journal_target(target: str, journal_files: set[str] | None) -> bool:
    return bool(target) and (
        journal.is_journal_name(target)
        or bool(journal_files and target in journal_files)
    )


def _idle_time(target: str, idle_times: dict[str, float] | None) -> float:
    """丢弃排序用的"最近一次用到"；没有统计时调用方已按 updated 填好。"""
    try:
        return float((idle_times or {}).get(target, 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _is_feedback(target: str, file_types: dict[str, str] | None) -> bool:
    return (
        (file_types or {}).get(target, "").strip().lower() == _FEEDBACK_TYPE
    )


def _lines_fit(
    lines: list[str], keep: set[int], max_lines: int, max_bytes: int
) -> bool:
    if len(lines) - len(keep) > max_lines:
        return False
    used = 0
    for index, line in enumerate(lines):
        if index in keep:
            continue
        used += len(line.encode("utf-8")) + 1
        if used > max_bytes:
            return False
    return True


def _journal_span(
    lines: list[str], journal_files: set[str] | None
) -> tuple[int, int] | None:
    """日志块的范围 (起始行, 汇总行)；格式兼容紧凑行。

    不看 memstore 的 ``](文件)`` 链接格式：注入文本已经渲染成
    ``- 文件.md — 描述``，这里直接按文件名认日志行。汇总行后面可能有
    手工加的行（WebUI 编辑器允许），所以取最后一条汇总行往前认块，
    不要求它正好是索引最后一行。
    """
    end = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if journal.is_summary_line(lines[index])
        ),
        None,
    )
    if end is None:
        return None
    start = end
    while start > 0 and _is_journal_target(
        entry_target(lines[start - 1]), journal_files
    ):
        start -= 1
    return start, end


def _drop_note(count: int) -> str:
    return (
        f"……（索引超出加载上限，还有 {count} 条未加载，"
        "可用 memory_search 找到）"
    )


def render_for_injection(
    raw: str,
    *,
    journal_files: set[str] | None = None,
    file_types: dict[str, str] | None = None,
    idle_times: dict[str, float] | None = None,
    max_lines: int = 200,
    max_bytes: int = 25 * 1024,
) -> IndexView:
    """把磁盘上的索引渲染成注入文本，超限时按 3.6 第 4 条丢行。

    journal_files / file_types / idle_times 都以文件名为键；idle_times 的
    值应当是"最近一次用到的时间，没有统计时用 updated"。调用方在生成
    快照时读一次使用统计，本函数不做任何 I/O。

    "还有 N 条未加载"的说明也计入 200 行 / 25KB 上限：它是注入文本的
    一部分，不能让最终结果因为多出这一行而超限。为它腾地方时仍按原来的
    优先级继续丢行（日志 → 最久没用的非 feedback → feedback → 非条目行）。
    """
    lines = [compact_index_line(line) for line in (raw or "").splitlines()]
    # 首尾空行不占注入预算，也不影响日志块的识别
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    total = len(lines)
    if total == 0:
        return IndexView(text="", total_lines=0, loaded_lines=0)

    def kept_lines(dropped: set[int]) -> list[str]:
        return [line for index, line in enumerate(lines) if index not in dropped]

    def note_for(dropped: set[int]) -> tuple[str, int]:
        """按已丢的行算出说明文字与"未加载"条数；没有丢条目时说明为空。"""
        if not dropped:
            return "", 0
        dropped_targets = {
            target
            for target in (entry_target(lines[index]) for index in dropped)
            if target
        }
        journal_targets = {
            target
            for target in dropped_targets
            if _is_journal_target(target, journal_files)
        }
        # 日志块整块丢掉时，汇总行里的日志篇数才是"未加载"的条数；
        # 只丢了块外零散日志行时按行计。
        span = _journal_span(lines, journal_files)
        whole_block = span is not None and all(
            index in dropped for index in range(span[0], span[1] + 1)
        )
        if whole_block:
            summary = journal.summary_count(lines[span[1]])
            journal_count = summary or len(
                set(journal_files or ()) | journal_targets
            )
        else:
            journal_count = len(journal_targets)
        non_journal_count = sum(
            1
            for index in dropped
            if entry_target(lines[index])
            and not _is_journal_target(
                entry_target(lines[index]), journal_files
            )
        )
        count = non_journal_count + journal_count
        return (_drop_note(count) if count else ""), count

    def fits(dropped: set[int], note: str) -> bool:
        """最终注入文本（保留行 + 说明行）是否在行数与字节上限内。"""
        kept = kept_lines(dropped)
        if len(kept) + (1 if note else 0) > max_lines:
            return False
        used = sum(len(line.encode("utf-8")) + 1 for line in kept)
        if note:
            used += len(note.encode("utf-8")) + 1
        return used <= max_bytes

    dropped: set[int] = set()
    note, dropped_entries = note_for(dropped)
    if not fits(dropped, note):
        span = _journal_span(lines, journal_files)
        # 日志块整体先让位；散落在别处的日志行（手工编辑过的旧格式）
        # 也按日志处理，保证日志永远最先丢。
        journal_indexes: set[int] = set()
        if span is not None:
            journal_indexes.update(range(span[0], span[1] + 1))
        for index, line in enumerate(lines):
            if _is_journal_target(entry_target(line), journal_files):
                journal_indexes.add(index)
        dropped.update(journal_indexes)
        note, dropped_entries = note_for(dropped)

        if not fits(dropped, note):
            # 非日志条目：feedback 排最后；同组内按最近用到的时间从旧到新。
            candidates: list[tuple[int, float, int]] = []
            for index, line in enumerate(lines):
                if index in dropped:
                    continue
                target = entry_target(line)
                if not target:
                    continue
                rank = 1 if _is_feedback(target, file_types) else 0
                candidates.append(
                    (rank, _idle_time(target, idle_times), index)
                )
            candidates.sort()
            for _, _, index in candidates:
                dropped.add(index)
                note, dropped_entries = note_for(dropped)
                if fits(dropped, note):
                    break

            # 兜底：可丢条目已经丢完仍然超限（索引里全是分组标题、手工
            # 说明这类非条目行，或者说明行本身也需要空间）。从底部去掉
            # 剩余行，保证注入不超过上限；这些行不是记忆条目，不计入
            # "还有 N 条"。说明行比整个上限还长时只能省略说明。
            for index in range(len(lines) - 1, -1, -1):
                if fits(dropped, note):
                    break
                if index not in dropped:
                    dropped.add(index)
                    note, dropped_entries = note_for(dropped)
            if not fits(dropped, note):
                note, dropped_entries = "", 0

    kept = kept_lines(dropped)
    text = "\n".join(kept + ([note] if note else []))
    return IndexView(
        text=text,
        total_lines=total,
        loaded_lines=total - len(dropped),
        dropped_entries=dropped_entries,
    )
