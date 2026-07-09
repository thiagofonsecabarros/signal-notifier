from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.formatter import format_alert_message, format_test_message
from stock_notifier.notifications.schedule import (
    is_schedule_due,
    parse_datetime as parse_scheduled_datetime,
    parse_schedule,
)
from stock_notifier.notifications.telegram import TelegramClient


@dataclass(frozen=True)
class AlertScanResult:
    evaluated: int
    alerts_created: int
    deliveries_attempted: int
    delivered: int
    dry_run: bool
    queued: int = 0
    dropped: int = 0


def _parse_datetime(value: Any) -> datetime | None:
    return parse_scheduled_datetime(value)


def _cooldown_elapsed(state: dict[str, Any] | None, cooldown_hours: float) -> bool:
    if not state or not state.get("last_alerted_at"):
        return True
    last_alerted_at = _parse_datetime(state.get("last_alerted_at"))
    if last_alerted_at is None:
        return True
    return datetime.now(UTC) - last_alerted_at >= timedelta(hours=max(0.0, cooldown_hours))


def _should_trigger(
    *,
    direction: str,
    score: float,
    threshold: float,
    state: dict[str, Any] | None,
    cooldown_hours: float,
) -> bool:
    _ = cooldown_hours  # retained for compatibility with older alert-rule rows
    previous_score = state.get("last_score") if state else None
    if direction == "BUY":
        crossed = previous_score is None or float(previous_score) < threshold
        return crossed and score >= threshold
    if direction == "SELL":
        if previous_score is None:
            return False
        crossed = float(previous_score) > threshold
        return crossed and score <= threshold
    raise ValueError(f"Unsupported alert direction: {direction}")


def seed_alert_rules(database: Database, settings: Settings) -> int:
    return database.seed_alert_rules(
        buy_threshold=settings.alert_default_buy_threshold,
        sell_threshold=settings.alert_default_sell_threshold,
        cooldown_hours=settings.alert_cooldown_hours,
        frequency_amount=settings.alert_default_frequency_amount,
        frequency_unit=settings.alert_default_frequency_unit,
        start_time=settings.alert_default_start_time,
        timezone=settings.alert_default_timezone,
        market_hours_only=settings.alert_default_market_hours_only,
    )


def _deliver_alert(
    database: Database,
    settings: Settings,
    *,
    alert_id: int,
    alert: dict[str, Any],
    components: list[dict[str, Any]],
    dry_run: bool,
) -> bool:
    text = format_alert_message(alert, components, dashboard_base_url=settings.dashboard_base_url)
    request_payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if dry_run:
        database.record_notification_delivery(
            alert_id=alert_id,
            channel_type="telegram",
            status="dry_run",
            request=request_payload,
            response={"dry_run": True},
        )
        return False

    result = TelegramClient(
        settings.telegram_bot_token,
        timeout_seconds=settings.http_timeout_seconds,
    ).send_message(chat_id=settings.telegram_chat_id, text=text)
    database.record_notification_delivery(
        alert_id=alert_id,
        channel_type="telegram",
        status="delivered" if result.ok else "failed",
        request=result.request,
        response=result.response,
        error_text=result.error_text,
    )
    return result.ok


def _threshold_satisfied(direction: str, score: float, threshold: float) -> bool:
    if direction == "BUY":
        return score >= threshold
    if direction == "SELL":
        return score <= threshold
    raise ValueError(f"Unsupported alert direction: {direction}")


def _create_and_deliver_alert(
    database: Database,
    settings: Settings,
    *,
    score_row: dict[str, Any],
    direction: str,
    threshold: float,
    message: str,
    dry_run: bool,
) -> tuple[int, bool]:
    alert_id = database.create_alert(
        alert_rule_id=int(score_row["alert_rule_id"]),
        signal_id=score_row.get("signal_id"),
        signal_name=str(score_row["signal_name"]),
        symbol=str(score_row["symbol"]),
        direction=direction,
        score=float(score_row["score"]),
        threshold=threshold,
        trading_date=score_row.get("trading_date"),
        close=score_row.get("close"),
        message=message,
    )
    alert = {
        **score_row,
        "id": alert_id,
        "direction": direction,
        "threshold": threshold,
        "message": message,
    }
    components = (
        database.score_components_for_score(int(score_row["score_id"]))
        if score_row.get("score_id") is not None
        else []
    )
    delivered = _deliver_alert(
        database,
        settings,
        alert_id=alert_id,
        alert=alert,
        components=components,
        dry_run=dry_run,
    )
    return alert_id, delivered


def _process_pending_alerts(
    database: Database,
    settings: Settings,
    *,
    now: datetime,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    alerts_created = 0
    deliveries_attempted = 0
    delivered = 0
    dropped = 0

    for pending in database.pending_alerts_for_rules():
        direction = str(pending["direction"])
        latest_score = pending.get("latest_score")
        if latest_score is None or not int(pending.get("latest_eligible") or 0):
            database.update_pending_alert_status(int(pending["id"]), "dropped")
            dropped += 1
            continue

        threshold = float(pending["buy_threshold"] if direction == "BUY" else pending["sell_threshold"])
        score = float(latest_score)
        if not _threshold_satisfied(direction, score, threshold):
            database.update_pending_alert_status(int(pending["id"]), "dropped")
            dropped += 1
            continue

        schedule = parse_schedule(pending)
        state = database.get_alert_state(str(pending["signal_name"]), str(pending["symbol"]), direction)
        if not is_schedule_due(
            now=now,
            schedule=schedule,
            last_alerted_at=state.get("last_alerted_at") if state else None,
        ):
            continue

        score_row = {
            **pending,
            "score_id": pending.get("latest_score_id") or pending.get("score_id"),
            "score": score,
            "trading_date": pending.get("latest_trading_date") or pending.get("trading_date"),
            "close": pending.get("latest_close") or pending.get("close"),
        }
        message = f"{direction} queued threshold confirmed: {score:.2f} vs {threshold:.2f}"
        alert_id, sent = _create_and_deliver_alert(
            database,
            settings,
            score_row=score_row,
            direction=direction,
            threshold=threshold,
            message=message,
            dry_run=dry_run,
        )
        database.update_pending_alert_status(int(pending["id"]), "sent")
        database.upsert_alert_state(
            signal_name=str(pending["signal_name"]),
            symbol=str(pending["symbol"]),
            direction=direction,
            last_score=score,
            last_alerted_at=now.isoformat(),
            last_alert_id=alert_id,
        )
        alerts_created += 1
        deliveries_attempted += 1
        delivered += 1 if sent else 0

    return alerts_created, deliveries_attempted, delivered, dropped


def scan_alerts(
    database: Database,
    settings: Settings,
    *,
    dry_run: bool | None = None,
    now: datetime | None = None,
) -> AlertScanResult:
    effective_dry_run = settings.alert_dry_run if dry_run is None else dry_run
    database.upsert_notification_channel(
        "Telegram",
        channel_type="telegram",
        enabled=bool(settings.telegram_bot_token and settings.telegram_chat_id),
        config={"chat_id_configured": bool(settings.telegram_chat_id)},
    )

    evaluated = 0
    alerts_created = 0
    deliveries_attempted = 0
    delivered = 0
    queued = 0
    dropped = 0
    scan_time = (now or datetime.now(UTC)).astimezone(UTC)
    now_text = scan_time.isoformat()

    pending_counts = _process_pending_alerts(
        database,
        settings,
        now=scan_time,
        dry_run=effective_dry_run,
    )
    alerts_created += pending_counts[0]
    deliveries_attempted += pending_counts[1]
    delivered += pending_counts[2]
    dropped += pending_counts[3]

    for score_row in database.latest_scores_for_alert_rules():
        evaluated += 1
        score = float(score_row["score"])
        signal_name = str(score_row["signal_name"])
        symbol = str(score_row["symbol"])

        for direction, threshold_key in (("BUY", "buy_threshold"), ("SELL", "sell_threshold")):
            threshold = float(score_row[threshold_key])
            state = database.get_alert_state(signal_name, symbol, direction)
            if not int(score_row.get("eligible") or 0):
                database.upsert_alert_state(
                    signal_name=signal_name,
                    symbol=symbol,
                    direction=direction,
                    last_score=score,
                )
                continue
            if not _should_trigger(
                direction=direction,
                score=score,
                threshold=threshold,
                state=state,
                cooldown_hours=float(score_row["cooldown_hours"]),
            ):
                database.upsert_alert_state(
                    signal_name=signal_name,
                    symbol=symbol,
                    direction=direction,
                    last_score=score,
                )
                continue

            message = f"{direction} threshold crossed: {score:.2f} vs {threshold:.2f}"
            schedule = parse_schedule(score_row)
            if not is_schedule_due(
                now=scan_time,
                schedule=schedule,
                last_alerted_at=state.get("last_alerted_at") if state else None,
            ):
                database.upsert_pending_alert(
                    alert_rule_id=int(score_row["alert_rule_id"]),
                    signal_id=score_row.get("signal_id"),
                    signal_name=signal_name,
                    symbol=symbol,
                    direction=direction,
                    score=score,
                    threshold=threshold,
                    trading_date=score_row.get("trading_date"),
                    close=score_row.get("close"),
                    score_id=int(score_row["score_id"]),
                    message=message,
                )
                database.upsert_alert_state(
                    signal_name=signal_name,
                    symbol=symbol,
                    direction=direction,
                    last_score=score,
                )
                queued += 1
                continue

            alert_id, sent = _create_and_deliver_alert(
                database,
                settings,
                score_row=score_row,
                direction=direction,
                threshold=threshold,
                message=message,
                dry_run=effective_dry_run,
            )
            alerts_created += 1
            deliveries_attempted += 1
            delivered += 1 if sent else 0
            database.upsert_alert_state(
                signal_name=signal_name,
                symbol=symbol,
                direction=direction,
                last_score=score,
                last_alerted_at=now_text,
                last_alert_id=alert_id,
            )

    return AlertScanResult(
        evaluated=evaluated,
        alerts_created=alerts_created,
        deliveries_attempted=deliveries_attempted,
        delivered=delivered,
        dry_run=effective_dry_run,
        queued=queued,
        dropped=dropped,
    )


def send_telegram_test(
    database: Database,
    settings: Settings,
    *,
    dry_run: bool | None = None,
) -> bool:
    effective_dry_run = settings.alert_dry_run if dry_run is None else dry_run
    text = format_test_message(dashboard_base_url=settings.dashboard_base_url)
    request_payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if effective_dry_run:
        database.record_notification_delivery(
            alert_id=None,
            channel_type="telegram",
            status="dry_run",
            request=request_payload,
            response={"dry_run": True, "kind": "telegram-test"},
        )
        return False

    result = TelegramClient(
        settings.telegram_bot_token,
        timeout_seconds=settings.http_timeout_seconds,
    ).send_message(chat_id=settings.telegram_chat_id, text=text)
    database.record_notification_delivery(
        alert_id=None,
        channel_type="telegram",
        status="delivered" if result.ok else "failed",
        request=result.request,
        response=result.response,
        error_text=result.error_text,
    )
    return result.ok
