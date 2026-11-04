"""prompt_sizes.py 的输出与验收线。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from scripts import prompt_sizes  # noqa: E402


def test_collect_sizes_has_required_fields():
    sizes = prompt_sizes.collect_sizes()
    assert set(sizes["tools"]) == {"memory_read", "memory_search", "memory_note"}
    assert sizes["guidance"] > 0
    assert sizes["tool_total"] == sum(
        item["description"] for item in sizes["tools"].values()
    )
    assert sizes["tool_schema_total"] == sum(
        sum(item.values()) for item in sizes["tools"].values()
    )
    # chat_total 计模型实际看到的完整工具 schema（名称 + description +
    # 参数说明），不是只有 description。
    assert sizes["chat_total"] == (
        sizes["guidance"] + sizes["tool_schema_total"]
    )
    assert sizes["index_shell"] > 0
    assert sizes["recall_shell"] > 0
    assert sizes["background_prompt"] > 0
    assert sizes["within_limit"] is True
    assert sizes["chat_total"] <= 1200
    # 3.7.6 只保留聊天侧行动所需的文本；旧的作用域/遗忘变体说明已删除
    assert "variants" not in sizes
    assert "within_limit_all_variants" not in sizes


def test_cli_json_output():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "prompt_sizes.py"), "--json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["chat_total"] <= 1200
    assert payload["within_limit"] is True
