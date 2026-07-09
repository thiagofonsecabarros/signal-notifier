from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class AlertSchedule:
    frequency_amount: int = 15
    frequency_unit: str = "minutes"
    start_time: str = "09:45"
    timezone: str = "America/Toronto"
    market_hours_only: bool = True


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_schedule(row: dict[str, Any]) -> AlertSchedule:
    return AlertSchedule(
        frequency_amount=max(1, int(row.get("frequency_amount") or 15)),
        frequency_unit=str(row.get("frequency_unit") or "minutes").strip().lower(),
        start_time=str(row.get("start_time") or "09:45").strip(),
        timezone=str(row.get("timezone") or "America/Toronto").strip(),
        market_hours_only=bool(int(row.get("market_hours_only") if row.get("market_hours_only") is not None else 1)),
    )


def frequency_delta(schedule: AlertSchedule) -> timedelta:
    amount = max(1, int(schedule.frequency_amount))
    if schedule.frequency_unit == "minutes":
        return timedelta(minutes=amount)
    if schedule.frequency_unit == "hours":
        return timedelta(hours=amount)
    if schedule.frequency_unit == "days":
        return timedelta(days=amount)
    raise ValueError(f"Unsupported alert frequency unit: {schedule.frequency_unit}")


def parse_start_time(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        return time(hour=max(0, min(23, int(hour_text))), minute=max(0, min(59, int(minute_text))))
    except (AttributeError, ValueError):
        return time(hour=9, minute=45)


def is_market_hours(now: datetime, schedule: AlertSchedule) -> bool:
    local_now = now.astimezone(_zone(schedule.timezone))
    if local_now.weekday() >= 5:
        return False
    session_open = local_now.replace(hour=9, minute=30, second=0, microsecond=0)
    session_close = local_now.replace(hour=16, minute=0, second=0, microsecond=0)
    return session_open <= local_now <= session_close


def _anchor_for(now: datetime, schedule: AlertSchedule) -> datetime:
    local_now = now.astimezone(_zone(schedule.timezone))
    start = parse_start_time(schedule.start_time)
    return local_now.replace(
        hour=start.hour,
        minute=start.minute,
        second=0,
        microsecond=0,
    )


def previous_slot(now: datetime, schedule: AlertSchedule) -> datetime:
    anchor = _anchor_for(now, schedule)
    delta = frequency_delta(schedule)
    local_now = now.astimezone(anchor.tzinfo)
    if local_now < anchor:
        return (anchor - delta).astimezone(UTC)
    slots = int((local_now - anchor) // delta)
    return (anchor + slots * delta).astimezone(UTC)


def next_slot(now: datetime, schedule: AlertSchedule) -> datetime:
    slot = previous_slot(now, schedule)
    if slot >= now.astimezone(UTC):
        return slot
    return (slot + frequency_delta(schedule)).astimezone(UTC)


def is_schedule_due(
    *,
    now: datetime,
    schedule: AlertSchedule,
    last_alerted_at: str | None,
) -> bool:
    now_utc = now.astimezone(UTC)
    if schedule.market_hours_only and not is_market_hours(now_utc, schedule):
        return False
    last_alerted = parse_datetime(last_alerted_at)
    if last_alerted is None:
        return now_utc >= _anchor_for(now_utc, schedule).astimezone(UTC)
    return now_utc - last_alerted >= frequency_delta(schedule)


def next_eligible_send_at(
    *,
    now: datetime,
    schedule: AlertSchedule,
    last_alerted_at: str | None,
) -> datetime:
    now_utc = now.astimezone(UTC)
    last_alerted = parse_datetime(last_alerted_at)
    earliest = now_utc if last_alerted is None else last_alerted + frequency_delta(schedule)
    candidate = max(next_slot(earliest, schedule), _anchor_for(now_utc, schedule).astimezone(UTC))
    if not schedule.market_hours_only:
        return candidate

    for _ in range(10):
        if is_market_hours(candidate, schedule):
            return candidate
        local = candidate.astimezone(_zone(schedule.timezone)) + timedelta(days=1)
        start = parse_start_time(schedule.start_time)
        candidate = local.replace(hour=start.hour, minute=start.minute, second=0, microsecond=0).astimezone(UTC)
    return candidate
