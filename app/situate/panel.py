"""Point-in-time panel over Prism's data plumbing.

Situate's per-name work (exposure, state) is a pure function of a monthly return
panel and a set of daily realised-volatility series, all guaranteed to contain no
observation dated after the evaluation date ``t``. This module is the thin wrapper
that produces that panel from Prism's existing loaders:

* :func:`app.prism.data.load_universe` for Massive daily closes (already trims to
  ``as_of`` and reports honest coverage);
* :func:`app.prism.macro.fetch_fred_series` for FRED levels (yields, spreads,
  VIX), resampled to month-end levels here.

The point-in-time guarantee is enforced twice: the underlying loaders trim to
``as_of``, and every frame this module returns is re-filtered ``index <= t``. The
lookahead test in ``tests/test_situate_core.py`` recomputes after masking data
after ``t`` and asserts the panel — and everything built on it — is unchanged.

Nothing here fabricates a value: a symbol that fails to load becomes an entry in
:attr:`Panel.errors`, not a padded series.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.prism.cache import PrismCache
from app.prism.data import (
    DEFAULT_YEARS,
    SeriesLoad,
    align_series,
    load_universe,
    resolve_as_of,
)

__all__ = [
    "Panel",
    "load_macro_monthly",
    "load_panel",
    "monthly_log_returns",
    "monthly_total_returns",
    "month_end_levels",
    "realized_vol",
]

TRADING_DAYS_PER_YEAR = 252
#: Realised-volatility windows (trading days) exposed on every :class:`Panel`.
REALIZED_VOL_WINDOWS: tuple[int, ...] = (21, 63)


def month_end_levels(daily: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Last observation in each calendar month (month-end resample)."""
    if daily is None or (hasattr(daily, "empty") and daily.empty):
        return daily
    resampled = daily.resample("ME").last()
    return resampled.dropna(how="all")


def monthly_total_returns(daily: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Calendar-month simple total returns from daily (adjusted) closes."""
    levels = month_end_levels(daily)
    if levels is None or (hasattr(levels, "empty") and levels.empty):
        return levels
    returns = levels.pct_change()
    return returns.iloc[1:]


def monthly_log_returns(daily: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Calendar-month log total returns from daily (adjusted) closes."""
    levels = month_end_levels(daily)
    if levels is None or (hasattr(levels, "empty") and levels.empty):
        return levels
    returns = np.log(levels / levels.shift(1))
    return returns.iloc[1:]


def realized_vol(daily: pd.DataFrame | pd.Series, window: int) -> pd.DataFrame | pd.Series:
    """Annualised trailing realised volatility of daily returns.

    ``sqrt(252) * rolling(window).std`` of daily simple returns. Returned as a
    daily-indexed frame/series so a caller can read the value as of any date.
    """
    if daily is None or (hasattr(daily, "empty") and daily.empty):
        return daily
    returns = daily.pct_change()
    vol = returns.rolling(int(window), min_periods=max(5, int(window) // 2)).std(ddof=1)
    return vol * np.sqrt(TRADING_DAYS_PER_YEAR)


@dataclass
class Panel:
    """An aligned, point-in-time cross-asset panel as of ``as_of``.

    Every frame is filtered so no observation is dated after ``as_of``.
    """

    as_of: str
    years: int
    daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_total: pd.DataFrame = field(default_factory=pd.DataFrame)
    monthly_log: pd.DataFrame = field(default_factory=pd.DataFrame)
    realized_vol_21d: pd.DataFrame = field(default_factory=pd.DataFrame)
    realized_vol_63d: pd.DataFrame = field(default_factory=pd.DataFrame)
    loads: dict[str, SeriesLoad] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    cache_status: str = "disabled"

    def symbols(self) -> list[str]:
        """Symbols that loaded usable history, in insertion order."""
        return list(self.daily.columns)

    def has(self, symbol: str) -> bool:
        """True when ``symbol`` loaded and has a daily series."""
        return str(symbol).upper() in set(self.daily.columns)

    def daily_close(self, symbol: str) -> pd.Series:
        """The daily close series for one symbol (empty when absent)."""
        key = str(symbol).upper()
        if key not in self.daily.columns:
            return pd.Series(dtype="float64", name=key)
        return self.daily[key].dropna()

    def monthly_log_return(self, symbol: str) -> pd.Series:
        """Monthly log returns for one symbol (empty when absent)."""
        key = str(symbol).upper()
        if key not in self.monthly_log.columns:
            return pd.Series(dtype="float64", name=key)
        return self.monthly_log[key].dropna()

    def monthly_total_return(self, symbol: str) -> pd.Series:
        """Monthly simple total returns for one symbol (empty when absent)."""
        key = str(symbol).upper()
        if key not in self.monthly_total.columns:
            return pd.Series(dtype="float64", name=key)
        return self.monthly_total[key].dropna()

    def realized_vol_series(self, symbol: str, window: int = 21) -> pd.Series:
        """Trailing realised-vol series for one symbol at 21d (default) or 63d."""
        frame = self.realized_vol_63d if int(window) == 63 else self.realized_vol_21d
        key = str(symbol).upper()
        if key not in frame.columns:
            return pd.Series(dtype="float64", name=key)
        return frame[key].dropna()

    def coverage(self) -> dict[str, Any]:
        """Compact per-symbol coverage summary for ``meta``."""
        return {
            "loaded": len(self.loads),
            "failed": len(self.errors),
            "cache": self.cache_status,
            "symbols": {
                symbol: {
                    "first_date": load.first_date,
                    "last_date": load.last_date,
                    "n_days": load.n_days,
                    "cached": load.cached,
                }
                for symbol, load in self.loads.items()
            },
            "errors": dict(self.errors),
        }


def _filter_as_of(frame: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Belt-and-braces: drop any row dated after ``as_of``."""
    if frame is None or frame.empty:
        return frame
    return frame[frame.index <= as_of]


def load_panel(
    client: Any,
    symbols: Sequence[str],
    *,
    as_of: date | str | None = None,
    years: int = DEFAULT_YEARS,
    cache: PrismCache | None = None,
    max_workers: int | None = None,
) -> Panel:
    """Load an aligned point-in-time panel for ``symbols`` as of ``as_of``.

    Returns a :class:`Panel` whose daily, monthly and realised-vol frames all end
    on or before ``as_of``. A symbol that fails to load is recorded in
    :attr:`Panel.errors` and simply omitted from the frames.
    """
    end = resolve_as_of(as_of)
    stamp = pd.Timestamp(end)
    universe = load_universe(
        client, symbols, years=years, as_of=end, cache=cache, max_workers=max_workers
    )
    daily = align_series(universe.series, how="outer")
    if not daily.empty:
        daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index)).normalize()
        daily = _filter_as_of(daily.sort_index(), stamp)

    panel = Panel(
        as_of=end.isoformat(),
        years=int(years),
        daily=daily,
        loads=dict(universe.loads),
        errors=dict(universe.errors),
        cache_status=universe.cache_status,
    )
    if daily.empty:
        return panel

    monthly_total = monthly_total_returns(daily)
    monthly_log = monthly_log_returns(daily)
    panel.monthly_total = _filter_as_of(monthly_total, stamp)  # type: ignore[arg-type]
    panel.monthly_log = _filter_as_of(monthly_log, stamp)  # type: ignore[arg-type]
    panel.realized_vol_21d = _filter_as_of(realized_vol(daily, 21), stamp)  # type: ignore[arg-type]
    panel.realized_vol_63d = _filter_as_of(realized_vol(daily, 63), stamp)  # type: ignore[arg-type]
    return panel


def load_macro_monthly(
    fred: Any,
    series_ids: Sequence[str],
    *,
    as_of: date | str | None = None,
    years: int = 20,
    cache: PrismCache | None = None,
) -> pd.DataFrame:
    """Month-end levels for a set of FRED series, filtered to ``as_of``.

    Returns a month-end-indexed frame with one column per series id (columns for
    series that fail to load are simply omitted). Levels, not changes — the
    exposure and state modules take the single first difference they need
    themselves, so nothing is differentiated more than once upstream.
    """
    from app.prism.macro import fetch_fred_series

    end = resolve_as_of(as_of)
    stamp = pd.Timestamp(end)
    columns: dict[str, pd.Series] = {}
    for series_id in series_ids:
        resolved = str(series_id).strip().upper()
        if not resolved or resolved in columns:
            continue
        try:
            series = fetch_fred_series(fred, resolved, years=years, as_of=end, cache=cache)
        except Exception:
            continue
        if series is None or series.empty:
            continue
        series = series[series.index <= stamp]
        if series.empty:
            continue
        columns[resolved] = series
    if not columns:
        return pd.DataFrame()
    daily = pd.DataFrame(columns).sort_index()
    daily.index = pd.DatetimeIndex(pd.to_datetime(daily.index)).normalize()
    monthly = month_end_levels(daily)
    return _filter_as_of(monthly, stamp)  # type: ignore[arg-type,return-value]
