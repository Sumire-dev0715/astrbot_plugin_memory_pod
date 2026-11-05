"""数据目录里不认识的旧目录直接忽略：不读、不写、不删，也不报错。"""

from __future__ import annotations

from support import FakeEvent, make_plugin, pkg_memstore, run, thing


def _make_old_dirs(tmp_path):
    marker_dirs = [
        tmp_path / "archive",
        tmp_path / "session",
        tmp_path / "people",
        tmp_path / "global",
        tmp_path / "memories" / "archive",
        tmp_path / "memories" / "unknown-layer",
        tmp_path / "memories" / "session" / "not a valid key",
    ]
    for directory in marker_dirs:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "old.md").write_text("旧目录里的内容", "utf-8")
    return marker_dirs


def test_unknown_old_dirs_are_ignored_and_left_untouched(tmp_path):
    markers = _make_old_dirs(tmp_path)
    store = pkg_memstore.MemoryStore(tmp_path / "memories")
    assert store.notebook_keys() == []
    assert store.whisper_keys() == []
    assert not (store.board_scope().root / "old.md").exists()

    run(store.board_scope().write("notice.md", thing("notice", body="新告示")))
    for directory in markers:
        assert (directory / "old.md").read_text("utf-8") == "旧目录里的内容"


def test_plugin_scopes_and_opening_ignore_old_dirs(tmp_path):
    _make_old_dirs(tmp_path)
    plugin = make_plugin(tmp_path)
    event = FakeEvent("Bot-A")
    rt = __import__("support").main.RuntimeSession()
    opening = run(plugin._index_block(rt, event))
    assert opening is None or "旧目录里的内容" not in opening
    board = plugin._store.board_scope()
    assert board.list_files() == []
