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


def test_home_shows_compression_follow_notices():
    """3.7.1：管理页首页提示轮数上限冲突、AstrBot 用户识别与压缩关闭。"""
    source = _source()
    welcome = source.split("function renderWelcome()", 1)[1].split(
        "function renderDetail()", 1
    )[0]
    assert 't("compressionOffNotice")' in welcome
    assert 't("turnLimitNotice"' in welcome
    assert 't("userIdentifierNotice")' in welcome


def test_archive_tab_is_gone():
    source = _source()
    assert 't("tabArchives")' not in source
    assert "renderArchiveTab" not in source
    assert "openArchive" not in source
