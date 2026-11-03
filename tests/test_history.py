"""自动历史、回收站与审计：每条变更路径都能恢复出原内容，备份不干扰检索。"""

import asyncio
import json
import os
import threading

import pytest

from core import history, recall, tidy
from core.archive import ArchiveStore
from core.consolidate import Operation
from core.memstore import (
    CONFLICT_OVERWRITE,
    IMPORT_INVALID,
    INDEX_FILENAME,
    ImportEntry,
    MemoryStore,
    compose_memory,
    safe_key,
)
from core.registry import SessionRegistry
from core.transfer import MemoryTransfer, ScopeRef

UMO = "aiocqhttp:GroupMessage:100200"
KEY = safe_key(UMO)
DAY = 24 * 3600


def run(coro):
    return asyncio.run(coro)


def memory(name, description, body="正文"):
    return compose_memory(name, description, "user", body)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memories").session_scope(UMO)


# ---------------------------------------------------------------- 覆盖与修改


def test_write_overwrite_keeps_old_version_and_restores(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "第一版"), actor="aiocqhttp")
        before = await store.read("a.md")
        await store.write("a.md", memory("a", "甲", "第二版"), actor="aiocqhttp")
        second = await store.read("a.md")
        versions = store.history_versions("a.md")
        assert len(versions) == 1
        assert versions[0].action == history.ACTION_WRITE
        assert versions[0].actor == "aiocqhttp"
        assert store.read_history("a.md", versions[0].version) == before

        report = await store.restore_history("a.md", versions[0].version)
        assert report.ok
        assert await store.read("a.md") == before
        # 恢复前当前在册的内容也进了历史
        assert any(
            store.read_history("a.md", v.version) == second
            for v in store.history_versions("a.md")
        )

    run(scenario())


def test_write_refuses_unreadable_existing_file(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "第一版"), actor="aiocqhttp")
        path = store.root / "a.md"
        path.chmod(0o000)
        try:
            report = await store.write(
                "a.md", memory("a", "甲", "第二版"), actor="aiocqhttp"
            )
        finally:
            path.chmod(0o644)
        assert not report.ok and "读取" in report.message
        assert "第一版" in path.read_text("utf-8")
        assert store.history_versions("a.md") == []

        # 导入覆盖同样不能把读不出来的旧内容直接顶掉
        path.chmod(0o000)
        try:
            results = await store.import_files(
                [ImportEntry("a.md", memory("a", "甲", "导入版"))],
                CONFLICT_OVERWRITE,
            )
        finally:
            path.chmod(0o644)
        assert results[0].outcome == IMPORT_INVALID
        assert "第一版" in path.read_text("utf-8")

    if getattr(os, "geteuid", lambda: -1)() == 0:  # root 不受权限位限制
        pytest.skip("以 root 运行时无法用权限位模拟读失败")
    run(scenario())


def test_edit_keeps_old_version(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "喜欢周六开黑。"), actor="aiocqhttp")
        before = await store.read("a.md")
        report = await store.edit("a.md", "周六", "周日", actor="aiocqhttp")
        assert report.ok
        versions = store.history_versions("a.md")
        assert versions[0].action == history.ACTION_EDIT
        assert store.read_history("a.md", versions[0].version) == before
        await store.restore_history("a.md", versions[0].version)
        assert await store.read("a.md") == before

    run(scenario())


def test_delete_moves_to_trash_and_restores(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "会被删除"), actor="aiocqhttp")
        before = await store.read("a.md")
        assert (await store.delete("a.md", actor="webui")).ok
        assert store.list_files() == []
        entries = store.trash_entries()
        assert len(entries) == 1
        assert entries[0].file == "a.md"
        assert entries[0].action == history.ACTION_DELETE
        assert entries[0].actor == "webui"
        assert store.read_trash(entries[0].entry) == before
        assert store.read_trash("../a.md") is None
        assert store.read_history("a.md", "../x") is None
        assert not (await store.delete_trash("../a.md")).ok

        assert (await store.restore_trash(entries[0].entry)).ok
        assert await store.read("a.md") == before
        assert store.trash_entries() == []
        assert "](a.md)" in (store.root / INDEX_FILENAME).read_text("utf-8")

    run(scenario())


def test_restore_trash_over_active_saves_current_first(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "回收站版本"), actor="aiocqhttp")
        archived = await store.read("a.md")
        await store.delete("a.md", actor="webui")
        await store.write("a.md", memory("a", "甲", "在册版本"), actor="aiocqhttp")
        current = await store.read("a.md")
        entry = next(e for e in store.trash_entries() if e.file == "a.md")
        assert (await store.restore_trash(entry.entry)).ok
        assert await store.read("a.md") == archived
        assert any(
            store.read_history("a.md", v.version) == current
            for v in store.history_versions("a.md")
        )

    run(scenario())


def test_restore_trash_refuses_memory_index(store):
    async def scenario():
        await store.write_index("## 索引\n- [a](a.md) — 甲\n")
        before = (store.root / INDEX_FILENAME).read_text("utf-8")
        entry = "20200101-000000-000__MEMORY.md"
        trash = store.root / history.TRASH_DIRNAME
        trash.mkdir(parents=True, exist_ok=True)
        (trash / entry).write_text("## 被篡改的索引\n", "utf-8")
        report = await store.restore_trash(entry, actor="webui")
        assert not report.ok and "MEMORY.md" in report.message
        assert (store.root / INDEX_FILENAME).read_text("utf-8") == before
        assert store.trash_count() == 1

    run(scenario())


def test_trash_count_matches_list_and_ignores_junk(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲"))
        await store.write("b.md", memory("b", "乙"))
        assert (await store.delete("a.md", actor="webui")).ok
        assert (await store.delete("b.md", actor="webui")).ok
        trash = store.root / history.TRASH_DIRNAME
        (trash / "notes.txt").write_text("不是回收站条目", "utf-8")
        (trash / "99999999-000000-000__bad.md").write_text("坏时间戳", "utf-8")
        assert store.trash_count() == len(store.trash_entries()) == 2

    run(scenario())


def test_write_over_forgotten_name_keeps_forgotten_copy(store):
    async def scenario():
        await store.write("p.md", memory("p", "第一版"), actor="aiocqhttp")
        await store.forget("p.md", actor="webui")
        forgotten = await store.read_forgotten("p.md")
        await store.write("p.md", memory("p", "第二版"), actor="aiocqhttp")
        assert store.forgotten_files() == []
        entries = [e for e in store.trash_entries() if e.file == "p.md"]
        assert len(entries) == 1
        assert entries[0].action == history.ACTION_DISCARD_FORGOTTEN
        assert store.read_trash(entries[0].entry) == forgotten

    run(scenario())


def test_delete_forgotten_keeps_copy_in_trash(store):
    async def scenario():
        await store.write("old.md", memory("old", "旧"), actor="aiocqhttp")
        await store.forget("old.md", actor="webui")
        forgotten = await store.read_forgotten("old.md")
        report = await store.delete_forgotten("old.md", actor="webui")
        assert report.ok and "回收站" in report.message
        entries = [e for e in store.trash_entries() if e.file == "old.md"]
        assert len(entries) == 1
        assert entries[0].action == history.ACTION_DELETE_FORGOTTEN
        assert store.read_trash(entries[0].entry) == forgotten
        assert (await store.restore_trash(entries[0].entry)).ok
        assert await store.read("old.md") == forgotten

    run(scenario())


# ---------------------------------------------------------------- 导入、整理、迁移


def test_import_overwrite_keeps_old_version(store):
    async def scenario():
        await store.write("a.md", memory("a", "现有"), actor="aiocqhttp")
        before = await store.read("a.md")
        results = await store.import_files(
            [ImportEntry("a.md", memory("a", "导入的"))],
            CONFLICT_OVERWRITE,
            actor="transfer",
        )
        assert results[0].landed
        versions = store.history_versions("a.md")
        assert versions[0].action == history.ACTION_IMPORT
        assert versions[0].actor == "transfer"
        assert store.read_history("a.md", versions[0].version) == before
        await store.restore_history("a.md", versions[0].version)
        assert await store.read("a.md") == before

    run(scenario())


def test_import_overwrite_forgotten_copy_goes_to_trash(store):
    async def scenario():
        await store.write("a.md", memory("a", "睡着的旧版"), actor="aiocqhttp")
        await store.forget("a.md", actor="webui")
        forgotten = await store.read_forgotten("a.md")
        results = await store.import_files(
            [ImportEntry("a.md", memory("a", "导入的睡着版"), forgotten=True)],
            CONFLICT_OVERWRITE,
            actor="transfer",
        )
        assert results[0].landed
        entries = [e for e in store.trash_entries() if e.file == "a.md"]
        assert len(entries) == 1 and entries[0].action == history.ACTION_IMPORT
        assert store.read_trash(entries[0].entry) == forgotten
        assert "导入的睡着版" in (await store.read_forgotten("a.md") or "")

    run(scenario())


def test_tidy_apply_and_undo_are_recoverable(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲"), actor="aiocqhttp")
        await store.write("b.md", memory("b", "乙"), actor="aiocqhttp")
        before_a = await store.read("a.md")
        before_b = await store.read("b.md")
        ops = [
            Operation(
                "write",
                "a.md",
                type="user",
                description="甲乙",
                content="合并",
                base_mtime=store.mtime("a.md") or 0.0,
            ),
            Operation("delete", "b.md", base_mtime=store.mtime("b.md") or 0.0),
        ]
        results = await tidy.apply(store, ops, actor="tidy")
        assert all(r["ok"] for r in results)
        assert any(
            store.read_history("a.md", v.version) == before_a
            for v in store.history_versions("a.md")
        )
        assert any(
            store.read_trash(e.entry) == before_b for e in store.trash_entries()
        )
        assert all(v.actor == "tidy" for v in store.history_versions("a.md"))

        record = tidy.make_run(tidy.TRIGGER_AUTO, results, 1_800_000_000.0)
        undone = await tidy.undo(store, record, actor="tidy")
        assert all(r["ok"] for r in undone)
        assert await store.read("a.md") == before_a
        assert await store.read("b.md") == before_b
        # 撤销也是一次覆盖，当前版本同样能在历史里找到
        assert any(
            "合并" in (store.read_history("a.md", v.version) or "")
            for v in store.history_versions("a.md")
        )

    run(scenario())


def test_transfer_move_keeps_source_in_scope_trash(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    registry = SessionRegistry(tmp_path / "sessions.json")
    archive = ArchiveStore(tmp_path / "archive")
    mover = MemoryTransfer(store, registry, archive)
    src = store.session_scope(UMO)
    dst_umo = "qq_new:GroupMessage:100200"
    dst_key = safe_key(dst_umo)

    async def scenario():
        await src.write("a.md", memory("a", "要搬走"), actor="aiocqhttp")
        before = await src.read("a.md")
        report = await mover.transfer(
            ScopeRef("session", KEY),
            ScopeRef("session", dst_key),
            dst_umo=dst_umo,
            mode="move",
        )
        assert report["moved"] == 1
        assert src.list_files() == []
        assert dst_key in store.session_keys()
        entries = list((src.root / history.TRASH_DIRNAME).iterdir())
        assert len(entries) == 1
        assert entries[0].read_text("utf-8") == before

    run(scenario())


# ---------------------------------------------------------------- purge


def test_purge_moves_whole_scope_to_trash_scopes(tmp_path):
    store = MemoryStore(tmp_path / "memories").session_scope(UMO)

    async def scenario():
        await store.write("a.md", memory("a", "甲"), actor="aiocqhttp")
        before = await store.read("a.md")
        assert await store.purge(actor="transfer") == 1
        assert not store.root.exists()
        archived = list((tmp_path / "memories" / ".trash-scopes").iterdir())
        assert len(archived) == 1
        assert archived[0].name.startswith(f"session-{KEY}-")
        assert (archived[0] / "a.md").read_text("utf-8") == before
        assert (archived[0] / INDEX_FILENAME).is_file()
        # 重复 purge 不再报错，也不会产生第二份
        assert await store.purge() == 0
        assert len(list((tmp_path / "memories" / ".trash-scopes").iterdir())) == 1

    run(scenario())


def test_memory_store_returns_one_scope_per_directory_across_threads(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    barrier = threading.Barrier(8)
    seen: list = []

    def fetch():
        barrier.wait()
        seen.append(store.session_scope(UMO))

    threads = [threading.Thread(target=fetch) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({id(scope) for scope in seen}) == 1


def test_memory_store_retention_safe_while_scopes_created(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    errors: list[BaseException] = []

    def create():
        for i in range(300):
            store.session_scope(f"session-{i}")

    def retain():
        try:
            for _ in range(3000):
                store.set_retention(90, 90)
        except BaseException as exc:  # noqa: BLE001 - 记录测试里的并发异常
            errors.append(exc)

    create_thread = threading.Thread(target=create)
    retain_thread = threading.Thread(target=retain)
    create_thread.start()
    retain_thread.start()
    create_thread.join()
    retain_thread.join()
    assert errors == []


def test_memory_store_cleanup_history_covers_all_scopes(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memories")
    session = store.session_scope(UMO)
    people = store.people_scope("aiocqhttp")
    base = 1_800_000_000.0
    monkeypatch.setattr(history, "_now", lambda: base)
    for scope in (session, people):
        for i in range(13):
            run(scope.write("a.md", memory("a", "甲", f"第 {i} 版")))
    assert len(session.history_versions("a.md")) == 12
    assert len(people.history_versions("a.md")) == 12

    store.set_retention(30, 30)
    scopes = store.scopes_for_cleanup()
    removed = store.cleanup_history(now=base + 100 * DAY, scopes=scopes)
    assert sum(item["versions"] for item in removed.values()) == 4
    assert len(session.history_versions("a.md")) == history.MIN_KEEP_VERSIONS
    assert len(people.history_versions("a.md")) == history.MIN_KEEP_VERSIONS


# ---------------------------------------------------------------- 隔离


def test_history_and_trash_never_show_up_as_memory(store):
    async def scenario():
        await store.write("a.md", memory("a", "在册", "绝密旧版本一号"), actor="aiocqhttp")
        await store.write("a.md", memory("a", "在册", "当前版本"), actor="aiocqhttp")
        await store.write("b.md", memory("b", "将被删", "回收站内容二号"), actor="aiocqhttp")
        await store.delete("b.md", actor="webui")
        now = 1_800_000_000.0
        far = now + 100 * DAY
        store.note_used(["a.md"], now=far - DAY)
        assert await store.forget_stale(30 * DAY, far) == []

        assert store.list_files() == ["a.md"]
        assert [h.file for h in store.headers()] == ["a.md"]
        assert all("绝密旧版本一号" not in h.text for h in store.headers())
        assert all("回收站内容二号" not in h.text for h in store.headers())
        assert store.forgotten_files() == []
        index = (store.root / INDEX_FILENAME).read_text("utf-8")
        assert "a.md" in index and "b.md" not in index
        assert "绝密旧版本一号" not in index and "回收站内容二号" not in index
        assert await store.search("绝密旧版本一号") == []
        assert await store.search("回收站内容二号") == []
        assert await store.search("当前版本") == ["a.md: " + "当前版本"]
        # 查重也只看在册文件
        assert recall.similar_memories(
            "绝密旧版本一号 回收站内容二号 绝密旧版本一号",
            store.headers(),
            "c.md",
        ) == []
        report = store.index_report()
        assert report.missing == [] and report.dangling == []
        assert set(store.usage()) == {"a.md"}
        # 历史 / 回收站目录仍原样留着，只是不参与上面的任何扫描
        assert store.history_versions("a.md")
        assert store.trash_entries()

    run(scenario())


# ---------------------------------------------------------------- 保留期


def test_history_cleanup_keeps_latest_ten_versions(store, monkeypatch):
    base = 1_800_000_000.0
    monkeypatch.setattr(history, "_now", lambda: base)
    for i in range(13):
        run(store.write("a.md", memory("a", "甲", f"第 {i} 版")))
    versions = store.history_versions("a.md")
    assert len(versions) == 12

    store.set_retention(30, 0)
    removed = store.cleanup_history(now=base + 100 * DAY, force=True)
    assert removed["versions"] == 2
    kept = store.history_versions("a.md")
    assert len(kept) == history.MIN_KEEP_VERSIONS
    # 留下的是最新的 10 版
    assert kept[0].version == versions[0].version
    assert kept[-1].version == versions[9].version
    # 非 force 的调用被 6 小时间隔挡住
    assert store.cleanup_history(now=base + 100 * DAY) == {}


def test_history_cleanup_zero_keeps_everything(store, monkeypatch):
    base = 1_800_000_000.0
    monkeypatch.setattr(history, "_now", lambda: base)
    for i in range(13):
        run(store.write("a.md", memory("a", "甲", f"第 {i} 版")))
    store.set_retention(0, 0)
    assert store.cleanup_history(now=base + 100 * DAY, force=True) == {
        "versions": 0,
        "trash": 0,
    }
    assert len(store.history_versions("a.md")) == 12


def test_trash_cleanup_respects_keep_days(store, monkeypatch):
    base = 1_800_000_000.0
    monkeypatch.setattr(history, "_now", lambda: base)
    run(store.write("a.md", memory("a", "甲")))
    run(store.write("b.md", memory("b", "乙")))
    run(store.delete("a.md"))
    run(store.delete("b.md"))
    store.set_retention(0, 30)
    removed = store.cleanup_history(now=base + 100 * DAY, force=True)
    assert removed["trash"] == 2
    assert store.trash_entries() == []

    run(store.write("c.md", memory("c", "丙")))
    run(store.delete("c.md"))
    store.set_retention(0, 0)
    assert store.cleanup_history(now=base + 1000 * DAY, force=True)["trash"] == 0
    assert len(store.trash_entries()) == 1


def test_cleanup_waits_for_history_lock(store):
    log = store._history
    started = threading.Event()
    done = threading.Event()

    def worker():
        started.set()
        log.cleanup(1, 1, now=1_900_000_000.0)
        done.set()

    log._lock.acquire()
    try:
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(1)
        # 清理拿不到历史锁就不能开始，避免与正在写版本/移回收站的操作交错
        assert not done.wait(0.1)
    finally:
        log._lock.release()
    thread.join(2)
    assert done.is_set()


def test_malformed_archive_names_do_not_break_listing_or_cleanup(store):
    versions = store.root / history.HISTORY_DIRNAME / "a.md"
    versions.mkdir(parents=True, exist_ok=True)
    bad_version = versions / "99999999-999999-999-write.md"
    bad_version.write_text("坏的版本文件", "utf-8")
    trash = store.root / history.TRASH_DIRNAME
    trash.mkdir(parents=True, exist_ok=True)
    bad_trash = trash / "99999999-999999-999__a.md"
    bad_trash.write_text("坏的回收站文件", "utf-8")

    assert store.history_versions("a.md") == []
    assert store.trash_entries() == []
    store.set_retention(1, 1)
    # 日期非法既不能让清理抛错，也不能被误判成过期删掉
    assert store.cleanup_history(now=1_900_000_000.0, force=True) == {
        "versions": 0,
        "trash": 0,
    }
    assert bad_version.is_file() and bad_trash.is_file()


def test_remove_versions_counts_only_real_deletions(store, monkeypatch):
    run(store.write("a.md", memory("a", "甲", "第一版")))
    run(store.write("a.md", memory("a", "甲", "第二版")))
    log = store._history
    with monkeypatch.context() as patch:
        # 模拟 rmtree 失败：版本文件还在，就不能报告已删除
        patch.setattr(history.shutil, "rmtree", lambda *args, **kwargs: None)
        assert log.remove_versions("a.md") == 0
    assert log.remove_versions("a.md") == 1


# ---------------------------------------------------------------- 彻底删除


def test_delete_file_completely_clears_purged_scope_archives(tmp_path):
    store = MemoryStore(tmp_path / "memories").session_scope(UMO)

    async def scenario():
        await store.write("a.md", memory("a", "甲", "第一版"), actor="aiocqhttp")
        await store.write("a.md", memory("a", "甲", "第二版"), actor="aiocqhttp")
        await store.purge(actor="transfer")
        archived = next((tmp_path / "memories" / ".trash-scopes").iterdir())
        assert (archived / "a.md").is_file()
        assert (archived / history.HISTORY_DIRNAME / "a.md").is_dir()
        archive_index = archived / INDEX_FILENAME
        assert "](a.md)" in archive_index.read_text("utf-8")

        await store.write("a.md", memory("a", "甲", "purge 之后又写的"), actor="aiocqhttp")
        report = await store.delete_file_completely("a.md", actor="webui")
        assert report.ok and "归档副本" in report.message
        assert not (archived / "a.md").exists()
        assert not (archived / history.HISTORY_DIRNAME / "a.md").exists()
        # 归档索引里的描述也要清掉，不能只删正文
        assert "](a.md)" not in archive_index.read_text("utf-8")
        assert store.list_files() == []

    run(scenario())


def test_delete_file_completely_does_not_touch_other_scope_archives(tmp_path):
    store = MemoryStore(tmp_path / "memories")
    first = store.session_scope("a")
    second = store.session_scope("a-b")

    async def scenario():
        for scope in (first, second):
            await scope.write("a.md", memory("a", "甲"), actor="aiocqhttp")
            await scope.purge(actor="transfer")
        archives = list((tmp_path / "memories" / ".trash-scopes").iterdir())
        assert len(archives) == 2

        assert (await first.delete_file_completely("a.md", actor="webui")).ok
        remaining = [p for p in archives if (p / "a.md").is_file()]
        assert len(remaining) == 1
        assert remaining[0].name.startswith("session-a-b-")

    run(scenario())


def test_delete_file_completely_removes_history_and_trash(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "第一版"), actor="aiocqhttp")
        await store.write("a.md", memory("a", "甲", "第二版"), actor="aiocqhttp")
        await store.delete("a.md", actor="webui")
        await store.write("b.md", memory("b", "留着"), actor="aiocqhttp")
        assert store.history_versions("a.md") and store.trash_entries()

        report = await store.delete_file_completely("a.md", actor="webui")
        assert report.ok
        assert store.history_versions("a.md") == []
        assert [e for e in store.trash_entries() if e.file == "a.md"] == []
        assert store.list_files() == ["b.md"]
        assert not (store.root / history.HISTORY_DIRNAME / "a.md").exists()
        assert not (await store.delete_file_completely("a.md")).ok
        # 另一个文件的备份不受影响
        assert await store.read("b.md") is not None

    run(scenario())


def test_delete_file_completely_removes_forgotten_copy(store):
    async def scenario():
        await store.write("f.md", memory("f", "忘掉我"), actor="aiocqhttp")
        await store.forget("f.md", actor="webui")
        assert store.forgotten_files() == ["f.md"]
        assert (await store.delete_file_completely("f.md", actor="webui")).ok
        assert store.forgotten_files() == []
        assert store.list_files() == []
        assert not (store.root / "forgotten").exists()

    run(scenario())


def test_delete_file_completely_removes_unreadable_active(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲"), actor="aiocqhttp")
        path = store.root / "a.md"
        path.chmod(0o000)
        try:
            report = await store.delete_file_completely("a.md", actor="webui")
        finally:
            if path.exists():
                path.chmod(0o644)
        assert report.ok
        assert not path.exists()

    if getattr(os, "geteuid", lambda: -1)() == 0:
        pytest.skip("以 root 运行时无法用权限位模拟读失败")
    run(scenario())


# ---------------------------------------------------------------- 审计


@pytest.mark.parametrize(
    "name, expected",
    [
        ("a-backup.md", True),
        ("user-1.bak.md", True),
        ("备份.md", True),
        ("副本.md", True),
        ("zz-草稿.md", True),
        ("user-1.md", False),
        ("project-notes.md", False),
    ],
)
def test_looks_like_manual_backup(name, expected):
    assert history.looks_like_manual_backup(name) is expected


def test_restore_audit_records_actor(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲"), actor="aiocqhttp")
        await store.forget("a.md", actor="webui")
        assert (await store.restore("a.md", actor="aiocqhttp")).ok
        rows = [
            json.loads(line)
            for line in (store.root / history.AUDIT_FILENAME)
            .read_text("utf-8")
            .splitlines()
        ]
        assert rows[-1]["action"] == history.ACTION_RESTORE
        assert rows[-1]["actor"] == "aiocqhttp"

    run(scenario())


def test_audit_records_every_change(store):
    async def scenario():
        await store.write("a.md", memory("a", "甲", "第一版"), source="10001", actor="aiocqhttp")
        first = await store.read("a.md")
        await store.write("a.md", memory("a", "甲", "第二版"), source="10001", actor="aiocqhttp")
        await store.edit("a.md", "第二版", "改过", source="10001", actor="aiocqhttp")
        await store.delete("a.md", actor="webui")
        rows = [
            json.loads(line)
            for line in (store.root / history.AUDIT_FILENAME)
            .read_text("utf-8")
            .splitlines()
        ]
        assert [row["action"] for row in rows] == [
            history.ACTION_WRITE,
            history.ACTION_WRITE,
            history.ACTION_EDIT,
            history.ACTION_DELETE,
        ]
        assert rows[0]["before_sha1"] == "" and rows[0]["after_sha1"]
        assert rows[1]["before_sha1"] == history.content_sha1(first)
        assert rows[1]["actor"] == "aiocqhttp" and rows[1]["source"] == "10001"
        for row in rows:
            assert set(row) == {
                "ts",
                "action",
                "file",
                "actor",
                "source",
                "before_sha1",
                "after_sha1",
            }

    run(scenario())
