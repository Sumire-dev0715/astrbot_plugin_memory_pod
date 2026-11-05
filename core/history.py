"""自动历史、回收站与审计。

任何覆盖或移除记忆文件的代码路径，都在作用域锁里先经过这里把旧内容留住：

    <作用域>/.history/<文件名>/<YYYYmmdd-HHMMSS-fff>-<动作>.md   覆盖或修改前的旧内容
    <作用域>/.trash/<YYYYmmdd-HHMMSS-fff>__<文件名>               被删除的文件
    <作用域>/.audit.jsonl                                        每次变更一行

三个路径都以 "." 开头、不是作用域根目录下的 .md 文件，所以 list_files、
headers、索引、召回、搜索、查重与遗忘扫描天然看不到它们。

真正删除字节的只有两种情况：管理员在回收站点"彻底删除"，以及保留期到期
清理。保留期由 history_keep_days / trash_keep_days 控制，0 表示永久；
历史版本清理时每个文件至少保留最近 MIN_KEEP_VERSIONS 版。清理每个作用域
至多每 CLEANUP_INTERVAL 秒跑一次，由调用方放进工作线程执行。

这里只做文件操作，不碰索引与使用统计；作用域语义（哪条算在册、索引怎么
同步）仍由 core/memstore.py 的 ScopeStore 负责。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path

HISTORY_DIRNAME = ".history"
TRASH_DIRNAME = ".trash"
AUDIT_FILENAME = ".audit.jsonl"
# 与 memstore.FORGOTTEN_DIRNAME 一致：整作用域归档里的已遗忘子目录
FORGOTTEN_DIRNAME = "forgotten"
# 与 memstore.INDEX_FILENAME 一致：归档里的索引文件，彻底遗忘时也要删掉它的行
INDEX_FILENAME = "MEMORY.md"
# 被 purge 的整个作用域目录挪到 memories/.trash-scopes/<层>-<键>-<时间>/
SCOPE_TRASH_DIRNAME = ".trash-scopes"

DEFAULT_KEEP_DAYS = 90
# 即使超过保留天数，每个文件也至少留下最近这么多版
MIN_KEEP_VERSIONS = 10
CLEANUP_INTERVAL = 6 * 3600
DAY = 24 * 3600

# 变更动作（写进历史文件名与审计行）
ACTION_WRITE = "write"
ACTION_EDIT = "edit"
ACTION_DELETE = "delete"
ACTION_IMPORT = "import"
ACTION_RESTORE = "restore"
ACTION_FORGET = "forget"
ACTION_DISCARD_FORGOTTEN = "discard-forgotten"
ACTION_DELETE_FORGOTTEN = "delete-forgotten"
ACTION_DELETE_TRASH = "delete-trash"
ACTION_PURGE_FILE = "purge-file"
ACTION_PURGE_SCOPE = "purge-scope"
# 管理页改名：旧正文存为新名字下的一个版本，旧名的历史随后整份迁走
ACTION_RENAME = "rename"

# actor：谁改的
ACTOR_TIDY = "tidy"
ACTOR_WEBUI = "webui"
ACTOR_TRANSFER = "transfer"
ACTOR_SYSTEM = "system"

# 会在 .trash 里留下条目的动作；审计按 (文件, 时间戳) 找回条目属性时只认这些
TRASH_ACTIONS = (
    ACTION_DELETE,
    ACTION_DELETE_FORGOTTEN,
    ACTION_DISCARD_FORGOTTEN,
    ACTION_IMPORT,
)

_STAMP_FORMAT = "%Y%m%d-%H%M%S"
_VERSION_ID_RE = re.compile(r"^(\d{8}-\d{6}-\d{3})-([A-Za-z][A-Za-z0-9_-]*)$")
_TRASH_NAME_RE = re.compile(
    r"^(\d{8}-\d{6}-\d{3})__([A-Za-z0-9][A-Za-z0-9._-]{0,120}\.md)$"
)
# .trash-scopes 归档名：<层>-<键>-<时间>；键本身可能含连字符，按末尾时间戳切开
_ARCHIVE_NAME_RE = re.compile(r"^(.*)-(\d{8}-\d{6}-\d{3})$")


def _now() -> float:
    """当前时间；单独成函数，测试可以替换它造出不同时间的版本。"""
    return time.time()


def _stamp(ts: float) -> str:
    """时间戳文件名：YYYYmmdd-HHMMSS-fff（本地时间，毫秒）。"""
    millis = int(round(ts * 1000))
    head = time.strftime(_STAMP_FORMAT, time.localtime(millis / 1000))
    return f"{head}-{millis % 1000:03d}"


def _parse_stamp(text: str) -> float | None:
    """文件名里的时间戳；日期非法（手工改名、损坏副本）返回 None。"""
    try:
        base = time.mktime(time.strptime(text[:15], _STAMP_FORMAT))
        return base + int(text[16:19]) / 1000
    except (ValueError, OverflowError):
        return None


def content_sha1(content: str) -> str:
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


def _file_sha1(path: Path) -> str:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _write_atomic(path: Path, content: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(content, bytes):
        tmp.write_bytes(content)
    else:
        tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _rmdir_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def _remove_index_line(index_path: Path, file: str) -> bool:
    """删掉归档索引里指向 file 的行；文件不存在或没有这行返回 False。"""
    try:
        text = index_path.read_text("utf-8")
    except OSError:
        return False
    marker = f"]({file})"
    lines = text.splitlines()
    kept = [line for line in lines if marker not in line]
    if len(kept) == len(lines):
        return False
    _write_atomic(index_path, "\n".join(kept).strip("\n") + "\n" if kept else "")
    return True


def append_audit(path: Path, record: dict) -> None:
    """往 .audit.jsonl 追加一行；审计写失败不抛出。"""
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


@dataclass(frozen=True)
class HistoryVersion:
    """一个历史版本；管理页按时间、动作、actor、大小展示。"""

    file: str
    version: str
    action: str
    ts: float
    mtime: float
    size: int
    actor: str = ""
    source: str = ""


@dataclass(frozen=True)
class TrashEntry:
    """回收站里的一条；entry 是完整文件名，读取 / 恢复 / 删除都用它。"""

    entry: str
    file: str
    ts: float
    mtime: float
    size: int
    action: str = ""
    actor: str = ""
    source: str = ""


class HistoryLog:
    """一个作用域目录下的历史、回收站与审计（同步方法，调用方持锁）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        # 历史与回收站的写、删、清理共用一把可重入锁：清理线程不能与
        # save_version / move_to_trash 交错删掉刚建出来的目录
        self._lock = threading.RLock()

    # ------------------------------------------------------------ 路径

    @property
    def history_root(self) -> Path:
        return self.root / HISTORY_DIRNAME

    @property
    def trash_root(self) -> Path:
        return self.root / TRASH_DIRNAME

    @property
    def audit_path(self) -> Path:
        return self.root / AUDIT_FILENAME

    # ------------------------------------------------------------ 审计

    def audit_entries(self) -> list[dict]:
        """读全部审计行（坏行跳过）。"""
        path = self.audit_path
        if not path.is_file():
            return []
        try:
            lines = path.read_text("utf-8").splitlines()
        except OSError:
            return []
        result: list[dict] = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                result.append(row)
        return result

    def _audit_index(self) -> dict[tuple[str, str, str], dict]:
        index: dict[tuple[str, str, str], dict] = {}
        for row in self.audit_entries():
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            key = (
                str(row.get("file") or ""),
                str(row.get("action") or ""),
                _stamp(ts),
            )
            # 同一毫秒内的多次变更按写入顺序覆盖：版本对应最后一次审计
            index[key] = row
        return index

    def record(
        self,
        action: str,
        file: str,
        actor: str = "",
        source: str = "",
        before_sha1: str = "",
        after_sha1: str = "",
        ts: float | None = None,
        reason: str = "",
    ) -> None:
        """追加一行审计：ts、动作、文件、actor、来源、前后内容哈希与原因。"""
        ts = _now() if ts is None else ts
        record = {
            "ts": round(ts, 3),
            "action": action,
            "file": file,
            "actor": actor,
            "source": source,
            "before_sha1": before_sha1,
            "after_sha1": after_sha1,
        }
        if reason:
            record["reason"] = reason
        with self._lock:
            append_audit(self.audit_path, record)

    # ------------------------------------------------------------ 历史

    def save_version(
        self,
        file: str,
        content: str | bytes,
        action: str,
        actor: str = "",
        source: str = "",
        ts: float | None = None,
    ) -> HistoryVersion:
        """把旧内容存成一个历史版本；返回版本信息（审计由调用方在写完后记）。

        content 可以是 bytes：非 UTF-8 的在册文件被覆盖或恢复前，也按原始
        字节留一份副本，不因解码失败丢掉旧内容。
        """
        ts = _now() if ts is None else ts
        ts = round(ts * 1000) / 1000
        folder = self.history_root / file
        with self._lock:
            while True:
                version = f"{_stamp(ts)}-{action}"
                path = folder / f"{version}.md"
                if not path.exists():
                    break
                ts = round(ts + 0.001, 3)
            _write_atomic(path, content)
            try:
                st = path.stat()
            except OSError:
                st = None
        size = (
            len(content)
            if isinstance(content, bytes)
            else len(content.encode("utf-8"))
        )
        return HistoryVersion(
            file=file,
            version=version,
            action=action,
            ts=ts,
            mtime=st.st_mtime if st else ts,
            size=st.st_size if st else size,
            actor=actor,
            source=source,
        )

    def list_versions(self, file: str) -> list[HistoryVersion]:
        """某个文件的全部历史版本，新的在前；actor / source 从审计补齐。"""
        folder = self.history_root / file
        if not folder.is_dir():
            return []
        audit = self._audit_index()
        result: list[HistoryVersion] = []
        try:
            entries = list(folder.iterdir())
        except OSError:
            return []
        for path in entries:
            if not path.name.endswith(".md"):
                continue
            match = _VERSION_ID_RE.match(path.name[: -len(".md")])
            if match is None or not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            ts = _parse_stamp(match.group(1))
            if ts is None:
                # 名称像版本文件但日期非法：不参与列表，也不在清理时误删
                continue
            row = audit.get((file, match.group(2), match.group(1)), {})
            result.append(
                HistoryVersion(
                    file=file,
                    version=match.group(0),
                    action=match.group(2),
                    ts=ts,
                    mtime=st.st_mtime,
                    size=st.st_size,
                    actor=str(row.get("actor") or ""),
                    source=str(row.get("source") or ""),
                )
            )
        result.sort(key=lambda item: (-item.ts, item.version))
        return result

    @staticmethod
    def _version_id(version: str) -> str:
        version = (version or "").strip()
        if version.endswith(".md"):
            version = version[: -len(".md")]
        return version if _VERSION_ID_RE.match(version) else ""

    def read_version(self, file: str, version: str) -> str | None:
        version = self._version_id(version)
        if not version:
            return None
        path = self.history_root / file / f"{version}.md"
        try:
            return path.read_text("utf-8") if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            # 非 UTF-8 的历史副本（记录非文本文件时按字节留的）不能当文本查看
            return None

    def remove_versions(self, file: str) -> int:
        """彻底删除某个文件的全部历史版本，返回真正删掉的版本数。"""
        folder = self.history_root / file
        with self._lock:
            if not folder.is_dir():
                return 0
            count = self._count_versions(folder)
            try:
                shutil.rmtree(folder)
            except OSError:
                pass
            if not folder.exists():
                return count
            # rmtree 可能失败或只删了一部分：按残留重新算，绝不虚报
            return max(0, count - self._count_versions(folder))

    @staticmethod
    def _count_versions(folder: Path) -> int:
        try:
            return sum(
                1
                for path in folder.iterdir()
                if path.is_file() and path.name.endswith(".md")
            )
        except OSError:
            return 0

    def move_versions(self, old: str, new: str) -> int:
        """把旧文件名的全部历史版本迁到新文件名下，返回迁走的版本数。

        改名后旧名字不再单独出现：管理页只按新名字查看、恢复和「彻底删除
        （含历史）」。新名下已有同一时间戳的版本（删掉后重建再改名）时，
        把迁过来的版本时间往后挪到不冲突为止；日期非法的陌生文件不碰。
        审计按新文件名补一行（动作、actor、来源、前后哈希沿用原行），
        这样历史面板在新名字下仍能看到是谁改的。

        任何一个版本搬不动都整体回滚并抛 OSError：调用方据此保留旧文件，
        不会出现"部分历史留在旧名、旧文件却已删除"的静默成功。
        """
        if not old or not new or old == new:
            return 0
        source = self.history_root / old
        dest = self.history_root / new
        with self._lock:
            if not source.is_dir():
                return 0
            try:
                entries = list(source.iterdir())
            except OSError:
                return 0
            audit = self._audit_index()
            moved: list[tuple[Path, Path]] = []
            pending: list[tuple[float, str, dict]] = []
            try:
                for path in entries:
                    if not path.is_file() or not path.name.endswith(".md"):
                        continue
                    match = _VERSION_ID_RE.match(path.name[: -len(".md")])
                    if match is None:
                        continue
                    ts = _parse_stamp(match.group(1))
                    if ts is None:
                        continue
                    action = match.group(2)
                    target = dest / path.name
                    while target.exists():
                        ts = round(ts + 0.001, 3)
                        target = dest / f"{_stamp(ts)}-{action}.md"
                    dest.mkdir(parents=True, exist_ok=True)
                    os.replace(path, target)
                    moved.append((target, path))
                    row = audit.get((old, action, match.group(1)))
                    if row:
                        pending.append((ts, action, row))
            except OSError:
                # 把已经搬过去的版本放回原处，旧名字的历史保持完整
                for target, path in reversed(moved):
                    try:
                        os.replace(target, path)
                    except OSError:
                        continue
                _rmdir_if_empty(dest)
                raise
            for ts, action, row in pending:
                self.record(
                    action,
                    new,
                    str(row.get("actor") or ""),
                    str(row.get("source") or ""),
                    str(row.get("before_sha1") or ""),
                    str(row.get("after_sha1") or ""),
                    ts=ts,
                )
            _rmdir_if_empty(source)
            return len(moved)

    def move_trash(self, old: str, new: str) -> int:
        """把旧文件名的回收站条目迁到新文件名下，返回迁走的条数。

        之后对新名字「彻底删除（含历史）」会连同这些条目一起清掉，旧名字
        不再残留可读的正文。审计按新文件名补一行，actor / 来源沿用原行；
        搬不动时整体回滚并抛 OSError，由调用方保留旧文件。
        """
        if not old or not new or old == new:
            return 0
        with self._lock:
            if not self.trash_root.is_dir():
                return 0
            try:
                entries = list(self.trash_root.iterdir())
            except OSError:
                return 0
            audit: dict[tuple[str, str], dict] = {}
            for row in self.audit_entries():
                if str(row.get("action") or "") not in TRASH_ACTIONS:
                    continue
                try:
                    ts = float(row.get("ts") or 0)
                except (TypeError, ValueError):
                    continue
                audit.setdefault((str(row.get("file") or ""), _stamp(ts)), row)
            moved: list[tuple[Path, Path]] = []
            pending: list[tuple[float, dict]] = []
            try:
                for path in entries:
                    match = _TRASH_NAME_RE.match(path.name)
                    if (
                        match is None
                        or match.group(2) != old
                        or not path.is_file()
                    ):
                        continue
                    ts = _parse_stamp(match.group(1))
                    if ts is None:
                        continue
                    target = self.trash_root / f"{_stamp(ts)}__{new}"
                    while target.exists():
                        ts = round(ts + 0.001, 3)
                        target = self.trash_root / f"{_stamp(ts)}__{new}"
                    os.replace(path, target)
                    moved.append((target, path))
                    row = audit.get((old, match.group(1)))
                    if row:
                        pending.append((ts, row))
            except OSError:
                for target, path in reversed(moved):
                    try:
                        os.replace(target, path)
                    except OSError:
                        continue
                raise
            for ts, row in pending:
                self.record(
                    str(row.get("action") or ""),
                    new,
                    str(row.get("actor") or ""),
                    str(row.get("source") or ""),
                    str(row.get("before_sha1") or ""),
                    str(row.get("after_sha1") or ""),
                    ts=ts,
                )
            return len(moved)

    # ------------------------------------------------------------ 回收站

    def list_trash(self) -> list[TrashEntry]:
        """回收站条目，新的在前；actor / source 从审计补齐。"""
        if not self.trash_root.is_dir():
            return []
        audit: dict[tuple[str, str], dict] = {}
        for row in self.audit_entries():
            if str(row.get("action") or "") not in TRASH_ACTIONS:
                continue
            try:
                ts = float(row.get("ts") or 0)
            except (TypeError, ValueError):
                continue
            audit.setdefault((str(row.get("file") or ""), _stamp(ts)), row)
        result: list[TrashEntry] = []
        try:
            entries = list(self.trash_root.iterdir())
        except OSError:
            return []
        for path in entries:
            match = _TRASH_NAME_RE.match(path.name)
            if match is None or not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            ts = _parse_stamp(match.group(1))
            if ts is None:
                # 日期非法的陌生文件：不展示，也不在清理时误删
                continue
            row = audit.get((match.group(2), match.group(1)), {})
            result.append(
                TrashEntry(
                    entry=path.name,
                    file=match.group(2),
                    ts=ts,
                    mtime=st.st_mtime,
                    size=st.st_size,
                    action=str(row.get("action") or ""),
                    actor=str(row.get("actor") or ""),
                    source=str(row.get("source") or ""),
                )
            )
        result.sort(key=lambda item: item.ts, reverse=True)
        return result

    def count_trash(self) -> int:
        """回收站条目数；只扫 .trash 目录，不读审计（列表才需要 actor/source）。"""
        if not self.trash_root.is_dir():
            return 0
        try:
            entries = list(self.trash_root.iterdir())
        except OSError:
            return 0
        count = 0
        for path in entries:
            match = _TRASH_NAME_RE.match(path.name)
            if match is None or not path.is_file():
                continue
            if _parse_stamp(match.group(1)) is not None:
                count += 1
        return count

    @staticmethod
    def _trash_name(entry: str) -> str:
        entry = (entry or "").strip()
        return entry if _TRASH_NAME_RE.match(entry) else ""

    def read_trash(self, entry: str) -> str | None:
        entry = self._trash_name(entry)
        if not entry:
            return None
        path = self.trash_root / entry
        try:
            return path.read_text("utf-8") if path.is_file() else None
        except (OSError, UnicodeDecodeError):
            # 非 UTF-8 条目按字节保留在回收站里：不抛异常，管理页可以删除；
            # 恢复时由调用方给出明确说明，不拿替换字符改写原文件
            return None

    def read_trash_bytes(self, entry: str) -> bytes | None:
        """按原始字节读回收站条目（非 UTF-8 文件的查看/恢复判断用）。"""
        entry = self._trash_name(entry)
        if not entry:
            return None
        path = self.trash_root / entry
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError:
            return None

    def trash_info(self, entry: str) -> TrashEntry | None:
        entry = self._trash_name(entry)
        if not entry:
            return None
        return next((item for item in self.list_trash() if item.entry == entry), None)

    def move_to_trash(
        self,
        path: Path,
        file: str,
        action: str,
        actor: str = "",
        source: str = "",
        reason: str = "",
    ) -> TrashEntry:
        """把文件移进回收站并记审计；返回回收站条目。"""
        before_sha1 = _file_sha1(path)
        ts = round(_now() * 1000) / 1000
        with self._lock:
            while True:
                entry = f"{_stamp(ts)}__{file}"
                dest = self.trash_root / entry
                if not dest.exists():
                    break
                ts = round(ts + 0.001, 3)
            self.trash_root.mkdir(parents=True, exist_ok=True)
            os.replace(path, dest)
            st = dest.stat()
            self.record(
                action, file, actor, source, before_sha1, "", ts=ts, reason=reason
            )
        return TrashEntry(
            entry=entry,
            file=file,
            ts=ts,
            mtime=st.st_mtime,
            size=st.st_size,
            action=action,
            actor=actor,
            source=source,
        )

    def remove_entry(self, entry: str) -> bool:
        """从回收站删掉一条（恢复成功后或彻底删除时用）；不记审计。"""
        entry = self._trash_name(entry)
        if not entry:
            return False
        path = self.trash_root / entry
        with self._lock:
            try:
                path.unlink()
            except OSError:
                return False
            _rmdir_if_empty(self.trash_root)
        return True

    def delete_entry(self, entry: str, actor: str = "") -> bool:
        """彻底删除一条回收站条目，记审计。"""
        entry = self._trash_name(entry)
        if not entry:
            return False
        path = self.trash_root / entry
        before_sha1 = _file_sha1(path)
        with self._lock:
            try:
                path.unlink()
            except OSError:
                return False
            match = _TRASH_NAME_RE.match(entry)
            self.record(
                ACTION_DELETE_TRASH,
                match.group(2) if match else entry,
                actor,
                "",
                before_sha1,
                "",
            )
            _rmdir_if_empty(self.trash_root)
        return True

    def remove_trash_for(self, file: str) -> int:
        """彻底删除某个文件的全部回收站条目，返回真正删掉的条数。"""
        with self._lock:
            if not self.trash_root.is_dir():
                return 0
            count = 0
            try:
                entries = list(self.trash_root.iterdir())
            except OSError:
                return 0
            for path in entries:
                match = _TRASH_NAME_RE.match(path.name)
                if match is None or match.group(2) != file or not path.is_file():
                    continue
                try:
                    path.unlink()
                    count += 1
                except OSError:
                    continue
            _rmdir_if_empty(self.trash_root)
            return count

    def remove_from_archives(
        self, scopes_dir: Path, scope_label: str, file: str
    ) -> int:
        """从本作用域的 .trash-scopes 归档里删掉这个文件的全部副本。

        只匹配 `<scope_label>-<时间>` 的归档，不会碰别的会话或别的平台里
        同名的文件；用于管理员要求彻底遗忘时连同此前 purge 的备份一起清掉。
        """
        if not scopes_dir.is_dir():
            return 0
        try:
            archives = []
            for path in scopes_dir.iterdir():
                if not path.is_dir():
                    continue
                match = _ARCHIVE_NAME_RE.match(path.name)
                if match is not None and match.group(1) == scope_label:
                    archives.append(path)
        except OSError:
            return 0
        removed = 0
        for archive in archives:
            for path in (archive / file, archive / FORGOTTEN_DIRNAME / file):
                try:
                    if path.is_file():
                        path.unlink()
                        removed += 1
                except OSError:
                    continue
            _rmdir_if_empty(archive / FORGOTTEN_DIRNAME)
            # 索引行里的描述也算内容，不能留在隐藏归档里
            _remove_index_line(archive / INDEX_FILENAME, file)
            other = HistoryLog(archive)
            removed += other.remove_versions(file)
            removed += other.remove_trash_for(file)
            _rmdir_if_empty(archive / HISTORY_DIRNAME)
        return removed

    # ------------------------------------------------------------ 保留期清理

    def cleanup(
        self, history_keep_days: int, trash_keep_days: int, now: float | None = None
    ) -> dict[str, int]:
        """按保留期清理历史版本与回收站；返回 (删掉的版本数, 条目数)。

        历史版本每个文件至少保留最近 MIN_KEEP_VERSIONS 版，即使它们已经
        超过 history_keep_days。0 表示永久保留。
        """
        now = _now() if now is None else now
        removed_versions = 0
        removed_trash = 0
        with self._lock:
            if history_keep_days > 0 and self.history_root.is_dir():
                cutoff = now - history_keep_days * DAY
                try:
                    folders = list(self.history_root.iterdir())
                except OSError:
                    folders = []
                for folder in folders:
                    if not folder.is_dir():
                        continue
                    versions: list[tuple[float, Path]] = []
                    try:
                        for path in folder.iterdir():
                            if not path.name.endswith(".md"):
                                continue
                            match = _VERSION_ID_RE.match(path.name[: -len(".md")])
                            if match is None or not path.is_file():
                                continue
                            ts = _parse_stamp(match.group(1))
                            if ts is not None:
                                versions.append((ts, path))
                    except OSError:
                        continue
                    versions.sort(key=lambda item: item[0], reverse=True)
                    for ts, path in versions[MIN_KEEP_VERSIONS:]:
                        if ts >= cutoff:
                            continue
                        try:
                            path.unlink()
                            removed_versions += 1
                        except OSError:
                            continue
                    _rmdir_if_empty(folder)
                _rmdir_if_empty(self.history_root)
            if trash_keep_days > 0 and self.trash_root.is_dir():
                cutoff = now - trash_keep_days * DAY
                try:
                    entries = list(self.trash_root.iterdir())
                except OSError:
                    entries = []
                for path in entries:
                    match = _TRASH_NAME_RE.match(path.name)
                    if match is None or not path.is_file():
                        continue
                    ts = _parse_stamp(match.group(1))
                    if ts is None or ts >= cutoff:
                        continue
                    try:
                        path.unlink()
                        removed_trash += 1
                    except OSError:
                        continue
                _rmdir_if_empty(self.trash_root)
        return {"versions": removed_versions, "trash": removed_trash}


def move_scope(
    root: Path, scopes_dir: Path, label: str, actor: str = ""
) -> Path:
    """整个作用域目录挪进 .trash-scopes/，而不是 rmtree。"""
    scopes_dir.mkdir(parents=True, exist_ok=True)
    ts = round(_now() * 1000) / 1000
    while True:
        dest = scopes_dir / f"{label}-{_stamp(ts)}"
        if not dest.exists():
            break
        ts = round(ts + 0.001, 3)
    shutil.move(str(root), str(dest))
    append_audit(
        dest / AUDIT_FILENAME,
        {
            "ts": round(ts, 3),
            "action": ACTION_PURGE_SCOPE,
            "file": "*",
            "actor": actor,
            "source": "",
            "before_sha1": "",
            "after_sha1": "",
        },
    )
    return dest


def cleanup_scope_archives(
    scopes_dir: Path, keep_days: int, now: float | None = None
) -> int:
    """按回收站保留期清理 .trash-scopes 里的作用域归档，返回删掉的数量。

    purge（清除会话）把整个目录移到归档，里面是在册文件、历史
    与回收站；这些字节同样受 trash_keep_days 约束。0 表示永久保留。
    名称日期非法或不是归档的目录一律不碰。
    """
    try:
        keep_days = max(0, int(keep_days))
    except (TypeError, ValueError):
        return 0
    if keep_days <= 0 or not scopes_dir.is_dir():
        return 0
    now = _now() if now is None else now
    cutoff = now - keep_days * DAY
    removed = 0
    try:
        entries = list(scopes_dir.iterdir())
    except OSError:
        return 0
    for path in entries:
        if not path.is_dir():
            continue
        match = _ARCHIVE_NAME_RE.match(path.name)
        if match is None:
            continue
        ts = _parse_stamp(match.group(2))
        if ts is None or ts >= cutoff:
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            removed += 1
    _rmdir_if_empty(scopes_dir)
    return removed

