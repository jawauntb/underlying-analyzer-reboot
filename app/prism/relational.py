"""Gauge-fixed relational structure: beta, correlation, kinematics, impact.

A correlation is meaningless until you say *relative to what*. Following the
gauge-fixing idea in Brown (2026), "Gauge-Fixed Transport of Concern" — a
distinction only carries information once it is expressed in a fixed reference
frame and shown to survive transport between contexts — every comparison here is
computed in one declared frame:

``reference_frame = "excess_over_SPY_zscored"``
    Daily returns are first differenced against the reference benchmark (SPY),
    then z-scored *within the window being measured*. Levels and market-wide
    moves are the gauge freedom; subtracting the reference and rescaling fixes
    it, so a correlation computed over 3 months and one computed over 5 years
    are on the same footing and a sign flip between them is a real change in
    structure rather than a change of units.

Raw (unfixed) betas and correlations are reported alongside the gauge-fixed ones
because a portfolio still needs the raw market beta to size risk; the gauge-fixed
numbers are what the regime/symmetry analysis in ``eigen.py`` compares across
regimes.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from app.prism.contract import WINDOW_DAYS
from app.prism.data import align_series, finite, resolve_as_of

REFERENCE_FRAME = "excess_over_SPY_zscored"
DEFAULT_WINDOWS: tuple[str, ...] = ("3m", "6m", "1y", "2y", "5y", "10y")
ROLLING_BETA_DAYS = 63
KINEMATICS_SPAN = 21
MIN_WINDOW_POINTS = 20
#: Slope of the rolling-beta path per day beyond which the trend is named.
BETA_TREND_TOLERANCE = 1e-4


def daily_returns(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """Simple daily returns with the leading NaN row dropped."""
    return frame.ffill().pct_change().iloc[1:]


def gauge_fix(
    returns: pd.DataFrame,
    *,
    reference: str,
    zscore: bool = True,
) -> pd.DataFrame:
    """Express every column as excess over ``reference``, then z-score in-window.

    The reference column itself is dropped (its excess return is identically
    zero). Columns with no variance in the window are dropped rather than
    divided by zero.
    """
    if returns.empty or reference not in returns.columns:
        return pd.DataFrame(index=returns.index)
    excess = returns.drop(columns=[reference]).sub(returns[reference], axis=0)
    excess = excess.dropna(axis=1, how="all")
    if not zscore or excess.empty:
        return excess
    std = excess.std(ddof=1)
    usable = [column for column in excess.columns if finite(std.get(column)) not in (None, 0.0)]
    if not usable:
        return pd.DataFrame(index=excess.index)
    trimmed = excess[usable]
    return (trimmed - trimmed.mean()) / trimmed.std(ddof=1)


def ols_beta(target: pd.Series, benchmark: pd.Series) -> tuple[float | None, float | None]:
    """``(beta, alpha)`` from a two-variable OLS fit, or ``(None, None)``."""
    paired = pd.concat([target, benchmark], axis=1).dropna()
    if len(paired) < 3:
        return None, None
    y = paired.iloc[:, 0].to_numpy(dtype="float64")
    x = paired.iloc[:, 1].to_numpy(dtype="float64")
    variance = float(np.var(x, ddof=1))
    if not np.isfinite(variance) or variance == 0.0:
        return None, None
    beta = float(np.cov(y, x, ddof=1)[0, 1] / variance)
    alpha = float(y.mean() - beta * x.mean())
    return finite(beta), finite(alpha)


def rolling_beta(
    target: pd.Series,
    benchmark: pd.Series,
    *,
    window: int = ROLLING_BETA_DAYS,
) -> pd.Series:
    """Rolling OLS beta of ``target`` on ``benchmark``."""
    paired = pd.concat([target, benchmark], axis=1).dropna()
    if len(paired) < window + 1:
        return pd.Series(dtype="float64")
    y = paired.iloc[:, 0]
    x = paired.iloc[:, 1]
    covariance = y.rolling(window).cov(x)
    variance = x.rolling(window).var()
    beta = covariance / variance.replace(0.0, np.nan)
    return beta.replace([np.inf, -np.inf], np.nan).dropna()


def trend_label(series: pd.Series, *, tolerance: float = BETA_TREND_TOLERANCE) -> str:
    """"rising" / "falling" / "flat" from the slope of a short path."""
    clean = series.dropna()
    if len(clean) < 5:
        return "flat"
    xs = np.arange(len(clean), dtype="float64")
    slope = float(np.polyfit(xs, clean.to_numpy(dtype="float64"), 1)[0])
    if slope > tolerance:
        return "rising"
    if slope < -tolerance:
        return "falling"
    return "flat"


def _window_returns(
    frame: pd.DataFrame,
    window: str,
    *,
    as_of: date | str | None = None,
) -> pd.DataFrame:
    days = WINDOW_DAYS.get(window)
    if days is None:
        raise ValueError(f"unknown window: {window}")
    trimmed = frame
    if as_of is not None:
        trimmed = trimmed[trimmed.index <= pd.Timestamp(resolve_as_of(as_of))]
    if len(trimmed) > days + 1:
        trimmed = trimmed.iloc[-(days + 1) :]
    returns = trimmed.ffill().pct_change().iloc[1:]
    return returns.replace([np.inf, -np.inf], np.nan)


def beta_table(
    ticker: str,
    frame: pd.DataFrame,
    *,
    windows: tuple[str, ...] = DEFAULT_WINDOWS,
    as_of: date | str | None = None,
    rolling_window: int = ROLLING_BETA_DAYS,
) -> dict[str, dict[str, Any]]:
    """Per-benchmark raw beta of ``ticker`` over each window, plus the rolling trend.

    Betas stay in raw return space on purpose: sizing risk needs the actual
    sensitivity to the benchmark, not a z-scored one. The gauge-fixed frame is
    applied to correlations, where the shared market factor would otherwise
    dominate every pair.
    """
    if ticker not in frame.columns:
        return {}
    table: dict[str, dict[str, Any]] = {}
    full_returns = frame.ffill().pct_change().iloc[1:]
    for symbol in frame.columns:
        if symbol == ticker:
            continue
        entry: dict[str, Any] = {}
        for window in windows:
            returns = _window_returns(frame[[ticker, symbol]], window, as_of=as_of).dropna()
            if len(returns) < MIN_WINDOW_POINTS:
                entry[window] = None
                continue
            beta, _alpha = ols_beta(returns[ticker], returns[symbol])
            entry[window] = beta
        rolling = rolling_beta(
            full_returns[ticker], full_returns[symbol], window=rolling_window
        )
        entry["current_rolling_63d"] = finite(rolling.iloc[-1]) if len(rolling) else None
        entry["rolling_trend"] = (
            trend_label(rolling.iloc[-rolling_window:]) if len(rolling) else "flat"
        )
        entry["rolling_window_days"] = int(rolling_window)
        entry["n_rolling"] = int(len(rolling))
        table[symbol] = entry
    return table


def correlation_table(
    ticker: str,
    frame: pd.DataFrame,
    *,
    windows: tuple[str, ...] = DEFAULT_WINDOWS,
    reference: str,
    as_of: date | str | None = None,
    rolling_window: int = ROLLING_BETA_DAYS,
    gauge_fixed: bool = False,
) -> dict[str, dict[str, Any]]:
    """Per-benchmark correlation of ``ticker`` over each window.

    With ``gauge_fixed=True`` both legs are first expressed as excess over the
    reference and z-scored inside the window, which removes the shared market
    factor so what is left is the pair's own co-movement.

    Every entry carries a ``frame`` key ("raw" or
    ``"excess_over_<reference>_zscored"``) so the two tables can never be read as
    the same quantity.
    """
    if ticker not in frame.columns:
        return {}
    table: dict[str, dict[str, Any]] = {}
    full_returns = frame.ffill().pct_change().iloc[1:]
    # The rolling leg has to live in the same reference frame as the windowed
    # columns beside it. Computing it on raw returns while the window columns were
    # gauge-fixed put a +0.10 "current 63d" next to a -0.40 gauge-fixed 3m — the
    # same 63 sessions with opposite signs and nothing saying why. Correlation is
    # invariant to the per-window z-scoring, so excess-over-reference is enough.
    rolling_source = full_returns
    frame_label = "raw"
    if gauge_fixed:
        rolling_source = (
            full_returns.sub(full_returns[reference], axis=0)
            if reference in full_returns.columns
            else full_returns.iloc[:, :0]
        )
        frame_label = f"excess_over_{reference}_zscored"
    for symbol in frame.columns:
        if symbol == ticker:
            continue
        entry: dict[str, Any] = {}
        for window in windows:
            columns = [ticker, symbol] + ([reference] if gauge_fixed else [])
            available = [column for column in dict.fromkeys(columns) if column in frame.columns]
            returns = _window_returns(frame[available], window, as_of=as_of).dropna()
            if gauge_fixed:
                if reference not in returns.columns:
                    entry[window] = None
                    continue
                returns = gauge_fix(returns, reference=reference)
            if (
                len(returns) < MIN_WINDOW_POINTS
                or ticker not in returns.columns
                or symbol not in returns.columns
            ):
                entry[window] = None
                continue
            entry[window] = finite(returns[ticker].corr(returns[symbol]))
        entry["frame"] = frame_label
        if ticker in rolling_source.columns and symbol in rolling_source.columns:
            rolling = (
                rolling_source[ticker]
                .rolling(rolling_window)
                .corr(rolling_source[symbol])
                .replace([np.inf, -np.inf], np.nan)
                .dropna()
            )
        else:
            rolling = pd.Series(dtype="float64")
        entry["current_rolling_63d"] = finite(rolling.iloc[-1]) if len(rolling) else None
        entry["rolling_trend"] = (
            trend_label(rolling.iloc[-rolling_window:], tolerance=1e-4) if len(rolling) else "flat"
        )
        entry["rolling_window_days"] = int(rolling_window)
        table[symbol] = entry
    return table


def kinematics(
    prices: pd.Series,
    *,
    span: int = KINEMATICS_SPAN,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """Velocity / acceleration / jerk of an EMA-smoothed log price.

    ``velocity`` is the mean daily log-return over the last ``span`` days of the
    smoothed path, ``acceleration`` the change in velocity from the previous
    ``span``-day block, and ``jerk`` the change in acceleration — the three terms
    a memo needs to distinguish "up and speeding up" from "up but rolling over".
    """
    clean = prices.dropna()
    if as_of is not None:
        clean = clean[clean.index <= pd.Timestamp(resolve_as_of(as_of))]
    clean = clean[clean > 0]
    empty = {
        "velocity": None,
        "acceleration": None,
        "jerk": None,
        "window_days": int(span),
        "n": int(len(clean)),
    }
    if len(clean) < span * 4 + 2:
        return empty
    smoothed = np.log(clean).ewm(span=span, adjust=False).mean()
    deltas = smoothed.diff().dropna()
    if len(deltas) < span * 4:
        return empty
    blocks = [deltas.iloc[-span * (index + 1) : len(deltas) - span * index] for index in range(4)]
    velocities = [float(block.mean()) for block in blocks]
    velocity = velocities[0]
    acceleration = velocities[0] - velocities[1]
    previous_acceleration = velocities[1] - velocities[2]
    return {
        "velocity": finite(velocity),
        "acceleration": finite(acceleration),
        "jerk": finite(acceleration - previous_acceleration),
        "window_days": int(span),
        "n": int(len(clean)),
    }


def cosine_similarity_matrix(returns: pd.DataFrame) -> dict[str, Any]:
    """Cosine similarity of every pair of return vectors in the window."""
    if returns.empty or returns.shape[1] < 2:
        return {"symbols": list(returns.columns), "matrix": []}
    matrix = returns.to_numpy(dtype="float64")
    norms = np.linalg.norm(matrix, axis=0)
    safe = np.where(norms == 0, np.nan, norms)
    normalised = matrix / safe
    similarity = normalised.T @ normalised
    return {
        "symbols": list(returns.columns),
        "matrix": [[finite(value) for value in row] for row in similarity],
        "n": int(returns.shape[0]),
    }


def covariance_matrix(returns: pd.DataFrame, *, annualize: bool = True) -> dict[str, Any]:
    """Covariance of the aligned window returns (annualised by default)."""
    if returns.empty or returns.shape[1] < 2:
        return {"symbols": list(returns.columns), "matrix": []}
    covariance = returns.cov(ddof=1)
    scale = 252.0 if annualize else 1.0
    return {
        "symbols": list(covariance.columns),
        "matrix": [[finite(value * scale) for value in row] for row in covariance.to_numpy()],
        "annualized": bool(annualize),
        "n": int(returns.shape[0]),
    }


def impact_weights(
    ticker: str,
    returns: pd.DataFrame,
    *,
    top_n: int = 8,
) -> dict[str, dict[str, float | None]]:
    """How much of the ticker's variance each benchmark explains, normalised.

    ``explained_variance_share`` is the univariate R² of the ticker's returns on
    that benchmark; ``weight`` is that share renormalised across the retained
    benchmarks so the weights sum to 1 and can be used as a mixture.
    """
    if ticker not in returns.columns or returns.shape[1] < 2:
        return {}
    target = returns[ticker]
    shares: dict[str, float] = {}
    for symbol in returns.columns:
        if symbol == ticker:
            continue
        correlation = finite(target.corr(returns[symbol]))
        if correlation is None:
            continue
        shares[symbol] = float(correlation) ** 2
    if not shares:
        return {}
    ranked = sorted(shares.items(), key=lambda item: item[1], reverse=True)[: max(1, top_n)]
    total = sum(value for _, value in ranked)
    return {
        symbol: {
            "weight": finite(value / total) if total else None,
            "explained_variance_share": finite(value),
        }
        for symbol, value in ranked
    }


def relative_moving_average(
    ticker: str,
    frame: pd.DataFrame,
    *,
    window: str = "1y",
    average_days: int = 50,
    as_of: date | str | None = None,
) -> dict[str, Any]:
    """A synthetic benchmark weighted by explained variance, and the ticker vs it.

    The composite is a variance-weighted basket of the benchmarks that actually
    move with the ticker; ``value`` is the ticker's ``average_days`` moving-average
    ratio minus the composite's, so a positive number means the ticker is above
    its own trend by more than its peer basket is above theirs.
    """
    empty: dict[str, Any] = {
        "value": None,
        "signal": "unknown",
        "window": window,
        "average_days": int(average_days),
        "components": {},
    }
    if ticker not in frame.columns:
        return empty
    returns = _window_returns(frame, window, as_of=as_of).dropna(axis=1, how="all").dropna()
    if len(returns) < MIN_WINDOW_POINTS:
        return empty
    weights = impact_weights(ticker, returns)
    if not weights:
        return empty
    components: dict[str, float] = {}
    for symbol, entry in weights.items():
        weight = entry.get("weight")
        if weight is not None:
            components[symbol] = float(weight)
    if not components:
        return empty
    prices = frame[[ticker, *components]].dropna()
    if len(prices) < average_days + 1:
        return empty
    composite_returns = pd.Series(0.0, index=prices.index)
    for symbol, weight in components.items():
        composite_returns = composite_returns.add(
            prices[symbol].pct_change() * weight, fill_value=0.0
        )
    composite = (1.0 + composite_returns.fillna(0.0)).cumprod()
    ticker_average = prices[ticker].rolling(average_days).mean()
    ticker_ratio = float(prices[ticker].iloc[-1] / ticker_average.iloc[-1])
    composite_ratio = float(composite.iloc[-1] / composite.rolling(average_days).mean().iloc[-1])
    value = ticker_ratio - composite_ratio
    if value > 0.02:
        signal = "leading"
    elif value < -0.02:
        signal = "lagging"
    else:
        signal = "in_line"
    return {
        "value": finite(value),
        "signal": signal,
        "window": window,
        "average_days": int(average_days),
        "ticker_ratio": finite(ticker_ratio),
        "composite_ratio": finite(composite_ratio),
        "components": {symbol: finite(weight) for symbol, weight in components.items()},
    }


def restrict_to_trading_days(
    frame: pd.DataFrame, ticker: str, reference: str
) -> pd.DataFrame:
    """Drop rows that are not US equity trading days.

    ``X:BTCUSD`` quotes seven days a week, so an outer join of the universe adds
    roughly a hundred weekend rows per traded year. Left in, a window labelled
    "1y = 252 rows" would cover only about 218 actual sessions and every equity
    would show a manufactured zero return on Saturdays and Sundays. Reindexing on
    the sessions the reference and the ticker actually traded keeps the window
    lengths honest; the seven-day series are simply carried onto those sessions
    by the forward fill in :func:`_window_returns`.
    """
    sessions: pd.Index | None = None
    for column in (reference, ticker):
        if column in frame.columns:
            observed = frame[column].dropna().index
            if len(observed):
                sessions = observed if sessions is None else sessions.union(observed)
    if sessions is None or not len(sessions):
        return frame
    return frame.reindex(sessions.sort_values())


def build_relational_section(
    ticker: str,
    series_map: dict[str, pd.Series],
    *,
    reference: str = "SPY",
    windows: tuple[str, ...] = DEFAULT_WINDOWS,
    as_of: date | str | None = None,
    kinematics_symbols: tuple[str, ...] | None = None,
    matrix_window: str = "1y",
) -> dict[str, Any]:
    """Build ``packet["relational"]`` from aligned daily closes.

    ``series_map`` must contain ``ticker``; ``reference`` (SPY) is required for
    the gauge-fixed frame and, when it is missing, the gauge-fixed correlations
    are omitted with a stated reason rather than silently computed in a different
    frame.
    """
    symbol = str(ticker).strip().upper()
    frame = align_series(series_map, how="outer")
    if symbol not in frame.columns:
        raise ValueError(f"{symbol} is missing from the aligned frame")
    frame = restrict_to_trading_days(frame, symbol, reference)
    end = resolve_as_of(as_of)
    has_reference = reference in frame.columns

    matrix_returns = _window_returns(frame, matrix_window, as_of=end).dropna(
        axis=1, how="all"
    ).dropna()
    kinematics_targets = kinematics_symbols or tuple(
        candidate
        for candidate in (symbol, reference, "QQQ", "IWM")
        if candidate in frame.columns
    )

    section: dict[str, Any] = {
        "reference_frame": REFERENCE_FRAME if has_reference else "raw_returns",
        "reference_symbol": reference if has_reference else None,
        "reference_note": (
            "Comparisons are expressed as excess over the reference and z-scored "
            "within each window so windows of different length are on one footing."
            if has_reference
            else f"{reference} history was unavailable; correlations are raw daily returns."
        ),
        "as_of": end.isoformat(),
        "windows": list(windows),
        "symbols": list(frame.columns),
        "beta": beta_table(symbol, frame, windows=windows, as_of=end),
        "correlation": correlation_table(
            symbol, frame, windows=windows, reference=reference, as_of=end
        ),
        "correlation_gauge_fixed": (
            correlation_table(
                symbol,
                frame,
                windows=windows,
                reference=reference,
                as_of=end,
                gauge_fixed=True,
            )
            if has_reference
            else {}
        ),
        "kinematics": {
            candidate: kinematics(frame[candidate], as_of=end)
            for candidate in kinematics_targets
        },
        "cosine_similarity": cosine_similarity_matrix(matrix_returns),
        "covariance": covariance_matrix(matrix_returns),
        "relative_moving_average": relative_moving_average(
            symbol, frame, window="1y", as_of=end
        ),
        "impact_weights": impact_weights(symbol, matrix_returns),
        "matrix_window": matrix_window,
    }
    return section


def top_impact_symbols(section: dict[str, Any], *, limit: int = 5) -> list[str]:
    """The benchmarks carrying the most of the ticker's variance, best first."""
    weights = section.get("impact_weights")
    if not isinstance(weights, dict):
        return []
    ranked = sorted(
        (
            (symbol, float(entry.get("weight") or 0.0))
            for symbol, entry in weights.items()
            if isinstance(entry, dict)
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return [symbol for symbol, _ in ranked[: max(1, limit)]]
