"""共享人物空间在工具层的权限、版本检查、召回与提取门控。

用最小的 AstrBot 替身导入 main，按不同 platform_id 的模拟事件调用真实的
记忆工具方法；记忆读写全部落在临时目录的合成数据上。
"""

import asyncio
import importlib
import importlib.machinery
import importlib.util
import logging
import os
import sys
import time
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
        logger = logging.getLogger("memory_pod_sharing_test")
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
        web_mod._memory_pod_placeholder = True
        sys.modules["astrbot.api.web"] = web_mod
    api.web = web_mod


def _release_web_stub():
    """撤掉只为导入 main 而建的 astrbot.api.web 与 web_api 模块。"""
    web_mod = sys.modules.get("astrbot.api.web")
    if web_mod is None or not getattr(web_mod, "_memory_pod_placeholder", False):
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
pkg_context_stats = importlib.import_module(f"{PACKAGE}.core.context_stats")
pkg_history = importlib.import_module(f"{PACKAGE}.core.history")
pkg_index_view = importlib.import_module(f"{PACKAGE}.core.index_view")
pkg_injection = importlib.import_module(f"{PACKAGE}.core.injection")
pkg_memstore = importlib.import_module(f"{PACKAGE}.core.memstore")
pkg_prompts = importlib.import_module(f"{PACKAGE}.core.prompts")
pkg_recall = importlib.import_module(f"{PACKAGE}.core.recall")
pkg_registry = importlib.import_module(f"{PACKAGE}.core.registry")
pkg_sharing = importlib.import_module(f"{PACKAGE}.core.sharing")
pkg_pending = importlib.import_module(f"{PACKAGE}.core.pending")


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


def note_file(name="note", description="合成记忆", body="合成正文"):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        "  type: user\n"
        "---\n\n"
        f"{body}\n"
    )


def make_plugin(tmp_path, members):
    plugin = object.__new__(main.MemoryPodPlugin)
    plugin.context = FakeContext()
    plugin.config = {
        "enable_memory": True,
        "enable_global_memory": False,
        "shared_people_bots": members,
        "recall_max_files": 5,
        "auto_extract_interval": 0,
        "extract_idle_minutes": 10,
    }
    plugin.data_dir = tmp_path
    plugin._store = pkg_memstore.MemoryStore(tmp_path / "memories")
    plugin._sessions = OrderedDict()
    plugin._registry = pkg_registry.SessionRegistry(tmp_path / "sessions.json")
    plugin._history_cleanup_at = time.time()
    plugin._sharing = pkg_sharing.Membership()
    plugin._sharing_warned = set()
    plugin._state_dir = tmp_path / "state"
    plugin._pending = pkg_pending.PendingBook(tmp_path / "state" / "pending")
    plugin._write_scheduled = set()
    plugin._write_tasks = {}
    plugin._tasks = set()
    plugin._inflight = {}
    plugin._pending_loop = None
    plugin._tidy_checked = {}
    plugin._background_gate = asyncio.Semaphore(1)
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

    run(other.write("user-1.md", person_file("user-1")))
    assert (other.root / "user-1.md").is_file()
    assert not (member.root / "user-1.md").exists()


# ---------------------------------------------------------------- 三种权限






# ---------------------------------------------------------------- 版本检查








# ---------------------------------------------------------------- 私聊人物档案


def test_private_profile_pinned_in_opening_and_excluded_from_recall(tmp_path):
    """私聊时发送者的 session/people 档案随索引进开头块，不再每轮召回。"""
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A", sender_id="123")
    umo = event.unified_msg_origin
    session = plugin._store.session_scope(umo)
    people = plugin._store.people_scope("Bot-A")
    run(session.write("user-123.md", person_file(body="本会话里的身份"), actor="system"))
    run(people.write("user-123.md", person_file(body="跨群身份事实"), actor="system"))
    run(
        session.write(
            "weekend-game.md",
            note_file("weekend-game", "每周六晚上的开黑活动", "晚上八点集合"),
        )
    )

    rt = main.RuntimeSession()
    opening = run(plugin._index_block(rt, event, record_usage=True))
    assert opening is not None
    assert pkg_injection.PROFILE_HEADING in opening
    assert "### session/user-123.md（user｜" in opening
    assert "### people/user-123.md（user｜多个机器人共享｜" in opening
    assert "本会话里的身份" in opening and "跨群身份事实" in opening
    # 注入的是去掉 frontmatter 的正文
    assert "name: user-123" not in opening and "metadata:" not in opening
    # 钉在开头块的档案每轮都算用到：不会被遗忘扫描当成从未使用
    assert people.usage_of("user-123.md").uses == 1
    assert session.usage_of("user-123.md").uses == 1
    run(plugin._index_block(rt, event, record_usage=True))  # 命中快照也算一轮
    assert people.usage_of("user-123.md").uses == 2
    assert session.usage_of("user-123.md").uses == 2

    # 默认配置下召回块只带相关会话记忆，发送者档案不再出现
    message = run(plugin._recall_message(event, umo, "开黑", rt))
    assert message is not None
    content = message
    assert "weekend-game.md" in content
    assert "### session/user-123.md" not in content
    assert "### people/user-123.md" not in content
    assert "本会话里的身份" not in content and "跨群身份事实" not in content




def test_deleting_pinned_profile_invalidates_opening_snapshot(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A", sender_id="123")
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-123.md", person_file(body="跨群身份事实"), actor="system"))
    rt = main.RuntimeSession()
    plugin._sessions[event.unified_msg_origin] = rt
    opening = run(plugin._index_block(rt, event, record_usage=True))
    assert "跨群身份事实" in opening

    result = run(people.delete("user-123.md"))
    assert result.ok
    # 后台删除后由插件作废开头块快照（与 core.writer 的删除路径一致）
    plugin._invalidate_changes(event.unified_msg_origin, [("people", "user-123.md", "delete")])
    assert rt.index_loaded is False
    # 下一次开头块不再包含已删除的档案
    refreshed = run(plugin._index_block(rt, event, record_usage=True))
    assert refreshed is None or "跨群身份事实" not in refreshed


def test_forgetting_pinned_profile_invalidates_opening_snapshot(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["enable_forgetting"] = True
    plugin.config["forget_after_days"] = 1
    event = FakeEvent("Bot-A", sender_id="123")
    people = plugin._store.people_scope("Bot-A")
    people.root.mkdir(parents=True, exist_ok=True)
    path = people.root / "user-123.md"
    path.write_text(person_file(body="跨群身份事实"), encoding="utf-8")
    old = time.time() - 10 * 86400
    os.utime(path, (old, old))

    rt = main.RuntimeSession()
    plugin._sessions[event.unified_msg_origin] = rt
    opening = run(plugin._index_block(rt, event, record_usage=True))
    assert "跨群身份事实" in opening

    now = time.time()
    # 模拟这条档案确实很久没被用到（开头块每轮记使用统计会刷新它）
    run(asyncio.to_thread(people.note_used, ["user-123.md"], now - 10 * 86400))
    run(plugin._forget_sweep([("", people)], now))
    assert people.has_forgotten("user-123.md")
    assert rt.index_loaded is False
    refreshed = run(plugin._index_block(rt, event, record_usage=True))
    assert refreshed is None or "跨群身份事实" not in refreshed


def test_people_edit_invalidates_pinned_snapshot(tmp_path):
    """管理页按 people 目录名改动时也要作废开头块快照。"""
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A", sender_id="123")
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-123.md", person_file(body="旧身份"), actor="system"))
    rt = main.RuntimeSession()
    plugin._sessions[event.unified_msg_origin] = rt
    assert "旧身份" in run(plugin._index_block(rt, event))

    run(people.write("user-123.md", person_file(body="新身份"), actor="system"))
    # 管理页传进来的 key 是 people 目录名 "Bot-A"，不对应任何会话运行时
    plugin.invalidate_for_key("Bot-A")
    assert rt.index_loaded is False
    refreshed = run(plugin._index_block(rt, event))
    assert "新身份" in refreshed and "旧身份" not in refreshed


def test_sharing_membership_change_invalidates_pinned_snapshot(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A", sender_id="123")
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-123.md", person_file(body="共享身份事实"), actor="system"))
    rt = main.RuntimeSession()
    plugin._sessions[event.unified_msg_origin] = rt
    assert "共享身份事实" in run(plugin._index_block(rt, event))

    plugin.config["shared_people_bots"] = []
    plugin._refresh_sharing()
    assert rt.index_loaded is False
    refreshed = run(plugin._index_block(rt, event))
    assert refreshed is None or "共享身份事实" not in refreshed


def test_recall_off_does_not_restore_or_attach_updated_profile(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["recall_max_files"] = 0
    event = FakeEvent("Bot-A", sender_id="123")
    umo = event.unified_msg_origin
    session = plugin._store.session_scope(umo)
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-123.md", person_file(body="旧档案"), actor="system"))
    run(session.write("user-123.md", person_file(body="身份"), actor="system"))

    rt = main.RuntimeSession()
    assert "旧档案" in run(plugin._index_block(rt, event))
    run(people.write("user-123.md", person_file(body="新档案"), actor="system"))
    assert run(plugin._recall_message(event, umo, "你好", rt)) is None

    # 关闭召回时不再把已遗忘的身份文件移回在册
    run(session.forget("user-123.md"))
    assert session.has_forgotten("user-123.md")
    assert run(plugin._recall_message(event, umo, "你好", rt)) is None
    assert session.has_forgotten("user-123.md")
    assert not session.exists("user-123.md")


def test_group_chat_never_pins_profile(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:555")
    people = plugin._store.people_scope("Bot-A")
    session = plugin._store.session_scope(event.unified_msg_origin)
    run(people.write("user-123.md", person_file(body="跨群身份事实"), actor="system"))
    run(session.write("weekend-game.md", note_file("weekend-game", "开黑活动", "周六")))

    rt = main.RuntimeSession()
    opening = run(plugin._index_block(rt, event))
    # 群聊有索引块，但绝不含发送者档案区或钉住的 people 文件
    assert opening is not None
    assert pkg_injection.PROFILE_HEADING not in opening
    assert "跨群身份事实" not in opening
    assert "weekend-game.md" in opening
    assert rt.index_pinned == {}
    # 群聊行为不变：人物档案仍作为身份必选进入每轮召回
    message = run(plugin._recall_message(event, event.unified_msg_origin, "你好", rt))
    assert message is not None
    assert "跨群身份事实" in message
    assert pkg_injection.PROFILE_HEADING not in message


def test_pin_snapshot_is_stable_and_updated_profile_wins(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    event = FakeEvent("Bot-A", sender_id="123")
    umo = event.unified_msg_origin
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-123.md", person_file(body="旧版本档案"), actor="system"))

    rt = main.RuntimeSession()
    first = run(plugin._index_block(rt, event))
    # 记忆没变时逐轮逐字不变（前缀缓存）
    assert run(plugin._index_block(rt, event)) == first
    assert "旧版本档案" in first

    # 快照之后档案被改过：快照仍是旧的，本轮召回块附最新全文并注明以它为准
    run(people.write("user-123.md", person_file(body="新版本档案"), actor="system"))
    assert run(plugin._index_block(rt, event)) == first
    message = run(plugin._recall_message(event, umo, "你好", rt))
    assert message is not None
    assert "人物档案已更新，以这里为准" in message
    assert "新版本档案" in message

    # 索引刷新（压缩/切换对话/管理页改动后）改为钉住新版本
    rt.clear_index()
    refreshed = run(plugin._index_block(rt, event))
    assert "新版本档案" in refreshed and "旧版本档案" not in refreshed


def test_index_snapshot_does_not_follow_usage_changes(tmp_path, monkeypatch):
    """超限丢行只在生成快照时读一次使用统计，缓存期间不跟每轮召回漂移。"""
    plugin = make_plugin(tmp_path, [])
    long_desc = "描" * 100
    note = pkg_index_view._drop_note(1)
    budget = sum(
        len(pkg_index_view.compact_index_line(pkg_memstore.format_index_line(f"{name}.md", long_desc)).encode("utf-8")) + 1
        for name in ("a", "b", "c")
    )
    monkeypatch.setattr(pkg_memstore, "INDEX_MAX_BYTES", budget + len(note.encode("utf-8")) + 1)
    event = FakeEvent("Bot-A", sender_id="123")
    session = plugin._store.session_scope(event.unified_msg_origin)
    for name in ("a", "b", "c", "d"):
        run(session.write(f"{name}.md", note_file(name, long_desc, "正文")))
    # a 最久没用：第一次生成快照时丢 a
    session.note_used(["a.md"], 100.0)

    rt = main.RuntimeSession()
    first = run(plugin._index_block(rt, event))
    assert "a.md" not in first
    assert "还有 1 条未加载，可用 memory_search 找到" in first

    # 使用统计变了，但快照缓存期间注入文本逐字不变
    session.note_used(["b.md"], 50.0)
    assert run(plugin._index_block(rt, event)) == first

    # 快照刷新（压缩、切换对话或管理页改动后）才按新统计重新决定丢谁
    rt.clear_index()
    refreshed = run(plugin._index_block(rt, event))
    assert "b.md" not in refreshed and "a.md" in refreshed


def test_updated_profile_does_not_use_recall_full_quota(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    plugin.config["recall_full_files"] = 1
    event = FakeEvent("Bot-A", sender_id="123")
    umo = event.unified_msg_origin
    session = plugin._store.session_scope(umo)
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-123.md", person_file(body="旧档案"), actor="system"))
    run(session.write("a.md", note_file("a", "开黑活动安排", "周六晚上")))
    run(session.write("b.md", note_file("b", "开黑活动记录", "周日下午")))

    rt = main.RuntimeSession()
    run(plugin._index_block(rt, event))
    run(people.write("user-123.md", person_file(body="新档案"), actor="system"))

    message = run(plugin._recall_message(event, umo, "开黑", rt))
    content = message
    assert "人物档案已更新，以这里为准" in content
    # 更新档案不占名额：仍有一个会话文件带全文，另一个进指针
    assert "### session/a.md" in content or "### session/b.md" in content
    assert "可能也相关，需要细节时用 memory_read 读取：" in content


def test_full_quota_at_limit_matches_keyword_selection(tmp_path):
    """recall_full_files 不小于 recall_max_files：选中与顺序同不分档时一致。

    用群聊事件避开私聊开头块钉住的人物档案（那只在私聊生效）。
    """
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    plugin.config["recall_full_files"] = 5
    event = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:GroupMessage:555")
    umo = event.unified_msg_origin
    session = plugin._store.session_scope(umo)
    people = plugin._store.people_scope("Bot-A")
    run(session.write("a.md", note_file("a", "开黑活动安排", "周六晚上")))
    run(session.write("b.md", note_file("b", "开黑活动记录", "周日下午")))
    run(session.write("c.md", note_file("c", "开黑活动名单", "固定成员")))
    run(people.write("user-123.md", person_file(body="喜欢开黑"), actor="system"))

    candidates = [
        pkg_recall.Candidate(label, header)
        for label, store in (
            ("session", session),
            ("people", people),
        )
        for header in store.headers()
    ]
    expected = pkg_recall.select_by_keywords(
        candidates, "开黑", ["user-123.md"], 5
    )
    assert len(expected) >= 3
    rt = main.RuntimeSession()
    message = run(plugin._recall_message(event, umo, "开黑", rt))
    content = message
    positions = [content.index(f"### {cand.ref}（") for cand in expected]
    assert positions == sorted(positions)
    # 全部带全文，没有指针行
    assert "可能也相关" not in content
    assert pkg_injection.PROFILE_HEADING not in content


def test_recall_message_delegates_to_core_pipeline(tmp_path, monkeypatch):
    """main 的 _recall_message 把挑选、读文件、截断、渲染交给 core.recall。"""
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A", sender_id="123")
    umo = event.unified_msg_origin
    session = plugin._store.session_scope(umo)
    run(
        session.write(
            "weekend-game.md",
            note_file("weekend-game", "每周六晚上的开黑活动", "晚上八点集合"),
        )
    )

    calls = {}
    real = pkg_recall.assemble_recall

    async def spy(stores, candidates, **kwargs):
        calls["stores"] = sorted(stores)
        calls["refs"] = [c.ref for c in candidates]
        calls["kwargs"] = kwargs
        return await real(stores, candidates, **kwargs)

    monkeypatch.setattr(pkg_recall, "assemble_recall", spy)
    message = run(plugin._recall_message(event, umo, "这周六还开黑吗"))
    assert message is not None and "weekend-game.md" in message
    assert calls["stores"] == ["people", "session"]
    assert "session/weekend-game.md" in calls["refs"]
    assert calls["kwargs"]["query"] == "这周六还开黑吗"
    assert calls["kwargs"]["forced_files"] == ["user-123.md"]
    assert calls["kwargs"]["limit"] == 5
    assert calls["kwargs"]["shared_people"] is False
    assert calls["kwargs"]["data_notice"] == pkg_prompts.DATA_NOTICE


# ---------------------------------------------------------------- 提取门控



def test_auto_tidy_people_target_requires_edit(tmp_path):
    """定期自动整理共享人物空间只由 edit 级成员的会话触发。"""

    async def scheduled(level, event=None, members=None):
        plugin = make_plugin(
            tmp_path / level,
            members if members is not None else [f"Bot-A={level}"],
        )
        plugin.config["auto_consolidate"] = "auto"
        plugin._tidy_checked = {}
        plugin._tasks = set()
        seen = []

        async def fake_auto(targets, mode):
            seen.append([(scope, key) for scope, key, _ in targets])

        plugin._auto_tidy = fake_auto
        plugin._maybe_tidy(event or FakeEvent("Bot-A"), 1000.0)
        await asyncio.sleep(0.01)
        return seen

    edit_batches = run(scheduled("edit"))
    assert ("people", pkg_sharing.SHARED_KEY) in edit_batches[0]
    for level in ("append", "read"):
        batches = run(scheduled(level))
        # 会话作用域照旧；共享人物空间不排入
        assert batches and all(
            scope == "session" for batch in batches for scope, _ in batch
        )

    # 非共享人物空间：只由自己一个平台使用，照旧触发定期整理
    solo = run(scheduled("solo", members=[]))
    assert ("people", pkg_memstore.safe_key("Bot-A")) in solo[0]

    # UMO 前缀是 edit 成员、事件 platform_id 却是 read 成员：按事件身份判定，
    # 不能借 UMO 触发共享人物空间的自动整理
    mismatch = run(
        scheduled(
            "mismatch",
            event=FakeEvent("Bot-B", umo="Bot-A:FriendMessage:1"),
            members=["Bot-A=edit", "Bot-B=read"],
        )
    )
    assert all(
        scope != "people" for batch in mismatch for scope, _ in batch
    )


def test_auto_forget_people_target_uses_event_identity(tmp_path):
    """遗忘扫描同样按事件的 platform_id 判定共享人物空间权限。"""

    async def scheduled(members, event):
        plugin = make_plugin(tmp_path / "forget", members)
        plugin.config["enable_forgetting"] = True
        plugin._last_sweep = {}
        plugin._tasks = set()
        seen = []

        async def fake_sweep(targets, now):
            seen.append([(key, store.root.name) for key, store in targets])

        plugin._forget_sweep = fake_sweep
        plugin._maybe_forget(event, 10 ** 9)
        await asyncio.sleep(0.01)
        return seen

    normal = run(scheduled(["Bot-A=edit"], FakeEvent("Bot-A")))
    assert ("", pkg_sharing.SHARED_KEY) in normal[0]
    mismatch = run(
        scheduled(
            ["Bot-A=edit", "Bot-B=read"],
            FakeEvent("Bot-B", umo="Bot-A:FriendMessage:1"),
        )
    )
    assert all(name != pkg_sharing.SHARED_KEY for _, name in mismatch[0])




# ---------------------------------------------------------------- 空配置与规范


def test_empty_config_behaves_like_upstream(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._store.people_scope("Bot-A")
    assert store.root.name != pkg_sharing.SHARED_KEY
    run(store.write("user-123.md", person_file(), actor="system"))

    assert "身份：测试用户" in run(
        plugin.memory_read(event, "people", "user-123.md")
    )
    assert "user-123.md" in run(plugin.memory_search(event, "工程师"))
    assert "已记下" in run(plugin.memory_note(event, "记住用户是工程师"))

    guidance = plugin._guidance(event.unified_msg_origin)
    assert guidance == pkg_prompts.memory_guidance()
    assert "共享人物空间" not in guidance



# ---------------------------------------------------------------- /mb_status


def _status_text(plugin, event):
    async def collect():
        chunks = []
        async for chunk in plugin.show_status(event):
            chunks.append(chunk)
        return "\n".join(chunks)

    plugin._runtime = lambda umo: _async_value(
        types.SimpleNamespace(stats=pkg_context_stats.ContextStats())
    )
    plugin._resolve_window_for = lambda umo: _async_value((128000, "测试窗口"))
    plugin._estimate_live = lambda event, rt: _async_value((0, {}))
    plugin._recall_label = lambda: "关闭"
    plugin._extract_label = lambda state: "关闭"
    plugin._forget_label = lambda umo: "关闭"
    plugin._tidy_label = lambda umo: "关闭"
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


def test_memory_search_tool_not_crowded_out_by_journal_files(tmp_path):
    """回归：按文件名取前 8 条时，日志会把 important.md 挤出结果。"""
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._session_scope(event)
    run(store.write("important.md", person_file("important", "项目 事实")))
    for day in range(1, 10):
        run(
            store.write(
                f"journal-2026-01-{day:02d}.md",
                person_file(f"journal-2026-01-{day:02d}", "项目 日志"),
            )
        )
    result = run(plugin.memory_search(event, "项目", scope="session"))
    assert "important.md" in result
    # 非日志排在日志前面
    assert result.index("important.md") < result.index("journal-2026-01-01.md")


def test_memory_search_tool_forgotten_results_use_same_ranking_and_cap(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["enable_forgetting"] = True
    event = FakeEvent("Bot-A")
    store = plugin._session_scope(event)
    run(store.write("important.md", person_file("important", "项目 事实")))
    for day in range(1, 10):
        run(
            store.write(
                f"journal-2026-01-{day:02d}.md",
                person_file(f"journal-2026-01-{day:02d}", "项目 日志"),
            )
        )
    for name in store.list_files():
        assert run(store.forget(name)).ok

    result = run(plugin.memory_search(event, "项目", scope="session"))
    assert "important.md" in result
    entries = [
        line for line in result.splitlines() if line.startswith("[session")
    ]
    assert len(entries) == pkg_memstore.MAX_SEARCH_RESULTS == 8
    assert entries[0].startswith("[session·已遗忘] important.md")


def test_mb_status_lists_long_session_files(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._session_scope(event)
    assert run(store.write("big.md", person_file("big", "x" * (9 * 1024)))).ok
    assert run(store.write("short.md", person_file("short", "短正文"))).ok
    assert run(store.write("dense.md", person_file("dense", "2026-01-01 " * 16))).ok
    assert run(
        store.write(
            "journal-2026-11-03.md",
            person_file("journal-2026-11-03", "2026-01-01 " * 20),
        )
    ).ok
    text = _status_text(plugin, event)
    line = next(
        item for item in text.splitlines() if item.startswith("会话长文件：")
    )
    assert "big.md" in line and "超过 8KB" in line
    assert "dense.md" in line and "16 个日期" in line
    assert "short.md" not in line
    # 日志只看体积：日期虽多但文件不大，不列入长文件
    assert "journal-2026-11-03.md" not in line


def test_mb_status_survives_non_utf8_session_file(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    store = plugin._session_scope(event)
    assert run(store.write("ok.md", person_file("ok", "短正文"))).ok
    # 直接放一个非 UTF-8 的大文件：状态页只做统计，不能因解码失败而崩掉
    (store.root / "bad.md").write_bytes(b"\xff" * (9 * 1024))
    text = _status_text(plugin, event)
    line = next(
        item for item in text.splitlines() if item.startswith("会话长文件：")
    )
    assert "bad.md" in line and "超过 8KB" in line


# ---------------------------------------------------------------- 审查回归


def test_read_search_and_pin_follow_event_platform_not_umo_prefix(tmp_path):
    """UMO 前缀与事件 platform_id 不一致时，读/搜/钉不能落到别人的共享目录。"""
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    shared = plugin._store.people_scope("Bot-A")
    run(shared.write("user-123.md", person_file(body="共享私密正文"), actor="system"))
    event = FakeEvent("Bot-Z", sender_id="123", umo="Bot-A:FriendMessage:100")

    assert "共享私密正文" not in run(
        plugin.memory_read(event, "people", "user-123.md")
    )
    result = run(plugin.memory_search(event, "私密", scope="people"))
    assert "user-123" not in result and "正文" not in result
    rt = main.RuntimeSession()
    opening = run(plugin._index_block(rt, event))
    assert opening is None or "共享私密正文" not in opening

    # 非成员的后台写入也落在自己的 people 目录，不会借 UMO 前缀写进 _shared
    stores, _, _ = plugin._write_stores(event.unified_msg_origin, "Bot-Z", False)
    own = plugin._store.people_scope("Bot-Z")
    assert stores["people"].root == own.root
    assert run(own.write("user-9.md", person_file("user-9"))).ok
    assert own.exists("user-9.md")
    assert not shared.exists("user-9.md")

    # 取不到 platform_id 且 UMO 指向共享目录：fail-closed
    unknown = FakeEvent("", sender_id="123", umo="Bot-A:FriendMessage:100")
    assert run(
        plugin.memory_read(unknown, "people", "user-123.md")
    ) == pkg_sharing.IDENTITY_REFUSAL
    assert run(
        plugin.memory_search(unknown, "共享私密正文", scope="people")
    ) == pkg_sharing.IDENTITY_REFUSAL





def test_platform_type_mismatch_stays_disabled_when_types_temporarily_missing(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit", "Bot-B=edit", "Bot-C=append"])
    plugin.context.platform_manager = FakePlatformManager(
        [
            FakeInst("Bot-A", "aiocqhttp"),
            FakeInst("Bot-B", "aiocqhttp"),
            FakeInst("Bot-C", "discord"),
        ]
    )
    plugin._refresh_sharing()
    assert plugin._sharing.permission("Bot-C") is None

    # 适配器暂时取不到类型时，沿用上次已知类型，不能把 C 放回共享
    plugin.context.platform_manager = FakePlatformManager([])
    plugin._refresh_sharing()
    assert plugin._sharing.permission("Bot-C") is None
    assert plugin._store.people_scope("Bot-C").root.name != pkg_sharing.SHARED_KEY


def test_other_private_session_does_not_get_pinned_profile(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event_a = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:FriendMessage:100")
    event_b = FakeEvent("Bot-A", sender_id="123", umo="Bot-A:FriendMessage:200")
    session_a = plugin._store.session_scope(event_a.unified_msg_origin)
    session_b = plugin._store.session_scope(event_b.unified_msg_origin)
    people = plugin._store.people_scope("Bot-A")
    run(session_a.write("user-123.md", person_file(body="会话一的私密档案")))
    run(session_b.write("user-123.md", person_file(body="会话二的私密档案")))
    run(people.write("user-123.md", person_file(body="跨群身份事实")))

    rt_a = main.RuntimeSession()
    rt_b = main.RuntimeSession()
    opening_a = run(plugin._index_block(rt_a, event_a))
    opening_b = run(plugin._index_block(rt_b, event_b))
    assert "会话一的私密档案" in opening_a
    assert "会话一的私密档案" not in opening_b
    assert "会话二的私密档案" in opening_b
    # people 是按平台共享的，两边的跨群身份事实都应在
    assert "跨群身份事实" in opening_a and "跨群身份事实" in opening_b


def test_tools_cannot_read_history_trash_or_forgotten_paths(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    session = plugin._store.session_scope(event.unified_msg_origin)
    session.root.mkdir(parents=True, exist_ok=True)
    hidden = {
        ".history/a.md": "历史版本正文",
        ".trash/x__a.md": "回收站正文",
        "forgotten/b.md": "遗忘正文",
    }
    for name, body in hidden.items():
        path = session.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(person_file(body=body), encoding="utf-8")

    for name, body in hidden.items():
        read = run(plugin.memory_read(event, "session", name))
        assert body not in read
    # 搜索只扫作用域根目录的 .md，不碰隐藏目录
    assert run(plugin.memory_search(event, "历史版本正文")) == "没有找到匹配「历史版本正文」的记忆。"


# ---------------------------------------------------------------- 3.7.2 人物记忆恒开
