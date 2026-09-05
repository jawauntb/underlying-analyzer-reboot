"""Walk-forward validation report for the stack (SPEC 5.7 / §8).

``python -m app.situate.validate`` builds the cross-sectional feature panel for a
peer universe, runs the purged/embargoed walk-forward stack, and prints:

* **OOS IC by horizon** with its block-bootstrap 90% CI;
* the **deflated Sharpe** of the long/short quintile rule and the number of
  configurations tried;
* the **ablation** table (which features earned their place);
* **calibration** — do the model's 25–75 predictive bands actually cover ~50% of
  realised outcomes out-of-sample? (estimated on the first half of the OOS dates,
  measured on the second half, so it is not a tautology);
* the final **publish decision** and, when it fails, the honest reason the engine
  falls back to ``base_rates + implied``.

It runs offline against a synthetic panel (``--synthetic``) — deterministic and
used by the tests as a smoke — and live against Massive (``--live``) over a small,
cached universe, logging any truncation. It never fabricates a number.
"""

from __future__ import annotations

import argparse
import json
import math
from typing import Any

import numpy as np
import pandas as pd

from app.situate import peers as peers_mod
from app.situate.stack import (
    CANDIDATE_FEATURES,
    StackConfig,
    _resolve_features,
    build_feature_panel,
    cross_sectional_zscore,
    run_stack_core,
    walk_forward_oos,
)


def split_half_calibration(oos: pd.DataFrame) -> dict[str, Any]:
    """Out-of-sample coverage of the 25–75 predictive band.

    Residual quantiles are estimated on the earlier half of the OOS dates and the
    band ``pred + [q25, q75]`` is checked against the later half's realised
    outcomes. A well-calibrated band covers ~0.50. Returns ``None`` coverage when
    there is not enough OOS history to split.
    """
    if oos is None or oos.empty or "_m" not in oos.columns:
        return {"coverage": None, "n": 0, "reason": "no OOS rows"}
    months = np.sort(oos["_m"].unique())
    if months.size < 8:
        return {"coverage": None, "n": int(oos.shape[0]), "reason": "too few OOS months to split"}
    cut = months[months.size // 2]
    train = oos[oos["_m"] <= cut]
    test = oos[oos["_m"] > cut]
    resid_train = (train["actual"] - train["pred"]).to_numpy(dtype=float)
    resid_train = resid_train[np.isfinite(resid_train)]
    if resid_train.size < 10 or test.empty:
        return {"coverage": None, "n": int(oos.shape[0]), "reason": "insufficient residuals"}
    q25, q75 = np.quantile(resid_train, [0.25, 0.75])
    lo = test["pred"].to_numpy(dtype=float) + q25
    hi = test["pred"].to_numpy(dtype=float) + q75
    actual = test["actual"].to_numpy(dtype=float)
    inside = np.mean((actual >= lo) & (actual <= hi))
    return {
        "coverage": float(inside),
        "target": 0.50,
        "n": int(test.shape[0]),
        "band_width": float(q75 - q25),
    }


def synthetic_panel(
    *,
    n_symbols: int = 30,
    n_months: int = 160,
    horizons: tuple[int, ...] = (1, 3, 6),
    signal: float = 0.9,
    noise: float = 1.0,
    seed: int = 11,
) -> pd.DataFrame:
    """A long feature panel with a known cross-sectional signal (for the report).

    ``mom_12_1`` carries a genuine forward-return signal; ``rev_1m`` and the
    dummies are noise. With ``signal=0`` the panel is pure noise and the gates
    should reject it.
    """
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2008-01-31", periods=n_months, freq="ME")
    rows: list[dict[str, Any]] = []
    # Persistent per-symbol feature so the cross-section has structure.
    for s in range(n_symbols):
        sym = f"SYM{s:02d}"
        mom = rng.normal(0.0, 1.0, size=n_months)
        rev = rng.normal(0.0, 1.0, size=n_months)
        vold = rng.integers(0, 2, size=n_months).astype(float)
        trd = rng.integers(0, 2, size=n_months).astype(float)
        for i, d in enumerate(dates):
            row: dict[str, Any] = {
                "date": d,
                "symbol": sym,
                "mom_12_1": float(mom[i]),
                "rev_1m": float(rev[i]),
                "vol_dummy": float(vold[i]),
                "trend_dummy": float(trd[i]),
                "quality": np.nan,
                "value": np.nan,
            }
            for h in horizons:
                if i + h < n_months:
                    eps = rng.normal(0.0, noise)
                    row[f"target_h{h}"] = signal * mom[i] * 0.01 + 0.01 * eps
                else:
                    row[f"target_h{h}"] = np.nan
            rows.append(row)
    return pd.DataFrame(rows)


def run_report(
    frame: pd.DataFrame,
    *,
    ticker: str,
    cfg: StackConfig,
    features_absent: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run the stack and assemble the validation report dict."""
    core = run_stack_core(
        frame,
        ticker=ticker,
        cfg=cfg,
        candidate_features=CANDIDATE_FEATURES,
        absent_reasons=features_absent,
    )
    kept = core["features"]
    available, _ = _resolve_features(frame, kept)
    zframe = cross_sectional_zscore(frame, available) if available else frame
    calibration: dict[str, Any] = {}
    for h in cfg.horizons:
        target_col = f"target_h{h}"
        if target_col not in frame.columns or not available:
            continue
        oos = walk_forward_oos(zframe, available, horizon=h, cfg=cfg, target_col=target_col)
        calibration[str(h)] = split_half_calibration(oos["oos"])
    core["calibration"] = calibration
    return core


def _fmt(value: Any, nd: int = 4) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "  n/a "
    if isinstance(value, (int, float)):
        return f"{value:+.{nd}f}"
    return str(value)


def print_report(report: dict[str, Any]) -> None:
    """Human-readable rendering of :func:`run_report`'s output."""
    print("=" * 74)
    print(f"Situate stack validation — stack v{report.get('version')}")
    print("=" * 74)
    print(f"universe size      : {report.get('universe_size')} names, {report.get('n_rows')} rows")
    print(f"candidate features : {', '.join(report.get('candidate_features', []))}")
    print(f"features kept      : {', '.join(report.get('features', [])) or '(none)'}")
    absent = report.get("features_absent", {})
    if absent:
        print("features absent    :")
        for name, reason in absent.items():
            print(f"    - {name}: {reason}")
    print(f"configs tried (N)  : {report.get('configs_tried')}")
    gates = report.get("gates", {})
    print(f"var(trial Sharpes) : {_fmt(gates.get('var_trials'))}  ic_gate={gates.get('ic_gate')}")
    print("-" * 74)
    print(f"{'h':>3} {'OOS_IC':>9} {'IC_CI_lo':>9} {'IC_CI_hi':>9} {'defSharpe':>10} "
          f"{'DSRprob':>8} {'calib':>7} {'gates':>6}")
    for h_key, block in sorted(report.get("by_horizon", {}).items(), key=lambda kv: int(kv[0])):
        cal = report.get("calibration", {}).get(h_key, {})
        ci = block.get("oos_ic_ci") or [None, None]
        print(
            f"{h_key:>3} "
            f"{_fmt(block.get('oos_ic')):>9} "
            f"{_fmt(ci[0]):>9} "
            f"{_fmt(ci[1]):>9} "
            f"{_fmt(block.get('deflated_sharpe')):>10} "
            f"{_fmt(block.get('deflated_sharpe_prob'), 3):>8} "
            f"{_fmt(cal.get('coverage'), 2):>7} "
            f"{'PASS' if block.get('passed_gates') else 'fail':>6}"
        )
    print("-" * 74)
    ablations = report.get("ablations", {})
    if ablations:
        print("ablation (removing a feature; raises_ic ⇒ feature is not earning its place):")
        for key, rep in sorted(ablations.items()):
            flag = "DROP" if rep.get("raises_ic") else "keep"
            print(f"    {key:<16} ic_full={_fmt(rep.get('ic_full'))} "
                  f"ic_without={_fmt(rep.get('ic_without'))}  -> {flag}")
    print("-" * 74)
    if report.get("published"):
        print("DECISION: PUBLISHED — the stack passes its gates on at least one horizon.")
    else:
        print(f"DECISION: NOT PUBLISHED — {report.get('reason')}")
    print("=" * 74)


def _live_frame(
    tickers: list[str],
    *,
    sector: str | None,
    as_of: str | None,
    years: int,
    limit: int,
    cfg: StackConfig,
) -> tuple[pd.DataFrame, dict[str, str], str]:
    """Load a real, cached panel and build the feature frame. Logs truncation."""
    from app.prism.cache import PrismCache
    from app.prism.data import build_prism_client
    from app.situate.panel import load_panel

    focus = tickers[0] if tickers else "AAPL"
    if sector:
        universe = peers_mod.universe_for(sector=sector, limit=limit)
    else:
        universe = peers_mod.universe_for(focus, include_sp100=True, limit=limit)
    for t in tickers:
        if t not in universe:
            universe = [t, *universe]
    full = len(peers_mod.SP100)
    if len(universe) < full:
        print(f"[truncation] using {len(universe)} of {full} S&P100 names "
              f"(limit={limit}) for a cached live run")

    etf_of = peers_mod.etf_map(universe)
    etfs = list(peers_mod.all_sector_etfs())
    load_symbols = list(dict.fromkeys([*universe, *etfs]))
    print(f"[load] {len(load_symbols)} symbols ({len(universe)} names + {len(etfs)} ETFs), "
          f"years={years}, as_of={as_of or 'latest'} ...")

    client = build_prism_client()
    cache = PrismCache.from_env()
    panel = load_panel(client, load_symbols, as_of=as_of, years=years, cache=cache)
    print(f"[load] panel: {len(panel.symbols())} loaded, {len(panel.errors)} failed, "
          f"cache={panel.cache_status}")

    frame, absent = build_feature_panel(
        panel, universe, etf_of=etf_of, horizons=cfg.horizons
    )
    return frame, absent, focus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.situate.validate",
        description="Walk-forward validation report for the Situate stack.",
    )
    parser.add_argument("tickers", nargs="*", default=["AAPL"], help="focus ticker(s)")
    parser.add_argument("--sector", default=None, help="fit over one sector's peers")
    parser.add_argument("--as-of", dest="as_of", default=None, help="evaluation date (ISO)")
    parser.add_argument("--years", type=int, default=8, help="years of history to load")
    parser.add_argument("--limit", type=int, default=40, help="max universe size (live)")
    parser.add_argument("--horizons", default="1,3,6", help="comma horizons in months")
    parser.add_argument("--live", action="store_true", help="load real Massive data")
    parser.add_argument("--synthetic", action="store_true", help="use a synthetic panel")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)

    horizons = tuple(int(x) for x in str(args.horizons).split(",") if x.strip())
    cfg = StackConfig(horizons=horizons)
    tickers = [t.strip().upper() for t in args.tickers if t.strip()] or ["AAPL"]

    if args.live and not args.synthetic:
        frame, absent, focus = _live_frame(
            tickers, sector=args.sector, as_of=args.as_of, years=args.years,
            limit=args.limit, cfg=cfg,
        )
        if frame.empty:
            print("[error] no usable cross-sectional history was loaded.")
            return 1
    else:
        # Offline synthetic panel: relax breadth thresholds so the demo runs.
        cfg = StackConfig(
            horizons=horizons, min_train_months=24, min_train_rows=120, min_cross_section=8
        )
        frame = synthetic_panel(horizons=horizons)
        absent = {"quality": "synthetic panel", "value": "synthetic panel"}
        focus = "SYM00"

    report = run_report(frame, ticker=focus, cfg=cfg, features_absent=absent)
    if args.json:
        print(json.dumps(report, default=str, indent=2))
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
