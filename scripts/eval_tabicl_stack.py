#!/usr/bin/env python3
"""Offline walk-forward comparison: TabICL v2 in-context vs the Situate ridge stack.

Both models are scored on the **same** purged/embargoed walk-forward harness
that :mod:`app.situate.stack` uses to gate publication, so the comparison is
apples to apples:

* expanding window, refit annually (the ridge) / re-read annually (TabICL's
  context is the eligible rows as of the first test month of each year, capped
  at the most recent ``--max-context-rows``);
* purge ``h`` months + embargo 1 month (:func:`eligible_train_mask`);
* metrics: cross-sectional Spearman IC per month with a block-bootstrap 90% CI,
  the deflated Sharpe of the long-top / short-bottom quintile rule, and the two
  publish gates (``IC > 0.03`` with a CI excluding zero; deflated Sharpe ``> 0``).

TabICL is run as a **regressor** on the same forward excess-return target so
its predictions rank names exactly like the ridge's do. Nothing here runs in
CI: the default is a synthetic panel (no network); ``--live`` loads a cached
Massive panel over a sector's peers. The report lands under ``reports/``
(``reports/tabicl-eval/<timestamp>.json`` + ``.md``), see ``reports/README.md``.

Usage::

    python scripts/eval_tabicl_stack.py                      # synthetic, needs tabicl
    python scripts/eval_tabicl_stack.py --live --sector technology --years 10
    python scripts/eval_tabicl_stack.py --fake-predictor     # harness smoke, no model

``tabicl`` is optional everywhere else in this repo; this script needs it (or
``TABULAR_INFERENCE_URL``) unless ``--fake-predictor`` is passed.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# Allow running as `python scripts/eval_tabicl_stack.py` from a checkout.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in _REPO_ROOT.parts and str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app.situate.stack import (  # noqa: E402
    PRICE_FEATURES,
    StackConfig,
    _month_idx,
    _quintile_long_short,
    _resolve_features,
    block_bootstrap_ci,
    cross_sectional_zscore,
    deflated_sharpe,
    eligible_train_mask,
    sharpe_ratio,
    spearman_ic,
    walk_forward_oos,
)
from app.situate.validate import synthetic_panel  # noqa: E402
from app.tabular import TabularPrediction, TabularPredictor, TabularUnavailable  # noqa: E402

DEFAULT_REPORT_DIR = _REPO_ROOT / "reports" / "tabicl-eval"


# --------------------------------------------------------------------------
# Walk-forward with an in-context predictor (same purge/embargo as the ridge)
# --------------------------------------------------------------------------


def walk_forward_icl(
    frame: pd.DataFrame,
    feature_cols: list[str],
    *,
    horizon: int,
    cfg: StackConfig,
    target_col: str,
    predictor: TabularPredictor,
    max_context_rows: int,
    date_col: str = "date",
    symbol_col: str = "symbol",
) -> dict[str, Any]:
    """The :func:`walk_forward_oos` protocol with TabICL as the model.

    For each calendar year the context is every row whose label window closed
    before that year's first test month (purge + embargo), truncated to the most
    recent ``max_context_rows``; every test month of that year is then one query
    cross-section. Returns the same dict shape as :func:`walk_forward_oos`.
    """
    cols = list(feature_cols)
    work = frame[[date_col, symbol_col, target_col, *cols]].copy()
    work = work.dropna(subset=[target_col, *cols]).reset_index(drop=True)
    if work.empty:
        return _empty()
    work["_m"] = _month_idx(work[date_col])
    m_all = work["_m"].to_numpy(dtype=np.int64)
    test_months = np.sort(work["_m"].unique())

    rows: list[dict[str, Any]] = []
    calls = 0
    for year in np.unique(test_months // 12):
        year_months = test_months[(test_months // 12) == year]
        earliest = int(year_months.min())
        train_mask = eligible_train_mask(m_all, earliest, horizon, cfg.embargo_months)
        n_train_rows = int(train_mask.sum())
        n_train_months = int(np.unique(m_all[train_mask]).size)
        if n_train_rows < cfg.min_train_rows or n_train_months < cfg.min_train_months:
            continue
        context = work[train_mask].sort_values([date_col, symbol_col]).tail(max_context_rows)
        query_mask = np.isin(m_all, year_months)
        query = work[query_mask]
        # Drop query months that are too thin for a cross-sectional IC.
        counts = query.groupby("_m").size()
        keep = counts[counts >= cfg.min_cross_section].index
        query = query[query["_m"].isin(keep)]
        if query.empty:
            continue
        prediction = predictor.predict(
            "regression",
            context[cols].reset_index(drop=True),
            context[target_col].reset_index(drop=True),
            query[cols].reset_index(drop=True),
            categorical=(),
            options={"max_context_rows": max_context_rows, "n_estimators": 4},
        )
        calls += 1
        preds = [float(v) for v in prediction.predictions]
        for (_, row), pred in zip(query.iterrows(), preds, strict=True):
            rows.append(
                {
                    "date": row[date_col],
                    "_m": int(row["_m"]),
                    "symbol": row[symbol_col],
                    "pred": pred,
                    "actual": float(row[target_col]),
                }
            )
    if not rows:
        return _empty()
    oos = pd.DataFrame(rows)
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
    finite_ic = per_date_ic.dropna()
    return {
        "oos": oos,
        "per_date_ic": per_date_ic,
        "mean_ic": float(finite_ic.mean()) if not finite_ic.empty else float("nan"),
        "ls_returns": ls_returns,
        "n_test_months": int(finite_ic.shape[0]),
        "n_oos_rows": int(oos.shape[0]),
        "model_calls": calls,
    }


def _empty() -> dict[str, Any]:
    return {
        "oos": pd.DataFrame(columns=["date", "_m", "symbol", "pred", "actual"]),
        "per_date_ic": pd.Series(dtype=float, name="ic"),
        "mean_ic": float("nan"),
        "ls_returns": pd.Series(dtype=float, name="ls"),
        "n_test_months": 0,
        "n_oos_rows": 0,
        "model_calls": 0,
    }


# --------------------------------------------------------------------------
# Harness metrics (identical to run_stack_core's pass-2 gates)
# --------------------------------------------------------------------------


def _gates(
    result: dict[str, Any], *, cfg: StackConfig, horizon: int, n_trials: int, var_trials: float
) -> dict[str, Any]:
    ic = result["mean_ic"]
    ci_lo, ci_hi = block_bootstrap_ci(
        result["per_date_ic"],
        block=cfg.block_months,
        n_boot=cfg.n_bootstrap,
        low=cfg.ci_low,
        high=cfg.ci_high,
        seed=cfg.seed + horizon,
    )
    ds = deflated_sharpe(result["ls_returns"].to_numpy(), n_trials=n_trials, var_trials=var_trials)
    ic_pass = bool(math.isfinite(ic) and ic > cfg.ic_gate and ci_lo is not None and ci_lo > 0.0)
    ds_pass = bool(math.isfinite(ds["deflated_excess"]) and ds["deflated_excess"] > 0.0)
    return {
        "oos_ic": ic if math.isfinite(ic) else None,
        "oos_ic_ci": [ci_lo, ci_hi],
        "n_test_months": result["n_test_months"],
        "n_oos_rows": result["n_oos_rows"],
        "sharpe": ds["sharpe"] if math.isfinite(ds["sharpe"]) else None,
        "deflated_sharpe": ds["deflated_excess"] if math.isfinite(ds["deflated_excess"]) else None,
        "deflated_sharpe_prob": ds["dsr_prob"] if math.isfinite(ds["dsr_prob"]) else None,
        "ic_pass": ic_pass,
        "deflated_sharpe_pass": ds_pass,
        "passed_gates": bool(ic_pass and ds_pass),
        "model_calls": int(result.get("model_calls", 0)),
    }


def compare(
    frame: pd.DataFrame,
    *,
    cfg: StackConfig,
    predictor: TabularPredictor,
    max_context_rows: int,
    features: tuple[str, ...] = PRICE_FEATURES,
) -> dict[str, Any]:
    """Score ridge and TabICL per horizon on the same harness. Pure; no I/O."""
    available, absent = _resolve_features(frame, features)
    if not available:
        raise ValueError("no usable features across the panel")
    zframe = cross_sectional_zscore(frame, available)
    per_horizon: dict[str, dict[str, Any]] = {}
    results: dict[str, dict[int, dict[str, Any]]] = {"ridge": {}, "tabicl": {}}
    trial_sharpes: list[float] = []
    for h in cfg.horizons:
        target_col = f"target_h{h}"
        if target_col not in zframe.columns:
            continue
        ridge = walk_forward_oos(zframe, available, horizon=h, cfg=cfg, target_col=target_col)
        icl = walk_forward_icl(
            zframe,
            available,
            horizon=h,
            cfg=cfg,
            target_col=target_col,
            predictor=predictor,
            max_context_rows=max_context_rows,
        )
        results["ridge"][h] = ridge
        results["tabicl"][h] = icl
        for res in (ridge, icl):
            sr = sharpe_ratio(res["ls_returns"].to_numpy())
            if math.isfinite(sr):
                trial_sharpes.append(sr)
    var_trials = (
        float(np.var(np.asarray(trial_sharpes), ddof=1)) if len(trial_sharpes) >= 2 else 0.0
    )
    n_trials = max(1, len(trial_sharpes))
    for h in cfg.horizons:
        if h not in results["ridge"]:
            continue
        ridge_gates = _gates(
            results["ridge"][h], cfg=cfg, horizon=h, n_trials=n_trials, var_trials=var_trials
        )
        icl_gates = _gates(
            results["tabicl"][h], cfg=cfg, horizon=h, n_trials=n_trials, var_trials=var_trials
        )
        delta = None
        if ridge_gates["oos_ic"] is not None and icl_gates["oos_ic"] is not None:
            delta = icl_gates["oos_ic"] - ridge_gates["oos_ic"]
        per_horizon[str(h)] = {
            "ridge": ridge_gates,
            "tabicl": icl_gates,
            "ic_delta_tabicl_minus_ridge": delta,
        }
    return {
        "features": available,
        "features_absent": absent,
        "universe_size": int(frame["symbol"].nunique()),
        "n_rows": int(frame.shape[0]),
        "configs_tried": n_trials,
        "gates": {"ic_gate": cfg.ic_gate, "var_trials": var_trials, "n_trials": n_trials},
        "max_context_rows": max_context_rows,
        "by_horizon": per_horizon,
    }


# --------------------------------------------------------------------------
# Report writing
# --------------------------------------------------------------------------


def _fmt(value: Any, nd: int = 4) -> str:
    if value is None:
        return "n/a"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return "n/a" if not math.isfinite(number) else f"{number:.{nd}f}"


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# TabICL v2 vs ridge stack — walk-forward comparison",
        "",
        f"Generated {report['generated_at']} | source: {report['source']} | "
        f"universe {report['universe_size']} names, {report['n_rows']} rows | "
        f"features: {', '.join(report['features'])} | "
        f"context cap {report['max_context_rows']} rows | predictor: {report['predictor']}",
        "",
        "Same harness for both: expanding window, annual refit, purge h + embargo 1, "
        f"IC gate > {report['gates']['ic_gate']} with a 90% block-bootstrap CI excluding 0, "
        f"deflated Sharpe > 0 over {report['gates']['n_trials']} configs.",
        "",
        "| h | model | OOS IC | IC 90% CI | months | Sharpe | deflated | gates | calls |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for h, block in report["by_horizon"].items():
        for name in ("ridge", "tabicl"):
            g = block[name]
            ci = g["oos_ic_ci"]
            lines.append(
                f"| {h}m | {name} | {_fmt(g['oos_ic'])} | [{_fmt(ci[0])}, {_fmt(ci[1])}] | "
                f"{g['n_test_months']} | {_fmt(g['sharpe'], 3)} | {_fmt(g['deflated_sharpe'], 3)} | "
                f"{'PASS' if g['passed_gates'] else 'fail'} | {g['model_calls']} |"
            )
        lines.append(
            f"| {h}m | Δ IC (tabicl − ridge) | {_fmt(block['ic_delta_tabicl_minus_ridge'])} | | | | | | |"
        )
    lines.append("")
    lines.append(
        "Reading: a positive Δ IC with TabICL passing the gates where the ridge does not is the "
        "case for promoting it; the reverse means the ridge stays. Neither number is a forecast."
    )
    return "\n".join(lines) + "\n"


def write_report(report: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"{stamp}.json"
    md_path = out_dir / f"{stamp}.md"
    json_path.write_text(json.dumps(report, default=str, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


# --------------------------------------------------------------------------
# Predictors and data sources
# --------------------------------------------------------------------------


class FakeRegressor:
    """Harness smoke double: rank by the first feature (which carries the signal)."""

    name = "fake_regressor"

    def predict(
        self,
        task: str,
        x_context: pd.DataFrame,
        y_context: pd.Series,
        x_query: pd.DataFrame,
        *,
        categorical: Any = (),
        options: dict[str, Any] | None = None,
    ) -> TabularPrediction:
        del y_context, categorical, options
        first = x_query.columns[0]
        return TabularPrediction(
            task=task,
            predictions=[float(v) for v in x_query[first].fillna(0.0)],
            context_rows_used=int(x_context.shape[0]),
        )


def _resolve_predictor(fake: bool) -> TabularPredictor:
    if fake:
        return FakeRegressor()
    from app.tabular import get_predictor

    return get_predictor()


def _live_frame(
    sector: str | None, focus: str, *, as_of: str | None, years: int, horizons: tuple[int, ...]
) -> tuple[pd.DataFrame, str]:
    from app.prism.cache import PrismCache
    from app.prism.data import build_prism_client
    from app.situate import peers as peers_mod
    from app.situate.panel import load_panel
    from app.situate.stack import build_feature_panel

    universe = (
        peers_mod.universe_for(focus, sector=sector) if sector else peers_mod.universe_for(focus)
    )
    etf_of = peers_mod.etf_map(universe)
    symbols = sorted({*universe, *(e for e in etf_of.values() if e)})
    print(f"[load] {len(symbols)} symbols, years={years}, as_of={as_of or 'latest'} ...")
    panel = load_panel(
        build_prism_client(), symbols, as_of=as_of, years=years, cache=PrismCache.from_env()
    )
    frame, _absent = build_feature_panel(panel, universe, etf_of=etf_of, horizons=horizons)
    return frame, f"live:{sector or focus}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python scripts/eval_tabicl_stack.py",
        description="Walk-forward comparison of TabICL v2 vs the Situate ridge stack.",
    )
    parser.add_argument("--live", action="store_true", help="load a cached Massive panel")
    parser.add_argument("--sector", default=None, help="sector peers to evaluate (live)")
    parser.add_argument("--ticker", default="AAPL", help="focus ticker (live, no --sector)")
    parser.add_argument("--as-of", dest="as_of", default=None, help="evaluation date (ISO)")
    parser.add_argument("--years", type=int, default=10, help="years of history (live)")
    parser.add_argument("--horizons", default="1,3,6", help="comma horizons in months")
    parser.add_argument("--max-context-rows", type=int, default=8000, help="TabICL context cap")
    parser.add_argument("--n-symbols", type=int, default=30, help="synthetic universe size")
    parser.add_argument("--n-months", type=int, default=160, help="synthetic panel length")
    parser.add_argument("--signal", type=float, default=0.9, help="synthetic signal strength")
    parser.add_argument(
        "--fake-predictor", action="store_true", help="harness smoke without a model"
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR), help="report directory")
    args = parser.parse_args(argv)

    horizons = tuple(int(x) for x in str(args.horizons).split(",") if x.strip())
    try:
        predictor = _resolve_predictor(args.fake_predictor)
    except TabularUnavailable as exc:
        print(
            f"[error] {exc.reason}; install '.[tabular]' or set TABULAR_INFERENCE_URL "
            "(or pass --fake-predictor for a harness smoke)."
        )
        return 2

    if args.live:
        cfg = StackConfig(horizons=horizons)
        frame, source = _live_frame(
            args.sector,
            str(args.ticker).upper(),
            as_of=args.as_of,
            years=args.years,
            horizons=horizons,
        )
        if frame.empty:
            print("[error] no usable cross-sectional history was loaded.")
            return 1
    else:
        cfg = StackConfig(
            horizons=horizons, min_train_months=24, min_train_rows=120, min_cross_section=8
        )
        frame = synthetic_panel(
            n_symbols=args.n_symbols, n_months=args.n_months, horizons=horizons, signal=args.signal
        )
        source = (
            f"synthetic(n_symbols={args.n_symbols}, n_months={args.n_months}, signal={args.signal})"
        )

    started = time.perf_counter()
    report = compare(
        frame, cfg=cfg, predictor=predictor, max_context_rows=int(args.max_context_rows)
    )
    report.update(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "source": source,
            "predictor": str(getattr(predictor, "name", "unknown")),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "horizons": list(horizons),
        }
    )
    json_path, md_path = write_report(report, Path(args.out_dir))
    print(render_markdown(report))
    print(f"[written] {json_path}\n[written] {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
