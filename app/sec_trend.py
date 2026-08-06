"""SEC EDGAR multi-quarter XBRL trend & segment data for analyst memos.

This module extends :mod:`app.sec` without modifying it. It depends on
``SecClient.cik_for_ticker``, ``SecClient.submissions`` and ``SecClient.fetch_json``
to pull raw companyfacts JSON, then mines the full historical time-series for
each XBRL concept to build a multi-quarter trend pack.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from threading import Lock
from typing import Any

from app.market_data import clean_ticker
from app.sec import SEC_DATA_BASE, SecDataError

DEFAULT_TREND_CACHE_SECONDS = 6 * 60 * 60
DEFAULT_QUARTERS = 8
USABLE_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A"}

# Ordered list of (label, taxonomy, concepts) used to pull XBRL time series.
# The first concept with usable rows wins.
CONCEPT_SPECS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "revenue",
        "us-gaap",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "SalesRevenueGoodsNet",
        ),
    ),
    ("gross_profit", "us-gaap", ("GrossProfit",)),
    ("operating_income", "us-gaap", ("OperatingIncomeLoss",)),
    ("net_income", "us-gaap", ("NetIncomeLoss",)),
    ("rd_expense", "us-gaap", ("ResearchAndDevelopmentExpense",)),
    ("sga_expense", "us-gaap", ("SellingGeneralAndAdministrativeExpense",)),
    ("capex", "us-gaap", ("PaymentsToAcquirePropertyPlantAndEquipment",)),
    ("cash_from_operations", "us-gaap", ("NetCashProvidedByUsedInOperatingActivities",)),
    ("diluted_eps", "us-gaap", ("EarningsPerShareDiluted",)),
    (
        "diluted_shares",
        "us-gaap",
        ("WeightedAverageNumberOfDilutedSharesOutstanding",),
    ),
    (
        "cash",
        "us-gaap",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        ),
    ),
    ("long_term_debt", "us-gaap", ("LongTermDebt", "LongTermDebtNoncurrent")),
    ("inventory", "us-gaap", ("InventoryNet",)),
    ("total_assets", "us-gaap", ("Assets",)),
    ("total_equity", "us-gaap", ("StockholdersEquity",)),
)

# Flow metrics are reported per-period (income statement / cash flow) and 10-K
# rows represent a full fiscal year — needing Q4 derivation. Stock metrics are
# point-in-time (balance sheet) and 10-K rows are already a quarterly snapshot.
FLOW_METRICS = {
    "revenue",
    "gross_profit",
    "operating_income",
    "net_income",
    "rd_expense",
    "sga_expense",
    "capex",
    "cash_from_operations",
}
STOCK_METRICS = {
    "cash",
    "long_term_debt",
    "inventory",
    "total_assets",
    "total_equity",
}
# EPS / share counts are weighted-average per-period — treat as flow for Q4 derivation
# (Q4 EPS = FY EPS - sum of Q1-Q3 reported EPS values, an approximation).
PER_PERIOD_METRICS = {"diluted_eps", "diluted_shares"}

METRIC_SERIES_LABELS: tuple[tuple[str, str], ...] = (
    ("Revenue", "revenue"),
    ("Gross Profit", "gross_profit"),
    ("Operating Income", "operating_income"),
    ("Net Income", "net_income"),
    ("Gross Margin", "gross_margin"),
    ("Operating Margin", "operating_margin"),
    ("Free Cash Flow", "free_cash_flow"),
    ("Cash", "cash"),
    ("Long Term Debt", "long_term_debt"),
    ("Inventory", "inventory"),
    ("Diluted EPS", "diluted_eps"),
)


class SecTrendError(RuntimeError):
    """Raised when the trend pack cannot be assembled."""


@dataclass(frozen=True)
class TrendQuarter:
    period_end: str
    fiscal_period: str
    fiscal_year: int
    revenue: float | None
    gross_profit: float | None
    operating_income: float | None
    net_income: float | None
    gross_margin: float | None
    operating_margin: float | None
    rd_expense: float | None
    sga_expense: float | None
    capex: float | None
    cash_from_operations: float | None
    free_cash_flow: float | None
    diluted_eps: float | None
    diluted_shares: float | None
    cash: float | None
    long_term_debt: float | None
    inventory: float | None
    total_assets: float | None
    total_equity: float | None
    form: str
    filed_date: str


@dataclass(frozen=True)
class MetricSeries:
    name: str
    values: list[tuple[str, float]]
    yoy_growth_latest: float | None
    qoq_growth_latest: float | None
    trend: str


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: dict[str, Any]


_TREND_CACHE: dict[str, _CacheEntry] = {}
_TREND_CACHE_LOCK = Lock()


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_sec_trend_pack(
    sec_client: Any,
    ticker: str,
    *,
    quarters: int = DEFAULT_QUARTERS,
    cache_seconds: float = DEFAULT_TREND_CACHE_SECONDS,
) -> dict[str, Any]:
    """Build an SEC-EDGAR multi-quarter trend pack for ``ticker``.

    See module docstring for the returned shape.
    """
    if sec_client is None:
        return _not_configured_pack(ticker)

    try:
        symbol = clean_ticker(ticker) if ticker else ""
    except ValueError as exc:
        return _empty_pack(ticker or "", None, "unavailable", [str(exc)])
    if not symbol:
        return _empty_pack(
            ticker or "",
            None,
            "unavailable",
            ["Ticker is required to build SEC trend pack"],
        )

    cached = _cached_value(symbol, quarters)
    if cached is not None:
        return cached

    errors: list[str] = []
    try:
        cik = sec_client.cik_for_ticker(symbol)
    except SecDataError as exc:
        return _empty_pack(symbol, None, "unavailable", [str(exc)])
    except Exception as exc:  # noqa: BLE001 - any client failure is recoverable
        return _empty_pack(symbol, None, "unavailable", [f"CIK lookup failed: {exc}"])

    # Submissions metadata and the (large) XBRL companyfacts payload come from
    # two independent, idempotent SEC endpoints. Fetch them concurrently: the
    # client's own request gate still rate-limits dispatch, but the companyfacts
    # JSON parse can overlap the submissions fetch.
    submissions: Mapping[str, Any] = {}
    facts_url = f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{cik}.json"
    with ThreadPoolExecutor(max_workers=2) as executor:
        submissions_future = executor.submit(sec_client.submissions, cik)
        facts_future = executor.submit(sec_client.fetch_json, facts_url)
        try:
            submissions = submissions_future.result() or {}
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Could not fetch SEC submissions: {exc}")

        try:
            facts_payload = facts_future.result()
        except SecDataError as exc:
            return _empty_pack(symbol, cik, "unavailable", [*errors, str(exc)])
        except Exception as exc:  # noqa: BLE001
            return _empty_pack(
                symbol, cik, "unavailable", [*errors, f"Could not fetch SEC companyfacts: {exc}"]
            )

    facts = _facts_dict(facts_payload)
    if not facts:
        return _empty_pack(
            symbol,
            cik,
            "unavailable",
            [*errors, "SEC companyfacts response did not include facts"],
        )

    concept_rows = _extract_concept_rows(facts, errors)
    quarterly_rows = _build_quarterly_rows(concept_rows)
    annual_rows = _build_annual_rows(concept_rows)
    if not quarterly_rows and not annual_rows:
        return _empty_pack(
            symbol,
            cik,
            "unavailable",
            [*errors, "No usable XBRL rows in SEC companyfacts"],
        )

    quarter_objects = _trend_quarters(quarterly_rows, quarters)
    annual_objects = _trend_annuals(annual_rows, 3)

    metrics = _build_metric_series(quarter_objects)
    operating_leverage = _operating_leverage(quarter_objects)
    revenue_acceleration = _revenue_acceleration(quarter_objects)
    margin_trajectory = _margin_trajectory(quarter_objects)
    segments = _extract_segments(facts)
    citations = _citations(quarter_objects + annual_objects)

    quarters_dict = [asdict(q) for q in quarter_objects]
    annual_dict = [asdict(q) for q in annual_objects]
    metrics_dict = {name: asdict(series) for name, series in metrics.items()}

    status: str
    if quarter_objects and metrics:
        status = "partial" if errors else "available"
    elif quarter_objects or annual_objects:
        status = "partial"
    else:
        status = "unavailable"

    pack: dict[str, Any] = {
        "Status": status,
        "Provider": "SEC EDGAR",
        "Ticker": symbol,
        "CIK": cik,
        "Company Name": submissions.get("name") if isinstance(submissions, Mapping) else None,
        "Quarters": quarters_dict,
        "Annual": annual_dict,
        "Metrics": metrics_dict,
        "Operating Leverage": operating_leverage,
        "Segments": segments,
        "Revenue Acceleration": revenue_acceleration,
        "Margin Trajectory": margin_trajectory,
        "Citations": citations,
        "Errors": errors,
    }

    _remember_value(symbol, quarters, pack, cache_seconds)
    return copy.deepcopy(pack)


# ---------------------------------------------------------------------------
# Concept extraction
# ---------------------------------------------------------------------------


def _facts_dict(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    facts = payload.get("facts")
    return facts if isinstance(facts, Mapping) else {}


def _extract_concept_rows(
    facts: Mapping[str, Any], errors: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Return ``{metric_label: [row, ...]}`` for every metric with usable data."""
    out: dict[str, list[dict[str, Any]]] = {}
    for label, taxonomy, concepts in CONCEPT_SPECS:
        rows = _select_concept_rows(facts, taxonomy, concepts)
        if rows:
            out[label] = rows
    if not out:
        errors.append("No usable XBRL concepts matched")
    return out


def _select_concept_rows(
    facts: Mapping[str, Any],
    taxonomy: str,
    concepts: Sequence[str],
) -> list[dict[str, Any]]:
    taxonomy_facts = facts.get(taxonomy)
    if not isinstance(taxonomy_facts, Mapping):
        return []
    for concept in concepts:
        concept_data = taxonomy_facts.get(concept)
        if not isinstance(concept_data, Mapping):
            continue
        units = concept_data.get("units")
        if not isinstance(units, Mapping):
            continue
        # EPS sits in a USD/shares unit; everything else USD or shares.
        for unit_name, rows in units.items():
            if not isinstance(rows, list):
                continue
            usable = [_normalise_row(row) for row in rows if _is_usable_row(row)]
            usable = [row for row in usable if row is not None]
            if usable:
                for row in usable:
                    row["unit"] = unit_name
                    row["concept"] = concept
                return usable
    return []


def _is_usable_row(row: Any) -> bool:
    if not isinstance(row, Mapping):
        return False
    if row.get("val") is None:
        return False
    return row.get("form") in USABLE_FORMS


def _normalise_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    end = row.get("end")
    val = row.get("val")
    if not isinstance(end, str) or val is None:
        return None
    try:
        numeric = float(val)
    except (TypeError, ValueError):
        return None
    return {
        "end": end,
        "start": row.get("start") if isinstance(row.get("start"), str) else None,
        "val": numeric,
        "accn": row.get("accn"),
        "fy": _safe_int(row.get("fy")),
        "fp": str(row.get("fp") or "").upper(),
        "form": str(row.get("form") or ""),
        "filed": str(row.get("filed") or ""),
    }


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Quarter / annual row assembly
# ---------------------------------------------------------------------------


def _build_quarterly_rows(
    concept_rows: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Return one row per quarter end date.

    For flow metrics we take the per-quarter (10-Q) value where ``fp`` is
    Q1/Q2/Q3, and derive Q4 as FY - (Q1+Q2+Q3). For stock metrics any 10-K /
    10-Q row at that end-date is acceptable.
    """
    per_quarter: dict[str, dict[str, Any]] = {}

    for metric in FLOW_METRICS | PER_PERIOD_METRICS:
        rows = concept_rows.get(metric)
        if not rows:
            continue
        quarter_lookup = _quarter_only_rows(rows)
        fy_rows = _fiscal_year_rows(rows)
        derived_q4 = _derive_q4_rows(quarter_lookup, fy_rows)
        merged = {**quarter_lookup, **derived_q4}
        for key, row in merged.items():
            entry = per_quarter.setdefault(
                key,
                _new_quarter_entry(row["end"], row["fp"], row["fy"], row["form"], row["filed"]),
            )
            entry[metric] = row["val"]
            entry["_accns"][metric] = row.get("accn")
            # Prefer 10-Q filing metadata when populating the master row.
            if row["form"] == "10-Q" and entry["form"] != "10-Q":
                entry["form"] = row["form"]
                entry["filed_date"] = row["filed"]
                entry["fiscal_period"] = row["fp"]
                entry["fiscal_year"] = row["fy"]

    for metric in STOCK_METRICS:
        rows = concept_rows.get(metric)
        if not rows:
            continue
        for row in rows:
            key = row["end"]
            entry = per_quarter.setdefault(
                key,
                _new_quarter_entry(row["end"], row["fp"], row["fy"], row["form"], row["filed"]),
            )
            entry[metric] = row["val"]
            entry["_accns"][metric] = row.get("accn")

    finalised = [_finalise_quarter(entry) for entry in per_quarter.values()]
    finalised.sort(key=lambda q: q["period_end"], reverse=True)
    return finalised


def _new_quarter_entry(
    end: str, fp: str, fy: int, form: str, filed: str
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "period_end": end,
        "fiscal_period": fp or "",
        "fiscal_year": fy,
        "form": form or "",
        "filed_date": filed or "",
        "_accns": {},
    }
    for metric, *_ in CONCEPT_SPECS:
        entry[metric] = None
    return entry


def _quarter_only_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["form"] not in {"10-Q", "10-Q/A"}:
            continue
        if row["fp"] not in {"Q1", "Q2", "Q3"}:
            continue
        # If multiple rows for the same end (amendments etc.), keep the latest filed.
        existing = out.get(row["end"])
        if existing is None or row["filed"] > existing["filed"]:
            out[row["end"]] = row
    return out


def _fiscal_year_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        if row["fp"] != "FY":
            continue
        # 10-K rows for FY have whole-year duration; the row end is fiscal Q4 end.
        existing = out.get(row["fy"])
        if existing is None or row["filed"] > existing["filed"]:
            out[row["fy"]] = row
    return out


def _derive_q4_rows(
    quarter_rows: Mapping[str, dict[str, Any]],
    fy_rows: Mapping[int, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Synthesize Q4 quarterly values by subtracting Q1+Q2+Q3 from FY."""
    out: dict[str, dict[str, Any]] = {}
    by_year: dict[int, list[dict[str, Any]]] = {}
    for row in quarter_rows.values():
        by_year.setdefault(row["fy"], []).append(row)

    for fy, fy_row in fy_rows.items():
        prior = by_year.get(fy, [])
        if len(prior) < 3:
            continue
        # All three quarter values present? Sum the latest of each Q1/Q2/Q3.
        partials: dict[str, dict[str, Any]] = {}
        for row in prior:
            existing = partials.get(row["fp"])
            if existing is None or row["filed"] > existing["filed"]:
                partials[row["fp"]] = row
        if {"Q1", "Q2", "Q3"} - partials.keys():
            continue
        q4_val = fy_row["val"] - sum(partials[fp]["val"] for fp in ("Q1", "Q2", "Q3"))
        out[fy_row["end"]] = {
            "end": fy_row["end"],
            "start": None,
            "val": q4_val,
            "accn": fy_row.get("accn"),
            "fy": fy,
            "fp": "Q4",
            "form": fy_row["form"],
            "filed": fy_row["filed"],
        }
    return out


def _finalise_quarter(entry: dict[str, Any]) -> dict[str, Any]:
    revenue = entry.get("revenue")
    gross_profit = entry.get("gross_profit")
    operating_income = entry.get("operating_income")
    cfo = entry.get("cash_from_operations")
    capex = entry.get("capex")

    entry["gross_margin"] = _safe_div(gross_profit, revenue)
    entry["operating_margin"] = _safe_div(operating_income, revenue)
    if cfo is not None and capex is not None:
        entry["free_cash_flow"] = cfo - capex
    else:
        entry["free_cash_flow"] = None

    # Default empty strings so the dataclass is happy.
    entry["form"] = entry.get("form") or "10-Q"
    entry["filed_date"] = entry.get("filed_date") or ""
    entry["fiscal_period"] = entry.get("fiscal_period") or ""
    if not entry.get("fiscal_year"):
        entry["fiscal_year"] = _year_from_period_end(entry["period_end"])
    return entry


def _year_from_period_end(period_end: str) -> int:
    if len(period_end) >= 4 and period_end[:4].isdigit():
        return int(period_end[:4])
    return 0


def _build_annual_rows(
    concept_rows: Mapping[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Build full-year rows from FY 10-K filings only."""
    per_year: dict[str, dict[str, Any]] = {}

    for metric in FLOW_METRICS | PER_PERIOD_METRICS:
        rows = concept_rows.get(metric, [])
        for row in rows:
            if row["fp"] != "FY" or row["form"] not in {"10-K", "10-K/A"}:
                continue
            key = row["end"]
            existing = per_year.get(key)
            if existing is None:
                entry = _new_quarter_entry(
                    row["end"], "FY", row["fy"], row["form"], row["filed"]
                )
                per_year[key] = entry
                existing = entry
            elif row["filed"] > existing["filed_date"]:
                existing["filed_date"] = row["filed"]
                existing["form"] = row["form"]
            existing[metric] = row["val"]
            existing["_accns"][metric] = row.get("accn")

    for metric in STOCK_METRICS:
        rows = concept_rows.get(metric, [])
        for row in rows:
            if row["form"] not in {"10-K", "10-K/A"}:
                continue
            entry = per_year.get(row["end"])
            if entry is None:
                continue
            entry[metric] = row["val"]
            entry["_accns"][metric] = row.get("accn")

    finalised = [_finalise_quarter(entry) for entry in per_year.values()]
    finalised.sort(key=lambda q: q["period_end"], reverse=True)
    return finalised


def _trend_quarters(rows: list[dict[str, Any]], limit: int) -> list[TrendQuarter]:
    return [_row_to_trend_quarter(row) for row in rows[:limit]]


def _trend_annuals(rows: list[dict[str, Any]], limit: int) -> list[TrendQuarter]:
    return [_row_to_trend_quarter(row) for row in rows[:limit]]


def _row_to_trend_quarter(row: Mapping[str, Any]) -> TrendQuarter:
    return TrendQuarter(
        period_end=row["period_end"],
        fiscal_period=row.get("fiscal_period") or "",
        fiscal_year=int(row.get("fiscal_year") or 0),
        revenue=row.get("revenue"),
        gross_profit=row.get("gross_profit"),
        operating_income=row.get("operating_income"),
        net_income=row.get("net_income"),
        gross_margin=row.get("gross_margin"),
        operating_margin=row.get("operating_margin"),
        rd_expense=row.get("rd_expense"),
        sga_expense=row.get("sga_expense"),
        capex=row.get("capex"),
        cash_from_operations=row.get("cash_from_operations"),
        free_cash_flow=row.get("free_cash_flow"),
        diluted_eps=row.get("diluted_eps"),
        diluted_shares=row.get("diluted_shares"),
        cash=row.get("cash"),
        long_term_debt=row.get("long_term_debt"),
        inventory=row.get("inventory"),
        total_assets=row.get("total_assets"),
        total_equity=row.get("total_equity"),
        form=row.get("form") or "",
        filed_date=row.get("filed_date") or "",
    )


# ---------------------------------------------------------------------------
# Metric series, leverage, acceleration, margins
# ---------------------------------------------------------------------------


def _build_metric_series(quarters: list[TrendQuarter]) -> dict[str, MetricSeries]:
    out: dict[str, MetricSeries] = {}
    if not quarters:
        return out
    chronological = list(reversed(quarters))  # oldest first
    for display_name, attr in METRIC_SERIES_LABELS:
        pairs: list[tuple[str, float]] = []
        for q in chronological:
            value = getattr(q, attr, None)
            if value is None:
                continue
            pairs.append((q.period_end, float(value)))
        if not pairs:
            continue
        yoy = _series_growth(pairs, lag=4)
        qoq = _series_growth(pairs, lag=1)
        out[display_name] = MetricSeries(
            name=display_name,
            values=pairs,
            yoy_growth_latest=yoy,
            qoq_growth_latest=qoq,
            trend=_trend_label(pairs),
        )
    return out


def _series_growth(pairs: Sequence[tuple[str, float]], *, lag: int) -> float | None:
    if len(pairs) <= lag:
        return None
    latest = pairs[-1][1]
    prior = pairs[-1 - lag][1]
    if prior == 0:
        return None
    return (latest / prior) - 1.0


def _trend_label(pairs: Sequence[tuple[str, float]]) -> str:
    if len(pairs) < 3:
        return "stable"
    last = pairs[-1][1]
    mid = pairs[-2][1]
    earlier = pairs[-3][1]
    # YoY-style growth slope across 3 most recent points.
    if mid == 0 or earlier == 0:
        return "stable"
    recent_growth = (last / mid) - 1.0
    earlier_growth = (mid / earlier) - 1.0
    if recent_growth < 0 and earlier_growth < 0:
        return "declining"
    if recent_growth > earlier_growth + 0.01:
        return "accelerating"
    if recent_growth < earlier_growth - 0.01:
        return "decelerating"
    return "stable"


def _operating_leverage(quarters: list[TrendQuarter]) -> dict[str, Any]:
    if len(quarters) < 5:
        return {"value": None, "label": "insufficient"}
    latest = quarters[0]
    prior = quarters[4]
    if (
        latest.revenue is None
        or prior.revenue is None
        or latest.operating_income is None
        or prior.operating_income is None
    ):
        return {"value": None, "label": "insufficient"}
    delta_rev = latest.revenue - prior.revenue
    delta_op = latest.operating_income - prior.operating_income
    if delta_rev == 0:
        return {"value": None, "label": "insufficient"}
    value = delta_op / delta_rev
    if value < 0:
        label = "negative"
    elif value > 2:
        label = "high"
    elif value >= 1:
        label = "moderate"
    else:
        label = "low"
    return {"value": value, "label": label}


def _revenue_acceleration(quarters: list[TrendQuarter]) -> dict[str, Any]:
    revenues = [q.revenue for q in quarters]
    qoq_latest = _ratio_minus_one(_get(revenues, 0), _get(revenues, 1))
    qoq_prior = _ratio_minus_one(_get(revenues, 1), _get(revenues, 2))
    yoy_latest = _ratio_minus_one(_get(revenues, 0), _get(revenues, 4))
    yoy_prior = _ratio_minus_one(_get(revenues, 1), _get(revenues, 5))
    accelerating = bool(
        qoq_latest is not None
        and qoq_prior is not None
        and yoy_latest is not None
        and yoy_prior is not None
        and qoq_latest > qoq_prior
        and yoy_latest > yoy_prior
    )
    return {
        "qoq_latest": qoq_latest,
        "qoq_prior": qoq_prior,
        "yoy_latest": yoy_latest,
        "yoy_prior": yoy_prior,
        "accelerating": accelerating,
    }


def _margin_trajectory(quarters: list[TrendQuarter]) -> dict[str, Any]:
    gms = [q.gross_margin for q in quarters]
    oms = [q.operating_margin for q in quarters]
    gm_yoy = _delta(_get(gms, 0), _get(gms, 4))
    om_yoy = _delta(_get(oms, 0), _get(oms, 4))

    recent_gms = [g for g in gms[:4] if g is not None]
    gm_stability = _stability_label(recent_gms)
    recent_oms = [o for o in oms[:4] if o is not None]
    om_stability = _stability_label(recent_oms)

    return {
        "gross_margin_change_yoy": gm_yoy,
        "operating_margin_change_yoy": om_yoy,
        "gross_margin_stability": gm_stability,
        "operating_margin_stability": om_stability,
        "latest_gross_margin": _get(gms, 0),
        "latest_operating_margin": _get(oms, 0),
    }


def _stability_label(values: Sequence[float]) -> str:
    if len(values) < 2:
        return "insufficient"
    spread = max(values) - min(values)
    if spread < 0.01:
        return "very stable"
    if spread < 0.03:
        return "stable"
    if spread < 0.07:
        return "moderate"
    return "volatile"


def _get(values: Sequence[float | None], index: int) -> float | None:
    if 0 <= index < len(values):
        return values[index]
    return None


def _ratio_minus_one(a: float | None, b: float | None) -> float | None:
    if a is None or b is None or b == 0:
        return None
    return (a / b) - 1.0


def _delta(a: float | None, b: float | None) -> float | None:
    if a is None or b is None:
        return None
    return a - b


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


# ---------------------------------------------------------------------------
# Segments
# ---------------------------------------------------------------------------


def _extract_segments(facts: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    """Best-effort segment extraction from companyfacts.

    SEC companyfacts ordinarily omits the segment dimension, so this returns
    ``None`` unless a segment-flavored concept actually appears with usable
    ``members`` data.
    """
    gaap = facts.get("us-gaap") if isinstance(facts, Mapping) else None
    if not isinstance(gaap, Mapping):
        return None

    segment_concepts = (
        "SegmentReportingInformationRevenueFromExternalCustomers",
        "RevenueFromExternalCustomers",
    )
    raw_rows: list[dict[str, Any]] = []
    for concept in segment_concepts:
        concept_data = gaap.get(concept)
        if not isinstance(concept_data, Mapping):
            continue
        units = concept_data.get("units")
        if not isinstance(units, Mapping):
            continue
        for rows in units.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                member = row.get("member") or row.get("dim") or row.get("segment")
                if not isinstance(member, str):
                    continue
                if row.get("val") is None or not isinstance(row.get("end"), str):
                    continue
                try:
                    numeric = float(row["val"])
                except (TypeError, ValueError):
                    continue
                raw_rows.append(
                    {
                        "member": member,
                        "end": row["end"],
                        "val": numeric,
                        "fy": _safe_int(row.get("fy")),
                        "fp": str(row.get("fp") or "").upper(),
                        "form": str(row.get("form") or ""),
                    }
                )

    if not raw_rows:
        return None

    by_segment: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        by_segment.setdefault(row["member"], []).append(row)

    segments: list[dict[str, Any]] = []
    for name, rows in by_segment.items():
        rows.sort(key=lambda r: r["end"], reverse=True)
        latest = rows[0]
        prior = next(
            (r for r in rows[1:] if r["fp"] == latest["fp"] and r["fy"] != latest["fy"]),
            None,
        )
        prior_val = prior["val"] if prior else None
        growth = _ratio_minus_one(latest["val"], prior_val) if prior_val else None
        segments.append(
            {
                "name": name,
                "revenue": latest["val"],
                "revenue_prior_year": prior_val,
                "growth_yoy": growth,
                "share_of_total": None,
                "period_end": latest["end"],
            }
        )

    total = sum(s["revenue"] for s in segments if s["revenue"])
    if total:
        for seg in segments:
            if seg["revenue"]:
                seg["share_of_total"] = seg["revenue"] / total

    segments.sort(key=lambda s: s["revenue"] or 0, reverse=True)
    return segments


# ---------------------------------------------------------------------------
# Citations / cache / empty packs
# ---------------------------------------------------------------------------


def _citations(quarters: list[TrendQuarter]) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for q in quarters:
        key = (q.form, q.filed_date, q.period_end)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            {
                "form": q.form,
                "filed_date": q.filed_date,
                "period_end": q.period_end,
                "fiscal_period": q.fiscal_period,
                "fiscal_year": q.fiscal_year,
            }
        )
    return citations


def _cache_key(symbol: str, quarters: int) -> str:
    return f"{symbol}|{quarters}"


def _cached_value(symbol: str, quarters: int) -> dict[str, Any] | None:
    key = _cache_key(symbol, quarters)
    with _TREND_CACHE_LOCK:
        entry = _TREND_CACHE.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            _TREND_CACHE.pop(key, None)
            return None
        return copy.deepcopy(entry.value)


def _remember_value(
    symbol: str, quarters: int, value: dict[str, Any], ttl_seconds: float
) -> None:
    if ttl_seconds <= 0:
        return
    with _TREND_CACHE_LOCK:
        _TREND_CACHE[_cache_key(symbol, quarters)] = _CacheEntry(
            expires_at=time.monotonic() + ttl_seconds,
            value=copy.deepcopy(value),
        )


def clear_trend_cache() -> None:
    """Test helper — drop all cached trend packs."""
    with _TREND_CACHE_LOCK:
        _TREND_CACHE.clear()


def _empty_pack(
    ticker: str,
    cik: str | None,
    status: str,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "Status": status,
        "Provider": "SEC EDGAR",
        "Ticker": ticker,
        "CIK": cik,
        "Company Name": None,
        "Quarters": [],
        "Annual": [],
        "Metrics": {},
        "Operating Leverage": {"value": None, "label": "insufficient"},
        "Segments": None,
        "Revenue Acceleration": {
            "qoq_latest": None,
            "qoq_prior": None,
            "yoy_latest": None,
            "yoy_prior": None,
            "accelerating": False,
        },
        "Margin Trajectory": {
            "gross_margin_change_yoy": None,
            "operating_margin_change_yoy": None,
            "gross_margin_stability": "insufficient",
            "operating_margin_stability": "insufficient",
            "latest_gross_margin": None,
            "latest_operating_margin": None,
        },
        "Citations": [],
        "Errors": errors,
    }


def _not_configured_pack(ticker: str) -> dict[str, Any]:
    pack = _empty_pack(ticker or "", None, "not configured", [])
    return pack
