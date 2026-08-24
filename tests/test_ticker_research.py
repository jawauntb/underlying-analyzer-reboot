from __future__ import annotations

import json
from typing import Any

import pandas as pd

from app.main import create_app
from app.market_data import HistoryResult, OptionChainResult
from app.mcp_http import handle_mcp_payload
from app.ticker_research import (
    TickerResearchBusyError,
    build_ticker_research_bundle,
    release_ticker_research_client,
    try_acquire_ticker_research_client,
)
from app.tool_executor import execute_tool


def _history(ticker: str, rows: int, *, start: str = "2023-08-01") -> HistoryResult:
    index = pd.bdate_range(start, periods=rows)
    close = [100.0 + index_value * 0.25 for index_value in range(rows)]
    return HistoryResult(
        ticker=ticker,
        data=pd.DataFrame(
            {
                "Open": close,
                "High": [value + 1 for value in close],
                "Low": [value - 1 for value in close],
                "Close": close,
                "Adj Close": close,
                "Volume": [1_000_000 + index_value for index_value in range(rows)],
            },
            index=index,
        ),
        provider="massive",
        note="Massive test data",
    )


class BundleClient:
    def __init__(self) -> None:
        self.history_calls: list[dict[str, Any]] = []

    def get_history(self, ticker: str, **kwargs: Any) -> HistoryResult:
        self.history_calls.append({"ticker": ticker, **kwargs})
        if kwargs["period"] == "10y":
            return _history(ticker, 2_600, start="2014-08-01")
        return _history(ticker, 270)

    def get_profile(self, ticker: str) -> dict[str, Any]:
        return {"symbol": ticker, "longName": "Apple Inc.", "marketCap": 3_000_000_000_000}

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:
        assert expiry is None
        return OptionChainResult(
            ticker=ticker,
            expiry="2026-09-18",
            current_price=167.25,
            rows=[
                {
                    "strike": 165.0,
                    "call_open_interest": 400,
                    "put_open_interest": 210,
                    "call_last": 3.0,
                    "put_last": 1.5,
                    "net_open_interest": 190,
                    "put_call_ratio": 0.525,
                }
            ],
            expirations=["2026-09-18", "2026-09-25"],
            provider="massive",
            note="Massive options test data",
        )

    def get_snapshot(self, ticker: str) -> dict[str, Any]:
        return {"ticker": ticker, "last_trade": {"price": 167.25}}


def test_ticker_research_bundle_includes_every_chart_source_for_three_windows() -> None:
    client = BundleClient()

    bundle = build_ticker_research_bundle(client, "aapl")

    assert bundle["ticker"] == "AAPL"
    assert set(bundle["intervals"]) == {"1mo", "3mo", "1y"}
    for period, packet in bundle["intervals"].items():
        assert packet["period"] == period
        assert set(packet["charts"]) == {
            "auction",
            "regression",
            "ridge_growth",
            "flow_compass",
            "torque",
            "portfolio",
            "volatility",
        }
        assert packet["charts"]["portfolio"]["meta"]["benchmark_ticker"] == "SPY"
    assert bundle["seasonality"]["chart_type"] == "performance"
    assert bundle["options"]["moneyline"]["chart_type"] == "moneyline"
    assert bundle["source_data"]["profile"]["longName"] == "Apple Inc."
    assert bundle["source_data"]["options_chain"]["expirations"] == ["2026-09-18", "2026-09-25"]
    assert bundle["agent_context"]["intervals"]["1y"]["auction"]["poc"] is not None
    assert (
        bundle["agent_context"]["intervals"]["1y"]["auction"]["level_window"]
        == "21 completed daily sessions"
    )
    assert {(call["ticker"], call["period"]) for call in client.history_calls} == {
        ("AAPL", "10y"),
        ("SPY", "1y"),
    }
    assert bundle["meta"]["source_status"]["history_10y"] == "available"
    one_year_bars = bundle["intervals"]["1y"]["charts"]["auction"]["series"]["ohlcv"]
    one_year_start = pd.Timestamp(one_year_bars[-1]["date"]) - pd.DateOffset(years=1)
    assert pd.Timestamp(one_year_bars[0]["date"]) >= one_year_start
    bar_counts = [bundle["intervals"][period]["bars"] for period in ("1mo", "3mo", "1y")]
    assert bar_counts[0] < bar_counts[1] < bar_counts[2] < 270


def test_ticker_research_bundle_keeps_available_sources_when_options_fail() -> None:
    class NoOptionsClient(BundleClient):
        def get_option_chain(self, ticker: str, _expiry: str | None = None) -> OptionChainResult:
            raise RuntimeError(f"No options for {ticker}")

    bundle = build_ticker_research_bundle(NoOptionsClient(), "AAPL")

    assert "seasonality" in bundle
    assert "1y" in bundle["intervals"]
    assert bundle["options"] is None
    assert any(error["source"] == "options" for error in bundle["meta"]["errors"])


def test_ticker_research_bundle_keeps_price_charts_when_profile_and_snapshot_fail() -> None:
    class PartialClient(BundleClient):
        def get_profile(self, ticker: str) -> dict[str, Any]:
            raise RuntimeError(f"No profile for {ticker}")

        def get_snapshot(self, ticker: str) -> dict[str, Any]:
            raise RuntimeError(f"No snapshot for {ticker}")

    bundle = build_ticker_research_bundle(PartialClient(), "AAPL")

    assert set(bundle["intervals"]) == {"1mo", "3mo", "1y"}
    assert bundle["options"]["moneyline"]["chart_type"] == "moneyline"
    assert bundle["meta"]["source_status"]["profile"] == "unavailable"
    assert bundle["meta"]["source_status"]["snapshot"] == "unavailable"
    assert {error["source"] for error in bundle["meta"]["errors"]} >= {
        "profile",
        "snapshot",
    }


def test_ticker_research_bundle_marks_a_non_overlapping_benchmark() -> None:
    class StaleTickerClient(BundleClient):
        def get_history(self, ticker: str, **kwargs: Any) -> HistoryResult:
            self.history_calls.append({"ticker": ticker, **kwargs})
            if ticker == "SPY":
                return _history(ticker, 270, start="2026-08-01")
            return _history(ticker, 2_600, start="2014-08-01")

    bundle = build_ticker_research_bundle(StaleTickerClient(), "AAPL")

    portfolio = bundle["intervals"]["1y"]["charts"]["portfolio"]
    assert "benchmark_ticker" not in portfolio["meta"]
    assert any(error["source"] == "1y.portfolio_benchmark" for error in bundle["meta"]["errors"])


def test_ticker_research_endpoint_returns_the_data_only_packet() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = BundleClient()
    app.config["SEC_CLIENT"] = None

    response = app.test_client().post("/api/data/ticker-research", json={"ticker": "aapl"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ticker"] == "AAPL"
    assert payload["export"]["mode"] == "ticker-research"
    assert set(payload["intervals"]) == {"1mo", "3mo", "1y"}


def test_ticker_research_endpoint_rejects_non_object_json() -> None:
    app = create_app()

    response = app.test_client().post("/api/data/ticker-research", json=["AAPL"])

    assert response.status_code == 400
    assert response.get_json() == {"error": "Request body must be a JSON object"}


def test_ticker_research_endpoint_returns_retry_after_when_capacity_is_full(
    monkeypatch: Any,
) -> None:
    app = create_app()

    def busy(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise TickerResearchBusyError("Ticker research is busy; try again shortly.")

    monkeypatch.setattr("app.main.build_ticker_research_bundle", busy)
    response = app.test_client().post("/api/data/ticker-research", json={"ticker": "AAPL"})

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.get_json()["error"] == "Ticker research is busy; try again shortly."


def test_ticker_research_endpoint_limits_one_external_client_at_a_time() -> None:
    app = create_app()
    client_key = "203.0.113.7"
    assert try_acquire_ticker_research_client(client_key)
    try:
        response = app.test_client().post(
            "/api/data/ticker-research",
            json={"ticker": "AAPL"},
            headers={"X-Forwarded-For": client_key},
        )
    finally:
        release_ticker_research_client(client_key)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"
    assert response.get_json()["error"] == "Ticker research is already running for this client."


def test_ticker_research_agent_projection_stays_within_mobile_event_budget() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = BundleClient()
    app.config["SEC_CLIENT"] = None

    with app.app_context():
        agent_result = execute_tool("ticker_research_bundle", {"ticker": "AAPL"})
        full_result = execute_tool(
            "ticker_research_bundle",
            {"ticker": "AAPL"},
            result_view="full",
        )

    assert agent_result.ok
    assert "agent_context" in agent_result.result
    assert "intervals" not in agent_result.result
    assert "endpoint" not in agent_result.result["raw_series"]
    assert (
        "repeating this tool returns the same projection"
        in agent_result.result["raw_series"]["note"]
    )
    assert len(json.dumps(agent_result.to_event(), default=str)) < 256 * 1024
    assert "truncated" not in json.loads(agent_result.model_text())
    assert full_result.ok
    assert set(full_result.result["intervals"]) == {"1mo", "3mo", "1y"}


def test_ticker_research_mcp_call_retains_the_full_packet() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = BundleClient()
    app.config["SEC_CLIENT"] = None

    with app.app_context():
        reply = handle_mcp_payload(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ticker_research_bundle", "arguments": {"ticker": "AAPL"}},
            }
        )

    payload = json.loads(reply["result"]["content"][0]["text"])["result"]
    assert set(payload["intervals"]) == {"1mo", "3mo", "1y"}
    assert payload["source_data"]["options_chain"]["ticker"] == "AAPL"
    assert payload["intervals"]["1mo"]["charts"]["auction"]["series"]["ohlcv"]
