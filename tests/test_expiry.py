"""告示有效期（PLAN 3.9.2）：到期判断、memory_write 参数、开头块刷新与过期标注。

到期日当天仍然有效，本地日期进入第二天才算过期。时区按 AstrBot 全局
配置的 timezone；没有配置或名字无效时按本机时区。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from support import (
    FakeEvent,
    main,
    make_plugin,
    pkg_expiry,
    pkg_memstore,
    run,
    thing,
)

TOKYO = ZoneInfo("Asia/Tokyo")


def admin_event(umo="Bot-A:GroupMessage:900"):
    return FakeEvent(
        "Bot-A", "1", umo=umo, admin=True, group_id="900", group_name="合成群"
    )


# ---------------------------------------------------------------- 到期判断


def test_expires_round_trips_through_frontmatter():
    content = thing(
        "notice", body="正文", signature="管理员", expires="2026-11-11"
    )
    doc = pkg_memstore.parse_memory(content)
    assert doc.expires == "2026-11-11"
    assert "  expires: 2026-11-11" in content
    # 顶层 expires 也认；metadata 下的优先
    top = (
        "---\nname: notice\ndescription: 告示\nexpires: 2026-12-01\n"
        "metadata:\n  type: reference\n  expires: 2026-12-02\n---\n\n正文\n"
    )
    assert pkg_memstore.parse_memory(top).expires == "2026-12-02"


def test_expiry_day_stays_valid_until_the_day_ends():
    start = datetime(2026, 11, 11, 0, 0, tzinfo=TOKYO).timestamp()
    end = datetime(2026, 11, 11, 23, 59, 59, tzinfo=TOKYO).timestamp()
    next_day = datetime(2026, 11, 12, 0, 0, tzinfo=TOKYO).timestamp()
    assert not pkg_expiry.is_expired("2026-11-11", start, TOKYO)
    assert not pkg_expiry.is_expired("2026-11-11", end, TOKYO)
    assert pkg_expiry.is_expired("2026-11-11", next_day, TOKYO)


def test_expiry_without_timezone_uses_local_date():
    ts = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc).timestamp()
    today = pkg_expiry.local_date(ts, None)
    assert not pkg_expiry.is_expired(today.isoformat(), ts, None)
    assert pkg_expiry.is_expired((today - timedelta(days=1)).isoformat(), ts, None)


def test_expiry_rejects_malformed_dates():
    for value in ("2026-1-1", "2026/11/11", "2026-02-30", "明天", "20261111"):
        normalized, error = pkg_expiry.normalize(value)
        assert normalized == ""
        assert "YYYY-MM-DD" in error
    assert pkg_expiry.normalize("") == ("", "")
    # 读文件时格式不对当作没有到期日，不让旧文件被误判过期
    assert pkg_expiry.is_expired("2026-1-1", 0.0, TOKYO) is False


def test_plugin_uses_astrbot_configured_timezone(tmp_path):
    plugin = make_plugin(tmp_path)
    # 这个时刻在 UTC+14 已经是 11 月 7 日
    ts = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc).timestamp()
    plugin.context.get_config = lambda umo=None: {
        "timezone": "Pacific/Kiritimati"
    }
    assert plugin.notice_expired("2026-11-06", ts) is True

    plugin.context.get_config = lambda umo=None: {}
    assert plugin.expiry_timezone() is None
    assert plugin.notice_expired("2026-11-06", ts) == (
        pkg_expiry.local_date(ts, None) > date(2026, 11, 6)
    )

    # 配置了无效时区名时，与 AstrBot 一样退回本机时区
    plugin.context.get_config = lambda umo=None: {"timezone": "Not/AZone"}
    assert plugin.expiry_timezone() is None


# ------------------------------------------------------------ memory_write


def test_memory_write_expires_only_applies_to_board(tmp_path):
    plugin = make_plugin(tmp_path)
    admin = admin_event()
    receipt = run(
        plugin.memory_write(
            admin,
            "board",
            "notice.md",
            "reference",
            "告示",
            "正文",
            expires="2026-11-11",
        )
    )
    assert "已写入" in receipt
    content = run(plugin._store.board_scope().read("notice.md"))
    doc = pkg_memstore.parse_memory(content)
    assert doc.expires == "2026-11-11"
    assert doc.signature

    receipt = run(
        plugin.memory_write(
            admin,
            "board",
            "bad.md",
            "reference",
            "告示",
            "正文",
            expires="2026/11/11",
        )
    )
    assert "YYYY-MM-DD" in receipt
    assert not plugin._store.board_scope().exists("bad.md")

    # 其他作用域：整条忽略，格式错了也照常写
    member = FakeEvent("Bot-A", "123")
    receipt = run(
        plugin.memory_write(
            member,
            "notebook",
            "note.md",
            "reference",
            "笔记",
            "正文",
            expires="不是日期",
        )
    )
    assert "已写入" in receipt
    written = run(plugin._store.notebook_scope("Bot-A").read("note.md"))
    assert pkg_memstore.parse_memory(written).expires == ""
    assert "expires:" not in written


# ---------------------------------------------------------------- 开头块


def test_expired_notice_is_left_out_of_the_opening_block(tmp_path):
    plugin = make_plugin(tmp_path)
    plugin.context.get_config = lambda umo=None: {"timezone": "Asia/Tokyo"}
    board = plugin._store.board_scope()
    run(
        board.write(
            "old.md",
            thing(
                "old",
                description="旧告示",
                body="过期告示正文",
                signature="管理员",
                expires="2000-01-01",
            ),
        )
    )
    run(
        board.write(
            "today.md",
            thing(
                "today",
                description="到期日当天的告示",
                body="到期日当天的告示正文",
                signature="管理员",
                expires="2026-11-11",
            ),
        )
    )
    run(
        board.write(
            "later.md",
            thing(
                "later",
                description="以后的告示",
                body="未来告示正文",
                signature="管理员",
                expires="2026-11-12",
            ),
        )
    )
    run(
        board.write(
            "far.md",
            thing(
                "far",
                description="长期告示",
                body="长期告示正文",
                signature="管理员",
                expires="2999-12-31",
            ),
        )
    )
    last_moment = datetime(2026, 11, 11, 23, 59, 59, tzinfo=TOKYO).timestamp()
    assert [notice.file for notice in run(plugin._board_notices(last_moment))] == [
        "far.md",
        "later.md",
        "today.md",
    ]
    next_day = datetime(2026, 11, 12, 0, 0, 1, tzinfo=TOKYO).timestamp()
    assert [notice.file for notice in run(plugin._board_notices(next_day))] == [
        "far.md",
        "later.md",
    ]

    # 当前时刻下，只有 2000 年那条不在开头块里
    opening = run(plugin._index_block(main.RuntimeSession(), FakeEvent("Bot-A", "123")))
    assert "长期告示正文" in opening
    assert "过期告示正文" not in opening


def test_opening_block_refreshes_on_first_turn_of_a_new_day(tmp_path):
    plugin = make_plugin(tmp_path)
    board = plugin._store.board_scope()
    run(
        board.write(
            "first.md",
            thing("first", description="第一条", body="第一条告示", signature="管理员"),
        )
    )
    event = FakeEvent("Bot-A", "123")
    rt = main.RuntimeSession()
    first = run(plugin._index_block(rt, event))

    # 同一天：记忆不变时逐轮逐字不变，磁盘上新增的告示要等刷新才出现
    run(
        board.write(
            "second.md",
            thing("second", description="第二条", body="第二条告示", signature="管理员"),
        )
    )
    same_day = run(plugin._index_block(rt, event))
    assert same_day == first
    assert "第二条告示" not in same_day

    # 跨天后的第一轮重建快照
    rt.index_day = date(2000, 1, 1)
    refreshed = run(plugin._index_block(rt, event))
    assert refreshed != first
    assert "第二条告示" in refreshed


def test_board_tool_changes_refresh_every_session(tmp_path):
    plugin = make_plugin(tmp_path)
    event_a = FakeEvent("Bot-A", "1", umo="Bot-A:GroupMessage:900")
    event_b = FakeEvent("Bot-B", "2", umo="Bot-B:FriendMessage:2")
    rt_a = run(plugin._runtime(event_a.unified_msg_origin))
    rt_b = run(plugin._runtime(event_b.unified_msg_origin))
    run(plugin._index_block(rt_a, event_a))
    run(plugin._index_block(rt_b, event_b))

    admin = admin_event()
    assert "已写入" in run(
        plugin.memory_write(admin, "board", "notice.md", "reference", "告示", "新告示")
    )
    assert rt_a.index_loaded is False and rt_b.index_loaded is False
    assert "新告示" in run(plugin._index_block(rt_a, event_a))
    assert "新告示" in run(plugin._index_block(rt_b, event_b))

    assert "已修改" in run(
        plugin.memory_edit(
            admin,
            "board",
            "notice.md",
            mode="replace",
            old_text="新告示",
            new_text="改过的告示",
        )
    )
    assert rt_a.index_loaded is False and rt_b.index_loaded is False
    assert "改过的告示" in run(plugin._index_block(rt_b, event_b))

    assert "已删除" in run(
        plugin.memory_delete(admin, "board", "notice.md", reason="过期")
    )
    assert rt_a.index_loaded is False and rt_b.index_loaded is False
    assert run(plugin._index_block(rt_a, event_a)) is None


# ------------------------------------------------------------ 读取与搜索


def test_memory_read_marks_expired_board_notice(tmp_path):
    plugin = make_plugin(tmp_path)
    board = plugin._store.board_scope()
    run(
        board.write(
            "old.md",
            thing(
                "old",
                description="旧告示",
                body="过期告示正文",
                signature="管理员",
                expires="2000-01-01",
            ),
        )
    )
    run(
        board.write(
            "later.md",
            thing(
                "later",
                description="新告示",
                body="未来告示正文",
                signature="管理员",
                expires="2999-12-31",
            ),
        )
    )
    event = FakeEvent("Bot-A", "123")
    expired = run(plugin.memory_read(event, "board", "old.md"))
    assert "已过期" in expired
    assert "过期告示正文" in expired
    current = run(plugin.memory_read(event, "board", "later.md"))
    assert "已过期" not in current


def test_memory_search_marks_expired_board_notice(tmp_path):
    plugin = make_plugin(tmp_path)
    board = plugin._store.board_scope()
    run(
        board.write(
            "old.md",
            thing(
                "old",
                description="旧告示",
                body="入职安排：旧",
                signature="管理员",
                expires="2000-01-01",
            ),
        )
    )
    run(
        board.write(
            "later.md",
            thing(
                "later",
                description="新告示",
                body="入职安排：新",
                signature="管理员",
                expires="2999-12-31",
            ),
        )
    )
    text = run(plugin.memory_search(FakeEvent("Bot-A", "123"), "入职"))
    old_line = next(line for line in text.splitlines() if "old.md" in line)
    later_line = next(line for line in text.splitlines() if "later.md" in line)
    assert "已过期" in old_line
    assert "已过期" not in later_line


def test_renewed_notice_returns_to_the_opening_block(tmp_path):
    plugin = make_plugin(tmp_path)
    board = plugin._store.board_scope()
    run(
        board.write(
            "notice.md",
            thing(
                "notice",
                description="旧告示",
                body="过期告示正文",
                signature="管理员",
                expires="2000-01-01",
            ),
        )
    )
    event = FakeEvent("Bot-A", "123")
    rt = run(plugin._runtime(event.unified_msg_origin))
    run(plugin._index_block(rt, event))

    # 管理员续期：先读最新版本，再用 memory_write 整篇重写并给新的到期日
    admin = admin_event()
    assert "已过期" in run(plugin.memory_read(admin, "board", "notice.md"))
    receipt = run(
        plugin.memory_write(
            admin,
            "board",
            "notice.md",
            "reference",
            "续期后的告示",
            "续期后的正文",
            expires="2999-12-31",
        )
    )
    assert "整篇重写" in receipt
    assert rt.index_loaded is False
    opening = run(plugin._index_block(rt, event))
    assert "续期后的正文" in opening


def test_management_page_board_change_invalidates_every_session(tmp_path):
    plugin = make_plugin(tmp_path)
    rt_a = run(plugin._runtime("Bot-A:GroupMessage:900"))
    rt_b = run(plugin._runtime("Bot-B:FriendMessage:2"))
    run(plugin._index_block(rt_a, FakeEvent("Bot-A", "1", umo="Bot-A:GroupMessage:900")))
    run(plugin._index_block(rt_b, FakeEvent("Bot-B", "2", umo="Bot-B:FriendMessage:2")))

    # 管理页的 _changed(SCOPE_BOARD, key) 最终调用这里；key 不能把留言板
    # 的改动限定到某一个会话
    plugin.invalidate_scope("board", "")
    assert rt_a.index_loaded is False and rt_b.index_loaded is False
