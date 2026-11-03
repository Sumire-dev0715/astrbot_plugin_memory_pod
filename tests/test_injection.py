"""注入视图：去 frontmatter、头尾瘦身、标题、来源与开头块档案区。"""

from core import injection, memstore, prompts

NOW = 1_800_000_000.0
DAY = 86400


def memory(name, description, body, mem_type="user", source=""):
    meta = f"  type: {mem_type}\n"
    if source:
        meta += f"  source: {source}\n"
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"{meta}"
        "---\n\n"
        f"{body}\n"
    )


def test_view_strips_frontmatter_and_puts_description_under_title():
    content = memory("a", "扫一眼就知道何时该读", "正文第一行\n正文第二行", "project")
    view = injection.prepare_view("session/a.md", content, NOW - 3 * DAY)
    # 注入视图里没有 frontmatter，标题下的第一行是 description
    assert view.body == "正文第一行\n正文第二行"
    assert view.mem_type == "project"
    rendered = injection.render_view(view, NOW)
    lines = rendered.splitlines()
    assert lines[0] == "### session/a.md（project｜3 天前写入）"
    assert lines[1] == "扫一眼就知道何时该读"
    assert lines[2] == "正文第一行"
    assert "---" not in rendered and "name: a" not in rendered


def test_head_and_tail_kept_when_body_is_trimmed():
    head = "开头标记HEAD" * 20
    tail = "结尾标记TAIL" * 20
    middle = "中" * (injection.FILE_CHARS * 2)
    content = memory("long", "长文件", head + middle + tail)
    view = injection.prepare_view("session/long.md", content, NOW)
    assert view.truncated is True
    assert view.omitted == len(head + middle + tail) - injection.FILE_CHARS
    # 开头约六成、结尾约四成都保留：新内容补在末尾也不会被切掉
    assert view.body.startswith(head)
    assert view.body.endswith(tail)
    assert f"中间省略 {view.omitted} 字" in view.body
    assert "完整内容用 memory_read 读取" in view.body


def test_body_at_limit_is_not_truncated():
    content = memory("edge", "边界", "开" * injection.FILE_CHARS)
    view = injection.prepare_view("session/edge.md", content, NOW)
    assert view.truncated is False
    assert view.omitted == 0
    assert view.body == "开" * injection.FILE_CHARS


def test_long_description_is_capped_like_index_hooks():
    content = memory("a", "描" * 500, "正文")
    view = injection.prepare_view("session/a.md", content, NOW)
    assert len(view.description) == memstore.MAX_HOOK_CHARS + 1
    assert view.description.endswith("…")

    bracketed = injection.prepare_view(
        "session/a.md", memory("a", "a [b] 描述", "正文"), NOW
    )
    assert bracketed.description == "a ［b］ 描述"


def test_missing_frontmatter_does_not_duplicate_first_body_line():
    content = "第一行正文\n第二行正文"
    view = injection.prepare_view("session/a.md", content, NOW)
    assert view.description == ""
    rendered = injection.render_view(view, NOW)
    assert rendered.count("第一行正文") == 1
    assert view.body == content


def test_source_in_title_only_when_someone_else_told_it():
    own = injection.prepare_view(
        "session/user-111.md",
        memory("user-111", "本人", "工程师", source="111"),
        NOW,
    )
    assert "来源" not in injection.render_title(own, NOW)

    relayed = injection.prepare_view(
        "session/user-111.md",
        memory("user-111", "本人", "工程师", source="111, 222"),
        NOW,
    )
    title = injection.render_title(relayed, NOW)
    assert "来源 222" in title
    assert "来源 111" not in title

    # 非人物文件没有"文件所记的人"可比，标题里不带来源
    project = injection.prepare_view(
        "session/project.md",
        memory("project", "项目", "进度", "project", source="222"),
        NOW,
    )
    assert "来源" not in injection.render_title(project, NOW)


def test_shared_marker_in_title():
    view = injection.prepare_view(
        "people/user-1.md", memory("user-1", "阿杰", "群主"), NOW, shared=True
    )
    assert injection.render_title(view, NOW) == (
        "### people/user-1.md（user｜多个机器人共享｜今天写入）"
    )


def test_opening_block_appends_profiles_inside_index_block():
    index = "- [a](a.md) — 会话记忆"
    profile = injection.prepare_view(
        "people/user-1.md", memory("user-1", "阿杰", "工程师"), NOW
    )
    block = injection.build_opening_block(None, index, [profile], NOW)
    assert block.startswith(prompts.INDEX_BLOCK_OPEN)
    assert block.endswith(prompts.INDEX_BLOCK_CLOSE)
    assert "- [a](a.md) — 会话记忆" in block
    assert injection.PROFILE_HEADING in block
    assert "### people/user-1.md（user｜今天写入）" in block
    assert "工程师" in block
    # 没有档案时与原来的索引块完全相同
    assert injection.build_opening_block(None, index, [], NOW) == (
        prompts.build_index_block(None, index)
    )
    # 索引为空但有人物档案时仍要注入开头块
    only_profile = injection.build_opening_block(None, "", [profile], NOW)
    assert only_profile is not None
    assert injection.PROFILE_HEADING in only_profile


def test_indexed_names_accepts_both_line_formats():
    old = "- [a](a.md) — 描述\n- [b](b.md) — 描述"
    new = "- c.md — 描述\n- d.md"
    assert injection.indexed_names(old) == {"a.md", "b.md"}
    assert injection.indexed_names(new) == {"c.md", "d.md"}
    assert injection.indexed_names(old, new) == {"a.md", "b.md", "c.md", "d.md"}
