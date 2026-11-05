"""自动历史、回收站与整作用域归档在三级记忆里照常工作。"""

from __future__ import annotations

from support import pkg_history, pkg_memstore, run, thing


def store(tmp_path):
    return pkg_memstore.MemoryStore(tmp_path / "memories").notebook_scope("Bot-A")


def test_every_overwrite_keeps_the_previous_version(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="第一版")))
    run(scope.write("note.md", thing("note", body="第二版")))
    run(scope.edit("note.md", "", "追加", mode="append"))
    versions = scope.history_versions("note.md")
    assert len(versions) >= 2
    assert "第二版" in scope.read_history("note.md", versions[0].version)
    assert "第一版" in scope.read_history("note.md", versions[1].version)


def test_restore_history_version_keeps_current_as_history(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="第一版")))
    run(scope.write("note.md", thing("note", body="第二版")))
    first = scope.history_versions("note.md")[-1]
    report = run(scope.restore_history("note.md", first.version))
    assert report.ok
    assert "第一版" in run(scope.read("note.md"))
    assert "第二版" in scope.read_history("note.md", scope.history_versions("note.md")[0].version)


def test_delete_then_restore_from_trash(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    run(scope.delete("note.md"))
    entry = scope.trash_entries()[0]
    report = run(scope.restore_trash(entry.entry))
    assert report.ok
    assert "正文" in run(scope.read("note.md"))
    assert not scope.trash_entries()


def test_purge_scope_moves_directory_into_archive(tmp_path):
    scope = store(tmp_path)
    run(scope.write("note.md", thing("note", body="正文")))
    removed = run(scope.purge(actor="webui"))
    assert removed == 1
    assert not scope.root.exists()
    archives = tmp_path / "memories" / pkg_history.SCOPE_TRASH_DIRNAME
    assert archives.is_dir()
    assert any(path.is_dir() for path in archives.iterdir())


def test_cleanup_removes_old_trash_and_keeps_recent(tmp_path):
    scope = store(tmp_path)
    scope.set_retention(1, 1)
    run(scope.write("old.md", thing("old", body="旧")))
    run(scope.delete("old.md"))
    old = scope.trash_entries()[0]
    now = old.ts + 10 * 86400
    scope.cleanup_history(now=now, force=True)
    assert not scope.trash_entries()
