"""鲸群记忆 —— 为 AstrBot 重新设计的长期记忆系统。

核心理念：真相在磁盘，上下文只带索引，细节按需取回；上下文的截断与压缩
完全交给 AstrBot 的「上下文管理策略」，插件不再改动对话历史。

分层约定：
    main.py     —— 平台适配层（本文件）：钩子、LLM 工具、指令、provider 调用
    web_api.py  —— WebUI 记忆管理页的后端接口（页面在 pages/memory）
    core/       —— 纯逻辑：三层记忆库、索引、召回、token 估算、上下文统计、
                   会话登记、记忆整理

注入位置（PLAN 3.8.1，依据 AstrBot 4.29.4 源码）：
- 记忆规范（静态指令）接在 req.system_prompt 后面。AstrBot 把它作为开头
  唯一的 system 消息（tool_loop_agent_runner 第 324-328 行），按轮数截断
  时开头 system 消息整体保留（truncator _split_system_rest），回存历史时
  第一条 system 消息被跳过（agent_sub_stages/internal.py 第 517-520 行）。
- 开头块（索引、私聊档案）与每轮召回块都放进 req.extra_user_content_parts，
  带 _no_save 标记：它们随本轮用户消息一起进入请求（entities.assemble_context），
  属于最新的一轮，截断与压缩都保留；AstrBot 的摘要压缩只把最旧轮次发给
  摘要模型（round_utils.split_into_rounds），所以索引与档案不会被压缩进
  摘要；回存历史时按内容块丢弃（message.model_dump +
  dump_messages_with_checkpoints），不会在平台历史里逐轮累积。
  开头块不放系统提示词，是因为摘要压缩会把开头 system 消息当成最旧一轮
  一起喂给摘要模型，摘要又会被 AstrBot 持久化；把索引与档案留在最新一轮
  才能同时满足"不被截断/摘要"和"不进历史"。

兼容性立场：只依赖 @filter.on_llm_request() / @filter.on_llm_response() /
@filter.llm_tool() 等公开插件接口、不 monkey-patch。跟随 AstrBot 取上下文
窗口时要读 AstrBot 内部配置和模型表，相关 import 全部包在 try 里：
取不到就跳过那一级，插件照常加载。
"""

from __future__ import annotations

import asyncio
import hashlib
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
    memstore,
    prompts,
    recall,
    registry,
    tidy,
    tokens,
    writer,
)
from .web_api import MemoryDashboard

PLUGIN_NAME = "astrbot_plugin_memory_pod"
LOG_PREFIX = "[memory_pod] "

# 管理页手动整理调用模型失败时的尝试次数与单次超时（不再有配置项）。
CONSOLIDATE_RETRIES = 2
CONSOLIDATE_TIMEOUT = 120
# 拿不到本轮工具声明时（结构异常等）估算工具 schema 的兜底余量；
# 正常路径按 req.func_tool 的实际 schema 序列化后实测。
TOOL_SCHEMA_ALLOWANCE = 800
SCOPE_ARG_ERROR = "scope 参数必须是 board、notebook 或 whisper。"
# 会话运行时缓存上限（LRU 淘汰，仅内存快照，磁盘状态不受影响）。
MAX_RUNTIME_SESSIONS = 256
# 遗忘扫描：每个作用域目录至多每隔这么久扫一次（秒）；默认多久没用到算遗忘（天）
FORGET_SWEEP_INTERVAL = 6 * 3600
FORGET_DEFAULT_DAYS = 90
# 召回块的 _no_save 数据声明；同一条请求被重复处理时据此避免重复追加。
RECALL_MARKER = recall.RECALL_BLOCK_OPEN


def _content_sha1(content: str) -> str:
    """记忆正文的内容摘要；开头块快照用它检测档案有没有被改过。"""
    return hashlib.sha1((content or "").encode("utf-8")).hexdigest()


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
    # 私聊人物档案的快照签名（(pin, 平台, 发送者)）；身份变了就重建
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


class MemoryPodPlugin(Star):
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
        # 管理页手动整理的记录与进行中标记
        self._tidy = tidy.TidyBook(data_dir / "tidy")
        self.dashboard = MemoryDashboard(self)
        self.dashboard.register()

        logger.info(LOG_PREFIX + f"已加载：数据目录 {data_dir}")

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
        （留言板与小本本的变更对所有会话可见）。"""
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

    def runtime_for_key(self, key: str) -> tuple[str, RuntimeSession] | None:
        """按会话目录名找到已加载的运行时（WebUI 改动后同步内存态用）。"""
        for umo, rt in self._sessions.items():
            if memstore.safe_key(umo) == key:
                return umo, rt
        return None

    def invalidate_scope(self, scope: str, key: str | None = None) -> None:
        """管理页改动记忆后作废相应的开头块快照。

        留言板与小本本对所有会话可见，作废全部快照；悄悄话只作废对应会话。
        属于其他会话的悄悄话不对应任何运行时，也就不需要作废。
        """
        if scope in (memstore.SCOPE_BOARD, memstore.SCOPE_NOTEBOOK):
            self._invalidate_index_cache(None)
            return
        if scope == memstore.SCOPE_WHISPER and key:
            found = self.runtime_for_key(key)
            if found is not None:
                self._invalidate_index_cache(found[0])

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

    def _forgetting_on(self) -> bool:
        """记忆遗忘开关，默认关闭。开启后长期没被召回或读取的记忆移出索引与
        召回（文件保留在 forgotten/ 下），再被用到时自动恢复；关闭时什么都不动，
        使用统计照常记录。"""
        return self._bool("enable_forgetting", False)

    def _forget_after_days(self) -> int:
        return max(1, self._int("forget_after_days", FORGET_DEFAULT_DAYS))

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

    def _guidance(self, umo: str = "") -> str:
        return prompts.memory_guidance()

    @staticmethod
    def _platform_of(umo: str) -> str:
        platform, _, _ = registry.parse_umo(umo)
        return platform or str(umo or "").split(":", 1)[0] or "unknown"

    def _board_scope(self) -> memstore.ScopeStore:
        return self._store.board_scope()

    def _notebook_scope(self, event: AstrMessageEvent) -> memstore.ScopeStore:
        """事件所在机器人的小本本：身份优先用事件的 platform_id。

        platform_id 取不到时退回 UMO 前缀；AstrBot 正常构造时两者一致。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        platform = self._event_actor(event) or self._platform_of(umo)
        return self._store.notebook_scope(platform)

    def _whisper_scope(self, umo: str) -> memstore.ScopeStore:
        """当前这段对话的悄悄话；目录名是会话目录名（safe_key(UMO)）。"""
        return self._store.whisper_scope(memstore.safe_key(umo))

    def _scope_for(
        self, event: AstrMessageEvent, scope: str
    ) -> memstore.ScopeStore | None:
        scope = (scope or "").strip().lower()
        if scope == memstore.SCOPE_BOARD:
            return self._board_scope()
        if scope == memstore.SCOPE_NOTEBOOK:
            return self._notebook_scope(event)
        if scope == memstore.SCOPE_WHISPER:
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            return self._whisper_scope(umo) if umo else None
        return None

    def _tool_store(
        self, event: AstrMessageEvent, scope: str
    ) -> tuple[memstore.ScopeStore | None, str, str]:
        """记忆工具的作用域解析：返回 (作用域, 归一化名称, 拒绝原因)。

        悄悄话只解析到当前事件所在的这段对话，工具参数里没有会话键可填，
        所以模型碰不到别的对话的悄悄话（PLAN 3.8.3 第 1 条）。
        """
        scope = (scope or "").strip().lower()
        store = self._scope_for(event, scope)
        if store is None:
            return None, scope, SCOPE_ARG_ERROR
        return store, scope, ""

    # ================================================================ 钩子

    @filter.on_llm_request()
    async def inject_memory(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """请求前的介入点：记忆规范接进系统提示词，开头块与召回块作为本轮
        用户消息的附加内容。AstrBot 之后如何截断、压缩上下文由它自己决定。"""
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

        if memory_on:
            # ---- 遗忘扫描：长期没用到的记忆退出索引与召回（后台，按目录节流） ----
            self._maybe_forget(event, now)

        # ---- 开头块（对话内快照；新对话、历史开头变化、WebUI 改动后重读） ----
        opening = ""
        if memory_on:
            opening = await self._index_block(rt, event, contexts, record_usage=True) or ""
        guidance = self._guidance(umo) if memory_on else ""

        # ---- 相关记忆召回：附在本轮用户消息的附加内容里，不入会话历史 ----
        recall_block = None
        if memory_on:
            recall_block = await self._recall_message(event, umo, query, rt)

        # ---- 记忆规范留在系统提示词；开头块与召回块都作为本轮用户消息的
        # 附加内容。AstrBot 的摘要压缩会把开头 system 消息当成最旧的一轮
        # 一起喂给摘要模型（round_utils.split_into_rounds），只有最新一轮的
        # 附加内容既不会被截断，也不会被压缩进摘要。 ----
        system_prompt = str(req.system_prompt or "")
        if guidance and guidance not in system_prompt:
            system_prompt += guidance
        req.system_prompt = system_prompt

        # ---- 开头块与召回块：_no_save 附加内容，不进会话历史 ----
        if opening:
            self._attach_temp_part(req, opening, prompts.INDEX_BLOCK_OPEN)
        if recall_block:
            self._attach_temp_part(req, recall_block, RECALL_MARKER)

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
    def _attach_temp_part(req: ProviderRequest, block: str, marker: str) -> None:
        """把注入块作为本轮用户消息的附加内容放进去。

        AstrBot 的 ProviderRequest 文档明确这个字段用于"不能持久化的每轮
        上下文"（star/context.py 第 254 行）；内容块带 _no_save，回存历史时
        由 dump_messages_with_checkpoints 丢弃，提供商发送前也会忽略。
        同一个请求被重复处理时按 marker 去重，不重复追加。
        """
        parts = list(getattr(req, "extra_user_content_parts", None) or [])
        for part in parts:
            text = part.get("text") if isinstance(part, dict) else getattr(part, "text", "")
            if isinstance(text, str) and marker in text:
                return
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
        """开头块：留言板全文、小本本与当前悄悄话的索引、私聊人物档案。

        快照只在对话切换、重置、历史开头被 AstrBot 改动、管理页改动或
        身份变化时刷新；记忆不变时逐轮逐字不变，才能命中模型服务的
        前缀缓存。contexts 是构建快照时的历史，用来记下开头指纹。
        record_usage 只在真正发给模型的请求路径上置真：管理指令里估算
        开头块时不能把人物档案记成用过。

        群聊里只放这个群自己的悄悄话索引，别的对话（包括任何人的私聊）
        的悄悄话不会出现在候选里（PLAN 3.8.3 第 1 条）。
        """
        pin_key = self._pin_profile_key(event)
        if rt.index_loaded and rt.index_pin_key == pin_key:
            if record_usage:
                # 钉在开头块的档案每轮都真的发给了模型，使用统计也要每轮记
                await self._note_pinned_usage(rt, event)
            return rt.index_block
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        board_text = await self._board_text()
        notebook_snap = await self._notebook_scope(event).load_index()
        whisper_snap = await self._whisper_scope(umo).load_index()
        profiles: list[injection.MemoryView] = []
        pinned: dict[str, str] = {}
        if pin_key:
            profiles, pinned = await self._pinned_profiles(event)
        rt.index_block = injection.build_opening_block(
            board_text, notebook_snap.text, whisper_snap.text, profiles
        )
        rt.index_loaded = True
        rt.index_pin_key = pin_key
        rt.index_pinned = pinned
        if contexts is not None:
            head = context_stats.history_head(contexts)
            rt.history_head = head or None
        rt.index_refs = {
            f"{memstore.SCOPE_NOTEBOOK}/{name}"
            for name in injection.indexed_names(notebook_snap.text)
        } | {
            f"{memstore.SCOPE_WHISPER}/{name}"
            for name in injection.indexed_names(whisper_snap.text)
        }
        if record_usage:
            await self._note_pinned_usage(rt, event)
        return rt.index_block

    async def _board_text(self) -> str:
        """留言板开头段：全部告示从新到旧全文注入，超出 2000 字只列标题。"""
        store = self._board_scope()
        headers = await asyncio.to_thread(store.headers)
        notices: list[injection.BoardNotice] = []
        for header in headers:
            content = await store.read(header.file)
            if content is None:
                continue
            doc = memstore.parse_memory(content)
            notices.append(
                injection.BoardNotice(
                    file=header.file,
                    title=doc.description or header.description,
                    body=doc.body,
                    signature=doc.signature,
                    written=header.updated,
                )
            )
        return injection.render_board_notices(notices)

    def _pin_profile_key(self, event: AstrMessageEvent) -> tuple:
        """私聊人物档案的快照签名；不是私聊时为空元组（不钉档案）。

        私聊里发送者的档案固定进开头块，不再每轮召回；群聊里不钉任何
        人的悄悄话档案（PLAN 3.8.3）。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        _, kind, _ = registry.parse_umo(umo)
        if kind != registry.KIND_PRIVATE:
            return ()
        sender_id, _ = self._sender_identity(event)
        if not sender_id or not memstore.is_person_file(f"user-{sender_id}.md"):
            return ()
        platform = self._event_actor(event) or self._platform_of(umo)
        return ("pin", platform, sender_id)

    async def _pinned_profiles(
        self, event: AstrMessageEvent
    ) -> tuple[list[injection.MemoryView], dict[str, str]]:
        """读出发送者的小本本档案与悄悄话档案，渲染成开头块里钉住的部分。

        返回 (渲染视图, ref -> 注入时的内容摘要)；摘要用来检测快照之后
        档案有没有被改过。
        """
        sender_id, _ = self._sender_identity(event)
        name = f"user-{sender_id}.md"
        if not sender_id or not memstore.is_person_file(name):
            return [], {}
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        views: list[injection.MemoryView] = []
        pinned: dict[str, str] = {}
        pairs: list[tuple[str, memstore.ScopeStore]] = [
            (memstore.SCOPE_NOTEBOOK, self._notebook_scope(event)),
            (memstore.SCOPE_WHISPER, self._whisper_scope(umo)),
        ]
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
            )
            views.append(view)
            pinned[view.ref] = _content_sha1(content)
        return views, pinned

    async def _note_pinned_usage(
        self, rt: RuntimeSession, event: AstrMessageEvent
    ) -> None:
        """钉在开头块的档案每轮都算用到。

        不记的话，长期稳定的档案 uses 一直是 0，遗忘扫描会把它们当成
        "从未用过"移出在册，整理时也会被标成从未用到。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        by_store: dict[memstore.ScopeStore, list[str]] = {}
        for ref in rt.index_pinned:
            scope, _, file = ref.partition("/")
            if scope == memstore.SCOPE_NOTEBOOK:
                store = self._notebook_scope(event)
            else:
                store = self._whisper_scope(umo)
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
            # 只召回小本本与当前这段对话的悄悄话；别的对话的悄悄话不在候选里
            scopes: list[tuple[str, memstore.ScopeStore]] = [
                (memstore.SCOPE_NOTEBOOK, self._notebook_scope(event)),
                (memstore.SCOPE_WHISPER, self._whisper_scope(umo)),
            ]
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
            # 挑选、读文件、分档、截断与渲染统一在 core.recall 里
            plan = await recall.assemble_recall(
                stores,
                candidates,
                query=query,
                forced_files=forced,
                limit=limit,
                data_notice=prompts.DATA_NOTICE,
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
            if content is None or _content_sha1(content) == digest:
                continue
            updates.append(
                recall.Recalled(
                    ref,
                    content,
                    memstore.parse_stamp(memstore.parse_memory(content).updated)
                    or store.mtime(file)
                    or 0.0,
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

        只恢复小本本里的档案：它们本来是这台机器人在所有会话共享的记忆。
        """
        for label, store in scopes:
            if label not in recall.FORCED_SCOPES:
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
        # (作用域, 会话目录名, store)；留言板与小本本对所有会话可见，
        # 遗忘后作废全部开头块；悄悄话只作废这一段对话。
        targets: list[tuple[str, str, memstore.ScopeStore]] = [
            (memstore.SCOPE_WHISPER, memstore.safe_key(umo), self._whisper_scope(umo)),
            (
                memstore.SCOPE_NOTEBOOK,
                "",
                self._notebook_scope(event),
            ),
            (memstore.SCOPE_BOARD, "", self._board_scope()),
        ]
        due = [
            (scope, key, store)
            for scope, key, store in targets
            if now - self._last_sweep.get(store.root, 0.0) >= FORGET_SWEEP_INTERVAL
        ]
        if not due:
            return
        for _, _, store in due:
            self._last_sweep[store.root] = now
        self._spawn(self._forget_sweep(due, now), f"forget:{umo}")

    async def _forget_sweep(
        self, targets: list[tuple[str, str, memstore.ScopeStore]], now: float
    ) -> int:
        """对给定作用域做一次遗忘扫描，返回遗忘的文件总数。"""
        max_idle = self._forget_after_days() * 86400.0
        total = 0
        for scope, key, store in targets:
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
                + f"{store.layer}/{store.root.name} 遗忘了 {len(forgotten)} 条"
                f"长期未用的记忆：{', '.join(forgotten)}"
            )
            self.invalidate_scope(scope, key or None)
            # 钉在私聊开头块的档案要单独作废，否则遗忘后还继续注入。
            self._invalidate_pinned(scope, forgotten)
        return total

    # ============================================================ 角色判定

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

    # ============================================================ 模型调用链

    async def _call_model(
        self,
        prompt: str,
    ) -> tuple[str | None, list[str]]:
        """用 AstrBot 的默认对话模型做一次管理页整理（PLAN 3.8.4）。

        返回 (成功文本或 None, 尝试记录)。不再支持单独配置整理模型。
        """
        attempts_log: list[str] = []
        provider = await self._current_provider(None)
        if provider is None:
            return None, ["没有可用的默认对话模型"]
        for attempt in range(1, CONSOLIDATE_RETRIES + 1):
            try:
                resp = await asyncio.wait_for(
                    provider.text_chat(prompt=prompt), timeout=CONSOLIDATE_TIMEOUT
                )
                text = self._completion_text(resp)
                if text and text.strip():
                    attempts_log.append(f"默认对话模型第 {attempt} 次成功")
                    return text, attempts_log
                attempts_log.append(f"默认对话模型第 {attempt} 次返回空")
            except asyncio.TimeoutError:
                attempts_log.append(
                    f"默认对话模型第 {attempt} 次超时（{CONSOLIDATE_TIMEOUT}s）"
                )
            except Exception as exc:  # noqa: BLE001 - provider 实现不可控
                # 异常原文可能带请求内容或鉴权信息，而这段记录会进管理页；
                # 只留类型。
                attempts_log.append(
                    f"默认对话模型第 {attempt} 次失败（{type(exc).__name__}）"
                )
            if attempt < CONSOLIDATE_RETRIES:
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
        return None, attempts_log

    async def call_consolidator(self, prompt: str) -> tuple[str | None, list[str]]:
        """管理页整理记忆用的模型调用：使用 AstrBot 的默认对话模型。"""
        return await self._call_model(prompt)

    @staticmethod
    async def _maybe_await(value):
        if inspect.isawaitable(value):
            return await value
        return value

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
            scope(string): 作用域：board（留言板）、notebook（这台机器人的小本本）或 whisper（当前这段对话的悄悄话）
            file(string): 记忆文件名，如 user-12345678.md；省略时读当前作用域的 MEMORY.md
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        file = (file or "").strip()
        content = await store.read(file)
        restored = False
        forgotten_read = False
        if content is None and self._forgetting_on() and store.has_forgotten(file):
            if self._can_restore(event, scope, file):
                # 模型主动读一条已遗忘的记忆：读取即恢复在册
                report = await store.restore(file, actor=self._event_actor(event))
                if report.ok:
                    content = await store.read(file)
                    restored = True
                    logger.info(
                        LOG_PREFIX + f"{scope} 记忆 {file} 已从遗忘中恢复（模型读取）"
                    )
            else:
                # 别人的档案或留言板：只读遗忘副本，不替管理员恢复在册
                content = await store.read_forgotten(file)
                forgotten_read = content is not None
        if content is None:
            existing = store.list_files()
            listing = "、".join(existing) if existing else "（该作用域还没有记忆文件）"
            return f"文件 {file} 不存在。该作用域现有文件：{listing}"
        if not restored:
            # 恢复时已经记过一次使用
            await asyncio.to_thread(store.note_used, [file])
        # 时效按 frontmatter 的 updated 戳算；老文件没有戳才退回 mtime
        written = memstore.parse_stamp(
            memstore.parse_memory(content).updated
        ) or store.mtime(file)
        notes = []
        if forgotten_read:
            notes.append(
                "（这条记忆当前处于已遗忘状态；只有本人、管理员或留言板管理员"
                "能让它恢复在册，现在只返回内容）"
            )
        if restored:
            notes.append("（这条记忆此前因长期未用已被遗忘，刚刚已恢复在册）")
        note = recall.freshness_note(written) if written else ""
        if note:
            notes.append(note)
        if notes:
            return "\n".join(notes) + f"\n\n{content}"
        return content

    def _can_restore(
        self, event: AstrMessageEvent, scope: str, file: str
    ) -> bool:
        """读取已遗忘文件时能否顺手恢复在册。

        留言板是管理员的内容；人物档案只有本人或管理员能动；其他记忆
        读取即恢复。规则与写入层一致（PLAN 3.8.3 第 2、3 条）。
        """
        admin = self._is_admin_event(event)
        if scope == memstore.SCOPE_BOARD:
            return admin
        subject = writer.person_id(file)
        if subject:
            sender_id, _ = self._sender_identity(event)
            return admin or (bool(sender_id) and sender_id == subject)
        return True

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
            scope(string): 搜索范围：board、notebook、whisper 或 all（默认）
            mode(string): all（默认，所有词都要命中）或 any（命中任意一个词即可）
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        query = (query or "").strip()
        if not query:
            return "query 不能为空。"
        mode = (mode or insights.SEARCH_ALL).strip().lower()
        if mode not in insights.SEARCH_MODES:
            return "mode 只能是 all 或 any。"
        scope = (scope or "all").strip().lower()
        if scope != "all" and scope not in memstore.SCOPES:
            return "scope 参数必须是 board、notebook、whisper 或 all。"
        stores: list[tuple[str, memstore.ScopeStore]] = []
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if scope in (memstore.SCOPE_BOARD, "all"):
            stores.append((memstore.SCOPE_BOARD, self._board_scope()))
        if scope in (memstore.SCOPE_NOTEBOOK, "all"):
            stores.append((memstore.SCOPE_NOTEBOOK, self._notebook_scope(event)))
        # 悄悄话只搜当前这段对话，永远不提供别的会话的目录
        if scope in (memstore.SCOPE_WHISPER, "all") and umo:
            stores.append((memstore.SCOPE_WHISPER, self._whisper_scope(umo)))
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
        """构成说明：系统提示词 → 插件开头块 → 工具声明 → 历史 → 本轮输入 → 召回。"""
        return (
            f"　构成：系统提示词 {parts.get('system', 0)}"
            f" + 插件开头块 {parts.get('plugin', 0)}"
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
        rt = await self._runtime(umo)
        context = await self.context_status(umo)
        window = context["window"]
        window_note = context["windowNote"]
        board_files = self._board_scope().list_files()
        notebook_store = self._notebook_scope(event)
        notebook_files = notebook_store.list_files()
        whisper_store = self._whisper_scope(umo)
        whisper_files = whisper_store.list_files()
        try:
            long_files = await notebook_store.long_files()
        except Exception:  # noqa: BLE001 - 状态命令不能因单个文件读失败而崩掉
            logger.debug(LOG_PREFIX + "小本本长文件扫描失败", exc_info=True)
            long_files = []
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
            f"（留言板 {len(board_files)} 个文件 / 小本本 {len(notebook_files)} 个文件"
            f" / 本对话悄悄话 {len(whisper_files)} 个文件）",
            f"上下文窗口：{window if window > 0 else '未知'}（{window_note or 'AstrBot 未提供'}）",
            limit_line,
            *estimate_lines,
            "AstrBot 摘要："
            + (f"有（{len(summary)} 字）" if summary else "AstrBot 还没有压缩过这段对话"),
            "记忆整理：管理页手动发起，使用 AstrBot 的默认对话模型",
            f"相关记忆召回：{self._recall_label()}",
            f"记忆遗忘：{self._forget_label(umo)}",
            f"小本本长文件：{self._long_files_label(long_files)}",
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
        count = len(self._whisper_scope(umo).forgotten_files())
        return f"{self._forget_after_days()} 天没用到即遗忘（本对话悄悄话已遗忘 {count} 条）"

    def _recall_label(self) -> str:
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if limit <= 0:
            return "关闭"
        return f"关键词匹配，每轮最多 {limit} 条"

    async def terminate(self):
        """卸载/重载时：撤下 WebUI 接口、取消后台扫描任务、登记簿落盘。

        上下文统计与记忆在每次变更时已即时落盘。
        """
        self.dashboard.unregister()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_registry()
