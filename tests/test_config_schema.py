"""3.6 新增配置项的 schema 与英文翻译。"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load(name):
    return json.loads((ROOT / name).read_text("utf-8"))


def test_pin_private_profile_default_on():
    entry = load("_conf_schema.json")["pin_private_profile"]
    assert entry["type"] == "bool"
    assert entry["default"] is True
    assert entry["description"] and entry["hint"]


def test_recall_full_files_default_two():
    entry = load("_conf_schema.json")["recall_full_files"]
    assert entry["type"] == "int"
    assert entry["default"] == 2
    assert entry["description"] and entry["hint"]


def test_new_configs_have_english_translations():
    config = load(".astrbot-plugin/i18n/en-US.json")["config"]
    for key in ("pin_private_profile", "recall_full_files"):
        assert config[key]["description"]
        assert config[key]["hint"]
