"""整理调用的超时：管理页可选的几档、按内容量自动推算、超时后的重试放宽。

模型调用一律用假的替代实现；所有记忆都是合成数据。
"""

from __future__ import annotations

import asyncio

import pytest

import test_web_api as tw
import types

from support import main as mb_main
from support import make_plugin, pkg_tidy, run

resolve = mb_main.resolve_tidy_timeout


def test_auto_scales_with_prompt_size():
    assert resolve("短", {"mode": "auto"}) == 180
    # 每 100 字 1 秒，下限 180、上限 600
    assert resolve("字" * 25_000, {"mode": "auto"}) == 250
    assert resolve("字" * 60_000, {"mode": "auto"}) == 600
    assert resolve("字" * 10, None) == 180


def test_none_means_no_timeout():
    assert resolve("字" * 60_000, {"mode": "none"}) is None


def test_fixed_and_custom_use_given_seconds():
    assert resolve("字", {"mode": "fixed", "seconds": 300}) == 300
    assert resolve("字" * 60_000, {"mode": "custom", "seconds": 45}) == 45


def test_invalid_saved_setting_falls_back_to_auto():
    assert pkg_tidy.normalize_timeout(None) == {"mode": "auto"}
    assert pkg_tidy.normalize_timeout({"mode": "奇怪"}) == {"mode": "auto"}
    assert pkg_tidy.normalize_timeout({"mode": "fixed", "seconds": 7}) == {"mode": "auto"}
    assert pkg_tidy.normalize_timeout({"mode": "custom", "seconds": 5}) == {"mode": "auto"}
    assert pkg_tidy.normalize_timeout({"mode": "custom", "seconds": 9999}) == {"mode": "auto"}
    assert pkg_tidy.normalize_timeout({"mode": "custom", "seconds": "abc"}) == {"mode": "auto"}


def test_timeout_setting_survives_model_change(tmp_path):
    book = pkg_tidy.TidyBook(tmp_path / "tidy")
    assert book.timeout_setting() == {"mode": "auto"}
    book.save_timeout({"mode": "fixed", "seconds": 300})
    book.save_model("provider-a", "model-a")
    assert book.timeout_setting() == {"mode": "fixed", "seconds": 300}
    book.save_timeout({"mode": "custom", "seconds": 90})
    assert book.selected_model() == {"providerId": "provider-a", "model": "model-a"}
    assert book.timeout_setting() == {"mode": "custom", "seconds": 90}


def test_web_api_saves_timeout(tmp_path):
    plugin, dashboard = tw.setup(tmp_path)
    for setting, expect in (
        ({"mode": "none"}, {"mode": "none"}),
        ({"mode": "fixed", "seconds": 180}, {"mode": "fixed", "seconds": 180}),
        ({"mode": "custom", "seconds": 120}, {"mode": "custom", "seconds": 120}),
        ({"mode": "auto"}, {"mode": "auto"}),
    ):
        tw.REQUEST.body = {"timeout": setting}
        tw.REQUEST.query = {}
        result = run(dashboard._wrap(dashboard.tidy_timeout)())
        assert result["status"] == "ok", result
        assert result["data"]["timeout"] == expect
    assert pkg_tidy.TidyBook(tmp_path / "tidy").timeout_setting() == {"mode": "auto"}


def test_web_api_rejects_custom_out_of_range(tmp_path):
    plugin, dashboard = tw.setup(tmp_path)
    tw.REQUEST.body = {"timeout": {"mode": "custom", "seconds": 5}}
    tw.REQUEST.query = {}
    result = run(dashboard._wrap(dashboard.tidy_timeout)())
    assert result["status"] == "error"
    assert "秒" in result["message"]
    # 被拒绝时不落盘
    assert pkg_tidy.TidyBook(tmp_path / "tidy").timeout_setting() == {"mode": "auto"}


def test_model_list_exposes_timeout_state(tmp_path):
    plugin, dashboard = tw.setup(tmp_path)
    tw.REQUEST.body = None
    tw.REQUEST.query = {}
    result = run(dashboard._wrap(dashboard.tidy_models)())
    assert result["status"] == "ok", result
    data = result["data"]
    assert data["timeout"] == {"mode": "auto"}
    assert data["timeoutChoices"] == [180, 300]
    assert data["timeoutCustomRange"] == [30, 3600]


def _plugin_with_model(tmp_path, monkeypatch, timeout):
    """真插件（不是管理页的替身），这样才能测到 call_tidy_model 本身。"""
    plugin = make_plugin(tmp_path)
    plugin.context.provider_manager = types.SimpleNamespace(
        provider_insts=[],
        providers_config=[
            {"id": "provider-a", "model": "model-a", "enable": True},
        ],
    )
    plugin._tidy.save_model("provider-a", "model-a")
    monkeypatch.setattr(mb_main, "resolve_tidy_timeout", lambda prompt, setting: timeout)
    return plugin


def test_call_tidy_model_times_out_and_reports_wait_limit(tmp_path, monkeypatch):
    plugin = _plugin_with_model(tmp_path, monkeypatch, 0.05)

    async def slow(provider_id, prompt):
        await asyncio.sleep(1)
        return None

    monkeypatch.setattr(plugin, "_generate_with_provider", slow)
    text, log = run(plugin.call_tidy_model("整理一次"))
    assert text is None
    # 两次尝试都超时，第二次的等待上限放宽到 1.5 倍
    assert len(log) == 2, log
    assert "0.05s" in log[0] and "超时" in log[0]
    assert "0.075s" in log[1] and "超时" in log[1]


def test_call_tidy_model_unlimited_waits_for_slow_model(tmp_path, monkeypatch):
    plugin = _plugin_with_model(tmp_path, monkeypatch, None)

    async def slow(provider_id, prompt):
        await asyncio.sleep(0.05)
        return "整理结果"

    monkeypatch.setattr(plugin, "_generate_with_provider", slow)
    text, log = run(plugin.call_tidy_model("整理一次"))
    assert text == "整理结果"
    assert len(log) == 1
    assert "不限时" in log[0]
