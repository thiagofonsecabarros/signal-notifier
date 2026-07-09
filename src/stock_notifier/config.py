from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    massive_api_key: str
    db_path: Path
    symbols_path: Path
    massive_base_url: str = "https://api.massive.com"
    requests_per_minute: int = 5
    profile_requests_per_minute: int = 120
    http_timeout_seconds: float = 30.0
    log_level: str = "INFO"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dashboard_base_url: str = ""
    alert_default_buy_threshold: float = 75.0
    alert_default_sell_threshold: float = 40.0
    alert_cooldown_hours: float = 12.0
    alert_default_frequency_amount: int = 15
    alert_default_frequency_unit: str = "minutes"
    alert_default_start_time: str = "09:45"
    alert_default_timezone: str = "America/Toronto"
    alert_default_market_hours_only: bool = True
    alert_dry_run: bool = True
    scan_max_symbols: int = 500
    scan_min_price: float = 5.0
    scan_min_day_volume: float = 500_000.0
    scan_market_hours_only: bool = True
    scan_lock_path: Path = Path("./data/scan-cycle.lock")

    @classmethod
    def from_env(cls, *, require_api_key: bool = True) -> Settings:
        load_dotenv()
        api_key = os.getenv("MASSIVE_API_KEY", "").strip()
        if require_api_key and not api_key:
            raise ValueError("MASSIVE_API_KEY is missing; copy .env.example to .env and set it")
        return cls(
            massive_api_key=api_key,
            db_path=Path(os.getenv("DB_PATH", "./data/stock_notifier.db")).expanduser(),
            symbols_path=Path(os.getenv("SYMBOLS_PATH", "./config/symbols.txt")).expanduser(),
            massive_base_url=os.getenv("MASSIVE_BASE_URL", "https://api.massive.com").rstrip("/"),
            requests_per_minute=int(os.getenv("MASSIVE_REQUESTS_PER_MINUTE", "5")),
            profile_requests_per_minute=int(os.getenv("MASSIVE_PROFILE_REQUESTS_PER_MINUTE", "120")),
            http_timeout_seconds=float(os.getenv("MASSIVE_HTTP_TIMEOUT_SECONDS", "30")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            dashboard_base_url=os.getenv("DASHBOARD_BASE_URL", "").strip().rstrip("/"),
            alert_default_buy_threshold=float(os.getenv("ALERT_DEFAULT_BUY_THRESHOLD", "75")),
            alert_default_sell_threshold=float(os.getenv("ALERT_DEFAULT_SELL_THRESHOLD", "40")),
            alert_cooldown_hours=float(os.getenv("ALERT_COOLDOWN_HOURS", "12")),
            alert_default_frequency_amount=int(os.getenv("ALERT_DEFAULT_FREQUENCY_AMOUNT", "15")),
            alert_default_frequency_unit=os.getenv("ALERT_DEFAULT_FREQUENCY_UNIT", "minutes").strip().lower(),
            alert_default_start_time=os.getenv("ALERT_DEFAULT_START_TIME", "09:45").strip(),
            alert_default_timezone=os.getenv("ALERT_DEFAULT_TIMEZONE", "America/Toronto").strip(),
            alert_default_market_hours_only=os.getenv("ALERT_DEFAULT_MARKET_HOURS_ONLY", "true").strip().lower()
            in {"1", "true", "yes", "y", "on"},
            alert_dry_run=os.getenv("ALERT_DRY_RUN", "true").strip().lower()
            in {"1", "true", "yes", "y", "on"},
            scan_max_symbols=int(os.getenv("SCAN_MAX_SYMBOLS", "500")),
            scan_min_price=float(os.getenv("SCAN_MIN_PRICE", "5")),
            scan_min_day_volume=float(os.getenv("SCAN_MIN_DAY_VOLUME", "500000")),
            scan_market_hours_only=os.getenv("SCAN_MARKET_HOURS_ONLY", "true").strip().lower()
            in {"1", "true", "yes", "y", "on"},
            scan_lock_path=Path(os.getenv("SCAN_LOCK_PATH", "./data/scan-cycle.lock")).expanduser(),
        )
