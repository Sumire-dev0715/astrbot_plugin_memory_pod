"""聊天侧提示词体积测量（PLAN 3.8.8）。

统计模型每轮实际看到的固定文本：

- 记忆规范；
- 工具说明：工具名、AstrBot 取的 docstring 第一段 description，以及
  参数 description（模型实际收到的完整 schema）；
- 开头块外壳与召回块外壳（块首数据声明、标题与标记，不含具体记忆正文）。

`chat_total` = 记忆规范 + 工具完整 schema（口径与 3.7.6 相同，验收线
2000 字）。3.8.8 把开头块里的说明也算进聊天侧，所以另给
`chat_side_total` = chat_total + 开头块外壳 + 召回块外壳，同样不超过
2000 字；`within_limit` 同时检查两者，任何一个超出都会让脚本返回 1。

用法：
    python scripts/prompt_sizes.py --json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.eval import astrbot_stub  # noqa: E402

CHAT_LIMIT = 2000

_ARGS_LINE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*:\s*(.*)$")
_TYPE_MAP = {"string": "string", "int": "integer", "bool": "boolean", "float": "number"}


def first_paragraph(doc: str) -> str:
    """AstrBot 取工具 description 的口径：docstring 第一段。"""
    parts = [part.strip() for part in (doc or "").strip().split("\n\n") if part.strip()]
    return parts[0] if parts else ""


def tool_schema(func) -> dict:
    """按 AstrBot register_llm_tool 的口径还原 OpenAI 工具声明。"""
    doc = (func.__doc__ or "").strip()
    properties: dict[str, dict] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped == "Args:":
            in_args = True
            continue
        if not in_args:
            continue
        match = _ARGS_LINE.match(line)
        if not match:
            continue
        name, type_name, arg_desc = match.groups()
        properties[name] = {
            "type": _TYPE_MAP.get(type_name, type_name),
            "description": arg_desc.strip(),
        }
    return {
        "type": "function",
        "function": {
            "name": func.__astrbot_llm_tool__,
            "description": first_paragraph(doc),
            "parameters": {"type": "object", "properties": properties},
        },
    }


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
    item = recall_mod.Recalled("notebook/placeholder.md", "", 0.0)
    view = injection_mod.MemoryView(
        ref="notebook/placeholder.md",
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
    prefixes = ["core.prompts", "core.injection", "core.recall"]
    mods = {
        name: sys.modules[f"{astrbot_stub.PLUGIN_PACKAGE}.{name}"]
        for name in prefixes
    }
    prompts = mods["core.prompts"]
    injection = mods["core.injection"]
    recall = mods["core.recall"]

    guidance = prompts.memory_guidance()
    tools = _tool_docs(plugin_cls)
    tool_total = sum(item["description"] for item in tools.values())
    tool_schema_total = sum(sum(item.values()) for item in tools.values())

    index_shell = len(
        injection.build_opening_block("X", "Y", "Z", [], now=0.0)
    ) - 3
    recall_shell = _recall_shell(recall, injection, prompts)

    chat_total = len(guidance) + tool_schema_total
    # 3.8.8 的聊天侧还包括开头块里的说明（两块外壳），单独给一个合计
    chat_side_total = chat_total + index_shell + recall_shell
    return {
        "guidance": len(guidance),
        "tools": tools,
        "tool_total": tool_total,
        "tool_schema_total": tool_schema_total,
        "index_shell": index_shell,
        "recall_shell": recall_shell,
        "chat_total": chat_total,
        "chat_side_total": chat_side_total,
        "within_limit": chat_total <= CHAT_LIMIT
        and chat_side_total <= CHAT_LIMIT,
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
        print(f"开头块外壳：{sizes['index_shell']} 字")
        print(f"召回块外壳：{sizes['recall_shell']} 字")
        print(
            f"聊天侧合计 chat_total：{sizes['chat_total']} 字"
            f"（上限 {sizes['limit']}）"
        )
        print(
            f"加上开头块与召回块外壳 chat_side_total："
            f"{sizes['chat_side_total']} 字"
            f"（{'通过' if sizes['within_limit'] else '超出'}）"
        )
    return 0 if sizes["within_limit"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
