"""相关记忆召回：每轮挑出与当前消息最相关的少量记忆，把全文附在本轮请求末尾。

索引只是目录，模型需要细节时还得自己调 memory_read；召回把这一步提前：
插件按本轮消息选出最多几条相关记忆，以 _no_save 数据块附在历史末尾、
用户新消息之前。块放在末尾而不是开头，逐轮变化也不破坏前缀缓存。

挑选分两档，都不需要向量模型：
- 身份必选：发送者本人、被 @ 的人、消息里出现的数字 ID，对应的
  user-<ID>.md 存在就一定带上——聊天里最常用的记忆就是"正在说话的人"；
- 相关性：关键词模式按词重叠打分（英文按词、中文按相邻两字），名称与
  描述命中权重更高；模型模式把记忆清单交给摘要模型挑选，失败退回关键词。
  挑选提示词里清单在前、本轮消息在后，写入日期用绝对日期：同一会话里
  记忆没变时清单逐字不变，模型服务可以复用这段前缀。

每条召回记忆的标题带类型与写入时间；正文去掉 frontmatter，超过单条上限
时保留开头与结尾（3.6 第 3 条）。记忆是写入时的观察，不是现状。

日志（journal）是流水账，既不进身份必选，也不进关键词与模型挑选；
需要时按日期显式 memory_read 或 memory_search。

共享人物空间里召回的人物文件在标题里注明"多个机器人共享"：召回内容
只是数据，模型据此知道这不是当前机器人独有的人物记忆。

召回分两档（3.6 第 2 条）：相关度最高的 recall_full_files 个非身份文件
带全文，其余合成一行指针，需要细节时由模型 memory_read。身份必选文件
始终带全文、不占名额，但仍受整块字数预算约束。

挑选、读正文、按预算截断与渲染集中在 assemble_recall：main.py 的每轮
召回与 scripts/measure_recall.py 的离线测量调用同一个函数，两边对成本
的判断一致。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from . import injection, journal
from .injection import (
    DAY_SECONDS,
    FILE_CHARS,
    TOTAL_CHARS,
    age_days,
    age_label,  # noqa: F401 - core.consolidate 从这里导入
)
from .memstore import MemoryHeader, ScopeStore

RECALL_BLOCK_OPEN = "<memory_beyond_recall>"
RECALL_BLOCK_CLOSE = "</memory_beyond_recall>"

DEFAULT_MAX_FILES = 5
# 配置默认值：每轮带全文的非身份召回文件数
DEFAULT_FULL_FILES = 2
# 指针行里描述的字数上限：只做"要不要读"的提示，不展开正文
POINTER_DESC_CHARS = 100
# 快照之后被改过的人物档案附在召回块里的说明
PROFILE_UPDATED_NOTE = "人物档案已更新，以这里为准。"
# 关键词打分的门槛：名称/描述命中一次即达标，只靠正文需命中三次
MIN_SCORE = 3
KEY_WEIGHT = 3
BODY_CAP = 6
DAY_SECONDS = 86400

_ASCII_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RUN_RE = re.compile(r"[㐀-䶿一-鿿豈-﫿]+")
_DIGIT_ID_RE = re.compile(r"(?<!\d)(\d{5,12})(?!\d)")

# 高频虚词组合：出现在几乎任何中文句子里，计入打分只会制造噪声
_STOP_BIGRAMS = frozenset(
    """
    什么 怎么 这个 那个 一个 我们 你们 他们 她们 它们 自己 就是 不是 没有
    可以 知道 还是 已经 因为 所以 但是 如果 然后 现在 时候 这样 那样 这么
    那么 一下 一些 有点 觉得 应该 真的 还有 的话 不过 而且 或者 这些 那些
    的人 了吗 是不 不要 不会 一起 东西 事情 问题 今天 明天 昨天 大家 有没
    是什 么样 么时 可能 一直 一样 出来 起来 下来 上去 过来 好的 好像 为什
    """.split()
)
_STOP_WORDS = frozenset(
    "the and for you are with this that what how not but can was have from".split()
)


def tokenize(text: str) -> set[str]:
    """把文本拆成召回打分用的词集：英文/数字按词，中文按相邻两字。"""
    lowered = (text or "").lower()
    tokens: set[str] = set()
    for word in _ASCII_WORD_RE.findall(lowered):
        if word in _STOP_WORDS:
            continue
        # 纯数字只认像 ID 的长串，短数字（年份、序号）噪声太大
        if word.isdigit() and len(word) < 5:
            continue
        if len(word) >= 2:
            tokens.add(word)
    for run in _CJK_RUN_RE.findall(lowered):
        for i in range(len(run) - 1):
            pair = run[i : i + 2]
            if pair not in _STOP_BIGRAMS:
                tokens.add(pair)
    return tokens


def score(query_tokens: set[str], header: MemoryHeader) -> int:
    """关键词相关度：名称与描述的命中按 KEY_WEIGHT 计，正文命中计 1、封顶 BODY_CAP。"""
    if not query_tokens:
        return 0
    key_tokens = tokenize(f"{header.name.replace('-', ' ')} {header.description}")
    key_hits = len(query_tokens & key_tokens)
    body_hits = len((query_tokens - key_tokens) & tokenize(header.text))
    return key_hits * KEY_WEIGHT + min(body_hits, BODY_CAP)


def identity_files(
    sender_id: str, mentioned_ids: list[str], query: str
) -> list[str]:
    """身份必选的候选文件名：发送者、被 @ 的人、消息正文里出现的数字 ID。"""
    ids: list[str] = []
    for raw in [sender_id, *mentioned_ids, *_DIGIT_ID_RE.findall(query or "")]:
        uid = str(raw or "").strip()
        if uid and uid.isdigit() and uid not in ids:
            ids.append(uid)
    return [f"user-{uid}.md" for uid in ids]


# 身份必选只认这两个作用域：全局作用域不该有 user-<ID> 文件
FORCED_SCOPES = ("session", "people")


@dataclass
class Candidate:
    """一个可召回的记忆：作用域标签（global / session / people）+ 头信息。"""

    scope: str
    header: MemoryHeader

    @property
    def ref(self) -> str:
        return f"{self.scope}/{self.header.file}"


def select_by_keywords(
    candidates: list[Candidate],
    query: str,
    forced_files: list[str],
    limit: int = DEFAULT_MAX_FILES,
) -> list[Candidate]:
    """身份必选在前，其余按关键词得分（同分取较新）补足到 limit 条。

    人物作用域（people）只参与身份必选：正在说话、被提到的人才带上，
    不按关键词把别的群里的人扩散进来。日志是流水账，既不进身份必选，
    也不按关键词扩散。
    """
    if limit <= 0:
        return []
    chosen: list[Candidate] = []
    by_file: dict[str, list[Candidate]] = {}
    for cand in candidates:
        by_file.setdefault(cand.header.file, []).append(cand)
    for name in forced_files:
        for cand in by_file.get(name, []):
            if (
                cand.scope in FORCED_SCOPES
                and not journal.is_journal(cand.header.file, cand.header.type)
                and cand not in chosen
            ):
                chosen.append(cand)
    query_tokens = tokenize(query)
    scored = []
    for cand in candidates:
        if cand in chosen or cand.scope == "people":
            continue
        if journal.is_journal(cand.header.file, cand.header.type):
            continue
        value = score(query_tokens, cand.header)
        if value >= MIN_SCORE:
            scored.append((value, cand.header.updated, cand))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    chosen.extend(cand for _, _, cand in scored)
    return chosen[:limit]


def choose_candidates(
    candidates: list[Candidate],
    query: str,
    forced_files: list[str],
    limit: int,
    model_choice: list[Candidate] | None = None,
) -> list[Candidate]:
    """挑出本轮召回的候选：模型挑选结果与身份必选合并，或退回关键词。

    model_choice 为 None 表示这轮不用模型挑选（关键词模式，或模型调用
    失败）：完全走 select_by_keywords。非 None 时按旧行为合并——先放
    身份必选（关键词模式、空查询下选出），再接模型挑选的顺序，去重后
    截到 limit；模型明确回答"没有相关的"（空列表）时也不会退回关键词。
    """
    if limit <= 0:
        return []
    if model_choice is None:
        return select_by_keywords(candidates, query, forced_files, limit)
    keyword_forced = select_by_keywords(candidates, "", forced_files, limit)
    merged = keyword_forced + [c for c in model_choice if c not in keyword_forced]
    return merged[:limit]


# ---------------------------------------------------------------- 写入查重

# 新建文件时提示"可能重复"的门槛：描述三处重合，或两处重合加正文两处
DUP_MIN_SCORE = 8
_USER_FILE_RE = re.compile(r"^user-[^/]+\.md$")


def similar_memories(
    text: str,
    headers: list[MemoryHeader],
    exclude: str,
    limit: int = 2,
    mem_type: str = "",
) -> list[MemoryHeader]:
    """用召回同一套打分找出与新内容最像的已有记忆，供写入回执提示查重。

    两个记人文件（user-<ID>.md）ID 不同就是两个人，不算重复——同一个
    群里同行、同好的人很多，描述相近是常态。日志既不作为查重候选，新建
    日志本身也不查重：流水账天天写，提醒合并只会制造噪声。
    """
    if journal.is_journal(exclude, mem_type):
        return []
    tokens = tokenize(text)
    if not tokens:
        return []
    is_user = bool(_USER_FILE_RE.match(exclude))
    scored = []
    for header in headers:
        if header.file == exclude:
            continue
        if journal.is_journal(header.file, header.type):
            continue
        if is_user and _USER_FILE_RE.match(header.file):
            continue
        value = score(tokens, header)
        if value >= DUP_MIN_SCORE:
            scored.append((value, header.updated, header))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [header for _, _, header in scored[:limit]]


# ---------------------------------------------------------------- 模型挑选

SELECTOR_PROMPT = """记忆清单（每行：编号、作用域/文件名、类型、写入日期、一句话描述）：
{manifest}

你在帮一个聊天机器人挑选记忆。请从上面的清单里挑出机器人回复下面这条新消息时**确实用得上**的记忆，最多 {limit} 条。只选有把握有用的；拿不准就不选；一条都用不上就输出空数组。

只输出一个 JSON 数组，元素是所选记忆的「作用域/文件名」，例如 ["session/user-12345678.md"]，不要输出其他内容。

新消息：
{query}"""


def _selector_date(updated: float) -> str:
    """候选清单里的写入日期：绝对日期，不随"现在"漂移。

    用相对时间（3 天前）会让同一份记忆隔天换一行、破坏提示词前缀缓存；
    绝对日期在记忆不变时逐字稳定。
    """
    if not updated or updated <= 0:
        return "未知"
    return time.strftime("%Y-%m-%d", time.localtime(updated))


def build_selector_prompt(
    candidates: list[Candidate], query: str, limit: int
) -> str:
    """拼摘要模型挑选用的清单；日志是流水账，不交给模型挑选。

    清单在前、本轮消息在后，方便模型服务复用清单这一段稳定的前缀；
    清单里的写入日期用绝对日期，同一会话里记忆没变时逐字不变。
    """
    lines = []
    visible = [
        cand
        for cand in candidates
        if not journal.is_journal(cand.header.file, cand.header.type)
    ]
    for i, cand in enumerate(visible, 1):
        header = cand.header
        description = header.description or "（无描述）"
        lines.append(
            f"{i}. {cand.ref}｜{header.type or '未标注'}｜"
            f"写入 {_selector_date(header.updated)}｜{description}"
        )
    return SELECTOR_PROMPT.format(
        limit=limit, manifest="\n".join(lines), query=(query or "").strip()[:2000]
    )


def parse_selector_response(
    text: str, candidates: list[Candidate]
) -> list[Candidate] | None:
    """解析模型挑选结果：空数组表示"没有相关的"；找不到 JSON 数组返回 None，
    调用方据此退回关键词模式——模型答了句闲话不该让召回落空。日志一律不选。"""
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, list):
        return None
    by_ref = {cand.ref: cand for cand in candidates}
    by_file: dict[str, Candidate] = {}
    for cand in candidates:
        by_file.setdefault(cand.header.file, cand)
    chosen: list[Candidate] = []
    for item in data:
        ref = str(item or "").strip()
        cand = by_ref.get(ref) or by_file.get(ref.rsplit("/", 1)[-1])
        if cand is None or cand in chosen:
            continue
        if journal.is_journal(cand.header.file, cand.header.type):
            continue
        chosen.append(cand)
    return chosen


# ---------------------------------------------------------------- 注入块


@dataclass
class Recalled:
    ref: str
    content: str
    # 最近修改时间（frontmatter 的 updated 戳，缺省退回 mtime）
    written: float
    # 来自共享人物空间：标题里标明"多个机器人共享"，提醒模型这是共用记忆
    shared: bool = False
    # 取自 MemoryHeader：指针行只写引用，必要时附一句描述
    description: str = ""
    # 标题与 description 之后的附注（快照后更新的人物档案用）
    note: str = ""


@dataclass(frozen=True)
class RecallSection:
    """召回块里实际附上全文的一条：正文是否被单条上限截断由 truncated 标明。

    调用方据此判断"这条全文真的给模型看了"；不要再去块文本里找标题，
    别的文件正文里可以伪造同样的标题行。content 是去掉 frontmatter、
    可能已头尾瘦身的正文，不是磁盘上的原文。
    """

    item: Recalled
    content: str
    truncated: bool
    view: injection.MemoryView


def plan_sections(
    items: list[Recalled],
) -> tuple[list[RecallSection], list[Recalled]]:
    """按字符预算决定哪些条目真正带全文。

    单条超长只做头尾瘦身；预算按瘦身后的正文计算。超过整块预算的条目
    连同它后面的全部条目一起返回给调用方放进指针行——只写引用几乎不占
    字数，模型仍知道有这些记忆可读；顺序保持"相关度高的在前"，不会出现
    排在后面但更短的文件插队带全文。第一个条目总是保留（可能是快照后
    更新的人物档案），和改造前的行为一致。
    """
    sections: list[RecallSection] = []
    dropped: list[Recalled] = []
    used = 0
    for index, item in enumerate(items):
        view = injection.prepare_view(
            item.ref,
            item.content,
            item.written,
            shared=item.shared,
            note=item.note,
        )
        # 标题与 description 也真的进了块，预算不能只算正文
        cost = (
            len(view.body)
            + len(view.description)
            + len(view.note)
            + len(injection.render_title(view))
        )
        if sections and used + cost > TOTAL_CHARS:
            dropped.extend(items[index:])
            break
        used += cost
        sections.append(RecallSection(item, view.body, view.truncated, view))
    return sections, dropped


def plan_recall(items: list[Recalled]) -> list[RecallSection]:
    """只要带全文的条目（保持旧接口）；丢弃的条目由调用方决定是否转指针。"""
    sections, _ = plan_sections(items)
    return sections


def freshness_note(written: float, now: float | None = None) -> str:
    """超过一天的记忆附上时效提醒；今天、昨天写的不打扰。

    只用于 memory_read 的回执；召回块里不再重复这段长说明，标题已经写明
    写入时间，完整规则在记忆规范里（3.6 第 3 条）。
    """
    days = age_days(written, now)
    if days <= 1:
        return ""
    return (
        f"（这条记忆写于 {days} 天前，记录的是当时的情况；"
        "涉及现状的内容请先与当前对话核实再采信）"
    )


def _pointer_label(
    item: Recalled, indexed_refs: set[str] | None
) -> str:
    """指针行里的一项：只写引用；不在注入索引里的文件才附一句描述。"""
    if indexed_refs is not None and item.ref in indexed_refs:
        return item.ref
    description = " ".join((item.description or "").split())
    if not description:
        return item.ref
    if len(description) > POINTER_DESC_CHARS:
        description = description[:POINTER_DESC_CHARS] + "…"
    return f"{item.ref}（{description}）"


def render_pointer_line(
    pointers: list[Recalled], indexed_refs: set[str] | None = None
) -> str:
    """分档后的指针行；没有指针返回空串。"""
    if not pointers:
        return ""
    labels = "、".join(_pointer_label(item, indexed_refs) for item in pointers)
    return f"可能也相关，需要细节时用 memory_read 读取：{labels}"


def render_recall_block(
    sections: list[RecallSection],
    data_notice: str,
    now: float | None = None,
    pointers: list[Recalled] | None = None,
    indexed_refs: set[str] | None = None,
) -> str | None:
    """把已经过预算计划的全文条目与指针行渲染成召回块；都没有返回 None。

    块首只留一行数据声明：重复的长说明已挪进记忆规范（3.6 第 3 条）。
    每条全文的标题、description 与头尾瘦身在 core.injection 里统一处理。
    """
    parts = [injection.render_view(section.view, now) for section in sections]
    pointer_line = render_pointer_line(pointers or [], indexed_refs)
    if pointer_line:
        parts.append(pointer_line)
    if not parts:
        return None
    body = "\n\n".join(parts)
    return (
        f"{RECALL_BLOCK_OPEN}\n"
        f"{data_notice}\n\n"
        f"{body}\n"
        f"{RECALL_BLOCK_CLOSE}"
    )


def build_recall_block(
    items: list[Recalled], data_notice: str, now: float | None = None
) -> str | None:
    """拼召回块（无分档）；超过整块预算的条目同样转为指针。"""
    sections, dropped = plan_sections(items)
    return render_recall_block(sections, data_notice, now, pointers=dropped)


@dataclass
class RecallPlan:
    """一轮召回的编排结果：进入块里的条目、使用统计与渲染好的块。

    used 按作用域分组，记的是"被选中且读到正文"的文件，被预算挤成指针
    的也算用到过（它确实参与过本轮召回）。sections 是带全文的条目，
    pointers 是只在指针行里出现引用的条目；files 是块内实际提到的全部
    文件引用，按块内顺序排列。
    """

    items: list[Recalled]
    used: dict[str, list[str]]
    sections: list[RecallSection]
    pointers: list[Recalled]
    block: str | None

    @property
    def files(self) -> list[str]:
        return [section.item.ref for section in self.sections] + [
            item.ref for item in self.pointers
        ]

    @property
    def full_files(self) -> list[str]:
        return [section.item.ref for section in self.sections]


def _is_forced(cand: Candidate, forced_files: list[str]) -> bool:
    """这条候选是不是身份必选：文件名命中，且作用域允许。"""
    return (
        cand.header.file in forced_files
        and cand.scope in FORCED_SCOPES
        and not journal.is_journal(cand.header.file, cand.header.type)
    )


async def assemble_recall(
    stores: dict[str, ScopeStore],
    candidates: list[Candidate],
    *,
    query: str,
    forced_files: list[str],
    limit: int,
    data_notice: str,
    model_choice: list[Candidate] | None = None,
    shared_people: bool = False,
    now: float | None = None,
    full_files: int | None = None,
    extra_items: list[Recalled] | None = None,
    indexed_refs: set[str] | None = None,
) -> RecallPlan:
    """挑文件、读正文、分档、按预算瘦身并渲染召回块（不依赖 AstrBot 事件）。

    调用方准备好作用域表（键是 global / session / people）和候选清单；
    模型挑选的结果以 model_choice 传入，None 表示退回关键词。shared_people
    为真时 people 作用域的文件在块里标注"多个机器人共享"。

    full_files 是每轮带全文的非身份文件数；None 表示不分档（全部尝试带
    全文，与重构前行为一致）。身份必选文件始终带全文、不占这个名额，但仍
    受整块字数预算约束。extra_items 是调用方已经读好、必须附在块首的条目
    （快照之后被改过的人物档案），同样不占名额、优先进入预算。

    读不到正文的文件直接略过；used 供调用方记使用统计，本函数不写盘。
    main.py 的每轮召回与 scripts/measure_recall.py 的离线测量共用它，
    保证两边对"选中哪些文件、带多少字"的判断完全一致。
    """
    chosen = choose_candidates(candidates, query, forced_files, limit, model_choice)
    items: list[Recalled] = []
    used: dict[str, list[str]] = {}
    full_items: list[Recalled] = []
    pointer_items: list[Recalled] = []
    remaining = None if full_files is None else max(0, full_files)
    for cand in chosen:
        content = await stores[cand.scope].read(cand.header.file)
        if not content:
            continue
        item = Recalled(
            cand.ref,
            content,
            cand.header.updated,
            shared=shared_people and cand.scope == "people",
            description=cand.header.description,
        )
        items.append(item)
        used.setdefault(cand.scope, []).append(cand.header.file)
        if _is_forced(cand, forced_files):
            # 身份必选不占名额，但和其他条目一样受整块预算约束
            full_items.append(item)
        elif remaining is None or remaining > 0:
            full_items.append(item)
            if remaining is not None:
                remaining -= 1
        else:
            pointer_items.append(item)
    extras = list(extra_items or [])
    # 快照后更新的人物档案优先进入块首；同一文件不再按普通候选重复出现
    extra_refs = {item.ref for item in extras}
    sections, dropped = plan_sections(extras + full_items)
    pointers = [item for item in dropped if item.ref not in extra_refs] + pointer_items
    return RecallPlan(
        items=extras + items,
        used=used,
        sections=sections,
        pointers=pointers,
        block=render_recall_block(
            sections, data_notice, now, pointers=pointers, indexed_refs=indexed_refs
        ),
    )


def build_recall_message(block: str) -> dict:
    # 与索引块相同：_no_save 声明不回存，召回内容只存在于本轮请求
    return {"role": "user", "content": block, "_no_save": True}
