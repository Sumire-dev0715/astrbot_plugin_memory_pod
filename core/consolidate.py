"""记忆整理：把一个作用域的全部记忆交给模型复盘，产出一份可预览的修改方案。

记忆越积越多，难免出现同一个人分散在几个文件、前后矛盾、相对日期失效、
描述写得含糊这类问题。整理就是复盘：合并重复、修正过时、规范格式。
方案只包含两种操作——整文件写入与删除——先在 WebUI 里预览，管理员勾选
确认后才落盘；模型不直接动磁盘。应用时先写后删，合并途中出错也不丢内容。

整理只处理管理员选中的那一个作用域（留言板 / 某台机器人的小本本 /
某段对话的悄悄话），悄悄话的整理永远看不到别的对话的内容（PLAN 3.8.3）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from . import expiry
from .memstore import (
    INDEX_FILENAME,
    MEMORY_FILE_SUFFIX,
    MEMORY_TYPES,
    MemoryHeader,
    compose_memory,
    person_type_problem,
)
from .recall import age_label
from .writer import WRITE_RULES

# 一次整理最多带上的记忆正文总字符数；超出的文件本次不参与
MAX_INPUT_CHARS = 60000
MAX_OPERATIONS = 60
# 单条写入正文上限：超出的条目整条丢弃而不是截断——截断后合并文件残缺、
# 源文件又照删，内容就丢了。上限与输入预算一致，正常合并不会撞上
MAX_CONTENT_CHARS = MAX_INPUT_CHARS
MAX_REASON_CHARS = 200
# 预览与应用之间文件时间戳允许的误差（JSON 往返的浮点精度）
MTIME_TOLERANCE = 1e-3

_OPERATIONS_RE = re.compile(r"<operations>(.*?)</operations>", re.S | re.I)
_FENCE_RE = re.compile(r"^```[a-zA-Z]*\n?|```$", re.M)
_SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md$")

CONSOLIDATE_PROMPT = """你是聊天机器人的记忆整理助手。下面是记忆库「{scope_label}」里的全部记忆文件（每个文件标注了最近写入时间与最近被用到的时间）。请像整理笔记一样做一次复盘：

- 合并重复：同一个人（同一个数字 ID）或同一件事分散在几个文件里的，合并进一个文件——人物主档案统一命名为 user-<数字ID>.md，主题档案命名为 user-<数字ID>-<主题>.md（主题用小写英文、数字和连字符）——并删除其余文件；
- 修正过时与矛盾：同一事实前后矛盾时以较新的为准，删去明显已失效的内容；正文里的相对日期（明天、下周）按写入时间换算成绝对日期；
- 精简：长期没被用到、内容又明显只是一次性的条目（如早已过去的临时安排）可以删除；有长期价值的即使没用过也保留；
- 日记（journal）是每日流水账：保持 journal 类型与现有文件名（journal-<日期>.md），不要删除日记文件，也不要把它合并进其他记忆；只在确有重复或过期内容时精简正文；
- 规范格式：{type_rules}；description 写成一句能让人判断"何时该读全文"的钩子；
- 不编造：只能重组、精简、改写已有信息，不得添加文件里没有的事实；
- 拿不准的不动。不需要整理就输出空数组。

这次只整理上面这一处；留言板、小本本、悄悄话是三层不同的记忆，不要在这里跨层搬动内容。

{write_rules}

输出放在 <operations></operations> 标签内，是一个 JSON 数组，元素只有两种：
{{"op": "write", "file": "文件名.md", "type": "类型", "description": "一句话钩子", "content": "完整正文（不含 frontmatter）", "reason": "为什么这样改"}}
{{"op": "delete", "file": "文件名.md", "reason": "为什么删除，如：已合并进 user-123.md"}}
write 会整文件覆盖（新建或改写），所以 content 必须是完整正文，不能只写改动的部分。

记忆文件：
{files}"""


@dataclass
class Operation:
    op: str
    file: str
    reason: str = ""
    type: str = ""
    description: str = ""
    content: str = ""
    # 留言板告示的署名；整理时从原文件继承，新建的告示由调用方给默认值
    signature: str = ""
    # 留言板告示的到期日；整理时从原文件继承，避免一次复盘把日期清掉
    expires: str = ""
    # 来源记录；整理时把被合并进来的源文件的 origin 并进目标文件
    origin: str = ""
    # 预览时目标文件的写入时间（不存在为 0），应用时据此识别期间被改过的文件
    base_mtime: float = 0.0

    def render(self) -> str:
        name = self.file[: -len(MEMORY_FILE_SUFFIX)]
        return compose_memory(
            name,
            self.description,
            self.type,
            self.content,
            signature=self.signature,
            expires=self.expires,
            origin=self.origin,
        )

    def to_dict(self) -> dict:
        data = {
            "op": self.op,
            "file": self.file,
            "reason": self.reason,
            "baseMtime": self.base_mtime,
        }
        if self.op == "write":
            data.update(
                type=self.type,
                description=self.description,
                content=self.content,
                # 预览要能看到并原样带回署名：留言板告示的署名由插件写进
                # frontmatter，页面回传方案时少一个字段就会把它清空
                signature=self.signature,
                expires=self.expires,
            )
        return data


def build_prompt(
    scope_label: str,
    files: list[tuple[MemoryHeader, str]],
    now: float | None = None,
    *,
    board: bool = False,
) -> tuple[str, list[str]]:
    """拼整理提示词；返回 (提示词, 因超出预算未纳入的文件名)。"""
    blocks: list[str] = []
    skipped: list[str] = []
    used = 0
    for header, content in files:
        if used + len(content) > MAX_INPUT_CHARS:
            skipped.append(header.file)
            continue
        used += len(content)
        blocks.append(
            f"=== {header.file}（{age_label(header.updated, now)}写入，"
            f"{usage_label(header, now)}）===\n{content.strip()}"
        )
    type_rules = (
        "留言板告示没有类型，不要写 type"
        if board
        else "type 只能是 person、agreement、note、journal 之一，"
        "人物档案的文件名和 type 必须对上；agreement 类正文附 "
        "**Why:** 与 **How to apply:** 两行"
    )
    prompt = CONSOLIDATE_PROMPT.format(
        scope_label=scope_label,
        files="\n\n".join(blocks) or "（空）",
        write_rules=WRITE_RULES,
        type_rules=type_rules,
    )
    return prompt, skipped


def usage_label(header: MemoryHeader, now: float | None = None) -> str:
    """整理清单里的使用情况：模型据此判断哪些记忆长期没派上用场。"""
    if header.last_used <= 0:
        return "从未被用到"
    return f"最近用到于{age_label(header.last_used, now)}"


def _load_array(text: str):
    match = _OPERATIONS_RE.search(text or "")
    raw = _FENCE_RE.sub("", match.group(1) if match else (text or "")).strip()
    try:
        return json.loads(raw)
    except ValueError:
        start, end = raw.find("["), raw.rfind("]")
        if start < 0 or end <= start:
            return None
        try:
            return json.loads(raw[start : end + 1])
        except ValueError:
            return None


def parse_operations(
    text: str,
    existing: set[str],
    *,
    allow_expires: bool = False,
    board: bool = False,
) -> list[Operation] | None:
    """解析并校验整理方案；输出无法解析时返回 None（区别于"无需整理"的空列表）。

    丢弃的条目：文件名不合法、指向 MEMORY.md、删除不存在的文件、
    写入缺正文、正文超长或类型非法、同一文件出现多次（只认第一次）。
    allow_expires 只在整理留言板、应用页面回传的方案时为真；其他作用域
    的 expires 字段一律忽略（PLAN 3.9.2 第 3 条只让留言板有到期日）。
    """
    data = _load_array(text)
    if not isinstance(data, list):
        return None
    ops: list[Operation] = []
    seen: set[str] = set()
    for entry in data:
        if len(ops) >= MAX_OPERATIONS or not isinstance(entry, dict):
            continue
        op = str(entry.get("op", "")).strip().lower()
        file = str(entry.get("file", "")).strip()
        if not file.endswith(MEMORY_FILE_SUFFIX):
            file += MEMORY_FILE_SUFFIX
        if (
            file in seen
            or file.lower() == INDEX_FILENAME.lower()
            or not _SAFE_FILE_RE.match(file)
        ):
            continue
        reason = " ".join(str(entry.get("reason", "")).split())[:MAX_REASON_CHARS]
        try:
            base_mtime = float(entry.get("baseMtime") or 0.0)
        except (TypeError, ValueError):
            base_mtime = 0.0
        if op == "delete":
            if file not in existing:
                continue
            ops.append(
                Operation(op="delete", file=file, reason=reason, base_mtime=base_mtime)
            )
        elif op == "write":
            mem_type = (
                "" if board else str(entry.get("type", "")).strip().lower()
            )
            content = str(entry.get("content", "")).strip()
            if not content or len(content) > MAX_CONTENT_CHARS:
                continue
            if not board and (
                mem_type not in MEMORY_TYPES
                or person_type_problem(file, mem_type)
            ):
                continue
            signature = " ".join(str(entry.get("signature", "")).split())
            # 页面回传的方案会带回到期日；格式不对就当没有，不因此丢掉整条
            expires = ""
            if allow_expires:
                expires, _ = expiry.normalize(entry.get("expires"))
            ops.append(
                Operation(
                    op="write",
                    file=file,
                    reason=reason,
                    type=mem_type,
                    description=" ".join(str(entry.get("description", "")).split()),
                    content=content,
                    signature=signature,
                    expires=expires,
                    base_mtime=base_mtime,
                )
            )
        else:
            continue
        seen.add(file)
    return ops


def operations_from_payload(
    raw,
    existing: set[str],
    *,
    allow_expires: bool = False,
    board: bool = False,
) -> list[Operation]:
    """把 WebUI 回传的（可能被管理员改过的）方案重新校验一遍再应用。"""
    if not isinstance(raw, list):
        return []
    ops = parse_operations(
        json.dumps(raw, ensure_ascii=False),
        existing,
        allow_expires=allow_expires,
        board=board,
    )
    return ops or []


def _plan_entries(raw) -> list:
    """整理方案条目：既接受 JSON 文本（模型回复），也接受回传的列表。"""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        data = _load_array(raw)
        return data if isinstance(data, list) else []
    return []


def requested_files(raw, *, op: str | None = None) -> set[str]:
    """方案里点到的文件名（不论是否通过校验），用于报告被跳过的条目。

    raw 可以是模型返回的文本，也可以是管理页回传的列表；op 只统计指定操作。
    """
    files: set[str] = set()
    for entry in _plan_entries(raw):
        if not isinstance(entry, dict):
            continue
        entry_op = str(entry.get("op", "") or "write").strip().lower()
        if op is not None and entry_op != op:
            continue
        file = str(entry.get("file", "")).strip()
        if not file:
            continue
        if not file.endswith(MEMORY_FILE_SUFFIX):
            file += MEMORY_FILE_SUFFIX
        files.add(file)
    return files


def missing_write_files(raw, ops: list[Operation]) -> set[str]:
    """方案里要求写入、却没有进入可应用操作的文件名。

    有这种情况时调用方必须放弃同方案里的删除：写入没落盘，合并目标不存在，
    再删源文件就会丢内容（tidy.apply 的 write_failed 只覆盖已进入 ops 的写）。
    """
    applied = {op.file for op in ops if op.op == "write"}
    return requested_files(raw, op="write") - applied


def is_stale(op: Operation, current_mtime: float | None) -> bool:
    """预览之后目标文件是否变过：已有文件被改写、或要新建的文件已被别人建了。"""
    return abs((current_mtime or 0.0) - op.base_mtime) > MTIME_TOLERANCE


def ordered(ops: list[Operation]) -> list[Operation]:
    """应用顺序：先写后删，合并途中出错也不会先把源文件删掉。"""
    return [op for op in ops if op.op == "write"] + [
        op for op in ops if op.op == "delete"
    ]
