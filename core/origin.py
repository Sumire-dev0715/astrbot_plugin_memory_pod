"""记忆来源：记下谁在什么对话里说的，并在别的对话里注明来源（PLAN 3.9.1）。

每次通过工具写入记忆时，插件把这次写入的来源记进 frontmatter 的
``metadata.origin``：会话类型（私聊 / 群聊）、说话人 ID 与显示名、时间，
再加上会话键的短哈希。哈希只用来判断"这条记忆是不是在这段对话里写的"，
群号和群名绝不进记忆文件——``memory_read`` 会把带 frontmatter 的原文交给
模型，群名一旦写进去就会出现在别的对话里。

渲染时只写"群聊"或"私聊"：
- 来源全部是当前这段对话的，不标注；
- 通过管理页写入的（没有 origin）、没有 origin 的旧文件，不标注；
- 条目是说话人本人的人物档案时写"本人说的"；
- 其他来源列出最近的几个，格式如"（小明在群聊里说的，3 天前）"。

来源记录最多保留最近 MAX_ORIGINS 条；同一个人在同一段对话里重复写入时
只更新时间，不堆重复条目。
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

KIND_GROUP = "group"
KIND_PRIVATE = "private"
KINDS = (KIND_GROUP, KIND_PRIVATE)

# 来源记录与显示格式
MAX_ORIGINS = 5
MAX_NOTE_SOURCES = 3
ORIGIN_TIME_FORMAT = "%Y-%m-%d %H:%M"
_RECORD_SEP = "; "
_FIELD_SEP = "|"
_DAY_SECONDS = 86400

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
_ORIGIN_LINE_RE = re.compile(r"^[ \t]*origin[ \t]*:[^\n]*(?:\n|$)", re.M)
_PERSON_FILE_RE = re.compile(
    r"^user-(?P<id>[A-Za-z0-9_]+)"
    r"(?:-(?P<topic>[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*))?"
    r"\.md$",
    re.I,
)

# 显示名里可能冒充标注结构或截断来源记录的字符：圆括号能提前闭合
# “（小明在群聊里说的…）”，换行/行分隔符会把 frontmatter 的一行拆开。
_UNSAFE_NAME_RE = re.compile(r"[\[\]<>＜＞［］【】〔〕｛｝《》〈〉()（）|;；]")
_WS_RE = re.compile(r"\s+")
MAX_NAME_CHARS = 32


def safe_display_name(name: str | None, limit: int = MAX_NAME_CHARS) -> str:
    """来源显示名的最后一道清洗：折叠所有空白、去掉格式字符、限长。

    调用方已经按发送者名字的规则洗过一遍（core.prompts.clean_origin_name）；
    这里再兜一次底，保证手改过 origin 的历史文件也不能伪造来源标注，或
    用 U+2028 之类的行分隔符把一条来源记录截断。
    """
    text = _WS_RE.sub(" ", str(name or "")).strip()
    text = _UNSAFE_NAME_RE.sub("", text)
    return text[:limit].strip()


@dataclass(frozen=True)
class Origin:
    """一次写入的来源：会话类型、说话人、时间与会话键短哈希。"""

    kind: str
    speaker: str
    name: str
    ts: float
    session: str


def session_hash(umo: str) -> str:
    """会话键的短哈希：只用来判断是不是同一段对话，不存明文会话键。

    这是未加盐的截断哈希，不是隐私保护手段：UMO 的平台与会话类型已知，
    会话 ID 熵不高，拿到记忆文件的人可以穷举比对；它只是不把群号、QQ 号
    直接写进记忆文件。平台改名后 UMO 变了，旧记录的哈希也对不上。
    """
    return hashlib.sha1(str(umo or "").encode("utf-8")).hexdigest()[:16]


def _safe_speaker(speaker: str) -> str:
    """说话人 ID 的保底清洗：去掉记录分隔符与空白，防止拼出额外来源记录。

    主流平台的发送者 ID 是数字或固定格式；这里是最后一道防线，不改变
    正常 ID。
    """
    return re.sub(r"[;|\s]+", "", str(speaker or ""))


def _encode_one(record: Origin) -> str:
    return _FIELD_SEP.join(
        (
            record.kind,
            _safe_speaker(record.speaker),
            safe_display_name(record.name),
            time.strftime(ORIGIN_TIME_FORMAT, time.localtime(record.ts)),
            record.session,
        )
    )


def encode(records: list[Origin]) -> str:
    """把来源记录编码成 frontmatter 里的一行字符串。"""
    return _RECORD_SEP.join(_encode_one(record) for record in records)


def parse(text: str) -> list[Origin]:
    """解析 frontmatter 里的 origin；格式不对的条目跳过，不抛异常。"""
    records: list[Origin] = []
    for chunk in re.split(r";\s*", str(text or "")):
        parts = chunk.strip().split(_FIELD_SEP)
        if len(parts) != 5:
            continue
        kind, speaker, name, stamp, session = parts
        if kind not in KINDS or not session:
            continue
        try:
            ts = time.mktime(time.strptime(stamp.strip(), ORIGIN_TIME_FORMAT))
        except (ValueError, OverflowError):
            continue
        records.append(
            Origin(
                kind=kind,
                speaker=speaker.strip(),
                name=safe_display_name(name),
                ts=ts,
                session=session.strip(),
            )
        )
    return records


def merge(existing: str, added: Origin | None) -> str:
    """并入一条新来源：同一段对话里的同一个人只更新时间，保留最近 MAX_ORIGINS 条。"""
    records = parse(existing)
    if added is not None and (added.speaker or added.name):
        key = (added.session, added.speaker or added.name)
        records = [
            record
            for record in records
            if (record.session, record.speaker or record.name) != key
        ]
        records.append(added)
    return encode(records[-MAX_ORIGINS:])


def _age_label(ts: float, now: float) -> str:
    days = max(0, int((now - ts) // _DAY_SECONDS))
    if days == 0:
        return "今天"
    if days == 1:
        return "昨天"
    return f"{days} 天前"


def _person_id(ref: str) -> str:
    """档案文件名里的平台 ID；主档案与主题档案都算这个人的。"""
    match = _PERSON_FILE_RE.match(str(ref or "").rsplit("/", 1)[-1])
    if match is None:
        return ""
    topic = match.group("topic")
    if topic and topic != topic.lower():
        return ""
    return match.group("id")


def note(
    ref: str,
    origin_text: str,
    current_session: str,
    now: float | None = None,
) -> str:
    """在别的对话里显示这条记忆的来源；不需要标注时返回空串。

    current_session 是当前对话的会话键短哈希。来源全部来自当前对话，
    或文件没有 origin（旧文件、管理页写入）时返回空串。
    """
    records = [
        record for record in parse(origin_text) if record.session != current_session
    ]
    if not records:
        return ""
    now = time.time() if now is None else now
    subject = _person_id(ref)
    parts: list[str] = []
    seen: set[tuple[str, str]] = set()
    for record in reversed(records):
        key = (record.speaker, record.kind)
        if key in seen:
            continue
        seen.add(key)
        if subject and record.speaker == subject:
            parts.append("本人说的")
        else:
            name = safe_display_name(record.name) or "某人"
            where = "群聊" if record.kind == KIND_GROUP else "私聊"
            parts.append(f"{name}在{where}里说的，{_age_label(record.ts, now)}")
        if len(parts) >= MAX_NOTE_SOURCES:
            break
    if not parts:
        return ""
    return "（" + "；".join(parts) + "）"


def journal_line(record: Origin | None) -> str:
    """日记追加段落前的来源行，如"08:30 小明（群聊）"；不写群名。"""
    if record is None:
        return ""
    name = safe_display_name(record.name) or "某人"
    where = "群聊" if record.kind == KIND_GROUP else "私聊"
    stamp = time.strftime("%H:%M", time.localtime(record.ts))
    return f"{stamp} {name}（{where}）"


def strip_from_frontmatter(content: str) -> str:
    """从交给模型的原文里去掉 origin 原始字段（PLAN 3.9.1 第 4 条）。"""
    match = _FRONTMATTER_RE.match(content or "")
    if match is None:
        return content or ""
    inner = match.group(1)
    cleaned = _ORIGIN_LINE_RE.sub("", inner)
    if cleaned == inner:
        return content
    return content[: match.start(1)] + cleaned + content[match.end(1):]


def annotate(content: str, note_text: str) -> str:
    """把来源注释放到 frontmatter 之后；没有 frontmatter 时放到正文开头。"""
    if not note_text:
        return content
    match = _FRONTMATTER_RE.match(content or "")
    if match is None:
        body = (content or "").lstrip("\n")
        return f"{note_text}\n\n{body}"
    head = content[: match.end()].rstrip("\n")
    body = content[match.end():].lstrip("\n")
    return f"{head}\n\n{note_text}\n\n{body}"
