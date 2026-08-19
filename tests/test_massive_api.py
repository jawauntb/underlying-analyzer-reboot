from __future__ import annotations

from typing import Any

from app.main import create_app
from app.market_data import MarketDataCapabilityError, OptionChainResult


class AdditiveClient:
    provider = type("Provider", (), {"api_key": "configured"})()
    fallback_enabled = False
    provider_label = "massive"
    provider_note = "fixture"

    def get_snapshot(self, ticker: str, *, asset_class: str) -> dict[str, Any]:
        return {"ticker": ticker, "asset_class": asset_class, "snapshot": True}

    def get_aggregates(self, ticker: str, **kwargs: Any) -> dict[str, Any]:
        return {"ticker": ticker, "kwargs": kwargs, "results": [{"c": 10}]}

    def get_trades(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params}

    def get_quotes(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params}

    def get_expirations(self, ticker: str) -> list[str]:
        return ["2026-08-21"] if ticker == "AAPL" else []

    def get_option_chain(self, ticker: str, expiry: str | None) -> OptionChainResult:
        return OptionChainResult(
            ticker,
            expiry or "2026-08-21",
            100.0,
            [{"strike": 100.0, "call_open_interest": 10.0, "put_open_interest": 8.0}],
            ["2026-08-21"],
            "massive",
            "fixture",
        )

    def get_contracts(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params, "results": []}

    def get_events(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params, "results": []}

    def get_dividends(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params, "results": []}

    def get_splits(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params, "results": []}

    def get_financials(
        self, ticker: str, *, statement: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        raise MarketDataCapabilityError(f"financials unavailable: {ticker}/{statement}/{params}")

    def get_market_status(self) -> dict[str, Any]:
        raise MarketDataCapabilityError("market status unavailable")

    def get_news(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ticker": ticker, "params": params, "results": [{"title": "fixture"}]}

    def get_corporate_events(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"params": params, "results": [{"event_type": "earnings"}]}

    def get_ipos(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"params": params, "results": [{"ticker": "NEW"}]}

    def get_conditions(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"params": params, "results": [{"code": "T"}]}

    def get_all_snapshot(self, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"params": params, "tickers": [{"ticker": "AAPL"}]}

    def get_option_snapshot(self, underlying: str, contract: str) -> dict[str, Any]:
        return {"underlying": underlying, "contract": contract, "details": {"strike_price": 100}}


def test_additive_massive_routes_preserve_envelopes_and_query_shapes() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = AdditiveClient()
    client = app.test_client()

    snapshot = client.get("/api/data/market/snapshot", query_string={"ticker": "AAPL"})
    assert snapshot.status_code == 200
    assert snapshot.get_json() == {
        "ticker": "AAPL",
        "provider": "massive",
        "provider_note": "fixture",
        "data": {"ticker": "AAPL", "asset_class": "stocks", "snapshot": True},
    }

    aggregates = client.get(
        "/api/data/market/aggregates/AAPL",
        query_string={"start": "2026-08-01", "end": "2026-08-02", "timespan": "minute"},
    )
    assert aggregates.status_code == 200
    assert aggregates.get_json()["data"]["kwargs"] == {
        "multiplier": 1,
        "timespan": "minute",
        "start": "2026-08-01",
        "end": "2026-08-02",
        "asset_class": "stocks",
    }

    trades = client.get("/api/data/market/trades/AAPL", query_string={"timestamp": "2026-08-01"})
    assert trades.status_code == 200
    assert trades.get_json()["data"]["params"] == {"timestamp": "2026-08-01"}

    chain = client.get("/api/data/options/AAPL/chain", query_string={"expiry": "2026-08-21"})
    assert chain.status_code == 200
    assert chain.get_json()["rows"][0]["strike"] == 100.0

    expirations = client.get("/api/data/options/AAPL/expirations")
    assert expirations.status_code == 200
    assert expirations.get_json()["expirations"] == ["2026-08-21"]

    contracts = client.get("/api/data/options/AAPL/contracts", query_string={"expired": "true"})
    assert contracts.status_code == 200
    assert contracts.get_json()["data"]["params"] == {"expired": "true"}

    events = client.get("/api/data/market/events/AAPL", query_string={"types": "ticker_change"})
    assert events.status_code == 200
    assert events.get_json()["data"]["params"] == {"types": "ticker_change"}

    quotes = client.get("/api/data/market/quotes/AAPL")
    assert quotes.status_code == 200
    assert quotes.get_json()["data"]["params"] == {}

    news = client.get("/api/data/market/news/AAPL", query_string={"limit": "5"})
    assert news.status_code == 200
    assert news.get_json()["data"]["ticker"] == "AAPL"
    assert news.get_json()["data"]["params"] == {"limit": "5"}

    events = client.get(
        "/api/data/market/corporate-events", query_string={"event_type": "earnings"}
    )
    assert events.status_code == 200
    assert events.get_json()["data"]["params"] == {"event_type": "earnings"}

    assert client.get("/api/data/market/ipos").get_json()["data"]["results"] == [{"ticker": "NEW"}]
    assert client.get("/api/data/market/conditions").get_json()["data"]["results"] == [
        {"code": "T"}
    ]
    assert client.get("/api/data/market/snapshot/all").get_json()["data"]["tickers"] == [
        {"ticker": "AAPL"}
    ]

    option_snapshot = client.get("/api/data/options/AAPL/snapshot/O:AAPL260821C00100000")
    assert option_snapshot.status_code == 200
    assert option_snapshot.get_json()["data"]["contract"] == "O:AAPL260821C00100000"

    dividends = client.get("/api/data/market/dividends/AAPL")
    assert dividends.status_code == 200
    assert dividends.get_json()["ticker"] == "AAPL"

    financials = client.get("/api/data/market/financials/AAPL/income")
    assert financials.status_code == 501

    status = client.get("/api/data/market/status")
    assert status.status_code == 501

    assert client.get("/api/capabilities/stocks.quotes").status_code == 200
    assert client.get("/api/capabilities/does.not.exist").status_code == 404


def test_additive_routes_report_unavailable_subscription_without_fallback(monkeypatch: Any) -> None:
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    app = create_app()
    client = app.test_client()

    response = client.get("/api/data/market/snapshot", query_string={"ticker": "AAPL"})

    assert response.status_code == 501
    assert (
        "does not support" in response.get_json()["error"]
        or "not configured" in response.get_json()["error"]
    )


def test_capabilities_report_declared_stock_and_options_entitlements(monkeypatch: Any) -> None:
    monkeypatch.setenv("MASSIVE_API_KEY", "fixture-key")
    monkeypatch.setenv("MASSIVE_STOCKS_PLAN", "advanced")
    monkeypatch.setenv("MASSIVE_OPTIONS_PLAN", "developer")
    monkeypatch.delenv("MASSIVE_FINANCIALS_PLAN", raising=False)
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = AdditiveClient()

    payload = app.test_client().get("/api/capabilities").get_json()
    capabilities = {item["id"]: item for item in payload["capabilities"]}

    assert payload["massive_configured"] is True
    assert capabilities["stocks.quotes"]["available"] is True
    assert capabilities["stocks.quotes"]["freshness"] == "realtime"
    assert capabilities["options.trades"]["available"] is True
    assert capabilities["options.quotes"]["available"] is False
    assert capabilities["stocks.dividends"]["available"] is True
    assert capabilities["stocks.financials"]["available"] is False
    assert capabilities["partners.tmx_corporate_events"]["available"] is False
