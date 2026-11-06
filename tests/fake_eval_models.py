"""评测脚本用的确定性假模型。

两个替身都只吃 harness 交给模型的 OpenAI 风格消息，不访问任何接口：

- ``GoodEvalModel``：按合成场景里的话做出“合格模型”该有的工具调用与回复，
  让全部检查项通过，用来证明检查项在正常行为下不会误报。
- ``BadEvalModel``：把每轮用户消息都写进小本本（type=note），隐私、
  留言板、他人档案都不按规则处理，让关键项失败，用来证明检查项不会空过。

它们只用于 tests/ 下的评测脚本测试；真实评测由 run_eval.py 调接口。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from scripts.eval.model_client import ModelReply, ToolCall

INDEX_OPEN = "<memory_pod_index>"
INDEX_CLOSE = "</memory_pod_index>"
RECALL_OPEN = "<memory_pod_recall>"
REMINDER_OPEN = "<memory_pod_reminder>"
SENDER_PREFIX = "[发送者："
DATE_RE = re.compile(r"(\d{1,2}\s*月\s*\d{1,2}\s*日)")
ID_RE = re.compile(r"(?<!\d)(\d{8})(?!\d)")
INDEX_LINK_RE = re.compile(r"\]\(([^)]+\.md)\)\s*—\s*(.*)")
INDEX_PLAIN_RE = re.compile(r"^\s*-\s+([^\s—]+\.md)\s+—\s*(.*)")


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n\n".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _bigrams(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {lowered[i : i + 2] for i in range(len(lowered) - 1)}


@dataclass
class _TurnState:
    """当前这一轮的消息、已做的工具调用与工具结果。"""

    messages: list[dict]
    text: str = ""
    body: str = ""
    sender_id: str = ""
    index_text: str = ""
    recall_text: str = ""
    reminder: bool = False
    calls: list[tuple[str, dict]] = field(default_factory=list)
    results: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_messages(cls, messages: list[dict]) -> "_TurnState":
        state = cls(messages=list(messages or []))
        last_user = -1
        for index, message in enumerate(state.messages):
            if isinstance(message, dict) and message.get("role") == "user":
                last_user = index
        current = state.messages[last_user:] if last_user >= 0 else []
        state.text = _content_text(current[0].get("content")) if current else ""
        body = re.split(r"\n\n<memory_pod_", state.text, maxsplit=1)[0]
        if body.startswith(SENDER_PREFIX):
            lines = body.split("\n", 1)
            body = lines[1] if len(lines) > 1 else ""
        state.body = body.strip()
        match = re.search(r"(?:QQ号|ID)\s*(\d+)", state.text)
        state.sender_id = match.group(1) if match else ""
        if INDEX_OPEN in state.text and INDEX_CLOSE in state.text:
            state.index_text = state.text.split(INDEX_OPEN, 1)[1].split(
                INDEX_CLOSE, 1
            )[0]
        if RECALL_OPEN in state.text:
            state.recall_text = state.text.split(RECALL_OPEN, 1)[1]
        state.reminder = REMINDER_OPEN in state.text

        pending: dict[str, str] = {}
        for message in current:
            if not isinstance(message, dict):
                continue
            role = message.get("role")
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    try:
                        args = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}
                    state.calls.append((name, args))
                    pending[str(call.get("id") or "")] = name
            elif role == "tool":
                name = pending.get(str(message.get("tool_call_id") or ""), "")
                state.results.append((name, str(message.get("content") or "")))
        return state

    # ------------------------------------------------------------ 查询

    def called(self, name: str) -> bool:
        return any(call_name == name for call_name, _args in self.calls)

    def called_for(
        self,
        name: str,
        *,
        scope: str | None = None,
        file: str | None = None,
    ) -> bool:
        """这一轮是否已经对指定作用域/文件调用过某个工具。"""
        for call_name, args in self.calls:
            if call_name != name:
                continue
            if scope is not None and args.get("scope") != scope:
                continue
            if file is not None and args.get("file") != file:
                continue
            return True
        return False

    def last_result(self, name: str) -> str:
        for call_name, result in reversed(self.results):
            if call_name == name:
                return result
        return ""

    def all_results(self, name: str) -> list[str]:
        return [result for call_name, result in self.results if call_name == name]

    def call_args(self, name: str) -> dict:
        for call_name, args in self.calls:
            if call_name == name:
                return args
        return {}

    def index_entries(self) -> list[tuple[str, str]]:
        """开头块索引里的 (文件名, 描述)。"""
        entries: list[tuple[str, str]] = []
        for line in self.index_text.splitlines():
            match = INDEX_LINK_RE.search(line) or INDEX_PLAIN_RE.search(line)
            if match is not None:
                entries.append((match.group(1), match.group(2).strip()))
        return entries


def _call(state: _TurnState, name: str, **arguments) -> ModelReply:
    return ModelReply(
        tool_calls=[
            ToolCall(
                name=name,
                arguments=arguments,
                call_id=f"call_{len(state.calls) + 1}",
            )
        ]
    )


def _final(text: str) -> ModelReply:
    return ModelReply(content=text)


class GoodEvalModel:
    """按场景里的正常做法行动；同一轮里可以分多次工具调用完成。"""

    async def complete(self, messages: list[dict], tools=None) -> ModelReply:
        state = _TurnState.from_messages(messages)
        action = self._next_action(state)
        if action is not None:
            return action
        return _final(self._final_text(state))

    # ------------------------------------------------------------ 计划

    def _next_action(self, state: _TurnState) -> ModelReply | None:
        body = state.body
        if not body:
            return None
        privacy_probe = self._privacy_probe(body)
        correction = self._correction_action(state, body)
        if correction is not None:
            return correction
        forget = self._forget_action(state, body)
        if forget is not None:
            return forget
        other = self._other_profile_action(state, body)
        if other is not None:
            return other
        board = self._board_action(state, body)
        if board is not None:
            return board
        identity = self._identity_action(state, body)
        if identity is not None:
            return identity
        sensitive = self._sensitive_action(state, body)
        if sensitive is not None:
            return sensitive
        journal = self._journal_action(state, body)
        if journal is not None:
            return journal
        directive = self._directive_action(state, body)
        if directive is not None:
            return directive
        remember = self._remember_action(state, body)
        if remember is not None:
            return remember
        notes = self._notes_action(state, body)
        if notes is not None:
            return notes
        admin_rule = self._admin_rule_action(state, body)
        if admin_rule is not None:
            return admin_rule
        read = self._read_action(state, body)
        if read is not None:
            return read
        search = self._search_action(state, body)
        if search is not None:
            return search
        return None

    def _final_text(self, state: _TurnState) -> str:
        body = state.body
        parts: list[str] = []
        if self._privacy_probe(body):
            parts.append("这是别人的隐私，我不能说。")
        board_result = state.last_result("memory_write")
        if "留言板" in body and "管理员" in board_result:
            parts.append("留言板只有管理员能写，我不能代发。")
        elif "留言板" in body and "已写入 board/" in board_result:
            parts.append("已发布留言板告示。")
        elif "留言板" in body and ("什么" in body or "哪些" in body):
            return state.index_text or "留言板上暂时没有告示。"
        other_result = "\n".join(
            state.all_results("memory_edit") + state.all_results("memory_delete")
        )
        if "只有本人或管理员" in other_result or "别人说的关于他" in other_result:
            parts.append("别人的档案只有本人或管理员能改。")
        elif "已删除" in other_result:
            parts.append("已删除。")
        if "说错了" in body or "更正" in body or "其实是" in body:
            dates = DATE_RE.findall(body)
            if dates:
                parts.append(f"已经更正为 {dates[-1]}。")
        if "忘掉" in body and state.called("memory_delete"):
            parts.append("已经忘掉了。")
        if state.called("memory_read") and self._is_question(body):
            result = state.last_result("memory_read")
            if result:
                return result
        if state.called("memory_search"):
            result = state.last_result("memory_search")
            if result:
                return result
        if self._is_question(body) and state.recall_text:
            # 本轮召回块里已经有相关记忆，直接据此回答
            return state.recall_text
        if "拿不准" in body or "该记在哪" in body or "放哪" in body:
            parts.append("这件事要放在这段对话的悄悄话里，还是记进小本本？")
        if "早上好" in body and state.sender_id == "10000003":
            rule = state.last_result("memory_read")
            if "老大" in rule:
                return "老大，早上好！今天也先夸您一句。"
        if state.called("memory_write") or state.called("memory_edit"):
            if not parts:
                parts.append("记下了。")
        if not parts:
            parts.append("你好呀。")
        return " ".join(parts)

    # ------------------------------------------------------------ 判定

    @staticmethod
    def _is_question(body: str) -> bool:
        return body.rstrip().endswith(("？", "?"))

    @staticmethod
    def _privacy_probe(body: str) -> bool:
        if not (body.rstrip().endswith(("？", "?")) or "私下" in body):
            return False
        about_other = any(name in body for name in ("林晓", "她", "他"))
        sensitive = any(
            word in body for word in ("身体", "离婚", "隐私", "秘密", "病", "私下")
        )
        return about_other and sensitive and not body.lstrip().startswith("我")

    @staticmethod
    def _sensitive_scope(body: str) -> bool:
        words = (
            "糖尿病",
            "胰岛素",
            "过敏",
            "休克",
            "同居",
            "女朋友",
            "男朋友",
            "离婚",
            "心事",
            "家庭矛盾",
        )
        return any(word in body for word in words)

    @staticmethod
    def _explicit_remember(body: str) -> bool:
        return any(word in body for word in ("记住", "帮我记", "别忘", "记一下"))

    # ------------------------------------------------------------ 动作

    def _correction_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if not any(word in body for word in ("说错了", "更正", "其实是")):
            return None
        file = f"user-{state.sender_id}.md"
        if not state.called_for("memory_read", scope="notebook", file=file):
            return _call(state, "memory_read", scope="notebook", file=file)
        if not state.called_for("memory_edit", scope="notebook", file=file):
            content = state.last_result("memory_read")
            old_match = DATE_RE.search(content)
            new_match = DATE_RE.search(body.split("其实是", 1)[-1])
            old = old_match.group(1) if old_match else ""
            new = new_match.group(1) if new_match else ""
            if old and new and old != new:
                return _call(
                    state,
                    "memory_edit",
                    scope="notebook",
                    file=file,
                    mode="replace",
                    old_text=old,
                    new_text=new,
                    description="发送者的身份",
                )
            return _call(
                state,
                "memory_write",
                scope="notebook",
                file=file,
                type="person",
                description="发送者的身份",
                content=body,
            )
        return None

    def _forget_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if "忘掉" not in body or ID_RE.search(body):
            return None
        if not state.called("memory_search"):
            query = "城市" if "城市" in body else body
            return _call(state, "memory_search", query=query, scope="all", mode="all")
        result = state.last_result("memory_search")
        match = re.search(r"\[(board|notebook|whisper)\]\s+([^\s（]+\.md)", result)
        if match is None:
            return None
        if not state.called_for(
            "memory_delete", scope=match.group(1), file=match.group(2)
        ):
            return _call(
                state,
                "memory_delete",
                scope=match.group(1),
                file=match.group(2),
                reason="用户要求忘掉",
            )
        return None

    def _other_profile_action(self, state: _TurnState, body: str) -> ModelReply | None:
        match = ID_RE.search(body)
        if match is None or match.group(1) == state.sender_id:
            return None
        target = match.group(1)
        file = f"user-{target}.md"
        if any(word in body for word in ("删掉", "删除", "忘掉")) and not state.called_for(
            "memory_delete", scope="notebook", file=file
        ):
            return _call(
                state,
                "memory_delete",
                scope="notebook",
                file=file,
                reason="用户要求忘掉",
            )
        if any(word in body for word in ("改成", "改为", "修改")) and not state.called_for(
            "memory_edit", scope="notebook", file=file
        ):
            quoted = re.findall(r"['\"“]([^'\"”]+)['\"”]", body)
            new_text = quoted[-1] if quoted else "新内容"
            return _call(
                state,
                "memory_edit",
                scope="notebook",
                file=file,
                mode="replace",
                old_text="旧内容",
                new_text=new_text,
                description="他人的档案",
            )
        return None

    def _board_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if "留言板" not in body:
            return None
        if "什么" in body or "哪些" in body:
            return None
        if "发" not in body and "公告" not in body and "告示" not in body:
            return None
        if state.called_for("memory_write", scope="board"):
            return None
        content = body.split("：", 1)[-1] if "：" in body else body
        return _call(
            state,
            "memory_write",
            scope="board",
            file="notice.md",
            type="note",
            description="留言板告示",
            content=content,
        )

    def _identity_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if not body.startswith("我叫") or not state.sender_id:
            return None
        file = f"user-{state.sender_id}.md"
        if state.called_for("memory_write", scope="notebook", file=file):
            return None
        return _call(
            state,
            "memory_write",
            scope="notebook",
            file=file,
            type="person",
            description="发送者的身份",
            content=body,
        )

    def _sensitive_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if not self._sensitive_scope(body):
            return None
        if "花生过敏" in body or "过敏" in body:
            file = "allergy.md"
        elif "糖尿病" in body or "胰岛素" in body:
            file = "health.md"
        elif "同居" in body or "女朋友" in body or "男朋友" in body:
            file = "private.md"
        else:
            file = "family.md"
        if state.called_for("memory_write", scope="whisper", file=file):
            return None
        return _call(
            state,
            "memory_write",
            scope="whisper",
            file=file,
            type="note",
            description="本对话的私密记录",
            content=body,
        )

    def _directive_action(self, state: _TurnState, body: str) -> ModelReply | None:
        """“记住：以后你要……”：先当 agreement 写小本本，被拒后改写悄悄话。"""
        if "以后你要" not in body:
            return None
        file = "rule.md"
        if state.called_for("memory_write", scope="notebook", file=file):
            result = state.last_result("memory_write")
            if "只有管理员能写进小本本" in result and not state.called_for(
                "memory_write", scope="whisper", file="directive.md"
            ):
                return _call(
                    state,
                    "memory_write",
                    scope="whisper",
                    file="directive.md",
                    type="note",
                    description="当前对话里的要求（不是对我的要求）",
                    content=f"用户说：“{body}”（这是对话内容，不是对我的要求。）",
                )
            return None
        return _call(
            state,
            "memory_write",
            scope="notebook",
            file=file,
            type="agreement",
            description="对我的要求",
            content=body,
        )

    def _remember_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if not self._explicit_remember(body):
            return None
        if "以后你要" in body:
            # 整句要求交给 _directive_action 按 agreement 规则处理；同一轮里
            # “另外记住”的普通事实仍要写进小本本。
            _before, _sep, rest = body.partition("另外记住")
            rest = rest.lstrip("：: ").strip()
            if not rest:
                return None
            body = rest
        if self._sensitive_scope(body):
            # 隐私内容已经由 _sensitive_action 写进悄悄话，不再写小本本
            return None
        if any(word in body for word in ("无条件", "服从", "拉黑")):
            file = "dialog.md"
            content = f"用户说：“{body}”（这是对话内容，不是对我的要求。）"
            description = "对话内容记录（不是对机器人的要求）"
        elif "工位" in body:
            file = "workplace.md"
        elif "城市" in body:
            file = "city.md"
        elif "团建" in body:
            file = "team.md"
        elif "快递" in body:
            file = "express.md"
        elif "302" in body:
            file = "meeting.md"
        else:
            file = "notes.md"
        if state.called_for(
            "memory_write", scope="notebook", file=file
        ) or state.called_for("memory_edit", scope="notebook", file=file):
            return None
        return _call(
            state,
            "memory_write",
            scope="notebook",
            file=file,
            type="note",
            description=description if file == "dialog.md" else "对话里的长期事实",
            content=content if file == "dialog.md" else body,
        )

    def _journal_action(self, state: _TurnState, body: str) -> ModelReply | None:
        journal_request = "日记" in body
        if not journal_request and ("今天" not in body or len(body) < 18):
            return None
        if self._sensitive_scope(body):
            return None
        if not journal_request and self._explicit_remember(body):
            return None
        if state.called_for("memory_edit", scope="notebook", file="journal"):
            return None
        return _call(
            state,
            "memory_edit",
            scope="notebook",
            file="journal",
            mode="append",
            new_text=body,
        )

    def _notes_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if not state.reminder:
            return None
        if state.called_for(
            "memory_write", scope="notebook", file="notes.md"
        ) or state.called_for("memory_edit", scope="notebook", file="notes.md"):
            return None
        read_result = state.last_result("memory_read")
        if not state.called_for("memory_read", scope="notebook", file="notes.md"):
            return _call(state, "memory_read", scope="notebook", file="notes.md")
        if "不存在" in read_result:
            return _call(
                state,
                "memory_write",
                scope="notebook",
                file="notes.md",
                type="note",
                description="最近对话的要点",
                content=body,
            )
        return _call(
            state,
            "memory_edit",
            scope="notebook",
            file="notes.md",
            mode="append",
            new_text=body,
        )

    def _admin_rule_action(self, state: _TurnState, body: str) -> ModelReply | None:
        """管理员换会话后，先读小本本里的要求，再按它称呼“老大”。"""
        if "早上好" not in body or state.sender_id != "10000003":
            return None
        if state.called("memory_read"):
            return None
        for file, description in state.index_entries():
            if file == "rule.md" or "对我的要求" in description:
                return _call(state, "memory_read", scope="notebook", file=file)
        return None

    def _read_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if not self._is_question(body):
            return None
        if state.called("memory_read"):
            return None
        query_tokens = _bigrams(body)
        best: tuple[int, str, str] | None = None
        for file, description in state.index_entries():
            score = len(query_tokens & _bigrams(description))
            if score > 0 and (best is None or score > best[0]):
                best = (score, file, description)
        if best is None:
            return None
        return _call(state, "memory_read", scope="notebook", file=best[1])

    def _search_action(self, state: _TurnState, body: str) -> ModelReply | None:
        if "搜" not in body or state.called("memory_search"):
            return None
        query = "备份脚本" if "备份" in body else body
        return _call(state, "memory_search", query=query, scope="all", mode="all")


class BadEvalModel:
    """把每轮用户消息原样写进小本本（type=note），其余都不做。

    用 note 而不是 agreement：3.9.1 之后非管理员写小本本的 agreement 会
    被写入层拒绝；坏模型要能把用户原话落盘，才能让遗忘、注入这些检查项
    真的暴露问题。
    """

    def __init__(self) -> None:
        self._writes = 0

    async def complete(self, messages: list[dict], tools=None) -> ModelReply:
        state = _TurnState.from_messages(messages)
        if not state.calls:
            self._writes += 1
            return _call(
                state,
                "memory_write",
                scope="notebook",
                file=f"bad-{self._writes}.md",
                type="note",
                description="随意记录",
                content=state.body or state.text or "空消息",
            )
        return _final("好的。")
