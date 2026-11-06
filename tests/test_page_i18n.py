"""管理页的多语言：页面里的简体中文缺省值、两个语言文件、代码里用到的键要完全对得上。

沿用插件的多语言机制：.astrbot-plugin/i18n/<语言>.json 的 pages.memory 经桥接对象 t() 取，
页面里的 ZH 字典只是桥接取不到时的缺省值。这里不检查译文好不好，只防漏译、错位、
占位符对不上和留着没人用的文案。
"""

import json
import re
from pathlib import Path

from support import import_core

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "pages" / "memory"
I18N = ROOT / ".astrbot-plugin" / "i18n"

PLACEHOLDER = re.compile(r"\{(\w+)\}")
CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")


def _zh_defaults() -> dict:
    source = (PAGE / "i18n.js").read_text("utf-8")
    match = re.search(
        r"/\* ZH-BEGIN \*/\s*const ZH = (\{.*?\n  \});\s*/\* ZH-END \*/", source, re.S
    )
    assert match, "i18n.js 里找不到 ZH 字典"
    return json.loads(match.group(1))


def _pages(locale: str) -> dict:
    return json.loads((I18N / f"{locale}.json").read_text("utf-8"))["pages"]["memory"]


def _js_sources() -> dict[str, str]:
    return {
        path.name: path.read_text("utf-8")
        for path in sorted(PAGE.glob("*.js"))
        if path.name != "i18n.js"
    }


# 代码里按"前缀 + 变量"拼出来的键：每一族的取值来自后端的常量，漏一个就会在界面上露出键名
def _families() -> dict[str, set[str]]:
    history = import_core("history")
    tidy = import_core("tidy")
    transfer = import_core("transfer")
    context_stats = import_core("context_stats")
    memstore = import_core("memstore")
    actions = {
        getattr(history, name)
        for name in dir(history)
        if name.startswith("ACTION_") and isinstance(getattr(history, name), str)
    }
    actors = {
        getattr(history, name)
        for name in dir(history)
        if name.startswith("ACTOR_") and isinstance(getattr(history, name), str)
    }

    def key(value: str) -> str:
        return value.replace("-", "_")

    web_api = (ROOT / "web_api.py").read_text("utf-8")
    window_sources = set(re.findall(r'\("跟随[^"]*",\s*"(\w+)"\)', web_api))
    return {
        "type_": set(memstore.MEMORY_TYPES),
        "typeHint_": set(memstore.MEMORY_TYPES),
        "ver_": {key(action) for action in actions},
        "actor_": {key(actor) for actor in actors},
        # 回收站条目的来源：写入前的备份、删除、导入覆盖、替换遗忘副本……外加旧索引
        "trash_": {
            key(history.ACTION_WRITE),
            key(history.ACTION_DELETE),
            key(history.ACTION_IMPORT),
            key(history.ACTION_DELETE_FORGOTTEN),
            key(history.ACTION_DISCARD_FORGOTTEN),
            "index",
        },
        "trigger_": {tidy.TRIGGER_MANUAL},
        "part_": set(context_stats.PART_KEYS),
        "partNote_": set(context_stats.PART_KEYS),
        "policy_": set(memstore.CONFLICT_POLICIES),
        "outcome_": set(transfer.OUTCOMES),
        "op_": {"create", "rewrite", "delete"},
        "windowSource_": window_sources,
    }


def test_zh_defaults_equal_the_zh_cn_file():
    zh = _zh_defaults()
    assert zh == _pages("zh-CN")
    assert list(zh) == list(_pages("zh-CN")), "键的顺序也保持一致，改文案时好对照"


def test_en_us_has_exactly_the_same_keys_and_placeholders():
    zh = _zh_defaults()
    en = _pages("en-US")
    assert set(en) == set(zh), (sorted(set(zh) - set(en)), sorted(set(en) - set(zh)))
    for key, text in zh.items():
        assert sorted(PLACEHOLDER.findall(text)) == sorted(PLACEHOLDER.findall(en[key])), key
        assert en[key].strip(), key


def test_en_us_texts_are_translated():
    zh = _zh_defaults()
    en = _pages("en-US")
    for key, text in zh.items():
        # 英文里不留中日韩字符；中文里有汉字的键，英文必须真的翻过
        assert not CJK.search(en[key]), (key, en[key])
        if CJK.search(text):
            assert en[key] != text, key


def test_every_key_the_page_asks_for_exists():
    zh = _zh_defaults()
    families = _families()
    literal_keys: set[str] = set()
    prefixes: set[str] = set()
    for name, source in _js_sources().items():
        code = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        code = re.sub(r"(?m)(^|[^:\"'\\])//.*$", r"\1", code)
        for match in re.finditer(r'\bt\(\s*"([A-Za-z0-9_]+)"\s*([+,)])', code):
            if match.group(2) == "+":
                prefixes.add(match.group(1))
            else:
                literal_keys.add(match.group(1))
        # t(kind === "group" ? "kindGroup" : "kindPrivate") 这类条件写法：取 ? 和 : 后面的键名
        for match in re.finditer(r'\bt\(\s*[^"\s)][^)]*\)', code):
            literal_keys.update(re.findall(r'[?:]\s*"([A-Za-z][A-Za-z0-9_]*)"', match.group(0)))
        # 导航、分区、标签页的文案键放在数组里：[值, "键名", …]
        for match in re.finditer(r'\[\s*"[A-Za-z]+",\s*"((?:tab|manage|migrate)[A-Za-z]*)"', code):
            literal_keys.add(match.group(1))
        for match in re.finditer(r'"(manage[A-Za-z]*Hint)"', code):
            literal_keys.add(match.group(1))
        # 文案键以 key("前缀_", …) / MP.i18n.has 的形式拼
        for match in re.finditer(r'key\(\s*"([A-Za-z]+_)"', code):
            prefixes.add(match.group(1))
        for match in re.finditer(r'\bt\(\s*"([A-Za-z]+_)"\s*\+', code):
            prefixes.add(match.group(1))
    missing = sorted(key for key in literal_keys if key not in zh)
    assert not missing, missing
    for prefix in prefixes:
        assert prefix in families, ("没有登记这一族键的取值", prefix)
        for value in families[prefix]:
            assert prefix + value in zh, prefix + value


def test_every_text_is_used_by_the_page():
    """不留没人用的文案：改了功能忘了删字符串，会在这里露出来。"""
    zh = _zh_defaults()
    families = _families()
    blob = "\n".join(_js_sources().values())
    family_keys = {prefix + value for prefix, values in families.items() for value in values}
    unused = []
    for key in zh:
        if key in family_keys:
            continue
        if not re.search(r'["\']' + re.escape(key) + r'["\']', blob):
            unused.append(key)
    assert not unused, unused


def test_dynamic_key_families_are_complete_in_both_languages():
    en = _pages("en-US")
    zh = _pages("zh-CN")
    for prefix, values in _families().items():
        for value in values:
            assert prefix + value in zh, prefix + value
            assert prefix + value in en, prefix + value


def test_key_labels_the_page_relies_on_are_short_enough_for_buttons_and_tabs():
    """标签页、按钮、芯片的英文不要写成一整句话（窄屏会被挤坏）。"""
    en = _pages("en-US")
    for key in (
        "tabBoard",
        "tabNotebook",
        "tabWhisper",
        "tabContext",
        "tabManage",
        "tabIndex",
        "tabForgotten",
        "tabTrash",
        "tabTidy",
        "tabExport",
        "tabImport",
        "tabScope",
        "tabPlatform",
        "save",
        "cancel",
        "delete",
        "restore",
        "edit",
        "renew",
        "publishNotice",
        "newMemory",
    ):
        assert len(en[key]) <= 24, (key, en[key])
