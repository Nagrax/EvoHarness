"""时间与逻辑日工具：所有跨天判断必须经过这里，禁止依赖宿主本地时区。"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


UTC = timezone.utc


def utcnow() -> datetime:
    """返回不带 tzinfo 的 UTC 时间，SQLite DateTime 统一使用该口径。"""
    return datetime.now(UTC).replace(tzinfo=None)


def epoch_ms(value: datetime | None = None) -> int:
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def from_epoch_ms(value: int) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=UTC).replace(tzinfo=None)


def logical_date_for(moment: datetime, tz_name: str) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(tz_name)).date()


def previous_day_window(logical_date: date, tz_name: str) -> tuple[int, int]:
    """返回 logical_date 当天 [00:00, 24:00) 的毫秒时间戳（指定时区）。"""
    zone = ZoneInfo(tz_name)
    start = datetime.combine(logical_date, time.min, tzinfo=zone)
    end = datetime.combine(logical_date + timedelta(days=1), time.min, tzinfo=zone)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def current_logical_date(tz_name: str) -> date:
    """当前逻辑日。

    SIM 钩子（仅自迭代演练用）：设置环境变量 FEISHU_SIM_LOGICAL_DATE（ISO 日期，
    如 2026-11-08）时返回该日期，供日报补跑在单日内演练多个逻辑日窗口；
    未设置时行为与真实时钟完全一致（生产默认路径不得受影响）。
    """
    import os

    sim = (os.environ.get("FEISHU_SIM_LOGICAL_DATE") or "").strip()
    if sim:
        return date.fromisoformat(sim)
    return datetime.now(ZoneInfo(tz_name)).date()


def parse_hhmm(value: str) -> time:
    hour, minute = (int(part) for part in value.split(":", 1))
    return time(hour=hour, minute=minute)


def in_time_window(moment: datetime, start_hour: int, end_hour: int, tz_name: str) -> bool:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    local = moment.astimezone(ZoneInfo(tz_name))
    return start_hour <= local.hour < end_hour
