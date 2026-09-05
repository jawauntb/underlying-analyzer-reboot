"""Point-in-time fundamentals for Situate (SPEC 5.5).

For ticker ``T`` on date ``t`` this module produces the *robust* cross-sectional
fundamentals the literature actually supports (McLean & Pontiff 2016; Hou, Xue &
Zhang 2020), and nothing else:

* **momentum** — 12-1 momentum and the 1-month reversal, computed from the price
  panel (month-end levels), never from fundamentals;
* **quality** — gross-profitability (gross profit / total assets, Novy-Marx),
  accruals (Sloan: earnings minus operating cash flow, scaled by assets),
  net debt / EBITDA and interest coverage, from Massive quarterly statements;
* **value** — EV/Sales, EV/EBITDA, trailing P/E and FCF yield expressed as
  z-scores against *the ticker's own multi-year history* (sector-constituent
  cross-sections are not available from Massive — see the build plan);
* **trajectory** — the last eight quarters of revenue growth and margins, keyed
  on **filing date** (not fiscal-period end, so nothing that had not yet been
  disclosed at ``t`` can leak in), with a slope and a second-derivative
  acceleration flag for display.

Two SPEC 5.5 signals are genuinely unavailable and are returned as ``None`` with
a stated reason rather than fabricated: **revision momentum** and **PEAD** both
need a consensus-estimate feed, and Massive exposes no estimates endpoint
(``/stocks/financials/v1/estimates`` -> 404, probed 2026-09-05). A number that
cannot be computed honestly is ``None`` with a reason in ``errors`` — never a
placeholder and never a guess.

Point-in-time discipline: every quarter is filtered ``filing_date <= t`` and
every price read is taken as of the relevant date, so recomputing at ``t`` after
masking later data is bit-identical (the lookahead test).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "MODULE_VERSION",
    "FundamentalsError",
    "build_fundamentals",
    "build_fundamentals_section",
    "compute_momentum",
    "compute_quality",
    "compute_trajectory",
    "compute_value_z",
    "load_quarters",
]

MODULE_VERSION = "1.0.0"

#: How many quarters to request from Massive. Five years of history feeds the
#: value z-scores and the eight-quarter trajectory (which itself needs four extra
#: quarters behind the window to compute year-over-year growth).
DEFAULT_QUARTERS_LIMIT = 20
#: Quarters shown in the display trajectory.
TRAJECTORY_QUARTERS = 8
#: Trailing window (quarters) for TTM sums.
TTM_QUARTERS = 4
#: Minimum ratio observations before a z-score is trustworthy.
MIN_Z_OBS = 6

#: The consensus-estimate gap, stated once and reused for both unavailable fields.
NO_ESTIMATES_REASON = (
    "no consensus-estimate provider (Massive exposes no estimates endpoint; "
    "/stocks/financials/v1/estimates returns 404)"
)

STATEMENTS: tuple[str, ...] = ("income", "balance", "cash-flow")

INCOME_FIELDS: dict[str, str] = {
    "revenue": "revenue",
    "cost_of_revenue": "cost_of_revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income_loss_attributable_common_shareholders",
    "eps": "diluted_earnings_per_share",
    "shares": "diluted_shares_outstanding",
    "ebitda": "ebitda",
    "interest_expense": "interest_expense",
    "pretax_income": "income_before_income_taxes",
    "income_taxes": "income_taxes",
    "depreciation_income": "depreciation_and_amortization",
}
BALANCE_FIELDS: dict[str, str] = {
    "cash": "cash_and_equivalents",
    "short_term_investments": "short_term_investments",
    "total_assets": "total_assets",
    "total_liabilities": "total_liabilities",
    "total_equity": "total_equity",
    "debt_current": "debt_current",
    "long_term_debt": "long_term_debt_and_capital_lease_obligations",
}
CASHFLOW_FIELDS: dict[str, str] = {
    "cash_from_operations": "net_cash_from_operating_activities",
    "capex": "purchase_of_property_plant_and_equipment",
    "depreciation": "depreciation_depletion_and_amortization",
}


class FundamentalsError(RuntimeError):
    """Raised when the fundamentals section cannot be built at all."""


# --------------------------------------------------------------------------- #
# Small numeric helpers (never a silent zero).
# --------------------------------------------------------------------------- #
def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    top = _finite(numerator)
    bottom = _finite(denominator)
    if top is None or bottom is None or bottom == 0:
        return None
    return top / bottom


def _sum(values: Sequence[Any]) -> float | None:
    """Sum a window only when every element is present (no partial TTM)."""
    numbers = [_finite(value) for value in values]
    if not numbers or any(number is None for number in numbers):
        return None
    return float(sum(number for number in numbers if number is not None))


def _z_score(current: float | None, history: Sequence[float]) -> float | None:
    values = [v for v in (_finite(x) for x in history) if v is not None]
    cur = _finite(current)
    if cur is None or len(values) < MIN_Z_OBS:
        return None
    arr = np.asarray(values, dtype=np.float64)
    std = float(arr.std(ddof=1))
    if not math.isfinite(std) or std == 0:
        return None
    return float((cur - float(arr.mean())) / std)


def _rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        results = payload.get("results")
        if isinstance(results, list):
            return [dict(row) for row in results if isinstance(row, Mapping)]
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, Mapping)]
    return []


def _resolve_as_of(as_of: date | str | None) -> date:
    if as_of is None:
        return datetime.utcnow().date()
    if isinstance(as_of, date):
        return as_of
    return datetime.strptime(str(as_of)[:10], "%Y-%m-%d").date()


def _parse_iso(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Statement loading + point-in-time quarter join.
# --------------------------------------------------------------------------- #
def _pick(row: Mapping[str, Any], fields: Mapping[str, str]) -> dict[str, float | None]:
    return {key: _finite(row.get(source)) for key, source in fields.items()}


def _is_earlier_filing(candidate: Mapping[str, Any], incumbent: Mapping[str, Any]) -> bool:
    """True when ``candidate`` is the earlier original filing of a period_end.

    A row that carries a filing date beats one that has none; between two dated
    rows the earlier date wins. Ties and undatable candidates keep the incumbent.
    """
    cand = _parse_iso(candidate.get("filing_date"))
    inc = _parse_iso(incumbent.get("filing_date"))
    if cand is None:
        return False
    if inc is None:
        return True
    return cand < inc


def load_quarters(
    client: Any,
    ticker: str,
    *,
    as_of: date | str | None = None,
    limit: int = DEFAULT_QUARTERS_LIMIT,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Fetch and join Massive quarterly statements, newest first, filtered to ``t``.

    Returns ``(quarters, errors)`` where each quarter joins the income, balance
    and cash-flow statements on ``period_end`` and is dropped unless its
    ``filing_date`` is on or before ``as_of`` (point-in-time: a report the market
    had not seen at ``t`` cannot appear).
    """
    resolved = _resolve_as_of(as_of)
    errors: list[str] = []
    fetched: dict[str, list[dict[str, Any]]] = {}
    for statement in STATEMENTS:
        try:
            payload = client.get_financials(
                ticker,
                statement=statement,
                params={"timeframe": "quarterly", "limit": int(limit)},
            )
        except Exception as exc:  # noqa: BLE001 - one dead endpoint must not kill the section
            errors.append(f"massive {statement} statements: {exc}")
            continue
        rows = _rows(payload)
        if not rows:
            errors.append(f"massive {statement} statements returned no rows")
        fetched[statement] = rows

    merged: dict[str, dict[str, Any]] = {}
    specs = (("income", INCOME_FIELDS), ("balance", BALANCE_FIELDS), ("cash-flow", CASHFLOW_FIELDS))
    for statement, fields in specs:
        # Massive returns duplicate rows per period_end: the original 10-Q/10-K
        # filing AND the year-later comparative that reappears in the following
        # year's filing. Keep only the EARLIEST-filed row per period_end so the
        # recorded filing_date is the original filing (SPEC 4.5) and the numbers
        # are as-originally-reported — never a later restatement or a filing date
        # ~13 months too late.
        earliest: dict[str, dict[str, Any]] = {}
        for row in fetched.get(statement) or []:
            period_end = str(row.get("period_end") or "").strip()
            if not period_end:
                continue
            incumbent = earliest.get(period_end)
            if incumbent is None or _is_earlier_filing(row, incumbent):
                earliest[period_end] = row
        for period_end, row in earliest.items():
            entry = merged.setdefault(
                period_end,
                {
                    "period_end": period_end,
                    "fiscal_quarter": row.get("fiscal_quarter"),
                    "fiscal_year": row.get("fiscal_year"),
                    "filing_date": None,
                    "statements": [],
                },
            )
            entry["statements"].append(statement)
            entry.update(_pick(row, fields))
            # Across statements the same quarter should share one filing date;
            # keep the earliest actual date on hand.
            row_filed = _parse_iso(row.get("filing_date"))
            if row.get("filing_date"):
                cur_filed = _parse_iso(entry.get("filing_date"))
                if cur_filed is None or (row_filed is not None and row_filed < cur_filed):
                    entry["filing_date"] = row.get("filing_date")

    point_in_time: list[dict[str, Any]] = []
    for entry in merged.values():
        filed = _parse_iso(entry.get("filing_date"))
        # A quarter with no filing date cannot be proven point-in-time safe; only
        # keep it when its period end is safely in the past relative to ``t``.
        if filed is None:
            period = _parse_iso(entry.get("period_end"))
            if period is None or period > resolved:
                continue
        elif filed > resolved:
            continue
        point_in_time.append(_finalise_quarter(entry))

    point_in_time.sort(key=lambda item: str(item.get("period_end") or ""), reverse=True)
    return point_in_time, errors


def _finalise_quarter(entry: dict[str, Any]) -> dict[str, Any]:
    """Add per-quarter derived numbers a memo actually quotes."""
    revenue = _finite(entry.get("revenue"))
    gross_profit = _finite(entry.get("gross_profit"))
    operating_income = _finite(entry.get("operating_income"))
    cfo = _finite(entry.get("cash_from_operations"))
    capex = _finite(entry.get("capex"))
    # Massive reports capex as a negative outflow; FCF subtracts its magnitude so
    # a positive-signed feed cannot inflate free cash flow.
    fcf: float | None = None
    if cfo is not None and capex is not None:
        fcf = cfo - abs(capex)
    debt_current = _finite(entry.get("debt_current")) or 0.0
    long_term_debt = _finite(entry.get("long_term_debt"))
    total_debt = (
        None
        if long_term_debt is None and _finite(entry.get("debt_current")) is None
        else float(debt_current + (long_term_debt or 0.0))
    )
    ebitda = _finite(entry.get("ebitda"))
    if ebitda is None and operating_income is not None:
        dep = _finite(entry.get("depreciation")) or _finite(entry.get("depreciation_income"))
        if dep is not None:
            ebitda = operating_income + abs(dep)
    out = dict(entry)
    out.update(
        {
            "fcf": fcf,
            "total_debt": total_debt,
            "ebitda": ebitda,
            "gross_margin": _ratio(gross_profit, revenue),
            "op_margin": _ratio(operating_income, revenue),
            "fcf_margin": _ratio(fcf, revenue),
            "capex_to_rev": (_ratio(abs(capex), revenue) if capex is not None else None),
        }
    )
    return out


# --------------------------------------------------------------------------- #
# Momentum (from the price panel).
# --------------------------------------------------------------------------- #
def _month_end_levels(prices: pd.Series | None, as_of: date) -> pd.Series:
    if prices is None or len(prices) == 0:
        return pd.Series(dtype="float64")
    series = pd.Series(prices).dropna()
    series.index = pd.DatetimeIndex(pd.to_datetime(series.index)).normalize()
    series = series[series.index <= pd.Timestamp(as_of)]
    if series.empty:
        return series
    monthly = series.resample("ME").last().dropna()
    return monthly


def compute_momentum(
    prices: pd.Series | None, *, as_of: date
) -> tuple[dict[str, float | None], str | None]:
    """12-1 momentum and 1-month reversal from month-end levels.

    ``ret_12_1`` is the cumulative return from twelve months ago to one month ago
    (the most recent month is skipped, the standard momentum construction).
    ``ret_1m_reversal`` is the most recent completed month's return — the raw
    input to the short-term reversal signal (whose sign the reversal literature
    flips).
    """
    monthly = _month_end_levels(prices, as_of)
    if monthly.shape[0] < 13:
        return {"ret_12_1": None, "ret_1m_reversal": None}, (
            f"need >=13 month-end prices, have {monthly.shape[0]}"
        )
    levels = monthly.to_numpy(dtype=np.float64)
    ret_12_1 = _ratio(levels[-2], levels[-13])
    ret_12_1 = (ret_12_1 - 1.0) if ret_12_1 is not None else None
    ret_1m = _ratio(levels[-1], levels[-2])
    ret_1m = (ret_1m - 1.0) if ret_1m is not None else None
    return {"ret_12_1": ret_12_1, "ret_1m_reversal": ret_1m}, None


# --------------------------------------------------------------------------- #
# Quality.
# --------------------------------------------------------------------------- #
def compute_quality(
    quarters: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, float | None], list[str]]:
    """Gross-profitability, accruals, net-debt/EBITDA and interest coverage (TTM)."""
    errors: list[str] = []
    if len(quarters) < TTM_QUARTERS:
        return (
            {"gp_to_assets": None, "accruals": None, "net_debt_ebitda": None, "interest_cov": None},
            [f"need >={TTM_QUARTERS} quarters for TTM, have {len(quarters)}"],
        )
    ttm = quarters[:TTM_QUARTERS]
    latest = quarters[0]

    gross_ttm = _sum([q.get("gross_profit") for q in ttm])
    ebitda_ttm = _sum([q.get("ebitda") for q in ttm])
    ni_ttm = _sum([q.get("net_income") for q in ttm])
    cfo_ttm = _sum([q.get("cash_from_operations") for q in ttm])
    op_ttm = _sum([q.get("operating_income") for q in ttm])
    interest_ttm = _sum([q.get("interest_expense") for q in ttm])
    total_assets = _finite(latest.get("total_assets"))
    total_debt = _finite(latest.get("total_debt"))
    cash = _finite(latest.get("cash")) or 0.0
    sti = _finite(latest.get("short_term_investments")) or 0.0

    gp_to_assets = _ratio(gross_ttm, total_assets)

    accruals: float | None = None
    if ni_ttm is not None and cfo_ttm is not None and total_assets not in (None, 0):
        accruals = (ni_ttm - cfo_ttm) / total_assets  # type: ignore[operator]

    net_debt_ebitda: float | None = None
    if total_debt is not None and ebitda_ttm not in (None, 0):
        net_debt = total_debt - cash - sti
        net_debt_ebitda = net_debt / ebitda_ttm  # type: ignore[operator]
    elif total_debt is None:
        errors.append("net_debt/EBITDA: no debt figures on the latest balance sheet")

    interest_cov: float | None = None
    if op_ttm is not None and interest_ttm is not None and abs(interest_ttm) > 0:
        interest_cov = op_ttm / abs(interest_ttm)
    elif interest_ttm is not None and abs(interest_ttm) == 0:
        errors.append("interest coverage: no interest expense (undefined)")

    return (
        {
            "gp_to_assets": gp_to_assets,
            "accruals": accruals,
            "net_debt_ebitda": net_debt_ebitda,
            "interest_cov": interest_cov,
        },
        errors,
    )


# --------------------------------------------------------------------------- #
# Value z-scores vs the ticker's own history.
# --------------------------------------------------------------------------- #
def _price_asof(prices: pd.Series | None, when: date | None) -> float | None:
    if prices is None or len(prices) == 0 or when is None:
        return None
    series = pd.Series(prices).dropna()
    series.index = pd.DatetimeIndex(pd.to_datetime(series.index)).normalize()
    stamp = pd.Timestamp(when)
    series = series[series.index <= stamp]
    if series.empty:
        return None
    return float(series.iloc[-1])


def _ttm_multiples_at(
    quarters: Sequence[Mapping[str, Any]], start: int, price: float | None
) -> dict[str, float | None] | None:
    """EV/Sales, EV/EBITDA, trailing P/E and FCF yield at quarter index ``start``.

    ``start`` is the newest quarter of the trailing window; the window is the four
    quarters ``start .. start+3``. ``price`` is the close as of that quarter's
    filing date, so the multiple reflects only information available then.
    """
    if price is None or start + TTM_QUARTERS > len(quarters):
        return None
    window = quarters[start : start + TTM_QUARTERS]
    shares = _finite(quarters[start].get("shares"))
    if shares is None or shares <= 0:
        return None
    market_cap = price * shares
    rev_ttm = _sum([q.get("revenue") for q in window])
    ebitda_ttm = _sum([q.get("ebitda") for q in window])
    eps_ttm = _sum([q.get("eps") for q in window])
    fcf_ttm = _sum([q.get("fcf") for q in window])
    total_debt = _finite(quarters[start].get("total_debt"))
    cash = _finite(quarters[start].get("cash")) or 0.0
    sti = _finite(quarters[start].get("short_term_investments")) or 0.0
    ev = market_cap + (total_debt or 0.0) - cash - sti
    return {
        "ev_sales": _ratio(ev, rev_ttm),
        "ev_ebitda": (_ratio(ev, ebitda_ttm) if (ebitda_ttm or 0) > 0 else None),
        "pe": (_ratio(price, eps_ttm) if (eps_ttm or 0) > 0 else None),
        "fcf_yield": _ratio(fcf_ttm, market_cap),
    }


def compute_value_z(
    quarters: Sequence[Mapping[str, Any]],
    prices: pd.Series | None,
    *,
    current_price: float | None,
) -> tuple[dict[str, Any], list[str]]:
    """Value multiples expressed as z-scores versus the ticker's own history."""
    errors: list[str] = []
    keys = ("ev_sales", "ev_ebitda", "pe", "fcf_yield")
    if len(quarters) < TTM_QUARTERS + MIN_Z_OBS:
        return (
            {
                "ev_sales": None,
                "ev_ebitda": None,
                "pe_fwd": None,
                "fcf_yield": None,
                "basis": "own_history",
                "n_obs": 0,
                "pe_fwd_error": NO_ESTIMATES_REASON,
            },
            [f"value z: need >={TTM_QUARTERS + MIN_Z_OBS} quarters, have {len(quarters)}"],
        )

    history: dict[str, list[float]] = {key: [] for key in keys}
    for start in range(0, len(quarters) - TTM_QUARTERS + 1):
        filed = _parse_iso(quarters[start].get("filing_date")) or _parse_iso(
            quarters[start].get("period_end")
        )
        price = _price_asof(prices, filed)
        multiples = _ttm_multiples_at(quarters, start, price)
        if multiples is None:
            continue
        for key in keys:
            value = _finite(multiples.get(key))
            if value is not None:
                history[key].append(value)

    # The "current" observation uses the latest window with the current price.
    fallback_price = current_price or _price_asof(prices, _resolve_as_of(None))
    current = _ttm_multiples_at(quarters, 0, fallback_price)
    years = round(len(quarters) / 4.0, 1)
    result: dict[str, Any] = {
        "ev_sales": None,
        "ev_ebitda": None,
        "pe_fwd": None,
        "fcf_yield": None,
        "basis": f"own_{years}y",
        "n_obs": max((len(history[key]) for key in keys), default=0),
        "levels": (dict(current) if current else None),
        "pe_fwd_error": NO_ESTIMATES_REASON,
    }
    if current is not None:
        result["ev_sales"] = _z_score(current.get("ev_sales"), history["ev_sales"])
        result["ev_ebitda"] = _z_score(current.get("ev_ebitda"), history["ev_ebitda"])
        result["fcf_yield"] = _z_score(current.get("fcf_yield"), history["fcf_yield"])
    else:
        errors.append("value z: no current price to compute multiples")
    return result, errors


# --------------------------------------------------------------------------- #
# Eight-quarter trajectory (filing-date keyed) with slope + acceleration.
# --------------------------------------------------------------------------- #
def _slope(values: Sequence[float | None]) -> float | None:
    points = [(i, v) for i, v in enumerate(_finite(x) for x in values) if v is not None]
    if len(points) < 3:
        return None
    xs = np.asarray([p[0] for p in points], dtype=np.float64)
    ys = np.asarray([p[1] for p in points], dtype=np.float64)
    slope, _ = np.polyfit(xs, ys, 1)
    return float(slope) if math.isfinite(slope) else None


def _accel_flag(values: Sequence[float | None]) -> bool | None:
    """Second-derivative flag: is the series accelerating on average?"""
    clean = [_finite(x) for x in values]
    clean = [v for v in clean if v is not None]  # type: ignore[assignment]
    if len(clean) < 3:
        return None
    arr = np.asarray(clean, dtype=np.float64)
    second = np.diff(arr, n=2)
    if second.size == 0:
        return None
    return bool(float(second.mean()) > 0)


def compute_trajectory(
    quarters: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, bool | None]]:
    """Last eight quarters (oldest->newest for display) with YoY growth + flags."""
    if not quarters:
        return [], {"rev_accel": None, "margin_accel": None}
    # ``quarters`` is newest-first; build YoY revenue growth needing the quarter
    # four periods earlier (same fiscal quarter, prior year).
    rows: list[dict[str, Any]] = []
    for i in range(min(TRAJECTORY_QUARTERS, len(quarters))):
        q = quarters[i]
        rev = _finite(q.get("revenue"))
        rev_growth: float | None = None
        if i + TTM_QUARTERS < len(quarters):
            prior = _finite(quarters[i + TTM_QUARTERS].get("revenue"))
            if rev is not None and prior is not None and prior > 0:
                rev_growth = rev / prior - 1.0
        rows.append(
            {
                "period_end": q.get("period_end"),
                "filing_date": q.get("filing_date"),
                "rev_growth": rev_growth,
                "gross_margin": q.get("gross_margin"),
                "op_margin": q.get("op_margin"),
                "fcf_margin": q.get("fcf_margin"),
                "capex_to_rev": q.get("capex_to_rev"),
            }
        )
    rows.reverse()  # oldest -> newest for display and slope math
    rev_series = [r["rev_growth"] for r in rows]
    margin_series = [r["op_margin"] for r in rows]
    flags = {
        "rev_accel": _accel_flag(rev_series),
        "margin_accel": _accel_flag(margin_series),
        "rev_growth_slope": _slope(rev_series),
        "op_margin_slope": _slope(margin_series),
    }
    return rows, flags  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# Section assembly.
# --------------------------------------------------------------------------- #
def build_fundamentals(
    quarters: Sequence[Mapping[str, Any]],
    prices: pd.Series | None,
    *,
    as_of: date,
    current_price: float | None = None,
    load_errors: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Assemble the ``fundamentals`` section from joined quarters + a price series.

    Pure and offline: the network fetch lives in :func:`build_fundamentals_section`
    so this can be unit-tested with hand-built quarters and a synthetic price
    series (and so the lookahead test can mask data after ``t`` and recompute).
    """
    errors: list[dict[str, str]] = []
    for message in load_errors or []:
        errors.append({"source": "fundamentals.load", "error": str(message)})

    momentum, mom_error = compute_momentum(prices, as_of=as_of)
    if mom_error:
        errors.append({"source": "fundamentals.momentum", "error": mom_error})

    quality, quality_errors = compute_quality(quarters)
    for message in quality_errors:
        errors.append({"source": "fundamentals.quality", "error": message})

    resolved_price = current_price if current_price is not None else _price_asof(prices, as_of)
    value_z, value_errors = compute_value_z(quarters, prices, current_price=resolved_price)
    for message in value_errors:
        errors.append({"source": "fundamentals.value", "error": message})

    trajectory, trajectory_flags = compute_trajectory(quarters)

    latest = quarters[0] if quarters else {}
    section: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "n_quarters": len(quarters),
        "latest_period_end": latest.get("period_end"),
        "latest_filing_date": latest.get("filing_date"),
        "momentum": momentum,
        "quality": quality,
        "value_z": value_z,
        "trajectory": trajectory,
        "trajectory_flags": trajectory_flags,
        # Unavailable without a consensus-estimate feed — stated, never faked.
        "revisions": None,
        "pead": None,
        "revisions_error": NO_ESTIMATES_REASON,
        "pead_error": NO_ESTIMATES_REASON,
        "version": MODULE_VERSION,
        "errors": errors,
    }
    return section


def build_fundamentals_section(
    client: Any,
    ticker: str,
    *,
    prices: pd.Series | None = None,
    as_of: date | str | None = None,
    current_price: float | None = None,
    limit: int = DEFAULT_QUARTERS_LIMIT,
) -> dict[str, Any]:
    """Engine entry point: fetch statements + build the ``fundamentals`` section.

    ``prices`` is the ticker's daily-close series (from the Situate panel), used
    for momentum and for the point-in-time price behind every value multiple.
    Raises :class:`FundamentalsError` only when *no* statements load at all; a
    partial feed degrades field-by-field with reasons in ``errors``.
    """
    resolved = _resolve_as_of(as_of)
    quarters, load_errors = load_quarters(client, ticker, as_of=resolved, limit=limit)
    if not quarters:
        raise FundamentalsError(
            "; ".join(load_errors) or f"{ticker}: no quarterly financials available"
        )
    return build_fundamentals(
        quarters,
        prices,
        as_of=resolved,
        current_price=current_price,
        load_errors=load_errors,
    )
