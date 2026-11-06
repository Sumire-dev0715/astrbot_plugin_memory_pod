"""记忆管理页接口：三层作用域、留言板署名、整理与导出导入。"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import types
import time
from pathlib import Path

import pytest

from support import (
    PACKAGE,
    pkg_context_stats,
    pkg_expiry,
    pkg_history,
    pkg_memstore,
    pkg_origin,
    pkg_registry,
    pkg_tidy,
    thing,
)

ROOT = Path(__file__).resolve().parent.parent


class FakeRequest:
    def __init__(self):
        self.query: dict = {}
        self.body = None

    async def json(self, default=None):
        return default if self.body is None else self.body


REQUEST = FakeRequest()


def _load_web_api():
    if "astrbot.api.web" not in sys.modules or not hasattr(
        sys.modules["astrbot.api.web"], "request"
    ):
        astrbot = sys.modules.setdefault("astrbot", types.ModuleType("astrbot"))
        api = sys.modules.setdefault("astrbot.api", types.ModuleType("astrbot.api"))
        api.logger = logging.getLogger("memory_pod_test")
        web = types.ModuleType("astrbot.api.web")
        web.request = REQUEST
        sys.modules["astrbot.api.web"] = web
        astrbot.api = api
        api.web = web
    else:
        sys.modules["astrbot.api.web"].request = REQUEST
    sys.modules.pop(f"{PACKAGE}.web_api", None)
    return importlib.import_module(f"{PACKAGE}.web_api")


web_api = _load_web_api()


class FakeContext:
    def __init__(self):
        self.registered_web_apis = []

    def register_web_api(self, route, view, methods, desc):
        self.registered_web_apis.append((route, view, methods, desc))


class FakeRuntime:
    def __init__(self):
        self.stats = pkg_context_stats.ContextStats()


class FakePlugin:
    def __init__(self, base: Path):
        self.context = FakeContext()
        self._store = pkg_memstore.MemoryStore(base / "memories")
        self._registry = pkg_registry.SessionRegistry(base / "sessions.json")
        self._tidy = pkg_tidy.TidyBook(base / "tidy")
        self._sessions: dict = {}
        self._state_dir = base / "state"
        self._background_gate = asyncio.Semaphore(1)
        self.config = {"enable_memory": True}
        self.invalidated: list = []
        self.reply = ""
        self.prompts: list[str] = []
        self.astrbot_config: dict = {}

    def _bool(self, key, default):
        return self.config.get(key, default)

    def _astrbot_config(self, umo=None):
        return self.astrbot_config

    def notice_expired(self, expires, now=None):
        return pkg_expiry.is_expired(
            expires,
            time.time() if now is None else now,
            pkg_expiry.timezone(self.astrbot_config.get("timezone")),
        )

    def _forgetting_on(self):
        return self.config.get("enable_forgetting", False)

    def _forget_after_days(self):
        return self.config.get("forget_after_days", 90)

    async def context_status(self, umo=None):
        return {"window": 128000, "windowNote": "测试", "turnLimit": -1, "userIdentifier": False}

    def runtime_for_key(self, key):
        for umo, rt in self._sessions.items():
            if pkg_memstore.safe_key(umo) == key:
                return umo, rt
        return None

    def invalidate_scope(self, scope, key=None):
        self.invalidated.append((scope, key))

    def stats_path_for_key(self, key):
        return self._state_dir / "context" / f"{key}.json"

    async def load_stats_for_key(self, key):
        return pkg_context_stats.ContextStats()

    async def astrbot_summary(self, umo):
        return ""

    def platform_ids(self):
        return ["Bot-A"]

    async def call_consolidator(self, prompt):
        self.prompts.append(prompt)
        return self.reply, []


def setup(tmp_path):
    plugin = FakePlugin(tmp_path)
    dashboard = web_api.MemoryDashboard(plugin)
    return plugin, dashboard


def call(coro):
    return asyncio.run(coro)


def route(dashboard, name):
    """像注册到 AstrBot 的路由一样调用：把 ApiError 转成 status=error。"""
    return call(dashboard._wrap(getattr(dashboard, name))())


def body(data):
    REQUEST.body = data
    return data


def query(**params):
    REQUEST.query = params


def raw(result):
    return result.get("data", {})


def test_overview_lists_three_layers(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.board_scope().write("notice.md", thing("notice", body="告示")))
    call(plugin._store.notebook_scope("Bot-A").write("note.md", thing("note", body="小本本")))
    call(plugin._store.whisper_scope("session-a").write("secret.md", thing("secret", body="悄悄话")))
    data = raw(route(dashboard, "overview"))
    assert data["board"]["fileCount"] == 1
    assert [item["key"] for item in data["notebooks"]] == ["Bot-A"]
    assert [item["key"] for item in data["whispers"]] == ["session-a"]
    assert data["memoryEnabled"] is True


def test_board_publish_requires_signature_and_writes_it(tmp_path):
    plugin, dashboard = setup(tmp_path)
    body({"signature": "", "description": "新员工", "body": "11 月 11 日 teto 入职"})
    result = route(dashboard, "board_publish")
    assert result["status"] == "error"
    assert "署名" in result["message"]

    body({"signature": "管理员", "description": "新员工", "body": "11 月 11 日 teto 入职"})
    result = route(dashboard, "board_publish")
    assert result["status"] == "ok", result
    name = raw(result)["file"]
    content = call(plugin._store.board_scope().read(name))
    doc = pkg_memstore.parse_memory(content)
    assert doc.signature == "管理员"
    assert doc.body == "11 月 11 日 teto 入职"
    assert ("board", None) in plugin.invalidated


def test_board_file_save_rejects_missing_signature(tmp_path):
    plugin, dashboard = setup(tmp_path)
    body(
        {
            "scope": "board",
            "key": "",
            "file": "notice.md",
            "mode": "fields",
            "type": "reference",
            "description": "告示",
            "body": "正文",
            "signature": "",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "error"
    assert "署名" in result["message"]


def test_board_raw_save_cannot_bypass_signature(tmp_path):
    plugin, dashboard = setup(tmp_path)
    body(
        {
            "scope": "board",
            "key": "",
            "file": "notice.md",
            "mode": "raw",
            "signature": "管理员",
            "content": "---\nname: notice\ndescription: 告示\nmetadata:\n  type: reference\n---\n\n正文\n",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "ok", result
    content = call(plugin._store.board_scope().read("notice.md"))
    assert pkg_memstore.parse_memory(content).signature == "管理员"

    body(
        {
            "scope": "board",
            "key": "",
            "file": "no-meta.md",
            "mode": "raw",
            "signature": "管理员",
            "content": "没有 frontmatter 的正文",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "error"
    assert "signature" in result["message"]


def test_person_profile_can_be_edited_on_management_page(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("user-456.md", thing("user-456", body="旧", mem_type="user")))
    body(
        {
            "scope": "notebook",
            "key": "Bot-A",
            "file": "user-456.md",
            "oldFile": "user-456.md",
            "mode": "fields",
            "type": "user",
            "description": "阿杰",
            "body": "新身份",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "ok", result
    assert "新身份" in call(notebook.read("user-456.md"))


def test_whisper_scopes_are_addressable_by_key_only(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.whisper_scope("a").write("secret.md", thing("secret", body="A 的秘密")))
    call(plugin._store.whisper_scope("b").write("other.md", thing("other", body="B 的内容")))
    query(scope="whisper", key="a")
    data = raw(route(dashboard, "scope_detail"))
    assert [item["file"] for item in data["files"]] == ["secret.md"]
    query(scope="whisper", key="b")
    data = raw(route(dashboard, "scope_detail"))
    assert [item["file"] for item in data["files"]] == ["other.md"]


def test_consolidate_preview_only_sees_selected_scope(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.whisper_scope("a").write("secret.md", thing("secret", body="A 的秘密")))
    call(plugin._store.whisper_scope("b").write("other.md", thing("other", body="B 的内容")))
    plugin.reply = json.dumps([{"op": "delete", "file": "other.md", "reason": "过期"}])
    body({"scope": "whisper", "key": "a"})
    result = route(dashboard, "consolidate_preview")
    assert result["status"] == "ok", result
    prompt = plugin.prompts[-1]
    assert "secret.md" in prompt
    assert "other.md" not in prompt and "B 的内容" not in prompt
    # 方案里不能出现另一段对话的文件（delete 不存在的文件会被丢掉）
    assert raw(result)["operations"] == []


def test_consolidate_apply_cannot_touch_other_scope(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.whisper_scope("a").write("secret.md", thing("secret", body="A")))
    call(plugin._store.whisper_scope("b").write("other.md", thing("other", body="B")))
    base = plugin._store.whisper_scope("a").mtime("secret.md")
    body(
        {
            "scope": "whisper",
            "key": "a",
            "operations": [
                {"op": "delete", "file": "other.md", "reason": "越界"},
                {"op": "write", "file": "secret.md", "type": "reference",
                 "description": "新的", "content": "改写", "baseMtime": base},
            ],
        }
    )
    result = route(dashboard, "consolidate_apply")
    assert result["status"] == "ok", result
    assert "B" in call(plugin._store.whisper_scope("b").read("other.md"))
    assert "改写" in call(plugin._store.whisper_scope("a").read("secret.md"))


def test_board_consolidate_keeps_signature_through_preview_and_apply(tmp_path):
    plugin, dashboard = setup(tmp_path)
    board = plugin._store.board_scope()
    call(
        board.write(
            "notice.md",
            thing("notice", description="入职", body="旧告示", signature="原署名"),
        )
    )
    plugin.reply = json.dumps(
        [
            {
                "op": "write",
                "file": "notice.md",
                "type": "reference",
                "description": "入职",
                "content": "整理后的告示",
                "reason": "精简",
                "signature": "伪造的署名",
            }
        ]
    )
    body({"scope": "board", "key": ""})
    preview = raw(route(dashboard, "consolidate_preview"))
    operation = preview["operations"][0]
    # 预览能看到真实署名；模型给的署名被插件覆盖
    assert operation["signature"] == "原署名"

    body({"scope": "board", "key": "", "operations": preview["operations"]})
    assert route(dashboard, "consolidate_apply")["status"] == "ok"
    content = call(board.read("notice.md"))
    assert pkg_memstore.parse_memory(content).signature == "原署名"
    assert "伪造的署名" not in content


def test_export_and_import_keep_whisper_in_its_own_scope(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.whisper_scope("a").write("secret.md", thing("secret", body="A 的秘密")))
    body({"scopes": [{"scope": "whisper", "key": "a"}]})
    prepared = route(dashboard, "export_prepare")
    token = raw(prepared)["token"]
    query(token=token)
    bundle = route(dashboard, "export_file")
    assert bundle["scopes"][0]["key"] == "a"
    assert bundle["scopes"][0]["files"][0]["name"] == "secret.md"

    # 导入到另一段对话是管理员的显式操作；内容只会落在指定的 key
    body({"bundle": bundle, "policy": "newer"})
    preview = route(dashboard, "import_preview")
    assert preview["status"] == "ok"
    REQUEST.body = {"token": raw(preview)["token"], "policy": "newer"}
    applied = route(dashboard, "import_apply")
    assert applied["status"] == "ok", applied
    assert call(plugin._store.whisper_scope("a").read("secret.md"))
    assert call(plugin._store.whisper_scope("b").read("secret.md")) is None


def test_delete_and_trash_round_trip(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", body="正文")))
    body({"scope": "notebook", "key": "Bot-A", "file": "note.md"})
    assert route(dashboard, "file_delete")["status"] == "ok"
    query(scope="notebook", key="Bot-A")
    trash = raw(route(dashboard, "trash_entries"))
    assert trash["entries"][0]["file"] == "note.md"
    entry = trash["entries"][0]["entry"]
    body({"scope": "notebook", "key": "Bot-A", "entry": entry})
    assert route(dashboard, "trash_restore")["status"] == "ok"
    assert call(notebook.read("note.md"))


def test_registered_routes_have_no_autotidy_or_old_scopes(tmp_path):
    plugin, dashboard = setup(tmp_path)
    dashboard.register()
    routes = [item[0] for item in plugin.context.registered_web_apis]
    assert all("autotidy" not in route for route in routes)
    assert all("session/" not in route for route in routes)
    assert any(route.endswith("/board/publish") for route in routes)
    assert any(route.endswith("/whisper/context") for route in routes)


GROUP_NAME = "星河秘密基地-甲子园"


def test_file_detail_origin_includes_group_name_from_audit(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    now = time.time()
    record = pkg_origin.Origin(
        "group",
        "123",
        "小明",
        now,
        pkg_origin.session_hash("Bot-A:GroupMessage:987654321"),
    )
    call(
        store.write(
            "note.md",
            thing("note", description="合成记忆", body="合成正文"),
            source="123",
            actor="Bot-A",
            origin_record=record,
            audit=pkg_history.AuditContext(
                session="Bot-A:GroupMessage:987654321",
                group=GROUP_NAME,
                admin=False,
            ),
        )
    )
    query(scope="notebook", key="Bot-A", file="note.md")
    data = raw(route(dashboard, "file_detail"))
    assert data["origin"][0]["kind"] == "group"
    assert data["origin"][0]["name"] == "小明"
    assert data["origin"][0]["speaker"] == "123"
    assert data["origin"][0]["group"] == GROUP_NAME
    # 群名只在管理页从审计日志读出，记忆文件里没有
    content = call(store.read("note.md"))
    assert GROUP_NAME not in content
    # 没有 origin 的旧文件：来源列表为空
    call(store.write("old.md", thing("old", body="旧内容")))
    query(scope="notebook", key="Bot-A", file="old.md")
    assert raw(route(dashboard, "file_detail"))["origin"] == []


def test_management_page_can_write_feedback_without_admin_gate(tmp_path):
    plugin, dashboard = setup(tmp_path)
    body(
        {
            "scope": "notebook",
            "key": "Bot-A",
            "file": "rule.md",
            "mode": "fields",
            "type": "feedback",
            "description": "对机器人的要求",
            "body": "以后先问我",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "ok", result
    content = call(plugin._store.notebook_scope("Bot-A").read("rule.md"))
    doc = pkg_memstore.parse_memory(content)
    assert doc.type == "feedback"
    # 管理页写入不算来源：没有 origin，别的对话里不会带来源标注
    assert doc.origin == ""
    assert pkg_origin.note("notebook/rule.md", doc.origin, "other") == ""

    body(
        {
            "scope": "notebook",
            "key": "Bot-A",
            "file": "rule.md",
            "oldFile": "rule.md",
            "mode": "fields",
            "type": "feedback",
            "description": "对机器人的要求",
            "body": "改成以后先问我再动手",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "ok", result
    assert "再动手" in call(plugin._store.notebook_scope("Bot-A").read("rule.md"))


def test_file_detail_origin_matches_group_by_time(tmp_path):
    """同一个人在不同群写过时，每条来源按时间对上自己的群，而不是都取最新。"""
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    now = time.time()
    first = pkg_origin.Origin(
        "group",
        "123",
        "小明",
        now,
        pkg_origin.session_hash("Bot-A:GroupMessage:900"),
    )
    call(
        store.write(
            "note.md",
            thing("note", description="合成记忆", body="合成正文"),
            source="123",
            actor="Bot-A",
            origin_record=first,
            audit=pkg_history.AuditContext(
                session="Bot-A:GroupMessage:900", group="A 合成群"
            ),
        )
    )
    second = pkg_origin.Origin(
        "group",
        "123",
        "小明",
        now + 30,
        pkg_origin.session_hash("Bot-A:GroupMessage:901"),
    )
    call(
        store.write(
            "note.md",
            thing("note", description="合成记忆", body="合成正文"),
            source="123",
            actor="Bot-A",
            origin_record=second,
            audit=pkg_history.AuditContext(
                session="Bot-A:GroupMessage:901", group="B 合成群"
            ),
        )
    )
    query(scope="notebook", key="Bot-A", file="note.md")
    origins = raw(route(dashboard, "file_detail"))["origin"]
    assert [item["group"] for item in origins] == ["A 合成群", "B 合成群"]


def test_board_publish_and_detail_expose_expiry(tmp_path):
    plugin, dashboard = setup(tmp_path)
    body(
        {
            "signature": "管理员",
            "description": "新告示",
            "body": "正文",
            "expires": "2999-12-31",
        }
    )
    result = route(dashboard, "board_publish")
    assert result["status"] == "ok", result
    name = raw(result)["file"]
    doc = pkg_memstore.parse_memory(call(plugin._store.board_scope().read(name)))
    assert doc.expires == "2999-12-31"

    query(scope="board", key="")
    data = raw(route(dashboard, "scope_detail"))
    item = next(item for item in data["files"] if item["file"] == name)
    assert item["expires"] == "2999-12-31"
    assert item["expired"] is False

    query(scope="board", key="", file=name)
    detail = raw(route(dashboard, "file_detail"))
    assert detail["expires"] == "2999-12-31"
    assert detail["expired"] is False

    # 已经过了到期日的告示在列表里标成已过期
    body(
        {
            "signature": "管理员",
            "description": "旧告示",
            "body": "旧正文",
            "expires": "2000-01-01",
        }
    )
    old = raw(route(dashboard, "board_publish"))["file"]
    query(scope="board", key="")
    data = raw(route(dashboard, "scope_detail"))
    old_item = next(item for item in data["files"] if item["file"] == old)
    assert old_item["expired"] is True

    body({"signature": "管理员", "description": "坏日期", "body": "正文",
          "expires": "2999/12/31"})
    result = route(dashboard, "board_publish")
    assert result["status"] == "error"
    assert "YYYY-MM-DD" in result["message"]


def test_board_expiry_can_be_renewed_from_file_save(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.board_scope()
    call(
        store.write(
            "notice.md",
            thing(
                "notice",
                description="告示",
                body="正文",
                signature="管理员",
                expires="2000-01-01",
            ),
        )
    )
    before = len(plugin.invalidated)

    def save(expires_marker):
        payload = {
            "scope": "board",
            "key": "",
            "file": "notice.md",
            "oldFile": "notice.md",
            "mode": "fields",
            "type": "reference",
            "description": "告示",
            "body": "正文",
            "signature": "管理员",
        }
        payload.update(expires_marker)
        body(payload)
        return route(dashboard, "file_save")

    result = save({"expires": "2999-12-31"})
    assert result["status"] == "ok", result
    renewed = pkg_memstore.parse_memory(call(store.read("notice.md")))
    assert renewed.expires == "2999-12-31"
    assert renewed.signature == "管理员"
    assert len(plugin.invalidated) > before
    assert plugin.invalidated[-1][0] == "board"

    # 表单没带 expires 时沿用原值，不会因为旧页面保存把日期清掉
    result = save({})
    assert result["status"] == "ok", result
    assert pkg_memstore.parse_memory(call(store.read("notice.md"))).expires == "2999-12-31"

    result = save({"expires": "2999/12/31"})
    assert result["status"] == "error"
    assert "YYYY-MM-DD" in result["message"]
    assert pkg_memstore.parse_memory(call(store.read("notice.md"))).expires == "2999-12-31"

    body({"scope": "board", "key": "", "file": "notice.md"})
    result = route(dashboard, "file_delete")
    assert result["status"] == "ok", result
    assert plugin.invalidated[-1][0] == "board"


def test_non_board_files_never_report_expired(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(
        notebook.write(
            "note.md",
            thing(
                "note",
                description="笔记",
                body="正文",
                expires="2000-01-01",
            ),
        )
    )
    query(scope="notebook", key="Bot-A")
    data = raw(route(dashboard, "scope_detail"))
    item = next(item for item in data["files"] if item["file"] == "note.md")
    assert item["expired"] is False

    query(scope="notebook", key="Bot-A", file="note.md")
    detail = raw(route(dashboard, "file_detail"))
    assert detail["expired"] is False


def test_consolidate_ignores_expires_outside_board(tmp_path):
    plugin, dashboard = setup(tmp_path)
    body(
        {
            "scope": "notebook",
            "key": "Bot-A",
            "operations": [
                {
                    "op": "write",
                    "file": "new.md",
                    "type": "reference",
                    "description": "新笔记",
                    "content": "正文",
                    "expires": "2999-12-31",
                    "baseMtime": 0,
                }
            ],
        }
    )
    result = route(dashboard, "consolidate_apply")
    assert result["status"] == "ok", result
    content = call(plugin._store.notebook_scope("Bot-A").read("new.md"))
    assert "expires:" not in content


def test_board_save_tolerates_unchanged_malformed_expiry(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.board_scope()
    call(
        store.write(
            "notice.md",
            thing(
                "notice",
                description="告示",
                body="旧正文",
                signature="管理员",
                expires="明天",
            ),
        )
    )
    body(
        {
            "scope": "board",
            "key": "",
            "file": "notice.md",
            "oldFile": "notice.md",
            "mode": "fields",
            "type": "reference",
            "description": "告示",
            "body": "改过的正文",
            "signature": "管理员",
            "expires": "明天",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "ok", result
    doc = pkg_memstore.parse_memory(call(store.read("notice.md")))
    assert doc.body == "改过的正文"
    # 坏掉的旧日期在这次保存时被清掉，不再让表单永远存不了
    assert doc.expires == ""

    # 管理员改成另一个非法值仍然拒绝
    body(
        {
            "scope": "board",
            "key": "",
            "file": "notice.md",
            "oldFile": "notice.md",
            "mode": "fields",
            "type": "reference",
            "description": "告示",
            "body": "再改",
            "signature": "管理员",
            "expires": "后天",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "error"
    assert "YYYY-MM-DD" in result["message"]


def test_board_save_empty_expires_clears_date(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.board_scope()
    call(
        store.write(
            "notice.md",
            thing(
                "notice",
                description="告示",
                body="正文",
                signature="管理员",
                expires="2999-12-31",
            ),
        )
    )
    body(
        {
            "scope": "board",
            "key": "",
            "file": "notice.md",
            "oldFile": "notice.md",
            "mode": "fields",
            "type": "reference",
            "description": "告示",
            "body": "正文",
            "signature": "管理员",
            "expires": "",
        }
    )
    result = route(dashboard, "file_save")
    assert result["status"] == "ok", result
    assert pkg_memstore.parse_memory(call(store.read("notice.md"))).expires == ""
