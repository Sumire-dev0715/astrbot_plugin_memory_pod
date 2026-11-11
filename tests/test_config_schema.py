"""3.8 配置项：只保留仍在用的键，旧作用域与后台整理配置不再出现。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((ROOT / name).read_text("utf-8"))


def test_schema_has_exactly_the_remaining_configs():
    expected = {
        "enable_memory",
        "recall_max_files",
        "recall_full_files",
        "memory_reminder_turns",
        "history_keep_days",
        "trash_keep_days",
        "enable_forgetting",
        "forget_after_days",
    }
    assert set(load("_conf_schema.json")) == expected


def test_new_configs_have_english_translations():
    config = load(".astrbot-plugin/i18n/en-US.json")["config"]
    for key in load("_conf_schema.json"):
        assert key in config, f"{key} 缺少英文翻译"
        assert config[key]["description"]
        assert config[key]["hint"]


def test_recall_hint_has_no_model_selection():
    hint = load("_conf_schema.json")["recall_max_files"]["hint"]
    assert "模型" not in hint
    assert "填 0" in hint


def test_enable_memory_hint_names_three_layers_and_current_tools():
    hint = load("_conf_schema.json")["enable_memory"]["hint"]
    for name in (
        "memory_read",
        "memory_search",
        "memory_write",
        "memory_edit",
        "memory_delete",
    ):
        assert name in hint
    for layer in ("留言板", "小本本", "悄悄话"):
        assert layer in hint


def test_reminder_turns_config_defaults_to_10_and_can_be_disabled():
    schema = load("_conf_schema.json")["memory_reminder_turns"]
    assert schema["default"] == 10
    assert schema["type"] == "int"
    assert "填 0" in schema["hint"]
    assert "截断或压缩" in schema["hint"]


def test_metadata_version_and_no_removed_features():
    text = (ROOT / "metadata.yaml").read_text("utf-8")
    # 只锁大版本：每次部署都会改 dev 号，写死具体版本会让部署后的测试变红
    assert re.search(r"^version: v2\.\d+\.\d+", text, re.M)
    assert "共享人物空间" not in text
    assert "memory_note" not in text
    assert "后台整理写入" not in text
    assert "三层记忆" in text


def test_i18n_page_translations_have_no_removed_scopes():
    for name in ("zh-CN.json", "en-US.json"):
        pages = load(".astrbot-plugin/i18n/" + name)["pages"]["memory"]
        for key in ("globalTitle", "peopleTitle", "peopleSharedTitle", "tidyAskTitle"):
            assert key not in pages
        for key in ("boardTitle", "notebookTitle", "whisperTitle", "publishTitle"):
            assert key in pages


def test_english_consolidate_all_hint_has_no_global_scope():
    pages = load(".astrbot-plugin/i18n/en-US.json")["pages"]["memory"]
    hint = pages["consolidateAllHint"].lower()
    assert "global" not in hint
    assert "every scope" in hint


def test_dependent_configs_use_condition_so_the_page_can_hide_them():
    """长记忆关掉时下面的选项不可改；关掉遗忘时遗忘天数不可改。

    WebUI 的配置表单按 condition 里列的「键: 值」全部匹配才显示该项，
    所以这里锁住：除了总开关本身，每一项都依赖 enable_memory；遗忘天数
    还依赖 enable_forgetting。
    """
    schema = load("_conf_schema.json")
    for key, item in schema.items():
        if key == "enable_memory":
            assert "condition" not in item
            continue
        assert item.get("condition", {}).get("enable_memory") is True, key
    assert schema["forget_after_days"]["condition"] == {
        "enable_memory": True,
        "enable_forgetting": True,
    }


def test_numeric_configs_use_sliders_with_the_documented_ranges():
    schema = load("_conf_schema.json")
    for key in (
        "recall_max_files",
        "recall_full_files",
        "memory_reminder_turns",
        "forget_after_days",
        "history_keep_days",
        "trash_keep_days",
    ):
        item = schema[key]
        assert item["type"] == "int", key
        slider = item.get("slider") or {}
        assert set(slider) == {"min", "max", "step"}, key
        assert slider["min"] <= item["default"] <= slider["max"], key
