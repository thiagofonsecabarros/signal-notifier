from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.notifications.formatter import format_alert_message, format_test_message
from stock_notifier.notifications.telegram import TelegramClient


@dataclass(frozen=True)
class AlertScanResult:
    evaluated: int
    alerts_created: int
    deliveries_attempted: int
    delivered: int
    dry_run: bool


def _parse_datetime(value: Any) -> datetime | None:
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
    previous_score = state.get("last_score") if state else None
    if not _cooldown_elapsed(state, cooldown_hours):
        return False
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


def scan_alerts(
    database: Database,
    settings: Settings,
    *,
    dry_run: bool | None = None,
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
    now = datetime.now(UTC).isoformat()

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
            alert_id = database.create_alert(
                alert_rule_id=int(score_row["alert_rule_id"]),
                signal_id=score_row.get("signal_id"),
                signal_name=signal_name,
                symbol=symbol,
                direction=direction,
                score=score,
                threshold=threshold,
                trading_date=score_row.get("trading_date"),
                close=score_row.get("close"),
                message=message,
            )
            alerts_created += 1
            database.upsert_alert_state(
                signal_name=signal_name,
                symbol=symbol,
                direction=direction,
                last_score=score,
                last_alerted_at=now,
                last_alert_id=alert_id,
            )
            alert = {
                **score_row,
                "id": alert_id,
                "direction": direction,
                "threshold": threshold,
                "message": message,
            }
            components = database.score_components_for_score(int(score_row["score_id"]))
            deliveries_attempted += 1
            if _deliver_alert(
                database,
                settings,
                alert_id=alert_id,
                alert=alert,
                components=components,
                dry_run=effective_dry_run,
            ):
                delivered += 1

    return AlertScanResult(
        evaluated=evaluated,
        alerts_created=alerts_created,
        deliveries_attempted=deliveries_attempted,
        delivered=delivered,
        dry_run=effective_dry_run,
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
