"""Exposure: one regularised regression that says what a stock *is* (SPEC 5.1).

APT/Fama-French in one place. Instead of rolling betas, cosine similarity, a
covariance matrix and a PCA all describing the same thing, Situate runs a single
EWMA-weighted **ridge** of the ticker's monthly log returns on a tradable basket:

    [ SPY, matched sector ETF, most-related industry ETF, IWM-SPY, DXY, FXY,
      WTI, GLD, delta 10y yield, delta HY OAS, delta VIX ]

and, separately, an ordinary least squares on the Ken French monthly factors for
a *named-factor* read (MKT/SMB/HML/RMW/CMA/MOM) with t-statistics.

Method notes
------------
* **Closed form.** The ridge is ``(X'WX + lambda I)^-1 X'W y`` on column-
  standardised regressors, so lambda penalises every leg comparably; betas are
  mapped back to the raw scale before reporting.
* **EWMA weights.** Monthly weights decay with a 24-month half-life so recent
  regime matters more, without throwing away history.
* **lambda by leave-one-year-out CV.** Each calendar year is held out in turn
  (uniform weights, so the choice generalises across regimes rather than tracking
  recency); the lambda with the lowest pooled out-of-fold error wins.
* **Bootstrap SE.** Standard errors come from resampling months with replacement
  and refitting at the chosen lambda.
* **beta_path / change.** Betas are re-estimated at each recent month-end;
  ``change_6m`` / ``change_12m`` are the single difference of the beta level over
  6 and 12 month-ends. Nothing is differentiated more than once.

The whole module is a pure function of frames, so the lookahead test recomputes
after masking data after ``t`` and gets identical betas.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

from app.situate.contract import MODULE_VERSIONS, MONTHS_PER_YEAR
from app.situate.panel import Panel

FloatArray = npt.NDArray[np.float64]

__all__ = [
    "ExposureError",
    "RidgeFit",
    "build_basket",
    "build_exposure",
    "build_exposure_section",
    "ewma_weights",
    "loo_year_cv_lambda",
    "ridge_fit",
]

DEFAULT_HALF_LIFE_MONTHS = 24
DEFAULT_MIN_MONTHS = 36
DEFAULT_N_BOOT = 400
DEFAULT_SEED = 20260905
#: Standardised-scale ridge penalty grid searched by LOO-year CV.
DEFAULT_LAMBDAS: tuple[float, ...] = tuple(
    float(x) for x in np.logspace(-4.0, 2.0, 13)
)
#: Named factors regressed in the Ken French OLS view.
FACTOR_VIEW_NAMES: tuple[str, ...] = ("MKT", "SMB", "HML", "RMW", "CMA", "MOM")

#: Basket legs sourced from tradable ETF monthly log returns, in report order.
#: ``key`` is the column name in the packet; ``symbol`` the panel symbol.
_ETF_LEGS: tuple[tuple[str, str], ...] = (
    ("SPY", "SPY"),
    ("DXY", "UUP"),
    ("FXY", "FXY"),
    ("WTI", "USO"),
    ("GLD", "GLD"),
)
#: Basket legs sourced from month-end FRED level *differences*, in report order.
_MACRO_LEGS: tuple[tuple[str, str], ...] = (
    ("D_DGS10", "DGS10"),
    ("D_HY_OAS", "BAMLH0A0HYM2"),
    ("D_VIX", "VIXCLS"),
)


class ExposureError(RuntimeError):
    """Not enough aligned history to estimate exposures honestly."""


@dataclass(frozen=True)
class RidgeFit:
    """One EWMA-ridge fit on the raw (un-standardised) scale."""

    names: tuple[str, ...]
    betas: FloatArray
    alpha: float
    residuals: FloatArray
    r2: float
    lam: float

    def beta_map(self) -> dict[str, float]:
        return {name: float(b) for name, b in zip(self.names, self.betas, strict=True)}


# --------------------------------------------------------------------------
# Weights and the closed-form ridge
# --------------------------------------------------------------------------


def ewma_weights(n: int, *, half_life: float = DEFAULT_HALF_LIFE_MONTHS) -> FloatArray:
    """Exponentially-decaying weights for ``n`` months, newest last, sum 1.

    ``weight[i] = 0.5 ** (age / half_life)`` where ``age`` is months before the
    most recent observation (age 0 for the last row).
    """
    if n <= 0:
        return np.zeros(0, dtype=np.float64)
    ages = np.arange(n - 1, -1, -1, dtype=np.float64)
    weights = np.power(0.5, ages / float(half_life))
    total = float(weights.sum())
    return weights / total if total > 0 else np.full(n, 1.0 / n)


def _standardise(
    x: FloatArray, weights: FloatArray
) -> tuple[FloatArray, FloatArray, FloatArray]:
    """Weighted centre-and-scale of ``x``; returns ``(xs, mean, std)``."""
    w = weights / weights.sum()
    mean = (w[:, None] * x).sum(axis=0)
    centred = x - mean[None, :]
    var = (w[:, None] * centred**2).sum(axis=0)
    std = np.sqrt(var)
    std = np.where(std > 1e-12, std, 1.0)
    return centred / std[None, :], mean, std


def _solve_ridge(
    xs: FloatArray, yc: FloatArray, weights: FloatArray, lam: float
) -> FloatArray:
    """Closed-form ``(Xs' W Xs + lambda I)^-1 Xs' W yc`` on standardised inputs."""
    k = xs.shape[1]
    w = weights
    xtw = xs.T * w[None, :]
    gram = xtw @ xs + lam * np.eye(k, dtype=np.float64)
    rhs = xtw @ yc
    try:
        beta = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:  # pragma: no cover - lambda I makes this rare
        beta = np.linalg.pinv(gram) @ rhs
    return np.asarray(beta, dtype=np.float64)


def ridge_fit(
    x: FloatArray,
    y: FloatArray,
    *,
    names: tuple[str, ...],
    weights: FloatArray | None = None,
    lam: float = 1.0,
) -> RidgeFit:
    """Fit a weighted ridge on the raw scale via standardised solve + back-map."""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64).reshape(-1)
    n = x.shape[0]
    if weights is None:
        weights = np.full(n, 1.0 / n, dtype=np.float64)
    w = np.asarray(weights, dtype=np.float64)
    w = w / w.sum()

    xs, xmean, xstd = _standardise(x, w)
    ymean = float((w * y).sum())
    yc = y - ymean

    beta_std = _solve_ridge(xs, yc, w, lam)
    betas = beta_std / xstd
    alpha = ymean - float(np.dot(betas, xmean))

    fitted = x @ betas + alpha
    residuals = y - fitted
    sse = float(np.sum(residuals**2))
    sst = float(np.sum((y - float(np.mean(y))) ** 2))
    r2 = 1.0 - sse / sst if sst > 0 else float("nan")
    return RidgeFit(
        names=names,
        betas=np.asarray(betas, dtype=np.float64),
        alpha=alpha,
        residuals=np.asarray(residuals, dtype=np.float64),
        r2=r2,
        lam=float(lam),
    )


def loo_year_cv_lambda(
    x: FloatArray,
    y: FloatArray,
    years: npt.NDArray[np.int64],
    *,
    lambdas: tuple[float, ...] = DEFAULT_LAMBDAS,
) -> tuple[float, dict[float, float]]:
    """Pick lambda by leave-one-calendar-year-out CV (uniform weights).

    Returns ``(best_lambda, {lambda: pooled_sse})``. Folds with too few training
    rows are skipped; if no fold is usable the middle of the grid is returned.
    """
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64).reshape(-1)
    unique_years = np.unique(years)
    scores: dict[float, float] = {}
    k = x.shape[1]

    if unique_years.shape[0] < 2:
        # Not enough distinct years to hold one out; fall back to a light penalty.
        mid = lambdas[len(lambdas) // 2]
        return mid, {mid: float("nan")}

    for lam in lambdas:
        total = 0.0
        used = 0
        for held in unique_years:
            test_mask = years == held
            train_mask = ~test_mask
            if int(train_mask.sum()) < max(k + 2, 12) or int(test_mask.sum()) == 0:
                continue
            xtr, ytr = x[train_mask], y[train_mask]
            w = np.full(xtr.shape[0], 1.0 / xtr.shape[0], dtype=np.float64)
            xs, xmean, xstd = _standardise(xtr, w)
            ymean = float(ytr.mean())
            beta_std = _solve_ridge(xs, ytr - ymean, w, lam)
            betas = beta_std / xstd
            alpha = ymean - float(np.dot(betas, xmean))
            pred = x[test_mask] @ betas + alpha
            total += float(np.sum((y[test_mask] - pred) ** 2))
            used += 1
        scores[lam] = total if used > 0 else float("inf")

    finite = {lam: score for lam, score in scores.items() if math.isfinite(score)}
    if not finite:
        mid = lambdas[len(lambdas) // 2]
        return mid, scores
    best = min(finite, key=lambda lam: finite[lam])
    return best, scores


def _bootstrap_se(
    x: FloatArray,
    y: FloatArray,
    weights: FloatArray,
    *,
    names: tuple[str, ...],
    lam: float,
    n_boot: int,
    seed: int,
) -> dict[str, float | None]:
    """Bootstrap SE of each beta by resampling months with replacement."""
    n = x.shape[0]
    if n < 8 or n_boot <= 0:
        return dict.fromkeys(names, None)
    rng = np.random.default_rng(seed)
    draws = np.empty((n_boot, x.shape[1]), dtype=np.float64)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        w = weights[idx]
        fit = ridge_fit(x[idx], y[idx], names=names, weights=w, lam=lam)
        draws[b] = fit.betas
    se = draws.std(axis=0, ddof=1)
    return {
        name: (float(value) if math.isfinite(float(value)) else None)
        for name, value in zip(names, se, strict=True)
    }


# --------------------------------------------------------------------------
# Basket construction
# --------------------------------------------------------------------------


def build_basket(
    panel: Panel,
    macro_monthly: pd.DataFrame | None,
    *,
    sector_etf: str | None = None,
    industry_etf: str | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Assemble the monthly basket-return frame from a panel + FRED levels.

    ETF legs are monthly log returns; the sector and industry legs are named by
    their ETF symbol. The rates/credit/vol legs are the single first difference of
    month-end FRED levels (never differentiated more than once). Returns the frame
    plus a ``{column: source}`` map and a list of legs that could not be built.
    """
    columns: dict[str, pd.Series] = {}
    sources: dict[str, str] = {}
    dropped: list[str] = []

    def _logret(symbol: str) -> pd.Series | None:
        series = panel.monthly_log_return(symbol)
        return series if not series.empty else None

    # SPY / DXY / FXY / WTI / GLD.
    for key, symbol in _ETF_LEGS:
        series = _logret(symbol)
        if series is not None:
            columns[key] = series
            sources[key] = f"{symbol} monthly log return"
        else:
            dropped.append(f"{key} ({symbol}: no monthly returns)")

    # Sector and industry ETFs, named by their own symbol.
    for role, etf in (("sector", sector_etf), ("industry", industry_etf)):
        if not etf:
            continue
        etf = str(etf).upper()
        if etf in columns:  # already present (e.g. SPY doubling as sector)
            continue
        series = _logret(etf)
        if series is not None:
            columns[etf] = series
            sources[etf] = f"{etf} monthly log return ({role} ETF)"
        else:
            dropped.append(f"{etf} ({role} ETF: no monthly returns)")

    # Size spread IWM - SPY.
    iwm = _logret("IWM")
    spy = _logret("SPY")
    if iwm is not None and spy is not None:
        columns["IWM_SPY"] = (iwm - spy).dropna()
        sources["IWM_SPY"] = "IWM minus SPY monthly log return (size spread)"
    else:
        dropped.append("IWM_SPY (need both IWM and SPY monthly returns)")

    # Rates / credit / vol level differences.
    if macro_monthly is not None and not macro_monthly.empty:
        for key, series_id in _MACRO_LEGS:
            if series_id not in macro_monthly.columns:
                dropped.append(f"{key} ({series_id}: not in macro panel)")
                continue
            diff = macro_monthly[series_id].astype(float).diff().dropna()
            if diff.empty:
                dropped.append(f"{key} ({series_id}: no month-over-month changes)")
                continue
            columns[key] = diff
            sources[key] = f"month-over-month change in FRED {series_id}"
    else:
        for key, series_id in _MACRO_LEGS:
            dropped.append(f"{key} ({series_id}: macro panel unavailable)")

    if not columns:
        return pd.DataFrame(), {"sources": sources, "dropped": dropped}
    frame = pd.DataFrame(columns).sort_index()
    return frame, {"sources": sources, "dropped": dropped}


# --------------------------------------------------------------------------
# Named-factor OLS view (Ken French)
# --------------------------------------------------------------------------


def _factor_view(
    y_log: pd.Series, factors_monthly: pd.DataFrame | None
) -> dict[str, Any]:
    from app.prism.factors import ols_with_stats

    if factors_monthly is None or factors_monthly.empty:
        return {
            "alpha_annual": None,
            "loadings": {},
            "t_stats": {},
            "r2": None,
            "n": 0,
            "error": "Ken French monthly factors unavailable",
        }
    names = [n for n in FACTOR_VIEW_NAMES if n in factors_monthly.columns]
    if not names:
        return {
            "alpha_annual": None,
            "loadings": {},
            "t_stats": {},
            "r2": None,
            "n": 0,
            "error": "factor frame carries none of the expected factor columns",
        }
    rf = (
        factors_monthly["RF"]
        if "RF" in factors_monthly.columns
        else pd.Series(0.0, index=factors_monthly.index)
    )
    aligned = pd.concat(
        [y_log.rename("y"), rf.rename("RF"), factors_monthly[names]], axis=1
    ).dropna()
    min_obs = max(len(names) * 3, 24)
    if aligned.shape[0] < min_obs:
        return {
            "alpha_annual": None,
            "loadings": dict.fromkeys(names),
            "t_stats": dict.fromkeys(names),
            "r2": None,
            "n": int(aligned.shape[0]),
            "error": f"need at least {min_obs} aligned months, got {aligned.shape[0]}",
        }
    excess = (aligned["y"] - aligned["RF"]).to_numpy(dtype=np.float64)
    design = aligned[names].to_numpy(dtype=np.float64)
    result = ols_with_stats(excess, design, feature_names=names)
    return {
        "alpha_annual": float(result["alpha"] * MONTHS_PER_YEAR),
        "loadings": result["betas"],
        "t_stats": {k: v for k, v in result["t_stats"].items() if k != "alpha"},
        "alpha_t_stat": result["t_stats"].get("alpha"),
        "r2": result["r2"],
        "n": result["n"],
        "start": str(pd.Timestamp(aligned.index[0]).date()),
        "end": str(pd.Timestamp(aligned.index[-1]).date()),
        "error": None,
    }


# --------------------------------------------------------------------------
# beta path
# --------------------------------------------------------------------------


def _beta_path(
    aligned: pd.DataFrame,
    names: tuple[str, ...],
    *,
    lam: float,
    half_life: float,
    window_months: int,
    min_months: int,
    n_points: int,
) -> dict[str, list[dict[str, Any]]]:
    """Re-estimate betas at each of the last ``n_points`` month-ends."""
    path: dict[str, list[dict[str, Any]]] = {name: [] for name in names}
    total = aligned.shape[0]
    if total < min_months:
        return path
    # Month-end indices at which to re-fit: the last n_points rows with enough
    # trailing history.
    start_row = max(min_months, total - n_points)
    for end_row in range(start_row, total + 1):
        window = aligned.iloc[max(0, end_row - window_months) : end_row]
        if window.shape[0] < min_months:
            continue
        x = window[list(names)].to_numpy(dtype=np.float64)
        y = window["y"].to_numpy(dtype=np.float64)
        weights = ewma_weights(window.shape[0], half_life=half_life)
        fit = ridge_fit(x, y, names=names, weights=weights, lam=lam)
        as_of = str(pd.Timestamp(window.index[-1]).date())
        for name, beta in fit.beta_map().items():
            path[name].append({"date": as_of, "beta": float(beta)})
    return path


def _aligned_months(y_log: pd.Series, basket: pd.DataFrame, cols: list[str]) -> int:
    if not cols:
        return 0
    return pd.concat([y_log, basket[cols]], axis=1).dropna().shape[0]


def _prune_basket(
    y_log: pd.Series,
    basket: pd.DataFrame,
    *,
    target_months: int,
    keep_min_cols: int = 2,
) -> tuple[list[str], list[str]]:
    """Greedily drop the legs that truncate the joint sample the most.

    One basket leg with short history (a delisted ETF, a short cache row, thin
    Massive coverage) would otherwise collapse the whole inner-join. While the
    aligned sample is below ``target_months`` and more than ``keep_min_cols``
    legs remain, drop the single leg whose removal most extends the sample.
    Returns ``(kept_columns, dropped_columns)``; nothing is fabricated — dropped
    legs are recorded, not padded.
    """
    cols = [str(c) for c in basket.columns]
    dropped: list[str] = []
    current = _aligned_months(y_log, basket, cols)
    while current < target_months and len(cols) > keep_min_cols:
        best_col: str | None = None
        best_len = current
        for candidate in cols:
            remaining = [c for c in cols if c != candidate]
            length = _aligned_months(y_log, basket, remaining)
            if length > best_len:
                best_len = length
                best_col = candidate
        if best_col is None:  # no single drop improves the sample
            break
        cols.remove(best_col)
        dropped.append(best_col)
        current = best_len
    return cols, dropped


def _change_over(path_points: list[dict[str, Any]], steps: int) -> float | None:
    """Single difference of the beta level over ``steps`` month-ends."""
    if len(path_points) <= steps:
        return None
    latest = path_points[-1]["beta"]
    prior = path_points[-1 - steps]["beta"]
    if latest is None or prior is None:
        return None
    return float(latest - prior)


# --------------------------------------------------------------------------
# Top-level build
# --------------------------------------------------------------------------


def build_exposure(
    y_log: pd.Series,
    basket: pd.DataFrame,
    *,
    factors_monthly: pd.DataFrame | None = None,
    half_life: float = DEFAULT_HALF_LIFE_MONTHS,
    lambdas: tuple[float, ...] = DEFAULT_LAMBDAS,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    min_months: int = DEFAULT_MIN_MONTHS,
    beta_path_points: int = 24,
    beta_path_window: int = 60,
) -> dict[str, Any]:
    """Build the packet's ``exposure`` section from a ticker return series + basket.

    ``y_log`` is the ticker's monthly log return series (month-end indexed);
    ``basket`` is the aligned monthly basket-return frame from :func:`build_basket`.
    Raises :class:`ExposureError` when fewer than ``min_months`` aligned months
    are available.
    """
    if basket is None or basket.empty:
        raise ExposureError("basket is empty; no exposures can be estimated")
    # A single short-history leg must not collapse the joint sample; prune the
    # legs that truncate it the most, keeping the rest at full length.
    target_months = max(min_months, 60)
    kept, pruned = _prune_basket(y_log, basket, target_months=target_months)
    basket = basket[kept]
    aligned = pd.concat([y_log.rename("y"), basket], axis=1).dropna()
    names = tuple(str(c) for c in basket.columns)
    if aligned.shape[0] < min_months:
        raise ExposureError(
            f"need at least {min_months} aligned months, got {aligned.shape[0]}"
        )

    x = aligned[list(names)].to_numpy(dtype=np.float64)
    y = aligned["y"].to_numpy(dtype=np.float64)
    years = np.asarray([ts.year for ts in aligned.index], dtype=np.int64)

    lam, cv_scores = loo_year_cv_lambda(x, y, years, lambdas=lambdas)
    weights = ewma_weights(aligned.shape[0], half_life=half_life)
    fit = ridge_fit(x, y, names=names, weights=weights, lam=lam)
    se = _bootstrap_se(
        x, y, weights, names=names, lam=lam, n_boot=n_boot, seed=seed
    )

    residual_series = pd.Series(fit.residuals, index=aligned.index)
    residual_vol_annual = (
        float(residual_series.std(ddof=1) * math.sqrt(MONTHS_PER_YEAR))
        if residual_series.shape[0] > 1
        else None
    )
    r2 = float(fit.r2) if math.isfinite(fit.r2) else None
    idiosyncratic_share = (1.0 - r2) if r2 is not None else None

    path = _beta_path(
        aligned,
        names,
        lam=lam,
        half_life=half_life,
        window_months=beta_path_window,
        min_months=min_months,
        n_points=beta_path_points,
    )
    change_6m = {name: _change_over(path.get(name, []), 6) for name in names}
    change_12m = {name: _change_over(path.get(name, []), 12) for name in names}

    factor_view = _factor_view(y_log, factors_monthly)

    section: dict[str, Any] = {
        "basket": list(names),
        "betas": fit.beta_map(),
        "se": se,
        "alpha_monthly": float(fit.alpha),
        "r2": r2,
        "idiosyncratic_share": idiosyncratic_share,
        "residual_vol_annual": residual_vol_annual,
        "factor": factor_view,
        "beta_path": path,
        "change_6m": change_6m,
        "change_12m": change_12m,
        "method": "ewma_ridge",
        "lambda_": float(lam),
        "half_life_months": int(half_life),
        "n_months": int(aligned.shape[0]),
        "start": str(pd.Timestamp(aligned.index[0]).date()),
        "end": str(pd.Timestamp(aligned.index[-1]).date()),
        "cv": {"lambdas": list(lambdas), "scores": {str(k): v for k, v in cv_scores.items()}},
        "pruned_legs": pruned,
        "version": MODULE_VERSIONS["exposure"],
        "notes": (
            [f"basket leg pruned to preserve sample (short history): {leg}" for leg in pruned]
        ),
    }
    return section


def build_exposure_section(
    panel: Panel,
    *,
    ticker: str,
    macro_monthly: pd.DataFrame | None = None,
    factors_monthly: pd.DataFrame | None = None,
    sector_etf: str | None = None,
    industry_etf: str | None = None,
    half_life: float = DEFAULT_HALF_LIFE_MONTHS,
    n_boot: int = DEFAULT_N_BOOT,
    seed: int = DEFAULT_SEED,
    min_months: int = DEFAULT_MIN_MONTHS,
) -> dict[str, Any]:
    """Engine entry point: build ``exposure`` from a loaded :class:`Panel`.

    Raises :class:`ExposureError` when the ticker or basket lack enough history;
    the engine catches it and records ``exposure_error``.
    """
    y_log = panel.monthly_log_return(ticker)
    if y_log.empty:
        raise ExposureError(f"{ticker}: no monthly returns in the panel")
    basket, basket_meta = build_basket(
        panel, macro_monthly, sector_etf=sector_etf, industry_etf=industry_etf
    )
    section = build_exposure(
        y_log,
        basket,
        factors_monthly=factors_monthly,
        half_life=half_life,
        n_boot=n_boot,
        seed=seed,
        min_months=min_months,
    )
    section["basket_sources"] = basket_meta.get("sources", {})
    if basket_meta.get("dropped"):
        section.setdefault("notes", []).extend(
            f"basket leg unavailable: {leg}" for leg in basket_meta["dropped"]
        )
    return section
