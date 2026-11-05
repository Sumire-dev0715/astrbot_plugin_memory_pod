"""3.8 配置项：只保留本轮仍在用的键，上下文管理相关配置不再出现。"""

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
    for key in load("_conf_schema.json"):
        assert key in config, f"{key} 缺少英文翻译"
        assert config[key]["description"]
        assert config[key]["hint"]


def test_schema_has_exactly_the_remaining_configs():
    """3.8.1 / 3.8.7 删掉的配置不能以任何形式留下；用完整键集守住。"""
    expected = {
        "enable_memory",
        "enable_global_memory",
        "recall_max_files",
        "recall_full_files",
        "memory_provider_id",
        "auto_consolidate",
        "auto_consolidate_hours",
        "auto_extract_interval",
        "extract_idle_minutes",
        "shared_people_bots",
        "history_keep_days",
        "trash_keep_days",
        "enable_forgetting",
        "forget_after_days",
    }
    assert set(load("_conf_schema.json")) == expected


def test_recall_hint_has_no_model_selection():
    """召回只按关键词；配置说明不再提模型挑选，填 0 即关闭。"""
    hint = load("_conf_schema.json")["recall_max_files"]["hint"]
    assert "模型" not in hint
    assert "填 0" in hint


def test_metadata_help_no_longer_asks_to_change_astrbot():
    """插件说明不再要求用户调整 AstrBot 的内置设置，也不再有压缩指令。"""
    text = (ROOT / "metadata.yaml").read_text("utf-8")
    assert "按 README 调整 AstrBot" not in text
    assert "上下文压缩" not in text
    assert "/mb_reset" not in text and "/mb_probe" not in text
