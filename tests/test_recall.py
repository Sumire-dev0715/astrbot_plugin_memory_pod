from core import recall
from core.memstore import MemoryHeader
from core.recall import Candidate, Recalled

NOW = 1_800_000_000.0
DAY = 86400


def header(file, description="", body="", mtime=NOW, mem_type="user"):
    name = file[:-3]
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n{body}".lower()
    return MemoryHeader(
        file=file,
        name=name,
        description=description,
        type=mem_type,
        mtime=mtime,
        size=len(text),
        text=text,
    )


def cand(file, description="", body="", scope="session", mtime=NOW):
    return Candidate(scope, header(file, description, body, mtime))


# ---------------------------------------------------------------- 分词与打分


def test_tokenize_mixes_words_and_cjk_bigrams():
    tokens = recall.tokenize("Golang 后端工程师 2024 123456789")
    assert "golang" in tokens
    assert {"后端", "端工", "工程", "程师"} <= tokens
    assert "2024" not in tokens  # 短数字噪声大，不计
    assert "123456789" in tokens  # 像 ID 的长数字保留


def test_score_weights_description_over_body():
    query = recall.tokenize("周六开黑")
    in_desc = header("a.md", description="每周六晚上的开黑活动")
    in_body = header("b.md", description="无关", body="每周六晚上的开黑活动")
    assert recall.score(query, in_desc) > recall.score(query, in_body) > 0
    assert recall.score(set(), in_desc) == 0


def test_identity_files_dedupes_and_keeps_order():
    files = recall.identity_files("123456", ["654321", "123456"], "提到 777777 和 12")
    assert files == ["user-123456.md", "user-654321.md", "user-777777.md"]
    assert recall.identity_files("", [], "") == []
    assert recall.identity_files("abc", [], "") == []


# ---------------------------------------------------------------- 关键词挑选


def test_select_by_keywords_forced_first_then_scored():
    cands = [
        cand("project-game.md", "每周六晚上的开黑活动"),
        cand("user-123456.md", "群主阿杰"),
        cand("reference-rules.md", "群规公告的位置"),
    ]
    chosen = recall.select_by_keywords(cands, "这周六还开黑吗", ["user-123456.md"], 5)
    assert [c.header.file for c in chosen] == ["user-123456.md", "project-game.md"]


def test_select_by_keywords_never_forces_global_user_files():
    cands = [cand("user-123456.md", "x", scope="global")]
    assert recall.select_by_keywords(cands, "", ["user-123456.md"], 5) == []


def test_select_by_keywords_people_only_by_identity():
    cands = [
        cand("user-123456.md", "阿杰，开黑常客", scope="people"),
        cand("user-654321.md", "小鱼，也爱开黑", scope="people"),
        cand("project-game.md", "每周六晚上的开黑活动"),
    ]
    chosen = recall.select_by_keywords(cands, "这周六还开黑吗", ["user-123456.md"], 5)
    assert [(c.scope, c.header.file) for c in chosen] == [
        ("people", "user-123456.md"),
        ("session", "project-game.md"),
    ]


def test_select_by_keywords_respects_limit_and_prefers_newer_on_tie():
    old = cand("a.md", "开黑活动安排", mtime=NOW - 10 * DAY)
    new = cand("b.md", "开黑活动安排", mtime=NOW)
    chosen = recall.select_by_keywords([old, new], "开黑活动", [], 1)
    assert chosen == [new]
    assert recall.select_by_keywords([old, new], "开黑活动", [], 0) == []


def test_select_by_keywords_ignores_weak_body_matches():
    weak = cand("a.md", "无关描述", body="开黑")
    assert recall.select_by_keywords([weak], "开黑", [], 5) == []


# ---------------------------------------------------------------- 注入块



def test_freshness_note_only_for_older_than_a_day():
    assert recall.freshness_note(NOW, now=NOW) == ""
    assert recall.freshness_note(NOW - DAY, now=NOW) == ""
    assert "3 天前" in recall.freshness_note(NOW - 3 * DAY, now=NOW)


def test_age_label():
    assert recall.age_label(NOW, now=NOW) == "今天"
    assert recall.age_label(NOW - DAY, now=NOW) == "昨天"
    assert recall.age_label(NOW - 5 * DAY, now=NOW) == "5 天前"
    assert recall.age_label(NOW + 100, now=NOW) == "今天"


def test_build_recall_block_structure_and_budget():
    items = [
        Recalled("session/a.md", "A" * (recall.FILE_CHARS + 50), NOW - 4 * DAY),
        Recalled("session/b.md", "B" * recall.FILE_CHARS, NOW),
        Recalled("session/c.md", "C" * recall.FILE_CHARS, NOW),
        Recalled("session/d.md", "D" * 10, NOW),
    ]
    block = recall.build_recall_block(items, "[数据]", now=NOW)
    assert block.startswith(recall.RECALL_BLOCK_OPEN + "\n[数据]\n")
    assert block.endswith(recall.RECALL_BLOCK_CLOSE)
    # 标题带类型与写入时间；长说明不再逐条重复
    assert "### session/a.md（未标注｜4 天前写入）" in block
    assert "中间省略" in block
    assert "4 天前，记录的是当时的情况" not in block
    # a（瘦身后略超 3000 字）+ b 之后 c 会超出 9000 字预算；c 与它后面的
    # d 一起转为指针，顺序保持相关度高的在前
    assert "session/b.md" in block
    assert "session/c.md" not in block.split("可能也相关")[0]
    assert (
        "可能也相关，需要细节时用 memory_read 读取：session/c.md、session/d.md"
        in block
    )
    # 指针行里只有引用，不带正文
    assert "C" * 10 not in block and "D" * 10 not in block


def test_build_recall_block_empty():
    assert recall.build_recall_block([], "[数据]") is None
