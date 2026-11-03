"""共享人物空间在工具层的权限、版本检查、召回与提取门控。

用最小的 AstrBot 替身导入 main，按不同 platform_id 的模拟事件调用真实的
记忆工具方法；记忆读写全部落在临时目录的合成数据上。
"""

import asyncio
import importlib
import importlib.machinery
import importlib.util
import logging
import sys
import types
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "mb_plugin_under_test"


def _ensure_astrbot_stubs():
    """补足 main 需要的 AstrBot 模块；test_web_api 可能已经建过一部分。"""
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
        logger = logging.getLogger("memory_beyond_sharing_test")
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
            return lambda func: func

        def on_llm_request(self, *args, **kwargs):
            return lambda func: func

        def on_llm_response(self, *args, **kwargs):
            return lambda func: func

        def on_agent_begin(self, *args, **kwargs):
            return lambda func: func

        def on_llm_tool_respond(self, *args, **kwargs):
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

    web_mod = sys.modules.get("astrbot.api.web")
    if web_mod is None:
        web_mod = types.ModuleType("astrbot.api.web")
        # 只是为了让 web_api 能导入；本文件不调用管理页接口。测试结束后
        # 由 _release_web_stub 撤掉，让 test_web_api 自己建请求替身。
        web_mod.request = object()
        web_mod._memory_beyond_placeholder = True
        sys.modules["astrbot.api.web"] = web_mod
    api.web = web_mod


def _release_web_stub():
    """撤掉只为导入 main 而建的 astrbot.api.web 与 web_api 模块。"""
    web_mod = sys.modules.get("astrbot.api.web")
    if web_mod is None or not getattr(web_mod, "_memory_beyond_placeholder", False):
        return
    sys.modules.pop("astrbot.api.web", None)
    api = sys.modules.get("astrbot.api")
    if api is not None and getattr(api, "web", None) is web_mod:
        delattr(api, "web")
    sys.modules.pop(f"{PACKAGE}.web_api", None)


def _load_main():
    _ensure_astrbot_stubs()
    if PACKAGE not in sys.modules:
        spec = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    module = importlib.import_module(f"{PACKAGE}.main")
    _release_web_stub()
    return module


main = _load_main()
pkg_compress = importlib.import_module(f"{PACKAGE}.core.compress")
pkg_history = importlib.import_module(f"{PACKAGE}.core.history")
pkg_memstore = importlib.import_module(f"{PACKAGE}.core.memstore")
pkg_prompts = importlib.import_module(f"{PACKAGE}.core.prompts")
pkg_recall = importlib.import_module(f"{PACKAGE}.core.recall")
pkg_sharing = importlib.import_module(f"{PACKAGE}.core.sharing")


def run(coro):
    return asyncio.run(coro)


class FakeContext:
    pass


class FakeMeta:
    def __init__(self, platform_id, kind):
        self.id = platform_id
        self.name = kind


class FakeInst:
    def __init__(self, platform_id, kind):
        self._meta = FakeMeta(platform_id, kind)

    def meta(self):
        return self._meta


class FakePlatformManager:
    def __init__(self, insts):
        self._insts = insts

    def get_insts(self):
        return self._insts


class FakeEvent:
    def __init__(self, platform_id="Bot-A", sender_id="123", umo=None):
        self._platform_id = platform_id
        self._sender_id = sender_id
        self.unified_msg_origin = umo or f"{platform_id}:FriendMessage:100"

    def get_platform_id(self):
        return self._platform_id

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return "测试用户"

    def get_self_id(self):
        return "999"

    def get_messages(self):
        return []

    def plain_result(self, text):
        return text


def person_file(name="user-123", body="身份：测试用户，工程师"):
    return (
        "---\n"
        f"name: {name}\n"
        "description: 测试人物\n"
        "metadata:\n"
        "  type: user\n"
        "---\n\n"
        f"{body}\n"
    )


def make_plugin(tmp_path, members):
    plugin = object.__new__(main.MemoryBeyondPlugin)
    plugin.context = FakeContext()
    plugin.config = {
        "enable_memory": True,
        "enable_global_memory": False,
        "enable_people_memory": True,
        "shared_people_bots": members,
        "recall_mode": "keyword",
        "recall_max_files": 5,
        "auto_extract_interval": 0,
        "extract_memories": True,
        "archive_keep": 0,
        "keep_recent_turns": 1,
        "summary_max_chars": 500,
    }
    plugin.data_dir = tmp_path
    plugin._store = pkg_memstore.MemoryStore(tmp_path / "memories")
    plugin._sessions = OrderedDict()
    plugin._sharing = pkg_sharing.Membership()
    plugin._sharing_warned = set()
    plugin._seen = pkg_sharing.SeenVersions(tmp_path / "state" / "shared_seen.json")
    plugin._state_dir = tmp_path / "state"
    plugin._store.set_shared_people(plugin._sharing)
    plugin._refresh_sharing()
    return plugin


EXTRACT_TEXT = (
    "<memories>"
    '[{"type": "user", "name": "user-123", "description": "身份", '
    '"content": "工程师", "scope": "people"}]'
    "</memories>"
)


# ---------------------------------------------------------------- 成员与非成员


def test_platform_type_mismatch_disables_sharing_for_that_member(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=edit", "Bot-C=append"])
    plugin.context.platform_manager = FakePlatformManager(
        [
            FakeInst("Bot-A", "aiocqhttp"),
            FakeInst("Bot-B", "aiocqhttp"),
            FakeInst("Bot-C", "discord"),
        ]
    )
    plugin._refresh_sharing()
    assert plugin._sharing.permission("Bot-A") == pkg_sharing.PERMISSION_EDIT
    assert plugin._sharing.permission("Bot-B") == pkg_sharing.PERMISSION_EDIT
    assert plugin._sharing.permission("Bot-C") is None
    assert plugin._store.people_scope("Bot-A").root.name == pkg_sharing.SHARED_KEY
    assert plugin._store.people_scope("Bot-C").root.name != pkg_sharing.SHARED_KEY


def test_member_people_scope_is_shared_and_non_member_stays_own(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    member = plugin._store.people_scope("Bot-A")
    other = plugin._store.people_scope("Bot-C")
    assert member.root.name == pkg_sharing.SHARED_KEY
    assert other.root.name != pkg_sharing.SHARED_KEY
    assert member.root != other.root

    run(plugin.memory_write(FakeEvent("Bot-C"), "people", "user-1.md", person_file("user-1")))
    assert (other.root / "user-1.md").is_file()
    assert not (member.root / "user-1.md").exists()


# ---------------------------------------------------------------- 三种权限


def test_edit_permission_can_write_edit_delete(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A")

    result = run(plugin.memory_write(event, "people", "user-123.md", person_file()))
    assert "已写入" in result
    # 自己刚写的版本算看过，可以不重新读就覆盖
    result = run(
        plugin.memory_write(
            event, "people", "user-123.md", person_file(body="新的身份")
        )
    )
    assert "已写入" in result
    result = run(
        plugin.memory_edit(
            event, "people", "user-123.md", old_text="新的身份", new_text="改后的身份"
        )
    )
    assert "已修改" in result
    result = run(plugin.memory_write(event, "people", "user-123.md", delete=True))
    assert "已删除" in result


def test_append_permission_only_creates_and_appends(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=append"])
    event = FakeEvent("Bot-A")

    result = run(plugin.memory_write(event, "people", "user-123.md", person_file()))
    assert "已写入" in result

    result = run(
        plugin.memory_write(
            event, "people", "user-123.md", person_file(body="想整文件覆盖")
        )
    )
    assert result == pkg_sharing.APPEND_OVERWRITE_REFUSAL

    result = run(plugin.memory_write(event, "people", "user-123.md", delete=True))
    assert result == pkg_sharing.APPEND_DELETE_REFUSAL

    result = run(
        plugin.memory_edit(
            event, "people", "user-123.md", old_text="工程师", new_text="研究员"
        )
    )
    assert result == pkg_sharing.APPEND_EDIT_REFUSAL

    result = run(
        plugin.memory_edit(
            event, "people", "user-123.md", new_text="补充：喜欢钓鱼", mode="append"
        )
    )
    assert "追加" in result
    content = run(plugin._store.people_scope("Bot-A").read("user-123.md"))
    assert content.rstrip().endswith("补充：喜欢钓鱼")


def test_append_member_cannot_replace_a_forgotten_shared_file(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=append"])
    event_a = FakeEvent("Bot-A")
    event_b = FakeEvent("Bot-B")
    store = plugin._store.people_scope("Bot-A")
    run(plugin.memory_write(event_a, "people", "user-1.md", person_file("user-1")))
    run(store.forget("user-1.md", actor="system"))
    assert store.has_forgotten("user-1.md") and not store.exists("user-1.md")

    result = run(
        plugin.memory_write(
            event_b, "people", "user-1.md", person_file("user-1", body="取代")
        )
    )
    assert result == pkg_sharing.APPEND_OVERWRITE_REFUSAL
    assert store.has_forgotten("user-1.md")
    assert not store.exists("user-1.md")


def test_read_permission_can_read_but_not_write(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=read"])
    event = FakeEvent("Bot-A")
    store = plugin._store.people_scope("Bot-A")
    run(store.write("user-123.md", person_file(), actor="system"))

    assert "测试用户" in run(
        plugin.memory_read(event, "people", "user-123.md")
    )
    assert run(
        plugin.memory_write(event, "people", "user-2.md", person_file("user-2"))
    ) == pkg_sharing.READ_ONLY_REFUSAL
    assert run(
        plugin.memory_write(event, "people", "user-123.md", delete=True)
    ) == pkg_sharing.READ_ONLY_REFUSAL
    assert run(
        plugin.memory_edit(
            event, "people", "user-123.md", old_text="工程师", new_text="研究员"
        )
    ) == pkg_sharing.READ_ONLY_REFUSAL
    assert run(
        plugin.memory_edit(
            event, "people", "user-123.md", new_text="补充", mode="append"
        )
    ) == pkg_sharing.READ_ONLY_REFUSAL
    assert store.exists("user-123.md")


# ---------------------------------------------------------------- 版本检查


def test_version_check_read_never_read_and_changed_by_other(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=edit"])
    event_a = FakeEvent("Bot-A")
    event_b = FakeEvent("Bot-B")

    run(plugin.memory_write(event_a, "people", "user-123.md", person_file()))

    # 没读过：不允许整文件覆盖
    result = run(
        plugin.memory_write(
            event_b, "people", "user-123.md", person_file(body="B 的版本")
        )
    )
    assert result == pkg_sharing.version_conflict_message("user-123.md")
    assert "memory_read" in result and "memory_edit" in result

    # 读过之后、内容没变：允许
    assert "测试用户" in run(
        plugin.memory_read(event_b, "people", "user-123.md")
    )
    result = run(
        plugin.memory_write(
            event_b, "people", "user-123.md", person_file(body="B 的版本")
        )
    )
    assert "已写入" in result

    # A 读过 B 的版本后，B 又改了：A 的覆盖被拒
    run(plugin.memory_read(event_a, "people", "user-123.md"))
    assert "已写入" in run(
        plugin.memory_write(
            event_b, "people", "user-123.md", person_file(body="B 又改了")
        )
    )
    result = run(
        plugin.memory_write(
            event_a, "people", "user-123.md", person_file(body="A 想覆盖")
        )
    )
    assert result == pkg_sharing.version_conflict_message("user-123.md")


def test_concurrent_overwrites_only_one_passes_version_check(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=edit"])
    event_a = FakeEvent("Bot-A")
    event_b = FakeEvent("Bot-B")
    run(plugin.memory_write(event_a, "people", "user-123.md", person_file()))
    # A 自己刚写过（seen 已更新），B 也读到了同一版；两边随后同时整文件覆盖
    assert "测试用户" in run(
        plugin.memory_read(event_b, "people", "user-123.md")
    )

    async def both():
        return await asyncio.gather(
            plugin.memory_write(
                event_a, "people", "user-123.md", person_file(body="A 的版本")
            ),
            plugin.memory_write(
                event_b, "people", "user-123.md", person_file(body="B 的版本")
            ),
        )

    results = run(both())
    assert sum("已写入" in result for result in results) == 1
    assert sum(
        result == pkg_sharing.version_conflict_message("user-123.md")
        for result in results
    ) == 1
    final = run(plugin._store.people_scope("Bot-A").read("user-123.md"))
    assert ("A 的版本" in final) != ("B 的版本" in final)


def test_read_member_cannot_restore_a_forgotten_shared_file(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=read"])
    plugin.config["enable_forgetting"] = True
    event_a = FakeEvent("Bot-A", sender_id="123")
    event_b = FakeEvent("Bot-B", sender_id="123")
    store = plugin._store.people_scope("Bot-A")
    run(plugin.memory_write(event_a, "people", "user-123.md", person_file()))
    run(store.forget("user-123.md", actor="system"))
    assert store.has_forgotten("user-123.md") and not store.exists("user-123.md")

    result = run(plugin.memory_read(event_b, "people", "user-123.md"))
    assert "只返回内容，未恢复在册" in result
    assert store.has_forgotten("user-123.md") and not store.exists("user-123.md")

    # 身份必选召回也不能替 read 级成员恢复
    run(plugin._recall_message(event_b, event_b.unified_msg_origin, "你好"))
    assert store.has_forgotten("user-123.md") and not store.exists("user-123.md")


def test_recall_full_text_counts_as_seen(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A", sender_id="123")
    store = plugin._store.people_scope("Bot-A")
    run(store.write("user-123.md", person_file(), actor="system"))
    raw = run(store.read("user-123.md"))
    assert plugin._seen.seen("Bot-A", "user-123.md") == ""

    message = run(plugin._recall_message(event, event.unified_msg_origin, "你好"))
    assert message is not None
    assert "多个机器人共享" in message["content"]
    assert plugin._seen.seen("Bot-A", "user-123.md") == pkg_sharing.content_sha1(raw)

    # 召回附上的全文算看过，之后的整文件覆盖不会被版本检查挡住
    result = run(
        plugin.memory_write(
            event, "people", "user-123.md", person_file(body="召回后覆盖")
        )
    )
    assert "已写入" in result


def test_recall_only_counts_the_full_text_it_actually_attaches(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A", sender_id="123")
    store = plugin._store.people_scope("Bot-A")
    run(
        store.write(
            "user-123.md",
            person_file(body="长" * (pkg_recall.FILE_CHARS + 10)),
            actor="system",
        )
    )

    message = run(plugin._recall_message(event, event.unified_msg_origin, "你好"))
    assert message is not None and "已截断" in message["content"]
    # 召回块只附了截断后的内容，不算看过完整版本
    assert plugin._seen.seen("Bot-A", "user-123.md") == ""
    result = run(
        plugin.memory_write(
            event, "people", "user-123.md", person_file(body="想覆盖")
        )
    )
    assert result == pkg_sharing.version_conflict_message("user-123.md")


# ---------------------------------------------------------------- 提取门控


def test_extract_gate_depends_on_permission(tmp_path):
    edit = make_plugin(tmp_path / "edit", ["Bot-A=edit"])
    assert edit._can_extract_people("Bot-A:FriendMessage:1") is True
    for level in ("append", "read"):
        plugin = make_plugin(tmp_path / level, [f"Bot-A={level}"])
        assert plugin._can_extract_people("Bot-A:FriendMessage:1") is False
    # 非成员维持上游行为
    outsider = make_plugin(tmp_path / "outsider", ["Bot-A=edit"])
    assert outsider._can_extract_people("Bot-C:FriendMessage:1") is True


def test_background_extraction_writes_people_only_for_edit(tmp_path):
    for level, expect_shared in (("edit", True), ("append", False), ("read", False)):
        plugin = make_plugin(tmp_path / level, [f"Bot-A={level}"])
        event = FakeEvent("Bot-A")

        async def fake(umo, prompt):
            return EXTRACT_TEXT, []

        plugin._call_summarizer = fake
        rt = main.RuntimeSession(state=pkg_compress.SessionState())
        run(
            plugin._run_extraction(
                rt, event.unified_msg_origin, "用户 123 说自己是工程师"
            )
        )
        shared = plugin._store.people_scope("Bot-A")
        session = plugin._store.session_scope(event.unified_msg_origin)
        if expect_shared:
            assert shared.exists("user-123.md")
            assert not session.exists("user-123.md")
        else:
            assert not shared.exists("user-123.md")
            assert session.exists("user-123.md")


def test_compression_extraction_writes_people_only_for_edit(tmp_path):
    contexts = [
        {"role": "system", "content": "系统"},
        {"role": "user", "content": "我叫小明，是一名工程师"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "今天也在写代码"},
        {"role": "assistant", "content": "嗯"},
    ]
    for level, expect_shared in (("edit", True), ("append", False)):
        plugin = make_plugin(tmp_path / level, [f"Bot-A={level}"])
        event = FakeEvent("Bot-A")

        async def fake(umo, prompt):
            return f"<summary>摘要</summary>{EXTRACT_TEXT}", []

        plugin._call_summarizer = fake
        rt = main.RuntimeSession(state=pkg_compress.SessionState())
        run(
            plugin._compress(
                event, rt, event.unified_msg_origin, contexts, 1000
            )
        )
        shared = plugin._store.people_scope("Bot-A")
        session = plugin._store.session_scope(event.unified_msg_origin)
        if expect_shared:
            assert shared.exists("user-123.md")
            assert not session.exists("user-123.md")
        else:
            assert not shared.exists("user-123.md")
            assert session.exists("user-123.md")


# ---------------------------------------------------------------- 空配置与规范


def test_empty_config_behaves_like_upstream(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._store.people_scope("Bot-A")
    assert store.root.name != pkg_sharing.SHARED_KEY

    assert "已写入" in run(
        plugin.memory_write(event, "people", "user-123.md", person_file())
    )
    # 空配置没有版本检查：没读过也能整文件覆盖
    assert "已写入" in run(
        plugin.memory_write(
            event, "people", "user-123.md", person_file(body="第二版")
        )
    )
    assert "已修改" in run(
        plugin.memory_edit(
            event, "people", "user-123.md", old_text="第二版", new_text="第三版"
        )
    )
    assert "已删除" in run(
        plugin.memory_write(event, "people", "user-123.md", delete=True)
    )

    guidance = plugin._guidance(event.unified_msg_origin)
    assert guidance == pkg_prompts.memory_guidance(
        False, True, False, shared_people=False
    )
    assert "共享人物空间" not in guidance


def test_shared_member_guidance_adds_shared_rule(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=read"])
    guidance = plugin._guidance("Bot-A:FriendMessage:1")
    assert "共享人物空间" in guidance
    assert "中性的第三人称" in guidance
    assert "一律写 session" in guidance


# ---------------------------------------------------------------- /mb_status


def _status_text(plugin, event):
    async def collect():
        chunks = []
        async for chunk in plugin.show_status(event):
            chunks.append(chunk)
        return "\n".join(chunks)

    plugin._runtime = lambda umo: _async_value(
        types.SimpleNamespace(state=pkg_compress.SessionState())
    )
    plugin._resolve_window = lambda event: _async_value((128000, "测试窗口"))
    plugin._estimate_live = lambda event, rt: _async_value((0, {}))
    plugin._loop_guard_label = lambda rt: "关闭"
    plugin._recall_label = lambda: "关闭"
    plugin._extract_label = lambda state: "关闭"
    plugin._forget_label = lambda umo: "关闭"
    plugin._tidy_label = lambda umo: "关闭"
    plugin._warned_threshold = False
    return run(collect())


def _async_value(value):
    async def inner(*args, **kwargs):
        return value

    return inner()


def test_mb_status_shows_shared_space_and_permission(tmp_path):
    plugin = make_plugin(
        tmp_path, ["Bot-A=edit", "Bot-B=append", "Bot-C=read"]
    )
    for platform_id, permission in (
        ("Bot-A", "edit"),
        ("Bot-B", "append"),
        ("Bot-C", "read"),
    ):
        text = _status_text(plugin, FakeEvent(platform_id))
        assert f"共享人物空间：是（权限 {permission}）" in text
    outsider = _status_text(plugin, FakeEvent("Bot-D"))
    assert "共享人物空间：否（本机器人 Bot-D 不在共享成员列表）" in outsider

    off = _status_text(make_plugin(tmp_path / "off", []), FakeEvent("Bot-A"))
    assert "共享人物空间：否（未启用）" in off


# ---------------------------------------------------------------- 3.4 编辑与检索


def test_append_permission_cannot_use_section_mode(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=append"])
    event = FakeEvent("Bot-A")
    body = "## 基本信息\n\n身份：工程师\n"
    assert "已写入" in run(
        plugin.memory_write(event, "people", "user-123.md", person_file(body=body))
    )
    store = plugin._people_scope(event.unified_msg_origin)
    before = run(store.read("user-123.md"))

    result = run(
        plugin.memory_edit(
            event,
            "people",
            "user-123.md",
            old_text="## 基本信息",
            new_text="身份：研究员",
            mode="section",
        )
    )
    assert "只能用 mode=append" in result
    assert run(store.read("user-123.md")) == before


def test_edit_permission_section_mode_keeps_heading_and_history(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A")
    body = "## 基本信息\n\n身份：工程师\n\n## 爱好\n\n钓鱼\n"
    assert "已写入" in run(
        plugin.memory_write(event, "people", "user-123.md", person_file(body=body))
    )
    store = plugin._people_scope(event.unified_msg_origin)

    result = run(
        plugin.memory_edit(
            event,
            "people",
            "user-123.md",
            old_text="## 基本信息",
            new_text="身份：研究员",
            mode="section",
        )
    )
    assert "已替换" in result
    content = run(store.read("user-123.md"))
    assert "## 基本信息" in content and "身份：研究员" in content
    assert "身份：工程师" not in content
    assert "## 爱好" in content and "钓鱼" in content

    versions = store.history_versions("user-123.md")
    assert versions and versions[0].action == pkg_history.ACTION_EDIT
    assert versions[0].actor == "Bot-A"
    assert "身份：工程师" in store.read_history("user-123.md", versions[0].version)


def test_dangling_link_hint_only_checks_visible_scopes(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=edit"])
    bot_a = FakeEvent("Bot-A", umo="Bot-A:FriendMessage:1")
    bot_b = FakeEvent("Bot-B", umo="Bot-B:FriendMessage:2")
    bot_c = FakeEvent("Bot-C", umo="Bot-C:FriendMessage:3")

    # 共享人物文件对两个成员都可见
    assert "已写入" in run(
        plugin.memory_write(
            bot_a, "people", "user-1.md", person_file("user-1", "身份：甲")
        )
    )
    # Bot-A 的会话文件与 Bot-C 的独立人物文件，Bot-B 都看不到
    assert "已写入" in run(
        plugin.memory_write(
            bot_a, "session", "private-note.md", person_file("private-note", "甲的秘密")
        )
    )
    assert "已写入" in run(
        plugin.memory_write(
            bot_c, "people", "user-9.md", person_file("user-9", "身份：丙")
        )
    )

    result = run(
        plugin.memory_write(
            bot_b,
            "session",
            "b.md",
            person_file("b", "见 [[user-1]]、[[private-note]] 与 [[user-9]]。"),
        )
    )
    assert "已写入" in result
    assert "[[private-note]]" in result and "找不到" in result
    assert "[[user-9]]" in result
    # 共享人物文件可见，不算悬空
    assert "[[user-1]]" not in result


def test_dangling_link_hint_sees_enabled_global_but_not_disabled_people(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["enable_global_memory"] = True
    event = FakeEvent("Bot-A")
    assert "已写入" in run(
        plugin.memory_write(event, "global", "g.md", person_file("g", "全局事实"))
    )
    linked = run(
        plugin.memory_write(
            event, "session", "s.md", person_file("s", "见 [[g]]")
        )
    )
    assert "[[g]]" not in linked

    # people 关闭时，磁盘上即使有文件也不在可见范围里
    plugin.config["enable_people_memory"] = False
    run(
        plugin._store.people_scope("Bot-A").write(
            "user-1.md", person_file("user-1", "身份：甲")
        )
    )
    hidden = run(
        plugin.memory_write(
            event, "session", "t.md", person_file("t", "见 [[user-1]]")
        )
    )
    assert "[[user-1]]" in hidden and "找不到" in hidden


def test_write_and_edit_receipts_mention_links_and_long_files(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    long_body = "见 [[missing]]。" + "x" * (9 * 1024)
    result = run(
        plugin.memory_write(
            event, "session", "big.md", person_file("big", long_body)
        )
    )
    assert "找不到对应文件" in result and "[[missing]]" in result
    assert "偏长" in result and "超过 8KB" in result

    # 日志只按体积判断：日期很多但文件不大时不提醒
    log = run(
        plugin.memory_write(
            event,
            "session",
            "daily-2026-11-03.md",
            person_file("daily-2026-11-03", "2026-01-01 " * 20),
        )
    )
    assert "偏长" not in log

    # memory_edit 的成功回执也走同一套检查
    assert "已写入" in run(
        plugin.memory_write(
            event,
            "session",
            "small.md",
            person_file("small", "## 甲\n\n旧正文\n"),
        )
    )
    edited = run(
        plugin.memory_edit(
            event,
            "session",
            "small.md",
            old_text="## 甲",
            new_text="见 [[nope]]",
            mode="section",
        )
    )
    assert "已替换" in edited
    assert "[[nope]]" in edited and "找不到" in edited


def test_memory_search_tool_mode_ranking_and_format(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._session_scope(event)
    run(store.write("a.md", person_file("a", "alpha 第一行")))
    run(store.write("b.md", person_file("b", "beta 第二行")))

    assert "没有找到" in run(
        plugin.memory_search(event, "alpha beta", scope="session")
    )
    result = run(
        plugin.memory_search(event, "alpha beta", scope="session", mode="any")
    )
    assert "a.md" in result and "b.md" in result
    assert "（user，更新 " in result
    assert "alpha 第一行" in result
    assert "mode 只能是" in run(
        plugin.memory_search(event, "alpha", scope="session", mode="bogus")
    )


def test_memory_search_tool_not_crowded_out_by_daily_files(tmp_path):
    """回归：按文件名取前 8 条时，daily-* 会把 important.md 挤出结果。"""
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._session_scope(event)
    run(store.write("important.md", person_file("important", "项目 事实")))
    for day in range(1, 10):
        run(
            store.write(
                f"daily-2026-01-{day:02d}.md",
                person_file(f"daily-2026-01-{day:02d}", "项目 日志"),
            )
        )
    result = run(plugin.memory_search(event, "项目", scope="session"))
    assert "important.md" in result
    # 非日志排在日志前面
    assert result.index("important.md") < result.index("daily-2026-01-01.md")


def test_mb_status_lists_long_session_files(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    assert "已写入" in run(
        plugin.memory_write(
            event,
            "session",
            "big.md",
            person_file("big", "x" * (9 * 1024)),
        )
    )
    assert "已写入" in run(
        plugin.memory_write(
            event, "session", "short.md", person_file("short", "短正文")
        )
    )
    text = _status_text(plugin, event)
    line = next(
        item for item in text.splitlines() if item.startswith("会话长文件：")
    )
    assert "big.md" in line and "超过 8KB" in line
    assert "short.md" not in line
