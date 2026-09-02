"""Quarterly fundamentals, derived ratios, forecast and business-stage read.

Massive's ``/stocks/financials/v1/*`` endpoints are the primary source: income
statements, balance sheets and cash-flow statements come back per fiscal quarter
and are joined here on ``period_end``. The ``ratios`` endpoint ignores the
``tickers`` filter used by the statement endpoints, so it is queried with the
singular ``ticker`` parameter instead — verified live on 2026-09-01, where
``tickers=NVDA`` returned 2,000 rows for the whole market and ``ticker=NVDA``
returned exactly one.

When Massive has no statements for a symbol the section falls back to SEC EDGAR
XBRL via :func:`app.sec_trend.build_sec_trend_pack`, which mines companyfacts for
the same metrics. Either way, a number that cannot be computed is ``None`` with a
reason recorded in ``errors`` — never a placeholder.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

DEFAULT_QUARTERS = 8

#: Quarters loaded behind the reported window so year-over-year *acceleration*
#: (this quarter's growth against the year-ago quarter's growth) has a base.
EXTENDED_QUARTERS = 13

#: Massive statement endpoints joined into one quarterly row.
STATEMENTS: tuple[str, ...] = ("income", "balance", "cash-flow")

#: Fields lifted from each statement, mapped onto the packet's quarter keys.
INCOME_FIELDS: dict[str, str] = {
    "revenue": "revenue",
    "cost_of_revenue": "cost_of_revenue",
    "gross_profit": "gross_profit",
    "operating_income": "operating_income",
    "net_income": "net_income_loss_attributable_common_shareholders",
    "eps": "diluted_earnings_per_share",
    "basic_eps": "basic_earnings_per_share",
    "shares": "diluted_shares_outstanding",
    "ebitda": "ebitda",
    "research_development": "research_development",
    "interest_expense": "interest_expense",
    "income_taxes": "income_taxes",
    "pretax_income": "income_before_income_taxes",
}
BALANCE_FIELDS: dict[str, str] = {
    "cash": "cash_and_equivalents",
    "short_term_investments": "short_term_investments",
    "inventories": "inventories",
    "receivables": "receivables",
    "total_assets": "total_assets",
    "total_liabilities": "total_liabilities",
    "total_equity": "total_equity",
    "debt_current": "debt_current",
    "long_term_debt": "long_term_debt_and_capital_lease_obligations",
    "goodwill": "goodwill",
}
CASHFLOW_FIELDS: dict[str, str] = {
    "cash_from_operations": "net_cash_from_operating_activities",
    "capex": "purchase_of_property_plant_and_equipment",
    "dividends_paid": "dividends",
    "depreciation": "depreciation_depletion_and_amortization",
}

#: Stage labels the memo's fundamentals tilt understands.
STAGES: tuple[str, ...] = ("turnaround", "growing", "peaking", "declining", "stable")

_MARGIN_TOLERANCE = 0.005
_GROWTH_TOLERANCE = 0.02


def _finite(value: Any) -> float | None:
    """Coerce to a finite float, or ``None`` — never a silent zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _ratio(numerator: Any, denominator: Any) -> float | None:
    """``numerator / denominator`` when both are finite and the divisor is non-zero."""
    top = _finite(numerator)
    bottom = _finite(denominator)
    if top is None or bottom is None or bottom == 0:
        return None
    return top / bottom


def _growth(current: Any, prior: Any) -> float | None:
    """Percent change with a positive base; sign flips are reported as ``None``.

    A swing from a loss to a profit has no meaningful percentage, and inventing
    one ("+430% earnings growth" off a negative base) is exactly the kind of
    number a memo should never carry.
    """
    now = _finite(current)
    before = _finite(prior)
    if now is None or before is None or before <= 0:
        return None
    return now / before - 1.0


def _sum(values: Sequence[Any]) -> float | None:
    """Sum a window only when every element is present."""
    numbers = [_finite(value) for value in values]
    if not numbers or any(number is None for number in numbers):
        return None
    return float(sum(number for number in numbers if number is not None))


def _mean(values: Sequence[Any]) -> float | None:
    numbers = [number for number in (_finite(value) for value in values) if number is not None]
    if not numbers:
        return None
    return float(sum(numbers) / len(numbers))


def _rows(payload: Any) -> list[dict[str, Any]]:
    """Massive envelopes wrap rows in ``results``; anything else yields nothing."""
    if isinstance(payload, Mapping):
        results = payload.get("results")
        if isinstance(results, list):
            return [dict(row) for row in results if isinstance(row, Mapping)]
    if isinstance(payload, list):
        return [dict(row) for row in payload if isinstance(row, Mapping)]
    return []


def fetch_statements(
    client: Any,
    ticker: str,
    *,
    timeframe: str = "quarterly",
    statements: Sequence[str] = STATEMENTS,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """Fetch each statement endpoint, returning ``(rows_by_statement, errors)``."""
    fetched: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for statement in statements:
        try:
            payload = client.get_financials(
                ticker, statement=statement, params={"timeframe": timeframe}
            )
        except Exception as exc:  # noqa: BLE001 - one dead endpoint must not kill the section
            errors.append(f"massive {statement} statements: {exc}")
            continue
        rows = _rows(payload)
        if not rows:
            errors.append(f"massive {statement} statements returned no rows")
        fetched[statement] = rows
    return fetched, errors


def fetch_current_ratios(client: Any, ticker: str) -> tuple[dict[str, Any] | None, str | None]:
    """Massive's current ratio snapshot for one ticker.

    The endpoint's own filter key is ``ticker`` (singular). The statement wrapper
    always sends ``tickers``, which this endpoint ignores, so the singular key is
    passed through explicitly.
    """
    try:
        payload = client.get_financials(ticker, statement="ratios", params={"ticker": ticker})
    except Exception as exc:  # noqa: BLE001
        return None, f"massive ratios: {exc}"
    symbol = str(ticker).strip().upper()
    for row in _rows(payload):
        if str(row.get("ticker") or "").strip().upper() == symbol:
            return dict(row), None
    return None, "massive ratios returned no row for this ticker"


def _pick(row: Mapping[str, Any], fields: Mapping[str, str]) -> dict[str, float | None]:
    return {key: _finite(row.get(source)) for key, source in fields.items()}


def merge_quarters(
    fetched: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    limit: int = DEFAULT_QUARTERS,
) -> list[dict[str, Any]]:
    """Join the three statements on ``period_end``, most recent quarter first."""
    merged: dict[str, dict[str, Any]] = {}
    specs = (
        ("income", INCOME_FIELDS),
        ("balance", BALANCE_FIELDS),
        ("cash-flow", CASHFLOW_FIELDS),
    )
    for statement, fields in specs:
        for row in fetched.get(statement) or []:
            period_end = str(row.get("period_end") or "").strip()
            if not period_end:
                continue
            entry = merged.setdefault(
                period_end,
                {
                    "period_end": period_end,
                    "fiscal_quarter": row.get("fiscal_quarter"),
                    "fiscal_year": row.get("fiscal_year"),
                    "filing_date": row.get("filing_date"),
                    "statements": [],
                },
            )
            entry["statements"].append(statement)
            entry.update(_pick(row, fields))
            entry.setdefault("fiscal_quarter", row.get("fiscal_quarter"))

    ordered = sorted(merged.values(), key=lambda item: str(item["period_end"]), reverse=True)
    quarters: list[dict[str, Any]] = []
    for entry in ordered[: max(1, int(limit))]:
        quarters.append(_finalise_quarter(entry))
    return quarters


def _finalise_quarter(entry: dict[str, Any]) -> dict[str, Any]:
    """Add the derived per-quarter numbers a memo actually quotes."""
    revenue = _finite(entry.get("revenue"))
    gross_profit = _finite(entry.get("gross_profit"))
    operating_income = _finite(entry.get("operating_income"))
    net_income = _finite(entry.get("net_income"))
    cfo = _finite(entry.get("cash_from_operations"))
    capex = _finite(entry.get("capex"))
    # Massive reports capex as a negative cash outflow; FCF is CFO plus that
    # signed number, so a positive-signed feed cannot inflate free cash flow.
    fcf: float | None = None
    if cfo is not None and capex is not None:
        fcf = cfo - abs(capex)
    debt_current = _finite(entry.get("debt_current")) or 0.0
    long_term_debt = _finite(entry.get("long_term_debt"))
    total_debt = (
        None
        if long_term_debt is None and not _finite(entry.get("debt_current"))
        else float(debt_current + (long_term_debt or 0.0))
    )
    quarter = dict(entry)
    quarter.update(
        {
            "fcf": fcf,
            "total_debt": total_debt,
            "gross_margin": _ratio(gross_profit, revenue),
            "operating_margin": _ratio(operating_income, revenue),
            "net_margin": _ratio(net_income, revenue),
            "fcf_margin": _ratio(fcf, revenue),
        }
    )
    return quarter


def trailing_twelve_months(quarters: Sequence[Mapping[str, Any]]) -> dict[str, float | None]:
    """TTM flow metrics from the four most recent quarters (``None`` if short)."""
    window = list(quarters[:4])
    if len(window) < 4:
        return dict.fromkeys(
            ("revenue", "gross_profit", "operating_income", "net_income", "eps", "fcf", "ebitda")
        )
    return {
        "revenue": _sum([row.get("revenue") for row in window]),
        "gross_profit": _sum([row.get("gross_profit") for row in window]),
        "operating_income": _sum([row.get("operating_income") for row in window]),
        "net_income": _sum([row.get("net_income") for row in window]),
        "eps": _sum([row.get("eps") for row in window]),
        "fcf": _sum([row.get("fcf") for row in window]),
        "ebitda": _sum([row.get("ebitda") for row in window]),
    }


def derive_ratios(
    quarters: Sequence[Mapping[str, Any]],
    *,
    current_price: float | None = None,
    market_cap: float | None = None,
    provider_ratios: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Valuation and balance-sheet ratios, plus a per-key source map.

    Ratios computed from the statements win; Massive's own ratio snapshot fills
    the gaps and supplies the numbers no statement carries (dividend yield,
    returns on capital). ``ratios_source`` names which is which so a reader can
    tell a derived P/E from a vendor P/E.
    """
    provider = dict(provider_ratios or {})
    ttm = trailing_twelve_months(quarters)
    latest: Mapping[str, Any] = quarters[0] if quarters else {}
    price = _finite(current_price) or _finite(provider.get("price"))
    vendor_cap = _finite(market_cap) or _finite(provider.get("market_cap"))
    shares = _finite(latest.get("shares"))
    # Strike the cap off the same price every other section quotes. The vendor
    # cap is a snapshot taken at whatever price the vendor last saw, so mixing it
    # with the packet's close makes P/S and P/B disagree with P/E by ~1%. The
    # vendor figure is kept alongside rather than dropped.
    cap = price * shares if price is not None and shares is not None else vendor_cap

    cash = _finite(latest.get("cash"))
    investments = _finite(latest.get("short_term_investments")) or 0.0
    total_debt = _finite(latest.get("total_debt"))
    equity = _finite(latest.get("total_equity"))
    enterprise_value: float | None = None
    if cap is not None and total_debt is not None and cash is not None:
        enterprise_value = cap + total_debt - cash - investments

    derived: dict[str, float | None] = {
        "pe": _ratio(price, ttm["eps"]),
        "ps": _ratio(cap, ttm["revenue"]),
        "pb": _ratio(cap, equity),
        "ev_ebitda": _ratio(enterprise_value, ttm["ebitda"]),
        "ev_ebit": _ratio(enterprise_value, ttm["operating_income"]),
        "ev_sales": _ratio(enterprise_value, ttm["revenue"]),
        "debt_to_equity": _ratio(total_debt, equity),
        "fcf_yield": _ratio(ttm["fcf"], cap),
        "nav_per_share": _ratio(equity, shares),
        "net_cash_per_share": (
            None
            if shares is None or cash is None or total_debt is None
            else (cash + investments - total_debt) / shares
        ),
        "gross_margin_ttm": _ratio(ttm["gross_profit"], ttm["revenue"]),
        "operating_margin_ttm": _ratio(ttm["operating_income"], ttm["revenue"]),
        "net_margin_ttm": _ratio(ttm["net_income"], ttm["revenue"]),
    }

    provider_map: dict[str, str] = {
        "pe": "price_to_earnings",
        "ps": "price_to_sales",
        "pb": "price_to_book",
        "ev_ebitda": "ev_to_ebitda",
        "ev_sales": "ev_to_sales",
        "debt_to_equity": "debt_to_equity",
        "dividend_yield": "dividend_yield",
        "return_on_equity": "return_on_equity",
        "return_on_assets": "return_on_assets",
        "current_ratio": "current",
        "quick_ratio": "quick",
        "price_to_free_cash_flow": "price_to_free_cash_flow",
    }

    ratios: dict[str, Any] = {}
    source: dict[str, str] = {}
    for key, value in derived.items():
        if value is not None:
            ratios[key] = value
            source[key] = "derived_from_statements"
    for key, provider_key in provider_map.items():
        if key in ratios:
            continue
        value = _finite(provider.get(provider_key))
        if value is not None:
            ratios[key] = value
            source[key] = "massive_ratios"
    for key in provider_map:
        ratios.setdefault(key, None)
    ratios["current_price"] = price
    ratios["market_cap"] = cap
    ratios["market_cap_provider"] = vendor_cap
    ratios["enterprise_value"] = enterprise_value
    ratios["shares_outstanding"] = shares
    ratios["ttm"] = ttm
    if price is not None:
        source.setdefault("current_price", "massive")
    source["market_cap"] = (
        "price_x_shares_outstanding"
        if price is not None and shares is not None
        else "provider"
    )
    return ratios, source


def growth_metrics(quarters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Year-over-year and sequential growth plus a margin-trend label."""
    if not quarters:
        return {
            "revenue_yoy": None,
            "revenue_qoq": None,
            "net_income_yoy": None,
            "eps_yoy": None,
            "gross_margin_change_yoy": None,
            "operating_margin_change_yoy": None,
            "margin_trend": "unknown",
            "revenue_growth_acceleration": None,
        }
    latest = quarters[0]
    prior_q = quarters[1] if len(quarters) > 1 else {}
    year_ago = quarters[4] if len(quarters) > 4 else {}
    two_years = quarters[8] if len(quarters) > 8 else {}

    revenue_yoy = _growth(latest.get("revenue"), year_ago.get("revenue"))
    prior_yoy = _growth(year_ago.get("revenue"), two_years.get("revenue"))
    gm_now = _finite(latest.get("gross_margin"))
    gm_then = _finite(year_ago.get("gross_margin"))
    om_now = _finite(latest.get("operating_margin"))
    om_then = _finite(year_ago.get("operating_margin"))
    gm_change = None if gm_now is None or gm_then is None else gm_now - gm_then
    om_change = None if om_now is None or om_then is None else om_now - om_then

    trend = "unknown"
    reference = om_change if om_change is not None else gm_change
    if reference is not None:
        if reference > _MARGIN_TOLERANCE:
            trend = "expanding"
        elif reference < -_MARGIN_TOLERANCE:
            trend = "compressing"
        else:
            trend = "flat"

    return {
        "revenue_yoy": revenue_yoy,
        "revenue_qoq": _growth(latest.get("revenue"), prior_q.get("revenue")),
        "net_income_yoy": _growth(latest.get("net_income"), year_ago.get("net_income")),
        "eps_yoy": _growth(latest.get("eps"), year_ago.get("eps")),
        "gross_margin_change_yoy": gm_change,
        "operating_margin_change_yoy": om_change,
        "margin_trend": trend,
        "revenue_growth_acceleration": (
            None if revenue_yoy is None or prior_yoy is None else revenue_yoy - prior_yoy
        ),
    }


def moving_averages(quarters: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Four-quarter averages and the latest quarter's distance from them."""
    metrics = ("revenue", "gross_profit", "operating_income", "net_income", "fcf")
    window = list(quarters[:4])
    result: dict[str, Any] = {"window_quarters": len(window)}
    for metric in metrics:
        average = _mean([row.get(metric) for row in window])
        latest = _finite(quarters[0].get(metric)) if quarters else None
        result[metric] = {
            "ma_4q": average,
            "latest": latest,
            "latest_vs_ma": (
                None
                if average is None or latest is None or average == 0
                else latest / average - 1.0
            ),
        }
    return result


def forecast_next_quarters(
    quarters: Sequence[Mapping[str, Any]], *, periods: int = 4
) -> dict[str, Any]:
    """Linear revenue trend with a per-fiscal-quarter seasonal factor.

    Fit ``revenue ~ a + b * t`` over the available quarters (oldest first), take
    the multiplicative residual of each fiscal quarter as its seasonal factor,
    and carry the trailing net margin forward for the earnings line. With fewer
    than six quarters there is not enough to separate trend from season, so the
    forecast is refused rather than guessed.
    """
    ordered = list(reversed(list(quarters)))
    revenues = [(index, _finite(row.get("revenue")), row) for index, row in enumerate(ordered)]
    usable = [(index, value, row) for index, value, row in revenues if value is not None]
    if len(usable) < 6:
        return {
            "method": "linear+seasonal",
            "next_4q": [],
            "error": f"needs at least 6 quarters of revenue, found {len(usable)}",
        }

    xs = [float(index) for index, _value, _row in usable]
    ys = [float(value) for _index, value, _row in usable if value is not None]
    n = float(len(xs))
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    slope = 0.0 if variance == 0 else sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)
    ) / variance
    intercept = mean_y - slope * mean_x

    seasonal: dict[int, list[float]] = {}
    for index, value, row in usable:
        fitted = intercept + slope * index
        quarter = row.get("fiscal_quarter")
        if fitted > 0 and value is not None and isinstance(quarter, int | float):
            seasonal.setdefault(int(quarter), []).append(float(value) / fitted)
    factors = {quarter: sum(values) / len(values) for quarter, values in seasonal.items()}

    margin = _mean([row.get("net_margin") for row in list(quarters)[:4]])
    gross = _mean([row.get("gross_margin") for row in list(quarters)[:4]])
    shares = _finite(quarters[0].get("shares")) if quarters else None

    last_index = usable[-1][0]
    last_row = ordered[last_index]
    last_quarter = last_row.get("fiscal_quarter")
    last_year = last_row.get("fiscal_year")
    projections: list[dict[str, Any]] = []
    for step in range(1, max(1, int(periods)) + 1):
        index = last_index + step
        trend_value = intercept + slope * index
        quarter_number = (
            ((int(last_quarter) - 1 + step) % 4) + 1
            if isinstance(last_quarter, int | float)
            else None
        )
        year = (
            int(last_year) + (int(last_quarter) - 1 + step) // 4
            if isinstance(last_quarter, int | float) and isinstance(last_year, int | float)
            else None
        )
        factor = factors.get(quarter_number, 1.0) if quarter_number is not None else 1.0
        revenue = trend_value * factor if trend_value > 0 else None
        projections.append(
            {
                "step": step,
                "fiscal_quarter": quarter_number,
                "fiscal_year": year,
                "revenue": revenue,
                "seasonal_factor": factor,
                "gross_profit": None if revenue is None or gross is None else revenue * gross,
                "net_income": None if revenue is None or margin is None else revenue * margin,
                "eps": (
                    None
                    if revenue is None or margin is None or not shares
                    else revenue * margin / shares
                ),
            }
        )

    forward_revenue = _sum([row.get("revenue") for row in projections])
    trailing_revenue = trailing_twelve_months(quarters)["revenue"]
    return {
        "method": "linear+seasonal",
        "slope_per_quarter": slope,
        "intercept": intercept,
        "seasonal_factors": {str(key): value for key, value in sorted(factors.items())},
        "net_margin_used": margin,
        "gross_margin_used": gross,
        "next_4q": projections,
        "forward_revenue_ntm": forward_revenue,
        "implied_revenue_growth": (
            None
            if forward_revenue is None or trailing_revenue is None or trailing_revenue <= 0
            else forward_revenue / trailing_revenue - 1.0
        ),
        "error": None,
    }


def classify_stage(
    quarters: Sequence[Mapping[str, Any]],
    growth: Mapping[str, Any],
    *,
    forecast: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Label the business cycle stage and say exactly what carried the label."""
    evidence: list[str] = []
    revenue_yoy = _finite(growth.get("revenue_yoy"))
    acceleration = _finite(growth.get("revenue_growth_acceleration"))
    margin_trend = str(growth.get("margin_trend") or "unknown")
    net_income = _finite(quarters[0].get("net_income")) if quarters else None
    year_ago_income = _finite(quarters[4].get("net_income")) if len(quarters) > 4 else None

    if revenue_yoy is None:
        return {
            "label": "unknown",
            "evidence": ["no year-over-year revenue comparison available"],
            "confidence": 0.0,
        }

    evidence.append(f"revenue {revenue_yoy:+.1%} year over year")
    if acceleration is not None:
        evidence.append(
            f"growth {'accelerating' if acceleration > 0 else 'decelerating'} "
            f"by {abs(acceleration):.1%} versus the year-ago growth rate"
        )
    evidence.append(f"margins {margin_trend}")

    label = "stable"
    confidence = 0.4
    if net_income is not None and year_ago_income is not None and year_ago_income < 0 <= net_income:
        label = "turnaround"
        confidence = 0.7
        evidence.append("net income crossed from a loss to a profit versus the year-ago quarter")
    elif revenue_yoy > 0.15 and margin_trend != "compressing":
        label = "growing"
        confidence = 0.7 if (acceleration or 0.0) >= 0 else 0.55
    elif revenue_yoy > 0.15 and margin_trend == "compressing":
        label = "peaking"
        confidence = 0.6
        evidence.append("revenue still growing quickly while margins give ground")
    elif revenue_yoy < -_GROWTH_TOLERANCE:
        label = "declining"
        confidence = 0.65
    elif acceleration is not None and acceleration < -0.10 and revenue_yoy < 0.15:
        label = "peaking"
        confidence = 0.5
        evidence.append("growth rate has rolled over from its year-ago pace")

    implied = _finite((forecast or {}).get("implied_revenue_growth"))
    if implied is not None:
        evidence.append(
            f"trend-plus-seasonal forecast implies {implied:+.1%} revenue over the next year"
        )

    return {"label": label, "evidence": evidence, "confidence": confidence}


def quarters_from_sec_trend(
    pack: Mapping[str, Any], *, limit: int = DEFAULT_QUARTERS
) -> list[dict[str, Any]]:
    """Adapt an :mod:`app.sec_trend` pack into this module's quarter rows."""
    rows: list[dict[str, Any]] = []
    for row in (pack.get("Quarters") or [])[: max(1, int(limit))]:
        if not isinstance(row, Mapping):
            continue
        revenue = _finite(row.get("revenue"))
        cfo = _finite(row.get("cash_from_operations"))
        capex = _finite(row.get("capex"))
        entry = {
            "period_end": str(row.get("period_end") or ""),
            "fiscal_quarter": _fiscal_quarter_number(row.get("fiscal_period")),
            "fiscal_year": row.get("fiscal_year"),
            "filing_date": row.get("filed_date"),
            "form": row.get("form"),
            "statements": ["sec_xbrl"],
            "revenue": revenue,
            "gross_profit": _finite(row.get("gross_profit")),
            "operating_income": _finite(row.get("operating_income")),
            "net_income": _finite(row.get("net_income")),
            "eps": _finite(row.get("diluted_eps")),
            "shares": _finite(row.get("diluted_shares")),
            "cash": _finite(row.get("cash")),
            "long_term_debt": _finite(row.get("long_term_debt")),
            "debt_current": None,
            "total_equity": _finite(row.get("total_equity")),
            "total_assets": _finite(row.get("total_assets")),
            "inventories": _finite(row.get("inventory")),
            "cash_from_operations": cfo,
            "capex": capex,
            "ebitda": None,
        }
        entry["total_debt"] = _finite(row.get("long_term_debt"))
        entry["fcf"] = None if cfo is None or capex is None else cfo - abs(capex)
        entry["gross_margin"] = _ratio(entry["gross_profit"], revenue)
        entry["operating_margin"] = _ratio(entry["operating_income"], revenue)
        entry["net_margin"] = _ratio(entry["net_income"], revenue)
        entry["fcf_margin"] = _ratio(entry["fcf"], revenue)
        rows.append(entry)
    rows.sort(key=lambda item: str(item.get("period_end") or ""), reverse=True)
    return rows


def _fiscal_quarter_number(value: Any) -> int | None:
    """``"Q3"`` / ``3`` / ``"FY"`` -> ``3`` / ``3`` / ``None``."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = int(value)
        return number if 1 <= number <= 4 else None
    text = str(value or "").strip().upper()
    if text.startswith("Q") and text[1:].isdigit():
        number = int(text[1:])
        return number if 1 <= number <= 4 else None
    return None


def build_fundamentals(
    client: Any,
    ticker: str,
    *,
    sec_client: Any | None = None,
    current_price: float | None = None,
    market_cap: float | None = None,
    limit: int = DEFAULT_QUARTERS,
) -> dict[str, Any]:
    """Build ``packet["fundamentals"]``.

    Never raises: every failure path lands in ``errors`` and leaves the numbers it
    could not compute as ``None``.
    """
    symbol = str(ticker or "").strip().upper()
    errors: list[str] = []
    sources: list[dict[str, Any]] = []
    fetched_at = datetime.now(UTC).isoformat()

    extended: list[dict[str, Any]] = []
    fetched, statement_errors = fetch_statements(client, symbol)
    errors.extend(statement_errors)
    # Keep ``limit`` quarters in the packet, but derive growth and the forecast
    # from a longer window: year-over-year acceleration compares this quarter's
    # growth against the year-ago quarter's, which needs nine quarters, not eight.
    extended = merge_quarters(fetched, limit=max(int(limit), EXTENDED_QUARTERS))
    quarters = extended[: max(1, int(limit))]
    provider = "massive"

    if not quarters and sec_client is not None:
        try:
            from app.sec_trend import build_sec_trend_pack

            pack = build_sec_trend_pack(sec_client, symbol, quarters=limit)
        except Exception as exc:  # noqa: BLE001
            pack = {}
            errors.append(f"sec trend fallback: {exc}")
        if pack:
            extended = quarters_from_sec_trend(pack, limit=max(int(limit), EXTENDED_QUARTERS))
            quarters = extended[: max(1, int(limit))]
            if quarters:
                provider = "sec_edgar_xbrl"
                sources.append(
                    {
                        "provider": "sec",
                        "url": f"https://data.sec.gov/api/xbrl/companyfacts/CIK{pack.get('CIK')}.json",
                        "fetched_at": fetched_at,
                        "note": "Massive statements unavailable; XBRL companyfacts used instead",
                    }
                )
            for message in pack.get("Errors") or []:
                errors.append(f"sec trend: {message}")

    provider_ratios, ratio_error = (None, None)
    if provider == "massive":
        provider_ratios, ratio_error = fetch_current_ratios(client, symbol)
        if ratio_error:
            errors.append(ratio_error)
        else:
            sources.append(
                {
                    "provider": "massive",
                    "url": "/stocks/financials/v1/ratios",
                    "fetched_at": fetched_at,
                    "note": "current valuation ratio snapshot",
                }
            )

    if quarters and provider == "massive":
        for statement in fetched:
            sources.append(
                {
                    "provider": "massive",
                    "url": f"/stocks/financials/v1/{statement}",
                    "fetched_at": fetched_at,
                    "note": f"{len(fetched[statement])} quarterly rows",
                }
            )

    ratios, ratio_source = derive_ratios(
        quarters,
        current_price=current_price,
        market_cap=market_cap,
        provider_ratios=provider_ratios,
    )
    growth = growth_metrics(extended)
    forecast = forecast_next_quarters(extended)
    stage = classify_stage(extended, growth, forecast=forecast)

    return {
        "provider": provider,
        "fetched_at": fetched_at,
        "ticker": symbol,
        "quarters": quarters,
        "quarter_order": "most_recent_first",
        "quarters_analysed": len(extended),
        "ratios": ratios,
        "ratios_source": ratio_source,
        "growth": growth,
        "moving_averages": moving_averages(quarters),
        "forecast": forecast,
        "stage": stage,
        "sources": sources,
        "errors": errors,
    }
