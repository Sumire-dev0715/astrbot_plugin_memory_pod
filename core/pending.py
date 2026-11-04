"""待整理记录：每轮对话的临时缓冲，整理成功后删除。

记忆只由后台整理写入，聊天模型只读。每轮结束后，插件把这一轮的用户消息
（已带发送者标注）、机器人最终回复、发送者是否管理员记进本会话的待整理
记录；`memory_note` 的内容记在当前轮里。整理成功就删掉处理过的部分，失败
保留，连续失败 `MAX_FAILURES` 次后放弃并清空。

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
      "nextSeq": 2
    }
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

PENDING_VERSION = 1
# 一个会话最多保留多少轮对话、合计多少字；超出时丢最旧的
MAX_RECORDS = 40
MAX_TOTAL_CHARS = 24_000
# 单条消息与 memory_note 的截断上限（防止一段超长消息挤掉整段历史）
MAX_MESSAGE_CHARS = 4000
MAX_NOTE_CHARS = 1000
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

    @property
    def count(self) -> int:
        return len(self.records)

    def chars(self) -> int:
        return sum(round_record.chars() for round_record in self.records)

    def abandoned(self) -> bool:
        return self.failures >= MAX_FAILURES and not self.records

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

    def all(self) -> list[PendingState]:
        """全部会话的待整理记录；目录不存在时返回空列表。"""
        if not self.base_dir.is_dir():
            return []
        result: list[PendingState] = []
        for path in sorted(self.base_dir.glob("*.json")):
            key = path.stem
            state = self.load(key)
            if state.umo:
                result.append(state)
        return result

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
        text = (text or "")[:MAX_MESSAGE_CHARS]
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
                round_record.reply = (text or "")[:MAX_MESSAGE_CHARS]
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
            content=(content or "")[:MAX_NOTE_CHARS],
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
        round_record.notes.append(note)
        state.note_pending = True
        state.last_round_at = now
        dropped = self._trim(state)
        self.save(key, state)
        return state, dropped

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
        self, key: str, now: float, message: str
    ) -> tuple[PendingState, bool]:
        """记一次失败；达到上限后放弃并清空。返回 (状态, 是否已放弃)。"""
        state = self.load(key)
        state.failures += 1
        state.last_attempt = now
        state.rounds_since_write = 0
        abandoned = state.failures >= MAX_FAILURES
        if abandoned:
            state.records = []
            state.note_pending = False
            state.last_result = f"{message}（连续失败 {state.failures} 次，已放弃）"
        else:
            state.last_result = message
        self.save(key, state)
        return state, abandoned

    def clear(self, key: str) -> None:
        """管理员清除会话数据时一并丢掉待整理记录。"""
        self.remove(key)
