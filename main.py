"""鲸群记忆 —— 为 AstrBot 重新设计的长期记忆与上下文压缩系统。

核心理念：真相在磁盘，上下文只带索引，细节按需取回，压缩后从磁盘重新注入。

分层约定：
    main.py     —— 平台适配层（本文件）：钩子、LLM 工具、指令、provider 调用
    web_api.py  —— WebUI 记忆管理页的后端接口（页面在 pages/memory）
    core/       —— 纯逻辑：记忆库、索引、召回、token 估算、轮次切分、
                   压缩状态机、会话登记、记忆整理、工具循环保护

兼容性立场：只依赖 @filter.on_llm_request() / @filter.on_llm_response() /
@filter.llm_tool() 等公开插件接口、不 monkey-patch。跟随 AstrBot 取上下文
窗口与轮数上限时要读 AstrBot 内部配置和模型表，相关 import 全部包在 try
里：旧版本取不到就跳过那一级，插件照常加载。
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
    compress,
    history,
    injection,
    insights,
    journal,
    loopguard,
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
from .core.turns import split_leading_system
from .web_api import MemoryDashboard

PLUGIN_NAME = "astrbot_plugin_memory_pod"
LOG_PREFIX = "[memory_pod] "

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
SCOPE_ARG_ERROR = "scope 参数必须是 global、session 或 people。"
# 会话运行时缓存上限（LRU 淘汰，仅内存快照，磁盘状态不受影响）。
MAX_RUNTIME_SESSIONS = 256
# 跟随 AstrBot 取上下文窗口时，超过这个值的窗口视为按旧 README 调大过的
# 兜底值（不可信），改用插件按模型名探测。
UNTRUSTED_WINDOW_LIMIT = 10_000_000
# AstrBot 设了轮数上限时，上下文轮数达到「上限 - 2」就抢先压缩。
ASTRBOT_TURN_HEADROOM = 2
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
    # 开头块索引里实际出现过的文件引用；指针行据此决定是否附描述
    index_refs: set[str] = field(default_factory=set)
    awaiting_calibration: bool = False
    # 上一轮的 usage 样本待消费：校准延迟到下一轮请求开头，那时才能从
    # 回存的历史里看出上一轮是否走了工具循环（那样的样本不能学）
    pending_calibration: bool = False
    warned_window: bool = False
    warned_summarizer: bool = False
    warned_clip: bool = False
    # 工具循环中途累计清理的工具结果条数（仅内存，供 mb_status 展示）
    loop_cleared: int = 0

    def clear_index(self) -> None:
        """作废索引快照以及随快照生成的人物档案与索引引用集合。"""
        self.index_block = None
        self.index_loaded = False
        self.index_pin_key = ()
        self.index_pinned = {}
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
        self._sessions: OrderedDict[str, RuntimeSession] = OrderedDict()
        self._tasks: set[asyncio.Task] = set()
        self._warned_threshold = False
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
        self._pending_loop: asyncio.Task | None = None
        # 共享人物空间：成员表从配置解析
        self._sharing = sharing.Membership()
        self._sharing_warned: set[str] = set()
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
        return prompts.memory_guidance(
            self._global_on(),
            self._forgetting_on(),
        )

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
        # 配置页改了共享成员时，索引/召回立即按新成员表走
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
        self._label_sender(event, req)

        n_system = split_leading_system(contexts)
        memory_on = self._bool("enable_memory", True)

        # ---- 把本轮用户消息记进待整理记录；整理由后台队列统一执行 ----
        if memory_on:
            self._ensure_pending_loop()
            self._record_pending_user(event, req, now)
            # ---- 遗忘扫描：长期没用到的记忆退出索引与召回（后台，按目录节流） ----
            self._maybe_forget(event, now)
            # ---- 定期整理：到了时间又攒够新记忆的作用域在后台复盘 ----
            self._maybe_tidy(event, now)

        # ---- 记忆索引块（对话内快照；新对话、压缩后、WebUI 改动后从磁盘刷新） ----
        index_msg = None
        recall_msg = None
        if memory_on:
            block = await self._index_block(rt, event, record_usage=True)
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
        # 压缩关闭时插件完全不碰上下文，这项也一起停用
        compress_on = self._bool("enable_compression", True)
        idle_minutes = self._int("tool_result_idle_minutes", 60)
        cache_cold = (
            compress_on
            and idle_minutes > 0
            and state.last_request_at > 0
            and now - state.last_request_at >= idle_minutes * 60
        )
        cleared_total = 0

        def current_tail() -> list[dict]:
            nonlocal cleared_total
            # 压缩关闭时不看水位线：已持久化的摘要与水位线不能继续裁剪请求
            mark = n_system
            if compress_on and state.watermark:
                mark = max(state.watermark, n_system)
            tail = contexts[mark:]
            if cache_cold:
                tail, cleared = compress.clear_old_tool_results(
                    tail, self._int("keep_recent_turns", 3)
                )
                cleared_total = cleared
            return tail

        # ---- 抢先压缩：token 阈值，或 AstrBot 的轮数上限 ----
        window, window_note = await self._resolve_window(event)
        if compress_on and window <= 0 and not rt.warned_window:
            rt.warned_window = True
            logger.warning(LOG_PREFIX + f"会话 {umo} 压缩已停用：{window_note}")

        tail = current_tail()
        clipped = False
        if compress_on:
            turn_limit = self._astrbot_turn_limit(umo)
            threshold_tokens = int(window * self._threshold())
            fixed = overhead + tools_cost + index_cost + recall_cost

            def total_now() -> int:
                return fixed + estimator.text(state.summary) + estimator.messages(tail)

            total = total_now() if window > 0 else 0
            # AstrBot 也会看到本轮注入的索引/摘要/召回块，轮数要一并算
            extra_messages = (
                (1 if index_msg else 0)
                + (1 if state.summary else 0)
                + (1 if recall_msg else 0)
            )
            if self._compression_triggered(
                tail, total, window, turn_limit, extra_messages
            ):
                await self._compress(event, rt, umo, contexts, total)
                tail = current_tail()
                total = total_now() if window > 0 else 0

            # 兜底裁剪只在压缩尝试之后仍超阈值时接管（摘要失败、冷却期、
            # 单轮过大）。它的目标水位低于触发阈值，若每轮无条件执行，
            # 会把总量永远摁在触发线之下，滚动摘要一次也轮不到。
            # 水位线不动；被裁内容会随平台回存历史而丢失，属有损最后手段。
            if window > 0 and total > threshold_tokens:
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
            if (
                window > 0
                and self._bool("loop_guard", True)
                and LOOP_HOOKS_READY
            ):
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
        # 压缩关闭时不注入已持久化的摘要：那是本插件上一次压缩的产物，继续
        # 注入等于仍在用摘要替换原文。
        summary_msg = (
            prompts.build_summary_message(state.summary, state.compressed_at)
            if compress_on and state.summary
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
        first_line = prompt.split("\n", 1)[0]
        if first_line == tag:
            return  # 同一请求被重复处理时不叠加标注
        req.prompt = f"{tag}\n{prompts.neutralize_sender_forgery(prompt)}"

    async def _index_block(
        self,
        rt: RuntimeSession,
        event: AstrMessageEvent,
        *,
        record_usage: bool = False,
    ) -> str | None:
        """开头块：索引 + （私聊时）发送者人物档案，共用一个快照。

        快照只在对话切换、压缩、重置、管理页改动或相关配置变化时刷新；
        记忆不变时逐轮逐字不变，才能命中模型服务的前缀缓存。record_usage
        只在真正发给模型的请求路径上置真：管理指令里估算开头块时不能把
        人物档案记成用过。
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
            global_store = self._global_scope()
            await global_store.ensure_index_folded()
            global_index = (await global_store.load_index()).text
        session_store = self._session_scope(event)
        # 升级后的第一次注入把旧格式索引（散落的 daily 行）改写成折叠块，
        # 不必等到下一次写入/删除才同步。
        await session_store.ensure_index_folded()
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
    ) -> dict | None:
        """挑出本轮相关的记忆，拼成附在新消息之前的召回块；失败只记日志。

        rt 带上开头块的快照信息：快照里已钉住的人物档案不再进入召回候选，
        快照之后被改过的档案则附上最新全文并注明"以这里为准"。
        """
        mode = self._str("recall_mode", "keyword").strip().lower()
        limit = self._int("recall_max_files", recall.DEFAULT_MAX_FILES)
        if mode == "off" or limit <= 0:
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
            model_choice = None
            if mode == "llm" and limit > 0 and query.strip():
                # 人物文件只走身份必选，不让模型从别的群的人里挑
                model_choice = await self._select_by_model(
                    umo, [c for c in candidates if c.scope != "people"], query, limit
                )
            platform_id = self._event_actor(event)
            # 挑选、读文件、分档、截断与渲染统一在 core.recall 里
            plan = await recall.assemble_recall(
                stores,
                candidates,
                query=query,
                forced_files=forced,
                limit=limit,
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

    def _maybe_tidy(self, event: AstrMessageEvent, now: float) -> None:
        """本会话涉及的每个作用域至多每隔几分钟看一眼该不该整理，要整理的放后台。"""
        mode = self.tidy_mode()
        if mode == tidy.MODE_OFF:
            return
        umo = str(event.unified_msg_origin)
        # (作用域, 目录名, 索引快照要作废的会话)；含义同 _maybe_forget
        targets: list[tuple[str, str | None, str | None]] = [
            ("session", self._store.session_scope(umo).root.name, umo)
        ]
        if self._global_on():
            targets.append(("global", None, None))
        # 与遗忘扫描、后台整理一致：目录与权限都按事件的 platform_id 取；
        # 共享人物空间只有 edit 级成员的会话能触发定期自动整理，read /
        # append 成员不会间接改写共享文件。管理页手动整理不受此限制。
        people_store, _ = self._people_store_for_event(event)
        if people_store is not None and self._can_maintain_people(
            self._event_actor(event), people_store
        ):
            targets.append(("people", people_store.root.name, ""))
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
            platform=self._event_actor(event) or self._platform_of(umo),
        )
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
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        book = getattr(self, "_pending", None)
        if not umo or book is None:
            return
        key = memstore.safe_key(umo)
        state, found, dropped = book.finish_reply(
            key,
            umo,
            self._completion_text(resp),
            now,
            platform=self._event_actor(event) or self._platform_of(umo),
        )
        if dropped:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的待整理记录超过上限，丢弃了最旧的 {dropped} 轮"
            )
        if not found:
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
                self._pending_loop_run(), name="memory_beyond:pending-sweep"
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
        self._spawn(
            self._run_write_for(key, umo, reason), f"write:{key}"
        )

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

    def _write_stores(
        self, umo: str, platform: str, admin: bool
    ) -> tuple[dict[str, memstore.ScopeStore], str | None, bool]:
        """这次后台整理可用的作用域与共享人物空间权限。"""
        stores: dict[str, memstore.ScopeStore] = {
            "session": self._store.session_scope(umo)
        }
        platform = platform or self._platform_of(umo)
        people_store = self._store.people_scope(platform)
        people_shared = self._store.is_shared_people_scope(people_store)
        permission = self._people_permission(platform) if people_shared else None
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
        seqs = {record.seq for record in records}
        sender = records[-1].sender
        admin = records[-1].admin
        platform = state.platform or self._platform_of(umo)
        actor = f"extract@{platform}"
        stores, permission, people_shared = self._write_stores(
            umo, platform, admin
        )
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
        text, attempts_log = await self._call_summarizer(
            umo, context.prompt, preferred_id=self._memory_provider_id()
        )
        if not text:
            message = "整理模型调用失败：" + (
                "；".join(attempts_log) or "没有可用的模型提供商"
            )
            failed, abandoned = self._pending.record_failure(key, now, message)
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
        raw_ops, journal_text = writer.parse_response(text)
        if raw_ops is None:
            failed, abandoned = self._pending.record_failure(
                key, now, "整理模型输出无法解析"
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
            journal_files=journal_files,
        )
        for item in skipped:
            logger.info(LOG_PREFIX + f"会话 {umo} 后台整理跳过：{item}")
        result = await writer.apply_operations(
            stores,
            ops,
            snapshots=context.snapshots,
            now=now,
            actor=actor,
            source=sender,
        )
        for item in result.skipped:
            logger.info(LOG_PREFIX + f"会话 {umo} 后台整理未写入：{item}")
        if result.stale:
            logger.info(LOG_PREFIX + f"会话 {umo} {result.message}")
            self._pending.mark_attempt(key, now, result.message)
            return ""
        if ops and result.applied == 0:
            # 模型给的操作一条都没写成功：记为一次失败，满 3 次后放弃
            failed, abandoned = self._pending.record_failure(
                key, now, "整理操作全部未写入"
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
                    + f"会话 {umo} 后台整理的操作全部未写入，保留记录下次重试"
                )
            return ""
        journal_file = ""
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
        message = result.message
        if journal_file:
            message += f"；日志 {'新开' if journal_new else '续写'} {journal_file}"
        self._pending.drop_records(key, seqs, now, message)
        self._invalidate_changes(umo, result.changes)
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
        """记忆整理模型：新键优先，旧键 consolidate_provider_id 兼容读取。"""
        return self._str("memory_provider_id").strip() or self._str(
            "consolidate_provider_id"
        ).strip()

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

        max_chars = max(
            200, self._int("summary_max_chars", prompts.SUMMARY_MAX_CHARS_DEFAULT)
        )
        prompt = prompts.build_summary_prompt(
            self._str("summary_prompt_template"),
            state.summary,
            transcript,
            now=prompts.format_time(now),
            max_chars=max_chars,
        )
        text, attempts_log = await self._call_summarizer(umo, prompt)
        summary = prompts.parse_summary(text) if text else ""
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
        # 压缩后从磁盘重新注入记忆索引：清空快照，下次请求重新加载。
        # 索引不是靠"被摘要保留下来"，而是每次从源头重新加载。
        rt.clear_index()

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
        """管理页整理记忆用的模型调用：记忆整理模型优先，留空同摘要提供商。"""
        return await self._call_summarizer(
            None, prompt, preferred_id=self._memory_provider_id()
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

    # ======================================================== AstrBot 配置

    def _astrbot_config(self, umo: str | None = None) -> dict:
        """读取 AstrBot 的全局/会话配置；取不到时返回空字典。

        跟随 AstrBot 的窗口与轮数上限都从这里取。AstrBot 的配置结构随版本
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

    def _astrbot_compression_config(self, umo: str | None = None) -> dict:
        """AstrBot 的 `agent_runner.config.compression` 段（4.29.4 的配置路径）。"""
        return self._dict_at(
            self._astrbot_config(umo), "agent_runner", "config", "compression"
        )

    def _astrbot_turn_limit(self, umo: str | None = None) -> int:
        """AstrBot 的上下文轮数上限；-1 表示未设置。"""
        try:
            return int(self._astrbot_compression_config(umo).get("max_turns", -1))
        except (TypeError, ValueError):
            return -1

    def _user_identification_on(self, umo: str | None = None) -> bool:
        """AstrBot 的「用户识别」开关：开启时它自己也会在提示词前带用户 ID。"""
        settings = self._dict_at(self._astrbot_config(umo), "provider_settings")
        return bool(settings.get("identifier", False))

    # ========================================================== 窗口探测

    async def _resolve_window(self, event: AstrMessageEvent) -> tuple[int, str]:
        umo = str(getattr(event, "unified_msg_origin", "") or "") or None
        return await self._resolve_window_for(umo)

    async def _resolve_window_for(self, umo: str | None) -> tuple[int, str]:
        """确定上下文窗口：手动配置 > 跟随 AstrBot > 按模型名探测。

        跟随 AstrBot 时与它取窗口的顺序一致：提供商配置里的窗口 → 内置
        模型表 `LLM_METADATAS` → AstrBot 的兜底值。取到的值超过
        `UNTRUSTED_WINDOW_LIMIT` 时视为按旧 README 调大过的兜底值，不可信，
        改用插件原有的模型名探测；仍然失败才停用压缩并给出提示。
        """
        configured = self._int("max_context_tokens", 0)
        if configured > 0:
            return configured, "手动配置"
        provider = await self._current_provider(umo)
        window, note = self._astrbot_window(provider, umo)
        if window > UNTRUSTED_WINDOW_LIMIT:
            note = (
                f"{note}的 {window} tokens 超过 {UNTRUSTED_WINDOW_LIMIT}，"
                "视为按旧 README 调大过的兜底值，不可信"
            )
            window = 0
        if window > 0:
            return window, note
        auto = self._model_window(provider)
        if auto > 0:
            suffix = f"；{note}" if note else ""
            return auto, f"按模型 {self._provider_model_name(provider)} 自动探测{suffix}"
        model = self._provider_model_name(provider)
        reason = note or f"模型名：{model or '未知'}"
        return 0, (
            f"自动探测模型窗口失败（{reason}），"
            "请在插件配置中手动指定 max_context_tokens"
        )

    def _astrbot_window(self, provider, umo: str | None) -> tuple[int, str]:
        """按 AstrBot 的顺序取窗口；取不到返回 (0, 原因或空串)。"""
        # 1. 提供商配置里的窗口（AstrBot 在请求发出前已填好）
        config = getattr(provider, "provider_config", None)
        if isinstance(config, dict):
            try:
                value = int(config.get("max_context_tokens", 0) or 0)
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
                self._astrbot_compression_config(umo).get("fallback_max_tokens", 0)
                or 0
            )
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value, "跟随 AstrBot 的上下文窗口兜底值"
        return 0, ""

    @staticmethod
    def _model_window(provider) -> int:
        model = MemoryBeyondPlugin._provider_model_name(provider)
        lowered = model.lower()
        for needle, window in MODEL_WINDOWS:
            if needle in lowered:
                return window
        return 0

    async def compression_status(self, umo: str | None = None) -> dict:
        """管理页首页与 /mb_status 共用的压缩跟随状态。"""
        window, note = await self._resolve_window_for(umo)
        limit = self._astrbot_turn_limit(umo)
        keep = self._int("keep_recent_turns", 3)
        return {
            "enabled": self._bool("enable_compression", True),
            "window": window,
            "windowNote": note,
            "turnLimit": limit,
            "keepRecentTurns": keep,
            "turnLimitWarning": bool(
                limit > 0 and limit <= keep + ASTRBOT_TURN_HEADROOM
            ),
            "userIdentifier": self._user_identification_on(umo),
        }

    def _compression_triggered(
        self,
        tail: list[dict],
        total: int,
        window: int,
        turn_limit: int,
        extra_messages: int = 0,
    ) -> bool:
        """本轮是否触发压缩：token 超过阈值，或达到 AstrBot 轮数上限 - 2。

        窗口未知时压缩已经停用（日志会提示），轮数触发也不再生效——否则
        提示与实际行为矛盾。
        """
        if window <= 0:
            return False
        if total > int(window * self._threshold()):
            return True
        return self._turn_limit_reached(tail, turn_limit, extra_messages)

    @staticmethod
    def _turn_limit_reached(
        messages: list[dict], turn_limit: int, extra_messages: int = 0
    ) -> bool:
        """AstrBot 的 truncate_by_turns 按非 system 消息条数对半计轮。

        每轮通常两条消息（user + assistant），工具调用和注入块会多出消息。
        这里按 AstrBot 的实际口径算，并把本轮还会注入的索引/摘要/召回块
        一并算上；达到上限减 2 时抢先压缩，免得被 AstrBot 直接截断。
        """
        if turn_limit <= 0:
            return False
        pairs = (len(messages) + extra_messages) // 2
        return pairs >= max(1, turn_limit - ASTRBOT_TURN_HEADROOM)

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
        """读取长期记忆文件的完整内容。索引只是目录：回答涉及某人、某事的细节之前，先用本工具取回对应文件再作答，不要凭索引行猜。

        Args:
            scope(string): 作用域，global（机器人自我的全局记忆）、session（当前会话的记忆）或 people（人物记忆，一人一个 user-<数字ID>.md；通常同一平台所有会话共享）
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
        """用户明确要求记住、更正或忘掉某件事时调用，用一句话写清要求。插件会把这条要求和发送者、时间记进本会话的待整理记录，在本轮回复结束后整理进长期记忆。

        Args:
            content(string): 一句话写清用户的要求，例如"用户要求记住他的生日是 3 月 5 日"或"用户要求忘掉上次说的搬家计划"
        """
        if not self._bool("enable_memory", True):
            return "记忆功能已在插件配置中停用。"
        content = (content or "").strip()
        if not content:
            return "content 不能为空：请用一句话写清用户要求记住、更正或忘掉什么。"
        if len(content) > pending.MAX_NOTE_CHARS:
            content = content[: pending.MAX_NOTE_CHARS]
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
            platform=self._event_actor(event) or self._platform_of(umo),
        )
        if dropped:
            logger.warning(
                LOG_PREFIX
                + f"会话 {umo} 的待整理记录超过上限，丢弃了最旧的 {dropped} 轮"
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
        """在长期记忆里全文搜索。索引被截断、或不确定某条记忆是否存在时，用本工具查找；它直接搜文件内容，不依赖索引。

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
        compress_on = self._bool("enable_compression", True)
        estimator = tokens.TokenEstimator(state.ratio)
        n_system = split_leading_system(contexts)
        if (
            compress_on
            and compress.state_matches(state, str(cid), contexts)
            and state.watermark
        ):
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
                if compress_on and state.summary
                else 0
            ),
            "recall": 0,
            "history": estimator.messages(contexts[mark:]),
        }
        return sum(parts.values()), parts

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("mb_status")
    async def show_status(self, event: AstrMessageEvent):
        """查看鲸群记忆当前会话的记忆与压缩状态。"""
        umo = str(event.unified_msg_origin)
        # 配置页可能刚改过共享成员，状态里的目录与权限要和工具一致
        self._refresh_sharing()
        rt = await self._runtime(umo)
        state = rt.state
        compression = await self.compression_status(umo)
        window = compression["window"]
        window_note = compression["windowNote"]
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
        configured_id = self._str("summary_provider_id").strip() or "（未配置，用当前对话提供商）"
        tidy_id = self._memory_provider_id() or "（未配置，同摘要提供商）"

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
        limit = compression["turnLimit"]
        limit_line = (
            f"AstrBot 轮数上限：{limit}（达到 "
            f"{max(1, limit - ASTRBOT_TURN_HEADROOM)} 轮也触发压缩）"
            if limit > 0
            else "AstrBot 轮数上限：未设置"
        )
        lines = [
            "鲸群记忆 状态",
            f"记忆：{'启用' if self._bool('enable_memory', True) else '停用'}"
            f"（{global_part} / {people_part} / 会话 {len(session_files)} 个文件）",
            f"压缩：{'启用' if compression['enabled'] else '停用（交给 AstrBot 自带的压缩）'}",
            f"上下文窗口：{window if window > 0 else '未知'}（{window_note}）",
            limit_line,
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
            f"后台整理：{self._pending_label(umo)}",
            f"记忆遗忘：{self._forget_label(umo)}",
            f"共享人物空间：{self._sharing_label(umo)}",
            f"会话长文件：{self._long_files_label(long_files)}",
            f"定期整理：{self._tidy_label(umo)}",
            "记忆管理页：AstrBot WebUI 侧边栏「插件页面」",
        ]
        if compression["turnLimitWarning"]:
            lines.insert(
                -1,
                "⚠️ AstrBot 的轮数上限不大于本插件保留轮数 + 2"
                f"（{compression['keepRecentTurns'] + ASTRBOT_TURN_HEADROOM}）："
                "插件来不及抢先压缩，旧轮次会由 AstrBot 直接截断；"
                "请在 AstrBot 调大轮数上限或调小「保留原文的最近轮数」",
            )
        if compression["userIdentifier"]:
            lines.insert(
                -1,
                "提示：AstrBot 的「用户识别」开着，发送者会被标注两次；"
                "可以关掉 AstrBot 的「用户识别」，由本插件接管",
            )
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
        """卸载/重载时：撤下 WebUI 接口、取消后台整理与扫描任务、登记簿落盘。

        压缩状态与记忆在每次变更时已即时落盘。
        """
        self.dashboard.unregister()
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._flush_registry()
