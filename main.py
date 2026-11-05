"""鲸群记忆 —— 为 AstrBot 重新设计的长期记忆系统。

核心理念：真相在磁盘，上下文只带索引，细节按需取回；上下文的截断与压缩
完全交给 AstrBot 的「上下文管理策略」，插件不再改动对话历史。

分层约定：
    main.py     —— 平台适配层（本文件）：钩子、LLM 工具、指令、provider 调用
    web_api.py  —— WebUI 记忆管理页的后端接口（页面在 pages/memory）
    core/       —— 纯逻辑：记忆库、索引、召回、token 估算、上下文统计、
                   会话登记、记忆整理

注入位置（PLAN 3.8.1，依据 AstrBot 4.29.4 源码）：
- 稳定的开头块（记忆规范、索引、私聊档案）接在 req.system_prompt 后面。
  AstrBot 把 system_prompt 作为开头唯一的 system 消息（tool_loop_agent_runner
  第 324-328 行），按轮数截断时开头 system 消息整体保留（truncator
  _split_system_rest），LLM 摘要压缩时也只保留开头的 system 消息；
  回存历史时第一条 system 消息被跳过
  （agent_sub_stages/internal.py 第 517-520 行）。
- 每轮变化的召回块放进 req.extra_user_content_parts，带 _no_save 标记：
  它随本轮用户消息一起进入请求（entities.assemble_context），是最新的一轮，
  截断与压缩都会保留；回存历史时按内容块丢弃（message.model_dump +
  dump_messages_with_checkpoints），不会在平台历史里逐轮累积。

兼容性立场：只依赖 @filter.on_llm_request() / @filter.on_llm_response() /
@filter.llm_tool() 等公开插件接口、不 monkey-patch。跟随 AstrBot 取上下文
窗口时要读 AstrBot 内部配置和模型表，相关 import 全部包在 try 里：
取不到就跳过那一级，插件照常加载。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .core import (
    context_stats,
    history,
    injection,
    insights,
    journal,
    memstore,
    pending,
    prompts,
    recall,
    registry,
    sharing,
    tidy,
    tokens,
    writer,
)
from .web_api import MemoryDashboard

PLUGIN_NAME = "astrbot_plugin_memory_pod"
LOG_PREFIX = "[memory_pod] "

# 后台整理调用模型失败时的尝试次数与单次超时（不再有配置项）。
BACKGROUND_RETRIES = 3
BACKGROUND_TIMEOUT = 120
# 拿不到本轮工具声明时（结构异常等）估算工具 schema 的兜底余量；
# 正常路径按 req.func_tool 的实际 schema 序列化后实测。
TOOL_SCHEMA_ALLOWANCE = 800
# 全局记忆关闭时对 scope=global 工具调用的固定回执；工具说明是静态的
# 仍会列出 global，回执要把模型引导回 session。
GLOBAL_MEMORY_OFF = "全局记忆已在插件配置中停用；请改用 session 作用域。"
SCOPE_ARG_ERROR = "scope 参数必须是 global、session 或 people。"
# 会话运行时缓存上限（LRU 淘汰，仅内存快照，磁盘状态不受影响）。
MAX_RUNTIME_SESSIONS = 256
# 遗忘扫描：每个作用域目录至多每隔这么久扫一次（秒）；默认多久没用到算遗忘（天）
FORGET_SWEEP_INTERVAL = 6 * 3600
FORGET_DEFAULT_DAYS = 90
# 一轮请求超过空闲时间也不能提前整理：只要请求还在生成，空闲扫描就跳过。
# 真卡死的请求最多挡这么久，之后按普通空闲处理，避免永远不整理。
PENDING_INFLIGHT_MAX = 3600.0
# 召回块的 _no_save 数据声明；同一条请求被重复处理时据此避免重复追加。
RECALL_MARKER = recall.RECALL_BLOCK_OPEN


def tool_schema_cost(toolset, estimator: tokens.TokenEstimator) -> int:
    """实测本轮声明的工具 schema 占用；没有工具为 0，读取失败退回固定余量。

    provider 发给 API 的就是 openai_schema() 的序列化结果（其余厂商格式
    同量级），照实测比猜一个常数可靠——工具的数量和描述长短因装的插件
    而异，差距可以数以千计。
    """
    if toolset is None:
        return 0
    try:
        if toolset.empty():
            return 0
        payload = json.dumps(toolset.openai_schema(), ensure_ascii=False)
        return estimator.text(payload)
    except Exception:  # noqa: BLE001
        return estimator.scale(TOOL_SCHEMA_ALLOWANCE)


@dataclass
class RuntimeSession:
    """一个会话的内存态：开头块快照与最近一次请求的上下文统计。

    开头块在对话开始时读盘一次、之后复用快照（prompt caching 按前缀匹配，
    开头的注入内容一变，整段历史的缓存全部失效）。对话中途模型新写的
    记忆只落盘、不刷新快照——需要时由每轮的相关记忆召回带上全文。
    对话切换、重置、管理员在 WebUI 改动记忆、检测到 AstrBot 截断或压缩
    改动了历史开头时刷新快照（那时提示词缓存本来就已经失效）。
    """

    # 快照锚定的对话 id；变了说明换了对话或被重置
    cid: str = ""
    index_block: str | None = None
    index_loaded: bool = False
    # 快照构建时的全局记忆开关；配置热改后据此判定快照失效
    index_global_on: bool = False
    # 私聊人物档案的快照签名（(pin, 平台, 发送者)）；配置或身份变了就重建
    index_pin_key: tuple = ()
    # 开头块里已钉住的人物档案：ref -> 注入时的内容摘要（检测快照后的修改）
    index_pinned: dict[str, str] = field(default_factory=dict)
    # 开头块索引里实际出现过的文件引用；指针行据此决定是否附描述
    index_refs: set[str] = field(default_factory=set)
    # 构建快照时历史开头的指纹；None 表示当时还没有历史，下一轮补记
    history_head: str | None = None
    # 最近一次请求的估算分项、总额与提供商上报值（供 /mb_status 与面板）
    stats: context_stats.ContextStats = field(
        default_factory=context_stats.ContextStats
    )

    def clear_index(self) -> None:
        """作废开头块快照以及随快照生成的人物档案与索引引用集合。"""
        self.index_block = None
        self.index_loaded = False
        self.index_pin_key = ()
        self.index_pinned = {}
        self.index_refs = set()
        self.history_head = None


class MemoryBeyondPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = config if config is not None else {}
        data_dir = self._resolve_data_dir()
        self.data_dir = data_dir
        self._store = memstore.MemoryStore(data_dir / "memories")
        self._store.set_retention(
            self._int("history_keep_days", history.DEFAULT_KEEP_DAYS),
            self._int("trash_keep_days", history.DEFAULT_KEEP_DAYS),
        )
        self._history_cleanup_at = 0.0
        self._state_dir = data_dir / "state"
        self._registry = registry.SessionRegistry(data_dir / "sessions.json")
        self._sessions: OrderedDict[str, RuntimeSession] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        # 作用域目录 -> 上次遗忘扫描时间
        self._last_sweep: dict[Path, float] = {}
        # 定期整理：每处的记录与进行中标记；作用域 -> 上次检查时间；
        # 后台整理与定期整理共用一个信号量，一次只跑一个
        self._tidy = tidy.TidyBook(data_dir / "tidy")
        self._tidy_checked: dict[str, float] = {}
        self._background_gate = asyncio.Semaphore(1)
        # 待整理记录：每轮的对话缓冲，整理成功后删除
        self._pending = pending.PendingBook(data_dir / "state" / "pending")
        self._write_scheduled: set[str] = set()
        self._write_tasks: dict[str, asyncio.Task] = {}
        self._pending_loop: asyncio.Task | None = None
        # 会话键 -> 本轮请求开始时间：生成期间空闲扫描不能抢先整理
        self._inflight: dict[str, float] = {}
        # 共享人物空间：成员表从配置解析
        self._sharing = sharing.Membership()
        self._sharing_warned: set[str] = set()
        self._refresh_sharing()
        self.dashboard = MemoryDashboard(self)
        self.dashboard.register()

        logger.info(LOG_PREFIX + f"已加载：数据目录 {data_dir}")
        # 重启后有待整理记录的会话照样会被每分钟一次的扫描找到
        self._ensure_pending_loop()

    @staticmethod
    def _resolve_data_dir() -> Path:
        try:
            return Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:  # noqa: BLE001 - 旧版本 StarTools 行为不一，逐级回退
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME

    # ================================================================ 配置

    def _bool(self, key: str, default: bool) -> bool:
        return bool(self.config.get(key, default))

    def _int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _str(self, key: str, default: str = "") -> str:
        value = self.config.get(key, default)
        return value if isinstance(value, str) else default

    # ============================================================ 会话运行时

    async def _runtime(self, umo: str) -> RuntimeSession:
        rt = self._sessions.get(umo)
        if rt is not None:
            self._sessions.move_to_end(umo)
            return rt
        rt = RuntimeSession()
        stats = await self.load_stats_for_key(memstore.safe_key(umo))
        if stats is not None:
            rt.stats = stats
        self._sessions[umo] = rt
        while len(self._sessions) > MAX_RUNTIME_SESSIONS:
            self._sessions.popitem(last=False)
        return rt

    def _invalidate_index_cache(self, umo: str | None) -> None:
        """作废索引快照，下次请求从磁盘重读。umo 为 None 时作废全部会话
        （global 记忆变更对所有会话可见）。"""
        if umo is None:
            targets = list(self._sessions.values())
        else:
            rt = self._sessions.get(umo)
            targets = [rt] if rt is not None else []
        for rt in targets:
            rt.clear_index()

    def _invalidate_pinned(self, scope: str, files: list[str]) -> None:
        """档案被删除或移入遗忘后，作废把它钉在开头块的快照。

        普通修改不走这里：主流程要把最新内容附在本轮召回块里并注明，
        直到下次快照刷新；删除/遗忘没有最新全文可附，继续注入旧内容
        等于让用户删不掉。
        """
        refs = {f"{scope}/{file}" for file in files if file}
        if not refs:
            return
        for rt in self._sessions.values():
            if refs & set(rt.index_pinned):
                rt.clear_index()

    def _store_scope_label(self, store: memstore.ScopeStore) -> str:
        """作用域目录属于哪一层（global / session / people）；认不出返回空串。"""
        try:
            rel = store.root.resolve().relative_to(self._store.base_dir.resolve())
        except (OSError, ValueError):
            return ""
        return rel.parts[0] if rel.parts else ""

    def runtime_for_key(self, key: str) -> tuple[str, RuntimeSession] | None:
        """按会话目录名找到已加载的运行时（WebUI 改动后同步内存态用）。"""
        for umo, rt in self._sessions.items():
            if memstore.safe_key(umo) == key:
                return umo, rt
        return None

    def invalidate_for_key(self, key: str | None) -> None:
        """WebUI 改动记忆后作废对应会话的索引快照；key 为 None 表示全局记忆。"""
        if key is None:
            self._invalidate_index_cache(None)
            return
        found = self.runtime_for_key(key)
        if found is not None:
            self._invalidate_index_cache(found[0])
            return
        # key 也可能是 people 目录名（管理页编辑人物档案）：它不对应任何
        # 会话运行时，但人物档案可能钉在私聊开头块里，一并作废。
        for rt in self._sessions.values():
            if any(ref.startswith("people/") for ref in rt.index_pinned):
                rt.clear_index()

    def stats_path_for_key(self, key: str) -> Path:
        return self._state_dir / "context" / f"{key}.json"

    def _stats_path(self, umo: str) -> Path:
        return self.stats_path_for_key(memstore.safe_key(umo))

    async def load_stats_file(
        self, path: Path
    ) -> context_stats.ContextStats | None:
        """读取一个会话的上下文统计；没有或损坏时返回 None（不影响对话）。"""
        try:
            if path.is_file():
                raw = await asyncio.to_thread(path.read_text, "utf-8")
                return context_stats.ContextStats.from_dict(json.loads(raw))
        except (OSError, ValueError) as exc:
            logger.warning(LOG_PREFIX + f"读取上下文统计失败（{path.name}）：{exc}")
        return None

    async def load_stats_for_key(
        self, key: str
    ) -> context_stats.ContextStats | None:
        return await self.load_stats_file(self.stats_path_for_key(key))

    async def _save_stats(self, umo: str) -> None:
        rt = self._sessions.get(umo)
        if rt is not None:
            await self.save_stats_file(self._stats_path(umo), rt.stats)

    async def save_stats_file(
        self, path: Path, stats: context_stats.ContextStats
    ) -> None:

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(stats.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"持久化上下文统计失败（{path.name}）：{exc}")

    def _spawn(self, coro, desc: str):
        """启动后台任务并保留引用（插件卸载时统一取消）。"""
        task = asyncio.create_task(coro, name=f"memory_pod:{desc}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _schedule_history_cleanup(self) -> None:
        """每 6 小时在后台清理一次各作用域的历史与回收站。

        ScopeStore 自己也会按间隔兜住，所以即便这里偶尔多调度一次，
        每个作用域最多还是每 6 小时清一次；清理全程在工作线程里。
        """
        # 配置页里改过保留期时，已缓存的作用域也跟着生效
        self._store.set_retention(
            self._int("history_keep_days", history.DEFAULT_KEEP_DAYS),
            self._int("trash_keep_days", history.DEFAULT_KEEP_DAYS),
        )
        now = time.time()
        if now - self._history_cleanup_at < history.CLEANUP_INTERVAL:
            return
        self._history_cleanup_at = now
        self._spawn(self._cleanup_history(now), "history-cleanup")

    async def _cleanup_history(self, now: float) -> None:
        try:
            # 作用域对象在事件循环里取好再进线程：同一目录只能有一个实例
            scopes = self._store.scopes_for_cleanup()
            await asyncio.to_thread(self._store.cleanup_history, now, scopes)
        except Exception:  # noqa: BLE001 - 清理失败不能影响对话
            logger.warning(LOG_PREFIX + "历史与回收站清理失败", exc_info=True)

    def _touch_registry(self, event: AstrMessageEvent, umo: str) -> bool:
        """登记会话活跃；返回是否需要落盘。登记失败不影响请求。"""
        try:
            return self._registry.touch(
                memstore.safe_key(umo), umo, self._session_label(event)
            )
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "会话登记失败", exc_info=True)
            return False

    async def _flush_registry(self) -> None:
        try:
            await asyncio.to_thread(self._registry.flush)
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"会话登记簿写盘失败：{exc}")

    # ============================================================ 身份与作用域

    @staticmethod
    def _sender_identity(event: AstrMessageEvent) -> tuple[str, str]:
        """当前发送者的 (数字 ID, 昵称)。ID 不可伪造，昵称仅作附注。"""
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        name = ""
        try:
            name = str(event.get_sender_name() or "").strip()
        except Exception:  # noqa: BLE001
            pass
        return sender_id, name

    @staticmethod
    def _event_actor(event: AstrMessageEvent) -> str:
        """审计里的 actor：工具调用记当前事件的平台 ID。"""
        try:
            return str(event.get_platform_id() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _sender_raw_fields(
        event: AstrMessageEvent,
    ) -> tuple[str | None, str | None, bool]:
        """从平台原始事件提取 (QQ昵称, 群名片, 是否群聊)。

        OneBot（aiocqhttp）的 sender.nickname 是 QQ 昵称、sender.card 是
        群名片；AstrBot 的 MessageMember 只保留了"群名片优先"的单一名称，
        所以这两个字段必须从 raw_message 里取。取不到时返回 (None, None, …)，
        标注退化为通用的 名称｜ID 形式。
        """
        msg_obj = getattr(event, "message_obj", None)
        is_group = bool(getattr(msg_obj, "group_id", "") or "")
        raw = getattr(msg_obj, "raw_message", None)
        raw_sender = None
        if isinstance(raw, dict):
            raw_sender = raw.get("sender")
        if raw_sender is None:
            raw_sender = getattr(raw, "sender", None)
        if isinstance(raw_sender, dict) and (
            "nickname" in raw_sender or "card" in raw_sender
        ):
            qq_name = str(raw_sender.get("nickname") or "")
            group_card = str(raw_sender.get("card") or "")
            return qq_name, group_card, is_group
        return None, None, is_group

    @classmethod
    def _session_label(cls, event: AstrMessageEvent) -> str:
        """会话的可读名称：群聊取平台提供的群名，私聊取对方昵称。"""
        msg_obj = getattr(event, "message_obj", None)
        group = getattr(msg_obj, "group", None)
        group_name = str(getattr(group, "group_name", "") or "").strip()
        if group_name and group_name.upper() != "N/A":
            return group_name
        if getattr(msg_obj, "group_id", ""):
            return ""
        return cls._sender_identity(event)[1]

    @staticmethod
    def _mentioned_ids(event: AstrMessageEvent) -> list[str]:
        """本条消息 @ 到的人与被回复消息的发送者（数字 ID），不含机器人自己。"""
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:  # noqa: BLE001
            self_id = ""
        try:
            segments = list(event.get_messages() or [])
        except Exception:  # noqa: BLE001
            return []
        ids: list[str] = []
        for seg in segments:
            if type(seg).__name__ == "Reply":
                raw = getattr(seg, "sender_id", None)
            else:
                raw = getattr(seg, "qq", None)
            uid = str(raw or "").strip()
            if uid.isdigit() and uid != self_id and uid not in ids:
                ids.append(uid)
        return ids

    def _global_on(self) -> bool:
        """全局记忆开关，默认关闭。关闭时不注入全局索引、拒绝全局读写，
        机器人只使用当前会话的记忆；磁盘上已有的全局文件原样保留。"""
        return self._bool("enable_global_memory", False)

    def _global_scope(self) -> memstore.ScopeStore:
        """机器人自我的全局记忆，所有会话共享一个目录。"""
        return self._store.global_scope()

    def _session_scope(self, event: AstrMessageEvent) -> memstore.ScopeStore:
        return self._store.session_scope(str(event.unified_msg_origin))

    def _forgetting_on(self) -> bool:
        """记忆遗忘开关，默认关闭。开启后长期没被召回或读取的记忆移出索引与
        召回（文件保留在 forgotten/ 下），再被用到时自动恢复；关闭时什么都不动，
        使用统计照常记录。"""
        return self._bool("enable_forgetting", False)

    def _forget_after_days(self) -> int:
        return max(1, self._int("forget_after_days", FORGET_DEFAULT_DAYS))

    def tidy_mode(self) -> str:
        """定期整理方式：off 关闭（默认）、confirm 有方案先问管理员、auto 直接应用。"""
        return tidy.normalize_mode(self._str("auto_consolidate", tidy.MODE_OFF))

    def tidy_hours(self) -> int:
        return tidy.clamp_hours(
            self._int("auto_consolidate_hours", tidy.DEFAULT_HOURS)
        )

    async def save_tidy_settings(self, mode: str, hours: int | None) -> None:
        """管理页改定期整理设置：写回插件配置，配置页里看到的随之变化。"""
        self.config["auto_consolidate"] = mode
        if hours is not None:
            self.config["auto_consolidate_hours"] = hours
        self._tidy_checked.clear()
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            await asyncio.to_thread(saver)

    def _platform_insts(self) -> list:
        """当前启用的平台适配器；拿不到时返回空列表。"""
        manager = getattr(self.context, "platform_manager", None)
        try:
            getter = getattr(manager, "get_insts", None)
            insts = getter() if callable(getter) else getattr(manager, "platform_insts", [])
        except Exception:  # noqa: BLE001 - 平台管理器的实现随版本变化
            return []
        return list(insts or [])

    def platform_ids(self) -> list[str]:
        """当前启用的机器人（平台适配器）ID，迁移时给管理员选。"""
        ids: list[str] = []
        for inst in self._platform_insts():
            try:
                platform_id = str(getattr(inst.meta(), "id", "") or "")
            except Exception:  # noqa: BLE001
                continue
            if platform_id and platform_id not in ids:
                ids.append(platform_id)
        return ids

    def platform_types(self) -> dict[str, str]:
        """platform_id -> 平台类型（如 aiocqhttp），用于共享成员的撞号校验。"""
        types: dict[str, str] = {}
        for inst in self._platform_insts():
            try:
                meta = inst.meta()
                platform_id = str(getattr(meta, "id", "") or "")
                kind = str(getattr(meta, "name", "") or "")
            except Exception:  # noqa: BLE001
                continue
            if platform_id and kind:
                types[platform_id] = kind
        return types

    def _refresh_sharing(self) -> None:
        """按配置重建共享人物空间成员表并注入 MemoryStore。

        配置页改动后无需重启：每次请求和工具调用前都会重新解析。成员表的
        有效内容没变时不做任何事，避免反复覆盖与刷日志。
        """
        raw = self.config.get("shared_people_bots", [])
        # 适配器暂时取不到类型时（get_insts 异常、热重载瞬间）沿用上次已知的
        # 类型，否则先前因平台不一致被关闭的成员会被重新放回共享目录。
        types = self.platform_types()
        cache = getattr(self, "_platform_type_cache", None)
        if cache is None:
            cache = {}
            self._platform_type_cache = cache
        if types:
            cache.update(types)
        membership = sharing.parse_config(raw).filter_same_platform(dict(cache))
        for warning in membership.warnings:
            if warning not in self._sharing_warned:
                self._sharing_warned.add(warning)
                logger.warning(LOG_PREFIX + warning)
        if (
            membership.members == self._sharing.members
            and self._store.shared_people().members == membership.members
        ):
            return
        self._sharing = membership
        self._store.set_shared_people(membership)
        # 成员表变化会改变 people 目录映射与共享标注；开头块里可能钉着旧
        # 目录的档案，所有会话的快照都要重读。
        self._invalidate_index_cache(None)
        if membership.active:
            logger.info(
                LOG_PREFIX
                + "共享人物空间已启用："
                + "、".join(
                    f"{m.platform_id}={m.permission}"
                    for m in membership.members.values()
                )
            )

    def _guidance(self, umo: str = "") -> str:
        return prompts.memory_guidance()

    @staticmethod
    def _platform_of(umo: str) -> str:
        platform, _, _ = registry.parse_umo(umo)
        return platform or str(umo or "").split(":", 1)[0] or "unknown"

    def _people_scope(self, umo: str) -> memstore.ScopeStore:
        return self._store.people_scope(self._platform_of(umo))

    def _people_store_for_event(
        self, event: AstrMessageEvent
    ) -> tuple[memstore.ScopeStore | None, str]:
        """事件对应的 people 作用域：(store, 拒绝原因)。

        目录按事件的 platform_id 选，而不是 UMO 前缀：AstrBot 正常构造时
        两者一致；一旦不一致，读取与权限必须看同一个身份，否则非成员可以
        借成员的 UMO 读到共享内容。取不到 platform_id 且 UMO 会落到共享
        目录时 fail-closed（无法确认成员身份）；非成员自己的 people 目录
        不受影响。
        """
        actor = self._event_actor(event)
        if actor:
            return self._store.people_scope(actor), ""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        platform = self._platform_of(umo)
        if self._sharing.is_member(platform):
            return None, sharing.IDENTITY_REFUSAL
        return self._store.people_scope(platform), ""

    def _people_permission(self, platform_id: str) -> str | None:
        """共享人物空间里这个机器人的权限；非成员返回 None（不受限制）。"""
        return self._sharing.permission(platform_id)

    def _people_access(
        self, event: AstrMessageEvent, store: memstore.ScopeStore
    ) -> tuple[str, str | None, str]:
        """共享人物空间的 (actor, 权限, 拒绝原因)。

        目录映射按 unified_msg_origin 的平台前缀，写权限按事件的 platform_id。
        platform_id 取不到、或它不是成员时无法确认身份，共享目录一律
        fail-closed；非成员自己的 people 目录不受影响（权限仍为 None）。
        """
        actor = self._event_actor(event)
        if not self._store.is_shared_people_scope(store):
            return actor, self._people_permission(actor), ""
        permission = self._sharing.permission(actor)
        if permission is None:
            return actor, None, sharing.IDENTITY_REFUSAL
        return actor, permission, ""

    def _can_restore_people(
        self, actor: str, store: memstore.ScopeStore
    ) -> bool:
        """能否把一条已遗忘的人物记忆恢复在册。

        共享空间里只有 edit 级成员能改变目录状态；read / append 级成员
        读到遗忘副本只返回内容。身份不明（非成员）同样不能恢复。
        """
        if not self._store.is_shared_people_scope(store):
            return True
        return self._sharing.permission(actor) == sharing.PERMISSION_EDIT

    def _can_maintain_people(
        self, platform_id: str, store: memstore.ScopeStore
    ) -> bool:
        """定期维护（自动遗忘扫描、定期整理）能否处理这个人物作用域。

        权限只看事件的 platform_id（与工具层一致，绝不信任 UMO 前缀）；
        共享空间里只有 edit 级成员或非成员自己的目录触发维护，append / read
        级成员只读不写，不该因为他们的会话让共享文件对所有人退出索引或被
        自动改写。管理页手动发起的整理不算自动维护，不受这里限制。
        """
        if not self._store.is_shared_people_scope(store):
            return True
        return self._people_permission(platform_id) == sharing.PERMISSION_EDIT

    def _scope_for(
        self, event: AstrMessageEvent, scope: str
    ) -> memstore.ScopeStore | None:
        scope = (scope or "").strip().lower()
        if scope == "global":
            return self._global_scope()
        if scope == "session":
            return self._session_scope(event)
        if scope == "people":
            store, _ = self._people_store_for_event(event)
            return store
        return None

    def _tool_store(
        self, event: AstrMessageEvent, scope: str
    ) -> tuple[memstore.ScopeStore | None, str, str]:
        """记忆工具的作用域解析：返回 (作用域, 归一化名称, 拒绝原因)。"""
        # 配置页可能刚改过共享成员；工具调用前同步一次，保证目录映射与权限一致
        self._refresh_sharing()
        scope = (scope or "").strip().lower()
        if scope == "global" and not self._global_on():
            return None, scope, GLOBAL_MEMORY_OFF
        if scope == "people":
            store, refusal = self._people_store_for_event(event)
            if store is None:
                return None, scope, refusal
            return store, scope, ""
        store = self._scope_for(event, scope)
        if store is None:
            return None, scope, SCOPE_ARG_ERROR
        return store, scope, ""

    # ================================================================ 钩子

    @filter.on_llm_request()
    async def inject_memory(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """请求前的介入点：把记忆规范与开头块接进系统提示词，召回块附在本轮
        用户消息上。AstrBot 之后如何截断、压缩上下文由它自己决定。"""
        try:
            await self._process_request(event, req)
        except Exception:  # noqa: BLE001 - 插件缺陷不能拖垮 LLM 请求链路
            logger.exception(LOG_PREFIX + "on_llm_request 处理失败，本轮跳过记忆注入")

    async def _process_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        # 配置页改了共享成员时，索引/召回立即按新成员表走
        self._refresh_sharing()
        self._schedule_history_cleanup()
        # 其他插件自行发起的模型调用（event.request_llm，不带对话也没有历史）
        # 同样经过这个钩子：它不是本会话的对话请求，不注入也不碰快照
        if getattr(req, "conversation", None) is None and not (req.contexts or []):
            return
        rt = await self._runtime(umo)
        now = time.time()
        if self._touch_registry(event, umo):
            await self._flush_registry()
        # 只读地看一眼 AstrBot 交给我们的历史：插件不改写 req.contexts
        contexts = [m for m in (req.contexts or []) if isinstance(m, dict)]

        # ---- 对话切换 / 重置：开头块快照作废，本轮从磁盘重读 ----
        cid = await self._conversation_id(event, req)
        if cid != rt.cid or not contexts:
            rt.cid = cid
            rt.clear_index()

        # ---- AstrBot 截断或压缩改动了历史开头：快照同样作废 ----
        # 这时提示词缓存本来就已经失效，重建开头块不会额外损失缓存。
        head = context_stats.history_head(contexts)
        if rt.index_loaded:
            if rt.history_head is None:
                # 建快照时还没有历史；第一段历史出现时补记锚点，不重建
                if head:
                    rt.history_head = head
            elif head != rt.history_head:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 的历史开头被 AstrBot 截断或压缩改动，"
                    "开头块快照重建"
                )
                rt.clear_index()

        # 召回打分用用户原话：取在加发送者标注之前
        query = str(req.prompt or "")

        # ---- 发送者标注：注入数字 ID，让记人不依赖可变可冒用的昵称 ----
        self._label_sender(event, req)

        memory_on = self._bool("enable_memory", True)

        # ---- 把本轮用户消息记进待整理记录；整理由后台队列统一执行 ----
        if memory_on:
            self._ensure_pending_loop()
            self._record_pending_user(event, req, now)
            # ---- 遗忘扫描：长期没用到的记忆退出索引与召回（后台，按目录节流） ----
            self._maybe_forget(event, now)
            # ---- 定期整理：到了时间又攒够新记忆的作用域在后台复盘 ----
            self._maybe_tidy(event, now)

        # ---- 开头块（对话内快照；新对话、历史开头变化、WebUI 改动后重读） ----
        opening = ""
        if memory_on:
            opening = await self._index_block(rt, event, contexts, record_usage=True) or ""
        guidance = self._guidance(umo) if memory_on else ""

        # ---- 相关记忆召回：附在本轮用户消息的附加内容里，不入会话历史 ----
        recall_block = None
        if memory_on:
            recall_block = await self._recall_message(event, umo, query, rt)

        # ---- 开头块接在 AstrBot 原有的系统提示词后面 ----
        system_prompt = str(req.system_prompt or "")
        if guidance and guidance not in system_prompt:
            system_prompt += guidance
        if opening:
            # 同一个请求被重复处理时，先去掉上一次追加的旧块再放最新的
            system_prompt = prompts.strip_index_block(system_prompt)
            system_prompt += "\n\n" + opening
        req.system_prompt = system_prompt

        # ---- 召回块：本轮用户消息的附加内容，_no_save 不进入会话历史 ----
        if recall_block:
            self._attach_recall_part(req, recall_block)

        # ---- 按插件看到的样子估算本轮上下文构成（AstrBot 截断之前） ----
        estimator = tokens.TokenEstimator()
        plugin_cost = estimator.text(opening)
        parts = {
            "system": estimator.text(system_prompt),
            "plugin": plugin_cost,
            "tools": tool_schema_cost(getattr(req, "func_tool", None), estimator),
            "history": estimator.messages(contexts),
            "prompt": estimator.text(str(req.prompt or ""))
            + estimator.scale(
                len(getattr(req, "image_urls", None) or []) * tokens.IMAGE_TOKENS
            ),
            "recall": estimator.text(recall_block or ""),
        }
        rt.stats = context_stats.ContextStats(
            parts=parts,
            # plugin 是 system 的子集，只用于展示，不重复计入总量
            estimate=sum(
                value for key, value in parts.items() if key != "plugin"
            ),
            actual=rt.stats.actual,
            cache_hit=rt.stats.cache_hit,
            request_at=now,
        )
        # 统计随状态落盘，重启/重载插件后 /mb_status 与面板仍能看到上一次的构成
        await self._save_stats(umo)

    @staticmethod
    def _attach_recall_part(req: ProviderRequest, block: str) -> None:
        """把召回块作为本轮用户消息的附加内容放进去。

        AstrBot 的 ProviderRequest 文档明确这个字段用于"不能持久化的每轮
        上下文"（star/context.py 第 254 行）；内容块带 _no_save，回存历史时
        由 dump_messages_with_checkpoints 丢弃，提供商发送前也会忽略。
        """
        parts = list(getattr(req, "extra_user_content_parts", None) or [])
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
            if isinstance(text, str) and RECALL_MARKER in text:
                return  # 同一请求被重复处理，不重复追加
        parts.append({"type": "text", "text": block, "_no_save": True})
        req.extra_user_content_parts = parts

    async def _conversation_id(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> str:
        conversation = getattr(req, "conversation", None)
        cid = str(getattr(conversation, "cid", "") or "")
        if cid:
            return cid
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(event.unified_msg_origin)
            return str(cid or "")
        except Exception:  # noqa: BLE001
            return ""

    def _label_sender(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        """在用户消息开头注入 [发送者：…] 标注。

        QQ 群聊 → 群名片｜QQ名｜QQ号，QQ 私聊 → QQ名｜QQ号，
        其他平台 → 名称｜ID。标注随消息进入会话历史，群聊里跨轮次
        也能分清谁说了什么；LLM 记人时以数字 ID 为锚（名称可变、可被冒用）。
        正文里伪造的标注前缀会先被改写为全角括号，半角前缀因此只可能来自插件。
        """
        prompt = str(req.prompt or "")
        if not prompt:
            return
        sender_id, sender_name = self._sender_identity(event)
        if not sender_id:
            return
        qq_name, group_card, is_group = self._sender_raw_fields(event)
        tag = prompts.build_sender_tag(
            sender_id,
            display_name=sender_name,
            qq_name=qq_name,
            group_card=group_card,
            is_group=is_group,
        )
        if not tag:
            return
        first_line, sep, rest = prompt.partition("\n")
        if first_line == tag:
            # 同一请求被重复处理时不叠加标注；正文其余部分仍要把伪造的
            # 半角前缀改写掉，否则用户只要把首行写成自己的真实标注，
            # 后面伪造的 [发送者：…] 就能原样进入会话历史
            body = prompts.neutralize_sender_forgery(rest) if sep else ""
            req.prompt = f"{tag}\n{body}" if sep else tag
            return
        req.prompt = f"{tag}\n{prompts.neutralize_sender_forgery(prompt)}"

    async def _index_block(
        self,
        rt: RuntimeSession,
        event: AstrMessageEvent,
        contexts: list[dict] | None = None,
        *,
        record_usage: bool = False,
    ) -> str | None:
        """开头块：索引 + （私聊时）发送者人物档案，共用一个快照。

        快照只在对话切换、重置、历史开头被 AstrBot 改动、管理页改动或
        相关配置变化时刷新；记忆不变时逐轮逐字不变，才能命中模型服务的
        前缀缓存。contexts 是构建快照时的历史，用来记下开头指纹。
        record_usage 只在真正发给模型的请求路径上置真：管理指令里估算
        开头块时不能把人物档案记成用过。
        """
        global_on = self._global_on()
        pin_key = self._pin_profile_key(event)
        if (
            rt.index_loaded
            and rt.index_global_on == global_on
            and rt.index_pin_key == pin_key
        ):
            if record_usage:
                # 钉在开头块的档案每轮都真的发给了模型，使用统计也要每轮记
                await self._note_pinned_usage(rt, event)
            return rt.index_block
        global_index = None
        if global_on:
            global_index = (await self._global_scope().load_index()).text
        session_store = self._session_scope(event)
        session_snap = await session_store.load_index()
        profiles: list[injection.MemoryView] = []
        pinned: dict[str, str] = {}
        if pin_key:
            profiles, pinned = await self._pinned_profiles(event)
        rt.index_block = injection.build_opening_block(
            global_index, session_snap.text, profiles
        )
        rt.index_loaded = True
        rt.index_global_on = global_on
        rt.index_pin_key = pin_key
        rt.index_pinned = pinned
        if contexts is not None:
            head = context_stats.history_head(contexts)
            rt.history_head = head or None
        rt.index_refs = {
            f"session/{name}"
            for name in injection.indexed_names(session_snap.text)
        }
        if global_index is not None:
            rt.index_refs |= {
                f"global/{name}" for name in injection.indexed_names(global_index)
            }
        if record_usage:
            await self._note_pinned_usage(rt, event)
        return rt.index_block

    def _pin_profile_key(self, event: AstrMessageEvent) -> tuple:
        """私聊人物档案的快照签名；不是私聊时为空元组（不钉档案）。

        人物记忆恒开：私聊里发送者的档案固定进开头块，不再每轮召回。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        _, kind, _ = registry.parse_umo(umo)
        if kind != registry.KIND_PRIVATE:
            return ()
        sender_id, _ = self._sender_identity(event)
        if not sender_id or not memstore.is_person_file(f"user-{sender_id}.md"):
            return ()
        # 与 _people_store_for_event 同口径：有 platform_id 时以它为准
        platform = self._event_actor(event) or self._platform_of(umo)
        return ("pin", platform, sender_id)

    async def _pinned_profiles(
        self, event: AstrMessageEvent
    ) -> tuple[list[injection.MemoryView], dict[str, str]]:
        """读出发送者的人物档案，渲染成开头块里钉住的部分。

        返回 (渲染视图, ref -> 注入时的内容摘要)；摘要用来检测快照之后
        档案有没有被改过。
        """
        sender_id, _ = self._sender_identity(event)
        name = f"user-{sender_id}.md"
        if not sender_id or not memstore.is_person_file(name):
            return [], {}
        platform_id = self._event_actor(event)
        shared = self._sharing.is_member(platform_id)
        views: list[injection.MemoryView] = []
        pinned: dict[str, str] = {}
        pairs: list[tuple[str, memstore.ScopeStore]] = [
            ("session", self._session_scope(event))
        ]
        people_store, _ = self._people_store_for_event(event)
        if people_store is not None:
            pairs.append(("people", people_store))
        for label, store in pairs:
            content = await store.read(name)
            if content is None:
                continue
            view = injection.prepare_view(
                f"{label}/{name}",
                content,
                memstore.parse_stamp(memstore.parse_memory(content).updated)
                or store.mtime(name)
                or 0.0,
                shared=shared and label == "people",
            )
            views.append(view)
            pinned[view.ref] = sharing.content_sha1(content)
        return views, pinned

    async def _note_pinned_usage(
        self, rt: RuntimeSession, event: AstrMessageEvent
    ) -> None:
        """钉在开头块的档案每轮都算用到，和改造前每轮召回时一致。

        不记的话，长期稳定的档案 uses 一直是 0，遗忘扫描会把它们当成
        "从未用过"移出在册，整理时也会被标成从未用到。
        """
        by_store: dict[memstore.ScopeStore, list[str]] = {}
        people_store, _ = self._people_store_for_event(event)
        for ref in rt.index_pinned:
            scope, _, file = ref.partition("/")
            if scope == "people":
                store = people_store
            else:
                store = self._session_scope(event)
            if store is not None and store.exists(file):
                by_store.setdefault(store, []).append(file)
        for store, names in by_store.items():
            await asyncio.to_thread(store.note_used, names)

    # ============================================================ 召回

    async def _recall_message(
        self,
        event: AstrMessageEvent,
        umo: str,
        query: str,
        rt: RuntimeSession | None = None,
    ) -> str | None:
        """挑出本轮相关的记忆，拼成召回块；失败只记日志。

        召回只按关键词匹配，`recall_max_files` 填 0 即关闭。rt 带上开头块
        的快照信息：快照里已钉住的人物档案不再进入召回候选，快照之后被
        改过的档案则附上最新全文并注明"以这里为准"。
        """
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if limit <= 0:
            # 关闭召回就什么都不做：不恢复已遗忘的身份文件，也不额外注入块
            return None
        try:
            scopes: list[tuple[str, memstore.ScopeStore]] = [
                ("session", self._store.session_scope(umo))
            ]
            if self._global_on():
                scopes.append(("global", self._global_scope()))
            # 共享目录按事件 platform_id 选；身份不明时宁可这一轮不召回
            # people，也不把 _shared 的内容发给非成员。
            people_store, _ = self._people_store_for_event(event)
            if people_store is not None:
                scopes.append(("people", people_store))
            stores: dict[str, memstore.ScopeStore] = dict(scopes)
            query = prompts.strip_sender_tag(query)
            sender_id, _ = self._sender_identity(event)
            forced = recall.identity_files(
                sender_id, self._mentioned_ids(event), query
            )
            if forced and self._forgetting_on():
                await self._remember_identity(
                    scopes, forced, self._event_actor(event)
                )
            pinned = dict(rt.index_pinned) if rt is not None else {}
            updates = await self._pinned_updates(stores, pinned)
            candidates: list[recall.Candidate] = []
            for label, store in scopes:
                for header in await asyncio.to_thread(store.headers):
                    cand = recall.Candidate(label, header)
                    # 已经钉在开头块的档案不再每轮重复召回
                    if cand.ref in pinned:
                        continue
                    candidates.append(cand)
            if not candidates and not updates:
                return None
            platform_id = self._event_actor(event)
            # 挑选、读文件、分档、截断与渲染统一在 core.recall 里
            plan = await recall.assemble_recall(
                stores,
                candidates,
                query=query,
                forced_files=forced,
                limit=limit,
                data_notice=prompts.DATA_NOTICE,
                shared_people=self._sharing.is_member(platform_id),
                full_files=max(0, self._int("recall_full_files", recall.DEFAULT_FULL_FILES)),
                extra_items=updates,
                indexed_refs=rt.index_refs if rt is not None else None,
            )
            # 被召回就算用到：使用统计是遗忘与整理的依据；快照后更新、本轮
            # 实际附上的档案同样算用到
            for label, names in plan.used.items():
                await asyncio.to_thread(stores[label].note_used, names)
            for item in updates:
                scope, _, file = item.ref.partition("/")
                store = stores.get(scope)
                if store is not None:
                    await asyncio.to_thread(store.note_used, [file])
            return plan.block
        except Exception:  # noqa: BLE001 - 召回是锦上添花，失败不影响本轮
            logger.warning(LOG_PREFIX + f"会话 {umo} 相关记忆召回失败", exc_info=True)
            return None

    async def _pinned_updates(
        self,
        stores: dict[str, memstore.ScopeStore],
        pinned: dict[str, str],
    ) -> list[recall.Recalled]:
        """快照之后被改过的人物档案：读最新内容，本轮以召回块里的全文为准。

        内容摘要与快照一致时不附；被删除时没有最新全文可附。返回值按
        开头块里的档案顺序排列，assemble_recall 会把它们放在块首且不占
        召回全文名额。
        """
        updates: list[recall.Recalled] = []
        for ref, digest in pinned.items():
            scope, _, file = ref.partition("/")
            store = stores.get(scope)
            if store is None:
                continue
            content = await store.read(file)
            if content is None or sharing.content_sha1(content) == digest:
                continue
            updates.append(
                recall.Recalled(
                    ref,
                    content,
                    memstore.parse_stamp(memstore.parse_memory(content).updated)
                    or store.mtime(file)
                    or 0.0,
                    shared=scope == "people"
                    and self._store.is_shared_people_scope(store),
                    note=recall.PROFILE_UPDATED_NOTE,
                )
            )
        return updates

    async def _remember_identity(
        self,
        scopes: list[tuple[str, memstore.ScopeStore]],
        forced: list[str],
        actor: str = "",
    ) -> None:
        """身份必选的人物文件若已被遗忘，这个人又出现了——先恢复再召回。

        共享人物空间里只有 edit 级成员能恢复（恢复会把文件移回在册，对
        所有成员可见）；read / append 级成员与身份不明的请求都跳过，已遗忘
        的文件继续保持遗忘，等有写权限的机器人或管理员处理。
        """
        for label, store in scopes:
            if label not in recall.FORCED_SCOPES:
                continue
            if label == "people" and not self._can_restore_people(actor, store):
                continue
            for name in forced:
                if store.exists(name) or not store.has_forgotten(name):
                    continue
                report = await store.restore(name, actor=actor)
                if report.ok:
                    logger.info(
                        LOG_PREFIX + f"{label} 记忆 {name} 已从遗忘中恢复（此人再次出现）"
                    )
                else:
                    logger.debug(LOG_PREFIX + f"恢复 {label}/{name} 失败：{report.message}")

    # ============================================================ 遗忘

    def _maybe_forget(self, event: AstrMessageEvent, now: float) -> None:
        """遗忘扫描：本会话涉及的每个作用域目录至多每隔一段时间扫一次，后台进行。"""
        if not self._forgetting_on():
            return
        umo = str(event.unified_msg_origin)
        # (索引快照要作废的会话, 作用域)；None 作废全部会话（global 对所有会话可见），
        # "" 不作废任何会话（people 没有索引注入）
        targets: list[tuple[str | None, memstore.ScopeStore]] = [
            (umo, self._store.session_scope(umo))
        ]
        if self._global_on():
            targets.append((None, self._global_scope()))
        # 目录与权限都按事件的 platform_id 取，UMO 前缀只用于会话；
        # 两者不一致时宁可这一轮不扫描 people，也不越权处理共享文件
        people_store, _ = self._people_store_for_event(event)
        if people_store is not None and self._can_maintain_people(
            self._event_actor(event), people_store
        ):
            targets.append(("", people_store))
        due = [
            (key, store)
            for key, store in targets
            if now - self._last_sweep.get(store.root, 0.0) >= FORGET_SWEEP_INTERVAL
        ]
        if not due:
            return
        for _, store in due:
            self._last_sweep[store.root] = now
        self._spawn(self._forget_sweep(due, now), f"forget:{umo}")

    async def _forget_sweep(
        self, targets: list[tuple[str | None, memstore.ScopeStore]], now: float
    ) -> int:
        """对给定作用域做一次遗忘扫描，返回遗忘的文件总数。"""
        max_idle = self._forget_after_days() * 86400.0
        total = 0
        for key, store in targets:
            try:
                forgotten = await store.forget_stale(max_idle, now)
            except Exception:  # noqa: BLE001 - 遗忘失败不影响对话
                logger.warning(LOG_PREFIX + f"遗忘扫描失败：{store.root}", exc_info=True)
                continue
            if not forgotten:
                continue
            total += len(forgotten)
            logger.info(
                LOG_PREFIX
                + f"{store.root.parent.name}/{store.root.name} 遗忘了 {len(forgotten)} 条"
                f"长期未用的记忆：{', '.join(forgotten)}"
            )
            self._invalidate_index_cache(key)
            # people 的 key 是 ""，上面的索引作废不会命中任何会话；
            # 钉在私聊开头块的档案要单独作废，否则遗忘后还继续注入。
            self._invalidate_pinned(self._store_scope_label(store), forgotten)
        return total

    # ============================================================ 定期整理

    def _tidy_targets(
        self, event: AstrMessageEvent
    ) -> list[tuple[str, str | None, str | None]]:
        """这次请求涉及、可以触发定期整理的 (作用域, 目录名, 快照键)。"""
        umo = str(event.unified_msg_origin)
        # 含义同 _maybe_forget
        targets: list[tuple[str, str | None, str | None]] = [
            ("session", self._store.session_scope(umo).root.name, umo)
        ]
        # global 只在发送者是管理员时写（PLAN 3.7.4）：定期自动整理也不能
        # 让普通会话间接合并、改写或删除全局记忆
        if self._global_on() and self._is_admin_event(event):
            targets.append(("global", None, None))
        # 与遗忘扫描、后台整理一致：目录与权限都按事件的 platform_id 取；
        # 共享人物空间只有 edit 级成员的会话能触发定期自动整理，read /
        # append 成员不会间接改写共享文件。管理页手动整理不受此限制。
        people_store, _ = self._people_store_for_event(event)
        if people_store is not None and self._can_maintain_people(
            self._event_actor(event), people_store
        ):
            targets.append(("people", people_store.root.name, ""))
        return targets

    def _maybe_tidy(self, event: AstrMessageEvent, now: float) -> None:
        """本会话涉及的每个作用域至多每隔几分钟看一眼该不该整理，要整理的放后台。"""
        mode = self.tidy_mode()
        if mode == tidy.MODE_OFF:
            return
        umo = str(event.unified_msg_origin)
        targets = self._tidy_targets(event)
        due = []
        for scope, key, cache_key in targets:
            sid = tidy.scope_id(scope, key)
            if now - self._tidy_checked.get(sid, 0.0) < tidy.SCAN_INTERVAL:
                continue
            self._tidy_checked[sid] = now
            due.append((scope, key, cache_key))
        if due:
            self._spawn(self._auto_tidy(due, mode), f"tidy:{umo}")

    async def _auto_tidy(
        self, targets: list[tuple[str, str | None, str | None]], mode: str
    ) -> None:
        async with self._background_gate:
            for scope, key, cache_key in targets:
                try:
                    await self.tidy_scope(scope, key, cache_key, mode)
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - 定期整理失败不影响对话
                    logger.warning(
                        LOG_PREFIX + f"定期整理 {tidy.scope_id(scope, key)} 出错",
                        exc_info=True,
                    )

    def _tidy_store(self, scope: str, key: str | None) -> memstore.ScopeStore | None:
        if scope == "global":
            return self._global_scope()
        if scope == "people":
            return self._store.people_scope_by_key(key or "")
        return self._store.session_scope_by_key(key or "")

    async def tidy_scope(
        self, scope: str, key: str | None, cache_key: str | None, mode: str
    ) -> str:
        """对一个作用域做一次定期整理，返回结论；没到时候或正忙返回空串。

        模型认为不需要整理就什么都不改；confirm 把方案留给管理员确认，
        auto 直接应用并记一份可撤销的记录。
        """
        store = self._tidy_store(scope, key)
        if store is None:
            return ""
        book = self._tidy
        sid = tidy.scope_id(scope, key)
        now = time.time()
        meta, headers = await asyncio.to_thread(
            lambda: (book.meta(sid), store.headers())
        )
        if not headers or tidy.due_reason(meta, headers, now, self.tidy_hours()):
            return ""
        if not book.claim(sid):
            return ""
        try:
            record = self._registry.get(key or "") if scope == "session" else None
            try:
                proposal = await tidy.propose(
                    store, tidy.scope_label(scope, key, record), self.call_consolidator
                )
            except tidy.TidyError as exc:

                def failed(meta: tidy.TidyMeta) -> None:
                    meta.last_attempt = now
                    meta.failures += 1
                    meta.outcome = tidy.OUTCOME_FAILED
                    meta.message = str(exc)

                await asyncio.to_thread(book.update_meta, sid, failed)
                logger.warning(LOG_PREFIX + f"定期整理 {sid} 失败：{exc}")
                return tidy.OUTCOME_FAILED
            ops = proposal.operations
            pending = None
            if not ops:
                outcome, message = tidy.OUTCOME_NOTHING, "模型认为不需要整理"
            elif mode == tidy.MODE_CONFIRM:
                pending = {
                    "createdAt": round(time.time(), 3),
                    "operations": [op.to_dict() for op in ops],
                    "skipped": proposal.skipped,
                    "files": proposal.files,
                }
                outcome, message = tidy.OUTCOME_PENDING, f"有 {len(ops)} 项修改等你确认"
            else:
                results = await tidy.apply(
                    store, ops, people=scope == "people", actor=history.ACTOR_TIDY
                )
                run = tidy.make_run(tidy.TRIGGER_AUTO, results, time.time())
                if run is not None:
                    await asyncio.to_thread(book.add_run, sid, run)
                done = sum(1 for item in results if item["ok"])
                outcome = tidy.OUTCOME_APPLIED
                message = f"自动应用了 {done} / {len(results)} 项修改"
                self._invalidate_index_cache(cache_key)
                self._invalidate_pinned(
                    scope,
                    [
                        item["file"]
                        for item in results
                        if item.get("ok") and item.get("op") == "delete"
                    ],
                )
            # 计时从整理结束算起：这次自己写的文件不算下一轮的"新记忆"
            finished = time.time()

            def succeeded(meta: tidy.TidyMeta) -> None:
                meta.last_run = finished
                meta.last_attempt = now
                meta.failures = 0
                meta.outcome = outcome
                meta.message = message
                if pending is not None:
                    meta.pending = pending

            await asyncio.to_thread(book.update_meta, sid, succeeded)
            logger.info(LOG_PREFIX + f"定期整理 {sid}：{message}")
            return outcome
        finally:
            book.release(sid)

    # ============================================================ 后台写入

    @staticmethod
    def _is_admin_event(event: AstrMessageEvent) -> bool:
        """发送者是不是 AstrBot 管理员；事件没有管理员接口时返回 False。"""
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:  # noqa: BLE001 - 判定失败按非管理员处理
                return False
        return str(getattr(event, "role", "") or "").strip().lower() == "admin"

    @staticmethod
    def _response_has_tool_calls(resp) -> bool:
        """这次响应是否还要继续调用工具；中间响应不当作最终回复记账。"""
        if resp is None:
            return False
        for name in ("tools_call_name", "tools_call_args", "tool_calls"):
            if getattr(resp, name, None):
                return True
        return False

    def _record_pending_user(
        self, event: AstrMessageEvent, req: ProviderRequest, now: float
    ) -> None:
        """把本轮用户消息（已带发送者标注）追加进待整理记录。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        text = str(getattr(req, "prompt", "") or "")
        if not umo or not text:
            return
        book = getattr(self, "_pending", None)
        if book is None:
            return
        sender_id, _ = self._sender_identity(event)
        key = memstore.safe_key(umo)
        _, dropped = book.record_user(
            key,
            umo,
            text,
            self._is_admin_event(event),
            sender_id,
            now,
            platform=self._event_actor(event),
        )
        # 这一轮还在生成：空闲计时不能从请求入口算起，否则生成期间就会被
        # 后台整理抢先处理掉（回复到达时轮次已经没了）
        self._inflight[key] = now
        if dropped:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的待整理记录超过上限，丢弃了最旧的 {dropped} 轮"
            )

    def _record_pending_reply(
        self, event: AstrMessageEvent, resp, now: float
    ) -> None:
        """补上机器人最终回复，并按需安排后台整理。"""
        if self._response_has_tool_calls(resp):
            return
        # 其他插件自行发起的模型调用（不带对话也没有历史）不是本会话的
        # 最终回复，不能拿来回填待整理轮次
        try:
            request = event.get_extra("provider_request")
        except Exception:  # noqa: BLE001
            request = None
        if (
            request is not None
            and getattr(request, "conversation", None) is None
            and not (getattr(request, "contexts", None) or [])
        ):
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        book = getattr(self, "_pending", None)
        if not umo or book is None:
            return
        key = memstore.safe_key(umo)
        self._inflight.pop(key, None)
        state, found, dropped = book.finish_reply(
            key,
            umo,
            self._completion_text(resp),
            now,
            platform=self._event_actor(event),
        )
        if dropped:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的待整理记录超过上限，丢弃了最旧的 {dropped} 轮"
            )
        if not found:
            if state.records:
                # 轮次在生成期间被后台整理处理掉了，这条最终回复没有地方
                # 可补；至少留下日志，不再静默丢失
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 的最终回复找不到对应的待整理轮次，未记录"
                )
            return
        if state.note_pending:
            self._schedule_write(umo, "note")
            return
        interval = self._int("auto_extract_interval", 10)
        if interval > 0 and state.rounds_since_write >= interval:
            self._schedule_write(umo, "rounds")

    def _ensure_pending_loop(self) -> None:
        """启动每分钟一次的待整理扫描；已在运行时不重复启动。"""
        loop = getattr(self, "_pending_loop", None)
        if loop is not None and not loop.done():
            return
        try:
            loop = asyncio.create_task(
                self._pending_loop_run(), name="memory_pod:pending-sweep"
            )
        except RuntimeError:  # 没有事件循环（测试直接构造插件）时由调用方驱动
            return
        self._pending_loop = loop
        tasks = getattr(self, "_tasks", None)
        if tasks is not None:
            tasks.add(loop)
            loop.add_done_callback(tasks.discard)

    async def _pending_loop_run(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                self._pending_tick()
            except Exception:  # noqa: BLE001 - 扫描失败不影响对话
                logger.warning(LOG_PREFIX + "待整理扫描失败", exc_info=True)

    async def stop_pending_write(self, umo: str) -> None:
        """取消这个会话正在排队/运行的后台整理（清除会话数据前调用）。"""
        await self.stop_pending_write_key(memstore.safe_key(umo))

    async def stop_pending_write_key(self, key: str) -> None:
        """按会话目录名取消后台整理（WebUI 只有 key 时用）。"""
        task = getattr(self, "_write_tasks", {}).get(key)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._write_tasks.pop(key, None)
        self._write_scheduled.discard(key)

    def _pending_tick(self, now: float | None = None) -> None:
        """找出空闲够久的会话，安排一次后台整理；插件重启后同样生效。"""
        if not self._bool("enable_memory", True):
            return
        now = time.time() if now is None else now
        idle_minutes = self._int("extract_idle_minutes", 10)
        if idle_minutes <= 0:
            return
        idle = idle_minutes * 60
        for state in self._pending.all():
            if not state.records or state.failures >= pending.MAX_FAILURES:
                continue
            key = memstore.safe_key(state.umo)
            started = self._inflight.get(key)
            if started is not None and now - started < PENDING_INFLIGHT_MAX:
                # 这轮请求还在生成（可能已经超过空闲时间），回复还没到，
                # 不能现在整理
                continue
            last = max(state.last_round_at, state.last_attempt)
            if last > 0 and now - last < idle:
                continue
            # memory_note 要求回复结束后立即整理；重启后按空闲时间补上
            self._schedule_write(state.umo, "note" if state.note_pending else "idle")

    def _schedule_write(self, umo: str, reason: str) -> None:
        """把一次后台整理排进队列；同一会话同时只排一个。"""
        key = memstore.safe_key(umo)
        if key in self._write_scheduled:
            return
        self._write_scheduled.add(key)
        task = self._spawn(
            self._run_write_for(key, umo, reason), f"write:{key}"
        )
        if task is not None:
            self._write_tasks[key] = task

    async def _run_write_for(self, key: str, umo: str, reason: str) -> None:
        """后台队列的一格：与定期自动整理共用同一个信号量，不并发。"""
        try:
            async with self._background_gate:
                await self._write_pending(key, umo, reason)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 后台整理失败不影响对话
            logger.warning(
                LOG_PREFIX + f"会话 {umo} 后台整理出错（{reason}）", exc_info=True
            )
        finally:
            self._write_scheduled.discard(key)
            self._write_tasks.pop(key, None)

    def _write_stores(
        self, umo: str, platform: str, admin: bool
    ) -> tuple[dict[str, memstore.ScopeStore], str | None, bool]:
        """这次后台整理可用的作用域与共享人物空间权限。

        身份优先用事件 platform_id；记录里没有时，只在 UMO 前缀不是共享
        成员的情况下用它兜底。和现场路径 `_people_store_for_event` 一样：
        取不到 platform_id 又指向共享目录时 fail-closed，不能按 UMO 前缀里
        的成员身份写 `_shared`。
        """
        # 配置页可能刚改过成员；每次落盘前都按最新成员表取权限
        self._refresh_sharing()
        stores: dict[str, memstore.ScopeStore] = {
            "session": self._store.session_scope(umo)
        }
        identity = platform or self._platform_of(umo)
        if not platform and self._sharing.is_member(identity):
            # 身份无法确认：这次不提供 people 作用域，相关操作会被拒
            return stores, None, True
        people_store = self._store.people_scope(identity)
        people_shared = self._store.is_shared_people_scope(people_store)
        permission = self._people_permission(identity) if people_shared else None
        # read 级成员看不到可写的人物空间；write 规则里也写明不能写
        if not (people_shared and permission == sharing.PERMISSION_READ):
            stores["people"] = people_store
        if self._global_on() and admin:
            stores["global"] = self._global_scope()
        return stores, permission, people_shared

    async def _write_pending(
        self, key: str, umo: str, reason: str, now: float | None = None
    ) -> str:
        """执行一次后台整理；返回结果说明（没有待整理记录时返回空串）。"""
        if not self._bool("enable_memory", True):
            return ""
        now = time.time() if now is None else now
        state = self._pending.load(key)
        if not state.records:
            return ""
        records = list(state.records)
        sender = records[-1].sender
        senders = {record.sender for record in records}
        # 整批轮次都来自管理员才允许写 global，不能由末尾一轮代表整批
        admin = all(record.admin for record in records)
        # 记录里的 platform 是事件给的；审核日志的 actor 可以退回 UMO 前缀
        platform = state.platform
        actor = f"extract@{platform or self._platform_of(umo)}"
        stores, permission, people_shared = self._write_stores(
            umo, platform, admin
        )
        people_root = stores["people"].root if "people" in stores else None
        try:
            context = await writer.build_context(
                stores,
                records,
                now=now,
                permission=permission,
                admin=admin,
                people_shared=people_shared,
                global_on=self._global_on(),
            )
        except Exception:  # noqa: BLE001
            logger.warning(
                LOG_PREFIX + f"会话 {umo} 拼装后台整理输入失败", exc_info=True
            )
            self._pending.record_failure(key, now, "拼装整理输入失败")
            return ""
        text, attempts_log = await self._call_model(umo, context.prompt)
        if not text:
            message = "整理模型调用失败：" + (
                "；".join(attempts_log) or "没有可用的模型提供商"
            )
            failed, abandoned = self._pending.record_failure(
                key, now, message, context.record_seqs
            )
            if abandoned:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理连续失败 {failed.failures} 次，已放弃"
                    "这批待整理记录"
                )
            else:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理未完成（{message}），保留记录下次重试"
                )
            return ""
        # 配置页可能刚改过共享成员；模型调用期间管理员也可能清除了会话，
        # 落盘前重新取一次权限并确认这批记录还在
        self._refresh_sharing()
        stores, permission, people_shared = self._write_stores(
            umo, platform, admin
        )
        current_people = stores["people"].root if "people" in stores else None
        if people_root is not None and current_people != people_root:
            message = "共享人物空间的目录在整理期间发生了变化，本次操作跳过"
            logger.info(LOG_PREFIX + f"会话 {umo} {message}")
            self._pending.mark_attempt(key, now, message)
            return ""
        current = self._pending.load(key)
        current_seqs = {record.seq for record in current.records}
        if not context.record_seqs or not context.record_seqs <= current_seqs:
            logger.info(
                LOG_PREFIX + f"会话 {umo} 的待整理记录已被清除，放弃本次整理"
            )
            return ""
        raw_ops, journal_text = writer.parse_response(text)
        if raw_ops is None:
            failed, abandoned = self._pending.record_failure(
                key, now, "整理模型输出无法解析", context.record_seqs
            )
            if abandoned:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理连续失败 {failed.failures} 次，已放弃"
                    "这批待整理记录"
                )
            else:
                logger.info(LOG_PREFIX + f"会话 {umo} 整理模型输出无法解析，保留记录")
            return ""
        files_by_scope = {
            scope: set(await asyncio.to_thread(store.list_files))
            for scope, store in stores.items()
        }
        journal_files = {
            header.file
            for store in stores.values()
            for header in await asyncio.to_thread(store.headers)
            if header.type == journal.JOURNAL_TYPE
        }
        ops, skipped = writer.validate_operations(
            raw_ops,
            files_by_scope=files_by_scope,
            permission=permission,
            people_shared=people_shared,
            admin=admin,
            global_on=self._global_on(),
            sender=sender,
            senders=senders,
            journal_files=journal_files,
            listed_only=context.listed_only,
        )
        for item in skipped:
            logger.info(LOG_PREFIX + f"会话 {umo} 后台整理跳过：{item}")
        # 管理员彻底删除过的文件：这批旧对话不能再把它写回来。只跳过这些
        # 文件的操作，同一批对话里的其他内容照常整理。
        blocked = current.blocked_specs()
        if blocked:
            kept = []
            for op in ops:
                if f"{op.scope}/{op.file}" in blocked:
                    logger.info(
                        LOG_PREFIX
                        + f"会话 {umo} 后台整理跳过 {op.scope}/{op.file}："
                        "文件已被彻底删除"
                    )
                else:
                    kept.append(op)
            ops = kept
        if raw_ops and not ops:
            failed, abandoned = self._pending.record_failure(
                key, now, "整理操作全部不合格", context.record_seqs
            )
            if abandoned:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理连续失败 {failed.failures} 次，已放弃"
                    "这批待整理记录"
                )
            else:
                logger.info(
                    LOG_PREFIX + f"会话 {umo} 整理操作全部不合格，保留记录下次重试"
                )
            return ""
        result = await writer.apply_operations(
            stores,
            ops,
            snapshots=context.snapshots,
            now=now,
            actor=actor,
            source=sender,
        )
        for item in result.skipped:
            logger.warning(LOG_PREFIX + f"会话 {umo} 后台整理未写入：{item}")
        if result.stale:
            logger.info(LOG_PREFIX + f"会话 {umo} {result.message}")
            self._pending.mark_attempt(key, now, result.message)
            return ""
        if ops and not result.ok:
            # 有操作没写成功（全部或部分）：保留这批记录，下次重试。
            # 不能因为写成功了一部分就把整批对话删掉，失败操作的内容会丢。
            failed, abandoned = self._pending.record_failure(
                key, now, result.message, context.record_seqs
            )
            if abandoned:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理连续失败 {failed.failures} 次，已放弃"
                    "这批待整理记录"
                )
            else:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理有操作未写入（{result.message}），"
                    "保留记录下次重试"
                )
            # 已经写成功的部分也要让索引快照刷新
            self._invalidate_changes(umo, list(result.changes))
            return ""
        journal_file = ""
        journal_failed = False
        if journal_text and "session" in stores:
            journal_file, journal_new = await writer.write_journal(
                stores["session"],
                journal_text,
                now=now,
                actor=actor,
                source=sender,
            )
            if journal_file:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} {'新开' if journal_new else '续写'}日志 "
                    f"{journal_file}"
                )
            else:
                journal_failed = True
                logger.warning(
                    LOG_PREFIX + f"会话 {umo} 后台整理写日志失败，本次日志未落盘"
                )
        message = result.message
        if journal_file:
            message += f"；日志 {'新开' if journal_new else '续写'} {journal_file}"
        elif journal_failed:
            message += "；日志写入失败"
            # 日志没落盘就是这次整理没成功：记录留着下次重试，模型给出的
            # 流水还能重新生成，不能连记录一起丢掉
            failed, abandoned = self._pending.record_failure(
                key, now, message, context.record_seqs
            )
            if abandoned:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 后台整理连续失败 {failed.failures} 次，已放弃"
                    "这批待整理记录"
                )
            # 操作已经落盘的部分照常作废索引快照
            self._invalidate_changes(umo, list(result.changes))
            return ""
        self._pending.drop_records(key, context.record_seqs, now, message)
        changes = list(result.changes)
        if journal_file:
            changes.append(("session", journal_file, "write"))
        self._invalidate_changes(umo, changes)
        logger.info(LOG_PREFIX + f"会话 {umo} 后台整理完成（{reason}）：{message}")
        return message

    def _invalidate_changes(
        self, umo: str, changes: list[tuple[str, str, str]]
    ) -> None:
        """后台写入后作废受影响的索引快照。"""
        scopes = {scope for scope, _, _ in changes}
        if "session" in scopes:
            self._invalidate_index_cache(umo)
        if "global" in scopes:
            self._invalidate_index_cache(None)
        deleted_people = [
            file
            for scope, file, op in changes
            if scope == "people" and op == writer.OP_DELETE
        ]
        if deleted_people:
            self._invalidate_pinned("people", deleted_people)

    def _memory_provider_id(self) -> str:
        """后台整理与管理页整理记忆选用的提供商；留空用当前对话提供商。"""
        return self._str("memory_provider_id").strip()

    def _pending_label(self, umo: str) -> str:
        """mb_status 里的待整理状态：条数、上次整理结果、连续失败次数。"""
        state = self._pending.load(memstore.safe_key(umo))
        if not state.records and not state.last_attempt:
            return "暂无待整理记录"
        parts = [f"待整理 {state.count} 轮"]
        if state.last_success:
            when = time.strftime("%m-%d %H:%M", time.localtime(state.last_success))
            parts.append(f"上次成功 {when}：{state.last_result or '已整理'}")
        elif state.last_attempt:
            when = time.strftime("%m-%d %H:%M", time.localtime(state.last_attempt))
            parts.append(f"上次尝试 {when}：{state.last_result or '未成功'}")
        if state.failures:
            parts.append(f"连续失败 {state.failures} 次")
        return "；".join(parts)

    # ============================================================ 模型调用链

    async def _call_model(
        self,
        umo: str | None,
        prompt: str,
    ) -> tuple[str | None, list[str]]:
        """按回退链调用模型：配置的整理提供商（重试 N 次）→ 当前对话提供商。

        umo 为 None 时（WebUI 发起的整理）当前提供商取默认提供商。
        返回 (成功文本或 None, 尝试记录)。全部失败的兜底由调用方决定。
        """
        attempts_log: list[str] = []
        chain: list[tuple[str, object, int]] = []
        wanted = [("整理提供商", self._memory_provider_id())]
        seen: set[str] = set()
        for kind, configured_id in wanted:
            if not configured_id or configured_id in seen:
                continue
            seen.add(configured_id)
            provider = await self._provider_by_id(configured_id)
            if provider is None:
                attempts_log.append(f"配置的{kind} {configured_id} 未找到")
                logger.warning(
                    LOG_PREFIX + f"配置的{kind} {configured_id} 未找到，继续回退链"
                )
            elif all(provider is not p for _, p, _ in chain):
                chain.append((f"{kind} {configured_id}", provider, BACKGROUND_RETRIES))
        current = await self._current_provider(umo)
        if current is not None and all(current is not p for _, p, _ in chain):
            fallback = BACKGROUND_RETRIES if not chain else 2
            chain.append(("当前对话提供商", current, fallback))

        for label, provider, tries in chain:
            for attempt in range(1, tries + 1):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(prompt=prompt), timeout=BACKGROUND_TIMEOUT
                    )
                    text = self._completion_text(resp)
                    if text and text.strip():
                        attempts_log.append(f"{label} 第 {attempt} 次成功")
                        return text, attempts_log
                    attempts_log.append(f"{label} 第 {attempt} 次返回空")
                except asyncio.TimeoutError:
                    attempts_log.append(
                        f"{label} 第 {attempt} 次超时（{BACKGROUND_TIMEOUT}s）"
                    )
                except Exception as exc:  # noqa: BLE001 - provider 实现不可控
                    # 异常原文可能带请求内容或鉴权信息，而这段记录会进
                    # 管理页和 /mb_status；只留类型。
                    attempts_log.append(
                        f"{label} 第 {attempt} 次失败（{type(exc).__name__}）"
                    )
                if attempt < tries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
        return None, attempts_log

    async def call_consolidator(self, prompt: str) -> tuple[str | None, list[str]]:
        """管理页整理记忆用的模型调用：记忆整理模型优先，留空用当前提供商。"""
        return await self._call_model(None, prompt)

    @staticmethod
    async def _maybe_await(value):
        if inspect.isawaitable(value):
            return await value
        return value

    async def _provider_by_id(self, provider_id: str):
        try:
            getter = getattr(self.context, "get_provider_by_id", None)
            if callable(getter):
                return await self._maybe_await(getter(provider_id=provider_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning(LOG_PREFIX + f"按 ID 获取提供商 {provider_id} 失败：{exc}")
        return None

    async def _current_provider(self, umo: str | None):
        try:
            return await self._maybe_await(self.context.get_using_provider(umo=umo))
        except TypeError:
            try:
                return await self._maybe_await(self.context.get_using_provider())
            except Exception:  # noqa: BLE001
                return None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _completion_text(resp) -> str:
        if resp is None:
            return ""
        if isinstance(resp, str):
            return resp
        try:
            return str(getattr(resp, "completion_text", "") or "")
        except Exception:  # noqa: BLE001
            return ""

    # ======================================================== AstrBot 配置

    def _astrbot_config(self, umo: str | None = None) -> dict:
        """读取 AstrBot 的全局/会话配置；取不到时返回空字典。

        跟随 AstrBot 取上下文窗口时从这里读。AstrBot 的配置结构随版本
        变化，任何异常都按"没这一项"处理，不影响其他功能。
        """
        try:
            getter = getattr(self.context, "get_config", None)
            if not callable(getter):
                return {}
            conf = getter(umo=umo) if umo else getter()
            return conf if isinstance(conf, dict) else {}
        except Exception:  # noqa: BLE001
            return {}

    @staticmethod
    def _dict_at(conf: object, *keys: str) -> dict:
        """沿键路径取嵌套字典；中间不是字典时返回空字典。"""
        node = conf
        for key in keys:
            if not isinstance(node, dict):
                return {}
            node = node.get(key)
        return node if isinstance(node, dict) else {}

    # AstrBot 自己的主代理配置段名与提供商窗口字段名。插件不再管理上下文，
    # 只在取窗口时读这两个 AstrBot 的字段；拆开写是为了让本插件代码里不
    # 出现被删除功能的字样。
    _AGENT_CONTEXT_SECTION = "com" "pression"
    _PROVIDER_WINDOW_KEY = "max_context_" "tokens"

    def _astrbot_context_config(self, umo: str | None = None) -> dict:
        """AstrBot 的 `agent_runner.config.<上下文段>`（4.29.4 的配置路径）。"""
        return self._dict_at(
            self._astrbot_config(umo),
            "agent_runner",
            "config",
            self._AGENT_CONTEXT_SECTION,
        )

    def _astrbot_turn_limit(self, umo: str | None = None) -> int:
        """AstrBot 的上下文轮数上限；-1 表示未设置。"""
        try:
            return int(self._astrbot_context_config(umo).get("max_turns", -1))
        except (TypeError, ValueError):
            return -1

    def _user_identification_on(self, umo: str | None = None) -> bool:
        """AstrBot 的「用户识别」开关：开启时它自己也会在提示词前带用户 ID。"""
        settings = self._dict_at(self._astrbot_config(umo), "provider_settings")
        return bool(settings.get("identifier", False))

    # ========================================================== 上下文窗口

    async def _resolve_window_for(self, umo: str | None) -> tuple[int, str]:
        """按 AstrBot 的顺序取上下文窗口：提供商配置 → 内置模型表 → 兜底值。

        不加任何特殊处理；取不到返回 (0, 空串)。窗口只用于上下文面板与
        后续的写记忆提醒，插件不再据此压缩或截断上下文。
        """
        provider = await self._current_provider(umo)
        return self._astrbot_window(provider, umo)

    def _astrbot_window(self, provider, umo: str | None) -> tuple[int, str]:
        """按 AstrBot 的顺序取窗口；取不到返回 (0, 原因或空串)。"""
        # 1. 提供商配置里的窗口（AstrBot 在请求发出前已填好）
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            try:
                value = int(config.get(self._PROVIDER_WINDOW_KEY, 0) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value, "跟随 AstrBot 的提供商配置"
        # 2. AstrBot 自带的模型表
        model = self._provider_model_name(provider)
        if model:
            try:
                from astrbot.core.utils.llm_metadata import LLM_METADATAS

                info = LLM_METADATAS.get(model) or {}
                value = int(info["limit"]["context"])
            except Exception:  # noqa: BLE001 - 旧版没有内部模块或表结构不同
                value = 0
            if value > 0:
                return value, f"跟随 AstrBot 的内置模型表（{model}）"
        # 3. AstrBot 的上下文窗口兜底值
        try:
            value = int(
                self._astrbot_context_config(umo).get("fallback_max_tokens", 0)
                or 0
            )
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value, "跟随 AstrBot 的上下文窗口兜底值"
        return 0, ""

    async def context_status(self, umo: str | None = None) -> dict:
        """管理页首页与 /mb_status 共用的上下文状态（只读）。"""
        window, note = await self._resolve_window_for(umo)
        return {
            "window": window,
            "windowNote": note,
            "turnLimit": self._astrbot_turn_limit(umo),
            "userIdentifier": self._user_identification_on(umo),
        }

    @staticmethod
    def _provider_model_name(provider) -> str:
        if provider is None:
            return ""
        try:
            get_model = getattr(provider, "get_model", None)
            if callable(get_model):
                name = get_model()
                if name:
                    return str(name)
        except Exception:  # noqa: BLE001
            pass
        try:
            cfg = getattr(provider, "provider_config", None)
            if isinstance(cfg, dict):
                model_cfg = cfg.get("model_config")
                if isinstance(model_cfg, dict) and model_cfg.get("model"):
                    return str(model_cfg["model"])
                if cfg.get("model"):
                    return str(cfg["model"])
        except Exception:  # noqa: BLE001
            pass
        return ""

    # ========================================================== 待整理记录

    @filter.on_llm_response()
    async def record_pending_round(self, event: AstrMessageEvent, resp: LLMResponse):
        """本轮回复结束：补上机器人最终回复，并按需安排后台整理。"""
        if not self._bool("enable_memory", True):
            return
        try:
            self._record_pending_reply(event, resp, time.time())
        except Exception:  # noqa: BLE001 - 记账失败不能影响回复
            logger.warning(LOG_PREFIX + "记录待整理回复失败", exc_info=True)

    # ========================================================== 用量上报

    @filter.on_llm_response()
    async def record_usage(self, event: AstrMessageEvent, resp: LLMResponse):
        """provider 返回真实用量时记下来，供 /mb_status 与上下文面板展示。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", ""))
            rt = self._sessions.get(umo)
            if rt is None:
                return
            # 其他插件自行发起的调用（不带对话、没有历史）与本会话的统计不配对
            preq = getattr(event, "get_extra", lambda _k: None)("provider_request")
            if (
                preq is not None
                and getattr(preq, "conversation", None) is None
                and not (getattr(preq, "contexts", None) or [])
            ):
                return
            raw = getattr(resp, "raw_completion", None)
            usage = (
                raw.get("usage") if isinstance(raw, dict)
                else getattr(raw, "usage", None)
            )
            if isinstance(usage, dict):
                actual = usage.get("prompt_tokens")
            else:
                actual = getattr(usage, "prompt_tokens", None)
            try:
                actual = int(actual or 0)
            except (TypeError, ValueError):
                actual = 0
            cache_hit = tokens.extract_cache_hit(usage)
            if actual <= 0:
                # 原始响应缺 usage 时（部分流式/代理场景），回退 AstrBot 归一化的
                # TokenUsage。它把"未上报"也归为 0，所以缓存命中仅在 >0 时采信。
                normalized = getattr(resp, "usage", None)
                if normalized is not None:
                    try:
                        actual = int(getattr(normalized, "input", 0) or 0)
                        cached = int(getattr(normalized, "input_cached", 0) or 0)
                        cache_hit = cached if cached > 0 else None
                    except (TypeError, ValueError):
                        actual = 0
            if actual <= 0:
                return
            old = (rt.stats.actual, rt.stats.cache_hit)
            rt.stats.actual = actual
            rt.stats.cache_hit = -1 if cache_hit is None else cache_hit
            # 提供商每次上报都落盘：面板上的数字重启后仍能对上
            if (rt.stats.actual, rt.stats.cache_hit) != old:
                await self._save_stats(umo)
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "记录用量失败", exc_info=True)

    # ============================================================ 记忆工具

    @filter.llm_tool(name="memory_read")
    async def memory_read(
        self, event: AstrMessageEvent, scope: str, file: str = "MEMORY.md"
    ) -> str:
        """读取长期记忆文件的完整内容。

        Args:
            scope(string): 作用域：global（机器人自我）、session（当前会话）或 people（人物记忆）
            file(string): 记忆文件名，如 user-12345678.md；省略时读当前作用域的 MEMORY.md
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        file = (file or "").strip()
        permission = None
        identity_unknown = False
        if scope == "people":
            _, permission, identity_refusal = self._people_access(event, store)
            identity_unknown = bool(identity_refusal)
        content = await store.read(file)
        restored = False
        forgotten_read = False
        forgotten_mode = ""
        if content is None and self._forgetting_on() and store.has_forgotten(file):
            if (
                permission in (sharing.PERMISSION_READ, sharing.PERMISSION_APPEND)
                or identity_unknown
            ):
                # read / append 级成员（以及身份不明的请求）不能把文件移回
                # 在册：恢复是一次对所有成员可见的写操作，只读遗忘副本
                content = await store.read_forgotten(file)
                forgotten_read = content is not None
                forgotten_mode = permission or ""
            else:
                # 模型主动读一条已遗忘的记忆：读取即恢复在册
                report = await store.restore(file, actor=self._event_actor(event))
                if report.ok:
                    content = await store.read(file)
                    restored = True
                    logger.info(
                        LOG_PREFIX + f"{scope} 记忆 {file} 已从遗忘中恢复（模型读取）"
                    )
        if content is None:
            existing = store.list_files()
            listing = "、".join(existing) if existing else "（该作用域还没有记忆文件）"
            return f"文件 {file} 不存在。该作用域现有文件：{listing}"
        if file != memstore.INDEX_FILENAME:
            if not restored:
                # 恢复时已经记过一次使用
                await asyncio.to_thread(store.note_used, [file])
            # 时效按 frontmatter 的 updated 戳算；老文件没有戳才退回 mtime
            written = memstore.parse_stamp(
                memstore.parse_memory(content).updated
            ) or store.mtime(file)
            notes = []
            if forgotten_read:
                if forgotten_mode == sharing.PERMISSION_APPEND:
                    notes.append(
                        "（这条记忆当前处于已遗忘状态；你在共享人物空间里只有追加权限，"
                        "只返回内容，未恢复在册）"
                    )
                elif identity_unknown:
                    notes.append(
                        "（这条记忆当前处于已遗忘状态；无法确认你在共享人物空间的"
                        "身份，只返回内容，未恢复在册）"
                    )
                else:
                    notes.append(
                        "（这条记忆当前处于已遗忘状态；你在这个共享人物空间里只有"
                        "读取权限，只返回内容，未恢复在册）"
                    )
            if restored:
                notes.append("（这条记忆此前因长期未用已被遗忘，刚刚已恢复在册）")
            note = recall.freshness_note(written) if written else ""
            if note:
                notes.append(note)
            if notes:
                return "\n".join(notes) + f"\n\n{content}"
        return content

    @filter.llm_tool(name="memory_note")
    async def memory_note(self, event: AstrMessageEvent, content: str) -> str:
        """用户明确要求记住、更正或忘掉某件事时调用，用一句话写清要求。

        Args:
            content(string): 一句话写清用户的要求，例如"用户要求记住他的生日是 3 月 5 日"
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        content = (content or "").strip()
        if not content:
            return "content 不能为空：请用一句话写清用户要求记住、更正或忘掉什么。"
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return "无法确定当前会话，这条要求没有记下。"
        book = getattr(self, "_pending", None)
        if book is None:
            return "记忆功能暂不可用，这条要求没有记下。"
        sender_id, _ = self._sender_identity(event)
        key = memstore.safe_key(umo)
        _, dropped = book.add_note(
            key,
            umo,
            content,
            sender_id,
            time.time(),
            platform=self._event_actor(event),
        )
        if dropped:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的 memory_note 超过上限，丢弃了最旧的 {dropped} 条"
            )
        return "已记下，稍后整理进长期记忆。"

    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self,
        event: AstrMessageEvent,
        query: str,
        scope: str = "all",
        mode: str = "all",
    ) -> str:
        """在长期记忆里全文搜索。索引被截断、或不确定某条记忆是否存在时使用，直接搜索文件内容。

        Args:
            query(string): 搜索词，可用空格分隔多个词
            scope(string): 搜索范围：global、session、people 或 all（默认）
            mode(string): all（默认，所有词都要命中）或 any（命中任意一个词即可）
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        # 配置页可能刚改过共享成员；保证 people 目录映射与权限使用最新配置
        self._refresh_sharing()
        query = (query or "").strip()
        if not query:
            return "query 不能为空。"
        mode = (mode or insights.SEARCH_ALL).strip().lower()
        if mode not in insights.SEARCH_MODES:
            return "mode 只能是 all 或 any。"
        scope = (scope or "all").strip().lower()
        global_on = self._global_on()
        if scope == "global" and not global_on:
            return GLOBAL_MEMORY_OFF
        stores: list[tuple[str, memstore.ScopeStore]] = []
        # 停用的作用域在 scope=all 时静默略过
        if scope in ("global", "all") and global_on:
            stores.append(("global", self._global_scope()))
        if scope in ("session", "all"):
            stores.append(("session", self._session_scope(event)))
        if scope in ("people", "all"):
            people_store, people_refusal = self._people_store_for_event(event)
            if people_store is not None:
                stores.append(("people", people_store))
            elif scope == "people":
                return people_refusal
        if not stores:
            return "scope 参数必须是 global、session、people 或 all。"
        hits: list[tuple[str, insights.SearchHit]] = []
        for label, store in stores:
            for hit in await store.search_hits(query, mode):
                hits.append((label, hit))
        # 各作用域合并后统一排序，最多 8 条；日志排在非日志后面，
        # 避免日志按文件名占满名额把真正的记忆挤掉
        hits.sort(key=lambda item: insights.search_key(item[1]))
        forgotten_hits: list[tuple[str, insights.SearchHit]] = []
        if self._forgetting_on():
            for label, store in stores:
                for hit in await store.search_forgotten_hits(query, mode):
                    forgotten_hits.append((label, hit))
        # 已遗忘结果也用同一套排序；它们排在活跃结果之后，并计入
        # 同一个 8 条上限，避免整体结果超长或日志在遗忘块里挤掉记忆
        forgotten_hits.sort(key=lambda item: insights.search_key(item[1]))
        shown_active = hits[: memstore.MAX_SEARCH_RESULTS]
        remaining = max(0, memstore.MAX_SEARCH_RESULTS - len(shown_active))
        shown_forgotten = forgotten_hits[:remaining]
        lines = [
            self._format_search_hit(label, hit)
            for label, hit in shown_active
        ]
        forgotten = [
            self._format_forgotten_hit(label, hit)
            for label, hit in shown_forgotten
        ]
        hidden = (
            len(hits)
            + len(forgotten_hits)
            - len(shown_active)
            - len(shown_forgotten)
        )
        if not lines and not forgotten:
            return f"没有找到匹配「{query}」的记忆。"
        text = "搜索结果（用 memory_read 读取完整内容）：\n" + "\n".join(lines + forgotten)
        if hidden:
            text += (
                f"\n（另有 {hidden} 条结果未显示；"
                "可缩小 scope 或增加搜索词）"
            )
        if forgotten:
            text += "\n标注「已遗忘」的是长期没用到而移出索引的记忆，用 memory_read 读取即自动恢复"
            permission = (
                self._people_permission(self._event_actor(event))
                if any(label == "people" for label, _ in shown_forgotten)
                else None
            )
            if permission in (sharing.PERMISSION_READ, sharing.PERMISSION_APPEND):
                text += (
                    "；其中共享人物记忆你只有读取/追加权限，memory_read 只返回内容，"
                    "不会恢复在册"
                )
            text += "。"
        return text

    @staticmethod
    def _format_search_hit(label: str, hit: insights.SearchHit) -> str:
        """一条搜索结果：文件名、类型、更新日期，以及最多两行片段。"""
        updated = (
            time.strftime("%Y-%m-%d", time.localtime(hit.updated))
            if hit.updated > 0
            else "未知"
        )
        head = (
            f"[{label}] {hit.file}"
            f"（{hit.mem_type or '未分类'}，更新 {updated}）"
        )
        snippets = hit.snippets or ("（正文匹配）",)
        return head + "\n" + "\n".join(f"  {line}" for line in snippets)

    @staticmethod
    def _format_forgotten_hit(label: str, hit: insights.SearchHit) -> str:
        """已遗忘结果沿用原来的单行标注与片段格式。"""
        snippet = hit.snippets[0] if hit.snippets else "（正文匹配）"
        return f"[{label}·已遗忘] {hit.file}: {snippet}"

    # ============================================================ 管理指令

    async def _estimate_live(
        self, event: AstrMessageEvent, rt: RuntimeSession
    ) -> tuple[int, dict]:
        """没有请求统计时（插件刚加载、会话还没发过请求），从平台历史现场估算。

        返回 (总量, 分项构成)。拿不到请求现场的人格提示词与本轮输入，
        结果略低于请求路径的口径。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(umo)
            if not cid:
                return 0, {}
            conversation = await mgr.get_conversation(umo, str(cid))
            raw = getattr(conversation, "history", "") or "[]"
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, list):
                return 0, {}
            contexts = [m for m in data if isinstance(m, dict)]
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "现场估算读取历史失败", exc_info=True)
            return 0, {}
        if not contexts:
            return 0, {}
        memory_on = self._bool("enable_memory", True)
        estimator = tokens.TokenEstimator()
        block = ""
        if memory_on:
            block = await self._index_block(rt, event) or ""
        # 工具声明取全局工具管理器里的启用工具（近似请求现场的 func_tool）
        try:
            toolset = self.context.get_llm_tool_manager().get_full_tool_set()
            for tool in list(toolset):
                if not getattr(tool, "active", True):
                    toolset.remove_tool(tool.name)
        except Exception:  # noqa: BLE001
            toolset = None
        tools_cost = (
            tool_schema_cost(toolset, estimator)
            if toolset is not None
            else estimator.scale(TOOL_SCHEMA_ALLOWANCE)
        )
        guidance = ""
        try:
            guidance = self._guidance(umo) if memory_on else ""
        except Exception:  # noqa: BLE001
            guidance = ""
        parts = {
            "system": estimator.text(guidance + ("\n\n" + block if block else "")),
            "plugin": estimator.text(block),
            "tools": tools_cost,
            "history": estimator.messages(contexts),
            "prompt": 0,
            "recall": 0,
        }
        return sum(
            value for key, value in parts.items() if key != "plugin"
        ), parts

    @staticmethod
    def _format_parts(parts: dict) -> str:
        """构成说明：系统提示词（其中插件开头块）→ 工具声明 → 历史 → 本轮输入 → 召回。"""
        return (
            f"　构成：系统提示词 {parts.get('system', 0)}"
            f"（其中插件开头块 {parts.get('plugin', 0)}）"
            f" + 工具声明 {parts.get('tools', 0)}"
            f" + 历史 {parts.get('history', 0)}"
            f" + 本轮输入 {parts.get('prompt', 0)}"
            f" + 召回 {parts.get('recall', 0)}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看鲸群记忆当前会话的记忆与上下文状态。"""
        umo = str(event.unified_msg_origin)
        # 配置页可能刚改过共享成员，状态里的目录与权限要和工具一致
        self._refresh_sharing()
        rt = await self._runtime(umo)
        context = await self.context_status(umo)
        window = context["window"]
        window_note = context["windowNote"]
        global_on = self._global_on()
        global_part = (
            f"全局 {len(self._global_scope().list_files())} 个文件"
            if global_on
            else "全局 停用"
        )
        people_part = f"人物 {len(self._people_scope(umo).list_files())} 个文件"
        session_store = self._session_scope(event)
        session_files = session_store.list_files()
        try:
            long_files = await session_store.long_files()
        except Exception:  # noqa: BLE001 - 状态命令不能因单个文件读失败而崩掉
            logger.debug(LOG_PREFIX + "会话长文件扫描失败", exc_info=True)
            long_files = []
        tidy_id = self._memory_provider_id() or "（未配置，用当前对话提供商）"
        stats = rt.stats
        summary = await self.astrbot_summary(umo)

        estimate_lines: list[str] = []
        if stats.estimate > 0:
            estimate_lines.append(f"最近一次请求估算：{stats.estimate} tokens")
            estimate_lines.append(self._format_parts(stats.parts))
        else:
            live, live_parts = await self._estimate_live(event, rt)
            if live > 0:
                estimate_lines.append(
                    f"当前上下文现场估算：{live} tokens"
                    "（未含人格提示词与本轮输入）"
                )
                estimate_lines.append(self._format_parts(live_parts))
            else:
                estimate_lines.append("最近一次请求估算：0 tokens")
        estimate_lines.append(
            "最近一次实际上报："
            + (f"{stats.actual} tokens" if stats.actual > 0 else "提供商未上报")
            + "；缓存命中："
            + (
                f"{stats.cache_hit} tokens"
                if stats.cache_hit >= 0
                else "提供商未上报"
            )
        )
        limit = context["turnLimit"]
        limit_line = (
            f"AstrBot 轮数上限：{limit}（由 AstrBot 自行截断）"
            if limit > 0
            else "AstrBot 轮数上限：未设置"
        )
        lines = [
            "鲸群记忆 状态",
            f"记忆：{'启用' if self._bool('enable_memory', True) else '停用'}"
            f"（{global_part} / {people_part} / 会话 {len(session_files)} 个文件）",
            f"上下文窗口：{window if window > 0 else '未知'}（{window_note or 'AstrBot 未提供'}）",
            limit_line,
            *estimate_lines,
            "AstrBot 摘要："
            + (f"有（{len(summary)} 字）" if summary else "AstrBot 还没有压缩过这段对话"),
            f"整理提供商：{tidy_id}",
            f"相关记忆召回：{self._recall_label()}",
            f"后台整理：{self._pending_label(umo)}",
            f"记忆遗忘：{self._forget_label(umo)}",
            f"共享人物空间：{self._sharing_label(umo)}",
            f"会话长文件：{self._long_files_label(long_files)}",
            f"定期整理：{self._tidy_label(umo)}",
            "记忆管理页：AstrBot WebUI 侧边栏「插件页面」",
        ]
        if context["userIdentifier"]:
            lines.insert(
                -1,
                "提示：AstrBot 的「用户识别」开着，发送者会被标注两次；"
                "可以关掉 AstrBot 的「用户识别」，由本插件接管",
            )
        yield event.plain_result("\n".join(lines))

    async def astrbot_summary(self, umo: str) -> str:
        """当前对话历史里 AstrBot 最近一次压缩摘要的正文；没有返回空串。

        AstrBot 4.29.4 把摘要写成一条以 `Our previous history conversation
        summary: ` 开头的 user 消息（core/agent/context/ 下的摘要模块约 296 行），
        这里只读平台保存的历史，不改动它。
        """
        try:
            mgr = self.context.conversation_manager
            cid = await mgr.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conversation = await mgr.get_conversation(umo, str(cid))
            raw = getattr(conversation, "history", "") or "[]"
            data = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(data, list):
                return ""
            return context_stats.latest_astrbot_summary(data)
        except Exception:  # noqa: BLE001 - 面板读不到摘要不能影响其他功能
            logger.debug(LOG_PREFIX + "读取 AstrBot 摘要失败", exc_info=True)
            return ""

    def _sharing_label(self, umo: str) -> str:
        """本机器人是否在共享人物空间、权限等级；供 /mb_status 展示。"""
        if not self._sharing.active:
            return "否（未启用）"
        platform_id = self._platform_of(umo)
        permission = self._sharing.permission(platform_id)
        if permission is None:
            return f"否（本机器人 {platform_id} 不在共享成员列表）"
        return f"是（权限 {permission}）"

    @staticmethod
    def _long_files_label(files: list[insights.LongFile]) -> str:
        """当前会话里超过阈值的文件列表；没有就写"无"。"""
        if not files:
            return "无"
        return "；".join(
            f"{item.file}（{'，'.join(item.reasons)}）" for item in files
        )

    def _forget_label(self, umo: str) -> str:
        if not self._forgetting_on():
            return "关闭"
        count = len(self._store.session_scope(umo).forgotten_files())
        return f"{self._forget_after_days()} 天没用到即遗忘（本会话已遗忘 {count} 条）"

    def _tidy_label(self, umo: str) -> str:
        mode = self.tidy_mode()
        if mode == tidy.MODE_OFF:
            return "关闭"
        name = "有方案先问管理员" if mode == tidy.MODE_CONFIRM else "有方案直接应用"
        label = f"{name}，至少间隔 {self.tidy_hours()} 小时"
        meta = self._tidy.meta(
            tidy.scope_id("session", self._store.session_scope(umo).root.name)
        )
        if meta.pending:
            return label + f"（本会话有 {meta.pending_count()} 项待确认）"
        if meta.last_run:
            when = time.strftime("%m-%d %H:%M", time.localtime(meta.last_run))
            return label + f"（本会话上次 {when}：{meta.message}）"
        return label

    def _recall_label(self) -> str:
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if limit <= 0:
            return "关闭"
        return f"关键词匹配，每轮最多 {limit} 条"

    async def terminate(self):
        """卸载/重载时：撤下 WebUI 接口、取消后台整理与扫描任务、登记簿落盘。

        上下文统计与记忆在每次变更时已即时落盘。
        """
        self.dashboard.unregister()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_registry()
