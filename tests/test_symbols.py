import pytest

from stock_notifier.symbols import load_symbols


def test_load_symbols_supports_comments_and_metadata(tmp_path):
    path = tmp_path / "symbols.txt"
    path.write_text("# watchlist\nAAPL,Apple Inc.,stock,NASDAQ\nSPY\n", encoding="utf-8")
    symbols = load_symbols(path)
    assert [symbol.ticker for symbol in symbols] == ["AAPL", "SPY"]
    assert symbols[0].name == "Apple Inc."


def test_load_symbols_rejects_duplicates(tmp_path):
    path = tmp_path / "symbols.txt"
    path.write_text("AAPL\naapl\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        load_symbols(path)

