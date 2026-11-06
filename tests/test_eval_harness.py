"""评测桩：AstrBot 的请求顺序、工具循环、截断/压缩与历史回存。"""

from __future__ import annotations

import asyncio
import json
from fake_eval_models import GoodEvalModel
from scripts.eval import harness
from scripts.eval.model_client import ModelReply, ToolCall
from scripts.eval.scenarios import (
    ALICE,
    compression_reminder,
    turn_limit_reminder,
)


def _run(scenario, client, data_dir):
    return asyncio.run(harness.run_scenario(scenario, client, data_dir))


class _RecordingModel:
    """先把插件的 memory_write 调出来，再给最终回复；记录最后一次请求。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None):
        self.calls.append(messages)
        if not any(
            message.get("role") == "tool"
            for message in messages
            if isinstance(message, dict)
        ):
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        name="memory_write",
                        arguments={
                            "scope": "notebook",
                            "file": "note.md",
                            "type": "reference",
                            "description": "测试记忆",
                            "content": "合成内容",
                        },
                        call_id="call_1",
                    )
                ]
            )
        return ModelReply(content="好。")


class _LoopingModel:
    """永远要求调用工具，用来验证工具循环上限。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    async def complete(self, messages, tools=None):
        self.calls.append(messages)
        if tools is None:
            return ModelReply(content="好。")
        return ModelReply(
            tool_calls=[
                ToolCall(
                    name="memory_search",
                    arguments={"query": "合成", "scope": "all", "mode": "all"},
                    call_id=f"call_{len(self.calls)}",
                )
            ]
        )


def _simple_scenario():
    async def setup(env):
        await env.seed(
            env.plugin._store.notebook_scope("10001"),
            "project.md",
            "项目代号与负责人",
            "项目代号是蓝鲸七号。",
        )

    return harness.Scenario(
        name="simple",
        title="工具循环与回存",
        setup=setup,
        turns=[harness.Turn("你好。", ALICE, mode="private")],
        checks=[],
    )


def test_tool_loop_saves_messages_without_no_save_parts(tmp_path):
    scenario = _simple_scenario()
    model = _RecordingModel()
    run = _run(scenario, model, tmp_path)
    turn = run.env.turns[0]
    assert [name for name, _args, _result in turn.tool_calls] == ["memory_write"]
    assert "已写入 notebook/note.md" in turn.tool_calls[0][2]
    # provider 收到的请求里没有 _no_save（AstrBot openai_source 会先删掉），
    # 但开头块仍然作为用户消息的附加内容发出
    sent = json.dumps(model.calls, ensure_ascii=False)
    assert "_no_save" not in sent
    assert "<memory_pod_index>" in sent
    history = run.env.history[run.env.umo_for(ALICE)]
    roles = [message["role"] for message in history]
    assert roles == ["user", "assistant", "tool", "assistant"]
    dumped = json.dumps(history, ensure_ascii=False)
    assert "<memory_pod_index>" not in dumped
    assert "_no_save" not in dumped
    assert any(
        part.startswith("<memory_pod_index>") for part in turn.extra_parts
    )


def test_turn_limit_truncation_happens_after_request_hook(tmp_path):
    run = _run(turn_limit_reminder(), GoodEvalModel(), tmp_path)
    turn = run.env.turns[0]
    assert turn.truncated is True
    assert turn.contexts_before > turn.contexts_after
    assert "<memory_pod_reminder>" in "\n".join(turn.extra_parts)
    saved = json.dumps(
        run.env.history[run.env.umo_for(ALICE)], ensure_ascii=False
    )
    assert "<memory_pod_reminder>" not in saved


def test_token_compression_adds_astrbot_summary_message(tmp_path):
    run = _run(compression_reminder(), GoodEvalModel(), tmp_path)
    turn = run.env.turns[0]
    assert turn.compressed is True
    history = run.env.history[run.env.umo_for(ALICE)]
    summaries = [
        message
        for message in history
        if message.get("role") == "user"
        and isinstance(message.get("content"), str)
        and message["content"].startswith(harness.ASTRBOT_SUMMARY_PREFIX)
    ]
    assert summaries, history


def test_tool_loop_stops_at_step_budget_and_forces_final_answer(tmp_path):
    scenario = _simple_scenario()
    model = _LoopingModel()
    run = _run(scenario, model, tmp_path)
    turn = run.env.turns[0]
    assert len(turn.tool_calls) == harness.MAX_TOOL_STEPS
    # 最后一次工具结果带着步数耗尽提示，最后一条请求还带着 user 提示
    assert harness.MAX_STEPS_REACHED_PROMPT in turn.tool_calls[-1][2]
    assert any(
        message.get("content") == harness.MAX_STEPS_REACHED_PROMPT
        for message in model.calls[-1]
    )
    assert run.env.turns[0].reply == "好。"


class _ExtraArgumentModel:
    """工具调用多带一个未声明参数，AstrBot 会忽略它。"""

    async def complete(self, messages, tools=None):
        if not any(
            message.get("role") == "tool"
            for message in messages
            if isinstance(message, dict)
        ):
            return ModelReply(
                tool_calls=[
                    ToolCall(
                        name="memory_search",
                        arguments={
                            "query": "合成",
                            "scope": "all",
                            "mode": "all",
                            "unexpected": "应当被忽略",
                        },
                        call_id="call_1",
                    )
                ]
            )
        return ModelReply(content="好。")


def test_tool_arguments_are_filtered_to_declared_properties(tmp_path):
    scenario = _simple_scenario()
    run = _run(scenario, _ExtraArgumentModel(), tmp_path)
    result = run.env.turns[0].tool_calls[0][2]
    assert not result.startswith("工具参数错误")


def test_compression_keeps_current_user_when_all_rounds_fit_budget():
    messages = [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    processed, compressed = harness._compress_context(messages)
    assert compressed is False
    assert processed[-1]["content"] == "c"


def test_truncate_by_turns_compression_has_no_summary(tmp_path):
    scenario = compression_reminder()
    scenario.compression_strategy = "truncate_by_turns"
    run = _run(scenario, GoodEvalModel(), tmp_path)
    turn = run.env.turns[0]
    assert turn.compressed is True
    history = json.dumps(
        run.env.history[run.env.umo_for(ALICE)], ensure_ascii=False
    )
    assert harness.ASTRBOT_SUMMARY_PREFIX not in history


def test_tool_schema_matches_astrbot_docstring_rules():
    async def demo(event, scope, count=1):
        """第一段说明。

        第二段也是 description，AstrBot 会一起发给模型。

        Args:
            scope(string): 作用域
            count(int): 次数
        """

    demo.__astrbot_llm_tool__ = "demo_tool"
    schema = harness.tool_schema(demo)["function"]
    assert "第一段说明" in schema["description"]
    assert "第二段也是 description" in schema["description"]
    assert schema["parameters"]["properties"]["scope"] == {
        "type": "string",
        "description": "作用域",
    }
    assert schema["parameters"]["properties"]["count"]["type"] == "integer"
