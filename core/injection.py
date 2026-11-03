"""注入块里的记忆正文：去掉 frontmatter、瘦身、标题与档案区渲染。

索引块、私聊人物档案（开头块）和每轮召回块共用同一套呈现方式：

- 记忆文件只在磁盘上带 frontmatter；注入给模型的只有正文，标题行写作
  ``### 作用域/文件名（类型｜几天前写入）``，description 作为标题下的第一行。
- 正文超过单文件上限时保留开头约六成与结尾约四成，中间用一行说明省略了
  多少字——新内容通常补在末尾，只取前 N 字会把最新的事实切掉。
- 来源 ID 只在它和文件所记的人不同（旁人转述）时写进标题，避免每行都拖着
  一串无信息量的数字。
- 共享人物空间的文件在标题里注明"多个机器人共享"，和 3.3 的规范一致。

这里只做纯渲染，不读盘、不依赖 AstrBot 事件；main.py 的开头块、
core.recall 的召回块与测量脚本都调用同一套函数，呈现在线上和离线一致。
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
# 开头块里人物档案区的标题（标题下才是各条档案）
PROFILE_HEADING = "# 发送者人物档案（私聊）"

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
    # 来自共享人物空间，标题里注明"多个机器人共享"
    shared: bool = False
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
    shared: bool = False,
    note: str = "",
    limit: int = FILE_CHARS,
) -> MemoryView:
    """把磁盘上的记忆文件变成注入视图：去 frontmatter、瘦身、取钩子与来源。"""
    doc = memstore.parse_memory(content or "")
    description = " ".join(doc.description.split())
    if not description:
        description = " ".join(memstore.parse_description(content or "")[0].split())
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
        shared=shared,
        note=note,
    )


def render_title(view: MemoryView, now: float | None = None) -> str:
    """条目标题：``### 引用（类型｜几天前写入）``；共享与旁人转述追加在括号里。"""
    parts = [view.mem_type or "未标注"]
    if view.shared:
        parts.append("多个机器人共享")
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


def build_opening_block(
    global_index: str | None,
    session_index: str,
    profiles: list[MemoryView] | None = None,
    now: float | None = None,
) -> str | None:
    """拼开头块：索引与私聊人物档案共用同一个快照。

    没有人物档案时与 prompts.build_index_block 完全相同；有档案时把档案区
    放在索引段之后、关闭标记之前。索引与档案都为空才返回 None。
    """
    profiles = profiles or []
    base = prompts.build_index_block(global_index, session_index)
    if not profiles:
        return base
    profile_text = render_profiles(profiles, now)
    if base is None:
        return (
            f"{prompts.INDEX_BLOCK_OPEN}\n"
            f"{prompts.DATA_NOTICE}\n\n"
            f"{profile_text}\n"
            f"{prompts.INDEX_BLOCK_CLOSE}"
        )
    head = base[: -len(prompts.INDEX_BLOCK_CLOSE)].rstrip("\n")
    return f"{head}\n\n{profile_text}\n{prompts.INDEX_BLOCK_CLOSE}"


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
