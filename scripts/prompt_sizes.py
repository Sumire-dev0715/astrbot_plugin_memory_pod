"""聊天侧提示词体积测量（PLAN 3.7.6）。

统计模型每轮实际看到的固定文本：

- 记忆规范；
- 三个工具说明：工具名、AstrBot 取的 docstring 第一段 description，以及
  参数 description（模型实际收到的完整 schema）；
- 索引块外壳与召回块外壳（块首数据声明、标题与标记，不含具体记忆正文）；
- 后台整理提示词（空索引、空对话时的模板体积）。

`chat_total` = 记忆规范 + 三个工具的完整 schema，验收线 1200 字。

用法：
    python scripts/prompt_sizes.py --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval import astrbot_stub  # noqa: E402
from scripts.eval.harness import tool_schema  # noqa: E402

CHAT_LIMIT = 1200


def _tool_docs(plugin_cls) -> dict[str, dict]:
    """工具名 -> 名称/描述/参数说明字数，口径与 AstrBot 发出去的 schema 一致。"""
    result: dict[str, dict] = {}
    for value in vars(plugin_cls).values():
        name = getattr(value, "__astrbot_llm_tool__", None)
        if not name:
            continue
        schema = tool_schema(value)["function"]
        params = sum(
            len(item.get("description") or "")
            for item in schema["parameters"]["properties"].values()
        )
        result[name] = {
            "name": len(name),
            "description": len(schema["description"]),
            "parameters": params,
        }
    return result


def _recall_shell(recall_mod, injection_mod, prompts) -> int:
    item = recall_mod.Recalled("session/placeholder.md", "", 0.0)
    view = injection_mod.MemoryView(
        ref="session/placeholder.md",
        mem_type="reference",
        description="",
        source="",
        body="",
        truncated=False,
        omitted=0,
        written=0.0,
    )
    section = recall_mod.RecallSection(item, "", False, view)
    block = recall_mod.render_recall_block(
        [section], prompts.DATA_NOTICE, now=0.0
    )
    if not block:
        return 0
    entry = injection_mod.render_view(view, now=0.0)
    return len(block) - len(entry)


def collect_sizes() -> dict:
    astrbot_stub.install()
    module, plugin_cls = astrbot_stub.load_plugin(REPO_ROOT)
    prefixes = ["core.prompts", "core.injection", "core.recall", "core.writer"]
    mods = {
        name: sys.modules[f"{astrbot_stub.PLUGIN_PACKAGE}.{name}"]
        for name in prefixes
    }
    prompts = mods["core.prompts"]
    injection = mods["core.injection"]
    recall = mods["core.recall"]
    writer = mods["core.writer"]

    guidance = prompts.memory_guidance()
    tools = _tool_docs(plugin_cls)
    tool_total = sum(item["description"] for item in tools.values())
    tool_schema_total = sum(sum(item.values()) for item in tools.values())

    index_shell = len(injection.build_opening_block(None, "X", [], now=0.0)) - 1
    index_shell_with_global = (
        len(injection.build_opening_block("X", "Y", [], now=0.0)) - 2
    )
    recall_shell = _recall_shell(recall, injection, prompts)

    context = asyncio.run(
        writer.build_context(
            {},
            [],
            now=0.0,
            permission=None,
            admin=False,
            people_shared=False,
            global_on=False,
        )
    )
    chat_total = len(guidance) + tool_schema_total
    return {
        "guidance": len(guidance),
        "tools": tools,
        "tool_total": tool_total,
        "tool_schema_total": tool_schema_total,
        "index_shell": index_shell,
        "index_shell_with_global": index_shell_with_global,
        "recall_shell": recall_shell,
        "background_prompt": len(context.prompt),
        "background_rules": len(writer.WRITE_RULES),
        "chat_total": chat_total,
        "within_limit": chat_total <= CHAT_LIMIT,
        "limit": CHAT_LIMIT,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="测量聊天侧提示词体积")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)
    sizes = collect_sizes()
    if args.json:
        print(json.dumps(sizes, ensure_ascii=False))
    else:
        print(f"记忆规范：{sizes['guidance']} 字")
        for name, item in sizes["tools"].items():
            print(
                f"工具说明 {name}：{item['description']} 字"
                f"（参数说明 {item['parameters']} 字）"
            )
        print(f"工具说明合计（description）：{sizes['tool_total']} 字")
        print(f"工具完整 schema 合计：{sizes['tool_schema_total']} 字")
        print(f"索引块外壳：{sizes['index_shell']} 字（含全局段 {sizes['index_shell_with_global']} 字）")
        print(f"召回块外壳：{sizes['recall_shell']} 字")
        print(f"后台整理提示词：{sizes['background_prompt']} 字")
        print(
            f"聊天侧合计 chat_total：{sizes['chat_total']} 字"
            f"（上限 {sizes['limit']}，{'通过' if sizes['within_limit'] else '超出'}）"
        )
    return 0 if sizes["within_limit"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
