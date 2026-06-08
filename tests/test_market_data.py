from __future__ import annotations

from datetime import date

import pandas as pd

from app.charts import (
    calculate_auction_levels,
    calculate_flow_compass_indicator,
    calculate_ridge_growth_strategy,
)
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


def test_ridge_growth_strategy_enters_persistent_trend() -> None:
    dates = pd.date_range(date(2025, 1, 1), periods=260)
    frame = pd.DataFrame(
        {
            "Open": [100 + index * 0.12 for index in range(260)],
            "High": [101 + index * 0.12 for index in range(260)],
            "Low": [99 + index * 0.12 for index in range(260)],
            "Close": [100.5 + index * 0.12 for index in range(260)],
            "Adj Close": [100.5 + index * 0.12 for index in range(260)],
            "Volume": [1_000_000 + index for index in range(260)],
        },
        index=dates,
    )
    history = HistoryResult("AAPL", frame, "fake", "unit test")

    signals, meta = calculate_ridge_growth_strategy(history)

    assert signals["buy_signal"].sum() >= 1
    assert meta["state"] == "LONG"
    assert meta["recommendation"] == "HOLD LONG"
    assert meta["ending_equity"] > 10_000


def test_flow_compass_indicator_returns_bounded_scores() -> None:
    dates = pd.date_range(date(2025, 1, 1), periods=140)
    frame = pd.DataFrame(
        {
            "Open": [100 + index * 0.08 for index in range(140)],
            "High": [101 + index * 0.08 for index in range(140)],
            "Low": [99 + index * 0.08 for index in range(140)],
            "Close": [100.4 + index * 0.08 for index in range(140)],
            "Adj Close": [100.4 + index * 0.08 for index in range(140)],
            "Volume": [900_000 + index * 100 for index in range(140)],
        },
        index=dates,
    )
    history = HistoryResult("AAPL", frame, "fake", "unit test")

    signals, meta = calculate_flow_compass_indicator(history)

    assert signals["flow_score"].between(-100, 100).all()
    assert meta["state"] in {"STRONG LONG", "LONG OK", "STRONG SHORT", "AVOID CALLS", "NEUTRAL"}
    assert meta["delta_method"] == "daily signed-volume proxy"


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
