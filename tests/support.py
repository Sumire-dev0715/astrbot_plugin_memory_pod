"""测试用的最小 AstrBot 替身与插件工厂。

用真实的事件循环驱动 main.py 的钩子/工具方法，记忆全部落在临时目录的
合成数据上。这里只补插件实际用到的公开接口，不 monkey-patch 生产代码。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.machinery
import importlib.util
import logging
import sys
import time
import types
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "mb_plugin_under_test"


def _ensure_astrbot_stubs():
    astrbot = sys.modules.get("astrbot")
    if astrbot is None:
        astrbot = types.ModuleType("astrbot")
        sys.modules["astrbot"] = astrbot
    api = sys.modules.get("astrbot.api")
    if api is None:
        api = types.ModuleType("astrbot.api")
        sys.modules["astrbot.api"] = api
    astrbot.api = api
    if not hasattr(api, "logger"):
        logger = logging.getLogger("memory_pod_test")
        logger.addHandler(logging.NullHandler())
        logger.setLevel(logging.DEBUG)
        api.logger = logger
    if not hasattr(api, "AstrBotConfig"):
        api.AstrBotConfig = dict

    event_mod = sys.modules.get("astrbot.api.event")
    if event_mod is None:
        event_mod = types.ModuleType("astrbot.api.event")
        sys.modules["astrbot.api.event"] = event_mod

    class AstrMessageEvent:
        pass

    class FakeFilter:
        class PermissionType:
            ADMIN = "admin"

        def llm_tool(self, *args, **kwargs):
            name = kwargs.get("name")

            def decorator(func):
                func.__astrbot_llm_tool__ = name or func.__name__
                return func

            return decorator

        def on_llm_request(self, *args, **kwargs):
            return lambda func: func

        def on_llm_response(self, *args, **kwargs):
            return lambda func: func

        def permission_type(self, *args, **kwargs):
            return lambda func: func

        def command(self, *args, **kwargs):
            return lambda func: func

    event_mod.AstrMessageEvent = AstrMessageEvent
    event_mod.filter = FakeFilter()
    api.event = event_mod

    provider_mod = sys.modules.get("astrbot.api.provider")
    if provider_mod is None:
        provider_mod = types.ModuleType("astrbot.api.provider")
        sys.modules["astrbot.api.provider"] = provider_mod
    if not hasattr(provider_mod, "LLMResponse"):
        provider_mod.LLMResponse = type("LLMResponse", (), {})
    if not hasattr(provider_mod, "ProviderRequest"):
        provider_mod.ProviderRequest = type("ProviderRequest", (), {})

    star_mod = sys.modules.get("astrbot.api.star")
    if star_mod is None:
        star_mod = types.ModuleType("astrbot.api.star")
        sys.modules["astrbot.api.star"] = star_mod

    class Star:
        def __init__(self, context=None, config=None):
            self.context = context
            self.config = config

    class Context:
        pass

    class StarTools:
        @staticmethod
        def get_data_dir(name):
            return str(ROOT / "unused")

    star_mod.Star = Star
    star_mod.Context = Context
    star_mod.StarTools = StarTools
    api.star = star_mod

    web_mod = sys.modules.get("astrbot.api.web")
    if web_mod is None:
        web_mod = types.ModuleType("astrbot.api.web")
        # 只是为了让 web_api 能导入；本文件不调用管理页接口。
        web_mod._memory_pod_placeholder = True
        sys.modules["astrbot.api.web"] = web_mod
    api.web = web_mod


def _release_web_stub():
    web_mod = sys.modules.get("astrbot.api.web")
    if web_mod is None or not getattr(web_mod, "_memory_pod_placeholder", False):
        return
    sys.modules.pop("astrbot.api.web", None)
    api = sys.modules.get("astrbot.api")
    if api is not None and getattr(api, "web", None) is web_mod:
        delattr(api, "web")
    sys.modules.pop(f"{PACKAGE}.web_api", None)


def load_main():
    _ensure_astrbot_stubs()
    if PACKAGE not in sys.modules:
        spec = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    module = importlib.import_module(f"{PACKAGE}.main")
    return module


main = load_main()
pkg_context_stats = importlib.import_module(f"{PACKAGE}.core.context_stats")
pkg_expiry = importlib.import_module(f"{PACKAGE}.core.expiry")
pkg_history = importlib.import_module(f"{PACKAGE}.core.history")
pkg_injection = importlib.import_module(f"{PACKAGE}.core.injection")
pkg_journal = importlib.import_module(f"{PACKAGE}.core.journal")
pkg_memstore = importlib.import_module(f"{PACKAGE}.core.memstore")
pkg_origin = importlib.import_module(f"{PACKAGE}.core.origin")
pkg_prompts = importlib.import_module(f"{PACKAGE}.core.prompts")
pkg_recall = importlib.import_module(f"{PACKAGE}.core.recall")
pkg_registry = importlib.import_module(f"{PACKAGE}.core.registry")
pkg_reminder = importlib.import_module(f"{PACKAGE}.core.reminder")
pkg_tidy = importlib.import_module(f"{PACKAGE}.core.tidy")
pkg_writer = importlib.import_module(f"{PACKAGE}.core.writer")


def import_core(name: str):
    return importlib.import_module(f"{PACKAGE}.core.{name}")


def run(coro):
    return asyncio.run(coro)


class FakeContext:
    pass


class FakeEvent:
    def __init__(
        self,
        platform_id="Bot-A",
        sender_id="123",
        umo=None,
        admin=False,
        name="测试用户",
        role="member",
        group_id="",
        group_name="",
        card="",
        nickname=None,
    ):
        self._platform_id = platform_id
        self._sender_id = sender_id
        self.unified_msg_origin = umo or f"{platform_id}:FriendMessage:{sender_id}"
        self._admin = admin
        self._name = name
        self.role = role
        self.message_obj = types.SimpleNamespace(
            group_id=group_id,
            group=types.SimpleNamespace(group_name=group_name),
            raw_message={
                "sender": {
                    "nickname": name if nickname is None else nickname,
                    "card": card,
                }
            },
        )

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._name

    def get_self_id(self):
        return "999"

    def get_messages(self):
        return []

    def is_admin(self):
        return self._admin

    def plain_result(self, text):
        return text


class FakeProvider:
    """默认对话模型的确定性替身：按队列返回 text_chat 结果。"""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.prompts: list[str] = []

    async def text_chat(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else ""
        return types.SimpleNamespace(completion_text=reply)


def make_plugin(tmp_path, **config):
    plugin = object.__new__(main.MemoryPodPlugin)
    plugin.context = FakeContext()
    plugin.config = {
        "enable_memory": True,
        "recall_max_files": 5,
        "recall_full_files": 2,
    }
    plugin.config.update(config)
    plugin.data_dir = tmp_path
    plugin._store = pkg_memstore.MemoryStore(tmp_path / "memories")
    plugin._sessions = OrderedDict()
    plugin._registry = pkg_registry.SessionRegistry(tmp_path / "sessions.json")
    plugin._history_cleanup_at = time.time()
    plugin._state_dir = tmp_path / "state"
    plugin._tasks = set()
    plugin._last_sweep = {}
    plugin._tidy = pkg_tidy.TidyBook(tmp_path / "tidy")
    return plugin


def thing(
    name,
    description="合成记忆",
    body="合成正文",
    mem_type="note",
    signature="",
    expires="",
):
    return pkg_memstore.compose_memory(
        name,
        description,
        mem_type,
        body,
        signature=signature,
        expires=expires,
    )


def person_file(sender_id="123", body="身份：测试用户，工程师", description="测试人物"):
    return thing(
        f"user-{sender_id}",
        description=description,
        body=body,
        mem_type="person",
    )


def write_person(store, sender_id="123", body="身份：测试用户，工程师"):
    return run(store.write(f"user-{sender_id}.md", person_file(sender_id, body)))
