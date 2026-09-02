"""W3 narrative-layer tests: fundamentals, filings, news, volatility, levels,
memo, chat, store, export and the engine orchestration.

Everything here is offline and deterministic. The market, SEC, Exa and text-model
doubles follow the fixture pattern in ``tests/test_ticker_research.py`` and
``tests/test_api.py``: plain classes with the exact methods the code under test
calls, so a signature change fails loudly instead of being mocked away.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.anthropic import GeneratedText
from app.market_data import HistoryResult, OptionChainResult
from app.prism import chat as chat_module
from app.prism import engine as engine_module
from app.prism import export as export_module
from app.prism import filings as filings_module
from app.prism import fundamentals as fundamentals_module
from app.prism import levels as levels_module
from app.prism import memo as memo_module
from app.prism import news as news_module
from app.prism import store as store_module
from app.prism import volatility as volatility_module

# ---------------------------------------------------------------------------
# Deterministic doubles
# ---------------------------------------------------------------------------

BUILD_ROWS = 900


def _ohlcv(ticker: str, *, rows: int, seed: int) -> pd.DataFrame:  # noqa: ARG001, ARG002
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2019-01-01", periods=rows)
    close = 100.0 * np.exp(np.cumsum(rng.normal(0.0004, 0.014, rows)))
    return pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Adj Close": close,
            "Volume": rng.integers(1_000_000, 5_000_000, rows).astype(float),
        },
        index=index,
    )


def _quarter_rows(ticker: str, statement: str, count: int = 12) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(count):
        year = 2024 + index // 4
        quarter = index % 4 + 1
        revenue = 20_000_000_000.0 * (1.08**index)
        row: dict[str, Any] = {
            "tickers": [ticker],
            "period_end": f"{year}-{quarter * 3:02d}-28",
            "filing_date": f"{year}-{quarter * 3:02d}-28",
            "fiscal_quarter": quarter,
            "fiscal_year": year,
            "timeframe": "quarterly",
        }
        if statement == "income":
            row.update(
                {
                    "revenue": revenue,
                    "cost_of_revenue": revenue * 0.25,
                    "gross_profit": revenue * 0.75,
                    "operating_income": revenue * 0.60,
                    "net_income_loss_attributable_common_shareholders": revenue * 0.52,
                    "basic_earnings_per_share": revenue * 0.52 / 24_000_000_000.0,
                    "diluted_earnings_per_share": revenue * 0.52 / 24_500_000_000.0,
                    "diluted_shares_outstanding": 24_500_000_000.0,
                    "ebitda": revenue * 0.63,
                }
            )
        elif statement == "balance":
            row.update(
                {
                    "cash_and_equivalents": 15_000_000_000.0,
                    "short_term_investments": 40_000_000_000.0,
                    "inventories": 12_000_000_000.0,
                    "total_assets": 160_000_000_000.0,
                    "total_liabilities": 60_000_000_000.0,
                    "total_equity": 100_000_000_000.0,
                    "debt_current": 1_000_000_000.0,
                    "long_term_debt_and_capital_lease_obligations": 8_000_000_000.0,
                }
            )
        else:
            row.update(
                {
                    "net_cash_from_operating_activities": revenue * 0.55,
                    "purchase_of_property_plant_and_equipment": -revenue * 0.05,
                    "dividends": -100_000_000.0,
                }
            )
        rows.append(row)
    return rows


class FakePrismMarketClient:
    """Every market call the W3 layer makes, with stable numbers."""

    def __init__(self, *, rows: int = BUILD_ROWS, fail_options: bool = False) -> None:
        self.rows = rows
        self.fail_options = fail_options
        self.calls: list[tuple[str, str]] = []
        self.financial_params: list[dict[str, Any]] = []

    def get_history(self, ticker: str, **kwargs: Any) -> HistoryResult:
        self.calls.append(("history", ticker))
        rows = 260 if kwargs.get("period") == "1y" else self.rows
        return HistoryResult(
            ticker=ticker,
            data=_ohlcv(ticker, rows=rows, seed=abs(hash(ticker)) % 991),
            provider="fake-massive",
            note="deterministic test series",
            interval="1d",
        )

    def get_profile(self, ticker: str) -> dict[str, Any]:
        self.calls.append(("profile", ticker))
        return {
            "longName": f"{ticker} Corp",
            "symbol": ticker,
            "exchange": "XNAS",
            "marketCap": 1_500_000_000_000.0,
            "industry": "SEMICONDUCTORS & RELATED DEVICES",
            "longBusinessSummary": f"{ticker} designs semiconductors.",
            "cik": "0001045810",
            "massive": {"primary_exchange": "XNAS", "list_date": "1999-01-22"},
        }

    def get_financials(
        self, ticker: str, *, statement: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self.financial_params.append({"statement": statement, **(params or {})})
        if statement == "ratios":
            return {
                "results": [
                    {"ticker": "OTHER", "price": 1.0},
                    {
                        "ticker": ticker,
                        "price": 220.0,
                        "market_cap": 1_500_000_000_000.0,
                        "price_to_earnings": 27.6,
                        "dividend_yield": 0.0013,
                        "return_on_equity": 0.84,
                        "current": 4.59,
                    },
                ]
            }
        return {"results": _quarter_rows(ticker, statement)}

    def get_expirations(self, ticker: str) -> list[str]:  # noqa: ARG001, ARG002
        return ["2026-09-04", "2026-09-18", "2026-10-16"]

    def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:
        if self.fail_options:
            raise RuntimeError("option entitlement missing")
        price = 220.0
        rows = []
        for offset in range(-4, 5):
            strike = price + offset * 5.0
            call_delta = max(0.02, min(0.98, 0.5 - offset * 0.0625))
            rows.append(
                {
                    "strike": strike,
                    "call_implied_volatility": 0.42 + 0.01 * abs(offset),
                    "put_implied_volatility": 0.44 + 0.012 * abs(offset),
                    "call_delta": call_delta,
                    "put_delta": call_delta - 1.0,
                    "call_open_interest": 1000.0,
                    "put_open_interest": 900.0,
                }
            )
        return OptionChainResult(
            ticker=ticker,
            expiry=expiry or "2026-09-18",
            current_price=price,
            rows=rows,
            expirations=self.get_expirations(ticker),
            provider="fake-massive",
            note="deterministic option chain",
        )

    def get_news(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG001, ARG002
        return {
            "results": [
                {
                    "title": f"{ticker} beats expectations",
                    "article_url": f"https://news.example.com/{ticker.lower()}-beats",
                    "published_utc": "2026-08-30T12:00:00Z",
                    "description": "Revenue and margins came in ahead of consensus.",
                    "publisher": {"name": "Example Wire"},
                    "tickers": [ticker],
                }
            ]
        }

    def get_snapshot(self, ticker: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001, ARG002
        return {"ticker": ticker, "last_trade": {"price": 220.0}}


class FakeTextGenerator:
    def __init__(self, text: str = "Fake analyst text.") -> None:
        self.text = text
        self.calls: list[dict[str, Any]] = []

    def generate_text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 700,
        temperature: float = 0.2,  # noqa: ARG002
    ) -> GeneratedText:
        self.calls.append({"system": system, "prompt": prompt, "max_tokens": max_tokens})
        return GeneratedText(text=self.text, model="claude-test")


class ExplodingTextGenerator:
    def generate_text(self, **_: Any) -> GeneratedText:
        raise RuntimeError("model is offline")


class FakeSecClient:
    SUBMISSIONS: dict[str, Any] = {
        "cik": "1045810",
        "name": "NVIDIA CORP",
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q", "10-K", "10-Q", "10-Q", "10-K", "10-K"],
                "accessionNumber": [f"0001045810-26-00000{index}" for index in range(7)],
                "primaryDocument": [f"doc{index}.htm" for index in range(7)],
                "filingDate": [
                    "2026-08-01",
                    "2026-08-28",
                    "2026-02-26",
                    "2025-11-20",
                    "2025-08-28",
                    "2025-02-26",
                    "2024-02-21",
                ],
                "reportDate": [
                    "2026-08-01",
                    "2026-07-26",
                    "2026-01-26",
                    "2025-10-26",
                    "2025-07-28",
                    "2025-01-26",
                    "2024-01-28",
                ],
            }
        },
    }

    DOCUMENT = (
        "<html><body>Item 1. Business "
        + "The company designs accelerated computing platforms. " * 40
        + "Item 1A. Risk Factors "
        + "Demand may not materialise as expected. " * 40
        + "Item 1B. Unresolved Staff Comments none. "
        + "Item 7. Management's Discussion and Analysis "
        + "Revenue grew on data centre demand and margins expanded. " * 40
        + "Item 7A. Quantitative and Qualitative Disclosures</body></html>"
    )

    def __init__(self) -> None:
        self.fetched: list[str] = []

    def cik_for_ticker(self, ticker: str) -> str:  # noqa: ARG001, ARG002
        return "0001045810"

    def submissions(self, cik: str) -> dict[str, Any]:  # noqa: ARG001, ARG002
        return self.SUBMISSIONS

    def fetch_text(self, url: str) -> str:
        self.fetched.append(url)
        return self.DOCUMENT

    def fetch_json(self, url: str) -> Any:  # noqa: ARG001, ARG002
        raise RuntimeError("companyfacts not available in this fake")


class FakeExaResult:
    def __init__(self, index: int) -> None:
        self.title = f"Headline {index}"
        self.url = f"https://example.com/story/{index}"
        self.published_date = "2026-08-25T00:00:00Z"
        self.snippet = f"Summary of story {index}."
        self.text = None
        self.score = 0.5
        self.author = None


class FakeExaClient:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.queries: list[str] = []
        self.fail_on = fail_on

    def search(self, query: str, **kwargs: Any) -> list[FakeExaResult]:  # noqa: ARG001, ARG002
        self.queries.append(query)
        if self.fail_on and self.fail_on in query:
            raise RuntimeError("exa rate limited")
        return [FakeExaResult(index) for index in range(3)]


@pytest.fixture()
def isolated_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> store_module.PrismStore:
    monkeypatch.setenv("PRISM_CACHE_DIR", str(tmp_path))
    store_module.reset_default_store()
    yield store_module.PrismStore(base_dir=tmp_path, supabase=None)
    store_module.reset_default_store()


# ---------------------------------------------------------------------------
# fundamentals
# ---------------------------------------------------------------------------


class TestFundamentals:
    def test_quarters_join_three_statements_newest_first(self) -> None:
        client = FakePrismMarketClient()

        section = fundamentals_module.build_fundamentals(client, "NVDA", current_price=220.0)

        assert section["provider"] == "massive"
        assert len(section["quarters"]) == 8
        periods = [row["period_end"] for row in section["quarters"]]
        assert periods == sorted(periods, reverse=True)
        latest = section["quarters"][0]
        assert {"income", "balance", "cash-flow"} == set(latest["statements"])
        assert latest["revenue"] > 0
        assert latest["gross_margin"] == pytest.approx(0.75)
        assert latest["fcf"] == pytest.approx(latest["cash_from_operations"] - abs(latest["capex"]))

    def test_ratios_endpoint_is_queried_with_the_singular_ticker_key(self) -> None:
        """Massive's ratios endpoint ignores ``tickers`` and returns the market."""
        client = FakePrismMarketClient()

        fundamentals_module.build_fundamentals(client, "NVDA")

        ratio_call = next(
            call for call in client.financial_params if call["statement"] == "ratios"
        )
        assert ratio_call["ticker"] == "NVDA"

    def test_derived_ratios_beat_provider_ratios_and_record_their_source(self) -> None:
        client = FakePrismMarketClient()

        section = fundamentals_module.build_fundamentals(client, "NVDA", current_price=220.0)

        ratios = section["ratios"]
        ttm = ratios["ttm"]
        assert ratios["pe"] == pytest.approx(220.0 / ttm["eps"])
        assert section["ratios_source"]["pe"] == "derived_from_statements"
        # Nothing in the statements gives a dividend yield, so the vendor supplies it.
        assert ratios["dividend_yield"] == pytest.approx(0.0013)
        assert section["ratios_source"]["dividend_yield"] == "massive_ratios"

    def test_growth_refuses_a_percentage_off_a_negative_base(self) -> None:
        quarters = [
            {"period_end": "2026-06-30", "revenue": 100.0, "net_income": 10.0},
            {"period_end": "2026-03-31", "revenue": 95.0, "net_income": 8.0},
            {"period_end": "2025-12-31", "revenue": 90.0, "net_income": 5.0},
            {"period_end": "2025-09-30", "revenue": 88.0, "net_income": 1.0},
            {"period_end": "2025-06-30", "revenue": 80.0, "net_income": -20.0},
        ]

        growth = fundamentals_module.growth_metrics(quarters)

        assert growth["revenue_yoy"] == pytest.approx(0.25)
        assert growth["net_income_yoy"] is None

    def test_stage_reads_turnaround_when_losses_become_profits(self) -> None:
        quarters = [
            {"period_end": "2026-06-30", "revenue": 100.0, "net_income": 10.0,
             "operating_margin": 0.10, "gross_margin": 0.4},
            *[
                {"period_end": f"2026-0{index}-30", "revenue": 95.0, "net_income": 5.0}
                for index in (3, 2, 1)
            ],
            {"period_end": "2025-06-30", "revenue": 80.0, "net_income": -20.0,
             "operating_margin": -0.10, "gross_margin": 0.35},
        ]
        growth = fundamentals_module.growth_metrics(quarters)

        stage = fundamentals_module.classify_stage(quarters, growth)

        assert stage["label"] == "turnaround"
        assert any("loss to a profit" in item for item in stage["evidence"])

    def test_forecast_needs_six_quarters_and_carries_a_seasonal_factor(self) -> None:
        client = FakePrismMarketClient()
        section = fundamentals_module.build_fundamentals(client, "NVDA")

        forecast = section["forecast"]
        assert forecast["error"] is None
        assert len(forecast["next_4q"]) == 4
        assert forecast["method"] == "linear+seasonal"
        assert all(row["revenue"] > 0 for row in forecast["next_4q"])

        short = fundamentals_module.forecast_next_quarters(section["quarters"][:3])
        assert short["next_4q"] == []
        assert "at least 6 quarters" in short["error"]

    def test_dead_endpoints_leave_an_empty_section_with_reasons(self) -> None:
        class DeadClient(FakePrismMarketClient):
            def get_financials(self, ticker: str, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG001, ARG002
                raise RuntimeError("no financials entitlement")

        section = fundamentals_module.build_fundamentals(DeadClient(), "NVDA")

        assert section["quarters"] == []
        assert any("no financials entitlement" in error for error in section["errors"])
        assert section["ratios"]["pe"] is None
        assert section["stage"]["label"] == "unknown"


# ---------------------------------------------------------------------------
# filings
# ---------------------------------------------------------------------------


class TestFilings:
    def test_walks_the_index_for_two_annuals_and_three_quarterlies(self) -> None:
        sec = FakeSecClient()

        section = filings_module.build_filings(sec, "NVDA", text_generator=None)

        assert [row["form"] for row in section["ten_k"]] == ["10-K", "10-K"]
        assert [row["form"] for row in section["ten_q"]] == ["10-Q", "10-Q", "10-Q"]
        assert len(sec.fetched) == 5
        assert all(url.startswith("https://www.sec.gov/Archives/") for url in sec.fetched)

    def test_sections_are_extracted_at_the_larger_prism_bound(self) -> None:
        sec = FakeSecClient()

        section = filings_module.build_filings(sec, "NVDA")

        sections = section["ten_k"][0]["sections"]
        assert set(sections) == {"business", "risk_factors", "mdna"}
        # app.sec's one-page brief trims to 1800 characters; Prism keeps far more.
        assert section["ten_k"][0]["section_chars"]["business"] > 1800

    def test_model_synthesis_is_parsed_from_json(self) -> None:
        payload = {
            "performance": "Revenue rose across all five filings.",
            "risks": "Concentration risk is repeated in every filing.",
            "growth_opportunities": "Data centre expansion.",
            "new_business_lines": "Networking.",
            "operating_context": "Supply constrained.",
            "capex_suppliers_customers": "Capex rising; foundry partner concentration.",
        }
        generator = FakeTextGenerator(f"```json\n{json.dumps(payload)}\n```")

        section = filings_module.build_filings(FakeSecClient(), "NVDA", text_generator=generator)

        assert section["synthesis"]["method"] == "model"
        assert section["synthesis"]["performance"] == payload["performance"]
        assert section["ten_k"][0]["summary_method"] == "model"

    def test_unparseable_synthesis_falls_back_to_filing_excerpts(self) -> None:
        section = filings_module.build_filings(
            FakeSecClient(), "NVDA", text_generator=FakeTextGenerator("not json at all")
        )

        assert section["synthesis"]["method"] == "deterministic_excerpt"
        assert "parseable JSON" in section["synthesis"]["error"]
        assert section["synthesis"]["performance"]

    def test_quarterly_items_use_the_10_q_numbering(self) -> None:
        """A 10-Q puts MD&A at Item 2 and risk factors in Part II, after a
        cross-reference that the annual patterns would swallow whole."""
        document = (
            "<html><body>"
            "PART I Item 1. Financial Statements "
            + "Balance sheet line items and notes. " * 60
            + ("See Item 1A. Risk Factors for additional information about our "
               "investments. ") * 5
            + "Item 2. Management's Discussion and Analysis of Financial Condition "
            + "and Results of Operations Revenue rose on data centre demand. " * 40
            + "Item 3. Quantitative and Qualitative Disclosures About Market Risk "
            + "Item 4. Controls and Procedures "
            + "PART II Item 1. Legal Proceedings "
            + "Item 1A. Risk Factors "
            + "Other than the risk factors listed below there have been no "
            "material changes to our disclosed risks. " * 40
            + "Item 2. Unregistered Sales of Equity Securities "
            "</body></html>"
        )

        sections = filings_module.extract_sections(document, form="10-Q")

        assert set(sections) == {"risk_factors", "mdna"}
        assert sections["risk_factors"].startswith("Item 1A. Risk Factors Other than")
        assert "Unregistered Sales" not in sections["risk_factors"]
        assert sections["mdna"].startswith("Item 2. Management's Discussion")
        assert "Quantitative and Qualitative" not in sections["mdna"]

    def test_annual_items_still_use_the_10_k_numbering(self) -> None:
        sections = filings_module.extract_sections(FakeSecClient.DOCUMENT, form="10-K")

        assert set(sections) == {"business", "risk_factors", "mdna"}
        assert sections["mdna"].startswith("Item 7. Management's Discussion")

    def test_no_sec_client_is_reported_not_raised(self) -> None:
        section = filings_module.build_filings(None, "NVDA")

        assert section["ten_k"] == []
        assert section["errors"] == ["SEC client is not configured"]

    def test_a_dead_document_costs_one_filing_not_the_section(self) -> None:
        class PartlyDeadSec(FakeSecClient):
            def fetch_text(self, url: str) -> str:
                if url.endswith("doc2.htm"):
                    raise RuntimeError("404 from EDGAR")
                return super().fetch_text(url)

        section = filings_module.build_filings(PartlyDeadSec(), "NVDA")

        assert any("404 from EDGAR" in error for error in section["errors"])
        assert any(row["sections"] for row in section["ten_q"])


# ---------------------------------------------------------------------------
# news
# ---------------------------------------------------------------------------


class TestNews:
    def test_six_categories_are_searched_and_logged(self) -> None:
        exa = FakeExaClient()
        client = FakePrismMarketClient()

        section = news_module.build_news(
            exa, "NVDA", company_name="NVDA Corp", industry="semiconductors", market_client=client
        )

        categories = {row["category"] for row in section["query_log"]}
        assert set(news_module.CATEGORIES) <= categories
        assert len(exa.queries) == len(news_module.CATEGORIES)
        assert section["items"]
        assert "massive" in section["providers"]

    def test_urls_are_deduplicated_across_providers(self) -> None:
        class RepeatingExa(FakeExaClient):
            def search(self, query: str, **kwargs: Any) -> list[FakeExaResult]:  # noqa: ARG001, ARG002
                self.queries.append(query)
                result = FakeExaResult(0)
                result.url = "https://example.com/story/0?utm_source=x"
                return [result, FakeExaResult(0)]

        section = news_module.build_news(RepeatingExa(), "NVDA", market_client=None)

        urls = [item["url"] for item in section["items"]]
        assert len(urls) == len(set(urls))
        assert len(urls) == 1

    def test_one_failing_bucket_does_not_lose_the_others(self) -> None:
        section = news_module.build_news(
            FakeExaClient(fail_on="Federal Reserve"), "NVDA", market_client=None
        )

        failed = [row for row in section["query_log"] if row["error"]]
        assert len(failed) == 1
        assert failed[0]["category"] == "policy"
        assert section["items"]

    def test_massive_round_ups_are_filtered_out_and_the_feed_is_bounded(self) -> None:
        """Massive paginates the whole feed; most of it merely mentions the symbol."""

        class WideFeedClient(FakePrismMarketClient):
            def get_news(
                self, ticker: str, *, params: dict[str, Any] | None = None  # noqa: ARG002
            ) -> dict[str, Any]:
                rows = [
                    {
                        "title": f"Round-up {index}",
                        "article_url": f"https://news.example.com/roundup-{index}",
                        "published_utc": "2026-08-30T12:00:00Z",
                        "publisher": {"name": "Wire"},
                        "tickers": ["AAPL", "MSFT", "AMZN", "META", "GOOG", "AMD", ticker],
                    }
                    for index in range(50)
                ]
                rows += [
                    {
                        "title": f"About the company {index}",
                        "article_url": f"https://news.example.com/company-{index}",
                        "published_utc": "2026-08-31T12:00:00Z",
                        "publisher": {"name": "Wire"},
                        "tickers": [ticker],
                    }
                    for index in range(30)
                ]
                return {"results": rows}

        section = news_module.build_news(None, "NVDA", market_client=WideFeedClient())

        assert len(section["items"]) == news_module.MASSIVE_MAX_ITEMS
        assert all("Round-up" not in item["title"] for item in section["items"])
        massive_log = next(
            row for row in section["query_log"] if row["provider"] == "massive"
        )
        assert massive_log["n_returned"] == 80
        assert massive_log["n_results"] == news_module.MASSIVE_MAX_ITEMS

    def test_missing_exa_key_is_stated_rather_than_hidden(self) -> None:
        section = news_module.build_news(None, "NVDA", market_client=FakePrismMarketClient())

        assert "Exa client is not configured" in section["errors"]
        assert all(item["provider"] == "massive" for item in section["items"])


# ---------------------------------------------------------------------------
# volatility
# ---------------------------------------------------------------------------


class TestVolatility:
    def _close(self, rows: int = 800) -> pd.Series:
        frame = _ohlcv("NVDA", rows=rows, seed=7)
        return frame["Adj Close"]

    def test_realized_windows_carry_a_percentile_and_a_count(self) -> None:
        realized = volatility_module.realized_volatility(self._close())

        assert set(realized) == {"1m", "3m", "6m", "1y"}
        assert realized["1y"]["n"] == 252
        assert 0.0 <= realized["1y"]["percentile"] <= 1.0
        assert realized["1y"]["annualized"] > 0

    def test_annualized_volatility_matches_an_independent_computation(self) -> None:
        close = self._close()
        returns = np.diff(np.log(close.to_numpy(dtype=float)))[-252:]
        expected = float(np.std(returns, ddof=1) * np.sqrt(252.0))

        realized = volatility_module.realized_volatility(close)

        assert realized["1y"]["annualized"] == pytest.approx(expected, rel=1e-9)

    def test_nearest_monthly_expiry_skips_the_weeklies(self) -> None:
        import datetime

        expirations = ["2026-09-04", "2026-09-11", "2026-09-18", "2026-10-16"]

        chosen = volatility_module.nearest_monthly_expiry(
            expirations, as_of=datetime.date(2026, 9, 1)
        )

        assert chosen == "2026-09-18"
        assert volatility_module.third_friday(2026, 9).isoformat() == "2026-09-18"

    def test_implied_block_reports_atm_iv_and_a_reachable_skew(self) -> None:
        import datetime

        section = volatility_module.build_volatility(
            self._close(),
            client=FakePrismMarketClient(),
            ticker="NVDA",
            as_of=datetime.date(2026, 9, 1),
        )

        implied = section["implied"]
        assert implied["expiry"] == "2026-09-18"
        assert implied["expiry_kind"] == "monthly"
        assert implied["atm_iv"] == pytest.approx(0.43, abs=0.01)
        assert implied["skew_25d"] is not None
        assert implied["skew_detail"]["reason"] is None
        assert section["variance_risk_premium"] is not None

    def test_skew_is_refused_when_no_wing_is_near_25_delta(self) -> None:
        points = [
            {"strike": 200.0, "iv": 0.5, "type": "put", "delta": -0.45},
            {"strike": 240.0, "iv": 0.4, "type": "call", "delta": 0.44},
        ]

        skew = volatility_module.delta_skew(points)

        assert skew["skew_25d"] is None
        assert "further than" in skew["reason"]

    def test_missing_option_entitlement_keeps_the_realized_block(self) -> None:
        section = volatility_module.build_volatility(
            self._close(), client=FakePrismMarketClient(fail_options=True), ticker="NVDA"
        )

        assert section["realized"]["1y"]["annualized"] > 0
        assert "option chain unavailable" in section["implied_error"]

    def test_regime_averages_are_grouped_by_the_supplied_labels(self) -> None:
        close = self._close()
        labels = pd.Series(
            ["bull" if index % 2 else "bear" for index in range(close.shape[0])],
            index=close.index,
        )

        section = volatility_module.build_volatility(close, regime_labels=labels)

        assert set(section["regime_avg"]) == {"bull", "bear"}
        assert section["regime_avg"]["bull"]["n_days"] > 0


# ---------------------------------------------------------------------------
# levels
# ---------------------------------------------------------------------------


class TestLevels:
    def _history(self) -> HistoryResult:
        return FakePrismMarketClient().get_history("NVDA", period="1y", interval="1d")

    def test_every_level_family_is_extracted_as_numbers(self) -> None:
        section = levels_module.build_levels(self._history(), period="1y")

        assert section["auction"]["poc"] > 0
        assert section["auction"]["val"] <= section["auction"]["vah"]
        assert section["regression"]["ema200"] is not None
        assert section["ridge"]["major_ma"] is not None
        assert section["extremes"]["high_52w"] >= section["extremes"]["low_52w"]
        assert section["errors"] == []

    def test_key_levels_are_ranked_by_distance_from_the_last_price(self) -> None:
        history = self._history()
        price = float(history.data["Close"].iloc[-1])

        section = levels_module.build_levels(history, period="1y", current_price=price)

        distances = [abs(row["distance_pct"]) for row in section["key_levels"]]
        assert distances == sorted(distances)
        assert {row["kind"] for row in section["key_levels"]} <= {
            "support",
            "resistance",
            "magnet",
            "trend",
        }

    def test_one_broken_builder_leaves_the_other_levels_standing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def explode(*_: Any, **__: Any) -> dict[str, Any]:
            raise RuntimeError("torque needs SEC data")

        monkeypatch.setattr(levels_module, "torque_levels", explode)

        section = levels_module.build_levels(self._history(), period="1y")

        assert section["torque"] is None
        assert section["errors"][0]["source"] == "levels.torque"
        assert section["auction"] is not None


# ---------------------------------------------------------------------------
# memo
# ---------------------------------------------------------------------------


def _packet_with_scenarios(**overrides: Any) -> dict[str, Any]:
    from app.prism.contract import empty_packet

    packet = empty_packet("NVDA", as_of="2026-09-01")
    packet["profile"] = {"name": "NVDA Corp", "sector": "Technology", "industry": "Semis"}
    packet["profile_error"] = None
    packet["scenarios"] = {
        "probability_horizon": "3m",
        "weights": {"seasonality": 0.4, "regime": 0.6},
        "components": {
            "seasonality": {
                "available": True,
                "basis": "calendar month",
                "expected_return": {"6m": 0.05},
            },
            "regime": {
                "available": True,
                "basis": "regime conditional",
                "expected_return": {"6m": 0.08},
            },
        },
        "cases": {
            "bull": {
                "probability": 0.55,
                "narrative": "bull",
                "horizons": {
                    "3m": {"probability": 0.55, "p50": 0.10, "price_p50": 240.0},
                    "6m": {"probability": 0.55, "p50": 0.16, "price_p50": 255.0},
                    "12m": {"probability": 0.55, "p50": 0.25, "price_p50": 275.0},
                },
            },
            "bear": {
                "probability": 0.15,
                "narrative": "bear",
                "horizons": {"3m": {"probability": 0.15, "p50": -0.09, "price_p50": 200.0}},
            },
            "neutral": {"probability": 0.30, "narrative": "neutral", "horizons": {}},
        },
        "entry": {
            "bargain_below": 205.0,
            "fair_value": 230.0,
            "expensive_above": 255.0,
            "current_price": 220.0,
            "current_vs_fair": -0.043,
        },
        "timing": {"this_month": "good", "reason": "September has been positive 7 of 10 years"},
        "watch_signals": [
            {"symbol": "SOXX", "condition": "breaks trend", "implication": "risk off"}
        ],
    }
    packet["scenarios_error"] = None
    packet["entropy"] = {"windows": {"3m": {"H": 0.30, "classification": "structure", "n": 63}}}
    packet["entropy_error"] = None
    packet["regimes"] = {
        "current": {"label": "bull", "days_in_regime": 41, "switch_confidence": 0.72},
    }
    packet["regimes_error"] = None
    packet["seasonality"] = {
        "month_label": "September",
        "ticker": {"this_month": {"10y": {"mean": 0.031, "n": 10, "hit_rate": 0.7}}},
    }
    packet["seasonality_error"] = None
    packet["fundamentals"] = {
        "provider": "massive",
        "growth": {"revenue_yoy": 0.42, "margin_trend": "expanding"},
        "stage": {"label": "growing"},
        "ratios": {"pe": 27.6, "ev_ebitda": 26.5},
        "forecast": {"implied_revenue_growth": 0.30},
    }
    packet["fundamentals_error"] = None
    packet.update(overrides)
    return packet


class TestMemo:
    def test_recommendation_is_derived_from_the_probability_edge_and_value_gap(self) -> None:
        packet = _packet_with_scenarios()

        derived = memo_module.derive_recommendation(packet)

        # edge 0.55 - 0.15 = 0.40; value gap 230/220 - 1 = +0.0455 -> score 0.4455
        assert derived["basis"]["probability_edge"] == pytest.approx(0.40)
        assert derived["basis"]["value_gap"] == pytest.approx(0.0455, abs=1e-3)
        assert derived["score"] == pytest.approx(0.4455, abs=1e-3)
        assert derived["action"] == "strong_buy"
        assert 0.0 <= derived["conviction"] <= 1.0

    def test_conviction_is_cut_when_returns_are_classified_as_noise(self) -> None:
        structured = memo_module.derive_recommendation(_packet_with_scenarios())
        noisy_packet = _packet_with_scenarios()
        noisy_packet["entropy"] = {"windows": {"3m": {"H": 0.85, "classification": "noise"}}}

        noisy = memo_module.derive_recommendation(noisy_packet)

        assert noisy["conviction"] < structured["conviction"]

    def test_targets_come_from_the_case_price_paths(self) -> None:
        targets = memo_module.derive_targets(_packet_with_scenarios())

        assert targets["entry_price"] == 205.0
        assert targets["stop_or_reassess"] == 200.0
        assert [row["horizon"] for row in targets["exit_targets"]] == ["3m", "6m", "12m"]
        assert targets["exit_targets"][0]["price"] == 240.0

    def test_projection_is_bounded_and_names_what_failed(self) -> None:
        packet = _packet_with_scenarios()
        packet["macro"] = None
        packet["macro_error"] = "FRED_API_KEY is not configured"
        packet["meta"]["errors"].append({"source": "macro", "error": "no key"})

        briefing = memo_module.project_packet(packet, max_chars=1200)

        assert len(briefing) <= 1200
        assert "truncated" in briefing

        full = memo_module.project_packet(packet)
        assert "FRED_API_KEY is not configured" in full
        assert "What the engine could NOT compute" in full

    def test_citations_are_numbered_and_reference_real_sections(self) -> None:
        citations = memo_module.build_citations(_packet_with_scenarios())

        assert [row["id"] for row in citations] == [
            f"C{index}" for index in range(1, len(citations) + 1)
        ]
        assert any("seasonality" in row["source"] for row in citations)

    def test_model_json_is_used_and_the_disclaimer_is_enforced(self) -> None:
        reply = {
            "action": "buy",
            "strength": "strong",
            "conviction": 0.8,
            "one_line": "Constructive into September.",
            "entry_price": 210.0,
            "exit_targets": [{"horizon": "6m", "price": 260.0, "probability": 0.5}],
            "stop_or_reassess": 198.0,
            "key_determinants": [
                {
                    "name": "regime",
                    "explanation": "bull",
                    "direction": "bullish",
                    "weight": 0.6,
                }
            ],
            "priced_in": ["The AI capex cycle."],
            "citation_ids": ["C1", "C99"],
            "text": "# NVDA\n\nThe case is constructive [C1].",
        }
        generator = FakeTextGenerator(json.dumps(reply))

        memo = memo_module.build_memo(_packet_with_scenarios(), text_generator=generator)

        assert memo["method"] == "model"
        assert memo["recommendation"]["action"] == "buy"
        assert memo["recommendation"]["strength"] == "strong"
        assert memo["entry_price"] == 210.0
        assert memo["citation_ids_used"] == ["C1"]  # C99 is not a real citation id
        assert memo["text"].endswith(memo_module.DISCLAIMER)

    def test_two_block_reply_keeps_the_markdown_out_of_the_json(self) -> None:
        reply = (
            "<PRISM_JSON>\n"
            + json.dumps(
                {
                    "action": "buy",
                    "strength": "normal",
                    "conviction": 0.5,
                    "one_line": "Constructive.",
                    "citation_ids": ["C2"],
                }
            )
            + "\n</PRISM_JSON>\n<PRISM_MEMO>\n# NVDA\n\nRegime is bull [C2].\n"
            '\nA "quoted" line and a stray \\ backslash would break JSON escaping.\n'
            "</PRISM_MEMO>"
        )

        memo = memo_module.build_memo(
            _packet_with_scenarios(), text_generator=FakeTextGenerator(reply)
        )

        assert memo["method"] == "model"
        assert memo["truncated"] is False
        assert memo["text"].startswith("# NVDA")
        assert "backslash" in memo["text"]
        assert memo["citation_ids_used"] == ["C2"]

    def test_a_truncated_reply_keeps_the_fields_and_the_partial_memo(self) -> None:
        reply = (
            "<PRISM_JSON>\n"
            + json.dumps({"action": "sell", "strength": "weak", "conviction": 0.2})
            + "\n</PRISM_JSON>\n<PRISM_MEMO>\n# NVDA\n\nThe memo stops mid-sen"
        )

        memo = memo_module.build_memo(
            _packet_with_scenarios(), text_generator=FakeTextGenerator(reply)
        )

        assert memo["method"] == "model"
        assert memo["truncated"] is True
        assert memo["recommendation"]["action"] == "sell"
        assert memo["text"].startswith("# NVDA")

    def test_invented_citation_ids_are_reported_not_accepted(self) -> None:
        reply = (
            "<PRISM_JSON>\n"
            + json.dumps({"action": "hold", "strength": "weak", "conviction": 0.1})
            + "\n</PRISM_JSON>\n<PRISM_MEMO>\nRegime is bull [C_regime] and "
            "seasonality is soft [C1] and [C99].\n</PRISM_MEMO>"
        )

        memo = memo_module.build_memo(
            _packet_with_scenarios(), text_generator=FakeTextGenerator(reply)
        )

        assert memo["citation_ids_used"] == ["C1"]
        assert memo["unknown_citation_ids"] == ["C99", "C_regime"]

    def test_an_invalid_action_falls_back_to_the_derived_one(self) -> None:
        generator = FakeTextGenerator(
            json.dumps({"action": "moon", "strength": "epic", "text": "# memo\n\nbody"})
        )

        memo = memo_module.build_memo(_packet_with_scenarios(), text_generator=generator)

        derived = memo_module.derive_recommendation(_packet_with_scenarios())
        assert memo["recommendation"]["action"] == derived["action"]
        assert memo["recommendation"]["strength"] == derived["strength"]

    def test_no_key_still_produces_a_complete_memo(self) -> None:
        memo = memo_module.build_memo(_packet_with_scenarios(), text_generator=None, api_key=None)

        assert memo["method"] == "deterministic"
        assert "ANTHROPIC_API_KEY" in memo["reason"]
        assert "## Recommendation" in memo["text"]
        assert "## Citations" in memo["text"]
        assert memo["text"].rstrip().endswith(memo_module.DISCLAIMER)

    def test_a_model_outage_degrades_to_the_deterministic_memo(self) -> None:
        memo = memo_module.build_memo(
            _packet_with_scenarios(), text_generator=ExplodingTextGenerator()
        )

        assert memo["method"] == "deterministic"
        assert "model is offline" in memo["reason"]
        assert memo["recommendation"]["action"] in memo_module.ACTIONS


# ---------------------------------------------------------------------------
# chat
# ---------------------------------------------------------------------------


class TestChat:
    def test_reply_is_persisted_with_the_citations_it_used(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        packet = _packet_with_scenarios()
        generator = FakeTextGenerator("The regime is bull [C2].")

        result = chat_module.chat_turn(
            packet, [], "What regime are we in?", text_generator=generator, store=isolated_store
        )

        assert result["method"] == "model"
        assert [row["id"] for row in result["citations"]] == ["C2"]
        stored = isolated_store.chat_history(result["conversation_id"])
        assert [row["role"] for row in stored] == ["user", "assistant"]
        assert stored[1]["content"] == "The regime is bull [C2]."

    def test_history_is_loaded_from_the_store_when_only_an_id_is_given(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        packet = _packet_with_scenarios()
        first = chat_module.chat_turn(
            packet, [], "First question?", text_generator=FakeTextGenerator("First answer."),
            store=isolated_store,
        )
        generator = FakeTextGenerator("Second answer.")

        chat_module.chat_turn(
            packet,
            [],
            "And the follow up?",
            text_generator=generator,
            store=isolated_store,
            conversation_id=first["conversation_id"],
        )

        assert "First question?" in generator.calls[0]["prompt"]

    def test_system_prompt_carries_the_packet_briefing(self) -> None:
        prompt = chat_module.build_system_prompt(_packet_with_scenarios())

        assert "Prism briefing: NVDA" in prompt
        assert "Citations you may reference by id" in prompt

    def test_offline_chat_returns_the_stored_read_not_an_error(self) -> None:
        packet = _packet_with_scenarios()
        packet["memo"] = {
            "recommendation": {"action": "buy", "conviction": 0.6, "one_line": "Constructive."}
        }
        packet["memo_error"] = None

        result = chat_module.chat_turn(packet, [], "Why buy?", persist=False)

        assert result["method"] == "deterministic"
        assert "buy" in result["reply"]
        assert memo_module.DISCLAIMER in result["reply"]

    def test_empty_message_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="message is required"):
            chat_module.chat_turn(_packet_with_scenarios(), [], "   ")


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


class TestStore:
    def test_packet_round_trips_through_the_local_tier(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        packet = _packet_with_scenarios()

        record = isolated_store.save_packet(packet)

        assert record["errors"] == []
        assert Path(record["local_path"]).is_file()
        loaded = isolated_store.load_packet("NVDA")
        assert loaded is not None
        assert loaded["ticker"] == "NVDA"
        assert isolated_store.list_packets("NVDA") == ["2026-09-01"]

    def test_missing_packet_reads_as_none(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        assert isolated_store.load_packet("ZZZZ") is None

    def test_supabase_failures_are_recorded_not_raised(
        self, tmp_path: Path
    ) -> None:
        class BrokenSupabase:
            def upsert_packet(self, row: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ARG001, ARG002
                raise store_module.PrismStoreError("Supabase prism_packets POST failed: 500")

        store = store_module.PrismStore(base_dir=tmp_path, supabase=BrokenSupabase())

        record = store.save_packet(_packet_with_scenarios())

        assert Path(record["local_path"]).is_file()
        assert any("supabase write failed" in error for error in record["errors"])

    def test_chat_role_is_validated(self, isolated_store: store_module.PrismStore) -> None:
        with pytest.raises(store_module.PrismStoreError):
            isolated_store.append_chat(
                conversation_id="c1", ticker="NVDA", role="system", content="x"
            )


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------


class TestExport:
    def test_text_export_covers_every_section_heading(self) -> None:
        text = export_module.to_text(_packet_with_scenarios())

        for heading in (
            "PRISM MEMO - NVDA",
            "RECOMMENDATION",
            "SEASONALITY",
            "MACRO",
            "SCENARIOS",
            "BUILD REPORT",
        ):
            assert heading in text
        assert "unavailable" in text  # macro was never built on this packet

    def test_json_export_is_parseable_and_complete(self) -> None:
        packet = _packet_with_scenarios()

        parsed = json.loads(export_module.to_json(packet))

        assert parsed["ticker"] == "NVDA"
        assert parsed["scenarios"]["cases"]["bull"]["probability"] == 0.55

    def test_pdf_export_returns_a_real_pdf(self) -> None:
        packet = _packet_with_scenarios()
        packet["memo"] = memo_module.build_memo(packet, text_generator=None, api_key=None)
        packet["memo_error"] = None

        body, content_type, filename = export_module.export_packet(packet, "pdf")

        assert body[:5] == b"%PDF-"
        assert content_type == "application/pdf"
        assert filename == "prism-NVDA-2026-09-01.pdf"

    def test_unknown_format_is_refused(self) -> None:
        with pytest.raises(export_module.PrismExportError, match="format must be one of"):
            export_module.export_packet(_packet_with_scenarios(), "docx")


# ---------------------------------------------------------------------------
# engine
# ---------------------------------------------------------------------------


class TestEngine:
    def test_full_offline_build_populates_every_section(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        from app.prism.contract import PACKET_KEYS, validate_packet

        packet = engine_module.build_prism_packet(
            FakePrismMarketClient(),
            "nvda",
            sec_client=FakeSecClient(),
            exa_client=FakeExaClient(),
            text_generator=FakeTextGenerator(
                json.dumps({"action": "buy", "strength": "normal", "conviction": 0.6,
                            "one_line": "ok", "text": "# NVDA\n\nBody [C1]."})
            ),
            as_of="2026-09-01",
            years=3,
            force=True,
            store=isolated_store,
        )

        assert validate_packet(packet) == []
        assert set(PACKET_KEYS) <= set(packet)
        assert packet["ticker"] == "NVDA"
        for section in (
            "profile",
            "seasonality",
            "macro",
            "relational",
            "factors",
            "regimes",
            "entropy",
            "spectral",
            "eigen",
            "fundamentals",
            "filings",
            "volatility",
            "levels",
            "news",
            "scenarios",
            "recent",
            "memo",
        ):
            assert packet[section] is not None, (section, packet[f"{section}_error"])
        assert packet["memo"]["recommendation"]["action"] in memo_module.ACTIONS
        assert packet["meta"]["timings_ms"]["total"] > 0
        assert isolated_store.load_packet("NVDA") is not None

    def test_a_dead_source_costs_one_section_not_the_packet(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        class NoOptionsClient(FakePrismMarketClient):
            def get_expirations(self, ticker: str) -> list[str]:  # noqa: ARG001, ARG002
                raise RuntimeError("no options entitlement")

            def get_option_chain(self, ticker: str, expiry: str | None = None) -> OptionChainResult:  # noqa: ARG001, ARG002
                raise RuntimeError("no options entitlement")

        packet = engine_module.build_prism_packet(
            NoOptionsClient(),
            "NVDA",
            sec_client=None,
            exa_client=None,
            as_of="2026-09-01",
            years=3,
            force=True,
            include_memo=False,
            store=isolated_store,
        )

        assert packet["volatility"] is not None
        assert packet["volatility"]["implied_error"]
        assert packet["filings"]["errors"] == ["SEC client is not configured"]
        assert packet["memo"] is None
        assert packet["memo_error"] == "include_memo=False"
        assert packet["scenarios"] is not None

    def test_a_second_build_reads_the_stored_packet_unless_forced(
        self, isolated_store: store_module.PrismStore
    ) -> None:
        client = FakePrismMarketClient()
        first = engine_module.build_prism_packet(
            client, "NVDA", as_of="2026-09-01", years=3, force=True,
            include_memo=False, store=isolated_store,
        )
        calls_after_first = len(client.calls)

        second = engine_module.build_prism_packet(
            client, "NVDA", as_of="2026-09-01", years=3, include_memo=False, store=isolated_store
        )

        assert second["generated_at"] == first["generated_at"]
        assert second["meta"]["cache"]["packet"] == "hit"
        assert len(client.calls) == calls_after_first

    def test_summary_projection_is_small_and_names_missing_sections(self) -> None:
        packet = _packet_with_scenarios()
        packet["memo"] = memo_module.build_memo(packet, text_generator=None, api_key=None)
        packet["memo_error"] = None

        summary = engine_module.prism_summary(packet)

        assert summary["ticker"] == "NVDA"
        assert summary["recommendation"]["action"] in memo_module.ACTIONS
        assert "macro" in summary["unavailable_sections"]
        assert len(json.dumps(summary, default=str)) < 30_000
        assert summary["disclaimer"].startswith("Research only")

    def test_sector_is_inferred_from_the_sic_industry_and_flagged(self) -> None:
        profile = engine_module.build_profile(FakePrismMarketClient(), "NVDA")

        assert profile["sector"] == "Technology"
        assert profile["sector_inferred"] is True
        assert profile["related_etfs"][0] == "SOXX"

    def test_signal_prediction_history_never_peeks_at_the_future(self) -> None:
        index = pd.date_range("2018-01-31", periods=80, freq="ME")
        rng = np.random.default_rng(3)
        signals = pd.DataFrame({"alpha": rng.normal(0, 1, 80)}, index=index)
        target = pd.Series(rng.normal(0.01, 0.05, 80), index=index)

        predictions, realized = engine_module.signal_prediction_history(signals, target)

        assert not predictions.empty
        # The first prediction can only appear once three prior months have settled.
        first_valid = predictions["alpha"].first_valid_index()
        assert index.get_loc(first_valid) >= 3
        assert realized.index.equals(target.index[:-1])
