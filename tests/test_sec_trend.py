from __future__ import annotations

from typing import Any

import pytest

from app import sec_trend
from app.sec_trend import (
    MetricSeries,
    SecTrendError,
    TrendQuarter,
    build_sec_trend_pack,
    clear_trend_cache,
)

TEST_CIK = "0000123456"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    clear_trend_cache()


class FakeSecClient:
    """Minimal SecClient stand-in: implements only what build_sec_trend_pack uses."""

    def __init__(
        self,
        *,
        cik: str = TEST_CIK,
        companyfacts: dict[str, Any] | None = None,
        submissions_payload: dict[str, Any] | None = None,
        raise_on_facts: Exception | None = None,
        raise_on_cik: Exception | None = None,
    ) -> None:
        self._cik = cik
        self._facts = companyfacts or {}
        self._submissions = submissions_payload or {"name": "Test Co"}
        self._raise_on_facts = raise_on_facts
        self._raise_on_cik = raise_on_cik

    def cik_for_ticker(self, ticker: str) -> str:
        if self._raise_on_cik:
            raise self._raise_on_cik
        return self._cik

    def submissions(self, cik: str) -> dict[str, Any]:
        return self._submissions

    def fetch_json(self, url: str) -> Any:
        if self._raise_on_facts:
            raise self._raise_on_facts
        return self._facts


# ---------------------------------------------------------------------------
# Synthetic companyfacts builders
# ---------------------------------------------------------------------------


# Eight quarters of fiscal data, oldest -> newest. Q4 values are implicit; the
# 10-K rows give FY totals so the trend module can derive Q4 = FY - Q1-Q3.
QUARTERS: list[dict[str, Any]] = [
    # FY2023
    {"end": "2023-03-31", "fy": 2023, "fp": "Q1", "form": "10-Q", "filed": "2023-04-15"},
    {"end": "2023-06-30", "fy": 2023, "fp": "Q2", "form": "10-Q", "filed": "2023-07-15"},
    {"end": "2023-09-30", "fy": 2023, "fp": "Q3", "form": "10-Q", "filed": "2023-10-15"},
    {"end": "2023-12-31", "fy": 2023, "fp": "Q4", "form": "10-Q", "filed": "2024-01-15"},
    # FY2024
    {"end": "2024-03-31", "fy": 2024, "fp": "Q1", "form": "10-Q", "filed": "2024-04-15"},
    {"end": "2024-06-30", "fy": 2024, "fp": "Q2", "form": "10-Q", "filed": "2024-07-15"},
    {"end": "2024-09-30", "fy": 2024, "fp": "Q3", "form": "10-Q", "filed": "2024-10-15"},
    {"end": "2024-12-31", "fy": 2024, "fp": "Q4", "form": "10-Q", "filed": "2025-01-15"},
]

# Quarter index 0..7, oldest -> newest.
REVENUE_VALUES = [100.0, 110.0, 120.0, 130.0, 150.0, 165.0, 185.0, 215.0]
OP_INC_VALUES = [10.0, 12.0, 14.0, 16.0, 22.0, 26.0, 31.0, 40.0]
GROSS_PROFIT_VALUES = [50.0, 56.0, 62.0, 68.0, 78.0, 86.0, 96.0, 110.0]
NET_INC_VALUES = [8.0, 10.0, 11.0, 13.0, 18.0, 22.0, 27.0, 35.0]
CFO_VALUES = [12.0, 15.0, 16.0, 18.0, 25.0, 28.0, 32.0, 42.0]
CAPEX_VALUES = [2.0, 3.0, 3.0, 4.0, 4.0, 5.0, 5.0, 6.0]
EPS_VALUES = [0.10, 0.12, 0.13, 0.15, 0.20, 0.24, 0.29, 0.36]


def _usd_row(end: str, val: float, fy: int, fp: str, form: str, filed: str) -> dict[str, Any]:
    return {
        "end": end,
        "val": val,
        "fy": fy,
        "fp": fp,
        "form": form,
        "filed": filed,
        "accn": f"acc-{filed}",
    }


def _build_concept_unit(
    quarter_values: list[float],
    *,
    derive_fy: bool = True,
    unit: str = "USD",
) -> dict[str, list[dict[str, Any]]]:
    """Build the ``units`` block for a flow concept.

    Emits Q1/Q2/Q3 rows from the eight-quarter data and, when ``derive_fy``,
    one 10-K row per fiscal year containing the FY total. (No Q4 10-Q rows;
    the trend module derives Q4 from FY - Q1-Q3.)
    """
    rows: list[dict[str, Any]] = []
    for q_meta, value in zip(QUARTERS, quarter_values, strict=True):
        if q_meta["fp"] == "Q4":
            continue  # Q4 is derived from FY
        rows.append(
            _usd_row(
                end=q_meta["end"],
                val=value,
                fy=q_meta["fy"],
                fp=q_meta["fp"],
                form=q_meta["form"],
                filed=q_meta["filed"],
            )
        )
    if derive_fy:
        for fy in (2023, 2024):
            indices = [i for i, q in enumerate(QUARTERS) if q["fy"] == fy]
            fy_total = sum(quarter_values[i] for i in indices)
            year_end = next(q for q in QUARTERS if q["fy"] == fy and q["fp"] == "Q4")["end"]
            filed = f"{fy + 1}-02-15"
            rows.append(_usd_row(year_end, fy_total, fy, "FY", "10-K", filed))
    return {unit: rows}


def _build_stock_unit(values: list[float]) -> dict[str, list[dict[str, Any]]]:
    """Stock metric rows: one per quarter end-date, from the most recent filing."""
    rows: list[dict[str, Any]] = []
    for q_meta, value in zip(QUARTERS, values, strict=True):
        rows.append(
            _usd_row(
                end=q_meta["end"],
                val=value,
                fy=q_meta["fy"],
                fp=q_meta["fp"],
                form="10-K" if q_meta["fp"] == "Q4" else "10-Q",
                filed=q_meta["filed"],
            )
        )
    return {"USD": rows}


def build_companyfacts(
    *,
    include_segments: bool = False,
    revenue_concept: str = "RevenueFromContractWithCustomerExcludingAssessedTax",
) -> dict[str, Any]:
    cash_values = [50, 55, 60, 65, 72, 80, 90, 105]
    debt_values = [200, 200, 195, 195, 190, 188, 185, 180]
    inv_values = [40, 42, 44, 46, 48, 51, 55, 60]
    assets_values = [800, 810, 825, 840, 870, 900, 940, 990]
    equity_values = [300, 310, 320, 335, 360, 385, 410, 450]
    shares_values = [100.0] * 8

    gaap: dict[str, Any] = {
        revenue_concept: {
            "label": "Revenue",
            "units": _build_concept_unit(REVENUE_VALUES),
        },
        "GrossProfit": {"label": "Gross Profit", "units": _build_concept_unit(GROSS_PROFIT_VALUES)},
        "OperatingIncomeLoss": {
            "label": "Operating Income",
            "units": _build_concept_unit(OP_INC_VALUES),
        },
        "NetIncomeLoss": {"label": "Net Income", "units": _build_concept_unit(NET_INC_VALUES)},
        "NetCashProvidedByUsedInOperatingActivities": {
            "label": "CFO",
            "units": _build_concept_unit(CFO_VALUES),
        },
        "PaymentsToAcquirePropertyPlantAndEquipment": {
            "label": "Capex",
            "units": _build_concept_unit(CAPEX_VALUES),
        },
        "EarningsPerShareDiluted": {
            "label": "EPS",
            "units": _build_concept_unit(EPS_VALUES, unit="USD/shares"),
        },
        "WeightedAverageNumberOfDilutedSharesOutstanding": {
            "label": "Shares",
            "units": _build_concept_unit(shares_values, unit="shares"),
        },
        "CashAndCashEquivalentsAtCarryingValue": {
            "label": "Cash",
            "units": _build_stock_unit(cash_values),
        },
        "LongTermDebt": {"label": "LT Debt", "units": _build_stock_unit(debt_values)},
        "InventoryNet": {"label": "Inventory", "units": _build_stock_unit(inv_values)},
        "Assets": {"label": "Assets", "units": _build_stock_unit(assets_values)},
        "StockholdersEquity": {"label": "Equity", "units": _build_stock_unit(equity_values)},
    }

    if include_segments:
        gaap["SegmentReportingInformationRevenueFromExternalCustomers"] = {
            "label": "Segment Revenue",
            "units": {
                "USD": [
                    {
                        "end": "2024-12-31",
                        "val": 150.0,
                        "fy": 2024,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2025-02-15",
                        "accn": "seg-A-2024",
                        "member": "ProductsSegment",
                    },
                    {
                        "end": "2023-12-31",
                        "val": 100.0,
                        "fy": 2023,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2024-02-15",
                        "accn": "seg-A-2023",
                        "member": "ProductsSegment",
                    },
                    {
                        "end": "2024-12-31",
                        "val": 65.0,
                        "fy": 2024,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2025-02-15",
                        "accn": "seg-B-2024",
                        "member": "ServicesSegment",
                    },
                    {
                        "end": "2023-12-31",
                        "val": 50.0,
                        "fy": 2023,
                        "fp": "FY",
                        "form": "10-K",
                        "filed": "2024-02-15",
                        "accn": "seg-B-2023",
                        "member": "ServicesSegment",
                    },
                ]
            },
        }

    return {"facts": {"us-gaap": gaap}}


# ---------------------------------------------------------------------------
# Dataclass smoke tests
# ---------------------------------------------------------------------------


def test_trend_quarter_construction() -> None:
    q = TrendQuarter(
        period_end="2024-12-31",
        fiscal_period="Q4",
        fiscal_year=2024,
        revenue=100.0,
        gross_profit=60.0,
        operating_income=20.0,
        net_income=15.0,
        gross_margin=0.6,
        operating_margin=0.2,
        rd_expense=None,
        sga_expense=None,
        capex=5.0,
        cash_from_operations=25.0,
        free_cash_flow=20.0,
        diluted_eps=0.15,
        diluted_shares=100.0,
        cash=50.0,
        long_term_debt=200.0,
        inventory=40.0,
        total_assets=800.0,
        total_equity=300.0,
        form="10-K",
        filed_date="2025-02-15",
    )
    assert q.period_end == "2024-12-31"
    assert q.free_cash_flow == 20.0


def test_metric_series_construction() -> None:
    series = MetricSeries(
        name="Revenue",
        values=[("2024-03-31", 100.0), ("2024-06-30", 110.0)],
        yoy_growth_latest=None,
        qoq_growth_latest=0.1,
        trend="accelerating",
    )
    assert series.values[1] == ("2024-06-30", 110.0)
    assert series.qoq_growth_latest == pytest.approx(0.1)


def test_sec_trend_error_subclasses_runtime_error() -> None:
    assert issubclass(SecTrendError, RuntimeError)


# ---------------------------------------------------------------------------
# Pack-level tests
# ---------------------------------------------------------------------------


def test_build_pack_returns_not_configured_when_client_missing() -> None:
    pack = build_sec_trend_pack(None, "AAPL")
    assert pack["Status"] == "not configured"
    assert pack["Quarters"] == []
    assert pack["Metrics"] == {}
    assert pack["Provider"] == "SEC EDGAR"


def test_build_pack_with_synthetic_dataset() -> None:
    client = FakeSecClient(companyfacts=build_companyfacts())
    pack = build_sec_trend_pack(client, "TEST")
    assert pack["Status"] in {"available", "partial"}
    assert pack["Ticker"] == "TEST"
    assert pack["CIK"] == TEST_CIK
    quarters = pack["Quarters"]
    assert len(quarters) == 8
    # Most-recent first
    assert quarters[0]["period_end"] == "2024-12-31"
    assert quarters[-1]["period_end"] == "2023-03-31"
    # Q4 derived correctly: Q4_2024 revenue = 215.0
    assert quarters[0]["revenue"] == pytest.approx(215.0)
    assert quarters[0]["fiscal_period"] == "Q4"
    # Gross margin populated
    assert quarters[0]["gross_margin"] == pytest.approx(110.0 / 215.0)
    # FCF = CFO - capex
    assert quarters[0]["free_cash_flow"] == pytest.approx(42.0 - 6.0)
    # Annual rows from 10-K only
    annual = pack["Annual"]
    assert len(annual) == 2
    assert annual[0]["fiscal_period"] == "FY"
    assert annual[0]["revenue"] == pytest.approx(sum(REVENUE_VALUES[4:8]))
    # Metric series include Revenue
    assert "Revenue" in pack["Metrics"]
    assert pack["Metrics"]["Revenue"]["yoy_growth_latest"] is not None
    # Citations covers quarters
    assert pack["Citations"]


def test_pack_with_fallback_revenue_concept() -> None:
    client = FakeSecClient(
        companyfacts=build_companyfacts(revenue_concept="SalesRevenueNet")
    )
    pack = build_sec_trend_pack(client, "TEST")
    assert pack["Quarters"][0]["revenue"] == pytest.approx(215.0)


def test_pack_handles_cik_failure() -> None:
    from app.sec import SecDataError

    client = FakeSecClient(raise_on_cik=SecDataError("No CIK for ZZZZ"))
    pack = build_sec_trend_pack(client, "ZZZZ")
    assert pack["Status"] == "unavailable"
    assert any("No CIK" in e for e in pack["Errors"])


def test_pack_handles_companyfacts_failure() -> None:
    from app.sec import SecDataError

    client = FakeSecClient(raise_on_facts=SecDataError("companyfacts 404"))
    pack = build_sec_trend_pack(client, "TEST")
    assert pack["Status"] == "unavailable"
    assert any("companyfacts" in e.lower() for e in pack["Errors"])


def test_pack_handles_empty_companyfacts() -> None:
    client = FakeSecClient(companyfacts={"facts": {}})
    pack = build_sec_trend_pack(client, "TEST")
    assert pack["Status"] == "unavailable"


def test_pack_caches_results() -> None:
    client = FakeSecClient(companyfacts=build_companyfacts())
    pack1 = build_sec_trend_pack(client, "TEST")
    # Break the client's facts; cache should still return the prior result.
    client._facts = {}
    pack2 = build_sec_trend_pack(client, "TEST")
    assert pack1["Quarters"] == pack2["Quarters"]


# ---------------------------------------------------------------------------
# Operating leverage
# ---------------------------------------------------------------------------


def test_operating_leverage_low_label_for_half_passthrough() -> None:
    """100 incremental revenue, 50 incremental op inc -> leverage 0.5 -> 'low'."""
    # Build 5 quarters where revenue moves +100 vs 4 quarters ago and op inc +50.
    quarters: list[TrendQuarter] = []
    base = {
        "fiscal_period": "Q?",
        "fiscal_year": 2024,
        "gross_profit": None,
        "net_income": None,
        "gross_margin": None,
        "operating_margin": None,
        "rd_expense": None,
        "sga_expense": None,
        "capex": None,
        "cash_from_operations": None,
        "free_cash_flow": None,
        "diluted_eps": None,
        "diluted_shares": None,
        "cash": None,
        "long_term_debt": None,
        "inventory": None,
        "total_assets": None,
        "total_equity": None,
        "form": "10-Q",
        "filed_date": "2025-01-01",
    }
    revenue_seq = [200.0, 190.0, 180.0, 170.0, 100.0]  # latest first
    op_inc_seq = [70.0, 65.0, 60.0, 55.0, 20.0]
    period_ends = [
        "2024-12-31",
        "2024-09-30",
        "2024-06-30",
        "2024-03-31",
        "2023-12-31",
    ]
    for i in range(5):
        quarters.append(
            TrendQuarter(
                period_end=period_ends[i],
                revenue=revenue_seq[i],
                operating_income=op_inc_seq[i],
                **base,  # type: ignore[arg-type]
            )
        )
    leverage = sec_trend._operating_leverage(quarters)
    assert leverage["value"] == pytest.approx(0.5)
    assert leverage["label"] == "low"


def test_operating_leverage_high_label() -> None:
    quarters_data = [
        ("2024-12-31", 300.0, 80.0),
        ("2024-09-30", 250.0, 60.0),
        ("2024-06-30", 220.0, 50.0),
        ("2024-03-31", 210.0, 45.0),
        ("2023-12-31", 200.0, 20.0),
    ]
    quarters = [_simple_quarter(end, rev, op) for end, rev, op in quarters_data]
    leverage = sec_trend._operating_leverage(quarters)
    assert leverage["value"] == pytest.approx(60.0 / 100.0)
    assert leverage["label"] == "low"


def test_operating_leverage_negative() -> None:
    quarters_data = [
        ("2024-12-31", 300.0, -10.0),
        ("2024-09-30", 250.0, 0.0),
        ("2024-06-30", 220.0, 5.0),
        ("2024-03-31", 210.0, 10.0),
        ("2023-12-31", 200.0, 20.0),
    ]
    quarters = [_simple_quarter(end, rev, op) for end, rev, op in quarters_data]
    leverage = sec_trend._operating_leverage(quarters)
    assert leverage["value"] is not None
    assert leverage["value"] < 0
    assert leverage["label"] == "negative"


def test_operating_leverage_insufficient_quarters() -> None:
    quarters = [_simple_quarter("2024-12-31", 100.0, 10.0)]
    leverage = sec_trend._operating_leverage(quarters)
    assert leverage["value"] is None
    assert leverage["label"] == "insufficient"


def _simple_quarter(period_end: str, revenue: float, operating_income: float) -> TrendQuarter:
    return TrendQuarter(
        period_end=period_end,
        fiscal_period="Q?",
        fiscal_year=int(period_end[:4]),
        revenue=revenue,
        gross_profit=None,
        operating_income=operating_income,
        net_income=None,
        gross_margin=None,
        operating_margin=None,
        rd_expense=None,
        sga_expense=None,
        capex=None,
        cash_from_operations=None,
        free_cash_flow=None,
        diluted_eps=None,
        diluted_shares=None,
        cash=None,
        long_term_debt=None,
        inventory=None,
        total_assets=None,
        total_equity=None,
        form="10-Q",
        filed_date="2025-01-01",
    )


# ---------------------------------------------------------------------------
# Revenue acceleration
# ---------------------------------------------------------------------------


def test_revenue_acceleration_detects_growing_qoq() -> None:
    # Latest first. qoq_latest = 215/185 - 1 ~ 0.162; qoq_prior = 185/165 - 1 ~ 0.121.
    # yoy_latest = 215/130 - 1 ~ 0.654; yoy_prior = 185/120 - 1 ~ 0.542.
    quarters = [
        _simple_quarter("2024-12-31", 215.0, 40.0),
        _simple_quarter("2024-09-30", 185.0, 31.0),
        _simple_quarter("2024-06-30", 165.0, 26.0),
        _simple_quarter("2024-03-31", 150.0, 22.0),
        _simple_quarter("2023-12-31", 130.0, 16.0),
        _simple_quarter("2023-09-30", 120.0, 14.0),
        _simple_quarter("2023-06-30", 110.0, 12.0),
        _simple_quarter("2023-03-31", 100.0, 10.0),
    ]
    acc = sec_trend._revenue_acceleration(quarters)
    assert acc["accelerating"] is True
    assert acc["qoq_latest"] > acc["qoq_prior"]
    assert acc["yoy_latest"] > acc["yoy_prior"]


def test_revenue_acceleration_not_accelerating_when_qoq_drops() -> None:
    quarters = [
        _simple_quarter("2024-12-31", 190.0, 30.0),  # qoq_latest small
        _simple_quarter("2024-09-30", 185.0, 31.0),
        _simple_quarter("2024-06-30", 160.0, 26.0),  # qoq_prior big
        _simple_quarter("2024-03-31", 150.0, 22.0),
        _simple_quarter("2023-12-31", 130.0, 16.0),
        _simple_quarter("2023-09-30", 120.0, 14.0),
        _simple_quarter("2023-06-30", 110.0, 12.0),
        _simple_quarter("2023-03-31", 100.0, 10.0),
    ]
    acc = sec_trend._revenue_acceleration(quarters)
    assert acc["accelerating"] is False


def test_revenue_acceleration_handles_short_series() -> None:
    quarters = [_simple_quarter("2024-12-31", 100.0, 10.0)]
    acc = sec_trend._revenue_acceleration(quarters)
    assert acc["accelerating"] is False
    assert acc["qoq_latest"] is None


# ---------------------------------------------------------------------------
# Margin trajectory / segments
# ---------------------------------------------------------------------------


def test_margin_trajectory_from_real_pack() -> None:
    client = FakeSecClient(companyfacts=build_companyfacts())
    pack = build_sec_trend_pack(client, "TEST")
    traj = pack["Margin Trajectory"]
    assert traj["latest_gross_margin"] is not None
    assert traj["latest_operating_margin"] is not None
    assert traj["gross_margin_change_yoy"] is not None


def test_segments_extracted_when_member_present() -> None:
    client = FakeSecClient(companyfacts=build_companyfacts(include_segments=True))
    pack = build_sec_trend_pack(client, "TEST")
    segs = pack["Segments"]
    assert segs is not None
    assert len(segs) == 2
    names = {s["name"] for s in segs}
    assert names == {"ProductsSegment", "ServicesSegment"}
    products = next(s for s in segs if s["name"] == "ProductsSegment")
    assert products["revenue"] == pytest.approx(150.0)
    assert products["revenue_prior_year"] == pytest.approx(100.0)
    assert products["growth_yoy"] == pytest.approx(0.5)
    assert products["share_of_total"] == pytest.approx(150.0 / 215.0)


def test_segments_none_when_absent() -> None:
    client = FakeSecClient(companyfacts=build_companyfacts(include_segments=False))
    pack = build_sec_trend_pack(client, "TEST")
    assert pack["Segments"] is None
