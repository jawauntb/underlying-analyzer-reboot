"""Market-regime detection, following Wang, Lin & Mikhelson (2020).

*Regime-Switching Factor Investing with Hidden Markov Models*
(J. Risk Financial Manag. 2020, 13, 311) specifies, and this module reproduces:

* **Observations** — two features derived from the S&P 500 ETF's daily closes:
  the daily return ``(C_t - C_{t-1}) / C_{t-1}``, and a volatility proxy equal to
  the *mean squared error of the closes around their moving average inside a
  10-day sliding window* (paper §3.1).
* **Model** — a three-state Gaussian HMM with full covariance matrices,
  trained by Baum-Welch (paper §3.2, §3.2.3). Implemented in
  :mod:`app.prism.hmm` in pure numpy.
* **Training window** — a sliding window of the most recent **2707 days** of
  daily return / volatility data (paper §3.5.1).
* **Labelling** — "the regime with the lowest or most negative returns, which
  typically also has the highest volatility, is the *bear* regime and the regime
  with the highest return is the *bull* regime" (paper §3.5.1); the remaining
  state is the sideways / "kangaroo" regime, which Prism calls *neutral*.
* **Regime detection** — the paper fits each regime's observations to one of
  several common distributions with a Kolmogorov-Smirnov test and then compares
  the PDF of the newest observation across regimes, declaring the regime
  confirmed when the volatility PDF exceeds 0.3 and the return PDF exceeds 0.5
  (paper §3.5.2). Reproduced here with closed-form families only (normal,
  Laplace, lognormal, exponential, Pareto) because SciPy is not available; the
  chosen family and its KS statistic are reported so the fit is auditable.

Every public function is pure: pandas in, JSON-serialisable dicts out.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from app.prism.hmm import (
    GaussianHMM,
    expected_durations,
    filtered_posteriors,
    filtered_states,
    fit_gaussian_hmm,
    posterior_states,
    stationary_distribution,
    viterbi,
)

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "DEFAULT_TRAIN_WINDOW_DAYS",
    "FEATURE_NAMES",
    "REGIME_LABELS",
    "RegimeFit",
    "build_regimes",
    "fit_distribution",
    "fit_regime_model",
    "label_states",
    "regime_features",
    "regime_filtered_state_series",
    "regime_state_series",
    "ticker_stats_by_regime",
]

#: Paper §3.5.1 — sliding retraining window, in observations.
DEFAULT_TRAIN_WINDOW_DAYS = 2707
#: Paper §3.1 — the two observation variables.
FEATURE_NAMES = ("daily_return", "vol_10d_mse")
REGIME_LABELS = ("bull", "neutral", "bear")
_TRADING_DAYS = 252.0
_DEFAULT_SEED = 20260901
#: Paper §3.5.2 confirmation thresholds.
_PAPER_PDF_VOL_THRESHOLD = 0.3
_PAPER_PDF_RETURN_THRESHOLD = 0.5


# --------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------


def regime_features(
    close: pd.Series,
    *,
    window: int = 10,
    feature_scale: str = "relative",
) -> pd.DataFrame:
    """Build the paper's two observation variables from a close series.

    Parameters
    ----------
    close:
        Daily closes indexed by date (a ``DatetimeIndex`` is expected but any
        monotonically ordered index works).
    window:
        Sliding-window length for the volatility proxy (10 in the paper).
    feature_scale:
        ``"paper"`` reproduces the paper literally: the volatility feature is
        the MSE of the raw closes around their ``window``-day moving average, so
        it carries price-squared units. ``"relative"`` (default) divides that
        MSE by the squared moving average, making it the MSE of the *fractional*
        deviation from the moving average. The relative form is scale free and
        therefore stationary across a 2707-day window over which the index level
        can double; the paper's raw form silently up-weights recent years for a
        rising index. The two are a fixed positive rescaling of each other
        within any single day, so the regime *ordering* logic is unchanged.

    Returns
    -------
    DataFrame with columns ``daily_return`` and ``vol_10d_mse``, indexed by date,
    with the leading warm-up rows dropped.
    """
    if feature_scale not in {"paper", "relative"}:
        raise ValueError("feature_scale must be 'paper' or 'relative'")
    prices = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    prices = prices[prices > 0]
    if prices.shape[0] <= window + 1:
        raise ValueError(f"need more than {window + 1} closes to build regime features")

    daily_return = prices.pct_change()
    moving_average = prices.rolling(window).mean()
    squared_error = (prices - moving_average) ** 2
    mse = squared_error.rolling(window).mean()
    if feature_scale == "relative":
        mse = mse / (moving_average**2)

    frame = pd.DataFrame(
        {FEATURE_NAMES[0]: daily_return, FEATURE_NAMES[1]: mse},
        index=prices.index,
    ).dropna()
    frame = frame[np.isfinite(frame.to_numpy(dtype=np.float64)).all(axis=1)]
    return frame


# --------------------------------------------------------------------------
# Closed-form distribution fitting with a Kolmogorov-Smirnov selection
# --------------------------------------------------------------------------


def _normal_cdf(x: FloatArray, loc: float, scale: float) -> FloatArray:
    z = (x - loc) / (scale * math.sqrt(2.0))
    return np.asarray(0.5 * (1.0 + np.vectorize(math.erf)(z)), dtype=np.float64)


def _normal_pdf(x: float, loc: float, scale: float) -> float:
    return float(math.exp(-0.5 * ((x - loc) / scale) ** 2) / (scale * math.sqrt(2.0 * math.pi)))


def _laplace_cdf(x: FloatArray, loc: float, scale: float) -> FloatArray:
    out = np.where(
        x < loc,
        0.5 * np.exp((x - loc) / scale),
        1.0 - 0.5 * np.exp(-(x - loc) / scale),
    )
    return np.asarray(out, dtype=np.float64)


def _laplace_pdf(x: float, loc: float, scale: float) -> float:
    return float(math.exp(-abs(x - loc) / scale) / (2.0 * scale))


def _lognormal_cdf(x: FloatArray, loc: float, scale: float) -> FloatArray:
    out = np.zeros_like(x, dtype=np.float64)
    positive = x > 0
    out[positive] = _normal_cdf(np.log(x[positive]), loc, scale)
    return out


def _lognormal_pdf(x: float, loc: float, scale: float) -> float:
    if x <= 0:
        return 0.0
    return float(_normal_pdf(math.log(x), loc, scale) / x)


def _exponential_cdf(x: FloatArray, rate: float) -> FloatArray:
    return np.asarray(np.where(x > 0, 1.0 - np.exp(-rate * np.clip(x, 0, None)), 0.0), np.float64)


def _exponential_pdf(x: float, rate: float) -> float:
    return float(rate * math.exp(-rate * x)) if x > 0 else 0.0


def _pareto_cdf(x: FloatArray, scale: float, shape: float) -> FloatArray:
    out = np.zeros_like(x, dtype=np.float64)
    above = x >= scale
    out[above] = 1.0 - (scale / x[above]) ** shape
    return out


def _pareto_pdf(x: float, scale: float, shape: float) -> float:
    if x < scale:
        return 0.0
    return float(shape * (scale**shape) / (x ** (shape + 1.0)))


def _ks_statistic(sorted_values: FloatArray, cdf: FloatArray) -> float:
    n = sorted_values.shape[0]
    if n == 0:
        return float("inf")
    upper = np.arange(1, n + 1, dtype=np.float64) / n
    lower = np.arange(0, n, dtype=np.float64) / n
    return float(max(np.max(upper - cdf), np.max(cdf - lower)))


@dataclass(frozen=True)
class _Candidate:
    family: str
    params: dict[str, float]
    ks_statistic: float


def fit_distribution(values: Sequence[float] | FloatArray | pd.Series) -> dict[str, Any]:
    """Fit common closed-form distributions and pick the best by KS distance.

    Reproduces the paper's §3.5.2 selection step (which used SciPy's KS test over
    normal / lognormal / Pareto / gamma / beta / exponential) with the families
    whose MLE **and** CDF are available in closed form without SciPy: normal and
    Laplace on any support, plus lognormal, exponential and Pareto when every
    observation is strictly positive. Gamma and beta are deliberately omitted —
    their CDFs need the incomplete gamma/beta functions — and the omission is
    reported in ``families_considered``.

    Returns
    -------
    ``{"family", "params", "ks_statistic", "n", "families_considered", "mean",
    "std"}``; ``family`` is ``None`` when there is not enough data.
    """
    array = np.asarray(pd.Series(values).astype(float).dropna().to_numpy(), dtype=np.float64)
    array = array[np.isfinite(array)]
    n = int(array.shape[0])
    base: dict[str, Any] = {
        "family": None,
        "params": {},
        "ks_statistic": None,
        "n": n,
        "families_considered": [],
        "mean": float(np.mean(array)) if n else None,
        "std": float(np.std(array, ddof=1)) if n > 1 else None,
        "omitted_families": ["gamma", "beta"],
        "omitted_reason": "cdf requires incomplete gamma/beta; scipy unavailable",
    }
    if n < 8:
        return base

    ordered = np.sort(array)
    candidates: list[_Candidate] = []
    considered: list[str] = []

    mean = float(np.mean(ordered))
    std = float(np.std(ordered, ddof=1))
    if std > 0:
        considered.append("normal")
        candidates.append(
            _Candidate(
                "normal",
                {"loc": mean, "scale": std},
                _ks_statistic(ordered, _normal_cdf(ordered, mean, std)),
            )
        )

    median = float(np.median(ordered))
    mad = float(np.mean(np.abs(ordered - median)))
    if mad > 0:
        considered.append("laplace")
        candidates.append(
            _Candidate(
                "laplace",
                {"loc": median, "scale": mad},
                _ks_statistic(ordered, _laplace_cdf(ordered, median, mad)),
            )
        )

    if float(ordered[0]) > 0:
        logs = np.log(ordered)
        log_mean = float(np.mean(logs))
        log_std = float(np.std(logs, ddof=1))
        if log_std > 0:
            considered.append("lognormal")
            candidates.append(
                _Candidate(
                    "lognormal",
                    {"loc": log_mean, "scale": log_std},
                    _ks_statistic(ordered, _lognormal_cdf(ordered, log_mean, log_std)),
                )
            )
        if mean > 0:
            rate = 1.0 / mean
            considered.append("exponential")
            candidates.append(
                _Candidate(
                    "exponential",
                    {"rate": rate},
                    _ks_statistic(ordered, _exponential_cdf(ordered, rate)),
                )
            )
        minimum = float(ordered[0])
        log_ratio = float(np.sum(np.log(ordered / minimum)))
        if log_ratio > 0:
            shape = n / log_ratio
            considered.append("pareto")
            candidates.append(
                _Candidate(
                    "pareto",
                    {"scale": minimum, "shape": shape},
                    _ks_statistic(ordered, _pareto_cdf(ordered, minimum, shape)),
                )
            )

    base["families_considered"] = considered
    if not candidates:
        return base
    best = min(candidates, key=lambda item: item.ks_statistic)
    base["family"] = best.family
    base["params"] = {key: float(value) for key, value in best.params.items()}
    base["ks_statistic"] = float(best.ks_statistic)
    base["all_ks"] = {item.family: float(item.ks_statistic) for item in candidates}
    return base


def _fitted_pdf(fit: Mapping[str, Any], x: float) -> float:
    """Evaluate the density selected by :func:`fit_distribution` at ``x``."""
    family = fit.get("family")
    params = fit.get("params") or {}
    if not isinstance(family, str) or not math.isfinite(x):
        return 0.0
    try:
        if family == "normal":
            return _normal_pdf(x, float(params["loc"]), float(params["scale"]))
        if family == "laplace":
            return _laplace_pdf(x, float(params["loc"]), float(params["scale"]))
        if family == "lognormal":
            return _lognormal_pdf(x, float(params["loc"]), float(params["scale"]))
        if family == "exponential":
            return _exponential_pdf(x, float(params["rate"]))
        if family == "pareto":
            return _pareto_pdf(x, float(params["scale"]), float(params["shape"]))
    except (KeyError, ValueError, OverflowError):
        return 0.0
    return 0.0


# --------------------------------------------------------------------------
# Fitting and labelling
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeFit:
    """A fitted regime model plus the decoded training history."""

    model: GaussianHMM
    features: pd.DataFrame
    #: Viterbi (smoothed) path — the descriptive decoding. The state at ``t``
    #: depends on observations after ``t``, so this must never be used as a
    #: predictor.
    states: pd.Series
    posteriors: FloatArray
    labels: dict[int, str]
    feature_scale: str
    standardisation: tuple[FloatArray, FloatArray]
    #: Forward-filtered path — the state at ``t`` from data through ``t`` only.
    #: This is the decoding to hand anything that measures forward skill.
    filtered_states: pd.Series = field(default_factory=lambda: pd.Series(dtype="int64"))
    filtered_posteriors: FloatArray = field(
        default_factory=lambda: np.zeros((0, 0), dtype=np.float64)
    )


def label_states(
    mean_returns: Sequence[float] | FloatArray,
    mean_volatility: Sequence[float] | FloatArray,
) -> dict[int, str]:
    """Map state ids to ``bull`` / ``bear`` / ``neutral`` labels.

    Paper §3.5.1: the highest-mean-return state is *bull*; the lowest-mean-return
    state (typically also the highest-volatility one) is *bear*; everything else
    is the sideways state Prism calls *neutral*. Ties on return are broken by
    volatility — lower volatility wins the bull label, higher volatility takes
    the bear label — so the mapping is total and deterministic.
    """
    returns = np.asarray(mean_returns, dtype=np.float64)
    volatility = np.asarray(mean_volatility, dtype=np.float64)
    n_states = returns.shape[0]
    if volatility.shape[0] != n_states:
        raise ValueError("mean_returns and mean_volatility must have the same length")
    if n_states == 0:
        return {}

    order_bull = sorted(range(n_states), key=lambda i: (-returns[i], volatility[i]))
    order_bear = sorted(range(n_states), key=lambda i: (returns[i], -volatility[i]))
    bull = order_bull[0]
    bear = order_bear[0]
    if bear == bull:  # single state
        return {bull: "bull"}

    labels: dict[int, str] = {bull: "bull", bear: "bear"}
    rest = [state for state in range(n_states) if state not in labels]
    for position, state in enumerate(sorted(rest, key=lambda i: -returns[i])):
        labels[state] = "neutral" if position == 0 else f"neutral_{position + 1}"
    return labels


def fit_regime_model(
    close: pd.Series,
    *,
    n_states: int = 3,
    train_window_days: int = DEFAULT_TRAIN_WINDOW_DAYS,
    window: int = 10,
    feature_scale: str = "relative",
    seed: int = _DEFAULT_SEED,
    n_iter: int = 200,
    tol: float = 1e-5,
) -> RegimeFit:
    """Fit the paper's HMM on the most recent ``train_window_days`` observations.

    The two features live on wildly different scales (returns ~1e-2, the
    volatility MSE ~1e-4 or ~1e1 depending on ``feature_scale``), so they are
    z-scored before the EM fit purely for numerical conditioning; every reported
    statistic is computed back in raw feature units.
    """
    features = regime_features(close, window=window, feature_scale=feature_scale)
    if train_window_days > 0:
        features = features.iloc[-train_window_days:]
    if features.shape[0] < max(n_states * 20, 60):
        raise ValueError(
            f"need at least {max(n_states * 20, 60)} observations to fit a "
            f"{n_states}-state regime model, got {features.shape[0]}"
        )

    raw = features.to_numpy(dtype=np.float64)
    centre = raw.mean(axis=0)
    spread = raw.std(axis=0, ddof=0)
    spread = np.where(spread > 0, spread, 1.0)
    standardised = (raw - centre) / spread

    model = fit_gaussian_hmm(
        standardised, n_states=n_states, n_iter=n_iter, tol=tol, seed=seed
    )
    path = viterbi(model, standardised)
    posteriors = posterior_states(model, standardised)

    mean_returns = np.array(
        [
            float(raw[path == state, 0].mean()) if int(np.sum(path == state)) else float("nan")
            for state in range(n_states)
        ]
    )
    mean_vol = np.array(
        [
            float(raw[path == state, 1].mean()) if int(np.sum(path == state)) else float("nan")
            for state in range(n_states)
        ]
    )
    # A state that the Viterbi path never visits still has HMM emission means;
    # fall back to those (de-standardised) so labelling stays total.
    for state in range(n_states):
        if not math.isfinite(mean_returns[state]):
            mean_returns[state] = float(model.means[state, 0] * spread[0] + centre[0])
        if not math.isfinite(mean_vol[state]):
            mean_vol[state] = float(model.means[state, 1] * spread[1] + centre[1])

    labels = label_states(mean_returns, mean_vol)
    states = pd.Series(path, index=features.index, name="state")
    # Forward-filtered decoding alongside the smoothed one: day t's label from
    # data through t only, for anything that measures forward behaviour.
    filtered_probabilities = filtered_posteriors(model, standardised)
    filtered_path = filtered_states(model, standardised)
    return RegimeFit(
        model=model,
        features=features,
        states=states,
        posteriors=posteriors,
        labels=labels,
        feature_scale=feature_scale,
        standardisation=(centre, spread),
        filtered_states=pd.Series(
            filtered_path, index=features.index, name="filtered_state"
        ),
        filtered_posteriors=filtered_probabilities,
    )


def regime_state_series(fit: RegimeFit) -> pd.Series:
    """Smoothed (Viterbi) regime labels indexed by date.

    Descriptive only: the label at ``t`` uses the whole training window, so it
    depends on data after ``t``. Use :func:`regime_filtered_state_series` for
    anything predictive.
    """
    return fit.states.map(lambda state: fit.labels.get(int(state), f"state_{int(state)}"))


def regime_filtered_state_series(fit: RegimeFit) -> pd.Series:
    """Forward-filtered regime labels: day ``t`` from data through ``t`` only."""
    if fit.filtered_states is None or fit.filtered_states.empty:
        return regime_state_series(fit)
    return fit.filtered_states.map(
        lambda state: fit.labels.get(int(state), f"state_{int(state)}")
    )


def ticker_stats_by_regime(
    ticker_close: pd.Series,
    regime_labels: pd.Series,
    *,
    min_observations: int = 10,
    forward: bool = True,
) -> dict[str, Any]:
    """Per-regime distribution of a ticker's daily returns.

    ``regime_labels`` is a date-indexed series of regime labels (see
    :func:`regime_filtered_state_series`). Returns one entry per label present,
    each with ``mean_daily``, ``std_daily``, ``sharpe`` (annualised, zero
    risk-free), ``n``, ``hit_rate``, ``total_return`` and ``max_drawdown``.
    Labels with fewer than ``min_observations`` aligned days report ``null``
    statistics and a ``reason``.

    With ``forward=True`` (the default) the return series is shifted one day
    forward relative to the labels, so each entry answers "what does the ticker
    do *after* a day classified as X". The contemporaneous join it replaces was a
    same-day accounting identity — the SPY return that put day ``t`` in the bear
    state is the same day whose ticker return was being averaged — and it flipped
    sign against the forward statistic it was consumed as.
    """
    prices = pd.to_numeric(pd.Series(ticker_close), errors="coerce").dropna()
    returns = prices.pct_change().dropna()
    if forward:
        returns = returns.shift(-1).dropna()
    aligned = pd.DataFrame({"ret": returns}).join(
        pd.DataFrame({"label": regime_labels}), how="inner"
    )
    out: dict[str, Any] = {}
    for label in sorted({str(value) for value in pd.Series(regime_labels).dropna().unique()}):
        subset = aligned.loc[aligned["label"] == label, "ret"].astype(float)
        n = int(subset.shape[0])
        if n < min_observations:
            out[label] = {
                "mean_daily": None,
                "std_daily": None,
                "sharpe": None,
                "n": n,
                "hit_rate": None,
                "total_return": None,
                "max_drawdown": None,
                "reason": f"only {n} aligned days (< {min_observations})",
            }
            continue
        values = subset.to_numpy(dtype=np.float64)
        mean = float(np.mean(values))
        std = float(np.std(values, ddof=1)) if n > 1 else 0.0
        equity = np.cumprod(1.0 + values)
        peak = np.maximum.accumulate(equity)
        drawdown = float(np.min(equity / peak - 1.0)) if n else 0.0
        out[label] = {
            "mean_daily": mean,
            "std_daily": std,
            "sharpe": float(mean / std * math.sqrt(_TRADING_DAYS)) if std > 0 else None,
            "n": n,
            "hit_rate": float(np.mean(values > 0.0)),
            "total_return": float(equity[-1] - 1.0),
            "max_drawdown": drawdown,
            "annualized_return": float(mean * _TRADING_DAYS),
            "annualized_volatility": float(std * math.sqrt(_TRADING_DAYS)),
            "alignment": "next_day" if forward else "same_day",
        }
    return out


def _days_in_regime(states: pd.Series) -> int:
    values = states.to_numpy()
    if values.size == 0:
        return 0
    current = values[-1]
    count = 0
    for value in values[::-1]:
        if value != current:
            break
        count += 1
    return int(count)


def _monthly_history(states: pd.Series, labels: Mapping[int, str]) -> list[dict[str, Any]]:
    if states.empty:
        return []
    frame = states.to_frame("state")
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="coerce")
    frame = frame[frame.index.notna()]
    if frame.empty:
        return []
    history: list[dict[str, Any]] = []
    for _, group in frame.groupby(frame.index.to_period("M"), sort=True):
        if group.empty:
            continue
        # The real last trading day of the month, not the calendar month end.
        stamp = pd.Timestamp(group.index[-1])
        state = int(group["state"].iloc[-1])
        history.append(
            {
                "date": stamp.date().isoformat(),
                "state": state,
                "label": labels.get(state, f"state_{state}"),
            }
        )
    return history


def build_regimes(
    spy_close: pd.Series,
    ticker_close: pd.Series | None = None,
    *,
    trained_on: str = "SPY",
    n_states: int = 3,
    train_window_days: int = DEFAULT_TRAIN_WINDOW_DAYS,
    window: int = 10,
    feature_scale: str = "relative",
    seed: int = _DEFAULT_SEED,
) -> dict[str, Any]:
    """Build the packet's ``regimes`` section.

    Parameters
    ----------
    spy_close:
        Daily closes of the market proxy the HMM is trained on (SPY per the
        paper). At least ~2707 daily observations are wanted; fewer still works,
        the actual count is reported as ``n_observations``.
    ticker_close:
        Optional daily closes for the analysed ticker; when supplied the section
        carries ``ticker_by_regime`` statistics aligned to the decoded states.

    Returns
    -------
    A JSON-serialisable dict matching the ``regimes`` block of the packet
    contract. On failure every key is present with ``null`` values and ``error``
    carries the reason — the engine never dies because one section failed.
    """
    section: dict[str, Any] = {
        "trained_on": trained_on,
        "n_states": n_states,
        "features": list(FEATURE_NAMES),
        "feature_scale": feature_scale,
        "feature_definition": {
            "daily_return": "(close_t - close_{t-1}) / close_{t-1}",
            "vol_10d_mse": (
                "mean squared deviation of the last 10 closes from their 10-day moving average"
                + (" divided by the squared moving average" if feature_scale == "relative" else "")
            ),
            "source": "Wang, Lin & Mikhelson (2020), JRFM 13(311), section 3.1",
        },
        "train_window_days": train_window_days,
        "states": [],
        "transition_matrix": [],
        "stationary_distribution": [],
        "current": None,
        "ticker_by_regime": None,
        "history": [],
        "fitted_distributions": {},
        "error": None,
    }

    try:
        fit = fit_regime_model(
            spy_close,
            n_states=n_states,
            train_window_days=train_window_days,
            window=window,
            feature_scale=feature_scale,
            seed=seed,
        )
    except (ValueError, np.linalg.LinAlgError) as exc:
        section["error"] = str(exc)
        return section

    raw = fit.features.to_numpy(dtype=np.float64)
    path = fit.states.to_numpy()
    durations = expected_durations(fit.model.transition)
    stationary = stationary_distribution(fit.model.transition)
    label_series = regime_state_series(fit)
    filtered_series = regime_filtered_state_series(fit)

    states_payload: list[dict[str, Any]] = []
    fitted: dict[str, Any] = {}
    for state in range(n_states):
        mask = path == state
        count = int(np.sum(mask))
        returns = raw[mask, 0] if count else np.array([], dtype=np.float64)
        vols = raw[mask, 1] if count else np.array([], dtype=np.float64)
        label = fit.labels.get(state, f"state_{state}")
        mean_return = float(np.mean(returns)) if count else None
        mean_vol = float(np.mean(vols)) if count else None
        std_return = float(np.std(returns, ddof=1)) if count > 1 else None
        states_payload.append(
            {
                "id": state,
                "label": label,
                "mean_daily_return": mean_return,
                # `volatility` is the packet-contract key and carries the raw
                # `vol_10d_mse` *feature* mean, which in "relative" feature scale
                # is a dimensionless squared fraction (order 1e-5..1e-3), not a
                # volatility. `vol_feature_mean` names it for what it is and
                # `volatility_annualized` is the usable figure; nothing may print
                # `volatility` under a column headed "volatility".
                "volatility": mean_vol,
                "vol_feature_mean": mean_vol,
                "vol_feature_units": (
                    "mean squared deviation from the 10-day moving average"
                    + (
                        ", divided by the squared moving average"
                        if feature_scale == "relative"
                        else ""
                    )
                ),
                "volatility_annualized": (
                    float(std_return * math.sqrt(_TRADING_DAYS)) if std_return is not None else None
                ),
                "std_daily_return": std_return,
                "occupancy": float(count / raw.shape[0]) if raw.shape[0] else None,
                "stationary_occupancy": float(stationary[state]),
                "avg_duration_days": (
                    float(durations[state]) if math.isfinite(float(durations[state])) else None
                ),
                "n_days": count,
            }
        )
        fitted[label] = {
            "state": state,
            FEATURE_NAMES[0]: fit_distribution(returns),
            FEATURE_NAMES[1]: fit_distribution(vols),
        }

    section["states"] = states_payload
    section["transition_matrix"] = [
        [float(value) for value in row] for row in fit.model.transition
    ]
    section["stationary_distribution"] = [float(value) for value in stationary]
    section["fitted_distributions"] = fitted
    section["history"] = _monthly_history(fit.states, fit.labels)
    section["train_start"] = str(pd.Timestamp(fit.features.index[0]).date())
    section["train_end"] = str(pd.Timestamp(fit.features.index[-1]).date())
    section["n_observations"] = int(raw.shape[0])
    section["converged"] = bool(fit.model.converged)
    section["log_likelihood"] = float(fit.model.log_likelihood)
    section["n_iter_run"] = int(fit.model.n_iter_run)
    section["seed"] = int(seed)

    current_state = int(path[-1])
    current_label = fit.labels.get(current_state, f"state_{current_state}")
    latest_return = float(raw[-1, 0])
    latest_vol = float(raw[-1, 1])

    # Paper §3.5.2: compare the newest observation's density under each regime's
    # KS-selected fitted distributions, then normalise to a relative confidence.
    likelihoods: dict[str, float] = {}
    for label, entry in fitted.items():
        pdf_return = _fitted_pdf(entry[FEATURE_NAMES[0]], latest_return)
        pdf_vol = _fitted_pdf(entry[FEATURE_NAMES[1]], latest_vol)
        likelihoods[label] = float(pdf_return * pdf_vol)
    total_likelihood = float(sum(likelihoods.values()))
    relative = (
        {label: value / total_likelihood for label, value in likelihoods.items()}
        if total_likelihood > 0
        else dict.fromkeys(likelihoods, float("nan"))
    )
    pdf_return_current = _fitted_pdf(fitted[current_label][FEATURE_NAMES[0]], latest_return)
    pdf_vol_current = _fitted_pdf(fitted[current_label][FEATURE_NAMES[1]], latest_vol)
    switch_confidence = (
        float(relative[current_label])
        if total_likelihood > 0
        else float(fit.posteriors[-1, current_state])
    )

    section["current"] = {
        "state": current_state,
        "label": current_label,
        "date": str(pd.Timestamp(fit.features.index[-1]).date()),
        "posterior": [float(value) for value in fit.posteriors[-1]],
        "days_in_regime": _days_in_regime(fit.states),
        "switch_confidence": switch_confidence,
        "relative_regime_likelihood": {
            label: (float(value) if math.isfinite(value) else None)
            for label, value in relative.items()
        },
        "next_state_probabilities": {
            fit.labels.get(target, f"state_{target}"): float(
                fit.model.transition[current_state, target]
            )
            for target in range(n_states)
        },
        "latest_observation": {
            FEATURE_NAMES[0]: latest_return,
            FEATURE_NAMES[1]: latest_vol,
        },
        "paper_pdf_return": float(pdf_return_current),
        "paper_pdf_volatility": float(pdf_vol_current),
        "paper_threshold_met": bool(
            pdf_vol_current > _PAPER_PDF_VOL_THRESHOLD
            and pdf_return_current > _PAPER_PDF_RETURN_THRESHOLD
        ),
        "paper_thresholds": {
            "volatility_pdf": _PAPER_PDF_VOL_THRESHOLD,
            "return_pdf": _PAPER_PDF_RETURN_THRESHOLD,
            "note": (
                "Paper section 3.5.2 thresholds; the PDF values are density units and "
                "therefore scale dependent, so relative_regime_likelihood is the "
                "comparison Prism actually uses."
            ),
        },
    }

    if ticker_close is not None:
        try:
            # Predictive block: filtered labels (no look-ahead in the decoding)
            # crossed with the *next* day's return. This is the block the scenario
            # mixture reads as E[r | state].
            section["ticker_by_regime"] = ticker_stats_by_regime(
                ticker_close, filtered_series, forward=True
            )
            section["ticker_by_regime_basis"] = (
                "forward-filtered regime label at day t (data through t only) crossed "
                "with the ticker's day t+1 return"
            )
            # Descriptive block: the smoothed same-day view, kept because it is
            # what "the market fell on the days the model calls bear" means — but
            # named so it can never be read as a forward expectation.
            section["ticker_by_regime_contemporaneous"] = ticker_stats_by_regime(
                ticker_close, label_series, forward=False
            )
        except (ValueError, KeyError) as exc:  # pragma: no cover - defensive
            section["ticker_by_regime"] = None
            section["ticker_by_regime_error"] = str(exc)
    return section
