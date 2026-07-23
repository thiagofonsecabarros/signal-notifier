from __future__ import annotations

import csv
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import requests

from stock_notifier.db import Database
from stock_notifier.models import PriceTarget

SOURCE_URL = "https://www.pricetargets.com/"


def _normalize_text(value: object) -> str:
    return re.sub(r"\s+", " ", unescape(str(value or "")).strip())


def _safe_float(value: Any) -> float | None:
    if value in (None, "", "-", "—", "N/A", "No data"):
        return None
    text = str(value).strip().replace(",", "").replace("$", "").replace("%", "")
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _extract_latest_price(value: str) -> float | None:
    matches = re.findall(r"\$?\s*(-?\d+(?:,\d{3})*(?:\.\d+)?)", str(value or ""))
    if not matches:
        return None
    parsed = _safe_float(matches[-1])
    return parsed if parsed not in (None, 0) else None


def _extract_latest_segment(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    parts = [part.strip() for part in re.split(r"\s*[➝→]\s*", normalized) if part.strip()]
    return parts[-1] if parts else normalized


def _country_is_unsupported(*values: object) -> bool:
    combined = " ".join(str(value or "") for value in values).upper()
    return "/LON/" in combined or "£" in combined


class _PriceTargetsTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[dict[str, Any]]] = []
        self._current_row: list[dict[str, Any]] = []
        self._current_cell: dict[str, Any] | None = None
        self._inside_td = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {str(key): str(value) for key, value in attrs if value is not None}
        if tag == "tr":
            if len(self._current_row) >= 6:
                self.rows.append(self._current_row)
            self._current_row = []
        elif tag == "td":
            self._inside_td = True
            self._current_cell = {"attrs": attr_map, "text_parts": [], "links": []}
        elif self._inside_td and tag == "a" and self._current_cell is not None:
            self._current_cell["links"].append({"href": attr_map.get("href"), "text_parts": []})
        elif self._inside_td and tag == "br" and self._current_cell is not None:
            self._current_cell["text_parts"].append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._inside_td and self._current_cell is not None:
            links = [
                {"href": link.get("href"), "text": _normalize_text("".join(link.get("text_parts") or []))}
                for link in self._current_cell["links"]
            ]
            self._current_row.append(
                {
                    "attrs": self._current_cell["attrs"],
                    "text": _normalize_text("".join(self._current_cell["text_parts"])),
                    "links": links,
                }
            )
            self._current_cell = None
            self._inside_td = False
        elif tag == "tr":
            if len(self._current_row) >= 6:
                self.rows.append(self._current_row)
            self._current_row = []

    def handle_data(self, data: str) -> None:
        if not self._inside_td or self._current_cell is None:
            return
        self._current_cell["text_parts"].append(data)
        if self._current_cell["links"]:
            self._current_cell["links"][-1]["text_parts"].append(data)

    def close(self) -> None:
        super().close()
        if len(self._current_row) >= 6:
            self.rows.append(self._current_row)
            self._current_row = []


@dataclass(frozen=True)
class PriceTargetFetchResult:
    fetched: int
    stored_latest: int
    stored_events: int
    skipped_unknown: int
    targets: list[PriceTarget]


def parse_price_targets_html(html: str, *, captured_at: str | None = None, source_url: str = SOURCE_URL) -> list[PriceTarget]:
    parser = _PriceTargetsTableParser()
    parser.feed(html)
    parser.close()
    captured_at = captured_at or datetime.now(UTC).isoformat()
    targets: list[PriceTarget] = []
    for cells in parser.rows:
        if len(cells) < 6:
            continue
        brokerage_cell, action_cell, company_cell, current_price_cell, target_price_cell, rating_cell = cells[:6]
        company_text = _normalize_text(company_cell.get("text"))
        company_link = next((link for link in company_cell.get("links") or [] if link.get("href")), None)
        href = str((company_link or {}).get("href") or "")
        if _country_is_unsupported(href, current_price_cell.get("text"), target_price_cell.get("text")):
            continue
        symbol = ""
        href_match = re.search(r"/[A-Z]+/([A-Z0-9.\-]+)/?", href)
        if href_match:
            symbol = href_match.group(1).strip().upper()
        if not symbol:
            paren_match = re.search(r"\(([A-Z][A-Z0-9.\-]{0,12})\)", company_text)
            if paren_match:
                symbol = paren_match.group(1).strip().upper()
        company_name = re.sub(r"\s*\([A-Z][A-Z0-9.\-]{0,12}\)\s*$", "", company_text).strip() or company_text
        brokerage = _normalize_text(
            (brokerage_cell.get("links") or [{}])[0].get("text")
            or brokerage_cell.get("attrs", {}).get("data-sort-value")
            or brokerage_cell.get("text")
        )
        action = _normalize_text(action_cell.get("text") or action_cell.get("attrs", {}).get("data-sort-value")).title()
        current_price = _safe_float(current_price_cell.get("attrs", {}).get("data-sort-value") or current_price_cell.get("text"))
        target_text = target_price_cell.get("text") or target_price_cell.get("attrs", {}).get("data-sort-value")
        target_price = _extract_latest_price(str(target_text or ""))
        rating = _extract_latest_segment(str(rating_cell.get("text") or rating_cell.get("attrs", {}).get("data-sort-value") or ""))
        if not symbol or not brokerage:
            continue
        raw_payload = {
            "brokerage": brokerage,
            "action": action,
            "company_name": company_name,
            "symbol": symbol,
            "current_price": current_price,
            "target_price": target_price,
            "rating": rating,
            "company_href": href or None,
            "raw_company_text": company_text,
            "raw_target_price_text": _normalize_text(str(target_text or "")),
        }
        targets.append(
            PriceTarget(
                symbol=symbol,
                brokerage=brokerage,
                company_name=company_name,
                action=action,
                rating=rating,
                target_price=target_price,
                price_then=current_price,
                source_current_price=current_price,
                effective_date=captured_at[:10],
                source_url=source_url,
                captured_at=captured_at,
                raw_payload_json=json.dumps(raw_payload, sort_keys=True),
            )
        )
    return targets


def fetch_price_targets(source_url: str = SOURCE_URL, *, timeout_seconds: int = 45) -> list[PriceTarget]:
    response = requests.get(
        source_url,
        headers={
            "accept": "text/html,application/xhtml+xml",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0 Safari/537.36"
            ),
        },
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    return parse_price_targets_html(response.text, source_url=source_url)


def fetch_and_store_price_targets(
    database: Database,
    *,
    source_url: str = SOURCE_URL,
    timeout_seconds: int = 45,
    allow_unknown_symbols: bool = False,
) -> PriceTargetFetchResult:
    targets = fetch_price_targets(source_url, timeout_seconds=timeout_seconds)
    universe = {str(row["ticker"]).upper() for row in database.query("SELECT ticker FROM symbols WHERE active=1")}
    selected = [target for target in targets if allow_unknown_symbols or target.symbol in universe]
    skipped_unknown = len(targets) - len(selected)
    if allow_unknown_symbols:
        database.ensure_symbols(target.symbol for target in selected)
    stored_latest, stored_events = database.upsert_price_targets(selected, import_source="pricetargets.com")
    return PriceTargetFetchResult(
        fetched=len(targets),
        stored_latest=stored_latest,
        stored_events=stored_events,
        skipped_unknown=skipped_unknown,
        targets=selected,
    )


CSV_FIELDS = [
    "row_kind",
    "symbol",
    "company_name",
    "brokerage",
    "action",
    "rating",
    "previous_target_price",
    "target_price",
    "price_then",
    "source_current_price",
    "effective_date",
    "source_url",
    "captured_at",
    "raw_payload_json",
]


def export_investment_analysis_price_targets(source_db: Path, output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with sqlite3.connect(source_db) as connection, output.open("w", newline="", encoding="utf-8") as handle:
        connection.row_factory = sqlite3.Row
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in connection.execute(
            """
            SELECT provider_name, asset_name, trade_symbol, provider_rating_numeric, target_price,
                   rating_change_type, rating_date, raw_payload_json, updated_at
            FROM trusted_provider_ratings
            WHERE source_key='stocks-price-targets'
              AND trade_symbol IS NOT NULL
            """
        ):
            raw_payload = json.loads(row["raw_payload_json"] or "{}")
            price_then = _safe_float(raw_payload.get("current_price"))
            writer.writerow(
                {
                    "row_kind": "latest",
                    "symbol": str(row["trade_symbol"] or "").upper(),
                    "company_name": row["asset_name"] or "",
                    "brokerage": row["provider_name"] or "",
                    "action": row["rating_change_type"] or "",
                    "rating": row["provider_rating_numeric"] or "",
                    "previous_target_price": "",
                    "target_price": row["target_price"] if row["target_price"] is not None else "",
                    "price_then": price_then if price_then is not None else "",
                    "source_current_price": price_then if price_then is not None else "",
                    "effective_date": row["rating_date"] or "",
                    "source_url": SOURCE_URL,
                    "captured_at": row["updated_at"] or "",
                    "raw_payload_json": row["raw_payload_json"] or "{}",
                }
            )
            count += 1
        for row in connection.execute(
            """
            SELECT symbol, asset_name, provider_name, change_summary, previous_target_price,
                   target_price, previous_rating, rating, previous_action, action,
                   effective_date, raw_payload_json, created_at
            FROM job_result_items
            WHERE job_key='stocks-price-targets'
              AND symbol IS NOT NULL
            """
        ):
            raw_payload = json.loads(row["raw_payload_json"] or "{}")
            price_then = _safe_float(raw_payload.get("current_price"))
            writer.writerow(
                {
                    "row_kind": "event",
                    "symbol": str(row["symbol"] or "").upper(),
                    "company_name": row["asset_name"] or "",
                    "brokerage": row["provider_name"] or "",
                    "action": row["action"] or row["change_summary"] or "",
                    "rating": row["rating"] or row["previous_rating"] or "",
                    "previous_target_price": row["previous_target_price"] if row["previous_target_price"] is not None else "",
                    "target_price": row["target_price"] if row["target_price"] is not None else "",
                    "price_then": price_then if price_then is not None else "",
                    "source_current_price": price_then if price_then is not None else "",
                    "effective_date": row["effective_date"] or "",
                    "source_url": SOURCE_URL,
                    "captured_at": row["created_at"] or "",
                    "raw_payload_json": row["raw_payload_json"] or "{}",
                }
            )
            count += 1
    return count


def _target_from_csv_row(row: dict[str, str]) -> PriceTarget:
    return PriceTarget(
        symbol=str(row.get("symbol") or "").upper().strip(),
        brokerage=str(row.get("brokerage") or "").strip(),
        company_name=str(row.get("company_name") or "").strip(),
        action=str(row.get("action") or "").strip(),
        rating=str(row.get("rating") or "").strip(),
        previous_target_price=_safe_float(row.get("previous_target_price")),
        target_price=_safe_float(row.get("target_price")),
        price_then=_safe_float(row.get("price_then")),
        source_current_price=_safe_float(row.get("source_current_price")),
        effective_date=str(row.get("effective_date") or "").strip()[:10],
        source_url=str(row.get("source_url") or SOURCE_URL).strip(),
        captured_at=str(row.get("captured_at") or "").strip(),
        raw_payload_json=str(row.get("raw_payload_json") or "{}"),
    )


def import_price_targets_csv(
    database: Database,
    input_path: Path,
    *,
    allow_unknown_symbols: bool = False,
) -> dict[str, int]:
    universe = {str(row["ticker"]).upper() for row in database.query("SELECT ticker FROM symbols")}
    latest: list[PriceTarget] = []
    events: list[PriceTarget] = []
    skipped_unknown = 0
    with input_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            target = _target_from_csv_row(row)
            if not target.symbol or not target.brokerage:
                continue
            if not allow_unknown_symbols and target.symbol not in universe:
                skipped_unknown += 1
                continue
            if allow_unknown_symbols:
                database.ensure_symbols([target.symbol])
            if str(row.get("row_kind") or "").lower() == "event":
                events.append(target)
            else:
                latest.append(target)
    latest_stored, latest_events = database.upsert_price_targets(latest, import_source="investment_analysis_csv", update_latest=True)
    _, historical_events = database.upsert_price_targets(events, import_source="investment_analysis_csv", update_latest=False)
    return {
        "latest_rows": len(latest),
        "event_rows": len(events),
        "latest_stored": latest_stored,
        "events_inserted": latest_events + historical_events,
        "skipped_unknown": skipped_unknown,
    }
