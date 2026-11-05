"""main.astrbot_summary：从 AstrBot 的会话历史里读最新一条摘要。"""

from __future__ import annotations

import json
import types

from test_sharing_tools import main, run

UMO = "Bot-A:FriendMessage:123"


class FakeConversationManager:
    def __init__(self, history=None, cid="cid-1", error=None):
        self.history = [] if history is None else history
        self.cid = cid
        self.error = error

    async def get_curr_conversation_id(self, umo):
        if self.error:
            raise self.error
        return self.cid

    async def get_conversation(self, umo, cid):
        return types.SimpleNamespace(history=json.dumps(self.history))


def _summary(manager):
    plugin = types.SimpleNamespace(
        context=types.SimpleNamespace(conversation_manager=manager)
    )
    return run(main.MemoryPodPlugin.astrbot_summary(plugin, UMO))


def test_astrbot_summary_reads_latest_summary_from_history():
    manager = FakeConversationManager(
        [
            {"role": "user", "content": "我们的摘要：第一次"},
            {"role": "user", "content": "普通消息"},
            {
                "role": "user",
                "content": "Our previous history conversation summary: 最新摘要",
            },
            {"role": "assistant", "content": "Acknowledged the summary."},
        ]
    )
    assert _summary(manager) == "最新摘要"


def test_astrbot_summary_without_summary_or_conversation():
    assert _summary(FakeConversationManager([{"role": "user", "content": "hi"}])) == ""
    assert _summary(FakeConversationManager([], cid="")) == ""


def test_astrbot_summary_returns_empty_on_errors():
    assert _summary(FakeConversationManager(error=RuntimeError("boom"))) == ""
