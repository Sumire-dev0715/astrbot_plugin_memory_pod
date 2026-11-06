"""管理页保留功能的接口：详情、搜索、历史、回收站、索引、上下文、迁移。"""

from __future__ import annotations

from support import pkg_context_stats, pkg_memstore, run, thing

from test_web_api import body, call, query, raw, route, setup


def test_file_detail_and_search(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", description="作息", body="早睡早起")))
    query(scope="notebook", key="Bot-A", file="note.md")
    data = raw(route(dashboard, "file_detail"))
    assert data["description"] == "作息"
    assert "早睡早起" in data["body"]

    query(scope="notebook", key="Bot-A", q="早睡")
    data = raw(route(dashboard, "search"))
    assert data["results"][0]["file"] == "note.md"


def test_forgotten_view_restore_and_delete(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", body="正文")))
    call(notebook.forget("note.md"))
    query(scope="notebook", key="Bot-A", file="note.md")
    assert route(dashboard, "forgotten_file")["status"] == "ok"
    body({"scope": "notebook", "key": "Bot-A", "file": "note.md"})
    assert route(dashboard, "forgotten_restore")["status"] == "ok"
    assert call(notebook.read("note.md"))

    body({"scope": "notebook", "key": "Bot-A", "file": "note.md"})
    call(notebook.forget("note.md"))
    assert route(dashboard, "forgotten_delete")["status"] == "ok"
    assert notebook.trash_entries()


def test_history_versions_file_and_restore(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", body="第一版")))
    call(notebook.write("note.md", thing("note", body="第二版")))
    query(scope="notebook", key="Bot-A", file="note.md")
    versions = raw(route(dashboard, "history_versions"))["versions"]
    assert versions
    version = versions[-1]["version"]
    query(scope="notebook", key="Bot-A", file="note.md", version=version)
    assert "第一版" in raw(route(dashboard, "history_file"))["content"]
    body({"scope": "notebook", "key": "Bot-A", "file": "note.md", "version": version})
    assert route(dashboard, "history_restore")["status"] == "ok"
    assert "第一版" in call(notebook.read("note.md"))


def test_trash_file_and_delete(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", body="正文")))
    call(notebook.delete("note.md"))
    query(scope="notebook", key="Bot-A")
    entries = raw(route(dashboard, "trash_entries"))["entries"]
    entry = entries[0]["entry"]
    query(scope="notebook", key="Bot-A", entry=entry)
    data = raw(route(dashboard, "trash_file"))
    assert "正文" in data["content"]
    body({"scope": "notebook", "key": "Bot-A", "entry": entry})
    assert route(dashboard, "trash_delete")["status"] == "ok"
    assert notebook.trash_entries() == []


def test_index_save_and_rebuild(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", description="甲", body="内容")))
    body({"scope": "notebook", "key": "Bot-A", "content": "- [note](note.md) — 手工\n"})
    assert route(dashboard, "index_save")["status"] == "ok"
    assert "手工" in call(notebook.read("MEMORY.md"))
    body({"scope": "notebook", "key": "Bot-A"})
    data = raw(route(dashboard, "index_rebuild"))
    assert data["count"] == 1
    assert "手工" not in call(notebook.read("MEMORY.md"))


def test_index_save_keeps_old_index_restorable_from_trash(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", description="甲", body="内容")))
    old = call(notebook.read("MEMORY.md"))
    body({"scope": "notebook", "key": "Bot-A", "content": "- [note](note.md) — 手工\n"})
    assert route(dashboard, "index_save")["status"] == "ok"

    query(scope="notebook", key="Bot-A")
    entries = raw(route(dashboard, "trash_entries"))["entries"]
    entry = next(item for item in entries if item["file"] == "MEMORY.md")
    body({"scope": "notebook", "key": "Bot-A", "entry": entry["entry"]})
    assert route(dashboard, "trash_restore")["status"] == "ok"
    assert call(notebook.read("MEMORY.md")) == old


def test_whisper_context_remark_and_purge(tmp_path):
    plugin, dashboard = setup(tmp_path)
    umo = "Bot-A:FriendMessage:1"
    key = pkg_memstore.safe_key(umo)
    plugin._sessions[umo] = type(
        "Runtime",
        (),
        {"stats": pkg_context_stats.ContextStats(parts={"system": 10}, estimate=10)},
    )()
    query(key=key)
    data = raw(route(dashboard, "whisper_context"))
    assert data["live"] is True
    assert data["parts"]["system"] == 10

    body({"key": key, "remark": "小明"})
    assert route(dashboard, "whisper_remark")["status"] == "ok"
    assert plugin._registry.get(key).remark == "小明"

    call(plugin._store.whisper_scope(key).write("secret.md", thing("secret", body="正文")))
    body({"key": key, "target": "all"})
    assert route(dashboard, "whisper_purge")["status"] == "ok"
    assert call(plugin._store.whisper_scope(key).read("secret.md")) is None


def test_migrate_info_and_platform_dry_run(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.notebook_scope("Bot-A").write("note.md", thing("note", body="内容")))
    data = raw(route(dashboard, "migrate_info"))
    assert any(item["platform"] == "Bot-A" for item in data["platforms"])

    body(
        {
            "from": "Bot-A",
            "to": "Bot-B",
            "mode": "move",
            "includeNotebook": True,
            "dryRun": True,
        }
    )
    result = route(dashboard, "migrate_platform")
    assert result["status"] == "ok"
    assert result["data"]["items"]
    # dry run 不落盘
    assert call(plugin._store.notebook_scope("Bot-A").read("note.md"))


def test_export_board_and_notebook_scopes(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(
        plugin._store.board_scope().write(
            "notice.md", thing("notice", body="告示", signature="管理员")
        )
    )
    call(plugin._store.notebook_scope("Bot-A").write("note.md", thing("note", body="内容")))
    body({"scopes": [{"scope": "board"}, {"scope": "notebook", "key": "Bot-A"}]})
    prepared = route(dashboard, "export_prepare")
    assert prepared["status"] == "ok", prepared
    token = raw(prepared)["token"]
    query(token=token)
    bundle = route(dashboard, "export_file")
    assert {item["scope"] for item in bundle["scopes"]} == {"board", "notebook"}


def test_file_forget_and_purge_routes(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", body="正文")))
    body({"scope": "notebook", "key": "Bot-A", "file": "note.md"})
    assert route(dashboard, "file_forget")["status"] == "ok"
    assert notebook.has_forgotten("note.md")
    body({"scope": "notebook", "key": "Bot-A", "file": "note.md"})
    assert route(dashboard, "file_purge")["status"] == "ok"
    assert run(notebook.read_forgotten("note.md")) is None


def test_rename_file_keeps_content(tmp_path):
    plugin, dashboard = setup(tmp_path)
    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("old.md", thing("old", description="旧", body="正文")))
    mtime = notebook.mtime("old.md")
    body(
        {
            "scope": "notebook",
            "key": "Bot-A",
            "file": "new.md",
            "oldFile": "old.md",
            "mode": "fields",
            "type": "reference",
            "description": "新",
            "body": "正文",
            "baseMtime": mtime,
        }
    )
    assert route(dashboard, "file_save")["status"] == "ok"
    assert run(notebook.read("old.md")) is None
    assert "正文" in run(notebook.read("new.md"))


def test_migrate_scope_dry_run(tmp_path):
    plugin, dashboard = setup(tmp_path)
    call(plugin._store.notebook_scope("Bot-A").write("note.md", thing("note", body="内容")))
    body(
        {
            "from": {"scope": "notebook", "key": "Bot-A"},
            "to": {"umo": "Bot-A:GroupMessage:9"},
            "dryRun": True,
        }
    )
    result = route(dashboard, "migrate_scope")
    assert result["status"] == "ok", result
    assert result["data"]["counts"]["added"] == 1
    assert run(plugin._store.whisper_scope(pkg_memstore.safe_key("Bot-A:GroupMessage:9")).read("note.md")) is None
