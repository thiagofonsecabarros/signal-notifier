from __future__ import annotations

import logging
import random
import time
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from threading import Lock
from typing import Any

import requests

from stock_notifier.models import DailyBar
from stock_notifier.providers.base import MarketDataNotAvailableError

LOGGER = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, requests_per_minute: int) -> None:
        if requests_per_minute <= 0:
            raise ValueError("requests_per_minute must be positive")
        self.minimum_interval = 60.0 / requests_per_minute
        self._last_request = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            remaining = self.minimum_interval - (now - self._last_request)
            if remaining > 0:
                time.sleep(remaining)
            self._last_request = time.monotonic()


class MassiveClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.massive.com",
        requests_per_minute: int = 5,
        timeout_seconds: float = 30.0,
        max_attempts: int = 5,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.session = session or requests.Session()
        self.rate_limiter = RateLimiter(requests_per_minute)

    def _retry_delay(self, response: requests.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                try:
                    when = parsedate_to_datetime(retry_after)
                    return max((when - datetime.now(UTC)).total_seconds(), 0.0)
                except (TypeError, ValueError):
                    pass
        return min(2**attempt + random.uniform(0, 1), 60.0)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_params = dict(params or {})
        request_params["apiKey"] = self.api_key
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            self.rate_limiter.wait()
            try:
                response = self.session.get(
                    f"{self.base_url}{path}", params=request_params, timeout=self.timeout_seconds
                )
            except requests.RequestException as error:
                last_error = error
                if attempt == self.max_attempts - 1:
                    break
                time.sleep(min(2**attempt + random.uniform(0, 1), 60.0))
                continue

            if response.status_code < 400:
                payload = response.json()
                if not isinstance(payload, dict):
                    raise RuntimeError("Massive returned a non-object JSON response")
                return payload
            if response.status_code == 403:
                # Basic accounts cannot request the current date until Massive has
                # published its end-of-day dataset. This is a date-availability
                # condition, unlike other 403 responses (bad key/plan entitlement).
                try:
                    error_payload = response.json()
                except ValueError:
                    error_payload = {}
                message = str(error_payload.get("message", response.text[:500]))
                if "before end of day" in message.lower():
                    raise MarketDataNotAvailableError(message)
            if response.status_code not in {429, 500, 502, 503, 504}:
                detail = response.text[:500]
                raise RuntimeError(f"Massive API HTTP {response.status_code}: {detail}")
            last_error = RuntimeError(f"Massive API HTTP {response.status_code}")
            if attempt < self.max_attempts - 1:
                delay = self._retry_delay(response, attempt)
                LOGGER.warning("Massive request failed; retrying in %.1fs", delay)
                time.sleep(delay)
        raise RuntimeError(f"Massive request failed after {self.max_attempts} attempts") from last_error

    @staticmethod
    def _bar_from_result(result: dict[str, Any], fallback_symbol: str = "") -> DailyBar:
        timestamp_ms = int(result["t"])
        trading_date = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC).date()
        return DailyBar(
            symbol=str(result.get("T") or fallback_symbol).upper(),
            trading_date=trading_date,
            open=float(result["o"]),
            high=float(result["h"]),
            low=float(result["l"]),
            close=float(result["c"]),
            volume=float(result["v"]),
            vwap=float(result["vw"]) if result.get("vw") is not None else None,
            transactions=int(result["n"]) if result.get("n") is not None else None,
        )

    def grouped_daily(self, trading_date: date, symbols: set[str]) -> list[DailyBar]:
        payload = self._get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{trading_date.isoformat()}",
            {"adjusted": "true"},
        )
        return [
            self._bar_from_result(result)
            for result in payload.get("results", [])
            if str(result.get("T", "")).upper() in symbols
        ]

    def historical_daily(self, symbol: str, start: date, end: date) -> list[DailyBar]:
        payload = self._get(
            f"/v2/aggs/ticker/{symbol}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
        )
        return [self._bar_from_result(result, symbol) for result in payload.get("results", [])]
