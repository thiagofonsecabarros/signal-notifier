from __future__ import annotations

from html import escape
from typing import Any


def _money(value: Any) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _number(value: Any) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "N/A"


def format_alert_message(
    alert: dict[str, Any],
    components: list[dict[str, Any]],
    *,
    dashboard_base_url: str = "",
) -> str:
    direction = str(alert.get("direction") or "").upper()
    icon = "🟢" if direction == "BUY" else "🔴"
    symbol = escape(str(alert.get("symbol") or ""))
    signal_name = escape(str(alert.get("signal_name") or ""))
    trading_date = escape(str(alert.get("trading_date") or "N/A"))
    score = _number(alert.get("score"))
    close = _money(alert.get("close"))

    reasons = []
    for component in components[:5]:
        name = escape(str(component.get("component_name") or "Component"))
        message = escape(str(component.get("message") or ""))
        reasons.append(f"• <b>{name}</b>: {message}")
    why = "\n".join(reasons) if reasons else "• No component breakdown available"

    links = [
        f'<a href="https://www.tradingview.com/chart/?symbol={symbol}">TradingView</a>',
        f'<a href="https://finance.yahoo.com/quote/{symbol}">Yahoo Finance</a>',
    ]
    if dashboard_base_url:
        links.insert(0, f'<a href="{escape(dashboard_base_url)}">Dashboard</a>')

    return (
        f"{icon} <b>{escape(direction)} signal: {symbol}</b>\n"
        f"Signal: {signal_name}\n"
        f"Score: {score}\n"
        f"Close: {close}\n"
        f"Date: {trading_date}\n\n"
        f"<b>Why</b>\n{why}\n\n"
        + " · ".join(links)
    )


def format_test_message(*, dashboard_base_url: str = "") -> str:
    suffix = f'\nDashboard: <a href="{escape(dashboard_base_url)}">{escape(dashboard_base_url)}</a>' if dashboard_base_url else ""
    return "✅ <b>Stock Notifier Telegram test</b>\nNotifications are configured correctly." + suffix
