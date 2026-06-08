from __future__ import annotations

from pathlib import Path

import pandas as pd
from pytest import MonkeyPatch

import app.main as main_module
from app.charts import RenderedImage
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


def test_index_includes_underlying_tool_dock() -> None:
    app = create_app()
    client = app.test_client()

    response = client.get("/")

    assert response.status_code == 200
    for route in (b"/vision", b"/pixel", b"/fax", b"/moneyline"):
        assert route in response.data


def test_legacy_tool_routes_render_status_page() -> None:
    app = create_app()
    client = app.test_client()

    for route in ("/vision", "/pixel", "/fax", "/moneyline"):
        response = client.get(route)

        assert response.status_code == 200
        assert b"/static/tools.js" in response.data


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
    assert payload["meta"]["benchmark_ticker"] == "SPY"
    assert payload["meta"]["total_return"] > 0
    assert "alpha_vs_benchmark" in payload["meta"]
    assert payload["meta"]["equity_curve"]
    assert payload["export"]["tickers"] == ["AAPL", "MSFT"]
    assert payload["export"]["meta"]["benchmark_equity_curve"]


def test_analysis_post_endpoint_returns_batch_summaries() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/analysis", json={"tickers": ["AAPL", "MSFT"]})

    payload = response.get_json()
    assert response.status_code == 200
    assert [summary["ticker"] for summary in payload["summaries"]] == ["AAPL", "MSFT"]
    assert [row["rank"] for row in payload["scanner"]] == [1, 2]
    assert {row["ticker"] for row in payload["scanner"]} == {"AAPL", "MSFT"}
    assert payload["export"]["mode"] == "analysis"
    assert payload["export"]["scanner"] == payload["scanner"]


def test_stock_fax_tool_returns_migrated_report() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/tools/fax", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["Ticker"] == "AAPL"
    assert payload["Volatility Metrics"]
    assert payload["Auction Market Theory Price Levels"]["Point of Control (POC)"] > 0


def test_vision_tool_returns_market_memo() -> None:
    app = create_app()
    app.config["MARKET_DATA_CLIENT"] = FakeMarketDataClient()
    client = app.test_client()

    response = client.post("/api/tools/vision", json={"ticker": "AAPL"})

    payload = response.get_json()
    assert response.status_code == 200
    assert payload["Ticker"] == "AAPL"
    assert "AAPL Vision" in payload["Market Memo"]
    assert payload["Report"]["Ticker"] == "AAPL"


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
