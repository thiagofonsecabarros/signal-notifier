from __future__ import annotations

import csv
from pathlib import Path

from stock_notifier.models import Symbol


def load_symbols(path: Path) -> list[Symbol]:
    symbols: list[Symbol] = []
    with path.open(encoding="utf-8") as handle:
        rows = (line for line in handle if line.strip() and not line.lstrip().startswith("#"))
        for row in csv.reader(rows):
            values = [value.strip() for value in row]
            ticker = values[0].upper()
            if not ticker:
                continue
            symbols.append(
                Symbol(
                    ticker=ticker,
                    name=values[1] if len(values) > 1 else "",
                    asset_type=values[2].lower() if len(values) > 2 else "stock",
                    exchange=values[3].upper() if len(values) > 3 else "",
                )
            )
    if not symbols:
        raise ValueError(f"No symbols found in {path}")
    duplicates = {s.ticker for s in symbols if sum(x.ticker == s.ticker for x in symbols) > 1}
    if duplicates:
        raise ValueError(f"Duplicate symbols in {path}: {', '.join(sorted(duplicates))}")
    return symbols

