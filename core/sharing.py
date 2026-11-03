"""共享人物空间：成员表、写权限等级与覆盖前的版本检查。

配置 `shared_people_bots` 每项是 `platform_id` 或 `platform_id=edit|append|read`，
不写等级默认 `append`；为空表示功能关闭，people 仍按平台各自一份，与上游一致。

成员机器人的 people 作用域统一映射到 `people/_shared/`，目录映射只由
MemoryStore.people_scope() 决定；本模块只负责回答"谁是成员、有什么权限"。

权限只看事件的 platform_id（服务端按适配器给出，模型无法通过工具参数伪造）：
    edit   完整写权限，和上游一样；
    append 只能新建文件，或用 memory_edit 的追加模式补充；不能覆盖、不能删除；
    read   只能读；写入请求一律拒绝，并建议写到 session。

覆盖前的版本检查只作用于共享空间：记住每个机器人最近读过（memory_read
或召回附上全文）的每个文件的哈希；memory_write 整文件覆盖时，如果当前
内容与它看过的不一致就拒绝，提示先重新读取或改用 memory_edit。追加与
逐字替换不会丢别人的内容，不做这项检查。

版本记录放在插件数据目录的 state/ 下（不是记忆文件），由 SeenVersions 管理。
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

# 共享人物作用域的目录名。safe_key 的输出不会以 _ 开头，不会与平台目录撞名。
SHARED_KEY = "_shared"

PERMISSION_EDIT = "edit"
PERMISSION_APPEND = "append"
PERMISSION_READ = "read"
PERMISSIONS = (PERMISSION_EDIT, PERMISSION_APPEND, PERMISSION_READ)
DEFAULT_PERMISSION = PERMISSION_APPEND

# 工具回执：拒绝时把模型引导到正确的作用域或做法
READ_ONLY_REFUSAL = (
    "共享人物空间里你没有写入权限（read）：不能新建、修改或删除 people 记忆。"
    "关于人的新信息请写到 session 作用域。"
)
APPEND_OVERWRITE_REFUSAL = (
    "你在共享人物空间里只有追加权限（append）：不能覆盖已有的 people 文件。"
    "要补充内容请用 memory_edit 并设 mode=append；确实要改写请让有 edit 权限的机器人处理。"
)
APPEND_DELETE_REFUSAL = (
    "你在共享人物空间里只有追加权限（append）：不能删除 people 记忆。"
    "如果内容有误，请用 memory_edit 的 mode=append 追加一条更正。"
)
APPEND_EDIT_REFUSAL = (
    "你在共享人物空间里只有追加权限（append）：memory_edit 只能用 mode=append。"
    "要修改已有文字请追加一条更正，或让有 edit 权限的机器人处理。"
)
# 目录映射按 unified_msg_origin 的平台前缀，权限按事件的 platform_id：
# 两者对不上或取不到 platform_id 时无法确认成员身份，共享目录一律拒绝写入。
# 宁可暂时挡住一次合法写入，也不能让 read/append 级成员借身份缺失整文件改写。
IDENTITY_REFUSAL = (
    "无法确认当前机器人在共享人物空间里的身份（取不到事件 platform_id，"
    "或它不在共享成员列表里），为避免越权，这次没有写入 people。"
    "请稍后重试；如持续出现，请检查 AstrBot 适配器与 shared_people_bots 配置。"
)


def version_conflict_message(file: str) -> str:
    """覆盖前的版本检查未通过时的回执。"""
    return (
        f"共享人物记忆 {file} 在你上次读取之后已发生变化（或你还没有读过当前版本）。"
        "为避免覆盖其他机器人的修改，请先用 memory_read 重新读取当前内容；"
        "只想补充内容的话，改用 memory_edit（追加或局部替换）。"
    )


def content_sha1(text: str) -> str:
    """与 core/history 相同的哈希口径；空内容也有稳定值。"""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Member:
    """一个共享成员：平台 ID 与写权限等级。"""

    platform_id: str
    permission: str


def normalize_permission(raw: str) -> str | None:
    """把配置里的等级归一化；未知等级返回 None（保守起见不猜默认值）。"""
    value = (raw or "").strip().lower()
    return value if value in PERMISSIONS else None


@dataclass
class Membership:
    """解析后的成员表；warnings 是给插件日志的告警，不影响其余成员。"""

    members: dict[str, Member] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return bool(self.members)

    def permission(self, platform_id: str) -> str | None:
        """该平台的权限等级；不是成员时返回 None（按上游的独立 people 目录）。"""
        member = self.members.get((platform_id or "").strip())
        return member.permission if member is not None else None

    def is_member(self, platform_id: str) -> bool:
        return (platform_id or "").strip() in self.members

    def platform_ids(self) -> list[str]:
        return list(self.members)

    def filter_same_platform(
        self, platform_types: dict[str, str]
    ) -> "Membership":
        """只保留同一种 IM 平台的成员。

        不同平台的数字 ID 会撞号，不能共用一个 people 目录。能查到适配器
        类型时按多数派（同数取配置里先出现的）确定共享平台，其余成员告警
        并关闭共享；查不到类型的成员（例如适配器暂时没加载）无法校验，保留。
        """
        if not self.members or not platform_types:
            return self
        counts: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        for index, platform_id in enumerate(self.members):
            kind = platform_types.get(platform_id)
            if not kind:
                continue
            counts[kind] = counts.get(kind, 0) + 1
            first_seen.setdefault(kind, index)
        if len(counts) <= 1:
            return self
        canonical = max(counts, key=lambda kind: (counts[kind], -first_seen[kind]))
        kept: dict[str, Member] = {}
        warnings = list(self.warnings)
        for platform_id, member in self.members.items():
            kind = platform_types.get(platform_id)
            if kind and kind != canonical:
                warnings.append(
                    f"共享人物空间：{platform_id} 的平台类型是 {kind}，"
                    f"与其他成员的 {canonical} 不一致，已对 {platform_id} 关闭共享"
                )
                continue
            kept[platform_id] = member
        return Membership(kept, warnings)


def parse_config(raw: object) -> Membership:
    """解析 shared_people_bots 配置。

    支持 list（推荐，AstrBot 4.29.4 的配置 schema 支持 list）以及 text
    的逐行写法，方便旧版本或手工填写的配置。空项与 # 开头的注释行忽略；
    等级写错时跳过该成员并记一条告警，不擅自当成默认的 append。
    """
    if isinstance(raw, str):
        items: list[object] = raw.splitlines()
    elif isinstance(raw, (list, tuple)):
        items = list(raw)
    else:
        items = []
    members: dict[str, Member] = {}
    warnings: list[str] = []
    for item in items:
        line = str(item or "").strip()
        if not line or line.startswith("#"):
            continue
        platform_id, sep, level = line.partition("=")
        platform_id = platform_id.strip()
        if not platform_id:
            warnings.append(f"共享人物空间：忽略没有 platform_id 的配置项 {line!r}")
            continue
        if sep:
            permission = normalize_permission(level)
            if permission is None:
                warnings.append(
                    f"共享人物空间：{platform_id} 的权限等级 {level.strip()!r} 无效"
                    "（只能是 edit、append、read），已跳过这个成员"
                )
                continue
        else:
            permission = DEFAULT_PERMISSION
        if platform_id in members:
            warnings.append(
                f"共享人物空间：{platform_id} 在配置里出现了多次，以最后一次为准"
            )
        members[platform_id] = Member(platform_id, permission)
    return Membership(members, warnings)


def write_refusal(permission: str | None, *, exists: bool, delete: bool) -> str:
    """memory_write 的权限回执；允许时返回空串。permission 为 None 表示非成员。"""
    if permission == PERMISSION_READ:
        return READ_ONLY_REFUSAL
    if permission == PERMISSION_APPEND:
        if delete:
            return APPEND_DELETE_REFUSAL
        if exists:
            return APPEND_OVERWRITE_REFUSAL
    return ""


def edit_refusal(permission: str | None, mode: str) -> str:
    """memory_edit 的权限回执；允许时返回空串。"""
    if permission == PERMISSION_READ:
        return READ_ONLY_REFUSAL
    if permission == PERMISSION_APPEND and mode != "append":
        return APPEND_EDIT_REFUSAL
    return ""


class SeenVersions:
    """每个机器人最近看过的共享文件内容哈希。

    存储格式：{"version": 1, "bots": {"<platform_id>": {"<file>": "<sha1>"}}}。
    只在哈希变化时写盘；文件读写都在锁里，调用方放进工作线程执行。
    """

    VERSION = 1

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        if not self.path.is_file():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        bots = data.get("bots") if isinstance(data, dict) else None
        if not isinstance(bots, dict):
            return {}
        result: dict[str, dict[str, str]] = {}
        for platform_id, files in bots.items():
            if not isinstance(files, dict):
                continue
            result[str(platform_id)] = {
                str(name): str(digest)
                for name, digest in files.items()
                if isinstance(name, str) and isinstance(digest, str)
            }
        return result

    def _save(self, bots: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": self.VERSION, "bots": bots},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(self.path)

    def note(self, platform_id: str, file: str, content: str | None) -> None:
        """记下这个机器人刚看过的版本；content 为 None 时忽略。"""
        platform_id = (platform_id or "").strip()
        file = (file or "").strip()
        if not platform_id or not file or content is None:
            return
        digest = content_sha1(content)
        with self._lock:
            bots = self._load()
            files = bots.setdefault(platform_id, {})
            if files.get(file) == digest:
                return
            files[file] = digest
            self._save(bots)

    def seen(self, platform_id: str, file: str) -> str:
        """该机器人最近看过的版本哈希；没看过返回空串。"""
        platform_id = (platform_id or "").strip()
        file = (file or "").strip()
        if not platform_id or not file:
            return ""
        with self._lock:
            return self._load().get(platform_id, {}).get(file, "")

    def is_current(self, platform_id: str, file: str, content: str) -> bool:
        """当前内容是否正是这个机器人看过的版本。"""
        seen = self.seen(platform_id, file)
        return bool(seen) and seen == content_sha1(content)

    def clear(self) -> None:
        """清空全部版本记录（人物空间整体迁移/清除后调用）。"""
        with self._lock:
            if self.path.is_file():
                self.path.unlink()

    def forget_file(self, file: str) -> int:
        """删除所有机器人对某个文件的版本记录（文件被删除/移走后调用）。

        不清理的话，同名文件以后重新创建时，旧哈希仍会被当成 expected_sha1；
        虽然文件不存在时写入已不再比较，但留着过期记录会让下一次覆盖检查
        误判成"看过旧版本"。返回清掉的记录数。
        """
        file = (file or "").strip()
        if not file:
            return 0
        removed = 0
        with self._lock:
            bots = self._load()
            changed = False
            for platform_id, files in bots.items():
                if file in files:
                    del files[file]
                    removed += 1
                    changed = True
            if changed:
                self._save(bots)
        return removed
