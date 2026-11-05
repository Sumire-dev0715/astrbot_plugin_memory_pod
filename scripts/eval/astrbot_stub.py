"""评测脚本用的 AstrBot 4.29.4 替身。

只补插件实际用到的公开接口：Star / StarTools / Context、事件基类、filter
装饰器、ProviderRequest / LLMResponse。钩子的调用顺序、请求对象字段与工具
循环由 harness 按 AstrBot 4.29.4 的真实流程驱动（见 harness.py），这里只
负责让插件在临时数据目录里可导入、可实例化。

替身只在评测进程或评测测试里安装，不参与插件运行时的任何逻辑。
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import logging
import sys
import tempfile
import types
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

# 插件在评测进程里的包名；与仓库目录解耦，main.py 里的相对导入照常工作。
PLUGIN_PACKAGE = "memory_pod_under_eval"
# 插件数据目录名，与 metadata.yaml / main.PLUGIN_NAME 一致。
DATA_PLUGIN_NAME = "astrbot_plugin_memory_pod"

# StarTools.get_data_dir 的返回值按场景任务隔离；场景并发时各用各的目录。
_DATA_DIR: ContextVar[str | None] = ContextVar("mb_eval_data_dir", default=None)

_installed = False


class AstrBotConfig(dict):
    """AstrBotConfig 的最小子集：dict + 可选 save_config。"""

    def save_config(self) -> None:  # pragma: no cover - 评测不写回配置
        pass


class Filter:
    """@filter 装饰器替身。

    llm_tool 记录工具名，调用方据此取出插件注册的记忆工具；其余钩子
    装饰器原样返回函数，由 harness 按真实顺序显式调用。
    """

    class PermissionType:
        ADMIN = "admin"

    @staticmethod
    def _noop(*args, **kwargs):
        return lambda func: func

    on_llm_request = _noop
    on_llm_response = _noop
    permission_type = _noop
    command = _noop

    def llm_tool(self, name: str | None = None, **kwargs):
        def decorator(func):
            func.__astrbot_llm_tool__ = name or func.__name__
            return func

        return decorator


class AstrMessageEvent:
    """事件基类占位；harness 的 EvalEvent 提供插件用到的字段与方法。"""


class LLMResponse:
    """足够插件读取 completion_text / tools_call_* / raw_completion / usage。"""

    def __init__(
        self,
        role: str = "assistant",
        completion_text: str | None = None,
        result_chain=None,
        tools_call_args=None,
        tools_call_name=None,
        tools_call_ids=None,
        raw_completion=None,
        usage=None,
        **kwargs,
    ) -> None:
        self.role = role
        self.completion_text = completion_text or ""
        self.result_chain = result_chain
        self.tools_call_args = tools_call_args or []
        self.tools_call_name = tools_call_name or []
        self.tools_call_ids = tools_call_ids or []
        self.raw_completion = raw_completion
        self.usage = usage


class ProviderRequest:
    """AstrBot 4.29.4 ProviderRequest 的字段（provider/entities.py:194）。"""

    def __init__(
        self,
        prompt: str | None = None,
        session_id: str | None = "",
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        extra_user_content_parts: list | None = None,
        func_tool=None,
        contexts: list[dict] | None = None,
        system_prompt: str = "",
        conversation=None,
        tool_calls_result=None,
        model: str | None = None,
    ) -> None:
        self.prompt = prompt
        self.session_id = session_id
        self.image_urls = image_urls or []
        self.audio_urls = audio_urls or []
        self.extra_user_content_parts = extra_user_content_parts or []
        self.func_tool = func_tool
        self.contexts = contexts or []
        self.system_prompt = system_prompt
        self.conversation = conversation
        self.tool_calls_result = tool_calls_result
        self.model = model

    def append_tool_calls_result(self, item) -> None:
        if self.tool_calls_result is None:
            self.tool_calls_result = []
        self.tool_calls_result.append(item)


class Context:
    """Context 占位；harness 的 EvalContext 提供插件用到的查询方法。"""


class Star:
    """插件基类的最小子集。"""

    def __init__(self, context=None, config=None) -> None:
        self.context = context
        self.config = config


class StarTools:
    """插件数据目录按当前场景任务解析。

    与 AstrBot 4.29.4 一样返回 ``<数据根>/plugin_data/<插件名>``；场景的
    临时根目录由 harness 通过 ContextVar 传入。
    """

    @staticmethod
    def get_data_dir(name: str) -> str:
        root = _DATA_DIR.get()
        if not root:
            root = str(Path(tempfile.gettempdir()) / "mb-eval-unset")
        return str(Path(root) / "plugin_data" / (name or DATA_PLUGIN_NAME))


def _module(name: str) -> types.ModuleType:
    if name in sys.modules:
        return sys.modules[name]
    module = types.ModuleType(name)
    sys.modules[name] = module
    return module


def _null_logger() -> logging.Logger:
    logger = logging.getLogger("astrbot_plugin_memory_pod.eval")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    logger.setLevel(logging.CRITICAL)
    logger.propagate = False
    return logger


def install(force: bool = False) -> None:
    """把替身装进 sys.modules。

    force=True 时重建本模块负责的 astrbot 子模块（测试里可能已有别的插件
    替身）；astrbot.api.web 不在本模块职责内，原样保留，避免影响其他测试。
    """
    global _installed
    if _installed and not force:
        return

    astrbot = _module("astrbot")
    api = _module("astrbot.api")
    astrbot.api = api

    event_mod = _module("astrbot.api.event")
    api.event = event_mod
    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = Filter()

    provider_mod = _module("astrbot.api.provider")
    api.provider = provider_mod
    provider_mod.LLMResponse = LLMResponse
    provider_mod.ProviderRequest = ProviderRequest

    star_mod = _module("astrbot.api.star")
    api.star = star_mod
    star_mod.Context = Context
    star_mod.Star = Star
    star_mod.StarTools = StarTools

    api.logger = _null_logger()
    api.AstrBotConfig = AstrBotConfig

    # `_astrbot_window` 会 import 这张表；给一张空表让它走到兜底值那一级，
    # 与“表里没有这个模型”的真实行为一致。
    try:
        core_mod = _module("astrbot.core")
        utils_mod = _module("astrbot.core.utils")
        core_mod.utils = utils_mod
        meta_mod = _module("astrbot.core.utils.llm_metadata")
        utils_mod.llm_metadata = meta_mod
        meta_mod.LLM_METADATAS = {}
    except Exception:  # noqa: BLE001 - 替身失败按旧版本处理，不影响导入
        pass

    _installed = True


def set_data_dir(path: str | Path):
    """为当前任务设置插件数据目录；返回可用于 reset 的 token。"""
    return _DATA_DIR.set(str(path))


def reset_data_dir(token) -> None:
    _DATA_DIR.reset(token)


def load_plugin(repo_root: str | Path):
    """以独立包名导入仓库里的 main.py，返回 (main 模块, 插件类)。"""
    install()
    repo_root = Path(repo_root).resolve()
    for name in list(sys.modules):
        if name == PLUGIN_PACKAGE or name.startswith(PLUGIN_PACKAGE + "."):
            del sys.modules[name]
    spec = importlib.machinery.ModuleSpec(PLUGIN_PACKAGE, None, is_package=True)
    package = importlib.util.module_from_spec(spec)
    package.__path__ = [str(repo_root)]  # type: ignore[attr-defined]
    sys.modules[PLUGIN_PACKAGE] = package
    module = importlib.import_module(f"{PLUGIN_PACKAGE}.main")
    return module, module.MemoryPodPlugin


@contextmanager
def plugin_imported(repo_root: str | Path):
    """导入插件并在退出时清掉包模块（测试里反复导入时用）。"""
    module, plugin_cls = load_plugin(repo_root)
    try:
        yield module, plugin_cls
    finally:
        for name in list(sys.modules):
            if name == PLUGIN_PACKAGE or name.startswith(PLUGIN_PACKAGE + "."):
                del sys.modules[name]
