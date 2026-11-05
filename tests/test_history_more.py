"""保留下来的历史、回收站、索引、导入与搜索功能。"""

from __future__ import annotations

import json

from support import pkg_history, pkg_memstore, run, thing


def store(tmp_path):
    return pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")


def test_forgotten_headers_and_read(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.forget("note.md"))
    headers = scope.forgotten_headers()
    assert [header.file for header in headers] == ["note.md"]
    assert headers[0].forgotten_at > 0
    assert "正文" in run(scope.read_forgotten("note.md"))
    assert run(scope.read("note.md")) is None


def test_restore_and_delete_forgotten(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.forget("note.md"))
    report = run(scope.restore("note.md"))
    assert report.ok
    assert "正文" in run(scope.read("note.md"))

    run(scope.forget("note.md"))
    report = run(scope.delete_forgotten("note.md"))
    assert report.ok
    assert run(scope.read_forgotten("note.md")) is None
    assert scope.trash_entries()[0].file == "note.md"


def test_finalize_rename_moves_history_and_audit(tmp_path):
    scope = store(tmp_path)
    run(scope.write("old.md", thing("old", body="旧内容")))
    run(scope.write("old.md", thing("old", body="第二版")))
    run(scope.write("new.md", thing("new", body="新内容")))
    report = run(scope.finalize_rename("old.md", "new.md", actor="webui"))
    assert report.ok
    assert not (scope.root / "old.md").exists()
    assert (scope.root / "new.md").is_file()
    versions = scope.history_versions("new.md")
    assert any("旧内容" in scope.read_history("new.md", v.version) for v in versions)
    audit = (scope.root / ".audit.jsonl").read_text("utf-8")
    assert "rename" in audit


def test_delete_file_completely_removes_history_and_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="第一版")))
    run(scope.write("note.md", thing("note", body="第二版")))
    run(scope.delete("note.md"))
    run(scope.write("note.md", thing("note", body="第三版")))
    report = run(scope.delete_file_completely("note.md"))
    assert report.ok
    assert run(scope.read("note.md")) is None
    assert scope.history_versions("note.md") == []
    assert scope.trash_entries() == []
    assert run(scope.read("MEMORY.md")) is not None


def test_write_index_rebuild_and_report(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", description="甲", body="内容")))
    run(scope.write("b.md", thing("b", description="乙", body="内容")))
    report = run(scope.write_index("- [a](a.md) — 手工钩子\n"))
    assert report.ok
    health = scope.index_report()
    assert "b.md" in health.missing
    count = run(scope.rebuild_index())
    assert count == 2
    health = scope.index_report()
    assert health.missing == [] and health.dangling == []
    assert "手工钩子" not in run(scope.read("MEMORY.md"))


def test_import_conflict_policies(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="现有内容")))
    current = run(scope.read("note.md"))

    newer = pkg_memstore.ImportEntry("note.md", thing("note", body="较旧"), mtime=1.0)
    results = run(scope.import_files([newer], pkg_memstore.CONFLICT_NEWER))
    assert results[0].outcome == pkg_memstore.IMPORT_KEPT

    newer = pkg_memstore.ImportEntry("note.md", thing("note", body="较新"), mtime=9e9)
    results = run(scope.import_files([newer], pkg_memstore.CONFLICT_NEWER))
    assert results[0].outcome == pkg_memstore.IMPORT_REPLACED
    assert "较新" in run(scope.read("note.md"))

    skipped = pkg_memstore.ImportEntry("note.md", thing("note", body="跳过"))
    results = run(scope.import_files([skipped], pkg_memstore.CONFLICT_SKIP))
    assert results[0].outcome == pkg_memstore.IMPORT_KEPT
    assert "较新" in run(scope.read("note.md"))

    forced = pkg_memstore.ImportEntry("note.md", thing("note", body="覆盖"))
    results = run(scope.import_files([forced], pkg_memstore.CONFLICT_OVERWRITE))
    assert results[0].outcome == pkg_memstore.IMPORT_REPLACED
    assert "覆盖" in run(scope.read("note.md"))
    assert current != run(scope.read("note.md"))


def test_search_modes_and_forgotten_hits(tmp_path):
    scope = store(tmp_path)
    run(scope.write("a.md", thing("a", description="猫", body="喜欢猫和狗")))
    run(scope.write("b.md", thing("b", description="工作", body="写代码")))
    all_hits = run(scope.search("猫 狗", "all"))
    assert len(all_hits) == 1 and "a.md" in all_hits[0]
    any_hits = run(scope.search("猫 代码", "any"))
    assert {hit.split(":", 1)[0] for hit in any_hits} == {"a.md", "b.md"}

    run(scope.forget("a.md"))
    assert any("a.md" in hit for hit in run(scope.search_forgotten("猫", "all")))


def test_usage_statistics_and_long_files(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="内容")))
    scope.note_used(["note.md"], now=1000.0)
    assert scope.usage_of("note.md").uses == 1
    assert scope.usage_of("note.md").last_used == 1000.0
    assert run(scope.long_files()) == []


def test_cleanup_scope_archives(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.purge())
    archives = tmp_path / "memories" / pkg_history.SCOPE_TRASH_DIRNAME
    assert archives.is_dir()
    removed = pkg_history.cleanup_scope_archives(archives, 1, 1e12)
    assert removed >= 1
    assert not archives.exists() or not any(archives.iterdir())
