from __future__ import annotations

from typing import Any

import pandas as pd
import pytest

from app.market_context import (
    build_market_context,
    collect_market_context,
    summarize_moneyline,
)
from app.market_data import HistoryResult, MarketDataError


def _frame(rows: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [100 + index * 0.5 for index in range(rows)],
            "High": [101 + index * 0.5 for index in range(rows)],
            "Low": [99 + index * 0.5 for index in range(rows)],
            "Close": [100.5 + index * 0.5 for index in range(rows)],
            "Adj Close": [100.5 + index * 0.5 for index in range(rows)],
            "Volume": [1_000 + index for index in range(rows)],
        },
        index=pd.date_range("2026-01-01", periods=rows),
    )


class StubClient:
    def __init__(
        self,
        *,
        rows: int = 120,
        chain: Any | None = None,
        fail: str | None = None,
    ) -> None:
        self.rows = rows
        self.chain = chain
        self.fail = fail
        self.history_calls: list[dict[str, Any]] = []

    def get_history(self, ticker: str, **kwargs: Any) -> HistoryResult:
        self.history_calls.append({"ticker": ticker, **kwargs})
        if self.fail == "history":
            raise MarketDataError(f"{ticker} history unavailable")
        return HistoryResult(
            ticker=ticker,
            data=_frame(self.rows),
            provider="stub",
            note="stub note",
            interval=str(kwargs.get("interval") or "1d"),
        )

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {
            "longName": f"{ticker} Inc",
            "industry": "Software",
            "priceToSalesTrailing12Months": 3.0,
        }

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> Any:  # noqa: ARG002
        if self.chain is None:
            raise MarketDataError(f"No options chain for {ticker}")
        return self.chain


class StubChain:
    def __init__(self) -> None:
        self.current_price = 118.0
        self.expiry = "2026-09-18"
        self.provider = "stub-options"
        self.note = "stub options note"
        self.rows = [
            {
                "strike": 110.0,
                "call_open_interest": 100,
                "put_open_interest": 40,
                "call_last": 9.0,
                "put_last": 1.0,
                "net_open_interest": 60,
                "put_call_ratio": 0.4,
            },
            {
                "strike": 120.0,
                "call_open_interest": 300,
                "put_open_interest": 260,
                "call_last": 4.0,
                "put_last": 5.0,
                "net_open_interest": 40,
                "put_call_ratio": 0.87,
            },
        ]


def test_market_context_carries_auction_torque_and_options() -> None:
    client = StubClient(chain=StubChain())

    context = build_market_context(client, "aapl")

    assert context["ticker"] == "AAPL"
    assert context["provider"] == "stub"
    assert context["auction"]["location"] in {"above value", "inside value", "below value"}
    assert context["auction"]["vah"] >= context["auction"]["val"]
    assert context["auction"]["bars"] == 120
    assert context["torque"]["stage_label"]
    assert context["torque"]["close"] == pytest.approx(160.0)
    assert context["options"]["expiry"] == "2026-09-18"
    assert context["options"]["call_open_interest"] == 400
    assert context["options"]["put_open_interest"] == 300
    assert context["options"]["put_call_ratio"] == pytest.approx(0.75)
    assert context["options"]["peak_open_interest_strike"] == 120.0
    assert context["options"]["nearest_strike"] == 120.0
    # Torque without a SEC pack is technicals only, and the brief is told so.
    assert {gap["source"] for gap in context["unavailable"]} == {"sec_trend"}


def test_market_context_names_each_missing_source_instead_of_failing() -> None:
    short = build_market_context(StubClient(rows=5), "AAPL")

    assert "auction" not in short
    gaps = {gap["source"]: gap["error"] for gap in short["unavailable"]}
    assert "auction" in gaps
    assert "5 bars" in gaps["auction"]
    assert "options" in gaps
    assert short["torque"]["stage_label"]


def test_market_context_stops_at_a_history_failure() -> None:
    context = build_market_context(StubClient(fail="history"), "AAPL")

    assert context == {
        "ticker": "AAPL",
        "unavailable": [{"source": "history", "error": "AAPL history unavailable"}],
    }


def test_market_context_reuses_a_supplied_history() -> None:
    client = StubClient()
    history = client.get_history("AAPL", period="2y")
    client.history_calls.clear()

    context = build_market_context(client, "AAPL", history=history, period="2y")

    assert client.history_calls == []
    assert context["auction"]["period"] == "2y"


def test_collect_market_context_is_bounded_and_isolates_failures() -> None:
    class OneBadTicker(StubClient):
        def get_history(self, ticker: str, **kwargs: Any) -> HistoryResult:
            if ticker == "MSFT":
                raise MarketDataError("MSFT unavailable")
            return super().get_history(ticker, **kwargs)

    contexts = collect_market_context(
        OneBadTicker(),
        ["AAPL", "MSFT", "NVDA", "AMD", "TSLA", "META"],
        max_tickers=3,
    )

    assert [context["ticker"] for context in contexts] == ["AAPL", "MSFT", "NVDA"]
    assert "auction" in contexts[0]
    assert contexts[1]["unavailable"] == [{"source": "history", "error": "MSFT unavailable"}]
    assert "auction" in contexts[2]


def test_summarize_moneyline_reports_no_ratio_without_call_interest() -> None:
    summary = summarize_moneyline(
        {
            "meta": {"expiry": "2026-09-18", "current_price": None},
            "series": {
                "strikes": [{"strike": 100.0, "call_open_interest": 0, "put_open_interest": 25}],
            },
        }
    )

    assert summary["put_call_ratio"] is None
    assert summary["nearest_strike"] is None
    assert summary["peak_open_interest_strike"] == 100.0
    assert summary["strikes_covered"] == 1
