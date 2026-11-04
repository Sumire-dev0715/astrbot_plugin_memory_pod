"""3.7.1 压缩跟随 AstrBot：窗口取值顺序、超大兜底、轮数上限、关闭压缩与提示。

用桩模拟 AstrBot 的提供商配置（provider.provider_config）、内置模型表
（LLM_METADATAS）和系统配置（agent_runner.config.compression），不读任何
真实配置或真实记忆。
"""

import sys
import time
import types

import pytest

from test_sharing_tools import (
    FakeEvent,
    _async_value,
    _status_text,
    main,
    make_plugin,
    pkg_compress,
    run,
)


class FakeProvider:
    """最小的提供商替身：AstrBot 在请求前会把窗口填进 provider_config。"""

    def __init__(self, model="", window=0):
        self._model = model
        self.provider_config = {"max_context_tokens": window}

    def get_model(self):
        return self._model


def astrbot_conf(fallback=0, max_turns=-1, identifier=False):
    compression = {"max_turns": max_turns}
    if fallback:
        compression["fallback_max_tokens"] = fallback
    return {
        "agent_runner": {"config": {"compression": compression}},
        "provider_settings": {"identifier": identifier},
    }


def make_window_plugin(tmp_path, conf, provider):
    plugin = make_plugin(tmp_path, [])
    plugin.context.get_config = lambda umo=None: conf

    async def provide(umo=None):
        return provider

    plugin._current_provider = provide
    return plugin


def resolve(plugin, umo=None):
    return run(plugin._resolve_window_for(umo))


def install_metadata(monkeypatch, table):
    """把 AstrBot 的内置模型表换成本用例的桩。"""
    core = types.ModuleType("astrbot.core")
    core.__path__ = []
    utils = types.ModuleType("astrbot.core.utils")
    utils.__path__ = []
    meta = types.ModuleType("astrbot.core.utils.llm_metadata")
    meta.LLM_METADATAS = table
    core.utils = utils
    utils.llm_metadata = meta
    monkeypatch.setitem(sys.modules, "astrbot.core", core)
    monkeypatch.setitem(sys.modules, "astrbot.core.utils", utils)
    monkeypatch.setitem(sys.modules, "astrbot.core.utils.llm_metadata", meta)


# ---------------------------------------------------------------- 窗口取值顺序


def test_manual_window_wins_over_astrbot(tmp_path):
    plugin = make_window_plugin(
        tmp_path, astrbot_conf(fallback=64000), FakeProvider("stub-model", 200000)
    )
    plugin.config["max_context_tokens"] = 100000
    assert resolve(plugin) == (100000, "手动配置")


def test_window_follows_astrbot_provider_config_first(tmp_path, monkeypatch):
    install_metadata(monkeypatch, {"stub-model": {"limit": {"context": 32000}}})
    plugin = make_window_plugin(
        tmp_path,
        astrbot_conf(fallback=64000),
        FakeProvider("stub-model", 200000),
    )
    # 钩子执行时 AstrBot 已把提供商配置里的窗口值填好，第一级直接命中
    assert resolve(plugin) == (200000, "跟随 AstrBot 的提供商配置")


def test_window_follows_astrbot_model_table(tmp_path, monkeypatch):
    install_metadata(monkeypatch, {"stub-model": {"limit": {"context": 32000}}})
    plugin = make_window_plugin(
        tmp_path,
        astrbot_conf(fallback=64000),
        FakeProvider("stub-model", 0),
    )
    window, note = resolve(plugin)
    assert window == 32000 and "内置模型表" in note


def test_window_follows_astrbot_fallback(tmp_path):
    # 没有 astrbot.core 模块：跳过模型表这一级，读 AstrBot 的兜底值
    plugin = make_window_plugin(
        tmp_path, astrbot_conf(fallback=48000), FakeProvider("mystery-model", 0)
    )
    assert resolve(plugin) == (48000, "跟随 AstrBot 的上下文窗口兜底值")


def test_huge_provider_window_is_untrusted_and_model_detected(tmp_path, monkeypatch):
    install_metadata(monkeypatch, {})
    plugin = make_window_plugin(
        tmp_path,
        astrbot_conf(fallback=64000),
        FakeProvider("deepseek-chat", 12_000_000),
    )
    window, note = resolve(plugin)
    assert window == 128000
    assert "不可信" in note and "自动探测" in note


def test_huge_fallback_with_unknown_model_disables_compression(tmp_path):
    plugin = make_window_plugin(
        tmp_path,
        astrbot_conf(fallback=12_000_000),
        FakeProvider("mystery-model", 0),
    )
    window, note = resolve(plugin)
    assert window == 0
    assert "不可信" in note and "自动探测模型窗口失败" in note


# ---------------------------------------------------------------- 轮数上限触发


def _contexts(turns: int) -> list[dict]:
    messages = [{"role": "system", "content": "人格"}]
    for i in range(turns):
        messages.append({"role": "user", "content": f"问题 {i}"})
        messages.append({"role": "assistant", "content": f"回答 {i}"})
    return messages


def _request(contexts):
    return types.SimpleNamespace(
        contexts=list(contexts),
        prompt="你好",
        system_prompt="",
        func_tool=None,
        conversation=types.SimpleNamespace(cid="conv-1"),
        image_urls=[],
    )


def make_request_plugin(tmp_path, conf, window=1_000_000):
    plugin = make_plugin(tmp_path, [])
    plugin.config.update(
        {
            "keep_recent_turns": 3,
            "enable_compression": True,
            "auto_extract_interval": 0,
        }
    )
    plugin.context.get_config = lambda umo=None: conf
    plugin._resolve_window_for = lambda umo: _async_value((window, "测试窗口"))
    plugin._history_cleanup_at = time.time()
    plugin._tasks = set()
    plugin._index_block = lambda *args, **kwargs: _async_value(None)
    plugin._recall_message = lambda *args, **kwargs: _async_value(None)
    plugin._maybe_forget = lambda *args, **kwargs: None
    plugin._maybe_tidy = lambda *args, **kwargs: None
    plugin._save_state = lambda umo, state: _async_value(None)
    calls = []

    async def fake_compress(event, rt, umo, contexts, total):
        calls.append(list(contexts))

    plugin._compress = fake_compress
    return plugin, calls


def test_astrbot_turn_limit_triggers_compression_below_token_threshold(tmp_path):
    # 轮数上限 6：4 轮时插件抢先压缩；token 总量远低于阈值
    plugin, calls = make_request_plugin(tmp_path, astrbot_conf(max_turns=6))
    event = FakeEvent("Bot-A")
    run(plugin._process_request(event, _request(_contexts(4))))
    assert len(calls) == 1


def test_no_turn_limit_keeps_short_context_untouched(tmp_path):
    plugin, calls = make_request_plugin(tmp_path, astrbot_conf(max_turns=-1))
    event = FakeEvent("Bot-A")
    run(plugin._process_request(event, _request(_contexts(4))))
    assert calls == []


def test_compression_disabled_ignores_persisted_summary_and_watermark(tmp_path):
    """关闭压缩后不能继续注入旧摘要、也不能按旧水位线丢弃原文。"""
    plugin, calls = make_request_plugin(
        tmp_path, astrbot_conf(max_turns=-1), window=1_000_000
    )
    plugin.config["enable_compression"] = False
    contexts = _contexts(6)
    state = pkg_compress.SessionState(cid="conv-1")
    plan = pkg_compress.plan_compression(contexts, 0, keep_recent_turns=2)
    assert plan is not None
    pkg_compress.apply_summary(state, contexts, plan, "旧摘要")
    assert state.watermark > 0 and state.summary == "旧摘要"

    event = FakeEvent("Bot-A")
    plugin._sessions[event.unified_msg_origin] = main.RuntimeSession(state=state)
    req = _request(contexts)
    run(plugin._process_request(event, req))
    assert calls == []
    assert req.contexts == contexts
    assert all(
        "<memory_beyond_summary>" not in str(message.get("content", ""))
        for message in req.contexts
    )


def test_unknown_window_disables_turn_limit_compression(tmp_path):
    """窗口未知时日志说“压缩已停用”，轮数触发也不能再生效。"""
    plugin, calls = make_request_plugin(tmp_path, astrbot_conf(max_turns=4), window=0)
    event = FakeEvent("Bot-A")
    run(plugin._process_request(event, _request(_contexts(2))))
    assert calls == []
    assert plugin._sessions[event.unified_msg_origin].warned_window is True


def test_turn_limit_counts_message_pairs_like_astrbot(tmp_path):
    """AstrBot 按非 system 消息条数对半计轮；工具调用多出来的消息也算。"""
    plugin, _ = make_request_plugin(tmp_path, astrbot_conf(max_turns=4))
    one_turn = [
        {"role": "user", "content": "查天气"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "晴"},
        {"role": "assistant", "content": "今天晴"},
    ]
    # 4 条消息 = 2 轮（AstrBot 口径），上限 4 - 2 = 2，达到即触发
    assert plugin._compression_triggered(
        one_turn, 0, 1_000_000, 4
    )
    # 只有 user + assistant 一条对时还不到 2 轮
    assert not plugin._compression_triggered(
        one_turn[:2], 0, 1_000_000, 4
    )
    # 本轮还会注入 3 个块，也算进 AstrBot 看到的轮数
    assert plugin._compression_triggered(
        one_turn[:2], 0, 1_000_000, 4, extra_messages=3
    )


# ---------------------------------------------------------------- 关闭压缩


def test_compression_disabled_never_compresses_or_clips(tmp_path, monkeypatch):
    plugin, calls = make_request_plugin(tmp_path, astrbot_conf(max_turns=6), window=100)
    plugin.config["enable_compression"] = False
    monkeypatch.setattr(
        pkg_compress,
        "fit_tail_to_budget",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("压缩关闭时不能裁剪")
        ),
    )
    monkeypatch.setattr(
        pkg_compress,
        "clear_old_tool_results",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("压缩关闭时不能清理上下文")
        ),
    )
    contexts = _contexts(4)
    for message in contexts[1:]:
        message["content"] = "长" * 3000
    event = FakeEvent("Bot-A")
    # 上一轮很久以前，缓存已冷；压缩关闭时也不许清理旧工具结果
    plugin._sessions[event.unified_msg_origin] = main.RuntimeSession(
        state=pkg_compress.SessionState(last_request_at=1.0)
    )
    req = _request(contexts)
    run(plugin._process_request(event, req))
    assert calls == []
    assert req.contexts == contexts


# ---------------------------------------------------------------- 状态与提示


def test_compression_status_reports_turn_limit_and_identifier(tmp_path):
    plugin = make_window_plugin(
        tmp_path,
        astrbot_conf(fallback=64000, max_turns=5, identifier=True),
        FakeProvider("stub-model", 0),
    )
    plugin.config["keep_recent_turns"] = 3
    status = run(plugin.compression_status("Bot-A:FriendMessage:1"))
    assert status["window"] == 64000
    assert status["turnLimit"] == 5
    assert status["turnLimitWarning"] is True  # 5 <= 3 + 2
    assert status["userIdentifier"] is True


def test_mb_status_hints_turn_limit_and_user_identifier(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.config["keep_recent_turns"] = 3
    plugin.context.get_config = lambda umo=None: astrbot_conf(
        fallback=64000, max_turns=5, identifier=True
    )
    text = _status_text(plugin, FakeEvent("Bot-A"))
    assert "AstrBot 轮数上限：5" in text
    assert "⚠️" in text and "保留轮数 + 2" in text
    assert "标注两次" in text


def test_mb_status_quiet_when_no_turn_limit_or_identifier(tmp_path):
    plugin = make_plugin(tmp_path, [])
    plugin.context.get_config = lambda umo=None: astrbot_conf(
        fallback=64000, max_turns=-1, identifier=False
    )
    text = _status_text(plugin, FakeEvent("Bot-A"))
    assert "AstrBot 轮数上限：未设置" in text
    assert "标注两次" not in text
    assert "⚠️" not in text
