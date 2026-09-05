"""Empirical forward-return base rates (SPEC 5.3).

For ticker ``T`` and its industry ETF this module builds, per horizon
``h ∈ {1,2,3,6,12,18}`` months, the empirical distribution of the *forward*
``h``-month total return:

* **unconditional** — over all overlapping monthly windows in history;
* **conditional** — restricted to windows whose start month sat in the *current*
  2×2 volatility×trend cell (SPEC 5.2 definition, computed point-in-time here so
  ``base_rates`` stays a pure function of the price series);
* **shrunk** — the conditional distribution pulled toward the unconditional one
  by ``w = n_eff / (n_eff + 24)``, reported alongside ``w``;
* **vol-managed** — the unconditional distribution rescaled by
  ``target_vol / current_vol`` (Moreira & Muir 2017), a documented heuristic.

Windows overlap, so the effective sample size is ``n_eff = n / h`` — an
``h``-month forward return sampled every month shares ``h − 1`` months with its
neighbour. That ``n_eff`` is what drives the shrink weight, never the raw ``n``.

Everything is walk-forward: a forward return starting at month ``m`` is the only
place a value dated after ``m`` is used, and it is the *outcome* being measured,
never a conditioning input. Returns are decimal fractions (``0.034`` = 3.4%).
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

try:  # S1 owns the canonical contract; degrade to the SPEC constant if absent.
    from app.situate.contract import HORIZONS as _CONTRACT_HORIZONS

    HORIZONS: tuple[int, ...] = tuple(_CONTRACT_HORIZONS)
except Exception:  # noqa: BLE001 - the contract module may not exist yet
    HORIZONS = (1, 2, 3, 6, 12, 18)

#: Prior strength in the conditional→unconditional shrinkage (SPEC 5.3).
SHRINK_K = 24.0
#: Trading days per calendar month, used for the daily→volatility conversions.
TRADING_DAYS_PER_MONTH = 21
TRADING_DAYS_PER_YEAR = 252
#: Quantile levels reported for every distribution.
QUANTILE_LEVELS: tuple[float, ...] = (0.05, 0.25, 0.50, 0.75, 0.95)
_QUANTILE_KEYS: tuple[str, ...] = ("q05", "q25", "q50", "q75", "q95")


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clean_close(close: pd.Series | None) -> pd.Series:
    """A sorted, positive, tz-naive daily close series (may be empty)."""
    if close is None:
        return pd.Series(dtype="float64")
    series = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    series = series[series > 0]
    if series.empty:
        return series
    index = pd.to_datetime(series.index)
    if getattr(index, "tz", None) is not None:
        index = index.tz_convert(None)
    series.index = pd.DatetimeIndex(index).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series


def realized_vol_annual(close: pd.Series, *, window: int = TRADING_DAYS_PER_MONTH) -> pd.Series:
    """Rolling annualised realized volatility from daily log returns."""
    series = _clean_close(close)
    if series.shape[0] < window + 1:
        return pd.Series(dtype="float64")
    log_ret = np.log(series).diff().dropna()
    rolling = log_ret.rolling(int(window)).std(ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
    return rolling.dropna()


def cell_series(close: pd.Series) -> pd.Series:
    """Point-in-time 2×2 vol×trend cell label at each daily date.

    Volatility state compares the 21-day realized vol to its trailing two-year
    (504-session) *median*; trend state is the sign of the 12-month minus the
    1-month return. Both references use only data up to and including the date in
    question, so the label at date ``d`` is exactly what would have been known at
    ``d``. Labels look like ``"highvol_up"`` / ``"lowvol_down"``.
    """
    series = _clean_close(close)
    if series.shape[0] < TRADING_DAYS_PER_YEAR + TRADING_DAYS_PER_MONTH + 1:
        return pd.Series(dtype="object")
    vol = realized_vol_annual(series)
    if vol.empty:
        return pd.Series(dtype="object")
    # Trailing 2y median of the realized-vol series, point-in-time (min 63 obs).
    vol_median = vol.rolling(2 * TRADING_DAYS_PER_YEAR, min_periods=63).median()
    ret_12m = series / series.shift(TRADING_DAYS_PER_YEAR) - 1.0
    ret_1m = series / series.shift(TRADING_DAYS_PER_MONTH) - 1.0
    trend_metric = (ret_12m - ret_1m).reindex(vol.index)
    frame = pd.DataFrame(
        {"vol": vol, "vol_median": vol_median, "trend": trend_metric}
    ).dropna()
    if frame.empty:
        return pd.Series(dtype="object")
    vol_state = np.where(
        frame["vol"].to_numpy() > frame["vol_median"].to_numpy(), "highvol", "lowvol"
    )
    trend_state = np.where(frame["trend"].to_numpy() > 0.0, "up", "down")
    labels = [f"{v}_{t}" for v, t in zip(vol_state, trend_state, strict=True)]
    return pd.Series(labels, index=frame.index, dtype="object")


def current_cell(close: pd.Series) -> str | None:
    """The most recent 2×2 cell label, or ``None`` when history is too short."""
    labels = cell_series(close)
    if labels.empty:
        return None
    return str(labels.iloc[-1])


def _month_end_close(close: pd.Series) -> pd.Series:
    """Last close of each calendar month (the sampling grid for base rates)."""
    series = _clean_close(close)
    if series.empty:
        return series
    return series.resample("ME").last().dropna()


def forward_returns_monthly(close: pd.Series, horizon_months: int) -> pd.Series:
    """Overlapping forward ``h``-month total returns indexed by their start month.

    Sampled at month-ends: the value at month ``m`` is ``P[m+h] / P[m] − 1``. The
    windows overlap (consecutive starts share ``h − 1`` months); the effective
    independent count is ``n / h``, applied by the callers via :func:`_stats`.
    """
    monthly = _month_end_close(close)
    h = int(horizon_months)
    if monthly.shape[0] <= h or h <= 0:
        return pd.Series(dtype="float64")
    forward = monthly.shift(-h) / monthly - 1.0
    return forward.dropna()


def _stats(returns: np.ndarray, *, horizon_months: int) -> dict[str, Any]:
    """Quantiles, hit rate, ``n`` and ``n_eff = n / h`` for a return sample."""
    clean = returns[np.isfinite(returns)]
    n = int(clean.size)
    if n == 0:
        block: dict[str, Any] = dict.fromkeys(_QUANTILE_KEYS)
        block.update({"hit": None, "n": 0, "n_eff": 0.0, "mean": None})
        return block
    quantiles = np.quantile(clean, QUANTILE_LEVELS)
    block = {key: float(value) for key, value in zip(_QUANTILE_KEYS, quantiles, strict=True)}
    block["hit"] = float(np.mean(clean > 0.0))
    block["mean"] = float(np.mean(clean))
    block["n"] = n
    block["n_eff"] = round(n / float(max(1, horizon_months)), 4)
    return block


def _shrink(cond: dict[str, Any], uncond: dict[str, Any], *, k: float = SHRINK_K) -> dict[str, Any]:
    """Shrink the conditional block toward the unconditional one.

    ``w = n_eff / (n_eff + k)`` on the conditional effective sample; each quantile
    (and the hit rate / mean) is ``w·cond + (1−w)·uncond``. ``w`` is reported so a
    reader can see how much the conditional sample was trusted.
    """
    n_eff = float(cond.get("n_eff") or 0.0)
    w = n_eff / (n_eff + k) if (n_eff + k) > 0 else 0.0
    shrunk: dict[str, Any] = {"w": round(w, 4), "n_eff": n_eff, "n": cond.get("n", 0)}
    for key in (*_QUANTILE_KEYS, "hit", "mean"):
        c = _finite(cond.get(key))
        u = _finite(uncond.get(key))
        if c is None or u is None:
            shrunk[key] = u if c is None else c
        else:
            shrunk[key] = w * c + (1.0 - w) * u
    return shrunk


def _vol_managed(
    uncond: dict[str, Any], *, target_vol: float | None, current_vol: float | None
) -> dict[str, Any]:
    """Rescale the unconditional quantiles by ``target_vol / current_vol``.

    A documented heuristic (Moreira & Muir 2017): sizing inversely to volatility
    rescales the whole return distribution by ``target/current``. The sign of
    every scaled return is preserved, so the hit rate is unchanged.
    """
    block: dict[str, Any] = {
        "target_vol": target_vol,
        "current_vol": current_vol,
        "scale": None,
        "hit": uncond.get("hit"),
        "n": uncond.get("n", 0),
        "n_eff": uncond.get("n_eff", 0.0),
    }
    t = _finite(target_vol)
    c = _finite(current_vol)
    if t is None or c is None or c <= 0.0:
        for key in _QUANTILE_KEYS:
            block[key] = None
        block["scale_error"] = "target or current volatility unavailable"
        return block
    scale = t / c
    block["scale"] = scale
    for key in _QUANTILE_KEYS:
        value = _finite(uncond.get(key))
        block[key] = value * scale if value is not None else None
    return block


def _symbol_horizon(
    close: pd.Series,
    *,
    horizon_months: int,
    cell_at_month: pd.Series,
    target_cell: str | None,
    target_vol: float | None,
    current_vol: float | None,
) -> dict[str, Any]:
    """One symbol × one horizon: uncond / cond / shrunk / vol-managed blocks."""
    forward = forward_returns_monthly(close, horizon_months)
    uncond = _stats(forward.to_numpy(dtype=float), horizon_months=horizon_months)

    if target_cell is not None and not cell_at_month.empty and not forward.empty:
        aligned_cells = cell_at_month.reindex(forward.index)
        mask = (aligned_cells == target_cell).to_numpy()
        cond_returns = forward.to_numpy(dtype=float)[mask]
        cond = _stats(cond_returns, horizon_months=horizon_months)
        cond["cell"] = target_cell
    else:
        cond = _stats(np.asarray([], dtype=float), horizon_months=horizon_months)
        cond["cell"] = target_cell

    shrunk = _shrink(cond, uncond)
    shrunk["cell"] = target_cell
    vol_managed = _vol_managed(uncond, target_vol=target_vol, current_vol=current_vol)
    return {
        "uncond": uncond,
        "cond": cond,
        "shrunk": shrunk,
        "vol_managed": vol_managed,
    }


def _target_and_current_vol(close: pd.Series) -> tuple[float | None, float | None]:
    """Trailing-median (target) and latest (current) annualised realized vol."""
    vol = realized_vol_annual(close)
    if vol.empty:
        return None, None
    target = _finite(float(vol.median()))
    current = _finite(float(vol.iloc[-1]))
    return target, current


def build_symbol_base_rates(
    close: pd.Series,
    *,
    current_cell_label: str | None,
    horizons: tuple[int, ...] = HORIZONS,
    target_vol: float | None = None,
) -> dict[str, Any]:
    """Per-horizon base rates for one symbol.

    ``current_cell_label`` is the cell to condition on; when ``None`` the cell is
    read from ``close`` itself. ``target_vol`` overrides the trailing-median
    volatility used by the vol-managed variant.
    """
    series = _clean_close(close)
    labels = cell_series(series)
    cell_month = labels.resample("ME").last() if not labels.empty else pd.Series(dtype="object")

    resolved_cell = current_cell_label if current_cell_label is not None else current_cell(series)
    median_vol, current_vol = _target_and_current_vol(series)
    use_target = target_vol if target_vol is not None else median_vol

    by_horizon: dict[str, Any] = {}
    for h in horizons:
        by_horizon[str(h)] = _symbol_horizon(
            series,
            horizon_months=int(h),
            cell_at_month=cell_month,
            target_cell=resolved_cell,
            target_vol=use_target,
            current_vol=current_vol,
        )
    return {
        "by_horizon": by_horizon,
        "cell": resolved_cell,
        "target_vol": use_target,
        "current_vol": current_vol,
        "n_months": int(_month_end_close(series).shape[0]),
    }


def conditional_iqr_by_horizon(section: dict[str, Any]) -> dict[int, float | None]:
    """The shrunk conditional inter-quartile range per horizon (for width_ratio).

    ``implied`` divides its risk-neutral IQR by this to form ``width_ratio``.
    """
    result: dict[int, float | None] = {}
    for key, block in (section.get("by_horizon") or {}).items():
        try:
            h = int(key)
        except (TypeError, ValueError):
            continue
        shrunk = (block or {}).get("shrunk") or {}
        q75 = _finite(shrunk.get("q75"))
        q25 = _finite(shrunk.get("q25"))
        result[h] = (q75 - q25) if (q75 is not None and q25 is not None) else None
    return result


def shrunk_median_by_horizon(section: dict[str, Any]) -> dict[int, float | None]:
    """The shrunk conditional median per horizon (target for the implied shift)."""
    result: dict[int, float | None] = {}
    for key, block in (section.get("by_horizon") or {}).items():
        try:
            h = int(key)
        except (TypeError, ValueError):
            continue
        shrunk = (block or {}).get("shrunk") or {}
        result[h] = _finite(shrunk.get("q50"))
    return result


def build_base_rates(
    close_t: pd.Series,
    close_industry: pd.Series | None = None,
    *,
    current_cell_label: str | None = None,
    horizons: tuple[int, ...] = HORIZONS,
    target_vol: float | None = None,
) -> dict[str, Any]:
    """Build ``packet["base_rates"]`` (SPEC 5.3).

    ``by_horizon[h]`` carries the ticker's ``uncond``/``cond``/``shrunk``/
    ``vol_managed`` blocks plus an ``industry`` sub-block (``uncond``/``cond``/
    ``shrunk``) computed on the industry ETF's own history and conditioned on the
    *ticker's* current cell. Missing history degrades to empty blocks with a
    reason recorded in ``errors`` — never a fabricated number.
    """
    series_t = _clean_close(close_t)
    errors: list[dict[str, str]] = []

    if series_t.empty:
        errors.append({"source": "base_rates.ticker", "error": "no usable price history"})
        return {
            "method": "empirical_overlapping_monthly",
            "shrink_k": SHRINK_K,
            "cell": current_cell_label,
            "by_horizon": {str(h): _empty_horizon(h) for h in horizons},
            "errors": errors,
        }

    resolved_cell = current_cell_label if current_cell_label is not None else current_cell(series_t)
    ticker_rates = build_symbol_base_rates(
        series_t, current_cell_label=resolved_cell, horizons=horizons, target_vol=target_vol
    )

    industry_rates: dict[str, Any] | None = None
    series_ind = _clean_close(close_industry)
    if not series_ind.empty:
        try:
            industry_rates = build_symbol_base_rates(
                series_ind,
                current_cell_label=resolved_cell,
                horizons=horizons,
                target_vol=target_vol,
            )
        except Exception as exc:  # noqa: BLE001 - industry is a best-effort overlay
            industry_rates = None
            errors.append({"source": "base_rates.industry", "error": str(exc)})
    else:
        errors.append(
            {"source": "base_rates.industry", "error": "no industry ETF history supplied"}
        )

    by_horizon: dict[str, Any] = {}
    for h in horizons:
        key = str(h)
        block = dict(ticker_rates["by_horizon"][key])
        if industry_rates is not None:
            ind_block = industry_rates["by_horizon"][key]
            block["industry"] = {
                "uncond": ind_block["uncond"],
                "cond": ind_block["cond"],
                "shrunk": ind_block["shrunk"],
            }
        else:
            block["industry"] = None
        by_horizon[key] = block

    return {
        "method": "empirical_overlapping_monthly",
        "shrink_k": SHRINK_K,
        "cell": resolved_cell,
        "target_vol": ticker_rates["target_vol"],
        "current_vol": ticker_rates["current_vol"],
        "n_months": ticker_rates["n_months"],
        "by_horizon": by_horizon,
        "errors": errors,
    }


def _empty_horizon(horizon_months: int) -> dict[str, Any]:
    empty = _stats(np.asarray([], dtype=float), horizon_months=horizon_months)
    return {
        "uncond": empty,
        "cond": {**empty, "cell": None},
        "shrunk": {**empty, "w": 0.0, "cell": None},
        "vol_managed": {**empty, "scale": None, "target_vol": None, "current_vol": None},
        "industry": None,
    }
