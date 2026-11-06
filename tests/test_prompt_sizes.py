"""prompt_sizes.py 的输出与 3.8.8 验收线。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from scripts import prompt_sizes  # noqa: E402


def test_collect_sizes_has_required_fields():
    sizes = prompt_sizes.collect_sizes()
    assert set(sizes["tools"]) == {
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_delete",
    }
    assert sizes["guidance"] > 0
    assert sizes["tool_total"] == sum(
        item["description"] for item in sizes["tools"].values()
    )
    assert sizes["tool_schema_total"] == sum(
        sum(item.values()) for item in sizes["tools"].values()
    )
    assert sizes["chat_total"] == (
        sizes["guidance"] + sizes["tool_schema_total"]
    )
    assert sizes["chat_side_total"] == (
        sizes["chat_total"] + sizes["index_shell"] + sizes["recall_shell"]
    )
    assert sizes["index_shell"] > 0
    assert sizes["recall_shell"] > 0
    assert sizes["within_limit"] is True
    assert sizes["chat_total"] <= 2000
    # 3.8.8：记忆规范 + 开头块里的说明 + 5 个工具说明合计也不超过 2000
    assert sizes["chat_side_total"] <= 2000
    assert "background_prompt" not in sizes
    # AstrBot 的 description 包含短描述与长描述，不能只量第一段
    assert sizes["tools"]["memory_write"]["description"] > 38
    assert sizes["tools"]["memory_edit"]["description"] > 100


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
    assert payload["chat_total"] <= 2000
    assert payload["chat_side_total"] <= 2000
    assert payload["within_limit"] is True
