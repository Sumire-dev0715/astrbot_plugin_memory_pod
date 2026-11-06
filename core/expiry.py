"""告示有效期：解析 frontmatter 的 expires，按 AstrBot 的时区判断是否过期。

到期日当天仍然有效，第二天开始过期：比较的是本地日期，不是写入时间或
24 小时窗口。时区取 AstrBot 全局配置的 timezone——AstrBot 4.29.4 的默认值
在 ``astrbot/core/config/default.py`` 第 319 行，它自己用
``plugin_context.get_config().get("timezone")`` 读取（``astrbot/core/
astr_main_agent.py`` 第 1083 行）；没有配置或名字无效时按本机时区，与
AstrBot 的处理一致。

这里只做日期解析与比较，不读盘、不认识作用域。只有留言板告示会用到
expires；其他作用域写了也由调用方忽略（PLAN 3.9.2 第 3 条）。
"""

from __future__ import annotations

from datetime import date, datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXPIRES_FORMAT = "%Y-%m-%d"
MAX_EXPIRES_CHARS = 10


def error_message(value: str) -> str:
    """格式错误时给模型/管理员看的说明。"""
    return (
        "到期日必须是 YYYY-MM-DD 格式的日期（如 2026-11-11），"
        f"收到：{value[:40]}"
    )


def normalize(text: object) -> tuple[str, str]:
    """(规范化的到期日, 错误说明)；空值表示没有到期日，两者都为空串。

    只接受 YYYY-MM-DD，月和日必须补零；不是有效日期时返回错误说明，
    由调用方决定拒绝写入还是当作没有到期日。
    """
    value = str(text if text is not None else "").strip()
    if not value:
        return "", ""
    if len(value) != MAX_EXPIRES_CHARS:
        return "", error_message(value)
    try:
        parsed = datetime.strptime(value, EXPIRES_FORMAT).date()
    except ValueError:
        return "", error_message(value)
    # strptime 接受 2026-1-1 这类不补零的写法，按原样比较才能严格要求格式
    if parsed.strftime(EXPIRES_FORMAT) != value:
        return "", error_message(value)
    return value, ""


def parse(text: object) -> date | None:
    """读到期的日期；没有写、写坏了都当没有到期日（旧文件不会被误判过期）。"""
    value, error = normalize(text)
    if error or not value:
        return None
    return datetime.strptime(value, EXPIRES_FORMAT).date()


def timezone(name: object) -> tzinfo | None:
    """AstrBot 配置的时区名转成 tzinfo；没配置或名字无效返回 None（本机时区）。"""
    value = str(name or "").strip()
    if not value:
        return None
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_date(now: float, tz: tzinfo | None = None) -> date:
    """时间戳在指定时区下的日期；tz 为 None 时用本机时区。"""
    if tz is None:
        return datetime.fromtimestamp(now).date()
    return datetime.fromtimestamp(now, tz).date()


def is_expired(expires: object, now: float, tz: tzinfo | None = None) -> bool:
    """告示是否已过期：到期日当天仍有效，本地日期超过到期日才算过期。"""
    target = parse(expires)
    if target is None:
        return False
    return local_date(now, tz) > target
