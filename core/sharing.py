"""共享人物空间：成员表与写权限等级。

配置 `shared_people_bots` 每项是 `platform_id` 或 `platform_id=edit|append|read`，
不写等级默认 `append`；为空表示功能关闭，people 仍按平台各自一份，与上游一致。

成员机器人的 people 作用域统一映射到 `people/_shared/`，目录映射只由
MemoryStore.people_scope() 决定；本模块只负责回答"谁是成员、有什么权限"。

权限只看事件的 platform_id（服务端按适配器给出，模型无法通过参数伪造）：
    edit   完整写权限；
    append 只能 create 和 append；不能覆盖、替换或删除；
    read   只能读；写人物空间的请求一律跳过。

后台整理按这里的权限逐条过滤操作；覆盖前的内容哈希由整理流程自己记
（core/writer.py 的快照校验），不再维护"看过版本"记录。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

# 共享人物作用域的目录名。safe_key 的输出不会以 _ 开头，不会与平台目录撞名。
SHARED_KEY = "_shared"

PERMISSION_EDIT = "edit"
PERMISSION_APPEND = "append"
PERMISSION_READ = "read"
PERMISSIONS = (PERMISSION_EDIT, PERMISSION_APPEND, PERMISSION_READ)
DEFAULT_PERMISSION = PERMISSION_APPEND

# 目录映射按 unified_msg_origin 的平台前缀，权限按事件的 platform_id：
# 两者对不上或取不到 platform_id 时无法确认成员身份，共享目录一律拒绝写入。
# 宁可暂时挡住一次合法写入，也不能让 read/append 级成员借身份缺失整文件改写。
IDENTITY_REFUSAL = (
    "无法确认当前机器人在共享人物空间里的身份（取不到事件 platform_id，"
    "或它不在共享成员列表里），为避免越权，这次没有动 people。"
)
# ScopeStore.write(forbid_overwrite=True) 的拒绝文案：append 级成员只能新建
APPEND_OVERWRITE_REFUSAL = (
    "共享人物空间里只有追加权限（append）：不能覆盖已有的 people 文件；"
    "要补充内容请用追加方式，或新建另一个文件。"
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
