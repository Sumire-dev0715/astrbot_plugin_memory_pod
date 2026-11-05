"""聊天模型写记忆：五个工具的模式、三层强制规则、读后重写与回执。

所有写入都必须经过 core.writer 的统一校验与 ScopeStore（历史、回收站、
审计 actor 记平台 ID）；这里从工具入口验证，确保没有绕过的路径。
"""

from __future__ import annotations

import json
import time
import types

from support import (
    FakeEvent,
    make_plugin,
    pkg_memstore,
    pkg_writer,
    person_file,
    run,
    thing,
)


def make_request(prompt="今天怎么样", contexts=None, cid="conv-1"):
    """一个最小的 ProviderRequest 替身，字段与 AstrBot 4.29.4 一致。"""
    return types.SimpleNamespace(
        prompt=prompt,
        contexts=contexts
        if contexts is not None
        else [
            {"role": "user", "content": "早"},
            {"role": "assistant", "content": "早"},
        ],
        system_prompt="你是测试机器人。",
        conversation=types.SimpleNamespace(cid=cid),
        func_tool=None,
        image_urls=[],
        extra_user_content_parts=[],
    )


def notebook(plugin, platform="Bot-A"):
    return plugin._store.notebook_scope(platform)


def whisper(plugin, event):
    return plugin._store.whisper_scope(pkg_memstore.safe_key(event.unified_msg_origin))


def read_audit(store):
    path = store.root / ".audit.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines()]


# ---------------------------------------------------------------- memory_write


def test_memory_write_creates_file_and_records_actor_source(tmp_path):
    plugin = make_plugin(tmp_path)
    event = FakeEvent("Bot-A", "123", name="小明")
    receipt = run(
        plugin.memory_write(
            event, "notebook", "note", "reference", "周末安排", "每周六开黑"
        )
    )
    assert "已写入 notebook/note.md（新建）" in receipt
    content = run(notebook(plugin).read("note.md"))
    doc = pkg_memstore.parse_memory(content)
    assert doc.description == "周末安排"
    assert doc.type == "reference"
    assert doc.source == "123"
    assert "每周六开黑" in doc.body
    # 索引行由插件同步
    index = (notebook(plugin).root / "MEMORY.md").read_text("utf-8")
    assert "note.md" in index and "周末安排" in index
    # 审计 actor 记当前事件的 platform_id，source 记发送者 ID
    audit = read_audit(notebook(plugin))
    assert audit[-1]["actor"] == "Bot-A"
    assert audit[-1]["source"] == "123"


def test_memory_write_board_requires_admin_and_signs_notice(tmp_path):
    plugin = make_plugin(tmp_path)
    member = FakeEvent("Bot-A", "123", name="小明")
    receipt = run(
        plugin.memory_write(member, "board", "notice.md", "reference", "告示", "入职")
    )
    assert "留言板只有管理员能写" in receipt
    assert run(plugin._store.board_scope().read("notice.md")) is None

    admin = FakeEvent("Bot-A", "123", admin=True, name="小明")
    receipt = run(
        plugin.memory_write(admin, "board", "notice.md", "reference", "告示", "入职")
    )
    assert "已写入 board/notice.md（新建）" in receipt
    doc = pkg_memstore.parse_memory(run(plugin._store.board_scope().read("notice.md")))
    assert doc.signature.startswith("小明（由 ")
    assert doc.signature.endswith(" 代发）")


def test_memory_write_rejects_unsafe_names_and_journal_type(tmp_path):
    plugin = make_plugin(tmp_path)
    event = FakeEvent("Bot-A", "123")
    assert "文件名不合法" in run(
        plugin.memory_write(event, "notebook", "../x.md", "reference", "摘要", "正文")
    )
    assert "文件名不合法" in run(
        plugin.memory_write(event, "notebook", "中文.md", "reference", "摘要", "正文")
    )
    assert "类型不合法" in run(
        plugin.memory_write(event, "notebook", "j.md", "journal", "摘要", "正文")
    )
    assert "正文不能为空" in run(
        plugin.memory_write(event, "notebook", "empty.md", "reference", "摘要", "  ")
    )
    assert "正文超过" in run(
        plugin.memory_write(
            event,
            "notebook",
            "big.md",
            "reference",
            "摘要",
            "字" * (pkg_writer.MAX_CONTENT_CHARS + 1),
        )
    )


def test_memory_write_file_journal_points_to_memory_edit(tmp_path):
    plugin = make_plugin(tmp_path)
    receipt = run(
        plugin.memory_write(
            FakeEvent("Bot-A", "123"), "notebook", "journal", "reference", "摘要", "今天"
        )
    )
    assert "写日记请用 memory_edit" in receipt


# ---------------------------------------------------------------- memory_edit


def test_memory_edit_replace_append_and_section(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(
        store.write(
            "profile.md",
            thing(
                "profile",
                description="旧摘要",
                body="# 资料\n\n旧内容\n\n## 作息\n\n早睡",
            ),
        )
    )
    event = FakeEvent("Bot-A", "123")

    receipt = run(
        plugin.memory_edit(
            event, "notebook", "profile.md", "replace", "旧内容", "新内容", "新摘要"
        )
    )
    assert "已修改 notebook/profile.md" in receipt
    content = run(store.read("profile.md"))
    assert "新内容" in content and "旧内容" not in content
    assert pkg_memstore.parse_memory(content).description == "新摘要"

    receipt = run(
        plugin.memory_edit(event, "notebook", "profile.md", "append", "", "追加到末尾")
    )
    assert "已修改 notebook/profile.md" in receipt
    assert run(store.read("profile.md")).rstrip().endswith("追加到末尾")

    run(
        plugin.memory_edit(
            event, "notebook", "profile.md", "append", "## 作息", "十点睡"
        )
    )
    content = run(store.read("profile.md"))
    assert "早睡" in content and "十点睡" in content

    run(
        plugin.memory_edit(
            event, "notebook", "profile.md", "section", "## 作息", "十点半睡"
        )
    )
    content = run(store.read("profile.md"))
    assert "十点半睡" in content and "十点睡" not in content and "早睡" not in content


def test_memory_edit_replace_requires_unique_existing_text(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(store.write("dup.md", thing("dup", body="重复 重复")))
    event = FakeEvent("Bot-A", "123")
    assert "出现了 2 处" in run(
        plugin.memory_edit(event, "notebook", "dup.md", "replace", "重复", "一次")
    )
    assert "没有找到" in run(
        plugin.memory_edit(event, "notebook", "dup.md", "replace", "不存在", "一次")
    )
    assert "文件不存在" in run(
        plugin.memory_edit(event, "notebook", "missing.md", "append", "", "内容")
    )
    assert "mode 只能是" in run(
        plugin.memory_edit(event, "notebook", "dup.md", "bad", "", "内容")
    )


def test_memory_edit_journal_uses_plugin_chosen_file(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    event = FakeEvent("Bot-A", "123")
    receipt = run(
        plugin.memory_edit(
            event, "notebook", "journal", "append", "", "今天修好了 bug"
        )
    )
    date = time.strftime("%Y-%m-%d")
    name = f"journal-{date}.md"
    assert f"已写日记 notebook/{name}" in receipt
    content = run(store.read(name))
    assert "今天修好了 bug" in content
    assert content.count(f"## {date}") == 1
    assert pkg_memstore.parse_memory(content).type == "journal"

    # 同一天再写接着同一篇，不重复日期小标题
    run(plugin.memory_edit(event, "notebook", "journal", "append", "", "又记一笔"))
    assert run(store.read(name)).count(f"## {date}") == 1
    assert "又记一笔" in run(store.read(name))

    # 日记只能 append，且必须用 file=journal
    assert "只支持 append" in run(
        plugin.memory_edit(event, "notebook", "journal", "replace", "x", "y")
    )
    assert "留言板不记日记" in run(
        plugin.memory_edit(
            FakeEvent("Bot-A", "1", admin=True),
            "board",
            "journal",
            "append",
            "",
            "告示",
        )
    )
    assert "日记由插件决定写到哪" in run(
        plugin.memory_edit(
            event, "notebook", f"journal-{date}.md", "append", "", "正文"
        )
    )
    # 悄悄话里也可以记日记
    receipt = run(
        plugin.memory_edit(
            FakeEvent("Bot-A", "123"),
            "whisper",
            "journal",
            "append",
            "",
            "私密流水",
        )
    )
    assert "已写日记 whisper/" in receipt


# ---------------------------------------------------------------- memory_delete


def test_memory_delete_moves_to_trash_and_records_reason(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(store.write("note.md", thing("note", body="正文")))
    receipt = run(
        plugin.memory_delete(FakeEvent("Bot-A", "123"), "notebook", "note.md", "过期")
    )
    assert "已删除 notebook/note.md" in receipt
    assert "回收站" in receipt
    assert "原因：过期" in receipt
    assert run(store.read("note.md")) is None
    assert store.trash_entries()
    audit = read_audit(store)[-1]
    assert audit["reason"] == "过期"
    assert audit["actor"] == "Bot-A"
    assert "note.md" not in (store.root / "MEMORY.md").read_text("utf-8")

    assert "文件不存在" in run(
        plugin.memory_delete(FakeEvent("Bot-A", "123"), "notebook", "note.md")
    )


# ------------------------------------------------------------ 3.8.3 强制规则


def test_board_edit_and_delete_require_admin(tmp_path):
    plugin = make_plugin(tmp_path)
    board = plugin._store.board_scope()
    run(board.write("notice.md", thing("notice", body="告示", signature="管理员")))
    member = FakeEvent("Bot-A", "123")

    assert "留言板只有管理员能写" in run(
        plugin.memory_edit(member, "board", "notice.md", "append", "", "补充")
    )
    assert "留言板只有管理员能写" in run(
        plugin.memory_delete(member, "board", "notice.md", "过期")
    )
    assert run(board.read("notice.md")) is not None

    admin = FakeEvent("Bot-A", "1", admin=True)
    assert "已修改 board/notice.md" in run(
        plugin.memory_edit(admin, "board", "notice.md", "append", "", "补充")
    )
    assert "已删除 board/notice.md" in run(
        plugin.memory_delete(admin, "board", "notice.md", "过期")
    )


def test_person_profile_only_self_or_admin_can_change(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(store.write("user-456.md", person_file("456", "身份：设计师")))

    stranger = FakeEvent("Bot-A", "123")
    # 先读（读别人的档案允许），再整篇重写会被规则 3 拦住
    run(plugin.memory_read(stranger, "notebook", "user-456.md"))
    assert "只有本人或管理员" in run(
        plugin.memory_write(
            stranger, "notebook", "user-456.md", "user", "阿杰", "身份：工程师"
        )
    )
    assert "只有本人或管理员" in run(
        plugin.memory_edit(
            stranger, "notebook", "user-456.md", "append", "", "补充"
        )
    )
    assert "只有本人或管理员" in run(
        plugin.memory_delete(stranger, "notebook", "user-456.md")
    )
    assert "身份：设计师" in run(store.read("user-456.md"))

    owner = FakeEvent("Bot-A", "456")
    assert "已修改 notebook/user-456.md" in run(
        plugin.memory_edit(owner, "notebook", "user-456.md", "append", "", "喜欢猫")
    )
    admin = FakeEvent("Bot-A", "1", admin=True)
    run(plugin.memory_read(admin, "notebook", "user-456.md"))
    assert "已写入 notebook/user-456.md" in run(
        plugin.memory_write(
            admin, "notebook", "user-456.md", "user", "阿杰", "身份：管理员更正"
        )
    )


def test_whisper_writes_never_reach_another_conversation(tmp_path):
    plugin = make_plugin(tmp_path)
    alice = FakeEvent("Bot-A", "123", umo="Bot-A:FriendMessage:123")
    bob = FakeEvent("Bot-A", "456", umo="Bot-A:FriendMessage:456")

    receipt = run(
        plugin.memory_write(alice, "whisper", "secret.md", "reference", "秘密", "只给爱丽丝")
    )
    assert "已写入 whisper/secret.md" in receipt
    assert run(whisper(plugin, alice).read("secret.md")) is not None

    # 别人的私聊：同一文件名既读不到，也改不了、删不了
    assert "不存在" in run(plugin.memory_read(bob, "whisper", "secret.md"))
    assert "文件不存在" in run(
        plugin.memory_edit(bob, "whisper", "secret.md", "append", "", "偷看")
    )
    assert "文件不存在" in run(
        plugin.memory_delete(bob, "whisper", "secret.md", "偷删")
    )
    assert "只给爱丽丝" in run(whisper(plugin, alice).read("secret.md"))

    # 鲍勃写自己的悄悄话，落在自己的会话目录
    run(plugin.memory_write(bob, "whisper", "secret.md", "reference", "秘密", "只给鲍勃"))
    assert "只给鲍勃" in run(whisper(plugin, bob).read("secret.md"))
    assert "只给爱丽丝" in run(whisper(plugin, alice).read("secret.md"))


def test_write_tools_disabled_with_memory_off(tmp_path):
    plugin = make_plugin(tmp_path, enable_memory=False)
    event = FakeEvent("Bot-A", "123")
    for result in (
        run(plugin.memory_write(event, "notebook", "a.md", "reference", "摘要", "正文")),
        run(plugin.memory_edit(event, "notebook", "a.md", "append", "", "正文")),
        run(plugin.memory_delete(event, "notebook", "a.md")),
    ):
        assert "停用" in result


def test_write_tools_reject_unknown_scope(tmp_path):
    plugin = make_plugin(tmp_path)
    event = FakeEvent("Bot-A", "123")
    for result in (
        run(plugin.memory_write(event, "global", "a.md", "reference", "摘要", "正文")),
        run(plugin.memory_edit(event, "people", "a.md", "append", "", "正文")),
        run(plugin.memory_delete(event, "session", "a.md")),
    ):
        assert "scope 参数必须是" in result


# ------------------------------------------------------- 3.8.4 读后重写校验


def test_rewrite_requires_fresh_read_and_rejects_after_external_change(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    event = FakeEvent("Bot-A", "123")
    run(store.write("note.md", thing("note", body="第一版")))

    receipt = run(
        plugin.memory_write(
            event, "notebook", "note.md", "reference", "摘要", "第二版"
        )
    )
    assert "必须先读" in receipt or "必须先读过" in receipt
    assert "第一版" in run(store.read("note.md"))

    run(plugin.memory_read(event, "notebook", "note.md"))
    receipt = run(
        plugin.memory_write(
            event, "notebook", "note.md", "reference", "摘要", "第二版"
        )
    )
    assert "整篇重写" in receipt
    assert "第二版" in run(store.read("note.md"))

    # 读过之后又别处改过：同样拒绝，要求重读
    run(store.write("note.md", thing("note", body="第三版")))
    receipt = run(
        plugin.memory_write(
            event, "notebook", "note.md", "reference", "摘要", "第四版"
        )
    )
    assert "必须先读过" in receipt
    assert "第三版" in run(store.read("note.md"))


def test_append_and_replace_work_without_the_read_gate(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(store.write("note.md", thing("note", body="旧正文")))
    event = FakeEvent("Bot-A", "123")
    assert "已修改" in run(
        plugin.memory_edit(event, "notebook", "note.md", "append", "", "追加")
    )
    assert "已修改" in run(
        plugin.memory_edit(event, "notebook", "note.md", "replace", "旧正文", "新正文")
    )


def test_recall_full_text_counts_as_read(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(
        store.write(
            "weekend.md",
            thing("weekend", description="周末安排", body="每周六晚上八点开黑"),
        )
    )
    event = FakeEvent("Bot-A", "123", umo="Bot-A:GroupMessage:900")
    req = make_request("周末开黑吗")
    run(plugin._process_request(event, req))
    texts = [part["text"] for part in req.extra_user_content_parts]
    assert any(t.startswith("<memory_pod_recall>") and "每周六晚上八点开黑" in t for t in texts)
    receipt = run(
        plugin.memory_write(
            event, "notebook", "weekend.md", "reference", "周末安排", "改到周日"
        )
    )
    assert "整篇重写" in receipt
    assert "改到周日" in run(store.read("weekend.md"))


def test_truncated_recall_does_not_count_as_read(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(
        store.write(
            "long.md",
            thing("long", description="长文资料", body="甲" * 4000),
        )
    )
    event = FakeEvent("Bot-A", "123", umo="Bot-A:GroupMessage:900")
    req = make_request("长文资料")
    run(plugin._process_request(event, req))
    texts = [part["text"] for part in req.extra_user_content_parts]
    assert any("中间省略" in t for t in texts)
    receipt = run(
        plugin.memory_write(
            event, "notebook", "long.md", "reference", "长文资料", "重写后的正文"
        )
    )
    assert "必须先读过" in receipt


def test_opening_block_full_text_counts_as_read(tmp_path):
    plugin = make_plugin(tmp_path)
    board = plugin._store.board_scope()
    run(board.write("notice.md", thing("notice", body="11 月 11 日入职", signature="管理员")))
    admin = FakeEvent("Bot-A", "1", admin=True, umo="Bot-A:GroupMessage:900")
    run(plugin._process_request(admin, make_request("你好")))
    receipt = run(
        plugin.memory_write(
            admin, "board", "notice.md", "reference", "告示", "11 月 12 日入职"
        )
    )
    assert "整篇重写" in receipt
    assert "11 月 12 日入职" in run(board.read("notice.md"))


def test_pinned_private_profile_counts_as_read(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(store.write("user-123.md", person_file("123", "身份：工程师")))
    event = FakeEvent("Bot-A", "123", umo="Bot-A:FriendMessage:123")
    run(plugin._process_request(event, make_request("你好")))
    receipt = run(
        plugin.memory_write(
            event, "notebook", "user-123.md", "user", "我", "身份：高级工程师"
        )
    )
    assert "整篇重写" in receipt
    assert "高级工程师" in run(store.read("user-123.md"))


# ---------------------------------------------------------------- 回执


def test_receipt_names_file_and_warns_dangling_and_long(tmp_path):
    plugin = make_plugin(tmp_path)
    event = FakeEvent("Bot-A", "123")
    receipt = run(
        plugin.memory_write(
            event,
            "notebook",
            "links.md",
            "reference",
            "链接",
            "参见 [[不存在的文件]]",
        )
    )
    assert "已写入 notebook/links.md" in receipt
    assert "[[不存在的文件.md]]" in receipt
    assert "没有对应文件" in receipt

    receipt = run(
        plugin.memory_write(
            event, "notebook", "long.md", "reference", "长文", "字" * 3000
        )
    )
    assert "已写入 notebook/long.md" in receipt
    assert "已经很长" in receipt
    assert "超过 8KB" in receipt


def test_delete_receipt_and_index_cleanup(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    run(store.write("gone.md", thing("gone", body="正文")))
    receipt = run(
        plugin.memory_delete(FakeEvent("Bot-A", "123"), "notebook", "gone.md", "不再需要")
    )
    assert "已删除 notebook/gone.md" in receipt
    assert "回收站" in receipt and "原因：不再需要" in receipt
    assert "gone.md" not in (store.root / "MEMORY.md").read_text("utf-8")
    assert store.trash_entries()[0].file == "gone.md"


def test_rewrite_saves_previous_version_for_undo(tmp_path):
    plugin = make_plugin(tmp_path)
    store = notebook(plugin)
    event = FakeEvent("Bot-A", "123")
    run(plugin.memory_write(event, "notebook", "note.md", "reference", "摘要", "第一版"))
    run(plugin.memory_read(event, "notebook", "note.md"))
    run(plugin.memory_write(event, "notebook", "note.md", "reference", "摘要", "第二版"))
    versions = store.history_versions("note.md")
    assert versions
    assert "第一版" in store.read_history("note.md", versions[0].version)
