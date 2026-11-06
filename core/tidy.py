"""管理页的手动整理：让模型复盘一个作用域，给出可预览、可撤销的修改方案。

定期自动整理已经删除（PLAN 3.8.4）：整理只由管理员在管理页发起，使用
AstrBot 的默认对话模型。每处同一时间只跑一份（TidyBook.claim），模型认为
不需要整理就什么都不改；应用后记下改动前的原文，最近 KEEP_RUNS 次可以撤销。

磁盘布局（插件数据目录下 tidy/）：
    <作用域>.json           上次整理的时间与结论
    <作用域>.runs.json      最近几次应用的改动记录（含改动前原文，供撤销）
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

from . import consolidate, history, journal, memstore, origin, writer
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

# 撤销时判断"之后有没有再改过"的写入时间误差
MTIME_TOLERANCE = consolidate.MTIME_TOLERANCE


class TidyError(Exception):
    """可以直接展示给管理员的错误。"""


def scope_id(scope: str, key: str | None) -> str:
    if scope == "board":
        return "board"
    return f"{scope}-{key or ''}"


def scope_label(scope: str, key: str | None) -> str:
    """整理提示词里的作用域名称。

    这段提示词会发给 AstrBot 的默认对话模型，所以悄悄话不能用群名、
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
class Proposal:
    """一次复盘的结果：空的 operations 表示模型认为不需要整理。"""

    operations: list[consolidate.Operation]
    skipped: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)


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
            for suffix in (".json", ".runs.json"):
                path = self._path(sid, suffix)
                if path.is_file():
                    path.unlink()

    def remove_file(self, sid: str, file: str) -> bool:
        """把一个文件从整理记录里抹掉（管理员彻底删除文件时）。

        整理记录里的 ops.before 留着改动前的原文；文件被彻底删除后这些
        副本不能继续留在磁盘上，否则撤销整理还能把已删除的正文写回。
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
            return changed


# ---------------------------------------------------------------- 复盘与应用


async def propose(
    store: ScopeStore,
    label: str,
    call_model: Callable[[str], Awaitable[tuple[str | None, list[str]]]],
) -> Proposal:
    """把作用域的全部记忆交给模型复盘；失败抛 TidyError。"""
    headers = await asyncio.to_thread(store.headers)
    if not headers:
        raise TidyError("这里还没有记忆，无需整理")
    files = []
    for header in headers:
        content = await store.read(header.file)
        if content:
            files.append((header, content))
    prompt, skipped = consolidate.build_prompt(label, files)
    text, attempts = await call_model(prompt)
    if not text:
        raise TidyError(
            "整理模型调用失败：" + ("；".join(attempts) or "没有可用的模型提供商")
        )
    existing = {h.file for h in headers}
    ops = consolidate.parse_operations(text, existing)
    if ops is None:
        raise TidyError("模型返回的整理方案无法解析，请重试")
    # 日记只作整理参考，不允许整理改写或删除：流水账过时与否不该由一次
    # 复盘决定；误改也能撤销，但日记本就不该进入这套合并逻辑。
    journal_files = {
        header.file
        for header in headers
        if journal.is_journal(header.file, header.type)
    }
    ops = [
        op
        for op in ops
        if op.file not in journal_files and not journal.is_journal(op.file, op.type)
    ]
    # 留言板每条告示都必须带署名，且署名由插件决定：改写沿用原署名，
    # 合并出的新告示用这次整理看到的第一个署名；都没有时按管理页操作
    # 署名"管理员"。模型给的 signature 不可信（提示注入可能借整理换掉
    # 别人告示的署名），这里一律覆盖。
    if store.layer == memstore.SCOPE_BOARD:
        signatures = {}
        expiries = {}
        for header, content in files:
            doc = memstore.parse_memory(content)
            signatures[header.file] = doc.signature
            expiries[header.file] = doc.expires
        fallback = next((value for value in signatures.values() if value), "管理员")
        for op in ops:
            if op.op == "write":
                op.signature = signatures.get(op.file) or fallback
                # 到期日同样沿用原告示；合并出的新告示不凭空设日期
                op.expires = expiries.get(op.file) or ""
    # 记下复盘时各目标文件的写入时间，应用时据此识别期间被改过的文件
    for op in ops:
        op.base_mtime = store.mtime(op.file) or 0.0
    return Proposal(operations=ops, skipped=skipped, files=sorted(existing))


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
    """先写后删地应用方案；每项结果带改动前原文（before）供撤销。"""
    await _inherit_origins(store, ops)
    results = []
    write_failed = False
    for op in consolidate.ordered(ops):
        before = await store.read(op.file)
        # 删除会连使用统计一起清掉，撤销时要原样还回去
        usage = store.usage_of(op.file).to_raw() if op.op == "delete" else None
        if consolidate.is_stale(op, store.mtime(op.file)):
            ok, message = False, "复盘之后这个文件被改过，已跳过；请重新整理"
        elif op.op == "write":
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
                "created": op.get("op") == "write" and op.get("before") is None,
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
    源文件、再回退写入，与应用时的先写后删相反。
    """
    ops = [op for op in run.get("ops") or [] if isinstance(op, dict)]
    ordered = [op for op in ops if op.get("op") == "delete"] + [
        op for op in ops if op.get("op") == "write"
    ]
    results = []
    for op in ordered:
        file = str(op.get("file") or "")
        before = op.get("before")
        current = store.mtime(file)
        if op.get("op") == "delete":
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
        else:
            after = float(op.get("afterMtime") or 0.0)
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
        results.append({"op": op.get("op", ""), "file": file, "ok": ok, "message": message})
    return results
