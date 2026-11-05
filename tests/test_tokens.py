import math
from types import SimpleNamespace

from core import tokens


def test_estimate_text_english():
    assert tokens.estimate_text("hello") == math.ceil(5 * tokens.OTHER_WEIGHT)


def test_estimate_text_chinese():
    assert tokens.estimate_text("你好") == math.ceil(2 * tokens.CHINESE_WEIGHT)


def test_estimate_text_mixed_and_empty():
    assert tokens.estimate_text("") == 0
    mixed = tokens.estimate_text("你好ab")
    assert mixed == math.ceil(2 * tokens.CHINESE_WEIGHT + 2 * tokens.OTHER_WEIGHT)


def test_estimate_content_multimodal():
    content = [
        {"type": "text", "text": "hello"},
        {"type": "image_url", "image_url": {"url": "http://x"}},
        {"type": "input_audio", "input_audio": {}},
    ]
    expected = (
        tokens.estimate_text("hello") + tokens.IMAGE_TOKENS + tokens.AUDIO_TOKENS
    )
    assert tokens.estimate_content(content) == expected


def test_estimate_message_counts_tool_calls():
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"function": {"name": "memory_read", "arguments": '{"file": "a.md"}'}}
        ],
    }
    assert tokens.estimate_message(message) > tokens.MESSAGE_OVERHEAD


def test_estimate_messages_skips_non_dict():
    assert tokens.estimate_messages([{"role": "user", "content": "hi"}, "junk"]) == \
        tokens.estimate_message({"role": "user", "content": "hi"})


def test_estimator_text_messages_and_scale():
    est = tokens.TokenEstimator()
    assert est.text("hello") == tokens.estimate_text("hello")
    assert est.messages([{"role": "user", "content": "hi"}]) == tokens.estimate_messages(
        [{"role": "user", "content": "hi"}]
    )
    assert est.scale(800) == 800


# ---------------------------------------------------------------- 缓存命中提取


def test_extract_cache_hit_flat_keys():
    assert tokens.extract_cache_hit({"prompt_cache_hit_tokens": 128}) == 128
    assert tokens.extract_cache_hit({"cache_read_input_tokens": 64}) == 64
    assert tokens.extract_cache_hit({"cached_content_token_count": 32}) == 32


def test_extract_cache_hit_openai_nested():
    usage = {"prompt_tokens": 500, "prompt_tokens_details": {"cached_tokens": 256}}
    assert tokens.extract_cache_hit(usage) == 256


def test_extract_cache_hit_object_usage():
    details = SimpleNamespace(cached_tokens=77)
    usage = SimpleNamespace(prompt_tokens=100, prompt_tokens_details=details)
    assert tokens.extract_cache_hit(usage) == 77
    assert tokens.extract_cache_hit(SimpleNamespace(prompt_cache_hit_tokens=5)) == 5


def test_extract_cache_hit_absent_or_invalid():
    assert tokens.extract_cache_hit(None) is None
    assert tokens.extract_cache_hit({"prompt_tokens": 100}) is None
    assert tokens.extract_cache_hit({"prompt_cache_hit_tokens": "junk"}) is None
    assert tokens.extract_cache_hit({"prompt_cache_hit_tokens": -3}) == 0


def test_estimate_messages_skips_platform_internal_records():
    plain = [{"role": "user", "content": "hi"}]
    with_checkpoint = plain + [{"role": "_checkpoint", "content": {"id": "ckpt-1"}}]
    assert tokens.estimate_messages(with_checkpoint) == tokens.estimate_messages(plain)
