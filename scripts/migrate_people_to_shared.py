#!/usr/bin/env python3
"""把各机器人原来的 people 目录复制进共享人物空间 people/_shared/。

共享人物空间启用后，成员机器人统一读写 people/_shared/，原来按平台分开的
people/<platform>/ 不再被插件使用。本脚本把那些目录里的 user-*.md（含
forgotten/ 下已遗忘的）和使用统计原样复制过去，写入时间与 frontmatter 都
不改，原目录保留不动。

默认只预演（dry-run），不写任何文件；加 --apply 才真正复制。

    python scripts/migrate_people_to_shared.py \
        --data-dir /path/to/plugin_data/astrbot_plugin_memory_beyond \
        --from Bot-A --from Bot-B

同一个 user-<数字ID>.md 在多个来源都有、或共享目录里已经有同名文件时，
不覆盖也不合并，写进冲突报告等人工处理。复制经 ScopeStore.import_files
落盘，会按 3.1 节在 _shared/.audit.jsonl 记一行 actor=transfer 的审计；
共享目录里原本没有这份文件，所以不需要额外的历史版本。

脚本只打印文件名、条数、来源和冲突，不打印任何记忆正文。预演会列出计划
复制的文件名；--apply 之前会再次提醒：规格要求先人工确认 user-1085188139.md
怎么拆，脚本不会拆分文件里的关系 / 健康 / 性 / 家庭等 session 私有内容。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许直接运行 scripts/migrate_people_to_shared.py（仓库根加入导入路径）
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import history, memstore, sharing  # noqa: E402


class MigrationError(Exception):
    """可以直接展示给使用者的错误。"""


@dataclass
class SourceFile:
    """来源目录里的一份人物记忆。"""

    name: str
    source: str
    content: str
    usage: memstore.Usage
    mtime: float
    forgotten: bool = False


@dataclass
class Conflict:
    """没有复制、需要人工处理的一份记忆。"""

    name: str
    sources: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass
class Plan:
    """预演结果：要复制的条目与冲突报告。"""

    entries: list[memstore.ImportEntry] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    files: int = 0
    sources: list[str] = field(default_factory=list)
    # 文件名 -> 来源标签；没有冲突的条目也记一份，导入阶段报错时能找回来源
    origins: dict[str, str] = field(default_factory=dict)


def _source_key(people_root: Path, raw: str) -> str:
    """把 --from 的参数换成 people 下的目录名。

    优先接受已经是 safe_key 输出形态、且磁盘上确实存在的目录名（方便手工
    对照管理页）；否则按 platform_id 计算 safe_key。目录不存在时直接报错，
    避免拼错的 platform_id 被当成空来源、看起来迁移成功却漏了数据。
    共享目录本身不能当来源。
    """
    raw = (raw or "").strip()
    if not raw:
        raise MigrationError("--from 不能为空")
    if raw == sharing.SHARED_KEY:
        raise MigrationError("--from 不能是共享目录 _shared")
    if memstore.is_scope_key(raw) and (people_root / raw).is_dir():
        return raw
    key = memstore.safe_key(raw)
    if not (people_root / key).is_dir():
        raise MigrationError(f"来源 {raw} 的 people 目录不存在：{people_root / key}")
    return key


async def _read_source(store: memstore.ScopeStore, label: str) -> list[SourceFile]:
    """读一个来源目录里的全部 user-*.md（含已遗忘的、使用统计与写入时间）。"""
    result: list[SourceFile] = []
    for name in store.list_files():
        if not memstore.is_person_file(name):
            continue
        content = await store.read(name)
        if content is None:
            continue
        result.append(
            SourceFile(
                name=name,
                source=label,
                content=content,
                usage=store.usage_of(name),
                mtime=store.mtime(name) or 0.0,
            )
        )
    for name in store.forgotten_files():
        if not memstore.is_person_file(name):
            continue
        content = await store.read_forgotten(name)
        if content is None:
            continue
        path = store.root / memstore.FORGOTTEN_DIRNAME / name
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        result.append(
            SourceFile(
                name=name,
                source=label,
                content=content,
                usage=store.usage_of(name),
                mtime=mtime,
                forgotten=True,
            )
        )
    return result


async def _collect_plan(
    base_dir: Path, source_specs: list[str]
) -> tuple[Plan, memstore.ScopeStore, list[tuple[str, int]]]:
    """读来源并算出去向；返回 (计划, 共享作用域, 每个来源的文件数)。"""
    if not base_dir.is_dir():
        raise MigrationError(f"找不到 memories 目录：{base_dir}")
    store = memstore.MemoryStore(base_dir)
    target = store.people_scope_by_key(sharing.SHARED_KEY)
    if target is None:
        raise MigrationError("共享人物目录不可用")

    people_root = base_dir / "people"
    files: list[SourceFile] = []
    counts: list[tuple[str, int]] = []
    seen_sources: set[str] = set()
    for raw in source_specs:
        key = _source_key(people_root, raw)
        if key in seen_sources:
            raise MigrationError(f"来源 {raw} 重复")
        seen_sources.add(key)
        scope = store.people_scope_by_key(key)
        if scope is None:
            raise MigrationError(f"来源目录名不合法：{raw}")
        found = await _read_source(scope, raw.strip())
        counts.append((raw.strip(), len(found)))
        files.extend(found)

    plan = Plan(files=len(files), sources=[raw for raw, _ in counts])
    grouped: dict[str, list[SourceFile]] = {}
    for item in files:
        grouped.setdefault(item.name, []).append(item)
    for name in sorted(grouped):
        group = grouped[name]
        sources = list(dict.fromkeys(item.source for item in group))
        if len(group) > 1:
            plan.conflicts.append(
                Conflict(name, sources, "同一个人的记忆在多个来源都有")
            )
            continue
        if target.exists(name) or target.has_forgotten(name):
            plan.conflicts.append(
                Conflict(name, sources, "共享目录里已有同名记忆")
            )
            continue
        item = group[0]
        plan.origins[item.name] = item.source
        plan.entries.append(
            memstore.ImportEntry(
                name=item.name,
                content=item.content,
                forgotten=item.forgotten,
                usage=item.usage,
                mtime=item.mtime,
            )
        )
    return plan, target, counts


async def _run(args: argparse.Namespace) -> int:
    base_dir = Path(args.data_dir).expanduser() / "memories"
    plan, target, counts = await _collect_plan(base_dir, args.from_platforms)

    for label, count in counts:
        if count == 0:
            print(f"来源 {label}：没有 user-*.md，跳过")
    if not plan.files:
        print("没有找到可迁移的人物记忆")
        return 0
    if not args.apply:
        names = "、".join(entry.name for entry in plan.entries) or "（无）"
        print(f"计划复制的文件（{len(plan.entries)} 个）：{names}")
    else:
        print(
            "注意：迁移按整份 user-*.md 复制，不会拆分文件内容。"
            "若某个文件里有与某个机器人的关系、健康、性、家庭矛盾等只应在"
            "原私聊可见的段落，请先停止 --apply，按 PLAN 第 6 节人工拆分到"
            "对应会话的 session；复制之后所有共享成员机器人都能读到整份文件。"
        )

    results = await target.import_files(
        plan.entries,
        memstore.CONFLICT_SKIP,
        dry_run=not args.apply,
        actor=history.ACTOR_TRANSFER,
    )
    copied = sum(1 for item in results if item.outcome == memstore.IMPORT_ADDED)
    for item in results:
        if item.outcome in (memstore.IMPORT_KEPT, memstore.IMPORT_INVALID):
            origin = plan.origins.get(item.name, "")
            plan.conflicts.append(
                Conflict(item.name, [origin] if origin else [], item.reason or "没有写入")
            )

    if args.apply:
        # 共享目录里可能已有文件而索引缺失（上次迁移中断、手工拷入）：
        # 导入只补本次文件的行，这里按磁盘实际文件把缺的行补齐
        report = await asyncio.to_thread(target.index_report)
        if report.missing:
            await target.rebuild_index()
        print(f"复制 {copied} 条，冲突 {len(plan.conflicts)} 条")
    else:
        print(f"计划复制 {copied} 条，冲突 {len(plan.conflicts)} 条")
    if plan.conflicts:
        print("冲突报告（没有覆盖任何文件）：")
        for item in plan.conflicts:
            sources = "、".join(item.sources) if item.sources else "本次计划"
            print(f"  {item.name}：{item.reason}（来源 {sources}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="把各机器人原来的 people 目录复制进共享人物空间 _shared"
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="插件数据目录（包含 memories/），例如 plugin_data/astrbot_plugin_memory_beyond",
    )
    parser.add_argument(
        "--from",
        dest="from_platforms",
        action="append",
        required=True,
        help="来源机器人的 platform_id，可重复；原目录按 people/<safe_key> 查找",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正复制；不加时只预演，不写任何文件",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except MigrationError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
