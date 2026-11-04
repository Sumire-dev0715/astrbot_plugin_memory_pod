"""3.7.4 后台写入与 3.7.5 日志按段记。

全部使用临时目录里的合成数据；模型调用一律用确定性替身。
"""

import asyncio
import importlib
import os
import time
import types
from pathlib import Path

from test_sharing_tools import (
    PACKAGE,
    FakeEvent,
    make_plugin,
    person_file,
    run,
)

main = importlib.import_module(f"{PACKAGE}.main")
pkg_compress = importlib.import_module(f"{PACKAGE}.core.compress")
pkg_memstore = importlib.import_module(f"{PACKAGE}.core.memstore")
pkg_pending = importlib.import_module(f"{PACKAGE}.core.pending")
pkg_prompts = importlib.import_module(f"{PACKAGE}.core.prompts")
pkg_sharing = importlib.import_module(f"{PACKAGE}.core.sharing")
pkg_writer = importlib.import_module(f"{PACKAGE}.core.writer")

NOW = 1_800_000_000.0


def memory_md(name: str, body: str, mem_type: str = "user") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {name} 的描述\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        "---\n\n"
        f"{body}\n"
    )


def files_by_scope(stores: dict) -> dict[str, set[str]]:
    return {scope: set(store.list_files()) for scope, store in stores.items()}


def snapshots_for(stores: dict) -> dict[str, str]:
    result = {}
    for scope, store in stores.items():
        for name in store.list_files():
            content = run(store.read(name))
            if content is not None:
                result[f"{scope}/{name}"] = pkg_sharing.content_sha1(content)
    return result


def validate(
    plugin, stores, raw, *, sender="123", admin=False, permission=None,
    shared=False, senders=None, listed_only=None,
):
    return pkg_writer.validate_operations(
        raw,
        files_by_scope=files_by_scope(stores),
        permission=permission,
        people_shared=shared,
        admin=admin,
        global_on=plugin._global_on(),
        sender=sender,
        senders=senders,
        listed_only=listed_only,
    )


def apply(plugin, stores, ops, snapshots=None):
    return run(
        pkg_writer.apply_operations(
            stores,
            ops,
            snapshots=snapshots if snapshots is not None else snapshots_for(stores),
            now=NOW,
            actor="extract@Bot-A",
            source="123",
        )
    )


# ---------------------------------------------------------------- 每种操作


def test_each_operation_validates_and_writes(tmp_path):
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:1"
    session = plugin._store.session_scope(umo)
    run(session.write("note.md", memory_md("note", "## 作息\n\n早睡")))
    run(session.write("append.md", memory_md("append", "原内容")))
    run(session.write("old.md", memory_md("old", "过期内容")))
    stores = {"session": session}
    raw = [
        {"op": "create", "scope": "session", "file": "user-1.md",
         "type": "user", "description": "阿杰", "content": "工程师"},
        {"op": "append", "scope": "session", "file": "append.md", "content": "补充一句"},
        {"op": "replace_section", "scope": "session", "file": "note.md",
         "section": "## 作息", "content": "晚睡"},
        {"op": "replace", "scope": "session", "file": "note.md",
         "old": "晚睡", "new": "晚睡晚起"},
        {"op": "delete", "scope": "session", "file": "old.md", "reason": "过期"},
    ]
    ops, skipped = validate(plugin, stores, raw, sender="1")
    assert skipped == []
    assert [op.op for op in ops] == [
        "create", "append", "replace_section", "replace", "delete"
    ]
    result = apply(plugin, stores, ops)
    assert result.ok and result.applied == 5
    content = run(session.read("note.md"))
    assert "晚睡晚起" in content and "早睡" not in content
    assert "补充一句" in run(session.read("append.md"))
    # 后台写入同样留历史、进回收站
    assert session.history_versions("append.md")
    assert [item.file for item in session.trash_entries()] == ["old.md"]
    assert session.exists("user-1.md") and not session.exists("old.md")


def test_operation_validation_drops_bad_entries(tmp_path):
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:1"
    session = plugin._store.session_scope(umo)
    run(session.write("note.md", memory_md("note", "正文")))
    stores = {"session": session}
    raw = [
        {"op": "create", "scope": "session", "file": "MEMORY.md",
         "type": "user", "description": "d", "content": "c"},
        {"op": "create", "scope": "session", "file": "journal-2026-11-04.md",
         "type": "journal", "description": "d", "content": "c"},
        {"op": "create", "scope": "session", "file": "../evil.md",
         "type": "user", "description": "d", "content": "c"},
        {"op": "create", "scope": "session", "file": "bad.md",
         "type": "secret", "description": "d", "content": "c"},
        {"op": "create", "scope": "session", "file": "note.md",
         "type": "user", "description": "d", "content": "c"},
        {"op": "append", "scope": "session", "file": "ghost.md", "content": "c"},
        {"op": "replace", "scope": "session", "file": "note.md",
         "old": "正文", "new": "正文"},
        {"op": "unknown", "scope": "session", "file": "note.md"},
        "not a dict",
    ]
    ops, skipped = validate(plugin, stores, raw)
    assert ops == []
    assert len(skipped) >= 7


def test_operations_are_capped_per_batch(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    raw = [
        {"op": "create", "scope": "session", "file": f"m{i}.md",
         "type": "user", "description": "d", "content": "c"}
        for i in range(pkg_writer.MAX_OPERATIONS + 5)
    ]
    ops, skipped = validate(plugin, {"session": session}, raw)
    assert len(ops) == pkg_writer.MAX_OPERATIONS
    assert any("上限" in item for item in skipped)


def test_listed_only_file_cannot_be_deleted_or_rewritten(tmp_path):
    """只给了文件名、没附全文的文件不能被删除或改写，append 仍允许。"""
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    run(session.write("note.md", memory_md("note", "## 作息\n\n早睡")))
    stores = {"session": session}
    raw = [
        {"op": "replace_section", "scope": "session", "file": "note.md",
         "section": "## 作息", "content": "晚睡"},
        {"op": "replace", "scope": "session", "file": "note.md",
         "old": "早睡", "new": "晚睡"},
        {"op": "delete", "scope": "session", "file": "note.md", "reason": "过期"},
    ]
    ops, skipped = validate(
        plugin, stores, raw, listed_only={"session/note.md"}
    )
    assert ops == []
    assert len(skipped) == 3
    assert all("只给了文件名" in item for item in skipped)
    assert "早睡" in run(session.read("note.md"))

    ops, skipped = validate(
        plugin,
        stores,
        [{"op": "append", "scope": "session", "file": "note.md", "content": "补充"}],
        listed_only={"session/note.md"},
    )
    assert skipped == [] and [op.op for op in ops] == ["append"]


def test_listed_only_people_delete_still_follows_requester_rule(tmp_path):
    """管理员要求忘掉某人时可能只见文件名：删除按本人/管理员规则放行。"""
    plugin = make_plugin(tmp_path, [])
    people = plugin._store.people_scope("Bot-A")
    run(people.write("user-9.md", person_file("user-9")))
    stores = {"people": people}
    listed = {"people/user-9.md"}
    raw = [{"op": "delete", "scope": "people", "file": "user-9.md",
            "reason": "管理员要求"}]
    ops, skipped = validate(
        plugin, stores, raw, sender="1", senders={"1"}, admin=True,
        listed_only=listed,
    )
    assert skipped == [] and [op.op for op in ops] == ["delete"]

    ops, skipped = validate(
        plugin, stores, raw, sender="1", senders={"1"}, listed_only=listed
    )
    assert ops == []
    assert any("不是本人或管理员" in item for item in skipped)


def test_listed_only_long_file_is_not_told_to_replace_section(tmp_path, monkeypatch):
    """预算放不下的长文件只列名字：不能提示按小节替换，也不能被改写。"""
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    run(session.write("user-999.md", memory_md("user-999", "旧" * 3500)))
    monkeypatch.setattr(pkg_writer, "MAX_INPUT_CHARS", 200)
    context = run(
        pkg_writer.build_context(
            {"session": session},
            [],
            now=NOW,
            permission=None,
            admin=False,
            people_shared=False,
            global_on=False,
        )
    )
    assert "session/user-999.md" in context.listed_only
    listed = context.prompt.split("只列名称的文件", 1)[1].split(
        "相关文件的全文", 1
    )[0]
    assert "session/user-999.md" in listed
    assert "优先按小节替换" not in listed
    assert "不要覆盖它们" in context.prompt

    ops, skipped = validate(
        plugin,
        {"session": session},
        [{"op": "delete", "scope": "session", "file": "user-999.md",
          "reason": "过期"}],
        listed_only=context.listed_only,
    )
    assert ops == [] and "只给了文件名" in skipped[0]


# ---------------------------------------------------------------- 三种权限


def test_edit_append_read_permissions_for_people(tmp_path):
    for level in ("edit", "append", "read"):
        plugin = make_plugin(tmp_path / level, [f"Bot-A={level}"])
        run(plugin._store.people_scope("Bot-A").write(
            "user-1.md", person_file("user-1")
        ))
        stores, permission, shared = plugin._write_stores(
            "Bot-A:FriendMessage:1", "Bot-A", False
        )
        assert shared is True
        raw = [
            {"op": "create", "scope": "people", "file": "user-2.md",
             "type": "user", "description": "新人", "content": "工程师"},
            {"op": "append", "scope": "people", "file": "user-1.md",
             "content": "补充"},
            {"op": "replace_section", "scope": "people", "file": "user-1.md",
             "section": "## 身份", "content": "改"},
            {"op": "replace", "scope": "people", "file": "user-1.md",
             "old": "测试", "new": "改"},
            {"op": "delete", "scope": "people", "file": "user-1.md",
             "reason": "过期"},
        ]
        ops, skipped = validate(
            plugin, stores, raw, sender="1", permission=permission, shared=shared
        )
        names = [op.op for op in ops]
        if level == "edit":
            assert names == ["create", "append", "replace_section", "replace", "delete"]
        elif level == "append":
            assert names == ["create", "append"]
            assert all("append" in item or "people" in item for item in skipped)
        else:
            assert names == []
            assert len(skipped) == 5


def test_append_permission_can_actually_create_and_append(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=append"])
    run(plugin._store.people_scope("Bot-A").write(
        "user-1.md", person_file("user-1")
    ))
    stores, permission, shared = plugin._write_stores(
        "Bot-A:FriendMessage:1", "Bot-A", False
    )
    ops, _ = validate(
        plugin, stores,
        [
            {"op": "create", "scope": "people", "file": "user-2.md",
             "type": "user", "description": "新人", "content": "工程师"},
            {"op": "append", "scope": "people", "file": "user-1.md",
             "content": "补充"},
        ],
        permission=permission, shared=shared,
    )
    result = apply(plugin, stores, ops)
    assert result.applied == 2
    people = plugin._store.people_scope("Bot-A")
    assert people.exists("user-2.md")
    assert "补充" in run(people.read("user-1.md"))


# ---------------------------------------------------------------- global 与第三方


def test_only_admin_can_write_global(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["enable_global_memory"] = True
    umo = "Bot-A:FriendMessage:1"
    stores, _, _ = plugin._write_stores(umo, "Bot-A", False)
    assert "global" not in stores
    raw = [{"op": "create", "scope": "global", "file": "rules.md",
            "type": "feedback", "description": "准则", "content": "简短回答"}]
    ops, skipped = validate(plugin, stores, raw, admin=False)
    assert ops == [] and skipped

    stores, _, _ = plugin._write_stores(umo, "Bot-A", True)
    assert "global" in stores
    ops, skipped = validate(plugin, stores, raw, admin=True)
    assert len(ops) == 1
    assert apply(plugin, stores, ops).applied == 1
    assert plugin._global_scope().exists("rules.md")


def test_third_party_cannot_delete_or_edit_someone_elses_memory(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    run(session.write("user-2.md", memory_md("user-2", "别人的记忆")))
    stores = {"session": session}
    raw = [
        {"op": "delete", "scope": "session", "file": "user-2.md", "reason": "删"},
        {"op": "replace", "scope": "session", "file": "user-2.md",
         "old": "别人", "new": "改了"},
        {"op": "append", "scope": "session", "file": "user-2.md", "content": "转述"},
    ]
    ops, skipped = validate(plugin, stores, raw, sender="3", admin=False)
    # 第三方只能追加转述，不能删除或更正
    assert [op.op for op in ops] == ["append"]
    assert any("不是本人或管理员" in item for item in skipped)
    assert run(session.read("user-2.md")).count("别人的记忆") == 1

    # 本人可以删
    ops, _ = validate(plugin, stores, raw[:1], sender="2", admin=False)
    assert [op.op for op in ops] == ["delete"]
    # 管理员也可以删
    ops, _ = validate(plugin, stores, raw[:1], sender="3", admin=True)
    assert [op.op for op in ops] == ["delete"]


# ---------------------------------------------------------------- 快照校验


def test_snapshot_check_skips_file_changed_after_prompt(tmp_path):
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:1"
    session = plugin._store.session_scope(umo)
    run(session.write("note.md", memory_md("note", "原内容")))
    stores = {"session": session}
    snapshots = {
        "session/note.md": pkg_sharing.content_sha1(run(session.read("note.md")))
    }
    # 模型看过文件之后，别处又改了它
    run(session.write("note.md", memory_md("note", "别处改过")))
    ops, _ = validate(
        plugin, stores,
        [{"op": "append", "scope": "session", "file": "note.md", "content": "新增"}],
    )
    result = apply(plugin, stores, ops, snapshots=snapshots)
    assert result.stale is True and result.applied == 0
    assert "新增" not in run(session.read("note.md"))


# ---------------------------------------------------------------- 触发


def _request(prompt: str):
    return types.SimpleNamespace(prompt=prompt)


def _response(text: str = "回复"):
    return types.SimpleNamespace(completion_text=text)


def test_idle_trigger_uses_configurable_clock(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["extract_idle_minutes"] = 10
    umo = "Bot-A:FriendMessage:1"
    plugin._pending.record_user(
        pkg_memstore.safe_key(umo), umo, "你好", False, "123", NOW,
        platform="Bot-A",
    )
    scheduled = []
    plugin._schedule_write = lambda umo, reason: scheduled.append((umo, reason))
    plugin._pending_tick(NOW + 9 * 60)
    assert scheduled == []
    plugin._pending_tick(NOW + 10 * 60)
    assert scheduled == [(umo, "idle")]


def test_every_n_turns_trigger(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["auto_extract_interval"] = 3
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    scheduled = []
    plugin._schedule_write = lambda umo, reason: scheduled.append((umo, reason))
    for index in range(3):
        plugin._record_pending_user(event, _request(f"第 {index} 句"), NOW + index)
        plugin._record_pending_reply(event, _response(), NOW + index)
    assert scheduled == [(umo, "rounds")]


def test_memory_note_triggers_immediately_after_reply(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["auto_extract_interval"] = 0
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    plugin._record_pending_user(event, _request("请记住我的生日"), NOW)
    receipt = run(plugin.memory_note(event, "用户要求记住生日是 3 月 5 日"))
    assert "已记下" in receipt
    state = plugin._pending.load(pkg_memstore.safe_key(umo))
    assert state.note_pending is True and state.records[0].notes[0].sender == "123"

    scheduled = []
    plugin._schedule_write = lambda umo, reason: scheduled.append((umo, reason))
    plugin._record_pending_reply(event, _response(), NOW + 1)
    assert scheduled == [(umo, "note")]


# ---------------------------------------------------------------- 持久化与失败


def test_pending_records_survive_restart(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    plugin._record_pending_user(event, _request("重启前的对话"), NOW)
    plugin._record_pending_reply(event, _response("重启前的回复"), NOW + 1)

    # 重新构造插件（同一数据目录）后仍能读到；扫描能找到空闲会话
    plugin2 = make_plugin(tmp_path, [])
    state = plugin2._pending.load(pkg_memstore.safe_key(umo))
    assert state.count == 1
    assert state.records[0].user.endswith("重启前的对话")
    assert state.records[0].reply == "重启前的回复"
    scheduled = []
    plugin2._schedule_write = lambda umo, reason: scheduled.append((umo, reason))
    plugin2._pending_tick(NOW + 11 * 60)
    assert scheduled == [(umo, "idle")]


def test_three_consecutive_failures_give_up(tmp_path):
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:1"
    key = pkg_memstore.safe_key(umo)
    plugin._pending.record_user(key, umo, "对话", False, "123", NOW, platform="Bot-A")

    async def failing(umo, prompt, **kwargs):
        return None, ["模型不可用"]

    plugin._call_summarizer = failing
    for _ in range(3):
        run(plugin._write_pending(key, umo, "idle", now=NOW))
    state = plugin._pending.load(key)
    assert state.failures == pkg_pending.MAX_FAILURES
    assert state.records == []
    # 放弃后不再重试
    assert run(plugin._write_pending(key, umo, "idle", now=NOW + 60)) == ""


def test_background_queue_does_not_run_concurrently(tmp_path):
    async def scenario():
        plugin = make_plugin(tmp_path, [])
        plugin._pending.record_user("k1", "Bot-A:FriendMessage:1", "a", False, "1", NOW, platform="Bot-A")
        plugin._pending.record_user("k2", "Bot-B:FriendMessage:1", "b", False, "1", NOW, platform="Bot-B")
        active = 0
        peak = 0

        async def fake(key, umo, reason, now=None):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

        plugin._write_pending = fake
        plugin._schedule_write("Bot-A:FriendMessage:1", "idle")
        plugin._schedule_write("Bot-B:FriendMessage:1", "idle")
        await asyncio.sleep(0.1)
        return peak

    assert run(scenario()) == 1


# ---------------------------------------------------------------- 日志分段


def journal_file(name: str, body: str) -> str:
    return memory_md(name, body, "journal")


def test_journal_appends_until_3000_chars(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    body = "## 2026-11-01\n\n" + "旧" * 3200  # 超过 3000 字
    path = session.root / "journal-2026-11-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(journal_file("journal-2026-11-01", body), "utf-8")
    os.utime(path, (NOW, NOW))

    name, new = run(pkg_writer.write_journal(
        session, "今天聊了新话题", now=NOW + 3600, actor="extract@Bot-A", source="123"
    ))
    assert new is True
    assert name.startswith("journal-")
    assert name != "journal-2026-11-01.md"
    content = run(session.read(name))
    assert "## " + time.strftime("%Y-%m-%d", time.localtime(NOW + 3600)) in content


def test_journal_reuses_recent_short_file_and_adds_daily_heading(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    path = session.root / "daily-2026-11-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(journal_file("daily-2026-11-01", "## 2026-11-01\n\n旧流水"), "utf-8")
    os.utime(path, (NOW, NOW))

    name, new = run(pkg_writer.write_journal(
        session, "新的流水", now=NOW + 3600, actor="extract@Bot-A", source="123"
    ))
    assert (name, new) == ("daily-2026-11-01.md", False)
    content = run(session.read(name))
    today = time.strftime("%Y-%m-%d", time.localtime(NOW + 3600))
    assert f"## {today}" in content and content.count(f"## {today}") == 1
    assert "新的流水" in content


def test_journal_starts_new_file_after_seven_days(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    path = session.root / "journal-2026-11-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(journal_file("journal-2026-11-01", "## 2026-11-01\n\n旧流水"), "utf-8")
    old = NOW - 8 * 86400
    os.utime(path, (old, old))

    name, new = run(pkg_writer.write_journal(
        session, "八天后的流水", now=NOW, actor="extract@Bot-A", source="123"
    ))
    assert new is True and name == "journal-" + time.strftime(
        "%Y-%m-%d", time.localtime(NOW)
    ) + ".md"
    assert "八天后的流水" in run(session.read(name))


def test_journal_exactly_at_boundaries_reuses_file(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    path = session.root / "journal-2026-11-01.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    prefix = journal_file("journal-2026-11-01", "## 2026-11-01\n\n")
    content = prefix + "x" * (3000 - len(prefix))
    path.write_text(content, "utf-8")
    # 正好 7 天、正好 3000 字（含 frontmatter）：续写同一篇
    old = NOW - 7 * 86400
    os.utime(path, (old, old))
    assert len(content) == 3000
    name, new = run(pkg_writer.write_journal(
        session, "边界流水", now=NOW, actor="extract@Bot-A", source="123"
    ))
    assert (name, new) == ("journal-2026-11-01.md", False)


# ---------------------------------------------------------------- 记录不可见


def test_pending_records_are_invisible_to_memory_channels(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    marker = "只有待整理记录里才有的标记词"
    plugin._record_pending_user(event, _request(marker), NOW)
    plugin._record_pending_reply(event, _response("回复"), NOW + 1)

    session = plugin._store.session_scope(umo)
    assert session.list_files() == []
    assert run(session.load_index()).text.strip() == ""
    assert marker not in (run(session.load_index()).text)
    read = run(plugin.memory_read(event, "session", "../state/pending/x.md"))
    assert "不合法" in read or "不存在" in read
    assert "没有找到" in run(plugin.memory_search(event, marker))
    rt = main.RuntimeSession(state=pkg_compress.SessionState())
    assert run(plugin._index_block(rt, event)) is None
    assert run(plugin._recall_message(event, umo, marker, rt)) is None
    # 记录确实在 state/pending 下
    state = plugin._pending.load(pkg_memstore.safe_key(umo))
    assert state.count == 1


# ---------------------------------------------------------------- 旧摘要


def test_old_summary_block_in_history_is_kept(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin._resolve_window_for = lambda umo: _async_value((1_000_000, "测试窗口"))
    plugin._index_block = lambda *args, **kwargs: _async_value(None)
    plugin._recall_message = lambda *args, **kwargs: _async_value(None)
    plugin._maybe_forget = lambda *args, **kwargs: None
    plugin._maybe_tidy = lambda *args, **kwargs: None
    event = FakeEvent("Bot-A")
    old_summary = {
        "role": "user",
        "content": pkg_prompts.SUMMARY_BLOCK_OPEN
        + "\n旧格式摘要正文\n"
        + pkg_prompts.SUMMARY_BLOCK_CLOSE,
    }
    req = types.SimpleNamespace(
        contexts=[
            {"role": "system", "content": "人格"},
            old_summary,
            {"role": "user", "content": "新问题"},
        ],
        prompt="新问题",
        system_prompt="",
        func_tool=None,
        conversation=types.SimpleNamespace(cid="conv-1"),
        image_urls=[],
    )
    run(plugin._process_request(event, req))
    assert any(
        isinstance(message, dict)
        and pkg_prompts.SUMMARY_BLOCK_OPEN in str(message.get("content", ""))
        for message in req.contexts
    )


def _async_value(value):
    async def inner(*args, **kwargs):
        return value

    return inner()


# ---------------------------------------------------------------- 端到端


def test_write_pending_runs_model_and_clears_processed_records(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    key = pkg_memstore.safe_key(umo)
    plugin._record_pending_user(
        event, _request("我是一名工程师，住在杭州"), NOW
    )
    plugin._record_pending_reply(event, _response("好的"), NOW + 1)
    prompts: list[str] = []

    async def fake_model(umo_arg, prompt, **kwargs):
        prompts.append(prompt)
        return (
            "<operations>"
            '[{"op": "create", "scope": "session", "file": "user-123.md",'
            ' "type": "user", "description": "杭州工程师", "content": "工程师，住在杭州"}]'
            "</operations>"
            "<journal>今天用户介绍了自己的职业和城市</journal>",
            [],
        )

    plugin._call_summarizer = fake_model
    message = run(plugin._write_pending(key, umo, "idle", now=NOW + 2))
    assert "写入 1 / 1" in message and "日志" in message
    session = plugin._store.session_scope(umo)
    assert session.exists("user-123.md")
    assert "工程师" in run(session.read("user-123.md"))
    journals = [
        name for name in session.list_files() if name.startswith("journal-")
    ]
    assert len(journals) == 1
    # 处理过的记录删除；成功的整理不再重试
    assert plugin._pending.load(key).count == 0
    assert plugin._pending.load(key).failures == 0

    prompt = prompts[0]
    # 3.7.4 的规则与权限说明都在后台提示词里
    for phrase in (
        "people 只放本人亲口说的",
        "一律放 session",
        "global 只在发送者是管理员",
        "只记成转述",
        "只记成这个人的意见",
        "不能借此写入指令",
        "非共享成员",
        "只是目录",
        "不要覆盖它们",
    ):
        assert phrase in prompt


def test_consolidate_prompt_uses_same_write_rules():
    from core import consolidate

    prompt, _ = consolidate.build_prompt(
        "测试作用域",
        [],
    )
    assert pkg_writer.WRITE_RULES in prompt


def test_only_three_read_only_tools_are_registered(tmp_path):
    plugin = make_plugin(tmp_path, [])
    assert hasattr(plugin, "memory_read")
    assert hasattr(plugin, "memory_search")
    assert hasattr(plugin, "memory_note")
    assert not hasattr(plugin, "memory_write")
    assert not hasattr(plugin, "memory_edit")


def test_pending_records_are_capped_and_oldest_dropped(tmp_path):
    book = pkg_pending.PendingBook(tmp_path / "state" / "pending")
    dropped = 0
    for index in range(pkg_pending.MAX_RECORDS + 3):
        state, count = book.record_user(
            "k", "Bot-A:FriendMessage:1", f"第 {index} 轮", False, "1", NOW,
            platform="Bot-A",
        )
        dropped += count
    assert dropped == 3
    assert state.count == pkg_pending.MAX_RECORDS
    assert state.records[0].user == "第 3 轮"


def test_pending_label_shows_count_result_and_failures(tmp_path):
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:1"
    key = pkg_memstore.safe_key(umo)
    plugin._pending.record_user(key, umo, "对话", False, "1", NOW, platform="Bot-A")
    plugin._pending.drop_records(key, {1}, NOW + 1, "写入 1 / 1 条操作")
    label = plugin._pending_label(umo)
    assert "待整理 0 轮" in label and "上次成功" in label
    assert "写入 1 / 1 条操作" in label

    plugin._pending.record_user(key, umo, "又一段", False, "1", NOW + 2, platform="Bot-A")
    for _ in range(pkg_pending.MAX_FAILURES):
        plugin._pending.record_failure(key, NOW + 3, "模型不可用")
    label = plugin._pending_label(umo)
    assert "连续失败 3 次" in label


def test_background_prompt_contains_link_and_long_file_hints(tmp_path):
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:1"
    session = plugin._store.session_scope(umo)
    run(session.write("long.md", memory_md("long", "x" * 7000)))
    run(session.write("link.md", memory_md("link", "参见 [[missing]]")))

    async def build():
        return await pkg_writer.build_context(
            {"session": session},
            [pkg_pending.Round(seq=1, at=NOW, user="link long", reply="好",
                                sender="1")],
            now=NOW,
            permission=None,
            admin=False,
            people_shared=False,
            global_on=False,
        )

    context = run(build())
    assert "悬空链接" in context.prompt and "missing.md" in context.prompt
    assert "已经很长，优先按小节替换" in context.prompt


def test_web_state_and_page_show_pending_status():
    pkg_web = importlib.import_module(f"{PACKAGE}.web_api")
    pkg_compress_mod = importlib.import_module(f"{PACKAGE}.core.compress")

    state = pkg_pending.PendingState(
        umo="Bot-A:FriendMessage:1",
        records=[pkg_pending.Round(seq=1, at=NOW, user="u", reply="r")],
        failures=1,
        last_attempt=NOW,
        last_result="模型不可用",
        last_success=NOW - 10,
    )
    data = pkg_web._state_dict(pkg_compress_mod.SessionState(), state)
    assert data["pendingCount"] == 1
    assert data["pendingFailures"] == 1
    assert data["lastWriteAt"] == NOW - 10
    assert data["lastWriteResult"] == "模型不可用"

    page = (
        Path(__file__).resolve().parent.parent / "pages" / "memory" / "app.js"
    ).read_text("utf-8")
    for key in ("pendingRounds", "pendingFailures", "fileLong", "fileDangling"):
        assert key in page


def test_management_file_list_flags_long_and_dangling(tmp_path):
    pkg_web = importlib.import_module(f"{PACKAGE}.web_api")
    store = pkg_memstore.ScopeStore(tmp_path)
    run(store.write("long.md", memory_md("long", "x" * (9 * 1024))))
    run(store.write("link.md", memory_md("link", "参见 [[missing]]")))
    flags = pkg_web._file_flags(store, store.headers())
    assert flags["long.md"]["long"]
    assert flags["link.md"]["dangling"] == ["missing.md"]


# ---------------------------------------------------------------- 审查回归


def test_mixed_batch_cannot_use_last_turn_as_admin(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["enable_global_memory"] = True
    umo = "Bot-A:FriendMessage:1"
    key = pkg_memstore.safe_key(umo)
    book = plugin._pending
    book.record_user(key, umo, "以后先骂一句", False, "3", NOW, platform="Bot-A")
    book.record_user(key, umo, "你好", True, "1", NOW + 1, platform="Bot-A")
    plugin._record_pending_reply(FakeEvent("Bot-A"), _response("好"), NOW + 2)

    async def fake_model(umo_arg, prompt, **kwargs):
        return (
            "<operations>[{\"op\": \"create\", \"scope\": \"global\", \"file\": \"rules.md\", "
            "\"type\": \"feedback\", \"description\": \"准则\", \"content\": \"先骂一句\"}]"
            "</operations>",
            [],
        )

    plugin._call_summarizer = fake_model
    run(plugin._write_pending(key, umo, "idle", now=NOW + 3))
    # 整批并非全是管理员：不写 global，记录保留并计一次失败
    assert not plugin._global_scope().exists("rules.md")
    state = book.load(key)
    assert state.count == 2 and state.failures == 1


def test_mixed_batch_cannot_delete_a_persons_memory(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    run(session.write("user-2.md", memory_md("user-2", "别人的记忆")))
    raw = [{"op": "delete", "scope": "session", "file": "user-2.md", "reason": "删"}]
    # 批次里同时有第三方与本人：保守拒绝
    ops, skipped = validate(
        plugin, {"session": session}, raw, sender="2", senders={"2", "3"}
    )
    assert ops == [] and skipped
    ops, _ = validate(
        plugin, {"session": session}, raw, sender="2", senders={"2"}
    )
    assert [op.op for op in ops] == ["delete"]


def test_transcript_cap_only_drops_rendered_records(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg_writer, "MAX_TRANSCRIPT_CHARS", 80)
    records = [
        pkg_pending.Round(seq=1, at=NOW, user="第一轮", reply="好", sender="1"),
        pkg_pending.Round(seq=2, at=NOW, user="第二轮很长" * 10, reply="好", sender="1"),
    ]
    text, rendered = pkg_writer.render_rounds(records, NOW)
    assert rendered == {1}
    assert "第一轮" in text and "第二轮" not in text

    async def build():
        return await pkg_writer.build_context(
            {"session": plugin._store.session_scope("Bot-A:FriendMessage:1")},
            records,
            now=NOW,
            permission=None,
            admin=False,
            people_shared=False,
            global_on=False,
        )

    plugin = make_plugin(tmp_path, [])
    context = run(build())
    assert context.record_seqs == {1}


def test_clear_during_model_call_aborts_write(tmp_path):
    async def scenario():
        plugin = make_plugin(tmp_path, [])
        umo = "Bot-A:FriendMessage:100"
        key = pkg_memstore.safe_key(umo)
        plugin._pending.record_user(key, umo, "待整理", False, "1", NOW, platform="Bot-A")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow(umo_arg, prompt, **kwargs):
            started.set()
            await release.wait()
            return (
                "<operations>[{\"op\": \"create\", \"scope\": \"session\", "
                "\"file\": \"user-1.md\", \"type\": \"user\", "
                "\"description\": \"d\", \"content\": \"c\"}]</operations>",
                [],
            )

        plugin._call_summarizer = slow
        plugin._schedule_write(umo, "idle")
        await started.wait()
        plugin._pending.clear(key)
        release.set()
        await asyncio.sleep(0.05)
        return plugin._store.session_scope(umo).exists("user-1.md")

    assert run(scenario()) is False


def test_stop_pending_write_cancels_running_task(tmp_path):
    async def scenario():
        plugin = make_plugin(tmp_path, [])
        umo = "Bot-A:FriendMessage:100"
        key = pkg_memstore.safe_key(umo)
        plugin._pending.record_user(key, umo, "待整理", False, "1", NOW, platform="Bot-A")
        started = asyncio.Event()

        async def slow(umo_arg, prompt, **kwargs):
            started.set()
            await asyncio.sleep(10)
            return "", []

        plugin._call_summarizer = slow
        plugin._schedule_write(umo, "idle")
        await started.wait()
        await plugin.stop_pending_write(umo)
        return key in plugin._write_scheduled, plugin._store.session_scope(umo).list_files()

    scheduled, files = run(scenario())
    assert scheduled is False and files == []


def test_new_conversation_gets_fresh_failure_budget(tmp_path):
    book = pkg_pending.PendingBook(tmp_path / "state" / "pending")
    key = "k"
    book.record_user(key, "umo", "旧对话", False, "1", NOW, platform="Bot-A")
    for _ in range(pkg_pending.MAX_FAILURES):
        book.record_failure(key, NOW + 1, "模型不可用")
    assert book.load(key).records == []
    state, _ = book.record_user(
        key, "umo", "新对话", False, "1", NOW + 2, platform="Bot-A"
    )
    assert state.failures == 0 and state.count == 1


def test_journal_only_output_is_written(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    key = pkg_memstore.safe_key(umo)
    plugin._record_pending_user(event, _request("今天聊了天气"), NOW)
    plugin._record_pending_reply(event, _response("是啊"), NOW + 1)

    async def fake_model(umo_arg, prompt, **kwargs):
        return ("<journal>今天聊了天气</journal>", [])

    plugin._call_summarizer = fake_model
    run(plugin._write_pending(key, umo, "idle", now=NOW + 2))
    journals = [
        name for name in plugin._store.session_scope(umo).list_files()
        if name.startswith("journal-")
    ]
    assert len(journals) == 1
    assert "今天聊了天气" in run(plugin._store.session_scope(umo).read(journals[0]))


def test_all_rejected_operations_keep_records(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    key = pkg_memstore.safe_key(umo)
    plugin._record_pending_user(event, _request("随便"), NOW)
    plugin._record_pending_reply(event, _response("好"), NOW + 1)

    async def fake_model(umo_arg, prompt, **kwargs):
        return (
            "<operations>[{\"op\": \"create\", \"scope\": \"bogus\", "
            "\"file\": \"x.md\", \"type\": \"user\", \"description\": \"d\", "
            "\"content\": \"c\"}]</operations>",
            [],
        )

    plugin._call_summarizer = fake_model
    run(plugin._write_pending(key, umo, "idle", now=NOW + 2))
    state = plugin._pending.load(key)
    assert state.count == 1 and state.failures == 1
    assert plugin._store.session_scope(umo).list_files() == []


def test_replace_cannot_touch_frontmatter(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope("Bot-A:FriendMessage:1")
    run(session.write("user-123.md", memory_md("user-123", "正文")))
    stores = {"session": session}
    ops, _ = validate(
        plugin, stores,
        [{"op": "replace", "scope": "session", "file": "user-123.md",
          "old": "type: user", "new": "type: journal"}],
        sender="123",
    )
    result = apply(plugin, stores, ops)
    assert result.applied == 0
    doc = pkg_memstore.parse_memory(run(session.read("user-123.md")))
    assert doc.type == "user"
    assert not pkg_memstore.journal.is_journal("user-123.md", doc.type)


def test_write_stores_refreshes_shared_permissions(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=edit"])
    plugin.config["shared_people_bots"] = ["Bot-A=read"]
    stores, permission, shared = plugin._write_stores(
        "Bot-A:FriendMessage:1", "Bot-A", False
    )
    assert shared is True and permission == pkg_sharing.PERMISSION_READ
    assert "people" not in stores


def test_append_create_uses_atomic_forbid_overwrite(tmp_path):
    plugin = make_plugin(tmp_path, ["Bot-A=append"])
    session = plugin._store.people_scope("Bot-A")
    stores = {"people": session}
    ops, _ = validate(
        plugin, stores,
        [{"op": "create", "scope": "people", "file": "user-5.md",
          "type": "user", "description": "新人", "content": "工程师"}],
        sender="5", permission=pkg_sharing.PERMISSION_APPEND, shared=True,
    )
    assert ops and ops[0].forbid_overwrite is True


def test_journal_write_failure_is_reported(tmp_path, monkeypatch):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    key = pkg_memstore.safe_key(umo)
    plugin._record_pending_user(event, _request("聊点事"), NOW)
    plugin._record_pending_reply(event, _response("好"), NOW + 1)

    async def fake_model(umo_arg, prompt, **kwargs):
        return ("<journal>一段流水</journal>", [])

    async def failing_journal(*args, **kwargs):
        return "", False

    plugin._call_summarizer = fake_model
    monkeypatch.setattr(pkg_writer, "write_journal", failing_journal)
    message = run(plugin._write_pending(key, umo, "idle", now=NOW + 2))
    assert "日志写入失败" in message


def test_journal_write_invalidates_open_snapshot(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    key = pkg_memstore.safe_key(umo)
    plugin._record_pending_user(event, _request("聊点事"), NOW)
    plugin._record_pending_reply(event, _response("好"), NOW + 1)
    rt = main.RuntimeSession(state=pkg_compress.SessionState())
    run(plugin._index_block(rt, event))
    plugin._sessions[umo] = rt

    async def fake_model(umo_arg, prompt, **kwargs):
        return ("<journal>一段流水</journal>", [])

    plugin._call_summarizer = fake_model
    run(plugin._write_pending(key, umo, "idle", now=NOW + 2))
    assert rt.index_loaded is False


def test_memory_note_per_round_cap(tmp_path):
    book = pkg_pending.PendingBook(tmp_path / "state" / "pending")
    key = "k"
    dropped = 0
    for index in range(pkg_pending.MAX_NOTES_PER_ROUND + 2):
        state, count = book.add_note(
            key, "umo", f"note-{index}", "1", NOW + index, platform="Bot-A"
        )
        dropped += count
    assert dropped == 2
    notes = state.records[0].notes
    assert len(notes) == pkg_pending.MAX_NOTES_PER_ROUND
    assert notes[0].content == "note-2"


def test_journal_index_reads_headings_beyond_header_text(tmp_path):
    store = pkg_memstore.ScopeStore(tmp_path)
    body = "x" * (9 * 1024) + "\n## 2026-11-01\n\n正文"
    path = tmp_path / "journal-2026-01-01.md"
    path.write_text(
        "---\nname: journal-2026-01-01\ndescription: 日志\nmetadata:\n"
        "  type: journal\n---\n\n" + body,
        "utf-8",
    )
    entries = store._journal_entries()
    assert entries[0].start_date == "2026-11-01"


def test_external_plugin_llm_response_does_not_fill_pending_reply(tmp_path):
    plugin = make_plugin(tmp_path, [])
    event = FakeEvent("Bot-A")
    umo = event.unified_msg_origin
    key = pkg_memstore.safe_key(umo)
    plugin._record_pending_user(event, _request("用户消息"), NOW)
    event.get_extra = lambda name: types.SimpleNamespace(
        conversation=None, contexts=[]
    )
    plugin._record_pending_reply(event, _response("别的插件的回复"), NOW + 1)
    state = plugin._pending.load(key)
    assert state.records[-1].reply == ""


def test_unrendered_records_are_kept_after_success(tmp_path, monkeypatch):
    monkeypatch.setattr(pkg_writer, "MAX_TRANSCRIPT_CHARS", 80)
    plugin = make_plugin(tmp_path, [])
    umo = "Bot-A:FriendMessage:100"
    key = pkg_memstore.safe_key(umo)
    book = plugin._pending
    book.record_user(key, umo, "第一轮", False, "1", NOW, platform="Bot-A")
    book.record_user(key, umo, "第二轮" + "很长" * 30, False, "1", NOW + 1, platform="Bot-A")
    book.finish_reply(key, umo, "好", NOW + 2)

    async def fake_model(umo_arg, prompt, **kwargs):
        return (
            "<operations>[{\"op\": \"create\", \"scope\": \"session\", "
            "\"file\": \"user-1.md\", \"type\": \"user\", \"description\": \"d\", "
            "\"content\": \"c\"}]</operations>",
            [],
        )

    plugin._call_summarizer = fake_model
    run(plugin._write_pending(key, umo, "idle", now=NOW + 3))
    state = book.load(key)
    # 只删模型实际看到的第 1 轮，第 2 轮留到下次
    assert [record.seq for record in state.records] == [2]
