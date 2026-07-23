from __future__ import annotations

import csv

from stock_notifier.db import Database
from stock_notifier.models import DailyBar, PriceTarget, Symbol
from stock_notifier.services.price_targets import import_price_targets_csv, parse_price_targets_html
from datetime import date


def test_parse_price_targets_html_extracts_core_fields():
    html = """
    <table>
      <tr>
        <td><a href="/brokerage/example/">Example Brokerage</a></td>
        <td>Raises Target</td>
        <td><a href="/NASDAQ/NVDA/">NVIDIA Co. (NVDA)</a></td>
        <td data-sort-value="145.23">$145.23</td>
        <td>$160.00 ➝ $180.00</td>
        <td>Buy ➝ Strong Buy</td>
      </tr>
    </table>
    """

    rows = parse_price_targets_html(html, captured_at="2026-07-09T20:00:00+00:00")

    assert len(rows) == 1
    assert rows[0].symbol == "NVDA"
    assert rows[0].brokerage == "Example Brokerage"
    assert rows[0].target_price == 180.0
    assert rows[0].price_then == 145.23
    assert rows[0].rating == "Strong Buy"


def test_price_targets_upsert_average_and_reached_status(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("NVDA", "NVIDIA")])
    database.upsert_bars(
        [
            DailyBar("NVDA", date(2026, 7, 8), 140, 150, 139, 145, 1_000),
            DailyBar("NVDA", date(2026, 7, 9), 150, 181, 149, 178, 1_000),
        ]
    )

    latest, events = database.upsert_price_targets(
        [
            PriceTarget(
                symbol="NVDA",
                brokerage="Example Brokerage",
                target_price=180,
                price_then=145,
                effective_date="2026-07-08",
                captured_at="2026-07-08T20:00:00+00:00",
            ),
            PriceTarget(
                symbol="NVDA",
                brokerage="Second Brokerage",
                target_price=200,
                price_then=145,
                effective_date="2026-07-08",
                captured_at="2026-07-08T20:00:00+00:00",
            ),
        ],
        import_source="test",
    )

    assert latest == 2
    assert events == 2
    assert database.price_target_averages()["NVDA"] == 190
    detail = database.price_targets_for_symbol("NVDA")
    first = {row["brokerage"]: row for row in detail}["Example Brokerage"]
    assert first["reached_date"] == "2026-07-09"


def test_import_price_targets_csv_does_not_replace_unknown_symbols(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("NVDA", "NVIDIA")])
    path = tmp_path / "targets.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
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
            ],
        )
        writer.writeheader()
        writer.writerow({"row_kind": "latest", "symbol": "NVDA", "brokerage": "Example", "target_price": "180"})
        writer.writerow({"row_kind": "latest", "symbol": "UNKNOWN", "brokerage": "Example", "target_price": "10"})

    result = import_price_targets_csv(database, path)

    assert result["latest_stored"] == 1
    assert result["skipped_unknown"] == 1
    assert database.price_target_averages()["NVDA"] == 180
