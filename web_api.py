"""WebUI 记忆管理页的后端接口。

页面（pages/memory）经 AstrBot 的插件页面桥接调用这里注册的接口，
管理员可以查看并编辑三层记忆（留言板 / 小本本 / 悄悄话）、发布带署名的
留言板告示、手动整理记忆、查看只读的上下文统计，以及导出导入备份、
在作用域之间或平台改名后迁移记忆。

接口约定：
    - 只用 GET / POST；成功返回 {"status": "ok", "message": "", "data": {...}}，
      失败返回 {"status": "error", "message": "原因", "data": {}}，HTTP 状态码
      一律 200（面板把 status=error 转成页面里的报错文案）。
    - 作用域用 scope=board|notebook|whisper 加 key 定位：board 无键，
      notebook 的键是平台目录名，whisper 的键是会话目录名。目录名先校验
      格式再拼路径，不接受任何路径片段。
    - 管理页是管理员操作，所以可以编辑任意会话的悄悄话、直接写留言板；
      聊天工具与召回没有这些通道（PLAN 3.8.3）。
    - 改动记忆后作废相应会话的索引快照，下一轮对话就能看到。

AstrBot v4.24.1 起支持插件页面；更早的版本没有 register_web_api，
这里静默跳过注册，插件其余功能照常。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from astrbot.api import logger

from .core import (
    consolidate,
    context_stats,
    history,
    insights,
    memstore,
    registry,
    tidy,
    transfer,
)

if TYPE_CHECKING:
    from .main import MemoryPodPlugin

try:  # v4.26 起的插件 Web API 请求对象
    from astrbot.api.web import request as _plugin_request
except ImportError:  # 更早的版本走 Quart
    _plugin_request = None

PLUGIN_NAME = "astrbot_plugin_memory_pod"
ROUTE_PREFIX = f"/{PLUGIN_NAME}/"
LOG_PREFIX = "[MemoryPod] "

SCOPE_BOARD = memstore.SCOPE_BOARD
SCOPE_NOTEBOOK = memstore.SCOPE_NOTEBOOK
SCOPE_WHISPER = memstore.SCOPE_WHISPER
SCOPES = memstore.SCOPES

MAX_REMARK = 64
MAX_QUERY = 200
MAX_BOARD_TITLE = 120
MAX_BOARD_BODY = 8000
# 导出、导入的暂存：先生成再下载、先预演再执行，期间内容放在内存里
STAGE_TTL = 30 * 60
MAX_STAGED = 4


class ApiError(Exception):
    """可以直接展示给管理员的错误。"""


def _ok(data: Any = None, message: str = "") -> dict:
    data = {} if data is None else data
    # 面板只把 data 转交给页面，提示文案（如索引快满）随 data 一起带过去
    if message and isinstance(data, dict):
        data.setdefault("message", message)
    return {"status": "ok", "message": message, "data": data}


def _error(message: str) -> dict:
    return {"status": "error", "message": message, "data": {}}


def _query(name: str, default: str = "") -> str:
    if _plugin_request is not None:
        value = _plugin_request.query.get(name, default)
    else:
        from quart import request

        value = request.args.get(name, default)
    return str(value if value is not None else default)


async def _body() -> dict:
    if _plugin_request is not None:
        data = await _plugin_request.json(default={})
    else:
        from quart import request

        data = await request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _text(raw: Any, limit: int | None = None) -> str:
    value = str(raw if raw is not None else "")
    return value[:limit] if limit else value


def _header_dict(header: memstore.MemoryHeader) -> dict:
    return {
        "file": header.file,
        "name": header.name,
        "description": header.description,
        "type": header.type,
        "mtime": header.mtime,
        "updated": header.updated,
        "created": header.created,
        "source": header.source,
        "signature": header.signature,
        "size": header.size,
        "links": header.links,
        "lastUsed": header.last_used,
        "uses": header.uses,
        "forgottenAt": header.forgotten_at,
    }


def _file_flags(
    store: memstore.ScopeStore, headers: list[memstore.MemoryHeader]
) -> dict[str, dict]:
    """文件列表里的提醒标记：悬空链接与过长文件（同步，放进线程执行）。"""
    available = {header.file for header in headers} | set(store.forgotten_files())
    flags: dict[str, dict] = {}
    for header in headers:
        content = store._read_text_lenient(header.file)
        if content is None:
            continue
        doc = memstore.parse_memory(content)
        missing = insights.dangling_links(
            memstore.extract_links(doc.body), available
        )
        reasons = insights.long_file_reasons(
            header.file, doc.type, doc.body, len(content.encode("utf-8"))
        )
        if missing or reasons:
            flags[header.file] = {
                "dangling": missing,
                "long": reasons,
            }
    return flags


def _history_dict(version: history.HistoryVersion) -> dict:
    return {
        "file": version.file,
        "version": version.version,
        "action": version.action,
        "ts": version.ts,
        "mtime": version.mtime,
        "size": version.size,
        "actor": version.actor,
        "source": version.source,
    }


def _trash_dict(entry: history.TrashEntry) -> dict:
    return {
        "entry": entry.entry,
        "file": entry.file,
        "ts": entry.ts,
        "mtime": entry.mtime,
        "size": entry.size,
        "action": entry.action,
        "actor": entry.actor,
        "source": entry.source,
    }


def _record_dict(key: str, record: registry.SessionRecord | None) -> dict:
    if record is None:
        platform, kind, target = registry.guess_from_key(key)
        record = registry.SessionRecord(
            key=key, platform=platform, kind=kind, target=target
        )
    return {
        "key": key,
        "umo": record.umo,
        "platform": record.platform,
        "kind": record.kind,
        "target": record.target,
        "name": record.name,
        "remark": record.remark,
        "firstSeen": record.first_seen,
        "lastActive": record.last_active,
    }


def _context_dict(stats: context_stats.ContextStats) -> dict:
    return {
        "parts": dict(stats.parts or {}),
        "estimate": stats.estimate,
        "actual": stats.actual,
        "cacheHit": stats.cache_hit,
        "requestAt": stats.request_at,
    }


def _scan_scope(store: memstore.ScopeStore) -> tuple[int, float]:
    """(记忆文件数, 最近写入时间)；同步，放进线程执行。"""
    count = 0
    latest = 0.0
    if not store.root.is_dir():
        return 0, 0.0
    for name in store.list_files():
        count += 1
        mtime = store.mtime(name) or 0.0
        latest = max(latest, mtime)
    return count, latest


class MemoryDashboard:
    """把 WebUI 接口挂到 AstrBot 上，并实现每个接口。"""

    def __init__(self, plugin: "MemoryPodPlugin"):
        self.plugin = plugin
        self.transfer = transfer.MemoryTransfer(plugin._store, plugin._registry)
        # 令牌 -> (过期时间, 内容)：生成好的备份、上传待导入的备份
        self._staged: dict[str, tuple[float, Any]] = {}

    # ------------------------------------------------------------ 注册

    def _routes(self) -> list[tuple[str, str, Callable[[], Awaitable[dict]], str]]:
        return [
            ("overview", "GET", self.overview, "记忆管理：三层记忆总览"),
            ("scope", "GET", self.scope_detail, "记忆管理：作用域详情"),
            ("file", "GET", self.file_detail, "记忆管理：读取记忆文件"),
            ("search", "GET", self.search, "记忆管理：全文搜索"),
            ("whisper/context", "GET", self.whisper_context, "记忆管理：上下文统计"),
            ("forgotten/file", "GET", self.forgotten_file, "记忆管理：读取已遗忘的文件"),
            ("file/save", "POST", self.file_save, "记忆管理：保存记忆文件"),
            ("file/delete", "POST", self.file_delete, "记忆管理：删除记忆文件"),
            ("file/forget", "POST", self.file_forget, "记忆管理：遗忘记忆文件"),
            ("forgotten/restore", "POST", self.forgotten_restore, "记忆管理：恢复已遗忘的文件"),
            ("forgotten/delete", "POST", self.forgotten_delete, "记忆管理：删除已遗忘的文件（移入回收站）"),
            ("history/versions", "GET", self.history_versions, "记忆管理：文件的历史版本"),
            ("history/file", "GET", self.history_file, "记忆管理：读取某一历史版本"),
            ("history/restore", "POST", self.history_restore, "记忆管理：恢复历史版本"),
            ("trash", "GET", self.trash_entries, "记忆管理：回收站列表"),
            ("trash/file", "GET", self.trash_file, "记忆管理：读取回收站条目"),
            ("trash/restore", "POST", self.trash_restore, "记忆管理：从回收站恢复"),
            ("trash/delete", "POST", self.trash_delete, "记忆管理：彻底删除回收站条目"),
            ("file/purge", "POST", self.file_purge, "记忆管理：彻底删除文件及其历史"),
            ("index/save", "POST", self.index_save, "记忆管理：保存索引"),
            ("index/rebuild", "POST", self.index_rebuild, "记忆管理：重建索引"),
            ("whisper/remark", "POST", self.whisper_remark, "记忆管理：会话备注"),
            ("whisper/purge", "POST", self.whisper_purge, "记忆管理：清除会话数据"),
            ("board/publish", "POST", self.board_publish, "记忆管理：发布留言板告示"),
            (
                "consolidate/preview",
                "POST",
                self.consolidate_preview,
                "记忆管理：生成整理方案",
            ),
            (
                "consolidate/apply",
                "POST",
                self.consolidate_apply,
                "记忆管理：应用整理方案",
            ),
            ("tidy/runs", "GET", self.tidy_runs, "记忆管理：整理记录"),
            ("tidy/undo", "POST", self.tidy_undo, "记忆管理：撤销整理"),
            ("migrate/info", "GET", self.migrate_info, "记忆管理：迁移可选项"),
            ("export/prepare", "POST", self.export_prepare, "记忆管理：生成备份"),
            ("export/file", "GET", self.export_file, "记忆管理：下载备份"),
            ("import/preview", "POST", self.import_preview, "记忆管理：预览导入"),
            ("import/apply", "POST", self.import_apply, "记忆管理：导入备份"),
            ("migrate/scope", "POST", self.migrate_scope, "记忆管理：迁移一处记忆"),
            ("migrate/platform", "POST", self.migrate_platform, "记忆管理：平台改名迁移"),
        ]

    def register(self) -> None:
        context = self.plugin.context
        register = getattr(context, "register_web_api", None)
        if not callable(register):
            logger.info(
                LOG_PREFIX
                + "当前 AstrBot 版本不支持插件 Web 接口，记忆管理页不可用"
                "（需要 v4.24.1 及以上），其余功能不受影响"
            )
            return
        # 插件重载时旧实例的路由可能还在，先清掉再挂新的
        self._purge_routes()
        for path, method, handler, desc in self._routes():
            register(ROUTE_PREFIX + path, self._wrap(handler), [method], desc)

    def unregister(self) -> None:
        self._purge_routes()

    def _purge_routes(self) -> None:
        apis = getattr(self.plugin.context, "registered_web_apis", None)
        if isinstance(apis, list):
            apis[:] = [
                api
                for api in apis
                if not str(api[0] if api else "").startswith(ROUTE_PREFIX)
            ]

    @staticmethod
    def _wrap(handler: Callable[[], Awaitable[dict]]):
        async def view(*_args, **_kwargs):
            try:
                return await handler()
            except (ApiError, transfer.TransferError, tidy.TidyError) as exc:
                return _error(str(exc))
            except Exception:  # noqa: BLE001 - 接口异常不能打断面板
                logger.error(LOG_PREFIX + "记忆管理接口出错", exc_info=True)
                return _error("服务器内部错误，详情见 AstrBot 日志")

        view.__name__ = f"memory_pod_{handler.__name__}"
        return view

    # ------------------------------------------------------------ 定位

    def _scope_key(self, raw: Any, scope: str) -> str:
        key = _text(raw).strip()
        if not memstore.is_scope_key(key):
            raise ApiError("平台标识不合法" if scope == SCOPE_NOTEBOOK else "会话标识不合法")
        return key

    def _scope(self, scope: Any, key: Any) -> tuple[str, str, memstore.ScopeStore]:
        scope = _text(scope).strip().lower()
        if scope == SCOPE_BOARD:
            return SCOPE_BOARD, "", self.plugin._store.board_scope()
        if scope == SCOPE_NOTEBOOK:
            notebook_key = self._scope_key(key, scope)
            store = self.plugin._store.notebook_scope_by_key(notebook_key)
            if store is None:
                raise ApiError("平台标识不合法")
            return SCOPE_NOTEBOOK, notebook_key, store
        if scope == SCOPE_WHISPER:
            whisper_key = self._scope_key(key, scope)
            store = self.plugin._store.whisper_scope_by_key(whisper_key)
            if store is None:
                raise ApiError("会话标识不合法")
            return SCOPE_WHISPER, whisper_key, store
        raise ApiError("作用域只能是 board、notebook 或 whisper")

    def _scope_from_query(self):
        return self._scope(_query("scope"), _query("key"))

    def _scope_from_body(self, body: dict):
        return self._scope(body.get("scope"), body.get("key"))

    @staticmethod
    def _file_name(raw: Any) -> str:
        name = _text(raw).strip()
        if name and not name.endswith(memstore.MEMORY_FILE_SUFFIX):
            name += memstore.MEMORY_FILE_SUFFIX
        # 不分大小写：Windows / macOS 上 memory.md 和 MEMORY.md 是同一个文件
        if name.lower() == memstore.INDEX_FILENAME.lower():
            raise ApiError("MEMORY.md 请在「索引」页编辑")
        if not name or not memstore.ScopeStore.is_valid_name(name):
            raise ApiError("文件名不合法：" + memstore.ScopeStore.path_rules())
        return name

    def _changed(self, scope: str, key: str | None) -> None:
        self.plugin.invalidate_scope(scope, key)

    def _scope_label(self, scope: str, key: str | None) -> str:
        record = self.plugin._registry.get(key or "") if scope == SCOPE_WHISPER else None
        return tidy.scope_label(scope, key, record)

    def _stage(self, payload: Any) -> str:
        now = time.time()
        for token, (expires, _) in list(self._staged.items()):
            if expires < now:
                del self._staged[token]
        while len(self._staged) >= MAX_STAGED:
            del self._staged[min(self._staged, key=lambda t: self._staged[t][0])]
        token = secrets.token_urlsafe(16)
        self._staged[token] = (now + STAGE_TTL, payload)
        return token

    def _staged_payload(self, raw: Any) -> Any:
        found = self._staged.get(_text(raw).strip())
        if found is None or found[0] < time.time():
            raise ApiError("内容已过期，请重新选择备份文件或重新生成")
        return found[1]

    @staticmethod
    def _tidy_info(meta: tidy.TidyMeta) -> dict:
        return {
            "lastRun": meta.last_run,
            "lastAttempt": meta.last_attempt,
            "outcome": meta.outcome,
            "message": meta.message,
        }

    # ------------------------------------------------------------ 查询

    async def overview(self) -> dict:
        plugin = self.plugin
        store = plugin._store
        records = plugin._registry.all()
        whisper_keys = sorted(set(store.whisper_keys()) | set(records))
        notebook_keys = store.notebook_keys()
        board_scope = store.board_scope()
        notebook_scopes = {
            key: store.notebook_scope_by_key(key) for key in notebook_keys
        }
        whisper_scopes = {
            key: store.whisper_scope_by_key(key)
            for key in whisper_keys
            if store.whisper_scope_by_key(key) is not None
        }

        def collect() -> tuple[int, float, list[dict], list[dict]]:
            board_count, board_latest = _scan_scope(board_scope)
            notebooks = []
            for key, scope in notebook_scopes.items():
                count, latest = _scan_scope(scope) if scope else (0, 0.0)
                notebooks.append(
                    {"key": key, "platform": key, "fileCount": count, "updatedAt": latest}
                )
            whispers = []
            for key, scope in whisper_scopes.items():
                count, latest = _scan_scope(scope) if scope else (0, 0.0)
                item = _record_dict(key, records.get(key))
                item.update(fileCount=count, updatedAt=latest)
                whispers.append(item)
            return board_count, board_latest, notebooks, whispers

        board_count, board_latest, notebooks, whispers = await asyncio.to_thread(
            collect
        )
        return _ok(
            {
                "memoryEnabled": plugin._bool("enable_memory", True),
                "board": {"fileCount": board_count, "updatedAt": board_latest},
                "notebooks": notebooks,
                "whispers": whispers,
                "context": await plugin.context_status(None),
            }
        )

    async def scope_detail(self) -> dict:
        scope, key, store = self._scope_from_query()

        def collect() -> dict:
            headers = store.headers()
            report = store.index_report()
            index_path = store.root / memstore.INDEX_FILENAME
            index_text = (
                index_path.read_text("utf-8") if index_path.is_file() else ""
            )
            flags = _file_flags(store, headers)
            files = []
            for header in headers:
                item = _header_dict(header)
                item.update(flags.get(header.file, {}))
                files.append(item)
            return {
                "files": files,
                "forgotten": [_header_dict(h) for h in store.forgotten_headers()],
                # 回收站条目只在打开标签时按需拉取，这里只给数量
                "trashCount": store.trash_count(),
                "index": {
                    "exists": report.exists,
                    "content": index_text,
                    "lines": report.lines,
                    "size": report.size,
                    "truncated": report.truncated,
                    "missing": report.missing,
                    "dangling": report.dangling,
                    "maxLines": memstore.INDEX_MAX_LINES,
                    "maxBytes": memstore.INDEX_MAX_BYTES,
                },
            }

        data = await asyncio.to_thread(collect)
        meta = await asyncio.to_thread(
            self.plugin._tidy.meta, tidy.scope_id(scope, key)
        )
        data["scope"] = scope
        data["key"] = key
        data["tidy"] = self._tidy_info(meta)
        data["forgettingEnabled"] = self.plugin._forgetting_on()
        data["forgetAfterDays"] = self.plugin._forget_after_days()
        if scope == SCOPE_NOTEBOOK:
            data["notebook"] = {"platform": key}
        elif scope == SCOPE_WHISPER:
            data["whisper"] = _record_dict(key, self.plugin._registry.get(key))
        return _ok(data)

    async def file_detail(self) -> dict:
        _, _, store = self._scope_from_query()
        name = self._file_name(_query("file"))
        content = await store.read(name)
        if content is None:
            raise ApiError(f"文件不存在：{name}")
        doc = memstore.parse_memory(content)
        used = await asyncio.to_thread(store.usage_of, name)
        return _ok(
            {
                "file": name,
                "content": content,
                "mtime": store.mtime(name) or 0.0,
                "lastUsed": used.last_used,
                "uses": used.uses,
                "name": doc.name,
                "description": doc.description,
                "type": doc.type,
                "body": doc.body,
                "created": doc.created,
                "updated": doc.updated,
                "source": doc.source,
                "signature": doc.signature,
                "hasFrontmatter": doc.has_frontmatter,
                "links": memstore.extract_links(content),
            }
        )

    async def search(self) -> dict:
        _, _, store = self._scope_from_query()
        query = _query("q").strip()[:MAX_QUERY]
        if not query:
            return _ok({"results": []})
        results = []
        for line in await store.search(query):
            file, _, excerpt = line.partition(": ")
            results.append({"file": file, "excerpt": excerpt})
        return _ok({"results": results})

    async def _whisper_stats(
        self, key: str
    ) -> tuple[context_stats.ContextStats, bool]:
        """(上下文统计, 是否正被插件持有)；持有中的以内存为准。"""
        found = self.plugin.runtime_for_key(key)
        if found is not None:
            return found[1].stats, True
        stats = await self.plugin.load_stats_for_key(key)
        return stats or context_stats.ContextStats(), False

    async def whisper_context(self) -> dict:
        """只读的上下文面板：最近一次请求的构成、用量上报与 AstrBot 摘要。"""
        key = self._scope_key(_query("key"), SCOPE_WHISPER)
        stats, live = await self._whisper_stats(key)
        record = self.plugin._registry.get(key)
        umo = record.umo if record is not None else registry.umo_from_key(key)
        # 目录名不可逆：还原出的 UMO 必须再能转回同一个目录名，否则
        # 读到的是别的对话的摘要
        if umo and memstore.safe_key(umo) != key:
            umo = ""
        summary = await self.plugin.astrbot_summary(umo) if umo else ""
        data = _context_dict(stats)
        data["live"] = live
        data["now"] = time.time()
        data["summary"] = summary
        data["hasSummary"] = bool(summary)
        data["context"] = await self.plugin.context_status(umo or None)
        return _ok(data)

    # ------------------------------------------------------------ 记忆编辑

    async def file_save(self) -> dict:
        """保存记忆文件。

        mode=fields：按表单字段（名称取自文件名、类型、描述、正文）生成标准格式；
        mode=raw：原样保存管理员编辑的完整文本。
        oldFile 与 file 不同时视为改名：先写新文件，再把旧名的历史版本迁到
        新名下并删除旧文件（不给旧名留回收站条目）。
        baseMtime 是打开文件时的写入时间，期间文件被改过（比如模型刚写过）
        就拒绝保存，避免覆盖。
        """
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        old_raw = _text(body.get("oldFile")).strip()
        old = self._file_name(old_raw) if old_raw else ""
        renaming = bool(old) and old != name

        current_content = await store.read(old or name) if old or store.exists(name) else None
        signature = " ".join(_text(body.get("signature")).split())
        if not signature and current_content:
            signature = memstore.parse_memory(current_content).signature

        if _text(body.get("mode")).strip().lower() == "raw":
            content = _text(body.get("content"))
        else:
            mem_type = _text(body.get("type")).strip().lower()
            if mem_type not in memstore.MEMORY_TYPES:
                raise ApiError(
                    "类型只能是 user、feedback、project、reference、journal 之一"
                )
            description = " ".join(_text(body.get("description")).split())
            if not description:
                raise ApiError("请填写描述：它会成为索引里的那一行")
            content = memstore.compose_memory(
                name[: -len(memstore.MEMORY_FILE_SUFFIX)],
                description,
                mem_type,
                _text(body.get("body")).strip("\n"),
                signature=signature,
            )
        if not content.strip():
            raise ApiError("内容不能为空")
        # 留言板每条告示都必须带署名（PLAN 3.8.3）
        if scope == SCOPE_BOARD:
            if not signature:
                raise ApiError("留言板告示必须填写署名")
            signature = signature[: memstore.MAX_SIGNATURE_CHARS]
            if _text(body.get("mode")).strip().lower() == "raw":
                # 原文模式由页面直接给出 frontmatter；没有署名就补进去，
                # 补不进去（没有 metadata 块）就拒绝，避免绕过"每条告示带署名"。
                if not memstore.parse_memory(content).signature:
                    content = content.replace(
                        "metadata:\n",
                        f"metadata:\n  signature: {signature}\n",
                        1,
                    )
                if not memstore.parse_memory(content).signature:
                    raise ApiError("留言板原文缺少 metadata.signature；请切换表单模式填写署名")

        if (not old or renaming) and store.exists(name):
            raise ApiError(f"已存在同名文件 {name}，请换一个文件名")
        if old:
            if not store.exists(old):
                raise ApiError(f"文件 {old} 已不存在，可能刚被删除，请刷新")
            base = body.get("baseMtime")
            current = store.mtime(old) or 0.0
            if isinstance(base, (int, float)) and abs(current - float(base)) > 1e-3:
                raise ApiError("这个文件在你编辑期间被修改过（可能是模型刚写入），请刷新后再改")

        report = await store.write(name, content, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        message = report.message
        if renaming:
            renamed = await store.finalize_rename(
                old, name, actor=history.ACTOR_WEBUI
            )
            if not renamed.ok:
                # 新文件是这次刚写的；改名没完成就撤回，保证旧文件不动、
                # 用户刷新后还能用原来的名字重试
                rolled = await store.delete(name, actor=history.ACTOR_WEBUI)
                detail = "；已撤回刚写入的新文件" if rolled.ok else ""
                raise ApiError(renamed.message + detail)
            # 整理记录里的旧名正文副本不能留下，否则撤销整理还能把 old.md 写回来
            await asyncio.to_thread(
                self.plugin._tidy.remove_file,
                tidy.scope_id(scope, key),
                old,
            )
            message = f"{message}；{renamed.message}"
        self._changed(scope, key)
        return _ok({"file": name, "mtime": store.mtime(name) or 0.0}, message)

    async def board_publish(self) -> dict:
        """管理页发布一条留言板告示：署名必填，正文与标题由页面填写。"""
        body = await _body()
        signature = " ".join(_text(body.get("signature")).split())
        if not signature:
            raise ApiError("请填写署名：每条留言板告示都要带署名")
        if len(signature) > memstore.MAX_SIGNATURE_CHARS:
            raise ApiError(f"署名最长 {memstore.MAX_SIGNATURE_CHARS} 个字")
        description = " ".join(
            _text(body.get("description") or body.get("title")).split()
        )
        if not description:
            raise ApiError("请填写标题")
        if len(description) > MAX_BOARD_TITLE:
            raise ApiError(f"标题最长 {MAX_BOARD_TITLE} 个字")
        content = _text(body.get("body") or body.get("content")).strip()
        if not content:
            raise ApiError("正文不能为空")
        if len(content) > MAX_BOARD_BODY:
            raise ApiError(f"正文最长 {MAX_BOARD_BODY} 个字")
        store = self.plugin._store.board_scope()
        name = self._board_file_name(body.get("file"), store)
        text = memstore.compose_memory(
            name[: -len(memstore.MEMORY_FILE_SUFFIX)],
            description,
            "reference",
            content,
            signature=signature,
        )
        report = await store.write(name, text, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        # 留言板对所有会话可见：作废全部开头块快照
        self._changed(SCOPE_BOARD, None)
        return _ok({"file": name}, report.message)

    @staticmethod
    def _board_file_name(raw: Any, store: memstore.ScopeStore) -> str:
        requested = _text(raw).strip()
        if requested:
            name = requested
            if not name.endswith(memstore.MEMORY_FILE_SUFFIX):
                name += memstore.MEMORY_FILE_SUFFIX
            if not memstore.ScopeStore.is_valid_name(name):
                raise ApiError("文件名不合法：" + memstore.ScopeStore.path_rules())
            if name.lower() == memstore.INDEX_FILENAME.lower():
                raise ApiError("MEMORY.md 请在「索引」页编辑")
            return name
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        base = f"notice-{stamp}"
        name = f"{base}.md"
        if not store.exists(name):
            return name
        for index in range(2, 100):
            name = f"{base}-{index}.md"
            if not store.exists(name):
                return name
        raise ApiError("同时发布的告示太多，请稍后再试")

    async def file_delete(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.delete(name, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(scope, key)
        return _ok({"file": name}, report.message)

    # ------------------------------------------------------------ 遗忘

    async def forgotten_file(self) -> dict:
        _, _, store = self._scope_from_query()
        name = self._file_name(_query("file"))
        content = await store.read_forgotten(name)
        if content is None:
            raise ApiError(f"没有已遗忘的 {name}")
        used = await asyncio.to_thread(store.usage_of, name)
        return _ok(
            {
                "file": name,
                "content": content,
                "lastUsed": used.last_used,
                "uses": used.uses,
                "forgottenAt": used.forgotten_at,
            }
        )

    async def file_forget(self) -> dict:
        """管理员手动把一个在册文件移入已遗忘。"""
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.forget(name, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(scope, key)
        return _ok({"file": name}, report.message)

    async def forgotten_restore(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.restore(name, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(scope, key)
        return _ok({"file": name}, report.message)

    async def forgotten_delete(self) -> dict:
        body = await _body()
        _, _, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        report = await store.delete_forgotten(name, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        return _ok({"file": name}, report.message)

    # ------------------------------------------------------------ 历史与回收站

    async def history_versions(self) -> dict:
        """某个文件的全部历史版本，新的在前。"""
        _, _, store = self._scope_from_query()
        name = self._file_name(_query("file"))
        versions = await asyncio.to_thread(store.history_versions, name)
        return _ok(
            {
                "file": name,
                "versions": [_history_dict(item) for item in versions],
            }
        )

    async def history_file(self) -> dict:
        """读某一版历史的原文。"""
        _, _, store = self._scope_from_query()
        name = self._file_name(_query("file"))
        version = _text(_query("version")).strip()
        if not version:
            raise ApiError("缺少历史版本")
        content = await asyncio.to_thread(store.read_history, name, version)
        if content is None:
            raise ApiError(f"没有这个历史版本：{version}")
        return _ok({"file": name, "version": version, "content": content})

    async def history_restore(self) -> dict:
        """恢复某一版历史：当前在册文件会先存进历史再覆盖。"""
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        version = _text(body.get("version")).strip()
        if not version:
            raise ApiError("缺少历史版本")
        report = await store.restore_history(
            name, version, actor=history.ACTOR_WEBUI
        )
        if not report.ok:
            raise ApiError(report.message)
        self._changed(scope, key)
        return _ok({"file": name, "version": version}, report.message)

    async def trash_entries(self) -> dict:
        """回收站条目，新的在前。"""
        _, _, store = self._scope_from_query()
        entries = await asyncio.to_thread(store.trash_entries)
        return _ok({"entries": [_trash_dict(item) for item in entries]})

    async def trash_file(self) -> dict:
        """读回收站条目的原文。"""
        _, _, store = self._scope_from_query()
        entry_name = _text(_query("entry")).strip()
        if not entry_name:
            raise ApiError("缺少回收站条目")

        def read():
            info = next(
                (item for item in store.trash_entries() if item.entry == entry_name),
                None,
            )
            return info, store.read_trash(entry_name)

        info, content = await asyncio.to_thread(read)
        if info is None or content is None:
            raise ApiError("回收站里没有这个条目")
        data = _trash_dict(info)
        data["content"] = content
        return _ok(data)

    async def trash_restore(self) -> dict:
        """从回收站恢复一条：同名在册文件先存进历史再覆盖。"""
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        entry_name = _text(body.get("entry")).strip()
        if not entry_name:
            raise ApiError("缺少回收站条目")
        report = await store.restore_trash(entry_name, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        self._changed(scope, key)
        return _ok({"entry": entry_name}, report.message)

    async def trash_delete(self) -> dict:
        """彻底删除一条回收站条目。"""
        body = await _body()
        _, _, store = self._scope_from_body(body)
        entry_name = _text(body.get("entry")).strip()
        if not entry_name:
            raise ApiError("缺少回收站条目")
        report = await store.delete_trash(entry_name, actor=history.ACTOR_WEBUI)
        if not report.ok:
            raise ApiError(report.message)
        return _ok({"entry": entry_name}, report.message)

    async def file_purge(self) -> dict:
        """彻底删除在册/已遗忘文件及其全部历史、回收站条目（要求遗忘时用）。"""
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        name = self._file_name(body.get("file"))
        sid = tidy.scope_id(scope, key)
        report = await store.delete_file_completely(
            name, actor=history.ACTOR_WEBUI
        )
        if not report.ok:
            # 正文可能只残留在 tidy/*.runs.json 的 before 里（整理合并后
            # 回收站又按保留期清掉了）。这时 memstore 报"没有找到"，但
            # 撤销整理仍能把内容写回；把整理记录清掉后按成功处理。
            if report.message == f"没有找到 {name}":
                removed = await asyncio.to_thread(
                    self.plugin._tidy.remove_file, sid, name
                )
                if removed:
                    self._changed(scope, key)
                    return _ok(
                        {"file": name},
                        f"已清除 {name} 在整理记录里的残留内容",
                    )
            raise ApiError(report.message)
        # 整理记录里也留着改动前原文；彻底删除要一并清掉，否则撤销整理
        # 会把刚删掉的正文写回来
        await asyncio.to_thread(self.plugin._tidy.remove_file, sid, name)
        self._changed(scope, key)
        return _ok({"file": name}, report.message)

    async def index_save(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        report = await store.write_index(_text(body.get("content")))
        if not report.ok:
            raise ApiError(report.message)
        self._changed(scope, key)
        return _ok({}, report.message)

    async def index_rebuild(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        count = await store.rebuild_index()
        self._changed(scope, key)
        return _ok({"count": count}, f"索引已按 {count} 个记忆文件重建")

    # ------------------------------------------------------------ 会话管理

    async def whisper_remark(self) -> dict:
        body = await _body()
        key = self._scope_key(body.get("key"), SCOPE_WHISPER)
        remark = _text(body.get("remark")).strip()[:MAX_REMARK]
        record = await asyncio.to_thread(self.plugin._registry.set_remark, key, remark)
        return _ok(_record_dict(key, record), "备注已保存")

    async def whisper_purge(self) -> dict:
        """清除一段对话的悄悄话。

        target=memories（默认）：只删这个会话的悄悄话目录；
        target=all：连同上下文统计、登记记录一并删除。
        """
        body = await _body()
        key = self._scope_key(body.get("key"), SCOPE_WHISPER)
        plugin = self.plugin
        store = plugin._store.whisper_scope_by_key(key)
        removed = (
            await store.purge(actor=history.ACTOR_WEBUI)
            if store is not None
            else 0
        )
        plugin.invalidate_scope(SCOPE_WHISPER, key)
        # 记忆都没了，整理记录也跟着作废
        await asyncio.to_thread(
            plugin._tidy.remove, tidy.scope_id(SCOPE_WHISPER, key)
        )
        if _text(body.get("target")).strip().lower() != "all":
            return _ok({"removed": removed}, f"已删除 {removed} 个记忆文件")

        found = plugin.runtime_for_key(key)
        if found is not None:
            plugin._sessions.pop(found[0], None)

        def wipe() -> None:
            path = plugin.stats_path_for_key(key)
            if path.is_file():
                path.unlink()
            plugin._registry.remove(key)

        await asyncio.to_thread(wipe)
        return _ok(
            {"removed": removed},
            "已删除这段对话的悄悄话与上下文统计；会话再次发言时会重新登记",
        )

    # ------------------------------------------------------------ 记忆整理

    async def consolidate_preview(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        if not book.claim(sid):
            raise ApiError("这个作用域正在整理中，请等待结果")
        try:
            proposal = await tidy.propose(
                store, self._scope_label(scope, key), self.plugin.call_consolidator
            )
        finally:
            book.release(sid)
        return _ok(
            {
                "operations": [op.to_dict() for op in proposal.operations],
                "skipped": proposal.skipped,
                "fileCount": len(proposal.files),
                "files": proposal.files,
            }
        )

    async def consolidate_apply(self) -> dict:
        """应用整理方案（管理页手动整理，使用默认对话模型）。"""
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        raw_ops = body.get("operations")
        existing = set(await asyncio.to_thread(store.list_files))
        ops = consolidate.operations_from_payload(raw_ops, existing)
        requested = consolidate.requested_files(raw_ops)
        if not ops and not requested:
            raise ApiError("没有可应用的操作")
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        if not book.claim(sid):
            raise ApiError("这个作用域正在整理中，请稍后再试")
        try:
            # 没通过重新校验的条目（比如复盘后文件已不在）也要让管理员看到
            skipped = [
                {"op": "", "file": file, "ok": False, "message": "未通过校验，已跳过"}
                for file in sorted(requested - {op.file for op in ops})
            ]
            results = await tidy.apply(
                store,
                ops,
                actor=history.ACTOR_TIDY,
            )
            now = time.time()
            run = tidy.make_run(tidy.TRIGGER_MANUAL, results, now)
            done = sum(1 for item in results if item["ok"])

            def record() -> None:
                if run is not None:
                    book.add_run(sid, run)

                def change(meta: tidy.TidyMeta) -> None:
                    meta.last_run = now
                    meta.failures = 0
                    meta.outcome = tidy.OUTCOME_APPLIED
                    meta.message = f"应用了 {done} 项修改"

                book.update_meta(sid, change)

            await asyncio.to_thread(record)
        finally:
            book.release(sid)
        self._changed(scope, key)
        results = skipped + tidy.public_results(results)
        return _ok(
            {"results": results, "runId": run["id"] if run else ""},
            f"已应用 {done} / {len(results)} 项操作",
        )

    async def tidy_runs(self) -> dict:
        scope, key, _ = self._scope_from_query()
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        runs, meta = await asyncio.to_thread(lambda: (book.runs(sid), book.meta(sid)))
        return _ok(
            {
                "runs": [tidy.public_run(run) for run in runs],
                "tidy": self._tidy_info(meta),
            }
        )

    async def tidy_undo(self) -> dict:
        body = await _body()
        scope, key, store = self._scope_from_body(body)
        run_id = _text(body.get("id")).strip()
        book = self.plugin._tidy
        sid = tidy.scope_id(scope, key)
        runs = await asyncio.to_thread(book.runs, sid)
        run = next((r for r in runs if r.get("id") == run_id), None)
        if run is None:
            raise ApiError("这条整理记录已不存在")
        if run.get("undoneAt"):
            raise ApiError("这次整理已经撤销过了")
        if not book.claim(sid):
            raise ApiError("这个作用域正在整理中，请稍后再试")
        try:
            results = await tidy.undo(store, run, actor=history.ACTOR_TIDY)
            await asyncio.to_thread(book.mark_undone, sid, run_id, time.time())
        finally:
            book.release(sid)
        self._changed(scope, key)
        done = sum(1 for item in results if item["ok"])
        return _ok({"results": results}, f"已撤销 {done} / {len(results)} 项")

    # ------------------------------------------------------------ 迁移与备份

    def _ref(self, raw: Any) -> transfer.ScopeRef:
        if not isinstance(raw, dict):
            raise ApiError("缺少作用域")
        return transfer.make_ref(raw.get("scope"), raw.get("key"))

    def _changed_ref(self, ref: transfer.ScopeRef) -> None:
        self._changed(ref.scope, ref.key or None)

    async def migrate_info(self) -> dict:
        """迁移时可选的平台 ID：已有小本本、悄悄话与当前启用的机器人。"""
        records = self.plugin._registry.all()
        counts: dict[str, int] = {}

        def collect() -> list[str]:
            for key in sorted(set(self.plugin._store.whisper_keys()) | set(records)):
                record = records.get(key)
                platform = (
                    record.platform if record is not None and record.platform
                    else registry.guess_from_key(key)[0]
                )
                if platform:
                    counts[platform] = counts.get(platform, 0) + 1
            return list(self.plugin._store.notebook_keys())

        notebooks = await asyncio.to_thread(collect)
        return _ok(
            {
                "platforms": [
                    {"platform": p, "sessions": n, "notebook": p in notebooks}
                    for p, n in sorted(counts.items())
                ]
                + [
                    {"platform": p, "sessions": 0, "notebook": True}
                    for p in notebooks
                    if p not in counts
                ],
                "adapters": self.plugin.platform_ids(),
            }
        )

    async def export_prepare(self) -> dict:
        """生成一份备份暂存起来，返回下载用的令牌和内容概要。"""
        body = await _body()
        raw_scopes = body.get("scopes")
        refs = None
        if isinstance(raw_scopes, list):
            refs = []
            for raw in raw_scopes:
                ref = self._ref(raw)
                if ref not in refs:
                    refs.append(ref)
        bundle = await self.transfer.export(
            refs, include_forgotten=bool(body.get("includeForgotten", True))
        )
        if not bundle["scopes"]:
            raise ApiError("选中的地方都还没有记忆，没有可导出的内容")
        token = self._stage(bundle)
        files = sum(len(item["files"]) for item in bundle["scopes"])
        stamp = time.strftime("%Y%m%d-%H%M", time.localtime(bundle["exportedAt"]))
        return _ok(
            {
                "token": token,
                "scopeCount": len(bundle["scopes"]),
                "fileCount": files,
                "filename": f"memory-backup-{stamp}.json",
            }
        )

    async def export_file(self) -> dict:
        """下载生成好的备份：直接返回备份内容本身。"""
        bundle = self._staged_payload(_query("token"))
        if not isinstance(bundle, dict) or bundle.get("format") != transfer.BUNDLE_FORMAT:
            raise ApiError("内容已过期，请重新生成备份")
        return bundle

    async def import_preview(self) -> dict:
        """读入备份并预演：每处有多少条会新增、覆盖、保留。

        首次传 bundle（备份内容），之后改冲突策略时只传 token。
        """
        body = await _body()
        if body.get("token"):
            token = _text(body.get("token")).strip()
            scopes = self._staged_payload(token)
        else:
            scopes = transfer.parse_bundle(body.get("bundle"))
            token = self._stage(scopes)
        if not isinstance(scopes, list):
            raise ApiError("内容已过期，请重新选择备份文件")
        if not scopes:
            raise ApiError("备份里没有记忆")
        policy = transfer.check_policy(body.get("policy"))
        summary = self.transfer.bundle_summary(scopes)
        reports = await self.transfer.import_bundle(scopes, policy, dry_run=True)
        for item, report in zip(summary, reports):
            item["counts"] = report["counts"]
            item["issues"] = report["issues"]
        return _ok({"token": token, "scopes": summary, "warnings": []})

    async def import_apply(self) -> dict:
        body = await _body()
        scopes = self._staged_payload(body.get("token"))
        if not isinstance(scopes, list):
            raise ApiError("内容已过期，请重新选择备份文件")
        policy = transfer.check_policy(body.get("policy"))
        wanted = body.get("selection")
        if isinstance(wanted, list):
            ids = {self._ref(raw).id for raw in wanted}
            scopes = [item for item in scopes if item.ref.id in ids]
        if not scopes:
            raise ApiError("没有选中要导入的地方")
        reports = await self.transfer.import_bundle(scopes, policy)
        for item in scopes:
            self._changed_ref(item.ref)
        written = sum(
            r["counts"]["added"] + r["counts"]["replaced"] for r in reports
        )
        message = f"导入完成：{len(reports)} 处，写入 {written} 条记忆"
        return _ok({"reports": reports}, message)

    async def migrate_scope(self) -> dict:
        """把一处的记忆复制或移动到另一处；目标会话可以用完整的会话标识新建。"""
        body = await _body()
        src = self._ref(body.get("from"))
        target = body.get("to") if isinstance(body.get("to"), dict) else {}
        dst_umo = _text(target.get("umo")).strip()
        if dst_umo:
            if target.get("scope") not in (None, "", SCOPE_WHISPER):
                raise ApiError("会话标识只能用于悄悄话")
            if dst_umo.count(":") < 2:
                raise ApiError("会话标识的格式是 平台ID:消息类型:会话ID")
            dst = transfer.ScopeRef(SCOPE_WHISPER, memstore.safe_key(dst_umo))
        elif target.get("scope") == SCOPE_NOTEBOOK and target.get("platform"):
            platform = transfer.check_platform(target.get("platform"))
            dst = transfer.ScopeRef(SCOPE_NOTEBOOK, memstore.safe_key(platform))
        else:
            dst = self._ref(target)
        raw_files = body.get("files")
        files = (
            [_text(name).strip() for name in raw_files]
            if isinstance(raw_files, list)
            else None
        )
        dry_run = bool(body.get("dryRun"))
        mode = _text(body.get("mode") or transfer.MODE_COPY).strip().lower()
        report = await self.transfer.transfer(
            src,
            dst,
            dst_umo=dst_umo,
            files=files,
            include_forgotten=bool(body.get("includeForgotten", True)),
            mode=mode,
            policy=transfer.check_policy(body.get("policy")),
            dry_run=dry_run,
        )
        if dry_run:
            return _ok(report)
        self._changed_ref(dst)
        if report.get("moved"):
            # 搬走之后原处不再存在于原位置
            self._changed_ref(src)
        counts = report["counts"]
        message = f"已写入 {counts['added'] + counts['replaced']} 条"
        if report.get("moved"):
            message += f"，已从原处移走 {report['moved']} 条"
        return _ok(report, message)

    async def migrate_platform(self) -> dict:
        """平台 ID 变了之后，把旧 ID 下的小本本与悄悄话搬到新 ID 下。"""
        body = await _body()
        dry_run = bool(body.get("dryRun"))
        mode = _text(body.get("mode") or transfer.MODE_MOVE).strip().lower()
        include_notebook = bool(body.get("includeNotebook", True))
        src_platform = _text(body.get("from"))
        dst_platform = _text(body.get("to"))
        result = await self.transfer.remap_platform(
            src_platform,
            dst_platform,
            mode=mode,
            policy=transfer.check_policy(body.get("policy")),
            include_notebook=include_notebook,
            dry_run=dry_run,
        )
        if dry_run:
            return _ok(result)
        for item in result["items"]:
            for side in ("from", "to"):
                ref = transfer.ScopeRef(item[side]["scope"], item[side]["key"])
                self._changed_ref(ref)
        totals = result["totals"]
        message = (
            f"迁移完成：{len(result['items'])} 处，写入 "
            f"{totals['added'] + totals['replaced']} 条记忆"
        )
        return _ok(result, message)
