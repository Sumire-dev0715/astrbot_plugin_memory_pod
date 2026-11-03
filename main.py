"""Memory Beyond —— 为 AstrBot 重新设计的长期记忆与上下文压缩系统。

核心理念：真相在磁盘，上下文只带索引，细节按需取回，压缩后从磁盘重新注入。

分层约定：
    main.py     —— 平台适配层（本文件）：钩子、LLM 工具、指令、provider 调用
    web_api.py  —— WebUI 记忆管理页的后端接口（页面在 pages/memory）
    core/       —— 纯逻辑：记忆库、索引、召回、token 估算、轮次切分、
                   压缩状态机、会话登记、压缩归档、记忆整理、工具循环保护

兼容性立场：只依赖 @filter.on_llm_request() / @filter.on_llm_response() /
@filter.llm_tool() 等公开插件接口，不 import AstrBot 内部实现、不 monkey-patch。
压缩采用抢先策略：在钩子里把 req.contexts 压到低于内置压缩器 0.82 的阈值，
这一轮开头内置压缩器无事可做，同时留在下游当安全网。但模型一次回复里可以连续
调用很多轮工具，结果一轮轮涨进上下文，开头的判断管不到，所以另在每个工具返回
之后盯一次（core/loopguard.py）：快越过阈值就把较早的工具结果换成占位说明。
AstrBot 每轮会把实际发送的消息列表回存为会话历史，
因此注入块一律带 _no_save 声明不入史；压缩后被摘要覆盖的原文会随回存从
平台历史卸下，由持久化的摘要继续代表（详见 core/compress.py 模块注释）。
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
    archive,
    compress,
    history,
    injection,
    insights,
    journal,
    loopguard,
    memstore,
    prompts,
    recall,
    registry,
    sharing,
    tidy,
    tokens,
)
from .core.turns import split_leading_system, split_turns
from .web_api import MemoryDashboard

PLUGIN_NAME = "astrbot_plugin_memory_beyond"
LOG_PREFIX = "[memory_beyond] "

# 摘要提供商回退链中，当前对话提供商作为兜底时的尝试次数
# （作为首选时按配置的 summary_retry_count）。
FALLBACK_ATTEMPTS = 2
# 拿不到本轮工具声明时（结构异常等）估算工具 schema 的兜底余量；
# 正常路径按 req.func_tool 的实际 schema 序列化后实测。
TOOL_SCHEMA_ALLOWANCE = 800
# 应急裁剪时留给正文消息的最小预算。
MIN_TAIL_BUDGET = 1000
# 全局记忆关闭时对 scope=global 工具调用的固定回执；工具说明是静态的
# 仍会列出 global，回执要把模型引导回 session。
GLOBAL_MEMORY_OFF = "全局记忆已在插件配置中停用；请改用 session 作用域。"
PEOPLE_MEMORY_OFF = "人物记忆已在插件配置中停用；关于人的信息请写 session 作用域。"
PEOPLE_FILE_RULE = "people 作用域一人一个文件，文件名必须是 user-<数字ID>.md。"
SCOPE_ARG_ERROR = "scope 参数必须是 global、session 或 people。"
# 会话运行时缓存上限（LRU 淘汰，仅内存快照，磁盘状态不受影响）。
MAX_RUNTIME_SESSIONS = 256
# 模型挑选召回记忆时的单次超时：挑选在回复之前完成，等太久不如退回关键词
RECALL_SELECT_TIMEOUT = 15
# 遗忘扫描：每个作用域目录至多每隔这么久扫一次（秒）；默认多久没用到算遗忘（天）
FORGET_SWEEP_INTERVAL = 6 * 3600
FORGET_DEFAULT_DAYS = 90
# 工具循环守卫在事件额外字段里的键（on_llm_request 建好，工具钩子取用）
LOOP_GUARD_KEY = "memory_beyond_loop_guard"


def _hook_or_noop(name: str):
    """取一个较新才有的钩子装饰器；旧版 AstrBot 没有时退化为空装饰器，
    插件照常加载，只是对应功能不生效。"""
    hook = getattr(filter, name, None)
    if hook is not None:
        return hook
    return lambda *args, **kwargs: (lambda func: func)


_on_agent_begin = _hook_or_noop("on_agent_begin")
_on_tool_respond = _hook_or_noop("on_llm_tool_respond")
LOOP_HOOKS_READY = all(
    hasattr(filter, name) for name in ("on_agent_begin", "on_llm_tool_respond")
)

# 自动探测模型上下文窗口的对照表：子串匹配，先命中先用。
# 探测只是兜底，README 中明确建议手动配置 max_context_tokens。
MODEL_WINDOWS: list[tuple[str, int]] = [
    ("gpt-5", 400_000),
    ("gpt-4.1", 1_000_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("o4", 200_000),
    ("o3", 200_000),
    ("o1", 200_000),
    ("claude", 200_000),
    ("gemini-2", 1_000_000),
    ("gemini", 1_000_000),
    ("deepseek", 128_000),
    ("glm", 128_000),
    ("qwen3", 128_000),
    ("qwen-max", 128_000),
    ("qwen-plus", 128_000),
    ("qwen", 32_000),
    ("kimi", 128_000),
    ("moonshot", 128_000),
    ("grok-4", 256_000),
    ("grok", 128_000),
    ("doubao", 128_000),
]


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
    """一个会话的内存态：压缩状态 + 索引快照缓存。

    索引在对话开始时读盘一次、之后复用快照（prompt caching 按前缀匹配，
    开头的注入内容一变，整段历史的缓存全部失效）。对话中途模型新写的
    记忆只落盘、不刷新快照——需要时由每轮的相关记忆召回带上全文。
    压缩发生时清空快照、从磁盘重新加载——这是记忆扛过压缩的核心机制；
    对话切换、管理员在 WebUI 改动记忆时同样刷新。
    """

    state: compress.SessionState
    index_block: str | None = None
    index_loaded: bool = False
    # 快照构建时的全局记忆开关；配置热改后据此判定快照失效
    index_global_on: bool = False
    # 私聊人物档案的快照签名（(pin, 平台, 发送者)）；配置或身份变了就重建
    index_pin_key: tuple = ()
    # 开头块里已钉住的人物档案：ref -> 注入时的内容摘要（检测快照后的修改）
    index_pinned: dict[str, str] = field(default_factory=dict)
    # 这些档案在快照里没有头尾截断，可以记"看过"
    index_pinned_full: set[str] = field(default_factory=set)
    # 当前快照是否已经在真实请求里记过共享"看过"（管理指令估算时不算）
    index_seen_noted: bool = False
    # 开头块索引里实际出现过的文件引用；指针行据此决定是否附描述
    index_refs: set[str] = field(default_factory=set)
    awaiting_calibration: bool = False
    # 上一轮的 usage 样本待消费：校准延迟到下一轮请求开头，那时才能从
    # 回存的历史里看出上一轮是否走了工具循环（那样的样本不能学）
    pending_calibration: bool = False
    warned_window: bool = False
    warned_summarizer: bool = False
    warned_clip: bool = False
    # 后台提取进行中（同一会话不并发提取）
    extracting: bool = False
    # 工具循环中途累计清理的工具结果条数（仅内存，供 mb_status 展示）
    loop_cleared: int = 0

    def clear_index(self) -> None:
        """作废索引快照以及随快照生成的人物档案与索引引用集合。"""
        self.index_block = None
        self.index_loaded = False
        self.index_pin_key = ()
        self.index_pinned = {}
        self.index_pinned_full = set()
        self.index_seen_noted = False
        self.index_refs = set()


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
        self._archive = archive.ArchiveStore(data_dir / "archive")
        self._sessions: OrderedDict[str, RuntimeSession] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._warned_threshold = False
        # 作用域目录 -> 上次遗忘扫描时间
        self._last_sweep: dict[Path, float] = {}
        # 定期整理：每处的记录与进行中标记；作用域 -> 上次检查时间；
        # 模型调用一次只跑一处，避免一波消息同时触发好几次复盘
        self._tidy = tidy.TidyBook(data_dir / "tidy")
        self._tidy_checked: dict[str, float] = {}
        self._tidy_gate = asyncio.Semaphore(1)
        # 共享人物空间：成员表从配置解析，版本记录放在 state/ 下（不是记忆文件）
        self._sharing = sharing.Membership()
        self._sharing_warned: set[str] = set()
        self._seen = sharing.SeenVersions(data_dir / "state" / "shared_seen.json")
        self._refresh_sharing()
        self.dashboard = MemoryDashboard(self)
        self.dashboard.register()

        threshold, warning = compress.validate_threshold(
            self.config.get("compress_threshold", compress.THRESHOLD_FALLBACK)
        )
        if warning:
            logger.error(LOG_PREFIX + warning)
        logger.info(
            LOG_PREFIX
            + f"已加载：压缩阈值 {threshold}，数据目录 {data_dir}"
        )
        if self._bool("loop_guard", True) and not LOOP_HOOKS_READY:
            logger.warning(
                LOG_PREFIX
                + "当前 AstrBot 缺少 on_agent_begin / on_llm_tool_respond 钩子，"
                "工具循环中途保护不生效（升级 AstrBot 后恢复）"
            )

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

    def _threshold(self) -> float:
        value, warning = compress.validate_threshold(
            self.config.get("compress_threshold", compress.THRESHOLD_FALLBACK)
        )
        if warning and not self._warned_threshold:
            self._warned_threshold = True
            logger.error(LOG_PREFIX + warning)
        return value

    def _target_ratio(self) -> float:
        """压缩完成后的目标占用 = 阈值 - 缓冲区，预留空间避免压完立刻又逼近上限。

        缓冲区默认 0.165，约留出窗口六分之一的余量。
        """
        try:
            buffer = float(self.config.get("compress_buffer", 0.165))
        except (TypeError, ValueError):
            buffer = 0.165
        buffer = min(max(buffer, 0.0), 0.5)
        return max(0.15, self._threshold() - buffer)

    # ============================================================ 会话运行时

    async def _runtime(self, umo: str) -> RuntimeSession:
        rt = self._sessions.get(umo)
        if rt is not None:
            self._sessions.move_to_end(umo)
            return rt
        rt = RuntimeSession(state=await self._load_state(umo))
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

    def state_path_for_key(self, key: str) -> Path:
        return self._state_dir / f"{key}.json"

    def _state_path(self, umo: str) -> Path:
        return self.state_path_for_key(memstore.safe_key(umo))

    async def load_state_file(self, path: Path) -> compress.SessionState:
        try:
            if path.is_file():
                raw = await asyncio.to_thread(path.read_text, "utf-8")
                return compress.SessionState.from_dict(json.loads(raw))
        except (OSError, ValueError) as exc:
            logger.warning(LOG_PREFIX + f"读取压缩状态失败（{path.name}）：{exc}")
        return compress.SessionState()

    async def _load_state(self, umo: str) -> compress.SessionState:
        return await self.load_state_file(self._state_path(umo))

    async def _save_state(self, umo: str, state: compress.SessionState) -> None:
        await self.save_state_file(self._state_path(umo), state)

    async def save_state_file(
        self, path: Path, state: compress.SessionState
    ) -> None:

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_text(
                json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8"
            )
            tmp.replace(path)

        try:
            await asyncio.to_thread(_write)
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"持久化压缩状态失败（{path.name}）：{exc}")

    def _spawn(self, coro, desc: str) -> None:
        """启动后台任务并保留引用（插件卸载时统一取消）。"""
        task = asyncio.create_task(coro, name=f"memory_beyond:{desc}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

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

    def _people_on(self) -> bool:
        """人物记忆开关，默认关闭。开启后每个平台一份人物目录、一人一个文件，
        该平台的所有会话共享；关闭时各会话各记各的，已有文件原样保留。"""
        return self._bool("enable_people_memory", False)

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
        membership = sharing.parse_config(raw).filter_same_platform(
            self.platform_types()
        )
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
        shared = bool(umo) and self._sharing.is_member(self._platform_of(umo))
        return prompts.memory_guidance(
            self._global_on(),
            self._people_on(),
            self._forgetting_on(),
            shared_people=shared,
        )

    @staticmethod
    def _platform_of(umo: str) -> str:
        platform, _, _ = registry.parse_umo(umo)
        return platform or str(umo or "").split(":", 1)[0] or "unknown"

    def _people_scope(self, umo: str) -> memstore.ScopeStore:
        return self._store.people_scope(self._platform_of(umo))

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
        self, umo: str, store: memstore.ScopeStore
    ) -> bool:
        """自动遗忘扫描能否处理这个人物作用域。

        共享空间里只有 edit 级成员或非成员自己的目录触发扫描；append / read
        级成员只读不写，不该因为他们的会话让共享文件对所有人退出索引。
        """
        if not self._store.is_shared_people_scope(store):
            return True
        return self._people_permission(self._platform_of(umo)) in (
            None,
            sharing.PERMISSION_EDIT,
        )

    def _shared_people_actor(self, umo: str, store: memstore.ScopeStore) -> str:
        """共享人物作用域对应的机器人 platform_id；不是共享空间返回空串。

        提取写盘不在工具调用现场，没有事件的 platform_id，用 UMO 前缀定位
        同一个成员身份，保证 SeenVersions 的键与工具层一致。
        """
        if not self._store.is_shared_people_scope(store):
            return ""
        platform_id = self._platform_of(umo)
        return platform_id if self._sharing.is_member(platform_id) else ""

    def _can_extract_people(self, umo: str) -> bool:
        """后台 / 压缩联动提取能否写 people。

        只有 edit 级成员能写共享人物空间；append / read 级成员的提取拿不到
        people 目标，和 people 关闭时一样只写 session。非成员维持上游行为。
        """
        if not self._people_on():
            return False
        permission = self._people_permission(self._platform_of(umo))
        return permission is None or permission == sharing.PERMISSION_EDIT

    async def _note_shared_seen(
        self,
        event: AstrMessageEvent,
        scope: str,
        store: memstore.ScopeStore,
        file: str,
        content: str | None = None,
    ) -> None:
        """记住这个机器人刚看过的共享文件版本（读取与召回全文都算看过）。"""
        if scope != "people" or not file:
            return
        platform_id = self._event_actor(event)
        if not self._sharing.is_member(platform_id):
            return
        try:
            if content is None:
                content = await store.read(file)
            if content is None:
                return
            await asyncio.to_thread(self._seen.note, platform_id, file, content)
        except OSError:
            # 版本记录写不进去只影响覆盖检查，不能影响读取/写入本身
            logger.debug(LOG_PREFIX + "共享人物空间的版本记录写盘失败", exc_info=True)

    def _scope_for(
        self, event: AstrMessageEvent, scope: str
    ) -> memstore.ScopeStore | None:
        scope = (scope or "").strip().lower()
        if scope == "global":
            return self._global_scope()
        if scope == "session":
            return self._session_scope(event)
        if scope == "people":
            return self._people_scope(str(event.unified_msg_origin))
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
        if scope == "people" and not self._people_on():
            return None, scope, PEOPLE_MEMORY_OFF
        store = self._scope_for(event, scope)
        if store is None:
            return None, scope, SCOPE_ARG_ERROR
        return store, scope, ""

    # ================================================================ 钩子

    @filter.on_llm_request()
    async def inject_and_compress(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """请求前的介入点：注入记忆索引、必要时抢先压缩，拼出本轮发送视图。"""
        # 同一事件里上一次请求遗留的循环守卫不能带进这一次
        self._arm_loop_guard(event, None)
        try:
            await self._process_request(event, req)
        except Exception:  # noqa: BLE001 - 插件缺陷不能拖垮 LLM 请求链路
            logger.exception(LOG_PREFIX + "on_llm_request 处理失败，本轮跳过注入与压缩")

    async def _process_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        # 配置页改了共享成员时，索引/召回/提取立即按新成员表走
        self._refresh_sharing()
        self._schedule_history_cleanup()
        # 其他插件自行发起的模型调用（event.request_llm，不带对话也没有历史）
        # 同样经过这个钩子：它不是本会话的对话请求，不注入也不动压缩状态——
        # 否则"没有历史"会被当成对话被清空，把摘要作废
        if getattr(req, "conversation", None) is None and not (req.contexts or []):
            return
        rt = await self._runtime(umo)
        state = rt.state
        now = time.time()
        if self._touch_registry(event, umo):
            await self._flush_registry()
        # v1.2.5 之前注入的索引块会被 AstrBot 回存进平台历史，读入时清掉
        # 这些副本（现在注入块带 _no_save 不再入史，此过滤兼作历史自愈）
        contexts = [
            m
            for m in (req.contexts or [])
            if isinstance(m, dict) and not prompts.is_index_block(m)
        ]

        # ---- 消费上一轮的用量校准样本 ----
        # 估算只覆盖一轮的首次调用，而 usage 来自最后一次调用：上一轮若走
        # 了工具循环，最后一次调用会多出中途塞入的工具结果，这种样本会把
        # 比例系统性推高。工具痕迹要等历史回存后才能看到，所以校准延迟到
        # 这里；比例仍在本轮估算之前更新完毕，生效时机与原来一致。
        if rt.pending_calibration:
            rt.pending_calibration = False
            if compress.last_round_used_tools(contexts):
                logger.debug(
                    LOG_PREFIX
                    + f"会话 {umo} 上一轮走了工具循环，usage 样本不用于校准"
                )
            elif state.last_estimate > 0 and state.last_actual > 0:
                calibrator = tokens.TokenEstimator(state.ratio)
                calibrator.calibrate(state.last_estimate, state.last_actual)
                state.ratio = calibrator.ratio

        # ---- 状态与当前历史对齐：对话切换则作废，压缩被固化则重定位 ----
        cid = await self._conversation_id(event, req)
        had_summary = bool(state.summary)
        outcome = compress.reconcile(state, cid, contexts)
        if outcome == compress.RECONCILE_RESET:
            if had_summary:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 的历史与压缩状态不再匹配（对话切换或历史清空），"
                    "压缩状态作废重建"
                )
            await self._save_state(umo, state)
            # 对话已切换/重置，索引快照跟着作废，本轮从磁盘重读
            rt.clear_index()
        elif outcome == compress.RECONCILE_COLLAPSED:
            logger.info(
                LOG_PREFIX
                + f"会话 {umo} 的压缩视图已被平台回存固化，水位线重定位为 0"
                "（摘要保留，当前历史整体视为摘要之后的尾部）"
            )
            await self._save_state(umo, state)

        # /reset 后 cid 不变、水位线为 0 时上面的校验不会触发：
        # 历史为空本身就说明是新对话，索引快照同样要刷新
        if not contexts and rt.index_loaded:
            rt.clear_index()

        # 召回打分用用户原话：取在加发送者标注之前
        query = str(req.prompt or "")

        # ---- 发送者标注：注入数字 ID，让记人不依赖可变可冒用的昵称 ----
        if self._bool("annotate_sender", True):
            self._annotate_sender(event, req)

        n_system = split_leading_system(contexts)
        memory_on = self._bool("enable_memory", True)

        # ---- 后台定期提取：每隔 N 轮把最近的对话交给模型补记长期事实 ----
        if memory_on:
            self._maybe_extract(rt, umo, contexts, n_system, now)
            # ---- 遗忘扫描：长期没用到的记忆退出索引与召回（后台，按目录节流） ----
            self._maybe_forget(umo, now)
            # ---- 定期整理：到了时间又攒够新记忆的作用域在后台复盘 ----
            self._maybe_tidy(umo, now)

        # ---- 记忆索引块（对话内快照；新对话、压缩后、WebUI 改动后从磁盘刷新） ----
        index_msg = None
        recall_msg = None
        if memory_on:
            block = await self._index_block(rt, event, note_seen=True)
            if block:
                index_msg = prompts.build_index_message(block)
            # ---- 相关记忆召回：本轮用得上的记忆全文附在新消息之前 ----
            recall_msg = await self._recall_message(event, umo, query, rt)

        guidance = self._guidance(umo) if memory_on else ""
        estimator = tokens.TokenEstimator(state.ratio)

        overhead = (
            estimator.messages(contexts[:n_system])
            + estimator.text(str(req.system_prompt or "") + guidance)
            + estimator.text(str(req.prompt or ""))
            + estimator.scale(
                len(getattr(req, "image_urls", None) or []) * tokens.IMAGE_TOKENS
            )
        )
        tools_cost = tool_schema_cost(getattr(req, "func_tool", None), estimator)
        index_cost = estimator.messages([index_msg]) if index_msg else 0
        recall_cost = estimator.messages([recall_msg]) if recall_msg else 0

        # ---- 缓存已冷时清理旧工具结果：整段前缀反正要重新计费 ----
        idle_minutes = self._int("tool_result_idle_minutes", 60)
        cache_cold = (
            idle_minutes > 0
            and state.last_request_at > 0
            and now - state.last_request_at >= idle_minutes * 60
        )
        cleared_total = 0

        def current_tail() -> list[dict]:
            nonlocal cleared_total
            mark = max(state.watermark, n_system) if state.watermark else n_system
            tail = contexts[mark:]
            if cache_cold:
                tail, cleared = compress.clear_old_tool_results(
                    tail, self._int("keep_recent_turns", 3)
                )
                cleared_total = cleared
            return tail

        # ---- 抢先压缩 ----
        window, window_note = await self._resolve_window(event)
        compress_on = self._bool("enable_compression", True)
        if compress_on and window <= 0 and not rt.warned_window:
            rt.warned_window = True
            logger.warning(LOG_PREFIX + f"会话 {umo} 压缩已停用：{window_note}")

        tail = current_tail()
        clipped = False
        if compress_on and window > 0:
            threshold_tokens = int(window * self._threshold())
            fixed = overhead + tools_cost + index_cost + recall_cost

            def total_now() -> int:
                return fixed + estimator.text(state.summary) + estimator.messages(tail)

            total = total_now()
            if total > threshold_tokens:
                await self._compress(event, rt, umo, contexts, total)
                tail = current_tail()
                total = total_now()

            # 兜底裁剪只在压缩尝试之后仍超阈值时接管（摘要失败、冷却期、
            # 单轮过大）。它的目标水位低于触发阈值，若每轮无条件执行，
            # 会把总量永远摁在触发线之下，滚动摘要一次也轮不到。
            # 水位线不动；被裁内容会随平台回存历史而丢失，属有损最后手段。
            if total > threshold_tokens:
                budget = (
                    int(window * self._target_ratio())
                    - fixed
                    - estimator.text(state.summary)
                )
                tail, clipped = compress.fit_tail_to_budget(
                    tail, estimator, max(budget, MIN_TAIL_BUDGET)
                )
            if clipped:
                message = (
                    LOG_PREFIX
                    + f"会话 {umo} 本轮启用应急裁剪视图（摘要不可用或单轮过大），"
                    "被裁内容可能随平台回存历史而丢失"
                )
                if rt.warned_clip:
                    logger.debug(message)
                else:
                    rt.warned_clip = True
                    logger.warning(message)
            # 上面只管这条消息开头；工具循环里结果还会一轮轮涨进上下文，
            # 交给循环守卫在每个工具返回后盯着（预算与上面的压缩同一套）
            if self._bool("loop_guard", True) and LOOP_HOOKS_READY:
                self._arm_loop_guard(
                    event,
                    loopguard.LoopGuard.for_window(
                        window,
                        threshold=self._threshold(),
                        target_ratio=self._target_ratio(),
                        tools_cost=tools_cost,
                        ratio=estimator.ratio,
                    ),
                )
        if cleared_total:
            logger.info(
                LOG_PREFIX
                + f"会话 {umo} 空闲超过 {idle_minutes} 分钟，提示词缓存已冷，"
                f"清理了 {cleared_total} 条较早的工具结果"
            )

        # ---- 拼装发送视图：system 消息 + 索引块 + 摘要块 + 原文尾部 + 召回块 ----
        summary_msg = (
            prompts.build_summary_message(state.summary, state.compressed_at)
            if state.summary
            else None
        )
        view = list(contexts[:n_system])
        if index_msg:
            view.append(index_msg)
        if summary_msg:
            view.append(summary_msg)
        view.extend(tail)
        # 召回块放在最末（紧挨着本轮新消息）：逐轮变化也不破坏前缀缓存
        if recall_msg:
            view.append(recall_msg)
        req.contexts = view

        if guidance and guidance not in str(req.system_prompt or ""):
            req.system_prompt = str(req.system_prompt or "") + guidance

        summary_cost = estimator.messages([summary_msg]) if summary_msg else 0
        tail_cost = estimator.messages(tail)
        # 分项构成随估算落盘，mb_status 与 WebUI 用它解释数值由什么组成
        state.last_parts = {
            "overhead": overhead,
            "tools": tools_cost,
            "index": index_cost,
            "summary": summary_cost,
            "recall": recall_cost,
            "history": tail_cost,
        }
        state.last_estimate = sum(state.last_parts.values())
        state.last_request_at = now
        rt.awaiting_calibration = True
        # 估算值随状态落盘，重启/重载插件后 mb_status 仍能给出上一次的统计
        await self._save_state(umo, state)

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

    def _annotate_sender(
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
        first_line = prompt.split("\n", 1)[0]
        if first_line == tag:
            return  # 同一请求被重复处理时不叠加标注
        req.prompt = f"{tag}\n{prompts.neutralize_sender_forgery(prompt)}"

    async def _index_block(
        self,
        rt: RuntimeSession,
        event: AstrMessageEvent,
        *,
        note_seen: bool = False,
    ) -> str | None:
        """开头块：索引 + （私聊时）发送者人物档案，共用一个快照。

        快照只在对话切换、压缩、重置、管理页改动或相关配置变化时刷新；
        记忆不变时逐轮逐字不变，才能命中模型服务的前缀缓存。note_seen 只在
        真正发给模型的请求路径上置真：管理指令里估算开头块时不能把人物档案
        记成"模型看过"。
        """
        global_on = self._global_on()
        pin_key = self._pin_profile_key(event)
        if (
            rt.index_loaded
            and rt.index_global_on == global_on
            and rt.index_pin_key == pin_key
        ):
            # 管理指令先估算过开头块时，快照可能还没在真实请求里记过"看过"；
            # 这里按快照里的内容摘要补记，内容已变则交给召回块的更新提示。
            if note_seen and pin_key and not rt.index_seen_noted:
                await self._note_pinned_seen(rt, event)
            return rt.index_block
        global_index = None
        if global_on:
            global_index = (await self._global_scope().load_index()).text
        session_snap = await self._session_scope(event).load_index()
        profiles: list[injection.MemoryView] = []
        pinned: dict[str, str] = {}
        pinned_full: set[str] = set()
        if pin_key:
            profiles, pinned, pinned_full = await self._pinned_profiles(
                event, note_seen=note_seen
            )
        rt.index_block = injection.build_opening_block(
            global_index, session_snap.text, profiles
        )
        rt.index_loaded = True
        rt.index_global_on = global_on
        rt.index_pin_key = pin_key
        rt.index_pinned = pinned
        rt.index_pinned_full = pinned_full
        rt.index_seen_noted = note_seen
        rt.index_refs = {
            f"session/{name}"
            for name in injection.indexed_names(session_snap.text)
        }
        if global_index is not None:
            rt.index_refs |= {
                f"global/{name}" for name in injection.indexed_names(global_index)
            }
        return rt.index_block

    def _pin_profile_key(self, event: AstrMessageEvent) -> tuple:
        """私聊人物档案的快照签名；不满足开启条件时为空元组（不钉档案）。"""
        if not self._bool("pin_private_profile", True) or not self._people_on():
            return ()
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        _, kind, _ = registry.parse_umo(umo)
        if kind != registry.KIND_PRIVATE:
            return ()
        sender_id, _ = self._sender_identity(event)
        if not sender_id or not memstore.is_person_file(f"user-{sender_id}.md"):
            return ()
        return ("pin", self._platform_of(umo), sender_id)

    async def _pinned_profiles(
        self, event: AstrMessageEvent, *, note_seen: bool = False
    ) -> tuple[list[injection.MemoryView], dict[str, str], set[str]]:
        """读出发送者的人物档案，渲染成开头块里钉住的部分。

        返回 (渲染视图, ref -> 注入时的内容摘要, 未截断的 ref 集合)。共享
        人物空间里，未被头尾截断的 people 档案记进"看过"：模型确实看过
        这份完整内容，之后整文件覆盖才允许。
        """
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        sender_id, _ = self._sender_identity(event)
        name = f"user-{sender_id}.md"
        if not sender_id or not memstore.is_person_file(name):
            return [], {}, set()
        platform_id = self._event_actor(event)
        shared = self._sharing.is_member(platform_id)
        views: list[injection.MemoryView] = []
        pinned: dict[str, str] = {}
        full: set[str] = set()
        for label, store in (
            ("session", self._session_scope(event)),
            ("people", self._people_scope(umo)),
        ):
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
            if not view.truncated:
                full.add(view.ref)
            if (
                note_seen
                and label == "people"
                and view.shared
                and not view.truncated
            ):
                await self._note_shared_seen(event, label, store, name, content)
        return views, pinned, full

    async def _note_pinned_seen(
        self, rt: RuntimeSession, event: AstrMessageEvent
    ) -> None:
        """给快照里未截断的人物档案补记"看过"，只认与快照一致的内容。"""
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        for ref in rt.index_pinned:
            if ref not in rt.index_pinned_full:
                continue
            scope, _, file = ref.partition("/")
            store = (
                self._people_scope(umo)
                if scope == "people"
                else self._session_scope(event)
            )
            content = await store.read(file)
            if content is None:
                continue
            if sharing.content_sha1(content) != rt.index_pinned[ref]:
                continue
            await self._note_shared_seen(event, scope, store, file, content)
        rt.index_seen_noted = True

    # ============================================================ 召回

    async def _recall_message(
        self,
        event: AstrMessageEvent,
        umo: str,
        query: str,
        rt: RuntimeSession | None = None,
    ) -> dict | None:
        """挑出本轮相关的记忆，拼成附在新消息之前的召回块；失败只记日志。

        rt 带上开头块的快照信息：快照里已钉住的人物档案不再进入召回候选，
        快照之后被改过的档案则附上最新全文并注明"以这里为准"。
        """
        mode = self._str("recall_mode", "keyword").strip().lower()
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        try:
            scopes: list[tuple[str, memstore.ScopeStore]] = [
                ("session", self._store.session_scope(umo))
            ]
            if self._global_on():
                scopes.append(("global", self._global_scope()))
            if self._people_on():
                scopes.append(("people", self._people_scope(umo)))
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
            if mode == "off":
                limit = 0
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
            model_choice = None
            if mode == "llm" and limit > 0 and query.strip():
                # 人物文件只走身份必选，不让模型从别的群的人里挑
                model_choice = await self._select_by_model(
                    umo, [c for c in candidates if c.scope != "people"], query, limit
                )
            platform_id = self._event_actor(event)
            # 挑选、读文件、分档、截断与渲染统一在 core.recall 里，测量脚本共用
            plan = await recall.assemble_recall(
                stores,
                candidates,
                query=query,
                forced_files=forced,
                limit=limit if mode != "off" else 0,
                data_notice=prompts.DATA_NOTICE,
                model_choice=model_choice,
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
            block = plan.block
            if block:
                # 召回实际附上的全文才算"看过"：以拼块时的同一份计划为准，
                # 被预算挤成指针或按长度截断的不算。不再去块文本里找
                # "### ref（" —— 别的记忆正文可以伪造相同的标题行。
                for section in plan.sections:
                    item = section.item
                    if item.shared and not section.truncated:
                        scope, _, file = item.ref.partition("/")
                        await self._note_shared_seen(
                            event, scope, stores[scope], file, item.content
                        )
            return recall.build_recall_message(block) if block else None
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

    def _maybe_forget(self, umo: str, now: float) -> None:
        """遗忘扫描：本会话涉及的每个作用域目录至多每隔一段时间扫一次，后台进行。"""
        if not self._forgetting_on():
            return
        # (索引快照要作废的会话, 作用域)；None 作废全部会话（global 对所有会话可见），
        # "" 不作废任何会话（people 没有索引注入）
        targets: list[tuple[str | None, memstore.ScopeStore]] = [
            (umo, self._store.session_scope(umo))
        ]
        if self._global_on():
            targets.append((None, self._global_scope()))
        if self._people_on():
            people_store = self._people_scope(umo)
            # 共享空间里 append / read 级成员不触发遗忘扫描：那会把文件对所有
            # 成员移出索引，属于写操作
            if self._can_maintain_people(umo, people_store):
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
        return total

    # ============================================================ 定期整理

    def _maybe_tidy(self, umo: str, now: float) -> None:
        """本会话涉及的每个作用域至多每隔几分钟看一眼该不该整理，要整理的放后台。"""
        mode = self.tidy_mode()
        if mode == tidy.MODE_OFF:
            return
        # (作用域, 目录名, 索引快照要作废的会话)；含义同 _maybe_forget
        targets: list[tuple[str, str | None, str | None]] = [
            ("session", self._store.session_scope(umo).root.name, umo)
        ]
        if self._global_on():
            targets.append(("global", None, None))
        if self._people_on():
            targets.append(("people", self._people_scope(umo).root.name, ""))
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
        async with self._tidy_gate:
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

    async def _select_by_model(
        self,
        umo: str,
        candidates: list[recall.Candidate],
        query: str,
        limit: int,
    ) -> list[recall.Candidate] | None:
        """让摘要模型从记忆清单里挑选；调用失败返回 None（退回关键词）。"""
        prompt = recall.build_selector_prompt(candidates, query, limit)
        text, attempts_log = await self._call_summarizer(
            umo, prompt, attempts=1, timeout=RECALL_SELECT_TIMEOUT
        )
        if not text:
            logger.debug(
                LOG_PREFIX + f"模型挑选召回失败，退回关键词：{'; '.join(attempts_log)}"
            )
            return None
        chosen = recall.parse_selector_response(text, candidates)
        if chosen is None:
            logger.debug(LOG_PREFIX + "模型挑选结果无法解析，退回关键词")
            return None
        return chosen[:limit]

    # ============================================================ 记忆提取

    async def _existing_for_extract(
        self,
        scope: memstore.ScopeStore,
        transcript: str,
        people: memstore.ScopeStore | None = None,
        seen_actor: str = "",
    ) -> tuple[str, dict[str, set[str]]]:
        """抽取前给模型看的已有记忆：相关的附全文（可合并改写），其余只列名字；
        人物记忆开启时，对话里说话人的人物文件也附全文（文件名前缀 people/）。

        seen_actor 非空时，附上全文的人物文件同时记进 SeenVersions：这些
        内容模型确实看过，真正落盘时的共享版本检查据此放行；期间被别的
        机器人改过则拒绝覆盖。

        返回 (提示词片段, 各作用域允许被改写的文件名集合)。
        """
        updatable: dict[str, set[str]] = {"session": set(), "people": set()}
        headers = await asyncio.to_thread(scope.headers)
        speakers = [f"user-{uid}.md" for uid in prompts.sender_ids_in(transcript)]
        full: list[prompts.ExistingMemory] = []
        used = 0
        if headers:
            candidates = [recall.Candidate("session", h) for h in headers]
            related = recall.select_by_keywords(
                candidates, transcript, speakers, recall.DEFAULT_MAX_FILES
            )
            for cand in related:
                content = await scope.read(cand.header.file)
                if not content or used + len(content) > prompts.EXISTING_FULL_CHARS:
                    continue
                used += len(content)
                full.append(
                    prompts.ExistingMemory(
                        cand.header.file, cand.header.description, content
                    )
                )
            updatable["session"] = {item.file for item in full}
        if people is not None:
            for name in speakers:
                content = await people.read(name)
                if not content or used + len(content) > prompts.EXISTING_FULL_CHARS:
                    continue
                used += len(content)
                doc = memstore.parse_memory(content)
                full.append(
                    prompts.ExistingMemory(
                        prompts.PEOPLE_PREFIX + name, doc.description, content
                    )
                )
                updatable["people"].add(name)
                if seen_actor:
                    await asyncio.to_thread(
                        self._seen.note, seen_actor, name, content
                    )
        listed = [
            prompts.ExistingMemory(h.file, h.description)
            for h in headers
            if h.file not in updatable["session"]
            and not journal.is_journal(h.file, h.type)
        ]
        if not full and not listed:
            return "", updatable
        return prompts.build_existing_section(full, listed), updatable

    async def _store_drafts(
        self,
        umo: str,
        drafts: list[prompts.MemoryDraft],
        updatable: dict[str, set[str]],
        source: str,
        actor: str = "",
        people: memstore.ScopeStore | None = None,
    ) -> int:
        """抽取出的记忆落盘：默认进会话作用域；people 不为 None 时，模型标为
        people 的人物文件进该平台的人物作用域。绝不进全局。

        调用方只在有写权限时传入 people（共享空间里仅 edit 级成员），
        否则传 None，标为 people 的草稿会退回 session——和 people 关闭时
        一样，提取拿不到 people 目标。

        已存在的文件只有在抽取时附过全文（模型据此合并）才允许改写，
        其余同名条目跳过——看不到原文就覆盖会丢内容。

        共享人物空间里提取写入沿用工具层的覆盖前版本检查：提示词读到的全文
        到真正写入之间若有别的机器人改过，宁可跳过这一条，也不整文件覆盖。
        """
        stores = {"session": self._store.session_scope(umo)}
        shared_people_actor = ""
        if people is not None:
            stores["people"] = people
            shared_people_actor = self._shared_people_actor(umo, people)
        stored = 0
        for draft in drafts:
            label = draft.scope if draft.scope in stores else "session"
            scope = stores[label]
            try:
                if draft.filename not in updatable.get(label, set()) and scope.exists(
                    draft.filename
                ):
                    continue
                expected = None
                if label == "people" and shared_people_actor:
                    expected = await asyncio.to_thread(
                        self._seen.seen, shared_people_actor, draft.filename
                    )
                report = await scope.write(
                    draft.filename,
                    prompts.render_memory_file(draft),
                    actor=actor,
                    expected_sha1=expected,
                )
                if report.ok:
                    # 索引行由 write 按 frontmatter description 自动同步
                    stored += 1
                    if label == "people" and shared_people_actor:
                        # 自己刚写的版本算看过；同一批里再改同一文件不会被误拦
                        written = await scope.read(draft.filename)
                        if written is not None:
                            await asyncio.to_thread(
                                self._seen.note,
                                shared_people_actor,
                                draft.filename,
                                written,
                            )
                elif label == "people" and shared_people_actor:
                    logger.info(
                        LOG_PREFIX
                        + f"{source}跳过共享人物记忆 {draft.filename}："
                        + report.message
                    )
            except OSError as exc:
                logger.warning(
                    LOG_PREFIX + f"{source}写入记忆 {label}/{draft.filename} 失败：{exc}"
                )
        if stored:
            logger.info(LOG_PREFIX + f"会话 {umo} {source}写入了 {stored} 条记忆")
        return stored

    def _maybe_extract(
        self,
        rt: RuntimeSession,
        umo: str,
        contexts: list[dict],
        n_system: int,
        now: float,
    ) -> None:
        """每隔 auto_extract_interval 轮，在后台把最近的对话交给模型补记长期事实。

        主模型在这段时间里自己写过记忆就跳过：它已经在记，这一段交给它。
        提取在后台进行，不拖慢本轮回复；同一会话同时只有一个提取任务。
        """
        interval = self._int("auto_extract_interval", 10)
        state = rt.state
        if interval <= 0:
            return
        state.turns_since_extract += 1
        if state.turns_since_extract < interval or rt.extracting:
            return
        state.turns_since_extract = 0
        if state.wrote_since_extract:
            state.wrote_since_extract = False
            return
        turns = split_turns(contexts, n_system)[-interval:]
        messages = [m for turn in turns for m in turn.messages]
        transcript = compress.render_transcript(messages)
        if not transcript.strip():
            return
        state.extracted_at = now
        rt.extracting = True
        self._spawn(self._run_extraction(rt, umo, transcript), f"extract:{umo}")

    async def _run_extraction(
        self, rt: RuntimeSession, umo: str, transcript: str
    ) -> None:
        try:
            # 共享空间里只有 edit 级成员的后台提取能写 people
            people = (
                self._people_scope(umo) if self._can_extract_people(umo) else None
            )
            people_on = people is not None
            scope = self._store.session_scope(umo)
            existing, updatable = await self._existing_for_extract(
                scope,
                transcript,
                people,
                self._shared_people_actor(umo, people) if people is not None else "",
            )
            prompt = prompts.build_background_extract_prompt(
                transcript, existing, people_on
            )
            text, attempts_log = await self._call_summarizer(umo, prompt)
            if not text:
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 后台记忆提取未完成（{'; '.join(attempts_log) or '无可用提供商'}），"
                    "下个周期再试"
                )
                return
            drafts = prompts.parse_memories(text, people_on)
            await self._store_drafts(
                umo,
                drafts,
                updatable,
                "后台提取",
                actor=f"extract@{self._platform_of(umo)}",
                people=people,
            )
        except Exception:  # noqa: BLE001
            logger.warning(LOG_PREFIX + f"会话 {umo} 后台记忆提取出错", exc_info=True)
        finally:
            rt.extracting = False

    # ============================================================ 压缩执行

    async def _compress(
        self,
        event: AstrMessageEvent,
        rt: RuntimeSession,
        umo: str,
        contexts: list[dict],
        total_estimate: int,
    ) -> None:
        state = rt.state
        now = time.time()
        if state.in_cooldown(now):
            return
        keep = self._int("keep_recent_turns", 3)
        plan = compress.plan_compression(contexts, state.watermark, keep)
        if plan is None and keep > 1:
            # 历史凑不满配置的保留轮数时，降为只保最新一轮再试——直接放弃
            # 的话下游只剩有损裁剪，能卷进摘要的旧轮不该白白被裁掉
            plan = compress.plan_compression(contexts, state.watermark, 1)
        if plan is None:
            return
        transcript = compress.render_transcript(plan.to_summarize)
        if not transcript.strip():
            return

        extract = self._bool("extract_memories", True) and self._bool(
            "enable_memory", True
        )
        # 压缩联动提取与后台提取同一套门控：只有 edit 级成员能写 people
        people = self._people_scope(umo) if self._can_extract_people(umo) else None
        people_on = people is not None
        existing, updatable = "", {}
        if extract:
            existing, updatable = await self._existing_for_extract(
                self._store.session_scope(umo),
                transcript,
                people,
                self._shared_people_actor(umo, people) if people is not None else "",
            )
        max_chars = max(
            200, self._int("summary_max_chars", prompts.SUMMARY_MAX_CHARS_DEFAULT)
        )
        prompt = prompts.build_summary_prompt(
            self._str("summary_prompt_template"),
            state.summary,
            transcript,
            extract,
            existing,
            now=prompts.format_time(now),
            max_chars=max_chars,
            include_people=people_on,
        )
        text, attempts_log = await self._call_summarizer(umo, prompt)
        if text:
            summary, drafts = prompts.parse_summary_response(text, people_on)
        else:
            summary, drafts = "", []
        if not summary:
            state.record_failure(now)
            await self._save_state(umo, state)
            if not rt.warned_summarizer:
                rt.warned_summarizer = True
                logger.error(
                    LOG_PREFIX
                    + f"会话 {umo} 摘要生成失败（{'; '.join(attempts_log) or '无可用提供商'}），"
                    f"已连续失败 {state.fail_count} 次，进入退避冷却；"
                    "冷却期内启用应急裁剪视图（有损兜底），冷却后自动重试"
                )
            return

        old_watermark = state.watermark
        compress.apply_summary(state, contexts, plan, summary)
        rt.warned_summarizer = False
        rt.warned_clip = False
        await self._save_state(umo, state)
        logger.info(
            LOG_PREFIX
            + f"会话 {umo} 完成滚动摘要：水位线 {old_watermark} → {state.watermark}"
            f"（共 {len(contexts)} 条），压缩前估算 {total_estimate} tokens，"
            f"摘要 {len(state.summary)} 字"
        )
        if len(state.summary) > 2 * max_chars:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的摘要 {len(state.summary)} 字，远超 summary_max_chars"
                f"（{max_chars}）：摘要模型没守住字数上限，每轮都要多带这些内容；"
                "可考虑换摘要模型或调大上限"
            )
        await self._archive_compressed(umo, state, plan.to_summarize)

        if extract and drafts:
            await self._store_drafts(
                umo,
                drafts,
                updatable,
                "压缩联动提取",
                actor=f"extract@{self._platform_of(umo)}",
                people=people,
            )
        # 压缩后从磁盘重新注入记忆索引：清空快照，下次请求重新加载。
        # 索引不是靠"被摘要保留下来"，而是每次从源头重新加载。
        rt.clear_index()

    async def _archive_compressed(
        self, umo: str, state: compress.SessionState, messages: list[dict]
    ) -> None:
        """把刚被摘要覆盖、即将从平台历史卸下的原文另存一份。"""
        keep = self._int("archive_keep", 20)
        if keep <= 0:
            return
        try:
            await asyncio.to_thread(
                lambda: self._archive.save(
                    memstore.safe_key(umo),
                    umo=umo,
                    cid=state.cid,
                    messages=messages,
                    summary=state.summary,
                    keep=keep,
                )
            )
        except OSError as exc:
            logger.warning(LOG_PREFIX + f"会话 {umo} 压缩归档写盘失败：{exc}")

    # ============================================================ 摘要调用链

    async def _call_summarizer(
        self,
        umo: str | None,
        prompt: str,
        *,
        attempts: int | None = None,
        timeout: int | None = None,
        preferred_id: str = "",
    ) -> tuple[str | None, list[str]]:
        """按回退链调用摘要模型：配置的提供商（重试 N 次）→ 当前对话提供商。

        umo 为 None 时（WebUI 发起的整理）当前提供商取默认提供商。
        preferred_id 是某类任务单独指定的提供商（如整理记忆），排在摘要提供商
        之前；两者相同只试一次。
        attempts / timeout 不传时按配置；召回挑选这类要求快的调用只试一次。
        返回 (成功文本或 None, 尝试记录)。全部失败的兜底不在这里——由调用方
        决定（压缩记录失败进入退避冷却，召回退回关键词）。
        """
        if timeout is None:
            timeout = max(10, self._int("summary_timeout", 120))
        retries = attempts or max(1, self._int("summary_retry_count", 3))
        attempts_log: list[str] = []

        chain: list[tuple[str, object, int]] = []
        wanted = [
            ("专用提供商", (preferred_id or "").strip()),
            ("摘要提供商", self._str("summary_provider_id").strip()),
        ]
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
                chain.append((f"{kind} {configured_id}", provider, retries))
        current = await self._current_provider(umo)
        if current is not None and all(current is not p for _, p, _ in chain):
            fallback = retries if not chain else min(retries, FALLBACK_ATTEMPTS)
            chain.append(("当前对话提供商", current, fallback))

        for label, provider, tries in chain:
            for attempt in range(1, tries + 1):
                try:
                    resp = await asyncio.wait_for(
                        provider.text_chat(prompt=prompt), timeout=timeout
                    )
                    text = self._completion_text(resp)
                    if text and text.strip():
                        attempts_log.append(f"{label} 第 {attempt} 次成功")
                        return text, attempts_log
                    attempts_log.append(f"{label} 第 {attempt} 次返回空")
                except asyncio.TimeoutError:
                    attempts_log.append(f"{label} 第 {attempt} 次超时（{timeout}s）")
                except Exception as exc:  # noqa: BLE001 - provider 实现不可控
                    attempts_log.append(f"{label} 第 {attempt} 次失败：{exc}")
                if attempt < tries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
        return None, attempts_log

    async def call_summarizer(self, prompt: str) -> tuple[str | None, list[str]]:
        """供 WebUI 使用的摘要模型调用（不绑定会话）。"""
        return await self._call_summarizer(None, prompt)

    async def call_consolidator(self, prompt: str) -> tuple[str | None, list[str]]:
        """管理页整理记忆用的模型调用：整理专用提供商优先，留空同摘要提供商。"""
        return await self._call_summarizer(
            None, prompt, preferred_id=self._str("consolidate_provider_id")
        )

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

    # ========================================================== 窗口探测

    async def _resolve_window(self, event: AstrMessageEvent) -> tuple[int, str]:
        configured = self._int("max_context_tokens", 0)
        if configured > 0:
            return configured, "手动配置"
        provider = await self._current_provider(
            str(getattr(event, "unified_msg_origin", "") or "") or None
        )
        model = self._provider_model_name(provider)
        if model:
            lowered = model.lower()
            for needle, window in MODEL_WINDOWS:
                if needle in lowered:
                    return window, f"按模型 {model} 自动探测"
        return 0, (
            f"自动探测模型窗口失败（模型名：{model or '未知'}），"
            "请在插件配置中手动指定 max_context_tokens"
        )

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

    # ========================================================== token 校准

    @filter.on_llm_response()
    async def calibrate_tokens(self, event: AstrMessageEvent, resp: LLMResponse):
        """provider 返回真实用量时记录样本，校准延迟到下一轮请求时消费。"""
        try:
            umo = str(getattr(event, "unified_msg_origin", ""))
            rt = self._sessions.get(umo)
            if rt is None or not rt.awaiting_calibration:
                return
            # 其他插件自行发起的调用（不带对话、没有历史）与本会话的估算不配对
            preq = getattr(event, "get_extra", lambda _k: None)("provider_request")
            if (
                preq is not None
                and getattr(preq, "conversation", None) is None
                and not (getattr(preq, "contexts", None) or [])
            ):
                return
            rt.awaiting_calibration = False
            if rt.state.last_estimate <= 0:
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
            state = rt.state
            old_cache_hit = state.cache_hit_tokens
            # 真实上报值记下来，mb_status 里与估算并排展示可直接对照。
            # 比例不在这里学：本轮是否走了工具循环要等历史回存后才能判断，
            # 样本留到下一轮请求开头消费（compress.last_round_used_tools）。
            state.last_actual = actual
            state.cache_hit_tokens = -1 if cache_hit is None else cache_hit
            rt.pending_calibration = True
            # 缓存命中数变化时落盘；比例更新随下一轮请求末尾的常规落盘。
            if state.cache_hit_tokens != old_cache_hit:
                await self._save_state(umo, state)
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "token 校准失败", exc_info=True)

    # ====================================================== 工具循环中途保护

    @staticmethod
    def _arm_loop_guard(event: AstrMessageEvent, guard) -> None:
        """把守卫（或 None）挂到事件上；事件对象不支持额外字段时放弃，不影响请求。"""
        try:
            event.set_extra(LOOP_GUARD_KEY, guard)
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _loop_guard(event: AstrMessageEvent) -> loopguard.LoopGuard | None:
        try:
            guard = event.get_extra(LOOP_GUARD_KEY)
        except Exception:  # noqa: BLE001
            return None
        return guard if isinstance(guard, loopguard.LoopGuard) else None

    @_on_agent_begin()
    async def loop_guard_begin(self, event: AstrMessageEvent, run_context=None):
        """Agent 开始运行：把运行上下文交给本轮的守卫——工具钩子里拿不到消息列表，
        要靠它就地读写。"""
        guard = self._loop_guard(event)
        if guard is not None:
            guard.run_context = run_context

    @_on_tool_respond()
    async def loop_guard_observe(
        self,
        event: AstrMessageEvent,
        tool=None,
        tool_args=None,
        tool_result=None,
    ):
        """每个工具返回之后估算下一次请求的大小，快越过压缩阈值就清理较早的
        工具结果。出任何错都只放弃本轮的保护，绝不能打断用户的工具循环。"""
        guard = self._loop_guard(event)
        if guard is None or guard.run_context is None:
            return
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        try:
            text, images = loopguard.describe_result(tool_result)
            outcome = guard.after_tool(
                getattr(guard.run_context, "messages", None),
                name=str(getattr(tool, "name", "") or ""),
                args=tool_args,
                result_text=text,
                images=images,
            )
            if outcome is None:
                return
            if outcome.cleared:
                rt = self._sessions.get(umo)
                if rt is not None:
                    rt.loop_cleared += outcome.cleared
                logger.info(
                    LOG_PREFIX
                    + f"会话 {umo} 工具循环中途，下一次请求估算约 {outcome.before} "
                    f"tokens，越过触发线 {guard.limit}；清理了 {outcome.cleared} 条"
                    f"较早的工具结果，降到约 {outcome.after}"
                )
            # 两种「救不了」的情况每轮回复只告警一次（stuck 由守卫收口）；
            # 同一批里后续工具再撞上，只记 debug，免得并行多个工具时刷屏
            if outcome.futile and not outcome.stuck:
                logger.debug(
                    LOG_PREFIX
                    + f"会话 {umo} 工具循环中途仍是徒劳：估算约 {outcome.before} "
                    f"tokens，超出窗口 {guard.ceiling}，未动较早的工具结果"
                )
            elif outcome.futile:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 工具循环中途，下一次请求估算约 {outcome.before} "
                    f"tokens，即使清掉所有较早的工具结果也超出窗口 {guard.ceiling}，"
                    "所以没有动它们。多半是某个工具一次返回的内容过大（AstrBot 把它"
                    "落盘成预览的话则无妨）；否则请让工具返回更精简的内容，"
                    "或调大 max_context_tokens"
                )
            elif outcome.stuck:
                logger.warning(
                    LOG_PREFIX
                    + f"会话 {umo} 工具循环中途估算约 {outcome.after} tokens，仍高于"
                    f"触发线 {guard.limit}，但已没有可清理的较早工具结果。再涨下去"
                    "可能超出模型窗口，或触发平台内置压缩（会成段丢掉较早的消息）；"
                    "单次工具返回过大时，请让工具返回更精简的内容，或调大 "
                    "max_context_tokens"
                )
        except Exception:  # noqa: BLE001
            guard.run_context = None  # 本轮不再介入
            logger.warning(
                LOG_PREFIX + "工具循环中途保护出错，本轮不再介入", exc_info=True
            )

    # ============================================================ 记忆工具

    @filter.llm_tool(name="memory_read")
    async def memory_read(
        self, event: AstrMessageEvent, scope: str, file: str = "MEMORY.md"
    ) -> str:
        """读取长期记忆文件的完整内容。注入的索引只是目录：回答涉及某人、某事的细节之前，先用本工具取回对应文件再作答，不要凭索引行猜；更新某条记忆之前也先读它的当前内容。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（人物记忆，一人一个 user-<数字ID>.md；通常同一平台所有会话共享，启用共享人物空间时是多个成员机器人共用的 _shared 目录）
            file(string): 记忆文件名，如 user-12345678.md；省略时读 MEMORY.md 索引全文
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
            # 共享人物空间：读过当前版本，之后的整文件覆盖才允许
            await self._note_shared_seen(event, scope, store, file, content)
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

    @filter.llm_tool(name="memory_write")
    async def memory_write(
        self,
        event: AstrMessageEvent,
        scope: str,
        file: str,
        content: str = "",
        delete: bool = False,
    ) -> str:
        """写入、修改或删除一个长期记忆文件（整文件覆盖；修改＝读出后改写重写入，删除＝delete 设 true）。一个文件只记一条事实，content 必须以 frontmatter 开头：--- / name: 与文件名一致 / description: 一句话钩子 / metadata: / type: user|feedback|project|reference|journal / ---，正文写事实本身。MEMORY.md 索引由插件自动维护，禁止直接写：要改索引行就重写对应文件的 description，要删索引行就删除对应文件。写前先查重，同一个人、同一件事始终更新同一个文件，不另建重复文件。type 为 journal 的是每日日志（文件名建议 daily-<日期>.md）：流水账不占单独索引行，只在索引末尾折叠成最近两篇加一行汇总，不参与自动召回，也不参与查重；需要时按日期用 memory_read 或 memory_search 检索。写入后会检查正文里的 [[文件名]] 链接指向的文件是否在当前机器人可见的作用域里，并在文件超过 8KB 或日期过密时提醒拆分或整理；这些只是回执提示，不会自动创建链接目标。作用域边界：关于具体用户或本会话的信息一律写 session，记人以发送者标注中的数字 ID 为锚（文件名 user-<数字ID>.md，昵称只作正文附注）；global 只存机器人自身的偏好与行为准则；people（启用时）只存某人亲口说的、换个群也成立的身份类事实。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（人物记忆，一人一个 user-<数字ID>.md；通常同一平台所有会话共享，启用共享人物空间时是多个成员机器人共用的 _shared 目录）
            file(string): 记忆文件名，须以 .md 结尾，如 user-12345678.md；不可为 MEMORY.md
            content(string): 完整的文件内容，含 frontmatter（delete 为 false 时必填）
            delete(boolean): 设为 true 时删除该文件并自动移除其索引行，此时忽略 content
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        if scope == "people" and not memstore.is_person_file(file):
            return PEOPLE_FILE_RULE
        actor = self._event_actor(event)
        if scope == "people":
            actor, permission, identity_refusal = self._people_access(event, store)
            if identity_refusal:
                return identity_refusal
        else:
            permission = None
        if delete:
            # append 与 read 级成员都不能删除共享人物记忆
            refusal = sharing.write_refusal(
                permission, exists=True, delete=True
            )
            if refusal:
                return refusal
            report = await store.delete(file, actor=actor)
        else:
            if not content.strip():
                return "content 为空。写入需提供完整文件内容；如要删除请设 delete=true。"
            existed = store.exists(file)
            # 已遗忘的同名文件也会被整文件写入顶替（遗忘副本进回收站），
            # 对 append 级成员同样是"覆盖"，不能当成新建放行
            dormant = store.has_forgotten(file)
            refusal = sharing.write_refusal(
                permission, exists=existed or dormant, delete=False
            )
            if refusal:
                return refusal
            # 共享空间的版本检查放进 store.write 的锁里：先看自己最近读过的
            # 版本，空串表示没看过——新建放行，覆盖（含并发新建时被对方抢先
            # 写入）由 store 持锁拒绝。这样并发新建不会绕开检查。
            expected = None
            if scope == "people" and self._store.is_shared_people_scope(store):
                expected = await asyncio.to_thread(self._seen.seen, actor, file)
            report = await store.write(
                file,
                content,
                source=self._sender_identity(event)[0],
                actor=actor,
                expected_sha1=expected,
                # append 级成员"只能新建"也放进同一把锁，避免并发下绕过
                forbid_overwrite=(permission == sharing.PERMISSION_APPEND),
            )
            if report.ok:
                # 自己刚写的版本算看过，紧接着再整文件覆盖不会被版本检查误拦
                await self._note_shared_seen(event, scope, store, file)
            if report.ok and not existed:
                hint = await self._duplicate_hint(store, file, content)
                if hint:
                    report = memstore.WriteReport(True, report.message + "\n" + hint)
                if history.looks_like_manual_backup(file):
                    report = memstore.WriteReport(
                        True,
                        report.message
                        + "\n提醒：覆盖和删除前插件已自动保存旧版，管理页可以恢复，"
                        "不需要手动备份（backup、副本或 zz- 开头的文件）。",
                    )
            if report.ok:
                extra = await self._write_insights(event, store, file)
                if extra:
                    report = memstore.WriteReport(
                        True, report.message + "\n" + extra
                    )
        if report.ok:
            self._note_model_wrote(event)
        return report.message

    @filter.llm_tool(name="memory_edit")
    async def memory_edit(
        self,
        event: AstrMessageEvent,
        scope: str,
        file: str,
        old_text: str = "",
        new_text: str = "",
        replace_all: bool = False,
        mode: str = "replace",
    ) -> str:
        """修改一个已有的长期记忆文件。默认 replace 模式：把 old_text 替换成 new_text，文件其余内容原样保留；改一处、补一句、更新 description 时用它，不要用 memory_write 整文件重写（重写容易丢内容）。replace 时 old_text 必须与文件现有内容逐字一致（空格、换行、标点都算）且只出现一次，所以先用 memory_read 读出当前内容再复制片段；new_text 留空等于删掉这段。append 模式：old_text 留空时把 new_text 追加到文件末尾，old_text 是某个小节标题行（如 ## 作息与习惯）时追加到该小节末尾。section 模式：old_text 是一个小节标题行，new_text 替换这一节标题下的正文，标题行保留，范围到下一个同级或更高级标题为止；标题不存在或出现多次时报错，并列出文件里的所有标题。共享人物空间里 section 模式会做覆盖前版本检查：先用 memory_read 读当前版本，其他机器人改过之后要先重读，否则会拒绝覆盖。每次修改前插件自动保存旧版；修改后会检查 [[文件名]] 链接和文件长度并在回执里提醒。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（人物记忆，一人一个 user-<数字ID>.md；通常同一平台所有会话共享，启用共享人物空间时是多个成员机器人共用的 _shared 目录）
            file(string): 要修改的记忆文件名，如 user-12345678.md；不可为 MEMORY.md
            old_text(string): replace 模式下要被替换的原文片段，须与文件内容逐字一致且只出现一次；append 模式下留空表示追加到文件末尾，填小节标题行表示追加到该小节末尾；section 模式下填一个小节标题行，如 ## 作息与习惯
            new_text(string): replace 模式下的替换文字（留空表示删掉 old_text）；append 模式下要追加的文字；section 模式下是这一节的新正文（留空表示清空这一节正文）
            replace_all(boolean): replace 模式下设为 true 时替换文件里所有匹配处（如统一改一个称呼），默认只接受唯一匹配；append 与 section 模式下忽略
            mode(string): replace（默认，局部替换）、append（追加，不覆盖已有文字）或 section（按小节标题替换该节正文）
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        store, scope, refused = self._tool_store(event, scope)
        if store is None:
            return refused
        if scope == "people" and not memstore.is_person_file(file):
            return PEOPLE_FILE_RULE
        mode = (mode or "replace").strip().lower()
        if mode not in ("replace", "append", "section"):
            return "mode 只能是 replace（默认）、append 或 section。"
        if scope == "people":
            actor, permission, identity_refusal = self._people_access(event, store)
            if identity_refusal:
                return identity_refusal
        else:
            actor, permission = self._event_actor(event), None
        refusal = sharing.edit_refusal(permission, mode)
        if refusal:
            return refusal
        expected = None
        if (
            scope == "people"
            and self._store.is_shared_people_scope(store)
            and mode == "section"
            and (file or "").strip()
        ):
            # section 不是逐字替换，可能基于过期内容整节覆盖；共享空间里沿用
            # memory_write 的覆盖前版本检查。空串表示没读过，锁内会拒绝。
            expected = await asyncio.to_thread(self._seen.seen, actor, file)
        report = await store.edit(
            file,
            old_text,
            new_text,
            replace_all,
            source=self._sender_identity(event)[0],
            actor=actor,
            mode=mode,
            expected_sha1=expected,
        )
        if report.ok:
            # 自己刚改过的版本算看过，后续整文件覆盖不会被版本检查误拦
            await self._note_shared_seen(event, scope, store, file)
            self._note_model_wrote(event)
            extra = await self._write_insights(event, store, file)
            if extra:
                report = memstore.WriteReport(True, report.message + "\n" + extra)
        return report.message

    async def _duplicate_hint(
        self, store: memstore.ScopeStore, file: str, content: str
    ) -> str:
        """新建文件后用召回的打分看看有没有疑似记同一件事的旧文件，有就提醒合并。

        日志是流水账，既不拿别的日志当查重候选，新建日志时也不提示查重。
        """
        try:
            doc = memstore.parse_memory(content)
            if journal.is_journal(file, doc.type):
                return ""
            text = f"{doc.description} {doc.body[:600]}"
            headers = await asyncio.to_thread(store.headers)
            similar = recall.similar_memories(
                text, headers, file, mem_type=doc.type
            )
        except Exception:  # noqa: BLE001 - 查重只是提醒，失败不影响写入
            logger.debug(LOG_PREFIX + "写入查重失败", exc_info=True)
            return ""
        if not similar:
            return ""
        listing = "；".join(
            f"{h.file}（{h.description or '无描述'}）" for h in similar
        )
        return (
            f"提醒：新文件可能与已有记忆记的是同一件事：{listing}。"
            "请核对，确属同一件事就合并进一个文件、删掉多余的。"
        )

    def _visible_stores(self, event: AstrMessageEvent) -> list[memstore.ScopeStore]:
        """当前机器人能看到的记忆作用域：global（启用时）、当前会话、people（启用时）。

        共享人物空间只映射到成员共用的 _shared；非成员的 people 目录只属于
        自己。链接检查只用这几个作用域，不会暴露别的会话或别的机器人里有没有
        某个文件。
        """
        stores: list[memstore.ScopeStore] = []
        if self._global_on():
            stores.append(self._global_scope())
        stores.append(self._session_scope(event))
        if self._people_on():
            stores.append(self._people_scope(str(event.unified_msg_origin)))
        return stores

    async def _write_insights(
        self, event: AstrMessageEvent, store: memstore.ScopeStore, file: str
    ) -> str:
        """写入 / 修改成功后的回执补充：悬空链接与长文件提醒。

        只是提醒，失败也不影响已经完成的写入，所以这里吞掉异常，最多记一条
        调试日志。
        """
        try:
            file = (file or "").strip().lstrip("/")
            content = await store.read(file)
            if content is None:
                return ""
            doc = memstore.parse_memory(content)
            available: set[str] = set()
            for visible in self._visible_stores(event):
                available.update(visible.list_files())
                if self._forgetting_on():
                    # 已遗忘文件仍可被 memory_read 恢复，链接到它不算悬空
                    available.update(visible.forgotten_files())
            notes: list[str] = []
            missing = insights.dangling_links(
                memstore.extract_links(doc.body), available
            )
            hint = insights.dangling_link_hint(missing)
            if hint:
                notes.append(hint)
            reasons = insights.long_file_reasons(
                file, doc.type, doc.body, len(content.encode("utf-8"))
            )
            hint = insights.long_file_hint(file, reasons)
            if hint:
                notes.append(hint)
            return "\n".join(notes)
        except Exception:  # noqa: BLE001 - 提醒失败不影响已完成的写入
            logger.debug(LOG_PREFIX + "写入回执的链接/长文件检查失败", exc_info=True)
            return ""

    def _note_model_wrote(self, event: AstrMessageEvent) -> None:
        # 索引快照不在这里刷新：开头的注入内容一变，整段历史的提示词
        # 缓存都会失效。新写的记忆已在磁盘上，相关时由召回带上全文，
        # 下次压缩或新对话时进入索引。这里只记下"主模型自己记过了"，
        # 后台定期提取据此跳过这一段。
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        rt = self._sessions.get(umo)
        if rt is not None:
            rt.state.wrote_since_extract = True

    @filter.llm_tool(name="memory_search")
    async def memory_search(
        self,
        event: AstrMessageEvent,
        query: str,
        scope: str = "all",
        mode: str = "all",
    ) -> str:
        """在长期记忆里全文搜索。写入前查重、索引被截断、或不确定某条记忆是否存在时，用本工具查找；它直接搜文件内容，不依赖索引。

        Args:
            query(string): 搜索词，可用空格分隔多个词；默认要同时命中所有词，mode=any 时命中任意一个即可
            scope(string): 搜索范围，global、session、people 或 all（默认，启用的作用域都搜）
            mode(string): all（默认，所有词都要命中）或 any（命中任意一个词即可）；结果按非日志优先、命中词数、updated 倒序排列，最多 8 条，每条附文件名、类型、更新日期和最多 2 行命中片段
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
        people_on = self._people_on()
        if scope == "global" and not global_on:
            return GLOBAL_MEMORY_OFF
        if scope == "people" and not people_on:
            return PEOPLE_MEMORY_OFF
        stores: list[tuple[str, memstore.ScopeStore]] = []
        # 停用的作用域在 scope=all 时静默略过
        if scope in ("global", "all") and global_on:
            stores.append(("global", self._global_scope()))
        if scope in ("session", "all"):
            stores.append(("session", self._session_scope(event)))
        if scope in ("people", "all") and people_on:
            stores.append(
                ("people", self._people_scope(str(event.unified_msg_origin)))
            )
        if not stores:
            return "scope 参数必须是 global、session、people 或 all。"
        hits: list[tuple[str, insights.SearchHit]] = []
        for label, store in stores:
            for hit in await store.search_hits(query, mode):
                hits.append((label, hit))
        # 各作用域合并后统一排序，最多 8 条；日志排在非日志后面，
        # 避免 daily-* 按文件名占满名额把真正的记忆挤掉
        hits.sort(key=lambda item: insights.search_key(item[1]))
        forgotten_hits: list[tuple[str, insights.SearchHit]] = []
        if self._forgetting_on():
            for label, store in stores:
                for hit in await store.search_forgotten_hits(query, mode):
                    forgotten_hits.append((label, hit))
        # 已遗忘结果也用同一套排序；它们排在活跃结果之后，并计入
        # 同一个 8 条上限，避免整体结果超长或 daily-* 在遗忘块里挤掉记忆
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
        """无估算记录时（如旧版本升级来的会话），从平台存储的历史现场估算。

        返回 (总量, 分项构成)。拿不到请求现场的人格提示词与本轮输入，
        结果略低于请求路径的口径。
        """
        state = rt.state
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
            # 与请求路径同口径：剔除旧版本固化进历史的索引块副本
            contexts = [
                m
                for m in data
                if isinstance(m, dict) and not prompts.is_index_block(m)
            ]
        except Exception:  # noqa: BLE001
            logger.debug(LOG_PREFIX + "现场估算读取历史失败", exc_info=True)
            return 0, {}
        if not contexts:
            return 0, {}
        memory_on = self._bool("enable_memory", True)
        estimator = tokens.TokenEstimator(state.ratio)
        n_system = split_leading_system(contexts)
        if compress.state_matches(state, str(cid), contexts) and state.watermark:
            mark = max(state.watermark, n_system)
        else:
            mark = n_system
        index_cost = 0
        if memory_on:
            block = await self._index_block(rt, event)
            if block:
                index_cost = estimator.messages([prompts.build_index_message(block)])
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
        parts = {
            "overhead": (
                estimator.messages(contexts[:n_system])
                + estimator.text(
                    self._guidance(umo) if memory_on else ""
                )
            ),
            "tools": tools_cost,
            "index": index_cost,
            "summary": (
                estimator.messages(
                    [prompts.build_summary_message(state.summary, state.compressed_at)]
                )
                if state.summary
                else 0
            ),
            "recall": 0,
            "history": estimator.messages(contexts[mark:]),
        }
        return sum(parts.values()), parts

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看 Memory Beyond 当前会话的记忆与压缩状态。"""
        umo = str(event.unified_msg_origin)
        # 配置页可能刚改过共享成员，状态里的目录与权限要和工具一致
        self._refresh_sharing()
        rt = await self._runtime(umo)
        state = rt.state
        window, window_note = await self._resolve_window(event)
        global_on = self._global_on()
        global_part = (
            f"全局 {len(self._global_scope().list_files())} 个文件"
            if global_on
            else "全局 停用"
        )
        people_part = (
            f"人物 {len(self._people_scope(umo).list_files())} 个文件"
            if self._people_on()
            else "人物 停用"
        )
        session_store = self._session_scope(event)
        session_files = session_store.list_files()
        try:
            long_files = await session_store.long_files()
        except Exception:  # noqa: BLE001 - 状态命令不能因单个文件读失败而崩掉
            logger.debug(LOG_PREFIX + "会话长文件扫描失败", exc_info=True)
            long_files = []
        configured_id = self._str("summary_provider_id").strip() or "（未配置，用当前对话提供商）"
        tidy_id = self._str("consolidate_provider_id").strip() or "（未配置，同摘要提供商）"

        now = time.time()
        cooldown = (
            f"退避中，剩余 {int(state.fail_until - now)} 秒（已连续失败 {state.fail_count} 次）"
            if state.in_cooldown(now)
            else "正常"
        )
        if state.last_estimate > 0:
            estimate_line = (
                f"最近一次请求估算：{state.last_estimate} tokens"
                f"（校准比例 {state.ratio:.2f}）"
            )
            parts = state.last_parts
        else:
            live, live_parts = await self._estimate_live(event, rt)
            if live > 0:
                estimate_line = (
                    f"当前上下文现场估算：{live} tokens"
                    f"（校准比例 {state.ratio:.2f}，未含人格提示词与本轮输入）"
                )
                parts = live_parts
            else:
                estimate_line = (
                    f"最近一次请求估算：0 tokens（校准比例 {state.ratio:.2f}）"
                )
                parts = {}
        estimate_lines = [estimate_line]
        if parts:
            estimate_lines.append(
                f"　构成：固定开销 {parts.get('overhead', 0)}"
                f" + 工具声明 {parts.get('tools', 0)}"
                f" + 记忆索引 {parts.get('index', 0)}"
                f" + 摘要 {parts.get('summary', 0)}"
                f" + 召回 {parts.get('recall', 0)}"
                f" + 历史原文 {parts.get('history', 0)}"
            )
        if state.last_actual > 0:
            estimate_lines.append(
                f"最近一次实际上报：{state.last_actual} tokens"
                "（提供商 usage，估算校准以它为准）"
            )
        if window > 0 and parts:
            fixed_floor = parts.get("overhead", 0) + parts.get("tools", 0)
            if fixed_floor >= int(window * self._target_ratio()):
                estimate_lines.append(
                    "⚠️ 人格提示词与工具声明的固定开销已不低于压缩目标水位，"
                    "摘要腾不出空间，只能反复有损裁剪历史——"
                    "请调大 max_context_tokens，或减少启用的插件工具"
                )
        lines = [
            "Memory Beyond 状态",
            f"记忆：{'启用' if self._bool('enable_memory', True) else '停用'}"
            f"（{global_part} / {people_part} / 会话 {len(session_files)} 个文件）",
            f"压缩：{'启用' if self._bool('enable_compression', True) else '停用'}",
            f"上下文窗口：{window if window > 0 else '未知'}（{window_note}）",
            f"触发阈值：{self._threshold():g}，压缩目标：{self._target_ratio():g}",
            f"工具循环中途保护：{self._loop_guard_label(rt)}",
            f"水位线：{state.watermark}，摘要：{len(state.summary)} 字",
            *estimate_lines,
            "最近一次缓存命中："
            + (
                f"{state.cache_hit_tokens} tokens"
                if state.cache_hit_tokens >= 0
                else "提供商未上报"
            ),
            f"摘要提供商：{configured_id}",
            f"整理提供商：{tidy_id}",
            f"摘要链路：{cooldown}",
            f"相关记忆召回：{self._recall_label()}",
            f"后台记忆提取：{self._extract_label(state)}",
            f"记忆遗忘：{self._forget_label(umo)}",
            f"共享人物空间：{self._sharing_label(umo)}",
            f"会话长文件：{self._long_files_label(long_files)}",
            f"定期整理：{self._tidy_label(umo)}",
            "记忆管理页：AstrBot WebUI 侧边栏「插件页面」",
        ]
        yield event.plain_result("\n".join(lines))

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

    def _loop_guard_label(self, rt: RuntimeSession) -> str:
        if not self._bool("loop_guard", True):
            return "关闭"
        if not LOOP_HOOKS_READY:
            return "不可用（当前 AstrBot 缺少所需钩子）"
        if not self._bool("enable_compression", True):
            return "未生效（压缩已停用）"
        return f"开启（本次运行以来本会话共清理过 {rt.loop_cleared} 条工具结果）"

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
        mode = self._str("recall_mode", "keyword").strip().lower()
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if mode == "off" or limit <= 0:
            return "关闭"
        name = "模型挑选" if mode == "llm" else "关键词匹配"
        return f"{name}，每轮最多 {limit} 条"

    def _extract_label(self, state: compress.SessionState) -> str:
        interval = self._int("auto_extract_interval", 10)
        if interval <= 0:
            return "关闭"
        return f"每 {interval} 轮一次（距上次 {state.turns_since_extract} 轮）"

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_reset")
    async def reset_session(self, event: AstrMessageEvent):
        """清空本会话的压缩状态（摘要与水位线）。平台聊天历史不受影响。"""
        umo = str(event.unified_msg_origin)
        rt = await self._runtime(umo)
        rt.state.reset_compression()
        rt.state.record_success()
        rt.clear_index()
        await self._save_state(umo, rt.state)
        yield event.plain_result(
            "已清空本会话的压缩状态（摘要与水位线）。注意：此前已被压缩"
            "卸下的旧对话不会自动回来；之后超过阈值时会基于当前历史重新"
            "滚动摘要。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_probe")
    async def probe_summarizer(self, event: AstrMessageEvent):
        """测试摘要模型回退链是否可用（配置提供商 → 当前对话提供商）。"""
        yield event.plain_result("正在测试摘要链路……")
        text, attempts_log = await self._call_summarizer(
            str(event.unified_msg_origin), "请只回复两个字符：OK"
        )
        detail = "\n".join(attempts_log) if attempts_log else "（没有可尝试的提供商）"
        if text:
            yield event.plain_result(f"摘要链路可用。\n{detail}")
        else:
            yield event.plain_result(
                f"摘要链路全部失败，压缩时将启用应急裁剪视图并退避重试。\n{detail}"
            )

    async def terminate(self):
        """卸载/重载时：撤下 WebUI 接口、取消后台提取、登记簿落盘。

        压缩状态与记忆在每次变更时已即时落盘。
        """
        self.dashboard.unregister()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_registry()
