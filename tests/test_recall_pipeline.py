"""召回编排（core.recall.assemble_recall）与重构前逻辑的逐字对比。

参照实现 legacy_recall 是从 main.py 的 _recall_message 抽出去之前的原样
代码（挑选、读文件、拼块），用来证明重构前后选中的文件、顺序和块文本
完全一致。记忆全部是临时目录里的合成数据。
"""

import asyncio
import os

from core import memstore, prompts, recall

NOW = 1_800_000_000.0
DAY = 86400


def run(coro):
    return asyncio.run(coro)


def memory(name, description, body, mem_type="user"):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        "---\n\n"
        f"{body}\n"
    )


def make_store(tmp_path, files, mtime=NOW):
    """按 (作用域, 文件名, 描述, 正文) 建合成记忆库；返回 (MemoryStore, 作用域表)。"""
    root = tmp_path / "memories"
    store = memstore.MemoryStore(root)
    written = {}
    for scope, name, description, body in files:
        if scope == "global":
            scoped = store.global_scope()
        elif scope == "session":
            scoped = store.session_scope("Bot-A:FriendMessage:100")
        else:
            scoped = store.people_scope("Bot-A")
        report = run(scoped.write(name, memory(name[:-3], description, body)))
        assert report.ok, report.message
        written.setdefault(scope, scoped)
        path = scoped.root / name
        os.utime(path, (mtime, mtime))
    return store, written


def candidates_of(stores):
    result = []
    for label, store in stores.items():
        for header in store.headers():
            result.append(recall.Candidate(label, header))
    return result


async def legacy_recall(
    stores,
    candidates,
    *,
    query,
    forced_files,
    limit,
    data_notice,
    model_choice=None,
    shared_people=False,
    now=None,
):
    """重构前 _recall_message 里"候选收集之后"的原样逻辑（只作对照）。"""
    chosen = model_choice
    if chosen is None:
        chosen = recall.select_by_keywords(candidates, query, forced_files, limit)
    else:
        keyword_forced = recall.select_by_keywords(candidates, "", forced_files, limit)
        merged = keyword_forced + [c for c in chosen if c not in keyword_forced]
        chosen = merged[:limit]
    items = []
    used = {}
    for cand in chosen:
        content = await stores[cand.scope].read(cand.header.file)
        if content:
            is_shared = shared_people and cand.scope == "people"
            items.append(
                recall.Recalled(
                    cand.ref,
                    content,
                    cand.header.updated,
                    shared=is_shared,
                )
            )
            used.setdefault(cand.scope, []).append(cand.header.file)
    block = recall.build_recall_block(items, data_notice, now=now)
    return chosen, items, used, block


async def compare(stores, candidates, **kwargs):
    """新实现与参照实现的结果必须逐字一致。"""
    plan = await recall.assemble_recall(stores, candidates, **kwargs)
    chosen, items, used, block = await legacy_recall(
        stores, candidates, **kwargs
    )
    assert [item.ref for item in plan.items] == [item.ref for item in items]
    assert [item.content for item in plan.items] == [item.content for item in items]
    assert [item.shared for item in plan.items] == [item.shared for item in items]
    assert plan.used == used
    assert plan.block == block
    assert plan.files == [section.item.ref for section in plan.sections]
    return plan, chosen


def test_keyword_recall_matches_pre_refactor(tmp_path):
    store, stores = make_store(
        tmp_path,
        [
            ("session", "weekend-game.md", "每周六晚上的开黑活动", "晚上八点集合"),
            ("session", "deadline.md", "项目截止日", "八月十五日之前交"),
            ("session", "daily-2026-11-01.md", "流水账", "周六开黑"),
            ("people", "user-123456.md", "阿杰，群主", "喜欢开黑"),
            ("people", "user-654321.md", "小鱼", "也喜欢开黑"),
            ("global", "self.md", "机器人自我设定", "开黑话题不主动插话"),
        ],
    )
    candidates = candidates_of(stores)
    plan, chosen = run(
        compare(
            stores,
            candidates,
            query="这周六还开黑吗",
            forced_files=["user-123456.md"],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    # 身份必选在前；日志不参与；people 里的其他人不按关键词扩散
    assert plan.files[0] == "people/user-123456.md"
    assert "session/daily-2026-11-01.md" not in plan.files
    assert "people/user-654321.md" not in plan.files
    assert len(plan.files) >= 2
    # 被选中且读到内容的文件都记进 used（含被预算挤掉的）
    assert "user-123456.md" in plan.used["people"]
    assert plan.block.startswith(recall.RECALL_BLOCK_OPEN)


def test_model_choice_merge_matches_pre_refactor(tmp_path):
    store, stores = make_store(
        tmp_path,
        [
            ("session", "a.md", "开黑活动", "周六晚上"),
            ("session", "b.md", "点歌清单", "周杰伦"),
            ("people", "user-123456.md", "阿杰，群主", "喜欢开黑"),
        ],
    )
    candidates = candidates_of(stores)
    # 模型挑了 b（以及不在本轮清单里的条目由调用方过滤，这里不构造）
    model_choice = [c for c in candidates if c.header.file == "b.md"]
    plan, chosen = run(
        compare(
            stores,
            candidates,
            query="周六开黑",
            forced_files=["user-123456.md"],
            limit=5,
            model_choice=model_choice,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    # 模型模式只带身份必选与模型挑的，不按关键词补 a
    assert plan.files == ["people/user-123456.md", "session/b.md"]
    assert [c.ref for c in chosen] == plan.files


def test_model_empty_choice_falls_back_to_forced_only(tmp_path):
    store, stores = make_store(
        tmp_path,
        [
            ("session", "a.md", "开黑活动", "周六晚上"),
            ("people", "user-123456.md", "阿杰，群主", "喜欢开黑"),
        ],
    )
    candidates = candidates_of(stores)
    plan, _ = run(
        compare(
            stores,
            candidates,
            query="周六开黑",
            forced_files=["user-123456.md"],
            limit=5,
            model_choice=[],
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert plan.files == ["people/user-123456.md"]


def test_budget_truncation_matches_pre_refactor(tmp_path):
    long_body = "开黑" * (recall.FILE_CHARS // 2 + 200)
    store, stores = make_store(
        tmp_path,
        [
            ("session", "big-1.md", "开黑活动安排", long_body),
            ("session", "big-2.md", "开黑活动记录", long_body),
            ("session", "big-3.md", "开黑活动名单", "短"),
        ],
    )
    candidates = candidates_of(stores)
    plan, _ = run(
        compare(
            stores,
            candidates,
            query="开黑",
            forced_files=[],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert any(section.truncated for section in plan.sections)
    assert "已截断" in plan.block
    # 截断算正文被切过，但依然算"全文文件"之外的一条
    assert plan.files == [section.item.ref for section in plan.sections]


def test_missing_or_empty_file_is_skipped(tmp_path):
    store, stores = make_store(
        tmp_path,
        [
            ("session", "a.md", "开黑活动", "周六晚上"),
        ],
    )
    candidates = candidates_of(stores)
    from core.memstore import MemoryHeader

    missing = recall.Candidate(
        "session",
        MemoryHeader(
            file="gone.md",
            name="gone",
            description="开黑活动",
            type="user",
            mtime=NOW,
            size=10,
            text="开黑",
            updated=NOW,
        ),
    )
    plan, _ = run(
        compare(
            stores,
            candidates + [missing],
            query="开黑",
            forced_files=[],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert "session/gone.md" not in plan.files
    assert "gone.md" not in plan.used.get("session", [])


def test_shared_people_marks_only_people_scope(tmp_path):
    store, stores = make_store(
        tmp_path,
        [
            ("session", "a.md", "开黑活动", "周六晚上"),
            ("people", "user-123456.md", "阿杰", "喜欢开黑"),
        ],
    )
    candidates = candidates_of(stores)
    plan, _ = run(
        compare(
            stores,
            candidates,
            query="开黑",
            forced_files=["user-123456.md"],
            limit=5,
            shared_people=True,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert "多个机器人共享，" in plan.block
    assert [item.shared for item in plan.items] == [True, False][: len(plan.items)]
    # 非共享时同样的输入不带共享标注
    plain = run(
        recall.assemble_recall(
            stores,
            candidates,
            query="开黑",
            forced_files=["user-123456.md"],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert "多个机器人共享" not in plain.block
    assert plan.block != plain.block


def test_zero_limit_and_empty_candidates():
    assert recall.choose_candidates([], "x", [], 0) == []
    assert run(
        recall.assemble_recall(
            {},
            [],
            query="x",
            forced_files=[],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
        )
    ).block is None


def test_assemble_recall_does_not_write_usage(tmp_path):
    store, stores = make_store(
        tmp_path,
        [("session", "a.md", "开黑活动", "周六晚上")],
    )
    candidates = candidates_of(stores)
    path = stores["session"].root / ".usage.json"
    before = path.read_text("utf-8") if path.is_file() else None
    run(
        recall.assemble_recall(
            stores,
            candidates,
            query="开黑",
            forced_files=[],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    after = path.read_text("utf-8") if path.is_file() else None
    assert before == after
    headers = {h.file: h for h in stores["session"].headers()}
    assert headers["a.md"].uses == 0
