"""移动单个文件（PLAN 3.12.2）：小本本与悄悄话之间，先写目标再移来源。

三种同名处理：保留两份（新名字加序号）、覆盖（旧版进历史）、取消。
取消是页面上的选择，不发请求，由页面运行时场景测试。
"""

from __future__ import annotations

import os
import time

from support import pkg_memstore, thing

from test_web_api import body, call, route, setup


def make(tmp_path):
    plugin, dashboard = setup(tmp_path)
    key = pkg_memstore.safe_key("Bot-A:FriendMessage:10001")
    whisper = plugin._store.whisper_scope_by_key(key)
    notebook = plugin._store.notebook_scope("Bot-A")
    return plugin, dashboard, key, whisper, notebook


def move(dashboard, **payload):
    body(payload)
    return route(dashboard, "migrate_file")


def from_whisper(key, name, conflict="overwrite", dry_run=False):
    return {
        "from": {"scope": "whisper", "key": key},
        "to": {"scope": "notebook", "platform": "Bot-A"},
        "file": name,
        "conflict": conflict,
        "dryRun": dry_run,
    }


def test_move_whisper_to_notebook_keeps_content_source_and_timestamps(tmp_path):
    plugin, dashboard, key, whisper, notebook = make(tmp_path)
    content = thing("health", description="小红的健康", body="最近睡得不好")
    call(whisper.write("health.md", content, source="10001", actor="Bot-A"))
    written = whisper.root / "health.md"
    original = call(whisper.read("health.md"))
    stamp = time.time() - 3 * 86400
    os.utime(written, (stamp, stamp))

    dry = move(dashboard, **from_whisper(key, "health.md", dry_run=True))
    assert dry["status"] == "ok"
    assert dry["data"]["conflict"] is False
    assert dry["data"]["counts"]["added"] == 1
    assert written.is_file()
    assert not (notebook.root / "health.md").exists()

    done = move(dashboard, **from_whisper(key, "health.md"))
    assert done["status"] == "ok", done
    assert done["data"]["moved"] == 1
    assert not written.exists()
    assert [item.file for item in whisper.trash_entries()] == ["health.md"]

    target = notebook.root / "health.md"
    assert target.read_text("utf-8") == original
    assert abs(target.stat().st_mtime - stamp) < 1
    moved = call(notebook.read("health.md"))
    assert "created:" in moved and "updated:" in moved
    assert "source: 10001" in moved
    # 两处都要作废开头块快照
    assert ("notebook", "Bot-A") in plugin.invalidated
    assert ("whisper", key) in plugin.invalidated


def test_move_notebook_to_whisper_lands_only_in_that_conversation(tmp_path):
    _, dashboard, key, whisper, notebook = make(tmp_path)
    call(notebook.write("plans.md", thing("plans", description="计划", body="周三评审")))
    other_key = pkg_memstore.safe_key("Bot-A:FriendMessage:20002")
    payload = {
        "from": {"scope": "notebook", "key": "Bot-A"},
        "to": {"scope": "whisper", "key": key},
        "file": "plans.md",
        "conflict": "overwrite",
    }
    done = move(dashboard, **payload)
    assert done["status"] == "ok", done
    assert done["data"]["moved"] == 1
    assert done["data"]["to"]["key"] == key
    assert "周三评审" in call(whisper.read("plans.md"))
    assert not (notebook.root / "plans.md").exists()
    assert not (tmp_path / "memories" / "whisper" / other_key / "plans.md").exists()


def test_move_conflict_keep_both_uses_numbered_name(tmp_path):
    _, dashboard, key, whisper, notebook = make(tmp_path)
    call(notebook.write("health.md", thing("health", description="目标旧版", body="目标里的旧内容")))
    call(notebook.write("health-2.md", thing("health", description="目标第二份", body="目标里的另一份")))
    call(whisper.write("health.md", thing("health", description="搬来的", body="搬来的内容")))

    done = move(dashboard, **from_whisper(key, "health.md", conflict="keep"))
    assert done["status"] == "ok", done
    data = done["data"]
    assert data["conflict"] is True
    assert data["moved"] == 1
    assert data["renamed"] == [{"from": "health.md", "to": "health-3.md"}]
    assert "目标里的旧内容" in call(notebook.read("health.md"))
    assert "目标里的另一份" in call(notebook.read("health-2.md"))
    assert "搬来的内容" in call(notebook.read("health-3.md"))
    assert not (whisper.root / "health.md").exists()
    assert [item.file for item in whisper.trash_entries()] == ["health.md"]


def test_move_conflict_overwrite_puts_old_version_into_history(tmp_path):
    _, dashboard, key, whisper, notebook = make(tmp_path)
    call(notebook.write("health.md", thing("health", description="目标旧版", body="目标里的旧内容")))
    call(whisper.write("health.md", thing("health", description="搬来的", body="搬来的内容")))

    done = move(dashboard, **from_whisper(key, "health.md", conflict="overwrite"))
    assert done["status"] == "ok", done
    assert done["data"]["moved"] == 1
    assert done["data"]["renamed"] == []
    assert "搬来的内容" in call(notebook.read("health.md"))
    versions = notebook.history_versions("health.md")
    assert any(
        "目标里的旧内容" in notebook.read_history("health.md", version.version)
        for version in versions
    )
    assert not (whisper.root / "health.md").exists()


def test_move_dry_run_reports_conflict_and_writes_nothing(tmp_path):
    _, dashboard, key, whisper, notebook = make(tmp_path)
    call(notebook.write("health.md", thing("health", description="目标旧版", body="目标里的旧内容")))
    call(whisper.write("health.md", thing("health", description="搬来的", body="搬来的内容")))

    dry = move(dashboard, **from_whisper(key, "health.md", conflict="keep", dry_run=True))
    assert dry["status"] == "ok", dry
    assert dry["data"]["conflict"] is True
    assert dry["data"]["renamed"] == [{"from": "health.md", "to": "health-2.md"}]
    assert (whisper.root / "health.md").is_file()
    assert not (notebook.root / "health-2.md").exists()


def test_move_rejects_board_same_scope_and_missing_file(tmp_path):
    plugin, dashboard, key, whisper, _ = make(tmp_path)
    call(whisper.write("health.md", thing("health", body="内容")))
    board_notice = thing("welcome", description="告示", body="正文", signature="管理员")
    call(plugin._store.board_scope().write("welcome.md", board_notice))

    from_board = move(
        dashboard,
        **{
            "from": {"scope": "board", "key": ""},
            "to": {"scope": "notebook", "platform": "Bot-A"},
            "file": "welcome.md",
            "conflict": "overwrite",
        },
    )
    assert from_board["status"] == "error" and "留言板" in from_board["message"]

    to_board = move(
        dashboard,
        **{
            "from": {"scope": "whisper", "key": key},
            "to": {"scope": "board", "key": ""},
            "file": "health.md",
            "conflict": "overwrite",
        },
    )
    assert to_board["status"] == "error" and "留言板" in to_board["message"]

    same = move(
        dashboard,
        **{
            "from": {"scope": "whisper", "key": key},
            "to": {"scope": "whisper", "key": key},
            "file": "health.md",
            "conflict": "overwrite",
        },
    )
    assert same["status"] == "error"

    missing = move(dashboard, **from_whisper(key, "nope.md"))
    assert missing["status"] == "error" and "来源里没有" in missing["message"]

    bad = move(dashboard, **from_whisper(key, "health.md", conflict="rename"))
    assert bad["status"] == "error"
    assert whisper.root.joinpath("health.md").is_file()


def test_move_keeps_source_when_target_write_is_rejected(tmp_path):
    _, dashboard, key, whisper, notebook = make(tmp_path)
    huge = thing("huge", description="太大", body="x" * pkg_memstore.MAX_FILE_BYTES)
    whisper.root.mkdir(parents=True, exist_ok=True)
    (whisper.root / "huge.md").write_text(huge, encoding="utf-8")

    done = move(dashboard, **from_whisper(key, "huge.md"))
    assert done["status"] == "ok", done
    assert done["data"]["moved"] == 0
    assert done["data"]["counts"]["added"] == 0
    assert (whisper.root / "huge.md").is_file()
    assert not (notebook.root / "huge.md").exists()
    assert "没有写入目标" in done["message"]
