"""Price levels, extracted as numbers from the terminal's existing chart math.

``app.chart_data`` already computes the auction value area, the linear-regression
channel, the Ridge Growth strategy state and the Torque stage score for the
rendered charts. Prism reuses those builders verbatim and keeps only the levels
and metadata — the packet carries numbers a memo can cite, never images.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import pandas as pd


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _last_point(points: Sequence[Mapping[str, Any]] | None) -> float | None:
    """Final ``value`` of a ``series_points`` list."""
    if not points:
        return None
    for row in reversed(list(points)):
        value = _finite(row.get("value"))
        if value is not None:
            return value
    return None


def auction_levels(history: Any, *, period: str) -> dict[str, Any]:
    """Value area high/low and point of control, plus where price sits in it."""
    from app.chart_data import build_auction_chart_data

    payload = build_auction_chart_data(history, period=period)
    meta = dict(payload.get("meta") or {})
    levels = dict(payload.get("levels") or {})
    return {
        "vah": _finite(levels.get("vah")),
        "val": _finite(levels.get("val")),
        "poc": _finite(levels.get("poc")),
        "location": meta.get("location"),
        "state": meta.get("state"),
        "window": meta.get("level_window") or "21 completed daily sessions",
        "meta": {key: value for key, value in meta.items() if key not in {"vah", "val", "poc"}},
    }


def regression_levels(history: Any) -> dict[str, Any]:
    """Trend line, one-sigma channel and the moving averages, at the last bar."""
    from app.chart_data import build_regression_chart_data

    payload = build_regression_chart_data(history)
    meta = dict(payload.get("meta") or {})
    series = dict(payload.get("series") or {})
    trend = _last_point(series.get("trend"))
    upper = _last_point(series.get("upper_band"))
    lower = _last_point(series.get("lower_band"))
    close = _last_point(series.get("close"))
    residual_std = _finite(meta.get("residual_std"))
    position: float | None = None
    if close is not None and trend is not None and residual_std:
        position = (close - trend) / residual_std
    return {
        "slope_per_day": _finite(meta.get("slope_per_day")),
        "intercept": _finite(meta.get("intercept")),
        "residual_std": residual_std,
        "trend_value": trend,
        "upper_band": upper,
        "lower_band": lower,
        "close": close,
        "z_from_trend": position,
        "ema21": _last_point(series.get("ema21")),
        "ema50": _last_point(series.get("ema50")),
        "ema200": _last_point(series.get("ema200")),
    }


def torque_levels(
    history: Any, *, sec_trend: Mapping[str, Any] | None, profile: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Torque stage score and its component breakdown (no chart series)."""
    from app.chart_data import build_torque_chart_data

    payload = build_torque_chart_data(
        history=history,
        sec_trend=dict(sec_trend) if sec_trend else None,
        profile=dict(profile) if profile else None,
    )
    meta = dict(payload.get("meta") or {})
    return {
        "total_score": _finite(meta.get("total_score")),
        "stage_label": meta.get("stage_label"),
        "stage_detail": meta.get("stage_detail"),
        "recommendation": meta.get("recommendation"),
        "target_zone": meta.get("target_zone"),
        "components": meta.get("components") or {},
        "weights": meta.get("weights") or {},
        "fundamental_data_available": bool(meta.get("fundamental_data_available")),
    }


def ridge_levels(history: Any, *, period: str) -> dict[str, Any]:
    """Ridge Growth strategy state plus its moving-average ladder."""
    from app.chart_data import build_ridge_growth_chart_data

    payload = build_ridge_growth_chart_data(history, period=period)
    meta = dict(payload.get("meta") or {})
    series = dict(payload.get("series") or {})
    return {
        "fast_ma": _last_point(series.get("fast_ma")),
        "base_ma": _last_point(series.get("base_ma")),
        "major_ma": _last_point(series.get("major_ma")),
        "meta": {
            key: value
            for key, value in meta.items()
            if key not in {"flow_compass", "auction"} and not isinstance(value, list | dict)
        },
        "flow_compass": meta.get("flow_compass") or {},
        "auction": meta.get("auction") or {},
    }


def price_extremes(history: Any) -> dict[str, Any]:
    """52-week high/low and the recent swing range from the OHLCV frame."""
    frame = getattr(history, "data", None)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return {}
    highs = pd.to_numeric(frame.get("High"), errors="coerce").dropna()
    lows = pd.to_numeric(frame.get("Low"), errors="coerce").dropna()
    closes = pd.to_numeric(frame.get("Close"), errors="coerce").dropna()
    if closes.empty:
        return {}
    year = min(252, closes.shape[0])
    swing = min(21, closes.shape[0])
    return {
        "last_close": float(closes.iloc[-1]),
        "high_52w": float(highs.iloc[-year:].max()) if not highs.empty else None,
        "low_52w": float(lows.iloc[-year:].min()) if not lows.empty else None,
        "swing_high_21d": float(highs.iloc[-swing:].max()) if not highs.empty else None,
        "swing_low_21d": float(lows.iloc[-swing:].min()) if not lows.empty else None,
        "bars": int(closes.shape[0]),
    }


def key_levels(
    *,
    auction: Mapping[str, Any] | None,
    regression: Mapping[str, Any] | None,
    extremes: Mapping[str, Any] | None,
    ridge: Mapping[str, Any] | None,
    current_price: float | None,
) -> list[dict[str, Any]]:
    """One ranked list of prices worth watching, nearest the last close first."""
    rows: list[dict[str, Any]] = []

    def add(price: Any, kind: str, source: str) -> None:
        value = _finite(price)
        if value is None or value <= 0:
            return
        rows.append({"price": value, "kind": kind, "source": source})

    auction = auction or {}
    add(auction.get("vah"), "resistance", "auction value area high")
    add(auction.get("poc"), "magnet", "auction point of control")
    add(auction.get("val"), "support", "auction value area low")

    regression = regression or {}
    add(regression.get("upper_band"), "resistance", "regression +1 sigma")
    add(regression.get("trend_value"), "magnet", "regression trend")
    add(regression.get("lower_band"), "support", "regression -1 sigma")
    add(regression.get("ema21"), "magnet", "21-day EMA")
    add(regression.get("ema50"), "magnet", "50-day EMA")
    add(regression.get("ema200"), "magnet", "200-day EMA")

    extremes = extremes or {}
    add(extremes.get("high_52w"), "resistance", "52-week high")
    add(extremes.get("low_52w"), "support", "52-week low")
    add(extremes.get("swing_high_21d"), "resistance", "21-day swing high")
    add(extremes.get("swing_low_21d"), "support", "21-day swing low")

    ridge = ridge or {}
    add(ridge.get("major_ma"), "trend", "ridge major moving average")

    reference = _finite(current_price) or _finite(extremes.get("last_close"))
    for row in rows:
        if reference:
            row["distance_pct"] = row["price"] / reference - 1.0
        else:
            row["distance_pct"] = None
    if reference:
        rows.sort(key=lambda row: abs(float(row["distance_pct"] or 0.0)))
    return rows


def build_levels(
    history: Any,
    *,
    period: str = "1y",
    sec_trend: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Build ``packet["levels"]`` from one OHLCV history.

    Each sub-block is computed independently so a failure in, say, the Torque
    stage (which needs SEC data) cannot cost the memo its value area.
    """
    section: dict[str, Any] = {
        "period": period,
        "fetched_at": datetime.now(UTC).isoformat(),
        "provider": getattr(history, "provider", None),
        "auction": None,
        "regression": None,
        "torque": None,
        "ridge": None,
        "extremes": None,
        "key_levels": [],
        "errors": [],
    }
    builders: tuple[tuple[str, Any], ...] = (
        ("auction", lambda: auction_levels(history, period=period)),
        ("regression", lambda: regression_levels(history)),
        ("torque", lambda: torque_levels(history, sec_trend=sec_trend, profile=profile)),
        ("ridge", lambda: ridge_levels(history, period=period)),
        ("extremes", lambda: price_extremes(history)),
    )
    for name, builder in builders:
        try:
            section[name] = builder()
        except Exception as exc:  # noqa: BLE001 - one level family must not sink the rest
            section[name] = None
            section["errors"].append({"source": f"levels.{name}", "error": str(exc)})

    section["key_levels"] = key_levels(
        auction=section["auction"],
        regression=section["regression"],
        extremes=section["extremes"],
        ridge=section["ridge"],
        current_price=current_price,
    )
    return section
