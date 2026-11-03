"""共享人物空间：成员表解析、目录映射、追加模式与版本记录。"""

import asyncio
from pathlib import Path

from core import history, memstore, recall, sharing


def run(coro):
    return asyncio.run(coro)


def person_file(name="user-123", body="身份：测试用户，工程师"):
    return (
        "---\n"
        f"name: {name}\n"
        "description: 测试人物\n"
        "metadata:\n"
        "  type: user\n"
        "---\n\n"
        f"{body}\n"
    )


# ---------------------------------------------------------------- 配置解析


def test_parse_config_empty_is_off():
    membership = sharing.parse_config([])
    assert membership.active is False
    assert membership.permission("Bot-A") is None
    assert membership.warnings == []
    assert sharing.parse_config("").active is False
    assert sharing.parse_config(None).active is False


def test_parse_config_defaults_to_append_and_accepts_text_lines():
    membership = sharing.parse_config(
        "Bot-A\nBot-B = edit\nBot-C=read\n# 注释\n\n"
    )
    assert membership.active is True
    assert membership.permission("Bot-A") == sharing.PERMISSION_APPEND
    assert membership.permission("Bot-B") == sharing.PERMISSION_EDIT
    assert membership.permission("Bot-C") == sharing.PERMISSION_READ
    assert membership.warnings == []


def test_parse_config_rejects_unknown_level_and_keeps_others():
    membership = sharing.parse_config(["Bot-A=root", "Bot-B=read"])
    assert membership.permission("Bot-A") is None  # 不擅自当成默认 append
    assert membership.permission("Bot-B") == sharing.PERMISSION_READ
    assert any("root" in warning for warning in membership.warnings)


def test_parse_config_duplicate_takes_last():
    membership = sharing.parse_config(["Bot-A=read", "Bot-A=edit"])
    assert membership.permission("Bot-A") == sharing.PERMISSION_EDIT
    assert any("多次" in warning for warning in membership.warnings)


def test_filter_same_platform_drops_mismatched_members():
    membership = sharing.parse_config(
        ["Bot-A", "Bot-B=edit", "Bot-C=read", "Bot-D"]
    )
    filtered = membership.filter_same_platform(
        {"Bot-A": "aiocqhttp", "Bot-B": "aiocqhttp", "Bot-C": "discord"}
    )
    assert filtered.permission("Bot-A") == sharing.PERMISSION_APPEND
    assert filtered.permission("Bot-B") == sharing.PERMISSION_EDIT
    assert filtered.permission("Bot-C") is None  # 平台类型不一致，关闭共享
    assert filtered.permission("Bot-D") == sharing.PERMISSION_APPEND  # 查不到类型，保留
    assert any("Bot-C" in warning for warning in filtered.warnings)
    # 空配置与拿不到类型时原样返回
    assert sharing.parse_config([]).filter_same_platform({}).active is False
    assert membership.filter_same_platform({}) is membership


# ---------------------------------------------------------------- 目录映射


def test_members_share_one_directory_and_non_members_keep_theirs(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    store.set_shared_people(sharing.parse_config(["Bot-A", "Bot-B=edit"]))
    shared_root = (tmp_path / "memories" / "people" / "_shared").resolve()

    assert store.people_scope("Bot-A").root == shared_root
    assert store.people_scope("Bot-B").root == shared_root
    assert store.people_scope("Bot-A") is store.people_scope("Bot-B")

    other = store.people_scope("Bot-C")
    assert other.root == (
        tmp_path / "memories" / "people" / memstore.safe_key("Bot-C")
    ).resolve()
    assert other.root != shared_root


def test_shared_key_is_a_valid_scope_key_and_listed(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    store.set_shared_people(sharing.parse_config(["Bot-A=edit"]))
    scope = store.people_scope("Bot-A")
    run(scope.write("user-1.md", person_file("user-1"), actor="Bot-A"))

    assert memstore.is_scope_key(sharing.SHARED_KEY)
    assert store.people_scope_by_key("_shared") is scope
    assert "_shared" in store.people_keys()
    # safe_key 的输出永远不以 _ 开头，不会和平台目录撞名
    assert not memstore.safe_key("Bot-A").startswith("_")
    assert not memstore.safe_key("_shared").startswith("_")


def test_empty_config_keeps_upstream_platform_directories(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    store.set_shared_people(sharing.parse_config([]))
    assert store.people_scope("Bot-A").root == (
        tmp_path / "memories" / "people" / memstore.safe_key("Bot-A")
    ).resolve()
    assert store.people_scope("Bot-A") is not store.people_scope("Bot-B")


def test_write_forbid_overwrite_covers_active_and_forgotten(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    scope = store.people_scope("Bot-A")
    created = run(scope.write("user-1.md", person_file("user-1"), forbid_overwrite=True))
    assert created.ok
    again = run(
        scope.write(
            "user-1.md", person_file("user-1", body="覆盖"), forbid_overwrite=True
        )
    )
    assert not again.ok and again.message == sharing.APPEND_OVERWRITE_REFUSAL
    run(scope.forget("user-1.md", actor="system"))
    dormant = run(
        scope.write(
            "user-1.md", person_file("user-1", body="顶替遗忘副本"), forbid_overwrite=True
        )
    )
    assert not dormant.ok and dormant.message == sharing.APPEND_OVERWRITE_REFUSAL
    assert scope.has_forgotten("user-1.md")


# ---------------------------------------------------------------- 追加模式


def test_edit_append_to_end_saves_history(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    scope = store.people_scope("Bot-A")
    original = person_file()
    run(scope.write("user-123.md", original, actor="Bot-A"))

    report = run(scope.edit("user-123.md", "", "补充：喜欢钓鱼", mode="append"))
    assert report.ok, report.message
    content = run(scope.read("user-123.md"))
    assert "身份：测试用户，工程师" in content
    assert content.rstrip().endswith("补充：喜欢钓鱼")
    versions = scope.history_versions("user-123.md")
    assert versions and versions[0].action == history.ACTION_EDIT
    assert "补充：喜欢钓鱼" not in scope.read_history(
        "user-123.md", versions[0].version
    )


def test_edit_append_to_section_stays_inside_section(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    scope = store.people_scope("Bot-A")
    body = (
        "## 基本信息\n\n职业：工程师\n\n"
        "## 作息与习惯\n\n早睡早起\n"
    )
    run(scope.write("user-123.md", person_file(body=body), actor="Bot-A"))

    report = run(
        scope.edit("user-123.md", "## 基本信息", "兴趣：钓鱼", mode="append")
    )
    assert report.ok, report.message
    content = run(scope.read("user-123.md"))
    assert content.index("职业：工程师") < content.index("兴趣：钓鱼")
    assert content.index("兴趣：钓鱼") < content.index("## 作息与习惯")
    assert "兴趣：钓鱼" in content.split("## 作息与习惯")[0]


def test_edit_append_rejects_missing_or_duplicate_heading(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    scope = store.people_scope("Bot-A")
    run(
        scope.write(
            "user-123.md",
            person_file(body="## 甲\n\n一\n\n## 甲\n\n二\n"),
            actor="Bot-A",
        )
    )
    missing = run(scope.edit("user-123.md", "## 乙", "x", mode="append"))
    assert not missing.ok
    assert "现有标题" in missing.message and "## 甲" in missing.message
    duplicate = run(scope.edit("user-123.md", "## 甲", "x", mode="append"))
    assert not duplicate.ok
    assert "2 处" in duplicate.message


def test_edit_replace_default_unchanged(tmp_path):
    store = memstore.MemoryStore(tmp_path / "memories")
    scope = store.people_scope("Bot-A")
    run(scope.write("user-123.md", person_file(), actor="Bot-A"))
    report = run(scope.edit("user-123.md", "工程师", "研究员"))
    assert report.ok, report.message
    assert "研究员" in run(scope.read("user-123.md"))
    assert run(scope.edit("user-123.md", "", "x")).ok is False
    assert run(scope.edit("user-123.md", "工程师", "研究员", mode="bogus")).ok is False


# ---------------------------------------------------------------- 版本记录与召回标注


def test_seen_versions_roundtrip(tmp_path):
    seen = sharing.SeenVersions(tmp_path / "state" / "shared_seen.json")
    seen.note("Bot-A", "user-1.md", "第一版")
    assert seen.seen("Bot-A", "user-1.md") == sharing.content_sha1("第一版")
    assert seen.is_current("Bot-A", "user-1.md", "第一版") is True
    assert seen.is_current("Bot-A", "user-1.md", "第二版") is False
    assert seen.is_current("Bot-B", "user-1.md", "第一版") is False
    # 重新加载后仍然记得
    again = sharing.SeenVersions(tmp_path / "state" / "shared_seen.json")
    assert again.seen("Bot-A", "user-1.md") == sharing.content_sha1("第一版")


def test_recall_block_marks_shared_people_files():
    items = [
        recall.Recalled("people/user-1.md", "内容", 1_800_000_000.0, shared=True),
        recall.Recalled("session/user-2.md", "内容", 1_800_000_000.0),
    ]
    block = recall.build_recall_block(items, "[数据]", now=1_800_000_000.0)
    assert "### people/user-1.md（未标注｜多个机器人共享｜今天写入）" in block
    assert "### session/user-2.md（未标注｜今天写入）" in block
    assert "多个机器人共享" not in block.split("### session/user-2.md")[1]
