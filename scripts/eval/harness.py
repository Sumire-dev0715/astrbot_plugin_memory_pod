"""评测运行环境：AstrBot 事件的替身、插件实例、一轮请求的完整流程。

按 AstrBot 4.29.4 的真实顺序驱动：

    on_llm_request（插件注入/压缩/标注）
    → 模型（带工具循环，最多 8 步，工具直接调用插件注册的方法）
    → on_llm_response（记账；最终回复后）
    → 模拟空闲，后台整理在可控时钟上运行

聊天与后台整理走同一个 OpenAI 兼容客户端；后台整理通过 provider 的
text_chat 调用，与 AstrBot 的摘要/整理调用链一致。
"""

from __future__ import annotations

import asyncio
import copy
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
IDLE_MINUTES_DEFAULT = 11
# 假时钟起点：2026-10-01 10:00 UTC，远离真实时间，避免依赖机器时区之外的东西
CLOCK_START = 1790848800.0
# AstrBot 4.29.4 在达到工具步数上限后注入的提示词
# （core/agent/runners/tool_loop_agent_runner.py:126）。
MAX_STEPS_REACHED_PROMPT = (
    "[SYSTEM NOTICE: Agent step budget exhausted] "
    "Remaining steps: 0. Stop using tools; summarize results and unfinished work. "
    "If unfinished, tell the user they can simply ask you to continue with a fresh "
    "step budget. Optionally, they can raise the tool-call round limit in "
    "AstrBot WebUI to allow longer runs."
)


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
_SCENARIO: ContextVar[str] = ContextVar("mb_eval_scenario", default="")


def current_scenario() -> str:
    return _SCENARIO.get()


def _dispatched_time() -> float:
    clock = _CLOCK.get()
    return clock.time() if clock is not None else _REAL_TIME()


_REAL_TIME = time.time
_TIME_PATCH_LOCK = threading.Lock()
_TIME_PATCH_REFCOUNT = 0

# 插件包在进程内只导入一次：场景并发运行时共享同一个类对象，避免反复
# 删改 sys.modules 造成竞态。测试需要重新导入时显式调用 reset_plugin()。
_PLUGIN_LOCK = threading.Lock()
_PLUGIN_CACHE: tuple[Any, Any] | None = None


def get_plugin_class():
    global _PLUGIN_CACHE
    with _PLUGIN_LOCK:
        if _PLUGIN_CACHE is None:
            _PLUGIN_CACHE = astrbot_stub.load_plugin(REPO_ROOT)
        return _PLUGIN_CACHE


def reset_plugin() -> None:
    """清掉已导入的插件包，下一次 get_plugin_class() 重新导入。"""
    global _PLUGIN_CACHE
    with _PLUGIN_LOCK:
        _PLUGIN_CACHE = None
        for name in list(sys.modules):
            if name == astrbot_stub.PLUGIN_PACKAGE or name.startswith(
                astrbot_stub.PLUGIN_PACKAGE + "."
            ):
                del sys.modules[name]


@contextmanager
def patched_time():
    """把 time.time 换成按任务 ContextVar 分发的假时钟。

    各模块都是 `import time` 后调用 time.time，因此一次替换即可覆盖插件
    全部写入戳、日志分段与后台触发；未设置场景上下文时返回真实时间。
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


def first_paragraph(doc: str | None) -> str:
    """AstrBot 取 docstring.description：第一个空行之前的正文。"""
    text = (doc or "").strip()
    if not text:
        return ""
    return text.split("\n\n", 1)[0].strip()


_ARGS_LINE = re.compile(r"^\s+(\w+)\((\w+)\):\s*(.*)$")
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


def tool_schema(func) -> dict:
    """按 AstrBot register_llm_tool 的口径还原 OpenAI 工具声明。"""
    doc = (func.__doc__ or "").strip()
    description = first_paragraph(doc)
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
            "type": _TYPE_MAP.get(type_name, type_name),
            "description": arg_desc.strip(),
        }
    return {
        "type": "function",
        "function": {
            "name": func.__astrbot_llm_tool__,
            "description": description,
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
    # 事件所在的机器人（平台适配器）ID；共享人物空间用它判定权限。
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

    def get_sender_id(self) -> str:
        return self.persona.sender_id

    def get_sender_name(self) -> str:
        return self.persona.name

    def get_self_id(self) -> str:
        return self.chat.self_id

    def get_messages(self) -> list:
        return []

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


def build_chat(persona: Persona, scenario: "Scenario") -> EvalChat:
    if scenario.mode == "group":
        target = scenario.group_id
        msg_type = "GroupMessage"
    else:
        target = persona.sender_id
        msg_type = "FriendMessage"
    umo = f"{scenario.platform_type}:{msg_type}:{target}"
    return EvalChat(
        umo=umo,
        mode=scenario.mode,
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
    """Provider 替身：后台整理与摘要都走 text_chat。"""

    def __init__(self, client, model: str, window: int) -> None:
        self.client = client
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

        reply = await self.client.complete(
            [{"role": "user", "content": prompt or ""}],
            None,
            purpose="background",
        )
        tokens = reply.prompt_tokens or max(1, len(prompt or "") // 2)
        return LLMResponse(
            role="assistant",
            completion_text=reply.content,
            raw_completion={"usage": {"prompt_tokens": tokens}},
        )


class EvalContext:
    """插件 Context 的最小子集。"""

    def __init__(self, provider: EvalProvider, config: dict) -> None:
        self.provider = provider
        self.config = config
        self.registered_web_apis: list = []
        self.platform_manager = SimpleNamespace(get_insts=self._platform_insts)
        self.conversation_manager = SimpleNamespace(
            get_curr_conversation_id=self._curr_cid
        )

    @staticmethod
    def _platform_insts():
        def inst(platform_id: str):
            return SimpleNamespace(
                meta=lambda: SimpleNamespace(id=platform_id, name="aiocqhttp")
            )

        return [inst("10001"), inst("10002")]

    async def _curr_cid(self, umo: str) -> str:
        return f"cid-{umo}"

    def get_config(self, umo: str | None = None) -> dict:
        return self.config

    async def get_using_provider(self, umo: str | None = None):
        return self.provider

    async def get_provider_by_id(self, provider_id: str | None = None, **kwargs):
        if provider_id in (None, "", "eval-provider"):
            return self.provider
        return None


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
        self.astrbot_config = {
            "provider_settings": {"identifier": False, "enable": True},
            "agent_runner": {
                "config": {
                    "compression": {
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

    # ------------------------------------------------------------ 文件

    def _load_modules(self) -> None:
        prefix = astrbot_stub.PLUGIN_PACKAGE
        self.memstore = sys.modules[f"{prefix}.core.memstore"]
        self.journal = sys.modules[f"{prefix}.core.journal"]

    def memory_files(self) -> list[MemoryFile]:
        result: list[MemoryFile] = []
        base = self.memory_dir
        for scope in ("global", "session", "people"):
            root = base / scope
            if not root.is_dir():
                continue
            if scope == "global":
                pairs = [("global", root)]
            else:
                pairs = [
                    (path.name, path)
                    for path in sorted(root.iterdir())
                    if path.is_dir() and not path.name.startswith(".")
                ]
            for key, directory in pairs:
                if not directory.is_dir():
                    continue
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
                            is_journal=self.journal.is_journal(path.name, doc.type),
                        )
                    )
        return result

    def joined_memory(self, *, exclude_journal: bool = True) -> str:
        return "\n\n".join(
            item.text
            for item in self.memory_files()
            if not (exclude_journal and item.is_journal)
        )

    def journal_files(self, umo: str) -> list[MemoryFile]:
        key = self.memstore.safe_key(umo)
        return [
            item
            for item in self.memory_files()
            if item.scope == "session"
            and item.key == key
            and item.is_journal
        ]

    def session_files(self, umo: str) -> list[MemoryFile]:
        key = self.memstore.safe_key(umo)
        return [
            item
            for item in self.memory_files()
            if item.scope == "session" and item.key == key
        ]

    def people_files(self, platform_id: str) -> list[MemoryFile]:
        key = self.plugin._store.people_key(platform_id)
        return [
            item
            for item in self.memory_files()
            if item.scope == "people" and item.key == key
        ]

    def global_files(self) -> list[MemoryFile]:
        return [item for item in self.memory_files() if item.scope == "global"]

    def state(self, umo: str) -> dict:
        path = self.plugin.state_path_for_key(self.memstore.safe_key(umo))
        try:
            return json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def umo_for(self, persona: Persona) -> str:
        return build_chat(persona, self.scenario).umo

    def last_write_result(self, umo: str) -> str:
        """该会话最近一次后台整理的结果说明；没有记录返回空串。"""
        path = self.plugin._pending.path(self.memstore.safe_key(umo))
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return ""
        return str(raw.get("lastResult") or "")


# ---------------------------------------------------------------- 场景数据


@dataclass
class Turn:
    text: str
    persona: Persona
    # 本轮结束后把时钟推进这么久（天），用于“隔几天再聊”。
    advance_days: float = 0.0
    # 本轮结束后模拟空闲并触发一次后台整理。
    settle: bool = False
    # 本轮结束、后台整理后执行的探针，用于记录“忘掉前确实记过”之类的前置事实。
    probe: Callable[[EvalEnv], None] | None = None


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
    platform_type: str = "aiocqhttp"
    self_id: str = "90000000"
    config: dict = field(default_factory=dict)
    provider_window: int = 1_000_000
    max_turns: int = -1
    idle_minutes: int = IDLE_MINUTES_DEFAULT
    settle_after: bool = True
    setup: Callable[[EvalEnv], Awaitable[None] | None] | None = None


@dataclass
class TurnResult:
    text: str
    reply: str
    sent_prompt: str = ""
    tool_calls: list[tuple[str, dict, str]] = field(default_factory=list)
    prompt_chars: int = 0
    # 本轮回复后、模拟空闲前就完成的后台调用数（memory_note 立即整理）
    immediate_background_calls: int = 0
    background_calls: int = 0
    viewed_contexts: list[dict] = field(default_factory=list)


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
    background_calls: int
    # 供测试构造坏状态；CLI 不序列化。
    env: "EvalEnv | None" = field(default=None, repr=False)


# ---------------------------------------------------------------- 一轮对话


def _strip_no_save(messages: list[dict]) -> list[dict]:
    return [
        {key: value for key, value in message.items() if key != "_no_save"}
        for message in messages
        if isinstance(message, dict)
    ]


def _text_chars(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, dict):
                total += len(str(part.get("text") or ""))
        return total
    return 0


def prompt_chars(req) -> int:
    total = len(str(req.system_prompt or "")) + len(str(req.prompt or ""))
    total += sum(_text_chars(item.get("content")) for item in req.contexts or [])
    return total


def _base_system_prompt(clock: FakeClock) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(clock.now))
    return (
        "你是鲸群记忆的评测机器人阿鲸。回答简洁，严格依据记忆工具返回的"
        "内容，不要编造。用户明确要求记住、更正或忘掉某件事时调用 "
        "memory_note；需要细节时先用 memory_read，不确定时用 memory_search。\n"
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


async def run_turn(env: EvalEnv, turn: Turn) -> TurnResult:
    """按真实流程跑一轮：请求钩子 → 模型工具循环 → 回复钩子。"""
    plugin = env.plugin
    persona = turn.persona
    chat = build_chat(persona, env.scenario)
    umo = chat.umo
    conversation = SimpleNamespace(cid=f"cid-{umo}", token_usage=0, history="[]")
    tools, schemas = collect_tools(plugin)
    req = env.plugin_module.ProviderRequest(
        prompt=turn.text,
        session_id=umo,
        contexts=list(env.history.get(umo, [])),
        system_prompt=_base_system_prompt(env.clock),
        conversation=conversation,
        model=env.model,
        func_tool=EvalToolSet(schemas),
    )
    event = EvalEvent(chat, persona)
    event.set_extra("provider_request", req)

    await plugin.inject_and_compress(event, req)
    chars = prompt_chars(req)
    viewed = copy.deepcopy(req.contexts)

    system = str(req.system_prompt or "")
    run_messages: list[dict] = []
    if system:
        run_messages.append({"role": "system", "content": system})
    run_messages.extend(_strip_no_save(req.contexts))
    if req.prompt:
        run_messages.append({"role": "user", "content": str(req.prompt)})
    run_ctx = SimpleNamespace(messages=run_messages)
    await plugin.loop_guard_begin(event, run_ctx)

    tool_records: list[tuple[str, dict, str]] = []
    saved: list[dict] = [{"role": "user", "content": str(req.prompt or "")}]
    final: ModelReply | None = None
    for _step in range(MAX_TOOL_STEPS):
        reply = await env.client.complete(
            run_ctx.messages, schemas, purpose="chat"
        )
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
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in reply.tool_calls
            ],
        }
        run_ctx.messages.append(assistant)
        saved.append(assistant)
        for call in reply.tool_calls:
            result = await _execute_tool(plugin, event, tools, call)
            tool_records.append((call.name, dict(call.arguments), result))
            tool_message = {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": result,
            }
            run_ctx.messages.append(tool_message)
            saved.append(tool_message)
            await plugin.loop_guard_observe(
                event,
                tool=SimpleNamespace(name=call.name),
                tool_args=call.arguments,
                tool_result=result,
            )
    if final is None:
        # 工具循环达到上限：与 AstrBot 一样拔掉工具并注入步数耗尽提示，
        # 强制要一段最终回复（core/astr_agent_run_util.py:139-153）。
        run_ctx.messages.append(
            {"role": "user", "content": MAX_STEPS_REACHED_PROMPT}
        )
        saved.append({"role": "user", "content": MAX_STEPS_REACHED_PROMPT})
        final = await env.client.complete(run_ctx.messages, None, purpose="chat")
    text = final.content or ""
    run_ctx.messages.append({"role": "assistant", "content": text})
    saved.append({"role": "assistant", "content": text})

    resp = astrbot_stub.LLMResponse(
        role="assistant",
        completion_text=text,
        raw_completion={
            "usage": {"prompt_tokens": final.prompt_tokens or 0}
        },
    )
    resp.tools_call_name = []
    await plugin.record_pending_round(event, resp)
    await plugin.calibrate_tokens(event, resp)

    env.history.setdefault(umo, []).extend(saved)
    return TurnResult(
        text=turn.text,
        reply=text,
        sent_prompt=str(req.prompt or ""),
        tool_calls=tool_records,
        prompt_chars=chars,
        viewed_contexts=viewed,
    )


async def drain_background(env: EvalEnv) -> None:
    """等当前会话排队的后台整理全部结束（不含每分钟扫描任务）。"""
    plugin = env.plugin
    for _ in range(50):
        tasks = [
            task
            for task in list(plugin._write_tasks.values())
            if not task.done()
        ]
        if not tasks:
            await asyncio.sleep(0)
            tasks = [
                task
                for task in list(plugin._write_tasks.values())
                if not task.done()
            ]
            if not tasks:
                return
        await asyncio.gather(*tasks, return_exceptions=True)


async def settle(env: EvalEnv, minutes: int) -> None:
    """模拟空闲：推进时钟、触发每分钟一次的后台扫描，等整理完成。"""
    env.clock.advance(max(1, int(minutes)) * 60)
    env.plugin._pending_tick(now=env.clock.now)
    await drain_background(env)


def build_plugin(env: EvalEnv, client):
    module, plugin_cls = get_plugin_class()
    env.plugin_module = module
    env._load_modules()
    config = load_default_config()
    config.update(copy.deepcopy(env.scenario.config))
    config["summary_retry_count"] = 1
    config["summary_timeout"] = 60
    provider = EvalProvider(
        client, model=env.model, window=env.scenario.provider_window
    )
    context = EvalContext(provider, env.astrbot_config)
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
    scenario_token = _SCENARIO.set(scenario.name)
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
                result = await run_turn(env, turn)
                before = client.background_calls
                await drain_background(env)
                result.immediate_background_calls = (
                    client.background_calls - before
                )
                if turn.settle:
                    await settle(env, scenario.idle_minutes)
                result.background_calls = client.background_calls - before
                env.turns.append(result)
                if turn.probe is not None:
                    turn.probe(env)
            if scenario.settle_after:
                await settle(env, scenario.idle_minutes)

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
                background_calls=client.background_calls,
                env=env,
            )
    finally:
        if plugin is not None:
            try:
                await plugin.terminate()
            except Exception:  # noqa: BLE001 - 清理失败不影响评测结果
                pass
        astrbot_stub.reset_data_dir(data_token)
        _SCENARIO.reset(scenario_token)
        _CLOCK.reset(clock_token)
