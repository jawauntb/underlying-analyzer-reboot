"""Misclassified Revenue Torque composite indicator.

Torque surfaces "MXL" setups: old-noun companies (stale industry tag) showing
revenue inflection, margin torque, stale valuation, and a non-euphoric
technical structure. It pairs with Ridge Growth (pure technical trend) and
Flow Compass (pure volume/momentum) — Torque is the fundamental-driven leg
of the cockpit triad.

The composite score is a weighted blend of six components on a 0-100 scale.
Each component returns a ``TorqueComponent`` carrying its score, weight, and
a one-line human readable detail string. The final ``TorqueResult`` includes
a stage label, recommendation, and a suggested target zone.

Robustness contract:
    * Never raises. Missing / partial inputs degrade to neutral (50.0) for
      the affected components and the stage detail records the gap.
    * All component and total scores are clamped to [0, 100].
    * ``render_torque_chart`` accepts a pre-computed ``TorqueResult`` to
      avoid double work in the cockpit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from app.charts import (
    AMBER,
    AMBER_HOT,
    AX_BG,
    CHART_BG,
    CYAN,
    GREEN,
    MUTED,
    PANEL,
    RED,
    TEXT,
    TEXT_STRONG,
    RenderedImage,
    add_terminal_footer,
    apply_terminal_style,
    glow_effect,
    image_from_figure,
    safe_float,
    style_axis,
    style_legend,
    terminal_ema,
    terminal_rsi,
    terminal_sma,
)
from app.market_data import HistoryResult

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TorqueComponent:
    name: str
    score: float
    weight: float
    detail: str


@dataclass(frozen=True)
class TorqueResult:
    total_score: float
    stage_label: str
    stage_detail: str
    recommendation: str
    components: list[TorqueComponent]
    target_zone: str


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Industries that still carry an "old-noun" label even when revenue mix has
# shifted into AI / cloud / advanced compute. A high score on the
# Reclassification Lag component points to a likely re-rate candidate.
STALE_INDUSTRIES: frozenset[str] = frozenset(
    {
        "Semiconductors",
        "Communication Equipment",
        "Industrial Electrical Equipment",
        "Specialty Industrial Machinery",
        "Electronic Components",
        "Electrical Equipment & Parts",
        "Computer Hardware",
        "Scientific & Technical Instruments",
        "Auto Parts",
    }
)

HOT_INDUSTRIES: frozenset[str] = frozenset(
    {
        "Information Technology Services",
        "Software-Application",
        "Software-Infrastructure",
        "Semiconductor Equipment",
        "Internet Content & Information",
        "Data Storage",
    }
)

COMPONENT_WEIGHTS: dict[str, float] = {
    "Revenue Inflection": 0.25,
    "Margin Torque": 0.20,
    "Stale Valuation": 0.15,
    "Operating Leverage": 0.15,
    "Technical Discipline": 0.15,
    "Reclassification Lag": 0.10,
}

NEUTRAL_SCORE: float = 50.0


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    if value is None or not np.isfinite(value):
        return low
    return float(max(low, min(high, value)))


def _opt_float(value: Any) -> float | None:
    return safe_float(value)


def _percent(value: float | None) -> float | None:
    """Normalize a value that might be expressed as a fraction (0.56) or
    percent (56.0) into a percent number (56.0). Returns ``None`` on
    bad input."""
    number = _opt_float(value)
    if number is None:
        return None
    # Heuristic: |value| <= 5 -> treat as fraction (e.g. 0.56 -> 56)
    if abs(number) <= 5.0:
        return number * 100.0
    return number


# ---------------------------------------------------------------------------
# Sec-trend pack extraction helpers
# ---------------------------------------------------------------------------


def _sec_available(sec_trend: dict | None) -> bool:
    if not isinstance(sec_trend, dict):
        return False
    status = sec_trend.get("Status") or sec_trend.get("status")
    if status is None:
        # Some upstream packs omit Status when data is present; treat the
        # presence of a Metrics block as proof of availability.
        return bool(sec_trend.get("Metrics") or sec_trend.get("metrics"))
    return str(status).lower() == "available"


def _rev_acceleration(sec_trend: dict | None) -> dict:
    if not isinstance(sec_trend, dict):
        return {}
    block = sec_trend.get("Revenue Acceleration") or sec_trend.get("revenue_acceleration")
    return dict(block) if isinstance(block, dict) else {}


def _metrics(sec_trend: dict | None, key: str) -> dict:
    if not isinstance(sec_trend, dict):
        return {}
    metrics = sec_trend.get("Metrics") or sec_trend.get("metrics") or {}
    block = metrics.get(key) if isinstance(metrics, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def _margin_trajectory(sec_trend: dict | None) -> dict:
    if not isinstance(sec_trend, dict):
        return {}
    block = sec_trend.get("Margin Trajectory") or sec_trend.get("margin_trajectory")
    return dict(block) if isinstance(block, dict) else {}


def _op_leverage_label(sec_trend: dict | None) -> str:
    if not isinstance(sec_trend, dict):
        return ""
    block = sec_trend.get("Operating Leverage") or sec_trend.get("operating_leverage")
    if isinstance(block, dict):
        return str(block.get("label") or block.get("level") or "").lower()
    if isinstance(block, str):
        return block.lower()
    return ""


def _op_leverage_value(sec_trend: dict | None) -> float | None:
    if not isinstance(sec_trend, dict):
        return None
    block = sec_trend.get("Operating Leverage") or sec_trend.get("operating_leverage")
    if isinstance(block, dict):
        for key in ("value", "ratio", "multiple"):
            number = _opt_float(block.get(key))
            if number is not None:
                return number
    return None


# ---------------------------------------------------------------------------
# Component calculators
# ---------------------------------------------------------------------------


def score_revenue_inflection(sec_trend: dict | None) -> TorqueComponent:
    """0 declining; 30 decelerating; 60 mid-teens; 80 fast + accelerating;
    100 hyper + accelerating."""
    weight = COMPONENT_WEIGHTS["Revenue Inflection"]
    if not _sec_available(sec_trend):
        return TorqueComponent(
            "Revenue Inflection", NEUTRAL_SCORE, weight, "no fundamental data"
        )

    accel = _rev_acceleration(sec_trend)
    rev_metric = _metrics(sec_trend, "Revenue")
    yoy = _opt_float(
        rev_metric.get("yoy_growth_latest")
        or rev_metric.get("yoy_latest")
        or accel.get("yoy_latest")
    )
    qoq = _opt_float(accel.get("qoq_latest") or rev_metric.get("qoq_latest"))
    accelerating = bool(accel.get("accelerating"))

    if yoy is None:
        return TorqueComponent(
            "Revenue Inflection",
            NEUTRAL_SCORE,
            weight,
            "revenue growth unavailable",
        )

    yoy_pct = yoy * 100.0 if abs(yoy) <= 5.0 else yoy

    if yoy_pct < 0:
        score = 0.0
        detail = f"revenue declining {yoy_pct:.1f}% YoY"
    elif not accelerating and yoy_pct < 10.0:
        score = 30.0
        detail = f"growing {yoy_pct:.1f}% YoY but decelerating"
    elif yoy_pct < 25.0:
        score = 60.0
        detail = f"growing {yoy_pct:.1f}% YoY (mid pace)"
    elif yoy_pct < 50.0:
        score = 80.0 if accelerating else 65.0
        detail = (
            f"growing {yoy_pct:.1f}% YoY"
            + (" and accelerating" if accelerating else ", flat acceleration")
        )
    else:
        score = 100.0 if accelerating else 80.0
        detail = (
            f"hyper-growth {yoy_pct:.1f}% YoY"
            + (" and accelerating" if accelerating else "")
        )

    # QoQ tie-break: if QoQ is firmly negative while YoY is positive,
    # haircut by 15 to flag near-term softening.
    if qoq is not None:
        qoq_pct = qoq * 100.0 if abs(qoq) <= 5.0 else qoq
        if qoq_pct < -3.0 and score > 30.0:
            score = max(30.0, score - 15.0)
            detail += f"; QoQ {qoq_pct:.1f}% drag"

    return TorqueComponent("Revenue Inflection", _clamp(score), weight, detail)


def score_margin_torque(sec_trend: dict | None) -> TorqueComponent:
    """score = gross_margin_pct * 1.2 + op_leverage_value * 25 (clamped)."""
    weight = COMPONENT_WEIGHTS["Margin Torque"]
    if not _sec_available(sec_trend):
        return TorqueComponent(
            "Margin Torque", NEUTRAL_SCORE, weight, "no fundamental data"
        )

    gm_block = _metrics(sec_trend, "Gross Margin")
    gm_value = (
        gm_block.get("latest")
        or gm_block.get("value")
        or gm_block.get("gross_margin_pct")
    )
    gm_pct = _percent(gm_value)

    op_lev_label = _op_leverage_label(sec_trend)
    op_lev_value = _op_leverage_value(sec_trend)
    if op_lev_value is None:
        op_lev_value = {
            "high": 2.5,
            "moderate": 1.3,
            "low": 0.6,
            "negative": -0.5,
            "": 0.0,
        }.get(op_lev_label, 0.0)

    if gm_pct is None:
        # Without a margin reading we can still nudge from op leverage.
        score = 30.0 + op_lev_value * 20.0
        detail = f"GM unavailable; op-leverage {op_lev_label or 'unknown'}"
    else:
        score = gm_pct * 1.2 + op_lev_value * 25.0
        detail = f"GM {gm_pct:.1f}% + op-leverage {op_lev_value:+.2f}"

    return TorqueComponent("Margin Torque", _clamp(score), weight, detail)


def score_stale_valuation(
    profile: dict | None, *, sec_trend: dict | None, market_cap_override: float | None
) -> TorqueComponent:
    """Reward low P/S relative to growth, bonus for $500M-$8B mid-caps."""
    weight = COMPONENT_WEIGHTS["Stale Valuation"]
    profile = profile if isinstance(profile, dict) else {}

    ps = _opt_float(
        profile.get("priceToSalesTrailing12Months")
        or profile.get("price_to_sales")
        or profile.get("ps_ratio")
    )
    market_cap = _opt_float(market_cap_override) or _opt_float(profile.get("marketCap"))

    rev_metric = _metrics(sec_trend, "Revenue")
    yoy_raw = _opt_float(
        rev_metric.get("yoy_growth_latest") or rev_metric.get("yoy_latest")
    )
    if yoy_raw is None:
        growth_yoy = 0.0
    else:
        growth_yoy = yoy_raw if abs(yoy_raw) <= 5.0 else yoy_raw / 100.0

    if ps is None:
        return TorqueComponent(
            "Stale Valuation",
            NEUTRAL_SCORE,
            weight,
            "P/S unavailable",
        )

    if growth_yoy > 0.15:
        score = 100.0 - ps * 15.0
    else:
        score = 100.0 - ps * 25.0

    detail_parts = [f"P/S {ps:.2f}", f"growth {growth_yoy * 100:.1f}%"]

    # Sharp penalty for nosebleed multiples.
    if ps > 8.0:
        score -= (ps - 8.0) * 8.0
        detail_parts.append("P/S>8 penalty")

    # Mid-cap bonus.
    if market_cap is not None and 500_000_000.0 <= market_cap <= 8_000_000_000.0:
        score += 10.0
        detail_parts.append("mid-cap bonus")

    return TorqueComponent(
        "Stale Valuation", _clamp(score), weight, ", ".join(detail_parts)
    )


def score_operating_leverage(sec_trend: dict | None) -> TorqueComponent:
    """Map qualitative op-leverage label to a score, +20 if margin
    trajectory is improving while revenue rises."""
    weight = COMPONENT_WEIGHTS["Operating Leverage"]
    if not _sec_available(sec_trend):
        return TorqueComponent(
            "Operating Leverage",
            NEUTRAL_SCORE,
            weight,
            "no fundamental data",
        )

    label = _op_leverage_label(sec_trend)
    base = {
        "high": 80.0,
        "moderate": 50.0,
        "low": 25.0,
        "negative": 10.0,
    }.get(label, 40.0)

    margin_block = _margin_trajectory(sec_trend)
    gm_direction = str(
        margin_block.get("gross_margin_direction")
        or margin_block.get("gross_direction")
        or margin_block.get("direction")
        or ""
    ).lower()
    rev_direction = str(margin_block.get("revenue_direction") or "").lower()

    bonus = 0.0
    if (
        gm_direction in {"stable", "rising", "expanding", "up"}
        and rev_direction in {"rising", "up", "accelerating"}
    ):
        bonus = 20.0
    elif gm_direction in {"rising", "expanding", "up"} and not rev_direction:
        # Margin trajectory alone is improving; partial credit.
        bonus = 10.0

    score = base + bonus
    detail = f"op-lev {label or 'unknown'}"
    if bonus:
        detail += f" + margin trajectory bonus {bonus:.0f}"
    return TorqueComponent("Operating Leverage", _clamp(score), weight, detail)


def _calc_technical_metrics(
    history: HistoryResult | None,
) -> dict[str, float | None]:
    if history is None:
        return {
            "perf_3m": None,
            "perf_6m": None,
            "rsi_14": None,
            "ratio_50dma": None,
            "ratio_52wh": None,
        }
    data = history.data
    if data is None or data.empty or "Close" not in data:
        return {
            "perf_3m": None,
            "perf_6m": None,
            "rsi_14": None,
            "ratio_50dma": None,
            "ratio_52wh": None,
        }

    close = pd.to_numeric(data["Close"], errors="coerce").dropna()
    if close.empty:
        return {
            "perf_3m": None,
            "perf_6m": None,
            "rsi_14": None,
            "ratio_50dma": None,
            "ratio_52wh": None,
        }

    latest = float(close.iloc[-1])

    def _pct_from(window: int) -> float | None:
        if len(close) <= window:
            return None
        anchor = float(close.iloc[-window - 1])
        if anchor == 0:
            return None
        return latest / anchor - 1.0

    perf_3m = _pct_from(63)
    perf_6m = _pct_from(126)

    rsi_series = terminal_rsi(close, 14)
    rsi_latest = safe_float(rsi_series.iloc[-1]) if not rsi_series.empty else None

    sma50_series = terminal_sma(close, 50)
    sma50_latest = (
        safe_float(sma50_series.iloc[-1])
        if not sma50_series.empty and pd.notna(sma50_series.iloc[-1])
        else None
    )
    ratio_50dma = latest / sma50_latest if sma50_latest else None

    window_52w = close.tail(252) if len(close) >= 252 else close
    high_52w = float(window_52w.max()) if not window_52w.empty else None
    ratio_52wh = latest / high_52w if high_52w else None

    return {
        "perf_3m": perf_3m,
        "perf_6m": perf_6m,
        "rsi_14": rsi_latest,
        "ratio_50dma": ratio_50dma,
        "ratio_52wh": ratio_52wh,
    }


def score_technical_discipline(history: HistoryResult | None) -> TorqueComponent:
    """High score for constructive-but-not-extended setups."""
    weight = COMPONENT_WEIGHTS["Technical Discipline"]
    metrics = _calc_technical_metrics(history)

    perf_3m = metrics["perf_3m"]
    perf_6m = metrics["perf_6m"]
    rsi = metrics["rsi_14"]
    ratio_50 = metrics["ratio_50dma"]
    ratio_52wh = metrics["ratio_52wh"]

    available = [v for v in metrics.values() if v is not None]
    if not available:
        return TorqueComponent(
            "Technical Discipline",
            NEUTRAL_SCORE,
            weight,
            "no price history",
        )

    # Blowoff / overheating guard first.
    if (
        (perf_3m is not None and perf_3m > 1.0)
        or (rsi is not None and rsi > 80.0)
        or (ratio_50 is not None and ratio_50 > 1.25)
    ):
        detail = (
            f"extended: 3M {0 if perf_3m is None else perf_3m * 100:.0f}%, "
            f"RSI {0 if rsi is None else rsi:.0f}, "
            f"price/50DMA {0 if ratio_50 is None else ratio_50:.2f}"
        )
        return TorqueComponent("Technical Discipline", _clamp(15.0), weight, detail)

    score = 50.0

    # 3-6M performance: positive but moderate.
    perf_eff = perf_6m if perf_6m is not None else perf_3m
    if perf_eff is not None:
        if 0.05 <= perf_eff <= 0.80:
            score += 18.0
        elif perf_eff > 0.80:
            score += 6.0
        elif perf_eff > 0.0:
            score += 8.0
        else:
            score -= 12.0

    # RSI sweet spot 45-70.
    if rsi is not None:
        if 45.0 <= rsi <= 70.0:
            score += 14.0
        elif 35.0 <= rsi < 45.0 or 70.0 < rsi <= 75.0:
            score += 4.0
        else:
            score -= 8.0

    # Price / 50DMA: 0.90-1.18 healthy.
    if ratio_50 is not None:
        if 0.90 <= ratio_50 <= 1.18:
            score += 10.0
        else:
            score -= 6.0

    # Price / 52W-High: 0.55-0.95 = room to run.
    if ratio_52wh is not None:
        if 0.55 <= ratio_52wh <= 0.95:
            score += 8.0
        elif ratio_52wh > 0.98:
            score -= 6.0

    detail = (
        f"6M {0 if perf_6m is None else perf_6m * 100:.0f}%, "
        f"RSI {0 if rsi is None else rsi:.0f}, "
        f"px/50d {0 if ratio_50 is None else ratio_50:.2f}, "
        f"px/52wH {0 if ratio_52wh is None else ratio_52wh:.2f}"
    )
    return TorqueComponent("Technical Discipline", _clamp(score), weight, detail)


def score_reclassification_lag(
    profile: dict | None, sec_trend: dict | None
) -> TorqueComponent:
    """Industry tag vs. revenue growth proxy for re-rate potential."""
    weight = COMPONENT_WEIGHTS["Reclassification Lag"]
    profile = profile if isinstance(profile, dict) else {}
    industry = str(profile.get("industry") or "").strip()

    rev_metric = _metrics(sec_trend, "Revenue")
    yoy_raw = _opt_float(
        rev_metric.get("yoy_growth_latest") or rev_metric.get("yoy_latest")
    )
    growth_yoy = 0.0
    if yoy_raw is not None:
        growth_yoy = yoy_raw if abs(yoy_raw) <= 5.0 else yoy_raw / 100.0

    if not industry:
        return TorqueComponent(
            "Reclassification Lag",
            NEUTRAL_SCORE,
            weight,
            "industry tag unavailable",
        )

    if industry in STALE_INDUSTRIES and growth_yoy > 0.15:
        return TorqueComponent(
            "Reclassification Lag",
            80.0,
            weight,
            f"stale tag '{industry}' with {growth_yoy * 100:.1f}% growth",
        )
    if industry in HOT_INDUSTRIES:
        return TorqueComponent(
            "Reclassification Lag",
            30.0,
            weight,
            f"already in hot bucket '{industry}'",
        )
    return TorqueComponent(
        "Reclassification Lag",
        50.0,
        weight,
        f"neutral tag '{industry}'",
    )


# ---------------------------------------------------------------------------
# Composite assembly
# ---------------------------------------------------------------------------


def _stage_from_scores(
    *,
    total: float,
    revenue: float,
    technical: float,
    stale: float,
) -> tuple[str, str, str]:
    """Return (stage_label, stage_detail, target_zone)."""
    if technical < 30.0:
        return (
            "Extended",
            f"technical score {technical:.0f} flags blowoff conditions",
            "no edge",
        )
    if total >= 75.0 and technical >= 50.0 and revenue >= 50.0:
        return (
            "Coiled Spring",
            f"total {total:.0f} with constructive technicals and revenue inflection",
            "early entry",
        )
    if 60.0 <= total < 75.0 and revenue >= 50.0:
        return (
            "Inflecting",
            f"total {total:.0f}, revenue inflection {revenue:.0f}, building",
            "scale in",
        )
    if total >= 70.0 and technical >= 70.0:
        return (
            "Renaming Phase",
            f"total {total:.0f} but technical {technical:.0f} is extended",
            "let it digest",
        )
    if total >= 60.0 and 30.0 <= technical <= 70.0 and stale < 75.0:
        return (
            "Proof Phase",
            f"total {total:.0f}, guide-up window — stale valuation {stale:.0f}",
            "scale in",
        )
    return (
        "No Setup",
        f"total {total:.0f} below threshold",
        "no edge",
    )


_RECOMMENDATION_MAP: dict[str, str] = {
    "Coiled Spring": "BUY",
    "Inflecting": "BUY SETUP",
    "Proof Phase": "HOLD",
    "Renaming Phase": "WAIT",
    "Extended": "AVOID",
    "No Setup": "WAIT",
}


def compute_torque_score(
    *,
    history: HistoryResult | None,
    sec_trend: dict | None,
    profile: dict | None,
    market_cap: float | None = None,
) -> TorqueResult:
    try:
        revenue = score_revenue_inflection(sec_trend)
        margin = score_margin_torque(sec_trend)
        valuation = score_stale_valuation(
            profile, sec_trend=sec_trend, market_cap_override=market_cap
        )
        op_lev = score_operating_leverage(sec_trend)
        technical = score_technical_discipline(history)
        reclass = score_reclassification_lag(profile, sec_trend)

        components: list[TorqueComponent] = [
            revenue,
            margin,
            valuation,
            op_lev,
            technical,
            reclass,
        ]

        total = sum(c.score * c.weight for c in components)
        total = _clamp(total)

        stage_label, stage_detail, target_zone = _stage_from_scores(
            total=total,
            revenue=revenue.score,
            technical=technical.score,
            stale=valuation.score,
        )

        if not _sec_available(sec_trend):
            stage_detail = f"{stage_detail}; no fundamental data"

        recommendation = _RECOMMENDATION_MAP.get(stage_label, "WAIT")

        return TorqueResult(
            total_score=round(total, 2),
            stage_label=stage_label,
            stage_detail=stage_detail,
            recommendation=recommendation,
            components=components,
            target_zone=target_zone,
        )
    except Exception as exc:  # pragma: no cover - defensive net
        fallback = [
            TorqueComponent(name, NEUTRAL_SCORE, weight, "error during compute")
            for name, weight in COMPONENT_WEIGHTS.items()
        ]
        return TorqueResult(
            total_score=NEUTRAL_SCORE,
            stage_label="No Setup",
            stage_detail=f"torque compute failed: {exc!r}",
            recommendation="WAIT",
            components=fallback,
            target_zone="no edge",
        )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _revenue_series(sec_trend: dict | None) -> tuple[list[str], list[float]]:
    if not isinstance(sec_trend, dict):
        return [], []
    rev_metric = _metrics(sec_trend, "Revenue")
    series = (
        rev_metric.get("quarterly")
        or rev_metric.get("quarters")
        or rev_metric.get("series")
        or []
    )
    if not isinstance(series, list):
        return [], []
    labels: list[str] = []
    values: list[float] = []
    for entry in series[-8:]:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label") or entry.get("period") or "")
        value = _opt_float(entry.get("value") or entry.get("revenue"))
        if label and value is not None:
            labels.append(label)
            values.append(value)
    return labels, values


def _gross_margin_series(sec_trend: dict | None) -> list[float]:
    if not isinstance(sec_trend, dict):
        return []
    gm = _metrics(sec_trend, "Gross Margin")
    series = (
        gm.get("quarterly")
        or gm.get("quarters")
        or gm.get("series")
        or []
    )
    if not isinstance(series, list):
        return []
    values: list[float] = []
    for entry in series[-8:]:
        if isinstance(entry, dict):
            value = _percent(entry.get("value") or entry.get("margin"))
        else:
            value = _percent(entry)
        if value is not None:
            values.append(value)
    return values


def _operating_margin_series(sec_trend: dict | None) -> list[float]:
    if not isinstance(sec_trend, dict):
        return []
    om = _metrics(sec_trend, "Operating Margin")
    series = (
        om.get("quarterly")
        or om.get("quarters")
        or om.get("series")
        or []
    )
    if not isinstance(series, list):
        return []
    values: list[float] = []
    for entry in series[-8:]:
        if isinstance(entry, dict):
            value = _percent(entry.get("value") or entry.get("margin"))
        else:
            value = _percent(entry)
        if value is not None:
            values.append(value)
    return values


def _component_bar_color(score: float) -> str:
    if score >= 70.0:
        return GREEN
    if score >= 50.0:
        return CYAN
    if score >= 30.0:
        return AMBER
    return RED


def _component_summary(components: list[TorqueComponent]) -> dict[str, dict]:
    return {
        c.name: {
            "score": round(c.score, 2),
            "weight": c.weight,
            "detail": c.detail,
        }
        for c in components
    }


def _gauge_color(score: float) -> str:
    if score >= 75.0:
        return GREEN
    if score >= 60.0:
        return CYAN
    if score >= 45.0:
        return AMBER
    if score >= 30.0:
        return AMBER_HOT
    return RED


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------


def render_torque_chart(
    *,
    history: HistoryResult | None,
    sec_trend: dict | None,
    profile: dict | None,
    torque: TorqueResult | None = None,
) -> tuple[RenderedImage, dict]:
    """Render the 4-panel Torque dashboard.

    Panel 1 (top, full width): price with EMA(75)/SMA(200) and a shaded
    coiled-spring extension/compression band.
    Panel 2 (mid-left): 8Q revenue bars + gross margin overlay.
    Panel 3 (mid-right): operating margin trajectory + op-leverage marker.
    Panel 4 (bottom, full width): composite gauge + component bars.
    """
    apply_terminal_style()

    if torque is None:
        torque = compute_torque_score(
            history=history, sec_trend=sec_trend, profile=profile
        )

    ticker = (
        history.ticker
        if isinstance(history, HistoryResult)
        else str(profile.get("symbol") if isinstance(profile, dict) else "TICKER")
    )

    fig = plt.figure(figsize=(15, 13))
    gs = fig.add_gridspec(
        3,
        2,
        height_ratios=[2.5, 1.0, 1.05],
        width_ratios=[1.0, 1.0],
        hspace=0.55,
        wspace=0.22,
    )
    price_ax = fig.add_subplot(gs[0, :])
    rev_ax = fig.add_subplot(gs[1, 0])
    om_ax = fig.add_subplot(gs[1, 1])
    score_ax = fig.add_subplot(gs[2, :])

    # ---------------- Panel 1: Price -----------------
    style_axis(
        price_ax,
        title=f"{ticker} misclassified revenue torque",
        subtitle=(
            f"stage {torque.stage_label} | total {torque.total_score:.1f}"
            f" | rec {torque.recommendation} | zone {torque.target_zone}"
        ),
    )

    have_price = False
    if isinstance(history, HistoryResult) and not history.data.empty:
        data = history.data.copy()
        if "Close" in data.columns:
            close = pd.to_numeric(data["Close"], errors="coerce").dropna()
            if not close.empty:
                have_price = True
                ema75 = terminal_ema(close, 75)
                sma200 = terminal_sma(close, 200)
                price_ax.plot(
                    close.index,
                    close,
                    color=TEXT_STRONG,
                    linewidth=1.35,
                    label="Close",
                    path_effects=glow_effect(4.0),
                )
                price_ax.plot(
                    close.index, ema75, color=CYAN, linewidth=1.8, label="EMA 75"
                )
                price_ax.plot(
                    close.index, sma200, color=AMBER, linewidth=1.8, label="SMA 200"
                )
                # Coiled-spring band: compression below 50DMA*1.05, extension above
                sma50 = terminal_sma(close, 50)
                if not sma50.dropna().empty:
                    band_low = sma50 * 0.92
                    band_high = sma50 * 1.08
                    price_ax.fill_between(
                        close.index,
                        band_low,
                        band_high,
                        color=GREEN if torque.stage_label == "Coiled Spring" else AMBER,
                        alpha=0.07,
                        label="Coiled-spring zone",
                    )
                price_ax.set_ylabel("Price")
                style_legend(price_ax, ncols=4)

    if not have_price:
        price_ax.text(
            0.5,
            0.5,
            "NO PRICE HISTORY",
            transform=price_ax.transAxes,
            ha="center",
            va="center",
            color=MUTED,
            fontsize=14,
            fontweight="bold",
        )

    # ---------------- Panel 2: Revenue + GM ----------
    style_axis(rev_ax, title="Revenue (8Q) + Gross Margin", grid_axis="y")
    rev_labels, rev_values = _revenue_series(sec_trend)
    gm_values = _gross_margin_series(sec_trend)

    if rev_values:
        x_positions = np.arange(len(rev_values))
        rev_ax.bar(
            x_positions,
            rev_values,
            color=CYAN,
            alpha=0.78,
            edgecolor=AMBER,
            linewidth=0.4,
            label="Revenue",
        )
        rev_ax.set_xticks(x_positions)
        rev_ax.set_xticklabels(rev_labels, rotation=35, ha="right", fontsize=8)
        rev_ax.set_ylabel("Revenue ($)")

        if gm_values and len(gm_values) >= 1:
            gm_x = x_positions[-len(gm_values) :]
            twin = rev_ax.twinx()
            twin.plot(
                gm_x,
                gm_values,
                color=GREEN,
                linewidth=2.0,
                marker="o",
                markersize=5,
                label="Gross Margin",
                path_effects=glow_effect(3.0),
            )
            twin.set_ylabel("GM %", color=GREEN)
            twin.tick_params(colors=GREEN, labelsize=8)
            twin.set_ylim(
                bottom=max(0.0, min(gm_values) - 5.0),
                top=min(100.0, max(gm_values) + 5.0),
            )
            for spine in twin.spines.values():
                spine.set_color(AMBER)
                spine.set_alpha(0.3)
    else:
        rev_ax.text(
            0.5,
            0.5,
            "NO REVENUE DATA",
            transform=rev_ax.transAxes,
            ha="center",
            va="center",
            color=MUTED,
            fontsize=11,
        )

    # ---------------- Panel 3: Operating margin -----
    style_axis(om_ax, title="Operating Margin + Op-Leverage", grid_axis="y")
    om_values = _operating_margin_series(sec_trend)
    if om_values:
        om_x = np.arange(len(om_values))
        om_ax.plot(
            om_x,
            om_values,
            color=AMBER_HOT,
            linewidth=2.1,
            marker="o",
            markersize=5,
            path_effects=glow_effect(3.0),
            label="Op. Margin",
        )
        om_ax.fill_between(om_x, 0, om_values, color=AMBER, alpha=0.15)
        om_ax.axhline(0, color=TEXT, linewidth=0.8, alpha=0.65)
        om_ax.set_xticks(om_x)
        om_ax.set_xticklabels(
            (rev_labels[-len(om_values):] if rev_labels else [str(i) for i in om_x]),
            rotation=35,
            ha="right",
            fontsize=8,
        )
        om_ax.set_ylabel("Op. Margin %")

        # Op-leverage value annotation
        op_lev_label = _op_leverage_label(sec_trend) or "n/a"
        op_lev_value = _op_leverage_value(sec_trend)
        annotation = f"op-lev: {op_lev_label}"
        if op_lev_value is not None:
            annotation += f" ({op_lev_value:+.2f})"
        om_ax.text(
            0.02,
            0.94,
            annotation,
            transform=om_ax.transAxes,
            ha="left",
            va="top",
            color=AMBER_HOT,
            fontsize=9,
            fontweight="bold",
            bbox={
                "facecolor": PANEL,
                "edgecolor": AMBER,
                "alpha": 0.9,
                "boxstyle": "round,pad=0.3",
            },
        )
    else:
        om_ax.text(
            0.5,
            0.5,
            "NO OP. MARGIN DATA",
            transform=om_ax.transAxes,
            ha="center",
            va="center",
            color=MUTED,
            fontsize=11,
        )

    # ---------------- Panel 4: Score gauge + components --
    style_axis(score_ax, title="Composite Torque Score", grid_axis="x")
    score_ax.set_xlim(0, 100)

    component_names = [c.name for c in torque.components]
    component_scores = [c.score for c in torque.components]
    y_positions = np.arange(len(component_names))

    # Component bars (lower half of the score axis layout).
    bars = score_ax.barh(
        y_positions,
        component_scores,
        color=[_component_bar_color(s) for s in component_scores],
        alpha=0.86,
        edgecolor=TEXT,
        linewidth=0.35,
        height=0.62,
    )
    score_ax.set_yticks(y_positions, labels=component_names)
    score_ax.invert_yaxis()
    for bar, comp in zip(bars, torque.components, strict=False):
        score_ax.text(
            min(98.0, comp.score + 1.5),
            bar.get_y() + bar.get_height() / 2,
            f"{comp.score:.0f}  ({comp.weight:.0%})",
            ha="left",
            va="center",
            color=TEXT_STRONG,
            fontsize=9,
            fontweight="bold",
        )

    # Gauge: vertical band on right side showing total.
    gauge_color = _gauge_color(torque.total_score)
    score_ax.axvline(
        torque.total_score,
        color=gauge_color,
        linewidth=2.6,
        alpha=0.92,
        path_effects=glow_effect(4.5),
    )
    score_ax.axvspan(0, torque.total_score, color=gauge_color, alpha=0.07)

    score_ax.text(
        torque.total_score,
        -0.6,
        f"TOTAL {torque.total_score:.0f}",
        ha="center",
        va="bottom",
        color=gauge_color,
        fontsize=11,
        fontweight="bold",
        bbox={
            "facecolor": CHART_BG,
            "edgecolor": gauge_color,
            "alpha": 0.92,
            "boxstyle": "round,pad=0.35",
        },
    )
    score_ax.text(
        1.0,
        1.06,
        f"STAGE: {torque.stage_label.upper()}  -  {torque.recommendation}",
        transform=score_ax.transAxes,
        ha="right",
        va="bottom",
        color=AMBER_HOT,
        fontsize=11,
        fontweight="bold",
    )

    # Stage legend chips along the top.
    stage_chips = [
        ("Coiled Spring", GREEN),
        ("Inflecting", CYAN),
        ("Proof Phase", AMBER),
        ("Renaming", AMBER_HOT),
        ("Extended", RED),
    ]
    chip_patches = [
        mpatches.Patch(color=color, label=label, alpha=0.78)
        for label, color in stage_chips
    ]
    score_ax.legend(
        handles=chip_patches,
        loc="upper left",
        bbox_to_anchor=(0, 1.18),
        ncols=5,
        frameon=True,
        fontsize=8,
        facecolor=PANEL,
        edgecolor=AMBER,
        labelcolor=TEXT,
    )

    fig.tight_layout(rect=(0, 0.04, 1, 0.97))
    add_terminal_footer(
        fig,
        left=(
            f"{ticker} torque {torque.total_score:.0f} | "
            f"stage {torque.stage_label} | {torque.recommendation}"
        ),
        right="misclassified revenue torque",
    )

    image = image_from_figure(fig, f"{ticker.lower()}-torque.png")

    meta: dict[str, Any] = {
        "ticker": ticker,
        "total_score": torque.total_score,
        "stage_label": torque.stage_label,
        "stage_detail": torque.stage_detail,
        "recommendation": torque.recommendation,
        "target_zone": torque.target_zone,
        "components": _component_summary(torque.components),
        "panel_layout": {
            "price": "ema75/sma200 + coiled-spring band",
            "revenue": "8Q revenue bars + GM overlay",
            "operating_margin": "trajectory + op-leverage label",
            "score": "horizontal component bars + total gauge",
        },
        "weights": dict(COMPONENT_WEIGHTS),
        "fundamental_data_available": _sec_available(sec_trend),
    }
    # Ensure unused face colors are bound for static analyzers.
    _ = AX_BG
    return image, meta


__all__ = [
    "TorqueComponent",
    "TorqueResult",
    "compute_torque_score",
    "render_torque_chart",
    "score_revenue_inflection",
    "score_margin_torque",
    "score_stale_valuation",
    "score_operating_leverage",
    "score_technical_discipline",
    "score_reclassification_lag",
    "COMPONENT_WEIGHTS",
    "STALE_INDUSTRIES",
    "HOT_INDUSTRIES",
]
