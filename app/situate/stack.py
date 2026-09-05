"""Cross-sectional forecast stack with honest validation gates (SPEC 5.7).

The stack is the one place Situate combines features into a directional view, and
it is guarded like a "confident liar". For a small, fixed, versioned feature set
it fits a per-horizon ridge **cross-sectionally** over a curated peer universe
(:mod:`app.situate.peers`) — the only way to get breadth from a single-name
engine (Grinold: ``IR ≈ IC·√breadth``) — and applies the fit to the target
ticker. The target is the forward ``h``-month **excess** return over the name's
sector ("industry") ETF.

Validation is strictly walk-forward:

* **expanding window, refit annually** — the model that predicts a month is fit
  only on data whose labels closed before that year began;
* **purge ``h`` months + embargo 1 month** — a training row's label window
  ``[m, m+h]`` must end at least one month before the test month, so no label
  overlaps the evaluation (López de Prado 2018 purged CV);
* **gates that must pass to publish**:
    1. out-of-sample IC (cross-sectional Spearman) ``> 0.03`` with a
       block-bootstrap 90% CI that excludes 0;
    2. a **deflated** Sharpe (Bailey & López de Prado 2014) of the
       long-top-quintile / short-bottom-quintile rule that is positive after
       accounting for the number of configurations tried (every config is logged);
    3. per-feature ablation — a feature is kept only if removing it *lowers* OOS
       IC; a feature whose removal raises IC is dropped.

If any gate fails the stack returns ``published=False`` with a reason and the
engine falls back to ``base_rates + implied``. That fallback is an acceptable
ship state, and on thin single-name breadth it is the *expected* one.

Feature availability is honest: option-implied width/skew have no historical
panel (snapshots are point-in-time only) and quality/value need point-in-time
fundamentals (:mod:`app.situate.fundamentals`, imported lazily). Any feature that
cannot be built across the walk-forward history is logged and excluded rather
than faked. Everything is walk-forward with no data after the evaluation date.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import norm, rankdata

try:  # S1 owns the canonical contract; degrade to the SPEC constant if absent.
    from app.situate.contract import HORIZONS as _CONTRACT_HORIZONS

    HORIZONS: tuple[int, ...] = tuple(_CONTRACT_HORIZONS)
except Exception:  # noqa: BLE001 - the contract module may not exist yet
    HORIZONS = (1, 2, 3, 6, 12, 18)

STACK_VERSION = "1.0.0"

#: Euler-Mascheroni constant, used in the deflated-Sharpe deflation benchmark.
EULER_GAMMA = 0.5772156649015329
#: Quantile levels reported for the stack's predictive distribution.
_QUANTILE_LEVELS: tuple[float, ...] = (0.05, 0.25, 0.50, 0.75, 0.95)
_QUANTILE_KEYS: tuple[str, ...] = ("q05", "q25", "q50", "q75", "q95")

#: The full candidate feature set (SPEC 5.7). Only the ones that can actually be
#: built across the walk-forward history are used; the rest are logged as absent.
CANDIDATE_FEATURES: tuple[str, ...] = (
    "mom_12_1",
    "rev_1m",
    "quality",
    "value",
    "vol_dummy",
    "trend_dummy",
)
#: Features always attempted from price alone; the rest need fundamentals.
PRICE_FEATURES: tuple[str, ...] = ("mom_12_1", "rev_1m", "vol_dummy", "trend_dummy")


@dataclass(frozen=True)
class StackConfig:
    """Knobs for the walk-forward fit and the publish gates."""

    horizons: tuple[int, ...] = HORIZONS
    lambdas: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0, 100.0)
    min_train_months: int = 36
    min_train_rows: int = 200
    min_cross_section: int = 8
    embargo_months: int = 1
    ic_gate: float = 0.03
    n_bootstrap: int = 1000
    block_months: int = 6
    ci_low: float = 0.05
    ci_high: float = 0.95
    quintile: float = 0.2
    seed: int = 7


# --------------------------------------------------------------------------
# Deflated Sharpe (Bailey & López de Prado 2014) — pure, unit-tested helpers.
# --------------------------------------------------------------------------


def sharpe_ratio(returns: np.ndarray) -> float:
    """Per-observation Sharpe = mean / stdev(ddof=1). ``nan`` when undefined."""
    arr = np.asarray(returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return float("nan")
    sd = float(arr.std(ddof=1))
    if sd <= 0.0:
        return float("nan")
    return float(arr.mean()) / sd


def _sample_skew(returns: np.ndarray) -> float:
    arr = np.asarray(returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 3:
        return 0.0
    m = arr.mean()
    sd = arr.std(ddof=0)
    if sd <= 0.0:
        return 0.0
    return float(np.mean(((arr - m) / sd) ** 3))


def _sample_kurtosis(returns: np.ndarray) -> float:
    """Non-excess kurtosis (a normal sample gives ~3.0)."""
    arr = np.asarray(returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    n = arr.size
    if n < 4:
        return 3.0
    m = arr.mean()
    sd = arr.std(ddof=0)
    if sd <= 0.0:
        return 3.0
    return float(np.mean(((arr - m) / sd) ** 4))


def probabilistic_sharpe_ratio(
    sr_hat: float, sr_star: float, n_obs: int, *, skew: float = 0.0, kurtosis: float = 3.0
) -> float:
    """Probability the true Sharpe exceeds ``sr_star`` (Bailey & LdP PSR).

    ``PSR = Φ( (SR̂ − SR*)·√(T−1) / √(1 − γ3·SR̂ + ((γ4−1)/4)·SR̂²) )`` where the
    Sharpes are per-observation, ``T`` is the number of returns, ``γ3`` the
    skewness and ``γ4`` the (non-excess) kurtosis.
    """
    if n_obs < 2 or not math.isfinite(sr_hat):
        return float("nan")
    denom_var = 1.0 - skew * sr_hat + ((kurtosis - 1.0) / 4.0) * sr_hat * sr_hat
    if denom_var <= 0.0:
        return float("nan")
    z = (sr_hat - sr_star) * math.sqrt(n_obs - 1) / math.sqrt(denom_var)
    return float(norm.cdf(z))


def expected_max_sharpe(var_trials: float, n_trials: int) -> float:
    """Expected maximum of ``n_trials`` independent null Sharpes (SR0 benchmark).

    ``SR0 = √V · [ (1−γ)·Z⁻¹(1 − 1/N) + γ·Z⁻¹(1 − 1/(N·e)) ]`` where ``V`` is the
    variance across the trials' Sharpe ratios, ``γ`` is Euler-Mascheroni and
    ``Z⁻¹`` the inverse standard-normal CDF. With one trial there is nothing to
    deflate, so ``SR0 = 0``.
    """
    if n_trials <= 1 or var_trials <= 0.0:
        return 0.0
    n = float(n_trials)
    term1 = (1.0 - EULER_GAMMA) * float(norm.ppf(1.0 - 1.0 / n))
    term2 = EULER_GAMMA * float(norm.ppf(1.0 - 1.0 / (n * math.e)))
    return math.sqrt(var_trials) * (term1 + term2)


def deflated_sharpe(
    returns: np.ndarray, *, n_trials: int, var_trials: float
) -> dict[str, float | int]:
    """Deflated Sharpe of a return series against ``n_trials`` configs tried.

    Returns the observed per-observation Sharpe, the deflation benchmark ``SR0``,
    the deflated excess ``SR̂ − SR0`` (the ``> 0`` publish criterion), and the
    deflated-Sharpe probability ``DSR = PSR(SR0)``.
    """
    arr = np.asarray(returns, dtype=float)
    arr = arr[np.isfinite(arr)]
    sr = sharpe_ratio(arr)
    sr0 = expected_max_sharpe(var_trials, n_trials)
    if not math.isfinite(sr):
        return {
            "sharpe": float("nan"),
            "sr0": sr0,
            "deflated_excess": float("nan"),
            "dsr_prob": float("nan"),
            "n_obs": int(arr.size),
            "n_trials": int(n_trials),
        }
    dsr = probabilistic_sharpe_ratio(
        sr, sr0, int(arr.size), skew=_sample_skew(arr), kurtosis=_sample_kurtosis(arr)
    )
    return {
        "sharpe": float(sr),
        "sr0": float(sr0),
        "deflated_excess": float(sr - sr0),
        "dsr_prob": float(dsr),
        "n_obs": int(arr.size),
        "n_trials": int(n_trials),
    }


# --------------------------------------------------------------------------
# Closed-form ridge and cross-sectional helpers.
# --------------------------------------------------------------------------


def ridge_fit(x: np.ndarray, y: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    """Closed-form ridge ``(XᵀX + λI)⁻¹Xᵀy`` via a standardised solve.

    Returns ``(betas, intercept)`` on the *raw* feature scale so the fit can be
    applied to un-standardised feature rows directly.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float).reshape(-1)
    n, k = x.shape
    xmean = x.mean(axis=0)
    xstd = x.std(axis=0, ddof=0)
    xstd = np.where(xstd > 1e-12, xstd, 1.0)
    xs = (x - xmean) / xstd
    ymean = float(y.mean())
    yc = y - ymean
    gram = xs.T @ xs + float(lam) * np.eye(k)
    rhs = xs.T @ yc
    try:
        beta_std = np.linalg.solve(gram, rhs)
    except np.linalg.LinAlgError:  # pragma: no cover - λI makes this rare
        beta_std = np.linalg.pinv(gram) @ rhs
    betas = beta_std / xstd
    intercept = ymean - float(np.dot(betas, xmean))
    return np.asarray(betas, dtype=float), intercept


def _loo_year_lambda(
    x: np.ndarray, y: np.ndarray, years: np.ndarray, lambdas: Sequence[float]
) -> float:
    """Pick λ by leave-one-calendar-year-out CV (pooled MSE). Middle on tie."""
    unique_years = np.unique(years)
    lam_list = list(lambdas)
    if unique_years.size < 2:
        return lam_list[len(lam_list) // 2]
    best_lam = lam_list[len(lam_list) // 2]
    best_score = math.inf
    k = x.shape[1]
    for lam in lam_list:
        total = 0.0
        used = 0
        for held in unique_years:
            test_mask = years == held
            train_mask = ~test_mask
            if int(train_mask.sum()) < max(k + 2, 20) or int(test_mask.sum()) == 0:
                continue
            betas, intercept = ridge_fit(x[train_mask], y[train_mask], lam)
            pred = x[test_mask] @ betas + intercept
            total += float(np.sum((y[test_mask] - pred) ** 2))
            used += 1
        if used == 0:
            continue
        if total < best_score:
            best_score = total
            best_lam = lam
    return best_lam


def spearman_ic(pred: np.ndarray, actual: np.ndarray) -> float:
    """Cross-sectional Spearman rank correlation (``nan`` when < 3 names)."""
    p = np.asarray(pred, dtype=float)
    a = np.asarray(actual, dtype=float)
    mask = np.isfinite(p) & np.isfinite(a)
    p, a = p[mask], a[mask]
    if p.size < 3:
        return float("nan")
    rp = rankdata(p)
    ra = rankdata(a)
    if rp.std() == 0.0 or ra.std() == 0.0:
        return float("nan")
    return float(np.corrcoef(rp, ra)[0, 1])


def cross_sectional_zscore(
    frame: pd.DataFrame, feature_cols: Sequence[str], *, date_col: str = "date"
) -> pd.DataFrame:
    """Z-score each feature within each date (cross-section), leaving NaNs as NaN."""
    out = frame.copy()
    for col in feature_cols:
        grouped = out.groupby(date_col)[col]
        mean = grouped.transform("mean")
        std = grouped.transform("std")
        std = std.where(std > 1e-12, other=np.nan)
        out[col] = (out[col] - mean) / std
        # A degenerate (zero-variance) cross-section becomes 0, not NaN.
        out[col] = out[col].where(std.notna(), other=0.0)
    return out


def _month_idx(dates: pd.Series) -> np.ndarray:
    """Integer month ordinal (``year*12 + month``) for purge/embargo math."""
    dt = pd.to_datetime(dates)
    return (dt.dt.year.to_numpy() * 12 + (dt.dt.month.to_numpy() - 1)).astype(np.int64)


def eligible_train_mask(
    train_month: np.ndarray, test_month: int, horizon: int, embargo: int
) -> np.ndarray:
    """Purge + embargo mask: a training label window must close before the test.

    A training row observed at ``m`` has its label realised at ``m + horizon``.
    It is eligible only when ``m + horizon <= test_month − embargo``, which purges
    the ``horizon`` months whose labels overlap the test month and embargoes one
    further month after them.
    """
    return (np.asarray(train_month, dtype=np.int64) + int(horizon)) <= (
        int(test_month) - int(embargo)
    )


def block_bootstrap_ci(
    per_date: pd.Series,
    *,
    block: int,
    n_boot: int,
    low: float,
    high: float,
    seed: int,
) -> tuple[float | None, float | None]:
    """Block-bootstrap CI of the mean of a per-date statistic (IC).

    Overlapping monthly ICs are autocorrelated, so we resample contiguous blocks
    of ``block`` months (circular) to preserve dependence, and take the ``low`` /
    ``high`` percentiles of the bootstrapped means.
    """
    values = per_date.dropna().to_numpy(dtype=float)
    n = values.size
    if n < 3:
        return None, None
    blk = max(1, min(int(block), n))
    n_blocks = int(math.ceil(n / blk))
    rng = np.random.default_rng(seed)
    means = np.empty(int(n_boot), dtype=float)
    for b in range(int(n_boot)):
        starts = rng.integers(0, n, size=n_blocks)
        idx = (starts[:, None] + np.arange(blk)[None, :]).reshape(-1) % n
        means[b] = values[idx][:n].mean()
    return float(np.quantile(means, low)), float(np.quantile(means, high))


# --------------------------------------------------------------------------
# Walk-forward out-of-sample engine (pure: operates on a long feature frame).
# --------------------------------------------------------------------------


def walk_forward_oos(
    frame: pd.DataFrame,
    feature_cols: Sequence[str],
    *,
    horizon: int,
    cfg: StackConfig,
    target_col: str,
    date_col: str = "date",
    symbol_col: str = "symbol",
) -> dict[str, Any]:
    """Expanding-window, annually-refit, purged/embargoed OOS predictions.

    ``frame`` is a long panel with one row per (date, symbol) carrying the
    cross-sectionally z-scored ``feature_cols`` and a realised ``target_col`` (the
    forward ``h``-month excess return). Returns the OOS prediction rows, the
    per-date cross-sectional IC series, the mean IC, and the quintile long/short
    monthly return series.
    """
    cols = list(feature_cols)
    work = frame[[date_col, symbol_col, target_col, *cols]].copy()
    work = work.dropna(subset=[target_col, *cols]).reset_index(drop=True)
    if work.empty:
        return _empty_oos()
    work["_m"] = _month_idx(work[date_col])
    work["_year"] = pd.to_datetime(work[date_col]).dt.year.to_numpy()

    test_months = np.sort(work["_m"].unique())
    x_all = work[cols].to_numpy(dtype=float)
    y_all = work[target_col].to_numpy(dtype=float)
    m_all = work["_m"].to_numpy(dtype=np.int64)
    yr_all = work["_year"].to_numpy(dtype=np.int64)

    oos_rows: list[dict[str, Any]] = []
    fitted_year: dict[int, tuple[np.ndarray, float] | None] = {}

    for test_m in test_months:
        year = int(test_m // 12)
        if year not in fitted_year:
            # Refit once per calendar year on data whose labels closed before the
            # earliest test month of that year (embargoed) — expanding window.
            year_test_months = test_months[(test_months // 12) == year]
            earliest = int(year_test_months.min())
            train_mask = eligible_train_mask(
                m_all, earliest, horizon, cfg.embargo_months
            )
            n_train_rows = int(train_mask.sum())
            n_train_months = int(np.unique(m_all[train_mask]).size)
            if (
                n_train_rows < cfg.min_train_rows
                or n_train_months < cfg.min_train_months
            ):
                fitted_year[year] = None
            else:
                xt, yt, yrt = x_all[train_mask], y_all[train_mask], yr_all[train_mask]
                lam = _loo_year_lambda(xt, yt, yrt, cfg.lambdas)
                fitted_year[year] = ridge_fit(xt, yt, lam)
        model = fitted_year[year]
        if model is None:
            continue
        betas, intercept = model
        test_mask = m_all == test_m
        if int(test_mask.sum()) < cfg.min_cross_section:
            continue
        xtest = x_all[test_mask]
        pred = xtest @ betas + intercept
        actual = y_all[test_mask]
        syms = work.loc[test_mask, symbol_col].to_numpy()
        the_date = work.loc[test_mask, date_col].iloc[0]
        for sym, p, a in zip(syms, pred, actual, strict=True):
            oos_rows.append(
                {
                    "date": the_date,
                    "_m": int(test_m),
                    "symbol": sym,
                    "pred": float(p),
                    "actual": float(a),
                }
            )

    if not oos_rows:
        return _empty_oos()

    oos = pd.DataFrame(oos_rows)
    per_date_ic = (
        oos.groupby("_m")
        .apply(
            lambda g: spearman_ic(g["pred"].to_numpy(), g["actual"].to_numpy()),
            include_groups=False,
        )
        .rename("ic")
    )
    ls_returns = (
        oos.groupby("_m")
        .apply(lambda g: _quintile_long_short(g, cfg.quintile), include_groups=False)
        .rename("ls")
        .dropna()
    )
    mean_ic = float(per_date_ic.dropna().mean()) if not per_date_ic.dropna().empty else float("nan")
    return {
        "oos": oos,
        "per_date_ic": per_date_ic,
        "mean_ic": mean_ic,
        "ls_returns": ls_returns,
        "n_test_months": int(per_date_ic.dropna().shape[0]),
        "n_oos_rows": int(oos.shape[0]),
    }


def _empty_oos() -> dict[str, Any]:
    return {
        "oos": pd.DataFrame(columns=["date", "_m", "symbol", "pred", "actual"]),
        "per_date_ic": pd.Series(dtype=float, name="ic"),
        "mean_ic": float("nan"),
        "ls_returns": pd.Series(dtype=float, name="ls"),
        "n_test_months": 0,
        "n_oos_rows": 0,
    }


def _quintile_long_short(group: pd.DataFrame, quintile: float) -> float:
    """Mean(top-quintile actual) − mean(bottom-quintile actual) by prediction."""
    g = group.dropna(subset=["pred", "actual"])
    n = g.shape[0]
    if n < 5:
        return float("nan")
    k = max(1, int(math.floor(n * quintile)))
    ordered = g.sort_values("pred")
    bottom = ordered.head(k)["actual"].mean()
    top = ordered.tail(k)["actual"].mean()
    return float(top - bottom)


# --------------------------------------------------------------------------
# Ablation, gates, prediction — the published stack section.
# --------------------------------------------------------------------------


def _resolve_features(
    frame: pd.DataFrame, candidates: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split candidates into those with real cross-sectional signal vs absent.

    A feature is *available* only if it is present, not entirely NaN, and has some
    variation; otherwise it is logged as absent rather than fed a constant.
    """
    available: list[str] = []
    absent: list[str] = []
    for col in candidates:
        if col not in frame.columns:
            absent.append(col)
            continue
        series = frame[col].dropna()
        if series.empty or float(series.std(ddof=0) or 0.0) <= 1e-12:
            absent.append(col)
        else:
            available.append(col)
    return available, absent


def run_stack_core(
    frame: pd.DataFrame,
    *,
    ticker: str,
    cfg: StackConfig,
    candidate_features: Sequence[str] = CANDIDATE_FEATURES,
    absent_features: Sequence[str] | None = None,
    absent_reasons: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Fit, validate, gate and (maybe) publish the stack from a prepared panel.

    ``frame`` is a long (date, symbol) panel carrying the candidate feature
    columns (raw scale) plus one ``target_h{h}`` column per horizon. This function
    z-scores features per date, runs the walk-forward OOS engine per horizon,
    ablates features, applies the publish gates and — if they pass — fits a final
    model on all observed labels and predicts the ticker's forward excess
    distribution. It is pure and deterministic, which is what the tests pin.
    """
    ticker = str(ticker).strip().upper()
    absent_log: dict[str, str] = dict(absent_reasons or {})
    for name in absent_features or ():
        absent_log.setdefault(name, "not built for the walk-forward panel")

    available, absent = _resolve_features(frame, candidate_features)
    for name in absent:
        absent_log.setdefault(name, "absent or constant across the panel")

    result: dict[str, Any] = {
        "version": STACK_VERSION,
        "published": False,
        "reason": None,
        "method": "cross_sectional_ridge_walk_forward",
        "universe_size": int(frame["symbol"].nunique()) if "symbol" in frame else 0,
        "n_rows": int(frame.shape[0]),
        "candidate_features": list(candidate_features),
        "features": list(available),
        "features_absent": absent_log,
        "configs_tried": 0,
        "by_horizon": {},
        "ablations": {},
        "gates": {},
    }

    if not available:
        result["reason"] = "no usable features across the walk-forward panel"
        return result
    if result["universe_size"] < cfg.min_cross_section:
        result["reason"] = (
            f"cross-section too thin: {result['universe_size']} names "
            f"< min {cfg.min_cross_section}"
        )
        return result

    zframe = cross_sectional_zscore(frame, available)

    trial_sharpes: list[float] = []

    def _record_trial(ls: pd.Series) -> None:
        sr = sharpe_ratio(ls.to_numpy())
        if math.isfinite(sr):
            trial_sharpes.append(sr)

    # Pass 1 — per horizon: full model + leave-one-feature-out ablation.
    per_horizon_full: dict[int, dict[str, Any]] = {}
    ablation_report: dict[str, dict[str, Any]] = {}
    for h in cfg.horizons:
        target_col = f"target_h{h}"
        if target_col not in zframe.columns:
            continue
        full = walk_forward_oos(zframe, available, horizon=h, cfg=cfg, target_col=target_col)
        _record_trial(full["ls_returns"])
        per_horizon_full[h] = full
        for feat in available:
            reduced = [f for f in available if f != feat]
            if not reduced:
                continue
            abl = walk_forward_oos(zframe, reduced, horizon=h, cfg=cfg, target_col=target_col)
            _record_trial(abl["ls_returns"])
            key = f"h{h}:-{feat}"
            ablation_report[key] = {
                "horizon": h,
                "dropped": feat,
                "ic_full": full["mean_ic"],
                "ic_without": abl["mean_ic"],
                "raises_ic": bool(
                    math.isfinite(abl["mean_ic"])
                    and math.isfinite(full["mean_ic"])
                    and abl["mean_ic"] > full["mean_ic"]
                ),
            }

    # Feature selection: drop a feature if removing it raises IC on a majority of
    # horizons where both ICs are finite (SPEC 5.7 ablation rule).
    kept: list[str] = []
    feature_votes: dict[str, dict[str, int]] = {f: {"drop": 0, "keep": 0} for f in available}
    for rep in ablation_report.values():
        feat = rep["dropped"]
        if not (math.isfinite(rep["ic_full"]) and math.isfinite(rep["ic_without"])):
            continue
        if rep["raises_ic"]:
            feature_votes[feat]["drop"] += 1
        else:
            feature_votes[feat]["keep"] += 1
    for feat in available:
        votes = feature_votes[feat]
        if votes["drop"] > votes["keep"]:
            absent_log.setdefault(feat, "dropped by ablation (removal raised OOS IC)")
        else:
            kept.append(feat)
    if not kept:
        kept = list(available)  # never end with an empty model
    result["features"] = kept
    result["ablations"] = ablation_report

    # Pass 2 — final model per horizon on the kept features, with gates.
    # Fit every horizon's final model and record its trial FIRST, so the deflated
    # Sharpe's trial variance V and trial count N are computed from the identical,
    # fully-populated set of configs (full + ablations + all finals). Computing V
    # before the loop while N grows inside it would deflate against a V and an N
    # that describe different config sets.
    finals: dict[int, dict[str, Any]] = {}
    for h in cfg.horizons:
        target_col = f"target_h{h}"
        if target_col not in zframe.columns:
            continue
        final = walk_forward_oos(zframe, kept, horizon=h, cfg=cfg, target_col=target_col)
        _record_trial(final["ls_returns"])
        finals[h] = final

    var_trials = (
        float(np.var(np.asarray(trial_sharpes), ddof=1)) if len(trial_sharpes) >= 2 else 0.0
    )
    n_trials = max(1, len(trial_sharpes))
    any_pass = False
    for h, final in finals.items():
        target_col = f"target_h{h}"
        ic = final["mean_ic"]
        ci_lo, ci_hi = block_bootstrap_ci(
            final["per_date_ic"],
            block=cfg.block_months,
            n_boot=cfg.n_bootstrap,
            low=cfg.ci_low,
            high=cfg.ci_high,
            seed=cfg.seed + h,
        )
        ds = deflated_sharpe(
            final["ls_returns"].to_numpy(),
            n_trials=n_trials,
            var_trials=var_trials,
        )
        ic_pass = bool(
            math.isfinite(ic) and ic > cfg.ic_gate and ci_lo is not None and ci_lo > 0.0
        )
        ds_pass = bool(math.isfinite(ds["deflated_excess"]) and ds["deflated_excess"] > 0.0)
        passed = bool(ic_pass and ds_pass)
        any_pass = any_pass or passed

        pred_block = _predict_ticker(
            zframe, kept, horizon=h, cfg=cfg, target_col=target_col, ticker=ticker
        )
        result["by_horizon"][str(h)] = {
            "oos_ic": ic if math.isfinite(ic) else None,
            "oos_ic_ci": [ci_lo, ci_hi],
            "n_test_months": final["n_test_months"],
            "n_oos_rows": final["n_oos_rows"],
            "deflated_sharpe": (
                ds["deflated_excess"] if math.isfinite(ds["deflated_excess"]) else None
            ),
            "deflated_sharpe_prob": ds["dsr_prob"] if math.isfinite(ds["dsr_prob"]) else None,
            "sharpe": ds["sharpe"] if math.isfinite(ds["sharpe"]) else None,
            "ic_pass": ic_pass,
            "deflated_sharpe_pass": ds_pass,
            "passed_gates": passed,
            "expected_excess": pred_block["expected_excess"],
            "quantiles": pred_block["quantiles"],
            "feature_contributions": pred_block["feature_contributions"],
        }

    result["configs_tried"] = int(n_trials)
    result["gates"] = {
        "ic_gate": cfg.ic_gate,
        "var_trials": var_trials,
        "n_trials": int(n_trials),
    }
    result["published"] = bool(any_pass)
    if not any_pass:
        result["reason"] = (
            "gates not met (OOS IC / deflated Sharpe); "
            "engine falls back to base_rates + implied"
        )
    return result


def _predict_ticker(
    zframe: pd.DataFrame,
    features: Sequence[str],
    *,
    horizon: int,
    cfg: StackConfig,
    target_col: str,
    ticker: str,
) -> dict[str, Any]:
    """Fit a final model on all observed labels and predict ``ticker``.

    The prediction row is the ticker's most recent cross-section (features known,
    label not yet realised). Quantiles are the point forecast plus the empirical
    quantiles of the OOS residuals; feature contributions are ``β·feature``.
    """
    empty = {
        "expected_excess": None,
        "quantiles": dict.fromkeys(_QUANTILE_KEYS),
        "feature_contributions": {},
    }
    cols = list(features)
    labelled = zframe.dropna(subset=[target_col, *cols])
    if labelled.empty:
        return empty
    x = labelled[cols].to_numpy(dtype=float)
    y = labelled[target_col].to_numpy(dtype=float)
    years = pd.to_datetime(labelled["date"]).dt.year.to_numpy()
    if x.shape[0] < cfg.min_train_rows:
        return empty
    lam = _loo_year_lambda(x, y, years, cfg.lambdas)
    betas, intercept = ridge_fit(x, y, lam)

    # OOS residuals for the predictive band.
    oos = walk_forward_oos(zframe, cols, horizon=horizon, cfg=cfg, target_col=target_col)
    resid = (oos["oos"]["actual"] - oos["oos"]["pred"]).to_numpy(dtype=float)
    resid = resid[np.isfinite(resid)]

    ticker_rows = zframe[zframe["symbol"] == ticker].dropna(subset=cols)
    if ticker_rows.empty:
        return empty
    latest = ticker_rows.sort_values("date").iloc[-1]
    feats = latest[cols].to_numpy(dtype=float)
    expected = float(np.dot(betas, feats) + intercept)
    contributions = {f: float(b * v) for f, b, v in zip(cols, betas, feats, strict=True)}

    quantiles: dict[str, float | None]
    if resid.size >= 10:
        quantiles = {
            key: float(expected + np.quantile(resid, level))
            for key, level in zip(_QUANTILE_KEYS, _QUANTILE_LEVELS, strict=True)
        }
    else:
        quantiles = dict.fromkeys(_QUANTILE_KEYS)
        quantiles["q50"] = expected
    return {
        "expected_excess": expected,
        "quantiles": quantiles,
        "feature_contributions": contributions,
    }


# --------------------------------------------------------------------------
# Live feature panel from a Situate Panel (price features + optional fundamentals).
# --------------------------------------------------------------------------


def _month_end_levels(close: pd.Series) -> pd.Series:
    series = pd.to_numeric(pd.Series(close), errors="coerce").dropna()
    series = series[series > 0]
    if series.empty:
        return series
    idx = pd.to_datetime(series.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_convert(None)
    series.index = pd.DatetimeIndex(idx).normalize()
    series = series[~series.index.duplicated(keep="last")].sort_index()
    return series.resample("ME").last().dropna()


def build_feature_panel(
    panel: Any,
    symbols: Sequence[str],
    *,
    etf_of: dict[str, str | None],
    horizons: tuple[int, ...] = HORIZONS,
    fundamentals: dict[str, dict[str, Any]] | None = None,
) -> tuple[pd.DataFrame, dict[str, str]]:
    """Build the long cross-sectional feature+target panel from a :class:`Panel`.

    Price features (12-1 momentum, 1-month reversal, vol/trend dummies) are built
    for every symbol; ``quality``/``value`` are filled only if ``fundamentals``
    supplies a point-in-time value keyed by symbol (else left absent — never
    faked). The target ``target_h{h}`` is the symbol's forward ``h``-month total
    return minus its sector ETF's over the same window. Returns the frame and a
    ``{feature: reason}`` map of features that could not be built.
    """
    from app.situate.base_rates import cell_series

    absent: dict[str, str] = {}
    etf_levels: dict[str, pd.Series] = {}
    rows: list[dict[str, Any]] = []

    for symbol in symbols:
        sym = str(symbol).strip().upper()
        close = panel.daily_close(sym) if hasattr(panel, "daily_close") else pd.Series(dtype=float)
        me = _month_end_levels(close)
        if me.shape[0] < 15:
            continue
        etf = (etf_of.get(sym) or "").upper() or None
        if etf is None:
            continue
        if etf not in etf_levels:
            etf_close = (
                panel.daily_close(etf)
                if hasattr(panel, "daily_close")
                else pd.Series(dtype=float)
            )
            etf_levels[etf] = _month_end_levels(etf_close)
        etf_me = etf_levels[etf]
        if etf_me.empty:
            continue

        # Cell (vol/trend) labels at month-end, point-in-time.
        labels = cell_series(close)
        cell_month = labels.resample("ME").last() if not labels.empty else pd.Series(dtype="object")

        # Fundamentals (optional, point-in-time scalars keyed by symbol).
        fund = (fundamentals or {}).get(sym) or {}
        quality = fund.get("quality")
        value = fund.get("value")

        months = me.index
        for i, m in enumerate(months):
            if i < 12:
                continue  # need 12 months of history for 12-1 momentum
            mom = float(me.iloc[i - 1] / me.iloc[i - 12] - 1.0)
            rev = float(me.iloc[i] / me.iloc[i - 1] - 1.0)
            cell = cell_month.reindex([m]).iloc[0] if not cell_month.empty else None
            vol_dummy = None
            trend_dummy = None
            if isinstance(cell, str):
                vol_dummy = 1.0 if cell.startswith("highvol") else 0.0
                trend_dummy = 1.0 if cell.endswith("up") else 0.0

            row: dict[str, Any] = {
                "date": m,
                "symbol": sym,
                "mom_12_1": mom,
                "rev_1m": rev,
                "vol_dummy": vol_dummy,
                "trend_dummy": trend_dummy,
                "quality": float(quality) if isinstance(quality, (int, float)) else np.nan,
                "value": float(value) if isinstance(value, (int, float)) else np.nan,
            }
            # Forward excess targets.
            for h in horizons:
                if i + h < len(months):
                    fwd_sym = float(me.iloc[i + h] / me.iloc[i] - 1.0)
                    etf_at = etf_me.reindex([months[i]]).iloc[0]
                    etf_fwd_at = etf_me.reindex([months[i + h]]).iloc[0]
                    if (
                        etf_at is not None
                        and etf_fwd_at is not None
                        and np.isfinite(etf_at)
                        and np.isfinite(etf_fwd_at)
                        and etf_at > 0
                    ):
                        fwd_etf = float(etf_fwd_at / etf_at - 1.0)
                        row[f"target_h{h}"] = fwd_sym - fwd_etf
                    else:
                        row[f"target_h{h}"] = np.nan
                else:
                    row[f"target_h{h}"] = np.nan
            rows.append(row)

    if not rows:
        return pd.DataFrame(), {"all": "no symbol had usable month-end history"}

    frame = pd.DataFrame(rows)
    if fundamentals is None or frame["quality"].dropna().empty:
        absent["quality"] = "no point-in-time fundamentals supplied (S3)"
    if fundamentals is None or frame["value"].dropna().empty:
        absent["value"] = "no point-in-time fundamentals supplied (S3)"
    return frame, absent


def build_stack(
    panel: Any,
    ticker: str,
    *,
    universe: Sequence[str] | None = None,
    etf_of: dict[str, str | None] | None = None,
    cfg: StackConfig | None = None,
    fundamentals: dict[str, dict[str, Any]] | None = None,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Build ``packet["stack"]`` from a loaded :class:`Panel` (SPEC 5.7).

    ``universe`` defaults to the ticker's curated sector peers; ``etf_of`` maps
    each symbol to its sector ETF (both from :mod:`app.situate.peers`). Options-
    implied width/skew are intentionally NOT features here — they have no
    walk-forward history — and that is logged. Returns ``published=False`` with a
    reason whenever breadth or the gates are insufficient.
    """
    from app.situate import peers as peers_mod

    ticker = str(ticker).strip().upper()
    cfg = cfg or StackConfig()
    if universe is None:
        universe = peers_mod.universe_for(ticker)
    universe = [str(s).strip().upper() for s in universe]
    if ticker not in universe:
        universe = [ticker, *universe]
    if etf_of is None:
        etf_of = peers_mod.etf_map(universe)

    frame, absent = build_feature_panel(
        panel, universe, etf_of=etf_of, horizons=cfg.horizons, fundamentals=fundamentals
    )
    absent_reasons = dict(absent)
    # Implied width/skew and revision/PEAD have no walk-forward panel; log honestly.
    _pit_only = "options snapshots are point-in-time only; no historical panel"
    absent_reasons.setdefault("implied_width_ratio", _pit_only)
    absent_reasons.setdefault("implied_skew", _pit_only)

    if frame.empty:
        return {
            "version": STACK_VERSION,
            "published": False,
            "reason": "no usable cross-sectional history for the peer universe",
            "method": "cross_sectional_ridge_walk_forward",
            "universe_size": 0,
            "features": [],
            "features_absent": absent_reasons,
            "configs_tried": 0,
            "by_horizon": {},
            "ablations": {},
            "gates": {},
        }

    out = run_stack_core(
        frame,
        ticker=ticker,
        cfg=cfg,
        candidate_features=CANDIDATE_FEATURES,
        absent_reasons=absent_reasons,
    )
    if as_of is not None:
        out["as_of"] = str(as_of)
    else:
        out["as_of"] = panel.as_of if hasattr(panel, "as_of") else None
    out["universe_requested"] = list(universe)
    return out
