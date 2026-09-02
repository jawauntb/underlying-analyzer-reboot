"""Spectral decomposition of a price series.

Prism's namesake step: take the log price, remove its linear trend, and split
what is left into Fourier modes. Each surviving mode carries a period, an
amplitude, a phase, its share of the residual power and where in its cycle the
series currently sits. A trend term plus the top modes are then extrapolated to
give a cycle-implied expected return per horizon, and a hold-out check reports
whether the recent 60 days actually behaved like the fitted cycles or have
drifted away from them.

This is descriptive spectral estimation, not a claim that markets are periodic:
``reconstruction_r2`` and ``consistency`` are reported precisely so a consumer
can discount the projection when the cycles do not explain much.

Projection discipline
---------------------

The raw fit extrapolates beautifully and forecasts absurdly. Three constraints
keep :func:`build_spectral`'s ``projection`` inside what the fit can support:

1. **Robust recent trend, not the whole-sample OLS slope.** The least-squares
   slope over ten years of log price is dominated by the sample's biggest
   re-rating, and projecting it forward reads a decade of multiple expansion as
   a permanent drift (NVDA: +62% a year). The projected trend is instead the
   **median** one-day log change over the last ``_ROBUST_TREND_DAYS`` sessions —
   a median is unmoved by a handful of gap days — **shrunk 50% toward zero**,
   because even a robust trailing slope is a description of the past.
   ``trend`` still reports the OLS fit that produced the residual; the slope the
   projection actually uses is published separately as ``robust_trend``.
2. **Cycle damped by the reconstruction R-squared.** The cycle contribution is
   multiplied by ``reconstruction_r2``: a fit that explains a third of the
   detrended variance moves the forecast a third as far.
3. **Cycle extrapolation truncated at a quarter of the dominant period.** Past
   ``dominant_period_days / 4`` the phase of an estimated mode is not
   identified well enough to be worth extrapolating, so the cycle term is held
   at its value at the truncation point and the horizon's ``confidence`` is
   forced below 0.3 (and keeps falling with the horizon) so the mixture shrinks
   the component away.
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
    "SPECTRAL_HORIZONS",
    "build_spectral",
    "cycle_position",
    "detrend_log_price",
    "reconstruct",
    "robust_trend_slope",
    "spectral_modes",
]

#: Horizon label -> trading days.
SPECTRAL_HORIZONS: dict[str, int] = {
    "1m": 21,
    "2m": 42,
    "3m": 63,
    "6m": 126,
    "12m": 252,
    "18m": 378,
}
_MIN_PERIOD_DAYS = 5
_PEAK_BAND = math.pi / 8.0
#: Lookback for the robust trend used by the projection (about two years).
_ROBUST_TREND_DAYS = 504
#: How much of the robust trailing slope survives into the forecast.
_ROBUST_TREND_KEEP = 0.5
#: Cycle extrapolation is truncated past this fraction of the dominant period.
_CYCLE_TRUNCATION_FRACTION = 0.25
#: Confidence ceiling applied once the cycle extrapolation is truncated. Must
#: stay strictly below the 0.3 the consumers treat as "worth weighting".
_TRUNCATED_CONFIDENCE_CEILING = 0.29


def detrend_log_price(close: pd.Series | Sequence[float]) -> tuple[FloatArray, dict[str, float]]:
    """Remove a least-squares linear trend from ``log(close)``.

    Returns ``(residual, trend)`` where ``trend`` holds the fitted
    ``intercept``/``slope_per_day`` (in log points per trading day), the implied
    ``annualized_drift`` and the trend's own ``r2``.
    """
    prices = np.asarray(pd.Series(close, dtype="float64").dropna().to_numpy(), dtype=np.float64)
    prices = prices[np.isfinite(prices) & (prices > 0)]
    if prices.size < 3:
        raise ValueError("need at least 3 positive closes to detrend")
    logs = np.log(prices)
    t = np.arange(prices.size, dtype=np.float64)
    design = np.column_stack([np.ones_like(t), t])
    coefficients, *_ = np.linalg.lstsq(design, logs, rcond=None)
    fitted = design @ coefficients
    residual = logs - fitted
    total = float(np.sum((logs - logs.mean()) ** 2))
    r2 = float(1.0 - np.sum(residual**2) / total) if total > 0 else 0.0
    trend = {
        "intercept": float(coefficients[0]),
        "slope_per_day": float(coefficients[1]),
        "annualized_drift": float(math.expm1(float(coefficients[1]) * 252.0)),
        "r2": r2,
    }
    return residual, trend


def robust_trend_slope(
    close: pd.Series | Sequence[float],
    *,
    lookback: int = _ROBUST_TREND_DAYS,
    keep: float = _ROBUST_TREND_KEEP,
) -> dict[str, Any]:
    """Median one-day log slope over the last ``lookback`` sessions, shrunk.

    The projection needs a drift that a handful of gap days cannot set. The
    median daily log change is that: it is the slope of the typical session
    rather than of the sample's biggest re-rating. It is then multiplied by
    ``keep`` (0.5 by default) so even the robust trailing slope only half
    survives into the forecast.

    Returns ``slope_per_day`` (the shrunk slope the projection uses),
    ``median_slope_per_day`` (before shrinking), the implied annualised drifts
    and the sample size actually used.
    """
    prices = np.asarray(pd.Series(close, dtype="float64").dropna().to_numpy(), dtype=np.float64)
    prices = prices[np.isfinite(prices) & (prices > 0)]
    if prices.size < 3:
        return {
            "slope_per_day": 0.0,
            "median_slope_per_day": 0.0,
            "annualized_drift": 0.0,
            "raw_annualized_drift": 0.0,
            "lookback_days": 0,
            "shrink_to_zero": float(1.0 - keep),
            "reason": "need at least 3 positive closes",
        }
    steps = np.diff(np.log(prices))
    window = steps[-int(lookback) :] if lookback and lookback > 0 else steps
    median = float(np.median(window))
    slope = float(median * float(keep))
    return {
        "slope_per_day": slope,
        "median_slope_per_day": median,
        "annualized_drift": float(math.expm1(slope * 252.0)),
        "raw_annualized_drift": float(math.expm1(median * 252.0)),
        "lookback_days": int(window.size),
        "shrink_to_zero": float(1.0 - keep),
        "method": (
            f"median daily log change over the last {int(window.size)} sessions, "
            f"shrunk {1.0 - float(keep):.0%} toward zero"
        ),
        "reason": None,
    }


def cycle_position(phase: float) -> tuple[str, float]:
    """Label a mode's instantaneous phase.

    The mode is written ``A * cos(theta)``. ``theta`` near 0 is the top of the
    cycle, ``theta`` near ``pi`` the bottom; between them the mode is falling on
    ``(0, pi)`` and rising on ``(pi, 2*pi)``. Returns ``(label, fraction)`` where
    ``fraction = theta / (2*pi)`` in ``[0, 1)``.
    """
    theta = float(phase) % (2.0 * math.pi)
    fraction = theta / (2.0 * math.pi)
    if theta <= _PEAK_BAND or theta >= 2.0 * math.pi - _PEAK_BAND:
        return "peak", fraction
    if abs(theta - math.pi) <= _PEAK_BAND:
        return "trough", fraction
    if theta < math.pi:
        return "falling", fraction
    return "rising", fraction


def spectral_modes(
    residual: FloatArray,
    *,
    top_k: int = 5,
    min_period: int = _MIN_PERIOD_DAYS,
    max_period: float | None = None,
) -> list[dict[str, Any]]:
    """Top-``k`` Fourier modes of a detrended series, ranked by power.

    DC is dropped, as are periods shorter than ``min_period`` days (measurement
    noise) or longer than ``n / 2`` (a single cycle over the sample, which the
    linear detrend has already partly absorbed).
    """
    values = np.asarray(residual, dtype=np.float64)
    n = int(values.size)
    if n < 2 * max(min_period, 2):
        return []
    limit = float(n) / 2.0 if max_period is None else float(max_period)
    spectrum = np.fft.rfft(values)
    power = np.abs(spectrum) ** 2
    total_power = float(np.sum(power[1:]))
    modes: list[dict[str, Any]] = []
    for k in range(1, spectrum.size):
        period = n / k
        if period < min_period or period > limit:
            continue
        amplitude = 2.0 * float(np.abs(spectrum[k])) / n
        phase = float(np.angle(spectrum[k]))
        # value at sample index t is amplitude * cos(2*pi*k*t/n + phase)
        theta_now = 2.0 * math.pi * k * (n - 1) / n + phase
        label, fraction = cycle_position(theta_now)
        modes.append(
            {
                "index": int(k),
                "period_days": float(period),
                "amplitude": amplitude,
                "phase_rad": phase,
                "power": float(power[k]),
                "power_share": float(power[k] / total_power) if total_power > 0 else 0.0,
                "cycle_position": label,
                "phase_fraction": float(fraction),
                "current_phase_rad": float(theta_now % (2.0 * math.pi)),
            }
        )
    modes.sort(key=lambda item: -float(item["power"]))
    return modes[:top_k]


def reconstruct(modes: Sequence[Mapping[str, Any]], n: int, offsets: FloatArray) -> FloatArray:
    """Evaluate the sum of ``modes`` at sample positions ``offsets``.

    ``n`` is the sample length the modes were estimated on; ``offsets`` may run
    past ``n - 1`` to extrapolate.
    """
    out = np.zeros(np.asarray(offsets, dtype=np.float64).shape, dtype=np.float64)
    positions = np.asarray(offsets, dtype=np.float64)
    for mode in modes:
        k = float(mode["index"])
        amplitude = float(mode["amplitude"])
        phase = float(mode["phase_rad"])
        out += amplitude * np.cos(2.0 * math.pi * k * positions / n + phase)
    return out


def _consistency(
    residual: FloatArray, *, top_k: int, min_period: int, holdout: int = 60
) -> dict[str, Any]:
    """Hold-out check: fit on all but the last ``holdout`` days and score them."""
    n = int(residual.size)
    result: dict[str, Any] = {
        "recent_fit_error": None,
        "historical_fit_error": None,
        "z": None,
        "likelihood_label": "unknown",
        "holdout_days": holdout,
        "reason": None,
    }
    if n < holdout * 4:
        result["reason"] = f"need at least {holdout * 4} observations, got {n}"
        return result

    train = residual[:-holdout]
    modes = spectral_modes(train, top_k=top_k, min_period=min_period)
    if not modes:
        result["reason"] = "no modes survived the period filter on the training slice"
        return result

    train_n = int(train.size)
    forward_positions = np.arange(train_n, train_n + holdout, dtype=np.float64)
    forward = reconstruct(modes, train_n, forward_positions)
    recent_error = float(np.sqrt(np.mean((residual[-holdout:] - forward) ** 2)))

    # The reference distribution has to be built the same way `recent_error` is:
    # by refitting on a prefix and *extrapolating* forward. Comparing an
    # out-of-sample forecast error against in-sample residual blocks — which are
    # systematically smaller — inflated the z by roughly an order of magnitude and
    # halved every reported projection confidence through `consistency_factor`.
    block_errors: list[float] = []
    step = max(holdout // 2, 1)
    for end in range(4 * holdout, n - holdout + 1, step):
        prefix_modes = spectral_modes(residual[:end], top_k=top_k, min_period=min_period)
        if not prefix_modes:
            continue
        positions = np.arange(end, end + holdout, dtype=np.float64)
        prediction = reconstruct(prefix_modes, end, positions)
        actual = residual[end : end + holdout]
        if actual.size != holdout:
            continue
        block_errors.append(float(np.sqrt(np.mean((actual - prediction) ** 2))))
    if len(block_errors) < 4:
        result["recent_fit_error"] = recent_error
        result["reason"] = "not enough historical blocks to build a reference distribution"
        return result

    reference = np.asarray(block_errors, dtype=np.float64)
    mean = float(np.mean(reference))
    std = float(np.std(reference, ddof=1))
    z = float((recent_error - mean) / std) if std > 0 else 0.0
    if z <= 1.0:
        label = "consistent"
    elif z <= 2.5:
        label = "drifting"
    else:
        label = "broken"
    result.update(
        {
            "recent_fit_error": recent_error,
            "historical_fit_error": mean,
            "historical_fit_error_std": std,
            "z": z,
            "likelihood_label": label,
            "n_reference_blocks": int(reference.size),
            "reference_method": "walk_forward_refit_and_extrapolate",
            "reference_note": (
                "Each reference block refits the modes on residual[:end] and scores a "
                f"{holdout}-step extrapolation against residual[end:end+{holdout}], so the "
                "reference is measured exactly the way recent_fit_error is."
            ),
        }
    )
    return result


def build_spectral(
    close: pd.Series,
    *,
    top_k: int = 5,
    min_period: int = _MIN_PERIOD_DAYS,
    horizons: Mapping[str, int] | None = None,
    holdout: int = 60,
) -> dict[str, Any]:
    """Build the packet's ``spectral`` section."""
    horizon_map = dict(SPECTRAL_HORIZONS if horizons is None else horizons)
    section: dict[str, Any] = {
        "detrend": "log_price_linear",
        "modes": [],
        "reconstruction_r2": None,
        "projection": {
            label: {"expected_return": None, "confidence": None} for label in horizon_map
        },
        "consistency": {
            "recent_fit_error": None,
            "historical_fit_error": None,
            "z": None,
            "likelihood_label": "unknown",
        },
        "trend": None,
        "robust_trend": None,
        "cycle_damping": None,
        "cycle_extrapolation_limit_days": None,
        "projection_method": None,
        "error": None,
    }
    try:
        residual, trend = detrend_log_price(close)
    except ValueError as exc:
        section["error"] = str(exc)
        return section

    section["trend"] = trend
    n = int(residual.size)
    section["n_observations"] = n
    modes = spectral_modes(residual, top_k=top_k, min_period=min_period)
    section["modes"] = [dict(mode) for mode in modes]
    if not modes:
        section["error"] = "no spectral modes survived the period filter"
        return section

    fitted = reconstruct(modes, n, np.arange(n, dtype=np.float64))
    total = float(np.sum((residual - residual.mean()) ** 2))
    r2 = float(1.0 - np.sum((residual - fitted) ** 2) / total) if total > 0 else 0.0
    section["reconstruction_r2"] = r2
    section["cycle_share_of_variance"] = float(max(min(r2, 1.0), 0.0))

    consistency = _consistency(residual, top_k=top_k, min_period=min_period, holdout=holdout)
    section["consistency"] = consistency

    dominant_period = float(modes[0]["period_days"])
    z = consistency.get("z")
    consistency_factor = 1.0
    if isinstance(z, (int, float)) and math.isfinite(float(z)):
        consistency_factor = float(min(max(1.0 - abs(float(z)) / 4.0, 0.1), 1.0))

    # The projection's drift is NOT the whole-sample OLS slope that produced the
    # residual: over ten years that slope is set by the sample's biggest
    # re-rating and extrapolating it forward forecasts a decade of multiple
    # expansion as permanent drift. `trend` still reports the OLS fit (the
    # residual is defined by it); the forecast uses the robust recent slope.
    robust = robust_trend_slope(close)
    section["robust_trend"] = robust
    slope = float(robust["slope_per_day"])

    cycle_damping = float(min(max(r2, 0.0), 1.0))
    cycle_limit_days = float(max(dominant_period * _CYCLE_TRUNCATION_FRACTION, 1.0))
    section["cycle_extrapolation_limit_days"] = cycle_limit_days
    section["cycle_damping"] = cycle_damping
    section["projection_method"] = (
        "robust recent trend (median daily log change over the last "
        f"{robust.get('lookback_days')} sessions, shrunk "
        f"{robust.get('shrink_to_zero', 0.5):.0%} toward zero) plus the top modes' cycle "
        f"change damped by reconstruction_r2 ({cycle_damping:.3f}) and truncated past "
        f"{cycle_limit_days:.0f} days (a quarter of the dominant period)"
    )

    current_cycle = float(fitted[-1])
    projection: dict[str, Any] = {}
    for label, days in horizon_map.items():
        # Past a quarter of the dominant period the mode's phase is no longer
        # identified well enough to extrapolate, so the cycle term is held at
        # its value there rather than allowed to keep swinging.
        cycle_days = float(min(float(days), cycle_limit_days))
        truncated = bool(float(days) > cycle_limit_days)
        future_cycle = float(
            reconstruct(modes, n, np.array([n - 1 + cycle_days], dtype=np.float64))[0]
        )
        cycle_delta = cycle_damping * (future_cycle - current_cycle)
        delta_log = slope * days + cycle_delta
        expected_return = float(math.expm1(delta_log))
        damping = 1.0 / (1.0 + days / max(dominant_period, 1.0))
        confidence = float(min(max(r2, 0.0), 1.0) * damping * consistency_factor)
        if truncated:
            # Strictly decreasing in `days` and always below 0.3, so a horizon
            # beyond the truncation point cannot carry weight in the mixture.
            confidence = float(
                min(confidence, _TRUNCATED_CONFIDENCE_CEILING) * (cycle_limit_days / float(days))
            )
        projection[label] = {
            "expected_return": expected_return,
            "confidence": confidence,
            "horizon_days": days,
            "trend_component": float(math.expm1(slope * days)),
            "cycle_component": float(math.expm1(cycle_delta)),
            "cycle_extrapolation_days": cycle_days,
            "cycle_truncated": truncated,
            "cycle_damping": cycle_damping,
        }
    section["projection"] = projection
    section["dominant_period_days"] = dominant_period
    section["current_cycle_position"] = modes[0]["cycle_position"]
    section["current_phase_fraction"] = modes[0]["phase_fraction"]
    return section
