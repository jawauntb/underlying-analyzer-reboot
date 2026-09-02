"""Shannon entropy of a ticker's return distribution.

Entropy is Prism's "is this series structured or is it noise?" gauge. A window
whose returns pile into a few bins of a *fixed* grid is **structured** (low
entropy); a window that spreads across the whole grid is indistinguishable from
a wide, unconstrained distribution and is treated as **noise**.

Binning grid
------------

The primary reading uses a **fixed-width grid**: ``bins`` equal-width cells
spanning ``[-3*sigma, +3*sigma]`` where ``sigma`` is the **full-sample** daily
return standard deviation. The grid is therefore fixed over calendar time, so
two windows of the same ticker are measured on the same ruler and a window that
compresses (or blows out) relative to the ticker's long-run dispersion actually
moves ``H``. Returns outside ``+/-3*sigma`` are clipped into the edge bins, and
``H`` is normalised by ``log2(bins)`` so it lands in ``[0, 1]``: 0 = every
return in one cell, 1 = uniform across all cells.

This replaces the previous quantile grid as the primary reading. A quantile grid
built on the full sample is uninformative by construction: any window that looks
like the full sample lands one tenth of its mass in each of the ten cells and
scores ``H ~ 1``, so a liquid equity read "noise" in every window and the 0.35 /
0.70 thresholds could never fire. The old value is still reported per window as
``H_quantile`` for continuity, and :func:`quantile_bin_edges` is still exported.

Reading the label
-----------------

``structure`` / ``mixed`` / ``noise`` are a statement about *dispersion against
the ticker's own long-run dispersion*, not about predictability. Below
``STRUCTURE_THRESHOLD`` the window's returns are concentrated in a few cells of
the fixed grid — a quiet, tightly-ranged tape. Above ``NOISE_THRESHOLD`` the
window fills the grid, which is what an unconstrained tape looks like. The
``percentile`` and ``relative_classification`` fields put the same reading
inside the ticker's own history of that window length.

``sigma`` is still estimated on the whole sample, which is in-sample by
construction: the scale applied at date ``t`` was set partly by returns after
``t``. That is a scale choice rather than a signal, but it is a look-ahead all
the same, so :func:`entropy_backtest` states it in ``bin_grid_note`` rather than
claiming it has none, and enters its position at the close of the signal day so
the signal day's own move, which is already inside the entropy window, is not
counted as forward P&L.

All functions are pure — pandas/numpy in, JSON-serialisable dicts out.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "BIN_GRID",
    "DEFAULT_BINS",
    "ENTROPY_WINDOWS",
    "NOISE_THRESHOLD",
    "SIGMA_MULTIPLE",
    "STRUCTURE_THRESHOLD",
    "build_entropy",
    "classify_entropy",
    "entropy_backtest",
    "entropy_series",
    "fixed_width_bin_edges",
    "full_sample_sigma",
    "quantile_bin_edges",
    "shannon_entropy",
]

DEFAULT_BINS = 10
#: Window label -> trading days.
ENTROPY_WINDOWS: dict[str, int] = {"1m": 21, "2m": 42, "3m": 63, "6m": 126, "12m": 252}
STRUCTURE_THRESHOLD = 0.35
NOISE_THRESHOLD = 0.70
#: Half-width of the fixed grid, in full-sample standard deviations.
SIGMA_MULTIPLE = 3.0
#: Name of the primary binning grid, reported in every section that uses it.
BIN_GRID = "fixed_width_3sigma"

_GRID_NOTE = (
    f"{DEFAULT_BINS} equal-width bins spanning [-{SIGMA_MULTIPLE:g} sigma, "
    f"+{SIGMA_MULTIPLE:g} sigma] where sigma is the full-sample daily-return standard "
    "deviation; returns beyond the edges are clipped into the edge bins and H is "
    "normalised by log2(bins). The grid is fixed over time, so windows are comparable."
)


def _clean(values: Sequence[float] | FloatArray | pd.Series) -> FloatArray:
    array = np.asarray(pd.Series(values, dtype="float64").to_numpy(), dtype=np.float64)
    return array[np.isfinite(array)]


def full_sample_sigma(values: Sequence[float] | FloatArray | pd.Series) -> float:
    """Standard deviation of ``values`` (ddof=1), the scale of the fixed grid."""
    array = _clean(values)
    if array.size < 2:
        return 0.0
    sigma = float(np.std(array, ddof=1))
    return sigma if math.isfinite(sigma) and sigma > 0 else 0.0


def fixed_width_bin_edges(
    sigma: float, *, bins: int = DEFAULT_BINS, sigma_multiple: float = SIGMA_MULTIPLE
) -> FloatArray:
    """``bins + 1`` equal-width edges spanning ``[-k*sigma, +k*sigma]``.

    ``sigma`` is meant to be the *full-sample* daily-return standard deviation so
    the grid does not move as the window slides. A non-positive or non-finite
    ``sigma`` raises: there is no scale to bin on, and inventing one would make
    a constant series look structured for the wrong reason.
    """
    if bins < 2:
        raise ValueError("bins must be >= 2")
    value = float(sigma)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("sigma must be positive and finite to build a fixed-width grid")
    half = float(sigma_multiple) * value
    return np.linspace(-half, half, bins + 1, dtype=np.float64)


def quantile_bin_edges(
    values: Sequence[float] | FloatArray | pd.Series, *, bins: int = DEFAULT_BINS
) -> FloatArray:
    """Bin edges on the empirical quantile grid of ``values``.

    Kept for the secondary ``H_quantile`` reading. Returns ``bins + 1`` strictly
    increasing edges. Degenerate samples (every value identical, or so tied that
    the quantiles collapse) fall back to an equal-width grid; a truly constant
    sample returns a single degenerate interval, which :func:`shannon_entropy`
    scores as 0.
    """
    if bins < 2:
        raise ValueError("bins must be >= 2")
    array = _clean(values)
    if array.size == 0:
        raise ValueError("cannot build bin edges from an empty sample")
    quantiles = np.linspace(0.0, 1.0, bins + 1)
    edges = np.quantile(array, quantiles)
    if np.unique(edges).size < 3:
        low = float(np.min(array))
        high = float(np.max(array))
        if not math.isfinite(low) or not math.isfinite(high) or high <= low:
            return np.array([low, low], dtype=np.float64)
        edges = np.linspace(low, high, bins + 1)
    # Nudge duplicate interior edges apart so np.digitize keeps `bins` cells.
    edges = np.asarray(edges, dtype=np.float64)
    for index in range(1, edges.size):
        if edges[index] <= edges[index - 1]:
            edges[index] = np.nextafter(edges[index - 1], np.inf)
    return edges


def shannon_entropy(
    values: Sequence[float] | FloatArray | pd.Series,
    *,
    bins: int = DEFAULT_BINS,
    edges: FloatArray | None = None,
    sigma: float | None = None,
) -> float:
    """Normalised Shannon entropy of ``values`` in ``[0, 1]``.

    ``H = -sum(p_i log2 p_i) / log2(bins)`` over the bin histogram. When
    ``edges`` is omitted the **fixed-width** grid of
    :func:`fixed_width_bin_edges` is used, scaled by ``sigma`` when given and
    otherwise by the standard deviation of ``values`` themselves. Values outside
    the grid fall into the edge bins. A constant sample has no scale and scores
    0.
    """
    array = _clean(values)
    if array.size == 0:
        return float("nan")
    if edges is None:
        scale = full_sample_sigma(array) if sigma is None else float(sigma)
        if not math.isfinite(scale) or scale <= 0:
            return 0.0
        edges = fixed_width_bin_edges(scale, bins=bins)
    edges = np.asarray(edges, dtype=np.float64)
    if edges.size < 3:
        return 0.0
    indices = np.clip(np.digitize(array, edges[1:-1], right=False), 0, edges.size - 2)
    counts = np.bincount(indices, minlength=edges.size - 1).astype(np.float64)
    total = float(counts.sum())
    if total <= 0:
        return float("nan")
    probabilities = counts[counts > 0] / total
    entropy = float(-np.sum(probabilities * np.log2(probabilities)))
    denominator = math.log2(edges.size - 1)
    if denominator <= 0:
        return 0.0
    return float(min(max(entropy / denominator, 0.0), 1.0))


def classify_entropy(value: float | None) -> str:
    """Label an entropy reading: ``structure`` / ``mixed`` / ``noise``.

    Read on the fixed ``[-3 sigma, +3 sigma]`` grid: *structure* means the
    window's returns concentrate in a few cells of the ticker's long-run range,
    *noise* means they fill it.
    """
    if value is None or not math.isfinite(float(value)):
        return "unknown"
    if value < STRUCTURE_THRESHOLD:
        return "structure"
    if value > NOISE_THRESHOLD:
        return "noise"
    return "mixed"


def entropy_series(
    returns: pd.Series,
    *,
    window: int = 63,
    bins: int = DEFAULT_BINS,
    edges: FloatArray | None = None,
) -> pd.Series:
    """Rolling entropy of ``returns`` over a fixed-length window.

    The bin grid is the full-sample fixed-width grid unless ``edges`` is
    supplied, so the series is directly comparable across time. Computed with a
    single digitisation plus a cumulative-sum sliding histogram, so the cost is
    linear in the sample rather than ``O(n * window)``.
    """
    clean = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    if window < 2 or clean.shape[0] < window:
        return pd.Series(dtype="float64")
    if edges is None:
        sigma = full_sample_sigma(clean)
        if sigma <= 0:
            return pd.Series(0.0, index=clean.index[window - 1 :], name="entropy")
        grid = fixed_width_bin_edges(sigma, bins=bins)
    else:
        grid = np.asarray(edges, np.float64)
    n_bins = int(grid.size) - 1
    if n_bins < 2:
        return pd.Series(0.0, index=clean.index[window - 1 :], name="entropy")

    values = clean.to_numpy(dtype=np.float64)
    indices = np.clip(np.digitize(values, grid[1:-1], right=False), 0, n_bins - 1)
    one_hot = np.zeros((values.shape[0], n_bins), dtype=np.float64)
    one_hot[np.arange(values.shape[0]), indices] = 1.0
    cumulative = np.vstack([np.zeros((1, n_bins), dtype=np.float64), np.cumsum(one_hot, axis=0)])
    counts = cumulative[window:] - cumulative[:-window]
    probabilities = counts / float(window)
    with np.errstate(divide="ignore", invalid="ignore"):
        terms = np.where(probabilities > 0, -probabilities * np.log2(probabilities), 0.0)
    entropies = terms.sum(axis=1) / math.log2(n_bins)
    entropies = np.clip(entropies, 0.0, 1.0)
    return pd.Series(entropies, index=clean.index[window - 1 :], name="entropy")


def entropy_backtest(
    returns: pd.Series,
    *,
    window: int = 63,
    horizon_days: int = 21,
    bins: int = DEFAULT_BINS,
    edges: FloatArray | None = None,
    structure_threshold: float = STRUCTURE_THRESHOLD,
    noise_threshold: float = NOISE_THRESHOLD,
    min_bucket: int = 20,
) -> dict[str, Any]:
    """Does low entropy predict a positive forward return?

    Entropy is the fixed-width reading of :func:`entropy_series` (the same grid
    the packet publishes), so the buckets below are the same "structure" and
    "noise" the memo talks about.

    For every day with a valid rolling entropy reading we take a long position at
    that day's close and hold ``horizon_days``; a *win* is a positive forward
    return. The signal day's own return is deliberately excluded from the P&L —
    it is already inside the entropy window, so a position taken on the signal
    could not have captured it. Days are split into a low bucket
    (``H < structure_threshold``) and a high bucket (``H > noise_threshold``). If
    either fixed-threshold bucket is thinner than ``min_bucket`` the split falls
    back to the entropy terciles of the sample and ``split`` records which rule
    was used.

    One look-ahead remains and is reported rather than hidden: the grid's scale
    (``sigma``) is estimated on the whole sample, so the ruler applied at date
    ``t`` was set partly by returns after ``t``. ``bin_grid_note`` says so.
    """
    empty: dict[str, Any] = {
        "low_entropy_win_rate": None,
        "high_entropy_win_rate": None,
        "n_low": 0,
        "n_high": 0,
        "edge": None,
        "split": None,
        "horizon_days": horizon_days,
        "window_days": window,
        "reason": None,
    }
    clean = pd.to_numeric(pd.Series(returns), errors="coerce").dropna()
    if clean.shape[0] < window + horizon_days + 2 * min_bucket:
        empty["reason"] = (
            f"need at least {window + horizon_days + 2 * min_bucket} returns, got {clean.shape[0]}"
        )
        return empty

    series = entropy_series(clean, window=window, bins=bins, edges=edges)
    if series.empty:
        empty["reason"] = "rolling entropy series is empty"
        return empty

    log_returns = np.log1p(clean.to_numpy(dtype=np.float64))
    cumulative = np.concatenate([[0.0], np.cumsum(log_returns)])
    positions = {timestamp: index for index, timestamp in enumerate(clean.index)}

    entropies: list[float] = []
    forwards: list[float] = []
    for timestamp, value in series.items():
        index = positions[timestamp]
        # The entropy reading at `index` is computed over returns up to and
        # including day `index`, so day `index`'s own move is already inside the
        # signal window. A position taken on that signal is entered at the close
        # of day `index` and earns days index+1 .. index+horizon.
        end = index + 1 + horizon_days
        if end >= cumulative.shape[0]:
            break
        forward = float(math.expm1(cumulative[end] - cumulative[index + 1]))
        entropies.append(float(value))
        forwards.append(forward)

    if not entropies:
        empty["reason"] = "no observations with a full forward horizon"
        return empty

    entropy_array = np.asarray(entropies, dtype=np.float64)
    forward_array = np.asarray(forwards, dtype=np.float64)

    low_mask = entropy_array < structure_threshold
    high_mask = entropy_array > noise_threshold
    split = "fixed_threshold"
    low_cut: float = structure_threshold
    high_cut: float = noise_threshold
    if int(low_mask.sum()) < min_bucket or int(high_mask.sum()) < min_bucket:
        low_cut = float(np.quantile(entropy_array, 1.0 / 3.0))
        high_cut = float(np.quantile(entropy_array, 2.0 / 3.0))
        low_mask = entropy_array <= low_cut
        high_mask = entropy_array >= high_cut
        split = "tercile"

    n_low = int(low_mask.sum())
    n_high = int(high_mask.sum())
    low_win = float(np.mean(forward_array[low_mask] > 0.0)) if n_low else None
    high_win = float(np.mean(forward_array[high_mask] > 0.0)) if n_high else None
    return {
        "low_entropy_win_rate": low_win,
        "high_entropy_win_rate": high_win,
        "n_low": n_low,
        "n_high": n_high,
        "edge": (low_win - high_win) if (low_win is not None and high_win is not None) else None,
        "split": split,
        "low_threshold": low_cut,
        "high_threshold": high_cut,
        "horizon_days": horizon_days,
        "window_days": window,
        "base_win_rate": float(np.mean(forward_array > 0.0)),
        "low_mean_forward_return": float(np.mean(forward_array[low_mask])) if n_low else None,
        "high_mean_forward_return": float(np.mean(forward_array[high_mask])) if n_high else None,
        "n_observations": int(entropy_array.shape[0]),
        "entry": "close of the signal day; forward return excludes the signal day itself",
        "bin_grid": BIN_GRID,
        "bin_grid_note": (
            f"{_GRID_NOTE} The scale (sigma) is estimated in-sample on the whole history, "
            "so the ruler applied at date t was set partly by returns after t."
        ),
        "reason": None,
    }


def build_entropy(
    close: pd.Series,
    *,
    bins: int = DEFAULT_BINS,
    windows: Mapping[str, int] | None = None,
    series_window: int = 63,
    horizon_days: int = 21,
) -> dict[str, Any]:
    """Build the packet's ``entropy`` section.

    Parameters
    ----------
    close:
        Daily closes for the ticker.
    bins / windows / series_window / horizon_days:
        Binning resolution, the per-window entropy readings to report, the
        rolling window used for the weekly series and the backtest, and the
        backtest holding period.
    """
    window_map = dict(ENTROPY_WINDOWS if windows is None else windows)
    section: dict[str, Any] = {
        "bins": bins,
        "bin_grid": BIN_GRID,
        "bin_grid_note": _GRID_NOTE,
        "sigma_multiple": SIGMA_MULTIPLE,
        "sigma_full_sample": None,
        "method": "shannon_normalised_by_log2_bins_on_a_fixed_width_3sigma_grid",
        "thresholds": {"structure_below": STRUCTURE_THRESHOLD, "noise_above": NOISE_THRESHOLD},
        "windows": {},
        "series": [],
        "backtest": None,
        "error": None,
    }
    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    prices = prices[prices > 0]
    if prices.shape[0] < 30:
        section["error"] = f"need at least 30 closes, got {prices.shape[0]}"
        section["windows"] = {
            label: {"H": None, "classification": "unknown", "n": 0} for label in window_map
        }
        return section

    returns = prices.pct_change().dropna()
    sigma = full_sample_sigma(returns)
    section["sigma_full_sample"] = sigma if sigma > 0 else None
    if sigma <= 0:
        section["error"] = "full-sample return standard deviation is zero; no scale to bin on"
        section["windows"] = {
            label: {"H": None, "classification": "unknown", "n": 0} for label in window_map
        }
        return section

    grid = fixed_width_bin_edges(sigma, bins=bins)
    quantile_grid = quantile_bin_edges(returns, bins=bins)
    section["bin_edges"] = [float(value) for value in grid]
    section["quantile_bin_edges"] = [float(value) for value in quantile_grid]

    for label, days in window_map.items():
        subset = returns.iloc[-days:]
        if subset.shape[0] < max(bins, 10):
            section["windows"][label] = {
                "H": None,
                "classification": "unknown",
                "n": int(subset.shape[0]),
                "reason": f"need at least {max(bins, 10)} returns for {label}",
            }
            continue
        value = shannon_entropy(subset, bins=bins, edges=grid)
        entry: dict[str, Any] = {
            "H": value,
            "classification": classify_entropy(value),
            "n": int(subset.shape[0]),
            "window_days": days,
            "bin_grid": BIN_GRID,
            # The superseded reading: a full-sample quantile grid scores any
            # window that resembles the full sample at H ~ 1, so every window of
            # a liquid equity read "noise". Kept for continuity, not for the
            # classification.
            "H_quantile": shannon_entropy(subset, bins=bins, edges=quantile_grid),
        }
        history = entropy_series(returns, window=days, bins=bins, edges=grid)
        if history.shape[0] >= 30 and math.isfinite(value):
            percentile = float((history <= value).mean())
            entry["percentile"] = percentile
            entry["relative_classification"] = (
                "structure_vs_own_history"
                if percentile <= 1.0 / 3.0
                else ("noise_vs_own_history" if percentile >= 2.0 / 3.0 else "typical")
            )
            entry["history_min"] = float(history.min())
            entry["history_max"] = float(history.max())
            entry["history_median"] = float(history.median())
        section["windows"][label] = entry

    rolling = entropy_series(returns, window=series_window, bins=bins, edges=grid)
    if not rolling.empty:
        if isinstance(rolling.index, pd.DatetimeIndex):
            # Last reading of each calendar week, keeping the real trading date.
            weekly = rolling.groupby(rolling.index.to_period("W"), sort=True).tail(1)
        else:
            weekly = rolling
        section["series"] = [
            {
                "date": str(pd.Timestamp(timestamp).date()),
                "H": float(value),
                "classification": classify_entropy(float(value)),
            }
            for timestamp, value in weekly.items()
        ]
        section["current"] = {
            "H": float(rolling.iloc[-1]),
            "classification": classify_entropy(float(rolling.iloc[-1])),
            "percentile": float((rolling <= rolling.iloc[-1]).mean()),
            "window_days": series_window,
            "bin_grid": BIN_GRID,
        }

    section["backtest"] = entropy_backtest(
        returns, window=series_window, horizon_days=horizon_days, bins=bins, edges=grid
    )
    return section
