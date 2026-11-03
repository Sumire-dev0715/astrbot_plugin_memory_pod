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


def write_person(root: Path, name: str, body: str, uses: int = 1):
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(person(name[: -len(".md")], body), encoding="utf-8")
    os.utime(path, (SOURCE_MTIME, SOURCE_MTIME))
    usage_path = root / ".usage.json"
    data = {}
    if usage_path.is_file():
        data = json.loads(usage_path.read_text("utf-8"))
    data[name] = {"last_used": SOURCE_MTIME + uses, "uses": uses}
    usage_path.write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return path


def source_dir(data_dir: Path, platform: str) -> Path:
    return data_dir / "memories" / "people" / script.memstore.safe_key(platform)


def shared_dir(data_dir: Path) -> Path:
    return data_dir / "memories" / "people" / script.sharing.SHARED_KEY


def make_data(tmp_path: Path) -> tuple[Path, Path, Path]:
    data = tmp_path / "plugin_data"
    a = source_dir(data, "Bot-A")
    b = source_dir(data, "Bot-B")
    write_person(a, "user-1.md", "A 的合成人物一", uses=3)
    write_person(a, "user-2.md", "A 版本的合成人物二", uses=2)
    write_person(a, "user-4.md", "A 版本的合成人物四", uses=4)
    write_person(b, "user-2.md", "B 版本的合成人物二", uses=7)
    write_person(b, "user-3.md", "B 的合成人物三", uses=5)
    # 共享目录里已经有一份同名记忆，也不能被覆盖
    write_person(shared_dir(data), "user-4.md", "共享目录原有的合成人物四", uses=9)
    return data, a, b


def run_main(args, capsys):
    code = script.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_dry_run_only_reports_and_writes_nothing(tmp_path, capsys):
    data, a, b = make_data(tmp_path)
    before_a = sorted(p.name for p in a.iterdir())
    before_b = sorted(p.name for p in b.iterdir())
    shared_before = sorted(p.name for p in shared_dir(data).iterdir())

    code, out, err = run_main(
        ["--data-dir", str(data), "--from", "Bot-A", "--from", "Bot-B"],
        capsys,
    )

    assert code == 0 and err == ""
    # user-1、user-3 可复制；user-2 两个来源都有、user-4 共享目录已有
    assert "计划复制 2 条，冲突 2 条" in out
    assert "user-2.md" in out and "user-4.md" in out
    # 不打印正文
    assert "A 的合成人物一" not in out and "B 版本的合成人物二" not in out
    assert "A 版本的合成人物四" not in out
    assert sorted(p.name for p in a.iterdir()) == before_a
    assert sorted(p.name for p in b.iterdir()) == before_b
    assert sorted(p.name for p in shared_dir(data).iterdir()) == shared_before
    assert not (shared_dir(data) / "MEMORY.md").exists()


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
