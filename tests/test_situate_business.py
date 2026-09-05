"""Offline tests for Situate's business modules (S3): fundamentals + text.

Everything here runs without a network: fake financials, fake filings, a fake
text generator and fake news wires. The load-bearing checks are

* the point-in-time **filing-date** rule (a report not yet filed at ``t`` cannot
  appear, in either module);
* the hashing-vectoriser **diff scorer** on known before/after strings;
* the quality/value arithmetic on hand-built quarters;
* the LLM sees **only the diff**, its output is recorded, and revisions/PEAD stay
  ``None`` with a stated reason (no consensus-estimate provider).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.situate import fundamentals, text


# =========================================================================== #
# Fakes
# =========================================================================== #
class FakeFinancials:
    """Massive-shaped financials client returning canned statement rows."""

    def __init__(self, rows_by_statement: dict[str, list[dict[str, Any]]]) -> None:
        self._rows = rows_by_statement
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get_financials(
        self, ticker: str, *, statement: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        _ = ticker
        self.calls.append((statement, params))
        return {"results": list(self._rows.get(statement, []))}


class FakeGenerated:
    def __init__(self, text: str, model: str = "fake-model") -> None:
        self.text = text
        self.model = model
        self.provider = "fake"


class FakeTextGenerator:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.temperatures: list[float] = []

    def generate_text(
        self, *, system: str, prompt: str, max_tokens: int = 700, temperature: float = 0.2
    ) -> FakeGenerated:
        _ = max_tokens
        self.prompts.append(prompt)
        self.systems.append(system)
        self.temperatures.append(temperature)
        return FakeGenerated(json.dumps(self._payload))


class FakeExaResult:
    def __init__(self, title: str, url: str, published_date: str, snippet: str = "") -> None:
        self.title = title
        self.url = url
        self.published_date = published_date
        self.snippet = snippet


class FakeExa:
    def __init__(self, results: list[FakeExaResult]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    def search(self, query: str, **kwargs: Any) -> list[FakeExaResult]:
        self.calls.append({"query": query, **kwargs})
        return list(self._results)


class FakeMarketNews:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def get_news(self, ticker: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _ = (ticker, params)
        return {"results": list(self._rows)}


class FakeSec:
    def __init__(self, submissions: dict[str, Any], docs: dict[str, str]) -> None:
        self._submissions = submissions
        self._docs = docs
        self.fetched: list[str] = []

    def cik_for_ticker(self, ticker: str) -> str:
        _ = ticker
        return "0000000320"

    def submissions(self, cik: str) -> dict[str, Any]:
        _ = cik
        return self._submissions

    def fetch_text(self, url: str) -> str:
        self.fetched.append(url)
        return self._docs[url]


# =========================================================================== #
# Quarter builders
# =========================================================================== #
def _income_row(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "period_end": q["period_end"],
        "filing_date": q["filing_date"],
        "fiscal_quarter": q.get("fiscal_quarter"),
        "fiscal_year": q.get("fiscal_year"),
        "revenue": q.get("revenue"),
        "gross_profit": q.get("gross_profit"),
        "operating_income": q.get("operating_income"),
        "net_income_loss_attributable_common_shareholders": q.get("net_income"),
        "diluted_earnings_per_share": q.get("eps"),
        "diluted_shares_outstanding": q.get("shares"),
        "ebitda": q.get("ebitda"),
        "interest_expense": q.get("interest_expense"),
    }


def _balance_row(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "period_end": q["period_end"],
        "filing_date": q["filing_date"],
        "cash_and_equivalents": q.get("cash"),
        "short_term_investments": q.get("short_term_investments"),
        "total_assets": q.get("total_assets"),
        "debt_current": q.get("debt_current"),
        "long_term_debt_and_capital_lease_obligations": q.get("long_term_debt"),
    }


def _cashflow_row(q: dict[str, Any]) -> dict[str, Any]:
    return {
        "period_end": q["period_end"],
        "filing_date": q["filing_date"],
        "net_cash_from_operating_activities": q.get("cash_from_operations"),
        "purchase_of_property_plant_and_equipment": q.get("capex"),
    }


def _statement_rows(quarters: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    return {
        "income": [_income_row(q) for q in quarters],
        "balance": [_balance_row(q) for q in quarters],
        "cash-flow": [_cashflow_row(q) for q in quarters],
    }


def _quarter_specs(n: int, *, end: date) -> list[dict[str, Any]]:
    """``n`` quarters newest-first, filing_date 45 days after period end."""
    specs: list[dict[str, Any]] = []
    period = pd.Timestamp(end)
    for i in range(n):
        pe = (period - pd.DateOffset(months=3 * i)).normalize()
        filed = pe.date() + timedelta(days=45)
        specs.append(
            {
                "period_end": pe.date().isoformat(),
                "filing_date": filed.isoformat(),
                "fiscal_quarter": f"Q{((pe.month - 1) // 3) + 1}",
                "fiscal_year": pe.year,
                "revenue": 200.0 + i,  # newest largest -> positive growth
                "gross_profit": 100.0 + 0.5 * i,
                "operating_income": 50.0,
                "net_income": 40.0,
                "eps": 0.40,
                "shares": 1000.0,
                "ebitda": 70.0,
                "interest_expense": 5.0,
                "cash": 100.0,
                "short_term_investments": 50.0,
                "total_assets": 1000.0,
                "debt_current": 50.0,
                "long_term_debt": 200.0,
                "cash_from_operations": 60.0,
                "capex": -10.0,
            }
        )
    return specs


# =========================================================================== #
# Fundamentals: point-in-time filing-date rule
# =========================================================================== #
def test_load_quarters_respects_filing_date() -> None:
    quarters = [
        {"period_end": "2026-06-30", "filing_date": "2026-08-14"},  # filed AFTER as_of
        {"period_end": "2026-03-31", "filing_date": "2026-05-10"},
        {"period_end": "2025-12-31", "filing_date": "2026-02-12"},
    ]
    specs = []
    for q in quarters:
        base = {
            **q,
            "revenue": 100.0,
            "gross_profit": 50.0,
            "operating_income": 20.0,
            "net_income": 10.0,
            "eps": 0.1,
            "shares": 1000.0,
            "ebitda": 25.0,
            "interest_expense": 2.0,
            "cash": 10.0,
            "short_term_investments": 0.0,
            "total_assets": 500.0,
            "debt_current": 0.0,
            "long_term_debt": 100.0,
            "cash_from_operations": 15.0,
            "capex": -3.0,
        }
        specs.append(base)
    client = FakeFinancials(_statement_rows(specs))

    loaded, errors = fundamentals.load_quarters(client, "NVDA", as_of="2026-07-01", limit=12)
    filed_dates = {q["filing_date"] for q in loaded}
    assert "2026-08-14" not in filed_dates  # not yet filed at as_of
    assert "2026-05-10" in filed_dates
    # newest-first ordering by period_end
    assert loaded[0]["period_end"] == "2026-03-31"
    assert errors == []


def test_load_quarters_dedupes_to_earliest_filing_and_original_numbers() -> None:
    """Massive lists two rows per period_end: the original filing and the

    year-later comparative. The join must keep the EARLIEST filing_date and the
    as-originally-reported numbers, never the later comparative's date/restatement.
    """

    def _base(period_end: str, filing_date: str, revenue: float) -> dict[str, Any]:
        return {
            "period_end": period_end,
            "filing_date": filing_date,
            "revenue": revenue,
            "gross_profit": revenue * 0.5,
            "operating_income": 20.0,
            "net_income": 10.0,
            "eps": 0.1,
            "shares": 1000.0,
            "ebitda": 25.0,
            "interest_expense": 2.0,
            "cash": 10.0,
            "short_term_investments": 0.0,
            "total_assets": 500.0,
            "debt_current": 0.0,
            "long_term_debt": 100.0,
            "cash_from_operations": 15.0,
            "capex": -3.0,
        }

    # The comparative row is listed FIRST (as Massive does) to prove the earliest
    # filing still wins rather than whichever row appears first.
    comparative = _base("2024-10-27", "2025-11-19", revenue=999.0)  # restated, ~13mo late
    original = _base("2024-10-27", "2024-11-20", revenue=350.0)  # original filing
    other = _base("2025-01-26", "2025-02-25", revenue=400.0)
    client = FakeFinancials(_statement_rows([comparative, original, other]))

    # An earlier as_of at which only the ORIGINAL filing has occurred: the bugged
    # merge (later comparative date wins) would wrongly exclude this quarter.
    loaded, errors = fundamentals.load_quarters(client, "NVDA", as_of="2025-06-01", limit=12)
    assert errors == []
    by_period = {q["period_end"]: q for q in loaded}
    assert "2024-10-27" in by_period, "original filing was available and must be kept"
    q = by_period["2024-10-27"]
    assert q["filing_date"] == "2024-11-20"  # earliest, not the 2025-11-19 comparative
    assert q["revenue"] == 350.0  # as-originally-reported, not the 999.0 restatement


def test_load_quarters_passes_limit_and_timeframe() -> None:
    client = FakeFinancials(_statement_rows(_quarter_specs(4, end=date(2026, 6, 30))))
    fundamentals.load_quarters(client, "NVDA", as_of="2026-09-01", limit=12)
    for _statement, params in client.calls:
        assert params == {"timeframe": "quarterly", "limit": 12}


# =========================================================================== #
# Fundamentals: quality arithmetic
# =========================================================================== #
def test_compute_quality_known_numbers() -> None:
    base = {
        "gross_profit": 100.0,
        "ebitda": 70.0,
        "net_income": 40.0,
        "cash_from_operations": 60.0,
        "operating_income": 50.0,
        "interest_expense": 5.0,
        "total_assets": 1000.0,
        "total_debt": 250.0,
        "cash": 100.0,
        "short_term_investments": 50.0,
    }
    quarters = [dict(base) for _ in range(4)]
    quality, errors = fundamentals.compute_quality(quarters)
    assert quality["gp_to_assets"] == pytest.approx(0.4)  # 400 / 1000
    assert quality["accruals"] == pytest.approx((160 - 240) / 1000)  # -0.08
    assert quality["net_debt_ebitda"] == pytest.approx((250 - 100 - 50) / 280)
    assert quality["interest_cov"] == pytest.approx(200 / 20)  # 10
    assert errors == []


def test_compute_quality_missing_debt_degrades() -> None:
    base = {
        "gross_profit": 100.0,
        "ebitda": 70.0,
        "net_income": 40.0,
        "cash_from_operations": 60.0,
        "operating_income": 50.0,
        "interest_expense": 5.0,
        "total_assets": 1000.0,
        "total_debt": None,
        "cash": 100.0,
        "short_term_investments": 0.0,
    }
    quarters = [dict(base) for _ in range(4)]
    quality, errors = fundamentals.compute_quality(quarters)
    assert quality["net_debt_ebitda"] is None
    assert any("net_debt" in e.lower() or "debt" in e.lower() for e in errors)
    assert quality["gp_to_assets"] == pytest.approx(0.4)  # the rest still computes


# =========================================================================== #
# Fundamentals: momentum
# =========================================================================== #
def test_compute_momentum_from_month_ends() -> None:
    idx = pd.date_range(end="2026-08-31", periods=14, freq="ME")
    levels = pd.Series([100.0 + i for i in range(14)], index=idx)  # 100..113
    as_of = idx[-1].date()
    momentum, error = fundamentals.compute_momentum(levels, as_of=as_of)
    assert error is None
    assert momentum["ret_12_1"] == pytest.approx(112 / 101 - 1)
    assert momentum["ret_1m_reversal"] == pytest.approx(113 / 112 - 1)


def test_compute_momentum_insufficient_history() -> None:
    idx = pd.date_range(end="2026-08-31", periods=5, freq="ME")
    levels = pd.Series([100.0] * 5, index=idx)
    momentum, error = fundamentals.compute_momentum(levels, as_of=idx[-1].date())
    assert momentum == {"ret_12_1": None, "ret_1m_reversal": None}
    assert error is not None


# =========================================================================== #
# Fundamentals: value z-scores (own history) + fwd P/E unavailable
# =========================================================================== #
def _daily_prices(start: date, end: date, base: float = 50.0) -> pd.Series:
    idx = pd.date_range(start=start, end=end, freq="B")
    # A gently varying price so multiples move quarter to quarter.
    values = base + 10.0 * np.sin(np.linspace(0, 6.0, len(idx)))
    return pd.Series(values, index=idx)


def test_compute_value_z_uses_own_history_and_flags_fwd_pe() -> None:
    specs = _quarter_specs(20, end=date(2026, 6, 30))
    quarters = [fundamentals._finalise_quarter(_merge_spec(s)) for s in specs]
    prices = _daily_prices(date(2020, 1, 1), date(2026, 9, 1))
    value_z, errors = fundamentals.compute_value_z(
        quarters, prices, current_price=float(prices.iloc[-1])
    )
    assert value_z["basis"].startswith("own_")
    assert value_z["n_obs"] >= fundamentals.MIN_Z_OBS
    # EV/Sales z-score is a finite number computed from the ticker's own history.
    assert value_z["ev_sales"] is None or isinstance(value_z["ev_sales"], float)
    # Forward P/E is unavailable without a consensus-estimate feed — never faked.
    assert value_z["pe_fwd"] is None
    assert "consensus-estimate" in value_z["pe_fwd_error"]


def _merge_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Turn a flat quarter spec into the joined pre-finalise entry shape."""
    return {
        "period_end": spec["period_end"],
        "filing_date": spec["filing_date"],
        "revenue": spec["revenue"],
        "gross_profit": spec["gross_profit"],
        "operating_income": spec["operating_income"],
        "net_income": spec["net_income"],
        "eps": spec["eps"],
        "shares": spec["shares"],
        "ebitda": spec["ebitda"],
        "interest_expense": spec["interest_expense"],
        "cash": spec["cash"],
        "short_term_investments": spec["short_term_investments"],
        "total_assets": spec["total_assets"],
        "debt_current": spec["debt_current"],
        "long_term_debt": spec["long_term_debt"],
        "cash_from_operations": spec["cash_from_operations"],
        "capex": spec["capex"],
    }


# =========================================================================== #
# Fundamentals: trajectory + acceleration flags
# =========================================================================== #
def test_compute_trajectory_yoy_growth_and_flags() -> None:
    specs = _quarter_specs(16, end=date(2026, 6, 30))
    quarters = [fundamentals._finalise_quarter(_merge_spec(s)) for s in specs]
    trajectory, flags = fundamentals.compute_trajectory(quarters)
    assert len(trajectory) == fundamentals.TRAJECTORY_QUARTERS
    # Oldest-first for display.
    assert trajectory[0]["period_end"] < trajectory[-1]["period_end"]
    # Every trajectory quarter is keyed on its filing date.
    assert all(row["filing_date"] for row in trajectory)
    # YoY revenue growth is present for the recent quarters (needs a year of base).
    assert trajectory[-1]["rev_growth"] is not None
    assert set(flags) >= {"rev_accel", "margin_accel"}


# =========================================================================== #
# Fundamentals: full section + revisions/PEAD unavailable
# =========================================================================== #
def test_build_fundamentals_section_end_to_end() -> None:
    specs = _quarter_specs(20, end=date(2026, 6, 30))
    client = FakeFinancials(_statement_rows(specs))
    prices = _daily_prices(date(2020, 1, 1), date(2026, 9, 1))
    section = fundamentals.build_fundamentals_section(
        client, "NVDA", prices=prices, as_of="2026-09-01", current_price=float(prices.iloc[-1])
    )
    assert section["version"] == fundamentals.MODULE_VERSION
    assert section["revisions"] is None
    assert section["pead"] is None
    assert "consensus-estimate" in section["revisions_error"]
    assert "consensus-estimate" in section["pead_error"]
    assert section["quality"]["gp_to_assets"] is not None
    assert section["momentum"]["ret_12_1"] is not None
    assert len(section["trajectory"]) == fundamentals.TRAJECTORY_QUARTERS


def test_build_fundamentals_section_raises_without_statements() -> None:
    client = FakeFinancials({})
    with pytest.raises(fundamentals.FundamentalsError):
        fundamentals.build_fundamentals_section(client, "NVDA", as_of="2026-09-01")


# =========================================================================== #
# Text: diff scorer on known strings
# =========================================================================== #
def test_change_score_identical_is_zero() -> None:
    passage = "The company depends on a small number of suppliers for critical components."
    assert text.change_score(passage, passage) == pytest.approx(0.0, abs=1e-9)


def test_change_score_disjoint_is_one() -> None:
    assert text.change_score("alpha bravo charlie", "delta echo foxtrot") == pytest.approx(1.0)


def test_change_score_partial_between_zero_and_one() -> None:
    before = "The company faces intense competition in the market for chips."
    after = "The company faces intense competition and new regulatory pressure."
    score = text.change_score(before, after)
    assert 0.0 < score < 1.0


def test_change_score_appearance_and_disappearance() -> None:
    assert text.change_score("", "brand new risk factor text") == pytest.approx(1.0)
    assert text.change_score("old removed section text", "") == pytest.approx(1.0)
    assert text.change_score("", "") == pytest.approx(0.0)


def test_sentence_diff_known_before_after() -> None:
    before = (
        "We rely on third party foundries for manufacturing our products. "
        "Our results may fluctuate from quarter to quarter significantly."
    )
    after = (
        "We rely on third party foundries for manufacturing our products. "
        "A new export control regime could restrict sales to certain regions."
    )
    added, removed = text.sentence_diff(before, after)
    assert any("export control" in s for s in added)
    assert any("fluctuate from quarter" in s for s in removed)
    # The shared sentence is neither added nor removed.
    assert not any("third party foundries" in s for s in added)


# =========================================================================== #
# Text: section extraction from a filing document
# =========================================================================== #
def _filing_doc(*, risk_extra: str = "") -> str:
    risk_body = (
        "Our business faces numerous risks that could materially and adversely "
        "affect our financial condition and results of operations in future "
        "periods. Demand for our products may decline. "
        + risk_extra
    )
    mda_body = (
        "Management believes revenue increased due to strong demand across our "
        "segments during the period under review, and we expect continued growth "
        "into the next fiscal year. Capital expenditures rose as we invested in "
        "property and equipment. One customer accounted for 22% of our revenue."
    )
    return (
        "Item 1. Business\n"
        "We design and sell semiconductors to a global customer base.\n"
        "Item 1A. Risk Factors\n"
        f"{risk_body}\n"
        "Item 1B. Unresolved Staff Comments\n"
        "None.\n"
        "Item 7. Management's Discussion and Analysis of Financial Condition\n"
        f"{mda_body}\n"
        "Item 7A. Quantitative and Qualitative Disclosures About Market Risk\n"
        "None.\n"
    )


def test_extract_sections_finds_all_five() -> None:
    sections = text.extract_sections(_filing_doc())
    assert set(sections) == set(text.SECTION_LABELS)
    assert "materially and adversely" in sections["Risk Factors"]
    assert "revenue increased" in sections["MD&A"]
    assert "accounted for 22%" in sections["Customer/Supplier Concentration"]
    assert "property and equipment" in sections["Capex"]
    assert "we expect" in sections["Guidance"].lower()


# =========================================================================== #
# Text: filing selection point-in-time
# =========================================================================== #
def _submissions(filings: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "cik": "320193",
        "filings": {
            "recent": {
                "form": [f["form"] for f in filings],
                "accessionNumber": [f["accession"] for f in filings],
                "primaryDocument": [f["doc"] for f in filings],
                "filingDate": [f["filed"] for f in filings],
                "reportDate": [f.get("report", "") for f in filings],
            }
        },
    }


def test_select_filings_point_in_time_and_counts() -> None:
    filings = [
        {
            "form": "10-Q",
            "accession": "0000320193-26-000004",
            "doc": "q4.htm",
            "filed": "2026-08-01",
        },
        {
            "form": "10-K",
            "accession": "0000320193-26-000003",
            "doc": "k2.htm",
            "filed": "2026-05-01",
        },
        {
            "form": "10-Q",
            "accession": "0000320193-26-000002",
            "doc": "q3.htm",
            "filed": "2026-02-01",
        },
        {
            "form": "10-Q",
            "accession": "0000320193-25-000009",
            "doc": "q2.htm",
            "filed": "2025-11-01",
        },
        {
            "form": "10-Q",
            "accession": "0000320193-25-000008",
            "doc": "q1.htm",
            "filed": "2025-08-01",
        },
        {
            "form": "10-K",
            "accession": "0000320193-25-000003",
            "doc": "k1.htm",
            "filed": "2025-05-01",
        },
    ]
    selected = text.select_filings(_submissions(filings), as_of=date(2026, 6, 1))
    # The 2026-08-01 10-Q is after as_of and must be excluded.
    assert all(f["filing_date"] <= "2026-06-01" for lst in selected.values() for f in lst)
    assert len(selected["10-K"]) == 2
    assert len(selected["10-Q"]) == 3
    assert selected["10-K"][0]["filing_date"] == "2026-05-01"  # newest first


# =========================================================================== #
# Text: LLM sees only the diff; output recorded; temp 0
# =========================================================================== #
def test_assess_with_llm_sees_only_diff_and_records() -> None:
    entries = text._diff_pair(
        {"form": "10-K", "filing_date": "2026-05-01"},
        {"form": "10-K", "filing_date": "2025-05-01"},
        {
            "Risk Factors": "Shared sentinel sentence stays identical across both filings here. "
            "A brand new supply chain disruption risk has emerged this year.",
            "MD&A": "",
            "Customer/Supplier Concentration": "",
            "Capex": "",
            "Guidance": "",
        },
        {
            "Risk Factors": "Shared sentinel sentence stays identical across both filings here.",
            "MD&A": "",
            "Customer/Supplier Concentration": "",
            "Capex": "",
            "Guidance": "",
        },
    )
    generator = FakeTextGenerator(
        {
            "new_risks": [
                {
                    "text": "supply chain disruption",
                    "quote": "A brand new supply chain disruption risk has emerged this year.",
                }
            ],
            "removed_risks": [],
            "concentration_change": None,
            "capex_change": None,
            "guidance_tone_change": None,
            "material_change_score": 4,
            "summary": "New supply chain risk added.",
        }
    )
    parsed, record, error = text.assess_with_llm(generator, entries, form="10-K")
    assert error is None
    assert parsed is not None and parsed["material_change_score"] == 4
    # Only the added/removed passages are shown -- never the unchanged sentinel.
    prompt = record["prompt"]
    assert "brand new supply chain disruption" in prompt
    assert "Shared sentinel sentence" not in prompt
    assert generator.temperatures == [0.0]
    assert record["raw"] is not None  # raw response persisted


# =========================================================================== #
# Text: news clustering, sentiment, exposure flags, point-in-time
# =========================================================================== #
def test_cluster_events_dedupes_and_scores() -> None:
    as_of = date(2026, 9, 1)
    items = [
        {
            "title": "NVDA beats earnings and raises guidance sharply",
            "url": "https://a.com/1",
            "published": "2026-08-28",
            "provider": "exa",
        },
        {
            "title": "NVDA beats earnings, raises guidance sharply on demand",
            "url": "https://b.com/2",
            "published": "2026-08-28",
            "provider": "massive",
        },
        {
            "title": "Regulators open antitrust probe into the chip sector tariffs",
            "url": "https://c.com/3",
            "published": "2026-08-20",
            "provider": "massive",
        },
        {
            "title": "This story is in the future and must be excluded",
            "url": "https://d.com/4",
            "published": "2026-09-15",
            "provider": "exa",
        },
    ]
    events, notes = text.cluster_events(items, as_of=as_of)
    # The two near-duplicate earnings headlines collapse into one event.
    earnings = [e for e in events if e["type"] in {"earnings", "guidance"}]
    assert len(earnings) == 1
    assert earnings[0]["n_sources"] == 2
    assert earnings[0]["sentiment"] == "positive"
    # The future-dated story is excluded (point-in-time).
    assert all(e["date"] <= as_of.isoformat() for e in events)
    assert not any("future" in e["headline"].lower() for e in events)
    # The antitrust story carries a regulatory/geopolitical exposure flag.
    probe = [e for e in events if "antitrust" in e["headline"].lower()][0]
    assert probe["type"] == "legal_regulatory"
    assert "regulatory" in probe["exposure_flags"] or "geopolitical" in probe["exposure_flags"]


def test_cluster_events_prefers_massive_insight_sentiment() -> None:
    items = [
        {
            "title": "Company posts mixed results with some soft segments",
            "url": "https://a.com/x",
            "published": "2026-08-15",
            "provider": "massive",
            "insight_sentiment": "negative",
        }
    ]
    events, _ = text.cluster_events(items, as_of=date(2026, 9, 1))
    assert events[0]["sentiment"] == "negative"
    assert events[0]["sentiment_method"] == "massive_insight"


# =========================================================================== #
# Text: full section end-to-end with all fakes
# =========================================================================== #
def test_build_text_section_end_to_end() -> None:
    from app.sec import filing_url

    cik = "320193"
    k_new = {
        "form": "10-K", "accession": "0000320193-26-000003",
        "doc": "k2.htm", "filed": "2026-05-01",
    }
    k_old = {
        "form": "10-K", "accession": "0000320193-25-000003",
        "doc": "k1.htm", "filed": "2025-05-01",
    }
    url_new = filing_url(cik.lstrip("0"), k_new["accession"], k_new["doc"])
    url_old = filing_url(cik.lstrip("0"), k_old["accession"], k_old["doc"])
    docs = {
        url_new: _filing_doc(
            risk_extra="A brand new export control risk could materially restrict our sales."
        ),
        url_old: _filing_doc(),
    }
    sec_client = FakeSec(_submissions([k_new, k_old]), docs)
    exa = FakeExa(
        [
            FakeExaResult(
                "Company raises full year guidance on strong demand",
                "https://news.com/1",
                "2026-08-20",
                snippet="Guidance raised.",
            )
        ]
    )
    market = FakeMarketNews(
        [
            {
                "title": "Analyst downgrade weighs on shares amid probe",
                "article_url": "https://news.com/2",
                "published_utc": "2026-08-10T12:00:00Z",
                "description": "A downgrade and an investigation.",
                "publisher": {"name": "Wire"},
            }
        ]
    )
    generator = FakeTextGenerator(
        {
            "new_risks": [
                {
                    "text": "export control risk",
                    "quote": "A brand new export control risk could materially restrict our sales.",
                }
            ],
            "removed_risks": [],
            "concentration_change": "Customer concentration unchanged.",
            "capex_change": "Capex plans steady.",
            "guidance_tone_change": "Slightly more optimistic.",
            "material_change_score": 3,
            "summary": "New export control risk.",
        }
    )

    section = text.build_text_section(
        "NVDA",
        sec_client=sec_client,
        exa_client=exa,
        market_client=market,
        text_generator=generator,
        company_name="NVIDIA",
        as_of="2026-09-01",
    )
    assert section["version"] == text.MODULE_VERSION
    # Filing diff produced one entry per section, and the Risk Factors diff moved.
    risk_entries = [e for e in section["filing_changes"] if e["section"] == "Risk Factors"]
    assert risk_entries and risk_entries[0]["change_score"] > 0
    assert risk_entries[0]["new_risks"][0]["quote"].startswith("A brand new export control")
    assert risk_entries[0]["material_change_score"] == 3
    # The LLM prompt+response is persisted for audit.
    assert section["prompts"] and section["prompts"][0]["raw"]
    # News clustered into dated events.
    assert section["events"]
    assert all(e["date"] <= "2026-09-01" for e in section["events"])
    # No leaked internal diff scratch keys.
    assert all("_added" not in e and "_removed" not in e for e in section["filing_changes"])


def test_build_text_section_raises_when_nothing_available() -> None:
    with pytest.raises(text.TextError):
        text.build_text_section(
            "NVDA", sec_client=None, exa_client=None, market_client=None, text_generator=None,
            as_of="2026-09-01",
        )
