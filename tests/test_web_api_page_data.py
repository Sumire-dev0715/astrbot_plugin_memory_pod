"""3.11 新管理页用到的接口数据：署名记忆、列表来源、告示摘要、保留期。"""

from __future__ import annotations

import json
import time

from support import pkg_history, pkg_memstore, pkg_origin, thing

from test_web_api import body, call, query, raw, route, setup, web_api

GROUP_NAME = "合成测试群"
UMO = "Bot-A:GroupMessage:987654321"


def _publish(dashboard, signature, description="新告示", text="正文"):
    body({"signature": signature, "description": description, "body": text})
    return route(dashboard, "board_publish")


def test_overview_remembers_last_board_signature(tmp_path):
    plugin, dashboard = setup(tmp_path)
    assert raw(route(dashboard, "overview"))["board"]["lastSignature"] == ""

    assert _publish(dashboard, "  堇   管理员 ")["status"] == "ok"
    data = raw(route(dashboard, "overview"))
    # 与写进 frontmatter 的署名同一口径：空白折叠
    assert data["board"]["lastSignature"] == "堇 管理员"
    saved = json.loads((tmp_path / "state" / web_api.PREFS_FILENAME).read_text("utf-8"))
    assert saved == {"boardSignature": "堇 管理员"}

    # 失败的发布不改记下的署名
    body({"signature": "别人", "description": "缺正文", "body": ""})
    assert route(dashboard, "board_publish")["status"] == "error"
    assert raw(route(dashboard, "overview"))["board"]["lastSignature"] == "堇 管理员"

    # 插件重载（新的接口实例）后仍然记得
    fresh = web_api.MemoryDashboard(plugin)
    assert raw(route(fresh, "overview"))["board"]["lastSignature"] == "堇 管理员"

    # 再发一次就换成新的署名
    assert _publish(dashboard, "teto")["status"] == "ok"
    assert raw(route(dashboard, "overview"))["board"]["lastSignature"] == "teto"


def test_last_signature_tolerates_broken_or_unwritable_prefs(tmp_path):
    plugin, dashboard = setup(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    (state / web_api.PREFS_FILENAME).write_text("{不是 JSON", "utf-8")
    assert raw(route(dashboard, "overview"))["board"]["lastSignature"] == ""
    (state / web_api.PREFS_FILENAME).write_text(
        json.dumps({"boardSignature": ["不是字符串"]}), "utf-8"
    )
    assert raw(route(dashboard, "overview"))["board"]["lastSignature"] == ""
    (state / web_api.PREFS_FILENAME).write_text(
        json.dumps({"boardSignature": "长" * 300}), "utf-8"
    )
    signature = raw(route(dashboard, "overview"))["board"]["lastSignature"]
    assert signature == "长" * pkg_memstore.MAX_SIGNATURE_CHARS

    # state 目录写不进去时，告示照样发布成功，只是记不住署名
    other, other_dashboard = setup(tmp_path / "other")
    (tmp_path / "other").mkdir(exist_ok=True)
    (tmp_path / "other" / "state").write_text("占位文件，挡住目录", "utf-8")
    result = _publish(other_dashboard, "管理员")
    assert result["status"] == "ok", result
    assert call(other._store.board_scope().read(raw(result)["file"]))
    assert raw(route(other_dashboard, "overview"))["board"]["lastSignature"] == ""


def test_scope_rows_carry_origin_without_group_or_session(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    now = time.time()
    record = pkg_origin.Origin("group", "123", "小明", now, pkg_origin.session_hash(UMO))
    call(
        store.write(
            "note.md",
            thing("note", body="合成正文"),
            source="123",
            actor="Bot-A",
            origin_record=record,
            audit=pkg_history.AuditContext(session=UMO, group=GROUP_NAME),
        )
    )
    call(store.write("plain.md", thing("plain", body="管理页写的")))
    query(scope="notebook", key="Bot-A")
    data = raw(route(dashboard, "scope_detail"))
    rows = {item["file"]: item for item in data["files"]}
    origins = rows["note.md"]["origin"]
    assert len(origins) == 1
    assert origins[0]["kind"] == "group"
    assert origins[0]["speaker"] == "123"
    assert origins[0]["name"] == "小明"
    assert abs(origins[0]["ts"] - now) < 61
    # 列表里不带群名与会话哈希：群名只在文件详情里从审计读
    assert set(origins[0]) == {"kind", "speaker", "name", "ts"}
    assert GROUP_NAME not in json.dumps(data, ensure_ascii=False)
    assert pkg_origin.session_hash(UMO) not in json.dumps(data, ensure_ascii=False)
    # 没有 origin 的文件给空列表
    assert rows["plain.md"]["origin"] == []

    # 已遗忘的文件同样带来源
    call(store.forget("note.md"))
    data = raw(route(dashboard, "scope_detail"))
    forgotten = {item["file"]: item for item in data["forgotten"]}
    assert forgotten["note.md"]["origin"][0]["name"] == "小明"


def test_board_rows_carry_excerpt_only_on_board(tmp_path):
    plugin, dashboard = setup(tmp_path)
    long_text = "第一行告示\n\n" + "很长的内容 " * 80
    result = _publish(dashboard, "管理员", description="长告示", text=long_text)
    assert result["status"] == "ok", result
    assert _publish(dashboard, "管理员", description="短告示", text="  短\n正文  ")[
        "status"
    ] == "ok"
    query(scope="board", key="")
    rows = raw(route(dashboard, "scope_detail"))["files"]
    by_title = {item["description"]: item for item in rows}
    long_row = by_title["长告示"]
    assert long_row["excerpt"].startswith("第一行告示 很长的内容")
    assert long_row["excerpt"].endswith("…")
    assert len(long_row["excerpt"]) <= web_api.BOARD_EXCERPT_CHARS + 1
    assert "\n" not in long_row["excerpt"]
    # 摘要只取正文，不带 frontmatter
    assert "signature" not in long_row["excerpt"]
    assert by_title["短告示"]["excerpt"] == "短 正文"

    notebook = plugin._store.notebook_scope("Bot-A")
    call(notebook.write("note.md", thing("note", body="小本本正文")))
    query(scope="notebook", key="Bot-A")
    rows = raw(route(dashboard, "scope_detail"))["files"]
    assert rows and all("excerpt" not in item for item in rows)


def test_scope_detail_reports_retention_days(tmp_path):
    plugin, dashboard = setup(tmp_path)
    query(scope="board", key="")
    retention = raw(route(dashboard, "scope_detail"))["retention"]
    assert retention == {
        "historyDays": pkg_history.DEFAULT_KEEP_DAYS,
        "trashDays": pkg_history.DEFAULT_KEEP_DAYS,
        "minVersions": pkg_history.MIN_KEEP_VERSIONS,
    }

    plugin.config.update(history_keep_days=30, trash_keep_days=0)
    query(scope="notebook", key="Bot-A")
    retention = raw(route(dashboard, "scope_detail"))["retention"]
    assert retention["historyDays"] == 30
    # 0 表示永久保留，原样告诉页面
    assert retention["trashDays"] == 0

    # 与 ScopeStore.set_retention 同一口径：负数按 0，写坏的按默认
    plugin.config.update(history_keep_days=-5, trash_keep_days="abc")
    query(scope="whisper", key="session-a")
    retention = raw(route(dashboard, "scope_detail"))["retention"]
    assert retention["historyDays"] == 0
    assert retention["trashDays"] == pkg_history.DEFAULT_KEEP_DAYS
