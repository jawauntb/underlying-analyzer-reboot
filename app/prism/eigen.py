"""Eigen-structure of the signal set: PCA, ranking, symmetry, load bearing.

Three ideas from the user's own papers are used here, and only where they earn
their place:

* **Gauge fixing** (*Gauge-Fixed Transport of Concern*, 2026). Signals arrive in
  incompatible units — a beta, a yield change, an entropy reading. Before any of
  them are compared they are put in one fixed reference frame: z-scored over the
  evaluation window. Comparisons are then frame invariant, which is what makes
  the correlation structure below meaningful rather than an artefact of scaling.
* **Symmetry breaking**. A pair of signals whose correlation keeps its sign and
  rough magnitude across regimes is *gauge invariant* — the relationship is a
  property of the system, not of the current regime. A pair whose correlation
  flips sign between regimes is *broken*: the market is running a different
  mechanism in each regime, and any model that averaged over regimes has been
  reading a phantom.
* **Load bearing** (*A Load-Bearing Standard for Representation Claims*). A
  signal being *available* (present, correlated, legible) is not a claim that it
  is doing work. The standard demanded here is interventional: remove the signal
  and re-run the scenario weighting; if the weights barely move, the signal was
  decorative. ``load_bearing`` reports the leave-one-out weight delta, not the
  correlation.

Everything is pure numpy/pandas; the load-bearing test takes a callback so the
dependency on :mod:`app.prism.scenarios` stays one-directional.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

import numpy as np
import numpy.typing as npt
import pandas as pd

FloatArray = npt.NDArray[np.float64]
WeightFn = Callable[[Sequence[str]], Mapping[str, float]]

__all__ = [
    "RANKING_WINDOWS",
    "build_eigen",
    "gauge_fix",
    "load_bearing_test",
    "pca_svd",
    "rank_signals",
    "regime_correlation_flips",
]

#: Ranking-window label -> number of observations (daily signals by default).
RANKING_WINDOWS: dict[str, int] = {"1y": 252, "6m": 126, "3m": 63}
_FLIP_MIN_ABS_CORR = 0.20
_INVARIANT_MAX_SPREAD = 0.25
_DEFAULT_LOAD_BEARING_THRESHOLD = 0.05


def gauge_fix(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Z-score every column; drop columns with no variation.

    This is the fixed reference frame every later comparison happens in. Returns
    ``(standardised_frame, dropped_column_names)``.
    """
    numeric = frame.apply(pd.to_numeric, errors="coerce")
    std = numeric.std(ddof=0)
    dropped = [str(name) for name in std.index[~(std > 0) | std.isna()]]
    kept = numeric.drop(columns=dropped)
    if kept.empty:
        return kept, dropped
    standardised = (kept - kept.mean()) / kept.std(ddof=0)
    return standardised, dropped


def pca_svd(
    matrix: pd.DataFrame | FloatArray,
    *,
    standardize: bool = True,
    n_components: int | None = None,
) -> dict[str, Any]:
    """Principal components via SVD of the centred (optionally scaled) matrix.

    Returns ``explained_variance_ratio``, ``eigenvalues`` (of the covariance
    /correlation matrix), ``singular_values``, ``components`` (rows are the
    principal axes in feature space) and ``feature_names``.
    """
    if isinstance(matrix, pd.DataFrame):
        frame = matrix.apply(pd.to_numeric, errors="coerce").dropna()
        names = [str(name) for name in frame.columns]
        data = frame.to_numpy(dtype=np.float64)
    else:
        data = np.asarray(matrix, dtype=np.float64)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        names = [f"f{index}" for index in range(data.shape[1])]

    empty: dict[str, Any] = {
        "explained_variance_ratio": [],
        "eigenvalues": [],
        "singular_values": [],
        "components": [],
        "feature_names": names,
        "n_samples": int(data.shape[0]) if data.size else 0,
        "error": None,
    }
    if data.size == 0 or data.shape[0] < 2 or data.shape[1] < 1:
        empty["error"] = "need at least 2 rows and 1 column"
        return empty

    centred = data - data.mean(axis=0, keepdims=True)
    if standardize:
        scale = data.std(axis=0, ddof=0)
        keep = scale > 0
        if not bool(np.any(keep)):
            empty["error"] = "every column is constant"
            return empty
        centred = centred[:, keep] / scale[keep]
        names = [name for name, flag in zip(names, keep, strict=True) if bool(flag)]

    try:
        _, singular, vt = np.linalg.svd(centred, full_matrices=False)
    except np.linalg.LinAlgError as exc:  # pragma: no cover - defensive
        empty["error"] = f"SVD failed: {exc}"
        return empty

    dof = max(centred.shape[0] - 1, 1)
    eigenvalues = (singular**2) / dof
    total = float(np.sum(eigenvalues))
    ratio = eigenvalues / total if total > 0 else np.zeros_like(eigenvalues)
    limit = len(eigenvalues) if n_components is None else min(n_components, len(eigenvalues))
    return {
        "explained_variance_ratio": [float(value) for value in ratio[:limit]],
        "eigenvalues": [float(value) for value in eigenvalues[:limit]],
        "singular_values": [float(value) for value in singular[:limit]],
        "components": [[float(value) for value in row] for row in vt[:limit]],
        "feature_names": names,
        "n_samples": int(centred.shape[0]),
        "standardized": bool(standardize),
        "error": None,
    }


def rank_signals(
    signals: pd.DataFrame,
    target: pd.Series,
    *,
    windows: Mapping[str, int] | None = None,
    forward_days: int = 21,
    min_observations: int = 20,
) -> list[dict[str, Any]]:
    """Rank signals by |correlation| with the target over several windows.

    ``target`` is normally the ticker's daily return series. Each entry carries
    the per-window correlations, a ``forward_corr`` against the target's forward
    ``forward_days`` return (the predictive rather than contemporaneous view),
    and a ``rank`` on the longest window's absolute correlation.
    """
    window_map = dict(RANKING_WINDOWS if windows is None else windows)
    # A window shorter than `min_observations` can never produce a correlation, so
    # a caller passing monthly windows (12/6/3) against the daily default of 20
    # silently nulled every column. Cap the floor at the shortest window asked for
    # rather than returning a table of nulls with a meaningless rank.
    if window_map:
        min_observations = max(3, min(int(min_observations), min(window_map.values())))
    frame = signals.apply(pd.to_numeric, errors="coerce")
    series = pd.to_numeric(pd.Series(target), errors="coerce")
    aligned = frame.join(series.rename("__target__"), how="inner").dropna(how="all")
    if aligned.empty or "__target__" not in aligned.columns:
        return []

    forward = (
        np.log1p(aligned["__target__"]).rolling(forward_days).sum().shift(-forward_days)
    ).apply(np.expm1)

    longest = max(window_map.values()) if window_map else len(aligned)
    rows: list[dict[str, Any]] = []
    for column in frame.columns:
        entry: dict[str, Any] = {"signal": str(column)}
        for label, days in window_map.items():
            subset = aligned[[str(column), "__target__"]].iloc[-days:].dropna()
            # `n_<window>` is reported so a correlation computed on three
            # observations is visibly weak rather than looking like the others.
            entry[f"n_{label}"] = int(subset.shape[0])
            if subset.shape[0] < min_observations or subset[str(column)].std(ddof=0) == 0:
                entry[f"corr_{label}"] = None
                continue
            value = float(subset[str(column)].corr(subset["__target__"]))
            entry[f"corr_{label}"] = value if math.isfinite(value) else None
        forward_subset = pd.concat(
            [aligned[str(column)], forward.rename("__forward__")], axis=1
        ).dropna()
        enough = forward_subset.shape[0] >= min_observations
        if enough and forward_subset[str(column)].std(ddof=0) > 0:
            forward_corr = float(
                forward_subset[str(column)].corr(forward_subset["__forward__"])
            )
            entry["forward_corr"] = forward_corr if math.isfinite(forward_corr) else None
        else:
            entry["forward_corr"] = None
        entry["forward_days"] = forward_days
        entry["n"] = int(aligned[str(column)].dropna().shape[0])
        rows.append(entry)

    longest_label = next(
        (label for label, days in window_map.items() if days == longest), None
    )
    key = f"corr_{longest_label}" if longest_label else "forward_corr"
    # Fall back to `forward_corr` when the longest window produced nothing: sorting
    # on an all-null key leaves Python's stable sort untouched, so `rank` would be
    # nothing but the input column order dressed up as a ranking.
    if not any(row.get(key) is not None for row in rows):
        key = "forward_corr"
    ranked_by = key
    if not any(row.get(key) is not None for row in rows):
        # Nothing to rank on at all: say so rather than numbering an arbitrary order.
        for row in rows:
            row["rank"] = None
            row["ranked_by"] = None
        return rows
    rows.sort(key=lambda row: -abs(row.get(key) or 0.0))
    for position, row in enumerate(rows, start=1):
        row["rank"] = position
        row["ranked_by"] = ranked_by
    return rows


def regime_correlation_flips(
    signals: pd.DataFrame,
    regime_labels: pd.Series,
    *,
    min_observations: int = 30,
    min_abs_corr: float = _FLIP_MIN_ABS_CORR,
    max_invariant_spread: float = _INVARIANT_MAX_SPREAD,
) -> dict[str, Any]:
    """Correlation structure per regime; which pairs break and which hold.

    A pair is **broken** (symmetry breaking) when its correlation changes sign
    across two regimes and is at least ``min_abs_corr`` in magnitude on both
    sides. A pair is **gauge invariant** when the sign is the same in every
    regime with enough data and the spread between the extreme correlations is at
    most ``max_invariant_spread``.
    """
    result: dict[str, Any] = {
        "regime_correlation_flip": [],
        "gauge_invariant_pairs": [],
        "broken_pairs": [],
        "per_regime": {},
        "regimes_used": [],
        "reason": None,
    }
    frame = signals.apply(pd.to_numeric, errors="coerce")
    labels = pd.Series(regime_labels).astype("object")
    aligned = frame.join(labels.rename("__regime__"), how="inner")
    aligned = aligned[aligned["__regime__"].notna()]
    if aligned.empty:
        result["reason"] = "no overlap between signals and regime labels"
        return result

    per_regime: dict[str, pd.DataFrame] = {}
    for label in sorted({str(value) for value in aligned["__regime__"].unique()}):
        subset = aligned[aligned["__regime__"] == label].drop(columns="__regime__")
        subset = subset.dropna(axis=1, how="all")
        usable = subset.dropna()
        if usable.shape[0] < min_observations or usable.shape[1] < 2:
            continue
        correlation = usable.corr()
        per_regime[label] = correlation
        result["per_regime"][label] = {
            "n": int(usable.shape[0]),
            "columns": [str(name) for name in correlation.columns],
            "matrix": [[float(value) for value in row] for row in correlation.to_numpy()],
        }
    if len(per_regime) < 2:
        result["reason"] = (
            f"need at least two regimes with {min_observations}+ complete observations, "
            f"got {len(per_regime)}"
        )
        result["regimes_used"] = sorted(per_regime)
        return result

    result["regimes_used"] = sorted(per_regime)
    columns = sorted(
        set.intersection(*(set(matrix.columns) for matrix in per_regime.values())),
        key=str,
    )
    for i, left in enumerate(columns):
        for right in columns[i + 1 :]:
            by_regime: dict[str, float] = {}
            for label, matrix in per_regime.items():
                value = float(matrix.loc[left, right])
                if math.isfinite(value):
                    by_regime[label] = value
            if len(by_regime) < 2:
                continue
            values = list(by_regime.values())
            spread = float(max(values) - min(values))
            signs = {1 if value > 0 else (-1 if value < 0 else 0) for value in values}
            entry = {
                "pair": [str(left), str(right)],
                "by_regime": {label: float(value) for label, value in by_regime.items()},
                "spread": spread,
            }
            flipped = (
                len(signs - {0}) > 1
                and min(abs(value) for value in values) >= min_abs_corr
            )
            if flipped:
                result["regime_correlation_flip"].append(entry)
                result["broken_pairs"].append(entry)
            elif len(signs - {0}) <= 1 and spread <= max_invariant_spread:
                result["gauge_invariant_pairs"].append(entry)

    result["regime_correlation_flip"].sort(key=lambda item: -float(item["spread"]))
    result["broken_pairs"].sort(key=lambda item: -float(item["spread"]))
    result["gauge_invariant_pairs"].sort(key=lambda item: float(item["spread"]))
    result["thresholds"] = {
        "min_abs_corr_for_flip": min_abs_corr,
        "max_spread_for_invariance": max_invariant_spread,
        "min_observations_per_regime": min_observations,
    }
    return result


def load_bearing_test(
    signal_names: Sequence[str],
    weight_fn: WeightFn,
    *,
    threshold: float = _DEFAULT_LOAD_BEARING_THRESHOLD,
) -> list[dict[str, Any]]:
    """Leave-one-out interventional test on the scenario weights.

    ``weight_fn`` takes the surviving signal names and returns the scenario
    weight mapping produced from them. For each signal, ``weight_delta_if_removed``
    is the L1 distance between the full-set weights and the weights recomputed
    without it.

    That total is dominated by arithmetic: dropping a signal always removes its
    own weight and renormalises, so the total is at least ``2 * w_i`` whatever
    the signal does. ``load_bearing`` is therefore set on
    ``survivor_weight_delta`` instead — the L1 change in the *surviving* signals'
    weights against the full-set weights renormalised over the same survivors.
    That isolates the interventional question ("does dropping this signal change
    how the engine weights everything else?") from the trivial renormalisation,
    and it is what the load-bearing standard actually asks.

    Availability is not load bearing: a signal only counts as load bearing here
    if removing it actually moves the decision.
    """
    names = [str(name) for name in signal_names]
    if not names:
        return []
    try:
        base = {str(key): float(value) for key, value in weight_fn(names).items()}
    except Exception as exc:  # noqa: BLE001 - callback belongs to the caller
        return [
            {
                "signal": name,
                "weight_delta_if_removed": None,
                "load_bearing": None,
                "error": f"weight_fn failed on the full set: {exc}",
            }
            for name in names
        ]

    rows: list[dict[str, Any]] = []
    for name in names:
        remaining = [other for other in names if other != name]
        try:
            alternative = {str(key): float(value) for key, value in weight_fn(remaining).items()}
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "signal": name,
                    "weight_delta_if_removed": None,
                    "load_bearing": None,
                    "error": str(exc),
                }
            )
            continue
        keys = set(base) | set(alternative)
        delta = float(sum(abs(alternative.get(key, 0.0) - base.get(key, 0.0)) for key in keys))
        # Renormalise over the keys the callback still returns, not over the
        # signal names: `weight_fn` maps a surviving signal set to a weight
        # vector that need not be indexed by those same names.
        survivors = sorted(alternative)
        survivor_mass = float(sum(base.get(key, 0.0) for key in survivors))
        survivor_delta: float | None = None
        if len(survivors) >= 2 and survivor_mass > 0:
            renormalised = {key: base.get(key, 0.0) / survivor_mass for key in survivors}
            survivor_delta = float(
                sum(abs(alternative.get(key, 0.0) - renormalised[key]) for key in survivors)
            )
        rows.append(
            {
                "signal": name,
                "weight_delta_if_removed": delta,
                "survivor_weight_delta": survivor_delta,
                # Set on the survivor delta, not the total: the total is >= 2*w_i
                # by arithmetic and would call every signal load bearing. When
                # fewer than two weights survive there is nothing to redistribute,
                # so the total is the only measurement available.
                "load_bearing": bool(
                    (survivor_delta if survivor_delta is not None else delta) >= threshold
                ),
                "baseline_weight": base.get(name),
            }
        )
    def _rank_key(row: Mapping[str, Any]) -> float:
        value = row.get("survivor_weight_delta")
        if value is None:
            value = row.get("weight_delta_if_removed")
        return -float(value or 0.0)

    rows.sort(key=_rank_key)
    for row in rows:
        row["threshold"] = threshold
        row["basis"] = (
            "survivor_weight_delta >= threshold; weight_delta_if_removed is the raw "
            "L1 including the removed signal's own weight and its renormalisation"
        )
    return rows


def build_eigen(
    signals: pd.DataFrame,
    target: pd.Series,
    *,
    regime_labels: pd.Series | None = None,
    weight_fn: WeightFn | None = None,
    windows: Mapping[str, int] | None = None,
    n_components: int = 5,
    forward_days: int = 21,
    load_bearing_threshold: float = _DEFAULT_LOAD_BEARING_THRESHOLD,
    ranking_min_observations: int = 20,
) -> dict[str, Any]:
    """Build the packet's ``eigen`` section.

    Parameters
    ----------
    signals:
        Date-indexed frame of candidate signals (benchmark returns, macro
        changes, the factor residual, entropy, regime posterior, spectral
        position, ...). Frequency is the caller's choice; ``windows`` counts
        observations, not calendar days.
    target:
        The ticker's return series, aligned by index.
    regime_labels:
        Optional date-indexed regime labels driving the symmetry analysis.
    weight_fn:
        Optional callback into the scenario weighting used for the load-bearing
        test; see :func:`load_bearing_test`.
    """
    section: dict[str, Any] = {
        "reference_frame": "zscored_per_window (gauge-fixed)",
        "feature_names": [],
        "pca": {"explained_variance_ratio": [], "components": []},
        "eigenvalues": [],
        "signal_ranking": [],
        "symmetry": {
            "regime_correlation_flip": [],
            "gauge_invariant_pairs": [],
            "broken_pairs": [],
        },
        "load_bearing": [],
        "error": None,
    }
    if signals is None or signals.empty:
        section["error"] = "no signals supplied"
        return section

    numeric = signals.apply(pd.to_numeric, errors="coerce")
    numeric = numeric.dropna(axis=1, how="all")
    if numeric.empty:
        section["error"] = "signal frame has no numeric columns"
        return section

    standardised, dropped = gauge_fix(numeric.dropna())
    section["dropped_constant_signals"] = dropped
    section["feature_names"] = [str(name) for name in standardised.columns]
    section["n_observations"] = int(standardised.shape[0])

    pca = pca_svd(standardised, standardize=False, n_components=n_components)
    section["pca"] = {
        "explained_variance_ratio": pca["explained_variance_ratio"],
        "components": pca["components"],
        "feature_names": pca["feature_names"],
        "n_samples": pca["n_samples"],
        "error": pca["error"],
    }
    section["eigenvalues"] = pca["eigenvalues"]

    section["signal_ranking"] = rank_signals(
        numeric,
        target,
        windows=windows,
        forward_days=forward_days,
        min_observations=ranking_min_observations,
    )

    if regime_labels is not None:
        section["symmetry"] = regime_correlation_flips(numeric, regime_labels)
    else:
        section["symmetry"]["reason"] = "no regime labels supplied"

    if weight_fn is not None:
        section["load_bearing"] = load_bearing_test(
            [str(name) for name in numeric.columns],
            weight_fn,
            threshold=load_bearing_threshold,
        )
    else:
        section["load_bearing"] = []
        section["load_bearing_reason"] = "no scenario weight callback supplied"
    return section
