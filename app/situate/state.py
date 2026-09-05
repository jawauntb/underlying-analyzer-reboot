"""State: what regime we are in right now (SPEC 5.2).

Volatility is persistent; expected return barely is. So the state Situate
conditions on is primarily a *volatility* state, expressed as a 2x2 grid on both
the market (SPY) and the ticker:

* **vol**: trailing 21-day realised volatility vs its own trailing 2-year median
  (``high`` / ``low``);
* **trend**: 12-1 momentum, the return from 12 months ago to 1 month ago
  (``up`` / ``down``).

An optional 3-state Gaussian HMM on SPY daily ``[return, 10-day vol]`` (trailing
10 years, refit here, *filtered* current-state probabilities so there is no
lookahead) is carried as a labelled second opinion, never as a signal. VIX, HY OAS
and the 10y-2y curve are reported as context percentiles.

Everything is a pure function of the series handed in and filters ``index <= t``,
so the lookahead test recomputes the same cell after masking data after ``t``.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.prism.data import resolve_as_of
from app.situate.contract import MODULE_VERSIONS, empty_grid_state
from app.situate.panel import Panel

__all__ = [
    "build_context",
    "build_grid",
    "build_hmm_opinion",
    "build_state",
    "state_section",
]

TRADING_DAYS_PER_YEAR = 252
_TWO_YEARS_DAYS = 504
_HMM_LABELS = ("bear", "neutral", "bull")


def _month_end_levels(daily: pd.Series, as_of: pd.Timestamp) -> pd.Series:
    prices = pd.to_numeric(pd.Series(daily), errors="coerce").dropna()
    prices = prices[prices > 0]
    prices = prices[prices.index <= as_of]
    if prices.empty:
        return prices
    prices.index = pd.DatetimeIndex(pd.to_datetime(prices.index)).normalize()
    return prices.resample("ME").last().dropna()


def build_grid(
    daily_close: pd.Series, *, as_of: date | str | None = None
) -> dict[str, Any]:
    """The 2x2 vol x trend cell for one symbol's daily close series."""
    stamp = pd.Timestamp(resolve_as_of(as_of))
    prices = pd.to_numeric(pd.Series(daily_close), errors="coerce").dropna()
    prices = prices[(prices > 0) & (prices.index <= stamp)]
    if prices.shape[0] < 63:
        grid = empty_grid_state(error="need at least 63 daily closes")
        return dict(grid)

    returns = prices.pct_change().dropna()
    vol_21d = returns.rolling(21, min_periods=15).std(ddof=1) * np.sqrt(TRADING_DAYS_PER_YEAR)
    vol_21d = vol_21d.dropna()
    if vol_21d.empty:
        grid = empty_grid_state(error="realised volatility could not be computed")
        return dict(grid)
    realized = float(vol_21d.iloc[-1])
    window = vol_21d.iloc[-_TWO_YEARS_DAYS:]
    vol_median = float(window.median())
    vol_state = "high" if realized > vol_median else "low"

    levels = _month_end_levels(prices, stamp)
    ret_12m_1m: float | None = None
    trend_state: str | None = None
    if levels.shape[0] >= 13:
        prev_1m = float(levels.iloc[-2])
        prev_12m = float(levels.iloc[-13])
        if prev_12m > 0:
            ret_12m_1m = prev_1m / prev_12m - 1.0
            trend_state = "up" if ret_12m_1m >= 0 else "down"

    cell = f"{vol_state}_{trend_state}" if trend_state is not None else None
    return {
        "vol_state": vol_state,
        "trend_state": trend_state,
        "cell": cell,
        "realized_vol_21d": realized,
        "vol_median_2y": vol_median,
        "ret_12m_1m": ret_12m_1m,
        "n_months": int(levels.shape[0]),
        "error": None,
    }


def build_hmm_opinion(
    spy_daily: pd.Series, *, as_of: date | str | None = None, years: int = 10
) -> dict[str, Any] | None:
    """A 3-state Gaussian HMM second opinion on SPY (filtered, no lookahead)."""
    from app.prism.hmm import HMMError, filtered_posteriors, fit_gaussian_hmm

    stamp = pd.Timestamp(resolve_as_of(as_of))
    prices = pd.to_numeric(pd.Series(spy_daily), errors="coerce").dropna()
    prices = prices[(prices > 0) & (prices.index <= stamp)]
    cutoff = stamp - pd.DateOffset(years=int(years))
    prices = prices[prices.index >= cutoff]
    returns = prices.pct_change().dropna()
    vol_10d = returns.rolling(10, min_periods=10).std(ddof=1)
    features = pd.concat([returns.rename("ret"), vol_10d.rename("vol")], axis=1).dropna()
    if features.shape[0] < TRADING_DAYS_PER_YEAR:
        return {
            "probs": dict.fromkeys(_HMM_LABELS),
            "label": None,
            "n_days": int(features.shape[0]),
            "converged": False,
            "error": f"need at least {TRADING_DAYS_PER_YEAR} daily observations",
        }
    obs = features.to_numpy(dtype=np.float64)
    try:
        model = fit_gaussian_hmm(obs, n_states=3)
        posteriors = filtered_posteriors(model, obs)
    except (HMMError, np.linalg.LinAlgError) as exc:
        return {
            "probs": dict.fromkeys(_HMM_LABELS),
            "label": None,
            "n_days": int(features.shape[0]),
            "converged": False,
            "error": f"HMM fit failed: {exc}",
        }
    # Relabel raw states by mean daily return: lowest -> bear, highest -> bull.
    order = np.argsort(model.means[:, 0])  # ascending mean return
    current = posteriors[-1]
    probs = {
        _HMM_LABELS[rank]: float(current[state])
        for rank, state in enumerate(order)
    }
    label = max(probs, key=lambda k: probs[k])
    return {
        "probs": probs,
        "label": label,
        "n_days": int(features.shape[0]),
        "converged": bool(model.converged),
        "error": None,
    }


def _percentile(series: pd.Series, value: float | None, *, as_of: pd.Timestamp) -> float | None:
    if value is None:
        return None
    clean = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    clean = clean[clean.index <= as_of]
    if clean.empty:
        return None
    return float((clean.to_numpy() <= value).mean())


def _last_level(series: pd.Series | None, as_of: pd.Timestamp) -> float | None:
    if series is None:
        return None
    clean = pd.to_numeric(pd.Series(series), errors="coerce").dropna()
    clean = clean[clean.index <= as_of]
    if clean.empty:
        return None
    return float(clean.iloc[-1])


def build_context(
    *,
    vix: pd.Series | None = None,
    hy_oas: pd.Series | None = None,
    dgs10: pd.Series | None = None,
    dgs2: pd.Series | None = None,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """VIX / HY OAS percentiles and the current 10y-2y curve level."""
    stamp = pd.Timestamp(resolve_as_of(as_of))
    vix_level = _last_level(vix, stamp)
    hy_level = _last_level(hy_oas, stamp)
    ten = _last_level(dgs10, stamp)
    two = _last_level(dgs2, stamp)
    curve = (ten - two) if (ten is not None and two is not None) else None
    return {
        "vix_pct": _percentile(vix, vix_level, as_of=stamp) if vix is not None else None,
        "hy_oas_pct": _percentile(hy_oas, hy_level, as_of=stamp)
        if hy_oas is not None
        else None,
        "curve_10y_2y": curve,
        "vix_level": vix_level,
        "hy_oas_level": hy_level,
        "error": None,
    }


def build_state(
    *,
    spy_daily: pd.Series,
    ticker_daily: pd.Series,
    vix: pd.Series | None = None,
    hy_oas: pd.Series | None = None,
    dgs10: pd.Series | None = None,
    dgs2: pd.Series | None = None,
    as_of: date | str | None = None,
    run_hmm: bool = True,
) -> dict[str, Any]:
    """Assemble the packet's ``state`` section from raw daily series."""
    section: dict[str, Any] = {
        "spy": build_grid(spy_daily, as_of=as_of),
        "ticker": build_grid(ticker_daily, as_of=as_of),
        "hmm": build_hmm_opinion(spy_daily, as_of=as_of) if run_hmm else None,
        "context": build_context(
            vix=vix, hy_oas=hy_oas, dgs10=dgs10, dgs2=dgs2, as_of=as_of
        ),
        "version": MODULE_VERSIONS["state"],
    }
    return section


def state_section(
    panel: Panel,
    *,
    ticker: str,
    fred: Any | None = None,
    cache: Any | None = None,
    as_of: date | str | None = None,
    run_hmm: bool = True,
) -> dict[str, Any]:
    """Engine entry point: build ``state`` from a loaded :class:`Panel` + FRED.

    SPY and the ticker come from the panel; VIX / HY OAS / the 2s10s curve come
    from FRED via Prism's cached fetch. A missing FRED client simply leaves the
    context percentiles ``None`` rather than failing the section.
    """
    resolved = resolve_as_of(as_of or panel.as_of)
    spy_daily = panel.daily_close("SPY")
    ticker_daily = panel.daily_close(ticker)

    vix = hy_oas = dgs10 = dgs2 = None
    if fred is not None:
        from app.prism.macro import fetch_fred_series

        def _fetch(series_id: str) -> pd.Series | None:
            try:
                return fetch_fred_series(
                    fred, series_id, years=15, as_of=resolved, cache=cache
                )
            except Exception:
                return None

        vix = _fetch("VIXCLS")
        hy_oas = _fetch("BAMLH0A0HYM2")
        dgs10 = _fetch("DGS10")
        dgs2 = _fetch("DGS2")

    return build_state(
        spy_daily=spy_daily,
        ticker_daily=ticker_daily,
        vix=vix,
        hy_oas=hy_oas,
        dgs10=dgs10,
        dgs2=dgs2,
        as_of=resolved,
        run_hmm=run_hmm,
    )
