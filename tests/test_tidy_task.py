"""整理任务：后台执行、状态持久化、分批覆盖、顺序轮换与悬空链接自动修复。

模型用确定性的假实现，时钟可替换；所有记忆都是合成数据。
"""

from __future__ import annotations

import asyncio
import json

import pytest

import test_web_api as tw
from support import import_core, pkg_tidy, run, thing

consolidate = import_core("consolidate")
insights = import_core("insights")
memstore = import_core("memstore")


def setup(tmp_path):
    return tw.setup(tmp_path)


def write(store, name, body="合成正文", mem_type="note", description="合成记忆"):
    run(store.write(name, thing(name[:-3], description=description, body=body, mem_type=mem_type)))


async def start(dashboard, scope, key):
    tw.REQUEST.body = {"scope": scope, "key": key}
    tw.REQUEST.query = {}
    return await dashboard._wrap(dashboard.tidy_start)()


async def get_task(dashboard, scope, key):
    tw.REQUEST.query = {"scope": scope, "key": key}
    tw.REQUEST.body = None
    result = await dashboard._wrap(dashboard.tidy_task)()
    assert result["status"] == "ok", result
    return result["data"]["task"]


def test_start_returns_before_model_finishes_and_exposes_running_state(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def model(prompt, selected=None):
        entered.set()
        await release.wait()
        return "[]", []

    plugin.call_tidy_model = model
    clock = [1000.0]
    original = pkg_tidy._now
    pkg_tidy._now = lambda: clock[0]
    try:
        async def scenario():
            result = await start(dashboard, "notebook", "Bot-A")
            assert result["status"] == "ok", result
            assert result["data"]["taskId"]
            await asyncio.wait_for(entered.wait(), timeout=2)
            task = await get_task(dashboard, "notebook", "Bot-A")
            assert task["state"] == "running"
            assert task["startedAt"] == 1000.0
            assert task["model"] == {"providerId": "provider-a", "model": "model-a"}
            # 读取与分批在调用模型之前完成，所以这里能看到共几批
            assert task["fileCount"] == 1
            assert task["batchCount"] == 1
            release.set()
            await dashboard.wait_background()
            task = await get_task(dashboard, "notebook", "Bot-A")
            assert task["state"] == "ready"
            assert task["fileCount"] == 1
            assert task["batchCount"] == 1
            assert task["batchesDone"] == 1

        run(scenario())
    finally:
        pkg_tidy._now = original


def test_cancel_keeps_finished_batches(tmp_path, monkeypatch):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md", body="甲" * 700)
    write(store, "b.md", body="乙" * 700)
    write(store, "c.md", body="丙" * 700)
    # 每批只放一个文件：第一批成功，第二批停下等人取消
    monkeypatch.setattr(consolidate, "MAX_INPUT_CHARS", 1000)
    clock = [2000.0]

    monkeypatch.setattr(pkg_tidy, "_now", lambda: clock[0])
    first_done = asyncio.Event()
    second_entered = asyncio.Event()
    release = asyncio.Event()
    calls = {"n": 0}

    async def model(prompt, selected=None):
        calls["n"] += 1
        if calls["n"] == 1:
            clock[0] = 2001.0
            first_done.set()
            return "[]", []
        second_entered.set()
        await release.wait()
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        result = await start(dashboard, "notebook", "Bot-A")
        assert result["status"] == "ok", result
        await asyncio.wait_for(second_entered.wait(), timeout=2)
        task = await get_task(dashboard, "notebook", "Bot-A")
        assert task["state"] == "running"
        assert task["batchCount"] == 3
        assert task["batchesDone"] == 1
        assert task["batches"][0]["state"] == "done"
        # 取消：已完成的批次保留，方案还在
        tw.REQUEST.body = {"scope": "notebook", "key": "Bot-A"}
        cancelled = await dashboard._wrap(dashboard.tidy_cancel)()
        assert cancelled["status"] == "ok", cancelled
        assert cancelled["data"]["task"]["state"] == "cancelled"
        assert cancelled["data"]["task"]["batchesDone"] == 1
        assert cancelled["data"]["task"]["plan"]["files"]
        release.set()
        await dashboard.wait_background()
        task = await get_task(dashboard, "notebook", "Bot-A")
        assert task["state"] == "cancelled"
        assert task["batchesDone"] == 1
        assert task["plan"]["files"]

    run(scenario())


def test_restart_marks_running_task_interrupted_and_keeps_plan(tmp_path):
    plugin, _ = setup(tmp_path)
    book = plugin._tidy
    task = pkg_tidy.new_task({"providerId": "provider-a", "model": "model-a"}, now=10.0)
    task["batchCount"] = 3
    task["batchesDone"] = 1
    task["batches"] = [
        {"index": 1, "files": ["a.md"], "state": "done", "error": ""},
        {"index": 2, "files": ["b.md"], "state": "pending", "error": ""},
    ]
    task["plan"] = {"operations": [{"op": "delete", "file": "a.md"}], "files": ["a.md", "b.md"]}
    book.create_task("notebook-Bot-A", task)
    assert book.mark_interrupted(now=20.0) == 1
    stored = book.task("notebook-Bot-A")
    assert stored["state"] == "interrupted"
    assert stored["message"] == "插件重启，整理已中断"
    assert stored["updatedAt"] == 20.0
    assert stored["plan"]["operations"] == [{"op": "delete", "file": "a.md"}]
    assert stored["batchesDone"] == 1


def test_second_tidy_orders_by_last_participation_time(tmp_path, monkeypatch):
    plugin, _ = setup(tmp_path)
    book = plugin._tidy
    store = plugin._store.notebook_scope("Bot-A")
    for name in ("a.md", "b.md", "c.md", "d.md"):
        write(store, name, body=name * 200)
    monkeypatch.setattr(consolidate, "MAX_INPUT_CHARS", 1000)
    clock = [0.0]
    monkeypatch.setattr(pkg_tidy, "_now", lambda: clock[0])

    async def model(prompt, selected=None):
        clock[0] += 10
        return "[]", []

    async def one_run():
        task = pkg_tidy.new_task(
            {"providerId": "provider-a", "model": "model-a"}, now=clock[0]
        )
        book.create_task("notebook-Bot-A", task)
        await pkg_tidy.run_task(
            book,
            "notebook-Bot-A",
            store,
            "小本本",
            model,
            now_fn=lambda: clock[0],
        )

    run(one_run())
    first = book.task("notebook-Bot-A")
    assert first["state"] == "ready"
    assert first["batchCount"] == 4
    first_batches = [batch["files"] for batch in first["batches"]]
    times = book.file_times("notebook-Bot-A")
    assert sorted(first_batches[0]) == [name for name, value in sorted(times.items(), key=lambda item: item[1])[:1]]

    # 第二次按上次参与整理的时间从早到晚：顺序与第一次一致（每批一个文件）
    run(one_run())
    second = book.task("notebook-Bot-A")
    second_batches = [batch["files"] for batch in second["batches"]]
    expected = [
        [name]
        for name, _ in sorted(book.file_times("notebook-Bot-A").items(), key=lambda item: item[1])
    ]
    assert second_batches == expected

    # 把 d 的时间改早（比如它在另一处更早参与过整理）后，下一轮它排到最前
    book.touch_files("notebook-Bot-A", ["d.md"], now=0.5)
    run(one_run())
    third = book.task("notebook-Bot-A")
    assert third["batches"][0]["files"] == ["d.md"]


def test_batches_cover_every_file_without_overlap(tmp_path, monkeypatch):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    sizes = {"a.md": 25000, "b.md": 25000, "c.md": 25000, "d.md": 25000}
    for name, size in sizes.items():
        write(store, name, body="x" * size)
    prompts: list[str] = []

    async def model(prompt, selected=None):
        prompts.append(prompt)
        return "[]", []

    proposal = run(
        pkg_tidy.propose(
            store,
            "小本本",
            model,
            file_times={},
            budget=consolidate.MAX_INPUT_CHARS,
        )
    )
    assert len(proposal.batches) >= 2
    seen: list[str] = []
    for batch in proposal.batches:
        for file in batch.files:
            assert file not in seen
            seen.append(file)
    assert sorted(seen) == sorted(sizes)
    # 每批正文合计不超过预算
    for batch in proposal.batches:
        total = sum(sizes[name] for name in batch.files)
        assert total <= consolidate.MAX_INPUT_CHARS
    assert len(prompts) == len(proposal.batches)
    # 每批提示词都带这一处全部文件的清单，但不带批外正文
    for prompt in prompts:
        for name in sizes:
            assert name in prompt
        assert prompt.count("=== ") == len(
            next(batch.files for batch in proposal.batches if batch.index == prompts.index(prompt) + 1)
        )


def test_single_file_over_budget_is_marked_for_split(tmp_path):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "small.md", body="短")
    write(store, "huge.md", body="x" * (consolidate.MAX_INPUT_CHARS + 10))

    async def model(prompt, selected=None):
        return "[]", []

    proposal = run(pkg_tidy.propose(store, "小本本", model, file_times={}))
    assert proposal.needs_split == ["huge.md"]
    assert "huge.md" not in [name for batch in proposal.batches for name in batch.files]
    assert proposal.notices[0]["kind"] == "needs_split"
    assert proposal.notices[0]["file"] == "huge.md"
    assert proposal.files == ["huge.md", "small.md"]


def test_order_puts_flagged_then_never_tidied_then_oldest(tmp_path):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "dangling.md", body="见 [[missing]]")
    write(store, "long.md", body="长" * (9 * 1024))
    write(store, "fresh.md", body="新")
    write(store, "old.md", body="旧")
    write(store, "newer.md", body="较新")

    async def model(prompt, selected=None):
        return "[]", []

    proposal = run(
        pkg_tidy.propose(
            store,
            "小本本",
            model,
            file_times={"old.md": 10.0, "newer.md": 20.0},
        )
    )
    assert proposal.batches[0].files == [
        "dangling.md",
        "long.md",
        "fresh.md",
        "old.md",
        "newer.md",
    ]


def test_auto_fix_case_insensitive_unique_and_plain_text(tmp_path):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "user-123.md", body="人物")
    write(store, "note.md", body="见 [[User-123]]")
    write(store, "other.md", body="见 [[gone]]")

    async def model(prompt, selected=None):
        return "[]", []

    proposal = run(pkg_tidy.propose(store, "小本本", model, file_times={}))
    fixes = {op.file: op for op in proposal.operations if op.op == "fix"}
    assert fixes["note.md"].content == "见 [[user-123]]"
    assert fixes["other.md"].content == "见 gone"
    assert all(op.auto for op in fixes.values())
    assert all(op.depends_on == "" for op in fixes.values())


def test_auto_fix_is_skipped_when_model_already_fixed_it(tmp_path):
    plugin, _ = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "user-123.md", body="人物")
    write(store, "note.md", body="见 [[User-123]]")

    async def model(prompt, selected=None):
        return json.dumps(
            [
                {
                    "op": "write",
                    "file": "note.md",
                    "type": "note",
                    "description": "合成记忆",
                    "content": "见 [[user-123]]",
                    "reason": "修链接",
                }
            ]
        ), []

    proposal = run(pkg_tidy.propose(store, "小本本", model, file_times={}))
    assert [op.file for op in proposal.operations if op.op == "fix"] == []


def test_apply_all_fixes_leaves_no_dangling_links(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "user-123.md", body="人物")
    write(store, "note.md", body="见 [[User-123]]")
    write(store, "other.md", body="见 [[gone]]")

    async def model(prompt, selected=None):
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        result = await start(dashboard, "notebook", "Bot-A")
        assert result["status"] == "ok", result
        await dashboard.wait_background()
        task = await get_task(dashboard, "notebook", "Bot-A")
        assert task["state"] == "ready"
        fix_ops = [op for op in task["plan"]["operations"] if op["op"] == "fix"]
        assert len(fix_ops) == 2
        tw.REQUEST.body = {
            "scope": "notebook",
            "key": "Bot-A",
            "operations": task["plan"]["operations"],
        }
        applied = await dashboard._wrap(dashboard.consolidate_apply)()
        assert applied["status"] == "ok", applied
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
        # 应用以后方案清掉
        assert await get_task(dashboard, "notebook", "Bot-A") is None

    run(scenario())


def test_unchecked_auto_fix_is_not_applied(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "note.md", body="见 [[missing]]")

    async def model(prompt, selected=None):
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        assert (await start(dashboard, "notebook", "Bot-A"))["status"] == "ok"
        await dashboard.wait_background()
        task = await get_task(dashboard, "notebook", "Bot-A")
        tw.REQUEST.body = {
            "scope": "notebook",
            "key": "Bot-A",
            "operations": [],  # 取消勾选自动修复
        }
        result = await dashboard._wrap(dashboard.consolidate_apply)()
        assert result["status"] == "error"
        content = await store.read("note.md")
        assert "[[missing]]" in content

    run(scenario())


def test_auto_fix_depends_on_model_write(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "note.md", body="旧正文")

    async def model(prompt, selected=None):
        return json.dumps(
            [
                {
                    "op": "write",
                    "file": "note.md",
                    "type": "note",
                    "description": "合成记忆",
                    "content": "新正文 [[gone]]",
                    "reason": "改写",
                }
            ]
        ), []

    plugin.call_tidy_model = model

    async def scenario():
        assert (await start(dashboard, "notebook", "Bot-A"))["status"] == "ok"
        await dashboard.wait_background()
        task = await get_task(dashboard, "notebook", "Bot-A")
        fix = next(op for op in task["plan"]["operations"] if op["op"] == "fix")
        assert fix["dependsOn"] == "note.md"
        # 只应用自动修复、不应用它依据的改写：必须跳过，不能把模型内容写进去
        tw.REQUEST.body = {
            "scope": "notebook",
            "key": "Bot-A",
            "operations": [fix],
        }
        result = await dashboard._wrap(dashboard.consolidate_apply)()
        assert result["status"] == "ok", result
        assert "旧正文" in await store.read("note.md")
        messages = [item["message"] for item in result["data"]["results"]]
        assert any("没有应用" in message for message in messages)

    run(scenario())


def test_tidy_requires_selected_model(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md")
    plugin._tidy.remove("notebook-Bot-A")  # 不影响文件；这里只清整理记录
    (tmp_path / "tidy" / "model.json").unlink()
    tw.REQUEST.body = {"scope": "notebook", "key": "Bot-A"}
    tw.REQUEST.query = {}
    result = run(dashboard._wrap(dashboard.tidy_start)())
    assert result["status"] == "error"
    assert "选择" in result["message"]


def test_unavailable_selected_model_is_reported_and_refused(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md")
    plugin.tidy_models = []
    tw.REQUEST.query = {}
    tw.REQUEST.body = None
    models = run(dashboard._wrap(dashboard.tidy_models)())
    assert models["status"] == "ok"
    assert models["data"]["selected"]["available"] is False
    tw.REQUEST.body = {"scope": "notebook", "key": "Bot-A"}
    result = run(dashboard._wrap(dashboard.tidy_start)())
    assert result["status"] == "error"
    assert "不可用" in result["message"]


def test_model_selection_is_persisted_and_listed(tmp_path):
    plugin, dashboard = setup(tmp_path)
    tw.REQUEST.body = {"providerId": "provider-a"}
    tw.REQUEST.query = {}
    result = run(dashboard._wrap(dashboard.tidy_model_select)())
    assert result["status"] == "ok", result
    assert result["data"]["selected"] == {
        "providerId": "provider-a",
        "model": "model-a",
        "available": True,
    }
    assert pkg_tidy.TidyBook(tmp_path / "tidy").selected_model() == {
        "providerId": "provider-a",
        "model": "model-a",
    }


def test_tasks_list_includes_all_scopes(tmp_path):
    plugin, dashboard = setup(tmp_path)
    write(plugin._store.notebook_scope("Bot-A"), "a.md")
    tasks = [
        pkg_tidy.new_task({"providerId": "provider-a", "model": "model-a"}),
        pkg_tidy.new_task({"providerId": "provider-a", "model": "model-a"}),
    ]
    tasks[0]["state"] = pkg_tidy.TASK_READY
    tasks[1]["state"] = pkg_tidy.TASK_INTERRUPTED
    plugin._tidy.create_task("board", tasks[0])
    plugin._tidy.create_task("whisper-abc", tasks[1])
    tw.REQUEST.query = {}
    tw.REQUEST.body = None
    result = run(dashboard._wrap(dashboard.tidy_tasks)())
    assert result["status"] == "ok"
    got = {item["scope"]: item for item in result["data"]["tasks"]}
    assert got["board"]["state"] == "ready"
    assert got["whisper"]["key"] == "abc"
    assert got["whisper"]["state"] == "interrupted"


def test_at_most_two_tasks_run_at_once(tmp_path):
    plugin, dashboard = setup(tmp_path)
    for key in ("Bot-A", "Bot-B", "Bot-C"):
        write(plugin._store.notebook_scope(key), "a.md")
    release = asyncio.Event()
    entered: list[str] = []

    async def model(prompt, selected=None):
        entered.append(prompt)
        await release.wait()
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        for key in ("Bot-A", "Bot-B", "Bot-C"):
            result = await start(dashboard, "notebook", key)
            assert result["status"] == "ok", result
        await asyncio.sleep(0)
        assert dashboard.tidy_runner.active_count() == 2
        assert dashboard.tidy_runner.pending_count() == 1
        queued = await get_task(dashboard, "notebook", "Bot-C")
        assert queued["queued"] is True
        assert queued["state"] == "running"
        release.set()
        await dashboard.wait_background()
        for key in ("Bot-A", "Bot-B", "Bot-C"):
            assert (await get_task(dashboard, "notebook", key))["state"] == "ready"

    run(scenario())


def test_failed_task_records_reason_and_keeps_finished_batches(tmp_path, monkeypatch):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "a.md", body="甲" * 700)
    write(store, "b.md", body="乙" * 700)
    monkeypatch.setattr(consolidate, "MAX_INPUT_CHARS", 1000)
    monkeypatch.setattr(pkg_tidy, "_now", lambda: 3000.0)
    calls = {"n": 0}

    async def model(prompt, selected=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return "[]", []
        return None, ["第二次调用失败（合成）"]

    plugin.call_tidy_model = model

    async def scenario():
        result = await start(dashboard, "notebook", "Bot-A")
        assert result["status"] == "ok", result
        await dashboard.wait_background()
        task = await get_task(dashboard, "notebook", "Bot-A")
        assert task["state"] == "failed"
        assert task["batchCount"] == 2
        assert task["batchesDone"] == 1
        assert task["batches"][0]["state"] == "done"
        assert task["batches"][1]["state"] == "failed"
        assert "第 2 批" in task["message"] and "合成" in task["message"]
        # 已完成的批次保留在方案里
        assert task["plan"]["files"]
        assert task["plan"]["operations"] == []

    run(scenario())


def test_selective_auto_fix_only_repairs_checked_file(tmp_path):
    plugin, dashboard = setup(tmp_path)
    store = plugin._store.notebook_scope("Bot-A")
    write(store, "user-123.md", body="人物")
    write(store, "note.md", body="见 [[User-123]]")
    write(store, "other.md", body="见 [[gone]]")

    async def model(prompt, selected=None):
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        assert (await start(dashboard, "notebook", "Bot-A"))["status"] == "ok"
        await dashboard.wait_background()
        task = await get_task(dashboard, "notebook", "Bot-A")
        fixes = [op for op in task["plan"]["operations"] if op["op"] == "fix"]
        note = next(op for op in fixes if op["file"] == "note.md")
        other = next(op for op in fixes if op["file"] == "other.md")
        # 只应用 other.md 的自动修复，note.md 保持原样
        tw.REQUEST.body = {
            "scope": "notebook",
            "key": "Bot-A",
            "operations": [other],
        }
        result = await dashboard._wrap(dashboard.consolidate_apply)()
        assert result["status"] == "ok", result
        note_content = await store.read("note.md")
        other_content = await store.read("other.md")
        assert "[[User-123]]" in note_content
        assert "见 gone" in other_content
        assert note["content"] == "见 [[user-123]]"

    run(scenario())


def test_same_scope_cannot_start_twice(tmp_path):
    plugin, dashboard = setup(tmp_path)
    write(plugin._store.notebook_scope("Bot-A"), "a.md")

    async def model(prompt, selected=None):
        return "[]", []

    plugin.call_tidy_model = model

    async def scenario():
        tw.REQUEST.body = {"scope": "notebook", "key": "Bot-A"}
        tw.REQUEST.query = {}
        outcomes = await asyncio.gather(
            dashboard.tidy_start(),
            dashboard.tidy_start(),
            return_exceptions=True,
        )
        errors = [item for item in outcomes if isinstance(item, Exception)]
        done = [item for item in outcomes if isinstance(item, dict)]
        assert len(done) == 1 and len(errors) == 1, outcomes
        await dashboard.wait_background()
        assert (await get_task(dashboard, "notebook", "Bot-A"))["state"] == "ready"
        # 任务结束后锁释放，可以再次整理
        assert plugin._tidy.claim("notebook-Bot-A") is True
        plugin._tidy.release("notebook-Bot-A")

    run(scenario())

