from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any, cast

import pandas as pd
from pytest import MonkeyPatch

import app.main as main_module
from app.alert_scheduler import AlertDeliveryResult, ScheduledAlertRule, deliver_alert_webhook
from app.anthropic import GeneratedText
from app.charts import RenderedImage
from app.main import create_app
from app.market_data import HistoryResult, MarketDataError
from app.watchlists import WatchlistResult, WatchlistSymbol


class FakeTextGenerator:
    def __init__(self, text: str = "Anthropic generated brief.") -> None:
        self.text = text
        self.calls: list[dict[str, object]] = []

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> GeneratedText:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        return GeneratedText(text=self.text, model="claude-test")


class FakeStreamingTextGenerator(FakeTextGenerator):
    provider = "anthropic"
    model = "claude-stream-test"

    def stream_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,
    ) -> Iterator[str]:
        self.calls.append(
            {
                "system": system,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "temperature": temperature,
            }
        )
        yield "### AAPL Vision\n\n"
        yield "Streamed memo."


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
        return {
            "longName": f"{ticker} Inc",
            "sector": "Testing",
            "industry": "Software Testing",
            "marketCap": 123_000_000,
            "longBusinessSummary": (
                f"{ticker} Inc provides testing platforms for institutional research "
                "teams and developer workflows."
            ),
            "country": "United States",
            "website": "https://example.com",
            "fullTimeEmployees": 1200,
            "trailingPE": 18.2,
            "forwardPE": 15.6,
            "priceToSalesTrailing12Months": 3.2,
            "priceToBook": 4.1,
            "enterpriseValue": 150_000_000,
            "enterpriseToRevenue": 3.4,
            "enterpriseToEbitda": 9.8,
            "totalRevenue": 50_000_000,
            "revenueGrowth": 0.18,
            "grossMargins": 0.72,
            "ebitdaMargins": 0.25,
            "operatingMargins": 0.21,
            "profitMargins": 0.16,
            "returnOnEquity": 0.19,
            "freeCashflow": 8_500_000,
            "totalCash": 12_000_000,
            "totalDebt": 4_000_000,
            "debtToEquity": 22.0,
            "currentRatio": 1.8,
            "recommendationKey": "buy",
            "targetMeanPrice": 180.0,
            "numberOfAnalystOpinions": 12,
            "earningsTimestamp": 1_767_225_600,
            "earningsTimestampStart": 1_767_225_600,
            "earningsTimestampEnd": 1_767_312_000,
            "isEarningsDateEstimate": True,
            "forwardEps": 7.25,
            "trailingEps": 6.85,
            "earningsGrowth": 0.14,
            "heldPercentInsiders": 0.04,
            "heldPercentInstitutions": 0.62,
            "companyOfficers": [
                {"name": "Jane Analyst", "title": "Chief Executive Officer"},
                {"name": "Sam Operator", "title": "Chief Financial Officer"},
            ],
        }


class PartiallyFailingMarketDataClient(FakeMarketDataClient):
    def get_history(self, ticker: str, **kwargs: object) -> HistoryResult:
        if ticker == "MSFT":
            raise ValueError("MSFT unavailable")
        return super().get_history(ticker, **kwargs)


class FailingMarketDataClient(FakeMarketDataClient):
    def get_history(self, ticker: str, **_: object) -> HistoryResult:
        raise ValueError(f"{ticker} unavailable")


class FakeSecClient:
    def get_source_pack(self, ticker: str) -> dict[str, object]:
        return {
            "Status": "available",
            "Provider": "SEC EDGAR",
            "Ticker": ticker,
            "CIK": "0000320193",
            "Company Name": "Apple Inc.",
            "Filings": {
                "10-K": {
                    "form": "10-K",
                    "filing_date": "2025-10-31",
                    "report_date": "2025-09-27",
                    "url": "https://www.sec.gov/Archives/example/aapl-10k.htm",
                },
                "8-K": {
                    "form": "8-K",
                    "filing_date": "2026-01-03",
                    "report_date": "2026-01-03",
                    "url": "https://www.sec.gov/Archives/example/aapl-8k.htm",
                }
            },
            "Filing Sections": {
                "Business": {
                    "Item": "Item 1",
                    "Heading": "Business",
                    "Snippet": "Apple sells products and services through global channels.",
                    "Form": "10-K",
                    "Filing Date": "2025-10-31",
                    "Source URL": "https://www.sec.gov/Archives/example/aapl-10k.htm",
                },
                "Risk Factors": {
                    "Item": "Item 1A",
                    "Heading": "Risk Factors",
                    "Snippet": "The company faces supply chain and market demand risks.",
                    "Form": "10-K",
                    "Filing Date": "2025-10-31",
                    "Source URL": "https://www.sec.gov/Archives/example/aapl-10k.htm",
                },
                "MD&A": {
                    "Item": "Item 7",
                    "Heading": "Management's Discussion And Analysis",
                    "Snippet": "Management discusses revenue, margins, and capital returns.",
                    "Form": "10-K",
                    "Filing Date": "2025-10-31",
                    "Source URL": "https://www.sec.gov/Archives/example/aapl-10k.htm",
                },
            },
            "Earnings Sections": {
                "Earnings Release": {
                    "Item": "Item 2.02",
                    "Heading": "Results Of Operations And Financial Condition",
                    "Snippet": "Apple reported record revenue and stronger services margins.",
                    "Form": "8-K",
                    "Filing Date": "2026-01-03",
                    "Source URL": "https://www.sec.gov/Archives/example/aapl-8k.htm",
                }
            },
            "Company Facts": {
                "Revenue": {
                    "Value": 391_035_000_000,
                    "Unit": "USD",
                    "Form": "10-K",
                    "Filed": "2025-10-31",
                    "Concept": "RevenueFromContractWithCustomerExcludingAssessedTax",
                }
            },
            "Citations": [
                {
                    "Label": "SEC 10-K Item 1 Business",
                    "Type": "filing-section",
                    "Form": "10-K",
                    "Filing Date": "2025-10-31",
                    "URL": "https://www.sec.gov/Archives/example/aapl-10k.htm",
                },
                {
                    "Label": "SEC 8-K Item 2.02 Earnings Release",
                    "Type": "earnings-section",
                    "Form": "8-K",
                    "Filing Date": "2026-01-03",
                    "URL": "https://www.sec.gov/Archives/example/aapl-8k.htm",
                }
            ],
            "Errors": [],
        }


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


class FakeAlertStore:
    def __init__(self, rules: list[ScheduledAlertRule]) -> None:
        self.rules = rules
        self.list_calls: list[dict[str, object]] = []
        self.deliveries: list[dict[str, object]] = []
        self.delivery_updates: list[dict[str, object]] = []
        self.runs: list[dict[str, object]] = []
        self.marked: list[dict[str, object]] = []

    def list_due_rules(self, *, run_date: date, force: bool = False) -> list[ScheduledAlertRule]:
        self.list_calls.append({"run_date": run_date, "force": force})
        if force:
            return self.rules
        return [
            rule
            for rule in self.rules
            if rule.last_run_date is None or rule.last_run_date < run_date
        ]

    def get_rule_for_user(self, *, rule_id: str, user_id: str) -> ScheduledAlertRule | None:
        for rule in self.rules:
            if rule.id == rule_id and rule.user_id == user_id:
                return rule
        return None

    def insert_run(
        self,
        *,
        rule: ScheduledAlertRule,
        run_date: date,
        trigger: str,
        status: str,
        payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> str | None:
        self.runs.append(
            {
                "rule": rule,
                "run_date": run_date,
                "trigger": trigger,
                "status": status,
                "payload": payload or {},
                "error": error,
            }
        )
        return f"run-{len(self.runs)}"

    def insert_delivery(
        self,
        *,
        rule: ScheduledAlertRule,
        alert_run_id: str,
        delivery: AlertDeliveryResult,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.deliveries.append(
            {
                "rule": rule,
                "alert_run_id": alert_run_id,
                "delivery": delivery,
                "payload": payload or {},
            }
        )

    def update_run_delivery_status(
        self,
        *,
        alert_run_id: str,
        delivery: AlertDeliveryResult,
    ) -> None:
        self.delivery_updates.append(
            {
                "alert_run_id": alert_run_id,
                "delivery": delivery,
            }
        )

    def mark_rule_ran(self, *, rule_id: str, run_date: date) -> None:
        self.marked.append({"rule_id": rule_id, "run_date": run_date})


class FakeWebhookResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class FakeWebhookSession:
    def __init__(self, status_code: int = 202) -> None:
        self.status_code = status_code
        self.requests: list[dict[str, object]] = []

    def post(self, url: str, *, json: dict[str, Any], timeout: int) -> FakeWebhookResponse:
        self.requests.append({"url": url, "json": json, "timeout": timeout})
        return FakeWebhookResponse(self.status_code)


def test_load_env_file_sets_missing_key(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-test\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    main_module.load_env_file(env_path)

    assert main_module.os.environ["OPENAI_API_KEY"] == "sk-test"


def test_load_env_file_does_not_override_existing_key(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=sk-file\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-shell")

    main_module.load_env_file(env_path)

    assert main_module.os.environ["OPENAI_API_KEY"] == "sk-shell"


def test_health_endpoint() -> None:
    app = create_app()
    client = app.test_client()

    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_security_search_endpoint_returns_public_lookup_results() -> None:
    class SearchClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []

        def search_securities(self, query: str, *, limit: int) -> list[dict[str, str]]:
            self.calls.append((query, limit))
            return [
                {
                    "symbol": "AAPL",
                    "name": "Apple Inc.",
                    "exchange": "NasdaqGS",
                    "asset_type": "equity",
                }
            ]

    app = create_app()
    search_client = SearchClient()
    app.config["MARKET_DATA_CLIENT"] = search_client

    response = app.test_client().get(
        "/api/data/search", query_string={"q": " Apple ", "limit": "1"}
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "query": "Apple",
        "results": [
            {
                "symbol": "AAPL",
                "name": "Apple Inc.",
                "exchange": "NasdaqGS",
                "asset_type": "equity",
            }
        ],
        "provider": "Yahoo Finance via yfinance",
    }
    assert search_client.calls == [("Apple", 1)]


def test_security_search_endpoint_rejects_invalid_query_and_limit() -> None:
    class SearchClient:
        def search_securities(self, query: str, *, limit: int) -> list[dict[str, str]]:
            raise AssertionError(f"unexpected provider call for {query!r}, {limit}")

    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = SearchClient()
    client = app.test_client()

    for query_string in (
        {},
        {"q": "   "},
        {"q": "x" * 101},
        {"q": "Apple", "limit": ""},
        {"q": "Apple", "limit": "zero"},
        {"q": "Apple", "limit": "0"},
        {"q": "Apple", "limit": "11"},
    ):
        response = client.get("/api/data/search", query_string=query_string)
        assert response.status_code == 400, query_string
        assert response.get_json()["error"]


def test_security_search_endpoint_uses_default_limit_and_maps_provider_error() -> None:
    class SearchClient:
        def __init__(self) -> None:
            self.limit: int | None = None

        def search_securities(self, _query: str, *, limit: int) -> list[dict[str, str]]:
            self.limit = limit
            raise MarketDataError("Security search failed: provider unavailable")

    app = create_app()
    search_client = SearchClient()
    app.config["MARKET_DATA_CLIENT"] = search_client

    response = app.test_client().get("/api/data/search", query_string={"q": "Apple"})

    assert response.status_code == 502
    assert response.get_json() == {"error": "Security search failed: provider unavailable"}
    assert search_client.limit == 8


def test_security_search_endpoint_fails_fast_when_provider_is_busy() -> None:
    class SearchClient:
        def search_securities(self, _query: str, *, limit: int) -> list[dict[str, str]]:
            _ = limit
            raise main_module.MarketDataBusyError("Security search is busy; try again shortly")

    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = SearchClient()

    response = app.test_client().get("/api/data/search", query_string={"q": "Apple"})

    assert response.status_code == 503
    assert response.get_json() == {"error": "Security search is busy; try again shortly"}


def test_config_endpoint_reports_disabled_supabase(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_ANON_KEY", raising=False)
    monkeypatch.delenv("NEXT_PUBLIC_SUPABASE_ANON_KEY", raising=False)

    app = create_app()
    client = app.test_client()

    response = client.get("/api/config")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["supabase"] == {"enabled": False, "url": None, "anon_key": None}


def test_config_endpoint_returns_only_public_supabase_values(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "public-anon")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "secret-service-role")

    app = create_app()
    client = app.test_client()

    response = client.get("/api/config")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["supabase"] == {
        "enabled": True,
        "url": "https://example.supabase.co",
        "anon_key": "public-anon",
    }
    assert "secret-service-role" not in response.text


def test_index_includes_underlying_tool_dock() -> None:
    app = create_app()
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    for route in (b"/vision", b"/pixel", b"/fax", b"/moneyline", b"/docs"):
        assert route in response.data
    # The results-first redesign removed the marketing hero (which held the
    # only /docs#api CTA); docs stay reachable via the tool-dock /docs link
    # asserted above.
    for mode in (
        b'data-mode="ridge-growth"',
        b'data-mode="flow-compass"',
        b'data-mode="alerts"',
    ):
        assert mode in response.data


def test_docs_page_and_api_markdown() -> None:
    app = create_app()
    client = app.test_client()

    page = client.get("/docs")
    assert page.status_code == 200
    assert b"API reference" in page.data
    assert b"/docs/api.md" in page.data
    assert b"/api/docs" in page.data
    assert b'href="#api"' in page.data or b'href="/docs#api"' in page.data

    markdown = client.get("/docs/api.md")
    assert markdown.status_code == 200
    assert b"# API Reference" in markdown.data
    assert b"/api/health" in markdown.data


def test_api_docs_catalog_is_public() -> None:
    app = create_app()
    client = app.test_client()

    response = client.get("/api/docs")
    payload = response.get_json()

    assert response.status_code == 200
    assert payload["ok"] is True
    assert payload["public"] is True
    assert payload["docs"]["html"] == "/docs"
    assert payload["docs"]["catalog"] == "/api/docs"
    paths = {item["path"] for item in payload["endpoints"]}
    assert "/api/tools/vision/v2" in paths
    assert "/api/charts/{chart_type}" in paths
    assert "/api/data/charts/{chart_type}" in paths
    search = next(item for item in payload["endpoints"] if item["path"] == "/api/data/search")
    assert search["method"] == "GET"
    assert search["query"] == {"q": "string (required, max 100)", "limit": "int (default 8, 1-10)"}
    assert any(tool["id"] == "docs" for tool in payload["site_tools"])


def test_legacy_tool_routes_render_status_page() -> None:
    app = create_app()
    client = app.test_client()

    for route in ("/vision", "/pixel", "/fax", "/moneyline"):
        response = client.get(route)

        assert response.status_code == 200
        assert b"/static/tools.js" in response.data
        assert b"/docs" in response.data


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


def test_chart_data_endpoint_returns_series_without_images() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/data/charts/auction", json={"ticker": "AAPL", "period": "1y"})

    payload = response.get_json()
    assert response.status_code == 200
    assert "images" not in payload
    assert payload["provider"] == "fake"
    assert payload["export"]["mode"] == "auction-data"
    assert payload["export"]["image_files"] == []
    dataset = payload["datasets"][0]
    assert dataset["chart_type"] == "auction"
    assert dataset["ticker"] == "AAPL"
    assert "poc" in dataset["levels"]
    assert len(dataset["series"]["ohlcv"]) > 10
    assert len(dataset["series"]["close"]) > 10


def test_regression_data_endpoint_returns_bands() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/data/charts/regression", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    dataset = payload["datasets"][0]
    assert "slope_per_day" in dataset["meta"]
    assert "upper_band" in dataset["series"]
    assert "ema21" in dataset["series"]


def test_ridge_growth_endpoint_returns_strategy_package() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/charts/ridge-growth", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["provider"] == "fake"
    assert len(payload["images"]) == 3
    assert [window["period"] for window in payload["meta"]["windows"]] == ["6mo", "1y", "2y"]
    assert "Ridge + Flow Read" in payload["meta"]["analysis_memo"]
    assert payload["meta"]["recommendation"] in {
        "BUY",
        "SELL",
        "HOLD LONG",
        "BUY SETUP",
        "WATCH",
        "CASH",
    }
    assert payload["meta"]["flow_state"]
    assert payload["meta"]["auction_location"]
    assert payload["export"]["mode"] == "ridge-growth"


def test_flow_compass_endpoint_returns_indicator_dashboard() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/charts/flow-compass", json={"ticker": "AAPL", "period": "6mo"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["images"][0]["filename"] == "aapl-flow-compass-6mo.png"
    assert payload["meta"]["state"] in {
        "STRONG LONG",
        "LONG OK",
        "STRONG SHORT",
        "AVOID CALLS",
        "NEUTRAL",
    }
    assert payload["meta"]["delta_method"] == "daily signed-volume proxy"
    assert payload["export"]["mode"] == "flow-compass"


def test_analysis_endpoint_returns_summary() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["TEXT_GENERATOR"] = FakeTextGenerator()
    client = app.test_client()

    response = client.get("/api/analysis/AAPL")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["name"] == "AAPL Inc"
    assert payload["provider"] == "fake"
    assert payload["Anthropic Brief"] == "Anthropic generated brief."
    assert payload["Text Model"] == "claude-test"


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


def test_watchlist_cockpit_endpoint_returns_ranked_rows_from_watchlist() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    client = app.test_client()

    response = client.post(
        "/api/watchlists/cockpit",
        json={"watchlist_url": "https://www.tradingview.com/watchlists/334089913/"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert [row["rank"] for row in payload["rows"]] == [1, 2]
    assert {row["ticker"] for row in payload["rows"]} == {"AAPL", "MSFT"}
    assert payload["meta"]["result_count"] == 2
    assert payload["meta"]["error_count"] == 0
    assert payload["meta"]["watchlist_name"] == "Test Watchlist"
    assert payload["watchlist"]["name"] == "Test Watchlist"
    assert payload["export"]["mode"] == "watchlist-cockpit"
    first = payload["rows"][0]
    assert first["lane"] in {"Priority", "Watch", "Review", "Risk"}
    assert first["scanner_score"] is not None
    assert first["ridge"]["recommendation"]
    assert first["flow"]["state"]
    assert first["auction"]["location"]


def test_watchlist_cockpit_endpoint_accepts_manual_tickers_and_max_results() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post(
        "/api/watchlists/cockpit",
        json={"tickers": ["AAPL", "MSFT"], "max_results": 1},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert [row["ticker"] for row in payload["rows"]] == ["AAPL"]
    assert payload["meta"]["watchlist_name"] == "Manual tickers"


def test_watchlist_cockpit_endpoint_continues_after_symbol_error() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = PartiallyFailingMarketDataClient()
    client = app.test_client()

    response = client.post("/api/watchlists/cockpit", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert [row["ticker"] for row in payload["rows"]] == ["AAPL"]
    assert payload["meta"]["error_count"] == 1
    assert payload["meta"]["errors"] == [{"ticker": "MSFT", "error": "MSFT unavailable"}]


def test_watchlist_cockpit_endpoint_returns_400_when_all_symbols_fail() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FailingMarketDataClient()
    client = app.test_client()

    response = client.post("/api/watchlists/cockpit", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 400
    assert "AAPL unavailable" in payload["error"]
    assert "MSFT unavailable" in payload["error"]


def test_watchlist_alerts_endpoint_returns_digest_and_limited_alerts() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    client = app.test_client()

    response = client.post(
        "/api/watchlists/alerts",
        json={
            "watchlist_url": "https://www.tradingview.com/watchlists/334089913/",
            "max_alerts": 2,
        },
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert len(payload["alerts"]) <= 2
    assert payload["digest"]["headline"]
    assert payload["meta"]["alert_count"] == len(payload["alerts"])
    assert payload["meta"]["result_count"] == 2
    assert payload["watchlist"]["name"] == "Test Watchlist"
    assert payload["export"]["mode"] == "watchlist-alerts"
    assert payload["export"]["alerts"] == payload["alerts"]
    assert [row["rank"] for row in payload["rows"]] == [1, 2]


def test_watchlist_alerts_endpoint_continues_after_symbol_error() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = PartiallyFailingMarketDataClient()
    client = app.test_client()

    response = client.post("/api/watchlists/alerts", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert [row["ticker"] for row in payload["rows"]] == ["AAPL"]
    assert payload["meta"]["error_count"] == 1
    assert payload["meta"]["errors"] == [{"ticker": "MSFT", "error": "MSFT unavailable"}]
    assert payload["export"]["mode"] == "watchlist-alerts"


def test_alert_scheduler_status_reports_configuration(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    app = create_app()
    app.config["SUPABASE_URL"] = "https://example.supabase.co"
    app.config["SUPABASE_SERVICE_ROLE_KEY"] = "service-secret"
    app.config["ALERT_SCHEDULER_TOKEN"] = "scheduler-secret"
    client = app.test_client()

    response = client.get("/api/alerts/scheduler/status")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload == {
        "configured": True,
        "service_role_configured": True,
        "token_configured": True,
        "schedule": "daily",
    }
    assert "scheduler-secret" not in response.text
    assert "service-secret" not in response.text


def test_alert_scheduler_run_requires_bearer_token(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    app = create_app()
    app.config["ALERT_SCHEDULER_TOKEN"] = "scheduler-secret"
    client = app.test_client()

    response = client.post("/api/alerts/scheduled/run", json={})

    payload = response.get_json()
    assert response.status_code == 401
    assert payload["error"] == "Scheduler authorization is required"


def test_deliver_alert_webhook_posts_digest_payload() -> None:
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Daily delivery",
        source_url=None,
        tickers=["AAPL"],
        period="1y",
        max_results=1,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="webhook",
        delivery_webhook_url="https://hooks.example.test/secret/path",
        delivery_min_severity="any",
        metadata={},
        last_run_date=None,
    )
    session = FakeWebhookSession(status_code=202)

    result = deliver_alert_webhook(
        rule=rule,
        run_date=date(2026, 6, 9),
        alert_run_id="run-1",
        payload={
            "digest": {"headline": "Two alerts"},
            "meta": {"alert_count": 2, "high_alert_count": 1},
            "alerts": [{"ticker": "AAPL"}],
            "rows": [{"ticker": "AAPL"}],
        },
        session=cast(Any, session),
    )

    assert result == AlertDeliveryResult(
        channel="webhook",
        status="success",
        destination="https://hooks.example.test",
        response_status=202,
    )
    assert session.requests[0]["url"] == "https://hooks.example.test/secret/path"
    request_payload = cast(dict[str, Any], session.requests[0]["json"])
    assert request_payload["type"] == "underlying.alert_digest"
    assert request_payload["rule"]["id"] == "rule-1"
    assert request_payload["run"] == {"id": "run-1", "date": "2026-06-09"}
    assert session.requests[0]["timeout"] == 15


def test_deliver_alert_webhook_rejects_local_targets() -> None:
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Local delivery",
        source_url=None,
        tickers=["AAPL"],
        period="1y",
        max_results=1,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="webhook",
        delivery_webhook_url="https://127.0.0.1/hook",
        delivery_min_severity="any",
        metadata={},
        last_run_date=None,
    )
    session = FakeWebhookSession()

    result = deliver_alert_webhook(
        rule=rule,
        run_date=date(2026, 6, 9),
        alert_run_id="run-1",
        payload={"meta": {"alert_count": 1, "high_alert_count": 1}},
        session=cast(Any, session),
    )

    assert result.status == "failed"
    assert result.destination == "https://127.0.0.1"
    assert "private or local" in str(result.error)
    assert session.requests == []


def test_deliver_alert_webhook_test_mode_sends_without_fired_alerts() -> None:
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Test delivery",
        source_url=None,
        tickers=["AAPL"],
        period="1y",
        max_results=1,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="webhook",
        delivery_webhook_url="https://hooks.example.test/underlying",
        delivery_min_severity="high",
        metadata={},
        last_run_date=None,
    )
    session = FakeWebhookSession(status_code=204)

    result = deliver_alert_webhook(
        rule=rule,
        run_date=date(2026, 6, 9),
        alert_run_id="run-1",
        payload={
            "digest": {"headline": "Webhook test"},
            "meta": {"alert_count": 0, "high_alert_count": 0, "test": True},
        },
        session=cast(Any, session),
        require_alerts=False,
    )

    assert result.status == "success"
    assert result.response_status == 204
    assert session.requests[0]["url"] == "https://hooks.example.test/underlying"


def test_alert_webhook_test_requires_supabase_user_token(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    app = create_app()
    app.config["SUPABASE_URL"] = "https://example.supabase.co"
    app.config["SUPABASE_ANON_KEY"] = "public-anon"
    app.config["SUPABASE_SERVICE_ROLE_KEY"] = "service-secret"
    client = app.test_client()

    response = client.post("/api/alerts/webhook/test", json={"rule_id": "rule-1"})

    payload = response.get_json()
    assert response.status_code == 401
    assert payload["error"] == "Supabase authorization is required"


def test_alert_webhook_test_sends_and_records_delivery(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Daily delivery",
        source_url=None,
        tickers=["AAPL"],
        period="1y",
        max_results=2,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="webhook",
        delivery_webhook_url="https://hooks.example.test/underlying",
        delivery_min_severity="any",
        metadata={},
        last_run_date=None,
    )
    store = FakeAlertStore([rule])
    delivery_calls: list[dict[str, object]] = []

    def fake_user_id_from_bearer(header_value: str | None) -> str:
        assert header_value == "Bearer user-token"
        return "user-1"

    def fake_deliver_alert_webhook(**kwargs: object) -> AlertDeliveryResult:
        delivery_calls.append(kwargs)
        return AlertDeliveryResult(
            channel="webhook",
            status="success",
            destination="https://hooks.example.test",
            response_status=202,
        )

    monkeypatch.setattr(main_module, "supabase_user_id_from_bearer", fake_user_id_from_bearer)
    monkeypatch.setattr(main_module, "deliver_alert_webhook", fake_deliver_alert_webhook)
    app = create_app()
    app.config["ALERT_STORE"] = store
    client = app.test_client()

    response = client.post(
        "/api/alerts/webhook/test",
        headers={"Authorization": "Bearer user-token"},
        json={"rule_id": "rule-1", "run_date": "2026-06-09"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["rule_id"] == "rule-1"
    assert payload["run_id"] == "run-1"
    assert payload["delivery"]["status"] == "success"
    assert store.runs[0]["trigger"] == "manual"
    assert store.runs[0]["status"] == "success"
    saved_payload = cast(dict[str, Any], store.runs[0]["payload"])
    assert saved_payload["digest"]["headline"] == "Webhook test: Daily delivery"
    assert saved_payload["meta"]["test"] is True
    assert delivery_calls[0]["require_alerts"] is False
    assert delivery_calls[0]["alert_run_id"] == "run-1"
    assert store.deliveries[0]["alert_run_id"] == "run-1"
    assert store.delivery_updates[0]["alert_run_id"] == "run-1"


def test_alert_scheduler_run_processes_due_rule(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Daily watchlist",
        source_url="https://www.tradingview.com/watchlists/334089913/",
        tickers=[],
        period="1y",
        max_results=2,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="none",
        delivery_webhook_url=None,
        delivery_min_severity="any",
        metadata={},
        last_run_date=None,
    )
    store = FakeAlertStore([rule])
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    app.config["ALERT_STORE"] = store
    app.config["ALERT_SCHEDULER_TOKEN"] = "scheduler-secret"
    client = app.test_client()

    response = client.post(
        "/api/alerts/scheduled/run",
        headers={"Authorization": "Bearer scheduler-secret"},
        json={"run_date": "2026-06-09", "force": True, "limit": 1},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["run_date"] == "2026-06-09"
    assert payload["processed"] == 1
    assert payload["results"][0]["status"] == "success"
    assert store.list_calls == [{"run_date": date(2026, 6, 9), "force": True}]
    assert store.marked == [{"rule_id": "rule-1", "run_date": date(2026, 6, 9)}]
    assert store.runs[0]["trigger"] == "scheduled"
    assert store.runs[0]["status"] == "success"
    saved_payload = cast(dict[str, Any], store.runs[0]["payload"])
    assert saved_payload["meta"]["result_count"] == 2
    assert saved_payload["export"]["mode"] == "watchlist-alerts"
    assert store.deliveries == []
    assert store.delivery_updates == []


def test_alert_scheduler_run_delivers_webhook_rule(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Daily delivery",
        source_url="https://www.tradingview.com/watchlists/334089913/",
        tickers=[],
        period="1y",
        max_results=2,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="webhook",
        delivery_webhook_url="https://hooks.example.test/underlying",
        delivery_min_severity="any",
        metadata={},
        last_run_date=None,
    )
    store = FakeAlertStore([rule])
    delivery_calls: list[dict[str, object]] = []

    def fake_deliver_alert_webhook(**kwargs: object) -> AlertDeliveryResult:
        delivery_calls.append(kwargs)
        return AlertDeliveryResult(
            channel="webhook",
            status="success",
            destination="https://hooks.example.test",
            response_status=204,
        )

    monkeypatch.setattr(main_module, "deliver_alert_webhook", fake_deliver_alert_webhook)
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["WATCHLIST_CLIENT"] = FakeWatchlistClient()
    app.config["ALERT_STORE"] = store
    app.config["ALERT_SCHEDULER_TOKEN"] = "scheduler-secret"
    client = app.test_client()

    response = client.post(
        "/api/alerts/scheduled/run",
        headers={"Authorization": "Bearer scheduler-secret"},
        json={"run_date": "2026-06-09", "force": True, "limit": 1},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["results"][0]["status"] == "success"
    assert payload["results"][0]["delivery_status"] == "success"
    assert payload["results"][0]["delivery_channel"] == "webhook"
    assert delivery_calls[0]["alert_run_id"] == "run-1"
    assert delivery_calls[0]["run_date"] == date(2026, 6, 9)
    assert store.deliveries[0]["alert_run_id"] == "run-1"
    delivery = cast(AlertDeliveryResult, store.deliveries[0]["delivery"])
    assert delivery.status == "success"
    assert store.delivery_updates[0]["alert_run_id"] == "run-1"


def test_alert_scheduler_run_records_failed_rule(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    rule = ScheduledAlertRule(
        id="rule-1",
        user_id="user-1",
        name="Daily failures",
        source_url=None,
        tickers=["AAPL", "MSFT"],
        period="1y",
        max_results=2,
        max_alerts=3,
        volatility_threshold=0.55,
        delivery_channel="none",
        delivery_webhook_url=None,
        delivery_min_severity="any",
        metadata={},
        last_run_date=None,
    )
    store = FakeAlertStore([rule])
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FailingMarketDataClient()
    app.config["ALERT_STORE"] = store
    app.config["ALERT_SCHEDULER_TOKEN"] = "scheduler-secret"
    client = app.test_client()

    response = client.post(
        "/api/alerts/scheduled/run",
        headers={"Authorization": "Bearer scheduler-secret"},
        json={"run_date": "2026-06-09"},
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["processed"] == 1
    assert payload["results"][0]["status"] == "failed"
    assert "AAPL unavailable" in payload["results"][0]["error"]
    assert store.marked == [{"rule_id": "rule-1", "run_date": date(2026, 6, 9)}]
    assert store.runs[0]["status"] == "failed"


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
    assert payload["meta"]["benchmark_ticker"] == "SPY"
    assert payload["meta"]["total_return"] > 0
    assert "alpha_vs_benchmark" in payload["meta"]
    assert payload["meta"]["equity_curve"]
    assert payload["export"]["tickers"] == ["AAPL", "MSFT"]
    assert payload["export"]["meta"]["benchmark_equity_curve"]


def test_analysis_post_endpoint_returns_batch_summaries() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["TEXT_GENERATOR"] = FakeTextGenerator()
    client = app.test_client()

    response = client.post("/api/analysis", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert [summary["ticker"] for summary in payload["summaries"]] == ["AAPL", "MSFT"]
    assert [row["rank"] for row in payload["scanner"]] == [1, 2]
    assert {row["ticker"] for row in payload["scanner"]} == {"AAPL", "MSFT"}
    assert payload["export"]["mode"] == "analysis"
    assert payload["export"]["scanner"] == payload["scanner"]
    assert payload["Anthropic Brief"] == "Anthropic generated brief."
    assert payload["export"]["anthropic_brief"] == "Anthropic generated brief."


def test_stock_fax_tool_returns_migrated_report() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["SEC_CLIENT"] = FakeSecClient()
    app.config["TEXT_GENERATOR"] = FakeTextGenerator("Stock Fax narrative.")
    client = app.test_client()

    response = client.post("/api/tools/fax", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["Ticker"] == "AAPL"
    assert payload["Business Context"]["Business Summary"].startswith("AAPL Inc provides")
    assert payload["Financial Quality"]["Revenue Growth"] == 0.18
    assert payload["Management Snapshot"]["Executive Officers"][0]["Name"] == "Jane Analyst"
    assert payload["SEC Source Pack"]["CIK"] == "0000320193"
    assert payload["Earnings Source Pack"]["Status"] == "available"
    assert payload["Earnings Source Pack"]["SEC 8-K Sections"]["Earnings Release"]["Item"] == (
        "Item 2.02"
    )
    assert payload["Data Coverage"]["SEC Filings / MD&A"] == "available"
    assert payload["Data Coverage"]["Earnings Transcript / Guidance"] == "available"
    assert payload["Volatility Metrics"]
    assert payload["Auction Market Theory Price Levels"]["Point of Control (POC)"] > 0
    assert payload["Anthropic Report"] == "Stock Fax narrative."
    assert payload["Text Provider"] == "anthropic"


def test_vision_tool_returns_market_memo() -> None:
    app = create_app()
    generator = FakeTextGenerator("### AAPL Vision\n\nAnthropic memo.")
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["SEC_CLIENT"] = FakeSecClient()
    app.config["TEXT_GENERATOR"] = generator
    client = app.test_client()

    response = client.post("/api/tools/vision", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["Ticker"] == "AAPL"
    assert "AAPL Vision" in payload["Market Memo"]
    assert payload["Report"]["Ticker"] == "AAPL"
    assert [chart["key"] for chart in payload["Memo Charts"]] == [
        "auction",
        "regression",
        "volatility",
    ]
    assert payload["Memo Charts"][0]["placement"] == "Price Map"
    assert payload["Memo Charts"][0]["image"]["mime"] == "image/png"
    assert payload["Chart Errors"] == []
    assert payload["Text Model"] == "claude-test"
    assert generator.calls[0]["max_tokens"] == 3200
    prompt = str(generator.calls[0]["prompt"])
    assert "Company, Sector, And Business Model" in prompt
    assert "Management And Execution" in prompt
    assert "Research Gaps / Next Diligence" in prompt
    assert "Final Rating" in prompt
    assert "Strong Buy, Buy, Hold, Neutral, Sell, Strong Sell" in prompt
    assert "this must be the final section at the bottom of the memo" in prompt
    assert "Do not issue a personal buy/sell recommendation" not in prompt
    assert "Jane Analyst" in prompt
    assert "SEC 10-K Item 1 Business" in prompt
    assert "Earnings Source Pack" in prompt
    assert "SEC 8-K Item 2.02 Earnings Release" in prompt
    assert "Apple reported record revenue" in prompt
    assert "RevenueFromContractWithCustomerExcludingAssessedTax" in prompt


def test_vision_stream_tool_returns_ndjson_events() -> None:
    app = create_app()
    generator = FakeStreamingTextGenerator()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["SEC_CLIENT"] = FakeSecClient()
    app.config["TEXT_GENERATOR"] = generator
    client = app.test_client()

    response = client.post("/api/tools/vision/stream", json={"ticker": "AAPL"})

    events = [
        json.loads(line)
        for line in response.data.decode("utf-8").splitlines()
        if line.strip()
    ]
    assert response.status_code == 200
    assert [event["type"] for event in events] == ["meta", "token", "token", "done"]
    assert events[0]["Ticker"] == "AAPL"
    assert events[0]["Text Model"] == "claude-stream-test"
    assert events[-1]["text"] == "### AAPL Vision\n\nStreamed memo."
    assert events[-1]["export"]["mode"] == "vision"
    assert events[-1]["export"]["market_memo"] == events[-1]["text"]
    assert [chart["key"] for chart in events[-1]["Memo Charts"]] == [
        "auction",
        "regression",
        "volatility",
    ]
    assert events[-1]["export"]["image_files"][0]["filename"].endswith("-auction.png")
    assert events[-1]["export"]["memo_charts"][1]["placement"] == (
        "Equity Performance And Positioning"
    )
    assert events[-1]["export"]["report"]["SEC Source Pack"]["CIK"] == "0000320193"
    assert events[-1]["export"]["report"]["Earnings Source Pack"]["Status"] == "available"
    assert generator.calls[0]["max_tokens"] == 3200


def test_text_tool_reports_missing_anthropic_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    app.config["SEC_CLIENT"] = FakeSecClient()
    app.config["ANTHROPIC_API_KEY"] = ""
    client = app.test_client()

    response = client.post("/api/tools/vision", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 400
    assert "ANTHROPIC_API_KEY" in payload["error"]


def test_sec_endpoint_returns_source_pack() -> None:
    app = create_app()
    app.config["SEC_CLIENT"] = FakeSecClient()
    client = app.test_client()

    response = client.get("/api/sec/AAPL")

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["Provider"] == "SEC EDGAR"
    assert payload["Filing Sections"]["MD&A"]["Item"] == "Item 7"


def test_pixel_tool_reports_missing_openai_key(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("UNDERLYING_SKIP_DOTENV", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    app = create_app()
    app.config["OPENAI_API_KEY"] = ""
    client = app.test_client()

    response = client.post("/api/tools/pixel", json={"prompt": "market mascot"})

    payload = response.get_json()
    assert response.status_code == 400
    assert "OPENAI_API_KEY" in payload["error"]


def test_moneyline_tool_returns_image_payload(monkeypatch: MonkeyPatch) -> None:
    def fake_moneyline(
        ticker: str, expiry: str | None = None
    ) -> tuple[RenderedImage, dict[str, object]]:
        return (
            RenderedImage("abc123", "image/png", "moneyline.png"),
            {
                "ticker": ticker,
                "expiry": expiry or "2026-06-19",
                "current_price": 100.0,
                "rows": [],
            },
        )

    monkeypatch.setattr(main_module, "render_moneyline_chart", fake_moneyline)
    app = create_app()
    client = app.test_client()

    response = client.post(
        "/api/tools/moneyline", json={"ticker": "AAPL", "expiry": "2026-06-19"}
    )

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["image"]["mime"] == "image/png"
    assert payload["meta"]["ticker"] == "AAPL"
