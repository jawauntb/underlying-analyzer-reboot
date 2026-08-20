from __future__ import annotations

from datetime import date
from typing import Any, cast

import pandas as pd
import pytest
import requests

from app.market_data import HistoryResult, MarketDataClient, MarketDataError, MarketDataProvider
from app.massive import MassiveProvider, MassiveProviderError


class FakeResponse:
    def __init__(
        self, payload: object, *, status_code: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def get(self, url: str, *, params: dict[str, Any], timeout: float) -> FakeResponse:
        self.calls.append((url, params, timeout))
        return self.responses.pop(0)


def test_massive_history_maps_bars_and_follows_next_url() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "status": "OK",
                    "results": [{"t": 1767358800000, "o": 10, "h": 12, "l": 9, "c": 11, "v": 100}],
                    "next_url": "https://api.massive.com/v2/aggs?page=2",
                }
            ),
            FakeResponse(
                {
                    "status": "OK",
                    "results": [{"t": 1767445200000, "o": 11, "h": 13, "l": 10, "c": 12, "v": 110}],
                }
            ),
        ]
    )
    provider = MassiveProvider("secret-key", session=session)

    result = provider.get_history(
        "AAPL", start=date(2026, 1, 1), end=date(2026, 1, 5), interval="1d"
    )

    assert list(result.data.columns) == ["Open", "High", "Low", "Close", "Volume", "Adj Close"]
    assert result.data["Adj Close"].tolist() == [11, 12]
    assert result.provider == "massive"
    assert session.calls[0][0].endswith("/v2/aggs/ticker/AAPL/range/1/day/2026-01-01/2026-01-05")
    assert session.calls[0][1]["apiKey"] == "secret-key"
    assert session.calls[1][0] == "https://api.massive.com/v2/aggs?page=2"
    assert result.interval == "1d"


@pytest.mark.parametrize(
    ("interval", "path_fragment"),
    [
        ("15m", "/v2/aggs/ticker/AAPL/range/15/minute/2026-01-01/2026-01-05"),
        ("1w", "/v2/aggs/ticker/AAPL/range/1/week/2026-01-01/2026-01-05"),
        ("1wk", "/v2/aggs/ticker/AAPL/range/1/week/2026-01-01/2026-01-05"),
    ],
)
def test_massive_history_maps_intraday_and_weekly_intervals(
    interval: str, path_fragment: str
) -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "status": "OK",
                    "results": [{"t": 1767358800000, "o": 10, "h": 12, "l": 9, "c": 11, "v": 100}],
                }
            )
        ]
    )
    result = MassiveProvider("secret-key", session=session).get_history(
        "AAPL", start=date(2026, 1, 1), end=date(2026, 1, 5), interval=interval
    )

    assert session.calls[0][0].endswith(path_fragment)
    assert result.interval in {"15m", "1w"}


def test_massive_history_rejects_unknown_interval() -> None:
    provider = MassiveProvider("secret-key", session=FakeSession([]))
    with pytest.raises(ValueError, match="15m, 1d, or 1w"):
        provider.get_history("AAPL", start=date(2026, 1, 1), end=date(2026, 1, 5), interval="4h")


def test_massive_retries_rate_limits_and_honors_retry_after_without_leaking_key() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"status": "ERROR", "error": "slow down"},
                status_code=429,
                headers={"Retry-After": "2"},
            ),
            FakeResponse({"status": "OK", "results": []}),
        ]
    )
    delays: list[float] = []
    provider = MassiveProvider("secret-key", session=session, sleep=delays.append, max_retries=1)

    payload = provider.get_snapshot("AAPL")

    assert payload["status"] == "OK"
    assert delays == [2.0]

    failing = FakeSession(
        [FakeResponse({"status": "ERROR", "error": "bad upstream"}, status_code=500)]
    )
    with pytest.raises(MassiveProviderError, match="status 500") as error:
        MassiveProvider("secret-key", session=failing, max_retries=0).get_snapshot("AAPL")
    assert "secret-key" not in str(error.value)


def test_massive_server_key_cannot_be_overridden_by_request_params() -> None:
    session = FakeSession([FakeResponse({"status": "OK", "results": []})])
    MassiveProvider("secret-key", session=session).get_trades("AAPL", params={"apiKey": "attacker"})
    assert session.calls[0][1]["apiKey"] == "secret-key"


def test_massive_rejects_untrusted_pagination_hosts() -> None:
    session = FakeSession(
        [FakeResponse({"status": "OK", "results": [], "next_url": "https://evil.example/next"})]
    )
    with pytest.raises(MassiveProviderError, match="untrusted"):
        MassiveProvider("secret-key", session=session).get_trades("AAPL")


@pytest.mark.parametrize("status_code", [401, 403])
def test_massive_auth_errors_are_terminal_and_redacted(status_code: int) -> None:
    session = FakeSession([FakeResponse({"status": "ERROR"}, status_code=status_code)])
    with pytest.raises(MassiveProviderError, match="authentication or subscription") as error:
        MassiveProvider("secret-key", session=session, max_retries=3).get_snapshot("AAPL")
    assert len(session.calls) == 1
    assert "secret-key" not in str(error.value)


def test_massive_exhausted_retries_and_transport_errors_are_explicit() -> None:
    session = FakeSession(
        [
            FakeResponse({"status": "ERROR"}, status_code=500),
            FakeResponse({"status": "ERROR"}, status_code=500),
        ]
    )
    with pytest.raises(MassiveProviderError, match="status 500"):
        MassiveProvider("secret-key", session=session, max_retries=1).get_snapshot("AAPL")
    assert len(session.calls) == 2

    class RaisingSession:
        def get(self, *_: Any, **__: Any) -> Any:
            raise requests.ConnectionError("offline")

    with pytest.raises(MassiveProviderError, match="request failed"):
        MassiveProvider("secret-key", session=RaisingSession(), max_retries=0).get_snapshot("AAPL")


@pytest.mark.parametrize("payload", [{"status": "ERROR", "message": "bad"}, [], "bad"])
def test_massive_malformed_or_provider_error_payloads_are_rejected(payload: object) -> None:
    session = FakeSession([FakeResponse(payload)])
    with pytest.raises(MassiveProviderError):
        MassiveProvider("secret-key", session=session).get_snapshot("AAPL")


def test_massive_options_chain_maps_contracts_and_current_price() -> None:
    contracts = {
        "status": "OK",
        "results": [
            {"expiration_date": "2026-08-21"},
            {"expiration_date": "2026-08-28"},
        ],
    }
    chain = {
        "status": "OK",
        "results": [
            {
                "details": {"strike_price": strike, "contract_type": contract_type},
                "open_interest": 100 if contract_type == "call" else 80,
                "last_trade": {"price": 2.5 if contract_type == "call" else 2.0},
                "underlying_asset": {"price": 101.0},
            }
            for strike in (99, 100, 101)
            for contract_type in ("call", "put")
        ],
    }
    session = FakeSession([FakeResponse(contracts), FakeResponse(chain)])
    provider = MassiveProvider("secret-key", session=session)

    result = provider.get_option_chain("AAPL", expiry="2026-08-21")

    assert result.expiry == "2026-08-21"
    assert result.current_price == 101.0
    assert result.expirations == ["2026-08-21", "2026-08-28"]
    assert result.rows[-1]["strike"] == 101.0
    assert result.rows[-1]["call_open_interest"] == 100
    assert result.rows[-1]["put_call_ratio"] == 0.8
    assert session.calls[1][0].endswith("/v3/snapshot/options/AAPL")
    assert session.calls[1][1]["expiration_date"] == "2026-08-21"


def test_massive_profile_maps_legacy_consumer_fields() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {
                    "status": "OK",
                    "results": {
                        "ticker": "AAPL",
                        "name": "Apple Inc.",
                        "market_cap": 1_000_000,
                        "sic_description": "Electronic computers",
                        "description": "Designs consumer electronics.",
                        "homepage_url": "https://apple.com",
                        "locale": "us",
                    },
                }
            )
        ]
    )
    profile = MassiveProvider("secret-key", session=session).get_profile("AAPL")

    assert profile["longName"] == "Apple Inc."
    assert profile["marketCap"] == 1_000_000
    assert profile["industry"] == "Electronic computers"
    assert profile["longBusinessSummary"] == "Designs consumer electronics."
    assert profile["website"] == "https://apple.com"


def test_massive_search_preserves_supported_asset_classes() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"status": "OK", "results": [{"ticker": "AAPL", "name": "Apple", "type": "ETF"}]}
            ),
            FakeResponse(
                {"status": "OK", "results": [{"ticker": "SPX", "name": "S&P 500", "type": "INDEX"}]}
            ),
            FakeResponse(
                {
                    "status": "OK",
                    "results": [{"ticker": "BTCUSD", "name": "Bitcoin", "type": "CRYPTOCURRENCY"}],
                }
            ),
        ]
    )
    results = MassiveProvider("secret-key", session=session).search_securities("A", limit=10)

    assert [result["asset_type"] for result in results] == ["etf", "index", "crypto"]
    assert len(session.calls) == 3


def test_massive_additive_dataset_paths_and_pagination() -> None:
    session = FakeSession(
        [
            FakeResponse(
                {"status": "OK", "results": [{"p": 1}], "next_url": "https://api.massive.com/next"}
            ),
            FakeResponse({"status": "OK", "results": [{"p": 2}]}),
            FakeResponse({"status": "OK", "results": [{"c": 3}]}),
            FakeResponse({"status": "OK", "results": [{"p": 4}]}),
            FakeResponse({"status": "OK", "results": [{"bid_price": 5}]}),
            FakeResponse({"status": "OK", "results": [{"event_type": "ticker_change"}]}),
        ]
    )
    provider = MassiveProvider("secret-key", session=session)

    trades = provider.get_trades("AAPL")
    aggregates = provider.get_aggregates(
        "AAPL", multiplier=5, timespan="minute", start="2026-08-01", end="2026-08-02"
    )
    quotes = provider.get_quotes("AAPL")
    contracts = provider.get_contracts("AAPL")
    events = provider.get_events("AAPL")

    assert trades["results"] == [{"p": 1}, {"p": 2}]
    assert aggregates["results"] == [{"c": 3}]
    assert quotes["results"] == [{"p": 4}]
    assert contracts["results"] == [{"bid_price": 5}]
    assert events["results"] == [{"event_type": "ticker_change"}]
    assert "/v3/trades/AAPL" in session.calls[0][0]
    assert "/v2/aggs/ticker/AAPL/range/5/minute/2026-08-01/2026-08-02" in session.calls[2][0]
    assert "/v3/quotes/AAPL" in session.calls[3][0]
    assert session.calls[4][1]["underlying_ticker"] == "AAPL"
    assert "/vX/reference/tickers/AAPL/events" in session.calls[5][0]
    assert session.responses == []


def test_massive_pagination_guards_repeated_urls_and_max_pages() -> None:
    repeated = FakeSession(
        [
            FakeResponse({"status": "OK", "results": [], "next_url": "/same"}),
            FakeResponse({"status": "OK", "results": [], "next_url": "/same"}),
        ]
    )
    provider = MassiveProvider("secret-key", session=repeated)
    with pytest.raises(MassiveProviderError, match="repeated"):
        provider.get_trades("AAPL")

    bounded = FakeSession(
        [
            FakeResponse({"status": "OK", "results": [{"p": 1}], "next_url": "/two"}),
            FakeResponse({"status": "OK", "results": [{"p": 2}], "next_url": "/three"}),
        ]
    )
    payload = MassiveProvider("secret-key", session=bounded, max_pages=2).get_trades("AAPL")
    assert payload["results"] == [{"p": 1}, {"p": 2}]
    assert "next_cursor" in payload
    assert "next_url" not in payload
    assert len(bounded.calls) == 2


def test_market_data_facade_falls_back_and_can_disable_fallback() -> None:
    frame = pd.DataFrame(
        {"Open": [1], "High": [2], "Low": [0], "Close": [1.5], "Volume": [10]},
        index=pd.to_datetime(["2026-08-01"]),
    )

    class FailingProvider:
        name = "massive"
        note = "test"

        def get_history(self, *_: Any, **__: Any) -> HistoryResult:
            raise MarketDataError("rate limited")

    class LegacyProvider:
        def get_history(self, ticker: str, **_: Any) -> HistoryResult:
            return HistoryResult(ticker, frame, "yfinance", "fixture")

    client = MarketDataClient(
        provider=cast(MarketDataProvider, FailingProvider()),
        fallback_provider=cast(MarketDataProvider, LegacyProvider()),
        fallback_enabled=True,
    )
    assert client.get_history("AAPL").provider == "yfinance"

    disabled = MarketDataClient(
        provider=cast(MarketDataProvider, FailingProvider()),
        fallback_provider=cast(MarketDataProvider, LegacyProvider()),
        fallback_enabled=False,
    )
    with pytest.raises(MarketDataError, match="No historical data"):
        disabled.get_history("AAPL")


def test_market_data_facade_reports_the_adapter_that_served_the_request() -> None:
    class PrimaryProvider:
        name = "massive"
        note = "Massive REST"

        def get_snapshot(self, ticker: str, *, asset_class: str) -> dict[str, Any]:
            return {"ticker": ticker, "asset_class": asset_class, "source": "primary"}

    class FallbackProvider:
        name = "yfinance"
        note = "legacy fallback"

        def get_snapshot(self, ticker: str, *, asset_class: str) -> dict[str, Any]:
            return {"ticker": ticker, "asset_class": asset_class, "source": "fallback"}

    primary_client = MarketDataClient(
        provider=cast(MarketDataProvider, PrimaryProvider()),
        fallback_provider=cast(MarketDataProvider, FallbackProvider()),
        fallback_enabled=True,
    )
    assert primary_client.get_snapshot("AAPL", asset_class="stocks")["source"] == "primary"
    assert primary_client.provider_label == "massive"

    class FailingPrimary(PrimaryProvider):
        def get_snapshot(self, *_: Any, **__: Any) -> dict[str, Any]:
            raise MarketDataError("provider unavailable")

    fallback_client = MarketDataClient(
        provider=cast(MarketDataProvider, FailingPrimary()),
        fallback_provider=cast(MarketDataProvider, FallbackProvider()),
        fallback_enabled=True,
    )
    assert fallback_client.get_snapshot("AAPL", asset_class="stocks")["source"] == "fallback"
    assert fallback_client.provider_label == "yfinance"


def test_massive_empty_history_is_explicit_missing_data() -> None:
    session = FakeSession([FakeResponse({"status": "OK", "results": []})])
    provider = MassiveProvider("secret-key", session=session)
    result = provider.get_history(
        "MISSING", start=date(2026, 1, 1), end=date(2026, 1, 5), interval="1d"
    )
    assert result.data.empty


def test_market_data_facade_falls_back_when_massive_returns_empty_history() -> None:
    frame = pd.DataFrame(
        {"Open": [1], "High": [2], "Low": [0], "Close": [1.5], "Volume": [10]},
        index=pd.to_datetime(["2026-08-01"]),
    )

    class EmptyProvider:
        name = "massive"
        note = "test"

        def get_history(self, ticker: str, **_: Any) -> HistoryResult:
            return HistoryResult(ticker, pd.DataFrame(), self.name, self.note)

    class FallbackProvider:
        def get_history(self, ticker: str, **_: Any) -> HistoryResult:
            return HistoryResult(ticker, frame, "yfinance", "fixture")

    client = MarketDataClient(
        provider=cast(MarketDataProvider, EmptyProvider()),
        fallback_provider=cast(MarketDataProvider, FallbackProvider()),
        fallback_enabled=True,
    )
    result = client.get_history("AAPL")
    assert result.provider == "yfinance"


def test_market_data_facade_enriches_incomplete_massive_profiles_from_legacy() -> None:
    class PrimaryProvider:
        name = "massive"
        note = "massive"

        def get_profile(self, _ticker: str) -> dict[str, Any]:
            return {"longName": "Apple Inc.", "marketCap": 1_000_000}

    class LegacyProfileProvider:
        name = "yfinance"
        note = "legacy"

        def get_profile(self, _ticker: str) -> dict[str, Any]:
            return {"sector": "Technology", "trailingPE": 30.0, "beta": 1.2}

    client = MarketDataClient(
        provider=cast(MarketDataProvider, PrimaryProvider()),
        fallback_provider=cast(MarketDataProvider, LegacyProfileProvider()),
        fallback_enabled=True,
    )
    profile = client.get_profile("AAPL")

    assert profile["marketCap"] == 1_000_000
    assert profile["sector"] == "Technology"
    assert profile["trailingPE"] == 30.0
    assert profile["provider_fields_fallback"] == "yfinance"


def test_massive_option_chain_rejects_missing_underlying_price() -> None:
    session = FakeSession(
        [
            FakeResponse({"status": "OK", "results": [{"expiration_date": "2026-08-21"}]}),
            FakeResponse(
                {
                    "status": "OK",
                    "results": [{"details": {"strike_price": 100, "contract_type": "call"}}],
                }
            ),
        ]
    )
    with pytest.raises(MarketDataError, match="underlying price"):
        MassiveProvider("secret-key", session=session).get_option_chain("AAPL")


def test_massive_corporate_actions_financials_and_market_status_paths() -> None:
    session = FakeSession(
        [
            FakeResponse({"status": "OK", "results": [{"cash_amount": 1}]}),
            FakeResponse({"status": "OK", "results": [{"split_to": 2}]}),
            FakeResponse({"status": "OK", "results": [{"revenue": 10}]}),
            FakeResponse({"market": "open", "serverTime": "2026-08-19T14:00:00Z"}),
        ]
    )
    provider = MassiveProvider("secret-key", session=session)

    assert provider.get_dividends("AAPL")["results"] == [{"cash_amount": 1}]
    assert provider.get_splits("AAPL")["results"] == [{"split_to": 2}]
    assert provider.get_financials("AAPL", statement="income")["results"] == [{"revenue": 10}]
    assert provider.get_market_status()["market"] == "open"
    assert session.calls[0][0].endswith("/stocks/v1/dividends")
    assert session.calls[1][0].endswith("/stocks/v1/splits")
    assert session.calls[2][0].endswith("/stocks/financials/v1/income-statements")
    assert session.calls[2][1]["tickers"] == "AAPL"
    assert session.calls[3][0].endswith("/v1/marketstatus/now")


def test_massive_entitlement_dataset_paths_and_option_snapshot() -> None:
    session = FakeSession(
        [
            FakeResponse({"status": "OK", "results": [{"title": "Apple news"}]}),
            FakeResponse({"status": "OK", "results": [{"event_type": "earnings"}]}),
            FakeResponse({"status": "OK", "results": [{"ticker": "NEW"}]}),
            FakeResponse({"status": "OK", "results": [{"code": "T"}]}),
            FakeResponse({"status": "OK", "tickers": [{"ticker": "AAPL"}]}),
            FakeResponse(
                {
                    "status": "OK",
                    "ticker": "O:AAPL260821C00100000",
                    "details": {"strike_price": 100},
                }
            ),
        ]
    )
    provider = MassiveProvider("secret-key", session=session)

    assert provider.get_news("AAPL")["results"] == [{"title": "Apple news"}]
    assert provider.get_corporate_events()["results"] == [{"event_type": "earnings"}]
    assert provider.get_ipos()["results"] == [{"ticker": "NEW"}]
    assert provider.get_conditions()["results"] == [{"code": "T"}]
    assert provider.get_all_snapshot()["tickers"] == [{"ticker": "AAPL"}]
    assert provider.get_option_snapshot("AAPL", "aapl260821c00100000")["ticker"] == (
        "O:AAPL260821C00100000"
    )

    assert session.calls[0][0].endswith("/v2/reference/news")
    assert session.calls[0][1] == {
        "ticker": "AAPL",
        "order": "desc",
        "sort": "published_utc",
        "limit": 20,
        "apiKey": "secret-key",
    }
    assert session.calls[1][0].endswith("/tmx/v1/corporate-events")
    assert session.calls[2][0].endswith("/vX/reference/ipos")
    assert session.calls[3][0].endswith("/v3/reference/conditions")
    assert session.calls[4][0].endswith("/v2/snapshot/locale/us/markets/stocks/tickers")
    assert session.calls[5][0].endswith("/v3/snapshot/options/AAPL/O:AAPL260821C00100000")


def test_massive_option_rows_preserve_greeks_quotes_and_contract_identity() -> None:
    session = FakeSession(
        [
            FakeResponse({"status": "OK", "results": [{"expiration_date": "2026-08-21"}]}),
            FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {
                            "details": {
                                "strike_price": 100,
                                "contract_type": "call",
                                "ticker": "O:AAPL",
                            },
                            "open_interest": 12,
                            "implied_volatility": 0.25,
                            "greeks": {"delta": 0.5, "gamma": 0.1, "theta": -0.02, "vega": 0.3},
                            "last_quote": {"bid_price": 1.2, "ask_price": 1.4},
                            "day": {"volume": 99},
                            "last_trade": {"price": 1.3},
                            "underlying_asset": {"price": 100},
                        }
                    ],
                }
            ),
        ]
    )
    result = MassiveProvider("secret-key", session=session).get_option_chain(
        "AAPL", expiry="2026-08-21"
    )

    assert result.rows[0]["call_contract"] == "O:AAPL"
    assert result.rows[0]["call_implied_volatility"] == 0.25
    assert result.rows[0]["call_delta"] == 0.5
    assert result.rows[0]["call_bid"] == 1.2
    assert result.rows[0]["call_ask"] == 1.4
    assert result.rows[0]["call_volume"] == 99
