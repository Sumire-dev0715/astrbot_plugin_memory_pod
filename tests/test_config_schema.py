"""3.7 配置项：删除的开关与归档配置不再出现，窗口说明跟随 AstrBot。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((ROOT / name).read_text("utf-8"))


def test_recall_full_files_default_two():
    entry = load("_conf_schema.json")["recall_full_files"]
    assert entry["type"] == "int"
    assert entry["default"] == 2
    assert entry["description"] and entry["hint"]


def test_new_configs_have_english_translations():
    config = load(".astrbot-plugin/i18n/en-US.json")["config"]
    for key in ("recall_full_files",):
        assert config[key]["description"]
        assert config[key]["hint"]


def test_schema_has_exactly_the_remaining_configs():
    """3.7 删掉的开关与归档配置不能以任何形式留下；用完整键集守住。"""
    expected = {
        "enable_memory",
        "enable_global_memory",
        "recall_mode",
        "recall_max_files",
        "recall_full_files",
        "enable_compression",
        "max_context_tokens",
        "compress_threshold",
        "compress_buffer",
        "keep_recent_turns",
        "summary_provider_id",
        "consolidate_provider_id",
        "auto_consolidate",
        "auto_consolidate_hours",
        "summary_retry_count",
        "summary_timeout",
        "summary_prompt_template",
        "summary_max_chars",
        "extract_memories",
        "auto_extract_interval",
        "shared_people_bots",
        "history_keep_days",
        "trash_keep_days",
        "enable_forgetting",
        "forget_after_days",
        "tool_result_idle_minutes",
        "loop_guard",
    }
    assert set(load("_conf_schema.json")) == expected


def test_window_and_compression_hints_follow_astrbot():
    schema = load("_conf_schema.json")
    window_hint = schema["max_context_tokens"]["hint"]
    assert "跟随 AstrBot" in window_hint
    assert "强烈建议手动填写" not in window_hint
    compression_hint = schema["enable_compression"]["hint"]
    assert "交给 AstrBot 自带" in compression_hint
