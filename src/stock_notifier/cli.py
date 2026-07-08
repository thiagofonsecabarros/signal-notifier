from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta

from stock_notifier.config import Settings
from stock_notifier.db import Database
from stock_notifier.ingest import backfill_symbols, fetch_grouped_with_lookback
from stock_notifier.providers.massive import MassiveClient
from stock_notifier.scoring.service import score_enabled_signals, score_signal, seed_starter_signals
from stock_notifier.symbols import load_symbols


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stock Signal Notifier operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="Create or upgrade the SQLite schema")
    subparsers.add_parser("sync-symbols", help="Upsert config/symbols.txt into SQLite")

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
    return parser


def _provider(settings: Settings) -> MassiveClient:
    return MassiveClient(
        settings.massive_api_key,
        base_url=settings.massive_base_url,
        requests_per_minute=settings.requests_per_minute,
        timeout_seconds=settings.http_timeout_seconds,
    )


def main() -> None:
    args = _parser().parse_args()
    require_api_key = args.command in {"fetch-daily", "backfill"}
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


if __name__ == "__main__":
    main()
