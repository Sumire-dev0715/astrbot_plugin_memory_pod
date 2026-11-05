"""上下文统计：估算分项、历史开头指纹与 AstrBot 摘要读取。"""

from core import context_stats

SUMMARY_PREFIX = context_stats.ASTRBOT_SUMMARY_PREFIX


def test_latest_summary_finds_the_most_recent_one():
    messages = [
        {"role": "user", "content": f"{SUMMARY_PREFIX}第一次压缩的摘要"},
        {"role": "assistant", "content": "Acknowledged the summary."},
        {"role": "user", "content": "普通消息"},
        {"role": "user", "content": f"{SUMMARY_PREFIX}第二次压缩的摘要"},
        {"role": "assistant", "content": "Acknowledged the summary."},
    ]
    assert context_stats.latest_astrbot_summary(messages) == "第二次压缩的摘要"


def test_latest_summary_without_any_summary():
    messages = [
        {"role": "system", "content": "人格"},
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "你好呀"},
    ]
    assert context_stats.latest_astrbot_summary(messages) == ""
    assert context_stats.latest_astrbot_summary([]) == ""
    assert context_stats.latest_astrbot_summary(None) == ""


def test_latest_summary_ignores_non_user_and_non_prefix_messages():
    messages = [
        {"role": "assistant", "content": f"{SUMMARY_PREFIX}助手消息不算"},
        {"role": "user", "content": "引用：" + SUMMARY_PREFIX + "不是开头"},
        {"role": "user", "content": {"text": "非字符串"}},
    ]
    assert context_stats.latest_astrbot_summary(messages) == ""


def test_history_head_skips_leading_system_messages():
    contexts = [
        {"role": "system", "content": "人格"},
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "好"},
    ]
    head = context_stats.history_head(contexts)
    assert head == context_stats.message_fingerprint(contexts[1])
    assert context_stats.history_head([]) == ""
    assert context_stats.history_head([{"role": "system", "content": "x"}]) == ""


def test_history_head_skips_platform_internal_messages():
    contexts = [
        {"role": "_checkpoint", "content": {"id": "ckpt-1"}},
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "好"},
    ]
    assert context_stats.history_head(contexts) == context_stats.message_fingerprint(
        contexts[1]
    )
    # 内部记录 id 变化不会让开头指纹跟着变
    changed = [{"role": "_checkpoint", "content": {"id": "ckpt-2"}}, *contexts[1:]]
    assert context_stats.history_head(changed) == context_stats.history_head(contexts)


def test_history_head_changes_after_truncation_or_summary():
    original = [
        {"role": "user", "content": "第一句"},
        {"role": "assistant", "content": "好"},
        {"role": "user", "content": "第二句"},
        {"role": "assistant", "content": "嗯"},
    ]
    truncated = original[2:]
    summary = [{"role": "user", "content": f"{SUMMARY_PREFIX}摘要"}]
    assert context_stats.history_head(original) != context_stats.history_head(truncated)
    assert context_stats.history_head(original) != context_stats.history_head(summary)


def test_context_stats_round_trip_and_defaults():
    stats = context_stats.ContextStats(
        parts={key: index for index, key in enumerate(context_stats.PART_KEYS)},
        estimate=21,
        actual=18,
        cache_hit=4,
        request_at=1_700_000_000.0,
    )
    restored = context_stats.ContextStats.from_dict(stats.to_dict())
    assert restored == stats
    empty = context_stats.ContextStats.from_dict({})
    assert empty.estimate == 0 and empty.actual == 0
    assert empty.cache_hit == -1 and empty.parts == {}
