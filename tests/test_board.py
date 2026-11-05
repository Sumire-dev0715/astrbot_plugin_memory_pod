"""留言板：署名、开头块全文与 2000 字上限、超出只列标题。"""

from __future__ import annotations

from pathlib import Path

from support import pkg_injection, pkg_memstore, pkg_prompts


def notice(file, body, signature="管理员", written=0.0, title="告示"):
    return pkg_injection.BoardNotice(
        file=file, title=title, body=body, signature=signature, written=written
    )


def test_board_signature_round_trips_through_frontmatter():
    content = pkg_memstore.compose_memory(
        "notice", "新员工", "reference", "11 月 11 日 teto 入职", signature="teto"
    )
    doc = pkg_memstore.parse_memory(content)
    assert doc.signature == "teto"
    assert doc.description == "新员工"
    assert "signature: teto" in content


def test_board_full_text_newest_first_and_over_limit_titles_only():
    notices = [
        notice("old.md", "旧" * 800, signature="甲", written=1.0),
        notice("mid.md", "中" * 800, signature="乙", written=2.0),
        notice("new.md", "新" * 800, signature="丙", written=3.0),
    ]
    text = pkg_injection.render_board_notices(notices)
    # 从新到旧
    assert text.index("new.md") < text.index("mid.md") < text.index("old.md")
    full = text.split("其余告示只列标题")[0]
    assert "丙" in full and "乙" in full
    assert full.count("### ") == 2
    assert "新" * 800 in full and "中" * 800 in full
    # 放不下的那条只列标题，不出现正文
    assert "旧" * 800 not in text
    assert "old.md — 告示" in text
    # 全文字数（含标题/署名）不超过 2000
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


def test_management_page_has_signature_input_and_remembers_last_value():
    source = (Path(__file__).resolve().parent.parent / "pages" / "memory" / "app.js").read_text(
        "utf-8"
    )
    body = source.split("function publishNotice()", 1)[1].split(
        "// ------------------------------------------------------------ 搜索", 1
    )[0]
    assert 'SIGNATURE_KEY = "memoryPod.boardSignature"' in source
    assert "savedSignature()" in body
    assert "rememberSignature(" in body
    assert "signatureText" in body
    assert 'apiPost("board/publish"' in body
    assert 't("fieldSignature")' in body
