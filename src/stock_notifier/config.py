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
    http_timeout_seconds: float = 30.0
    log_level: str = "INFO"
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dashboard_base_url: str = ""
    alert_default_buy_threshold: float = 75.0
    alert_default_sell_threshold: float = 40.0
    alert_cooldown_hours: float = 12.0
    alert_dry_run: bool = True

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
            http_timeout_seconds=float(os.getenv("MASSIVE_HTTP_TIMEOUT_SECONDS", "30")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            dashboard_base_url=os.getenv("DASHBOARD_BASE_URL", "").strip().rstrip("/"),
            alert_default_buy_threshold=float(os.getenv("ALERT_DEFAULT_BUY_THRESHOLD", "75")),
            alert_default_sell_threshold=float(os.getenv("ALERT_DEFAULT_SELL_THRESHOLD", "40")),
            alert_cooldown_hours=float(os.getenv("ALERT_COOLDOWN_HOURS", "12")),
            alert_dry_run=os.getenv("ALERT_DRY_RUN", "true").strip().lower()
            in {"1", "true", "yes", "y", "on"},
        )
