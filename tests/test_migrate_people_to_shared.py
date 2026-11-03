"""迁移脚本：把各平台 people 目录复制进共享人物空间。

全部用临时目录里的合成人物记忆，不接触真实数据。
"""

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_script():
    path = ROOT / "scripts" / "migrate_people_to_shared.py"
    spec = importlib.util.spec_from_file_location("migrate_people_to_shared", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()

SOURCE_MTIME = 1_700_000_000.0


def person(name, body):
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {name} 的合成描述\n"
        "metadata:\n"
        "  type: user\n"
        "---\n\n"
        f"{body}\n"
    )


def write_person(
    root: Path,
    name: str,
    body: str,
    uses: int = 1,
    mtime: float = SOURCE_MTIME,
    usage_root: Path | None = None,
    forgotten_at: float = 0.0,
):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(person(name[: -len(".md")], body), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    usage_root = usage_root or root
    usage_path = usage_root / ".usage.json"
    data = {}
    if usage_path.is_file():
        data = json.loads(usage_path.read_text("utf-8"))
    entry = {"last_used": mtime + uses, "uses": uses}
    if forgotten_at:
        entry["forgotten_at"] = forgotten_at
    data[name] = entry
    usage_path.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return path


def source_dir(data_dir: Path, platform: str) -> Path:
    return data_dir / "memories" / "people" / script.memstore.safe_key(platform)


def shared_dir(data_dir: Path) -> Path:
    return data_dir / "memories" / "people" / script.sharing.SHARED_KEY


def make_sources(tmp_path: Path) -> tuple[Path, Path, Path]:
    data = tmp_path / "plugin_data"
    a = source_dir(data, "Bot-A")
    b = source_dir(data, "Bot-B")
    write_person(a, "user-1.md", "A 的合成人物一", uses=3)
    write_person(a, "user-2.md", "A 版本的合成人物二", uses=2)
    write_person(b, "user-2.md", "B 版本的合成人物二", uses=7)
    write_person(b, "user-3.md", "B 的合成人物三", uses=5)
    return data, a, b


def make_data(tmp_path: Path) -> tuple[Path, Path, Path]:
    data, a, b = make_sources(tmp_path)
    write_person(a, "user-4.md", "A 版本的合成人物四", uses=4)
    # 共享目录里已经有一份同名记忆，也不能被覆盖；此时还没有 MEMORY.md
    write_person(shared_dir(data), "user-4.md", "共享目录原有的合成人物四", uses=9)
    return data, a, b


def run_main(args, capsys):
    code = script.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_dry_run_only_reports_and_writes_nothing(tmp_path, capsys):
    data, a, b = make_sources(tmp_path)
    before_a = sorted(p.name for p in a.iterdir())
    before_b = sorted(p.name for p in b.iterdir())

    code, out, err = run_main(
        ["--data-dir", str(data), "--from", "Bot-A", "--from", "Bot-B"],
        capsys,
    )

    assert code == 0 and err == ""
    # user-1、user-3 可复制；user-2 两个来源都有
    assert "计划复制 2 条，冲突 1 条" in out
    assert "user-2.md" in out
    # 不打印正文
    assert "A 的合成人物一" not in out and "B 版本的合成人物二" not in out
    assert sorted(p.name for p in a.iterdir()) == before_a
    assert sorted(p.name for p in b.iterdir()) == before_b
    # dry-run 连共享目录都不创建
    assert not shared_dir(data).exists()


def test_apply_copies_content_usage_and_timestamps_with_audit(tmp_path, capsys):
    data, a, b = make_data(tmp_path)
    original_shared = (shared_dir(data) / "user-4.md").read_text("utf-8")

    code, out, err = run_main(
        [
            "--data-dir",
            str(data),
            "--from",
            "Bot-A",
            "--from",
            "Bot-B",
            "--apply",
        ],
        capsys,
    )

    assert code == 0 and err == ""
    assert "复制 2 条，冲突 2 条" in out
    assert "A 的合成人物一" not in out

    shared = shared_dir(data)
    assert set(shared.glob("user-*.md")) == {
        shared / "user-1.md",
        shared / "user-3.md",
        shared / "user-4.md",
    }
    # 冲突的 user-2 没有被任何一个来源覆盖
    assert not (shared / "user-2.md").exists()
    assert (shared / "user-4.md").read_text("utf-8") == original_shared
    # 原目录保留，一个文件都不少
    assert {p.name for p in a.glob("user-*.md")} == {
        "user-1.md",
        "user-2.md",
        "user-4.md",
    }
    assert {p.name for p in b.glob("user-*.md")} == {"user-2.md", "user-3.md"}

    # 正文、写入时间与使用统计原样带过去
    copied = (shared / "user-1.md").read_text("utf-8")
    assert "A 的合成人物一" in copied
    assert abs((shared / "user-1.md").stat().st_mtime - SOURCE_MTIME) < 1
    usage = json.loads((shared / ".usage.json").read_text("utf-8"))
    assert usage["user-1.md"]["uses"] == 3
    assert usage["user-1.md"]["last_used"] == SOURCE_MTIME + 3
    assert usage["user-3.md"]["uses"] == 5

    # 3.1 的审计：每份写入一行 import，actor=transfer，前后哈希可核对
    rows = [
        json.loads(line)
        for line in (shared / ".audit.jsonl").read_text("utf-8").splitlines()
    ]
    imports = {
        row["file"]: row
        for row in rows
        if row.get("action") == script.history.ACTION_IMPORT
    }
    assert set(imports) == {"user-1.md", "user-3.md"}
    assert all(row["actor"] == script.history.ACTOR_TRANSFER for row in imports.values())
    assert imports["user-1.md"]["after_sha1"] == hashlib.sha1(
        copied.encode("utf-8")
    ).hexdigest()
    assert imports["user-1.md"]["before_sha1"] == ""

    # 共享目录原有的 user-4 没有索引行：apply 后要按磁盘文件补齐
    index = (shared / "MEMORY.md").read_text("utf-8")
    for name in ("user-1.md", "user-3.md", "user-4.md"):
        assert f"]({name})" in index


def test_missing_source_directory_is_an_error(tmp_path, capsys):
    data = tmp_path / "plugin_data"
    write_person(source_dir(data, "Bot-A"), "user-1.md", "合成人物一")

    # 单独一个拼错的来源
    code, out, err = run_main(
        ["--data-dir", str(data), "--from", "Bot-Typo", "--apply"], capsys
    )
    assert code == 2
    assert "不存在" in err and "Bot-Typo" in err
    assert not shared_dir(data).exists()

    # 有效来源混着一个拼错的来源：整次失败，一个文件都不复制
    code, out, err = run_main(
        [
            "--data-dir",
            str(data),
            "--from",
            "Bot-A",
            "--from",
            "Bot-Typo",
            "--apply",
        ],
        capsys,
    )
    assert code == 2
    assert "Bot-Typo" in err
    assert out.strip() == ""
    assert not shared_dir(data).exists()
    assert (source_dir(data, "Bot-A") / "user-1.md").is_file()


def test_apply_is_idempotent_and_reports_existing_targets(tmp_path, capsys):
    data, a, b = make_sources(tmp_path)
    args = ["--data-dir", str(data), "--from", "Bot-A", "--from", "Bot-B", "--apply"]

    first_code, first_out, _ = run_main(args, capsys)
    assert first_code == 0 and "复制 2 条，冲突 1 条" in first_out
    shared = shared_dir(data)
    before = {p.name: p.read_text("utf-8") for p in shared.glob("user-*.md")}
    before_index = (shared / "MEMORY.md").read_text("utf-8")

    # 再跑一次：目标里已经有同名文件，全部按冲突处理，不覆盖也不重复
    code, out, _ = run_main(args, capsys)
    assert code == 0
    assert "复制 0 条，冲突 3 条" in out
    assert {p.name: p.read_text("utf-8") for p in shared.glob("user-*.md")} == before
    assert (shared / "MEMORY.md").read_text("utf-8") == before_index


def test_apply_migrates_forgotten_person_files(tmp_path, capsys):
    data = tmp_path / "plugin_data"
    a = source_dir(data, "Bot-A")
    write_person(a, "user-7.md", "在册的合成人物七", uses=1)
    write_person(
        a / "forgotten",
        "user-8.md",
        "已遗忘的合成人物八",
        uses=2,
        mtime=SOURCE_MTIME + 50,
        usage_root=a,
        forgotten_at=SOURCE_MTIME + 100,
    )

    dry_code, dry_out, _ = run_main(
        ["--data-dir", str(data), "--from", "Bot-A"], capsys
    )
    assert dry_code == 0 and "计划复制 2 条，冲突 0 条" in dry_out
    assert not shared_dir(data).exists()

    code, out, _ = run_main(
        ["--data-dir", str(data), "--from", "Bot-A", "--apply"], capsys
    )

    assert code == 0
    assert "复制 2 条，冲突 0 条" in out
    shared = shared_dir(data)
    assert (shared / "user-7.md").is_file()
    assert not (shared / "user-8.md").exists()
    copied = shared / "forgotten" / "user-8.md"
    assert copied.is_file()
    assert "已遗忘的合成人物八" in copied.read_text("utf-8")
    assert abs(copied.stat().st_mtime - (SOURCE_MTIME + 50)) < 1
    usage = json.loads((shared / ".usage.json").read_text("utf-8"))
    assert usage["user-8.md"]["uses"] == 2
    assert usage["user-8.md"]["forgotten_at"] == SOURCE_MTIME + 100
    # 原目录里的文件与遗忘副本都保留
    assert (a / "user-7.md").is_file()
    assert (a / "forgotten" / "user-8.md").is_file()


def test_source_directory_can_be_given_by_scope_key(tmp_path, capsys):
    data = tmp_path / "plugin_data"
    write_person(source_dir(data, "Bot-深海小鲸"), "user-5.md", "合成人物五")
    key = script.memstore.safe_key("Bot-深海小鲸")

    code, out, _ = run_main(
        ["--data-dir", str(data), "--from", key, "--apply"], capsys
    )

    assert code == 0
    assert "复制 1 条，冲突 0 条" in out
    assert (shared_dir(data) / "user-5.md").is_file()


def test_rejects_shared_directory_as_source(tmp_path, capsys):
    data = tmp_path / "plugin_data"
    (data / "memories" / "people" / "_shared").mkdir(parents=True)

    code, _, err = run_main(
        ["--data-dir", str(data), "--from", "_shared"], capsys
    )

    assert code == 2
    assert "_shared" in err


def test_missing_memories_directory_is_reported(tmp_path, capsys):
    code, _, err = run_main(
        ["--data-dir", str(tmp_path / "nope"), "--from", "Bot-A"], capsys
    )

    assert code == 2
    assert "memories" in err
