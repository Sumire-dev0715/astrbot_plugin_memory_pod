#!/usr/bin/env python3
"""测量召回成本：回放 archive/ 里存档的用户消息，统计每轮开头块与召回块的字数。

用法：

    python scripts/measure_recall.py \
        --data-dir /path/to/plugin_data/astrbot_plugin_memory_beyond

数据目录应当是插件数据目录的副本（里面有 archive/ 与 memories/）。脚本只读：
不写使用统计、不改任何记忆文件；输出只有统计数字，不打印任何记忆或对话正文。

口径：
- 每轮取 archive/<会话目录>/<归档ID>.json 里的一条 user 消息，按 created_at
  顺序在同一会话内串联；归档之间、会话之间不串轮。
- 作用域与生产一致：session 用存档里的 umo，people 用 umo 的平台前缀，
  global 按参数开启。默认与 dev 配置一致：人物记忆开、全局记忆关、遗忘关、
  关键词模式、每轮最多 5 个文件（recall.DEFAULT_MAX_FILES）。
- 召回块调用 core.recall.assemble_recall，与 main.py 每轮召回是同一个函数，
  所以"选中哪些文件、带多少字"的判断与线上完全一致。
- 发送者 ID 优先从消息开头的 [发送者：…] 标注取（prompts.sender_ids_in），
  取不到时用私聊会话键里的用户 ID。
- 开头块是会话索引块（prompts.build_index_block）的字数；其中人物档案指
  以全文形式钉在开头块里的人物文件字数——3.6 第 1 条（pin_private_profile）
  之前为 0，人物档案仍在每轮召回块里。
- 记忆用副本里的当前快照，不重建"那一轮当时"的历史状态：早期轮次可能
  召回后来才写入的记忆。改造前后用同一份数据、同一口径对比，结论不受
  影响。

--recall-full-files 是 3.6 第 2 条（召回分档）的入口，先保留：分档尚未实现，
给定时只在 stderr 说明本次仍按改造前口径统计。改造前后在同一份数据上跑
同一个脚本即可对比。
"""

from __future__ import annotations

import argparse
import asyncio
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

# 允许直接运行 scripts/measure_recall.py（仓库根加入导入路径）
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import archive, memstore, prompts, recall, registry  # noqa: E402


@dataclass
class Report:
    """一次测量的全部统计值；只含数字与计数，不含任何正文。"""

    rounds: int = 0
    sessions: int = 0
    archives: int = 0
    skipped_archives: int = 0
    # 每轮的数字，按回放顺序追加（会话之间也连续）
    recall_chars: list[int] = field(default_factory=list)
    recall_files: list[int] = field(default_factory=list)
    full_files: list[int] = field(default_factory=list)
    opening_chars: list[int] = field(default_factory=list)
    profile_chars: list[int] = field(default_factory=list)
    truncations: int = 0
    # 同一会话内相邻两轮的文件集合完全相同的对数 / 相邻对数
    identical_pairs: int = 0
    adjacent_pairs: int = 0


def _mean(values: list[int]) -> float:
    return sum(values) / len(values) if values else 0.0


def percentile(values: list[int], q: float) -> float:
    """最近秩法分位数：排序后取第 ceil(q*n) 个（P90 即第 90% 个）。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(q * len(ordered)) - 1)
    return float(ordered[index])


def _load_archives(data_dir: Path) -> list[tuple[str, list[dict]]]:
    """读出 archive/ 下每个会话的归档，按 created_at 从旧到新排序。"""
    root = data_dir / "archive"
    if not root.is_dir():
        return []
    store = archive.ArchiveStore(root)
    sessions: list[tuple[str, list[dict]]] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir():
            continue
        entries: list[dict] = []
        for path in sorted(folder.glob("*" + archive.ARCHIVE_SUFFIX)):
            data = store.load(folder.name, path.stem)
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                entries.append(data)
        if not entries:
            continue
        entries.sort(
            key=lambda item: (
                float(item.get("created_at") or 0.0),
                str(item.get("id") or ""),
            )
        )
        sessions.append((folder.name, entries))
    sessions.sort(key=lambda item: item[0])
    return sessions


def _session_umo(key: str, archives: list[dict]) -> str:
    """会话的 umo：存档里记的最可靠；没有时按目录名尝试还原。"""
    for data in archives:
        umo = str(data.get("umo") or "").strip()
        if umo:
            return umo
    return registry.umo_from_key(key)


def _round_query(text: str, umo: str) -> tuple[str, str]:
    """一条存档用户消息的 (发送者 ID, 去掉标注的正文)。

    发送者优先取插件注入的 [发送者：…] 标注；存档里没有标注（旧版本或
    非 QQ 平台）时，私聊会话键里的目标就是对方用户 ID。
    """
    query = prompts.strip_sender_tag(text)
    ids = prompts.sender_ids_in(text)
    sender = ids[0] if ids else ""
    if not sender:
        _, kind, target = registry.parse_umo(umo)
        if kind == registry.KIND_PRIVATE:
            sender = target
    return sender, query


def _user_messages(archives: list[dict]) -> list[tuple[str, float]]:
    """按时间顺序取出所有非空用户消息（正文与归档时间）。"""
    messages: list[tuple[str, float]] = []
    for data in archives:
        at = float(data.get("created_at") or 0.0)
        for message in data.get("messages") or []:
            if not isinstance(message, dict) or message.get("role") != "user":
                continue
            text = str(message.get("text") or "").strip()
            if text:
                messages.append((text, at))
    return messages


async def _measure(
    data_dir: Path,
    *,
    people: bool,
    global_memory: bool,
    limit: int,
) -> Report:
    report = Report()
    store = memstore.MemoryStore(data_dir / "memories")
    for key, archives in _load_archives(data_dir):
        umo = _session_umo(key, archives)
        if not umo:
            report.skipped_archives += len(archives)
            continue
        report.sessions += 1
        report.archives += len(archives)
        platform, _, _ = registry.parse_umo(umo)
        session_store = store.session_scope(umo)
        global_index = None
        if global_memory:
            global_index = (await store.global_scope().load_index()).text
        opening = prompts.build_index_block(
            global_index, (await session_store.load_index()).text
        ) or ""
        stores: dict[str, memstore.ScopeStore] = {"session": session_store}
        if global_memory:
            stores["global"] = store.global_scope()
        if people:
            stores["people"] = store.people_scope(platform)
        candidates: list[recall.Candidate] = []
        for label, scope_store in stores.items():
            for header in await asyncio.to_thread(scope_store.headers):
                candidates.append(recall.Candidate(label, header))
        # 3.6 第 1 条实现之前，开头块里没有以全文形式钉住的人物档案
        profile_chars = 0
        previous: list[str] | None = None
        for text, at in _user_messages(archives):
            sender, query = _round_query(text, umo)
            plan = await recall.assemble_recall(
                stores,
                candidates,
                query=query,
                forced_files=recall.identity_files(sender, [], query),
                limit=limit,
                data_notice=prompts.DATA_NOTICE,
                now=at or None,
            )
            files = plan.files
            report.rounds += 1
            report.recall_chars.append(len(plan.block) if plan.block else 0)
            report.recall_files.append(len(plan.sections))
            report.full_files.append(
                sum(1 for section in plan.sections if not section.truncated)
            )
            report.truncations += sum(
                1 for section in plan.sections if section.truncated
            )
            report.opening_chars.append(len(opening))
            report.profile_chars.append(profile_chars)
            if previous is not None:
                report.adjacent_pairs += 1
                if set(previous) == set(files):
                    report.identical_pairs += 1
            previous = files
    return report


def measure(
    data_dir: Path | str,
    *,
    people: bool = True,
    global_memory: bool = False,
    limit: int = recall.DEFAULT_MAX_FILES,
) -> Report:
    """同步入口：在数据目录副本上回放并测量（只读）。"""
    return asyncio.run(
        _measure(
            Path(data_dir),
            people=people,
            global_memory=global_memory,
            limit=limit,
        )
    )


def format_report(report: Report) -> str:
    """把统计值排成固定格式的几行；只有数字，没有正文或文件名。"""
    ratio = (
        100.0 * report.identical_pairs / report.adjacent_pairs
        if report.adjacent_pairs
        else 0.0
    )
    return "\n".join(
        [
            f"轮数：{report.rounds}（会话 {report.sessions}，存档 {report.archives}）",
            "每轮召回字数："
            f"平均 {_mean(report.recall_chars):.1f}，"
            f"中位数 {percentile(report.recall_chars, 0.5):.0f}，"
            f"P90 {percentile(report.recall_chars, 0.9):.0f}",
            f"平均全文文件数：{_mean(report.full_files):.2f}",
            f"平均召回文件数（含截断）：{_mean(report.recall_files):.2f}",
            f"截断次数：{report.truncations}",
            "相邻两轮召回同一批文件的比例："
            f"{ratio:.1f}%（共 {report.adjacent_pairs} 对）",
            "开头块字数："
            f"平均 {_mean(report.opening_chars):.1f}，"
            f"中位数 {percentile(report.opening_chars, 0.5):.0f}，"
            f"P90 {percentile(report.opening_chars, 0.9):.0f}"
            f"（其中人物档案 平均 {_mean(report.profile_chars):.1f}）",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="回放 archive/ 里的用户消息，统计每轮开头块与召回块字数（只输出数字）"
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        type=Path,
        help="插件数据目录的副本（含 archive/ 与 memories/）",
    )
    parser.add_argument(
        "--recall-full-files",
        type=int,
        default=None,
        help="每轮带全文的召回文件数（3.6 第 2 条；分档尚未实现，暂不影响统计）",
    )
    parser.add_argument(
        "--no-people",
        action="store_true",
        help="不计入人物记忆（默认计入，与 dev 配置一致）",
    )
    parser.add_argument(
        "--global-memory",
        action="store_true",
        help="计入全局记忆（默认关闭，与 dev 配置一致）",
    )
    args = parser.parse_args(argv)
    if not (args.data_dir / "archive").is_dir() and not (
        args.data_dir / "memories"
    ).is_dir():
        print(f"数据目录里没有 archive/ 或 memories/：{args.data_dir}", file=sys.stderr)
        return 2
    if args.recall_full_files is not None:
        if args.recall_full_files <= 0:
            print("--recall-full-files 必须是正整数", file=sys.stderr)
            return 2
        print(
            "提示：召回分档（recall_full_files）尚未实现，"
            "本次仍按改造前口径统计",
            file=sys.stderr,
        )
    report = measure(
        args.data_dir,
        people=not args.no_people,
        global_memory=args.global_memory,
    )
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
