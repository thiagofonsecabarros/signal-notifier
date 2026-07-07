from datetime import date

from stock_notifier.db import Database
from stock_notifier.models import DailyBar, Symbol


def test_database_upserts_are_idempotent(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple")])
    original = DailyBar("AAPL", date(2026, 7, 2), 200, 205, 199, 203, 1_000)
    revised = DailyBar("AAPL", date(2026, 7, 2), 200, 206, 199, 204, 1_100)

    assert database.upsert_bars([original]) == 1
    assert database.upsert_bars([revised]) == 1

    rows = database.query("SELECT close, volume FROM daily_bars")
    assert len(rows) == 1
    assert rows[0]["close"] == 204
    assert rows[0]["volume"] == 1_100


def test_fetch_log_lifecycle(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    log_id = database.start_fetch_log("test", "2026-07-02", 1)
    database.finish_fetch_log(log_id, status="success", bars_written=1)

    row = database.query("SELECT status, bars_written FROM fetch_log WHERE id=?", (log_id,))[0]
    assert row["status"] == "success"
    assert row["bars_written"] == 1

