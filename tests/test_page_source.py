"""管理页没有 DOM 测试环境：这里锁住回收站「彻底删除（含历史）」的关键接线。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "memory" / "app.js"


def _source() -> str:
    return PAGE.read_text("utf-8")


def test_trash_tab_purge_button_only_for_missing_active_files():
    source = _source()
    # 回收站渲染时先取当前在册文件名，只有不在册的条目才给彻底删除按钮
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
        "// ------------------------------------------------------------", 1
    )[0]
    assert "confirmDialog(" in body
    assert 't("trashPurgeTitle")' in body
    assert 't("trashPurgeMessage"' in body
    assert 'apiPost("file/purge"' in body
    assert "loadDetail()" in body and "loadOverview(true)" in body


def test_home_shows_user_identifier_notice():
    """3.7.1：管理页首页提示 AstrBot 用户识别；压缩相关提示随功能删除。"""
    source = _source()
    welcome = source.split("function renderWelcome()", 1)[1].split(
        "function renderDetail()", 1
    )[0]
    assert 't("userIdentifierNotice")' in welcome
    assert 't("turnLimitNotice"' not in welcome


def test_context_panel_is_read_only():
    """3.8.6：上下文面板只读，没有重置或改动上下文的入口。"""
    source = _source()
    assert '"tabContext"' in source
    assert 't("contextReadonlyNotice")' in source
    assert 'apiGet("session/context"' in source
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


def test_archive_tab_is_gone():
    source = _source()
    assert 't("tabArchives")' not in source
    assert "renderArchiveTab" not in source
    assert "openArchive" not in source


def test_consolidate_all_includes_people_without_enabled_flag():
    """overview 不再返回 people.enabled；整理全部仍要收进已有人物记忆。"""
    source = _source()
    body = source.split("function consolidateTargets()", 1)[1].split(
        "function sameScope(", 1
    )[0]
    assert "people.enabled" not in body
    assert "people.platforms" in body
    assert "p.fileCount > 0" in body
