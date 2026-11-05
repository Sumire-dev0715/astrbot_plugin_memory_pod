"""3.8 配置项：只保留仍在用的键，旧作用域与后台整理配置不再出现。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((ROOT / name).read_text("utf-8"))


def test_schema_has_exactly_the_remaining_configs():
    expected = {
        "enable_memory",
        "recall_max_files",
        "recall_full_files",
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


def test_enable_memory_hint_names_three_layers_and_five_tools():
    hint = load("_conf_schema.json")["enable_memory"]["hint"]
    for name in ("memory_read", "memory_search", "memory_write", "memory_edit", "memory_delete"):
        assert name in hint
    for layer in ("留言板", "小本本", "悄悄话"):
        assert layer in hint


def test_metadata_version_and_no_removed_features():
    text = (ROOT / "metadata.yaml").read_text("utf-8")
    assert "version: v2.0.0-dev.2" in text
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
