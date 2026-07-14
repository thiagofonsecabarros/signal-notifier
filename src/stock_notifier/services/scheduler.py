from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, time as dt_time, timedelta
from html import escape
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.service import scan_alerts
from stock_notifier.notifications.telegram import TelegramClient
from stock_notifier.providers.massive import MassiveClient
from stock_notifier.scoring.service import score_signal

SERVICE_KEYS = ["snapshot", "historical", "profiles"]
SERVICE_LABELS = {
    "snapshot": "Market snapshot",
    "historical": "Historical data",
    "profiles": "Company profiles",
}
SERVICE_RUN_NAMES = {
    "snapshot": "market_snapshot",
    "historical": "historical_data",
    "profiles": "company_profiles",
}
SCHEDULE_UNITS = ["minutes", "hours", "days", "business_days", "weeks"]
SIGNAL_SNAPSHOT_FRESH_MINUTES = 14


@dataclass(frozen=True)
class ScheduledServiceResult:
    service_key: str
    service_name: str
    due: bool
    ran: bool
    status: str
    message: str
    run_id: int | None = None


@dataclass(frozen=True)
class ScheduledSignalResult:
    signal_id: int
    signal_name: str
    due: bool
    ran: bool
    status: str
    message: str
    symbols_scored: int = 0
    alerts_created: int = 0
    delivered: int = 0


def dashboard_setting_key(key: str) -> str:
    return f"dashboard.{key}"


def get_dashboard_setting(database: Database, key: str, default: Any = None) -> Any:
    return database.get_app_setting(dashboard_setting_key(key), default)


def set_dashboard_setting(database: Database, key: str, value: Any) -> None:
    database.set_app_setting(dashboard_setting_key(key), value)


def default_schedule() -> dict[str, Any]:
    return {
        "enabled": False,
        "frequency_amount": 1,
        "frequency_unit": "days",
        "start_time": "09:45",
        "end_time": "16:00",
        "weekdays": [0, 1, 2, 3, 4],
        "timezone": "America/Toronto",
        "notify_telegram": False,
    }


def _clean_schedule(config: dict[str, Any]) -> dict[str, Any]:
    cleaned = default_schedule()
    cleaned.update(config)
    cleaned["enabled"] = bool(cleaned.get("enabled"))
    cleaned["notify_telegram"] = bool(cleaned.get("notify_telegram"))
    try:
        cleaned["frequency_amount"] = max(int(cleaned.get("frequency_amount") or 1), 1)
    except (TypeError, ValueError):
        cleaned["frequency_amount"] = 1
    cleaned["frequency_unit"] = str(cleaned.get("frequency_unit") or "days")
    if cleaned["frequency_unit"] not in SCHEDULE_UNITS:
        cleaned["frequency_unit"] = "days"
    cleaned["start_time"] = str(cleaned.get("start_time") or "09:45")
    cleaned["end_time"] = str(cleaned.get("end_time") or "16:00")
    cleaned["timezone"] = str(cleaned.get("timezone") or "America/Toronto")
    weekdays = cleaned.get("weekdays")
    if not isinstance(weekdays, list):
        weekdays = [0, 1, 2, 3, 4]
    valid_weekdays: list[int] = []
    for day in weekdays:
        try:
            candidate = int(day)
        except (TypeError, ValueError):
            continue
        if 0 <= candidate <= 6 and candidate not in valid_weekdays:
            valid_weekdays.append(candidate)
    cleaned["weekdays"] = valid_weekdays or [0, 1, 2, 3, 4]
    return cleaned


def get_service_schedule(database: Database, service_key: str) -> dict[str, Any]:
    saved = get_dashboard_setting(database, f"services.{service_key}.schedule", {})
    config = default_schedule()
    if isinstance(saved, dict):
        config.update(saved)
    return _clean_schedule(config)


def save_service_schedule(database: Database, service_key: str, config: dict[str, Any]) -> None:
    set_dashboard_setting(database, f"services.{service_key}.schedule", _clean_schedule(config))


def get_signal_schedule(database: Database, signal_id: int) -> dict[str, Any]:
    saved = get_dashboard_setting(database, f"signals.{int(signal_id)}.schedule", {})
    config = default_schedule()
    if isinstance(saved, dict):
        config.update(saved)
    return _clean_schedule(config)


def save_signal_schedule(database: Database, signal_id: int, config: dict[str, Any]) -> None:
    set_dashboard_setting(database, f"signals.{int(signal_id)}.schedule", _clean_schedule(config))


def _parse_time(value: Any) -> dt_time:
    try:
        hour, minute = str(value or "09:45").split(":", 1)
        return dt_time(hour=int(hour), minute=int(minute[:2]))
    except (TypeError, ValueError):
        return dt_time(hour=9, minute=45)


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _business_days_between(start: date, end: date) -> int:
    if end <= start:
        return 0
    days = 0
    current = start + timedelta(days=1)
    while current <= end:
        if current.weekday() < 5:
            days += 1
        current += timedelta(days=1)
    return days


def is_service_due(
    database: Database,
    service_key: str,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> bool:
    if force:
        return True
    schedule = get_service_schedule(database, service_key)
    return _is_schedule_due(
        schedule,
        last_run=get_dashboard_setting(database, f"services.{service_key}.last_scheduled_run_at", None),
        now=now,
    )


def _is_schedule_due(
    schedule: dict[str, Any],
    *,
    last_run: Any,
    now: datetime | None = None,
) -> bool:
    if not bool(schedule.get("enabled")):
        return False
    timezone_name = str(schedule.get("timezone") or "America/Toronto")
    try:
        tz = ZoneInfo(timezone_name)
    except Exception:
        tz = ZoneInfo("America/Toronto")
    local_now = (now or datetime.now(UTC)).astimezone(tz)
    start_time = _parse_time(schedule.get("start_time"))
    if local_now.time() < start_time:
        return False

    amount = max(int(schedule.get("frequency_amount") or 1), 1)
    unit = str(schedule.get("frequency_unit") or "days")
    if unit == "minutes":
        end_time = _parse_time(schedule.get("end_time") or "16:00")
        if local_now.time() > end_time:
            return False
        weekdays = schedule.get("weekdays")
        if isinstance(weekdays, list) and weekdays and local_now.weekday() not in {int(day) for day in weekdays}:
            return False

    parsed_last_run = _parse_datetime(last_run)
    if parsed_last_run is None:
        return True
    last_local = parsed_last_run.astimezone(tz)
    if unit == "minutes":
        return local_now - last_local >= timedelta(minutes=amount)
    if unit == "hours":
        return local_now - last_local >= timedelta(hours=amount)
    if unit == "days":
        return (local_now.date() - last_local.date()).days >= amount
    if unit == "business_days":
        return _business_days_between(last_local.date(), local_now.date()) >= amount and local_now.weekday() < 5
    if unit == "weeks":
        return local_now - last_local >= timedelta(weeks=amount)
    return False


def is_signal_due(
    database: Database,
    signal_id: int,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> bool:
    if force:
        return True
    schedule = get_signal_schedule(database, signal_id)
    return _is_schedule_due(
        schedule,
        last_run=get_dashboard_setting(database, f"signals.{int(signal_id)}.last_scheduled_run_at", None),
        now=now,
    )


def _active_universe_symbols(database: Database) -> list[str]:
    rows = database.query("SELECT ticker FROM symbols WHERE active=1 ORDER BY ticker")
    return [str(row["ticker"]) for row in rows]


def _profile_scope_symbols(database: Database, *, scope: str, selected_lists: list[str], typed_symbols: str) -> list[str]:
    symbols: set[str] = set()
    if scope == "Stocks universe":
        symbols.update(_active_universe_symbols(database))
    if scope in {"Selected lists", "Lists + typed tickers"} and selected_lists:
        symbols.update(database.symbols_for_list_names(selected_lists))
    if scope in {"Typed tickers", "Lists + typed tickers"}:
        symbols.update(item.strip().upper() for item in typed_symbols.split(",") if item.strip())
    return sorted(symbols)


def _snapshot_scope_symbols(database: Database, *, scope: str, selected_lists: list[str], typed_symbols: str) -> set[str] | None:
    if scope == "Stocks universe":
        return None
    return set(_profile_scope_symbols(database, scope=scope, selected_lists=selected_lists, typed_symbols=typed_symbols))


def _snapshot_dollar_volume(snapshot: object) -> float:
    price = float(getattr(snapshot, "price", 0) or 0)
    volume = float(getattr(snapshot, "day_volume", 0) or 0)
    return price * volume


def _provider(settings: Settings, *, requests_per_minute: int | None = None) -> MassiveClient:
    return MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=requests_per_minute or settings.requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )


def run_market_snapshot_service(database: Database, settings: Settings) -> tuple[str, str, int | None]:
    started = time.monotonic()
    scope = str(get_dashboard_setting(database, "services.snapshot.scope", "Stocks universe"))
    lists = get_dashboard_setting(database, "services.snapshot.lists", [])
    selected_lists = lists if isinstance(lists, list) else []
    typed_symbols = str(get_dashboard_setting(database, "services.snapshot.typed_symbols", ""))
    min_price = float(get_dashboard_setting(database, "services.snapshot.min_price", settings.scan_min_price))
    min_day_volume = float(get_dashboard_setting(database, "services.snapshot.min_day_volume", settings.scan_min_day_volume))
    min_dollar_volume = float(get_dashboard_setting(database, "services.snapshot.min_dollar_volume", 0.0))
    max_store = int(get_dashboard_setting(database, "services.snapshot.max_store", 5_000))
    service_scope = (
        f"{scope}; scheduled=true; min_price={min_price}; min_day_volume={min_day_volume}; "
        f"min_dollar_volume={min_dollar_volume}; max_store={max_store}"
    )
    run_id = database.start_service_run("market_snapshot", scope=service_scope)
    try:
        snapshots = _provider(settings).full_market_snapshot()
        scope_symbols = _snapshot_scope_symbols(database, scope=scope, selected_lists=selected_lists, typed_symbols=typed_symbols)
        filtered = [
            snapshot
            for snapshot in snapshots
            if (scope_symbols is None or snapshot.symbol in scope_symbols)
            and float(snapshot.price or 0) >= min_price
            and float(snapshot.day_volume or 0) >= min_day_volume
            and _snapshot_dollar_volume(snapshot) >= min_dollar_volume
        ]
        filtered.sort(key=_snapshot_dollar_volume, reverse=True)
        selected_snapshots = filtered[:max_store]
        database.ensure_symbols(snapshot.symbol for snapshot in selected_snapshots)
        stored = database.upsert_market_snapshots(selected_snapshots)
        duration = time.monotonic() - started
        database.finish_service_run(
            run_id,
            status="success",
            processed_count=len(snapshots),
            success_count=stored,
            skipped_count=max(len(snapshots) - stored, 0),
            duration_seconds=duration,
            message=f"scheduled=true, matched={len(filtered)}, stored={stored}",
        )
        return "success", f"Market snapshot scheduled run complete: fetched={len(snapshots):,}, matched={len(filtered):,}, stored={stored:,}, duration={duration:.1f}s", run_id
    except Exception as exc:
        database.finish_service_run(run_id, status="failed", error_count=1, duration_seconds=time.monotonic() - started, message=str(exc))
        return "failed", f"Market snapshot scheduled run failed: {exc}", run_id


def _symbols_with_profiles(database: Database) -> set[str]:
    rows = database.query("SELECT ticker FROM company_profiles")
    return {str(row["ticker"]) for row in rows}


def run_company_profiles_service(database: Database, settings: Settings) -> tuple[str, str, int | None]:
    started = time.monotonic()
    scope = str(get_dashboard_setting(database, "services.profiles.scope", "Stocks universe"))
    lists = get_dashboard_setting(database, "services.profiles.lists", [])
    selected_lists = lists if isinstance(lists, list) else []
    typed_symbols = str(get_dashboard_setting(database, "services.profiles.typed_symbols", ""))
    mode = str(get_dashboard_setting(database, "services.profiles.mode", "Only missing profiles"))
    chunk_size = int(get_dashboard_setting(database, "services.profiles.chunk_size", 25))
    requests_per_minute = int(get_dashboard_setting(database, "services.profiles.requests_per_minute", settings.profile_requests_per_minute))
    scope_symbols = _profile_scope_symbols(database, scope=scope, selected_lists=selected_lists, typed_symbols=typed_symbols)
    profiled = _symbols_with_profiles(database)
    pending = [symbol for symbol in scope_symbols if symbol not in profiled] if mode == "Only missing profiles" else scope_symbols
    next_chunk = pending[: max(chunk_size, 1)]
    service_scope = f"{scope}; scheduled=true; mode={mode}; chunk_size={chunk_size}; requests_per_minute={requests_per_minute}"
    run_id = database.start_service_run("company_profiles", scope=service_scope, requested_count=len(next_chunk))
    fetched = 0
    unavailable = 0
    errors: list[str] = []
    try:
        provider = _provider(settings, requests_per_minute=requests_per_minute)
        for symbol in next_chunk:
            try:
                profile = provider.ticker_overview(symbol)
                if profile is None:
                    database.mark_company_profile_unavailable(symbol)
                    unavailable += 1
                    continue
                database.ensure_symbols([profile.ticker])
                database.upsert_company_profile(profile)
                fetched += 1
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
        duration = time.monotonic() - started
        status = "partial" if errors else "success"
        database.finish_service_run(
            run_id,
            status=status,
            processed_count=len(next_chunk),
            success_count=fetched,
            skipped_count=unavailable,
            error_count=len(errors),
            duration_seconds=duration,
            message=f"scheduled=true, fetched={fetched}, unavailable={unavailable}, errors={len(errors)}; " + "; ".join(errors[:5]),
        )
        return status, f"Company profiles scheduled run complete: fetched={fetched}, unavailable={unavailable}, errors={len(errors)}, remaining={max(len(pending)-len(next_chunk), 0):,}, duration={duration:.1f}s", run_id
    except Exception as exc:
        database.finish_service_run(run_id, status="failed", error_count=1, duration_seconds=time.monotonic() - started, message=str(exc))
        return "failed", f"Company profiles scheduled run failed: {exc}", run_id


def _historical_bar_counts(database: Database, symbols: list[str], start: date, end: date) -> dict[str, int]:
    if not symbols:
        return {}
    placeholders = ", ".join("?" for _ in symbols)
    rows = database.query(
        f"""
        SELECT symbol, COUNT(*) AS bar_count
        FROM daily_bars
        WHERE symbol IN ({placeholders})
          AND trading_date >= ?
          AND trading_date <= ?
        GROUP BY symbol
        """,
        tuple(symbols) + (start.isoformat(), end.isoformat()),
    )
    counts = {str(row["symbol"]): int(row["bar_count"] or 0) for row in rows}
    return {symbol: counts.get(symbol, 0) for symbol in symbols}


def run_historical_data_service(database: Database, settings: Settings) -> tuple[str, str, int | None]:
    started = time.monotonic()
    scope = str(get_dashboard_setting(database, "services.historical.scope", "Selected lists"))
    lists = get_dashboard_setting(database, "services.historical.lists", [])
    selected_lists = lists if isinstance(lists, list) else []
    typed_symbols = str(get_dashboard_setting(database, "services.historical.typed_symbols", ""))
    mode = str(get_dashboard_setting(database, "services.historical.mode", "Only incomplete history"))
    years = int(get_dashboard_setting(database, "services.historical.years", 1))
    chunk_size = int(get_dashboard_setting(database, "services.historical.chunk_size", 50))
    chunks_to_run = int(get_dashboard_setting(database, "services.historical.chunks_to_run", 1))
    run_all_remaining = bool(get_dashboard_setting(database, "services.historical.run_all_remaining", False))
    requests_per_minute = int(get_dashboard_setting(database, "services.historical.requests_per_minute", settings.profile_requests_per_minute))
    coverage_threshold_pct = int(get_dashboard_setting(database, "services.historical.coverage_threshold_pct", 90))
    end_date = date.today()
    start_date = end_date - timedelta(days=max(years, 1) * 365)
    expected_bars = max(int(max(years, 1) * 252), 1)
    complete_bars = max(int(expected_bars * coverage_threshold_pct / 100), 1)
    scope_symbols = _profile_scope_symbols(database, scope=scope, selected_lists=selected_lists, typed_symbols=typed_symbols)
    bar_counts = _historical_bar_counts(database, scope_symbols, start_date, end_date)
    complete_symbols = {symbol for symbol, count in bar_counts.items() if count >= complete_bars}
    pending = [symbol for symbol in scope_symbols if symbol not in complete_symbols] if mode == "Only incomplete history" else scope_symbols
    chunks_remaining = (len(pending) + max(chunk_size, 1) - 1) // max(chunk_size, 1)
    run_chunk_count = chunks_remaining if run_all_remaining else min(max(chunks_to_run, 1), chunks_remaining)
    symbols_to_run = pending[: max(chunk_size, 1) * max(run_chunk_count, 0)]
    service_scope = f"{scope}; scheduled=true; mode={mode}; years={years}; chunks={run_chunk_count}; requests_per_minute={requests_per_minute}; range={start_date}..{end_date}"
    run_id = database.start_service_run("historical_data", scope=service_scope, requested_count=len(symbols_to_run))
    symbols_success = 0
    bars_written = 0
    errors: list[str] = []
    try:
        provider = _provider(settings, requests_per_minute=requests_per_minute)
        for symbol in symbols_to_run:
            try:
                database.ensure_symbols([symbol])
                bars_written += database.upsert_bars(provider.historical_daily(symbol, start_date, end_date))
                symbols_success += 1
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
        duration = time.monotonic() - started
        status = "partial" if errors else "success"
        database.finish_service_run(
            run_id,
            status=status,
            processed_count=len(symbols_to_run),
            success_count=symbols_success,
            error_count=len(errors),
            duration_seconds=duration,
            message=f"scheduled=true, bars_written={bars_written}, errors={len(errors)}; " + "; ".join(errors[:5]),
        )
        return status, f"Historical data scheduled run complete: symbols={symbols_success}/{len(symbols_to_run)}, bars_written={bars_written:,}, errors={len(errors)}, remaining={max(len(pending)-len(symbols_to_run), 0):,}, duration={duration:.1f}s", run_id
    except Exception as exc:
        database.finish_service_run(run_id, status="failed", error_count=1, duration_seconds=time.monotonic() - started, message=str(exc))
        return "failed", f"Historical data scheduled run failed: {exc}", run_id


def _send_service_notification(database: Database, settings: Settings, *, service_key: str, status: str, message: str) -> bool:
    schedule = get_service_schedule(database, service_key)
    if not bool(schedule.get("notify_telegram")):
        return False
    text = (
        f"🛠️ <b>Stock Notifier service {status}</b>\n"
        f"Service: {SERVICE_LABELS.get(service_key, service_key)}\n"
        f"{message}"
    )
    request_payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "kind": "scheduled-service",
        "service": service_key,
        "service_label": SERVICE_LABELS.get(service_key, service_key),
        "service_status": status,
    }
    if settings.alert_dry_run:
        database.record_notification_delivery(
            alert_id=None,
            channel_type="telegram",
            status="dry_run",
            request=request_payload,
            response={"dry_run": True, "kind": "scheduled-service"},
        )
        return False
    result = TelegramClient(settings.telegram_bot_token, timeout_seconds=settings.http_timeout_seconds).send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
    )
    database.record_notification_delivery(
        alert_id=None,
        channel_type="telegram",
        status="delivered" if result.ok else "failed",
        request={
            **result.request,
            **{key: value for key, value in request_payload.items() if key not in result.request},
        },
        response=result.response,
        error_text=result.error_text,
    )
    return result.ok


def run_service(database: Database, settings: Settings, service_key: str) -> ScheduledServiceResult:
    if service_key == "snapshot":
        status, message, run_id = run_market_snapshot_service(database, settings)
    elif service_key == "historical":
        status, message, run_id = run_historical_data_service(database, settings)
    elif service_key == "profiles":
        status, message, run_id = run_company_profiles_service(database, settings)
    else:
        raise ValueError(f"Unsupported service key: {service_key}")
    set_dashboard_setting(database, f"services.{service_key}.last_scheduled_run_at", datetime.now(UTC).isoformat())
    _send_service_notification(database, settings, service_key=service_key, status=status, message=message)
    return ScheduledServiceResult(service_key, SERVICE_LABELS.get(service_key, service_key), True, True, status, message, run_id)


def _send_signal_schedule_notification(
    database: Database,
    settings: Settings,
    *,
    signal_id: int,
    signal_name: str,
    status: str,
    message: str,
) -> bool:
    schedule = get_signal_schedule(database, signal_id)
    if not bool(schedule.get("notify_telegram")):
        return False
    text = (
        f"📈 <b>Stock Notifier signal schedule {status}</b>\n"
        f"Signal: {signal_name}\n"
        f"{message}"
    )
    request_payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "kind": "scheduled-signal",
        "signal_id": int(signal_id),
        "signal_name": signal_name,
        "signal_status": status,
    }
    if settings.alert_dry_run:
        database.record_notification_delivery(
            alert_id=None,
            channel_type="telegram",
            status="dry_run",
            request=request_payload,
            response={"dry_run": True, "kind": "scheduled-signal"},
        )
        return False
    result = TelegramClient(settings.telegram_bot_token, timeout_seconds=settings.http_timeout_seconds).send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
    )
    database.record_notification_delivery(
        alert_id=None,
        channel_type="telegram",
        status="delivered" if result.ok else "failed",
        request={
            **result.request,
            **{key: value for key, value in request_payload.items() if key not in result.request},
        },
        response=result.response,
        error_text=result.error_text,
    )
    return result.ok


def _signal_universe_symbols(database: Database, signal_row: dict[str, Any]) -> set[str]:
    config = dict(signal_row.get("config") or {})
    universe = dict(config.get("universe") or {})
    configured_symbols = {
        str(symbol).strip().upper()
        for symbol in universe.get("symbols", [])
        if str(symbol).strip()
    }
    configured_lists = [
        str(name).strip()
        for name in universe.get("lists", [])
        if str(name).strip()
    ]
    configured_universe = configured_symbols | database.symbols_for_list_names(configured_lists)
    if universe.get("mode") == "selected" and configured_universe:
        return configured_universe
    return set(database.active_symbols())


def _fresh_snapshot_count(database: Database, symbols: set[str], *, cutoff: datetime) -> int:
    if not symbols:
        return 0
    placeholders = ", ".join("?" for _ in symbols)
    rows = database.query(
        f"""
        SELECT COUNT(*) AS count
        FROM market_snapshots
        WHERE symbol IN ({placeholders})
          AND fetched_at >= ?
        """,
        tuple(sorted(symbols)) + (cutoff.isoformat(),),
    )
    return int(rows[0]["count"] or 0) if rows else 0


def _refresh_signal_snapshots(database: Database, settings: Settings, signal_row: dict[str, Any]) -> tuple[int, int, str]:
    signal_symbols = _signal_universe_symbols(database, signal_row)
    cutoff = datetime.now(UTC) - timedelta(minutes=SIGNAL_SNAPSHOT_FRESH_MINUTES)
    fresh_count = _fresh_snapshot_count(database, signal_symbols, cutoff=cutoff)
    required_fresh = max(1, int(len(signal_symbols) * 0.95)) if signal_symbols else 0
    if signal_symbols and fresh_count >= required_fresh:
        return 0, fresh_count, f"reused fresh snapshots ({fresh_count:,}/{len(signal_symbols):,})"

    snapshots = _provider(settings).full_market_snapshot()
    selected_snapshots = [snapshot for snapshot in snapshots if snapshot.symbol in signal_symbols]
    database.ensure_symbols(snapshot.symbol for snapshot in selected_snapshots)
    stored = database.upsert_market_snapshots(selected_snapshots)
    database.append_market_snapshot_history(selected_snapshots)
    database.prune_market_snapshot_history(keep_hours=10)
    return len(snapshots), stored, f"fetched fresh snapshots ({stored:,}/{len(signal_symbols):,})"


def _alert_thresholds_for_signal(database: Database, signal_name: str, settings: Settings) -> tuple[float, float]:
    for rule in database.list_alert_rules(enabled_only=False):
        if str(rule["signal_name"]).lower() == signal_name.lower():
            return float(rule["buy_threshold"]), float(rule["sell_threshold"])
    return settings.alert_default_buy_threshold, settings.alert_default_sell_threshold


def _signal_result_type(score: float, *, eligible: bool, buy_threshold: float, sell_threshold: float) -> str:
    if not eligible:
        return "Filtered"
    if score >= buy_threshold:
        return "Buy"
    if score <= sell_threshold:
        return "Sell"
    return "Watch"


def _format_price(value: object) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "-"


def _format_percent(value: object) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _format_volume_millions(value: object) -> str:
    try:
        return f"{float(value) / 1_000_000:.2f}"
    except (TypeError, ValueError):
        return "-"


def _symbol_external_links(symbol: str) -> str:
    clean_symbol = str(symbol).upper().strip()
    encoded_symbol = quote(clean_symbol, safe="")
    tradingview_url = f"https://www.tradingview.com/chart/?symbol={encoded_symbol}"
    yahoo_url = f"https://finance.yahoo.com/quote/{encoded_symbol}"
    return (
        f'{escape(clean_symbol)} '
        f'<a href="{escape(tradingview_url)}">📈</a> '
        f'<a href="{escape(yahoo_url)}">YH</a>'
    )


def _send_signal_digest_notification(
    database: Database,
    settings: Settings,
    *,
    signal_id: int,
    signal_name: str,
    scores: list[Any],
    snapshots_fetched: int,
    snapshots_stored: int,
    snapshot_status: str,
    alert_summary: str,
    dry_run: bool | None = None,
) -> bool:
    buy_threshold, sell_threshold = _alert_thresholds_for_signal(database, signal_name, settings)
    snapshot_rows = database.query(
        """
        SELECT symbol, price, percent_change, day_volume
        FROM market_snapshots
        WHERE symbol IN (
            SELECT symbol FROM signal_scores
            WHERE lower(signal_name)=lower(?) AND is_latest=1
        )
        """,
        (signal_name,),
    )
    snapshot_by_symbol = {str(row["symbol"]): dict(row) for row in snapshot_rows}
    ranked = sorted(scores, key=lambda item: (bool(item.eligible), float(item.score)), reverse=True)
    top = ranked[:10]
    lines = ["Type      Sym     Score   Price     Pc%    Vol(M)"]
    for item in top:
        market = snapshot_by_symbol.get(str(item.symbol), {})
        price = market.get("price", item.close)
        result_type = _signal_result_type(
            float(item.score),
            eligible=bool(item.eligible),
            buy_threshold=buy_threshold,
            sell_threshold=sell_threshold,
        )
        lines.append(
            f"{result_type[:8]:<8}  {str(item.symbol)[:6]:<6}  "
            f"{float(item.score):>5.1f}  {_format_price(price):>8}  "
            f"{_format_percent(market.get('percent_change')):>6}  "
            f"{_format_volume_millions(market.get('day_volume')):>7}"
        )
    if not top:
        lines.append("No scored symbols.")
    link_lines = []
    if settings.dashboard_base_url:
        link_lines.append(f'<a href="{escape(settings.dashboard_base_url)}">Dashboard</a>')
    if top:
        link_lines.append("Links: " + " · ".join(_symbol_external_links(str(item.symbol)) for item in top))

    text = (
        f"📊 <b>Signal results: {escape(signal_name)}</b>\n"
        f"Snapshot: {escape(snapshot_status)}; api_fetched={snapshots_fetched:,}, updated={snapshots_stored:,}\n"
        f"Scored: {len(scores):,} · {escape(alert_summary)}\n"
        f"<pre>{escape(chr(10).join(lines))}</pre>\n"
        f"{chr(10).join(link_lines)}"
    )
    request_payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "kind": "scheduled-signal-digest",
        "signal_id": int(signal_id),
        "signal_name": signal_name,
        "top_count": len(top),
    }
    effective_dry_run = settings.alert_dry_run if dry_run is None else dry_run
    if effective_dry_run:
        database.record_notification_delivery(
            alert_id=None,
            channel_type="telegram",
            status="dry_run",
            request=request_payload,
            response={"dry_run": True, "kind": "scheduled-signal-digest"},
        )
        return False
    result = TelegramClient(settings.telegram_bot_token, timeout_seconds=settings.http_timeout_seconds).send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
    )
    database.record_notification_delivery(
        alert_id=None,
        channel_type="telegram",
        status="delivered" if result.ok else "failed",
        request={
            **result.request,
            **{key: value for key, value in request_payload.items() if key not in result.request},
        },
        response=result.response,
        error_text=result.error_text,
    )
    return result.ok


def run_signal_schedule(database: Database, settings: Settings, signal_id: int) -> ScheduledSignalResult:
    signal_row = database.get_signal_definition(int(signal_id))
    if not signal_row:
        return ScheduledSignalResult(int(signal_id), f"Signal {signal_id}", True, False, "failed", "Signal not found")
    signal_name = str(signal_row["name"])
    try:
        snapshots_fetched, snapshots_stored, snapshot_status = _refresh_signal_snapshots(database, settings, signal_row)
        scores = score_signal(database, signal_row, include_latest_snapshot=True)
        alerts = scan_alerts(database, settings, signal_names={signal_name})
        set_dashboard_setting(database, f"signals.{int(signal_id)}.last_scheduled_run_at", datetime.now(UTC).isoformat())
        alert_summary = (
            f"alerts={alerts.alerts_created}, queued={alerts.queued}, delivered={alerts.delivered}, "
            f"dry_run={alerts.dry_run}"
        )
        digest_sent = False
        if bool(get_signal_schedule(database, int(signal_id)).get("notify_telegram")):
            digest_sent = _send_signal_digest_notification(
                database,
                settings,
                signal_id=int(signal_id),
                signal_name=signal_name,
                scores=scores,
                snapshots_fetched=snapshots_fetched,
                snapshots_stored=snapshots_stored,
                snapshot_status=snapshot_status,
                alert_summary=alert_summary,
            )
        message = (
            f"Signal scheduled run complete: scored={len(scores):,}, "
            f"{snapshot_status}, {alert_summary}, digest_sent={digest_sent}"
        )
        return ScheduledSignalResult(
            int(signal_id),
            signal_name,
            True,
            True,
            "success",
            message,
            symbols_scored=len(scores),
            alerts_created=alerts.alerts_created,
            delivered=alerts.delivered,
        )
    except Exception as exc:
        set_dashboard_setting(database, f"signals.{int(signal_id)}.last_scheduled_run_at", datetime.now(UTC).isoformat())
        message = f"Signal scheduled run failed: {exc}"
        _send_signal_schedule_notification(
            database,
            settings,
            signal_id=int(signal_id),
            signal_name=signal_name,
            status="failed",
            message=message,
        )
        return ScheduledSignalResult(int(signal_id), signal_name, True, True, "failed", message)


def run_signal_test_alert(
    database: Database,
    settings: Settings,
    signal_id: int,
    *,
    dry_run: bool | None = None,
) -> ScheduledSignalResult:
    signal_row = database.get_signal_definition(int(signal_id))
    if not signal_row:
        return ScheduledSignalResult(int(signal_id), f"Signal {signal_id}", True, False, "failed", "Signal not found")
    signal_name = str(signal_row["name"])
    try:
        snapshots_fetched, snapshots_stored, snapshot_status = _refresh_signal_snapshots(database, settings, signal_row)
        scores = score_signal(database, signal_row, include_latest_snapshot=True)
        if not scores:
            return ScheduledSignalResult(int(signal_id), signal_name, True, True, "failed", "No scored symbols available")
        sent = _send_signal_digest_notification(
            database,
            settings,
            signal_id=int(signal_id),
            signal_name=signal_name,
            scores=scores,
            snapshots_fetched=snapshots_fetched,
            snapshots_stored=snapshots_stored,
            snapshot_status=snapshot_status,
            alert_summary="test alert digest",
            dry_run=dry_run,
        )
        message = (
            f"Test alert digest complete: signal={signal_name}, scored={len(scores):,}, "
            f"{snapshot_status}, sent={sent}, "
            f"dry_run={settings.alert_dry_run if dry_run is None else dry_run}"
        )
        return ScheduledSignalResult(
            int(signal_id),
            signal_name,
            True,
            True,
            "success",
            message,
            symbols_scored=len(scores),
            alerts_created=1,
            delivered=1 if sent else 0,
        )
    except Exception as exc:
        return ScheduledSignalResult(int(signal_id), signal_name, True, True, "failed", f"Test alert failed: {exc}")


def run_due_signals(
    database: Database,
    settings: Settings,
    *,
    signal_ids: list[int] | None = None,
    force: bool = False,
) -> list[ScheduledSignalResult]:
    signal_rows = database.list_signal_definitions(enabled_only=True)
    if signal_ids:
        requested = {int(signal_id) for signal_id in signal_ids}
        signal_rows = [row for row in signal_rows if int(row["id"]) in requested]
    results: list[ScheduledSignalResult] = []
    for signal_row in signal_rows:
        signal_id = int(signal_row["id"])
        signal_name = str(signal_row["name"])
        if not is_signal_due(database, signal_id, force=force):
            results.append(
                ScheduledSignalResult(
                    signal_id,
                    signal_name,
                    False,
                    False,
                    "skipped",
                    "Not due or scheduler disabled",
                )
            )
            continue
        results.append(run_signal_schedule(database, settings, signal_id))
    return results


def run_due_services(
    database: Database,
    settings: Settings,
    *,
    service_keys: list[str] | None = None,
    force: bool = False,
) -> list[ScheduledServiceResult]:
    results: list[ScheduledServiceResult] = []
    for service_key in service_keys or SERVICE_KEYS:
        if not is_service_due(database, service_key, force=force):
            results.append(
                ScheduledServiceResult(
                    service_key,
                    SERVICE_LABELS.get(service_key, service_key),
                    False,
                    False,
                    "skipped",
                    "Not due or scheduler disabled",
                )
            )
            continue
        results.append(run_service(database, settings, service_key))
    return results
