"""注入块里的记忆正文：去掉 frontmatter、瘦身、标题与档案区渲染。

开头块（留言板全文、小本本索引、悄悄话索引、私聊人物档案）和每轮召回块
共用同一套呈现方式：

- 记忆文件只在磁盘上带 frontmatter；注入给模型的只有正文，标题行写作
  ``### 作用域/文件名（类型｜几天前写入）``，description 作为标题下的第一行。
- 正文超过单文件上限时保留开头约六成与结尾约四成，中间用一行说明省略了
  多少字——新内容通常补在末尾，只取前 N 字会把最新的事实切掉。
- 来源 ID 只在它和文件所记的人不同（旁人转述）时写进标题，避免每行都拖着
  一串无信息量的数字。
- 留言板告示带署名；全部告示从新到旧注入全文，合计不超过 2000 字，超出
  的只列标题（PLAN 3.8.3）。

这里只做纯渲染，不读盘、不依赖 AstrBot 事件；main.py 的开头块与
core.recall 的召回块都调用同一套函数，保证两处呈现一致。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import memstore, prompts

# 单条记忆注入的字符上限与整块上限：注入是辅助通道，不能挤占对话本身
FILE_CHARS = 3000
TOTAL_CHARS = 9000
DAY_SECONDS = 86400
# 开头块里各段的标题（标题下才是内容）
PROFILE_HEADING = "# 私聊人物档案"
BOARD_HEADING = "# 留言板"
NOTEBOOK_HEADING = "# 小本本索引"
WHISPER_HEADING = "# 这段对话的悄悄话索引"
# 留言板全文的总字数上限；超出的告示只列标题
BOARD_LIMIT_CHARS = 2000

_PERSON_FILE_RE = re.compile(r"^user-(\d+)\.md$")
_INDEX_LINK_RE = re.compile(r"\]\(([^()\s]+\.md)\)")
_INDEX_PLAIN_RE = re.compile(r"^\s*-\s+([^\s—]+\.md)(?:\s+—|\s*$)")


def age_days(mtime: float, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(0, int((now - mtime) // DAY_SECONDS))


def age_label(mtime: float, now: float | None = None) -> str:
    days = age_days(mtime, now)
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def clean_description(text: str) -> str:
    """注入用的 description：折叠空白、限长，避免一条钩子撑大整个块。

    与索引行的钩子一致；方括号改成全角，防止模型把描述里的括号误当结构。
    """
    description = " ".join((text or "").split())
    description = description.replace("[", "［").replace("]", "］")
    if len(description) > memstore.MAX_HOOK_CHARS:
        description = description[: memstore.MAX_HOOK_CHARS] + "…"
    return description


def source_note(ref: str, source: str) -> str:
    """旁人转述时返回要写进标题的来源 ID；本人自己说的话返回空串。

    只有 ``user-<数字ID>.md`` 能确定"文件所记的人"；来源里的其他 ID 就是
    转述者。其他类型的文件没有"所记的人"可比，标题里不带来源。
    """
    match = _PERSON_FILE_RE.match(ref.rsplit("/", 1)[-1])
    if not match:
        return ""
    subject = match.group(1)
    others: list[str] = []
    for uid in memstore.split_sources(source):
        if uid and uid != subject and uid not in others:
            others.append(uid)
    return "、".join(others)


@dataclass(frozen=True)
class MemoryView:
    """一条准备好注入的记忆：已去掉 frontmatter，正文可能已头尾瘦身。"""

    ref: str
    mem_type: str
    description: str
    source: str
    body: str
    # 正文超过单条上限，body 已按开头六成、结尾四成保留
    truncated: bool
    # 中间省略的字数；未截断为 0
    omitted: int
    written: float
    # 标题与 description 之后的附注（如"人物档案已更新，以这里为准"）
    note: str = ""


def trim_body(body: str, limit: int = FILE_CHARS) -> tuple[str, bool, int]:
    """正文瘦身：超过 limit 时保留开头约六成与结尾约四成。

    返回 (瘦身后的正文, 是否截断, 省略字数)。新内容通常补在末尾，只留
    开头会把最新写入的事实切掉，所以两头都留。
    """
    body = (body or "").strip()
    if limit <= 0 or len(body) <= limit:
        return body, False, 0
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(body) - limit
    marker = f"\n…（中间省略 {omitted} 字，完整内容用 memory_read 读取）\n"
    return f"{body[:head]}{marker}{body[-tail:]}", True, omitted


def prepare_view(
    ref: str,
    content: str,
    written: float,
    *,
    note: str = "",
    limit: int = FILE_CHARS,
) -> MemoryView:
    """把磁盘上的记忆文件变成注入视图：去 frontmatter、瘦身、取钩子与来源。"""
    doc = memstore.parse_memory(content or "")
    # 没有 description 就不写这一行：从正文首行硬猜会和下面的正文重复，
    # 没有 frontmatter 的文件尤其明显。
    description = clean_description(doc.description)
    body, truncated, omitted = trim_body(doc.body, limit)
    return MemoryView(
        ref=ref,
        mem_type=doc.type or "未标注",
        description=description,
        source=doc.source,
        body=body,
        truncated=truncated,
        omitted=omitted,
        written=written,
        note=note,
    )


def render_title(view: MemoryView, now: float | None = None) -> str:
    """条目标题：``### 引用（类型｜几天前写入）``；旁人转述追加在括号里。"""
    parts = [view.mem_type or "未标注"]
    parts.append(f"{age_label(view.written, now)}写入")
    source = source_note(view.ref, view.source)
    if source:
        parts.append(f"来源 {source}")
    return f"### {view.ref}（{'｜'.join(parts)}）"


def render_view(view: MemoryView, now: float | None = None) -> str:
    """渲染一条注入记忆：标题、description、附注、瘦身后的正文。"""
    parts = [render_title(view, now)]
    if view.description:
        parts.append(view.description)
    if view.note:
        parts.append(view.note)
    if view.body:
        parts.append(view.body)
    return "\n".join(parts)


def render_profiles(views: list[MemoryView], now: float | None = None) -> str:
    """开头块里的人物档案区；没有档案返回空串。"""
    if not views:
        return ""
    return "\n\n".join([PROFILE_HEADING, *[render_view(view, now) for view in views]])


@dataclass(frozen=True)
class BoardNotice:
    """留言板的一条告示；全文注入时带署名。"""

    file: str
    title: str
    body: str
    signature: str = ""
    written: float = 0.0


def render_board_notices(
    notices: list[BoardNotice],
    now: float | None = None,
    limit: int = BOARD_LIMIT_CHARS,
) -> str:
    """渲染留言板：从新到旧全文注入，合计不超过 limit 字，超出的只列标题。

    "全文"包含标题、署名与正文；一旦下一条会超出上限，它和更旧的告示都
    只列标题，并提示用 memory_read 读全文。
    """
    if not notices:
        return ""
    ordered = sorted(
        notices, key=lambda item: (item.written, item.file), reverse=True
    )
    full: list[str] = []
    overflow: list[BoardNotice] = []
    # 标题行与块之间的空行也算进 2000 字；全文段一旦放不下，更旧的告示
    # 全部只列标题，不再让后面的短告示插队。
    used = len(BOARD_HEADING) + 2
    overflow_started = False
    for notice in ordered:
        if overflow_started:
            overflow.append(notice)
            continue
        title = notice.title or notice.file[: -len(memstore.MEMORY_FILE_SUFFIX)]
        block_parts = [f"### {notice.file}"]
        title_line = f"标题：{title}"
        signature_line = f"署名：{notice.signature}" if notice.signature else ""
        body = (notice.body or "").strip()
        rendered = "\n".join(
            part for part in (block_parts[0], title_line, signature_line, body) if part
        )
        separator = 2 if full else 0
        if used + separator + len(rendered) > limit:
            overflow_started = True
            overflow.append(notice)
            continue
        used += separator + len(rendered)
        full.append(rendered)
    lines = [BOARD_HEADING]
    if full:
        lines.append("\n\n".join(full))
    if overflow:
        if full:
            lines.append("其余告示只列标题，需要时用 memory_read 读取：")
        else:
            lines.append("告示只列标题，需要时用 memory_read 读取：")
        for notice in overflow:
            title = notice.title or notice.file[: -len(memstore.MEMORY_FILE_SUFFIX)]
            lines.append(f"- {notice.file} — {title}")
    return "\n\n".join(lines)


def build_opening_block(
    board: str,
    notebook_index: str,
    whisper_index: str,
    profiles: list[MemoryView] | None = None,
    now: float | None = None,
) -> str | None:
    """拼开头块：留言板全文、小本本与当前悄悄话的索引、私聊人物档案。

    四个部分都是空时返回 None（不注入空块）。留言板由 render_board_notices
    渲染好传进来，本函数只负责分段与包裹数据标记。
    """
    sections: list[str] = []
    if board.strip():
        sections.append(board.strip())
    if notebook_index.strip():
        sections.append(f"{NOTEBOOK_HEADING}\n{notebook_index.strip()}")
    if whisper_index.strip():
        sections.append(f"{WHISPER_HEADING}\n{whisper_index.strip()}")
    profile_text = render_profiles(profiles or [], now)
    if profile_text:
        sections.append(profile_text)
    if not sections:
        return None
    body = "\n\n".join(sections)
    return (
        f"{prompts.INDEX_BLOCK_OPEN}\n"
        f"{prompts.DATA_NOTICE}\n\n"
        f"{body}\n"
        f"{prompts.INDEX_BLOCK_CLOSE}"
    )


def indexed_names(*index_texts: str) -> set[str]:
    """从已注入的索引文本里收集文件名（不区分作用域）。

    兼容现有 ``- [名称](文件.md) — 钩子`` 与 3.6 第 4 条之后的
    ``- 文件.md — 钩子`` 两种行格式；找不到行的文件就算"不在注入索引里"。
    """
    names: set[str] = set()
    for text in index_texts:
        for line in (text or "").splitlines():
            match = _INDEX_LINK_RE.search(line)
            if match is None:
                match = _INDEX_PLAIN_RE.match(line)
            if match is not None:
                names.add(match.group(1))
    return names
