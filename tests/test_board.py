"""留言板：署名、开头块全文与 2000 字上限、超出只列标题。"""

from __future__ import annotations

import re
from pathlib import Path

from support import pkg_injection, pkg_memstore, pkg_prompts


def notice(file, body, signature="管理员", written=0.0, title="告示"):
    return pkg_injection.BoardNotice(
        file=file, title=title, body=body, signature=signature, written=written
    )


def test_board_signature_round_trips_through_frontmatter():
    content = pkg_memstore.compose_memory(
        "notice", "新员工", "", "11 月 11 日 teto 入职", signature="teto"
    )
    doc = pkg_memstore.parse_memory(content)
    assert doc.signature == "teto"
    assert doc.description == "新员工"
    assert "signature: teto" in content
    # 留言板告示没有类型（PLAN 3.10.1）
    assert doc.type == ""
    assert "type:" not in content


def test_board_full_text_newest_first_and_over_limit_titles_only():
    notices = [
        notice("old.md", "旧" * 4, signature="甲", written=1.0),
        notice("mid.md", "中" * 1200, signature="乙", written=2.0),
        notice("new.md", "新" * 1200, signature="丙", written=3.0),
    ]
    text = pkg_injection.render_board_notices(notices)
    # 从新到旧：新的放得下就全文，一旦放不下，更旧的全部只列标题，
    # 不允许中间的短告示插队。
    assert text.index("new.md") < text.index("mid.md") < text.index("old.md")
    full = text.split("其余告示只列标题")[0]
    assert "新" * 1200 in full
    assert "中" * 1200 not in text
    assert "旧" * 4 not in text.split("其余告示只列标题")[0]
    assert "mid.md — 告示" in text and "old.md — 告示" in text
    # 全文段（含留言板标题）不超过 2000 字
    assert len(full) <= 2000


def test_board_budget_includes_heading_and_block_separators():
    notices = [
        notice("a.md", "甲" * 999, signature="甲", written=1.0),
        notice("b.md", "乙" * 999, signature="乙", written=2.0),
    ]
    text = pkg_injection.render_board_notices(notices)
    full = text.split("其余告示只列标题")[0]
    assert "乙" * 999 in full
    assert "甲" * 999 not in full
    assert len(full) <= 2000


def test_board_empty_returns_empty_string():
    assert pkg_injection.render_board_notices([]) == ""


def test_opening_block_contains_board_full_text_and_scope_indexes():
    board = pkg_injection.render_board_notices(
        [notice("notice.md", "11 月 11 日新员工 teto 入职", signature="管理员")]
    )
    block = pkg_injection.build_opening_block(
        board, "- notebook.md — 小本本", "- whisper.md — 悄悄话", []
    )
    assert block.startswith(pkg_prompts.INDEX_BLOCK_OPEN)
    assert pkg_prompts.DATA_NOTICE in block
    assert "# 留言板" in block
    assert "11 月 11 日新员工 teto 入职" in block
    assert "署名：管理员" in block
    assert pkg_injection.NOTEBOOK_HEADING in block
    assert pkg_injection.WHISPER_HEADING in block
    assert "notebook.md" in block and "whisper.md" in block


def test_opening_block_none_when_everything_empty():
    assert pkg_injection.build_opening_block("", " ", "", []) is None


def _page(name):
    return (Path(__file__).resolve().parent.parent / "pages" / "memory" / name).read_text(
        "utf-8"
    )


def test_management_page_has_signature_input_and_remembers_last_value():
    publish = _page("memory.js").split("function PublishForm()", 1)[1].split("/** 续期", 1)[0]
    # 页面跑在没有 allow-same-origin 的沙箱 iframe 里，localStorage 会抛 SecurityError：
    # 上次的署名由服务端记着（overview.board.lastSignature），表单据此预填
    assert "S.ov.board.lastSignature" in publish
    assert "signature: last" in publish
    assert 'apiPost("board/publish"' in publish
    assert "signature: cur.signature.trim()" in publish
    assert 't("fieldSignature")' in publish
    assert 't("signatureRequired")' in publish
    for name in ("memory.js", "manage.js", "app.js", "ui.js", "store.js"):
        code = re.sub(r"/\*.*?\*/", "", _page(name), flags=re.S)
        code = re.sub(r"(?m)(^|[^:\"'\\])//.*$", r"\1", code)
        assert "localStorage" not in code, name


def test_management_page_has_optional_expiry_date_and_expired_tag():
    memory = _page("memory.js")
    expiry = memory.split("function expiryInput(", 1)[1].split("function formText", 1)[0]
    assert 'type: "date"' in expiry
    # 常用的到期日有快捷选项，也可以改回长期有效
    assert "dateAfter(" in expiry and 't("expiryNone")' in expiry

    editor = memory.split("function Editor(view, r, opts)", 1)[1].split(
        "// ------------------------------------------------------------ 发布告示", 1
    )[0]
    assert 't("fieldExpires")' in editor
    assert "expiryInput(cur.expires" in editor
    # 到期日只属于留言板；空串表示清掉到期日
    assert "if (board) {\n        // 到期日只属于留言板" in editor
    assert 'payload.expires = cur.expires || ""' in editor

    publish = memory.split("function PublishForm()", 1)[1].split("/** 续期", 1)[0]
    assert 't("fieldExpires")' in publish
    assert "expiryInput(cur.expires" in publish
    assert 'expires: cur.expires || ""' in publish

    # 告示列表显示到期日，过期的标"已过期"，并且可以按有效期筛选和续期
    tag = memory.split("function expiryTag(row) {", 1)[1].split("function noticeCard", 1)[0]
    assert 't("expiredOn"' in tag
    assert 't("expiresOn"' in tag
    assert 't("noExpiry")' in tag
    assert "row.expired" in tag
    card = memory.split("function noticeCard(row) {", 1)[1].split("function boardRows", 1)[0]
    assert "expiryTag(row)" in card
    assert 't("expired")' in memory and "st.status === \"expired\"" in memory
    assert "function renewNotice(" in memory and 't("renewSave")' in memory
