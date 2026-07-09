from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.schedule import AlertSchedule, is_market_hours
from stock_notifier.notifications.service import AlertScanResult, scan_alerts
from stock_notifier.providers.base import MarketDataProvider
from stock_notifier.scoring.service import score_enabled_signals


@dataclass(frozen=True)
class ScanCycleResult:
    snapshots_fetched: int
    symbols_filtered: int
    symbols_scored: int
    alerts: AlertScanResult
    duration_seconds: float
    dry_run: bool
    message: str = ""


@contextmanager
def scan_cycle_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd: int | None = None
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        yield
    except FileExistsError as exc:
        raise RuntimeError(f"Scan cycle already running; lock exists at {path}") from exc
    finally:
        if fd is not None:
            os.close(fd)
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def run_scan_cycle(
    database: Database,
    provider: MarketDataProvider,
    settings: Settings,
    *,
    dry_run: bool | None = None,
    max_symbols: int | None = None,
    symbols: Iterable[str] | None = None,
    skip_telegram: bool = False,
    benchmark: bool = False,
) -> ScanCycleResult:
    started = time.monotonic()
    run_id = database.start_scan_cycle_run()
    effective_dry_run = settings.alert_dry_run if dry_run is None else dry_run
    if skip_telegram:
        effective_dry_run = True

    snapshots_fetched = 0
    symbols_filtered = 0
    symbols_scored = 0
    alert_result = AlertScanResult(0, 0, 0, 0, effective_dry_run)
    message = ""

    try:
        if settings.scan_market_hours_only and not is_market_hours(
            datetime.now(UTC),
            AlertSchedule(
                timezone=settings.alert_default_timezone,
                market_hours_only=True,
            ),
        ):
            message = "Skipped: outside configured market hours"
            duration = time.monotonic() - started
            database.finish_scan_cycle_run(
                run_id,
                status="skipped",
                duration_seconds=duration,
                message=message,
            )
            return ScanCycleResult(0, 0, 0, alert_result, duration, effective_dry_run, message)

        snapshots = provider.full_market_snapshot()
        database.ensure_symbols(snapshot.symbol for snapshot in snapshots)
        snapshots_fetched = database.upsert_market_snapshots(snapshots)

        requested_symbols = {symbol.upper().strip() for symbol in symbols or [] if symbol.strip()} or None
        candidate_symbols = database.filtered_snapshot_symbols(
            min_price=settings.scan_min_price,
            min_day_volume=settings.scan_min_day_volume,
            max_symbols=max_symbols or settings.scan_max_symbols,
            symbols=requested_symbols,
        )
        symbols_filtered = len(candidate_symbols)

        score_results = score_enabled_signals(
            database,
            symbols=set(candidate_symbols),
            include_latest_snapshot=True,
        )
        symbols_scored = sum(len(items) for items in score_results.values())
        alert_result = scan_alerts(database, settings, dry_run=effective_dry_run)
        duration = time.monotonic() - started
        if benchmark:
            message = (
                f"bench snapshots={snapshots_fetched} filtered={symbols_filtered} "
                f"scored={symbols_scored} duration={duration:.2f}s"
            )
        if duration > 720:
            message = (message + "; " if message else "") + "WARNING: scan exceeded 12 minutes"
        database.finish_scan_cycle_run(
            run_id,
            status="success",
            snapshots_fetched=snapshots_fetched,
            symbols_filtered=symbols_filtered,
            symbols_scored=symbols_scored,
            alerts_created=alert_result.alerts_created,
            deliveries_attempted=alert_result.deliveries_attempted,
            delivered=alert_result.delivered,
            duration_seconds=duration,
            message=message,
        )
        return ScanCycleResult(
            snapshots_fetched=snapshots_fetched,
            symbols_filtered=symbols_filtered,
            symbols_scored=symbols_scored,
            alerts=alert_result,
            duration_seconds=duration,
            dry_run=effective_dry_run,
            message=message,
        )
    except Exception as exc:
        duration = time.monotonic() - started
        database.finish_scan_cycle_run(
            run_id,
            status="failed",
            snapshots_fetched=snapshots_fetched,
            symbols_filtered=symbols_filtered,
            symbols_scored=symbols_scored,
            alerts_created=alert_result.alerts_created,
            deliveries_attempted=alert_result.deliveries_attempted,
            delivered=alert_result.delivered,
            duration_seconds=duration,
            message=str(exc),
        )
        raise
