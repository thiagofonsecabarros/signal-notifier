from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from pathlib import Path

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.ingest import backfill_symbols, fetch_grouped_with_lookback
from stock_notifier.notifications.service import (
    scan_alerts,
    seed_alert_rules,
    send_sample_alert,
    send_telegram_test,
)
from stock_notifier.pipeline import run_scan_cycle, scan_cycle_lock
from stock_notifier.providers.massive import MassiveClient
from stock_notifier.scoring.service import score_enabled_signals, score_signal, seed_starter_signals
from stock_notifier.services.price_targets import (
    export_investment_analysis_price_targets,
    fetch_and_store_price_targets,
    import_price_targets_csv,
)
from stock_notifier.services.scheduler import SERVICE_KEYS, run_due_services, run_due_signals, run_signal_test_alert
from stock_notifier.symbols import load_symbols


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stock Signal Notifier operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create or upgrade the SQLite schema")
    subparsers.add_parser("sync-symbols", help="Upsert config/symbols.txt into SQLite")

    reference_tickers = subparsers.add_parser(
        "sync-reference-tickers",
        help="Fetch active US stock tickers from Massive reference data into SQLite",
    )
    reference_tickers.add_argument("--include-inactive", action="store_true", help="Include inactive/delisted tickers")
    reference_tickers.add_argument("--limit", type=int, default=1000, help="Massive page size, max 1000")
    reference_tickers.add_argument("--max-pages", type=int, help="Optional safety cap for testing pagination")

    profiles = subparsers.add_parser("sync-profiles", help="Fetch Massive ticker overview metadata")
    profiles.add_argument("--symbols", help="Optional comma-separated subset")
    profiles.add_argument("--limit", type=int, default=100, help="Maximum missing profiles to fetch")
    profiles.add_argument(
        "--requests-per-minute",
        type=int,
        help="Override profile-sync request rate. Defaults to MASSIVE_PROFILE_REQUESTS_PER_MINUTE.",
    )

    price_targets = subparsers.add_parser("fetch-price-targets", help="Fetch latest PriceTargets.com rows")
    price_targets.add_argument("--source-url", default="https://www.pricetargets.com/")
    price_targets.add_argument("--timeout-seconds", type=int, default=45)
    price_targets.add_argument("--allow-unknown-symbols", action="store_true")

    export_targets = subparsers.add_parser(
        "export-price-targets",
        help="Export Investment Analysis price-target rows to a CSV for safe cloud import",
    )
    export_targets.add_argument("--source-db", required=True, help="Investment Analysis SQLite db_filepath")
    export_targets.add_argument("--output", required=True, help="CSV output filepath")

    import_targets = subparsers.add_parser(
        "import-price-targets",
        help="Import price-target CSV rows into the current Signal Notifier DB",
    )
    import_targets.add_argument("--input", required=True, help="CSV input filepath")
    import_targets.add_argument("--allow-unknown-symbols", action="store_true")

    daily = subparsers.add_parser("fetch-daily", help="Fetch the most recent grouped daily bars")
    daily.add_argument("--date", type=date.fromisoformat, default=date.today())
    daily.add_argument("--lookback-days", type=int, default=7)

    backfill = subparsers.add_parser("backfill", help="Fetch per-symbol daily history")
    backfill.add_argument("--days", type=int, default=90)
    backfill.add_argument("--end", type=date.fromisoformat, default=date.today())
    backfill.add_argument("--symbols", help="Optional comma-separated subset")

    subparsers.add_parser("seed-signals", help="Create or update starter signal definitions")
    subparsers.add_parser("list-signals", help="List saved signal definitions")

    score = subparsers.add_parser("score", help="Run one saved signal definition")
    score.add_argument("--signal", required=True, help="Signal id or name")
    score.add_argument("--symbols", help="Optional comma-separated subset")
    score.add_argument("--no-store", action="store_true", help="Preview without saving scores")

    subparsers.add_parser("score-all", help="Run every enabled saved signal definition")

    telegram_test = subparsers.add_parser("telegram-test", help="Send a Telegram test message")
    telegram_test.add_argument("--dry-run", action="store_true", help="Record the test without sending")

    alert_test = subparsers.add_parser("alert-test", help="Send a sample alert using latest saved score/components")
    alert_test.add_argument("--signal", required=True, help="Signal name")
    alert_test.add_argument("--symbol", required=True, help="Ticker symbol")
    alert_test.add_argument("--direction", choices=["BUY", "SELL"], default="BUY")
    alert_test.add_argument("--dry-run", action="store_true", help="Record the sample without sending")
    alert_test.add_argument("--send", action="store_true", help="Force real sending even if ALERT_DRY_RUN=true")

    subparsers.add_parser("alert-rules-seed", help="Create default alert rules for enabled signals")

    alerts_scan = subparsers.add_parser("alerts-scan", help="Scan latest signal scores for alerts")
    alerts_scan.add_argument("--dry-run", action="store_true", help="Record deliveries without sending")
    alerts_scan.add_argument("--send", action="store_true", help="Force real sending even if ALERT_DRY_RUN=true")

    alerts_history = subparsers.add_parser("alerts-history", help="Show recent generated alerts")
    alerts_history.add_argument("--limit", type=int, default=20)

    scan_cycle = subparsers.add_parser("run-scan-cycle", help="Fetch snapshot, score filtered symbols, scan alerts")
    scan_cycle.add_argument("--dry-run", action="store_true", help="Record deliveries without sending")
    scan_cycle.add_argument("--max-symbols", type=int, help="Maximum filtered symbols to score")
    scan_cycle.add_argument("--symbols", help="Optional comma-separated subset")
    scan_cycle.add_argument("--skip-telegram", action="store_true", help="Do not send Telegram messages")
    scan_cycle.add_argument("--benchmark", action="store_true", help="Print timing and volume details")
    scan_cycle.add_argument("--force", action="store_true", help="Run even outside configured market hours")

    services_due = subparsers.add_parser("services-run-due", help="Run enabled scheduled services/signals that are due")
    services_due.add_argument("--service", choices=SERVICE_KEYS, action="append", help="Limit to one service; can be repeated")
    services_due.add_argument("--signal", type=int, action="append", help="Limit to one signal id; can be repeated")
    services_due.add_argument("--force", action="store_true", help="Run selected services even if not due or disabled")
    services_due.add_argument("--test-alert", type=int, help="Score one signal id and send a sample Telegram alert for its top symbol")
    return parser


def _provider(settings: Settings) -> MassiveClient:
    return MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=settings.requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )


def _profile_provider(settings: Settings, requests_per_minute: int | None = None) -> MassiveClient:
    return MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=requests_per_minute or settings.profile_requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )


def main() -> None:
    args = _parser().parse_args()
    require_api_key = args.command in {
        "fetch-daily",
        "backfill",
        "run-scan-cycle",
        "sync-profiles",
        "sync-reference-tickers",
    }
    settings = Settings.from_env(require_api_key=require_api_key)
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    database = Database(settings.db_path)
    database.initialize()

    symbols = load_symbols(settings.symbols_path)
    database.sync_symbols(symbols)
    tickers = {symbol.ticker for symbol in symbols}

    if args.command == "init-db":
        print(f"Initialized {settings.db_path}")
    elif args.command == "sync-symbols":
        print(f"Synchronized {len(symbols)} symbols")
    elif args.command == "sync-reference-tickers":
        reference_symbols = _provider(settings).reference_tickers(
            active=not args.include_inactive,
            limit=args.limit,
            max_pages=args.max_pages,
        )
        count = database.sync_symbols(reference_symbols)
        print(f"Synchronized {count} Massive reference tickers")
    elif args.command == "sync-profiles":
        if args.symbols:
            profile_symbols = [
                item.strip().upper() for item in args.symbols.split(",") if item.strip()
            ]
        else:
            profile_symbols = database.symbols_missing_profiles(limit=args.limit)
        provider = _profile_provider(settings, args.requests_per_minute)
        fetched = 0
        missing = 0
        errors = 0
        for symbol in profile_symbols:
            try:
                profile = provider.ticker_overview(symbol)
                if profile is None:
                    missing += 1
                    database.mark_company_profile_unavailable(symbol)
                    continue
                database.ensure_symbols([profile.ticker])
                database.upsert_company_profile(profile)
                fetched += 1
            except Exception as exc:
                errors += 1
                logging.exception("Profile sync failed for %s", symbol)
                print(f"{symbol}: {exc}")
        print(
            f"Synchronized {fetched} company profiles; "
            f"unavailable={missing}, errors={errors}, total_profiles={database.count_company_profiles()}"
        )
        if errors:
            raise SystemExit(2)
    elif args.command == "fetch-price-targets":
        result = fetch_and_store_price_targets(
            database,
            source_url=args.source_url,
            timeout_seconds=args.timeout_seconds,
            allow_unknown_symbols=args.allow_unknown_symbols,
        )
        print(
            "Price targets complete: "
            f"fetched={result.fetched}, stored_latest={result.stored_latest}, "
            f"events={result.stored_events}, skipped_unknown={result.skipped_unknown}"
        )
    elif args.command == "export-price-targets":
        count = export_investment_analysis_price_targets(Path(args.source_db), Path(args.output))
        print(f"Exported {count} price-target rows to {args.output}")
    elif args.command == "import-price-targets":
        service_run_id = database.start_service_run("price_targets_import", scope=str(args.input))
        try:
            result = import_price_targets_csv(
                database,
                Path(args.input),
                allow_unknown_symbols=args.allow_unknown_symbols,
            )
            database.finish_service_run(
                service_run_id,
                status="success",
                processed_count=int(result["latest_rows"]) + int(result["event_rows"]),
                success_count=int(result["latest_stored"]),
                skipped_count=int(result["skipped_unknown"]),
                duration_seconds=0,
                message=(
                    f"latest_rows={result['latest_rows']}, event_rows={result['event_rows']}, "
                    f"events_inserted={result['events_inserted']}, skipped_unknown={result['skipped_unknown']}"
                ),
            )
            print(
                "Imported price targets: "
                f"latest_rows={result['latest_rows']}, event_rows={result['event_rows']}, "
                f"latest_stored={result['latest_stored']}, events_inserted={result['events_inserted']}, "
                f"skipped_unknown={result['skipped_unknown']}"
            )
        except Exception as exc:
            database.finish_service_run(service_run_id, status="failed", error_count=1, message=str(exc))
            raise
    elif args.command == "fetch-daily":
        actual_date, count = fetch_grouped_with_lookback(
            database,
            _provider(settings),
            tickers,
            args.date,
            lookback_days=args.lookback_days,
        )
        print(f"Stored {count} bars for {actual_date}")
    elif args.command == "backfill":
        if args.symbols:
            requested = {item.strip().upper() for item in args.symbols.split(",") if item.strip()}
            unknown = requested - tickers
            if unknown:
                raise SystemExit(f"Unknown symbols: {', '.join(sorted(unknown))}")
            tickers = requested
        start = args.end - timedelta(days=args.days)
        written, errors = backfill_symbols(database, _provider(settings), tickers, start, args.end)
        print(f"Stored {written} bars; {errors} symbol errors")
        if errors:
            raise SystemExit(2)
    elif args.command == "seed-signals":
        count = seed_starter_signals(database)
        print(f"Seeded {count} starter signals")
    elif args.command == "list-signals":
        definitions = database.list_signal_definitions()
        if not definitions:
            print("No saved signals. Run `stock-notifier seed-signals` first.")
        for definition in definitions:
            status = "enabled" if definition["enabled"] else "disabled"
            print(f"{definition['id']}: {definition['name']} ({status})")
    elif args.command == "score":
        definition = database.get_signal_definition(args.signal)
        if definition is None:
            raise SystemExit(f"Unknown signal: {args.signal}")
        requested_symbols = None
        if args.symbols:
            requested_symbols = {
                item.strip().upper() for item in args.symbols.split(",") if item.strip()
            }
        results = score_signal(database, definition, requested_symbols, store=not args.no_store)
        print(f"Scored {len(results)} symbols for {definition['name']}")
        for item in results[:20]:
            print(f"{item.symbol:8s} {item.score:6.2f} {'OK' if item.eligible else 'FILTERED'}")
    elif args.command == "score-all":
        results = score_enabled_signals(database)
        total = sum(len(items) for items in results.values())
        print(f"Scored {total} symbol/signal rows across {len(results)} signals")
    elif args.command == "telegram-test":
        sent = send_telegram_test(database, settings, dry_run=True if args.dry_run else None)
        if settings.alert_dry_run or args.dry_run:
            print("Recorded Telegram test in dry-run mode")
        else:
            print("Telegram test delivered" if sent else "Telegram test failed; check delivery history")
            if not sent:
                raise SystemExit(2)
    elif args.command == "alert-test":
        dry_run = True if args.dry_run else False if args.send else None
        sent = send_sample_alert(
            database,
            settings,
            signal_name=args.signal,
            symbol=args.symbol,
            direction=args.direction,
            dry_run=dry_run,
        )
        effective_dry_run = settings.alert_dry_run if dry_run is None else dry_run
        if effective_dry_run:
            print("Recorded sample alert in dry-run mode")
        else:
            print("Sample alert delivered" if sent else "Sample alert failed; check delivery history")
            if not sent:
                raise SystemExit(2)
    elif args.command == "alert-rules-seed":
        count = seed_alert_rules(database, settings)
        print(f"Seeded/updated {count} alert rules")
    elif args.command == "alerts-scan":
        dry_run = True if args.dry_run else False if args.send else None
        result = scan_alerts(database, settings, dry_run=dry_run)
        print(
            "Alert scan complete: "
            f"evaluated={result.evaluated}, alerts={result.alerts_created}, "
            f"deliveries={result.deliveries_attempted}, delivered={result.delivered}, "
            f"dry_run={result.dry_run}"
        )
    elif args.command == "alerts-history":
        alerts = database.recent_alerts(limit=args.limit)
        if not alerts:
            print("No alerts yet.")
        for alert in alerts:
            print(
                f"{alert['id']}: {alert['created_at']} {alert['direction']} "
                f"{alert['symbol']} {alert['signal_name']} score={alert['score']:.2f}"
            )
    elif args.command == "services-run-due":
        if args.test_alert is not None:
            result = run_signal_test_alert(database, settings, int(args.test_alert))
            print(f"signal:{result.signal_id}: ran status={result.status} message={result.message}")
            if result.status in {"failed", "partial"}:
                raise SystemExit(2)
            return
        if args.force and not (args.service or args.signal):
            raise SystemExit("--force requires at least one --service or --signal to avoid accidentally running everything")
        results = [] if args.signal and not args.service else run_due_services(
            database,
            settings,
            service_keys=args.service,
            force=args.force,
        )
        signal_results = [] if args.service and not args.signal else run_due_signals(
            database,
            settings,
            signal_ids=args.signal,
            force=args.force,
        )
        for result in results:
            state = "ran" if result.ran else "skipped"
            print(f"{result.service_key}: {state} status={result.status} message={result.message}")
        for result in signal_results:
            state = "ran" if result.ran else "skipped"
            print(f"signal:{result.signal_id}: {state} status={result.status} message={result.message}")
        if any(result.status in {"failed", "partial"} for result in [*results, *signal_results] if result.ran):
            raise SystemExit(2)
    elif args.command == "run-scan-cycle":
        requested_symbols = None
        if args.symbols:
            requested_symbols = {
                item.strip().upper() for item in args.symbols.split(",") if item.strip()
            }
        try:
            with scan_cycle_lock(settings.scan_lock_path):
                result = run_scan_cycle(
                    database,
                    _provider(settings),
                    settings,
                    dry_run=True if args.dry_run else None,
                    max_symbols=args.max_symbols,
                    symbols=requested_symbols,
                    skip_telegram=args.skip_telegram,
                    benchmark=args.benchmark,
                    force=args.force,
                )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        print(
            "Scan cycle complete: "
            f"snapshots={result.snapshots_fetched}, filtered={result.symbols_filtered}, "
            f"scored={result.symbols_scored}, alerts={result.alerts.alerts_created}, "
            f"queued={result.alerts.queued}, deliveries={result.alerts.deliveries_attempted}, "
            f"delivered={result.alerts.delivered}, dry_run={result.dry_run}, "
            f"history_rows={result.snapshot_history_rows}, pruned={result.snapshot_history_pruned}, "
            f"duration={result.duration_seconds:.2f}s"
        )
        if result.message:
            print(result.message)


if __name__ == "__main__":
    main()
