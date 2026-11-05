"""注入位置：开头块进系统提示词、召回块进本轮用户消息的附加内容。

用桩复刻 AstrBot 4.29.4 的处理顺序与规则，证明插件的两块内容在它的
截断、LLM 摘要压缩之后仍在请求里，同时不会随 `_save_to_history` 回存
进会话历史（PLAN 3.8.1）。

依据（AstrBot 4.29.4）：
- `core/agent/context/manager.py:46-80`：请求发出前先按轮数截断，再按 token
  阈值压缩。
- `core/agent/context/truncator.py:16-33,100-150`：截断保留开头的 system
  消息，丢掉最旧的轮次，最新一轮始终保留。
- `core/agent/context/` 下的摘要模块（约 225-233、292-305 行）：LLM 摘要压缩保留开头
  的 system 消息与最新一轮，摘要写成一条 user 消息。
- `core/pipeline/process_stage/method/agent_sub_stages/internal.py:504-527`：
  回存历史时跳过第一条 system 消息，跳过带 `_no_save` 的 user/assistant
  消息。
- `core/agent/message.py:345-355`：回存时丢掉内容块里的 `_no_save` 部分。
"""

from __future__ import annotations

import copy
import json
import types
from pathlib import Path

from test_sharing_tools import FakeEvent, make_plugin, note_file, person_file, run

from test_sharing_tools import main as plugin_main

import importlib

pkg_context_stats = importlib.import_module(
    f"{plugin_main.__package__}.core.context_stats"
)

UMO = "Bot-A:FriendMessage:123"


def _seed_plugin(tmp_path):
    plugin = make_plugin(tmp_path, [])
    session = plugin._store.session_scope(UMO)
    run(
        session.write(
            "weekend-game.md",
            note_file("weekend-game", "每周六晚上的开黑活动", "晚上八点集合"),
            actor="system",
        )
    )
    run(
        session.write(
            "user-123.md",
            person_file(body="身份：测试用户，工程师"),
            actor="system",
        )
    )
    plugin._ensure_pending_loop = lambda: None
    return plugin, session


def _request(contexts, prompt="这周六还开黑吗"):
    return types.SimpleNamespace(
        prompt=prompt,
        contexts=list(contexts),
        system_prompt="你是测试机器人。",
        conversation=types.SimpleNamespace(cid="conv-1"),
        func_tool=None,
        image_urls=[],
        extra_user_content_parts=[],
    )


def _history():
    return [
        {"role": "user", "content": "早上好"},
        {"role": "assistant", "content": "早上好呀"},
        {"role": "user", "content": "今天做了什么"},
        {"role": "assistant", "content": "整理了项目资料"},
        {"role": "user", "content": "辛苦了"},
        {"role": "assistant", "content": "不客气"},
    ]


def _provider_messages(req):
    """AstrBot 在拦截钩子之后、发给模型之前看到的完整消息列表。

    `core/agent/runners/tool_loop_agent_runner.py:315-328`：contexts + 本轮
    用户消息（prompt 与 extra_user_content_parts 合在一条上）+ 开头 system。
    """
    messages = [{"role": "system", "content": req.system_prompt}]
    messages.extend(copy.deepcopy(req.contexts))
    content = [{"type": "text", "text": req.prompt}]
    content.extend(copy.deepcopy(req.extra_user_content_parts))
    messages.append({"role": "user", "content": content})
    return messages


def _content_text(message) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict)
        )
    return ""


def _astrbot_truncate_by_turns(messages, keep_turns=1):
    """复刻 ContextTruncator.truncate_by_turns 的保留规则（只保留尾部轮次）。"""
    first_non_system = next(
        (i for i, msg in enumerate(messages) if msg.get("role") != "system"),
        len(messages),
    )
    system = messages[:first_non_system]
    rest = messages[first_non_system:]
    if len(rest) // 2 <= keep_turns:
        return messages
    kept = rest[-keep_turns * 2 :]
    return system + kept


def _astrbot_llm_summarize(messages, summary="更早的对话摘要"):
    """复刻 LLMSummaryCompressor：开头 system + 摘要消息对 + 最新一轮。"""
    system = []
    for message in messages:
        if message.get("role") != "system":
            break
        system.append(message)
    latest_round = messages[-2:]
    return (
        system
        + [
            {
                "role": "user",
                "content": f"{pkg_context_stats.ASTRBOT_SUMMARY_PREFIX}{summary}",
            },
            {"role": "assistant", "content": "Acknowledged the summary."},
        ]
        + latest_round
    )


def _astrbot_save_to_history(messages):
    """复刻 _save_to_history：跳过第一条 system 与 _no_save 内容。"""
    saved = []
    skipped_initial_system = False
    for message in messages:
        if message.get("role") == "system" and not skipped_initial_system:
            skipped_initial_system = True
            continue
        if message.get("role") in ("assistant", "user") and message.get("_no_save"):
            continue
        item = copy.deepcopy(message)
        if isinstance(item.get("content"), list):
            item["content"] = [
                part
                for part in item["content"]
                if not (isinstance(part, dict) and part.get("_no_save"))
            ]
        saved.append(item)
    return saved


def test_opening_block_goes_to_system_prompt_and_recall_is_temp_part(tmp_path):
    plugin, _ = _seed_plugin(tmp_path)
    contexts = _history()
    req = _request(contexts)
    run(plugin.inject_memory(FakeEvent("Bot-A", sender_id="123", umo=UMO), req))

    # 开头块与规范接在 AstrBot 原有的系统提示词后面
    assert req.system_prompt.startswith("你是测试机器人。")
    assert "# 长期记忆（鲸群记忆）" in req.system_prompt
    assert "<memory_pod_index>" in req.system_prompt
    assert "weekend-game.md" in req.system_prompt
    # 召回块是本轮用户消息的附加内容，带 _no_save，且不是历史消息
    parts = req.extra_user_content_parts
    assert len(parts) == 1
    assert parts[0]["_no_save"] is True
    assert "<memory_pod_recall>" in parts[0]["text"]
    # 插件不再改写 req.contexts
    assert req.contexts == contexts
    # 统计：插件开头块是系统提示词的子集，不重复计入总量
    stats = plugin._sessions[UMO].stats
    assert stats.parts["plugin"] > 0
    assert stats.parts["plugin"] < stats.parts["system"]
    assert stats.estimate == sum(
        value
        for key, value in stats.parts.items()
        if key != "plugin"
    )


def test_injected_blocks_survive_astrbot_truncation_and_summary(tmp_path):
    plugin, _ = _seed_plugin(tmp_path)
    req = _request(_history())
    run(plugin.inject_memory(FakeEvent("Bot-A", sender_id="123", umo=UMO), req))
    messages = _provider_messages(req)

    # 1. 轮数截断：开头 system 与原最新一轮都保留
    truncated = _astrbot_truncate_by_turns(messages, keep_turns=1)
    assert truncated[0]["role"] == "system"
    assert "<memory_pod_index>" in truncated[0]["content"]
    assert "# 长期记忆（鲸群记忆）" in truncated[0]["content"]
    assert "<memory_pod_recall>" in _content_text(truncated[-1])

    # 2. LLM 摘要压缩：开头 system 原样保留，最新一轮也保留
    summarized = _astrbot_llm_summarize(messages)
    assert summarized[0]["role"] == "system"
    assert "<memory_pod_index>" in summarized[0]["content"]
    assert "<memory_pod_recall>" in _content_text(summarized[-1])
    # 召回内容没有混进 AstrBot 的摘要
    summary_message = next(
        message
        for message in summarized
        if str(message.get("content", "")).startswith(
            pkg_context_stats.ASTRBOT_SUMMARY_PREFIX
        )
    )
    assert "<memory_pod_recall>" not in summary_message["content"]


def test_injected_blocks_never_enter_saved_history(tmp_path):
    plugin, _ = _seed_plugin(tmp_path)
    req = _request(_history())
    run(plugin.inject_memory(FakeEvent("Bot-A", sender_id="123", umo=UMO), req))
    saved = _astrbot_save_to_history(_provider_messages(req))
    dumped = json.dumps(saved, ensure_ascii=False)

    # 开头块与召回块都不在回存的历史里
    assert "<memory_pod_index>" not in dumped
    assert "# 长期记忆（鲸群记忆）" not in dumped
    assert "<memory_pod_recall>" not in dumped
    assert "晚上八点集合" not in dumped
    # 用户正文与发送者标注照常保存
    user_messages = [m for m in saved if m.get("role") == "user"]
    assert any("这周六还开黑吗" in _content_text(m) for m in user_messages)
    assert any("[发送者：" in _content_text(m) for m in user_messages)


def test_opening_block_snapshot_is_stable_until_history_head_changes(tmp_path):
    plugin, session = _seed_plugin(tmp_path)
    event = FakeEvent("Bot-A", sender_id="123", umo=UMO)
    rt = plugin_main.RuntimeSession()
    plugin._sessions[UMO] = rt

    first_contexts = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "好"},
    ]
    req = _request(first_contexts, prompt="随便聊聊")
    run(plugin.inject_memory(event, req))
    first_block = rt.index_block
    assert first_block and rt.index_loaded

    # 记忆变了但历史开头没变：快照逐字不变，新文件等下次快照刷新
    run(session.write("new-memory.md", note_file("new-memory", "新记忆", "新正文")))
    req = _request(
        first_contexts + [{"role": "user", "content": "第二句"},
                          {"role": "assistant", "content": "嗯"}],
        prompt="随便聊聊",
    )
    run(plugin.inject_memory(event, req))
    assert rt.index_block == first_block
    assert "new-memory.md" not in rt.index_block

    # AstrBot 截断/压缩改了历史开头：快照重建，新记忆进入开头块
    changed_head = [
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "嗯"},
        {"role": "user", "content": "第三句"},
    ]
    req = _request(changed_head, prompt="随便聊聊")
    run(plugin.inject_memory(event, req))
    assert rt.index_block != first_block
    assert "new-memory.md" in rt.index_block


def test_opening_block_snapshot_refreshes_on_webui_change(tmp_path):
    plugin, _ = _seed_plugin(tmp_path)
    event = FakeEvent("Bot-A", sender_id="123", umo=UMO)
    rt = plugin_main.RuntimeSession()
    plugin._sessions[UMO] = rt
    req = _request(_history(), prompt="随便聊聊")
    run(plugin.inject_memory(event, req))
    assert "webui-new.md" not in (rt.index_block or "")

    session = plugin._store.session_scope(UMO)
    run(session.write("webui-new.md", note_file("webui-new", "管理页新增", "正文")))
    plugin.invalidate_for_key(plugin_main.memstore.safe_key(UMO))
    run(plugin.inject_memory(event, req))
    assert "webui-new.md" in (rt.index_block or "")
