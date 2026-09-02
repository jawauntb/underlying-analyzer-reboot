"""Calendar-month seasonality and forward-return distributions.

Two questions, answered from the same monthly return series:

1. *What has this calendar month done?* — the mean/median/hit-rate of the target
   month's total return over the last 1, 2, 5 and 10 years, plus whether the
   edge is strengthening (short windows better than long ones) or fading.
2. *What happens from here?* — for every historical year, the return measured
   from the last close before the target month out to 1, 2, 3, 6, 12 and 18
   months later, summarised as mean/median/hit-rate/p10/p90.

Both are anchored on the last close of the month *preceding* the target month,
so ``forward["1m"]`` is exactly the target month's own return and the two blocks
are consistent by construction. A partial current month is never counted: a
month contributes only once its final observation is in the series and the month
has ended relative to ``as_of``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.prism.contract import (
    HORIZON_MONTHS,
    HORIZONS,
    SEASONAL_WINDOWS,
    SeasonalForwardStats,
    SeasonalStats,
    SeasonalTrend,
    SeasonalWindowStats,
    empty_seasonal_forward,
    empty_seasonal_stats,
    empty_seasonal_window,
    month_label,
)
from app.prism.data import finite, resolve_as_of

#: A window's mean must move by more than this per extra look-back year before
#: the trend is called accelerating or decelerating rather than flat.
TREND_TOLERANCE = 0.0005

#: Fewer completed months than this and the statistics are not worth reporting.
MIN_COMPLETED_MONTHS = 4


def month_end_closes(series: pd.Series, *, as_of: date | str | None = None) -> pd.Series:
    """Last close of every *completed* calendar month, indexed by month period."""
    if series is None or series.empty:
        return pd.Series(dtype="float64")
    end = pd.Timestamp(resolve_as_of(as_of))
    trimmed = series[series.index <= end].dropna()
    if trimmed.empty:
        return pd.Series(dtype="float64")
    periods = trimmed.index.to_period("M")
    closes = trimmed.groupby(periods).last()
    current_period = end.to_period("M")
    # The month containing ``as_of`` is still open: drop it so a two-day-old month
    # never gets compared against full historical months.
    return closes[closes.index < current_period]


def monthly_returns(series: pd.Series, *, as_of: date | str | None = None) -> pd.Series:
    """Total return of each completed calendar month, indexed by month period."""
    closes = month_end_closes(series, as_of=as_of)
    if len(closes) < 2:
        return pd.Series(dtype="float64")
    returns = closes.pct_change().iloc[1:]
    return returns.replace([np.inf, -np.inf], np.nan).dropna()


def _window_stats(values: list[tuple[int, float]]) -> SeasonalWindowStats:
    if not values:
        return empty_seasonal_window()
    array = np.array([value for _, value in values], dtype="float64")
    return SeasonalWindowStats(
        mean=finite(array.mean()),
        median=finite(np.median(array)),
        n=int(array.size),
        hit_rate=finite(float((array > 0).sum()) / array.size),
        values=[{"year": float(year), "ret": float(value)} for year, value in values],
    )


def this_month_stats(
    series: pd.Series,
    *,
    month: int,
    as_of: date | str | None = None,
    windows: tuple[int, ...] = SEASONAL_WINDOWS,
) -> dict[str, SeasonalWindowStats]:
    """Returns of one calendar month over each look-back window."""
    returns = monthly_returns(series, as_of=as_of)
    if returns.empty:
        return {f"{years}y": empty_seasonal_window() for years in windows}
    end = pd.Timestamp(resolve_as_of(as_of))
    reference_year = end.year
    matches: list[tuple[int, float]] = [
        (int(period.year), float(value))
        for period, value in returns.items()
        if period.month == int(month) and int(period.year) <= reference_year
    ]
    matches.sort(key=lambda item: item[0])
    stats: dict[str, SeasonalWindowStats] = {}
    for years in windows:
        # "last N years" is inclusive: a 10-year window on a 2026 as-of covers
        # the 2016..2025 occurrences, which is ten of them.
        cutoff = reference_year - int(years)
        stats[f"{years}y"] = _window_stats(
            [(year, value) for year, value in matches if year >= cutoff]
        )
    return stats


def seasonal_trend(stats: dict[str, SeasonalWindowStats]) -> SeasonalTrend:
    """Regress each window's mean on its look-back length.

    A *negative* slope means the short windows (recent years) print a higher mean
    than the long ones — the seasonal edge is accelerating. A positive slope means
    the edge lived mostly in the older years and is decelerating.
    """
    points: list[tuple[float, float]] = []
    for key, block in stats.items():
        mean = block.get("mean")
        if mean is None or block.get("n", 0) < 2:
            continue
        try:
            years = float(str(key).rstrip("y"))
        except ValueError:
            continue
        points.append((years, float(mean)))
    if len(points) < 2:
        return SeasonalTrend(
            direction="flat", slope=None, windows_used=[int(years) for years, _ in points]
        )
    xs = np.array([years for years, _ in points], dtype="float64")
    ys = np.array([mean for _, mean in points], dtype="float64")
    slope = float(np.polyfit(xs, ys, 1)[0])
    if slope < -TREND_TOLERANCE:
        direction = "accelerating"
    elif slope > TREND_TOLERANCE:
        direction = "decelerating"
    else:
        direction = "flat"
    return SeasonalTrend(
        direction=direction,
        slope=finite(slope),
        windows_used=sorted(int(years) for years, _ in points),
    )


def _forward_values(
    closes: pd.Series,
    *,
    month: int,
    months_ahead: int,
) -> list[tuple[int, float]]:
    """Returns from the close before each occurrence of ``month``, ``months_ahead`` on."""
    if closes.empty:
        return []
    lookup = {period: float(value) for period, value in closes.items()}
    results: list[tuple[int, float]] = []
    for period in lookup:
        if period.month != int(month):
            continue
        start_value = lookup.get(period - 1)
        end_value = lookup.get(period + (int(months_ahead) - 1))
        if start_value is None or end_value is None or start_value <= 0:
            continue
        results.append((int(period.year), end_value / start_value - 1.0))
    results.sort(key=lambda item: item[0])
    return results


def _forward_stats(values: list[tuple[int, float]]) -> SeasonalForwardStats:
    if not values:
        return empty_seasonal_forward()
    array = np.array([value for _, value in values], dtype="float64")
    return SeasonalForwardStats(
        mean=finite(array.mean()),
        median=finite(np.median(array)),
        n=int(array.size),
        hit_rate=finite(float((array > 0).sum()) / array.size),
        p10=finite(np.percentile(array, 10)),
        p90=finite(np.percentile(array, 90)),
    )


def forward_stats(
    series: pd.Series,
    *,
    month: int,
    as_of: date | str | None = None,
    horizons: tuple[str, ...] = HORIZONS,
) -> dict[str, SeasonalForwardStats]:
    """Forward-return distributions conditional on starting in ``month``."""
    closes = month_end_closes(series, as_of=as_of)
    return {
        horizon: _forward_stats(
            _forward_values(closes, month=month, months_ahead=HORIZON_MONTHS[horizon])
        )
        for horizon in horizons
    }


def seasonal_stats(
    series: pd.Series,
    *,
    month: int,
    symbol: str = "",
    as_of: date | str | None = None,
    windows: tuple[int, ...] = SEASONAL_WINDOWS,
    horizons: tuple[str, ...] = HORIZONS,
) -> SeasonalStats:
    """Full ``SeasonalStats`` for one close series and one calendar month."""
    label = symbol or str(getattr(series, "name", "") or "")
    if series is None or series.empty:
        return empty_seasonal_stats(label, month=month, error="no price history")
    closes = month_end_closes(series, as_of=as_of)
    if len(closes) < MIN_COMPLETED_MONTHS:
        return empty_seasonal_stats(
            label,
            month=month,
            error=f"only {len(closes)} completed months of history",
        )
    this_month = this_month_stats(series, month=month, as_of=as_of, windows=windows)
    payload = empty_seasonal_stats(label, month=month)
    payload["this_month"] = this_month
    payload["trend"] = seasonal_trend(this_month)
    payload["forward"] = forward_stats(series, month=month, as_of=as_of, horizons=horizons)
    payload["n_years"] = int(len({period.year for period in closes.index}))
    payload["first_date"] = str(closes.index[0])
    payload["last_date"] = str(closes.index[-1])
    payload["error"] = None
    return payload


def build_seasonality_section(
    ticker: str,
    ticker_series: pd.Series,
    benchmark_series: dict[str, pd.Series],
    *,
    as_of: date | str | None = None,
    month: int | None = None,
    windows: tuple[int, ...] = SEASONAL_WINDOWS,
    horizons: tuple[str, ...] = HORIZONS,
) -> dict[str, Any]:
    """Build ``packet["seasonality"]`` for a ticker and its benchmark set.

    ``month`` defaults to the calendar month of ``as_of`` — the month the user is
    standing at the start of, which is the one the memo has to have an opinion
    about.
    """
    end = resolve_as_of(as_of)
    target_month = int(month or end.month)
    benchmarks: dict[str, SeasonalStats] = {}
    for symbol, series in benchmark_series.items():
        if symbol == ticker:
            continue
        benchmarks[symbol] = seasonal_stats(
            series,
            month=target_month,
            symbol=symbol,
            as_of=end,
            windows=windows,
            horizons=horizons,
        )
    return {
        "month": target_month,
        "month_label": month_label(target_month),
        "as_of": end.isoformat(),
        "windows": [f"{years}y" for years in windows],
        "horizons": list(horizons),
        "ticker": seasonal_stats(
            ticker_series,
            month=target_month,
            symbol=ticker,
            as_of=end,
            windows=windows,
            horizons=horizons,
        ),
        "benchmarks": benchmarks,
    }


def seasonality_highlights(section: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """Rank benchmarks by this-month 10-year mean, for the memo's lead sentence."""
    rows: list[dict[str, Any]] = []
    entries: list[tuple[str, Any]] = [("ticker", section.get("ticker"))]
    benchmarks = section.get("benchmarks")
    if isinstance(benchmarks, dict):
        entries.extend(benchmarks.items())
    for key, stats in entries:
        if not isinstance(stats, dict):
            continue
        this_month = stats.get("this_month") or {}
        block = this_month.get("10y") or this_month.get("5y") or {}
        mean = block.get("mean") if isinstance(block, dict) else None
        if mean is None:
            continue
        rows.append(
            {
                "symbol": stats.get("symbol") or key,
                "mean": mean,
                "hit_rate": block.get("hit_rate"),
                "n": block.get("n"),
                "trend": (stats.get("trend") or {}).get("direction"),
            }
        )
    rows.sort(key=lambda row: float(row["mean"]), reverse=True)
    return rows[: max(1, limit)]
