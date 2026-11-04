"""编辑与检索体验：小节替换、搜索排序与片段、悬空链接与长文件提醒。

这一层只做纯文本判断，不碰文件、锁与作用域解析：

- `replace_section` 供后台整理按小节替换正文使用，标题行保留，
  正文范围到下一个同级或更高级标题为止；
- `build_hit` / `rank_hits` 供 memory_search 使用：非日志在前，再按命中
  词数、updated 倒序；每条最多两行命中片段；
- `dangling_links` 只和调用方给的"当前机器人可见的文件名集合"比较，
  不猜测别的会话或别的机器人目录里有什么；
- `long_file_reasons` 判断文件是否超过体积或日期密度阈值，日志只看体积。

可见性（哪些作用域算可见）由调用方决定：main.py 只传入 global（启用时）、
当前会话和 people（启用时，共享成员是 _shared），所以链接检查不会泄露
其他机器人或别的会话里是否存在某个文件。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from . import journal

# 搜索模式：all 要求所有词都命中，any 命中任意一个词即可
SEARCH_ALL = "all"
SEARCH_ANY = "any"
SEARCH_MODES = (SEARCH_ALL, SEARCH_ANY)

# 每条搜索结果最多给几行片段，以及每行片段最多多少字
MAX_SNIPPETS = 2
SNIPPET_CHARS = 160

# 长文件阈值：非日志超过 8KB，或正文日期超过 15 个；日志只看体积
LONG_FILE_BYTES = 8 * 1024
LONG_FILE_DATES = 15

_HEADING_RE = re.compile(r"^(#{1,6})\s+\S")
# 围栏代码块起止行：最多 3 个前导空格，至少 3 个反引号或波浪线
_FENCE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
# 只认 YYYY-MM-DD；前后不能再接数字，避免把更长的编号误算成日期
_DATE_RE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")


@dataclass(frozen=True)
class SearchHit:
    """一条结构化搜索结果；排序与展示需要的信息都在这里。"""

    file: str
    mem_type: str
    updated: float
    matched: int
    snippets: tuple[str, ...]
    journal: bool = False


@dataclass(frozen=True)
class LongFile:
    """一个超过阈值的作用域文件；reasons 直接给回执和 /mb_status 用。"""

    file: str
    size: int
    reasons: tuple[str, ...]


def normalize_search_mode(mode: str) -> str | None:
    """归一化搜索模式；未知模式返回 None（由调用方给模型明确的报错）。"""
    value = (mode or SEARCH_ALL).strip().lower()
    return value if value in SEARCH_MODES else None


def search_terms(query: str) -> list[str]:
    """把查询拆成去重保序的小写词；空查询返回空列表。"""
    terms: list[str] = []
    for raw in (query or "").split():
        term = raw.strip().lower()
        if term and term not in terms:
            terms.append(term)
    return terms


@dataclass(frozen=True)
class _Line:
    """带字符偏移与原始行尾的一行，用来原样保留小节之外的换行风格。"""

    start: int
    end: int
    next: int
    text: str
    ending: str


@dataclass(frozen=True)
class _Heading:
    """正文里的一个标题行；index 是 _Line 下标，level 是 # 的个数。"""

    index: int
    level: int
    text: str


def _split_lines(content: str) -> list[_Line]:
    """按 \r\n、\n 或 \r 拆行，保留字符偏移与每行自己的行尾。"""
    text = content or ""
    result: list[_Line] = []
    index = 0
    while index < len(text):
        end = index
        while end < len(text) and text[end] not in "\r\n":
            end += 1
        if end >= len(text):
            ending = ""
        elif text.startswith("\r\n", end):
            ending = "\r\n"
        else:
            ending = text[end]
        result.append(
            _Line(index, end, end + len(ending), text[index:end], ending)
        )
        index = end + len(ending)
    return result


def _frontmatter_end(lines: list[_Line]) -> int:
    """YAML frontmatter 的闭合行下标；没有 frontmatter 时返回 -1。

    闭合行必须顶格（与 memstore._FRONTMATTER_RE 的口径一致），
    block scalar 里缩进的 ``---`` 不算结束。
    """
    if not lines or lines[0].text.rstrip() != "---":
        return -1
    for index in range(1, len(lines)):
        if lines[index].text.rstrip() == "---":
            return index
    return -1


def _scan_headings(content: str) -> tuple[list[_Line], list[_Heading]]:
    """正文里的标题行；跳过 YAML frontmatter 与围栏代码块内的内容。

    小节标题只认正文里以 # 开头的行。frontmatter 里的 YAML 注释或块标量
    可能也以 # 开头，围栏代码块里的示例标题同样不是小节边界，两类都必须
    排除，否则 section/append 会把它们当成标题，删坏 frontmatter 或提前
    结束本节。
    """
    lines = _split_lines(content)
    headings: list[_Heading] = []
    if not lines:
        return lines, headings
    fm_end = _frontmatter_end(lines)
    in_fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        if index <= fm_end:
            continue
        text = line.text
        if in_fence is not None:
            match = _FENCE_RE.match(text)
            if (
                match
                and match.group(2)[0] == in_fence[0]
                and len(match.group(2)) >= in_fence[1]
                and not match.group(3).strip()
            ):
                in_fence = None
            continue
        fence = _FENCE_RE.match(text)
        if fence and not (fence.group(2)[0] == "`" and "`" in fence.group(3)):
            in_fence = (fence.group(2)[0], len(fence.group(2)))
            continue
        match = _HEADING_RE.match(text)
        if match:
            headings.append(_Heading(index, len(match.group(1)), text.strip()))
    return lines, headings


def _heading_names(headings: list[_Heading]) -> str:
    return "；".join(item.text for item in headings) or "（文件里没有标题行）"


def _locate_section(
    content: str, heading: str
) -> tuple[list[_Line], int, int, str]:
    """定位标题行与本节结束行；出错时返回 (行表, -1, -1, 错误)。"""
    lines, headings = _scan_headings(content)
    target = (heading or "").replace("\r\n", "\n").strip()
    if not target or "\n" in target or not _HEADING_RE.match(target):
        return lines, -1, -1, (
            "old_text 必须是一个小节标题行（如 ## 作息与习惯）。"
            f"现有标题：{_heading_names(headings)}"
        )
    matches = [item for item in headings if item.text == target]
    if not matches:
        return lines, -1, -1, (
            f"没有找到小节标题 {target!r}。现有标题：{_heading_names(headings)}"
        )
    if len(matches) > 1:
        return lines, -1, -1, (
            f"小节标题 {target!r} 在文件里出现了 {len(matches)} 处，"
            "无法确定操作哪一节；请先整理重复的标题。"
            f"现有标题：{_heading_names(headings)}"
        )
    start = matches[0]
    end = len(lines)
    for item in headings:
        if item.index > start.index and item.level <= start.level:
            end = item.index
            break
    return lines, start.index, end, ""


def _preferred_newline(content: str) -> str:
    """文件主要使用的换行符；新插入的行沿用它。"""
    crlf = (content or "").count("\r\n")
    lf = (content or "").count("\n") - crlf
    cr = (content or "").count("\r") - crlf
    if crlf and crlf >= lf and crlf >= cr:
        return "\r\n"
    if lf:
        return "\n"
    if cr:
        return "\r"
    return "\n"


def _block_lines(text: str) -> list[str]:
    """把待写入的正文拆成行；只去掉首尾空行，保留行内空格与空行。"""
    block = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return block.split("\n") if block.strip() else []


def replace_section(content: str, heading: str, text: str) -> tuple[str, str]:
    """用 text 替换 heading 这一节的正文，标题行原样保留。

    返回 (新内容, 错误)；出错时新内容为空串。heading 必须与正文里的某个
    标题行逐字一致（去掉首尾空白后比较）且只出现一次；正文范围到下一个
    同级或更高级标题为止，更深的子标题属于本节正文，一并被替换。text 留空
    表示清空这一节正文。标题不存在或重复时列出正文里的全部标题。

    frontmatter 与围栏代码块里的 # 行不算标题；小节之外的字符（含 CRLF
    行尾）原样保留，只有被替换的正文与新增的空白行会重新拼接。
    """
    lines, start, end, error = _locate_section(content, heading)
    if error:
        return "", error
    nl = _preferred_newline(content)
    heading_line = lines[start]
    body_lines = _block_lines(text)
    pieces: list[str] = []
    if body_lines:
        # 标题行与正文之间留一个空行；标题行自己没有行尾时先补一个
        pieces.append(nl if heading_line.ending else nl + nl)
        pieces.append(nl.join(body_lines))
        if end < len(lines):
            # 正文末尾一行 + 空行，接下一个标题
            pieces.append(nl + nl)
        elif content.endswith(("\n", "\r")):
            pieces.append(nl)
    elif end < len(lines):
        # 清空正文：标题与下一个标题之间仍留一个空行
        pieces.append(nl)
    tail = content[lines[end].start:] if end < len(lines) else ""
    return content[: heading_line.next] + "".join(pieces) + tail, ""


def append_section(content: str, heading: str, text: str) -> tuple[str, str]:
    """把 text 追加到 heading 这一节的末尾，标题与小节之外的字符保持原样。

    与 replace_section 使用同一套正文标题扫描（排除 frontmatter 与围栏
    代码块）；追加位置在本节最后一行非空正文之后、原有尾部空行之前。
    """
    lines, start, end, error = _locate_section(content, heading)
    if error:
        return "", error
    block = _block_lines(text)
    if not block:
        return "", "new_text 不能为空：追加模式要提供要补进去的文字。"
    nl = _preferred_newline(content)
    insert_index = start
    for index in range(start + 1, end):
        if lines[index].text.strip():
            insert_index = index
    insert_line = lines[insert_index]
    prefix = content[: insert_line.next]
    tail = content[insert_line.next:]
    if prefix and not prefix.endswith(("\n", "\r")):
        prefix += nl
    pieces = [nl, nl.join(block), nl]
    if tail and not tail.startswith(("\n", "\r")):
        pieces.append(nl)
    return prefix + "".join(pieces) + tail, ""


def _snippet_lines(content: str, terms: list[str]) -> tuple[str, ...]:
    """命中词所在的前几行，去重、去首尾空白并限长。

    超长行优先截取命中词附近的一小段，避免命中词在 160 字之后时
    片段里反而看不到它。
    """
    found: list[str] = []
    for line in (content or "").splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if not any(term in lowered for term in terms):
            continue
        if cleaned in found:
            continue
        if len(cleaned) > SNIPPET_CHARS:
            positions = [lowered.find(term) for term in terms]
            pos = min((item for item in positions if item >= 0), default=-1)
            if pos > SNIPPET_CHARS - 20:
                start = max(0, pos - 20)
                end = start + SNIPPET_CHARS - 2
                prefix = "…" if start > 0 else ""
                suffix = "…" if end < len(cleaned) else ""
                cleaned = prefix + cleaned[start:end] + suffix
            else:
                cleaned = cleaned[: SNIPPET_CHARS - 1] + "…"
        found.append(cleaned)
        if len(found) >= MAX_SNIPPETS:
            break
    return tuple(found)


def build_hit(
    name: str,
    mem_type: str,
    updated: float,
    content: str,
    terms: list[str],
    mode: str = SEARCH_ALL,
) -> SearchHit | None:
    """按模式判断内容是否命中，命中时给出片段与命中词数。"""
    if not terms:
        return None
    lowered = (content or "").lower()
    present = [term for term in terms if term in lowered]
    if mode == SEARCH_ANY:
        if not present:
            return None
    elif len(present) != len(terms):
        return None
    return SearchHit(
        file=name,
        mem_type=mem_type or "",
        updated=updated,
        matched=len(present),
        snippets=_snippet_lines(content, present or terms),
        journal=journal.is_journal(name, mem_type),
    )


def search_key(hit: SearchHit) -> tuple:
    """结果排序键：非日志在前，再按命中词数、updated 倒序，文件名兜底。"""
    return (hit.journal, -hit.matched, -hit.updated, hit.file)


def rank_hits(hits: list[SearchHit], limit: int | None = None) -> list[SearchHit]:
    """按 search_key 排序；limit 为 None 时不截断。"""
    ordered = sorted(hits, key=search_key)
    return ordered[:limit] if limit is not None else ordered


def normalize_link_target(target: str) -> str:
    """[[x]] 与管理页一致地解析成文件名：没有 .md 后缀就补上。"""
    value = (target or "").strip()
    if not value:
        return ""
    return value if value.endswith(".md") else value + ".md"


def dangling_links(links: list[str], available: set[str]) -> list[str]:
    """链接目标里在当前可见文件中找不到的那些（返回补全后的文件名）。

    只和调用方给的集合比较；不访问磁盘，也不去别的会话或别的机器人目录里
    试探，避免把"某个私有文件是否存在"泄露给模型。
    """
    missing: list[str] = []
    for link in links or []:
        name = normalize_link_target(link)
        if name and name not in available and name not in missing:
            missing.append(name)
    return missing


def _size_label(size: int) -> str:
    """向上取到 0.1KB，避免刚超过阈值时显示成“约 8.0KB，超过 8KB”。"""
    tenths = math.ceil(size / 102.4)
    return f"{tenths / 10:.1f}KB"


def count_dates(body: str) -> int:
    """正文里 YYYY-MM-DD 形式的日期个数（同一日期出现多次也算多次）。"""
    return len(_DATE_RE.findall(body or ""))


def long_file_reasons(
    file: str, mem_type: str, body: str, size: int
) -> list[str]:
    """超过阈值的具体原因；没有超过返回空列表。日志只看体积。"""
    reasons: list[str] = []
    if size > LONG_FILE_BYTES:
        reasons.append(
            f"约 {_size_label(size)}，超过 {LONG_FILE_BYTES // 1024}KB"
        )
    if not journal.is_journal(file, mem_type):
        dates = count_dates(body)
        if dates > LONG_FILE_DATES:
            reasons.append(f"正文里有 {dates} 个日期")
    return reasons
