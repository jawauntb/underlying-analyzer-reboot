"""Scenario construction: component forecasts -> weighted mixture -> cases.

Each analytical section of the packet produces an opinion about the ticker's
forward return: seasonality knows what this calendar month usually does, the HMM
knows what the current regime pays, the factor model knows what the exposures
imply, the spectral fit knows where in its cycle the series sits, the
fundamentals stage and the macro backdrop supply tilts. Every opinion is a
Gaussian ``(mu_h, sigma_h)`` per horizon.

Those Gaussians are combined into a **mixture**, not averaged: the mixture keeps
the disagreement between components as spread and skew instead of hiding it. The
mixture weights come from a walk-forward backtest that fits on everything before
the last twelve months and scores on the last twelve months, shrunk toward a
prior so a single unlucky holdout cannot hand one component the whole weight.

The mixture is built on **log returns**, not simple ones. Each component's
shrunk ``(mean, sigma)`` is moment-matched onto a lognormal by
:func:`to_log_space` (``s^2 = log(1 + (sigma/(1+mu))^2)``,
``m = log1p(mu) - s^2/2``, which reproduces both moments exactly); the mixture
moments, the case cuts, the probabilities and the truncated conditional means
are all computed on ``log(1 + return)``; and every published figure is converted
back with ``expm1`` for returns and ``exp`` for prices. A Gaussian on the simple
return has no floor, which is how a name with 80% annualised volatility printed
a twelve-month bear case of -176%. In log space a return is bounded below by
-100% and the price percentiles are lognormal-consistent — strictly positive,
monotone, and ``price_p50 = spot * exp(median_log)``. ``scenarios.return_space``
records this, ``distribution[h]`` keeps ``mean``/``std``/``skew``/``kurtosis`` in
simple-return terms (closed-form moments of the lognormal mixture) and adds
``*_log`` companions, and ``mixture_parts`` holds log-space triples
(``mixture_parts_space``).

The mixture is then cut at ``mean +/- 0.5 sigma`` (in log space) into bear /
neutral / bull cases, each with its own conditional percentiles and price
levels, and the 6-month distribution supplies the entry band.

Calibration: shrinkage toward the market, then a plausibility clamp
-------------------------------------------------------------------

Every component above is, at bottom, an extrapolation of something the ticker
already did. Left alone they compound: NVDA's bull-regime daily mean is
+0.28%/day, which is +100% a year if you simply keep multiplying it, and the
spectral fit's decade-long trend is no better behaved. Before the mixture sees
them, each component's expected return at each horizon is shrunk toward a
**market prior** and then clipped to what the name has actually done.

**1. The prior.** ``prior(h)`` is SPY's long-run drift compounded to the
horizon: ``expm1(mean(diff(log SPY)) * h)``, taken from the SPY series the
packet already loaded. Where no market series is available a documented
constant (:data:`DEFAULT_MARKET_DRIFT_ANNUAL`) stands in and ``prior.source``
says so.

**2. The shrinkage weight.** For component ``c`` at horizon ``h``::

    confidence(c, h) = clip(evidence(c, h) * skill(c) * horizon_factor(h),
                            0, MAX_COMPONENT_CONFIDENCE)
    shrink_weight(c, h) = 1 - confidence(c, h)
    expected_return(c, h) = confidence(c, h) * raw(c, h)
                          + shrink_weight(c, h) * prior(h)

with three evidence terms, each of which is a count or a measured score, never
a taste:

* ``evidence(c, h)`` — the component's own published ``confidence``, which is
  already evidence-based per component: seasonality is ``min(n_years / 20, 1)``
  (observation count), the regime block is the HMM's posterior switch
  confidence, spectral is ``reconstruction_r2 x horizon damping x consistency``
  per horizon, and factors/fundamentals/macro carry fixed conservative values
  because their forward skill is not measured. Components that publish a
  per-horizon confidence (``confidence_by_horizon``) are read per horizon.
* ``skill(c)`` — the walk-forward out-of-sample R-squared from
  :func:`walk_forward_weights`, mapped onto ``[SKILL_FLOOR, 1]`` by
  ``clip(0.5 + 0.5 * clip(skill / SKILL_SCALE, -1, 1), SKILL_FLOOR, 1)``. A
  component that was not scored gets the neutral ``SKILL_UNMEASURED = 0.5``, so
  "unmeasured" costs half the confidence rather than nothing.
* ``horizon_factor(h)`` — the classic ``n / (n + k)`` shrinkage on the effective
  sample size. A horizon-``h`` forecast has only ``n_observations / h``
  *non-overlapping* blocks of evidence behind it, so
  ``horizon_factor(h) = blocks / (blocks + PRIOR_BLOCKS)`` with
  ``PRIOR_BLOCKS = 10``. Ten years of daily closes gives ~0.92 at one month and
  ~0.50 at twelve: the further out the claim, the more of it is prior.

**3. The plausibility clamp.** The shrunk value is then clipped to the ticker's
own empirical ``[p5, p95]`` of historical rolling ``h``-day returns, computed
from the loaded close series and published as ``clamp_bounds``. Nothing is
allowed to forecast a move the name has never made. The clamp is a backstop,
not the mechanism — for a high-beta name its bounds are wide, and it is the
shrinkage above that does the work.

Both the raw and the shrunk numbers stay in the packet: each component carries
``shrinkage`` with ``raw_expected_return``, ``prior``, ``shrink_weight``,
``expected_return`` and ``clamp_bounds``, so a reader can always see how far the
calibration moved the component and why.

Every function here is pure: dicts and pandas objects in, JSON-serialisable
dicts out.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

__all__ = [
    "DEFAULT_MARKET_DRIFT_ANNUAL",
    "HORIZONS",
    "MAX_COMPONENT_CONFIDENCE",
    "PRIOR_BLOCKS",
    "PRIOR_WEIGHTS",
    "SKILL_FLOOR",
    "SKILL_SCALE",
    "SKILL_UNMEASURED",
    "build_scenarios",
    "component_agreement",
    "component_forecasts",
    "default_prediction_history",
    "empirical_return_bounds",
    "entry_zone",
    "make_weight_fn",
    "market_prior",
    "mix",
    "shrink_components",
    "signal_prediction_history",
    "timing_label",
    "to_log_space",
    "truncated_mixture_mean",
    "walk_forward_weights",
    "watch_signals",
]

#: Horizon label -> trading days.
HORIZONS: dict[str, int] = {"1m": 21, "2m": 42, "3m": 63, "6m": 126, "12m": 252, "18m": 378}

#: Prior mixture weights, used before (and shrunk toward after) the backtest.
PRIOR_WEIGHTS: dict[str, float] = {
    "seasonality": 0.15,
    "regime": 0.25,
    "factors": 0.20,
    "spectral": 0.15,
    "fundamentals": 0.15,
    "macro": 0.10,
}

_TRADING_DAYS = 252.0
#: Confidence reported for the factors component. Deliberately a constant: the
#: component's forward skill is not backtested, and the fitted window's R-squared
#: is an in-sample statistic that must not be read as one.
_FACTOR_UNMEASURED_CONFIDENCE = 0.20
_CASE_CUT_SIGMA = 0.5
_DEFAULT_BASE_VOL_ANNUAL = 0.25
_PRIOR_STRENGTH = 0.5

#: Annualised market drift used as the shrinkage prior when no market series is
#: available. A stated assumption, flagged as such in ``prior.source``.
DEFAULT_MARKET_DRIFT_ANNUAL = 0.08
#: Pseudo-observations in the ``blocks / (blocks + k)`` horizon shrinkage.
PRIOR_BLOCKS = 10.0
#: Out-of-sample R-squared that earns a component full credit for its skill.
SKILL_SCALE = 0.05
#: Floor on the walk-forward skill factor: even a beaten component keeps a
#: quarter of its confidence rather than being silently deleted.
SKILL_FLOOR = 0.25
#: Skill factor for a component the walk-forward backtest could not score.
SKILL_UNMEASURED = 0.5
#: No component is ever trusted completely; some prior always survives.
MAX_COMPONENT_CONFIDENCE = 0.90
#: Quantiles of the ticker's own rolling horizon returns used as the clamp.
CLAMP_QUANTILES = (0.05, 0.95)

#: Annualised drift attributed to each fundamentals stage label.
_STAGE_TILT: dict[str, float] = {
    "turnaround": 0.06,
    "growing": 0.04,
    "stable": 0.0,
    "peaking": -0.03,
    "declining": -0.08,
}


# --------------------------------------------------------------------------
# Small numeric helpers
# --------------------------------------------------------------------------


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    return 0.5 * (1.0 + math.erf((x - mu) / (sigma * math.sqrt(2.0))))


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _horizon_map(horizons: Mapping[str, int] | None) -> dict[str, int]:
    return dict(HORIZONS if horizons is None else horizons)


def _compound(daily: float, days: int) -> float:
    return float(math.expm1(math.log1p(daily) * days)) if daily > -1.0 else -1.0


def to_log_space(mu: float, sigma: float) -> tuple[float, float] | None:
    """Moment-match a simple-return forecast onto ``log(1 + X) ~ N(m, s^2)``.

    A component publishes a mean and a standard deviation of the *simple*
    return. Treating that pair as a Gaussian on the simple return is what let a
    high-volatility name print a bear case below -100%: nothing in a normal
    distribution knows a share cannot fall by more than its price. The same two
    moments are instead matched onto a lognormal::

        s^2 = log(1 + (sigma / (1 + mu))^2)
        m   = log1p(mu) - s^2 / 2

    which reproduces ``E[X] = mu`` and ``sd[X] = sigma`` exactly while bounding
    the return below by -100% and keeping prices strictly positive. Returns
    ``None`` when the forecast has no usable lognormal image (a mean at or below
    -100%, or a non-positive spread).

    ``s`` is *derived* rather than assumed to be the published ``sigma``. The
    components do not agree on which space their spread was measured in — the
    volatility-derived fallback is a log-return sd, seasonality's comes from
    p10/p90 of simple forward returns, the regime block's from per-regime daily
    simple-return variance — so reading every ``sigma`` as already-log would
    silently inflate the dispersion of the ones that are not. Matching both
    moments is the conservative reading: the change of space alters the
    distribution's *shape* (bounded below, right-skewed) and nothing else.
    """
    growth = 1.0 + float(mu)
    if not math.isfinite(growth) or growth <= 1e-9:
        return None
    if not math.isfinite(sigma) or sigma <= 0:
        return None
    variance = math.log1p((float(sigma) / growth) ** 2)
    if not math.isfinite(variance) or variance <= 0:
        return None
    return float(math.log(growth) - 0.5 * variance), float(math.sqrt(variance))


def _lognormal_mixture_moments(
    parts: Sequence[tuple[float, float, float]],
) -> dict[str, float | None]:
    """Simple-return moments of a mixture of lognormals, in closed form.

    ``E[(1+X)^k] = sum(w * exp(k*m + k^2*s^2/2))`` gives the raw moments; the
    central moments of ``X`` follow, so ``mean``/``std``/``skew``/``kurtosis``
    keep meaning exactly what they meant before this module moved to log space.
    """
    raw: list[float] = []
    for order in (1, 2, 3, 4):
        total = 0.0
        for weight, mu, sigma in parts:
            total += weight * math.exp(order * mu + 0.5 * (order**2) * sigma**2)
        raw.append(float(total))
    first, second, third, fourth = raw
    mean = first - 1.0
    variance = max(second - first**2, 0.0)
    std = math.sqrt(variance)
    # Central moments of Y = 1 + X about E[Y] = first.
    m3 = third - 3.0 * first * second + 2.0 * first**3
    m4 = fourth - 4.0 * first * third + 6.0 * (first**2) * second - 3.0 * first**4
    return {
        "mean": float(mean),
        "std": float(std),
        "skew": float(m3 / std**3) if std > 0 else None,
        "kurtosis": float(m4 / std**4) if std > 0 else None,
        "excess_kurtosis": float(m4 / std**4 - 3.0) if std > 0 else None,
    }


# --------------------------------------------------------------------------
# Component forecasts
# --------------------------------------------------------------------------


def _empty_component(name: str, reason: str, horizons: Mapping[str, int]) -> dict[str, Any]:
    return {
        "component": name,
        "available": False,
        "reason": reason,
        "confidence": 0.0,
        "basis": None,
        "expected_return": dict.fromkeys(horizons),
        "sigma": dict.fromkeys(horizons),
    }


def _seasonality_component(
    seasonality: Mapping[str, Any] | None,
    horizons: Mapping[str, int],
    base_sigma: Mapping[str, float],
) -> dict[str, Any]:
    if not seasonality:
        return _empty_component("seasonality", "no seasonality section", horizons)
    ticker_stats = seasonality.get("ticker") if isinstance(seasonality, Mapping) else None
    forward = (ticker_stats or {}).get("forward") if isinstance(ticker_stats, Mapping) else None
    if not isinstance(forward, Mapping) or not forward:
        forward = seasonality.get("forward") if isinstance(seasonality, Mapping) else None
    if not isinstance(forward, Mapping) or not forward:
        return _empty_component("seasonality", "no forward seasonality statistics", horizons)

    expected: dict[str, float | None] = {}
    sigma: dict[str, float | None] = {}
    per_horizon: dict[str, float] = {}
    counts: list[int] = []
    for label in horizons:
        entry = forward.get(label)
        if not isinstance(entry, Mapping):
            expected[label] = None
            sigma[label] = None
            continue
        mean = _finite(entry.get("mean"))
        expected[label] = mean
        p10 = _finite(entry.get("p10"))
        p90 = _finite(entry.get("p90"))
        if p10 is not None and p90 is not None and p90 > p10:
            sigma[label] = float((p90 - p10) / (2.0 * 1.2815515655446004))
        else:
            sigma[label] = base_sigma.get(label)
        count = entry.get("n")
        if isinstance(count, (int, float)) and count > 0:
            counts.append(int(count))
            # Each horizon is backed by its own number of observed years.
            per_horizon[label] = float(min(max(int(count) / 20.0, 0.0), 1.0))
    available = any(value is not None for value in expected.values())
    sample = min(counts) if counts else 0
    confidence = float(min(max(sample / 20.0, 0.0), 1.0)) if sample else 0.2
    return {
        "component": "seasonality",
        "available": available,
        "reason": None if available else "forward seasonality had no usable means",
        "confidence": confidence,
        "confidence_by_horizon": per_horizon,
        "confidence_basis": "n observed years for this calendar month, capped at 20",
        "basis": "mean forward return conditional on the current calendar month",
        "expected_return": expected,
        "sigma": sigma,
        "n_years": sample,
    }


def _regime_component(
    regimes: Mapping[str, Any] | None, horizons: Mapping[str, int], base_sigma: Mapping[str, float]
) -> dict[str, Any]:
    if not regimes or regimes.get("error"):
        reason = (regimes or {}).get("error") or "no regimes section"
        return _empty_component("regime", str(reason), horizons)
    current = regimes.get("current")
    by_regime = regimes.get("ticker_by_regime")
    states = regimes.get("states") or []
    transition = regimes.get("transition_matrix") or []
    if not isinstance(current, Mapping) or not isinstance(by_regime, Mapping) or not transition:
        return _empty_component(
            "regime", "regimes section is missing current/ticker stats", horizons
        )

    labels = [str(state.get("label")) for state in states]
    posterior = np.asarray(current.get("posterior") or [], dtype=np.float64)
    if posterior.size != len(labels) or not labels:
        return _empty_component("regime", "posterior does not match the state list", horizons)
    matrix = np.asarray(transition, dtype=np.float64)
    if matrix.shape != (len(labels), len(labels)):
        return _empty_component("regime", "transition matrix shape mismatch", horizons)

    means = np.zeros(len(labels), dtype=np.float64)
    variances = np.zeros(len(labels), dtype=np.float64)
    usable = 0
    for index, label in enumerate(labels):
        stats = by_regime.get(label)
        if not isinstance(stats, Mapping):
            continue
        mean = _finite(stats.get("mean_daily"))
        std = _finite(stats.get("std_daily"))
        if mean is None or std is None:
            continue
        means[index] = mean
        variances[index] = std**2
        usable += 1
    if usable == 0:
        return _empty_component("regime", "no regime carried usable ticker statistics", horizons)

    max_days = max(horizons.values())
    distribution = posterior / max(float(posterior.sum()), 1e-12)
    cumulative_mean = 0.0
    cumulative_var = 0.0
    per_day: list[tuple[float, float]] = []
    for _ in range(max_days):
        cumulative_mean += float(distribution @ means)
        cumulative_var += float(distribution @ variances)
        per_day.append((cumulative_mean, cumulative_var))
        distribution = distribution @ matrix

    expected: dict[str, float | None] = {}
    sigma: dict[str, float | None] = {}
    for label, days in horizons.items():
        mean_sum, var_sum = per_day[days - 1]
        expected[label] = float(math.expm1(mean_sum))
        sigma[label] = float(math.sqrt(var_sum)) if var_sum > 0 else base_sigma.get(label)
    confidence = float(min(max(_finite(current.get("switch_confidence")) or 0.5, 0.05), 1.0))
    return {
        "component": "regime",
        "available": True,
        "reason": None,
        "confidence": confidence,
        "basis": (
            f"HMM state distribution propagated through the transition matrix from the "
            f"current '{current.get('label')}' posterior, priced with the ticker's "
            f"per-regime daily statistics"
        ),
        "expected_return": expected,
        "sigma": sigma,
        "current_label": current.get("label"),
    }


def _factors_component(
    factors: Mapping[str, Any] | None,
    horizons: Mapping[str, int],
    base_sigma: Mapping[str, float],
    factor_premia: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    if not factors or factors.get("error"):
        return _empty_component(
            "factors",
            str((factors or {}).get("error") or "no factors section"),
            horizons,
        )
    windows = factors.get("windows")
    if not isinstance(windows, Mapping):
        return _empty_component("factors", "no factor windows", horizons)

    chosen: Mapping[str, Any] | None = None
    chosen_label: str | None = None
    for label in ("5y", "10y", "3y", "1y"):
        entry = windows.get(label)
        if isinstance(entry, Mapping) and entry.get("error") is None and entry.get("betas"):
            chosen = entry
            chosen_label = label
            break
    if chosen is None:
        return _empty_component(
            "factors", "no factor window produced a usable regression", horizons
        )

    # Pricing the exposures with the *fitted window's own* factor means is an OLS
    # identity: with an intercept, alpha + sum(beta_i * xbar_i) == ybar exactly, so
    # the "factor-implied" return would be nothing but the ticker's trailing mean
    # excess return over that window. Premia must therefore come from outside the
    # fitted window - an explicit assumption, or the full published factor history.
    premia: dict[str, float] = {}
    premia_source = "caller_supplied"
    if factor_premia:
        premia = {
            str(key): float(value)
            for key, value in factor_premia.items()
            if _finite(value) is not None
        }
    if not premia:
        block = factors.get("premia")
        candidate = block.get("daily") if isinstance(block, Mapping) else None
        if isinstance(candidate, Mapping):
            premia = {
                str(key): float(value)
                for key, value in candidate.items()
                if _finite(value) is not None
            }
            premia_source = str((block or {}).get("source") or "full_sample_factor_means")
    if not premia:
        return _empty_component(
            "factors",
            "no out-of-window factor premia available to price the exposures "
            "(the fitted window's own means would make the forecast an OLS identity "
            "for the ticker's trailing mean)",
            horizons,
        )

    betas = {
        str(key): (_finite(value) or 0.0) for key, value in (chosen.get("betas") or {}).items()
    }
    # Alpha is deliberately excluded from the forecast: a five-year in-sample alpha
    # of tens of percent a year is a description of the past, not a prediction. It
    # is reported beside the forecast as `alpha_component` instead.
    alpha_daily = _finite(chosen.get("alpha_daily")) or 0.0
    daily = sum(betas.get(name, 0.0) * value for name, value in premia.items())
    residual_daily = _finite(chosen.get("residual_vol_daily"))
    r2 = _finite(chosen.get("r2"))
    total_daily_vol: float | None = residual_daily
    if residual_daily is not None and r2 is not None and r2 < 0.98:
        # Total return variance implied by the residual variance and R-squared.
        total_daily_vol = residual_daily / math.sqrt(max(1.0 - r2, 0.02))

    expected: dict[str, float | None] = {}
    sigma: dict[str, float | None] = {}
    for label, days in horizons.items():
        expected[label] = _compound(daily, days)
        sigma[label] = (
            float(total_daily_vol * math.sqrt(days)) if total_daily_vol else base_sigma.get(label)
        )
    # Confidence is NOT the in-sample R-squared: a regression that explains the
    # past well says nothing about the forward skill of a premia dot product, and
    # reporting R-squared here handed this component the highest confidence in the
    # packet. The engine does not backtest this forecast, so it says so.
    confidence = _FACTOR_UNMEASURED_CONFIDENCE
    return {
        "component": "factors",
        "available": True,
        "reason": None,
        "confidence": confidence,
        "confidence_basis": (
            "not measured out of sample; the factor forecast is a betas-times-premia "
            "dot product whose forward skill the engine does not backtest, so a fixed "
            "conservative confidence is reported instead of the in-sample R-squared"
        ),
        "in_sample_r2": r2,
        "basis": (
            f"{chosen_label}-window betas ({factors.get('model')}) dotted with "
            f"{premia_source} factor premia; the in-sample alpha is excluded"
        ),
        "expected_return": expected,
        "sigma": sigma,
        "window_used": chosen_label,
        "premia_source": premia_source,
        "premia_used": dict(premia),
        "beta_premium_daily_return": daily,
        # Reported, never added to the forecast.
        "alpha_component": {
            "alpha_daily": alpha_daily,
            "alpha_annual": _finite(chosen.get("alpha_annual")),
            "note": (
                "In-sample intercept over the fitted window. Excluded from "
                "expected_return: it is a description of realised idiosyncratic "
                "return, not a forecast."
            ),
        },
    }


def _spectral_component(
    spectral: Mapping[str, Any] | None, horizons: Mapping[str, int], base_sigma: Mapping[str, float]
) -> dict[str, Any]:
    if not spectral or spectral.get("error"):
        return _empty_component(
            "spectral",
            str((spectral or {}).get("error") or "no spectral section"),
            horizons,
        )
    projection = spectral.get("projection")
    if not isinstance(projection, Mapping):
        return _empty_component("spectral", "no spectral projection", horizons)

    expected: dict[str, float | None] = {}
    sigma: dict[str, float | None] = {}
    per_horizon: dict[str, float] = {}
    confidences: list[float] = []
    for label in horizons:
        entry = projection.get(label)
        if not isinstance(entry, Mapping):
            expected[label] = None
            sigma[label] = None
            continue
        expected[label] = _finite(entry.get("expected_return"))
        confidence = _finite(entry.get("confidence")) or 0.05
        confidences.append(confidence)
        # The projection's confidence is already per horizon (R-squared times a
        # horizon damping times the walk-forward consistency factor, floored
        # hard once the cycle extrapolation is truncated), so the shrinkage
        # reads it per horizon rather than flattening it to a mean.
        per_horizon[label] = confidence
        base = base_sigma.get(label)
        if base is None:
            sigma[label] = None
        else:
            widened = float(base) / math.sqrt(max(confidence, 0.05))
            sigma[label] = float(min(widened, float(base) * 4.0))
    available = any(value is not None for value in expected.values())
    return {
        "component": "spectral",
        "available": available,
        "reason": None if available else "projection carried no expected returns",
        "confidence": float(np.mean(confidences)) if confidences else 0.0,
        "confidence_by_horizon": per_horizon,
        "confidence_basis": (
            "reconstruction R-squared x horizon damping x hold-out consistency, "
            "forced below 0.3 past a quarter of the dominant period"
        ),
        "basis": (
            "robust recent trend plus the top Fourier modes, damped by the "
            "reconstruction R-squared and truncated past a quarter of the dominant period"
        ),
        "expected_return": expected,
        "sigma": sigma,
    }


def _fundamentals_component(
    fundamentals: Mapping[str, Any] | None,
    horizons: Mapping[str, int],
    base_sigma: Mapping[str, float],
) -> dict[str, Any]:
    if not fundamentals:
        return _empty_component("fundamentals", "no fundamentals section", horizons)
    stage = fundamentals.get("stage")
    label = str((stage or {}).get("label") or "").lower() if isinstance(stage, Mapping) else ""
    tilt = _STAGE_TILT.get(label)
    growth = fundamentals.get("growth") if isinstance(fundamentals.get("growth"), Mapping) else {}
    revenue_yoy = _finite((growth or {}).get("revenue_yoy"))
    if tilt is None and revenue_yoy is None:
        return _empty_component(
            "fundamentals", "no stage label or revenue growth to tilt on", horizons
        )
    annual = (tilt or 0.0) + (
        float(np.clip(revenue_yoy, -0.5, 0.5)) * 0.25 if revenue_yoy is not None else 0.0
    )
    daily = float(math.expm1(math.log1p(annual) / _TRADING_DAYS)) if annual > -1.0 else -0.001
    expected = {label_: _compound(daily, days) for label_, days in horizons.items()}
    sigma = {
        label_: (float(base_sigma[label_] * 1.6) if label_ in base_sigma else None)
        for label_ in horizons
    }
    return {
        "component": "fundamentals",
        "available": True,
        "reason": None,
        "confidence": 0.30,
        "basis": f"stage '{label or 'unknown'}' tilt plus a quarter of trailing revenue growth",
        "expected_return": expected,
        "sigma": sigma,
        "annual_tilt": annual,
    }


def _macro_component(
    macro: Mapping[str, Any] | None, horizons: Mapping[str, int], base_sigma: Mapping[str, float]
) -> dict[str, Any]:
    if not macro:
        return _empty_component("macro", "no macro section", horizons)

    contributions: dict[str, float] = {}

    def _change(node: Any, key: str = "change_3m") -> tuple[float, str] | None:
        """Return ``(change, change_mode)`` for a ``MacroSeries``-shaped node.

        ``change_mode`` is ``"diff"`` (absolute units: index points, percentage
        points) or ``"pct"`` (a decimal fraction); the tilt scaling below depends
        on which one a series uses, so it is read rather than assumed.
        """
        if not isinstance(node, Mapping):
            return None
        value = _finite(node.get(key))
        if value is None:
            return None
        mode = str(node.get("change_mode") or "diff")
        return value, mode

    hy = _change(macro.get("hy_spread"))
    if hy is not None:
        # A widening high-yield spread is a risk-off signal. BAMLH0A0HYM2 is in
        # percentage points and uses diff mode; 1pp of widening -> -5% tilt.
        value = hy[0] if hy[1] == "diff" else hy[0] * 100.0
        contributions["hy_spread"] = float(np.clip(-value * 0.05, -0.08, 0.08))
    vix = _change(macro.get("vix"))
    if vix is not None:
        # VIXCLS is an index level in diff mode; +10 vol points -> -2% tilt.
        value = vix[0] if vix[1] == "diff" else vix[0] * 20.0
        contributions["vix"] = float(np.clip(-value * 0.002, -0.06, 0.06))
    dollar = _change(macro.get("dollar"))
    if dollar is not None:
        # DTWEXBGS uses pct mode; a 5% broad-dollar rally -> -2.5% tilt.
        fraction = dollar[0] if dollar[1] == "pct" else dollar[0] / 100.0
        contributions["dollar"] = float(np.clip(-fraction * 0.5, -0.04, 0.04))
    curve = macro.get("curve_shape")
    if isinstance(curve, Mapping):
        slope = _finite(curve.get("2s10s"))
        if slope is not None:
            contributions["curve"] = float(np.clip(slope * 0.01, -0.05, 0.05))
    if not contributions:
        return _empty_component("macro", "macro section carried no usable changes", horizons)

    annual = float(sum(contributions.values()))
    daily = float(math.expm1(math.log1p(annual) / _TRADING_DAYS)) if annual > -1.0 else -0.001
    expected = {label: _compound(daily, days) for label, days in horizons.items()}
    sigma = {
        label: (float(base_sigma[label] * 2.0) if label in base_sigma else None)
        for label in horizons
    }
    return {
        "component": "macro",
        "available": True,
        "reason": None,
        "confidence": 0.25,
        "basis": "risk-appetite tilt from HY spreads, VIX, the dollar and the 2s10s curve",
        "expected_return": expected,
        "sigma": sigma,
        "annual_tilt": annual,
        "contributions": contributions,
    }


def component_forecasts(
    *,
    seasonality: Mapping[str, Any] | None = None,
    regimes: Mapping[str, Any] | None = None,
    factors: Mapping[str, Any] | None = None,
    spectral: Mapping[str, Any] | None = None,
    fundamentals: Mapping[str, Any] | None = None,
    macro: Mapping[str, Any] | None = None,
    realized_vol_annual: float | None = None,
    factor_premia: Mapping[str, float] | None = None,
    horizons: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Turn packet sections into one Gaussian forecast per component per horizon.

    Every input is optional; a missing or failed section yields an entry with
    ``available: False`` and a ``reason``, never a fabricated number.
    ``realized_vol_annual`` sets the fallback spread used when a component has no
    natural uncertainty of its own (default 25% annualised).
    """
    horizon_map = _horizon_map(horizons)
    annual_vol = (
        realized_vol_annual
        if realized_vol_annual and realized_vol_annual > 0
        else _DEFAULT_BASE_VOL_ANNUAL
    )
    daily_vol = float(annual_vol) / math.sqrt(_TRADING_DAYS)
    base_sigma = {label: float(daily_vol * math.sqrt(days)) for label, days in horizon_map.items()}

    return {
        "seasonality": _seasonality_component(seasonality, horizon_map, base_sigma),
        "regime": _regime_component(regimes, horizon_map, base_sigma),
        "factors": _factors_component(factors, horizon_map, base_sigma, factor_premia),
        "spectral": _spectral_component(spectral, horizon_map, base_sigma),
        "fundamentals": _fundamentals_component(fundamentals, horizon_map, base_sigma),
        "macro": _macro_component(macro, horizon_map, base_sigma),
    }


# --------------------------------------------------------------------------
# Calibration: market prior, plausibility clamp, per-component shrinkage
# --------------------------------------------------------------------------


def market_prior(
    market_close: pd.Series | None,
    horizons: Mapping[str, int] | None = None,
    *,
    symbol: str = "SPY",
    default_annual_drift: float = DEFAULT_MARKET_DRIFT_ANNUAL,
) -> dict[str, Any]:
    """The prior every component is shrunk toward: the market's own long-run drift.

    ``market_close`` is the benchmark series the packet already loaded (SPY).
    The drift is the mean daily log return over the whole loaded series,
    compounded to each horizon. When no usable series is supplied the documented
    constant ``default_annual_drift`` stands in and ``source`` says so, so a
    reader can never mistake the assumption for a measurement.
    """
    horizon_map = _horizon_map(horizons)
    prices: pd.Series | None = None
    if market_close is not None:
        cleaned = pd.to_numeric(pd.Series(market_close), errors="coerce").dropna()
        cleaned = cleaned[cleaned > 0]
        if cleaned.shape[0] > 60:
            prices = cleaned

    if prices is None:
        daily_log = math.log1p(float(default_annual_drift)) / _TRADING_DAYS
        return {
            "source": "assumed_default",
            "symbol": None,
            "note": (
                "no market series was available; the prior is the documented "
                f"{default_annual_drift:.0%} annual equity drift assumption, not a measurement"
            ),
            "daily_log_drift": float(daily_log),
            "annualized_drift": float(default_annual_drift),
            "n_observations": 0,
            "by_horizon": {
                label: float(math.expm1(daily_log * days)) for label, days in horizon_map.items()
            },
        }

    log_returns = np.log(prices.to_numpy(dtype=np.float64))
    steps = np.diff(log_returns)
    steps = steps[np.isfinite(steps)]
    daily_log = float(np.mean(steps)) if steps.size else 0.0
    return {
        "source": f"{symbol}_mean_daily_log_return",
        "symbol": symbol,
        "note": (
            f"mean daily log return of the loaded {symbol} series ({steps.size} observations), "
            "compounded to each horizon"
        ),
        "daily_log_drift": daily_log,
        "annualized_drift": float(math.expm1(daily_log * _TRADING_DAYS)),
        "n_observations": int(steps.size),
        "first_date": _iso_date(prices.index[0]),
        "last_date": _iso_date(prices.index[-1]),
        "by_horizon": {
            label: float(math.expm1(daily_log * days)) for label, days in horizon_map.items()
        },
    }


def _iso_date(stamp: Any) -> str | None:
    try:
        return str(pd.Timestamp(stamp).date())
    except (TypeError, ValueError):
        return None


def empirical_return_bounds(
    close: pd.Series | None,
    horizons: Mapping[str, int] | None = None,
    *,
    quantiles: tuple[float, float] = CLAMP_QUANTILES,
) -> dict[str, Any]:
    """The ticker's own ``[p5, p95]`` of historical rolling ``h``-day returns.

    This is the plausibility clamp: whatever the components extrapolate, the
    published expected return is not allowed outside the band the name has
    actually traded over that holding period. Overlapping windows are used on
    purpose — the point is the observed range, not an inference from it — and
    ``n`` records how many windows the band was measured on.
    """
    horizon_map = _horizon_map(horizons)
    low_q, high_q = float(quantiles[0]), float(quantiles[1])
    out: dict[str, Any] = {}
    prices: pd.Series | None = None
    if close is not None:
        cleaned = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
        cleaned = cleaned[cleaned > 0]
        if cleaned.shape[0] > 2:
            prices = cleaned
    for label, days in horizon_map.items():
        entry: dict[str, Any] = {
            "horizon_days": int(days),
            "low": None,
            "high": None,
            "quantiles": [low_q, high_q],
            "n": 0,
            "reason": None,
        }
        if prices is None or prices.shape[0] <= days + 20:
            entry["reason"] = (
                f"need more than {days + 20} closes to measure rolling {days}-day returns"
            )
            out[label] = entry
            continue
        values = prices.to_numpy(dtype=np.float64)
        rolling = values[days:] / values[:-days] - 1.0
        rolling = rolling[np.isfinite(rolling)]
        if rolling.size < 20:
            entry["reason"] = f"only {rolling.size} rolling {days}-day windows"
            out[label] = entry
            continue
        entry["low"] = float(np.quantile(rolling, low_q))
        entry["high"] = float(np.quantile(rolling, high_q))
        entry["n"] = int(rolling.size)
        out[label] = entry
    return out


def _skill_factor(skill: float | None) -> float:
    """Map a walk-forward out-of-sample R-squared onto ``[SKILL_FLOOR, 1]``."""
    if skill is None or not math.isfinite(float(skill)):
        return float(SKILL_UNMEASURED)
    scaled = float(np.clip(float(skill) / SKILL_SCALE, -1.0, 1.0))
    return float(np.clip(0.5 + 0.5 * scaled, SKILL_FLOOR, 1.0))


def _horizon_factor(days: int, n_observations: int | None) -> float:
    """``blocks / (blocks + PRIOR_BLOCKS)`` on non-overlapping horizon blocks."""
    sample = int(n_observations) if n_observations and n_observations > 0 else int(10 * 252)
    blocks = float(sample) / float(max(int(days), 1))
    return float(blocks / (blocks + PRIOR_BLOCKS))


def shrink_components(
    components: Mapping[str, Mapping[str, Any]],
    *,
    prior: Mapping[str, Any],
    bounds: Mapping[str, Mapping[str, Any]] | None = None,
    weight_evidence: Mapping[str, Any] | None = None,
    n_observations: int | None = None,
    horizons: Mapping[str, int] | None = None,
) -> dict[str, dict[str, Any]]:
    """Shrink each component toward ``prior`` and clamp it to ``bounds``.

    See the module docstring for the exact formula. Returns a **new** mapping;
    the inputs are not mutated. Every component keeps its raw numbers under
    ``shrinkage.raw_expected_return`` so nothing is lost, and ``expected_return``
    — the field the mixture reads — becomes the calibrated value.
    """
    horizon_map = _horizon_map(horizons)
    prior_by_horizon = dict((prior or {}).get("by_horizon") or {})
    evidence_components = dict((weight_evidence or {}).get("components") or {})
    out: dict[str, dict[str, Any]] = {}

    for name, component in components.items():
        block = dict(component)
        raw = dict(block.get("expected_return") or {})
        if not block.get("available"):
            block["shrinkage"] = {
                "applied": False,
                "reason": str(block.get("reason") or "component unavailable"),
            }
            out[name] = block
            continue

        scored = evidence_components.get(name)
        measured_skill = (
            _finite((scored or {}).get("skill")) if isinstance(scored, Mapping) else None
        )
        skill_factor = _skill_factor(measured_skill)
        base_confidence = _finite(block.get("confidence"))
        by_horizon = dict(block.get("confidence_by_horizon") or {})

        shrunk: dict[str, float | None] = {}
        weights: dict[str, float | None] = {}
        confidences: dict[str, float | None] = {}
        horizon_factors: dict[str, float] = {}
        evidence_used: dict[str, float] = {}
        clamped: dict[str, str | None] = {}
        clamp_bounds: dict[str, Any] = {}

        for label, days in horizon_map.items():
            value = _finite(raw.get(label))
            prior_value = _finite(prior_by_horizon.get(label))
            evidence = _finite(by_horizon.get(label))
            if evidence is None:
                evidence = base_confidence if base_confidence is not None else 0.0
            evidence = float(min(max(evidence, 0.0), 1.0))
            factor = _horizon_factor(days, n_observations)
            confidence = float(
                min(max(evidence * skill_factor * factor, 0.0), MAX_COMPONENT_CONFIDENCE)
            )
            evidence_used[label] = evidence
            horizon_factors[label] = factor
            confidences[label] = confidence
            if value is None:
                shrunk[label] = None
                weights[label] = None
                clamped[label] = None
                continue
            if prior_value is None:
                # No prior to shrink toward: leave the component alone rather
                # than inventing a target, and say so.
                weights[label] = 0.0
                calibrated = value
            else:
                weights[label] = float(1.0 - confidence)
                calibrated = confidence * value + (1.0 - confidence) * prior_value

            bound = (bounds or {}).get(label) if bounds else None
            low = _finite((bound or {}).get("low")) if isinstance(bound, Mapping) else None
            high = _finite((bound or {}).get("high")) if isinstance(bound, Mapping) else None
            if isinstance(bound, Mapping):
                clamp_bounds[label] = {"low": low, "high": high, "n": bound.get("n")}
            hit: str | None = None
            if low is not None and calibrated < low:
                calibrated, hit = low, "low"
            elif high is not None and calibrated > high:
                calibrated, hit = high, "high"
            clamped[label] = hit
            shrunk[label] = float(calibrated)

        block["expected_return"] = shrunk
        block["shrinkage"] = {
            "applied": True,
            "method": (
                "expected_return = confidence * raw + (1 - confidence) * prior, then clipped "
                "to the ticker's empirical [p5, p95] of rolling horizon returns; "
                "confidence = evidence x walk_forward_skill_factor x horizon_factor"
            ),
            "raw_expected_return": raw,
            "prior": {
                label: _finite(prior_by_horizon.get(label)) for label in horizon_map
            },
            "prior_source": (prior or {}).get("source"),
            "shrink_weight": weights,
            "confidence": confidences,
            "evidence_confidence": evidence_used,
            "walk_forward_skill": measured_skill,
            "skill_factor": skill_factor,
            "horizon_factor": horizon_factors,
            "clamp_bounds": clamp_bounds,
            "clamped": clamped,
            "expected_return": shrunk,
        }
        out[name] = block
    return out


# --------------------------------------------------------------------------
# Walk-forward weighting
# --------------------------------------------------------------------------


def default_prediction_history(
    close: pd.Series,
    *,
    regime_labels: pd.Series | None = None,
    horizon_days: int = 21,
    min_history: int = 504,
) -> tuple[pd.DataFrame, pd.Series]:
    """Backward-looking prediction history for the walk-forward backtest.

    For each date ``t`` this builds what a component *would have* forecast using
    only information available strictly before ``t``:

    * ``seasonality`` — the expanding mean forward return of every prior year's
      same calendar month.
    * ``regime`` — the expanding mean forward return conditional on the regime
      label in force at ``t``.

    Only forward windows that had already fully realised before ``t`` are used,
    so there is no look-ahead. Components with no cheap backward-looking analogue
    (factors, spectral, fundamentals, macro) are absent from the frame and fall
    back to the prior weight.

    Returns ``(predictions, realized)`` where ``realized`` is the actual forward
    ``horizon_days`` return starting at each date.
    """
    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    prices = prices[prices > 0]
    empty = (pd.DataFrame(), pd.Series(dtype="float64"))
    if prices.shape[0] < min_history + horizon_days:
        return empty
    if not isinstance(prices.index, pd.DatetimeIndex):
        converted = pd.to_datetime(prices.index, errors="coerce")
        prices = prices[converted.notna()]
        prices.index = converted[converted.notna()]
    if prices.shape[0] < min_history + horizon_days:
        return empty

    values = prices.to_numpy(dtype=np.float64)
    n = values.shape[0]
    forward = np.full(n, np.nan, dtype=np.float64)
    forward[: n - horizon_days] = values[horizon_days:] / values[: n - horizon_days] - 1.0
    realized = pd.Series(forward, index=prices.index, name="realized")

    months = np.asarray([int(stamp.month) for stamp in prices.index], dtype=np.int64)
    label_values: np.ndarray | None = None
    if regime_labels is not None:
        aligned = pd.Series(regime_labels).reindex(prices.index).ffill()
        label_values = aligned.to_numpy(dtype=object)

    month_state: dict[int, list[float]] = {}
    regime_state: dict[str, list[float]] = {}
    seasonality_prediction = np.full(n, np.nan, dtype=np.float64)
    regime_prediction = np.full(n, np.nan, dtype=np.float64)

    for index in range(n):
        # Fold in every forward window that finished strictly before `index`.
        settled = index - horizon_days
        if settled >= 0 and math.isfinite(float(forward[settled])):
            bucket = month_state.setdefault(int(months[settled]), [])
            bucket.append(float(forward[settled]))
            if label_values is not None:
                key = label_values[settled]
                if isinstance(key, str):
                    regime_state.setdefault(key, []).append(float(forward[settled]))
        month_history = month_state.get(int(months[index]))
        if month_history and len(month_history) >= 3:
            seasonality_prediction[index] = float(np.mean(month_history))
        if label_values is not None:
            key = label_values[index]
            if isinstance(key, str):
                regime_history = regime_state.get(key)
                if regime_history and len(regime_history) >= 20:
                    regime_prediction[index] = float(np.mean(regime_history))

    columns: dict[str, pd.Series] = {
        "seasonality": pd.Series(seasonality_prediction, index=prices.index)
    }
    if label_values is not None and np.isfinite(regime_prediction).any():
        columns["regime"] = pd.Series(regime_prediction, index=prices.index)
    predictions = pd.DataFrame(columns)
    keep = realized.notna()
    return predictions[keep], realized[keep]



def signal_prediction_history(
    signals: pd.DataFrame,
    close: pd.Series,
    *,
    horizon_days: int = 21,
    min_train: int = 252,
) -> tuple[pd.DataFrame, pd.Series]:
    """Backward-looking forecast history for arbitrary signals.

    For every signal column this replays a univariate expanding-window OLS: at
    each date ``t`` the slope and intercept are estimated from the ``(signal,
    realised forward return)`` pairs that had *fully settled* before ``t`` (so
    the last ``horizon_days`` of pairs are excluded), then applied to the signal
    value at ``t``. The result is what that signal alone would have forecast in
    real time, which is exactly what :func:`walk_forward_weights` needs to score
    it — and what makes :func:`app.prism.eigen.load_bearing_test` an actual
    intervention rather than a no-op.

    Implemented with running sums, so it costs one pass per signal.

    Returns ``(predictions, realized)`` aligned on the signals' index.
    """
    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    prices = prices[prices > 0]
    frame = signals.apply(pd.to_numeric, errors="coerce")
    empty = (pd.DataFrame(), pd.Series(dtype="float64"))
    if prices.shape[0] < min_train + 2 * horizon_days or frame.empty:
        return empty

    values = prices.to_numpy(dtype=np.float64)
    n = values.shape[0]
    forward = np.full(n, np.nan, dtype=np.float64)
    forward[: n - horizon_days] = values[horizon_days:] / values[: n - horizon_days] - 1.0
    realized_full = pd.Series(forward, index=prices.index, name="realized")

    def settled_sum(series: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        """Cumulative sum over pairs whose forward window closed before each date."""
        cumulative = np.concatenate([[0.0], np.cumsum(series)])
        shifted = np.zeros(n, dtype=np.float64)
        if n > horizon_days:
            shifted[horizon_days:] = cumulative[: n - horizon_days]
        return shifted

    aligned = frame.reindex(prices.index).ffill()
    columns: dict[str, pd.Series] = {}
    for column in frame.columns:
        x = aligned[str(column)].to_numpy(dtype=np.float64)
        usable = np.isfinite(x) & np.isfinite(forward)
        xs = np.where(usable, x, 0.0)
        ys = np.where(usable, forward, 0.0)

        count = settled_sum(usable.astype(np.float64))
        sum_x = settled_sum(xs)
        sum_y = settled_sum(ys)
        sum_xx = settled_sum(xs * xs)
        sum_xy = settled_sum(xs * ys)
        denominator = count * sum_xx - sum_x**2
        with np.errstate(divide="ignore", invalid="ignore"):
            slope = np.where(
                denominator > 0, (count * sum_xy - sum_x * sum_y) / denominator, np.nan
            )
            intercept = np.where(
                count > 0, (sum_y - slope * sum_x) / np.maximum(count, 1.0), np.nan
            )
        prediction = intercept + slope * x
        prediction = np.where(count >= min_train, prediction, np.nan)
        columns[str(column)] = pd.Series(prediction, index=prices.index)

    predictions = pd.DataFrame(columns)
    keep = realized_full.notna()
    return predictions[keep], realized_full[keep]


def walk_forward_weights(
    predictions: pd.DataFrame,
    realized: pd.Series,
    *,
    holdout_days: int = 252,
    prior: Mapping[str, float] | None = None,
    prior_strength: float = _PRIOR_STRENGTH,
    components: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Weight components by out-of-sample explanatory power.

    The sample is split at ``holdout_days`` from the end. Each component's skill
    is its out-of-sample ``1 - MSE / MSE_baseline`` against a constant forecast
    equal to the in-sample mean realised return — the standard out-of-sample
    R-squared. Negative skill means the component was worse than the naive
    forecast and contributes no weight.

    Weights are ``prior_strength`` prior plus ``1 - prior_strength`` measured
    skill, so a single 12-month holdout cannot hand one component everything.
    Components with no prediction history keep their prior share and are flagged.
    """
    if prior is not None:
        prior_map = dict(prior)
        names = list(components) if components else list(prior_map)
    else:
        names = list(components) if components else list(PRIOR_WEIGHTS)
        prior_map = {name: float(PRIOR_WEIGHTS.get(name, 0.0)) for name in names}
        if sum(prior_map.values()) <= 0:
            # Arbitrary component names (e.g. the eigen signal set): fall back to
            # a flat prior rather than letting the shrinkage term vanish.
            prior_map = dict.fromkeys(names, 1.0 / max(len(names), 1))
    evidence: dict[str, Any] = {
        "method": "walk_forward_out_of_sample_r2",
        "holdout_days": holdout_days,
        "prior_strength": prior_strength,
        "components": {},
        "reason": None,
    }

    scored: dict[str, float] = {}
    if predictions is not None and not predictions.empty and realized is not None:
        frame = predictions.join(pd.Series(realized).rename("__realized__"), how="inner")
        frame = frame[frame["__realized__"].notna()]
        if frame.shape[0] > holdout_days + 60:
            train = frame.iloc[:-holdout_days]
            test = frame.iloc[-holdout_days:]
            baseline = float(train["__realized__"].mean())
            actual = test["__realized__"].to_numpy(dtype=np.float64)
            denominator = float(np.mean((actual - baseline) ** 2))
            evidence["n_train"] = int(train.shape[0])
            evidence["n_test"] = int(test.shape[0])
            evidence["baseline_forecast"] = baseline
            # The floor has to scale with the holdout: a hard 30 discarded every
            # component whenever the caller asked for a shorter holdout (the eigen
            # load-bearing test uses monthly rows), so nothing was ever scored and
            # every leave-one-out delta collapsed to zero.
            min_test = max(5, min(30, int(round(holdout_days * 0.75))))
            evidence["min_test_observations"] = int(min_test)
            for column in predictions.columns:
                name = str(column)
                subset = test[[str(column), "__realized__"]].dropna()
                if subset.shape[0] < min_test or denominator <= 0:
                    evidence["components"][name] = {
                        "skill": None,
                        "n_test": int(subset.shape[0]),
                        "reason": (
                            f"not enough out-of-sample observations "
                            f"({subset.shape[0]} < {min_test})"
                        ),
                    }
                    continue
                predicted = subset[str(column)].to_numpy(dtype=np.float64)
                truth = subset["__realized__"].to_numpy(dtype=np.float64)
                mse = float(np.mean((truth - predicted) ** 2))
                skill = float(1.0 - mse / denominator)
                correlation = (
                    float(np.corrcoef(predicted, truth)[0, 1])
                    if np.std(predicted) > 0 and np.std(truth) > 0
                    else None
                )
                evidence["components"][name] = {
                    "skill": skill,
                    "mse": mse,
                    "baseline_mse": denominator,
                    "correlation": (
                        correlation
                        if correlation is None or math.isfinite(correlation)
                        else None
                    ),
                    "n_test": int(subset.shape[0]),
                }
                scored[name] = skill
        else:
            evidence["reason"] = (
                f"need more than {holdout_days + 60} aligned observations for a walk-forward "
                f"split, got {0 if predictions is None else frame.shape[0]}"
            )
    else:
        evidence["reason"] = "no prediction history supplied"

    positive = {name: max(value, 0.0) for name, value in scored.items()}
    total_positive = float(sum(positive.values()))
    if total_positive <= 0 and len(scored) >= 2:
        # Nothing beat the naive constant forecast in absolute terms. Rather than
        # discarding the measurement entirely, fall back to a *relative* ranking
        # (each component's skill above the worst one) and say so loudly: this is
        # a preference between components that were all individually unhelpful,
        # and it is still shrunk halfway back to the prior below.
        floor = min(scored.values())
        relative_skill = {name: value - floor for name, value in scored.items()}
        if sum(relative_skill.values()) > 0:
            positive = relative_skill
            total_positive = float(sum(positive.values()))
            evidence["fallback"] = "relative_skill_ranking"
            evidence["fallback_note"] = (
                "no component beat the naive constant forecast out of sample; weights rank "
                "components by skill relative to the worst performer and are shrunk toward "
                "the prior"
            )

    measured: dict[str, float] = {}
    if total_positive > 0:
        share = {name: value / total_positive for name, value in positive.items()}
        prior_unscored = sum(prior_map.get(name, 0.0) for name in names if name not in scored)
        prior_scored = sum(prior_map.get(name, 0.0) for name in names if name in scored)
        scale = prior_scored if prior_scored > 0 else 1.0
        for name in names:
            if name in share:
                measured[name] = share[name] * scale
            else:
                measured[name] = prior_map.get(name, 0.0)
        total_measured = float(sum(measured.values())) or 1.0
        measured = {name: value / total_measured for name, value in measured.items()}
        evidence["measured_share"] = {name: float(value) for name, value in share.items()}
        evidence["prior_only_components"] = [name for name in names if name not in scored]
        evidence["unscored_prior_mass"] = float(prior_unscored)
    else:
        if evidence["reason"] is None:
            # "Nothing beat the baseline" and "nothing could be evaluated" are very
            # different claims; only say the first when something was actually scored.
            evidence["reason"] = (
                "no component beat the naive constant forecast out of sample"
                if scored
                else "no component could be scored out of sample"
            )
        prior_total = float(sum(prior_map.get(name, 0.0) for name in names)) or 1.0
        measured = {name: prior_map.get(name, 0.0) / prior_total for name in names}
    if scored and evidence.get("reason") is None and total_positive > 0 and all(
        value <= 0 for value in scored.values()
    ):
        evidence["reason"] = "no component beat the naive constant forecast out of sample"

    prior_total = float(sum(prior_map.get(name, 0.0) for name in names)) or 1.0
    blended = {
        name: prior_strength * (prior_map.get(name, 0.0) / prior_total)
        + (1.0 - prior_strength) * measured.get(name, 0.0)
        for name in names
    }
    total = float(sum(blended.values())) or 1.0
    weights = {name: float(value / total) for name, value in blended.items()}
    evidence["prior"] = {name: float(prior_map.get(name, 0.0) / prior_total) for name in names}
    return {"weights": weights, "evidence": evidence}


def make_weight_fn(
    predictions: pd.DataFrame,
    realized: pd.Series,
    *,
    holdout_days: int = 252,
    prior: Mapping[str, float] | None = None,
    components: Sequence[str] | None = None,
) -> Callable[[Sequence[str]], dict[str, float]]:
    """Build the callback :func:`app.prism.eigen.load_bearing_test` needs.

    The returned function takes the surviving signal names, drops the rest from
    the prediction history and re-runs :func:`walk_forward_weights`, so the
    leave-one-out delta is a genuine intervention on the weighting rather than a
    sensitivity of a formula.
    """

    def weight_fn(surviving: Sequence[str]) -> dict[str, float]:
        keep = [str(name) for name in surviving if str(name) in predictions.columns]
        subset = predictions[keep] if keep else predictions.iloc[:, :0]
        result = walk_forward_weights(
            subset,
            realized,
            holdout_days=holdout_days,
            prior=prior,
            # The *surviving* names, not the captured full list: passing the full
            # list put a dropped signal's prior weight straight back into the
            # output, so a removed signal never actually left the weight vector
            # and every leave-one-out delta came out at exactly zero.
            components=keep or list(components or []),
        )
        return dict(result["weights"])

    return weight_fn


# --------------------------------------------------------------------------
# Mixture
# --------------------------------------------------------------------------


def _mixture_cdf(x: float, parts: Sequence[tuple[float, float, float]]) -> float:
    return float(sum(weight * _normal_cdf(x, mu, sigma) for weight, mu, sigma in parts))


def _mixture_quantile(
    probability: float, parts: Sequence[tuple[float, float, float]], *, lo: float, hi: float
) -> float:
    target = float(min(max(probability, 1e-9), 1.0 - 1e-9))
    low, high = lo, hi
    for _ in range(200):
        middle = 0.5 * (low + high)
        if _mixture_cdf(middle, parts) < target:
            low = middle
        else:
            high = middle
        if high - low < 1e-10:
            break
    return 0.5 * (low + high)


def _normal_pdf(x: float) -> float:
    return float(math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi))


def truncated_mixture_mean(
    parts: Sequence[tuple[float, float, float]],
    *,
    lower: float | None,
    upper: float | None,
) -> float | None:
    """``E[X | lower <= X <= upper]`` for a Gaussian mixture, in closed form.

    For each part the truncated first moment on ``[a, b]`` (standardised) is
    ``w * (mu * (Phi(b) - Phi(a)) - sigma * (phi(b) - phi(a)))``; summing those and
    dividing by the truncated probability mass gives the conditional mean.

    This is what ``cases.*.expected_return`` must be. Using the conditional
    *median* instead understated a 12-month bear case by roughly 12 points,
    because the median of a truncated tail sits well inside its mean.

    :func:`mix` now hands this function **log-return** parts, so the value it
    returns is a conditional mean log return; the caller exponentiates it. The
    maths here is unchanged and space-agnostic.
    """
    numerator = 0.0
    denominator = 0.0
    for weight, mu, sigma in parts:
        if sigma <= 0:
            inside = (lower is None or mu >= lower) and (upper is None or mu <= upper)
            if inside:
                numerator += weight * mu
                denominator += weight
            continue
        a = (lower - mu) / sigma if lower is not None else None
        b = (upper - mu) / sigma if upper is not None else None
        cdf_a = 0.0 if a is None else 0.5 * (1.0 + math.erf(a / math.sqrt(2.0)))
        cdf_b = 1.0 if b is None else 0.5 * (1.0 + math.erf(b / math.sqrt(2.0)))
        pdf_a = 0.0 if a is None else _normal_pdf(a)
        pdf_b = 0.0 if b is None else _normal_pdf(b)
        mass = cdf_b - cdf_a
        numerator += weight * (mu * mass - sigma * (pdf_b - pdf_a))
        denominator += weight * mass
    if denominator <= 1e-12:
        return None
    value = numerator / denominator
    return float(value) if math.isfinite(value) else None


def _case_block(
    parts: Sequence[tuple[float, float, float]],
    *,
    lower: float | None,
    upper: float | None,
    probability: float,
    current_price: float | None,
    search_lo: float,
    search_hi: float,
) -> dict[str, Any]:
    """One case (bear / neutral / bull) of the **log-return** mixture.

    ``parts`` are ``(weight, mu_log, sigma_log)`` on ``log(1 + return)``, and
    ``lower``/``upper`` are the case's cuts in that same space. Every reported
    figure is converted back with ``expm1`` (returns) or ``exp`` (prices), so a
    return can never come out below -100% and the three price percentiles are
    lognormal-consistent: monotone, strictly positive, and
    ``price_p50 = spot * exp(median_log)``.
    """
    base = _mixture_cdf(lower, parts) if lower is not None else 0.0
    top = _mixture_cdf(upper, parts) if upper is not None else 1.0
    span = max(top - base, 1e-12)
    quantiles: dict[str, float] = {}
    for label, fraction in (("p10", 0.10), ("p50", 0.50), ("p90", 0.90)):
        quantiles[label] = _mixture_quantile(
            base + span * fraction, parts, lo=search_lo, hi=search_hi
        )
    conditional_mean = truncated_mixture_mean(parts, lower=lower, upper=upper)
    expected_log = (
        float(conditional_mean) if conditional_mean is not None else float(quantiles["p50"])
    )
    block: dict[str, Any] = {
        "probability": float(probability),
        # The conditional *expectation* of the log return, expressed as a simple
        # return — not the conditional median, which is reported as p50.
        "expected_return": float(math.expm1(expected_log)),
        "expected_log_return": expected_log,
        "median_return": float(math.expm1(quantiles["p50"])),
        "median_log_return": float(quantiles["p50"]),
        **{key: float(math.expm1(value)) for key, value in quantiles.items()},
        **{f"{key}_log": float(value) for key, value in quantiles.items()},
    }
    if current_price is not None and math.isfinite(current_price) and current_price > 0:
        for label in ("p10", "p50", "p90"):
            block[f"price_{label}"] = float(current_price * math.exp(quantiles[label]))
    return block


def component_agreement(
    components: Mapping[str, Mapping[str, Any]],
    *,
    trailing_mean_daily: float | None,
    horizons: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """How much the component opinions actually differ, per horizon.

    The mixture's variance formula ``sum(w*(sigma^2+mu^2)) - mean^2`` is only a
    fair spread if the components are independent opinions. In practice several
    of them can be restatements of the same trailing mean — seasonality, the
    regime block and the factor forecast all reduce toward the ticker's own
    unconditional drift — in which case the mixture reports a narrow distribution
    built from one estimate quoted several times.

    This block makes that visible: the standard deviation of the available
    component means at each horizon, and each component's distance from the
    ticker's trailing mean compounded over the same horizon.
    """
    horizon_map = _horizon_map(horizons)
    out: dict[str, Any] = {
        "reference": "ticker trailing mean daily return compounded over the horizon",
        "trailing_mean_daily": trailing_mean_daily,
        "horizons": {},
        "note": (
            "A small stdev_of_means with most components close to the trailing mean "
            "means the mixture is one estimate restated, not six opinions disagreeing; "
            "its spread is then too narrow to read as genuine uncertainty."
        ),
    }
    for label, days in horizon_map.items():
        reference = (
            _compound(float(trailing_mean_daily), days)
            if trailing_mean_daily is not None
            else None
        )
        means: dict[str, float] = {}
        for name, component in components.items():
            if not component.get("available"):
                continue
            value = _finite((component.get("expected_return") or {}).get(label))
            if value is not None:
                means[name] = value
        entry: dict[str, Any] = {
            "n_components": len(means),
            "means": means,
            "trailing_mean_return": reference,
            "stdev_of_means": (
                float(np.std(np.asarray(list(means.values()), dtype=np.float64), ddof=1))
                if len(means) > 1
                else None
            ),
            "spread_of_means": (
                float(max(means.values()) - min(means.values())) if means else None
            ),
        }
        if reference is not None:
            entry["vs_trailing_mean"] = {
                name: float(value - reference) for name, value in means.items()
            }
            close = [
                name
                for name, value in means.items()
                if abs(value - reference) <= 0.25 * max(abs(reference), 1e-9)
            ]
            entry["within_25pct_of_trailing_mean"] = sorted(close)
            entry["collinear"] = bool(len(close) >= 3)
        out["horizons"][label] = entry
    return out


def mix(
    components: Mapping[str, Mapping[str, Any]],
    weights: Mapping[str, float],
    *,
    current_price: float | None = None,
    horizons: Mapping[str, int] | None = None,
    cut_sigma: float = _CASE_CUT_SIGMA,
) -> dict[str, Any]:
    """Combine component forecasts into a mixture and cut it into three cases.

    **The mixture lives in log-return space.** Each component's shrunk
    ``(mean, sigma)`` on the simple return is moment-matched onto a lognormal by
    :func:`to_log_space`, the mixture moments, the ``+/- cut_sigma`` split, the
    case probabilities and the truncated conditional means are all computed on
    ``log(1 + return)``, and every published figure is converted back with
    ``expm1`` (returns) or ``exp`` (prices). A Gaussian on the *simple* return
    had no floor, so a name with 80% annualised volatility printed a 12-month
    bear case of -176%; in log space a return is bounded below by -100% and
    every price percentile is strictly positive and monotone by construction.

    Returns ``{"cases", "distribution", "effective_weights", "horizons", ...}``
    where ``cases`` holds ``bull``/``neutral``/``bear``, each with a
    probability and per-horizon conditional percentiles (and prices when
    ``current_price`` is given). Case probabilities sum to exactly 1 per horizon.
    ``distribution[h]`` keeps ``mean``/``std``/``skew``/``kurtosis`` in
    simple-return terms (closed-form moments of the lognormal mixture) and adds
    ``*_log`` for the space the model actually works in.
    """
    horizon_map = _horizon_map(horizons)
    distribution: dict[str, Any] = {}
    cases: dict[str, dict[str, Any]] = {
        "bull": {"horizons": {}},
        "neutral": {"horizons": {}},
        "bear": {"horizons": {}},
    }
    effective: dict[str, dict[str, float]] = {}
    missing: dict[str, str] = {}
    mixture_parts: dict[str, list[list[float]]] = {}

    for label, _days in horizon_map.items():
        parts: list[tuple[float, float, float]] = []
        used: dict[str, float] = {}
        for name, component in components.items():
            if not component.get("available"):
                missing.setdefault(name, str(component.get("reason") or "unavailable"))
                continue
            weight = _finite(weights.get(name))
            mu = _finite((component.get("expected_return") or {}).get(label))
            sigma = _finite((component.get("sigma") or {}).get(label))
            if weight is None or weight <= 0 or mu is None or sigma is None or sigma <= 0:
                continue
            converted = to_log_space(mu, sigma)
            if converted is None:
                # A forecast at or below -100% has no lognormal image. Say so
                # rather than silently dropping the component.
                missing.setdefault(
                    name,
                    f"{label}: expected return {mu:+.1%} has no lognormal image "
                    "(a simple return cannot be at or below -100%)",
                )
                continue
            parts.append((weight, converted[0], converted[1]))
            used[name] = weight
        if not parts:
            distribution[label] = {
                "mean": None,
                "std": None,
                "skew": None,
                "kurtosis": None,
                "reason": "no component produced a usable forecast at this horizon",
            }
            for case in cases.values():
                case["horizons"][label] = {
                    "expected_return": None,
                    "p10": None,
                    "p50": None,
                    "p90": None,
                    "probability": None,
                }
            effective[label] = {}
            continue

        total_weight = float(sum(weight for weight, _, _ in parts))
        parts = [(weight / total_weight, mu, sigma) for weight, mu, sigma in parts]
        effective[label] = {
            name: float(weight / total_weight) for name, weight in used.items()
        }
        mixture_parts[label] = [
            [float(weight), float(mu), float(sigma)] for weight, mu, sigma in parts
        ]

        # Moments of the Gaussian mixture on log(1 + return): this is the space
        # the cuts, the probabilities and the conditional means work in.
        mean_log = float(sum(weight * mu for weight, mu, _ in parts))
        second_log = float(sum(weight * (sigma**2 + mu**2) for weight, mu, sigma in parts))
        variance_log = max(second_log - mean_log**2, 1e-18)
        std_log = math.sqrt(variance_log)
        m3_log = float(
            sum(
                weight * (3.0 * sigma**2 * (mu - mean_log) + (mu - mean_log) ** 3)
                for weight, mu, sigma in parts
            )
        )
        m4_log = float(
            sum(
                weight
                * (
                    3.0 * sigma**4
                    + 6.0 * sigma**2 * (mu - mean_log) ** 2
                    + (mu - mean_log) ** 4
                )
                for weight, mu, sigma in parts
            )
        )
        # ... and the closed-form simple-return moments of the same mixture, so
        # `mean` / `std` / `skew` / `kurtosis` keep the meaning they always had.
        simple = _lognormal_mixture_moments(parts)
        distribution[label] = {
            "mean": simple["mean"],
            "std": simple["std"],
            "skew": simple["skew"],
            "kurtosis": simple["kurtosis"],
            "excess_kurtosis": simple["excess_kurtosis"],
            "return_space": "log",
            "mean_log": mean_log,
            "std_log": std_log,
            "skew_log": float(m3_log / std_log**3) if std_log > 0 else None,
            "kurtosis_log": float(m4_log / std_log**4) if std_log > 0 else None,
            # expm1 of the mean log return: the geometric (compounding) mean,
            # which sits below `mean` by the variance drag.
            "geometric_mean_return": float(math.expm1(mean_log)),
            "n_components": len(parts),
        }

        low_cut = mean_log - cut_sigma * std_log
        high_cut = mean_log + cut_sigma * std_log
        search_lo = mean_log - 12.0 * std_log
        search_hi = mean_log + 12.0 * std_log
        p_bear = _mixture_cdf(low_cut, parts)
        p_bull = 1.0 - _mixture_cdf(high_cut, parts)
        p_neutral = max(1.0 - p_bear - p_bull, 0.0)
        # Renormalise so the three probabilities sum to exactly 1.
        total_probability = p_bear + p_bull + p_neutral
        p_bear, p_bull, p_neutral = (
            p_bear / total_probability,
            p_bull / total_probability,
            p_neutral / total_probability,
        )

        cases["bear"]["horizons"][label] = _case_block(
            parts,
            lower=None,
            upper=low_cut,
            probability=p_bear,
            current_price=current_price,
            search_lo=search_lo,
            search_hi=search_hi,
        )
        cases["neutral"]["horizons"][label] = _case_block(
            parts,
            lower=low_cut,
            upper=high_cut,
            probability=p_neutral,
            current_price=current_price,
            search_lo=search_lo,
            search_hi=search_hi,
        )
        cases["bull"]["horizons"][label] = _case_block(
            parts,
            lower=high_cut,
            upper=None,
            probability=p_bull,
            current_price=current_price,
            search_lo=search_lo,
            search_hi=search_hi,
        )
        # The cuts are made in log space; both readings are published so a
        # consumer never has to guess which one a number is in.
        distribution[label]["cut_low"] = float(math.expm1(low_cut))
        distribution[label]["cut_high"] = float(math.expm1(high_cut))
        distribution[label]["cut_low_log"] = float(low_cut)
        distribution[label]["cut_high_log"] = float(high_cut)

    return {
        "cases": cases,
        "distribution": distribution,
        "effective_weights": effective,
        "unavailable_components": missing,
        "cut_sigma": cut_sigma,
        "horizons": horizon_map,
        # The mixture is built on log(1 + return); every published return and
        # price is converted back with expm1 / exp.
        "return_space": "log",
        # [weight, mu_log, sigma_log] per surviving component, so downstream
        # consumers can re-evaluate the exact mixture instead of approximating
        # it. These are LOG-space parameters.
        "mixture_parts": mixture_parts,
        "mixture_parts_space": "log",
    }


# --------------------------------------------------------------------------
# Derived views: entry band, timing, watch signals, narratives
# --------------------------------------------------------------------------


def entry_zone(
    mixture: Mapping[str, Any],
    current_price: float | None,
    *,
    horizon: str = "6m",
) -> dict[str, Any]:
    """Bargain / fair / expensive band from the ``horizon`` mixture.

    Works on the log-return mixture and exponentiates, so the band is
    lognormal-consistent: strictly positive, monotone, and
    ``fair_value = spot * exp(median_log)``.
    """
    zone: dict[str, Any] = {
        "bargain_below": None,
        "fair_value": None,
        "expensive_above": None,
        "current_price": current_price,
        "current_vs_fair": None,
        "horizon": horizon,
        "reason": None,
    }
    distribution = (mixture.get("distribution") or {}).get(horizon)
    if not isinstance(distribution, Mapping) or distribution.get("mean") is None:
        zone["reason"] = f"no usable mixture at the {horizon} horizon"
        return zone
    if current_price is None or not math.isfinite(current_price) or current_price <= 0:
        zone["reason"] = "no current price"
        return zone

    parts: list[tuple[float, float, float]] = []
    exact = (mixture.get("mixture_parts") or {}).get(horizon)
    if isinstance(exact, list) and exact:
        parts = [
            (float(row[0]), float(row[1]), float(row[2]))
            for row in exact
            if len(row) == 3 and float(row[2]) > 0
        ]
    if not parts:
        # Fall back to reconstructing an approximate mixture from the case blocks
        # (each case treated as a Gaussian on the LOG return, matched on its
        # p10/p50/p90 — the blocks publish those in simple-return terms, so they
        # are taken back into log space first).
        cases = mixture.get("cases") or {}
        for case in ("bear", "neutral", "bull"):
            block = (cases.get(case) or {}).get("horizons", {}).get(horizon)
            if not isinstance(block, Mapping):
                continue
            probability = _finite(block.get("probability")) or 0.0
            logs: list[float | None] = []
            for key in ("p10", "p50", "p90"):
                direct = _finite(block.get(f"{key}_log"))
                if direct is None:
                    simple = _finite(block.get(key))
                    direct = math.log1p(simple) if simple is not None and simple > -1.0 else None
                logs.append(direct)
            p10, p50, p90 = logs
            if p50 is not None and p10 is not None and p90 is not None and probability > 0:
                sigma = max((p90 - p10) / (2.0 * 1.2815515655446004), 1e-6)
                parts.append((probability, p50, sigma))
    if not parts:
        zone["reason"] = "the mixture carried no usable components at this horizon"
        return zone
    total = float(sum(weight for weight, _, _ in parts)) or 1.0
    parts = [(weight / total, mu, sigma) for weight, mu, sigma in parts]
    mean = _finite(distribution.get("mean_log"))
    if mean is None:
        mean = float(sum(weight * mu for weight, mu, _ in parts))
    std = _finite(distribution.get("std_log"))
    if std is None or std <= 0:
        second = float(sum(weight * (sigma**2 + mu**2) for weight, mu, sigma in parts))
        std = math.sqrt(max(second - mean**2, 1e-12))
    lo, hi = mean - 12.0 * std, mean + 12.0 * std

    p25 = _mixture_quantile(0.25, parts, lo=lo, hi=hi)
    p50 = _mixture_quantile(0.50, parts, lo=lo, hi=hi)
    p75 = _mixture_quantile(0.75, parts, lo=lo, hi=hi)
    fair = float(current_price * math.exp(p50))
    zone.update(
        {
            "bargain_below": float(current_price * math.exp(p25)),
            "fair_value": fair,
            "expensive_above": float(current_price * math.exp(p75)),
            "current_vs_fair": float(current_price / fair - 1.0) if fair > 0 else None,
            "return_space": "log",
            "fair_value_return": float(math.expm1(p50)),
            "method": (
                f"p25 / p50 / p75 of the {horizon} log-return mixture, exponentiated onto "
                "the current price"
            ),
        }
    )
    return zone


def timing_label(
    mixture: Mapping[str, Any],
    *,
    seasonality: Mapping[str, Any] | None = None,
    month_label: str | None = None,
    horizon: str = "1m",
) -> dict[str, Any]:
    """Is this a good month to be adding, on the mixture plus seasonality?"""
    distribution = (mixture.get("distribution") or {}).get(horizon)
    result: dict[str, Any] = {"this_month": "unknown", "reason": None}
    if not isinstance(distribution, Mapping) or distribution.get("mean") is None:
        result["reason"] = f"no usable {horizon} mixture"
        return result
    mean = float(distribution["mean"])
    std = float(distribution.get("std") or 0.0)
    ratio = mean / std if std > 0 else 0.0

    cases = mixture.get("cases") or {}

    def _case_probability(name: str) -> float:
        block = (cases.get(name) or {}).get("horizons", {}).get(horizon) or {}
        return _finite(block.get("probability")) or 0.0

    p_bull = _case_probability("bull")
    p_bear = _case_probability("bear")

    hit_rate: float | None = None
    if isinstance(seasonality, Mapping):
        ticker_stats = seasonality.get("ticker")
        this_month = (
            (ticker_stats or {}).get("this_month") if isinstance(ticker_stats, Mapping) else None
        )
        if isinstance(this_month, Mapping):
            for window in ("10y", "5y", "2y", "1y"):
                entry = this_month.get(window)
                if isinstance(entry, Mapping) and _finite(entry.get("hit_rate")) is not None:
                    hit_rate = _finite(entry.get("hit_rate"))
                    break

    score = ratio + (p_bull - p_bear) + ((hit_rate - 0.5) if hit_rate is not None else 0.0)
    if score > 0.25:
        label = "good"
    elif score < -0.25:
        label = "bad"
    else:
        label = "neutral"
    pieces = [
        f"{horizon} mixture mean {mean:+.2%} on a {std:.2%} spread (ratio {ratio:+.2f})",
        f"bull {p_bull:.0%} vs bear {p_bear:.0%}",
    ]
    if hit_rate is not None:
        pieces.append(
            f"{month_label or 'this month'} has closed higher {hit_rate:.0%} of the time"
        )
    result.update(
        {
            "this_month": label,
            "reason": "; ".join(pieces),
            "score": float(score),
            "month_label": month_label,
            "seasonal_hit_rate": hit_rate,
        }
    )
    return result


def watch_signals(
    impact_weights: Mapping[str, Any] | None,
    *,
    regimes: Mapping[str, Any] | None = None,
    entropy: Mapping[str, Any] | None = None,
    ticker: str = "the ticker",
    limit: int = 6,
) -> list[dict[str, Any]]:
    """What to watch, derived from the relational impact weights and regimes."""
    signals: list[dict[str, Any]] = []
    if isinstance(impact_weights, Mapping):
        scored: list[tuple[str, float, float | None]] = []
        for symbol, payload in impact_weights.items():
            if isinstance(payload, Mapping):
                weight = _finite(payload.get("weight"))
                share = _finite(payload.get("explained_variance_share"))
            else:
                weight = _finite(payload)
                share = None
            if weight is not None:
                scored.append((str(symbol), weight, share))
        scored.sort(key=lambda item: -abs(item[1]))
        for symbol, weight, share in scored[:limit]:
            explained = (
                f"{share:.0%} of {ticker}'s return variance"
                if share is not None
                else f"an impact weight of {weight:.2f}"
            )
            signals.append(
                {
                    "symbol": symbol,
                    "condition": f"{symbol} moves more than 5% over 20 sessions",
                    "implication": (
                        f"{symbol} alone explains {explained} (mixture weight "
                        f"{weight:.0%}), so a move of that size is the single largest "
                        f"co-movement risk to the scenario set"
                    ),
                    "impact_weight": float(weight),
                    "explained_variance_share": (float(share) if share is not None else None),
                }
            )

    if isinstance(regimes, Mapping) and isinstance(regimes.get("current"), Mapping):
        current = regimes["current"]
        next_probabilities = current.get("next_state_probabilities")
        if isinstance(next_probabilities, Mapping):
            label = str(current.get("label"))
            leaving = {
                key: _finite(value) or 0.0
                for key, value in next_probabilities.items()
                if str(key) != label
            }
            if leaving:
                target = max(leaving, key=lambda key: leaving[key])
                signals.append(
                    {
                        "symbol": str(regimes.get("trained_on") or "SPY"),
                        "condition": (
                            f"the HMM regime flips from '{label}' to '{target}' "
                            f"(daily transition probability {leaving[target]:.1%})"
                        ),
                        "implication": (
                            "regime-conditional expected return and volatility both reset; "
                            "the regime component of the mixture would be repriced"
                        ),
                        "impact_weight": None,
                    }
                )

    if isinstance(entropy, Mapping):
        current = entropy.get("current")
        if isinstance(current, Mapping) and _finite(current.get("H")) is not None:
            value = float(current["H"])
            signals.append(
                {
                    "symbol": ticker,
                    "condition": (
                        f"rolling return entropy (now {value:.2f}, "
                        f"{current.get('classification')}) crosses 0.35 or 0.70"
                    ),
                    "implication": (
                        "a move into the structure band historically preceded a higher "
                        "win rate on 21-day holds; a move into the noise band removes that edge"
                    ),
                    "impact_weight": None,
                }
            )
    return signals


def _narrative(
    case: str,
    horizon_block: Mapping[str, Any],
    weights: Mapping[str, float],
    components: Mapping[str, Mapping[str, Any]],
    horizon: str,
) -> str:
    ranked = sorted(
        (
            (name, float(weight))
            for name, weight in weights.items()
            if components.get(name, {}).get("available")
        ),
        key=lambda item: -item[1],
    )[:3]
    drivers = (
        ", ".join(f"{name} ({weight:.0%})" for name, weight in ranked)
        or "no weighted component"
    )
    p50 = _finite(horizon_block.get("p50"))
    probability = _finite(horizon_block.get("probability"))
    move = f"{p50:+.1%}" if p50 is not None else "an unquantified move"
    chance = f"{probability:.0%}" if probability is not None else "an unquantified share"
    return (
        f"{case.title()} case ({chance} of the {horizon} distribution): "
        f"median {move} over {horizon}. "
        f"Weight is carried by {drivers}. Not investment advice."
    )


# --------------------------------------------------------------------------
# Section builder
# --------------------------------------------------------------------------


def build_scenarios(
    *,
    close: pd.Series | None = None,
    market_close: pd.Series | None = None,
    market_symbol: str = "SPY",
    current_price: float | None = None,
    seasonality: Mapping[str, Any] | None = None,
    regimes: Mapping[str, Any] | None = None,
    factors: Mapping[str, Any] | None = None,
    spectral: Mapping[str, Any] | None = None,
    fundamentals: Mapping[str, Any] | None = None,
    macro: Mapping[str, Any] | None = None,
    impact_weights: Mapping[str, Any] | None = None,
    entropy: Mapping[str, Any] | None = None,
    regime_label_series: pd.Series | None = None,
    realized_vol_annual: float | None = None,
    factor_premia: Mapping[str, float] | None = None,
    horizons: Mapping[str, int] | None = None,
    holdout_days: int = 252,
    ticker: str = "the ticker",
    month_label: str | None = None,
    probability_horizon: str = "3m",
) -> dict[str, Any]:
    """Build the packet's ``scenarios`` section end to end.

    Any section may be ``None``; the mixture simply uses whatever is available
    and records the rest under ``unavailable_components``. ``close`` (the
    ticker's daily closes) powers the walk-forward weighting, the plausibility
    clamp and, when ``current_price`` is omitted, the last price.
    ``market_close`` (SPY by default) supplies the shrinkage prior; without it
    the documented default drift assumption stands in and says so.
    """
    horizon_map = _horizon_map(horizons)
    section: dict[str, Any] = {
        "method": "weighted_mixture_of_shrunk_components",
        # The mixture is built on log(1 + return) and every published return and
        # price is converted back with expm1 / exp, so nothing can print a
        # return below -100% or a negative price.
        "return_space": "log",
        "weights": {},
        "weight_evidence": {},
        "prior": {},
        "clamp_bounds": {},
        "cases": {},
        "distribution": {},
        "entry": None,
        "timing": None,
        "watch_signals": [],
        "components": {},
        "error": None,
    }

    prices: pd.Series | None = None
    if close is not None:
        prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
        prices = prices[prices > 0]
        if prices.empty:
            prices = None
    if current_price is None and prices is not None:
        current_price = float(prices.iloc[-1])
    realized_vol_source = "caller_supplied"
    if realized_vol_annual is None and prices is not None and prices.shape[0] > 30:
        # Fallback only. The caller passes the number the `volatility` section
        # already published; computing a second one here (and on simple rather
        # than log returns) made the mixture's base sigma disagree with the
        # volatility the packet publishes. Log returns match `volatility.py`.
        recent = np.log(prices).diff().dropna().iloc[-252:]
        if recent.shape[0] > 20:
            realized_vol_annual = float(recent.std(ddof=1) * math.sqrt(_TRADING_DAYS))
            realized_vol_source = "scenarios_fallback_log_returns_252d"

    components = component_forecasts(
        seasonality=seasonality,
        regimes=regimes,
        factors=factors,
        spectral=spectral,
        fundamentals=fundamentals,
        macro=macro,
        realized_vol_annual=realized_vol_annual,
        factor_premia=factor_premia,
        horizons=horizon_map,
    )
    predictions = pd.DataFrame()
    realized = pd.Series(dtype="float64")
    if prices is not None:
        predictions, realized = default_prediction_history(
            prices, regime_labels=regime_label_series, horizon_days=horizon_map.get("1m", 21)
        )
    weighting = walk_forward_weights(
        predictions,
        realized,
        holdout_days=holdout_days,
        components=list(components),
    )
    section["weights"] = weighting["weights"]
    section["weight_evidence"] = weighting["evidence"]

    # Calibration. Every component above extrapolates something the ticker
    # already did; shrink each one toward the market's own long-run drift by
    # how much evidence actually stands behind it, then clip what survives to
    # the range the name has historically traded over that horizon.
    prior = market_prior(market_close, horizon_map, symbol=market_symbol)
    bounds = empirical_return_bounds(prices, horizon_map)
    components = shrink_components(
        components,
        prior=prior,
        bounds=bounds,
        weight_evidence=weighting["evidence"],
        n_observations=int(prices.shape[0]) if prices is not None else None,
        horizons=horizon_map,
    )
    section["prior"] = prior
    section["clamp_bounds"] = bounds
    section["components"] = {name: dict(component) for name, component in components.items()}

    mixture = mix(
        components,
        weighting["weights"],
        current_price=current_price,
        horizons=horizon_map,
    )
    section["distribution"] = mixture["distribution"]
    section["effective_weights"] = mixture["effective_weights"]
    section["unavailable_components"] = mixture["unavailable_components"]
    # Ground rule: keep every intermediate so the memo and the chat can cite it.
    section["mixture_parts"] = mixture["mixture_parts"]
    section["mixture_parts_space"] = mixture["mixture_parts_space"]
    section["return_space"] = mixture["return_space"]
    section["cut_sigma"] = mixture["cut_sigma"]

    reference = (
        probability_horizon if probability_horizon in horizon_map else next(iter(horizon_map))
    )
    cases: dict[str, Any] = {}
    for case_name, case_body in mixture["cases"].items():
        block = case_body["horizons"].get(reference) or {}
        cases[case_name] = {
            "probability": _finite(block.get("probability")),
            "narrative": _narrative(
                case_name, block, weighting["weights"], components, reference
            ),
            "horizons": case_body["horizons"],
        }
    section["cases"] = cases
    section["probability_horizon"] = reference
    section["entry"] = entry_zone(
        mixture, current_price, horizon="6m" if "6m" in horizon_map else reference
    )
    section["timing"] = timing_label(
        mixture,
        seasonality=seasonality,
        month_label=month_label,
        horizon="1m" if "1m" in horizon_map else reference,
    )
    section["watch_signals"] = watch_signals(
        impact_weights, regimes=regimes, entropy=entropy, ticker=ticker
    )
    section["current_price"] = current_price
    section["realized_vol_annual"] = realized_vol_annual
    section["realized_vol_source"] = realized_vol_source

    trailing_mean_daily: float | None = None
    if prices is not None and prices.shape[0] > 30:
        trailing = prices.pct_change().dropna()
        if not trailing.empty:
            trailing_mean_daily = float(trailing.mean())
    section["component_agreement"] = component_agreement(
        components, trailing_mean_daily=trailing_mean_daily, horizons=horizon_map
    )
    if not any(component.get("available") for component in components.values()):
        section["error"] = "no component produced a forecast"
    return section
