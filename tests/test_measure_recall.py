"""报价脚本 scripts/measure_recall.py：合成存档与记忆上的统计口径。

全部数据在临时目录里合成；断言只看数字，并检查输出里不出现任何记忆或
对话正文。
"""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

from core import memstore, prompts, recall

ROOT = Path(__file__).resolve().parent.parent
UMO = "Bot-A:FriendMessage:100123"
KEY = memstore.safe_key(UMO)
AT = 1_760_000_000.0
DAY = 86400


def _load_script():
    path = ROOT / "scripts" / "measure_recall.py"
    spec = importlib.util.spec_from_file_location("measure_recall", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


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


def write_file(path: Path, text: str, mtime: float = AT - 30 * DAY):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def write_archive(
    root: Path,
    archive_id: str,
    messages: list[dict],
    *,
    created_at: float = AT,
    umo: str | None = UMO,
    key: str = KEY,
):
    payload = {
        "id": archive_id,
        "created_at": created_at,
        "count": len(messages),
        "summary": "",
        "messages": messages,
    }
    if umo is not None:
        payload["umo"] = umo
    text = json.dumps(payload, ensure_ascii=False)
    write_file(root / "archive" / key / f"{archive_id}.json", text)


def build_data(root: Path, *, with_tagged_round: bool = True):
    """两轮同题、一轮闲聊；人物档案与长记忆各一份。"""
    session = root / "memories" / "session" / KEY
    write_file(
        session / "weekend-game.md",
        memory("weekend-game", "每周六晚上的开黑活动", "晚上八点集合，秘密正文标记XYZ"),
    )
    write_file(
        session / "long.md",
        memory("long", "开黑活动长期安排", "开黑" * 2500),
    )
    write_file(
        session / memstore.INDEX_FILENAME,
        "# 会话记忆索引\n- [开黑](weekend-game.md) — 每周六晚上的开黑活动\n",
    )
    people = root / "memories" / "people" / "Bot-A"
    write_file(
        people / "user-100123.md",
        memory("user-100123", "阿杰，群主", "喜欢开黑，秘密正文标记XYZ"),
    )
    first = "[发送者：QQ名 阿杰｜QQ号 100123]\n这周六还开黑吗"
    if not with_tagged_round:
        first = "这周六还开黑吗"
    write_archive(
        root,
        "20260101-000000-000",
        [
            {"role": "user", "text": first},
            {"role": "assistant", "text": "在的"},
            {"role": "user", "text": "这周六还开黑吗"},
            {"role": "user", "text": "今天吃什么（用户对话标记ABC）"},
        ],
    )


def tree_digest(root: Path) -> str:
    """目录内容的哈希：验证测量脚本只读。"""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


def test_measure_counts_rounds_files_and_pairs(tmp_path):
    build_data(tmp_path)
    report = script.measure(tmp_path)
    assert report.rounds == 3
    assert report.sessions == 1
    assert report.archives == 1
    assert report.skipped_archives == 0
    # 人物档案钉在开头块，不再进入每轮召回：
    # 前两轮：两条会话记忆，其中 long 头尾瘦身；第三轮闲聊没有相关记忆
    assert report.recall_files == [2, 2, 0]
    assert report.full_files == [1, 1, 0]
    assert report.pointer_files == [0, 0, 0]
    assert report.truncations == 2
    # 两轮同题召回同一批文件；第三轮不同
    assert report.adjacent_pairs == 2
    assert report.identical_pairs == 1
    # 发送者 ID 从标注取；没有标注时用私聊会话键里的用户 ID，结果一致
    assert report.recall_chars[0] == report.recall_chars[1] > 0
    assert report.recall_chars[2] == 0
    # 开头块 = 会话索引 + 私聊发送者人物档案；三轮共用同一快照
    assert len(set(report.opening_chars)) == 1
    assert all(profile > 0 for profile in report.profile_chars)
    assert report.opening_chars[0] > len(
        prompts.build_index_block(
            None,
            (tmp_path / "memories" / "session" / KEY / memstore.INDEX_FILENAME).read_text(
                "utf-8"
            ),
        )
    )


def test_report_text_has_numbers_only(tmp_path):
    build_data(tmp_path)
    text = script.format_report(script.measure(tmp_path))
    assert "轮数：3" in text
    assert "跳过的存档：0" in text
    assert "每轮召回字数：平均" in text
    assert "平均全文文件数" in text
    assert "平均召回文件数（全文+指针）：1.33（其中指针 0.00）" in text
    assert "截断次数：2" in text
    assert "相邻两轮召回同一批文件的比例：50.0%（共 2 对）" in text
    assert "其中人物档案 平均 0.0" not in text
    # 隐私：记忆正文与对话正文都不能出现在输出里
    for secret in ("秘密正文标记XYZ", "用户对话标记ABC", "这周六还开黑吗", "阿杰"):
        assert secret not in text


def test_measure_is_read_only(tmp_path):
    build_data(tmp_path)
    before = tree_digest(tmp_path)
    script.measure(tmp_path)
    assert tree_digest(tmp_path) == before


def test_sender_falls_back_to_private_session_key(tmp_path):
    build_data(tmp_path, with_tagged_round=False)
    report = script.measure(tmp_path)
    # 没有标注时，私聊会话键里的目标 100123 仍是发送者，人物档案照常钉在开头块
    assert report.recall_files == [2, 2, 0]
    assert report.full_files[0] == 1
    assert all(profile > 0 for profile in report.profile_chars)


def test_no_people_excludes_profile(tmp_path):
    build_data(tmp_path)
    with_people = script.measure(tmp_path)
    without = script.measure(tmp_path, people=False)
    # 人物记忆关闭时 pin 也不生效：开头块只有索引，没有人物档案
    assert without.profile_chars == [0, 0, 0]
    assert without.opening_chars[0] < with_people.opening_chars[0]
    assert without.recall_files == with_people.recall_files == [2, 2, 0]


def test_group_message_without_tag_has_no_forced_identity(tmp_path):
    umo = "Bot-A:GroupMessage:555"
    key = memstore.safe_key(umo)
    write_archive(
        tmp_path,
        "20260102-000000-000",
        [{"role": "user", "text": "今天吃什么"}],
        umo=umo,
        key=key,
    )
    report = script.measure(tmp_path)
    assert report.rounds == 1
    assert report.recall_chars == [0]
    assert report.recall_files == [0]


def test_archive_without_umo_reconstructs_from_key(tmp_path):
    write_archive(
        tmp_path,
        "20260103-000000-000",
        [{"role": "user", "text": "这周六还开黑吗"}],
        umo=None,
    )
    report = script.measure(tmp_path)
    assert report.rounds == 1
    assert report.sessions == 1


def test_global_memory_flag(tmp_path):
    write_archive(
        tmp_path,
        "20260101-000000-000",
        [{"role": "user", "text": "开黑吗"}],
    )
    write_file(
        tmp_path / "memories" / "global" / "self.md",
        memory("self", "开黑时的自我设定", "不主动插话"),
    )
    write_file(
        tmp_path / "memories" / "global" / memstore.INDEX_FILENAME,
        "- [自我](self.md) — 开黑时的自我设定\n",
    )
    plain = script.measure(tmp_path)
    extended = script.measure(tmp_path, global_memory=True)
    assert plain.recall_files == [0]
    assert extended.recall_files == [1]
    assert extended.opening_chars[0] > plain.opening_chars[0]


def test_recall_full_files_tiering_reports_full_and_pointer_counts(tmp_path):
    session = tmp_path / "memories" / "session" / KEY
    for index in range(1, 5):
        write_file(
            session / f"game-{index}.md",
            memory(
                f"game-{index}",
                f"开黑活动安排{index}",
                (f"周六晚上集合{index}" * 100),
            ),
        )
    write_archive(
        tmp_path,
        "20260105-000000-000",
        [{"role": "user", "text": "这周六开黑吗"}],
    )
    reports = [
        script.measure(
            tmp_path,
            people=False,
            pin_private_profile=False,
            full_files=count,
        )
        for count in (1, 2, 3)
    ]
    assert [report.full_files for report in reports] == [[1], [2], [3]]
    assert [report.pointer_files for report in reports] == [[3], [2], [1]]
    # 全文越多，召回块字数越多
    assert (
        reports[0].recall_chars[0]
        < reports[1].recall_chars[0]
        < reports[2].recall_chars[0]
    )


def test_percentile_nearest_rank_and_median():
    assert script.percentile([], 0.9) == 0.0
    assert script.percentile([10], 0.9) == 10.0
    assert script.percentile([1, 2, 3, 4, 5], 0.9) == 5.0
    assert script.percentile(list(range(10)), 0.9) == 8.0
    # 中位数按统计惯例：偶数个样本取中间两个的平均，不是最近秩
    assert script.median([4, 1, 3, 2]) == 2.5
    assert script.median([1, 2, 3]) == 2.0
    assert script.median([]) == 0.0


def test_injected_blocks_are_not_counted_as_rounds(tmp_path):
    write_archive(
        tmp_path,
        "20260109-000000-000",
        [
            {
                "role": "user",
                "text": prompts.INDEX_BLOCK_OPEN
                + "\n旧索引\n"
                + prompts.INDEX_BLOCK_CLOSE,
            },
            {
                "role": "user",
                "text": prompts.SUMMARY_BLOCK_OPEN
                + "\n旧摘要\n"
                + prompts.SUMMARY_BLOCK_CLOSE,
            },
            {"role": "assistant", "text": "好"},
            {"role": "user", "text": "你好"},
        ],
    )
    report = script.measure(tmp_path)
    assert report.rounds == 1


def test_unreadable_archive_is_counted_as_skipped(tmp_path):
    write_archive(
        tmp_path, "20260107-000000-000", [{"role": "user", "text": "你好"}]
    )
    (tmp_path / "archive" / KEY / "bad_id.json").write_text(
        "{不是 JSON", encoding="utf-8"
    )
    report = script.measure(tmp_path)
    assert report.rounds == 1
    assert report.archives == 1
    assert report.skipped_archives == 1
    assert "跳过的存档：1" in script.format_report(report)


def test_session_without_umo_is_counted_as_skipped(tmp_path):
    write_archive(
        tmp_path,
        "20260108-000000-000",
        [{"role": "user", "text": "你好"}],
        umo=None,
        key="无法还原的目录名",
    )
    report = script.measure(tmp_path)
    assert report.rounds == 0
    assert report.skipped_archives == 1
    assert "跳过的存档：1" in script.format_report(report)


def test_session_umo_prefers_folder_key_after_platform_rename(tmp_path):
    """平台改名迁移后归档 payload 里还是旧 umo，测量要读新目录的会话记忆。"""
    new_umo = "Bot-B:GroupMessage:555555"
    key = memstore.safe_key(new_umo)
    write_file(
        tmp_path / "memories" / "session" / key / "weekend-game.md",
        memory("weekend-game", "开黑活动", "周六晚上"),
    )
    write_archive(
        tmp_path,
        "20260110-000000-000",
        [{"role": "user", "text": "开黑吗"}],
        umo="Bot-A:FriendMessage:100123",
        key=key,
    )
    report = script.measure(tmp_path)
    assert report.sessions == 1
    assert report.rounds == 1
    assert report.recall_files == [1]


def test_malformed_created_at_does_not_crash_or_leak(tmp_path, capsys):
    write_archive(
        tmp_path, "20260106-000000-000", [{"role": "user", "text": "你好"}]
    )
    path = tmp_path / "archive" / KEY / "20260106-000000-000.json"
    data = json.loads(path.read_text("utf-8"))
    data["created_at"] = "对话正文秘密SECRET"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    report = script.measure(tmp_path)
    assert report.rounds == 1
    assert "SECRET" not in script.format_report(report)

    assert script.main(["--data-dir", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err


def test_cli_hides_exception_details(tmp_path, capsys, monkeypatch):
    (tmp_path / "memories").mkdir()

    def boom(_data_dir, **_kwargs):
        raise RuntimeError("记忆正文SECRET")

    monkeypatch.setattr(script, "measure", boom)
    assert script.main(["--data-dir", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert "SECRET" not in captured.out + captured.err
    assert "RuntimeError" in captured.err


def test_cli_prints_report_and_tiering(tmp_path, capsys):
    build_data(tmp_path)
    assert script.main(["--data-dir", str(tmp_path)]) == 0
    default = capsys.readouterr()
    assert "轮数：3" in default.out
    assert default.err == ""

    # 默认 2 与显式 2 完全一致；1 把第二篇降为指针，成本更低
    assert script.main(["--data-dir", str(tmp_path), "--recall-full-files", "2"]) == 0
    explicit = capsys.readouterr()
    assert explicit.out == default.out
    assert explicit.err == ""

    assert script.main(["--data-dir", str(tmp_path), "--recall-full-files", "1"]) == 0
    tighter = capsys.readouterr()
    assert tighter.out != default.out
    assert "其中指针 0.67" in tighter.out

    # 0 合法：非身份文件全部只给指针；负数与不存在的目录仍是参数错误
    assert script.main(["--data-dir", str(tmp_path), "--recall-full-files", "0"]) == 0
    assert script.main(["--data-dir", str(tmp_path), "--recall-full-files", "-1"]) == 2
    assert script.main(["--data-dir", str(tmp_path / "missing")]) == 2


def padded_memory(name, description, total: int) -> str:
    """正文（去掉 frontmatter 后）总长正好 total 字的合成记忆。"""
    return memory(name, description, "开" * total)


def test_truncation_threshold_matches_recall_constant(tmp_path):
    """去掉 frontmatter 后的正文等于 FILE_CHARS 不算截断，多一个字才算。"""
    session = tmp_path / "memories" / "session" / KEY
    write_file(
        session / "edge.md",
        padded_memory("edge", "开黑活动安排", recall.FILE_CHARS),
    )
    write_file(
        session / "over.md",
        padded_memory("over", "开黑活动记录", recall.FILE_CHARS + 1),
    )
    write_archive(
        tmp_path,
        "20260104-000000-000",
        [{"role": "user", "text": "开黑活动"}],
    )
    report = script.measure(tmp_path)
    assert report.recall_files == [2]
    assert report.truncations == 1
    assert report.full_files == [1]
