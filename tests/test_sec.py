from __future__ import annotations

from datetime import date
from typing import Any

import pytest

import app.sec as sec_module
from app.sec import SecClient, SecDataError, extract_earnings_sections, extract_filing_sections

TEST_USER_AGENT = "Test Company contact@example.com"


class FakeResponse:
    def __init__(
        self,
        *,
        payload: Any | None = None,
        text: str = "",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, *, block_sec: bool = False) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.block_sec = block_sec

    def get(self, url: str, *, headers: dict[str, str], timeout: int) -> FakeResponse:
        _ = timeout
        self.requests.append((url, headers))
        if self.block_sec and ("sec.gov" in url or "data.sec.gov" in url):
            return FakeResponse(status_code=403, text="blocked")
        if url == "https://www.sec.gov/files/company_tickers.json":
            return FakeResponse(
                payload={
                    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
                }
            )
        if url == "https://data.sec.gov/submissions/CIK0000320193.json":
            return FakeResponse(payload=submissions_payload())
        if url.endswith("/aapl-20250927.htm"):
            return FakeResponse(text=filing_html())
        if url.endswith("/aapl-8k.htm"):
            return FakeResponse(text=earnings_8k_html())
        if url == "https://cdn.yahoofinance.com/prod/sec-filings/aapl-20250927.htm":
            return FakeResponse(text=filing_html())
        if url == "https://cdn.yahoofinance.com/prod/sec-filings/aapl-8k.htm":
            return FakeResponse(text=earnings_8k_html())
        if url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json":
            return FakeResponse(payload=companyfacts_payload())
        return FakeResponse(status_code=404, text="missing")


def make_sec_client(session: Any, **options: Any) -> SecClient:
    return SecClient(
        session=session,
        user_agent=TEST_USER_AGENT,
        request_interval_seconds=0,
        max_retries=0,
        **options,
    )


def test_sec_client_builds_source_pack() -> None:
    session = FakeSession()
    client = make_sec_client(session)

    pack = client.get_source_pack("aapl")

    assert pack["Status"] == "available"
    assert pack["CIK"] == "0000320193"
    assert pack["Filings"]["10-K"]["filing_date"] == "2025-10-31"
    assert pack["Filing Sections"]["Business"]["Item"] == "Item 1"
    assert "sells products" in pack["Filing Sections"]["Business"]["Snippet"]
    assert pack["Earnings Sections"]["Earnings Release"]["Item"] == "Item 2.02"
    assert "record revenue" in pack["Earnings Sections"]["Earnings Release"]["Snippet"]
    assert pack["Company Facts"]["Revenue"]["Value"] == 391_035_000_000
    assert pack["Company Facts"]["Revenue"]["Concept"] == (
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    assert any(citation["Label"] == "SEC 10-K Item 1 Business" for citation in pack["Citations"])
    assert any(
        citation["Label"] == "SEC 8-K Item 2.02 Earnings Release"
        for citation in pack["Citations"]
    )
    assert session.requests[0][1]["User-Agent"] == TEST_USER_AGENT


def test_sec_client_raises_for_unknown_ticker() -> None:
    client = make_sec_client(FakeSession())

    with pytest.raises(SecDataError, match="No SEC CIK found"):
        client.cik_for_ticker("NOPE")


def test_sec_client_uses_yahoo_filing_fallback_when_sec_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeTicker:
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker

        def get_sec_filings(self) -> list[dict[str, Any]]:
            return [
                {
                    "date": date(2025, 10, 31),
                    "type": "10-K",
                    "title": "Annual report",
                    "edgarUrl": "https://finance.yahoo.com/sec-filing/AAPL/0000320193-25-000030_320193",
                    "exhibits": {
                        "10-K": "https://cdn.yahoofinance.com/prod/sec-filings/aapl-20250927.htm"
                    },
                },
                {
                    "date": date(2026, 1, 3),
                    "type": "8-K",
                    "title": "Current report",
                    "edgarUrl": "https://finance.yahoo.com/sec-filing/AAPL/0000320193-26-000010_320193",
                    "exhibits": {
                        "8-K": "https://cdn.yahoofinance.com/prod/sec-filings/aapl-8k.htm"
                    },
                },
            ]

    monkeypatch.setattr(sec_module.yf, "Ticker", FakeTicker)
    client = make_sec_client(FakeSession(block_sec=True))

    pack = client.get_source_pack("AAPL")

    assert pack["Status"] == "partial"
    assert pack["Provider"] == "Yahoo Finance SEC filings mirror"
    assert pack["CIK"] == "0000320193"
    assert pack["Filings"]["10-K"]["filing_date"] == "2025-10-31"
    assert "Business" in pack["Filing Sections"]
    assert "Earnings Release" in pack["Earnings Sections"]
    assert "SEC direct API was unavailable" in pack["Errors"][1]


def test_sec_client_caches_source_packs_and_url_payloads() -> None:
    session = FakeSession()
    client = make_sec_client(session)

    first = client.get_source_pack("AAPL")
    second = client.get_source_pack("AAPL")

    assert first == second
    assert first is not second
    assert len(session.requests) == 5


def test_sec_request_gate_is_shared_across_client_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class OpenSession:
        def __init__(self) -> None:
            self.requests: list[str] = []

        def get(self, url: str, *, headers: dict[str, str], timeout: int) -> FakeResponse:
            _ = headers, timeout
            self.requests.append(url)
            return FakeResponse(text="ok")

    now = 10.0
    sleeps: list[float] = []

    def fake_monotonic() -> float:
        return now

    def fake_sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(sec_module, "_SEC_REQUEST_GATE", sec_module.SecRequestGate())
    monkeypatch.setattr(sec_module.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(sec_module.time, "sleep", fake_sleep)

    first_session = OpenSession()
    second_session = OpenSession()
    first = SecClient(
        session=first_session,
        user_agent=TEST_USER_AGENT,
        request_interval_seconds=0.5,
        response_cache_seconds=0,
        max_retries=0,
    )
    second = SecClient(
        session=second_session,
        user_agent=TEST_USER_AGENT,
        request_interval_seconds=0.5,
        response_cache_seconds=0,
        max_retries=0,
    )

    first.fetch_text("https://www.sec.gov/one")
    second.fetch_text("https://www.sec.gov/two")

    assert sleeps == [0.5]
    assert first_session.requests == ["https://www.sec.gov/one"]
    assert second_session.requests == ["https://www.sec.gov/two"]


def test_sec_client_retries_rate_limits_with_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    class RateLimitedSession:
        def __init__(self) -> None:
            self.requests = 0

        def get(self, url: str, *, headers: dict[str, str], timeout: int) -> FakeResponse:
            _ = url, headers, timeout
            self.requests += 1
            if self.requests == 1:
                return FakeResponse(status_code=429, headers={"Retry-After": "1.5"})
            return FakeResponse(payload={"ok": True})

    sleeps: list[float] = []
    monkeypatch.setattr(sec_module.time, "sleep", sleeps.append)
    session = RateLimitedSession()
    client = SecClient(
        session=session,
        user_agent=TEST_USER_AGENT,
        request_interval_seconds=0,
        max_retries=1,
        backoff_max_seconds=2,
    )

    payload = client.fetch_json("https://data.sec.gov/example.json")

    assert payload == {"ok": True}
    assert session.requests == 2
    assert sleeps == [1.5]


def test_extract_filing_sections_uses_real_sections_not_table_of_contents() -> None:
    short_toc = "Item 1. Business Item 1A. Risk Factors Item 7. Management's Discussion"
    actual = filing_html()

    sections = extract_filing_sections(short_toc + actual)

    assert sections["Business"]["Snippet"].startswith("Item 1. Business Apple")
    assert sections["Risk Factors"]["Snippet"].startswith("Item 1A. Risk Factors")
    assert sections["MD&A"]["Snippet"].startswith("Item 7. Management's Discussion")


def test_extract_earnings_sections_from_8k_event_text() -> None:
    sections = extract_earnings_sections(earnings_8k_html())

    assert sections["Earnings Release"]["Item"] == "Item 2.02"
    assert "record revenue" in sections["Earnings Release"]["Snippet"]
    assert sections["Event Update"]["Item"] == "Item 7.01"


def submissions_payload() -> dict[str, Any]:
    return {
        "cik": "0000320193",
        "name": "Apple Inc.",
        "sic": "3571",
        "sicDescription": "Electronic Computers",
        "exchanges": ["Nasdaq"],
        "filings": {
            "recent": {
                "form": ["8-K", "10-Q", "10-K"],
                "accessionNumber": [
                    "0000320193-26-000010",
                    "0000320193-26-000020",
                    "0000320193-25-000030",
                ],
                "primaryDocument": ["aapl-8k.htm", "aapl-10q.htm", "aapl-20250927.htm"],
                "filingDate": ["2026-01-03", "2026-02-01", "2025-10-31"],
                "reportDate": ["2026-01-03", "2025-12-27", "2025-09-27"],
            }
        },
    }


def companyfacts_payload() -> dict[str, Any]:
    return {
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "label": "Revenue",
                    "description": "Amount of revenue recognized from contracts.",
                    "units": {
                        "USD": [
                            {
                                "val": 383_285_000_000,
                                "form": "10-K",
                                "filed": "2024-11-01",
                                "fy": 2024,
                                "fp": "FY",
                                "end": "2024-09-28",
                                "accn": "0000320193-24-000030",
                            },
                            {
                                "val": 391_035_000_000,
                                "form": "10-K",
                                "filed": "2025-10-31",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-09-27",
                                "accn": "0000320193-25-000030",
                            },
                        ]
                    },
                },
                "NetIncomeLoss": {
                    "label": "Net Income",
                    "units": {
                        "USD": [
                            {
                                "val": 93_736_000_000,
                                "form": "10-K",
                                "filed": "2025-10-31",
                                "fy": 2025,
                                "fp": "FY",
                                "end": "2025-09-27",
                            }
                        ]
                    },
                },
            }
        }
    }


def filing_html() -> str:
    business = " ".join(["Apple sells products and services through global channels."] * 20)
    risks = " ".join(["The company faces supply chain and demand risks."] * 20)
    mda = " ".join(["Management discusses revenue, margins, liquidity, and capital returns."] * 20)
    return f"""
    <html><body>
      <h1>Item 1. Business</h1>
      <p>{business}</p>
      <h1>Item 1A. Risk Factors</h1>
      <p>{risks}</p>
      <h1>Item 1B. Unresolved Staff Comments</h1>
      <p>None.</p>
      <h1>Item 7. Management's Discussion and Analysis</h1>
      <p>{mda}</p>
      <h1>Item 7A. Quantitative and Qualitative Disclosures</h1>
    </body></html>
    """


def earnings_8k_html() -> str:
    release = " ".join(
        [
            "Apple reported record revenue, expanding services margin, and higher diluted EPS."
        ]
        * 10
    )
    update = " ".join(
        [
            "Management will host an earnings call and provided a cautious demand outlook."
        ]
        * 8
    )
    return f"""
    <html><body>
      <h1>Item 2.02 Results of Operations and Financial Condition</h1>
      <p>{release}</p>
      <h1>Item 7.01 Regulation FD Disclosure</h1>
      <p>{update}</p>
      <h1>Item 9.01 Financial Statements and Exhibits</h1>
    </body></html>
    """
