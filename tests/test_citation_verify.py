from __future__ import annotations

from typing import Any

from app.citation_verify import (
    CitationCheck,
    CitationVerificationResult,
    classify_citation,
    extract_citations,
    verify_citations,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _full_report() -> dict[str, Any]:
    """A synthetic Vision v2 report covering every cited kind."""

    return {
        "Ticker": "ACME",
        "Name": "Acme Corp",
        "SEC Source Pack": {
            "Status": "available",
            "Provider": "SEC EDGAR",
            "Company Facts": {
                "Revenue": {
                    "Value": 137_237_000_000,
                    "Unit": "USD",
                    "Form": "10-Q",
                    "Filed": "2026-05-20",
                    "Fiscal Period": "Q1",
                    "Fiscal Year": 2027,
                },
                "Cash And Equivalents": {
                    "Value": 13_237_000_000,
                    "Unit": "USD",
                    "Form": "10-Q",
                    "Filed": "2026-05-20",
                },
            },
            "Citations": [
                {
                    "Label": "SEC 10-K Item 1 Business",
                    "Form": "10-K",
                    "Item": "Item 1",
                    "Filing Date": "2026-02-25",
                    "URL": "https://example.com/10k",
                },
                {
                    "Label": "SEC 10-Q Item 7 MD&A",
                    "Form": "10-Q",
                    "Item": "Item 7",
                    "Filing Date": "2026-05-20",
                    "URL": "https://example.com/10q",
                },
            ],
            "Earnings Sections": {},
        },
        "SEC Trend Pack": {
            "Quarters": [
                {
                    "period": "Q1 FY2027",
                    "revenue": 137_237_000_000,
                    "cash_from_operations": 50_344_000_000,
                },
                {
                    "period": "Q4 FY2026",
                    "revenue": 124_300_000_000,
                    "cash_from_operations": 47_100_000_000,
                },
            ],
            "Metrics": {
                "Inventory": {
                    "name": "Inventory",
                    "values": [
                        ("Q4 FY2026", 5_200_000_000.0),
                        ("Q1 FY2027", 11_834_000_000.0),
                    ],
                    "yoy_growth_latest": 1.2763,
                    "qoq_growth_latest": 1.276,
                    "trend": "up",
                }
            },
        },
        "Earnings Source Pack": {
            "Status": "available",
            "SEC 8-K Sections": {
                "Results": {
                    "Form": "8-K",
                    "Item": "Item 2.02",
                    "Filing Date": "2026-05-20",
                }
            },
            "Calendar": {
                "earningsDate": "2026-07-25",
                "epsEstimate": 1.23,
            },
        },
        "Exa Research Pack": {
            "Status": "available",
            "Citations": [
                {
                    "url": "https://techcrunch.com/2025/12/03/acme-launches",
                    "title": "Acme launches X",
                    "published_date": "2025-12-03",
                    "query_bucket": "news",
                },
                {
                    "url": "https://www.reuters.com/2026/01/15/acme-q4",
                    "title": "Acme Q4 results",
                    "published_date": "2026-01-15",
                    "query_bucket": "news",
                },
            ],
        },
    }


# ---------------------------------------------------------------------------
# extract_citations
# ---------------------------------------------------------------------------


def test_extract_citations_finds_sec_xbrl() -> None:
    memo = "Revenue grew sharply (SEC XBRL Revenue, Q1 FY2027: $137,237M) last quarter."
    cites = extract_citations(memo)
    assert len(cites) == 1
    raw, pos = cites[0]
    assert raw == "(SEC XBRL Revenue, Q1 FY2027: $137,237M)"
    assert memo[pos] == "("


def test_extract_citations_ignores_generic_parens() -> None:
    memo = (
        "Revenue grew (see above) and margin expanded (roughly) as "
        "expected (i.e. nothing surprising)."
    )
    cites = extract_citations(memo)
    assert cites == []


def test_extract_citations_finds_multiple_kinds() -> None:
    memo = """
    The thesis hinges on cash conversion (SEC XBRL Cash, Q1 FY2027: $13,237M)
    and recent disclosures (SEC 10-K Item 1 Business, filed 2026-02-25).
    Trend pack confirms inventory build (SEC Trend Pack: Metrics.Inventory.yoy_growth_latest=1.2763).
    Earnings event was reported (SEC 8-K Item 2.02, filed 2026-05-20) and the
    next print is on the calendar (Earnings Calendar, source Yahoo Finance profile).
    News flow is supportive (Exa: techcrunch.com, 2025-12-03).
    """
    cites = extract_citations(memo)
    raws = [r for r, _ in cites]
    assert len(cites) == 6
    assert any("SEC XBRL Cash" in r for r in raws)
    assert any("SEC 10-K Item 1 Business" in r for r in raws)
    assert any("SEC Trend Pack" in r for r in raws)
    assert any("SEC 8-K Item 2.02" in r for r in raws)
    assert any("Earnings Calendar" in r for r in raws)
    assert any("Exa: techcrunch.com" in r for r in raws)


def test_extract_citations_handles_empty_or_none() -> None:
    assert extract_citations("") == []
    assert extract_citations(None) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# classify_citation
# ---------------------------------------------------------------------------


def test_classify_sec_xbrl_basic() -> None:
    info = classify_citation("(SEC XBRL Revenue, Q1 FY2027: $137,237M)")
    assert info["kind"] == "sec_xbrl"
    assert info["target"] == "Revenue"
    assert info["value"].strip() == "$137,237M"


def test_classify_sec_filing_basic() -> None:
    info = classify_citation("(SEC 10-K Item 1 Business, filed 2026-02-25)")
    assert info["kind"] == "sec_filing"
    assert info["form"] == "10-K"
    assert info["item"] == "Item 1"
    assert info["date"] == "2026-02-25"


def test_classify_sec_earnings_section() -> None:
    info = classify_citation("(SEC 8-K Item 2.02, filed 2026-05-20)")
    assert info["kind"] == "sec_earnings_section"
    assert info["item"] == "Item 2.02"
    assert info["date"] == "2026-05-20"


def test_classify_trend_pack() -> None:
    info = classify_citation(
        "(SEC Trend Pack: Quarters[0].cash_from_operations=50,344M)"
    )
    assert info["kind"] == "sec_trend_pack"
    assert info["target"].strip() == "Quarters[0].cash_from_operations"


def test_classify_exa() -> None:
    info = classify_citation("(Exa: techcrunch.com, 2025-12-03)")
    assert info["kind"] == "exa"
    assert info["domain"] == "techcrunch.com"
    assert info["date"] == "2025-12-03"


def test_classify_earnings_calendar() -> None:
    info = classify_citation("(Earnings Calendar, source Yahoo Finance profile)")
    assert info["kind"] == "earnings_calendar"


# ---------------------------------------------------------------------------
# SEC XBRL checks
# ---------------------------------------------------------------------------


def test_sec_xbrl_verified_match() -> None:
    memo = "(SEC XBRL Revenue, Q1 FY2027: $137,237M)"
    result = verify_citations(memo, report=_full_report())
    assert result.total == 1
    assert result.verified == 1
    assert result.checks[0].status == "verified"


def test_sec_xbrl_value_mismatch() -> None:
    report = _full_report()
    report["SEC Source Pack"]["Company Facts"]["Revenue"]["Value"] = 100_000_000_000
    memo = "(SEC XBRL Revenue, Q1 FY2027: $137,237M)"
    result = verify_citations(memo, report=report)
    assert result.value_mismatch == 1
    assert result.checks[0].status == "value_mismatch"


def test_sec_xbrl_concept_missing() -> None:
    report = _full_report()
    report["SEC Source Pack"]["Company Facts"] = {}
    report["SEC Trend Pack"]["Metrics"] = {}
    memo = "(SEC XBRL Revenue, Q1 FY2027: $137,237M)"
    result = verify_citations(memo, report=report)
    assert result.concept_missing == 1
    assert result.checks[0].status == "concept_missing"


def test_sec_xbrl_cash_alias_resolves() -> None:
    """A 'SEC XBRL Cash' citation should resolve to 'Cash And Equivalents'
    in Company Facts."""
    memo = "(SEC XBRL Cash, Q1 FY2027: $13,237M)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1


# ---------------------------------------------------------------------------
# SEC filing / earnings / calendar checks
# ---------------------------------------------------------------------------


def test_sec_filing_verified() -> None:
    memo = "(SEC 10-K Item 1 Business, filed 2026-02-25)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1
    assert result.checks[0].kind == "sec_filing"


def test_sec_filing_date_mismatch() -> None:
    memo = "(SEC 10-K Item 1 Business, filed 2024-01-01)"
    result = verify_citations(memo, report=_full_report())
    assert result.value_mismatch == 1


def test_sec_earnings_section_verified() -> None:
    memo = "(SEC 8-K Item 2.02, filed 2026-05-20)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1
    assert result.checks[0].kind == "sec_earnings_section"


def test_earnings_calendar_verified() -> None:
    memo = "(Earnings Calendar, source Yahoo Finance profile)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1


# ---------------------------------------------------------------------------
# Trend Pack path walking
# ---------------------------------------------------------------------------


def test_sec_trend_pack_quarters_verified() -> None:
    memo = "(SEC Trend Pack: Quarters[0].cash_from_operations=50,344M)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1


def test_sec_trend_pack_metrics_verified() -> None:
    memo = "(SEC Trend Pack: Metrics.Inventory.yoy_growth_latest=1.2763)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1


def test_sec_trend_pack_missing_path_is_concept_missing() -> None:
    memo = "(SEC Trend Pack: Quarters[5].does_not_exist=99)"
    result = verify_citations(memo, report=_full_report())
    assert result.concept_missing == 1


# ---------------------------------------------------------------------------
# Exa
# ---------------------------------------------------------------------------


def test_exa_verified() -> None:
    memo = "(Exa: techcrunch.com, 2025-12-03)"
    result = verify_citations(memo, report=_full_report())
    assert result.verified == 1


def test_exa_missing_domain_is_concept_missing() -> None:
    memo = "(Exa: example-not-in-report.com, 2025-12-03)"
    result = verify_citations(memo, report=_full_report())
    assert result.concept_missing == 1


# ---------------------------------------------------------------------------
# Unknown / uncheckable
# ---------------------------------------------------------------------------


def test_unknown_uncheckable_when_extracted() -> None:
    """A parenthesized chunk that has a strong token but doesn't match
    any known pattern should classify as unknown / uncheckable."""

    # "Citation" is one of the strong tokens so this passes the extractor.
    memo = "Background context (Citation: novel custom format we don't understand)."
    result = verify_citations(memo, report=_full_report())
    assert result.uncheckable == 1
    assert result.checks[0].kind == "unknown"


def test_obvious_aside_is_filtered_out() -> None:
    memo = "Revenue (see above) ticked up."
    result = verify_citations(memo, report=_full_report())
    assert result.total == 0


# ---------------------------------------------------------------------------
# Aggregation behaviour
# ---------------------------------------------------------------------------


def test_empty_report_all_concept_missing() -> None:
    memo = (
        "(SEC XBRL Revenue, Q1 FY2027: $137,237M) "
        "(SEC 10-K Item 1 Business, filed 2026-02-25) "
        "(Exa: techcrunch.com, 2025-12-03)"
    )
    result = verify_citations(memo, report=None)
    assert result.total == 3
    assert result.uncheckable == 0
    assert result.concept_missing == 3
    assert result.checkable == 3
    assert result.verified == 0


def test_percent_verified_is_one_when_no_checkable() -> None:
    # Only an uncheckable / unknown citation present.
    memo = "(Citation: totally custom format)"
    result = verify_citations(memo, report=_full_report())
    assert result.checkable == 0
    assert result.uncheckable == 1
    assert result.percent_verified == 1.0


def test_full_pipeline_synthetic_memo() -> None:
    memo = (
        "Vision v2 thesis follows.\n\n"
        "Revenue printed at a record (SEC XBRL Revenue, Q1 FY2027: $137,237M) "
        "while cash held (SEC XBRL Cash, Q1 FY2027: $13,237M). The annual report "
        "(SEC 10-K Item 1 Business, filed 2026-02-25) confirms the segment mix.\n"
        "Cash from ops trended up (SEC Trend Pack: Quarters[0].cash_from_operations=50,344M)\n"
        "Latest earnings event (SEC 8-K Item 2.02, filed 2026-05-20) and news\n"
        "flow (Exa: techcrunch.com, 2025-12-03) supports the thesis.\n"
    )
    result = verify_citations(memo, report=_full_report())
    assert result.total == 6
    assert result.verified == 6
    assert result.checkable == 6
    assert result.uncheckable == 0
    assert result.percent_verified == 1.0
    assert all(isinstance(c, CitationCheck) for c in result.checks)
    assert isinstance(result, CitationVerificationResult)
    assert result.elapsed_ms >= 0


def test_check_positions_point_into_memo() -> None:
    memo = "intro (SEC XBRL Revenue, Q1 FY2027: $137,237M) outro"
    result = verify_citations(memo, report=_full_report())
    assert result.total == 1
    pos = result.checks[0].position
    assert memo[pos] == "("
    assert memo.startswith(result.checks[0].raw, pos)
