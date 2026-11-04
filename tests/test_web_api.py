"""记忆管理页接口：用最小的 AstrBot 替身驱动 web_api，真实读写临时目录。"""

import asyncio
import importlib
import importlib.machinery
import importlib.util
import json
import os
import logging
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "mb_plugin_under_test"


class FakeRequest:
    def __init__(self):
        self.query: dict = {}
        self.body = None

    async def json(self, default=None):
        return default if self.body is None else self.body


REQUEST = FakeRequest()


def _load_web_api():
    # web_api 用相对导入（from .core import …），这里把仓库根挂成一个包
    if "astrbot.api.web" not in sys.modules:
        astrbot = types.ModuleType("astrbot")
        api = types.ModuleType("astrbot.api")
        api.logger = logging.getLogger("memory_beyond_test")
        web = types.ModuleType("astrbot.api.web")
        web.request = REQUEST
        astrbot.api = api
        api.web = web
        sys.modules.setdefault("astrbot", astrbot)
        sys.modules.setdefault("astrbot.api", api)
        sys.modules.setdefault("astrbot.api.web", web)
    if PACKAGE not in sys.modules:
        spec = importlib.machinery.ModuleSpec(PACKAGE, None, is_package=True)
        package = importlib.util.module_from_spec(spec)
        package.__path__ = [str(ROOT)]
        sys.modules[PACKAGE] = package
    return importlib.import_module(f"{PACKAGE}.web_api")


web_api = _load_web_api()
pkg_compress = importlib.import_module(f"{PACKAGE}.core.compress")
pkg_memstore = importlib.import_module(f"{PACKAGE}.core.memstore")
pkg_registry = importlib.import_module(f"{PACKAGE}.core.registry")
pkg_sharing = importlib.import_module(f"{PACKAGE}.core.sharing")
pkg_tidy = importlib.import_module(f"{PACKAGE}.core.tidy")
pkg_pending = importlib.import_module(f"{PACKAGE}.core.pending")

UMO = "aiocqhttp:GroupMessage:100200"
KEY = pkg_memstore.safe_key(UMO)


class FakeContext:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, route, view, methods, desc):
        self.registered_web_apis = [
            api
            for api in self.registered_web_apis
            if not (api[0] == route and api[2] == methods)
        ]
        self.registered_web_apis.append((route, view, methods, desc))


class FakeRuntime:
    def __init__(self, state):
        self.state = state


class FakePlugin:
    def __init__(self, base: Path):
        self.context = FakeContext()
        self._store = pkg_memstore.MemoryStore(base / "memories")
        self._registry = pkg_registry.SessionRegistry(base / "sessions.json")
        self._tidy = pkg_tidy.TidyBook(base / "tidy")
        self._pending = pkg_pending.PendingBook(base / "state" / "pending")
        self._sessions: dict = {}
        self._state_dir = base / "state"
        self.config = {
            "enable_memory": True,
            "enable_global_memory": False,
        }
        self.compression = {
            "enabled": True,
            "window": 128000,
            "windowNote": "测试窗口",
            "turnLimit": -1,
            "keepRecentTurns": 3,
            "turnLimitWarning": False,
            "userIdentifier": False,
        }
        self.invalidated: list = []
        self.prompts: list = []
        self.summarizer_reply = ""

    async def stop_pending_write(self, umo):
        return None

    async def stop_pending_write_key(self, key):
        return None

    def _global_on(self):
        return self.config["enable_global_memory"]

    async def compression_status(self, umo=None):
        return dict(self.compression)

    def _forgetting_on(self):
        return self.config.get("enable_forgetting", False)

    def _forget_after_days(self):
        return self.config.get("forget_after_days", 90)

    def _bool(self, key, default):
        return self.config.get(key, default)

    def tidy_mode(self):
        return pkg_tidy.normalize_mode(self.config.get("auto_consolidate"))

    def tidy_hours(self):
        return pkg_tidy.clamp_hours(self.config.get("auto_consolidate_hours", 24))

    async def save_tidy_settings(self, mode, hours):
        self.config["auto_consolidate"] = mode
        if hours is not None:
            self.config["auto_consolidate_hours"] = hours

    def platform_ids(self):
        return ["aiocqhttp", "qq_new"]

    def runtime_for_key(self, key):
        for umo, rt in self._sessions.items():
            if pkg_memstore.safe_key(umo) == key:
                return umo, rt
        return None

    def invalidate_for_key(self, key):
        self.invalidated.append(key)

    def state_path_for_key(self, key):
        return self._state_dir / f"{key}.json"

    async def load_state_file(self, path):
        if path.is_file():
            return pkg_compress.SessionState.from_dict(json.loads(path.read_text("utf-8")))
        return pkg_compress.SessionState()

    async def save_state_file(self, path, state):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state.to_dict()), "utf-8")

    async def call_consolidator(self, prompt):
        self.prompts.append(prompt)
        if not self.summarizer_reply:
            return "", ["provider-x: 超时"]
        return self.summarizer_reply, []


@pytest.fixture
def plugin(tmp_path):
    return FakePlugin(tmp_path)


@pytest.fixture
def dash(plugin):
    return web_api.MemoryDashboard(plugin)


def call(dash, route, query=None, body=None):
    handlers = {path: handler for path, _, handler, _ in dash._routes()}
    REQUEST.query = {k: str(v) for k, v in (query or {}).items()}
    REQUEST.body = body
    return asyncio.run(dash._wrap(handlers[route])())


def ok(result):
    assert result["status"] == "ok", result
    return result["data"]


def err(result):
    assert result["status"] == "error", result
    return result["message"]


def session_store(plugin):
    return plugin._store.session_scope(UMO)


def memory(name, description, body="正文", mem_type="user"):
    return pkg_memstore.compose_memory(name, description, mem_type, body)


# ---------------------------------------------------------------- 注册


def test_register_replaces_and_unregister_purges(plugin, dash):
    plugin.context.registered_web_apis.append(("/other/x", None, ["GET"], ""))
    dash.register()
    dash.register()  # 插件重载：不重复登记
    ours = [a for a in plugin.context.registered_web_apis if a[0].startswith(web_api.ROUTE_PREFIX)]
    assert len(ours) == len(dash._routes())
    assert {a[0] for a in ours} >= {web_api.ROUTE_PREFIX + "overview", web_api.ROUTE_PREFIX + "file/save"}
    dash.unregister()
    assert plugin.context.registered_web_apis == [("/other/x", None, ["GET"], "")]


def test_register_skips_on_old_astrbot(plugin, dash):
    plugin.context = types.SimpleNamespace()
    dash.register()  # 没有 register_web_api：静默跳过
    dash.unregister()


# ---------------------------------------------------------------- 查询


def test_overview_merges_memory_dirs_and_registry(plugin, dash):
    asyncio.run(session_store(plugin).write("user-1.md", memory("user-1", "阿杰")))
    plugin._registry.touch("tg_FriendMessage_42", "tg:FriendMessage:42", "小鱼")
    data = ok(call(dash, "overview"))
    assert data["memoryEnabled"] is True
    assert data["global"] == {
        "fileCount": 0,
        "updatedAt": 0.0,
        "enabled": False,
        "tidyPending": 0,
    }
    sessions = {s["key"]: s for s in data["sessions"]}
    assert sessions[KEY]["fileCount"] == 1
    assert sessions[KEY]["kind"] == "group"  # 没登记过的目录从目录名推断
    assert sessions[KEY]["target"] == "100200"
    assert sessions["tg_FriendMessage_42"]["name"] == "小鱼"
    assert sessions["tg_FriendMessage_42"]["fileCount"] == 0


def test_scope_detail_and_file(plugin, dash):
    asyncio.run(session_store(plugin).write("user-1.md", memory("user-1", "阿杰", "见 [[user-2]]")))
    data = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    assert [f["file"] for f in data["files"]] == ["user-1.md"]
    assert data["index"]["lines"] == 1 and data["index"]["missing"] == []
    assert data["index"]["maxLines"] == pkg_memstore.INDEX_MAX_LINES
    assert data["session"]["key"] == KEY

    detail = ok(call(dash, "file", {"scope": "session", "key": KEY, "file": "user-1"}))
    assert detail["file"] == "user-1.md"
    assert (detail["description"], detail["type"], detail["body"]) == ("阿杰", "user", "见 [[user-2]]")
    assert detail["links"] == ["user-2"]
    assert detail["mtime"] > 0
    assert "不存在" in err(call(dash, "file", {"scope": "session", "key": KEY, "file": "nope.md"}))

    g = ok(call(dash, "scope", {"scope": "global"}))
    assert g["files"] == [] and g["enabled"] is False and g["key"] is None


@pytest.mark.parametrize(
    "query, message",
    [
        ({"scope": "session", "key": "../escape"}, "会话标识不合法"),
        ({"scope": "session", "key": ""}, "会话标识不合法"),
        ({"scope": "elsewhere", "key": KEY}, "作用域只能是"),
    ],
)
def test_scope_rejects_bad_locators(dash, query, message):
    assert message in err(call(dash, "scope", query))


@pytest.mark.parametrize("name", ["MEMORY.md", "MEMORY", "../x.md", "a/b.md", ".hidden.md"])
def test_file_rejects_bad_names(dash, name):
    assert err(call(dash, "file", {"scope": "session", "key": KEY, "file": name}))


def test_search(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "开黑", "每周六晚上开黑")))
    data = ok(call(dash, "search", {"scope": "session", "key": KEY, "q": "周六 开黑"}))
    assert [r["file"] for r in data["results"]] == ["a.md"]
    assert data["results"][0]["excerpt"]
    assert ok(call(dash, "search", {"scope": "session", "key": KEY, "q": " "}))["results"] == []


# ---------------------------------------------------------------- 编辑


def save(dash, **fields):
    body = {"scope": "session", "key": KEY, "mode": "fields", "type": "user"}
    body.update(fields)
    return call(dash, "file/save", body=body)


def test_file_save_accepts_journal_type(plugin, dash):
    store = session_store(plugin)
    data = ok(
        save(
            dash,
            file="daily-2026-11-03.md",
            type="journal",
            description="今日日志",
            body="今天去开黑。",
        )
    )
    assert data["file"] == "daily-2026-11-03.md"
    doc = pkg_memstore.parse_memory(
        asyncio.run(store.read("daily-2026-11-03.md"))
    )
    assert doc.type == "journal"
    index = asyncio.run(store.read(pkg_memstore.INDEX_FILENAME))
    assert "- [daily-2026-11-03](daily-2026-11-03.md)" in index
    assert "日志共 1 篇" in index


def test_file_save_create_edit_rename(plugin, dash):
    store = session_store(plugin)
    data = ok(save(dash, file="user-1", description=" 阿杰\n 群主 ", body="\n正文\n"))
    assert data["file"] == "user-1.md"
    assert data["message"].startswith("已写入")
    assert plugin.invalidated == [KEY]
    doc = pkg_memstore.parse_memory(asyncio.run(store.read("user-1.md")))
    assert (doc.name, doc.description, doc.body) == ("user-1", "阿杰 群主", "正文")

    # 新建同名文件被拒
    assert "已存在" in err(save(dash, file="user-1.md", description="d", body="b"))

    # 编辑：带上打开时的写入时间
    base = store.mtime("user-1.md")
    ok(save(dash, file="user-1.md", oldFile="user-1.md", description="新描述", body="b", baseMtime=base))
    # 期间被改过（时间对不上）就拒绝
    assert "被修改过" in err(
        save(dash, file="user-1.md", oldFile="user-1.md", description="x", body="b", baseMtime=base - 100)
    )

    # 改名：先写新文件，再把旧名的历史迁到新名下、删旧文件；
    # 旧名不留下回收站条目
    base = store.mtime("user-1.md")
    renamed_from = asyncio.run(store.read("user-1.md"))
    ok(save(dash, file="user-2.md", oldFile="user-1.md", description="改名", body="b", baseMtime=base))
    assert store.list_files() == ["user-2.md"]
    assert [e for e in store.trash_entries() if e.file == "user-1.md"] == []
    assert store.history_versions("user-1.md") == []
    renamed_versions = store.history_versions("user-2.md")
    assert any(v.action == "rename" for v in renamed_versions)
    assert any(
        store.read_history("user-2.md", v.version) == renamed_from
        for v in renamed_versions
    )
    # 改名前的旧名历史（编辑时存的 write 版本）也跟着迁过来
    assert any(v.action == "write" for v in renamed_versions)
    report = store.index_report()
    assert report.missing == [] and report.dangling == []
    # 改名的目标已存在同样被拒
    asyncio.run(store.write("user-3.md", memory("user-3", "d")))
    assert "已存在" in err(save(dash, file="user-3.md", oldFile="user-2.md", description="d", body="b"))
    # 原文件已被删掉
    assert "已不存在" in err(save(dash, file="x.md", oldFile="gone.md", description="d", body="b"))
    # 改名后按新名字彻底删除，能连旧名迁过来的历史一起清干净
    assert call(dash, "file/purge", body={"scope": "session", "key": KEY, "file": "user-2.md"})["status"] == "ok"
    assert store.history_versions("user-2.md") == []
    history_root = store.root / ".history"
    assert not history_root.is_dir() or not any(history_root.iterdir())


def test_rename_clears_old_name_tidy_records(plugin, dash):
    """整理记录与待确认方案里的旧名正文副本不能跟着改名留在磁盘上。"""
    store = session_store(plugin)
    asyncio.run(store.write("old.md", memory("old", "甲")))
    sid = pkg_tidy.scope_id("session", KEY)
    plugin._tidy.add_run(
        sid,
        {
            "id": "run-1",
            "ops": [{"op": "delete", "file": "old.md", "before": "旧名正文副本"}],
        },
    )

    def leave_pending(meta):
        meta.pending = {
            "operations": [
                {"op": "delete", "file": "old.md", "content": "旧名正文副本"}
            ]
        }

    plugin._tidy.update_meta(sid, leave_pending)
    base = store.mtime("old.md")
    ok(save(dash, file="new.md", oldFile="old.md", description="改名", body="新", baseMtime=base))
    assert plugin._tidy.runs(sid) == []
    assert plugin._tidy.meta(sid).pending is None
    blob = "\n".join(
        path.read_text("utf-8")
        for path in plugin._tidy.base_dir.glob("*.json")
    )
    assert "旧名正文副本" not in blob


def test_rename_failure_rolls_back_new_file(plugin, dash, monkeypatch):
    """历史/回收站迁移失败时撤回刚写入的新文件，旧文件保持可重试。"""
    store = session_store(plugin)
    asyncio.run(store.write("old.md", memory("old", "甲", "旧版一")))
    asyncio.run(store.write("old.md", memory("old", "甲", "旧版二")))
    assert store.history_versions("old.md")
    base = store.mtime("old.md")

    def boom(*_args, **_kwargs):
        raise OSError("模拟迁移失败")

    monkeypatch.setattr(store._history, "move_versions", boom)
    result = save(
        dash,
        file="new.md",
        oldFile="old.md",
        description="改名",
        body="新",
        baseMtime=base,
    )
    assert result["status"] == "error" and "模拟迁移失败" in result["message"]
    assert store.exists("old.md") and not store.exists("new.md")
    # 旧名的历史没有被搬走或删掉
    assert store.history_versions("old.md")
    # 刚写入的新内容被撤回回收站，旧文件仍在册，可以按原名重试
    assert any(entry.file == "new.md" for entry in store.trash_entries())

    monkeypatch.undo()
    ok(save(dash, file="new.md", oldFile="old.md", description="改名", body="新", baseMtime=base))
    assert store.list_files() == ["new.md"]
    assert store.history_versions("old.md") == []
    assert asyncio.run(store.read("new.md"))


def test_file_save_raw_mode(plugin, dash):
    content = "---\nname: a\ndescription: 原文模式\nmetadata:\n  type: project\n---\n\n正文\n"
    ok(call(dash, "file/save", body={"scope": "session", "key": KEY, "file": "a.md", "mode": "raw", "content": content}))
    saved = asyncio.run(session_store(plugin).read("a.md"))
    # 原文照存，只在 metadata 下补了戳
    assert saved.startswith("---\nname: a\ndescription: 原文模式\nmetadata:\n  type: project\n  created: ")
    assert saved.endswith("\n---\n\n正文\n")
    assert "不能为空" in err(
        call(dash, "file/save", body={"scope": "session", "key": KEY, "file": "b.md", "mode": "raw", "content": "  "})
    )


def test_file_save_validates_fields(dash):
    assert "类型" in err(save(dash, file="a.md", type="secret", description="d", body="b"))
    assert "描述" in err(save(dash, file="a.md", description="  ", body="b"))
    assert "MEMORY.md" in err(save(dash, file="MEMORY.md", description="d", body="b"))


def test_global_scope_edits_invalidate_all(plugin, dash):
    body = {"scope": "global", "mode": "fields", "file": "style.md", "type": "feedback", "description": "简洁", "body": "b"}
    ok(call(dash, "file/save", body=body))
    assert plugin.invalidated == [None]
    assert plugin._store.global_scope().list_files() == ["style.md"]


def test_file_delete(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "d")))
    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))
    assert store.list_files() == []
    assert err(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))


def test_file_save_and_delete_keep_history_and_trash(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "甲", "第一版")))
    original = asyncio.run(store.read("a.md"))
    content = memory("a", "甲", "第二版")
    ok(
        call(
            dash,
            "file/save",
            body={
                "scope": "session",
                "key": KEY,
                "file": "a.md",
                "oldFile": "a.md",
                "mode": "raw",
                "content": content,
            },
        )
    )
    versions = store.history_versions("a.md")
    assert len(versions) == 1 and versions[0].actor == "webui"
    assert store.read_history("a.md", versions[0].version) == original
    updated = asyncio.run(store.read("a.md"))
    assert updated != original

    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))
    entries = store.trash_entries()
    assert len(entries) == 1 and entries[0].actor == "webui"
    assert store.read_trash(entries[0].entry) == updated


def test_session_purge_moves_scope_into_trash_scopes(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "d")))
    before = asyncio.run(store.read("a.md"))
    assert ok(call(dash, "session/purge", body={"key": KEY}))["removed"] == 1
    assert not store.root.exists()
    archived = list((plugin._store.base_dir / ".trash-scopes").iterdir())
    assert len(archived) == 1 and archived[0].name.startswith(f"session-{KEY}-")
    assert (archived[0] / "a.md").read_text("utf-8") == before


def test_index_save_and_rebuild(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "A")))
    data = ok(call(dash, "index/save", body={"scope": "session", "key": KEY, "content": "## 分组\n"}))
    assert data["message"].startswith("索引已保存")
    assert store.index_report().missing == ["a.md"]
    data = ok(call(dash, "index/rebuild", body={"scope": "session", "key": KEY}))
    assert data["count"] == 1
    report = store.index_report()
    assert report.missing == [] and report.lines == 2


# ---------------------------------------------------------------- 会话


def test_session_state_reads_disk_or_live(plugin, dash):
    state = pkg_compress.SessionState(cid="c", watermark=12, summary="旧摘要", last_parts={"history": 5})
    asyncio.run(plugin.save_state_file(plugin.state_path_for_key(KEY), state))
    data = ok(call(dash, "session/state", {"key": KEY}))
    assert data["live"] is False
    assert data["watermark"] == 12 and data["summary"] == "旧摘要"
    assert data["lastParts"]["history"] == 5
    assert data["now"] > 0

    plugin._sessions[UMO] = FakeRuntime(pkg_compress.SessionState(cid="c", watermark=30))
    data = ok(call(dash, "session/state", {"key": KEY}))
    assert data["live"] is True and data["watermark"] == 30


def test_session_reset(plugin, dash):
    assert "还没有压缩状态" in ok(call(dash, "session/reset", body={"key": KEY}))["message"]
    assert not plugin.state_path_for_key(KEY).exists()  # 不凭空创建状态文件

    path = plugin.state_path_for_key(KEY)
    state = pkg_compress.SessionState(cid="c", watermark=12, summary="s", fail_count=3, fail_until=9e12)
    asyncio.run(plugin.save_state_file(path, state))
    ok(call(dash, "session/reset", body={"key": KEY}))
    saved = json.loads(path.read_text("utf-8"))
    assert saved["watermark"] == 0 and saved["summary"] == ""
    assert saved["fail_count"] == 0
    assert plugin.invalidated == [KEY]


def test_session_remark(plugin, dash):
    data = ok(call(dash, "session/remark", body={"key": KEY, "remark": "  开黑群 " + "x" * 100}))
    assert data["remark"].startswith("开黑群")
    assert len(data["remark"]) <= web_api.MAX_REMARK
    assert plugin._registry.get(KEY).remark == data["remark"]


def test_session_purge_memories_then_all(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "d")))
    plugin._registry.touch(KEY, UMO, "开黑群")
    path = plugin.state_path_for_key(KEY)
    asyncio.run(plugin.save_state_file(path, pkg_compress.SessionState(cid="c")))
    plugin._sessions[UMO] = FakeRuntime(pkg_compress.SessionState())

    assert ok(call(dash, "session/purge", body={"key": KEY}))["removed"] == 1
    assert store.list_files() == []
    assert plugin._registry.get(KEY) is not None  # 只删记忆

    ok(call(dash, "session/purge", body={"key": KEY, "target": "all"}))
    assert plugin._registry.get(KEY) is None
    assert not path.exists()
    assert UMO not in plugin._sessions


# ---------------------------------------------------------------- 整理


def test_consolidate_preview_and_apply(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("user-1.md", memory("user-1", "阿杰")))
    asyncio.run(store.write("user-1-old.md", memory("user-1-old", "阿杰旧档")))
    plugin._registry.touch(KEY, UMO, "开黑群")
    plugin.summarizer_reply = "<operations>" + json.dumps(
        [
            {"op": "write", "file": "user-1.md", "type": "user", "description": "阿杰（合并）", "content": "合并后正文", "reason": "合并"},
            {"op": "delete", "file": "user-1-old.md", "reason": "已合并"},
            {"op": "delete", "file": "ghost.md"},
        ],
        ensure_ascii=False,
    ) + "</operations>"
    data = ok(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))
    assert [op["file"] for op in data["operations"]] == ["user-1.md", "user-1-old.md"]
    assert data["fileCount"] == 2 and data["skipped"] == []
    assert data["files"] == ["user-1-old.md", "user-1.md"]
    assert "「开黑群」" in plugin.prompts[0]

    result = ok(
        call(dash, "consolidate/apply", body={"scope": "session", "key": KEY, "operations": data["operations"]})
    )
    assert [r["ok"] for r in result["results"]] == [True, True]
    assert result["message"] == "已应用 2 / 2 项操作"
    assert store.list_files() == ["user-1.md"]
    doc = pkg_memstore.parse_memory(asyncio.run(store.read("user-1.md")))
    assert (doc.description, doc.body) == ("阿杰（合并）", "合并后正文")
    assert store.index_report().dangling == []


def test_manual_consolidate_people_scope_not_gated(plugin, dash):
    """管理页手动整理共享人物空间不受定期整理的 edit 门控限制。"""
    plugin._store.set_shared_people(
        pkg_sharing.Membership(
            {
                "aiocqhttp": pkg_sharing.Member(
                    "aiocqhttp", pkg_sharing.PERMISSION_READ
                )
            }
        )
    )
    store = plugin._store.people_scope("aiocqhttp")
    assert store.root.name == pkg_sharing.SHARED_KEY
    asyncio.run(store.write("user-1.md", memory("user-1", "阿杰")))
    plugin.summarizer_reply = "<operations>" + json.dumps(
        [
            {
                "op": "write",
                "file": "user-1.md",
                "type": "user",
                "description": "阿杰（整理）",
                "content": "管理页改后",
                "reason": "合并",
            }
        ],
        ensure_ascii=False,
    ) + "</operations>"
    body = {"scope": "people", "key": pkg_sharing.SHARED_KEY}
    data = ok(call(dash, "consolidate/preview", body=body))
    result = ok(
        call(
            dash,
            "consolidate/apply",
            body={**body, "operations": data["operations"]},
        )
    )
    assert result["message"] == "已应用 1 / 1 项操作"
    doc = pkg_memstore.parse_memory(asyncio.run(store.read("user-1.md")))
    assert (doc.description, doc.body) == ("阿杰（整理）", "管理页改后")


def test_consolidate_errors(plugin, dash):
    body = {"scope": "session", "key": KEY}
    assert "还没有记忆" in err(call(dash, "consolidate/preview", body=body))
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    assert "provider-x: 超时" in err(call(dash, "consolidate/preview", body=body))
    plugin.summarizer_reply = "我觉得挺好"
    assert "无法解析" in err(call(dash, "consolidate/preview", body=body))
    plugin.summarizer_reply = "<operations>[]</operations>"
    assert ok(call(dash, "consolidate/preview", body=body))["operations"] == []
    assert plugin._tidy.busy == set()  # 每次都释放了占用
    assert "没有可应用" in err(call(dash, "consolidate/apply", body={**body, "operations": "x"}))


def test_consolidate_busy_guard(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    plugin._tidy.claim(f"session-{KEY}")
    assert "正在整理" in err(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))


# ---------------------------------------------------------------- 兜底


def test_unexpected_errors_are_masked(plugin, dash):
    plugin._registry = None  # 让 overview 内部抛 AttributeError
    assert "服务器内部错误" in err(call(dash, "overview"))


def test_consolidate_apply_reports_skipped_and_stale(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("user-1.md", memory("user-1", "阿杰")))
    asyncio.run(store.write("user-1-old.md", memory("user-1-old", "阿杰旧档")))
    plugin.summarizer_reply = "<operations>" + json.dumps(
        [
            {"op": "write", "file": "user-1.md", "type": "user", "description": "合并", "content": "合并后正文", "reason": "合并"},
            {"op": "delete", "file": "user-1-old.md", "reason": "已合并"},
        ],
        ensure_ascii=False,
    ) + "</operations>"
    data = ok(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))
    assert all(op["baseMtime"] > 0 for op in data["operations"])

    # 预览之后模型又改了合并目标：写入要跳过，源文件也不能删；没通过校验的也要报出来
    path = store.root / "user-1.md"
    later = path.stat().st_mtime + 5
    os.utime(path, (later, later))
    payload = data["operations"] + [{"op": "delete", "file": "ghost.md"}]
    result = ok(call(dash, "consolidate/apply", body={"scope": "session", "key": KEY, "operations": payload}))
    by_file = {r["file"]: r for r in result["results"]}
    assert "未通过校验" in by_file["ghost.md"]["message"]
    assert "被改过" in by_file["user-1.md"]["message"]
    assert "跳过删除" in by_file["user-1-old.md"]["message"]
    assert result["message"] == "已应用 0 / 3 项操作"
    assert set(store.list_files()) == {"user-1.md", "user-1-old.md"}
    assert "合并后正文" not in asyncio.run(store.read("user-1.md"))


def test_file_name_guard_is_case_insensitive(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    msg = err(call(dash, "file", query={"scope": "session", "key": KEY, "file": "memory.md"}))
    assert "索引" in msg


def test_file_detail_and_listing_expose_stamps(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("s.md", memory("s", "d"), source="111"))
    detail = ok(call(dash, "file", {"scope": "session", "key": KEY, "file": "s.md"}))
    assert detail["source"] == "111" and detail["created"] and detail["updated"] == detail["created"]
    listing = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    item = next(f for f in listing["files"] if f["file"] == "s.md")
    assert item["source"] == "111" and item["created"] > 0 and item["updated"] > 0
    # 管理页改写：created 保留、source 继承
    ok(call(dash, "file/save", body={
        "scope": "session", "key": KEY, "file": "s.md", "oldFile": "s.md", "mode": "fields",
        "type": "user", "description": "d2", "body": "b2", "baseMtime": detail["mtime"],
    }))
    again = ok(call(dash, "file", {"scope": "session", "key": KEY, "file": "s.md"}))
    assert again["created"] == detail["created"] and again["source"] == "111"
    assert (again["description"], again["body"]) == ("d2", "b2")


def test_people_scope_routes(plugin, dash):
    assert ok(call(dash, "overview"))["people"] == {"platforms": []}
    people = plugin._store.people_scope("aiocqhttp")
    asyncio.run(people.write("user-1.md", memory("user-1", "阿杰")))
    # 人物记忆恒开：不需要任何开关，写过的目录立刻出现在页面
    data = ok(call(dash, "overview"))
    assert data["people"]["platforms"][0]["key"] == "aiocqhttp"
    assert data["people"]["platforms"][0]["fileCount"] == 1
    detail = ok(call(dash, "scope", {"scope": "people", "key": "aiocqhttp"}))
    assert detail["people"]["platform"] == "aiocqhttp"
    assert [f["file"] for f in detail["files"]] == ["user-1.md"]
    assert "不合法" in err(call(dash, "scope", {"scope": "people", "key": "../x"}))
    assert "people" in err(call(dash, "scope", {"scope": "nope", "key": ""}))
    base = {
        "scope": "people", "key": "aiocqhttp", "mode": "fields",
        "type": "user", "description": "d", "body": "b",
    }
    assert "user-<数字ID>" in err(call(dash, "file/save", body={**base, "file": "note.md"}))
    assert "只能是 user" in err(
        call(dash, "file/save", body={**base, "file": "user-2.md", "type": "project"})
    )
    ok(call(dash, "file/save", body={**base, "file": "user-2.md"}))
    assert set(people.list_files()) == {"user-1.md", "user-2.md"}
    assert ok(call(dash, "overview"))["people"]["platforms"][0]["fileCount"] == 2


def test_shared_people_overview_and_detail(plugin, dash):
    membership = pkg_sharing.parse_config(["Bot-A=edit", "Bot-B=append"])
    plugin._store.set_shared_people(membership)

    # 还没有文件时，共享目录也要出现在人物分组里，成员与权限一并带上
    data = ok(call(dash, "overview"))
    people = data["people"]
    assert people["shared"]["key"] == "_shared"
    assert people["shared"]["members"] == [
        {"platform": "Bot-A", "permission": "edit"},
        {"platform": "Bot-B", "permission": "append"},
    ]
    by_key = {p["key"]: p for p in people["platforms"]}
    assert by_key["_shared"]["shared"] is True
    assert by_key["_shared"]["active"] is True
    assert by_key["_shared"]["fileCount"] == 0
    assert by_key["_shared"]["members"] == people["shared"]["members"]

    # 共享目录里写一份；成员原来的平台目录里留一份旧数据
    asyncio.run(
        plugin._store.people_scope("Bot-A").write(
            "user-1.md", memory("user-1", "共享人物")
        )
    )
    asyncio.run(
        plugin._store.people_scope_by_key("Bot-A").write(
            "user-2.md", memory("user-2", "旧目录人物")
        )
    )
    data = ok(call(dash, "overview"))
    by_key = {p["key"]: p for p in data["people"]["platforms"]}
    assert by_key["_shared"]["fileCount"] == 1
    assert by_key["Bot-A"]["fileCount"] == 1
    # 成员已并入共享：原来的平台目录不再被插件读写；没有旧目录的成员不单列
    assert by_key["Bot-A"]["unused"] is True
    assert "Bot-B" not in by_key
    assert by_key["_shared"]["unused"] is False

    shared_detail = ok(call(dash, "scope", {"scope": "people", "key": "_shared"}))
    assert shared_detail["people"]["shared"] is True
    assert shared_detail["people"]["active"] is True
    assert shared_detail["people"]["members"] == data["people"]["shared"]["members"]
    assert shared_detail["people"]["platform"] == "_shared"
    assert [f["file"] for f in shared_detail["files"]] == ["user-1.md"]

    legacy_detail = ok(call(dash, "scope", {"scope": "people", "key": "Bot-A"}))
    assert legacy_detail["people"]["shared"] is False
    assert legacy_detail["people"]["unused"] is True
    assert legacy_detail["people"]["platform"] == "Bot-A"
    assert [f["file"] for f in legacy_detail["files"]] == ["user-2.md"]

    # 配置清空后共享标记消失，旧行为不变
    plugin._store.set_shared_people(pkg_sharing.Membership())
    data = ok(call(dash, "overview"))
    assert "shared" not in data["people"]
    by_key = {p["key"]: p for p in data["people"]["platforms"]}
    assert by_key["Bot-A"]["unused"] is False
    assert by_key["_shared"]["shared"] is True
    # 共享已关闭：列表里要显示未启用，而不是“多个机器人共用”
    assert by_key["_shared"]["active"] is False
    shared_detail = ok(call(dash, "scope", {"scope": "people", "key": "_shared"}))
    assert shared_detail["people"]["active"] is False
    assert shared_detail["people"]["members"] == []


# ---------------------------------------------------------------- 遗忘


def test_forgotten_routes(plugin, dash):
    plugin.config["enable_forgetting"] = True
    plugin.config["forget_after_days"] = 30
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "钩子A")))
    asyncio.run(store.write("b.md", memory("b", "钩子B")))
    store.note_used(["a.md"], now=123.0)
    q = {"scope": "session", "key": KEY}
    detail = ok(call(dash, "scope", q))
    assert detail["forgettingEnabled"] is True and detail["forgetAfterDays"] == 30
    assert detail["forgotten"] == []
    by_file = {f["file"]: f for f in detail["files"]}
    assert by_file["a.md"]["uses"] == 1 and by_file["a.md"]["lastUsed"] == 123.0
    assert by_file["b.md"]["uses"] == 0 and by_file["b.md"]["forgottenAt"] == 0
    assert ok(call(dash, "file", {**q, "file": "a.md"}))["lastUsed"] == 123.0

    ok(call(dash, "file/forget", body={**q, "file": "b.md"}))
    assert plugin.invalidated[-1] == KEY
    assert "不存在" in err(call(dash, "file", {**q, "file": "b.md"}))
    detail = ok(call(dash, "scope", q))
    assert [f["file"] for f in detail["files"]] == ["a.md"]
    assert [f["file"] for f in detail["forgotten"]] == ["b.md"]
    assert detail["forgotten"][0]["forgottenAt"] > 0
    assert "](b.md)" not in detail["index"]["content"]
    view = ok(call(dash, "forgotten/file", {**q, "file": "b.md"}))
    assert "钩子B" in view["content"] and view["forgottenAt"] > 0
    assert "没有已遗忘" in err(call(dash, "forgotten/file", {**q, "file": "a.md"}))

    ok(call(dash, "forgotten/restore", body={**q, "file": "b.md"}))
    detail = ok(call(dash, "scope", q))
    assert [f["file"] for f in detail["files"]] == ["a.md", "b.md"]
    assert detail["forgotten"] == [] and "](b.md)" in detail["index"]["content"]
    assert "没有已遗忘" in err(call(dash, "forgotten/restore", body={**q, "file": "b.md"}))

    ok(call(dash, "file/forget", body={**q, "file": "b.md"}))
    ok(call(dash, "forgotten/delete", body={**q, "file": "b.md"}))
    assert store.forgotten_files() == [] and store.list_files() == ["a.md"]
    assert "没有已遗忘" in err(call(dash, "forgotten/delete", body={**q, "file": "b.md"}))
    assert "索引" in err(call(dash, "file/forget", body={**q, "file": "MEMORY.md"}))
    # 关闭开关：页面照样能看、能恢复，只是标成未启用
    plugin.config["enable_forgetting"] = False
    assert ok(call(dash, "scope", q))["forgettingEnabled"] is False


# ---------------------------------------------------------------- 历史与回收站


def test_history_routes(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "甲", "第一版")))
    first = asyncio.run(store.read("a.md"))
    second = memory("a", "甲", "第二版")
    q = {"scope": "session", "key": KEY, "file": "a.md"}
    ok(
        call(
            dash,
            "file/save",
            body={**q, "oldFile": "a.md", "mode": "raw", "content": second},
        )
    )
    second_saved = asyncio.run(store.read("a.md"))

    data = ok(call(dash, "history/versions", q))
    assert data["file"] == "a.md"
    versions = data["versions"]
    assert len(versions) == 1
    version = versions[0]
    assert version["action"] == "write" and version["actor"] == "webui"
    assert version["ts"] > 0 and version["mtime"] > 0 and version["size"] > 0
    assert version["version"].endswith("-write")

    view = ok(call(dash, "history/file", {**q, "version": version["version"]}))
    assert view["file"] == "a.md" and view["content"] == first
    assert "没有这个历史版本" in err(
        call(dash, "history/file", {**q, "version": "20200101-000000-000-write"})
    )
    assert "没有这个历史版本" in err(
        call(dash, "history/file", {**q, "version": "../escape"})
    )
    assert "缺少历史版本" in err(call(dash, "history/file", q))
    assert "缺少历史版本" in err(
        call(dash, "history/restore", body={"scope": "session", "key": KEY, "file": "a.md"})
    )
    assert "索引" in err(call(dash, "history/versions", {**q, "file": "MEMORY.md"}))
    # file 参数同样不能带路径片段
    assert "文件名不合法" in err(call(dash, "history/versions", {**q, "file": "../x.md"}))
    assert "文件名不合法" in err(
        call(dash, "history/restore", body={**q, "file": "../x.md", "version": version["version"]})
    )
    assert "文件名不合法" in err(call(dash, "file/purge", body={**q, "file": "../x.md"}))

    result = call(dash, "history/restore", body={**q, "version": version["version"]})
    assert result["status"] == "ok" and "已恢复" in result["message"]
    assert asyncio.run(store.read("a.md")) == first
    assert plugin.invalidated[-1] == KEY
    assert store.index_report().dangling == []

    # 恢复前先把当时的在册内容存进历史，动作记 restore
    versions = ok(call(dash, "history/versions", q))["versions"]
    assert len(versions) == 2
    restore_version = next(item for item in versions if item["action"] == "restore")
    assert store.read_history("a.md", restore_version["version"]) == second_saved


def test_trash_routes(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "甲", "要删的内容")))
    old = asyncio.run(store.read("a.md"))
    q = {"scope": "session", "key": KEY}
    ok(call(dash, "file/delete", body={**q, "file": "a.md"}))
    assert ok(call(dash, "scope", q))["trashCount"] == 1

    entries = ok(call(dash, "trash", q))["entries"]
    assert len(entries) == 1
    item = entries[0]
    assert item["file"] == "a.md" and item["action"] == "delete"
    assert item["actor"] == "webui" and item["entry"].endswith("__a.md")
    assert item["ts"] > 0 and item["mtime"] > 0 and item["size"] > 0

    view = ok(call(dash, "trash/file", {**q, "entry": item["entry"]}))
    assert view["file"] == "a.md" and view["entry"] == item["entry"]
    assert view["content"] == old and view["action"] == "delete"
    assert "没有这个条目" in err(call(dash, "trash/file", {**q, "entry": "../escape"}))
    assert "缺少回收站条目" in err(call(dash, "trash/file", q))
    assert "缺少回收站条目" in err(call(dash, "trash/restore", body=q))
    assert "缺少回收站条目" in err(call(dash, "trash/delete", body=q))
    assert "没有这个条目" in err(call(dash, "trash/restore", body={**q, "entry": "../x"}))
    assert "没有这个条目" in err(call(dash, "trash/delete", body={**q, "entry": "../x"}))

    result = call(dash, "trash/restore", body={**q, "entry": item["entry"]})
    assert result["status"] == "ok" and "已从回收站恢复" in result["message"]
    assert asyncio.run(store.read("a.md")) == old
    assert store.trash_entries() == []
    assert ok(call(dash, "scope", q))["trashCount"] == 0
    assert plugin.invalidated[-1] == KEY
    assert store.index_report().dangling == []

    # 同名文件已存在时，恢复前先把它存进历史
    asyncio.run(store.write("a.md", memory("a", "甲", "后来写在册的版本")))
    later = asyncio.run(store.read("a.md"))
    ok(call(dash, "file/delete", body={**q, "file": "a.md"}))
    entry = ok(call(dash, "trash", q))["entries"][0]
    current = memory("a", "甲", "恢复前的当前版本")
    asyncio.run(store.write("a.md", current))
    ok(call(dash, "trash/restore", body={**q, "entry": entry["entry"]}))
    assert asyncio.run(store.read("a.md")) == later
    versions = store.history_versions("a.md")
    assert any(v.action == "restore" for v in versions)

    # 彻底删除回收站条目：只删这一条，历史版本仍在
    ok(call(dash, "file/delete", body={**q, "file": "a.md"}))
    entry = ok(call(dash, "trash", q))["entries"][0]
    history_before = len(store.history_versions("a.md"))
    assert history_before > 0
    result = call(dash, "trash/delete", body={**q, "entry": entry["entry"]})
    assert result["status"] == "ok" and "已彻底删除" in result["message"]
    assert store.trash_entries() == []
    assert len(store.history_versions("a.md")) == history_before
    assert "没有这个条目" in err(
        call(dash, "trash/delete", body={**q, "entry": entry["entry"]})
    )

    # 文件已不在册时，彻底删除接口仍能清掉它剩下的历史
    result = call(dash, "file/purge", body={**q, "file": "a.md"})
    assert result["status"] == "ok"
    assert store.history_versions("a.md") == []
    assert store.trash_entries() == []


def test_file_purge_removes_history_trash_and_forgotten(plugin, dash):
    store = session_store(plugin)
    # 同名以外的文件：历史与回收站都不能被顺带清掉
    asyncio.run(store.write("b.md", memory("b", "乙", "第一版")))
    asyncio.run(store.write("b.md", memory("b", "乙", "第二版")))
    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "b.md"}))
    b_history = len(store.history_versions("b.md"))
    b_trash = [item.entry for item in store.trash_entries() if item.file == "b.md"]
    assert b_history and b_trash

    asyncio.run(store.write("a.md", memory("a", "甲", "第一版")))
    asyncio.run(store.write("a.md", memory("a", "甲", "第二版")))
    ok(call(dash, "file/forget", body={"scope": "session", "key": KEY, "file": "a.md"}))
    asyncio.run(store.write("a.md", memory("a", "甲", "第三版")))
    asyncio.run(store.write("a.md", memory("a", "甲", "第四版")))
    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))
    assert store.history_versions("a.md") and store.trash_entries()

    plugin._pending.record_user(
        KEY, UMO, "待整理的对话", False, "1", 1_800_000_000.0, platform="aiocqhttp"
    )
    q = {"scope": "session", "key": KEY, "file": "a.md"}
    result = call(dash, "file/purge", body=q)
    assert result["status"] == "ok" and "彻底删除" in result["message"]
    assert ok(result)["file"] == "a.md"
    assert store.list_files() == [] and store.forgotten_files() == []
    assert store.history_versions("a.md") == []
    assert [item for item in store.trash_entries() if item.file == "a.md"] == []
    assert len(store.history_versions("b.md")) == b_history
    assert [item.entry for item in store.trash_entries() if item.file == "b.md"] == b_trash
    report = store.index_report()
    assert report.missing == [] and report.dangling == []
    assert plugin.invalidated[-1] == KEY
    assert plugin._pending.load(KEY).records == []

    assert "没有找到" in err(call(dash, "file/purge", body=q))
    assert "索引" in err(call(dash, "file/purge", body={**q, "file": "MEMORY.md"}))


def test_trash_entry_without_active_file_can_be_purged_by_name(plugin, dash):
    """回收站标签里不在册的条目可走 file/purge 彻底删除（含历史）。"""
    store = session_store(plugin)
    asyncio.run(store.write("gone.md", memory("gone", "甲", "第一版")))
    asyncio.run(store.write("gone.md", memory("gone", "甲", "第二版")))
    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "gone.md"}))

    # 页面据此判断要不要显示「彻底删除（含历史）」按钮
    detail = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    assert detail["files"] == []
    entries = ok(call(dash, "trash", {"scope": "session", "key": KEY}))["entries"]
    assert [entry["file"] for entry in entries] == ["gone.md"]
    assert store.history_versions("gone.md")

    result = call(
        dash,
        "file/purge",
        body={"scope": "session", "key": KEY, "file": "gone.md"},
    )
    assert result["status"] == "ok" and "彻底删除" in result["message"]
    assert store.trash_entries() == []
    assert store.history_versions("gone.md") == []
    assert ok(call(dash, "scope", {"scope": "session", "key": KEY}))["trashCount"] == 0


def test_scope_trash_count_does_not_read_audit(plugin, dash, monkeypatch):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "d")))
    ok(call(dash, "file/delete", body={"scope": "session", "key": KEY, "file": "a.md"}))

    def boom():
        raise AssertionError("作用域详情不应该为了数量去读完整审计")

    monkeypatch.setattr(store._history, "audit_entries", boom)
    data = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    assert data["trashCount"] == 1


def test_history_and_trash_are_scoped(plugin, dash):
    other_umo = "aiocqhttp:GroupMessage:300400"
    other_key = pkg_memstore.safe_key(other_umo)
    first = session_store(plugin)
    other = plugin._store.session_scope(other_umo)
    for store in (first, other):
        asyncio.run(store.write("a.md", memory("a", "甲", "第一版")))
        asyncio.run(store.write("a.md", memory("a", "甲", "第二版")))
        asyncio.run(store.delete("a.md", actor="webui"))

    q1 = {"scope": "session", "key": KEY, "file": "a.md"}
    q2 = {"scope": "session", "key": other_key, "file": "a.md"}
    assert len(ok(call(dash, "history/versions", q1))["versions"]) == 1
    assert len(ok(call(dash, "history/versions", q2))["versions"]) == 1
    assert len(ok(call(dash, "trash", {"scope": "session", "key": KEY}))["entries"]) == 1
    assert len(ok(call(dash, "trash", {"scope": "session", "key": other_key}))["entries"]) == 1

    assert call(dash, "file/purge", body=q1)["status"] == "ok"
    assert first.history_versions("a.md") == [] and first.trash_entries() == []
    assert other.history_versions("a.md") and other.trash_entries()
    assert len(ok(call(dash, "history/versions", q2))["versions"]) == 1
    assert len(ok(call(dash, "trash", {"scope": "session", "key": other_key}))["entries"]) == 1

    version = ok(call(dash, "history/versions", q2))["versions"][0]
    assert call(
        dash, "history/restore", body={**q2, "version": version["version"]}
    )["status"] == "ok"
    assert other.list_files() == ["a.md"]


# ---------------------------------------------------------------- 定期整理


MERGE_REPLY = "<operations>" + json.dumps(
    [
        {"op": "write", "file": "user-1.md", "type": "user", "description": "阿杰（合并）", "content": "合并后正文", "reason": "合并"},
        {"op": "delete", "file": "user-1-old.md", "reason": "已合并"},
    ],
    ensure_ascii=False,
) + "</operations>"


def test_autotidy_config_and_status(plugin, dash):
    data = ok(call(dash, "overview"))
    assert data["autoTidy"]["mode"] == "off" and data["autoTidy"]["asked"] is False
    assert "只能是" in err(call(dash, "autotidy/config", body={"mode": "sometimes"}))

    result = call(dash, "autotidy/config", body={"mode": "confirm", "hours": 0})
    assert ok(result)["mode"] == "confirm" and ok(result)["hours"] == 1
    assert "先问你" in result["message"]
    assert plugin.config["auto_consolidate"] == "confirm"
    assert plugin._tidy.settings()["asked"] is True

    # 暂不开启：方式仍是关闭，但不再问
    ok(call(dash, "autotidy/config", body={"mode": "off"}))
    assert ok(call(dash, "overview"))["autoTidy"] == {
        "mode": "off",
        "hours": 1,
        "asked": True,
        "minChanges": pkg_tidy.MIN_CHANGES,
    }


def test_pending_plan_confirm_and_undo(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("user-1.md", memory("user-1", "阿杰")))
    asyncio.run(store.write("user-1-old.md", memory("user-1-old", "阿杰旧档")))
    before = asyncio.run(store.read("user-1.md"))
    plugin.summarizer_reply = MERGE_REPLY
    data = ok(call(dash, "consolidate/preview", body={"scope": "session", "key": KEY}))
    sid = pkg_tidy.scope_id("session", KEY)

    def leave_pending(meta):
        meta.pending = {"createdAt": 1.0, "operations": data["operations"], "skipped": [], "files": data["files"]}

    plugin._tidy.update_meta(sid, leave_pending)
    overview = ok(call(dash, "overview"))
    assert {s["key"]: s for s in overview["sessions"]}[KEY]["tidyPending"] == 2
    status = ok(call(dash, "autotidy"))
    assert status["pendingTotal"] == 1 and status["scopes"][0]["key"] == KEY
    detail = ok(call(dash, "scope", {"scope": "session", "key": KEY}))
    assert detail["tidy"]["pendingCount"] == 2
    assert detail["tidy"]["plan"]["operations"] == data["operations"]

    body = {"scope": "session", "key": KEY, "operations": data["operations"], "fromPending": True}
    result = ok(call(dash, "consolidate/apply", body=body))
    assert result["runId"] and store.list_files() == ["user-1.md"]
    meta = plugin._tidy.meta(sid)
    assert meta.pending is None and meta.last_run > 0 and meta.outcome == "applied"

    runs = ok(call(dash, "tidy/runs", {"scope": "session", "key": KEY}))["runs"]
    assert [r["trigger"] for r in runs] == ["confirmed"]
    assert "before" not in json.dumps(runs)
    undone = call(dash, "tidy/undo", body={"scope": "session", "key": KEY, "id": runs[0]["id"]})
    assert ok(undone)["results"] and undone["message"] == "已撤销 2 / 2 项"
    assert set(store.list_files()) == {"user-1.md", "user-1-old.md"}
    assert asyncio.run(store.read("user-1.md")) == before
    assert "撤销过" in err(call(dash, "tidy/undo", body={"scope": "session", "key": KEY, "id": runs[0]["id"]}))
    assert "不存在" in err(call(dash, "tidy/undo", body={"scope": "session", "key": KEY, "id": "nope"}))


def test_autotidy_dismiss_and_purge_clears(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "d")))
    sid = pkg_tidy.scope_id("session", KEY)
    plugin._tidy.update_meta(sid, lambda meta: setattr(meta, "pending", {"operations": [{}]}))
    ok(call(dash, "autotidy/dismiss", body={"scope": "session", "key": KEY}))
    assert plugin._tidy.meta(sid).pending is None
    plugin._tidy.add_run(sid, {"id": "1", "ops": []})
    ok(call(dash, "session/purge", body={"key": KEY}))
    assert plugin._tidy.runs(sid) == [] and plugin._tidy.all_meta() == {}


# ---------------------------------------------------------------- 迁移与备份


def test_export_then_import_elsewhere(plugin, dash, tmp_path):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "甲")))
    plugin._registry.touch(KEY, UMO, "开黑群")
    assert "没有可导出" in err(call(dash, "export/prepare", body={"scopes": [{"scope": "global"}]}))
    prepared = ok(call(dash, "export/prepare", body={}))
    assert prepared["fileCount"] == 1 and prepared["filename"].startswith("memory-backup-")
    bundle = call(dash, "export/file", {"token": prepared["token"]})
    assert bundle["format"] == "memory-beyond-backup"
    assert "过期" in err(call(dash, "export/file", {"token": "bogus"}))

    other = FakePlugin(tmp_path / "other")
    other_dash = web_api.MemoryDashboard(other)
    assert "不是记忆备份" in err(call(other_dash, "import/preview", body={"bundle": {"x": 1}}))
    preview = ok(call(other_dash, "import/preview", body={"bundle": bundle}))
    item = preview["scopes"][0]
    assert item["name"] == "开黑群" and item["counts"]["added"] == 1
    # 改冲突策略只传令牌
    again = ok(call(other_dash, "import/preview", body={"token": preview["token"], "policy": "skip"}))
    assert again["token"] == preview["token"]
    assert "没有选中" in err(
        call(other_dash, "import/apply", body={"token": preview["token"], "selection": []})
    )
    result = call(
        other_dash,
        "import/apply",
        body={"token": preview["token"], "selection": [{"scope": "session", "key": KEY}]},
    )
    assert ok(result)["reports"][0]["counts"]["added"] == 1
    assert "写入 1 条" in result["message"]
    assert other._store.session_scope(UMO).list_files() == ["a.md"]
    assert other._registry.get(KEY).name == "开黑群"
    assert KEY in other.invalidated


def test_migrate_scope_to_new_session(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("a.md", memory("a", "甲")))
    asyncio.run(store.write("b.md", memory("b", "乙")))
    new_umo = "aiocqhttp:GroupMessage:300400"
    new_key = pkg_memstore.safe_key(new_umo)
    plugin._pending.record_user(
        KEY, UMO, "旧会话的待整理对话", False, "1", 1_800_000_000.0,
        platform="aiocqhttp",
    )
    body = {
        "from": {"scope": "session", "key": KEY},
        "to": {"umo": new_umo},
        "files": ["a.md"],
        "mode": "move",
    }
    assert "格式" in err(call(dash, "migrate/scope", body={**body, "to": {"umo": "abc"}}))
    preview = ok(call(dash, "migrate/scope", body={**body, "dryRun": True}))
    assert preview["counts"]["added"] == 1 and preview["to"]["key"] == new_key
    assert store.list_files() == ["a.md", "b.md"]
    result = call(dash, "migrate/scope", body=body)
    assert "移走 1 条" in result["message"]
    assert store.list_files() == ["b.md"]
    assert plugin._store.session_scope(new_umo).list_files() == ["a.md"]
    assert plugin._registry.get(new_key).umo == new_umo
    assert {KEY, new_key} <= set(plugin.invalidated)
    assert plugin._pending.load(KEY).records == []
    # 搬进还没有人物记忆的平台（人物记忆恒开）
    asyncio.run(store.write("user-9.md", memory("user-9", "小九")))
    people = {"scope": "people", "platform": "qq_new"}
    ok(call(dash, "migrate/scope", body={"from": {"scope": "session", "key": KEY}, "to": people, "files": ["user-9.md"]}))
    assert plugin._store.people_scope("qq_new").list_files() == ["user-9.md"]


def test_migrate_platform_and_info(plugin, dash):
    asyncio.run(session_store(plugin).write("a.md", memory("a", "甲")))
    plugin._registry.touch(KEY, UMO, "开黑群")
    info = ok(call(dash, "migrate/info"))
    assert info["platforms"] == [{"platform": "aiocqhttp", "sessions": 1, "people": False}]
    assert info["adapters"] == ["aiocqhttp", "qq_new"]
    # 共享人物目录不是平台，不能出现在平台改名迁移的选项里
    asyncio.run(
        plugin._store.people_scope_by_key("_shared").write(
            "user-1.md", memory("user-1", "共享人物")
        )
    )
    info = ok(call(dash, "migrate/info"))
    assert all(item["platform"] != "_shared" for item in info["platforms"])
    body = {"from": "aiocqhttp", "to": "qq_new"}
    preview = ok(call(dash, "migrate/platform", body={**body, "dryRun": True}))
    assert preview["totals"]["added"] == 1
    result = call(dash, "migrate/platform", body=body)
    assert "写入 1 条" in result["message"]
    new_key = pkg_memstore.safe_key("qq_new:GroupMessage:100200")
    assert plugin._store.session_scope_by_key(new_key).list_files() == ["a.md"]
    assert plugin._registry.get(KEY) is None
    assert "相同" in err(call(dash, "migrate/platform", body={"from": "x", "to": "x"}))


# ---------------------------------------------------------------- 审查回归


def test_migrate_to_shared_people_warns_and_invalidates_pinned(plugin, dash):
    store = session_store(plugin)
    asyncio.run(store.write("user-9.md", memory("user-9", "小九")))
    body = {
        "from": {"scope": "session", "key": KEY},
        "to": {"scope": "people", "key": pkg_sharing.SHARED_KEY},
        "files": ["user-9.md"],
        "mode": "move",
    }
    preview = ok(call(dash, "migrate/scope", body={**body, "dryRun": True}))
    assert preview["warnings"]
    assert "共享人物空间" in preview["warnings"][0]
    assert "拆分" in preview["warnings"][0]

    result = call(dash, "migrate/scope", body=body)
    assert result["status"] == "ok"
    assert "共享人物空间" in result["message"]
    # people 目录的迁移也要作废钉在私聊开头块的快照
    assert pkg_sharing.SHARED_KEY in plugin.invalidated
    assert plugin._store.people_scope_by_key(pkg_sharing.SHARED_KEY).exists("user-9.md")


def test_file_purge_clears_tidy_only_residual(plugin, dash):
    """正文只剩在整理记录 before 里时，彻底删除也要能清掉。"""
    sid = pkg_tidy.scope_id("session", KEY)
    plugin._tidy.add_run(
        sid,
        {
            "id": "run-1",
            "at": 1_700_000_000.0,
            "trigger": "auto",
            "ops": [
                {"op": "delete", "file": "ghost.md", "before": "整理前的残留正文"}
            ],
        },
    )
    assert plugin._tidy.runs(sid)

    result = call(
        dash,
        "file/purge",
        body={"scope": "session", "key": KEY, "file": "ghost.md"},
    )
    assert result["status"] == "ok"
    assert "整理记录" in result["message"]
    assert plugin._tidy.runs(sid) == []


# ---------------------------------------------------------------- 3.7 压缩跟随与归档删除


def test_overview_reports_compression_follow_status(plugin, dash):
    plugin.compression = {
        "enabled": False,
        "window": 64000,
        "windowNote": "跟随 AstrBot 的上下文窗口兜底值",
        "turnLimit": 5,
        "keepRecentTurns": 3,
        "turnLimitWarning": True,
        "userIdentifier": True,
    }
    data = ok(call(dash, "overview"))
    assert data["compression"] == plugin.compression


def test_archive_routes_are_gone(dash):
    paths = {path for path, _, _, _ in dash._routes()}
    assert "archives" not in paths and "archive" not in paths
    assert "session/state" in paths
