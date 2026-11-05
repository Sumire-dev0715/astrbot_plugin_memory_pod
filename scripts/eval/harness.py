"""评测运行环境：AstrBot 4.29.4 的请求流程、事件替身与插件实例。

按 AstrBot 4.29.4 的真实顺序驱动一轮请求（依据见各函数注释）：

    on_llm_request（插件注入记忆规范、开头块、召回块、提醒）
    → AgentRunner.reset：历史 + 本轮用户消息（正文后接 _no_save 附加内容）
      + 开头 system 消息
    → 每一步：ContextManager.process（先按轮数截断，再按 0.82 阈值压缩）
      → 模型（带工具循环；达到步数上限后拔掉工具、注入步数耗尽提示）
    → on_llm_response（记账）
    → 回存历史（跳过开头 system 消息，丢掉 _no_save 内容块）

对应 AstrBot 源码：
- core/pipeline/process_stage/method/agent_sub_stages/internal.py:307
  （on_llm_request 在 build_main_agent 之后、runner 处理上下文之前）
- core/agent/runners/tool_loop_agent_runner.py:324-328（system 插到开头）、
  807-832（每步先处理上下文）、1146-1180（步数耗尽）
- core/provider/entities.py:291-420（本轮用户消息 + 附加内容）
- core/agent/message.py:338-354（回存时按内容块丢弃 _no_save）
- core/agent/context/truncator.py（按轮数截断）
- core/agent/context/compressor.py:158-310（0.82 阈值与摘要消息）
- core/agent/context/manager.py:46-82（先截断再压缩）

替身只在评测进程的临时数据目录里使用，不参与插件运行时逻辑。
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import re
import sys
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from . import astrbot_stub
from .model_client import MAX_TOOL_STEPS, EvalError, ModelReply, ToolCall

REPO_ROOT = Path(__file__).resolve().parents[2]
# 假时钟起点：2026-10-01 10:00 UTC，远离真实时间，避免依赖机器时区之外的东西
CLOCK_START = 1790848800.0
# AstrBot 的上下文压缩阈值与保留最近上下文比例
COMPRESSION_THRESHOLD = 0.82
KEEP_RECENT_RATIO = 0.15
# AstrBot 4.29.4 在达到工具步数上限后注入的提示词
# （core/agent/runners/tool_loop_agent_runner.py:126）。
MAX_STEPS_REACHED_PROMPT = (
    "[SYSTEM NOTICE: Agent step budget exhausted] "
    "Remaining steps: 0. Stop using tools; summarize results and unfinished work. "
    "If unfinished, tell the user they can simply ask you to continue with a fresh "
    "step budget. Optionally, they can raise the tool-call round limit in "
    "AstrBot WebUI to allow longer runs."
)
# AstrBot 摘要压缩后写回历史的用户消息前缀（compressor.py:297）
ASTRBOT_SUMMARY_PREFIX = "Our previous history conversation summary: "


@dataclass
class FakeClock:
    """场景内可控时钟；秒级精度足够日志按天分段。"""

    now: float = CLOCK_START

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += max(0.0, float(seconds))

    def advance_days(self, days: float) -> None:
        self.advance(float(days) * 86400)


_CLOCK: ContextVar[FakeClock | None] = ContextVar("mb_eval_clock", default=None)


def _dispatched_time() -> float:
    clock = _CLOCK.get()
    return clock.time() if clock is not None else _REAL_TIME()


_REAL_TIME = time.time
_TIME_PATCH_LOCK = threading.Lock()
_TIME_PATCH_REFCOUNT = 0

# 插件包在进程内只导入一次：场景并发运行时共享同一个类对象，避免反复
# 删改 sys.modules 造成竞态。
_PLUGIN_LOCK = threading.Lock()
_PLUGIN_CACHE: tuple[Any, Any] | None = None


def get_plugin_class():
    global _PLUGIN_CACHE
    with _PLUGIN_LOCK:
        if _PLUGIN_CACHE is None:
            _PLUGIN_CACHE = astrbot_stub.load_plugin(REPO_ROOT)
        return _PLUGIN_CACHE


@contextmanager
def patched_time():
    """把 time.time 换成按任务 ContextVar 分发的假时钟。

    各模块都是 `import time` 后调用 time.time，因此一次替换即可覆盖插件
    全部写入戳、日志分段与日期判断；未设置场景上下文时返回真实时间。
    用引用计数支持并发场景：最后一个使用者退出时才恢复。
    """
    global _TIME_PATCH_REFCOUNT
    with _TIME_PATCH_LOCK:
        if _TIME_PATCH_REFCOUNT == 0:
            time.time = _dispatched_time
        _TIME_PATCH_REFCOUNT += 1
    try:
        yield
    finally:
        with _TIME_PATCH_LOCK:
            _TIME_PATCH_REFCOUNT -= 1
            if _TIME_PATCH_REFCOUNT == 0:
                time.time = _REAL_TIME


def load_default_config() -> dict:
    schema = json.loads((REPO_ROOT / "_conf_schema.json").read_text("utf-8"))
    config: dict = {}
    for key, meta in schema.items():
        config[key] = copy.deepcopy(meta.get("default"))
    return config


# ---------------------------------------------------------------- 工具 schema

_ARGS_LINE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*:\s*(.*)$"
)
_TYPE_MAP = {
    "string": "string",
    "str": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "object": "object",
    "dict": "object",
}


def tool_description(func) -> str:
    """AstrBot 取 docstring.description：Args 之前的全部正文（短描述 + 长描述）。

    register/star_handler.py:657-701 用 docstring_parser，description 是
    短描述与长描述按空行拼接的结果，不只是第一段。
    """
    doc = inspect.cleandoc(func.__doc__ or "")
    description = doc.split("Args:", 1)[0].strip()
    return description


def tool_schema(func) -> dict:
    """按 AstrBot register_llm_tool 的口径还原 OpenAI 工具声明。"""
    doc = inspect.cleandoc(func.__doc__ or "")
    properties: dict[str, dict] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if not in_args:
            continue
        match = _ARGS_LINE.match(line)
        if not match:
            continue
        name, type_name, arg_desc = match.groups()
        properties[name] = {
            "type": _TYPE_MAP.get(type_name.strip(), type_name.strip()),
            "description": arg_desc.strip(),
        }
    return {
        "type": "function",
        "function": {
            "name": func.__astrbot_llm_tool__,
            "description": tool_description(func),
            "parameters": {"type": "object", "properties": properties},
        },
    }


def collect_tools(plugin) -> tuple[dict[str, Callable], list[dict]]:
    tools: dict[str, Callable] = {}
    schemas: list[dict] = []
    for value in vars(type(plugin)).values():
        name = getattr(value, "__astrbot_llm_tool__", None)
        if not name:
            continue
        bound = getattr(plugin, value.__name__)
        tools[name] = bound
        schemas.append(tool_schema(value))
    schemas.sort(key=lambda item: item["function"]["name"])
    return tools, schemas


# ---------------------------------------------------------------- 事件与 provider


@dataclass
class Persona:
    sender_id: str
    name: str
    admin: bool = False
    # 事件所在的机器人（平台适配器）ID；小本本按它分目录。
    platform_id: str = "10001"
    group_card: str = ""


class EvalEvent:
    """AstrMessageEvent 的最小替身；字段与 4.29.4 的插件用法对齐。"""

    def __init__(self, chat: "EvalChat", persona: Persona) -> None:
        self.chat = chat
        self.persona = persona
        self.unified_msg_origin = chat.umo
        self._extra: dict[str, Any] = {}
        raw_sender = {"nickname": persona.name}
        if persona.group_card:
            raw_sender["card"] = persona.group_card
        self.message_obj = SimpleNamespace(
            group_id=chat.group_id if chat.mode == "group" else "",
            group=SimpleNamespace(
                group_name=chat.group_name if chat.mode == "group" else ""
            ),
            raw_message={"sender": raw_sender},
        )

    def get_platform_id(self) -> str:
        return self.persona.platform_id

    def get_platform_name(self) -> str:
        return "aiocqhttp"

    def get_sender_id(self) -> str:
        return self.persona.sender_id

    def get_sender_name(self) -> str:
        return self.persona.name

    def get_self_id(self) -> str:
        return self.chat.self_id

    def get_messages(self) -> list:
        return []

    def get_message_str(self) -> str:
        return ""

    def is_admin(self) -> bool:
        return bool(self.persona.admin)

    def set_extra(self, key: str, value: Any) -> None:
        self._extra[key] = value

    def get_extra(self, key: str | None = None, default=None):
        if key is None:
            return self._extra
        return self._extra.get(key, default)


@dataclass
class EvalChat:
    umo: str
    mode: str = "private"
    group_id: str = "20000001"
    group_name: str = "评测群"
    self_id: str = "90000000"


def build_chat(persona: Persona, scenario: "Scenario", mode: str | None = None) -> EvalChat:
    mode = mode or scenario.mode
    if mode == "group":
        target = scenario.group_id
        msg_type = "GroupMessage"
    else:
        target = persona.sender_id
        msg_type = "FriendMessage"
    # AstrBot 的 UMO 是 platform_id:message_type:session_id
    # （core/platform/message_session.py:19）。
    umo = f"{persona.platform_id}:{msg_type}:{target}"
    return EvalChat(
        umo=umo,
        mode=mode,
        group_id=scenario.group_id,
        group_name=scenario.group_name,
        self_id=scenario.self_id,
    )


class EvalToolSet:
    """AstrBot ToolSet 的最小子集：插件用它估算本轮工具声明开销。"""

    def __init__(self, schemas: list[dict]) -> None:
        self.schemas = schemas

    def empty(self) -> bool:
        return not self.schemas

    def openai_schema(self) -> list[dict]:
        return self.schemas


class EvalProvider:
    """Provider 替身：窗口按评测场景设置，管理页整理不调用真实接口。"""

    def __init__(self, model: str, window: int) -> None:
        self.model = model
        self.provider_config = {
            "id": "eval-provider",
            "max_context_tokens": int(window),
            "model_config": {"model": model},
        }

    def get_model(self) -> str:
        return self.model

    def meta(self):
        return SimpleNamespace(id="eval-provider", name="eval")

    async def text_chat(self, prompt: str | None = None, **kwargs):
        from .astrbot_stub import LLMResponse

        # 评测场景不跑管理页整理；返回一段固定文本即可，绝不访问真实接口。
        return LLMResponse(
            role="assistant",
            completion_text="（评测桩不调用整理模型）",
            raw_completion={"usage": {"prompt_tokens": 1}},
        )


class EvalContext:
    """插件 Context 的最小子集。"""

    def __init__(
        self,
        provider: EvalProvider,
        config: dict,
        history_getter: Callable[[str], list[dict]],
    ) -> None:
        self.provider = provider
        self.config = config
        self._history_getter = history_getter
        self.registered_web_apis: list = []
        self.platform_manager = SimpleNamespace(get_insts=self._platform_insts)
        self.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=self._curr_cid,
            get_conversation=self._get_conversation,
        )

    @staticmethod
    def _platform_insts():
        def inst(platform_id: str):
            return SimpleNamespace(
                meta=lambda: SimpleNamespace(id=platform_id, name="aiocqhttp"),
                bot_name="阿鲸",
            )

        return [inst("10001"), inst("10002")]

    async def _curr_cid(self, umo: str) -> str:
        return f"cid-{umo}"

    async def _get_conversation(self, umo: str, cid: str):
        return SimpleNamespace(
            cid=cid,
            token_usage=0,
            history=json.dumps(self._history_getter(umo), ensure_ascii=False),
        )

    def get_config(self, umo: str | None = None) -> dict:
        return self.config

    async def get_using_provider(self, umo: str | None = None):
        return self.provider

    async def get_provider_by_id(self, provider_id: str | None = None, **kwargs):
        if provider_id in (None, "", "eval-provider"):
            return self.provider
        return None

    def get_llm_tool_manager(self):
        class _Manager:
            @staticmethod
            def get_full_tool_set():
                return EvalToolSet([])

            @staticmethod
            def get_builtin_tool(_tool):
                return None

        return _Manager()


# ---------------------------------------------------------------- 数据目录视图


@dataclass
class MemoryFile:
    scope: str
    key: str
    file: str
    text: str
    mem_type: str
    description: str
    body: str
    is_journal: bool


class EvalEnv:
    """一个场景一次的运行环境与观测数据。"""

    def __init__(
        self,
        scenario: "Scenario",
        data_dir: Path,
        client,
        clock: FakeClock,
    ) -> None:
        self.scenario = scenario
        self.data_dir = Path(data_dir)
        self.client = client
        self.clock = clock
        self.model = "eval-model"
        self.plugin = None
        self.plugin_module = None
        self.memstore = None
        self.journal = None
        self.prompts = None
        self.injection = None
        self.recall = None
        self.astrbot_config = {
            "provider_settings": {"identifier": False, "enable": True},
            "agent_runner": {
                "config": {
                    # AstrBot 的上下文配置段；max_turns 对应
                    # resolve_context_compression_config 的 max_context_length。
                    "com" "pression": {
                        "max_turns": scenario.max_turns,
                        "fallback_max_tokens": 0,
                    }
                }
            },
        }
        self.history: dict[str, list[dict]] = {}
        self.turns: list["TurnResult"] = []
        self.probes: dict[str, Any] = {}
        self.memory_dir = self.data_dir / "memories"

    # ------------------------------------------------------------ 模块

    def _load_modules(self) -> None:
        prefix = astrbot_stub.PLUGIN_PACKAGE
        self.memstore = sys.modules[f"{prefix}.core.memstore"]
        self.journal = sys.modules[f"{prefix}.core.journal"]
        self.prompts = sys.modules[f"{prefix}.core.prompts"]
        self.injection = sys.modules[f"{prefix}.core.injection"]
        self.recall = sys.modules[f"{prefix}.core.recall"]

    # ------------------------------------------------------------ 文件

    def memory_files(self) -> list[MemoryFile]:
        """三层记忆目录里的全部在册文件；不含 MEMORY.md 索引与已遗忘副本。"""
        result: list[MemoryFile] = []
        base = self.memory_dir
        for scope in ("board", "notebook", "whisper"):
            root = base / scope
            if not root.is_dir():
                continue
            if scope == "board":
                pairs = [("", root)]
            else:
                pairs = [
                    (path.name, path)
                    for path in sorted(root.iterdir())
                    if path.is_dir() and not path.name.startswith(".")
                ]
            for key, directory in pairs:
                for path in sorted(directory.glob("*.md")):
                    if path.name == self.memstore.INDEX_FILENAME:
                        continue
                    try:
                        text = path.read_text("utf-8")
                    except OSError:
                        continue
                    doc = self.memstore.parse_memory(text)
                    result.append(
                        MemoryFile(
                            scope=scope,
                            key=key,
                            file=path.name,
                            text=text,
                            mem_type=doc.type,
                            description=doc.description,
                            body=doc.body,
                            is_journal=self.journal.is_journal(
                                path.name, doc.type
                            ),
                        )
                    )
        return result

    def notebook_files(self, persona: Persona) -> list[MemoryFile]:
        key = self.memstore.safe_key(persona.platform_id)
        return [
            item
            for item in self.memory_files()
            if item.scope == "notebook" and item.key == key
        ]

    def whisper_files(
        self, persona: Persona, mode: str | None = None
    ) -> list[MemoryFile]:
        key = self.memstore.safe_key(self.umo_for(persona, mode))
        return [
            item
            for item in self.memory_files()
            if item.scope == "whisper" and item.key == key
        ]

    def board_files(self) -> list[MemoryFile]:
        return [item for item in self.memory_files() if item.scope == "board"]

    def umo_for(self, persona: Persona, mode: str | None = None) -> str:
        return build_chat(persona, self.scenario, mode).umo

    def event_for(self, persona: Persona, mode: str | None = None) -> EvalEvent:
        return EvalEvent(build_chat(persona, self.scenario, mode), persona)

    def add_history(self, umo: str, messages: list[dict]) -> None:
        self.history.setdefault(umo, []).extend(
            copy.deepcopy(messages)
        )

    async def seed(
        self,
        store,
        file: str,
        description: str,
        body: str,
        mem_type: str = "reference",
        signature: str = "",
        source: str = "",
    ) -> None:
        """往临时数据目录里放一条合成记忆；写入失败直接中止场景。"""
        content = self.memstore.compose_memory(
            file[: -len(self.memstore.MEMORY_FILE_SUFFIX)],
            description,
            mem_type,
            body,
            signature=signature,
        )
        report = await store.write(file, content, actor="eval", source=source)
        if not report.ok:
            raise RuntimeError(f"评测准备写入失败：{report.message}")


# ---------------------------------------------------------------- 场景数据


@dataclass
class Turn:
    text: str
    persona: Persona
    # 本轮开始前把时钟推进这么久（天），用于“隔几天再聊”。
    advance_days: float = 0.0
    # 本轮用群聊还是私聊；None 表示跟场景默认值一致。
    mode: str | None = None


@dataclass
class Check:
    name: str
    fn: Callable[[EvalEnv], tuple[bool, str]]
    critical: bool = False
    category: str = "memory"


@dataclass
class Scenario:
    name: str
    title: str
    turns: list[Turn]
    checks: list[Check]
    mode: str = "private"
    group_id: str = "20000001"
    group_name: str = "评测群"
    self_id: str = "90000000"
    config: dict = field(default_factory=dict)
    provider_window: int = 1_000_000
    max_turns: int = -1
    setup: Callable[[EvalEnv], Awaitable[None] | None] | None = None


@dataclass
class TurnResult:
    text: str
    reply: str
    sent_prompt: str = ""
    tool_calls: list[tuple[str, dict, str]] = field(default_factory=list)
    prompt_chars: int = 0
    # 本轮请求的系统提示词（静态记忆规范在这里）与临时附加内容
    # （开头块、召回块、提醒都在这里，带 _no_save）
    system_prompt: str = ""
    extra_parts: list[str] = field(default_factory=list)
    # AstrBot 处理上下文前后的消息条数
    contexts_before: int = 0
    contexts_after: int = 0
    # AstrBot 本轮是否按轮数截断 / 按 token 压缩过
    truncated: bool = False
    compressed: bool = False
    # 回存的历史里是否出现 AstrBot 的摘要消息
    summary_in_history: bool = False


@dataclass
class CheckOutcome:
    name: str
    critical: bool
    category: str
    ok: bool
    detail: str


@dataclass
class ScenarioRun:
    """一次场景运行的全部结果与成本指标。"""

    scenario: str
    outcomes: list[CheckOutcome]
    prompt_chars: list[int]
    tool_calls_per_reply: list[int]
    # 3.7.7 的 JSON 字段；新设计没有后台模型调用，恒为 0。
    background_calls: int
    # 供测试构造坏状态；CLI 不序列化。
    env: "EvalEnv | None" = field(default=None, repr=False)


# ---------------------------------------------------------------- 上下文处理


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
        return "".join(parts)
    return ""


def _count_tokens(messages: list[dict]) -> int:
    """AstrBot EstimateTokenCounter 的字符启发式（中文 ×0.6、其他 ×0.3）。"""
    total = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            total += _estimate_text(content)
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                kind = part.get("type")
                if kind == "text":
                    total += _estimate_text(str(part.get("text") or ""))
                elif kind in ("image_url", "image"):
                    total += 765
                elif kind in ("input_audio", "audio", "record"):
                    total += 500
        for tool_call in message.get("tool_calls") or []:
            if isinstance(tool_call, dict):
                total += _estimate_text(
                    json.dumps(tool_call, ensure_ascii=False)
                )
    return total


_EMOJI_RE = re.compile("[\u200d\u2600-\u27bf\ufe00-\ufe0f\U0001f000-\U0001faff]")


def _estimate_text(text: str) -> int:
    chinese = len([ch for ch in text if "\u4e00" <= ch <= "\u9fff"])
    emoji = len(_EMOJI_RE.findall(text))
    other = len(text) - chinese - emoji
    return int(chinese * 0.6 + emoji * 2.0 + other * 0.3)


def _split_system_rest(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    index = len(messages)
    for i, message in enumerate(messages):
        if message.get("role") != "system":
            index = i
            break
    return messages[:index], messages[index:]


def _has_tool_calls(message: dict) -> bool:
    return (
        message.get("role") == "assistant"
        and bool(message.get("tool_calls"))
    )


def _fix_messages(messages: list[dict]) -> list[dict]:
    """保证 assistant(tool_calls) 与随后的 tool 消息成对（truncator.py:51-98）。"""
    fixed: list[dict] = []
    pending_assistant: dict | None = None
    pending_tools: list[dict] = []

    def flush() -> None:
        nonlocal pending_assistant, pending_tools
        if pending_assistant is not None and pending_tools:
            fixed.append(pending_assistant)
            fixed.extend(pending_tools)
        pending_assistant = None
        pending_tools = []

    for message in messages:
        if message.get("role") == "tool":
            if pending_assistant is not None:
                pending_tools.append(message)
            continue
        if _has_tool_calls(message):
            flush()
            pending_assistant = message
            continue
        flush()
        fixed.append(message)
    flush()
    return fixed


def _truncate_by_turns(
    messages: list[dict], keep_most_recent_turns: int, drop_turns: int = 1
) -> tuple[list[dict], bool]:
    """AstrBot ContextTruncator.truncate_by_turns 的等价实现。"""
    if keep_most_recent_turns == -1:
        return messages, False
    system_messages, non_system = _split_system_rest(messages)
    if len(non_system) // 2 <= keep_most_recent_turns:
        return messages, False
    num_to_keep = keep_most_recent_turns - drop_turns + 1
    if num_to_keep <= 0:
        truncated: list[dict] = []
    else:
        truncated = non_system[-num_to_keep * 2 :]
    index = next(
        (i for i, item in enumerate(truncated) if item.get("role") == "user"),
        None,
    )
    if index is not None and index > 0:
        truncated = truncated[index:]
    if not (truncated and truncated[0].get("role") == "user"):
        first_user = next(
            (item for item in messages if item.get("role") == "user"), None
        )
        if first_user is not None:
            truncated = [first_user, *truncated]
    return _fix_messages(system_messages + truncated), True


def _split_into_rounds(messages: list[dict]) -> list[list[dict]]:
    """AstrBot round_utils.split_into_rounds：user 开始新的一轮。"""
    rounds: list[list[dict]] = []
    current: list[dict] = []
    for message in messages:
        if message.get("role") == "user" and current:
            rounds.append(current)
            current = []
        current.append(message)
    if current:
        rounds.append(current)
    return rounds


def _compress_context(messages: list[dict]) -> tuple[list[dict], bool]:
    """按 AstrBot LLMSummaryCompressor 的形状压缩：摘要消息 + 最近轮次。

    评测不调用摘要模型，只生成一条与 AstrBot 同前缀的摘要消息；保留最近
    15% token 的轮次，最新一轮始终保留（compressor.py:220-310）。
    """
    system_messages, rest = _split_system_rest(messages)
    rounds = _split_into_rounds(rest)
    if len(rounds) <= 1:
        return messages, False
    total = _count_tokens(messages)
    budget = max(1, int(total * KEEP_RECENT_RATIO))
    used = 0
    recent_start = len(rounds)
    for index in range(len(rounds) - 1, -1, -1):
        round_tokens = _count_tokens(rounds[index])
        if used > 0 and used + round_tokens > budget:
            break
        used += round_tokens
        recent_start = index
    old_rounds = rounds[:recent_start]
    recent_rounds = rounds[recent_start:]
    if not old_rounds:
        old_rounds = rounds
        recent_rounds = []
    summary = f"评测桩：之前 {len(old_rounds)} 轮对话的摘要。"
    result = list(system_messages)
    result.append(
        {"role": "user", "content": f"{ASTRBOT_SUMMARY_PREFIX}{summary}"}
    )
    result.append(
        {
            "role": "assistant",
            "content": "Acknowledged the summary of our previous conversation history.",
        }
    )
    for round_messages in recent_rounds:
        result.extend(round_messages)
    return result, True


def _process_context(
    messages: list[dict], scenario: "Scenario", window: int
) -> tuple[list[dict], bool, bool]:
    """AstrBot ContextManager.process：先按轮数截断，再按 0.82 压缩。"""
    truncated = False
    compressed = False
    processed = messages
    if scenario.max_turns != -1:
        processed, truncated = _truncate_by_turns(
            processed, keep_most_recent_turns=scenario.max_turns, drop_turns=1
        )
    if window > 0:
        tokens = _count_tokens(processed)
        if tokens > window * COMPRESSION_THRESHOLD:
            processed, compressed = _compress_context(processed)
            if compressed and _count_tokens(processed) > window * COMPRESSION_THRESHOLD:
                system_messages, rest = _split_system_rest(processed)
                rounds = _split_into_rounds(rest)
                while len(rounds) > 1 and _count_tokens(
                    system_messages + [m for rnd in rounds for m in rnd]
                ) > window * COMPRESSION_THRESHOLD:
                    rounds.pop(0)
                processed = system_messages + [
                    m for rnd in rounds for m in rnd
                ]
    return processed, truncated, compressed


# ---------------------------------------------------------------- 历史回存


def _messages_to_save(messages: list[dict]) -> list[dict]:
    """按 AstrBot _save_to_history + dump_messages_with_checkpoints 回存。

    - 跳过开头 system 消息；
    - role 为 user/assistant 且整条 _no_save 的跳过；
    - 内容块里 _no_save 的部分丢掉（agent/message.py:338-354）。
    """
    saved: list[dict] = []
    skipped_initial_system = False
    for message in messages:
        role = message.get("role")
        if role == "system" and not skipped_initial_system:
            skipped_initial_system = True
            continue
        if role in ("assistant", "user") and message.get("_no_save"):
            continue
        item = copy.deepcopy(message)
        content = item.get("content")
        if isinstance(content, list):
            item["content"] = [
                part
                for part in content
                if not (isinstance(part, dict) and part.get("_no_save"))
            ]
        saved.append(item)
    return saved


# ---------------------------------------------------------------- 一轮对话


def _text_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(str(part.get("text") or ""))
            for part in content
            if isinstance(part, dict)
        )
    return 0


def prompt_chars(req) -> int:
    """3.7.7 的口径：系统提示词 + 历史 + 本轮正文与附加内容。"""
    total = len(str(req.system_prompt or "")) + len(str(req.prompt or ""))
    total += sum(_text_chars(item.get("content")) for item in req.contexts or [])
    for part in getattr(req, "extra_user_content_parts", None) or []:
        if isinstance(part, dict):
            total += len(str(part.get("text") or ""))
        else:
            total += len(str(getattr(part, "text", "") or ""))
    return total


def _base_system_prompt(clock: FakeClock) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(clock.now))
    return (
        "你是鲸群记忆的评测机器人阿鲸。回答简洁，严格依据记忆工具返回的"
        "内容，不要编造。用户明确要求记住、更正或忘掉某件事时当轮直接调用"
        "记忆工具；需要细节时先用 memory_read，不确定时用 memory_search。\n"
        f"当前时间：{stamp}"
    )


async def _execute_tool(
    plugin, event: EvalEvent, tools: dict[str, Callable], call: ToolCall
) -> str:
    func = tools.get(call.name)
    if func is None:
        return f"工具 {call.name} 不存在"
    try:
        result = await func(event, **call.arguments)
    except TypeError as exc:
        return f"工具参数错误：{exc}"
    except Exception as exc:  # noqa: BLE001 - 工具失败按真实工具结果回给模型
        return f"工具执行出错：{type(exc).__name__}: {exc}"
    if result is None:
        return ""
    return result if isinstance(result, str) else str(result)


def _runner_messages(req) -> list[dict]:
    """AgentRunner.reset 的等价实现：历史 + 本轮用户消息 + 开头 system。"""
    messages: list[dict] = [
        copy.deepcopy(item)
        for item in (req.contexts or [])
        if isinstance(item, dict)
    ]
    blocks: list[dict] = []
    prompt = str(req.prompt or "")
    if prompt.strip():
        blocks.append({"type": "text", "text": prompt})
    extra_parts = list(getattr(req, "extra_user_content_parts", None) or [])
    for part in extra_parts:
        dumped = part if isinstance(part, dict) else part.model_dump_for_context()
        blocks.append(copy.deepcopy(dumped))
    if (
        len(blocks) == 1
        and blocks[0].get("type") == "text"
        and not extra_parts
        and not (getattr(req, "image_urls", None) or [])
        and not (getattr(req, "audio_urls", None) or [])
    ):
        messages.append({"role": "user", "content": blocks[0]["text"]})
    elif blocks:
        messages.append({"role": "user", "content": blocks})
    system = str(req.system_prompt or "")
    if system:
        messages.insert(0, {"role": "system", "content": system})
    return messages


def _provider_messages(messages: list[dict]) -> list[dict]:
    """发给 provider 前的消息：剔除只给插件用的 _no_save 标记。

    AstrBot 的 openai_source.py:986-987 在发请求前删掉内容块里的 _no_save；
    评测客户端直接把消息交给接口，必须在同一位置做同样的清理。
    """
    result: list[dict] = []
    for message in messages:
        item = copy.deepcopy(message)
        item.pop("_no_save", None)
        content = item.get("content")
        if isinstance(content, list):
            parts: list = []
            for part in content:
                if isinstance(part, dict):
                    part = dict(part)
                    part.pop("_no_save", None)
                parts.append(part)
            item["content"] = parts
        result.append(item)
    return result


async def run_turn(env: EvalEnv, turn: Turn) -> TurnResult:
    """按真实流程跑一轮：请求钩子 → 上下文处理 → 模型工具循环 → 响应钩子。"""
    plugin = env.plugin
    persona = turn.persona
    chat = build_chat(persona, env.scenario, turn.mode)
    umo = chat.umo
    conversation = SimpleNamespace(cid=f"cid-{umo}", token_usage=0, history="[]")
    tools, schemas = collect_tools(plugin)
    req = env.plugin_module.ProviderRequest(
        prompt=turn.text,
        session_id=umo,
        contexts=copy.deepcopy(env.history.get(umo, [])),
        system_prompt=_base_system_prompt(env.clock),
        conversation=conversation,
        model=env.model,
        func_tool=EvalToolSet(schemas),
    )
    event = EvalEvent(chat, persona)
    event.set_extra("provider_request", req)

    # on_llm_request：插件看到的是 AstrBot 截断/压缩之前的历史
    await plugin.inject_memory(event, req)
    chars = prompt_chars(req)
    extra_parts = [
        str(part.get("text") or "")
        for part in getattr(req, "extra_user_content_parts", None) or []
        if isinstance(part, dict)
    ]
    contexts_before = len(req.contexts or [])

    messages = _runner_messages(req)
    tool_records: list[tuple[str, dict, str]] = []
    final: ModelReply | None = None
    truncated = False
    compressed = False
    for _step in range(MAX_TOOL_STEPS):
        messages, did_truncate, did_compress = _process_context(
            messages, env.scenario, int(env.scenario.provider_window)
        )
        truncated = truncated or did_truncate
        compressed = compressed or did_compress
        reply = await env.client.complete(_provider_messages(messages), schemas)
        if not reply.tool_calls:
            final = reply
            break
        assistant = {
            "role": "assistant",
            "content": reply.content or "",
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments, ensure_ascii=False
                        ),
                    },
                }
                for call in reply.tool_calls
            ],
        }
        messages.append(assistant)
        for call in reply.tool_calls:
            result = await _execute_tool(plugin, event, tools, call)
            tool_records.append((call.name, dict(call.arguments), result))
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.call_id,
                    "content": result,
                }
            )
    if final is None:
        # 工具循环达到上限：与 AstrBot 一样拔掉工具并注入步数耗尽提示，
        # 强制要一段最终回复（tool_loop_agent_runner.py:1146-1180）。
        req.func_tool = None
        messages.append({"role": "user", "content": MAX_STEPS_REACHED_PROMPT})
        messages, _, _ = _process_context(
            messages, env.scenario, int(env.scenario.provider_window)
        )
        final = await env.client.complete(_provider_messages(messages), None)
    text = final.content or ""
    messages.append({"role": "assistant", "content": text})
    contexts_after = len(_messages_to_save(messages))

    resp = astrbot_stub.LLMResponse(
        role="assistant",
        completion_text=text,
        raw_completion={"usage": {"prompt_tokens": final.prompt_tokens or 0}},
    )
    await plugin.record_usage(event, resp)

    saved = _messages_to_save(messages)
    env.history[umo] = saved
    summary = any(
        message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and message["content"].startswith(ASTRBOT_SUMMARY_PREFIX)
        for message in saved
    )
    return TurnResult(
        text=turn.text,
        reply=text,
        sent_prompt=str(req.prompt or ""),
        tool_calls=tool_records,
        prompt_chars=chars,
        system_prompt=str(req.system_prompt or ""),
        extra_parts=extra_parts,
        contexts_before=contexts_before,
        contexts_after=contexts_after,
        truncated=truncated,
        compressed=compressed,
        summary_in_history=summary,
    )


def build_plugin(env: EvalEnv, client):
    module, plugin_cls = get_plugin_class()
    env.plugin_module = module
    env._load_modules()
    config = load_default_config()
    config.update(copy.deepcopy(env.scenario.config))
    provider = EvalProvider(
        model=env.model, window=env.scenario.provider_window
    )
    context = EvalContext(
        provider, env.astrbot_config, lambda umo: env.history.get(umo, [])
    )
    return plugin_cls(context, config)


async def run_scenario(
    scenario: Scenario,
    client,
    data_dir: Path,
    *,
    model: str = "eval-model",
) -> ScenarioRun:
    """在独立数据目录里跑一个场景；返回全部检查项的结果与指标。"""
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    # 与真实 AstrBot 一致：插件数据目录是 <数据根>/plugin_data/<插件名>
    plugin_data = root / "plugin_data" / astrbot_stub.DATA_PLUGIN_NAME
    clock = FakeClock()
    env = EvalEnv(scenario, plugin_data, client, clock)
    env.model = model
    clock_token = _CLOCK.set(clock)
    data_token = astrbot_stub.set_data_dir(root)
    plugin = None
    try:
        with patched_time():
            plugin = build_plugin(env, client)
            env.plugin = plugin
            if scenario.setup is not None:
                result = scenario.setup(env)
                if asyncio.iscoroutine(result):
                    await result
            for turn in scenario.turns:
                if turn.advance_days:
                    clock.advance_days(turn.advance_days)
                env.turns.append(await run_turn(env, turn))

            outcomes: list[CheckOutcome] = []
            for check in scenario.checks:
                try:
                    value = check.fn(env)
                    if asyncio.iscoroutine(value):
                        value = await value
                    ok, detail = value
                except Exception as exc:  # noqa: BLE001 - 检查函数自身是脚本代码
                    raise EvalError(
                        f"检查 {scenario.name}.{check.name} 执行出错："
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                outcomes.append(
                    CheckOutcome(
                        name=f"{scenario.name}.{check.name}",
                        critical=check.critical,
                        category=check.category,
                        ok=bool(ok),
                        detail="" if ok else str(detail or "未通过"),
                    )
                )
            return ScenarioRun(
                scenario=scenario.name,
                outcomes=outcomes,
                prompt_chars=[turn.prompt_chars for turn in env.turns],
                tool_calls_per_reply=[len(turn.tool_calls) for turn in env.turns],
                background_calls=0,
                env=env,
            )
    finally:
        if plugin is not None:
            try:
                await plugin.terminate()
            except Exception:  # noqa: BLE001 - 清理失败不影响评测结果
                pass
        astrbot_stub.reset_data_dir(data_token)
        _CLOCK.reset(clock_token)
