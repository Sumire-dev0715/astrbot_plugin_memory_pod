"""管理页没有 DOM 测试环境：这里锁住关键接线与已删除功能的痕迹。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "memory" / "app.js"


def _source() -> str:
    return PAGE.read_text("utf-8")


def test_trash_tab_purge_button_only_for_missing_active_files():
    source = _source()
    render = source.split("function renderTrashTab()", 1)[1].split(
        "async function viewTrashEntry", 1
    )[0]
    assert "const active = new Set(" in render
    assert "active.has(item.file)" in render
    assert "purgeTrashFile(item)" in render
    assert 't("purgeFile")' in render


def test_trash_purge_uses_file_purge_with_confirmation():
    source = _source()
    body = source.split("async function purgeTrashFile(item)", 1)[1].split(
        "// ------------------------------------------------------------ 整理", 1
    )[0]
    assert "confirmDialog(" in body
    assert 't("trashPurgeTitle")' in body
    assert 't("trashPurgeMessage"' in body
    assert 'apiPost("file/purge"' in body
    assert "loadDetail()" in body and "loadOverview(true)" in body


def test_home_shows_user_identifier_notice():
    source = _source()
    welcome = source.split("function renderWelcome()", 1)[1].split(
        "function renderDetail()", 1
    )[0]
    assert 't("userIdentifierNotice")' in welcome
    assert 't("turnLimitNotice"' not in welcome


def test_context_panel_is_read_only():
    source = _source()
    assert '"tabContext"' in source
    assert 't("contextReadonlyNotice")' in source
    assert 'apiGet("whisper/context"' in source
    assert "session/context" not in source
    assert "session/reset" not in source
    assert "resetCompression" not in source
    assert 't("summaryTitle")' in source
    assert 't("noSummary")' in source


def test_context_panel_leaves_no_dead_ui():
    source = _source()
    assert "fmtDuration" not in source
    assert "pendingRounds" not in source
    assert "下一次压缩" not in source
    api_source = (ROOT / "web_api.py").read_text("utf-8")
    assert "hasContext" not in api_source


def test_removed_features_are_gone_from_page():
    source = _source()
    for needle in (
        "autotidy",
        "autoTidy",
        "tidyMode",
        "pendingScopes",
        "renderPendingPanel",
        "SHARED_KEY",
        "globalTitle",
        "peopleTitle",
        "renderArchiveTab",
    ):
        assert needle not in source, needle


def test_consolidate_all_covers_three_layers_only():
    source = _source()
    body = source.split("function consolidateTargets()", 1)[1].split(
        "function sameScope(", 1
    )[0]
    assert "ov.board" in body
    assert "ov.notebooks" in body
    assert "ov.whispers" in body
    assert "people.platforms" not in body
    assert "sessions" not in body
