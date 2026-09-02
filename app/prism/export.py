"""Export one packet as JSON, as a readable plain-text report, or as a PDF.

The text export is the packet in full: the memo first, then every section
rendered as aligned tables, so a reader with no dashboard still gets each number
the recommendation stands on. The PDF reuses :mod:`app.memo_pdf`, mapping the
packet's recommendation, entry band, scenario cases and citations onto
``MemoPdfPayload``.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

FORMATS: tuple[str, ...] = ("json", "txt", "pdf")

#: Characters allowed in an export filename before it reaches a
#: ``Content-Disposition`` header.
_FILENAME_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")

CONTENT_TYPES: dict[str, str] = {
    "json": "application/json",
    "txt": "text/plain; charset=utf-8",
    "pdf": "application/pdf",
}


class PrismExportError(ValueError):
    """Raised for an unsupported export format."""


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt(value: Any, *, digits: int = 4) -> str:
    number = _finite(value)
    if number is not None:
        return f"{number:,.{digits}f}"
    if value is None:
        return "-"
    text = str(value)
    return text if len(text) <= 120 else f"{text[:117]}..."


def _pct(value: Any, *, digits: int = 2) -> str:
    number = _finite(value)
    return "-" if number is None else f"{number * 100:+.{digits}f}%"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """A fixed-width table; empty input renders one honest line, not a blank."""
    if not rows:
        return ["  (no rows)"]
    columns = [str(header) for header in headers]
    body = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(columns[index]), *(len(row[index]) for row in body))
        for index in range(len(columns))
    ]
    lines = ["  " + "  ".join(name.ljust(widths[i]) for i, name in enumerate(columns))]
    lines.append("  " + "  ".join("-" * width for width in widths))
    for row in body:
        lines.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return lines


def _heading(title: str) -> list[str]:
    return ["", title.upper(), "=" * len(title), ""]


def _section(packet: Mapping[str, Any], name: str) -> Mapping[str, Any] | None:
    value = packet.get(name)
    return value if isinstance(value, Mapping) else None


def to_json(packet: Mapping[str, Any], *, indent: int | None = 2) -> str:
    """The packet as JSON, with non-serialisable values stringified."""
    return json.dumps(packet, indent=indent, ensure_ascii=False, default=str)


def to_text(packet: Mapping[str, Any]) -> str:
    """The whole packet as a readable report."""
    ticker = str(packet.get("ticker") or "")
    profile = _section(packet, "profile") or {}
    lines: list[str] = [
        f"PRISM MEMO - {ticker}",
        "=" * (14 + len(ticker)),
        f"as of {packet.get('as_of')}  |  generated {packet.get('generated_at')}  |  "
        f"engine {packet.get('engine_version')}",
        f"{profile.get('name') or ticker}  |  {profile.get('sector') or 'sector n/a'} / "
        f"{profile.get('industry') or 'industry n/a'}",
    ]

    memo = _section(packet, "memo")
    if memo:
        recommendation = memo.get("recommendation") or {}
        lines += _heading("Recommendation")
        lines += [
            f"  action      : {recommendation.get('action')}",
            f"  strength    : {recommendation.get('strength')}",
            f"  conviction  : {_fmt(recommendation.get('conviction'), digits=3)}",
            f"  one line    : {recommendation.get('one_line')}",
            f"  entry       : {_fmt(memo.get('entry_price'), digits=2)}",
            f"  fair value  : {_fmt(memo.get('fair_value'), digits=2)}",
            f"  reassess    : {_fmt(memo.get('stop_or_reassess'), digits=2)}",
            f"  method      : {memo.get('method')} ({memo.get('model') or 'no model'})",
        ]
        targets = memo.get("exit_targets") or []
        if targets:
            lines += ["", "  Exit targets:"]
            lines += _table(
                # Not "probability of reaching this price": it is the weight the
                # mixture puts on the bull case at that horizon, and the price is
                # that case's median. Each row carries the same statement in
                # `basis`; the header must not contradict it.
                ["horizon", "price", "bull case p"],
                [
                    [
                        row.get("horizon"),
                        _fmt(row.get("price"), digits=2),
                        _fmt(row.get("probability"), digits=3),
                    ]
                    for row in targets
                    if isinstance(row, Mapping)
                ],
            )
            lines.append(
                "  each price is the bull-case median at that horizon; "
                "'bull case p' is the mixture's probability of the bull case, "
                "not of reaching the price"
            )
        lines += _heading("Memo")
        lines.append(str(memo.get("text") or ""))
    else:
        lines += _heading("Recommendation")
        lines.append(f"  unavailable: {packet.get('memo_error')}")

    lines += _render_seasonality(packet)
    lines += _render_macro(packet)
    lines += _render_relational(packet)
    lines += _render_factors(packet)
    lines += _render_regimes(packet)
    lines += _render_entropy_spectral(packet)
    lines += _render_eigen(packet)
    lines += _render_fundamentals(packet)
    lines += _render_filings(packet)
    lines += _render_volatility(packet)
    lines += _render_levels(packet)
    lines += _render_news(packet)
    lines += _render_scenarios(packet)
    lines += _render_universe(packet)
    lines += _render_meta(packet)
    return "\n".join(lines) + "\n"


def _render_seasonality(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "seasonality")
    lines = _heading("Seasonality")
    if not section:
        return lines + [f"  unavailable: {packet.get('seasonality_error')}"]
    lines.append(f"  calendar month: {section.get('month_label')}")
    subject = section.get("ticker")
    if isinstance(subject, Mapping):
        lines += ["", "  This month, by look-back window:"]
        lines += _table(
            ["window", "mean", "median", "hit rate", "n"],
            [
                [
                    window,
                    _pct(block.get("mean")),
                    _pct(block.get("median")),
                    _fmt(block.get("hit_rate"), digits=3),
                    block.get("n"),
                ]
                for window, block in (subject.get("this_month") or {}).items()
                if isinstance(block, Mapping)
            ],
        )
        lines += ["", "  Forward returns from this month:"]
        lines += _table(
            ["horizon", "mean", "median", "p10", "p90", "hit rate", "n"],
            [
                [
                    horizon,
                    _pct(block.get("mean")),
                    _pct(block.get("median")),
                    _pct(block.get("p10")),
                    _pct(block.get("p90")),
                    _fmt(block.get("hit_rate"), digits=3),
                    block.get("n"),
                ]
                for horizon, block in (subject.get("forward") or {}).items()
                if isinstance(block, Mapping)
            ],
        )
    benchmarks = section.get("benchmarks")
    if isinstance(benchmarks, Mapping):
        lines += ["", "  Benchmarks this month (10-year window):"]
        lines += _table(
            ["symbol", "mean", "hit rate", "n"],
            [
                [
                    symbol,
                    _pct(((stats.get("this_month") or {}).get("10y") or {}).get("mean")),
                    _fmt(
                        ((stats.get("this_month") or {}).get("10y") or {}).get("hit_rate"),
                        digits=3,
                    ),
                    ((stats.get("this_month") or {}).get("10y") or {}).get("n"),
                ]
                for symbol, stats in benchmarks.items()
                if isinstance(stats, Mapping)
            ],
        )
    return lines


def _render_macro(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "macro")
    lines = _heading("Macro")
    if not section:
        return lines + [f"  unavailable: {packet.get('macro_error')}"]
    rows: list[list[Any]] = []
    yields = section.get("yields")
    if isinstance(yields, Mapping):
        for series_id, block in yields.items():
            if isinstance(block, Mapping):
                rows.append(
                    [
                        series_id,
                        _fmt(block.get("current"), digits=3),
                        block.get("as_of"),
                        _fmt(block.get("change_1m"), digits=3),
                        _fmt(block.get("change_3m"), digits=3),
                        _fmt(block.get("change_12m"), digits=3),
                    ]
                )
    for key in ("vix", "hy_spread", "dollar", "wti", "brent", "gold", "btc", "nfp"):
        block = section.get(key)
        if isinstance(block, Mapping):
            rows.append(
                [
                    f"{key} ({block.get('series_id')})",
                    _fmt(block.get("current"), digits=3),
                    block.get("as_of"),
                    _fmt(block.get("change_1m"), digits=3),
                    _fmt(block.get("change_3m"), digits=3),
                    _fmt(block.get("change_12m"), digits=3),
                ]
            )
    fx = section.get("fx")
    if isinstance(fx, Mapping):
        for name, block in fx.items():
            if isinstance(block, Mapping):
                rows.append(
                    [
                        f"fx {name} ({block.get('series_id')})",
                        _fmt(block.get("current"), digits=4),
                        block.get("as_of"),
                        _fmt(block.get("change_1m"), digits=4),
                        _fmt(block.get("change_3m"), digits=4),
                        _fmt(block.get("change_12m"), digits=4),
                    ]
                )
    lines += _table(["series", "current", "as of", "1m", "3m", "12m"], rows)
    curve = section.get("curve_shape") or {}
    lines += [
        "",
        f"  curve: {curve.get('label')} | 2s10s {_fmt(curve.get('2s10s'), digits=3)} "
        f"| 5s20s {_fmt(curve.get('5s20s'), digits=3)}",
    ]
    return lines


def _render_relational(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "relational")
    lines = _heading("Cross-asset relationships")
    if not section:
        return lines + [f"  unavailable: {packet.get('relational_error')}"]
    lines.append(f"  reference frame: {section.get('reference_frame')}")
    beta = section.get("beta")
    correlation = section.get("correlation")
    if isinstance(beta, Mapping):
        windows = list(section.get("windows") or ["3m", "6m", "1y", "2y", "5y", "10y"])
        rows = []
        for symbol, block in beta.items():
            if not isinstance(block, Mapping):
                continue
            corr = (correlation or {}).get(symbol) if isinstance(correlation, Mapping) else {}
            rows.append(
                [symbol]
                + [_fmt(block.get(window), digits=3) for window in windows]
                + [
                    _fmt(block.get("current_rolling_63d"), digits=3),
                    block.get("rolling_trend"),
                    _fmt((corr or {}).get("1y"), digits=3),
                ]
            )
        lines += _table(
            ["symbol", *[f"beta {window}" for window in windows], "beta 63d", "trend", "corr 1y"],
            rows,
        )
    kinematics = section.get("kinematics")
    if isinstance(kinematics, Mapping):
        lines += ["", "  Kinematics (21-day EMA log price):"]
        lines += _table(
            ["symbol", "velocity", "acceleration", "jerk"],
            [
                [
                    symbol,
                    _fmt(block.get("velocity"), digits=6),
                    _fmt(block.get("acceleration"), digits=6),
                    _fmt(block.get("jerk"), digits=6),
                ]
                for symbol, block in kinematics.items()
                if isinstance(block, Mapping)
            ],
        )
    impact = section.get("impact_weights")
    if isinstance(impact, Mapping):
        lines += ["", "  Impact weights:"]
        lines += _table(
            ["symbol", "weight", "explained variance share"],
            [
                [
                    symbol,
                    _fmt(block.get("weight"), digits=4),
                    _fmt(block.get("explained_variance_share"), digits=4),
                ]
                for symbol, block in impact.items()
                if isinstance(block, Mapping)
            ],
        )
    return lines


def _render_factors(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "factors")
    lines = _heading("Factor exposure")
    if not section:
        return lines + [f"  unavailable: {packet.get('factors_error')}"]
    lines.append(f"  model: {section.get('model')}")
    stale_days = _finite(section.get("stale_days"))
    raw_source = section.get("source")
    source: Mapping[str, Any] = raw_source if isinstance(raw_source, Mapping) else {}
    if section.get("as_of"):
        # The factor library publishes with a lag, so every window and the
        # residual end at the factor as-of, not at the packet's as-of.
        note = f"  factor data as of {section.get('as_of')}"
        if stale_days is not None and stale_days > 0:
            note += f" ({int(stale_days)} days behind the price series)"
        lines.append(note)
    elif source.get("factor_last_date"):
        lines.append(f"  factor data as of {source.get('factor_last_date')}")
    windows = section.get("windows")
    if isinstance(windows, Mapping):
        factor_names = ["MKT", "SMB", "HML", "RMW", "CMA", "MOM"]
        rows = []
        for window, block in windows.items():
            if not isinstance(block, Mapping):
                continue
            betas = block.get("betas") or {}
            rows.append(
                [
                    window,
                    block.get("start") or "-",
                    block.get("end") or "-",
                    _pct(block.get("alpha_annual")),
                    _fmt(block.get("r2"), digits=3),
                    _pct(block.get("residual_vol_annual")),
                    block.get("n"),
                ]
                + [_fmt(betas.get(name), digits=3) for name in factor_names]
            )
        lines += _table(
            ["window", "start", "end", "alpha", "R2", "resid vol", "n", *factor_names], rows
        )
    residuals = section.get("residuals")
    if isinstance(residuals, Mapping):
        as_of = residuals.get("as_of")
        lines += [
            "",
            f"  residual (20/60 sessions ending {as_of or 'unknown'}): "
            f"20d {_pct(residuals.get('last_20d_cum'))} | "
            f"60d {_pct(residuals.get('last_60d_cum'))} | "
            f"z {_fmt(residuals.get('z_score'), digits=3)}",
        ]
        if stale_days is not None and stale_days > 7:
            lines.append(
                f"  NOTE: these residuals end {int(stale_days)} days before the packet's "
                "as-of date and do not describe the most recent sessions."
            )
    return lines


def _render_regimes(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "regimes")
    lines = _heading("Regimes")
    if not section:
        return lines + [f"  unavailable: {packet.get('regimes_error')}"]
    lines.append(
        f"  trained on {section.get('trained_on')} | {section.get('n_states')} states | "
        f"{section.get('train_window_days')} day window | features "
        f"{', '.join(section.get('features') or [])}"
    )
    # `states[].volatility` is the raw vol_10d_mse feature mean (a dimensionless
    # squared fraction, order 1e-5), not a volatility — printing it under a
    # "volatility" header read as "bull volatility 0.00007". The annualised figure
    # is what belongs in this column; the feature mean keeps its own labelled one.
    lines += _table(
        [
            "id",
            "label",
            "mean daily",
            "vol (ann)",
            "vol_10d_mse feature",
            "occupancy",
            "avg duration",
        ],
        [
            [
                state.get("id"),
                state.get("label"),
                _pct(state.get("mean_daily_return"), digits=3),
                _pct(state.get("volatility_annualized"), digits=2),
                _fmt(state.get("vol_feature_mean", state.get("volatility")), digits=6),
                _fmt(state.get("occupancy"), digits=3),
                _fmt(state.get("avg_duration_days"), digits=1),
            ]
            for state in section.get("states") or []
            if isinstance(state, Mapping)
        ],
    )
    current = section.get("current") or {}
    lines += [
        "",
        f"  current: {current.get('label')} (state {current.get('state')}) for "
        f"{current.get('days_in_regime')} days | switch confidence "
        f"{_fmt(current.get('switch_confidence'), digits=3)}",
    ]
    by_regime = section.get("ticker_by_regime")
    if isinstance(by_regime, Mapping):
        lines += ["", "  Ticker behaviour by regime:"]
        lines += _table(
            ["regime", "mean daily", "std daily", "sharpe", "hit rate", "n"],
            [
                [
                    label,
                    _pct(block.get("mean_daily"), digits=3),
                    _fmt(block.get("std_daily"), digits=5),
                    _fmt(block.get("sharpe"), digits=3),
                    _fmt(block.get("hit_rate"), digits=3),
                    block.get("n"),
                ]
                for label, block in by_regime.items()
                if isinstance(block, Mapping)
            ],
        )
    return lines


def _render_entropy_spectral(packet: Mapping[str, Any]) -> list[str]:
    lines = _heading("Entropy")
    entropy = _section(packet, "entropy")
    if not entropy:
        lines.append(f"  unavailable: {packet.get('entropy_error')}")
    else:
        sigma = entropy.get("sigma_full_sample")
        lines += [
            f"  grid: {entropy.get('bin_grid')} - {entropy.get('bins')} equal-width bins over "
            f"+/-{_fmt(entropy.get('sigma_multiple'), digits=1)} full-sample sigma "
            f"(sigma {_fmt(sigma, digits=4)}); H is normalised by log2(bins).",
            "  structure = the window's returns concentrate in a few cells of that fixed "
            "grid; noise = they fill it. It is a dispersion reading, not a forecast.",
            "",
        ]
        lines += _table(
            ["window", "H", "classification", "n"],
            [
                [
                    window,
                    _fmt(block.get("H"), digits=4),
                    block.get("classification"),
                    block.get("n"),
                ]
                for window, block in (entropy.get("windows") or {}).items()
                if isinstance(block, Mapping)
            ],
        )
        backtest = entropy.get("backtest") or {}
        lines += [
            "",
            "  backtest: low-entropy win rate "
            f"{_fmt(backtest.get('low_entropy_win_rate'), digits=4)} "
            f"(n={backtest.get('n_low')}) | high-entropy "
            f"{_fmt(backtest.get('high_entropy_win_rate'), digits=4)} "
            f"(n={backtest.get('n_high')}) | edge {_fmt(backtest.get('edge'), digits=4)}",
        ]

    lines += _heading("Spectral")
    spectral = _section(packet, "spectral")
    if not spectral:
        lines.append(f"  unavailable: {packet.get('spectral_error')}")
    else:
        lines.append(
            f"  detrend: {spectral.get('detrend')} | reconstruction R2 "
            f"{_fmt(spectral.get('reconstruction_r2'), digits=4)}"
        )
        lines += _table(
            ["period (days)", "amplitude", "phase (rad)", "power share", "position", "phase frac"],
            [
                [
                    _fmt(mode.get("period_days"), digits=2),
                    _fmt(mode.get("amplitude"), digits=5),
                    _fmt(mode.get("phase_rad"), digits=4),
                    _fmt(mode.get("power_share"), digits=4),
                    mode.get("cycle_position"),
                    _fmt(mode.get("phase_fraction"), digits=3),
                ]
                for mode in spectral.get("modes") or []
                if isinstance(mode, Mapping)
            ],
        )
        projection = spectral.get("projection")
        if isinstance(projection, Mapping):
            lines += ["", "  Projection:"]
            lines += _table(
                ["horizon", "expected return", "confidence"],
                [
                    [
                        horizon,
                        _pct(block.get("expected_return")),
                        _fmt(block.get("confidence"), digits=3),
                    ]
                    for horizon, block in projection.items()
                    if isinstance(block, Mapping)
                ],
            )
    return lines


def _render_eigen(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "eigen")
    lines = _heading("Signal structure")
    if not section:
        return lines + [f"  unavailable: {packet.get('eigen_error')}"]
    pca = section.get("pca") or {}
    ratios = pca.get("explained_variance_ratio") or []
    if ratios:
        lines.append(
            "  PCA explained variance: "
            + ", ".join(f"{float(value):.4f}" for value in ratios[:8])
        )
    ranking = [row for row in section.get("signal_ranking") or [] if isinstance(row, Mapping)]
    ranked_by = next((row.get("ranked_by") for row in ranking if row.get("ranked_by")), None)
    lines += [
        "",
        "  Signal ranking"
        + (f" (ranked by {ranked_by}):" if ranked_by else " (unrankable: no correlation):"),
    ]
    if ranked_by:
        lines += _table(
            ["rank", "signal", "corr 1y", "corr 6m", "corr 3m", "fwd corr", "n 1y"],
            [
                [
                    row.get("rank"),
                    row.get("signal"),
                    _fmt(row.get("corr_1y"), digits=4),
                    _fmt(row.get("corr_6m"), digits=4),
                    _fmt(row.get("corr_3m"), digits=4),
                    _fmt(row.get("forward_corr"), digits=4),
                    row.get("n_1y", "-"),
                ]
                for row in ranking
            ],
        )
    else:
        # Every correlation is null, so any rank number would be nothing but the
        # input column order. Print the signals without one.
        lines += _table(["signal"], [[row.get("signal")] for row in ranking])
    lines += [
        "",
        "  Load-bearing test (leave one out). 'load bearing' is set on the survivor",
        "  delta - how much dropping the signal changes the weighting of the others -",
        "  because the raw delta always includes the signal's own weight twice:",
    ]
    lines += _table(
        ["signal", "raw delta", "survivor delta", "own weight", "load bearing"],
        [
            [
                row.get("signal"),
                _fmt(row.get("weight_delta_if_removed"), digits=5),
                _fmt(row.get("survivor_weight_delta"), digits=5),
                _fmt(row.get("baseline_weight"), digits=5),
                "yes" if row.get("load_bearing") else "no",
            ]
            for row in section.get("load_bearing") or []
            if isinstance(row, Mapping)
        ],
    )
    symmetry = section.get("symmetry") or {}
    lines += [
        "",
        f"  symmetry: {len(symmetry.get('broken_pairs') or [])} broken pairs, "
        f"{len(symmetry.get('gauge_invariant_pairs') or [])} gauge-invariant pairs",
    ]
    return lines


def _render_fundamentals(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "fundamentals")
    lines = _heading("Fundamentals")
    if not section:
        return lines + [f"  unavailable: {packet.get('fundamentals_error')}"]
    lines.append(f"  provider: {section.get('provider')}")
    lines += _table(
        ["period end", "FQ", "revenue", "gross", "operating", "net", "eps", "fcf", "gm", "om"],
        [
            [
                row.get("period_end"),
                f"FY{row.get('fiscal_year')}Q{row.get('fiscal_quarter')}",
                _fmt(row.get("revenue"), digits=0),
                _fmt(row.get("gross_profit"), digits=0),
                _fmt(row.get("operating_income"), digits=0),
                _fmt(row.get("net_income"), digits=0),
                _fmt(row.get("eps"), digits=2),
                _fmt(row.get("fcf"), digits=0),
                _pct(row.get("gross_margin"), digits=1),
                _pct(row.get("operating_margin"), digits=1),
            ]
            for row in section.get("quarters") or []
            if isinstance(row, Mapping)
        ],
    )
    ratios = section.get("ratios") or {}
    source = section.get("ratios_source") or {}
    lines += ["", "  Ratios:"]
    lines += _table(
        ["ratio", "value", "source"],
        [
            [key, _fmt(ratios.get(key), digits=4), source.get(key) or "-"]
            for key in (
                "pe",
                "ps",
                "pb",
                "ev_ebitda",
                "ev_ebit",
                "ev_sales",
                "debt_to_equity",
                "fcf_yield",
                "dividend_yield",
                "nav_per_share",
                "return_on_equity",
                "current_ratio",
            )
        ],
    )
    growth = section.get("growth") or {}
    lines += [
        "",
        f"  growth: revenue yoy {_pct(growth.get('revenue_yoy'))} | qoq "
        f"{_pct(growth.get('revenue_qoq'))} | net income yoy "
        f"{_pct(growth.get('net_income_yoy'))} | margins {growth.get('margin_trend')}",
    ]
    forecast = section.get("forecast") or {}
    if forecast.get("next_4q"):
        lines += ["", f"  Forecast ({forecast.get('method')}):"]
        lines += _table(
            ["FY", "FQ", "revenue", "net income", "eps", "seasonal factor"],
            [
                [
                    row.get("fiscal_year"),
                    row.get("fiscal_quarter"),
                    _fmt(row.get("revenue"), digits=0),
                    _fmt(row.get("net_income"), digits=0),
                    _fmt(row.get("eps"), digits=2),
                    _fmt(row.get("seasonal_factor"), digits=3),
                ]
                for row in forecast["next_4q"]
                if isinstance(row, Mapping)
            ],
        )
    stage = section.get("stage") or {}
    lines += ["", f"  stage: {stage.get('label')}"]
    for item in stage.get("evidence") or []:
        lines.append(f"    - {item}")
    return lines


def _render_filings(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "filings")
    lines = _heading("Filings")
    if not section:
        return lines + [f"  unavailable: {packet.get('filings_error')}"]
    lines.append(f"  CIK {section.get('cik')}")
    for row in list(section.get("ten_k") or []) + list(section.get("ten_q") or []):
        if not isinstance(row, Mapping):
            continue
        lines += [
            "",
            f"  {row.get('form')} filed {row.get('filing_date')} "
            f"(period {row.get('report_date')})",
            f"    {row.get('url')}",
        ]
        if row.get("summary"):
            lines.append(f"    {row['summary']}")
    synthesis = section.get("synthesis") or {}
    lines += ["", "  Cross-filing synthesis:"]
    for key in (
        "performance",
        "risks",
        "growth_opportunities",
        "new_business_lines",
        "operating_context",
        "capex_suppliers_customers",
    ):
        value = synthesis.get(key)
        if value:
            lines += ["", f"    {key.replace('_', ' ')}: {value}"]
    return lines


def _render_volatility(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "volatility")
    lines = _heading("Volatility")
    if not section:
        return lines + [f"  unavailable: {packet.get('volatility_error')}"]
    lines += _table(
        ["window", "annualized", "avg", "percentile", "n"],
        [
            [
                window,
                _pct(block.get("annualized")),
                _pct(block.get("avg")),
                _fmt(block.get("percentile"), digits=3),
                block.get("n"),
            ]
            for window, block in (section.get("realized") or {}).items()
            if isinstance(block, Mapping)
        ],
    )
    lines += [
        "",
        f"  vol of vol: {_pct(section.get('vol_of_vol'))} | current 21d "
        f"{_pct(section.get('realized_current_21d'))}",
    ]
    implied = section.get("implied")
    if isinstance(implied, Mapping):
        lines += [
            "",
            f"  implied: ATM {_pct(implied.get('atm_iv'))} at {implied.get('expiry')} "
            f"({implied.get('expiry_kind')}) | 25d skew {_pct(implied.get('skew_25d'))} "
            f"| strikes {implied.get('n_strikes')}",
        ]
        lines += _table(
            ["strike", "moneyness", "type", "iv", "delta"],
            [
                [
                    _fmt(point.get("strike"), digits=2),
                    _fmt(point.get("moneyness"), digits=3),
                    point.get("type"),
                    _pct(point.get("iv")),
                    _fmt(point.get("delta"), digits=3),
                ]
                for point in (implied.get("smile") or [])
                if isinstance(point, Mapping)
            ],
        )
    else:
        lines.append(f"  implied unavailable: {section.get('implied_error')}")
    return lines


def _render_levels(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "levels")
    lines = _heading("Levels")
    if not section:
        return lines + [f"  unavailable: {packet.get('levels_error')}"]
    lines += _table(
        ["price", "kind", "source", "distance"],
        [
            [
                _fmt(row.get("price"), digits=2),
                row.get("kind"),
                row.get("source"),
                _pct(row.get("distance_pct"), digits=2),
            ]
            for row in section.get("key_levels") or []
            if isinstance(row, Mapping)
        ],
    )
    torque = section.get("torque")
    if isinstance(torque, Mapping):
        lines += [
            "",
            f"  torque: score {_fmt(torque.get('total_score'), digits=2)} | "
            f"stage {torque.get('stage_label')} | {torque.get('recommendation')}",
        ]
    return lines


def _render_news(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "news")
    lines = _heading("News and policy")
    if not section:
        return lines + [f"  unavailable: {packet.get('news_error')}"]
    for item in section.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        lines += [
            "",
            f"  [{item.get('category')}] {item.get('title')}",
            f"    {item.get('source')} | {item.get('published')} | {item.get('url')}",
        ]
        if item.get("summary"):
            lines.append(f"    {item['summary']}")
    lines += ["", "  Query log:"]
    lines += _table(
        ["category", "provider", "results", "error"],
        [
            [
                row.get("category"),
                row.get("provider"),
                row.get("n_results"),
                row.get("error") or "-",
            ]
            for row in section.get("query_log") or []
            if isinstance(row, Mapping)
        ],
    )
    return lines


def _render_scenarios(packet: Mapping[str, Any]) -> list[str]:
    section = _section(packet, "scenarios")
    lines = _heading("Scenarios")
    if not section:
        return lines + [f"  unavailable: {packet.get('scenarios_error')}"]
    weights = section.get("weights") or {}
    lines += ["  Component weights:"]
    lines += _table(
        ["component", "weight"],
        [[name, _fmt(value, digits=4)] for name, value in weights.items()],
    )
    lines += _render_calibration(section)
    for case, block in (section.get("cases") or {}).items():
        if not isinstance(block, Mapping):
            continue
        lines += [
            "",
            f"  {str(case).upper()} - probability {_fmt(block.get('probability'), digits=3)}",
            f"    {block.get('narrative')}",
        ]
        lines += _table(
            ["horizon", "p10", "p50", "p90", "price p10", "price p50", "price p90"],
            [
                [
                    horizon,
                    _pct(row.get("p10")),
                    _pct(row.get("p50")),
                    _pct(row.get("p90")),
                    _fmt(row.get("price_p10"), digits=2),
                    _fmt(row.get("price_p50"), digits=2),
                    _fmt(row.get("price_p90"), digits=2),
                ]
                for horizon, row in (block.get("horizons") or {}).items()
                if isinstance(row, Mapping)
            ],
        )
    entry = section.get("entry") or {}
    timing = section.get("timing") or {}
    lines += [
        "",
        f"  entry: bargain below {_fmt(entry.get('bargain_below'), digits=2)} | fair "
        f"{_fmt(entry.get('fair_value'), digits=2)} | expensive above "
        f"{_fmt(entry.get('expensive_above'), digits=2)} | current "
        f"{_fmt(entry.get('current_price'), digits=2)}",
        f"  timing this month: {timing.get('this_month')} - {timing.get('reason')}",
    ]
    signals = section.get("watch_signals") or []
    if signals:
        lines += ["", "  Watch signals:"]
        lines += _table(
            ["symbol", "condition", "implication"],
            [
                [row.get("symbol"), row.get("condition"), row.get("implication")]
                for row in signals
                if isinstance(row, Mapping)
            ],
        )
    return lines


def _render_calibration(section: Mapping[str, Any]) -> list[str]:
    """Show how far the shrinkage moved each component, and toward what.

    Without this the entry band and the fair value read as raw model output.
    They are not: every component was pulled toward the market prior by how much
    evidence stood behind it, then clipped to what the ticker has actually done.
    """
    prior = section.get("prior") if isinstance(section.get("prior"), Mapping) else None
    components = (
        section.get("components") if isinstance(section.get("components"), Mapping) else {}
    )
    if not prior or not components:
        return []
    lines = [
        "",
        "  Calibration: each component is shrunk toward the market prior by "
        "1 - confidence (evidence x walk-forward skill x horizon evidence), then",
        "  clipped to the ticker's own [p5, p95] of rolling horizon returns.",
        f"  prior: {prior.get('source')} - "
        f"{_pct(prior.get('annualized_drift'))} a year "
        f"({_pct((prior.get('by_horizon') or {}).get('12m'))} at 12m)",
    ]
    rows: list[list[Any]] = []
    for name, component in components.items():
        shrinkage = component.get("shrinkage") if isinstance(component, Mapping) else None
        if not isinstance(shrinkage, Mapping) or not shrinkage.get("applied"):
            continue
        raw = shrinkage.get("raw_expected_return") or {}
        final = shrinkage.get("expected_return") or {}
        weight = shrinkage.get("shrink_weight") or {}
        clamped = shrinkage.get("clamped") or {}
        rows.append(
            [
                name,
                _pct(raw.get("12m")),
                _pct(final.get("12m")),
                _fmt(weight.get("12m"), digits=3),
                clamped.get("12m") or "-",
            ]
        )
    if not rows:
        return []
    lines += _table(
        ["component", "raw 12m", "shrunk 12m", "shrink w", "clamped"], rows
    )
    bounds = section.get("clamp_bounds") or {}
    twelve = bounds.get("12m") if isinstance(bounds, Mapping) else None
    if isinstance(twelve, Mapping) and twelve.get("low") is not None:
        lines.append(
            f"  12m clamp bounds: {_pct(twelve.get('low'))} to {_pct(twelve.get('high'))} "
            f"(p5/p95 of {twelve.get('n')} rolling 252-day windows)"
        )
    return lines


def _render_universe(packet: Mapping[str, Any]) -> list[str]:
    universe = packet.get("universe")
    lines = _heading("Universe")
    if not isinstance(universe, list) or not universe:
        return lines + ["  (no universe resolved)"]
    return lines + _table(
        ["symbol", "label", "role", "provider", "first", "last", "days", "note/error"],
        [
            [
                row.get("symbol"),
                row.get("label"),
                row.get("role"),
                row.get("provider"),
                row.get("first_date"),
                row.get("last_date"),
                row.get("n_days"),
                row.get("error") or row.get("note") or "-",
            ]
            for row in universe
            if isinstance(row, Mapping)
        ],
    )


def _render_meta(packet: Mapping[str, Any]) -> list[str]:
    meta = packet.get("meta")
    lines = _heading("Build report")
    if not isinstance(meta, Mapping):
        return lines + ["  (no meta block)"]
    errors = meta.get("errors") or []
    lines.append(f"  errors: {len(errors)}")
    for row in errors:
        if isinstance(row, Mapping):
            lines.append(f"    - {row.get('source')}: {row.get('error')}")
    unavailable = meta.get("unavailable") or []
    if unavailable:
        lines.append(f"  unavailable: {len(unavailable)}")
        for row in unavailable:
            if isinstance(row, Mapping):
                lines.append(f"    - {row.get('source')}: {row.get('reason')}")
    timings = meta.get("timings_ms") or {}
    if timings:
        lines += ["", "  Timings (ms):"]
        lines += _table(
            ["step", "ms"],
            [[name, _fmt(value, digits=1)] for name, value in timings.items()],
        )
    sources = packet.get("sources")
    if isinstance(sources, list) and sources:
        lines += ["", "  Sources:"]
        lines += _table(
            ["provider", "symbol/series", "url", "fetched at"],
            [
                [
                    row.get("provider"),
                    row.get("symbol") or row.get("series_id") or "-",
                    row.get("url") or "-",
                    row.get("fetched_at"),
                ]
                for row in sources
                if isinstance(row, Mapping)
            ],
        )
    lines += ["", "Research only. This is not investment advice and no order was placed."]
    return lines


def _pdf_scenarios(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Map the mixture cases onto the PDF's scenario table columns."""
    scenarios = _section(packet, "scenarios") or {}
    cases = scenarios.get("cases")
    if not isinstance(cases, Mapping):
        return []
    rows: list[dict[str, Any]] = []
    for name in ("bear", "neutral", "bull"):
        block = cases.get(name)
        if not isinstance(block, Mapping):
            continue
        horizons = block.get("horizons") or {}
        twelve = horizons.get("12m") if isinstance(horizons, Mapping) else None
        six = horizons.get("6m") if isinstance(horizons, Mapping) else None
        reference = twelve if isinstance(twelve, Mapping) else (six or {})
        probability = _finite(block.get("probability"))
        rows.append(
            {
                "name": name.title(),
                "rev_growth": _pct((reference or {}).get("p50")),
                "gm": "-",
                "eps": "-",
                "multiple": f"p{'50'} of case",
                "price": _fmt((reference or {}).get("price_p50"), digits=2),
                "notes": (
                    f"{probability:.0%} probability. "
                    if probability is not None
                    else ""
                )
                + str(block.get("narrative") or ""),
            }
        )
    return rows


def _pdf_citations(packet: Mapping[str, Any]) -> list[dict[str, Any]]:
    memo = _section(packet, "memo") or {}
    citations = memo.get("citations")
    if not isinstance(citations, list):
        return []
    return [
        {
            "label": str(row.get("claim") or row.get("id") or ""),
            "source": str(row.get("source") or ""),
            "url": str(row.get("url") or ""),
        }
        for row in citations
        if isinstance(row, Mapping)
    ]


#: A markdown ``## Heading`` at the start of a line.
_MEMO_HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)

#: Sections whose content becomes the PDF's opening "Executive Read" page.
_EXECUTIVE_SECTIONS: tuple[str, ...] = ("thesis", "recommendation")


def _memo_sections(text: str) -> dict[str, str]:
    """Split the memo's markdown into ``{heading: body}``, in document order.

    ``app.memo_pdf`` looks for an ``"Executive Read"`` key first, so the Thesis
    and Recommendation sections are merged under that name and the rest keep
    their own headings. Returns ``{}`` when the memo carries no headings, which
    leaves the template's own fallback in charge.
    """
    matches = list(_MEMO_HEADING.finditer(text or ""))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    executive: list[str] = []
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if not body:
            continue
        if title.lower() in _EXECUTIVE_SECTIONS:
            executive.append(f"### {title}\n\n{body}")
        else:
            sections[title] = body
    ordered: dict[str, str] = {}
    if executive:
        ordered["Executive Read"] = "\n\n".join(executive)
    ordered.update(sections)
    return ordered


def to_pdf(packet: Mapping[str, Any]) -> bytes:
    """Render the packet's memo to PDF bytes via :mod:`app.memo_pdf`."""
    from app.memo_pdf import MemoPdfPayload, render_memo_pdf

    memo = _section(packet, "memo") or {}
    profile = _section(packet, "profile") or {}
    scenarios = _section(packet, "scenarios") or {}
    entry = scenarios.get("entry") if isinstance(scenarios.get("entry"), Mapping) else {}
    levels = _section(packet, "levels") or {}
    torque = levels.get("torque") if isinstance(levels.get("torque"), Mapping) else {}
    recommendation = memo.get("recommendation") or {}

    text = str(memo.get("text") or "")
    if not text:
        text = (
            f"The memo section is unavailable for {packet.get('ticker')}: "
            f"{packet.get('memo_error') or 'no reason recorded'}.\n\n"
            "Every other section of the packet is available in the JSON and text exports."
        )

    targets = [
        _finite(row.get("price"))
        for row in (memo.get("exit_targets") or [])
        if isinstance(row, Mapping)
    ]
    prices = sorted(value for value in targets if value is not None)

    payload = MemoPdfPayload(
        ticker=str(packet.get("ticker") or ""),
        company_name=str(profile.get("name") or ""),
        memo_text=text,
        # Without sections the template flattens the markdown with
        # `" ".join(text.split()[:600])`, which destroys every newline and makes
        # the heading detector — which is line-based — never fire, so the first
        # content page printed literal "## Thesis" markers inside running text.
        memo_sections=_memo_sections(text) or None,
        document_title="PRISM MEMO",
        target_low_label="REASSESS BELOW",
        recommendation=str(recommendation.get("action") or ""),
        sector=str(profile.get("sector") or "") or None,
        industry=str(profile.get("industry") or "") or None,
        generated_at=str(packet.get("generated_at") or ""),
        # `stop_or_reassess` is the level at which the thesis is abandoned, not a
        # price target; it is labelled "REASSESS BELOW" above.
        target_low=_finite(memo.get("stop_or_reassess")),
        target_mid=_finite(memo.get("fair_value")) or _finite((entry or {}).get("fair_value")),
        target_high=prices[-1] if prices else None,
        current_price=_finite((entry or {}).get("current_price")),
        market_cap=_finite(profile.get("market_cap")),
        torque_score=_finite((torque or {}).get("total_score")),
        torque_stage=str((torque or {}).get("stage_label") or "") or None,
        torque_components=[
            {"name": name, **(values if isinstance(values, dict) else {})}
            for name, values in ((torque or {}).get("components") or {}).items()
        ]
        or None,
        scenarios=_pdf_scenarios(packet) or None,
        citations=_pdf_citations(packet) or None,
        catalysts=[
            str(row.get("condition") or "")
            for row in (scenarios.get("watch_signals") or [])
            if isinstance(row, Mapping)
        ]
        or None,
        kill_criteria=[
            f"Price closes below {_fmt(memo.get('stop_or_reassess'), digits=2)}, the bear "
            "case's central path at three months."
        ]
        if memo.get("stop_or_reassess") is not None
        else None,
        diligence_gaps=[
            f"{row.get('source')}: {row.get('error')}"
            for row in ((packet.get("meta") or {}).get("errors") or [])
            if isinstance(row, Mapping)
        ]
        or None,
    )
    return render_memo_pdf(payload)


def export_packet(packet: Mapping[str, Any], fmt: str) -> tuple[bytes, str, str]:
    """Return ``(body, content_type, filename)`` for one export format."""
    normalized = str(fmt or "").strip().lower()
    if normalized not in FORMATS:
        raise PrismExportError(
            f"format must be one of {', '.join(FORMATS)} (got '{fmt}')"
        )
    # The filename goes straight into a ``Content-Disposition`` header, and both
    # fields come from a stored packet whose ``as_of`` may predate validation.
    # Anything outside ``[A-Za-z0-9._-]`` (a quote, CR/LF, a path separator) is
    # replaced rather than trusted.
    ticker = _FILENAME_UNSAFE.sub("_", str(packet.get("ticker") or "packet").upper()) or "PACKET"
    as_of = _FILENAME_UNSAFE.sub("_", str(packet.get("as_of") or "latest")) or "latest"
    filename = f"prism-{ticker}-{as_of}.{normalized}"
    if normalized == "json":
        return to_json(packet).encode("utf-8"), CONTENT_TYPES["json"], filename
    if normalized == "txt":
        return to_text(packet).encode("utf-8"), CONTENT_TYPES["txt"], filename
    return to_pdf(packet), CONTENT_TYPES["pdf"], filename
