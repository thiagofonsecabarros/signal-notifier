from datetime import date

from stock_notifier.db import Database
from stock_notifier.models import CompanyProfile, DailyBar, Symbol


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


def test_service_run_lifecycle(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    run_id = database.start_service_run("market_snapshot", scope="Stocks universe", requested_count=10)
    database.finish_service_run(
        run_id,
        status="success",
        processed_count=10,
        success_count=8,
        skipped_count=2,
        duration_seconds=1.5,
        message="stored=8",
    )

    row = database.recent_service_runs(limit=1)[0]
    assert row["service_name"] == "market_snapshot"
    assert row["scope"] == "Stocks universe"
    assert row["status"] == "success"
    assert row["requested_count"] == 10
    assert row["processed_count"] == 10
    assert row["success_count"] == 8
    assert row["skipped_count"] == 2


def test_app_settings_round_trip(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()

    assert database.get_app_setting("missing", {"default": True}) == {"default": True}
    database.set_app_setting("services.snapshot.defaults", {"min_price": 5.0, "lists": ["Portfolio"]})
    assert database.get_app_setting("services.snapshot.defaults") == {
        "min_price": 5.0,
        "lists": ["Portfolio"],
    }


def test_company_profile_upsert_stores_sic_description(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple")])

    database.upsert_company_profile(
        CompanyProfile(
            ticker="AAPL",
            name="Apple Inc.",
            sic_code="3571",
            sic_description="ELECTRONIC COMPUTERS",
            market_cap=2_700_000_000_000,
        )
    )

    rows = database.query(
        "SELECT name, sic_code, sic_description, market_cap FROM company_profiles WHERE ticker='AAPL'"
    )
    assert rows[0]["name"] == "Apple Inc."
    assert rows[0]["sic_code"] == "3571"
    assert rows[0]["sic_description"] == "ELECTRONIC COMPUTERS"
    assert database.count_company_profiles() == 1


def test_symbol_lists_store_members(tmp_path):
    database = Database(tmp_path / "notifier.db")
    database.initialize()
    database.sync_symbols([Symbol("AAPL", "Apple"), Symbol("MSFT", "Microsoft")])

    list_id = database.create_symbol_list("Portfolio")
    database.add_symbols_to_list(list_id, ["AAPL", "MSFT"])
    database.remove_symbol_from_list(list_id, "MSFT")

    assert database.symbols_in_list(list_id) == ["AAPL"]
    assert database.symbols_for_list_names(["Portfolio"]) == {"AAPL"}
    lists = database.list_symbol_lists()
    assert lists[0]["name"] == "Portfolio"
    assert lists[0]["symbol_count"] == 1

    database.update_symbol_list(list_id, name="Core Portfolio", description="Long-term holdings")
    lists = database.list_symbol_lists()
    assert lists[0]["name"] == "Core Portfolio"
    assert lists[0]["description"] == "Long-term holdings"
    assert database.symbols_for_list_names(["Core Portfolio"]) == {"AAPL"}
