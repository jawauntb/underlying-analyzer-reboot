from __future__ import annotations

from datetime import date

import pandas as pd

from app.charts import calculate_auction_levels
from app.market_data import (
    HistoryResult,
    MarketDataClient,
    normalize_ohlcv,
    parse_market_number,
    to_nasdaq_symbol,
)


def test_normalize_ohlcv_flattens_yfinance_multiindex() -> None:
    frame = pd.DataFrame(
        [[1, 2, 3, 4, 100]],
        index=pd.to_datetime(["2026-01-02"]),
        columns=pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["AAPL"]]),
    )

    normalized = normalize_ohlcv(frame)

    assert list(normalized.columns) == ["Open", "High", "Low", "Close", "Volume", "Adj Close"]
    assert normalized["Adj Close"].iloc[0] == 4


def test_to_nasdaq_symbol_handles_common_us_ticker() -> None:
    assert to_nasdaq_symbol("AAPL") == "AAPL"


def test_parse_market_number_strips_currency_and_commas() -> None:
    assert parse_market_number("$3,102.50") == 3102.5


def test_calculate_auction_levels_uses_recent_window() -> None:
    index = pd.date_range(date(2026, 1, 1), periods=30)
    data = pd.DataFrame(
        {
            "High": range(30),
            "Low": range(30),
            "Close": range(30),
        },
        index=index,
    )

    vah, val, poc = calculate_auction_levels(data)

    assert vah == 28
    assert val == 8
    assert poc == 18


def test_market_data_client_caches_history_results() -> None:
    dates = pd.date_range(date(2026, 1, 1), periods=5)
    frame = pd.DataFrame(
        {
            "Open": [1, 2, 3, 4, 5],
            "High": [2, 3, 4, 5, 6],
            "Low": [0, 1, 2, 3, 4],
            "Close": [1, 2, 3, 4, 5],
            "Adj Close": [1, 2, 3, 4, 5],
            "Volume": [10, 11, 12, 13, 14],
        },
        index=dates,
    )
    result = HistoryResult("AAPL", frame, "fake", "cached")
    client = MarketDataClient()
    calls = 0

    def fake_yfinance(_ticker: str, **_: object) -> HistoryResult:
        nonlocal calls
        calls += 1
        return result

    client._history_from_yfinance = fake_yfinance  # type: ignore[assignment]

    first = client.get_history("AAPL", period="1y")
    second = client.get_history("AAPL", period="1y")

    assert first is result
    assert second is result
    assert calls == 1
