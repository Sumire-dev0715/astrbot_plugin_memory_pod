"""管理页的整理：后台分批复盘一个作用域，给出可预览、可取消、可撤销的修改方案。

整理只由管理员在管理页发起，使用管理员选定的 AstrBot 对话模型（PLAN
3.12.1）。每处同一时间只跑一份，请求立刻返回任务 ID，状态记在 tidy/
下，刷新页面或跳到别的页面再回来都能看到（PLAN 3.12.4）。一次整理分批
覆盖这一处的全部文件，每批不超过 MAX_INPUT_CHARS；每批成功后记下参与
整理的文件的本次时间，下一次按它轮换顺序（PLAN 3.12.5）。方案生成后
插件再按应用后的内容检查悬空链接，自动修复作为单独的操作列进方案
（PLAN 3.12.6）。应用后记下改动前的原文，最近 KEEP_RUNS 次可以撤销。

磁盘布局（插件数据目录下 tidy/）：
    model.json              管理员选定的整理模型
    <作用域>.json           上次整理的时间与结论
    <作用域>.runs.json      最近几次应用的改动记录（含改动前原文，供撤销）
    <作用域>.task.json      当前/最近一次整理任务的状态与方案
    <作用域>.files.json     每个文件上次参与整理的时间
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import consolidate, history, insights, journal, memstore, origin, writer
from .memstore import (
    CONFLICT_OVERWRITE,
    CONFLICT_SKIP,
    ImportEntry,
    MemoryHeader,
    ScopeStore,
    Usage,
)

KEEP_RUNS = 5

TRIGGER_MANUAL = "manual"

OUTCOME_APPLIED = "applied"

# 整理任务的状态（PLAN 3.12.4）。queued 仍算 running，只是还没轮到执行。
TASK_RUNNING = "running"
TASK_READY = "ready"
TASK_FAILED = "failed"
TASK_CANCELLED = "cancelled"
TASK_INTERRUPTED = "interrupted"

# 同时最多跑几处整理（PLAN 3.12.4）
MAX_ACTIVE_TASKS = 2

MODEL_FILENAME = "model.json"

# 撤销时判断"之后有没有再改过"的写入时间误差
MTIME_TOLERANCE = consolidate.MTIME_TOLERANCE


def _now() -> float:
    """当前时间；单独成函数，测试可以替换它造出不同时间的版本。"""
    return time.time()


class TidyError(Exception):
    """可以直接展示给管理员的错误；proposal 是失败前已经完成的批次。"""

    def __init__(self, message: str, proposal: "Proposal | None" = None):
        super().__init__(message)
        self.proposal = proposal
        self.batch_index = 0


class TidyCancelled(Exception):
    """整理任务被管理员取消；已完成的批次留在 proposal 里。"""

    def __init__(self, proposal: "Proposal | None" = None):
        super().__init__("整理已取消")
        self.proposal = proposal


def scope_id(scope: str, key: str | None) -> str:
    if scope == "board":
        return "board"
    return f"{scope}-{key or ''}"


def parse_scope_id(sid: str) -> tuple[str, str]:
    """把 tidy/ 里的任务文件名还原成 (scope, key)。"""
    if sid == "board":
        return "board", ""
    for prefix in ("notebook-", "whisper-"):
        if sid.startswith(prefix):
            return prefix[:-1], sid[len(prefix):]
    return "", ""


def scope_label(scope: str, key: str | None) -> str:
    """整理提示词里的作用域名称。

    这段提示词会发给管理员选定的整理模型，所以悄悄话不能用群名、
    昵称或带群号的目录名：群名只在管理页里显示（PLAN 3.9.1 第 1 条）。
    """
    if scope == "board":
        return "留言板（所有会话可见，管理员发布的告示）"
    if scope == "notebook":
        return f"小本本（平台 {key}，这台机器人的所有群聊和私聊共享）"
    return "这段对话的悄悄话（只有这段对话可见）"


# ---------------------------------------------------------------- 记录


@dataclass
class TidyMeta:
    """一个作用域的手动整理记录。"""

    last_run: float = 0.0
    outcome: str = ""
    message: str = ""

    @classmethod
    def from_raw(cls, raw: object) -> "TidyMeta":
        if not isinstance(raw, dict):
            return cls()
        meta = cls(
            outcome=str(raw.get("outcome") or ""),
            message=str(raw.get("message") or ""),
        )
        try:
            meta.last_run = float(raw.get("lastRun") or 0.0)
        except (TypeError, ValueError):
            pass
        return meta

    def to_raw(self) -> dict:
        return {
            "lastRun": round(self.last_run, 3),
            "outcome": self.outcome,
            "message": self.message,
        }


@dataclass
class Batch:
    """一批要送给模型的文件；index 从 1 开始。"""

    index: int
    files: list[str] = field(default_factory=list)


@dataclass
class Proposal:
    """一次复盘的结果：空的 operations 表示模型认为不需要整理。"""

    operations: list[consolidate.Operation]
    skipped: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    # 单个文件就超过一批的预算，没有送给模型（PLAN 3.12.5）
    needs_split: list[str] = field(default_factory=list)
    batches: list[Batch] = field(default_factory=list)
    # 给页面看的补充说明（需要先拆分的文件等）
    notices: list[dict] = field(default_factory=list)

    def plan_dict(self) -> dict:
        return {
            "operations": [op.to_dict() for op in self.operations],
            "skipped": list(self.skipped),
            "files": list(self.files),
            "needsSplit": list(self.needs_split),
            "notices": list(self.notices),
            "fileCount": len(self.files),
        }


class TidyBook:
    """tidy/ 目录的读写（同步方法，调用方放进线程执行）与进行中标记。"""

    def __init__(self, base_dir: Path):
        self.base_dir = Path(base_dir)
        self._lock = threading.Lock()
        # 正在整理的作用域：管理页发起的两次整理不会同时跑
        self.busy: set[str] = set()

    # ---- 进行中标记（只在事件循环里用）

    def claim(self, sid: str) -> bool:
        if sid in self.busy:
            return False
        self.busy.add(sid)
        return True

    def release(self, sid: str) -> None:
        self.busy.discard(sid)

    # ---- 文件

    def _path(self, sid: str, suffix: str = ".json") -> Path:
        return self.base_dir / f"{sid}{suffix}"

    def _read(self, path: Path):
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return None

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tidy-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def meta(self, sid: str) -> TidyMeta:
        return TidyMeta.from_raw(self._read(self._path(sid)))

    def update_meta(self, sid: str, change: Callable[[TidyMeta], None]) -> TidyMeta:
        with self._lock:
            meta = self.meta(sid)
            change(meta)
            self._write(self._path(sid), meta.to_raw())
            return meta

    def runs(self, sid: str) -> list[dict]:
        data = self._read(self._path(sid, ".runs.json"))
        return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []

    def add_run(self, sid: str, run: dict) -> None:
        with self._lock:
            runs = [run] + self.runs(sid)
            self._write(self._path(sid, ".runs.json"), runs[:KEEP_RUNS])

    def mark_undone(self, sid: str, run_id: str, now: float) -> None:
        with self._lock:
            runs = self.runs(sid)
            for run in runs:
                if run.get("id") == run_id:
                    run["undoneAt"] = round(now, 3)
            self._write(self._path(sid, ".runs.json"), runs)

    def remove(self, sid: str) -> None:
        with self._lock:
            for suffix in (".json", ".runs.json", ".task.json", ".files.json"):
                path = self._path(sid, suffix)
                if path.is_file():
                    path.unlink()

    def remove_file(self, sid: str, file: str, *, drop_plan: bool = False) -> bool:
        """把一个文件从整理记录里抹掉（管理员彻底删除或移走文件时）。

        整理记录里的 ops.before 留着改动前的原文，任务方案里的每个
        write/fix 项也带着完整正文；文件被彻底删除后这些副本不能继续
        留在磁盘上，否则撤销整理或再应用方案还能把已删除的正文写回。
        drop_plan 为真时整份任务方案作废：模型可能已经把这个文件合并进
        别的文件，残留的 write/fix 正文里仍有它的原文，只删同名操作不够。
        返回是否真的清掉了记录。
        """
        file = (file or "").strip()
        if not file:
            return False
        with self._lock:
            runs = self.runs(sid)
            kept_runs: list[dict] = []
            changed = False
            for run in runs:
                ops = run.get("ops")
                if not isinstance(ops, list):
                    kept_runs.append(run)
                    continue
                kept_ops = [
                    op
                    for op in ops
                    if not (isinstance(op, dict) and str(op.get("file")) == file)
                ]
                if len(kept_ops) == len(ops):
                    kept_runs.append(run)
                    continue
                changed = True
                if kept_ops:
                    kept_runs.append({**run, "ops": kept_ops})
            if changed:
                self._write(self._path(sid, ".runs.json"), kept_runs)
            times = self.file_times(sid)
            if file in times:
                times.pop(file, None)
                self._write(self._times_path(sid), times)
                changed = True
            if self._purge_from_task(sid, file, drop_plan=drop_plan):
                changed = True
            return changed

    def _purge_from_task(
        self, sid: str, file: str, *, drop_plan: bool = False
    ) -> bool:
        """从当前任务方案里清掉这个文件的痕迹（调用方已持锁）。

        drop_plan 为真且任务不在运行时，直接丢掉整份方案：方案里的
        write/fix 正文可能是这个文件被合并进别的文件后的内容，逐项按
        文件名过滤会留下正文，之后应用方案就把已删除的内容写回去了。
        """
        task = self.task(sid)
        if task is None:
            return False
        if drop_plan and task.get("state") != TASK_RUNNING:
            path = self._task_path(sid)
            if path.is_file():
                path.unlink()
            return True
        plan = task.get("plan")
        if not isinstance(plan, dict):
            return False
        ops = plan.get("operations")
        if not isinstance(ops, list):
            return False
        kept = [
            op
            for op in ops
            if not (isinstance(op, dict) and str(op.get("file")) == file)
        ]
        if len(kept) == len(ops):
            return False
        plan["operations"] = kept
        plan["files"] = [name for name in plan.get("files") or [] if str(name) != file]
        plan["needsSplit"] = [
            name for name in plan.get("needsSplit") or [] if str(name) != file
        ]
        plan["notices"] = [
            item
            for item in plan.get("notices") or []
            if not (isinstance(item, dict) and str(item.get("file")) == file)
        ]
        for item in task.get("batches") or []:
            if isinstance(item, dict):
                item["files"] = [
                    name for name in item.get("files") or [] if str(name) != file
                ]
        if task.get("state") != TASK_RUNNING and not kept and not plan["files"]:
            # 文件删干净了，方案也空了：不必再留一个空任务
            path = self._task_path(sid)
            if path.is_file():
                path.unlink()
            return True
        self._write(self._task_path(sid), task)
        return True

    # ---- 整理任务（PLAN 3.12.4）

    def _task_path(self, sid: str) -> Path:
        return self._path(sid, ".task.json")

    def _times_path(self, sid: str) -> Path:
        return self._path(sid, ".files.json")

    def task(self, sid: str) -> dict | None:
        data = self._read(self._task_path(sid))
        return data if isinstance(data, dict) and data.get("state") else None

    def write_task(self, sid: str, task: dict) -> dict:
        with self._lock:
            self._write(self._task_path(sid), task)
        return task

    def update_task(
        self,
        sid: str,
        change: Callable[[dict], None],
        *,
        only_state: str | None = TASK_RUNNING,
        task_id: str | None = None,
    ) -> dict | None:
        """在锁里读出最新的任务、改完写回；状态或任务 ID 对不上时不动。

        取消操作也会走这把锁，所以后台任务完成一批时的进度更新不会把
        "已取消"覆盖回"整理中"。任务 ID 用来防止插件重载后旧实例的
        后台任务覆盖新实例创建的同名任务。
        """
        with self._lock:
            task = self.task(sid)
            if task is None:
                return None
            if task_id is not None and str(task.get("id") or "") != str(task_id):
                return task
            if only_state is not None and task.get("state") != only_state:
                return task
            change(task)
            task["updatedAt"] = round(_now(), 3)
            self._write(self._task_path(sid), task)
            return task

    def create_task(self, sid: str, task: dict) -> dict:
        with self._lock:
            self._write(self._task_path(sid), task)
        return task

    def cancel_task(self, sid: str, task_id: str | None = None) -> dict | None:
        """取消正在跑的任务；已完成的批次和方案原样保留。"""
        def change(task: dict) -> None:
            task["state"] = TASK_CANCELLED
            task["message"] = "已取消"
            task["queued"] = False

        return self.update_task(sid, change, task_id=task_id)

    def clear_task(self, sid: str) -> None:
        with self._lock:
            path = self._task_path(sid)
            if path.is_file():
                path.unlink()

    def mark_interrupted(self, now: float | None = None) -> int:
        """插件启动时把上次没跑完的任务标成已中断；完成的批次保留。"""
        stamp = _now() if now is None else now
        count = 0
        with self._lock:
            if not self.base_dir.is_dir():
                return 0
            for path in sorted(self.base_dir.glob("*.task.json")):
                data = self._read(path)
                if not isinstance(data, dict) or data.get("state") != TASK_RUNNING:
                    continue
                data["state"] = TASK_INTERRUPTED
                data["message"] = "插件重启，整理已中断"
                data["queued"] = False
                data["updatedAt"] = round(stamp, 3)
                self._write(path, data)
                count += 1
        return count

    def all_tasks(self) -> list[dict]:
        """磁盘上全部作用域的整理任务（管理页打开时一次读回）。"""
        result: list[dict] = []
        with self._lock:
            if not self.base_dir.is_dir():
                return result
            for path in sorted(self.base_dir.glob("*.task.json")):
                sid = path.name[: -len(".task.json")]
                data = self._read(path)
                if isinstance(data, dict) and data.get("state"):
                    result.append({**data, "sid": sid})
        return result

    # ---- 每个文件上次参与整理的时间（PLAN 3.12.5）

    def file_times(self, sid: str) -> dict[str, float]:
        data = self._read(self._times_path(sid))
        if not isinstance(data, dict):
            return {}
        result: dict[str, float] = {}
        for file, value in data.items():
            try:
                result[str(file)] = float(value)
            except (TypeError, ValueError):
                continue
        return result

    def touch_files(
        self,
        sid: str,
        files: list[str],
        now: float | None = None,
        *,
        task_id: str | None = None,
    ) -> None:
        """记下这一批文件参与整理的时间（每批成功后调用）。

        task_id 用来防止插件重载后旧实例的任务把新实例的进度写脏。
        """
        stamp = _now() if now is None else now
        with self._lock:
            if task_id is not None:
                task = self.task(sid)
                if task is None or str(task.get("id") or "") != str(task_id):
                    return
            times = self.file_times(sid)
            for file in files:
                if file:
                    times[file] = round(stamp, 3)
            self._write(self._times_path(sid), times)

    # ---- 选定的整理模型（PLAN 3.12.1）

    def selected_model(self) -> dict | None:
        data = self._read(self.base_dir / MODEL_FILENAME)
        if not isinstance(data, dict):
            return None
        provider_id = str(data.get("providerId") or "").strip()
        if not provider_id:
            return None
        return {
            "providerId": provider_id,
            "model": str(data.get("model") or "").strip(),
        }

    def save_model(self, provider_id: str, model: str) -> dict:
        selected = {
            "providerId": str(provider_id or "").strip(),
            "model": str(model or "").strip(),
        }
        with self._lock:
            self._write(self.base_dir / MODEL_FILENAME, selected)
        return selected


# ---------------------------------------------------------------- 分批


def file_flags(
    store: ScopeStore, headers: list[MemoryHeader], contents: dict[str, str]
) -> dict[str, dict]:
    """文件列表里的提醒标记：悬空链接与偏长（PLAN 3.12.5/3.12.6）。

    已遗忘的文件仍能被 memory_read 读到，管理页也把它们算作可用目标；
    指向它们的 [[链接]] 不算悬空，不能自动改成纯文本。
    """
    available = {header.file for header in headers} | set(store.forgotten_files())
    flags: dict[str, dict] = {}
    for header in headers:
        content = contents.get(header.file)
        if content is None:
            continue
        doc = memstore.parse_memory(content)
        missing = insights.dangling_links(
            memstore.extract_links(doc.body), available
        )
        reasons = insights.long_file_reasons(
            header.file, header.type, doc.body, len(content.encode("utf-8"))
        )
        if missing or reasons:
            flags[header.file] = {"dangling": missing, "long": reasons}
    return flags


def order_files(
    headers: list[MemoryHeader],
    flags: dict[str, dict],
    times: dict[str, float],
) -> list[MemoryHeader]:
    """整理顺序（PLAN 3.12.5）：

    有悬空链接或偏长标记的文件优先；其次是从没整理过的；再按上次参与
    整理的时间从早到晚。文件名兜底，保证同一份数据每次顺序一致。
    """
    def key(header: MemoryHeader) -> tuple:
        return (
            0 if flags.get(header.file) else 1,
            0 if header.file not in times else 1,
            times.get(header.file, 0.0),
            header.file,
        )

    return sorted(headers, key=key)


def split_batches(
    files: list[tuple[MemoryHeader, str]],
    budget: int | None = None,
) -> tuple[list[tuple[Batch, list[tuple[MemoryHeader, str]]]], list[str]]:
    """把排好序的文件按预算切批；返回 (批列表, 单个就超预算的文件名)。

    每个文件只出现在一批里，各批正文合计不超过预算。单个文件超过预算
    时不送给模型，由调用方在方案里标"需要先拆分"（PLAN 3.12.5）。
    """
    limit = consolidate.MAX_INPUT_CHARS if budget is None else budget
    batches: list[tuple[Batch, list[tuple[MemoryHeader, str]]]] = []
    current: list[tuple[MemoryHeader, str]] = []
    used = 0
    needs_split: list[str] = []
    for header, content in files:
        size = len(content)
        if size > limit:
            needs_split.append(header.file)
            continue
        if current and used + size > limit:
            index = len(batches) + 1
            batches.append(
                (Batch(index, [item[0].file for item in current]), current)
            )
            current = []
            used = 0
        current.append((header, content))
        used += size
    if current:
        index = len(batches) + 1
        batches.append((Batch(index, [item[0].file for item in current]), current))
    return batches, needs_split


# ---------------------------------------------------------------- 复盘


def _split_journal_ops(
    ops: list[consolidate.Operation], journal_files: set[str]
) -> list[consolidate.Operation]:
    # 日记只作整理参考，不允许整理改写或删除：流水账过时与否不该由一次
    # 复盘决定；误改也能撤销，但日记本就不该进入这套合并逻辑。
    return [
        op
        for op in ops
        if op.file not in journal_files and not journal.is_journal(op.file, op.type)
    ]


def _merge_ops(
    merged: dict[str, consolidate.Operation],
    owners: dict[str, int],
    delete_owners: dict[str, int],
    dropped_batches: set[int],
    conflicts: set[str],
    ops: list[consolidate.Operation],
    batch_files: set[str],
    batch_index: int,
    skipped: list[str],
    notices: list[dict],
) -> None:
    """把一批方案并进总方案。

    两个批次都要写同一个文件时无法自动合并（各自只看到一部分源文件），
    放弃这些写入以及这两批的删除，避免源文件删了、内容却没落盘。
    """
    for op in [item for item in ops if item.op == "write"]:
        if op.file in conflicts:
            # 这个目标已经被判定为跨批冲突，后来的写入也不能再落盘
            dropped_batches.add(batch_index)
            continue
        current = merged.get(op.file)
        if current is None:
            merged[op.file] = op
            owners[op.file] = batch_index
            continue
        # 同一个文件只能有一个写入；第二个写入来自另一批（各批文件不重叠，
        # 新建的目标文件则可能被多批同时选中）。
        previous = owners.get(op.file)
        merged.pop(op.file, None)
        owners.pop(op.file, None)
        if current.op == "delete":
            delete_owners.pop(op.file, None)
        if previous is not None:
            dropped_batches.add(previous)
        dropped_batches.add(batch_index)
        conflicts.add(op.file)
        if op.file not in skipped:
            skipped.append(op.file)
        notices.append(
            {
                "kind": "conflict",
                "batch": batch_index,
                "file": op.file,
                "message": (
                    f"多个批次都要改写 {op.file}，无法安全合并；"
                    "已放弃这些写入和相关的删除，请再整理一次"
                ),
            }
        )
    for op in [item for item in ops if item.op == "delete"]:
        if batch_index in dropped_batches or op.file in conflicts:
            continue
        if merged.get(op.file) is not None:
            # 同一个文件不能既写又删
            continue
        merged[op.file] = op
        delete_owners[op.file] = batch_index
    # 写入被放弃的批次，它的删除也要一起放弃（合并目标没落盘）
    for file in [
        name for name, owner in delete_owners.items() if owner in dropped_batches
    ]:
        merged.pop(file, None)
        delete_owners.pop(file, None)


def _board_fields(
    headers: list[MemoryHeader], contents: dict[str, str], is_board: bool
) -> tuple[dict[str, str], dict[str, str], str]:
    if not is_board:
        return {}, {}, ""
    signatures: dict[str, str] = {}
    expiries: dict[str, str] = {}
    for header in headers:
        content = contents.get(header.file)
        if content is None:
            continue
        doc = memstore.parse_memory(content)
        signatures[header.file] = doc.signature
        expiries[header.file] = doc.expires
    fallback = next((value for value in signatures.values() if value), "管理员")
    return signatures, expiries, fallback


def _prepare_op(
    op: consolidate.Operation,
    store: ScopeStore,
    signatures: dict[str, str],
    expiries: dict[str, str],
    fallback_signature: str,
    mtimes: dict[str, float],
) -> None:
    """写入留言板时署名与到期日由插件决定；并记下开始整理时的写入时间。

    base_mtime 必须来自任务开始时的快照（mtimes），不能用现在重新读到的
    时间：分批时前几批模型调用期间被改过的文件，否则不会被判为过期，
    应用时就会用旧正文把它覆盖掉（PLAN 3.12.4）。
    """
    if op.op in ("write", "fix") and store.layer == memstore.SCOPE_BOARD:
        op.signature = signatures.get(op.file) or fallback_signature
        op.expires = expiries.get(op.file) or ""
    op.base_mtime = mtimes.get(op.file, 0.0)


_LINK_RE = re.compile(r"\[\[([^\[\]\n]{1,120})\]\]")


def _fix_links(body: str, available: set[str]) -> tuple[str, list[str]]:
    """按应用后的文件集合修复悬空链接；返回 (新正文, 改动说明)。

    忽略大小写和 .md 后缀能唯一对上某个文件时改成指向它；对不上的
    去掉方括号只留文字（PLAN 3.12.6）。
    """
    stems: dict[str, list[str]] = {}
    for file in available:
        stem = file[: -len(memstore.MEMORY_FILE_SUFFIX)] if file.endswith(
            memstore.MEMORY_FILE_SUFFIX
        ) else file
        stems.setdefault(stem.lower(), []).append(stem)
    missing = set(
        insights.dangling_links(memstore.extract_links(body), available)
    )
    if not missing:
        return body, []
    changes: list[str] = []

    def replace(match: re.Match) -> str:
        target = match.group(1).strip()
        name = insights.normalize_link_target(target)
        if name not in missing:
            return match.group(0)
        stem = name[: -len(memstore.MEMORY_FILE_SUFFIX)] if name.endswith(
            memstore.MEMORY_FILE_SUFFIX
        ) else name
        candidates = stems.get(stem.lower()) or []
        if len(candidates) == 1:
            changes.append(f"[[{target}]] 改成 [[{candidates[0]}]]")
            return f"[[{candidates[0]}]]"
        changes.append(f"[[{target}]] 改成纯文本")
        return target

    fixed = _LINK_RE.sub(replace, body)
    return fixed, changes


def auto_fix_operations(
    store: ScopeStore,
    headers: list[MemoryHeader],
    contents: dict[str, str],
    ops: list[consolidate.Operation],
) -> list[consolidate.Operation]:
    """按方案应用后的内容检查全部文件的悬空链接，生成单独的修复操作。"""
    is_board = store.layer == memstore.SCOPE_BOARD
    final: dict[str, dict] = {}
    for header in headers:
        content = contents.get(header.file)
        if content is not None:
            doc = memstore.parse_memory(content)
            final[header.file] = {
                "type": memstore.effective_type(header.file, doc.type),
                "description": doc.description,
                "body": doc.body,
                "signature": doc.signature,
                "expires": doc.expires,
            }
        else:
            # 非 UTF-8 文件读不出正文，但它确实存在：链接到它不算悬空；
            # 文件本身没法检查/修复，也不该被当成"不存在"。
            final[header.file] = {
                "type": memstore.effective_type(header.file, header.type),
                "description": header.description,
                "body": "",
                "signature": header.signature,
                "expires": header.expires,
            }
    written: set[str] = set()
    for op in ops:
        if op.op == "delete":
            final.pop(op.file, None)
            written.discard(op.file)
            continue
        if op.op not in ("write", "fix"):
            continue
        base = final.get(op.file) or {
            "type": "",
            "description": "",
            "body": "",
            "signature": "",
            "expires": "",
        }
        final[op.file] = {
            "type": op.type or base["type"],
            "description": op.description or base["description"],
            "body": op.content,
            "signature": (op.signature or base["signature"]) if is_board else "",
            "expires": (op.expires or base["expires"]) if is_board else "",
        }
        if op.op == "write":
            written.add(op.file)
    available = set(final) | set(store.forgotten_files())
    fixes: list[consolidate.Operation] = []
    for file in sorted(final):
        item = final[file]
        fixed, changes = _fix_links(item["body"], available)
        if not changes or fixed == item["body"]:
            continue
        fixes.append(
            consolidate.Operation(
                op="fix",
                file=file,
                reason=("自动修复悬空链接：" + "；".join(changes))[
                    : consolidate.MAX_REASON_CHARS
                ],
                type=item["type"],
                description=item["description"],
                content=fixed,
                signature=item["signature"] if is_board else "",
                expires=item["expires"] if is_board else "",
                auto=True,
                depends_on=file if file in written else "",
            )
        )
    return fixes


def _partial_proposal(
    store: ScopeStore,
    headers: list[MemoryHeader],
    contents: dict[str, str],
    merged: dict[str, consolidate.Operation],
    skipped: list[str],
    needs_split: list[str],
    batches: list[Batch],
    notices: list[dict],
    mtimes: dict[str, float],
) -> Proposal:
    ops = list(merged.values())
    ops.extend(auto_fix_operations(store, headers, contents, ops))
    for op in ops:
        if op.op == "fix":
            # 自动修复的正文也来自任务开始时的快照：期间被改过或删掉的
            # 文件，应用时要靠这个时间判为过期，不能用现在的时间
            op.base_mtime = mtimes.get(op.file, 0.0)
    return Proposal(
        operations=ops,
        skipped=list(skipped),
        files=sorted(header.file for header in headers),
        needs_split=list(needs_split),
        batches=list(batches),
        notices=list(notices),
    )


async def propose(
    store: ScopeStore,
    label: str,
    call_model: Callable[[str], Awaitable[tuple[str | None, list[str]]]],
    *,
    now: float | None = None,
    on_planned: Callable[[list[Batch], list[str], int], None] | None = None,
    on_batch: Callable[[Batch, "Proposal"], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    budget: int | None = None,
    file_times: dict[str, float] | None = None,
) -> Proposal:
    """分批把作用域的全部记忆交给模型复盘；失败抛 TidyError。

    每批调用一次模型，各批文件互不重叠（PLAN 3.12.5）。模型只能改动
    本批附了正文的文件，或者新建一个还不存在的文件；批外已有文件只在
    清单里出现名字和摘要，针对它们的写入/删除会被丢弃并在方案里说明，
    避免模型凭摘要改写/删除没读过的文件。两个批次同时写同一个文件时
    放弃这些写入及相关的删除。取消时抛 TidyCancelled，异常里带着已经
    完成的批次；失败时 TidyError.proposal 同理。两者都已经包含悬空
    链接的自动修复操作。
    """
    stamp = _now() if now is None else now
    headers = await asyncio.to_thread(store.headers)
    if not headers:
        raise TidyError("这里还没有记忆，无需整理")
    # 方案只认任务开始时的快照：先记下写入时间再读正文。读取之后（包括
    # 后续几批模型调用期间）文件再被改动，应用时就会因时间对不上而跳过，
    # 不会拿快照里的旧正文覆盖新内容（PLAN 3.12.4）。
    mtimes = {header.file: header.mtime for header in headers}
    contents: dict[str, str] = {}
    for header in headers:
        content = await store.read(header.file)
        if content is not None:
            contents[header.file] = content
    present = [header for header in headers if header.file in contents]
    existing = {header.file for header in headers}
    is_board = store.layer == memstore.SCOPE_BOARD
    flags = file_flags(store, headers, contents)
    ordered = order_files(present, flags, file_times or {})
    raw_batches, needs_split = split_batches(
        [(header, contents[header.file]) for header in ordered], budget
    )
    batches = [batch for batch, _ in raw_batches]
    notices: list[dict] = [
        {
            "kind": "needs_split",
            "file": file,
            "message": "这个文件单独就超过一次整理的预算，需要先拆分",
        }
        for file in needs_split
    ]
    notices.extend(
        {
            "kind": "unreadable",
            "file": header.file,
            "message": "文件不是有效的 UTF-8 文本，没有送给模型",
        }
        for header in headers
        if header.file not in contents
    )
    if on_planned is not None:
        on_planned(batches, needs_split, len(headers))
    signatures, expiries, fallback_signature = _board_fields(
        present, contents, is_board
    )
    journal_files = {
        header.file
        for header in headers
        if journal.is_journal(header.file, header.type)
    }
    merged: dict[str, consolidate.Operation] = {}
    owners: dict[str, int] = {}
    delete_owners: dict[str, int] = {}
    dropped_batches: set[int] = set()
    conflicts: set[str] = set()
    skipped: list[str] = []
    catalog = headers

    def partial() -> Proposal:
        return _partial_proposal(
            store,
            headers,
            contents,
            merged,
            skipped,
            needs_split,
            batches,
            notices,
            mtimes,
        )

    for batch, batch_items in raw_batches:
        if cancelled is not None and cancelled():
            raise TidyCancelled(partial())
        dangling = {
            header.file: (flags.get(header.file) or {}).get("dangling", [])
            for header, _ in batch_items
        }
        long_files = {
            header.file: (flags.get(header.file) or {}).get("long", [])
            for header, _ in batch_items
        }
        prompt, _ = consolidate.build_prompt(
            label,
            batch_items,
            stamp,
            board=is_board,
            catalog=catalog,
            dangling=dangling,
            long_files=long_files,
            total_files=len(headers),
        )
        text, attempts = await call_model(prompt)
        if cancelled is not None and cancelled():
            raise TidyCancelled(partial())
        if not text:
            error = TidyError(
                f"第 {batch.index} 批整理模型调用失败："
                + ("；".join(attempts) or "没有可用的模型"),
                partial(),
            )
            error.batch_index = batch.index
            raise error
        ops = consolidate.parse_operations(text, existing, board=is_board)
        if ops is None:
            error = TidyError(
                f"第 {batch.index} 批的整理方案无法解析，请重试", partial()
            )
            error.batch_index = batch.index
            raise error
        ops = _split_journal_ops(ops, journal_files)
        # 模型只能改本批附了正文的文件；新建文件没有旧正文可覆盖，也允许。
        # 批外已有文件只在清单里出现名字和摘要，针对它们的写入/删除一律
        # 丢弃；如果这一批因此丢了写入，同批的删除也一起放弃，避免源文件
        # 删了、合并目标却没落盘（PLAN 3.12.5）。
        batch_files = set(batch.files)

        def allowed(op: consolidate.Operation) -> bool:
            if op.file in batch_files:
                return True
            return op.op == "write" and op.file not in existing

        out_of_batch = [op for op in ops if not allowed(op)]
        ops = [op for op in ops if allowed(op)]
        if out_of_batch:
            if any(op.op == "write" for op in out_of_batch):
                ops = [op for op in ops if op.op != "delete"]
            for op in out_of_batch:
                if op.file not in skipped:
                    skipped.append(op.file)
            notices.append(
                {
                    "kind": "out_of_batch",
                    "batch": batch.index,
                    "file": sorted({op.file for op in out_of_batch})[0],
                    "message": (
                        f"第 {batch.index} 批的方案改动了本批之外的已有文件，已跳过："
                        + "、".join(sorted({op.file for op in out_of_batch}))
                    ),
                }
            )
        # 有写入没通过校验（类型与文件名对不上、正文为空等）时，同方案的删除
        # 必须一起放弃：合并目标没落盘，再删源文件就会丢内容。
        missing_writes = consolidate.missing_write_files(text, ops)
        if missing_writes:
            ops = [op for op in ops if op.op != "delete"]
            skipped.extend(name for name in sorted(missing_writes) if name not in skipped)
        for op in ops:
            _prepare_op(
                op,
                store,
                signatures,
                expiries,
                fallback_signature,
                mtimes,
            )
        _merge_ops(
            merged,
            owners,
            delete_owners,
            dropped_batches,
            conflicts,
            ops,
            batch_files,
            batch.index,
            skipped,
            notices,
        )
        if on_batch is not None:
            on_batch(batch, partial())
    final = partial()
    return final


# ---------------------------------------------------------------- 后台任务


def new_task(model: dict | None, now: float | None = None) -> dict:
    """一份新整理任务的初始状态（PLAN 3.12.4）。"""
    stamp = _now() if now is None else now
    return {
        "id": f"{int(stamp * 1000)}-{secrets.token_hex(4)}",
        "state": TASK_RUNNING,
        "startedAt": round(stamp, 3),
        "updatedAt": round(stamp, 3),
        "model": {
            "providerId": str((model or {}).get("providerId") or ""),
            "model": str((model or {}).get("model") or ""),
        },
        "queued": False,
        "fileCount": 0,
        "batchCount": 0,
        "batchesDone": 0,
        "batches": [],
        "needsSplit": [],
        "plan": {},
        "message": "",
    }


def public_task(task: dict | None) -> dict | None:
    """给页面的任务视图：只带状态、进度和方案，不含内部路径。"""
    if not isinstance(task, dict):
        return None
    plan = task.get("plan") if isinstance(task.get("plan"), dict) else {}
    batches = []
    for item in task.get("batches") or []:
        if not isinstance(item, dict):
            continue
        batches.append(
            {
                "index": int(item.get("index") or 0),
                "files": [str(name) for name in item.get("files") or []],
                "state": str(item.get("state") or ""),
                "error": str(item.get("error") or ""),
            }
        )
    return {
        "id": str(task.get("id") or ""),
        "state": str(task.get("state") or ""),
        "startedAt": float(task.get("startedAt") or 0.0),
        "updatedAt": float(task.get("updatedAt") or 0.0),
        "queued": bool(task.get("queued")),
        "model": {
            "providerId": str((task.get("model") or {}).get("providerId") or ""),
            "model": str((task.get("model") or {}).get("model") or ""),
        },
        "fileCount": int(task.get("fileCount") or 0),
        "batchCount": int(task.get("batchCount") or 0),
        "batchesDone": int(task.get("batchesDone") or 0),
        "batches": batches,
        "needsSplit": [str(name) for name in task.get("needsSplit") or []],
        "plan": {
            "operations": [
                op
                for op in plan.get("operations") or []
                if isinstance(op, dict)
            ],
            "skipped": [str(name) for name in plan.get("skipped") or []],
            "files": [str(name) for name in plan.get("files") or []],
            "needsSplit": [
                str(name) for name in plan.get("needsSplit") or []
            ],
            "notices": [
                item
                for item in plan.get("notices") or []
                if isinstance(item, dict)
            ],
            "fileCount": int(plan.get("fileCount") or 0),
        },
        "message": str(task.get("message") or ""),
    }


def _save_partial(
    book: TidyBook, sid: str, proposal: Proposal | None, task_id: str
) -> None:
    if proposal is None:
        return

    def change(task: dict) -> None:
        task["plan"] = proposal.plan_dict()
        task["needsSplit"] = list(proposal.needs_split)

    book.update_task(sid, change, only_state=None, task_id=task_id)


async def run_task(
    book: TidyBook,
    sid: str,
    store: ScopeStore,
    label: str,
    call_model: Callable[[str], Awaitable[tuple[str | None, list[str]]]],
    *,
    now_fn: Callable[[], float] = _now,
    task_id: str | None = None,
) -> dict | None:
    """在后台执行一份整理任务；进度、方案和终态都写进 tidy/。

    task_id 锁定这份任务：插件重载后旧实例的后台任务即使还在跑，也
    不能覆盖新实例用同一 sid 创建的新任务。
    """
    current = book.task(sid)
    if current is None:
        return None
    task_id = str(task_id or current.get("id") or "")
    if str(current.get("id") or "") != task_id:
        return current

    def cancelled() -> bool:
        task = book.task(sid)
        return (
            task is None
            or task.get("state") != TASK_RUNNING
            or str(task.get("id") or "") != task_id
        )

    def stopped() -> None:
        def change(task: dict) -> None:
            task["queued"] = False
            task["message"] = "正在读取文件…"

        book.update_task(sid, change, task_id=task_id)

    def planned(batches: list[Batch], needs_split: list[str], file_count: int) -> None:
        def change(task: dict) -> None:
            task["fileCount"] = file_count
            task["batchCount"] = len(batches)
            task["batchesDone"] = 0
            task["needsSplit"] = list(needs_split)
            task["batches"] = [
                {"index": batch.index, "files": list(batch.files), "state": "pending", "error": ""}
                for batch in batches
            ]
            task["message"] = ""

        book.update_task(sid, change, task_id=task_id)

    def batch_done(batch: Batch, proposal: Proposal) -> None:
        book.touch_files(sid, batch.files, now_fn(), task_id=task_id)

        def change(task: dict) -> None:
            task["batchesDone"] = batch.index
            items = task.get("batches") or []
            if 1 <= batch.index <= len(items):
                items[batch.index - 1]["state"] = "done"
                items[batch.index - 1]["error"] = ""
            task["plan"] = proposal.plan_dict()

        book.update_task(sid, change, task_id=task_id)

    if cancelled():
        return book.task(sid)
    stopped()
    try:
        proposal = await propose(
            store,
            label,
            call_model,
            now=now_fn(),
            on_planned=planned,
            on_batch=batch_done,
            cancelled=cancelled,
            file_times=book.file_times(sid),
        )
    except TidyCancelled as exc:
        _save_partial(book, sid, exc.proposal, task_id)
        return book.task(sid)
    except TidyError as exc:
        if cancelled():
            _save_partial(book, sid, exc.proposal, task_id)
            return book.task(sid)
        if exc.proposal is not None:
            _save_partial(book, sid, exc.proposal, task_id)

        def change(task: dict) -> None:
            task["state"] = TASK_FAILED
            task["queued"] = False
            task["message"] = str(exc) or "整理失败"
            index = int(getattr(exc, "batch_index", 0) or 0)
            items = task.get("batches") or []
            if 1 <= index <= len(items):
                items[index - 1]["state"] = "failed"
                items[index - 1]["error"] = str(exc) or "整理失败"

        book.update_task(sid, change, task_id=task_id)
        return book.task(sid)
    except asyncio.CancelledError:
        def change(task: dict) -> None:
            task["state"] = TASK_INTERRUPTED
            task["queued"] = False
            task["message"] = "插件停止，整理已中断"

        book.update_task(sid, change, task_id=task_id)
        raise
    except Exception as exc:  # noqa: BLE001 - 后台任务不能把异常抛回请求
        if cancelled():
            return book.task(sid)

        def change(task: dict) -> None:
            task["state"] = TASK_FAILED
            task["queued"] = False
            task["message"] = f"整理失败（{type(exc).__name__}）"

        book.update_task(sid, change, task_id=task_id)
        return book.task(sid)
    else:
        def change(task: dict) -> None:
            task["state"] = TASK_READY
            task["queued"] = False
            task["message"] = ""
            task["batchesDone"] = len(proposal.batches)
            for item in task.get("batches") or []:
                if item.get("state") == "pending":
                    item["state"] = "done"
            task["plan"] = proposal.plan_dict()

        book.update_task(sid, change, task_id=task_id)
        return book.task(sid)


class TidyRunner:
    """整理任务的调度：同时最多跑 max_active 处，其余排队（PLAN 3.12.4）。"""

    def __init__(self, max_active: int = MAX_ACTIVE_TASKS):
        self.max_active = max(1, int(max_active))
        self._active: set[str] = set()
        self._active_drop: dict[str, Callable[[], None] | None] = {}
        self._pending: list[
            tuple[str, Callable[[], Awaitable[None]], Callable[[], None] | None]
        ] = []
        self.tasks: set[asyncio.Task] = set()
        self._loop = None

    @staticmethod
    def _call_drop(callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:  # noqa: BLE001 - 释放 claim 失败不能拖垮调度
            pass

    def _drop_pending(self) -> None:
        pending, self._pending = self._pending, []
        for _sid, _work, on_drop in pending:
            self._call_drop(on_drop)

    def schedule(
        self,
        sid: str,
        work: Callable[[], Awaitable[None]],
        on_drop: Callable[[], None] | None = None,
    ) -> bool:
        """排入一个任务；返回 False 表示正在排队，还不算开始执行。

        on_drop 在这个任务被取消/丢弃、永远不会执行时调用，用来释放
        tidy_start 拿到的作用域锁。
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # 请求循环换了（测试里每次 asyncio.run 都是新循环）：旧任务已结束，
            # 没执行的排队任务要把 claim 一起释放，否则这一处会一直显示在整理中
            self._loop = loop
            self._drop_pending()
            for callback in list(self._active_drop.values()):
                self._call_drop(callback)
            self._active.clear()
            self._active_drop.clear()
            self.tasks.clear()
        if len(self._active) >= self.max_active:
            self._pending.append((sid, work, on_drop))
            return False
        self._spawn(sid, work, on_drop)
        return True

    def _spawn(
        self,
        sid: str,
        work: Callable[[], Awaitable[None]],
        on_drop: Callable[[], None] | None = None,
    ) -> None:
        self._active.add(sid)
        self._active_drop[sid] = on_drop
        task = asyncio.create_task(self._run(sid, work))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _run(
        self, sid: str, work: Callable[[], Awaitable[None]]
    ) -> None:
        try:
            await work()
        finally:
            self._active.discard(sid)
            self._call_drop(self._active_drop.pop(sid, None))
            while self._pending:
                next_sid, next_work, next_drop = self._pending.pop(0)
                if next_sid in self._active:
                    self._call_drop(next_drop)
                    continue
                self._spawn(next_sid, next_work, next_drop)
                break

    def drop(self, sid: str) -> bool:
        """丢掉一个还没开始执行的排队任务；返回是否真的丢掉了。"""
        kept = []
        dropped = False
        for sid_value, work, on_drop in self._pending:
            if sid_value == sid:
                self._call_drop(on_drop)
                dropped = True
            else:
                kept.append((sid_value, work, on_drop))
        self._pending = kept
        return dropped

    def active_count(self) -> int:
        return len(self._active)

    def pending_count(self) -> int:
        return len(self._pending)

    async def stop(self) -> None:
        """插件卸载/重载时取消所有整理任务（含排队中的）。"""
        self._drop_pending()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
        for callback in list(self._active_drop.values()):
            self._call_drop(callback)
        self._active.clear()
        self._active_drop.clear()

    async def wait(self) -> None:
        """等所有已排队的任务结束（测试与页面运行时后端用）。"""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


# ---------------------------------------------------------------- 合并与应用


def _merged_source(
    target: str,
    target_content: str,
    source_file: str,
    source_doc: memstore.MemoryDoc,
    single_write: bool,
) -> bool:
    """判断被删除的源文件是否并进了这个目标，用来继承它的 origin。

    先按人物档案的数字 ID 对（user-<ID>.md 合并同名 ID 的文件）；再按
    正文重叠判断（整理提示词要求只重组、精简、改写已有信息，合并后的
    内容通常保留源文件的整段或整行）。只有一个写入目标时，删掉的文件
    都只能并进它，直接算作来源。
    """
    if single_write:
        return True
    subject = writer.person_id(target)
    if subject:
        ids = set((source_doc.source or "").split())
        ids.update(record.speaker for record in origin.parse(source_doc.origin))
        if subject in ids or re.search(
            rf"(?<!\d){re.escape(subject)}(?!\d)", source_file
        ):
            return True
    body = (source_doc.body or "").strip()
    if body and body in target_content:
        return True
    lines = [
        line.strip()
        for line in body.splitlines()
        if len(line.strip()) >= 12
    ]
    return any(line in target_content for line in lines)


async def _inherit_origins(
    store: ScopeStore, ops: list[consolidate.Operation]
) -> None:
    """把被合并源文件的 origin 并进写入目标，避免一次整理丢掉来源标注。

    目标文件原本就有的 origin 由写入层继承；这里补的是"源文件删掉、正文
    并进新文件"时的那部分。管理页写入本身不新增来源记录（PLAN 3.9.1）。
    """
    writes = [op for op in ops if op.op == "write"]
    deletes = [op for op in ops if op.op == "delete"]
    if not writes or not deletes:
        return
    sources = {}
    for op in deletes:
        content = await store.read(op.file)
        if content is not None:
            sources[op.file] = memstore.parse_memory(content)
    single_write = len(writes) == 1
    for op in writes:
        current = await store.read(op.file)
        doc = memstore.parse_memory(current) if current is not None else None
        text = doc.origin if doc is not None else ""
        for source_file, source_doc in sources.items():
            if source_file == op.file or not source_doc.origin:
                continue
            if not _merged_source(
                op.file, op.content, source_file, source_doc, single_write
            ):
                continue
            for record in origin.parse(source_doc.origin):
                text = origin.merge(text, record)
        op.origin = text


async def apply(
    store: ScopeStore,
    ops: list[consolidate.Operation],
    actor: str = history.ACTOR_TIDY,
) -> list[dict]:
    """先写、再自动修复、最后删地应用方案；每项结果带改动前原文供撤销。"""
    await _inherit_origins(store, ops)
    results = []
    write_failed = False
    written: set[str] = set()
    for op in consolidate.ordered(ops):
        before = await store.read(op.file)
        # 删除会连使用统计一起清掉，撤销时要原样还回去
        usage = store.usage_of(op.file).to_raw() if op.op == "delete" else None
        if op.op == "fix" and op.depends_on and op.depends_on not in written:
            ok, message = False, "生成自动修复所依据的改写没有应用，已跳过"
        elif consolidate.is_stale(op, store.mtime(op.file)) and not (
            op.op == "fix" and op.file in written
        ):
            ok, message = False, "复盘之后这个文件被改过，已跳过；请重新整理"
        elif op.op in ("write", "fix"):
            report = await store.write(op.file, op.render(), actor=actor)
            ok, message = report.ok, report.message
        elif write_failed:
            # 合并目标没写成就不能删源文件，否则内容就丢了
            ok, message = False, "有写入未完成，为避免丢内容已跳过删除"
        else:
            report = await store.delete(op.file, actor=actor)
            ok, message = report.ok, report.message
        if op.op == "write" and not ok:
            write_failed = True
        if op.op in ("write", "fix") and ok:
            written.add(op.file)
        item = {
            "op": op.op,
            "file": op.file,
            "reason": op.reason,
            "ok": ok,
            "message": message,
        }
        if ok:
            item["before"] = before
            item["afterMtime"] = store.mtime(op.file) or 0.0
            if usage:
                item["usage"] = usage
        results.append(item)
    return results


def make_run(trigger: str, results: list[dict], now: float) -> dict | None:
    """把一次应用记成可撤销的记录；一项都没成功就不记。"""
    done = [r for r in results if r.get("ok")]
    if not done:
        return None
    return {
        "id": f"{int(now * 1000)}",
        "at": round(now, 3),
        "trigger": trigger,
        "ops": [
            {
                "op": r["op"],
                "file": r["file"],
                "reason": r.get("reason", ""),
                "before": r.get("before"),
                "afterMtime": r.get("afterMtime", 0.0),
                **({"usage": r["usage"]} if r.get("usage") else {}),
            }
            for r in done
        ],
        "undoneAt": 0,
    }


def public_results(results: list[dict]) -> list[dict]:
    """给页面的结果：去掉改动前原文。"""
    return [
        {k: v for k, v in r.items() if k not in ("before", "afterMtime", "usage")}
        for r in results
    ]


def public_run(run: dict) -> dict:
    return {
        "id": run.get("id", ""),
        "at": run.get("at", 0),
        "trigger": run.get("trigger", ""),
        "undoneAt": run.get("undoneAt", 0),
        "ops": [
            {
                "op": op.get("op", ""),
                "file": op.get("file", ""),
                "reason": op.get("reason", ""),
                "created": op.get("op") in ("write", "fix") and op.get("before") is None,
            }
            for op in run.get("ops") or []
            if isinstance(op, dict)
        ],
    }


async def undo(
    store: ScopeStore, run: dict, actor: str = history.ACTOR_TIDY
) -> list[dict]:
    """撤销一次整理：被删的文件写回、被改写的恢复原文、新建的删掉。

    之后又被改过的文件不动（以免盖掉新内容），逐项报告。先恢复被删的
    源文件、再回退写入，与应用时的先写后删相反。同一个文件同时有写入和
    自动修复时，整组按最后一次应用后的写入时间判断，恢复最早的原文。
    """
    ops = [op for op in run.get("ops") or [] if isinstance(op, dict)]
    deletes = [op for op in ops if op.get("op") == "delete"]
    groups: dict[str, list[dict]] = {}
    for op in ops:
        if op.get("op") == "delete":
            continue
        file = str(op.get("file") or "")
        groups.setdefault(file, []).append(op)
    results = []
    for op in deletes:
        file = str(op.get("file") or "")
        before = op.get("before")
        current = store.mtime(file)
        if current is not None or store.has_forgotten(file):
            ok, message = False, "同名文件已经存在，没有写回"
        elif not isinstance(before, str):
            ok, message = False, "没有留下原文，无法写回"
        else:
            entry = ImportEntry(file, before, usage=Usage.from_raw(op.get("usage")))
            outcome = await store.import_files(
                [entry], CONFLICT_SKIP, actor=actor
            )
            ok = bool(outcome) and outcome[0].landed
            message = "已写回" if ok else (outcome[0].reason if outcome else "写回失败")
        results.append({"op": "delete", "file": file, "ok": ok, "message": message})
    for file, group in groups.items():
        first = group[0]
        last = group[-1]
        before = first.get("before")
        current = store.mtime(file)
        after = float(last.get("afterMtime") or 0.0)
        if current is None or abs(current - after) > MTIME_TOLERANCE:
            ok, message = False, "整理之后这个文件又被改过或删掉，没有回退"
        elif before is None:
            report = await store.delete(file, actor=actor)
            ok, message = report.ok, "已删除整理时新建的文件" if report.ok else report.message
        else:
            outcome = await store.import_files(
                [ImportEntry(file, str(before))],
                CONFLICT_OVERWRITE,
                actor=actor,
            )
            ok = bool(outcome) and outcome[0].landed
            message = "已恢复原文" if ok else (outcome[0].reason if outcome else "恢复失败")
        for op in group:
            results.append(
                {
                    "op": op.get("op", ""),
                    "file": file,
                    "ok": ok,
                    "message": message,
                }
            )
    return results
