from __future__ import annotations

import pandas as pd

from app.main import create_app
from app.market_data import HistoryResult
from app.watchlists import WatchlistResult, WatchlistSymbol


class FakeMarketDataClient:
    def get_history(self, ticker: str, **_: object) -> HistoryResult:
        dates = pd.date_range("2025-01-01", periods=260)
        frame = pd.DataFrame(
            {
                "Open": [100 + index * 0.1 for index in range(260)],
                "High": [101 + index * 0.1 for index in range(260)],
                "Low": [99 + index * 0.1 for index in range(260)],
                "Close": [100.5 + index * 0.1 for index in range(260)],
                "Adj Close": [100.5 + index * 0.1 for index in range(260)],
                "Volume": [1_000_000 + index for index in range(260)],
            },
            index=dates,
        )
        return HistoryResult(ticker=ticker, data=frame, provider="fake", note="fake test provider")

    def get_profile(self, ticker: str) -> dict[str, object]:
        return {"longName": f"{ticker} Inc", "sector": "Testing", "marketCap": 123_000_000}


class FakeWatchlistClient:
    def get_watchlist(self, url: str) -> WatchlistResult:
        return WatchlistResult(
            id=334089913,
            name="Test Watchlist",
            source_url=url,
            symbols=[
                WatchlistSymbol(raw="NASDAQ:AAPL", exchange="NASDAQ", symbol="AAPL", ticker="AAPL"),
                WatchlistSymbol(raw="NASDAQ:MSFT", exchange="NASDAQ", symbol="MSFT", ticker="MSFT"),
            ],
        )


def test_health_endpoint() -> None:
    app = create_app()
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_chart_endpoint_returns_image_payload() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/charts/auction", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["provider"] == "fake"
    assert payload["images"][0]["mime"] == "image/png"
    assert len(payload["images"][0]["data"]) > 100


def test_analysis_endpoint_returns_summary() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.get("/api/analysis/AAPL")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["name"] == "AAPL Inc"
    assert payload["provider"] == "fake"


def test_watchlist_resolve_endpoint_returns_limited_tickers() -> None:
    app = create_app()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    client = app.test_client()

    response = client.post(
        "/api/watchlists/resolve",
        json={
            "watchlist_url": "https://www.tradingview.com/watchlists/334089913/",
            "max_results": 1,
        },
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["tickers"] == ["AAPL"]
    assert payload["watchlist"]["name"] == "Test Watchlist"


def test_portfolio_endpoint_can_use_watchlist_url() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    client = app.test_client()

    response = client.post(
        "/api/charts/portfolio",
        json={
            "watchlist_url": "https://www.tradingview.com/watchlists/334089913/",
            "investment_per_stock": 100,
        },
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["meta"]["result_count"] == 2
    assert payload["meta"]["watchlist_name"] == "Test Watchlist"
    assert payload["export"]["tickers"] == ["AAPL", "MSFT"]


def test_analysis_post_endpoint_returns_batch_summaries() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/analysis", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert [summary["ticker"] for summary in payload["summaries"]] == ["AAPL", "MSFT"]
    assert payload["export"]["mode"] == "analysis"
