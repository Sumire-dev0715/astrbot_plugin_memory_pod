"""召回编排（core.recall.assemble_recall）与改造前选择结果的对比。

改造前选中的文件和顺序由上游 v1.4.1（commit d217033 的 core/recall.py，
单独导出到临时目录运行）在同一批合成数据上算出，作为固定期望值写在各
测试里；legacy_recall 只按这份固定清单读正文，不再调用被测的挑选函数，
避免同源循环证明。3.6 第 2、3 条有意改变了块内呈现（分档、去 frontmatter、
头尾瘦身、标题格式），所以块文本不再逐字比较，渲染由
tests/test_injection.py 与 tests/test_recall_tiers.py 单独覆盖。
记忆全部是临时目录里的合成数据。
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
    """按 (作用域, 文件名, 描述, 正文) 建合成记忆库；返回 (MemoryStore, 作用域表)。

    写入后把 frontmatter 的 updated 统一改成 mtime：同分候选的"同分取较新"
    排序才稳定，固定期望值不会因为跑测试时恰好跨分钟而变。
    """
    root = tmp_path / "memories"
    store = memstore.MemoryStore(root)
    written = {}
    stamp = memstore.format_stamp(mtime)
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
        lines = []
        for line in path.read_text("utf-8").splitlines(keepends=True):
            if line.startswith("  updated: "):
                line = f"  updated: {stamp}\n"
            lines.append(line)
        path.write_text("".join(lines), "utf-8")
        os.utime(path, (mtime, mtime))
    return store, written


def candidates_of(stores):
    result = []
    for label, store in stores.items():
        for header in store.headers():
            result.append(recall.Candidate(label, header))
    return result


async def legacy_recall(stores, candidates, expected, *, shared_people=False):
    """按固定期望值读正文，返回 (选中候选, 条目, 使用统计)。

    expected 是上游 v1.4.1 在同一批合成数据上选出的 "作用域/文件名"，
    顺序原样保留；这里不做任何相关性计算，选中集合或顺序回归会直接暴露。
    """
    by_ref = {cand.ref: cand for cand in candidates}
    chosen = [by_ref[ref] for ref in expected if ref in by_ref]
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
    return chosen, items, used


async def compare(stores, candidates, expected, **kwargs):
    """新实现与固定期望值（选中的文件、顺序）以及读到的原文、使用统计一致。"""
    plan = await recall.assemble_recall(stores, candidates, **kwargs)
    chosen, items, used = await legacy_recall(
        stores,
        candidates,
        expected,
        shared_people=kwargs.get("shared_people", False),
    )
    # 选中集合与顺序来自上游 v1.4.1，不经过被测函数
    assert [item.ref for item in plan.items] == expected
    assert [item.ref for item in plan.items] == [item.ref for item in items]
    assert [item.content for item in plan.items] == [item.content for item in items]
    assert [item.shared for item in plan.items] == [item.shared for item in items]
    assert plan.used == used
    # 块内提到的文件与顺序不变；具体呈现（分档/瘦身）由新测试覆盖
    assert plan.files == expected
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
            # 上游 v1.4.1 在同一批数据上的选择：weekend-game 得分 6；
            # daily 得分 2（低于门槛）、people 的其他人不按关键词扩散
            ["people/user-123456.md", "session/weekend-game.md"],
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
            # 上游 v1.4.1：身份必选 user-123456 在前，再按模型挑的顺序接 b
            ["people/user-123456.md", "session/b.md"],
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
            # 上游 v1.4.1：模型明确回答"没有相关的"时只留身份必选
            ["people/user-123456.md"],
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
            # 上游 v1.4.1：三个文件同分（3），同分取较新，时间相同则保持候选顺序
            ["session/big-1.md", "session/big-2.md", "session/big-3.md"],
            query="开黑",
            forced_files=[],
            limit=5,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert any(section.truncated for section in plan.sections)
    assert "中间省略" in plan.block
    # 头尾瘦身过的条目仍在块里；选中的文件一个不少
    assert plan.files == [item.ref for item in plan.items]


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
            # 上游 v1.4.1：缺正文的 gone.md 不在候选里，选中的只有 a
            ["session/a.md"],
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
            # 上游 v1.4.1：身份必选 people 在前，a 按关键词得分补上
            ["people/user-123456.md", "session/a.md"],
            query="开黑",
            forced_files=["user-123456.md"],
            limit=5,
            shared_people=True,
            data_notice=prompts.DATA_NOTICE,
            now=NOW,
        )
    )
    assert "多个机器人共享｜" in plan.block
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
