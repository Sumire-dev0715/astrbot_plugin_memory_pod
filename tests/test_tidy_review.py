"""整理审查修复：批外操作、跨批冲突、超 60 项方案、空/非 UTF-8 文件、
任务 ID 隔离、取消排队任务、彻底删除后的方案残留。

全部使用合成数据与确定性假模型。
"""

from __future__ import annotations

import asyncio
import json

import test_web_api as tw
from support import import_core, pkg_tidy, run, thing

consolidate = import_core("consolidate")
insights = import_core("insights")
memstore = import_core("memstore")


def setup(tmp_path):
    return tw.setup(tmp_path)


def write(store, name, body="合成正文", mem_type="note", description="合成记忆"):
    run(store.write(name, thing(name[:-3], description=description, body=body, mem_type=mem_type)))


def replies(*items):
    queue = list(items)

    async def model(prompt, selected=None):
        return queue.pop(0), []

    return model


def apply_all(store, proposal):
    return run(pkg_tidy.apply(store, proposal.operations))


# ------------------------------------------------------------ 批外操作与跨批冲突


def test_out_of_batch_delete_is_dropped(tmp_path, monkeypatch):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md", body="x" * 700)
    write(store, "b.md", body="x" * 700)
    monkeypatch.setattr(consolidate, "MAX_INPUT_CHARS", 1000)

    proposal = run(
        pkg_tidy.propose(
            store,
            "小本本",
            replies(
                json.dumps([{"op": "delete", "file": "b.md", "reason": "批外删除"}]),
                "[]",
            ),
            budget=1000,
        )
    )
    assert [op.file for op in proposal.operations] == []
    assert "b.md" in proposal.skipped
    assert any(item["kind"] == "out_of_batch" for item in proposal.notices)
    apply_all(store, proposal)
    assert run(store.read("b.md")) is not None


def test_out_of_batch_write_blocks_same_batch_delete(tmp_path, monkeypatch):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md", body="x" * 700)
    write(store, "b.md", body="x" * 700)
    monkeypatch.setattr(consolidate, "MAX_INPUT_CHARS", 1000)

    proposal = run(
        pkg_tidy.propose(
            store,
            "小本本",
            replies(
                json.dumps(
                    [
                        {
                            "op": "write",
                            "file": "b.md",
                            "type": "note",
                            "description": "模型没读过 b 的正文",
                            "content": "凭摘要改写",
                        },
                        {"op": "delete", "file": "a.md", "reason": "已并入 b"},
                    ]
                ),
                "[]",
            ),
            budget=1000,
        )
    )
    assert [op.file for op in proposal.operations] == []
    apply_all(store, proposal)
    assert "x" * 700 in run(store.read("a.md"))
    assert "凭摘要改写" not in run(store.read("b.md"))


def test_cross_batch_same_target_does_not_lose_sources(tmp_path, monkeypatch):
    """两批各自往同一个新文件合并：写都被丢弃，两批的删除也必须放弃。"""
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "user-123-likes.md", body="围棋" * 300, mem_type="person")
    write(store, "user-123-work.md", body="后端" * 300, mem_type="person")
    monkeypatch.setattr(consolidate, "MAX_INPUT_CHARS", 1000)

    proposal = run(
        pkg_tidy.propose(
            store,
            "小本本",
            replies(
                json.dumps(
                    [
                        {
                            "op": "write",
                            "file": "user-123.md",
                            "type": "person",
                            "description": "合并档案",
                            "content": "围棋",
                        },
                        {"op": "delete", "file": "user-123-likes.md", "reason": "已合并"},
                    ]
                ),
                json.dumps(
                    [
                        {
                            "op": "write",
                            "file": "user-123.md",
                            "type": "person",
                            "description": "合并档案",
                            "content": "后端",
                        },
                        {"op": "delete", "file": "user-123-work.md", "reason": "已合并"},
                    ]
                ),
            ),
            budget=1000,
        )
    )
    assert [op.file for op in proposal.operations] == []
    assert run(store.read("user-123.md")) is None
    results = apply_all(store, proposal)
    assert all(item["ok"] for item in results)
    assert "围棋" in run(store.read("user-123-likes.md"))
    assert "后端" in run(store.read("user-123-work.md"))


# ------------------------------------------------------------ 操作条数上限


def test_page_payload_is_not_cut_at_max_operations():
    entries = [
        {
            "op": "write",
            "file": f"n{index}.md",
            "type": "note",
            "description": "合成",
            "content": "正文",
        }
        for index in range(consolidate.MAX_OPERATIONS + 5)
    ]
    model_ops = consolidate.parse_operations(json.dumps(entries), set())
    assert len(model_ops) == consolidate.MAX_OPERATIONS
    page_ops = consolidate.operations_from_payload(entries, set())
    assert len(page_ops) == consolidate.MAX_OPERATIONS + 5


def test_apply_all_auto_fixes_beyond_sixty(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    for index in range(consolidate.MAX_OPERATIONS + 5):
        write(store, f"f{index}.md", body=f"见 [[missing-{index}]]")

    async def model(prompt, selected=None):
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        tw.REQUEST.body = {"scope": "notebook", "key": "Bot-A"}
        tw.REQUEST.query = {}
        started = await dashboard._wrap(dashboard.tidy_start)()
        assert started["status"] == "ok", started
        await dashboard.wait_background()
        tw.REQUEST.query = {"scope": "notebook", "key": "Bot-A"}
        tw.REQUEST.body = None
        task = (await dashboard._wrap(dashboard.tidy_task)())["data"]["task"]
        assert len(task["plan"]["operations"]) == consolidate.MAX_OPERATIONS + 5
        tw.REQUEST.body = {
            "scope": "notebook",
            "key": "Bot-A",
            "operations": task["plan"]["operations"],
        }
        applied = await dashboard._wrap(dashboard.consolidate_apply)()
        assert applied["status"] == "ok", applied
        assert all(item["ok"] for item in applied["data"]["results"])
        contents = {
            name: await store.read(name)
            for name in await asyncio.to_thread(store.list_files)
        }
        available = set(contents)
        for name, content in contents.items():
            doc = memstore.parse_memory(content)
            missing = insights.dangling_links(
                memstore.extract_links(doc.body), available
            )
            assert not missing, (name, missing)

    run(scenario())


# ------------------------------------------------------------ 空文件与非 UTF-8


def test_empty_and_non_utf8_files_are_not_dangling(tmp_path):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "note.md", body="见 [[empty]] 和 [[binary]]")
    (store.root / "empty.md").write_text("", encoding="utf-8")
    (store.root / "binary.md").write_bytes(b"\xff\xfe\x00not-utf8")

    proposal = run(pkg_tidy.propose(store, "小本本", replies("[]")))

    assert proposal.files == ["binary.md", "empty.md", "note.md"]
    batch_files = [name for batch in proposal.batches for name in batch.files]
    assert "empty.md" in batch_files
    assert "binary.md" not in batch_files
    assert any(item["kind"] == "unreadable" for item in proposal.notices)
    # 两个真实存在的文件不能被当成悬空链接
    assert [op.file for op in proposal.operations if op.op == "fix"] == []
    apply_all(store, proposal)
    assert run(store.read("note.md")).count("[[empty]]") == 1


# ------------------------------------------------------------ 任务 ID 隔离


def test_update_task_ignores_stale_task_id(tmp_path):
    book = pkg_tidy.TidyBook(tmp_path / "tidy")
    sid = "notebook-Bot-A"
    first = pkg_tidy.new_task({"providerId": "p", "model": "m"}, now=1.0)
    second = pkg_tidy.new_task({"providerId": "p", "model": "m"}, now=2.0)
    book.create_task(sid, first)
    book.create_task(sid, second)

    def change(task):
        task["message"] = "旧实例"

    book.update_task(sid, change, task_id=first["id"])
    assert book.task(sid)["message"] == ""
    book.update_task(sid, change, task_id=second["id"])
    assert book.task(sid)["message"] == "旧实例"


def test_run_task_with_stale_task_id_does_not_touch_new_task(tmp_path):
    plugin, _ = setup(tmp_path)
    book = plugin._tidy
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md")
    old = pkg_tidy.new_task({"providerId": "p", "model": "m"}, now=1.0)
    new = pkg_tidy.new_task({"providerId": "p", "model": "m"}, now=2.0)
    book.create_task("notebook-Bot-A", new)
    calls = []

    async def model(prompt):
        calls.append(prompt)
        return "[]", []

    result = run(
        pkg_tidy.run_task(
            book,
            "notebook-Bot-A",
            store,
            "小本本",
            model,
            task_id=old["id"],
        )
    )
    assert result["id"] == new["id"]
    assert result["state"] == pkg_tidy.TASK_RUNNING
    assert result["plan"] == {}
    assert calls == []


def test_runner_stop_cancels_and_releases_pending(tmp_path):
    runner = pkg_tidy.TidyRunner(max_active=1)
    drops = []
    started = asyncio.Event()

    async def work():
        started.set()
        await asyncio.Event().wait()

    def drop(name):
        return lambda: drops.append(name)

    async def scenario():
        assert runner.schedule("a", work, on_drop=drop("a")) is True
        await started.wait()
        assert runner.schedule("b", work, on_drop=drop("b")) is False
        assert runner.pending_count() == 1
        await runner.stop()
        assert runner.active_count() == 0
        assert runner.pending_count() == 0
        assert sorted(drops) == ["a", "b"]

    run(scenario())


# ------------------------------------------------------------ 取消排队任务


def test_cancel_queued_task_releases_scope(tmp_path):
    plugin, dashboard = setup(tmp_path)
    for key in ("Bot-A", "Bot-B", "Bot-C"):
        write(plugin._store.notebook_scope(key), "a.md")
    release = asyncio.Event()

    async def model(prompt, selected=None):
        await release.wait()
        return "[]", []

    plugin.call_tidy_model = model

    async def start(key):
        tw.REQUEST.body = {"scope": "notebook", "key": key}
        tw.REQUEST.query = {}
        return await dashboard._wrap(dashboard.tidy_start)()

    async def cancel(key):
        tw.REQUEST.body = {"scope": "notebook", "key": key}
        return await dashboard._wrap(dashboard.tidy_cancel)()

    async def scenario():
        for key in ("Bot-A", "Bot-B", "Bot-C"):
            assert (await start(key))["status"] == "ok"
        sid = "notebook-Bot-C"
        assert dashboard.tidy_runner.pending_count() == 1
        result = await cancel("Bot-C")
        assert result["status"] == "ok", result
        assert dashboard.tidy_runner.pending_count() == 0
        # 取消后作用域锁立刻释放，可以重新整理
        assert plugin._tidy.claim(sid) is True
        plugin._tidy.release(sid)
        restarted = await start("Bot-C")
        assert restarted["status"] == "ok", restarted
        release.set()
        await dashboard.wait_background()
        tw.REQUEST.query = {"scope": "notebook", "key": "Bot-C"}
        tw.REQUEST.body = None
        task = (await dashboard._wrap(dashboard.tidy_task)())["data"]["task"]
        assert task["state"] == pkg_tidy.TASK_READY

    run(scenario())


# ------------------------------------------------------------ 彻底删除与方案残留


def test_file_purge_removes_content_from_task_plan(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "secret.md", body="绝密正文-abc")

    async def model(prompt, selected=None):
        return json.dumps(
            [
                {
                    "op": "write",
                    "file": "secret.md",
                    "type": "note",
                    "description": "合成",
                    "content": "整理后的绝密正文-xyz",
                    "reason": "改写",
                }
            ]
        ), []

    plugin.call_tidy_model = model

    async def scenario():
        tw.REQUEST.body = {"scope": "notebook", "key": "Bot-A"}
        tw.REQUEST.query = {}
        assert (await dashboard._wrap(dashboard.tidy_start)())["status"] == "ok"
        await dashboard.wait_background()
        tw.REQUEST.query = {"scope": "notebook", "key": "Bot-A"}
        tw.REQUEST.body = None
        task = (await dashboard._wrap(dashboard.tidy_task)())["data"]["task"]
        assert "绝密正文-xyz" in json.dumps(task, ensure_ascii=False)
        tw.REQUEST.body = {
            "scope": "notebook",
            "key": "Bot-A",
            "file": "secret.md",
        }
        purged = await dashboard._wrap(dashboard.file_purge)()
        assert purged["status"] == "ok", purged
        assert plugin._tidy.task("notebook-Bot-A") is None
        for path in (tmp_path / "tidy").glob("*.json"):
            assert "绝密正文" not in path.read_text("utf-8"), path.name

    run(scenario())


def test_file_purge_refuses_while_tidy_running(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "secret.md")
    task = pkg_tidy.new_task({"providerId": "p", "model": "m"})
    plugin._tidy.create_task("notebook-Bot-A", task)
    tw.REQUEST.body = {
        "scope": "notebook",
        "key": "Bot-A",
        "file": "secret.md",
    }
    tw.REQUEST.query = {}
    result = run(dashboard._wrap(dashboard.file_purge)())
    assert result["status"] == "error"
    assert "正在整理中" in result["message"]
    assert run(store.read("secret.md")) is not None


# ------------------------------------------------------------ 详情不再带任务大 payload


def test_scope_detail_does_not_return_tidy_task(tmp_path):
    plugin, dashboard = setup(tmp_path)
    write(plugin._store.notebook_scope("Bot-A"), "a.md")
    task = pkg_tidy.new_task({"providerId": "p", "model": "m"})
    task["state"] = pkg_tidy.TASK_READY
    task["plan"] = {"operations": [], "files": []}
    plugin._tidy.create_task("notebook-Bot-A", task)
    tw.REQUEST.query = {"scope": "notebook", "key": "Bot-A"}
    tw.REQUEST.body = None
    data = tw.raw(tw.route(dashboard, "scope_detail"))
    assert "task" not in data


def test_runner_loop_change_releases_dropped_pending_claims():
    runner = pkg_tidy.TidyRunner(max_active=2)
    drops = []

    async def work():
        return None

    async def scenario():
        runner._pending = [
            ("b", work, lambda: drops.append("b")),
            ("c", work, lambda: drops.append("c")),
        ]
        runner._loop = object()  # 模拟上一个请求循环
        assert runner.schedule("a", work, on_drop=lambda: drops.append("a")) is True
        assert sorted(drops) == ["b", "c"]
        assert runner.pending_count() == 0
        assert runner.active_count() == 1
        await runner.stop()

    run(scenario())
