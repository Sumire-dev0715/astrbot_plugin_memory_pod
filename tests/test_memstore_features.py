"""保留下来的文件编辑、索引、备份与遗忘行为。"""

from __future__ import annotations

import os
import time

import pytest

from support import pkg_memstore, run, thing


def store(tmp_path):
    return pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")


def test_replace_requires_exactly_one_match(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="甲 乙 乙")))
    report = run(scope.edit("note.md", "乙", "丙", mode="replace"))
    assert not report.ok and "出现了 2 处" in report.message
    report = run(scope.edit("note.md", "甲", "丙", mode="replace"))
    assert report.ok
    assert "丙 乙 乙" in run(scope.read("note.md"))


def test_section_replace_keeps_other_sections(tmp_path):
    scope = store(tmp_path)
    run(
        scope.write(
            "note.md",
            thing("note", body="## 作息\n\n早睡\n\n## 爱好\n\n猫"),
        )
    )
    report = run(scope.edit("note.md", "## 作息", "晚睡", mode="section"))
    assert report.ok
    content = run(scope.read("note.md"))
    assert "晚睡" in content and "早睡" not in content
    assert "## 爱好" in content and "猫" in content


def test_append_to_section(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="## 作息\n\n早睡")))
    report = run(scope.edit("note.md", "## 作息", "不熬夜", mode="append"))
    assert report.ok
    assert "不熬夜" in run(scope.read("note.md"))


def test_index_report_missing_and_dangling(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="内容")))
    index = scope.root / "MEMORY.md"
    index.write_text("- [gone](gone.md) — 旧行\n", encoding="utf-8")
    report = scope.index_report()
    assert "a.md" in report.missing
    assert "gone.md" in report.dangling


def test_rebuild_index_keeps_manual_groups(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", description="甲", body="内容")))
    index = scope.root / "MEMORY.md"
    index.write_text("# 分组\n- [a](a.md) — 旧钩子\n", encoding="utf-8")
    count = run(scope.rebuild_index())
    assert count == 1
    content = run(scope.read("MEMORY.md"))
    assert "# 分组" in content and "旧钩子" not in content


def test_write_index_rejects_too_large(tmp_path):
    scope = store(tmp_path)
    report = run(scope.write_index("x" * (pkg_memstore.MAX_FILE_BYTES + 1)))
    assert not report.ok


def test_snapshot_and_import_round_trip(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.write("gone.md", thing("gone", body="已遗忘")))
    run(scope.forget("gone.md"))
    entries, index = scope.snapshot()
    assert {entry.name for entry in entries} == {"note.md", "gone.md"}
    assert any(entry.forgotten for entry in entries)
    assert "note.md" in index

    fresh = pkg_memstore.MemoryStore(tmp_path / "other").notebook_scope("Bot-A")
    results = run(fresh.import_files(entries, pkg_memstore.CONFLICT_OVERWRITE, index=index))
    assert all(result.landed for result in results)
    assert "正文" in run(fresh.read("note.md"))
    assert run(fresh.read_forgotten("gone.md"))


def test_import_dry_run_does_not_write(tmp_path):
    scope = store(tmp_path)
    entry = pkg_memstore.ImportEntry("new.md", thing("new", body="内容"))
    results = run(scope.import_files([entry], dry_run=True))
    assert results[0].landed
    assert run(scope.read("new.md")) is None


def test_forget_stale_exempts_agreement(tmp_path):
    scope = store(tmp_path)
    old = time.time() - 20 * 86400
    run(scope.write("advice.md", thing("advice", body="指导", mem_type="agreement"), now=old))
    run(scope.write("note.md", thing("note", body="普通"), now=old))
    for name in ("advice.md", "note.md"):
        os.utime(scope.root / name, (old, old))
    forgotten = run(scope.forget_stale(86400.0))
    assert forgotten == ["note.md"]
    assert scope.exists("advice.md")


def test_delete_trash_removes_bytes(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.delete("note.md"))
    entry = scope.trash_entries()[0]
    report = run(scope.delete_trash(entry.entry))
    assert report.ok
    assert scope.trash_entries() == []


# ---------------------------------------------------------------- 路径与索引守门


def test_resolve_rejects_traversal(tmp_path):
    scope = store(tmp_path)
    for name in ("../evil.md", "sub/evil.md", "no-suffix", ".hidden.md", ""):
        assert scope._resolve(name) is None, name
    assert scope._resolve("note.md") is not None


def test_write_index_refused(tmp_path):
    scope = store(tmp_path)
    report = run(scope.write(pkg_memstore.INDEX_FILENAME, "# 乱写"))
    assert not report.ok and "自动维护" in report.message
    assert run(scope.read(pkg_memstore.INDEX_FILENAME)) is None


def test_delete_index_refused(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="甲")))
    report = run(scope.delete(pkg_memstore.INDEX_FILENAME))
    assert not report.ok
    assert run(scope.read(pkg_memstore.INDEX_FILENAME)) is not None


def test_index_guard_ignores_padding_and_case(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="甲")))
    before = run(scope.read(pkg_memstore.INDEX_FILENAME))
    for name in ("/MEMORY.md", " MEMORY.md", "MEMORY.md ", "memory.md"):
        assert not run(scope.write(name, "# 乱写")).ok, name
        assert not run(scope.delete(name)).ok, name
    assert run(scope.read(pkg_memstore.INDEX_FILENAME)) == before


def test_write_refuses_unreadable_existing_file(tmp_path):
    if os.geteuid() == 0:  # root 不受权限位限制
        pytest.skip("以 root 运行时无法用权限位模拟读失败")
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", body="第一版")))
    path = scope.root / "a.md"
    path.chmod(0o000)
    try:
        report = run(scope.write("a.md", thing("a", body="第二版")))
    finally:
        path.chmod(0o644)
    assert not report.ok and "读取" in report.message
    assert "第一版" in path.read_text("utf-8")
    assert scope.history_versions("a.md") == []


def test_search_excludes_index(tmp_path):
    scope = store(tmp_path)
    run(scope.write("empty.md", "---\nname: empty\n---\n"))
    # 钩子兜底文案只存在于 MEMORY.md，搜索不应命中索引文件
    assert "（无描述）" in run(scope.read(pkg_memstore.INDEX_FILENAME))
    assert run(scope.search("无描述")) == []


def test_memory_store_scopes_are_isolated(tmp_path):
    memory_store = pkg_memstore.MemoryStore(tmp_path / "memories")
    first = memory_store.notebook_scope("Bot-A")
    second = memory_store.notebook_scope("Bot-B")
    run(first.write("note.md", thing("note", description="A 的钩子", body="A 的内容")))
    run(second.write("note.md", thing("note", description="B 的钩子", body="B 的内容")))
    assert "A 的内容" in run(first.read("note.md"))
    assert "B 的内容" in run(second.read("note.md"))
    assert memory_store.notebook_scope("Bot-A") is first
    assert run(first.search("B 的内容")) == []
    assert "B 的钩子" not in run(first.read(pkg_memstore.INDEX_FILENAME))
    assert "A 的钩子" not in run(second.read(pkg_memstore.INDEX_FILENAME))


def test_unknown_type_is_treated_as_note(tmp_path):
    """文件里出现不认识的类型：按笔记处理，不报错，也不豁免遗忘。"""
    scope = store(tmp_path)
    old = time.time() - 20 * 86400
    run(
        scope.write(
            "legacy.md",
            thing("legacy", description="旧类型", body="旧内容", mem_type="legacy"),
            now=old,
        )
    )
    header = next(h for h in scope.headers() if h.file == "legacy.md")
    assert header.type == "note"
    hits = run(scope.search_hits("旧内容"))
    assert hits and hits[0].file == "legacy.md" and hits[0].mem_type == "note"
    assert run(scope.long_files()) == []

    run(scope.write("fresh.md", thing("fresh", body="新内容")))
    forgotten = run(scope.forget_stale(86400.0))
    assert forgotten == ["legacy.md"]
    assert not scope.exists("legacy.md")

    # 人物档案由文件名决定：未知类型不改变它是这个人的档案
    run(
        scope.write(
            "user-123.md",
            thing("user-123", description="阿杰", body="身份", mem_type="legacy"),
        )
    )
    header = next(h for h in scope.headers() if h.file == "user-123.md")
    assert header.type == "person"
