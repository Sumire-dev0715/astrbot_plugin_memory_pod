"""待整理记录：每轮对话的临时缓冲，整理成功后删除。

记忆只由后台整理写入，聊天模型只读。每轮结束后，插件把这一轮的用户消息
（已带发送者标注）、机器人最终回复、发送者是否管理员记进本会话的待整理
记录；`memory_note` 的内容记在当前轮里。整理成功就删掉处理过的部分，失败
保留，同一批连续失败 `MAX_FAILURES` 次后放弃；失败期间新到的轮次没有参与
这次尝试，不会被连带清掉。

待整理记录不是记忆：只放在插件数据目录的 `state/pending/` 下，模型的工具、
召回、索引、搜索都看不到它。每个会话一个 JSON 文件，原子写入；条数和总字数
有上限，超出时丢最旧的，调用方据此记日志。

文件格式（键名用 camelCase，与 state/ 下其他状态文件一致）：

    {
      "version": 1,
      "umo": "aiocqhttp:FriendMessage:...",
      "records": [
        {"seq": 1, "at": 1760000000.0, "user": "...", "reply": "...",
         "admin": false, "sender": "123",
         "notes": [{"content": "...", "sender": "123", "at": 1760000000.0}]}
      ],
      "failures": 0,
      "lastAttempt": 0.0,
      "lastResult": "",
      "lastSuccess": 0.0,
      "roundsSinceWrite": 0,
      "lastRoundAt": 0.0,
      "notePending": false,
      "nextSeq": 2,
      "purged": {"session/user-1.md": 1760000000.0}
    }

`purged` 记管理员「彻底删除（含历史）」过的文件：这批旧对话再整理时，
对这些文件的操作一律跳过，避免把刚删掉的事实写回来；等旧对话都处理完、
屏蔽记录不再对应任何待整理轮次后自动清掉。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .memstore import safe_key

logger = logging.getLogger(__name__)

PENDING_VERSION = 1
# 一个会话最多保留多少轮对话、合计多少字；超出时丢最旧的
MAX_RECORDS = 40
MAX_TOTAL_CHARS = 24_000
# 单条消息与 memory_note 的截断上限（防止一段超长消息挤掉整段历史）
MAX_MESSAGE_CHARS = 4000
MAX_NOTE_CHARS = 1000
# 单轮最多保留几条 memory_note：很多条时丢最旧的，避免一条轮次挤满上限
MAX_NOTES_PER_ROUND = 5
# 同一段连续失败多少次后放弃
MAX_FAILURES = 3


@dataclass
class Note:
    """一条 memory_note：用户明确要求记住、更正或忘掉的事。"""

    content: str = ""
    sender: str = ""
    at: float = 0.0

    def to_raw(self) -> dict:
        return {
            "content": self.content,
            "sender": self.sender,
            "at": round(self.at, 3),
        }

    @classmethod
    def from_raw(cls, raw: object) -> "Note":
        if not isinstance(raw, dict):
            return cls()
        try:
            at = float(raw.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        return cls(
            content=str(raw.get("content") or ""),
            sender=str(raw.get("sender") or ""),
            at=at,
        )


@dataclass
class Round:
    """待整理的一轮对话；seq 在会话内递增，用来精确删除已处理的部分。"""

    seq: int = 0
    at: float = 0.0
    user: str = ""
    reply: str = ""
    admin: bool = False
    sender: str = ""
    notes: list[Note] = field(default_factory=list)

    def chars(self) -> int:
        return (
            len(self.user)
            + len(self.reply)
            + sum(len(note.content) for note in self.notes)
        )

    def to_raw(self) -> dict:
        return {
            "seq": self.seq,
            "at": round(self.at, 3),
            "user": self.user,
            "reply": self.reply,
            "admin": self.admin,
            "sender": self.sender,
            "notes": [note.to_raw() for note in self.notes],
        }

    @classmethod
    def from_raw(cls, raw: object) -> "Round":
        if not isinstance(raw, dict):
            return cls()
        try:
            seq = int(raw.get("seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        try:
            at = float(raw.get("at") or 0.0)
        except (TypeError, ValueError):
            at = 0.0
        notes = raw.get("notes")
        return cls(
            seq=seq,
            at=at,
            user=str(raw.get("user") or ""),
            reply=str(raw.get("reply") or ""),
            admin=bool(raw.get("admin", False)),
            sender=str(raw.get("sender") or ""),
            notes=[
                Note.from_raw(item)
                for item in notes
                if isinstance(item, dict)
            ]
            if isinstance(notes, list)
            else [],
        )


def _to_float(raw: object, default: float = 0.0) -> float:
    try:
        return float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _to_int(raw: object, default: int = 0) -> int:
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


@dataclass
class PendingState:
    """一个会话的待整理记录与整理结果。"""

    umo: str = ""
    # 记录轮次时事件的 platform_id（服务端给出）；后台整理按它取共享权限，
    # 不依赖 UMO 前缀
    platform: str = ""
    records: list[Round] = field(default_factory=list)
    failures: int = 0
    last_attempt: float = 0.0
    last_result: str = ""
    last_success: float = 0.0
    # 上次整理（成功或失败）之后又过了多少轮
    rounds_since_write: int = 0
    last_round_at: float = 0.0
    # 本轮有 memory_note：回复结束后立即整理，不等空闲
    note_pending: bool = False
    next_seq: int = 1
    # "scope/file" -> 管理员彻底删除该文件的时间；只屏蔽删除之前就开始的
    # 那几轮对话，之后的新对话照常整理
    purged: dict[str, float] = field(default_factory=dict)

    @property
    def count(self) -> int:
        return len(self.records)

    def chars(self) -> int:
        return sum(round_record.chars() for round_record in self.records)

    def abandoned(self) -> bool:
        return self.failures >= MAX_FAILURES and not self.records

    def blocked_specs(self) -> set[str]:
        """这些旧轮次整理时不能再写的 "scope/file"。

        只屏蔽删除动作之前就已开始的轮次：它们可能记得被删掉的事实；之后
        的新对话不受影响。
        """
        if not self.purged or not self.records:
            return set()
        oldest = min(record.at for record in self.records)
        return {
            spec for spec, ts in self.purged.items() if ts >= oldest
        }

    def prune_purged(self) -> None:
        """去掉不再对应任何待整理轮次的屏蔽记录（记录都处理完时自动清空）。"""
        if not self.purged:
            return
        if not self.records:
            self.purged = {}
            return
        oldest = min(record.at for record in self.records)
        self.purged = {
            spec: ts for spec, ts in self.purged.items() if ts >= oldest
        }

    def to_raw(self) -> dict:
        return {
            "version": PENDING_VERSION,
            "umo": self.umo,
            "platform": self.platform,
            "records": [round_record.to_raw() for round_record in self.records],
            "failures": self.failures,
            "lastAttempt": round(self.last_attempt, 3),
            "lastResult": self.last_result,
            "lastSuccess": round(self.last_success, 3),
            "roundsSinceWrite": self.rounds_since_write,
            "lastRoundAt": round(self.last_round_at, 3),
            "notePending": self.note_pending,
            "nextSeq": self.next_seq,
            "purged": {
                spec: round(ts, 3) for spec, ts in self.purged.items()
            },
        }

    @classmethod
    def from_raw(cls, key: str, raw: object) -> "PendingState":
        if not isinstance(raw, dict):
            return cls()
        records = [
            Round.from_raw(item)
            for item in raw.get("records") or []
            if isinstance(item, dict)
        ]
        raw_purged = raw.get("purged")
        purged: dict[str, float] = {}
        if isinstance(raw_purged, dict):
            for spec, ts in raw_purged.items():
                if isinstance(spec, str) and spec:
                    purged[spec] = _to_float(ts)
        state = cls(
            umo=str(raw.get("umo") or key),
            platform=str(raw.get("platform") or ""),
            records=records,
            failures=max(0, _to_int(raw.get("failures"), 0)),
            last_attempt=_to_float(raw.get("lastAttempt")),
            last_result=str(raw.get("lastResult") or ""),
            last_success=_to_float(raw.get("lastSuccess")),
            rounds_since_write=max(
                0, _to_int(raw.get("roundsSinceWrite"), 0)
            ),
            last_round_at=_to_float(raw.get("lastRoundAt")),
            note_pending=bool(raw.get("notePending", False)),
            next_seq=max(1, _to_int(raw.get("nextSeq"), 1)),
            purged=purged,
        )
        # 兼容手工改坏的文件：序号缺失时按顺序补，保证删除时能定位
        seen = {record.seq for record in records if record.seq > 0}
        for record in records:
            if record.seq <= 0:
                while state.next_seq in seen:
                    state.next_seq += 1
                record.seq = state.next_seq
                seen.add(record.seq)
        if records:
            state.next_seq = max(state.next_seq, max(r.seq for r in records) + 1)
        return state


def _merge_states(base: PendingState, extra: PendingState) -> PendingState:
    """把 extra 的待整理轮次并进 base：按时间排序后重新编号。

    平台改名时新旧平台可能都已经有待整理记录，合并后统一交给新会话整理。
    """
    records = base.records + extra.records
    records.sort(key=lambda record: (record.at, record.seq))
    for index, record in enumerate(records, 1):
        record.seq = index
    purged = dict(extra.purged)
    for spec, ts in base.purged.items():
        purged[spec] = max(purged.get(spec, 0.0), ts)
    return PendingState(
        umo=base.umo or extra.umo,
        platform=base.platform or extra.platform,
        records=records,
        failures=0,
        last_attempt=max(base.last_attempt, extra.last_attempt),
        last_result=base.last_result or extra.last_result,
        last_success=max(base.last_success, extra.last_success),
        rounds_since_write=max(
            base.rounds_since_write, extra.rounds_since_write
        ),
        last_round_at=max(base.last_round_at, extra.last_round_at),
        note_pending=any(record.notes for record in records),
        next_seq=len(records) + 1,
        purged=purged,
    )


def _truncate(text: str, limit: int, key: str, what: str) -> str:
    """截断超长正文；截掉的部分记一条日志，不再静默丢失。"""
    text = text or ""
    if len(text) <= limit:
        return text
    logger.warning(
        "会话 %s 的%s超过 %d 字，已截断尾部 %d 字",
        key,
        what,
        limit,
        len(text) - limit,
    )
    return text[:limit]


class PendingBook:
    """`state/pending/` 目录的读写；同步方法，文件很小，调用方直接调用。"""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        return self.base_dir / f"{key}.json"

    def load(self, key: str) -> PendingState:
        path = self.path(key)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return PendingState()
        return PendingState.from_raw(key, data)

    def save(self, key: str, state: PendingState) -> None:
        with self._lock:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=".pending-", dir=self.base_dir
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        state.to_raw(),
                        fh,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                os.replace(tmp_name, self.path(key))
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

    def items(self) -> list[tuple[str, PendingState]]:
        """全部会话的 (目录名, 待整理记录)；目录不存在时返回空列表。"""
        if not self.base_dir.is_dir():
            return []
        result: list[tuple[str, PendingState]] = []
        for path in sorted(self.base_dir.glob("*.json")):
            key = path.stem
            state = self.load(key)
            if state.umo:
                result.append((key, state))
        return result

    def all(self) -> list[PendingState]:
        """全部会话的待整理记录；目录不存在时返回空列表。"""
        return [state for _, state in self.items()]

    def mark_purged(self, key: str, scope: str, file: str, now: float) -> None:
        """记下管理员彻底删除了某个文件：这批旧对话不能再把它写回来。

        只对已经存在的会话状态生效；没有待整理记录的会话不必新建文件。
        """
        if not self.path(key).is_file():
            return
        state = self.load(key)
        spec = f"{scope}/{file}"
        # 用现有轮次里最晚的时间兜底：即使系统时钟比记录时间慢，也要屏
        # 蔽删除动作之前就存在的对话；之后新到的轮次时间更晚，不受影响
        cutoff = max([now] + [record.at for record in state.records])
        state.purged[spec] = max(state.purged.get(spec, 0.0), cutoff)
        state.prune_purged()
        self.save(key, state)

    def remap_platform(self, src: str, dst: str, *, move: bool) -> int:
        """平台改名后把旧平台的待整理记录改到新平台的会话键下。

        move=True 时删掉旧键；copy 时两边都保留。返回处理的会话数。只有带
        待整理轮次的会话才需要搬：空状态里只剩上次整理结果，搬过去没有意义。
        """
        changed = 0
        for key, state in self.items():
            if not state.records:
                continue
            prefix, sep, rest = state.umo.partition(":")
            if not sep or (state.platform or prefix) != src:
                continue
            new_umo = f"{dst}:{rest}"
            new_key = safe_key(new_umo)
            if new_key == key:
                continue
            target = self.load(new_key)
            merged = _merge_states(target, state)
            merged.umo = new_umo
            merged.platform = dst
            self._trim(merged)
            self.save(new_key, merged)
            if move:
                self.remove(key)
            changed += 1
        return changed

    def remove(self, key: str) -> None:
        """整理成功且没有剩余记录后删掉文件，重启后不会再找它。"""
        with self._lock:
            try:
                self.path(key).unlink()
            except OSError:
                pass

    # ------------------------------------------------------------ 追加

    def _trim(self, state: PendingState) -> int:
        dropped = 0
        while state.records and (
            len(state.records) > MAX_RECORDS
            or state.chars() > MAX_TOTAL_CHARS
        ):
            state.records.pop(0)
            dropped += 1
        state.prune_purged()
        return dropped

    def record_user(
        self,
        key: str,
        umo: str,
        text: str,
        admin: bool,
        sender: str,
        now: float,
        platform: str = "",
    ) -> tuple[PendingState, int]:
        """追加一轮的用户消息；同一个 prompt 重复进入钩子时不重复记账。"""
        state = self.load(key)
        state.umo = umo or state.umo
        state.platform = platform or state.platform
        if state.failures >= MAX_FAILURES and not state.records:
            # 上一段已经放弃；新的对话是新的开始，重新给三次机会
            state.failures = 0
        text = _truncate(text, MAX_MESSAGE_CHARS, key, "用户消息")
        last = state.records[-1] if state.records else None
        if last is not None and not last.reply and last.user == text:
            last.admin = bool(admin)
            last.sender = sender or last.sender
            self.save(key, state)
            return state, 0
        state.records.append(
            Round(
                seq=state.next_seq,
                at=now,
                user=text,
                admin=bool(admin),
                sender=sender,
            )
        )
        state.next_seq += 1
        state.last_round_at = now
        dropped = self._trim(state)
        self.save(key, state)
        return state, dropped

    def finish_reply(
        self,
        key: str,
        umo: str,
        text: str,
        now: float,
        platform: str = "",
    ) -> tuple[PendingState, bool, int]:
        """补上机器人最终回复；返回 (状态, 是否找到待补的轮次, 丢弃条数)。"""
        state = self.load(key)
        state.umo = umo or state.umo
        state.platform = platform or state.platform
        found = False
        for round_record in reversed(state.records):
            if not round_record.reply:
                round_record.reply = _truncate(
                    text, MAX_MESSAGE_CHARS, key, "机器人回复"
                )
                found = True
                break
        if not found:
            return state, False, 0
        state.rounds_since_write += 1
        state.last_round_at = now
        dropped = self._trim(state)
        self.save(key, state)
        return state, True, dropped

    def add_note(
        self,
        key: str,
        umo: str,
        content: str,
        sender: str,
        now: float,
        platform: str = "",
    ) -> tuple[PendingState, int]:
        """把一条 memory_note 记到当前轮；没有当前轮时单开一条。"""
        state = self.load(key)
        state.umo = umo or state.umo
        state.platform = platform or state.platform
        note = Note(
            content=_truncate(content, MAX_NOTE_CHARS, key, "memory_note"),
            sender=sender,
            at=now,
        )
        round_record = None
        if state.records and not state.records[-1].reply:
            round_record = state.records[-1]
        if round_record is None:
            round_record = Round(
                seq=state.next_seq,
                at=now,
                sender=sender,
            )
            state.next_seq += 1
            state.records.append(round_record)
        note_dropped = 0
        while len(round_record.notes) >= MAX_NOTES_PER_ROUND:
            round_record.notes.pop(0)
            note_dropped += 1
        round_record.notes.append(note)
        state.note_pending = True
        state.last_round_at = now
        dropped = self._trim(state)
        self.save(key, state)
        return state, dropped + note_dropped

    # ------------------------------------------------------------ 整理结果

    def drop_records(
        self, key: str, seqs: set[int], now: float, message: str
    ) -> PendingState:
        """整理成功：删掉处理过的轮次，重置失败计数与轮数。"""
        state = self.load(key)
        state.records = [
            record for record in state.records if record.seq not in seqs
        ]
        state.failures = 0
        state.last_attempt = now
        state.last_success = now
        state.last_result = message
        state.rounds_since_write = 0
        state.note_pending = any(record.notes for record in state.records)
        state.prune_purged()
        # 即使没有剩余记录也保留状态：管理页要显示上次整理的时间与结果
        self.save(key, state)
        return state

    def mark_attempt(self, key: str, now: float, message: str) -> PendingState:
        """整理没有写成功（快照冲突等）：保留记录，不算失败。"""
        state = self.load(key)
        state.last_attempt = now
        state.last_result = message
        state.rounds_since_write = 0
        self.save(key, state)
        return state

    def record_failure(
        self,
        key: str,
        now: float,
        message: str,
        seqs: set[int] | None = None,
    ) -> tuple[PendingState, bool]:
        """记一次失败；达到上限后放弃这一批并清掉。

        seqs 是这次实际交给模型的轮次：模型调用期间新到的轮次没有参与这次
        失败，不能跟着一起丢，放弃后要重新给它们三次机会。不传 seqs 时
        视为整批都失败。
        """
        state = self.load(key)
        state.failures += 1
        state.last_attempt = now
        state.rounds_since_write = 0
        abandoned = state.failures >= MAX_FAILURES
        if abandoned:
            if seqs is None:
                state.records = []
            else:
                state.records = [
                    record for record in state.records if record.seq not in seqs
                ]
            failures = state.failures
            if state.records:
                # 留下的是这次没参与的新轮次，重新计数让它们照常整理
                state.failures = 0
            state.note_pending = any(record.notes for record in state.records)
            state.last_result = f"{message}（连续失败 {failures} 次，已放弃）"
        else:
            state.last_result = message
        state.prune_purged()
        self.save(key, state)
        return state, abandoned

    def clear(self, key: str) -> None:
        """管理员清除会话数据时一并丢掉待整理记录。"""
        self.remove(key)
